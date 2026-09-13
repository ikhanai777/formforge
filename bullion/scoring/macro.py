"""The macro layer: real yields, the dollar, inflation, the curve.

This is the layer that actually moves the metals (spec section 3.3), and the
implementation hinges on one decision: **everything is scored as a z-scored change,
never as a level.** A 1.9% real yield is not bullish or bearish for gold; a 25bp
fall in real yields over a month, when the typical monthly move is 8bp, is strongly
bullish. Levels invite curve-fitting to a decade that has ended -- gold traded at
$1,200 with 0% real yields and $2,400 with 2% real yields, so any model keyed to
the level of anything is describing the past.

Signs are taken from ``providers.macro.INVERSE_TO_GOLD`` rather than written out
per series, because a sign error here is the single most expensive bug available in
this codebase: it would produce a confident, well-reasoned, systematically wrong
call in exactly the macro conditions that matter most.

Silver gets different sub-weights, not a copy of gold's (spec section 10, phase 3).
Roughly half of silver demand is industrial, so the growth signal in the curve
carries more and the pure monetary real-yield channel carries less.
"""

from __future__ import annotations

from datetime import date

from ..config import GOLD
from ..providers.base import EconomicEvent, MacroSeries, freshness
from ..series import clamp
from .layer import LayerScore, norm, unavailable, weighted

# Per-asset sub-weights over the macro inputs.
SUB_WEIGHTS: dict[str, dict[str, float]] = {
    "gold": {"real_yield": 0.48, "dollar": 0.30, "inflation": 0.12, "curve": 0.10},
    "silver": {"real_yield": 0.34, "dollar": 0.28, "inflation": 0.14, "curve": 0.24},
}

# How often each series should update, in days. A CPI print that is 20 days old is
# perfectly fresh; a real yield that is 20 days old means FRED stopped answering.
EXPECTED_AGE_DAYS: dict[str, float] = {
    "DFII10": 4.0,
    "DTWEXBGS": 9.0,
    "CPIAUCSL": 40.0,
    "T10Y2Y": 4.0,
}

# Lookback for the change being scored, in observations. 20 business days is about
# a month: long enough to be a trend in the macro channel, short enough to still be
# about the current regime.
LOOKBACK = 20


def _series_vote(
    series: MacroSeries | None,
    *,
    inverse: bool,
    scale: float = 1.5,
) -> tuple[float | None, str, float]:
    """Score one macro series by the z-score of its recent change.

    ``scale`` is the z-score that earns full marks. 1.5 sigma rather than 2: macro
    moves that matter to gold are common enough that demanding a two-sigma event
    before voting means the layer is silent most of the time it should be talking.
    """
    if series is None or not series.points:
        return None, "unavailable", 0.0
    z = series.zscore_of_change(LOOKBACK)
    change = series.change(LOOKBACK)
    if z is None or change is None:
        return None, f"{series.label}: not enough history", 0.0
    vote = norm(-z if inverse else z, scale)
    expected = EXPECTED_AGE_DAYS.get(series.series_id, 7.0)
    fresh = freshness(series.age_days() * 86400.0, expected * 86400.0)

    direction = "falling" if change < 0 else "rising" if change > 0 else "flat"
    if series.units == "percent":
        magnitude = f"{change * 100:+.0f}bp/{LOOKBACK}d"
    else:
        pct = series.pct_change(LOOKBACK)
        magnitude = f"{pct * 100:+.1f}%/{LOOKBACK}d" if pct is not None else f"{change:+.2f}"
    note = f"{series.label} {direction} ({magnitude}, {z:+.1f}σ)"
    return vote, note, fresh


def _inflation_vote(series: MacroSeries | None) -> tuple[float | None, str, float]:
    """Year-over-year CPI, scored on its *direction of change*, mildly.

    Mildly on purpose. The "gold is an inflation hedge" story is much weaker in the
    data than the real-yield story -- gold lost money through the 2022 inflation
    spike because real yields rose faster than prices did. So CPI enters as a small
    confirming input, and real yields, which already embed it, do the work.
    """
    if series is None or len(series.points) < 14:
        return None, "unavailable", 0.0
    yoy_now = series.pct_change(12)
    yoy_prior = None
    if len(series.points) > 15:
        trimmed = MacroSeries(
            series_id=series.series_id,
            label=series.label,
            points=series.points[:-3],
            units=series.units,
            source=series.source,
        )
        yoy_prior = trimmed.pct_change(12)
    if yoy_now is None:
        return None, f"{series.label}: not enough history", 0.0
    accel = (yoy_now - yoy_prior) if yoy_prior is not None else 0.0
    # 50bp of YoY acceleration is a full-marks reading.
    vote = norm(accel, 0.005)
    fresh = freshness(series.age_days() * 86400.0, EXPECTED_AGE_DAYS["CPIAUCSL"] * 86400.0)
    return (
        vote,
        f"CPI {yoy_now * 100:.1f}% YoY, {'accelerating' if accel > 0 else 'cooling'}",
        fresh,
    )


