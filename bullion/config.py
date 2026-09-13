"""Every tunable number in the system, and the reason it is not a fixed constant.

Spec section 4 is explicit that layer weights are *not* fixed: they are fitted by
the walk-forward backtester and they shift by regime. That makes this module the
contract between three otherwise independent parts -- the fusion engine that
consumes weights, the backtester that produces them, and the API that reports
which profile produced a given call. Keeping them in one dataclass means a fitted
weight set can be round-tripped through JSON, stored against a backtest run, and
replayed later; a dict of floats scattered across modules cannot.

Two product decisions live here rather than in code:

* **Silver is not gold with a different ticker** (spec section 10, phase 3).
  It is roughly 1.6x as volatile, carries real industrial demand, and its COT
  positioning is a smaller, more easily squeezed market. It gets its own base
  weights and its own ATR stop multiple, not a copy of gold's.
* **The engine is allowed to say nothing.** ``confidence_floor`` is what makes
  NO_SIGNAL a first-class state. A system that always has an opinion is a system
  that is guessing for most of the week.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path

GOLD = "XAU/USD"
SILVER = "XAG/USD"
ASSETS: tuple[str, ...] = (GOLD, SILVER)

# The four fusion layers, in the order they are displayed in the UI.
LAYERS: tuple[str, ...] = ("technical", "macro", "sentiment", "positioning")


class Regime(str, Enum):
    """Which market condition the weights are being applied in.

    Regime is detected from data (see ``regime.py``), not configured. It exists
    because the same indicator reading means different things in a trending
    market and a chopping one: an EMA stack is information when ADX is 32 and
    noise when ADX is 12.
    """

    TREND = "trend"
    RANGE = "range"
    EVENT = "event"


@dataclass(frozen=True)
class LayerWeights:
    """Relative weight of each fusion layer. Not required to sum to 1.

    Normalisation happens in ``normalised()`` against the layers that actually
    have data, which is the case that matters: if the news provider is down, the
    sentiment weight has to be redistributed rather than silently counted as a
    zero score. A zero score is a neutral *opinion*; missing data is not an
    opinion at all, and conflating the two is how a signal engine quietly starts
    reporting "balanced" every time a feed breaks.
    """

    technical: float
    macro: float
    sentiment: float
    positioning: float

    def as_dict(self) -> dict[str, float]:
        return {layer: float(getattr(self, layer)) for layer in LAYERS}

    def normalised(self, available: set[str] | None = None) -> dict[str, float]:
        """Weights over the available layers, summing to 1 (empty if none are)."""
        present = set(LAYERS) if available is None else (set(available) & set(LAYERS))
        raw = {layer: max(0.0, getattr(self, layer)) for layer in present}
        total = sum(raw.values())
        if total <= 0:
            return {}
        return {layer: value / total for layer, value in raw.items()}

    def tilted(self, tilt: dict[str, float]) -> LayerWeights:
        """Multiply each weight by a regime tilt factor."""
        return LayerWeights(
            **{layer: getattr(self, layer) * tilt.get(layer, 1.0) for layer in LAYERS}
        )

    def coverage(self, available: set[str]) -> float:
        """Fraction of total weight backed by data. Feeds the confidence penalty."""
        total = sum(max(0.0, getattr(self, layer)) for layer in LAYERS)
        if total <= 0:
            return 0.0
        have = sum(max(0.0, getattr(self, layer)) for layer in (set(available) & set(LAYERS)))
        return have / total


# Starting weights, pre-backtest. These are priors, not results: the macro layer
# leads for gold because real yields and the dollar move it more than chart
# patterns do (spec section 3.3), and the technical layer leads for silver
# because silver trends and squeezes harder on flow than gold does. The
# backtester is expected to move these; `backtest.fit_weights` writes a fitted
# set and `load_settings` will read one back from BULLION_WEIGHTS.
BASE_WEIGHTS: dict[str, LayerWeights] = {
    GOLD: LayerWeights(technical=0.34, macro=0.40, sentiment=0.12, positioning=0.14),
    SILVER: LayerWeights(technical=0.42, macro=0.30, sentiment=0.13, positioning=0.15),
}

# Regime tilts, applied multiplicatively to the base weights.
#
# TREND  a confirmed trend is the one condition where chart structure carries
#        real information, so the technical layer gets more of the vote.
# RANGE  in chop, indicator crossovers generate the textbook whipsaw losses;
#        lean on the slower-moving macro picture instead.
# EVENT  FOMC/CPI weeks: positioning and the macro narrative dominate, and the
#        technical read is about to be overwritten by a print.
REGIME_TILT: dict[Regime, dict[str, float]] = {
    Regime.TREND: {"technical": 1.35, "macro": 0.95, "sentiment": 0.85, "positioning": 0.95},
    Regime.RANGE: {"technical": 0.65, "macro": 1.20, "sentiment": 1.00, "positioning": 1.15},
    Regime.EVENT: {"technical": 0.75, "macro": 1.30, "sentiment": 1.25, "positioning": 1.10},
}


@dataclass(frozen=True)
class Thresholds:
    """Where the composite score turns into a word, and where it refuses to.

    ``hold_band`` exists so a +0.08 reading is reported as HOLD rather than
    rounded up into a clean BUY (spec section 4). ``confidence_floor`` outranks
    the bands entirely: below it the state is NO_SIGNAL whatever the score says.
    """

    confidence_floor: float = 0.35
    hold_band: float = 0.15
    strong_band: float = 0.55
    # Alerting: how far confidence has to move before it is worth a push, and how
    # long to wait before re-alerting the same state. Nobody wants a ping every
    # recompute (spec section 6).
    alert_confidence_delta: float = 0.15
    alert_cooldown_minutes: int = 90


@dataclass(frozen=True)
class MetalProfile:
    """Per-asset risk geometry. Silver's numbers are wider on purpose.

    Stops are sized in ATR rather than percent because ATR adapts to the
    volatility regime: a 1% stop is loose in a quiet August and suicidal around
    an FOMC print.
    """

    asset: str
    display: str
    decimals: int
    # Stop distance in ATR(14) multiples.
    atr_stop_mult: float
    # Reward:risk at minimum and maximum confidence; interpolated by confidence,
    # so a marginal call is given a nearer target it can actually reach.
    target_r_min: float
    target_r_max: float
    # ETF whose volume stands in for the missing spot volume tape (spec 3.2).
    volume_proxy: str


METAL_PROFILES: dict[str, MetalProfile] = {
    GOLD: MetalProfile(
        asset=GOLD,
        display="Gold",
        decimals=2,
        atr_stop_mult=1.5,
        target_r_min=1.2,
        target_r_max=2.4,
        volume_proxy="GLD",
    ),
    SILVER: MetalProfile(
        asset=SILVER,
        display="Silver",
        decimals=3,
        atr_stop_mult=2.0,
        target_r_min=1.3,
        target_r_max=2.8,
        volume_proxy="SLV",
    ),
}


@dataclass(frozen=True)
class Tier:
    """A subscription tier, expressed as what it withholds (spec section 9)."""

    name: str
    delay_minutes: int
    assets: tuple[str, ...]
    reasoning: bool
    track_record: bool
    alerts: bool
    api_access: bool


TIERS: dict[str, Tier] = {
    "free": Tier(
        name="free",
        # Four hours. Long enough that the free tier is a teaser rather than the
        # product, short enough to be honest about what it is.
        delay_minutes=240,
        assets=(GOLD,),
        reasoning=False,
        track_record=True,
        alerts=False,
        api_access=False,
    ),
    "paid": Tier(
        name="paid",
        delay_minutes=0,
        assets=ASSETS,
        reasoning=True,
        track_record=True,
        alerts=True,
        api_access=False,
    ),
    "pro": Tier(
        name="pro",
        delay_minutes=0,
        assets=ASSETS,
        reasoning=True,
        track_record=True,
        alerts=True,
        api_access=True,
    ),
}

DEFAULT_STORE = Path(
    os.environ.get("BULLION_STORE", Path.home() / ".bullion" / "bullion.db")
).expanduser()


@dataclass
class Settings:
    """Runtime configuration, assembled from the environment by ``load_settings``.

    Keys are read here and nowhere else. Every provider accepts its key as a
    constructor argument so a test can build one without touching os.environ.
    """

    store_path: Path = DEFAULT_STORE
    weights: dict[str, LayerWeights] = field(default_factory=lambda: dict(BASE_WEIGHTS))
    thresholds: Thresholds = field(default_factory=Thresholds)

    twelvedata_key: str | None = None
    goldapi_key: str | None = None
    metalsapi_key: str | None = None
    fred_key: str | None = None
    finnhub_key: str | None = None
    marketaux_key: str | None = None
    anthropic_key: str | None = None

    # Model for the news re-scoring pass (spec section 3.4). Haiku is the right
    # tier: a headline classification is a one-line judgement at high volume.
    news_model: str = "claude-haiku-4-5"

    # When no price key is configured the engine runs on the synthetic generator
    # and says so in every payload. Set BULLION_ALLOW_SYNTHETIC=0 in production
    # so a missing key fails loudly instead of serving invented prices.
    allow_synthetic: bool = True

    @property
    def has_live_prices(self) -> bool:
        return bool(self.twelvedata_key or self.goldapi_key or self.metalsapi_key)

    def weights_for(self, asset: str) -> LayerWeights:
        return self.weights.get(asset, BASE_WEIGHTS.get(asset, BASE_WEIGHTS[GOLD]))

    def with_weights(self, asset: str, weights: LayerWeights) -> Settings:
        merged = dict(self.weights)
        merged[asset] = weights
        return replace(self, weights=merged)


def _flag(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Build settings from the environment.

    ``BULLION_WEIGHTS`` carries a fitted weight set as JSON -- the output of
    ``backtest.fit_weights`` -- so a deployment can adopt a new fit without a
    code change, which is the whole point of Section 4's "weights are not fixed".
    A malformed value is ignored rather than fatal: serving yesterday's priors
    beats refusing to start.
    """
    env = dict(os.environ if env is None else env)
    settings = Settings(
        store_path=Path(env.get("BULLION_STORE", str(DEFAULT_STORE))).expanduser(),
        twelvedata_key=env.get("TWELVEDATA_API_KEY") or None,
        goldapi_key=env.get("GOLDAPI_KEY") or None,
        metalsapi_key=env.get("METALS_API_KEY") or None,
        fred_key=env.get("FRED_API_KEY") or None,
        finnhub_key=env.get("FINNHUB_KEY") or None,
        marketaux_key=env.get("MARKETAUX_KEY") or None,
        anthropic_key=env.get("ANTHROPIC_API_KEY") or None,
        news_model=env.get("BULLION_NEWS_MODEL", "claude-haiku-4-5"),
        allow_synthetic=_flag(env, "BULLION_ALLOW_SYNTHETIC", True),
    )
    raw = env.get("BULLION_WEIGHTS")
    if raw:
        try:
            parsed = json.loads(raw)
            weights = dict(settings.weights)
            for asset, values in parsed.items():
                weights[asset] = LayerWeights(**{k: float(v) for k, v in values.items()})
            settings = replace(settings, weights=weights)
        except (ValueError, TypeError, KeyError):
            # Deliberately swallowed; see the docstring.
            pass
    return settings


def weights_to_json(weights: dict[str, LayerWeights]) -> str:
    """Serialise a fitted weight set into the form BULLION_WEIGHTS expects."""
    return json.dumps({asset: asdict(w) for asset, w in weights.items()}, sort_keys=True)
