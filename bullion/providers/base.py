"""Provider contracts and the shapes every feed is normalised into.

Spec section 3 names a dozen candidate sources across four kinds of data. The
thing that keeps that from becoming twelve special cases in the scoring layer is
this module: every feed, paid or free, JSON or scraped CSV, is normalised into one
of five dataclasses below, and the scorers never learn which provider produced
what.

Two rules that exist because of how these feeds fail in practice:

* **Never trust a single price source** (spec section 3.1). Feeds go stale without
  erroring -- returning yesterday's close all morning is far more common than
  returning an HTTP 500 -- so ``crossvalidate`` compares two and reports a
  disagreement as a data-quality fact that lands in the confidence score.
* **A provider that cannot answer raises.** It does not return zeros, an empty
  list, or a neutral score. The fusion engine distinguishes "no opinion" from "no
  data" and can only do that if the provider is honest about which one happened.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

from ..series import Series, percentile_rank, stdev


class ProviderError(RuntimeError):
    """Base class for every ingestion failure, so callers can degrade per-layer."""


class NotConfigured(ProviderError):
    """No credential for this provider. Raised at construction, not at call time."""


class RateLimited(ProviderError):
    """The provider's quota is exhausted, or our own budget for it is.

    Carried separately from a generic error because the correct response differs:
    a rate limit means fall back to the redundant feed and keep the cached value,
    not retry.
    """

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class StaleData(ProviderError):
    """The provider answered, but with data too old to score."""


@dataclass(frozen=True, slots=True)
class Quote:
    """A spot price at a moment, with the spread when the provider gives one."""

    asset: str
    price: float
    ts: datetime
    source: str
    bid: float | None = None
    ask: float | None = None

    @property
    def spread_bp(self) -> float | None:
        if self.bid is None or self.ask is None or not self.price:
            return None
        return 10_000.0 * (self.ask - self.bid) / self.price


@dataclass(frozen=True, slots=True)
class Headline:
    """One news item, before any sentiment work has been done on it.

    ``provider_sentiment`` is whatever the feed supplied, normalised to -1..+1 and
    left as None when the feed has no view. It is kept distinct from the score the
    LLM pass assigns, because the two disagree often enough to be worth
    inspecting, and because conflating them makes the sentiment layer impossible
    to debug.
    """

    id: str
    ts: datetime
    title: str
    source: str
    provider: str
    url: str | None = None
    summary: str | None = None
    provider_sentiment: float | None = None
    assets: tuple[str, ...] = ()

    def age_hours(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.ts).total_seconds() / 3600.0)


@dataclass(frozen=True, slots=True)
class MacroPoint:
    day: date
    value: float


@dataclass(frozen=True)
class MacroSeries:
    """A macro time series (real yields, DXY, CPI), oldest point first.

    Levels are almost never what matters for gold -- a 2% real yield is bullish or
    bearish depending entirely on where it was last month -- so the helpers here
    are about change and position in a distribution, not the latest print.
    """

    series_id: str
    label: str
    points: tuple[MacroPoint, ...]
    units: str = ""
    source: str = "unknown"

    @property
    def latest(self) -> MacroPoint | None:
        return self.points[-1] if self.points else None

    @property
    def values(self) -> list[float]:
        return [p.value for p in self.points]

    def change(self, periods: int) -> float | None:
        """Absolute change over the last ``periods`` observations."""
        if len(self.points) <= periods:
            return None
        return self.points[-1].value - self.points[-1 - periods].value

    def pct_change(self, periods: int) -> float | None:
        if len(self.points) <= periods:
            return None
        base = self.points[-1 - periods].value
        if not base:
            return None
        return (self.points[-1].value - base) / abs(base)

    def zscore_of_change(self, periods: int, window: int = 120) -> float | None:
        """How unusual the recent move is, in standard deviations.

        This is the form the macro scorer wants: a 10bp fall in real yields is a
        shrug in a volatile month and a regime signal in a quiet one, and only the
        z-score distinguishes them.
        """
        if len(self.points) <= periods + 5:
            return None
        values = self.values
        changes = [
            values[i] - values[i - periods] for i in range(periods, len(values))
        ][-window:]
        if len(changes) < 10:
            return None
        spread = stdev(changes)
        if spread == 0:
            return 0.0
        return (values[-1] - values[-1 - periods]) / spread

    def age_days(self, today: date | None = None) -> float:
        if not self.points:
            return math.inf
        today = today or datetime.now(timezone.utc).date()
        return (today - self.points[-1].day).days


@dataclass(frozen=True)
class CotReport:
    """One weekly CFTC Commitment of Traders row for a metal.

    Non-commercial (large speculator) positioning is the useful part: commercials
    are mostly hedging producers and their net position says more about mine
    output than about price. Spec section 3.3 calls this the signal most retail
    apps ignore, and the reason it is worth having is that it is the only one of
    the four layers that is *contrarian at extremes* -- the same reading is
    confirming on the way up and dangerous when crowded.
    """

    asset: str
    report_date: date
    noncomm_long: float
    noncomm_short: float
    open_interest: float
    source: str = "CFTC"

    @property
    def net(self) -> float:
        return self.noncomm_long - self.noncomm_short

    @property
    def net_pct_oi(self) -> float | None:
        if not self.open_interest:
            return None
        return self.net / self.open_interest

    @property
    def long_share(self) -> float | None:
        total = self.noncomm_long + self.noncomm_short
        if not total:
            return None
        return self.noncomm_long / total


@dataclass(frozen=True)
class CotHistory:
    """A run of COT reports for one asset, oldest first."""

    asset: str
    reports: tuple[CotReport, ...]

    @property
    def latest(self) -> CotReport | None:
        return self.reports[-1] if self.reports else None

    def net_series(self) -> list[float]:
        return [r.net for r in self.reports]

    def crowding_percentile(self, window: int = 156) -> float | None:
        """Where current net positioning sits in its own three-year distribution.

        Three years (156 weekly reports) rather than a longer window on purpose:
        the structural level of speculative length in gold futures has shifted
        with ETF and central-bank demand, so a 2011 comparison is a comparison
        against a different market.
        """
        if len(self.reports) < 26:
            return None
        history = self.net_series()[-window:]
        return percentile_rank(history, history[-1])

    def net_change(self, weeks: int = 4) -> float | None:
        if len(self.reports) <= weeks:
            return None
        return self.reports[-1].net - self.reports[-1 - weeks].net

    def age_days(self, today: date | None = None) -> float:
        latest = self.latest
        if latest is None:
            return math.inf
        today = today or datetime.now(timezone.utc).date()
        return (today - latest.report_date).days


@dataclass(frozen=True)
class EconomicEvent:
    """A scheduled macro event. Drives the EVENT regime and the timeline strip."""

    name: str
    ts: datetime
    kind: str  # "fomc" | "cpi" | "nfp" | "other"
    importance: str = "high"

    def hours_until(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (self.ts - now).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class PriceProvider(Protocol):
    """A source of OHLCV bars and spot quotes for XAU/XAG."""

    name: str

    def candles(self, asset: str, interval: str = "1day", limit: int = 400) -> Series: ...

    def quote(self, asset: str) -> Quote: ...


@runtime_checkable
class MacroProvider(Protocol):
    name: str

    def series(self, series_id: str, observations: int = 400) -> MacroSeries: ...


@runtime_checkable
class NewsProvider(Protocol):
    name: str

    def headlines(self, since: datetime, limit: int = 50) -> list[Headline]: ...


@runtime_checkable
class PositioningProvider(Protocol):
    name: str

    def history(self, asset: str, weeks: int = 156) -> CotHistory: ...


@dataclass
class LayerData:
    """Everything ingested for one asset at one moment, before scoring.

    Assembled by ``pipeline.collect`` and handed to the four scorers. Holding the
    failures alongside the data (rather than logging them and moving on) is what
    lets confidence reflect data coverage and lets the dashboard show a user
    *which* layer is dark rather than silently showing three bars where there
    should be four.
    """

    asset: str
    series: Series | None = None
    backup_quote: Quote | None = None
    macro: dict[str, MacroSeries] = field(default_factory=dict)
    headlines: list[Headline] = field(default_factory=list)
    cot: CotHistory | None = None
    events: list[EconomicEvent] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    # Set when any part of the data came from the synthetic generator. Carried all
    # the way to the API payload so a demo can never be mistaken for live data.
    synthetic: bool = False
    # The indicator set computed from ``series`` during scoring, parked here so the
    # cycle can persist it without recomputing. Stored rather than re-derived because
    # a historical signal has to be displayable with exactly the numbers it was made
    # on -- recomputing for the UI is how a dashboard ends up disagreeing with its own
    # signal after a library change. Typed loosely to avoid an import cycle.
    indicators: Any = None

    def note_failure(self, layer: str, error: BaseException | str) -> None:
        self.failures[layer] = str(error)

    def upcoming_event(self, within_hours: float = 72.0) -> EconomicEvent | None:
        soon = [
            event
            for event in self.events
            if 0 <= event.hours_until() <= within_hours and event.importance == "high"
        ]
        return min(soon, key=lambda event: event.hours_until()) if soon else None


def recency_weight(age_hours: float, half_life_hours: float = 18.0) -> float:
    """Exponential decay weight for a news item or a macro print.

    An 18-hour half-life for headlines is a deliberate choice: gold reacts to news
    within minutes and has usually finished reacting within a session, so a
    two-day-old headline should be barely visible in today's score rather than
    quietly propping it up.
    """
    if age_hours <= 0:
        return 1.0
    return 0.5 ** (age_hours / half_life_hours)


def freshness(age_seconds: float, expected_seconds: float) -> float:
    """Data-freshness multiplier in 0..1, 1.0 while data is on time.

    Degrades linearly to 0 at four times the expected interval rather than falling
    off a cliff, because the common case is a feed that is late, not dead, and a
    step function there makes confidence flap.
    """
    if expected_seconds <= 0:
        return 1.0
    overdue = age_seconds / expected_seconds
    if overdue <= 1.0:
        return 1.0
    return max(0.0, 1.0 - (overdue - 1.0) / 3.0)


def next_fomc_dates(after: datetime | None = None, count: int = 4) -> list[EconomicEvent]:
    """Scheduled FOMC decision dates, from the published calendar.

    Hardcoded because the Fed publishes the year's calendar in advance and it does
    not move; scraping it weekly for data that changes once a year is the wrong
    trade. ``pipeline`` treats an empty or exhausted list as "no event regime"
    rather than an error, so a stale table degrades to the RANGE/TREND weighting
    instead of breaking ingestion -- but refresh it each January.
    """
    # 2026 FOMC decision days (second day of each two-day meeting), 19:00 UTC.
    scheduled = [
        date(2026, 1, 28),
        date(2026, 3, 18),
        date(2026, 4, 29),
        date(2026, 6, 17),
        date(2026, 7, 29),
        date(2026, 9, 16),
        date(2026, 11, 4),
        date(2026, 12, 16),
    ]
    now = after or datetime.now(timezone.utc)
    out: list[EconomicEvent] = []
    for day in scheduled:
        ts = datetime(day.year, day.month, day.day, 19, 0, tzinfo=timezone.utc)
        if ts >= now - timedelta(hours=6):
            out.append(EconomicEvent(name="FOMC decision", ts=ts, kind="fomc"))
        if len(out) >= count:
            break
    return out


def pick_first(providers: Sequence[object], kind: str) -> object:
    """First provider in preference order, or raise a message naming the fix."""
    for provider in providers:
        if provider is not None:
            return provider
    raise NotConfigured(f"no {kind} provider configured")
