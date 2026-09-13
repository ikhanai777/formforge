"""Fusion: four layer scores in, one explainable call out.

Spec section 4, and the two sentences in it that this module exists to enforce:

**"Confidence is separate from direction."** The composite score says which way; the
confidence says how much the system believes itself. They come from different inputs
on purpose -- a +0.8 composite from one screaming layer while the other three shrug is
a *low* confidence call, and any scheme that derives confidence from |score| alone
cannot express that. Confidence here is built from five terms, only one of which is
the score itself:

    conviction   how far from neutral the composite is
    agreement    how much the layers actually concur, weight-aware
    trend        ADX-derived, from the same read the regime detector used
    coverage     what fraction of the weight is backed by live data
    freshness    how current that data is, as a multiplier rather than a term

**"The app should be willing to say nothing."** NO_SIGNAL is a state, not an error.
Below the confidence floor the engine reports no call and says why, and the dashboard
shows that as a legitimate outcome. A system that always has an opinion is guessing
four days out of five, and its track record will say so eventually -- better to say it
up front.

Levels (stop and target) are structure-aware, not pure ATR multiples. A stop placed
exactly at a support shelf is a stop placed where price is most likely to wick
through; it belongs a fraction beyond the shelf. A target placed past a three-touch
resistance is a target that will not print.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .config import METAL_PROFILES, Thresholds
from .disclosure import SHORT_DISCLOSURE
from .indicators import Indicators
from .providers.prices import PriceQuality
from .regime import RegimeRead
from .scoring.layer import LayerScore
from .series import clamp
from .structure import Level, nearest


class SignalState(str, Enum):
    """The six states from spec section 4, in one ordered enum."""

    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"
    NO_SIGNAL = "NO_SIGNAL"

    @property
    def direction(self) -> int:
        return {
            SignalState.STRONG_BUY: 1,
            SignalState.BUY: 1,
            SignalState.HOLD: 0,
            SignalState.SELL: -1,
            SignalState.STRONG_SELL: -1,
            SignalState.NO_SIGNAL: 0,
        }[self]

    @property
    def strength(self) -> str:
        """Bucket used by the scorecard, which reports win rate by strength."""
        if self in (SignalState.STRONG_BUY, SignalState.STRONG_SELL):
            return "strong"
        if self in (SignalState.BUY, SignalState.SELL):
            return "normal"
        return "none"

    @property
    def actionable(self) -> bool:
        return self.direction != 0


# Confidence term weights. They sum to 1 before the freshness and data-quality
# multipliers, both of which can only reduce. Confidence is therefore capped by the
# data as well as by the model, which is the intended asymmetry: good data cannot
# manufacture conviction, but bad data can remove it.
CONFIDENCE_TERMS: dict[str, float] = {
    "conviction": 0.34,
    "agreement": 0.28,
    "trend": 0.18,
    "coverage": 0.20,
}


@dataclass(frozen=True)
class Confidence:
    """The confidence score with its inputs kept, so the gauge can be explained."""

    value: float
    conviction: float
    agreement: float
    trend: float
    coverage: float
    freshness: float
    data_quality: float
    unanimous: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": round(self.value, 4),
            "conviction": round(self.conviction, 4),
            "agreement": round(self.agreement, 4),
            "trend": round(self.trend, 4),
            "coverage": round(self.coverage, 4),
            "freshness": round(self.freshness, 4),
            "data_quality": round(self.data_quality, 4),
            "unanimous": self.unanimous,
        }

    def explain(self) -> str:
        bits = [
            f"{self.agreement * 100:.0f}% layer agreement",
            f"trend strength {self.trend * 100:.0f}%",
            f"{self.coverage * 100:.0f}% of weight backed by live data",
        ]
        if self.freshness < 0.95:
            bits.append(f"data freshness {self.freshness * 100:.0f}%")
        if self.data_quality < 0.95:
            bits.append(f"price cross-check {self.data_quality * 100:.0f}%")
        if self.unanimous:
            bits.append("all layers agree on direction")
        return "; ".join(bits)


@dataclass(frozen=True)
class Levels:
    """Suggested entry, invalidation and target. None when there is no call."""

    entry: float | None
    stop: float | None
    target: float | None
    risk_reward: float | None
    basis: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "risk_reward": round(self.risk_reward, 2) if self.risk_reward else None,
            "basis": self.basis,
        }


@dataclass(frozen=True)
class Signal:
    """The API contract from spec section 6, plus what honesty requires.

    The spec's example object is a subset of this: ``asset``, ``signal``,
    ``confidence``, ``composite_score``, ``price_at_signal``, ``suggested_stop``,
    ``suggested_target``, ``reasoning`` and ``timestamp`` all appear with those names
    and that shape, so a client written against the spec works unchanged. The
    additions are the ones the framing note demands -- which weights were used, which
    regime, how confidence was arrived at, how fresh the data was, and whether any of
    it was synthetic.
    """

    asset: str
    state: SignalState
    composite_score: float
    confidence: Confidence
    price_at_signal: float
    levels: Levels
    layers: dict[str, LayerScore]
    weights: dict[str, float]
    regime: RegimeRead
    timestamp: datetime
    interval: str = "1day"
    data_quality: dict[str, Any] = field(default_factory=dict)
    synthetic: bool = False
    notes: tuple[str, ...] = ()

    @property
    def signal(self) -> str:
        return self.state.value

    @property
    def direction(self) -> int:
        return self.state.direction

    def reasoning(self) -> dict[str, str]:
        """Spec section 6's ``reasoning`` block: one signed line per layer."""
        return {name: layer.as_text() for name, layer in sorted(self.layers.items())}

    def headline(self) -> str:
        """One sentence for a push notification. Never imperative (spec section 11)."""
        if self.state is SignalState.NO_SIGNAL:
            return (
                f"{self.asset}: no signal — confidence "
                f"{self.confidence.value * 100:.0f}% is below the floor"
            )
        return (
            f"{self.asset} {self.state.value} at {self.price_at_signal:,.2f}, "
            f"confidence {self.confidence.value * 100:.0f}%"
        )

    def as_dict(self) -> dict[str, Any]:
        profile = METAL_PROFILES.get(self.asset)
        places = profile.decimals if profile else 2
        return {
            "asset": self.asset,
            "signal": self.state.value,
            "confidence": round(self.confidence.value, 4),
            "composite_score": round(self.composite_score, 4),
            "price_at_signal": round(self.price_at_signal, places),
            "suggested_stop": round(self.levels.stop, places) if self.levels.stop else None,
            "suggested_target": (
                round(self.levels.target, places) if self.levels.target else None
            ),
            "reasoning": self.reasoning(),
            "timestamp": self.timestamp.isoformat(),
            # Beyond the spec's contract, and the reason this product is defensible:
            "interval": self.interval,
            "strength": self.state.strength,
            "confidence_detail": self.confidence.as_dict(),
            "confidence_explained": self.confidence.explain(),
            "levels": self.levels.as_dict(),
            "layers": {name: layer.as_dict() for name, layer in self.layers.items()},
            "weights": {name: round(weight, 4) for name, weight in self.weights.items()},
            "regime": self.regime.as_dict(),
            "data_quality": self.data_quality,
            "synthetic": self.synthetic,
            "notes": list(self.notes),
            "disclosure": SHORT_DISCLOSURE,
        }


