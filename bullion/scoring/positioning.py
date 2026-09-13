"""The positioning layer: COT net speculative length, with a crowding penalty.

This layer is the only one of the four that is not monotonic, and that is the whole
reason it is worth having. Specs adding length from a neutral base is confirmation.
Specs at the 95th percentile of three years is a crowded trade: everyone who wants
to be long already is, and the next move needs new buyers that do not exist. Gold's
sharpest drawdowns -- 2011, 2020, 2024 -- all started from crowded spec positioning,
not from a technical signal.

So the score is momentum *times* a crowding discount, plus an explicit contrarian
term at the extremes:

    momentum   4-week change in net length, scaled by open interest
    crowding   percentile of net length in its own 3-year distribution
    score      momentum damped toward zero as crowding approaches an extreme,
               then pushed negative once past it

Silver's futures market is roughly a third of gold's open interest, which makes its
positioning both more volatile and more squeeze-prone, so its extremes are treated
as slightly less conclusive -- the same percentile means less when the sample is
thinner.

The data is always at least three days old (published Friday for Tuesday), so the
freshness term decays through the week and bottoms out just before the next release.
"""

from __future__ import annotations

from datetime import date

from ..config import GOLD
from ..providers.base import CotHistory, freshness
from ..series import clamp
from .layer import LayerScore, norm, unavailable

# Percentile beyond which positioning is treated as crowded in each direction.
CROWDED_LONG = 0.88
CROWDED_SHORT = 0.12

# A report is expected to be at most this old; past it, the layer is discounted.
# Ten days covers the Friday-for-Tuesday lag plus a holiday week.
EXPECTED_AGE_DAYS = 10.0


def _ordinal(value: float) -> str:
    """``44`` -> ``44th``. Present in the reasoning string a user reads."""
    n = int(round(value))
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def score(asset: str, history: CotHistory | None, today: date | None = None) -> LayerScore:
    if history is None or not history.reports:
        return unavailable("positioning", "no COT history available")
    if len(history.reports) < 26:
        return unavailable(
            "positioning",
            f"only {len(history.reports)} weekly reports; need 26 to judge crowding",
        )

    latest = history.latest
    assert latest is not None  # guarded above
    net_change = history.net_change(4)
    percentile = history.crowding_percentile()
    net_pct_oi = latest.net_pct_oi

    if net_change is None or percentile is None:
        return unavailable("positioning", "not enough history for a change or percentile read")

    # Momentum: the 4-week change in net length as a fraction of open interest. 10% of
    # OI in four weeks earns full marks -- on COMEX gold (~450k OI) that is a 45k-contract
    # swing, which is a large but not once-a-cycle move. Set tighter, this term saturates
    # at +/-1 most weeks and the layer stops discriminating.
    momentum = norm(net_change / latest.open_interest, 0.10) if latest.open_interest else 0.0

    # Crowding discount: 1.0 in the middle of the range, falling to 0 at the
    # extreme, so a crowded market's momentum stops counting before it reverses.
    if percentile >= CROWDED_LONG:
        crowd_room = (1.0 - percentile) / (1.0 - CROWDED_LONG)
    elif percentile <= CROWDED_SHORT:
        crowd_room = percentile / CROWDED_SHORT
    else:
        crowd_room = 1.0
    crowd_room = clamp(crowd_room, 0.0, 1.0)

    # Contrarian term, active only at the extremes and signed against the crowd.
    contrarian = 0.0
    if percentile >= CROWDED_LONG:
        contrarian = -((percentile - CROWDED_LONG) / (1.0 - CROWDED_LONG)) * 0.8
    elif percentile <= CROWDED_SHORT:
        contrarian = ((CROWDED_SHORT - percentile) / CROWDED_SHORT) * 0.8

    # Silver's thinner market makes its extremes less conclusive; damp the
    # contrarian push rather than the momentum, which is the better-behaved half.
    if asset != GOLD:
        contrarian *= 0.7

    total = clamp(momentum * crowd_room + contrarian)

    age = history.age_days(today)
    fresh = freshness(age * 86400.0, EXPECTED_AGE_DAYS * 86400.0)

    direction = "adding net length" if net_change > 0 else "cutting net length"
    crowd_text = (
        "crowded long"
        if percentile >= CROWDED_LONG
        else "crowded short"
        if percentile <= CROWDED_SHORT
        else "not yet crowded"
    )
    rank = _ordinal(percentile * 100)
    position = f"net {net_pct_oi * 100:+.1f}% of OI at the " if net_pct_oi is not None else ""
    reasoning = (
        f"large specs {direction} ({net_change:+,.0f} contracts/4w), "
        f"{position}{rank} percentile of 3 years — {crowd_text}; report {age:.0f}d old"
    )

    return LayerScore(
        name="positioning",
        score=total,
        reasoning=reasoning,
        components={
            "momentum": momentum,
            "crowding_room": crowd_room,
            "contrarian": contrarian,
        },
        freshness=fresh,
        detail={
            "report_date": latest.report_date.isoformat(),
            "net": latest.net,
            "net_pct_oi": net_pct_oi,
            "percentile": percentile,
            "age_days": age,
            "crowded": percentile >= CROWDED_LONG or percentile <= CROWDED_SHORT,
        },
    )
