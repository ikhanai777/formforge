"""Which market condition we are in, and therefore which weight profile applies.

Spec section 4: weights "can shift by regime (e.g., macro layer weighted higher
around FOMC weeks, technical layer weighted higher in strong-trend/low-news
periods)". This module is that sentence, made checkable.

Regime is *detected*, never configured, and the detection is deliberately crude --
three states off two inputs. A regime classifier with twelve states and a fitted
transition matrix is a second model to validate, and the backtester would have to
prove it earns its complexity. Three states, one of which is a calendar lookup, is
defensible from first principles: an FOMC decision in 18 hours genuinely does make
the chart less informative, and ADX genuinely does separate trend from chop.

Ordering matters: EVENT outranks TREND. A clean EMA stack into a Fed decision is not
a reason for more technical weight; it is a position about to be repriced by
something the chart does not know.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import REGIME_TILT, LayerWeights, Regime, Settings
from .indicators import Indicators
from .providers.base import EconomicEvent

# ADX above this is a trend (the conventional threshold, and the one a user's own
# chart will show).
TREND_ADX = 25.0
# Hours before a high-importance event at which the EVENT regime takes over. 48h
# covers the pre-positioning drift, which is where the damage happens.
EVENT_WINDOW_HOURS = 48.0


@dataclass(frozen=True)
class RegimeRead:
    regime: Regime
    reason: str
    # 0..1 strength of the trend read, reused by the confidence calculation so the
    # two cannot disagree about how trending the market is.
    trend_strength: float
    event: EconomicEvent | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "regime": self.regime.value,
            "reason": self.reason,
            "trend_strength": round(self.trend_strength, 3),
            "event": self.event.name if self.event else None,
            "event_hours": round(self.event.hours_until(), 1) if self.event else None,
        }


def trend_strength(adx: float | None) -> float:
    """Map ADX onto 0..1, saturating at 35.

    Above roughly 35 the trend is not "more established", it is extended -- and
    treating ADX 55 as a stronger mandate than ADX 35 is how a system buys the top
    of a parabolic move with maximum confidence.
    """
    if adx is None:
        return 0.4
    return max(0.0, min(1.0, (adx - 12.0) / (35.0 - 12.0)))


def detect(
    indicators: Indicators,
    events: list[EconomicEvent] | None = None,
    now: datetime | None = None,
) -> RegimeRead:
    now = now or datetime.now(timezone.utc)
    strength = trend_strength(indicators.adx14)

    upcoming = [
        event
        for event in (events or [])
        if event.importance == "high" and 0 <= event.hours_until(now) <= EVENT_WINDOW_HOURS
    ]
    if upcoming:
        event = min(upcoming, key=lambda e: e.hours_until(now))
        return RegimeRead(
            regime=Regime.EVENT,
            reason=(
                f"{event.name} in {event.hours_until(now):.0f}h — macro and positioning "
                "weighted up, technical weighted down"
            ),
            trend_strength=strength,
            event=event,
        )

    adx = indicators.adx14
    if adx is not None and adx >= TREND_ADX:
        rising = (
            indicators.adx14_prev is not None and adx > indicators.adx14_prev
        )
        return RegimeRead(
            regime=Regime.TREND,
            reason=(
                f"ADX {adx:.0f} and {'rising' if rising else 'easing'} — trending, "
                "technical weighted up"
            ),
            trend_strength=strength,
        )

    shown = f"{adx:.0f}" if adx is not None else "unavailable"
    return RegimeRead(
        regime=Regime.RANGE,
        reason=(
            f"ADX {shown} — no trend, technical weighted down in favour of macro "
            "and positioning"
        ),
        trend_strength=strength,
    )


def weights_for(settings: Settings, asset: str, regime: Regime) -> LayerWeights:
    """The asset's base weights with the regime tilt applied.

    The tilt is multiplicative and the result is renormalised downstream, so a tilt
    table that does not preserve total weight is fine -- only the ratios matter.
    """
    return settings.weights_for(asset).tilted(REGIME_TILT[regime])