# Floor on the dispersion denominator. Without it, four layers all reading +0.02 would
# divide a near-zero spread by a near-zero magnitude and the ratio would be meaningless.
AGREEMENT_SCALE_FLOOR = 0.15


def _agreement(
    layers: dict[str, LayerScore], weights: dict[str, float], composite: float
) -> float:
    """Weighted concordance of the layers with the composite, 0..1.

    Dispersion measured *relative to the size of the opinions*, which is the correction
    that makes this term mean anything. Absolute mean deviation alone scores one layer at
    +1.0 against three layers at 0.0 as 0.63 agreement -- because three silent layers are
    only 0.25 away from the resulting composite -- and the engine then publishes a
    single-layer call at 61% confidence. Dividing by the mean absolute layer score instead
    asks the right question: is the spread small *compared to the signal*? For that case it
    gives 0, which is the correct reading of one loud layer and three shrugs.

    Mean absolute deviation rather than variance, still: one layer screaming the opposite
    way should cost about twice what one layer leaning the opposite way costs, not four
    times. Squared error lets a single outlier dominate.
    """
    live = {name: layer for name, layer in layers.items() if layer.available}
    if not live:
        return 0.0
    total_weight = sum(weights.get(name, 0.0) for name in live)
    if total_weight <= 0:
        return 0.0
    deviation = (
        sum(
            weights.get(name, 0.0) * abs(layer.score - composite)
            for name, layer in live.items()
        )
        / total_weight
    )
    magnitude = (
        sum(weights.get(name, 0.0) * abs(layer.score) for name, layer in live.items())
        / total_weight
    )
    scale = max(AGREEMENT_SCALE_FLOOR, magnitude)
    return clamp(1.0 - deviation / scale, 0.0, 1.0)


