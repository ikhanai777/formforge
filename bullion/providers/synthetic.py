"""A deterministic synthetic market, for tests, CI and offline demos.

This exists for the same reason formforge ships an offline LLM client: a system
that needs six API keys to show anything is a system nobody evaluates, and a
test suite that needs the network is a test suite that goes red for reasons that
have nothing to do with the code. The generator produces all four layers --
bars, macro series, COT history, headlines -- from one seed, so a whole pipeline
run is reproducible to the last decimal.

**It is labelled everywhere.** ``name`` is ``"synthetic"``, ``LayerData.synthetic``
is set, the API echoes ``data_mode: "synthetic"`` and the dashboard paints a
persistent banner. This matters more than it looks: invented prices presented as
live ones in a financial product is the single most damaging thing this codebase
could do, and the defence has to be structural rather than a note in a README.
``BULLION_ALLOW_SYNTHETIC=0`` refuses to construct it at all.

The data is *plausible*, not real: regime-switching drift, fat-ish tails, gold and
silver correlated with silver running ~1.7x the volatility, a real-yield series
that moves against gold. Good enough to exercise every code path and to make the
dashboard legible. Not good enough to fit weights against -- ``backtest`` warns
when asked to.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..config import GOLD, SILVER
from ..series import Candle, Series, interval_seconds
from .base import (
    CotHistory,
    CotReport,
    Headline,
    MacroPoint,
    MacroSeries,
    ProviderError,
    Quote,
)

SOURCE = "synthetic"

# Anchors roughly in line with where these markets have traded recently. The
# absolute level barely matters -- every score in the system is computed from
# changes and distributions -- but a dashboard showing gold at $12 is distracting.
_ANCHOR = {GOLD: 2430.0, SILVER: 28.60}
_DAILY_VOL = {GOLD: 0.0092, SILVER: 0.0158}

_MACRO_ANCHOR: dict[str, tuple[str, float, float, str, int]] = {
    # series_id: (label, level, per-observation vol, units, days per observation)
    # CPI is monthly in reality, and generating it daily made the scorer's 12-period
    # year-over-year read span twelve *days*. The cadence is part of the series.
    "DFII10": ("10Y TIPS real yield", 1.85, 0.030, "percent", 1),
    "DTWEXBGS": ("Broad dollar index", 121.5, 0.0022, "index", 1),
    "CPIAUCSL": ("CPI, all items", 314.2, 0.0024, "index", 30),
    "T10Y2Y": ("10Y-2Y spread", 0.18, 0.035, "percent", 1),
}

_HEADLINE_TEMPLATES: tuple[tuple[str, float], ...] = (
    ("Fed officials signal patience as inflation cools, real yields slip", 0.55),
    ("Dollar retreats for a third session; gold extends gains", 0.65),
    ("Central banks added to gold reserves again last month, WGC data shows", 0.45),
    ("Safe-haven bid returns as geopolitical tensions escalate", 0.70),
    ("Silver industrial demand forecast raised on solar installs", 0.50),
    ("Hawkish FOMC minutes lift the dollar, metals under pressure", -0.60),
    ("Stronger payrolls push real yields higher; bullion slides", -0.65),
    ("ETF outflows continue for a fifth week in gold funds", -0.40),
    ("Profit-taking caps silver after a three-week run", -0.30),
    ("Metals steady ahead of the CPI print", 0.05),
    ("Range-bound gold waits on the Fed", 0.0),
)


def _seed_int(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")


@dataclass
class SyntheticWorld:
    """One reproducible market. Share an instance so the layers stay consistent.

    ``asof`` is the "now" the world ends at; everything is generated backwards
    from it, which is what makes a backtest over synthetic data repeatable.
    """

    seed: int = 7
    asof: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    name: str = SOURCE
    # Set to bias the generated world, so a test can ask for a trending-up tape
    # and assert the engine calls it rather than hoping a random seed obliges.
    drift_bias: float = 0.0
    # One master path per asset/interval, of this length, from which every request
    # is a tail slice. Generating per-request would make ``candles(limit=120)`` and
    # ``candles(limit=400)`` end at different prices -- which silently broke the
    # cross-feed validation the first time this was written, because the two
    # "feeds" were sampling two different worlds.
    history_bars: int = 1600
    _cache: dict[tuple[str, str], Series] = field(default_factory=dict, repr=False)

    # -- prices ------------------------------------------------------------

    def candles(self, asset: str, interval: str = "1day", limit: int = 400) -> Series:
        master = self._master(asset, interval)
        if limit > len(master):
            raise ProviderError(
                f"synthetic world holds {len(master)} {interval} bars; construct it with "
                f"history_bars>={limit} to ask for more"
            )
        return master.tail(limit)

    def _master(self, asset: str, interval: str) -> Series:
        if asset not in _ANCHOR:
            raise ProviderError(f"synthetic world has no {asset}")
        key = (asset, interval)
        if key in self._cache:
            return self._cache[key]

        bars = self.history_bars
        step = interval_seconds(interval)
        # Volatility scales with the square root of time.
        vol = _DAILY_VOL[asset] * math.sqrt(step / 86400.0)
        rng = random.Random(_seed_int(self.seed, asset, interval))

        # A shared driver so gold and silver move together; silver gets extra
        # idiosyncratic noise on top, which is what makes its COT and technical
        # reads genuinely different rather than a scaled copy.
        shared = random.Random(_seed_int(self.seed, "shared", interval))

        anchor = _ANCHOR[asset]
        closes: list[float] = []
        price = anchor
        drift = self.drift_bias
        for i in range(bars):
            if i % 45 == 0:
                # Regime switch: a new drift every ~45 bars, which is what gives the
                # ADX gate and the regime detector something to detect. The 0.12
                # coefficient is the whole calibration: at 0.45 the drift dominated the
                # noise, every segment became a clean one-way trend, and 1,600 bars of
                # it produced ADX readings in the 90s and gold at $285. Drift has to be
                # a minority of the per-bar variance for the tape to look like a market.
                drift = self.drift_bias + shared.gauss(0.0, 0.12) * vol
            common = shared.gauss(0.0, 1.0)
            own = rng.gauss(0.0, 1.0)
            # Fat tails: one bar in fifty gets a 2.5x shock.
            shock = 2.5 if rng.random() < 0.02 else 1.0
            # Gentle pull back toward the anchor (timescale ~500 bars), so a long
            # history stays in a plausible price range instead of wandering an order of
            # magnitude away. Real metals are not mean-reverting like this; the point is
            # a legible demo tape, not a model of the market.
            reversion = -0.002 * math.log(price / anchor)
            ret = drift + reversion + vol * shock * (0.75 * common + 0.66 * own)
            price *= math.exp(ret)
            closes.append(price)

        # Walk the close path into coherent bars.
        candles: list[Candle] = []
        last_close = closes[0] / math.exp(vol * rng.gauss(0, 1))
        end = self.asof.replace(minute=0, second=0, microsecond=0)
        for i, close in enumerate(closes):
            ts = end - timedelta(seconds=step * (bars - i))
            open_ = last_close
            wick = abs(rng.gauss(0.0, vol * 0.6)) * close
            high = max(open_, close) + wick
            low = min(open_, close) - abs(rng.gauss(0.0, vol * 0.6)) * close
            low = max(low, 0.01)
            volume = max(1.0, rng.gauss(180_000, 40_000)) * (1.0 + abs(close / open_ - 1) * 30)
            candles.append(
                Candle(
                    ts=ts,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=round(volume),
                )
            )
            last_close = close

        series = Series.of(asset, interval, candles, source=SOURCE)
        self._cache[key] = series
        return series

    def quote(self, asset: str) -> Quote:
        series = self._master(asset, "1day")
        # A hair off the last close, so the cross-validation path is exercised
        # rather than trivially agreeing.
        rng = random.Random(_seed_int(self.seed, "quote", asset))
        price = series.last.close * (1 + rng.gauss(0, 0.0004))
        return Quote(asset=asset, price=price, ts=self.asof, source=SOURCE)

    # -- macro -------------------------------------------------------------

    def series(self, series_id: str, observations: int = 400) -> MacroSeries:
        if series_id not in _MACRO_ANCHOR:
            raise ProviderError(f"synthetic world has no macro series {series_id}")
        label, level, vol, units, cadence = _MACRO_ANCHOR[series_id]
        # Monthly series do not need 400 observations; capping keeps CPI to ~8 years
        # instead of 33, which is the range a real FRED pull would cover.
        observations = min(observations, 400 if cadence == 1 else 100)
        rng = random.Random(_seed_int(self.seed, "macro", series_id))
        shared = random.Random(_seed_int(self.seed, "shared", "1day"))

        points: list[MacroPoint] = []
        value = level
        drift = 0.0
        today = self.asof.date()
        for i in range(observations):
            if i % 60 == 0:
                drift = rng.gauss(0.0, vol * 0.3)
            # Real yields and the dollar lean against the gold driver, which is
            # what makes the macro layer disagree with the technical layer
            # sometimes instead of always nodding along.
            leans = series_id in {"DFII10", "DTWEXBGS"}
            lean = -0.35 * shared.gauss(0.0, 1.0) if leans else 0.0
            if units == "percent":
                value += drift + vol * (rng.gauss(0, 1) + lean)
            else:
                # Index levels compound and CPI only falls in a real deflation, so the
                # drift floor keeps the generated series economically coherent.
                step = drift + vol * (rng.gauss(0, 1) + lean)
                if series_id == "CPIAUCSL":
                    step = max(step, -vol * 0.5)
                value *= math.exp(step)
            points.append(
                MacroPoint(
                    day=today - timedelta(days=cadence * (observations - i)), value=value
                )
            )
        return MacroSeries(
            series_id=series_id,
            label=label,
            points=tuple(points),
            units=units,
            source=SOURCE,
        )

    # -- positioning -------------------------------------------------------

    def history(self, asset: str, weeks: int = 156) -> CotHistory:
        if asset not in _ANCHOR:
            raise ProviderError(f"synthetic world has no {asset}")
        rng = random.Random(_seed_int(self.seed, "cot", asset, weeks))
        # Speculative length follows price with a lag, which is exactly why the
        # positioning layer is contrarian at extremes. The weekly series is mapped onto
        # whatever daily history exists rather than demanding weeks*7 bars -- a caller
        # asking for three years of COT should get it from a world holding two years of
        # bars, at lower resolution, not an exception.
        closes = self._master(asset, "1day").closes
        weeks = min(weeks, max(26, len(closes) // 5))
        oi = 420_000.0 if asset == GOLD else 150_000.0
        reports: list[CotReport] = []
        report_day = self.asof.date() - timedelta(days=(self.asof.weekday() - 1) % 7)
        for i in range(weeks):
            day = report_day - timedelta(weeks=weeks - 1 - i)
            # Map the week onto the price path, lagged a fortnight.
            idx = max(0, len(closes) - 1 - (weeks - i) * 5 - 10)
            anchor = closes[max(0, idx - 60)]
            momentum = (closes[idx] / anchor - 1.0) if anchor else 0.0
            net_share = max(-0.25, min(0.55, 0.18 + momentum * 1.1 + rng.gauss(0, 0.018)))
            total = oi * (0.62 + rng.gauss(0, 0.04))
            long_ = total * (0.5 + net_share / 2)
            short = total - long_
            reports.append(
                CotReport(
                    asset=asset,
                    report_date=day,
                    noncomm_long=round(long_),
                    noncomm_short=round(max(1.0, short)),
                    open_interest=round(oi * (1 + rng.gauss(0, 0.05))),
                    source=SOURCE,
                )
            )
        return CotHistory(asset=asset, reports=tuple(reports))

    # -- news --------------------------------------------------------------

    def headlines(self, since: datetime, limit: int = 50) -> list[Headline]:
        rng = random.Random(_seed_int(self.seed, "news", since.date().isoformat()))
        span_hours = max(1.0, (self.asof - since).total_seconds() / 3600.0)
        count = min(limit, max(3, int(span_hours / 6)))
        out: list[Headline] = []
        for i in range(count):
            title, lean = _HEADLINE_TEMPLATES[rng.randrange(len(_HEADLINE_TEMPLATES))]
            age = rng.random() * span_hours
            out.append(
                Headline(
                    id=f"synthetic-{_seed_int(self.seed, title, i):x}"[:24],
                    ts=self.asof - timedelta(hours=age),
                    title=f"[SYNTHETIC] {title}",
                    source="synthetic-wire",
                    provider=SOURCE,
                    summary=None,
                    provider_sentiment=max(-1.0, min(1.0, lean + rng.gauss(0, 0.12))),
                    assets=(GOLD, SILVER),
                )
            )
        out.sort(key=lambda h: h.ts)
        return out


@dataclass
class SyntheticCross:
    """A second synthetic feed, so the cross-validation path runs offline too.

    Quotes the same world with a few basis points of independent noise. Without
    this, offline runs never exercise ``CrossValidatedPrices``' agreement maths --
    and the one thing worse than an untested failover is an untested failover in
    the part of the system whose job is catching a bad feed.
    """

    world: SyntheticWorld
    name: str = "synthetic-cross"

    def quote(self, asset: str) -> Quote:
        base = self.world._master(asset, "1day").last.close
        rng = random.Random(_seed_int(self.world.seed, "cross", asset))
        return Quote(
            asset=asset,
            price=base * (1 + rng.gauss(0, 0.0006)),
            ts=self.world.asof,
            source=self.name,
        )

    def candles(self, asset: str, interval: str = "1day", limit: int = 400) -> Series:
        raise ProviderError("the synthetic cross feed is a spot cross-check only")


def synthetic_cot_csv(asset: str, weeks: int = 12, seed: int = 7) -> str:
    """A CFTC-shaped CSV, for testing the real parser without the network."""
    world = SyntheticWorld(seed=seed)
    history = world.history(asset, weeks)
    name = (
        "GOLD - COMMODITY EXCHANGE INC."
        if asset == GOLD
        else "SILVER - COMMODITY EXCHANGE INC."
    )
    header = (
        "Market_and_Exchange_Names,Report_Date_as_YYYY-MM-DD,"
        "NonComm_Positions_Long_All,NonComm_Positions_Short_All,Open_Interest_All"
    )
    rows = [
        f"{name},{r.report_date.isoformat()},"
        f"{int(r.noncomm_long)},{int(r.noncomm_short)},{int(r.open_interest)}"
        for r in history.reports
    ]
    return "\n".join([header, *rows])
