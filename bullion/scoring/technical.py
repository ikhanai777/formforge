"""The technical layer: seven sub-scores, one ADX gate, one sentence.

The design choice that matters here is that **ADX gates rather than votes**. Every
trend-following component -- the EMA stack, MACD -- is multiplied by a gate derived
from ADX, so in a chopping market the layer's trend opinion shrinks toward zero
instead of generating the textbook whipsaw. The same reading also flips how the
Bollinger position is read: at the upper band with ADX 32 price is breaking out, and
at the upper band with ADX 11 it is at the top of a range. One number, two opposite
meanings, and a layer that does not distinguish them is a coin flip with extra steps.

The second choice: **RSI is not a reversal signal on its own.** RSI 75 in a
confirmed uptrend is strength; RSI 75 in a range is exhaustion. Retail indicator
stacks lose money mostly by shorting strength, so the zone logic below is explicit
about which case it is in, and the reasoning string says which.
"""

from __future__ import annotations

from ..indicators import Indicators
from ..series import Series, clamp
from ..structure import structure_score
from .layer import LayerScore, norm, weighted

# Sub-weights within the layer. They sum to 1 before the ADX gate is applied;
# after gating the weighted mean renormalises, so a gated-down trend component
# hands its influence to structure and momentum rather than to nothing.
SUB_WEIGHTS: dict[str, float] = {
    "trend": 0.28,
    "macd": 0.15,
    "rsi": 0.14,
    "stoch": 0.08,
    "bands": 0.11,
    "structure": 0.17,
    "volume": 0.07,
}

# ADX below this is "no trend"; above it, "trending". 25 is the conventional
# threshold and the one users will cross-check against their own charts.
ADX_TREND = 25.0
ADX_DEAD = 12.0


def adx_gate(adx: float | None) -> float:
    """0.2..1.0 multiplier on trend-following components.

    Floored at 0.2 rather than 0: even in chop, a 200-EMA stack is weak evidence,
    and zeroing it entirely makes the layer lurch when ADX crosses a threshold.
    """
    if adx is None:
        return 0.6
    return clamp(0.2 + 0.8 * (adx - ADX_DEAD) / (ADX_TREND - ADX_DEAD), 0.2, 1.0)


def _trend_score(ind: Indicators) -> tuple[float, str]:
    """EMA stack alignment and slope.

    Separation is measured in percent and scaled so that 0.5% between two EMAs is a
    full-marks reading. On gold that is about $12 -- enough to be a real stack, not
    enough to require a once-a-year trend.
    """
    if ind.ema20 is None or ind.ema50 is None:
        return 0.0, "not enough bars for an EMA read"

    legs: list[float] = [
        norm((ind.close - ind.ema20) / ind.ema20, 0.005),
        norm((ind.ema20 - ind.ema50) / ind.ema50, 0.005),
    ]
    if ind.ema200 is not None:
        legs.append(norm((ind.ema50 - ind.ema200) / ind.ema200, 0.01))

    slope = 0.0
    if ind.ema20_prev:
        slope = norm((ind.ema20 - ind.ema20_prev) / ind.ema20_prev, 0.002)

    score = clamp(0.75 * (sum(legs) / len(legs)) + 0.25 * slope)

    if ind.ema200 is not None:
        order = (
            "EMA20>EMA50>EMA200"
            if ind.ema20 > ind.ema50 > ind.ema200
            else "EMA20<EMA50<EMA200"
            if ind.ema20 < ind.ema50 < ind.ema200
            else "EMAs unaligned"
        )
    else:
        order = "EMA20>EMA50" if ind.ema20 > ind.ema50 else "EMA20<EMA50"
    return score, order


def _macd_score(ind: Indicators) -> tuple[float, str]:
    """Histogram level and direction, scaled by price so it reads across assets."""
    if ind.macd_hist is None:
        return 0.0, "MACD warming up"
    level = norm(ind.macd_hist / ind.close, 0.0025)
    turn = 0.0
    if ind.macd_hist_prev is not None:
        turn = norm((ind.macd_hist - ind.macd_hist_prev) / ind.close, 0.0008)
    score = clamp(0.65 * level + 0.35 * turn)
    direction = "rising" if turn > 0.05 else "falling" if turn < -0.05 else "flat"
    sign = "positive" if ind.macd_hist > 0 else "negative"
    return score, f"MACD histogram {sign} and {direction}"


def _rsi_score(ind: Indicators, trending: bool) -> tuple[float, str]:
    """Momentum position, read differently in a trend than in a range."""
    if ind.rsi14 is None:
        return 0.0, "RSI warming up"
    rsi = ind.rsi14
    base = norm((rsi - 50.0) / 22.0, 1.0)

    if rsi >= 70:
        if trending:
            # Overbought in a trend is continuation. Cap it rather than fade it:
            # the move is real but late, which is a smaller edge than mid-trend.
            score, note = min(base, 0.55), f"RSI {rsi:.0f} (overbought but trending)"
        else:
            score, note = -0.45, f"RSI {rsi:.0f} (overbought in a range)"
    elif rsi <= 30:
        if trending:
            score, note = max(base, -0.55), f"RSI {rsi:.0f} (oversold, downtrend intact)"
        else:
            score, note = 0.45, f"RSI {rsi:.0f} (oversold in a range)"
    else:
        if 45 <= rsi <= 62:
            room = "room to run"
        elif rsi > 62:
            room = "extended, approaching overbought"
        elif rsi < 38:
            room = "weak, approaching oversold"
        else:
            room = "mid-range"
        score, note = base, f"RSI {rsi:.0f} ({room})"
    return clamp(score), note