def _unanimous(layers: dict[str, LayerScore], composite: float) -> bool:
    """True when every live layer with an opinion leans the composite's way."""
    if abs(composite) < 0.05:
        return False
    sign = 1 if composite > 0 else -1
    opinions = [
        layer.score for layer in layers.values() if layer.available and abs(layer.score) >= 0.05
    ]
    return len(opinions) >= 2 and all((1 if s > 0 else -1) == sign for s in opinions)


def confidence_of(
    layers: dict[str, LayerScore],
    weights: dict[str, float],
    composite: float,
    regime: RegimeRead,
    coverage: float,
    price_quality: PriceQuality | None = None,
) -> Confidence:
    """Build the confidence score from its five inputs. See the module docstring."""
    conviction = clamp(abs(composite) / 0.7, 0.0, 1.0)
    agreement = _agreement(layers, weights, composite)
    trend = regime.trend_strength

    live = [layer for layer in layers.values() if layer.available]
    if live:
        weight_sum = sum(weights.get(layer.name, 0.0) for layer in live)
        fresh = (
            sum(weights.get(layer.name, 0.0) * layer.freshness for layer in live) / weight_sum
            if weight_sum > 0
            else 0.0
        )
    else:
        fresh = 0.0

    data_quality = price_quality.agreement if price_quality else 0.8

    base = (
        CONFIDENCE_TERMS["conviction"] * conviction
        + CONFIDENCE_TERMS["agreement"] * agreement
        + CONFIDENCE_TERMS["trend"] * trend
        + CONFIDENCE_TERMS["coverage"] * clamp(coverage, 0.0, 1.0)
    )
    unanimous = _unanimous(layers, composite)
    if unanimous:
        # A modest bonus. Four layers agreeing is genuinely informative; making it
        # large would let the engine reach high confidence on four weak agreeing reads.
        base += 0.06

    # Stale or disputed data can only reduce confidence, never raise it.
    #
    # The freshness floor is 0.35 rather than something gentler on purpose: data four
    # intervals past due is not merely less trustworthy, it is describing a different
    # market. At freshness 0 even a unanimous, maximum-conviction read lands at roughly
    # the confidence floor, so it is published as NO_SIGNAL -- which is the correct answer
    # when no layer has current data. The price-quality floor is higher (0.75) because a
    # single feed disagreeing is evidence about the feed, not about the market.
    value = base * (0.35 + 0.65 * clamp(fresh, 0.0, 1.0)) * (0.75 + 0.25 * data_quality)

    return Confidence(
        value=clamp(value, 0.0, 1.0),
        conviction=conviction,
        agreement=agreement,
        trend=trend,
        coverage=clamp(coverage, 0.0, 1.0),
        freshness=clamp(fresh, 0.0, 1.0),
        data_quality=clamp(data_quality, 0.0, 1.0),
        unanimous=unanimous,
    )


def state_of(composite: float, confidence: float, thresholds: Thresholds) -> SignalState:
    """Turn score and confidence into one of six states.

    The confidence floor is checked first and outranks the score: a +0.9 composite the
    engine does not believe is NO_SIGNAL, not STRONG_BUY.
    """
    if confidence < thresholds.confidence_floor:
        return SignalState.NO_SIGNAL
    if abs(composite) < thresholds.hold_band:
        return SignalState.HOLD
    if composite >= thresholds.strong_band:
        return SignalState.STRONG_BUY
    if composite <= -thresholds.strong_band:
        return SignalState.STRONG_SELL
    return SignalState.BUY if composite > 0 else SignalState.SELL


