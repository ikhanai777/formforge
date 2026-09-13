"""Price structure: swing points, support/resistance and Fibonacci zones.

Spec section 3.2 lists this as its own indicator family, and it is the one that
most affects whether a signal is *tradeable* rather than merely correct. A buy
issued two dollars under a resistance shelf that has rejected price three times
is directionally fine and practically bad: the suggested target is on the far
side of a wall. So structure enters the technical score with a modest weight, and
enters the level suggestion (``fusion.levels``) with a large one.

The swing definition here is a fractal one: a bar is a swing high if its high is
the highest in a window of ``lookback`` bars either side. That requires
``lookback`` bars of confirmation *after* the pivot, which means the most recent
pivots are deliberately absent. Every alternative -- zigzag with a percentage
threshold, unconfirmed pivots -- leaks the future into the past, and in a
backtest that leak looks exactly like skill.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .series import Series


@dataclass(frozen=True, slots=True)
class Swing:
    index: int
    ts: datetime
    price: float
    kind: str  # "high" | "low"


@dataclass(frozen=True)
class Level:
    """A clustered support or resistance shelf.

    ``touches`` is the count of distinct swings in the cluster. Two touches is a
    line drawn through noise; four touches is a level other participants are also
    watching, which is the entire mechanism by which support works.
    """

    price: float
    kind: str  # "support" | "resistance"
    touches: int
    last_touch: datetime

    def distance_pct(self, price: float) -> float:
        return (self.price - price) / price if price else 0.0


def swings(series: Series, lookback: int = 3) -> list[Swing]:
    """Confirmed fractal swing highs and lows, oldest first."""
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    highs, lows = series.highs, series.lows
    found: list[Swing] = []
    for i in range(lookback, len(series) - lookback):
        window = range(i - lookback, i + lookback + 1)
        if all(highs[i] >= highs[j] for j in window) and any(
            highs[i] > highs[j] for j in window if j != i
        ):
            found.append(Swing(i, series[i].ts, highs[i], "high"))
        elif all(lows[i] <= lows[j] for j in window) and any(
            lows[i] < lows[j] for j in window if j != i
        ):
            found.append(Swing(i, series[i].ts, lows[i], "low"))
    return found


def _within(a: float, b: float, tolerance_pct: float) -> bool:
    return abs(a - b) / a <= tolerance_pct


def levels(
    series: Series,
    lookback: int = 3,
    tolerance_pct: float = 0.004,
    max_levels: int = 6,
) -> list[Level]:
    """Cluster swing points into levels, nearest-to-price first.

    ``tolerance_pct`` defaults to 40bp, which at gold's scale is roughly $9 -- wide
    enough that two tests of the same shelf a week apart cluster together, narrow
    enough that two genuinely separate shelves do not merge into a smear.
    """
    pivots = swings(series, lookback)
    if not pivots:
        return []

    clusters: list[list[Swing]] = []
    for pivot in sorted(pivots, key=lambda s: s.price):
        if clusters and _within(pivot.price, clusters[-1][-1].price, tolerance_pct):
            clusters[-1].append(pivot)
        else:
            clusters.append([pivot])

    price = series.last.close
    out: list[Level] = []
    for cluster in clusters:
        mean = sum(s.price for s in cluster) / len(cluster)
        out.append(
            Level(
                price=mean,
                kind="resistance" if mean >= price else "support",
                touches=len(cluster),
                last_touch=max(s.ts for s in cluster),
            )
        )
    out.sort(key=lambda level: abs(level.price - price))
    return out[:max_levels]


def nearest(levels_: list[Level], price: float, kind: str) -> Level | None:
    candidates = [level for level in levels_ if level.kind == kind]
    if not candidates:
        return None
    return min(candidates, key=lambda level: abs(level.price - price))


@dataclass(frozen=True)
class FibZone:
    """Retracement levels of the most recent confirmed impulse leg."""

    swing_high: float
    swing_low: float
    direction: str  # "up" | "down" -- the direction of the impulse being retraced
    levels: dict[str, float]

    def zone_position(self, price: float) -> float | None:
        """Where price sits in the leg: 0 at the origin, 1 at the extreme."""
        span = self.swing_high - self.swing_low
        if span <= 0:
            return None
        if self.direction == "up":
            return (price - self.swing_low) / span
        return (self.swing_high - price) / span


FIB_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)


def fib_zone(series: Series, lookback: int = 3) -> FibZone | None:
    """Fibonacci retracement of the last impulse leg, or None if there isn't one.

    "The last leg" is the most recent confirmed swing high/low pair. When the
    high came last the impulse was up and the levels are supports below; when the
    low came last the impulse was down and they are resistances above.
    """
    pivots = swings(series, lookback)
    if len(pivots) < 2:
        return None
    last = pivots[-1]
    prior = next((s for s in reversed(pivots[:-1]) if s.kind != last.kind), None)
    if prior is None:
        return None

    high = max(last.price, prior.price)
    low = min(last.price, prior.price)
    span = high - low
    if span <= 0:
        return None
    direction = "up" if last.kind == "high" else "down"
    if direction == "up":
        built = {f"{ratio:.3f}": high - span * ratio for ratio in FIB_RATIOS}
    else:
        built = {f"{ratio:.3f}": low + span * ratio for ratio in FIB_RATIOS}
    return FibZone(swing_high=high, swing_low=low, direction=direction, levels=built)


def structure_score(series: Series, lookback: int = 3) -> tuple[float, str]:
    """Score price position between the nearest support and resistance, -1..+1.

    Positive means there is room above and a floor close below -- a favourable
    place to be long. The asymmetry is intentional: price pinned against
    resistance scores more negatively than the same distance above support scores
    positively, because the rejection risk at a tested shelf is the more reliable
    of the two effects.
    """
    found = levels(series, lookback)
    price = series.last.close
    support = nearest(found, price, "support")
    resistance = nearest(found, price, "resistance")

    if support is None and resistance is None:
        return 0.0, "no confirmed swing structure yet"

    # Headroom and downside, as fractions of price, capped at 3% so a distant
    # level stops mattering rather than dominating.
    cap = 0.03
    up = min(abs(resistance.distance_pct(price)), cap) if resistance else cap
    down = min(abs(support.distance_pct(price)), cap) if support else cap

    score = (up - down) / cap
    # Weight by conviction in the levels themselves.
    touches = max((level.touches for level in (support, resistance) if level), default=1)
    conviction = min(1.0, 0.5 + 0.25 * (touches - 1))
    score *= conviction

    parts: list[str] = []
    for level, way in ((resistance, "up"), (support, "down")):
        if level is None:
            continue
        touches = f"{level.touches} touch" + ("" if level.touches == 1 else "es")
        parts.append(
            f"{level.kind} {level.price:,.2f} "
            f"({abs(level.distance_pct(price)) * 100:.1f}% {way}, {touches})"
        )
    return max(-1.0, min(1.0, score)), "; ".join(parts)