def _stoch_score(ind: Indicators) -> tuple[float, str]:
    """Only votes on a fresh cross, and only away from an extreme.

    Stoch RSI crosses constantly; treating every cross as information is how this
    indicator earned its reputation. A cross from below 20 or above 80 is the subset
    with any historical edge.
    """
    k, d = ind.stoch_k, ind.stoch_d
    kp, dp = ind.stoch_k_prev, ind.stoch_d_prev
    if None in (k, d, kp, dp):
        return 0.0, "Stoch RSI warming up"
    crossed_up = kp <= dp and k > d  # type: ignore[operator]
    crossed_down = kp >= dp and k < d  # type: ignore[operator]
    if crossed_up and k < 35:  # type: ignore[operator]
        return 0.7, f"Stoch RSI crossed up from {k:.0f}"
    if crossed_down and k > 65:  # type: ignore[operator]
        return -0.7, f"Stoch RSI crossed down from {k:.0f}"
    return norm((float(k) - 50.0) / 50.0, 1.0) * 0.3, f"Stoch RSI {float(k):.0f}, no cross"


def _band_score(ind: Indicators, trending: bool) -> tuple[float, str]:
    """Bollinger position: breakout in a trend, mean reversion in a range."""
    pb = ind.percent_b
    if pb is None:
        return 0.0, "bands warming up"
    centred = (pb - 0.5) * 2.0
    squeeze = ""
    if ind.bandwidth_percentile is not None and ind.bandwidth_percentile < 0.15:
        squeeze = ", bands squeezed (expansion pending)"
    if trending:
        return clamp(centred * 0.8), f"price at %B {pb:.2f}, trend continuation read{squeeze}"
    return clamp(-centred * 0.7), f"price at %B {pb:.2f}, range fade read{squeeze}"


def _volume_score(ind: Indicators) -> tuple[float, str]:
    """ETF volume as the missing spot volume tape (spec section 3.2).

    Confirmation only: it scales the direction of the last bar, and it cannot
    produce a direction of its own. Volume without price is not a signal.
    """
    if not ind.volume or ind.volume_sma20 in (None, 0):
        return 0.0, "no volume proxy"
    ratio = ind.volume / float(ind.volume_sma20)  # type: ignore[arg-type]
    if ind.prev_close is None:
        return 0.0, "no prior close"
    direction = 1.0 if ind.close >= ind.prev_close else -1.0
    conviction = norm(ratio - 1.0, 0.6)
    if conviction <= 0:
        # Below-average volume: a move on thin volume is weak evidence either way.
        return direction * conviction * 0.3, f"volume {ratio:.1f}x average (thin)"
    return clamp(direction * conviction), f"volume {ratio:.1f}x average, confirming"


def score(series: Series, ind: Indicators) -> LayerScore:
    """Score the technical layer for one asset.

    ``series`` is needed as well as ``ind`` because structure (swings, shelves) is
    computed from bars rather than from indicator scalars.
    """
    trending = (ind.adx14 or 0.0) >= ADX_TREND
    gate = adx_gate(ind.adx14)

    trend, trend_note = _trend_score(ind)
    macd_, macd_note = _macd_score(ind)
    rsi_, rsi_note = _rsi_score(ind, trending)
    stoch_, stoch_note = _stoch_score(ind)
    bands_, bands_note = _band_score(ind, trending)
    struct_, struct_note = structure_score(series)
    vol_, vol_note = _volume_score(ind)

    parts: dict[str, tuple[float, float]] = {
        # Trend components are gated by ADX; the rest are not.
        "trend": (trend, SUB_WEIGHTS["trend"] * gate),
        "macd": (macd_, SUB_WEIGHTS["macd"] * gate),
        "rsi": (rsi_, SUB_WEIGHTS["rsi"]),
        "stoch": (stoch_, SUB_WEIGHTS["stoch"]),
        "bands": (bands_, SUB_WEIGHTS["bands"]),
        "structure": (struct_, SUB_WEIGHTS["structure"]),
        "volume": (vol_, SUB_WEIGHTS["volume"]),
    }
    total = weighted(parts)

    adx_note = (
        f"ADX {ind.adx14:.0f} ({'trending' if trending else 'no trend'})"
        if ind.adx14 is not None
        else "ADX unavailable"
    )
    # The reasoning line leads with the three things a trader checks first, then
    # the structure note, because that is the part that decides the levels.
    reasoning = f"{trend_note}, {rsi_note}, {adx_note}; {macd_note}; {struct_note}"

    return LayerScore(
        name="technical",
        score=total,
        reasoning=reasoning,
        components={
            "trend": trend,
            "macd": macd_,
            "rsi": rsi_,
            "stoch": stoch_,
            "bands": bands_,
            "structure": struct_,
            "volume": vol_,
            "adx_gate": gate,
        },
        # Technical freshness is the bar series' own freshness, set by the caller
        # from the price-quality report; a bar that closed on time is fully fresh.
        freshness=1.0,
        detail={
            "adx": ind.adx14,
            "rsi": ind.rsi14,
            "atr": ind.atr14,
            "atr_pct": ind.atr_pct,
            "trending": trending,
            "structure_note": struct_note,
            "stoch_note": stoch_note,
            "bands_note": bands_note,
            "volume_note": vol_note,
        },
    )