def levels_for(
    asset: str,
    state: SignalState,
    price: float,
    atr: float | None,
    confidence: float,
    structure: list[Level] | None = None,
) -> Levels:
    """Structure-aware stop and target. Returns empties for HOLD/NO_SIGNAL.

    Deliberately no levels without a call: a stop on a HOLD invites the reader to
    treat the HOLD as a position, which is how a "wait" becomes a trade.
    """
    profile = METAL_PROFILES.get(asset)
    if state.direction == 0 or profile is None:
        return Levels(None, None, None, None, "no directional call — no levels suggested")
    if atr is None or atr <= 0:
        # 1% stands in for a missing ATR; flagged in the basis so nobody mistakes it
        # for a volatility-derived level.
        atr = price * 0.01
        basis_note = "ATR unavailable, 1% of price substituted"
    else:
        basis_note = f"ATR {atr:,.2f}"

    long_ = state.direction > 0
    stop_distance = atr * profile.atr_stop_mult
    stop = price - stop_distance if long_ else price + stop_distance

    adjustments: list[str] = []
    if structure:
        shelf = nearest(structure, price, "support" if long_ else "resistance")
        if shelf is not None:
            beyond = shelf.price * (1 - 0.0015) if long_ else shelf.price * (1 + 0.0015)
            # Only pull the stop to the shelf if the shelf is inside ~1.6x the ATR
            # stop; a shelf further away than that is not this trade's invalidation.
            within = abs(shelf.price - price) <= stop_distance * 1.6
            tighter_ok = (beyond < price) if long_ else (beyond > price)
            if within and tighter_ok:
                stop = beyond
                adjustments.append(
                    f"stop placed just beyond {shelf.kind} {shelf.price:,.2f} "
                    f"({shelf.touches} touches)"
                )

    risk = abs(price - stop)
    if risk <= 0:
        return Levels(price, None, None, None, "stop would be at entry; no levels suggested")

    # Reward:risk scales with confidence -- a marginal call gets a nearer target it can
    # actually reach, rather than a flattering one it cannot.
    span = profile.target_r_max - profile.target_r_min
    r_multiple = profile.target_r_min + span * clamp(confidence, 0.0, 1.0)
    target = price + risk * r_multiple if long_ else price - risk * r_multiple

    if structure:
        wall = nearest(structure, price, "resistance" if long_ else "support")
        if wall is not None and wall.touches >= 2:
            blocked = (wall.price < target) if long_ else (wall.price > target)
            beyond_entry = (wall.price > price) if long_ else (wall.price < price)
            if blocked and beyond_entry:
                # Take profit in front of the wall, not on the far side of it.
                target = wall.price * (1 - 0.001) if long_ else wall.price * (1 + 0.001)
                adjustments.append(
                    f"target trimmed to sit in front of {wall.kind} {wall.price:,.2f}"
                )
                r_multiple = abs(target - price) / risk

    basis = f"{basis_note} x {profile.atr_stop_mult:g} stop, {r_multiple:.1f}R target"
    if adjustments:
        basis = f"{basis}; " + "; ".join(adjustments)

    return Levels(
        entry=price,
        stop=stop,
        target=target,
        risk_reward=abs(target - price) / risk if risk else None,
        basis=basis,
    )


def fuse(
    asset: str,
    layers: dict[str, LayerScore],
    weights: dict[str, float],
    indicators: Indicators,
    regime: RegimeRead,
    thresholds: Thresholds | None = None,
    price_quality: PriceQuality | None = None,
    structure: list[Level] | None = None,
    coverage: float | None = None,
    now: datetime | None = None,
    synthetic: bool = False,
    notes: tuple[str, ...] = (),
) -> Signal:
    """Combine layer scores into a signal.

    ``weights`` must already be normalised over the *available* layers -- the caller
    does that via ``LayerWeights.normalised(available)``, because the caller is also
    the one that knows what ``coverage`` was lost in the process.
    """
    thresholds = thresholds or Thresholds()
    live = {name: layer for name, layer in layers.items() if layer.available}
    composite = sum(weights.get(name, 0.0) * layer.score for name, layer in live.items())
    composite = clamp(composite)

    if coverage is None:
        # Fall back to the share of supplied weight that is live, which is right when
        # the caller passed unnormalised weights and harmless when it did not.
        total = sum(weights.values()) or 1.0
        coverage = sum(weights.get(name, 0.0) for name in live) / total

    confidence = confidence_of(
        layers=layers,
        weights=weights,
        composite=composite,
        regime=regime,
        coverage=coverage,
        price_quality=price_quality,
    )
    state = state_of(composite, confidence.value, thresholds)
    price = indicators.close
    levels = levels_for(
        asset=asset,
        state=state,
        price=price,
        atr=indicators.atr14,
        confidence=confidence.value,
        structure=structure,
    )

    extra: list[str] = list(notes)
    dark = [name for name, layer in layers.items() if not layer.available]
    if dark:
        extra.append(
            f"{', '.join(sorted(dark))} layer(s) unavailable; weight redistributed and "
            "confidence reduced"
        )
    if state is SignalState.NO_SIGNAL:
        extra.append(
            f"confidence {confidence.value:.2f} is below the {thresholds.confidence_floor:.2f} "
            "floor, so no directional call is published"
        )

    return Signal(
        asset=asset,
        state=state,
        composite_score=composite,
        confidence=confidence,
        price_at_signal=price,
        levels=levels,
        layers=layers,
        weights=weights,
        regime=regime,
        timestamp=now or datetime.now(timezone.utc),
        interval=indicators.interval,
        data_quality=price_quality.as_dict() if price_quality else {},
        synthetic=synthetic,
        notes=tuple(extra),
    )