def _curve_vote(series: MacroSeries | None, asset: str) -> tuple[float | None, str, float]:
    """10Y-2Y spread, as a growth/recession read.

    Steepening out of inversion is the classic late-cycle easing signal: bullish
    gold (rate cuts coming) and ambiguous for silver, whose industrial half wants
    growth rather than cuts. So the sign is positive for gold and damped for silver
    rather than simply copied.
    """
    if series is None or not series.points:
        return None, "unavailable", 0.0
    z = series.zscore_of_change(LOOKBACK)
    if z is None:
        return None, f"{series.label}: not enough history", 0.0
    latest = series.points[-1].value
    vote = norm(z, 2.0)
    if asset != GOLD:
        # Steepening helps gold through the cuts channel and helps silver only if it
        # reflects growth rather than easing; halve the conviction.
        vote *= 0.5
    fresh = freshness(series.age_days() * 86400.0, EXPECTED_AGE_DAYS["T10Y2Y"] * 86400.0)
    shape = "inverted" if latest < 0 else "positive"
    trend = "steepening" if z > 0 else "flattening"
    return vote, f"curve {shape} and {trend} ({latest:+.2f})", fresh


def score(
    asset: str,
    macro: dict[str, MacroSeries],
    events: list[EconomicEvent] | None = None,
    today: date | None = None,
) -> LayerScore:
    """Score the macro layer for one asset.

    Returns an unavailable layer when neither of the two load-bearing series (real
    yields, the dollar) is present. Scoring macro off CPI alone would produce a
    number, and the number would be noise wearing the macro layer's authority.
    """
    profile = SUB_WEIGHTS["gold" if asset == GOLD else "silver"]

    real, real_note, real_fresh = _series_vote(macro.get("DFII10"), inverse=True)
    dollar, dollar_note, dollar_fresh = _series_vote(macro.get("DTWEXBGS"), inverse=True)
    infl, infl_note, infl_fresh = _inflation_vote(macro.get("CPIAUCSL"))
    curve, curve_note, curve_fresh = _curve_vote(macro.get("T10Y2Y"), asset)

    if real is None and dollar is None:
        return unavailable(
            "macro",
            "neither the real-yield nor the dollar series is available "
            f"(have: {sorted(macro) or 'nothing'})",
        )

    parts: dict[str, tuple[float, float]] = {}
    fresh_parts: list[tuple[float, float]] = []
    for key, vote, weight, fresh in (
        ("real_yield", real, profile["real_yield"], real_fresh),
        ("dollar", dollar, profile["dollar"], dollar_fresh),
        ("inflation", infl, profile["inflation"], infl_fresh),
        ("curve", curve, profile["curve"], curve_fresh),
    ):
        if vote is None:
            continue
        parts[key] = (vote, weight)
        fresh_parts.append((fresh, weight))

    total = weighted(parts)
    # Freshness is weight-weighted: a stale CPI barely matters, a stale real-yield
    # series should visibly cost confidence.
    fresh_total = (
        sum(f * w for f, w in fresh_parts) / sum(w for _, w in fresh_parts)
        if fresh_parts
        else 0.0
    )

    notes = [
        note
        for vote, note in (
            (real, real_note),
            (dollar, dollar_note),
            (infl, infl_note),
            (curve, curve_note),
        )
        if vote is not None
    ]
    event = next(
        (
            e
            for e in sorted(events or [], key=lambda e: e.hours_until())
            if e.hours_until() >= 0
        ),
        None,
    )
    if event is not None and event.hours_until() <= 72:
        notes.append(f"{event.name} in {event.hours_until():.0f}h")

    return LayerScore(
        name="macro",
        score=total,
        reasoning="; ".join(notes),
        components={k: v for k, (v, _) in parts.items()},
        freshness=clamp(fresh_total, 0.0, 1.0),
        detail={
            "series_used": sorted(parts),
            "series_missing": sorted(set(SUB_WEIGHTS["gold"]) - set(parts)),
            "next_event": event.name if event else None,
            "next_event_hours": round(event.hours_until(), 1) if event else None,
        },
    )
