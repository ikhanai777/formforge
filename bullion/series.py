"""OHLCV containers, on the standard library only.

No numpy, and the reason is not minimalism for its own sake. A signal engine
works on a few thousand bars per asset per recompute; the maths is O(n) and runs
in microseconds either way, so the only thing a numeric dependency buys here is a
build step on the deploy target and a wheel to pin. Spec section 8 picks
`pandas-ta` for exactly that reason -- no C build -- and dropping to stdlib takes
the same argument one step further: the indicator code in ``indicators.py`` is
then readable top to bottom by anyone auditing a signal, which is the product's
core claim about itself.

Bars are **closed bars only**. A forming bar's high, low and close all move, so
scoring one produces a signal that changes for reasons that have nothing to do
with the market -- and then flips back. ``Series.closed()`` is what the scoring
layers are given.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Candle:
    """One OHLCV bar, timestamped at its *open*, always tz-aware UTC."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("candle timestamps must be timezone-aware UTC")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(
                f"incoherent bar at {self.ts.isoformat()}: "
                f"o={self.open} h={self.high} l={self.low} c={self.close}"
            )
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite price, got {value!r}")

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    def as_dict(self) -> dict[str, float | str]:
        return {
            "ts": self.ts.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class Series:
    """An ordered, de-duplicated bar series for one asset at one interval.

    Constructed through ``Series.of`` so that the ordering and duplicate-timestamp
    guarantees hold everywhere. Providers disagree about both -- GoldAPI returns
    newest-first, Twelve Data returns newest-first with the forming bar included,
    and a retried request can deliver the same bar twice -- and an indicator fed a
    series in the wrong order returns a number rather than an error, which is the
    worst possible failure mode.
    """

    asset: str
    interval: str
    candles: tuple[Candle, ...]
    source: str = "unknown"

    @staticmethod
    def of(
        asset: str,
        interval: str,
        candles: Iterable[Candle],
        source: str = "unknown",
    ) -> Series:
        by_ts: dict[datetime, Candle] = {}
        for candle in candles:
            # Last write wins: a re-fetched bar is a corrected bar.
            by_ts[candle.ts] = candle
        ordered = tuple(by_ts[ts] for ts in sorted(by_ts))
        return Series(asset=asset, interval=interval, candles=ordered, source=source)

    def __len__(self) -> int:
        return len(self.candles)

    def __iter__(self) -> Iterator[Candle]:
        return iter(self.candles)

    def __getitem__(self, index: int) -> Candle:
        return self.candles[index]

    @property
    def opens(self) -> list[float]:
        return [c.open for c in self.candles]

    @property
    def highs(self) -> list[float]:
        return [c.high for c in self.candles]

    @property
    def lows(self) -> list[float]:
        return [c.low for c in self.candles]

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.candles]

    @property
    def volumes(self) -> list[float]:
        return [c.volume for c in self.candles]

    @property
    def timestamps(self) -> list[datetime]:
        return [c.ts for c in self.candles]

    @property
    def last(self) -> Candle:
        if not self.candles:
            raise ValueError(f"no candles for {self.asset} {self.interval}")
        return self.candles[-1]

    @property
    def bar_seconds(self) -> int:
        """Nominal bar length, parsed from the interval string."""
        return interval_seconds(self.interval)

    def closed(self, now: datetime | None = None) -> Series:
        """Drop a bar whose close is still in the future.

        Providers routinely include the forming bar. Scoring it means the signal
        moves as the bar fills and then snaps back when the next one opens.
        """
        now = now or utcnow()
        cutoff = now - timedelta(seconds=self.bar_seconds)
        kept = tuple(c for c in self.candles if c.ts <= cutoff)
        return Series(self.asset, self.interval, kept, self.source)

    def tail(self, n: int) -> Series:
        kept = self.candles[-n:] if n > 0 else ()
        return Series(self.asset, self.interval, kept, self.source)

    def slice_until(self, ts: datetime) -> Series:
        """Bars at or before ``ts``. The backtester's only window into history.

        Every lookahead bug in a backtest is a slice that included one bar too
        many, so the backtester is given this rather than an index.
        """
        kept = tuple(c for c in self.candles if c.ts <= ts)
        return Series(self.asset, self.interval, kept, self.source)

    def after(self, ts: datetime) -> Series:
        kept = tuple(c for c in self.candles if c.ts > ts)
        return Series(self.asset, self.interval, kept, self.source)

    def age_seconds(self, now: datetime | None = None) -> float:
        """Seconds since the last bar closed. Feeds the freshness penalty."""
        if not self.candles:
            return float("inf")
        now = now or utcnow()
        close_time = self.last.ts + timedelta(seconds=self.bar_seconds)
        return max(0.0, (now - close_time).total_seconds())

    def gaps(self) -> list[tuple[datetime, datetime]]:
        """Missing-bar spans, for data-quality reporting.

        Weekends and holidays make this noisy for a daily series, so callers
        treat it as a report rather than an alarm; what it is genuinely good at
        is catching a provider that silently truncates a backfill.
        """
        step = self.bar_seconds
        found: list[tuple[datetime, datetime]] = []
        for prev, curr in zip(self.candles, self.candles[1:]):
            expected = prev.ts + timedelta(seconds=step)
            if curr.ts > expected + timedelta(seconds=step * 0.5):
                found.append((expected, curr.ts))
        return found


_INTERVAL_SECONDS: dict[str, int] = {
    "1min": 60,
    "5min": 300,
    "15min": 900,
    "30min": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "1day": 86400,
    "1week": 604800,
}


def interval_seconds(interval: str) -> int:
    try:
        return _INTERVAL_SECONDS[interval]
    except KeyError:
        raise ValueError(
            f"unknown interval {interval!r}; known: {', '.join(sorted(_INTERVAL_SECONDS))}"
        ) from None


def resample(series: Series, interval: str) -> Series:
    """Aggregate a series up to a longer interval.

    Used so a single ingestion of hourly bars can feed both the intraday and the
    swing timeframe (spec section 9's Pro tier) without a second API call against
    a 500-request monthly budget.
    """
    target = interval_seconds(interval)
    source = series.bar_seconds
    if target < source:
        raise ValueError(f"cannot resample {series.interval} up to {interval}")
    if target == source:
        return Series(series.asset, interval, series.candles, series.source)

    buckets: dict[int, list[Candle]] = {}
    for candle in series.candles:
        key = int(candle.ts.timestamp()) // target
        buckets.setdefault(key, []).append(candle)

    out: list[Candle] = []
    for key in sorted(buckets):
        group = buckets[key]
        out.append(
            Candle(
                ts=datetime.fromtimestamp(key * target, tz=timezone.utc),
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
            )
        )
    return Series.of(series.asset, interval, out, series.source)


def returns(values: Sequence[float]) -> list[float]:
    """Simple period-over-period returns; first element is 0.0."""
    out = [0.0]
    for prev, curr in zip(values, values[1:]):
        out.append((curr - prev) / prev if prev else 0.0)
    return out


def stdev(values: Sequence[float]) -> float:
    """Population standard deviation. Returns 0.0 for fewer than two points."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / n)


def percentile_rank(values: Sequence[float], value: float) -> float:
    """Fraction of ``values`` at or below ``value``, in 0..1.

    Used for COT crowding and volatility percentiles. Deliberately the simple
    definition -- no interpolation -- because the consumers bucket it anyway.
    """
    if not values:
        return 0.5
    below = sum(1 for v in values if v <= value)
    return below / len(values)


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
