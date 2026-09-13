"""Builders for the bullion tests.

Deterministic tapes rather than the synthetic generator: a test that asserts "a clean
uptrend scores positive" should fail when the scorer breaks, not when a random seed
happens to produce chop. The generator has its own tests.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from bullion.config import GOLD, Regime
from bullion.fusion import Confidence, Levels, Signal, SignalState
from bullion.indicators import Indicators
from bullion.providers.base import CotHistory, CotReport, Headline, MacroPoint, MacroSeries
from bullion.regime import RegimeRead
from bullion.scoring.layer import LayerScore
from bullion.series import Candle, Series

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_series(
    closes: list[float],
    asset: str = GOLD,
    interval: str = "1day",
    start: datetime = START,
    wick: float = 0.002,
    volume: float = 100_000.0,
) -> Series:
    """Bars from a close path, with deterministic wicks and a flat volume profile."""
    candles: list[Candle] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        open_ = previous
        high = max(open_, close) * (1 + wick)
        low = min(open_, close) * (1 - wick)
        candles.append(
            Candle(
                ts=start + timedelta(days=index),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
        previous = close
    return Series.of(asset, interval, candles, source="test")


def trend_up(n: int = 300, start_price: float = 2000.0, per_bar: float = 0.004) -> Series:
    """A clean, steady uptrend: EMA stack aligned, ADX high, RSI elevated."""
    return make_series([start_price * (1 + per_bar) ** i for i in range(n)])


def trend_down(n: int = 300, start_price: float = 2400.0, per_bar: float = 0.004) -> Series:
    return make_series([start_price * (1 - per_bar) ** i for i in range(n)])


def choppy(n: int = 300, start_price: float = 2200.0, amplitude: float = 0.012) -> Series:
    """A sine-wave range: no trend, ADX low, price oscillating between shelves."""
    return make_series(
        [start_price * (1 + amplitude * math.sin(i / 7.0)) for i in range(n)]
    )


def make_indicators(**overrides) -> Indicators:
    """An Indicators bundle with sane defaults, for scoring tests."""
    defaults: dict[str, object] = {
        "asset": GOLD,
        "interval": "1day",
        "close": 2400.0,
        "prev_close": 2390.0,
        "ema20": 2380.0,
        "ema50": 2350.0,
        "ema200": 2300.0,
        "ema20_prev": 2375.0,
        "ema50_prev": 2348.0,
        "macd_line": 12.0,
        "macd_signal": 8.0,
        "macd_hist": 4.0,
        "macd_hist_prev": 3.0,
        "rsi14": 58.0,
        "rsi14_prev": 56.0,
        "stoch_k": 60.0,
        "stoch_d": 55.0,
        "stoch_k_prev": 52.0,
        "stoch_d_prev": 54.0,
        "atr14": 24.0,
        "adx14": 31.0,
        "adx14_prev": 29.0,
        "plus_di": 28.0,
        "minus_di": 14.0,
        "bb_upper": 2430.0,
        "bb_middle": 2380.0,
        "bb_lower": 2330.0,
        "percent_b": 0.7,
        "bandwidth": 0.042,
        "bandwidth_percentile": 0.5,
        "volume": 120_000.0,
        "volume_sma20": 100_000.0,
        "bars": 300,
        "arrays": {},
    }
    defaults.update(overrides)
    return Indicators(**defaults)


def make_macro(
    series_id: str = "DFII10",
    values: list[float] | None = None,
    units: str = "percent",
    cadence_days: int = 1,
    end: datetime | None = None,
) -> MacroSeries:
    values = values or [2.0] * 150
    end_day = (end or datetime.now(timezone.utc)).date()
    points = tuple(
        MacroPoint(
            day=end_day - timedelta(days=cadence_days * (len(values) - 1 - i)),
            value=value,
        )
        for i, value in enumerate(values)
    )
    return MacroSeries(
        series_id=series_id, label=series_id, points=points, units=units, source="test"
    )


def ramp(start: float, end: float, n: int, noise: float = 0.0) -> list[float]:
    """A linear ramp plus a deterministic wiggle.

    The wiggle matters: ``zscore_of_change`` divides by the spread of past changes, and a
    perfectly linear ramp has zero spread, so every macro test on a clean ramp would score
    0.0 and pass for the wrong reason. An irrational-frequency sine gives a non-repeating,
    reproducible spread.
    """
    step = (end - start) / max(1, n - 1)
    return [start + step * i + noise * math.sin(i * 1.7) for i in range(n)]


def make_cot(
    asset: str = GOLD,
    nets: list[float] | None = None,
    open_interest: float = 450_000.0,
    age_days: int = 4,
    weeks: int | None = None,
) -> CotHistory:
    """A COT history from a list of net positions, newest last."""
    nets = nets if nets is not None else [100_000.0] * 60
    weeks = weeks or len(nets)
    today = datetime.now(timezone.utc).date()
    reports = []
    for i, net in enumerate(nets):
        report_day = today - timedelta(days=age_days + 7 * (len(nets) - 1 - i))
        long_ = (open_interest * 0.6 + net) / 2
        short = long_ - net
        reports.append(
            CotReport(
                asset=asset,
                report_date=report_day,
                noncomm_long=long_,
                noncomm_short=short,
                open_interest=open_interest,
                source="test",
            )
        )
    return CotHistory(asset=asset, reports=tuple(reports))


def make_headline(
    title: str,
    hours_ago: float = 1.0,
    provider_sentiment: float | None = None,
    assets: tuple[str, ...] = (),
    now: datetime | None = None,
) -> Headline:
    now = now or datetime.now(timezone.utc)
    return Headline(
        id=f"t-{abs(hash(title)) % 10**8}",
        ts=now - timedelta(hours=hours_ago),
        title=title,
        source="test-wire",
        provider="test",
        provider_sentiment=provider_sentiment,
        assets=assets,
    )


def make_layer(
    name: str, score: float, available: bool = True, freshness: float = 1.0
) -> LayerScore:
    return LayerScore(
        name=name,
        score=score,
        reasoning=f"{name} test reason",
        freshness=freshness,
        available=available,
    )


def make_regime(regime: Regime = Regime.TREND, trend_strength: float = 0.8) -> RegimeRead:
    return RegimeRead(regime=regime, reason="test", trend_strength=trend_strength)


def make_signal(
    asset: str = GOLD,
    state: SignalState = SignalState.BUY,
    confidence: float = 0.6,
    composite: float = 0.4,
    price: float = 2400.0,
    stop: float | None = 2360.0,
    target: float | None = 2470.0,
    ts: datetime | None = None,
) -> Signal:
    """A Signal built directly, for alert and store tests that do not need scoring."""
    return Signal(
        asset=asset,
        state=state,
        composite_score=composite,
        confidence=Confidence(
            value=confidence,
            conviction=0.5,
            agreement=0.7,
            trend=0.8,
            coverage=1.0,
            freshness=1.0,
            data_quality=1.0,
            unanimous=False,
        ),
        price_at_signal=price,
        levels=Levels(
            entry=price, stop=stop, target=target, risk_reward=1.8, basis="test levels"
        ),
        layers={
            "technical": make_layer("technical", 0.5),
            "macro": make_layer("macro", 0.3),
            "sentiment": make_layer("sentiment", 0.1),
            "positioning": make_layer("positioning", 0.2),
        },
        weights={"technical": 0.35, "macro": 0.35, "sentiment": 0.15, "positioning": 0.15},
        regime=make_regime(),
        timestamp=ts or datetime.now(timezone.utc),
    )
