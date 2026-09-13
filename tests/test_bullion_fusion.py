"""Fusion: the two claims in spec section 4 that the product rests on.

1. Confidence is separate from direction. One screaming layer against three shrugs is a
   *low* confidence call, and the suite proves the implementation can express that.
2. The engine is allowed to say nothing. NO_SIGNAL is a state below the confidence floor,
   it outranks the score, and it suppresses levels.

Plus the level geometry, which is where a signal stops being an opinion and becomes
something a person would act on: a stop belongs just beyond a shelf, not at it, and a
target belongs in front of a wall, not on the far side.
"""

from __future__ import annotations

import pytest
from bullion_fixtures import make_indicators, make_layer, make_regime, trend_up

from bullion.config import GOLD, METAL_PROFILES, SILVER, Thresholds
from bullion.fusion import (
    Signal,
    SignalState,
    confidence_of,
    fuse,
    levels_for,
    state_of,
)
from bullion.structure import Level

EVEN = {"technical": 0.25, "macro": 0.25, "sentiment": 0.25, "positioning": 0.25}


def _layers(**scores) -> dict:
    return {name: make_layer(name, score) for name, score in scores.items()}


class TestSignalState:
    def test_direction_and_strength_buckets(self):
        assert SignalState.STRONG_BUY.direction == 1
        assert SignalState.SELL.direction == -1
        assert SignalState.HOLD.direction == 0
        assert SignalState.NO_SIGNAL.direction == 0
        assert SignalState.STRONG_SELL.strength == "strong"
        assert SignalState.BUY.strength == "normal"
        assert SignalState.HOLD.strength == "none"
        assert SignalState.BUY.actionable and not SignalState.HOLD.actionable

    def test_state_thresholds(self):
        t = Thresholds()
        assert state_of(0.8, 0.7, t) is SignalState.STRONG_BUY
        assert state_of(0.3, 0.7, t) is SignalState.BUY
        assert state_of(0.05, 0.7, t) is SignalState.HOLD
        assert state_of(-0.3, 0.7, t) is SignalState.SELL
        assert state_of(-0.8, 0.7, t) is SignalState.STRONG_SELL

    def test_confidence_floor_outranks_the_score(self):
        # A +0.9 composite the engine does not believe is NO_SIGNAL, not STRONG_BUY.
        assert state_of(0.9, 0.2, Thresholds()) is SignalState.NO_SIGNAL

    def test_a_weak_lean_is_reported_as_hold_not_rounded_up(self):
        assert state_of(0.12, 0.9, Thresholds()) is SignalState.HOLD


class TestConfidence:
    def test_agreement_raises_confidence_at_equal_composite(self):
        # Both cases produce the same composite (+0.4). The agreeing one must be more
        # confident; if it is not, confidence is just |score| wearing a disguise.
        agreeing = _layers(technical=0.4, macro=0.4, sentiment=0.4, positioning=0.4)
        split = _layers(technical=1.0, macro=0.2, sentiment=0.2, positioning=0.2)
        regime = make_regime()
        a = confidence_of(agreeing, EVEN, 0.4, regime, 1.0)
        b = confidence_of(split, EVEN, 0.4, regime, 1.0)
        assert a.agreement > b.agreement
        assert a.value > b.value

    def test_one_loud_layer_against_three_shrugs_is_low_confidence(self):
        loud = _layers(technical=1.0, macro=0.0, sentiment=0.0, positioning=0.0)
        composite = sum(EVEN[k] * v.score for k, v in loud.items())
        conf = confidence_of(loud, EVEN, composite, make_regime(), 1.0)
        assert conf.value < 0.5
        assert conf.unanimous is False

    def test_unanimity_is_a_modest_bonus_not_a_licence(self):
        unanimous = _layers(technical=0.3, macro=0.3, sentiment=0.3, positioning=0.3)
        conf = confidence_of(unanimous, EVEN, 0.3, make_regime(), 1.0)
        assert conf.unanimous is True
        # Four weak agreeing reads must not reach high confidence.
        assert conf.value < 0.8

    def test_missing_coverage_costs_confidence(self):
        layers = _layers(technical=0.5, macro=0.5)
        layers["sentiment"] = make_layer("sentiment", 0.0, available=False)
        layers["positioning"] = make_layer("positioning", 0.0, available=False)
        full = confidence_of(
            _layers(technical=0.5, macro=0.5, sentiment=0.5, positioning=0.5),
            EVEN, 0.5, make_regime(), 1.0,
        )
        partial = confidence_of(
            layers, {"technical": 0.5, "macro": 0.5}, 0.5, make_regime(), 0.5
        )
        assert partial.coverage == 0.5
        assert partial.value < full.value

    def test_stale_data_can_only_reduce_confidence(self):
        fresh = _layers(technical=0.5, macro=0.5, sentiment=0.5, positioning=0.5)
        stale = {
            name: make_layer(name, 0.5, freshness=0.1) for name in fresh
        }
        a = confidence_of(fresh, EVEN, 0.5, make_regime(), 1.0)
        b = confidence_of(stale, EVEN, 0.5, make_regime(), 1.0)
        assert b.value < a.value
        assert b.freshness < 0.2

    def test_trend_strength_feeds_in_from_the_same_read_as_the_regime(self):
        layers = _layers(technical=0.5, macro=0.5, sentiment=0.5, positioning=0.5)
        strong = confidence_of(layers, EVEN, 0.5, make_regime(trend_strength=1.0), 1.0)
        weak = confidence_of(layers, EVEN, 0.5, make_regime(trend_strength=0.0), 1.0)
        assert strong.value > weak.value

    def test_explain_mentions_every_live_term(self):
        conf = confidence_of(
            _layers(technical=0.5, macro=0.5), {"technical": 0.5, "macro": 0.5}, 0.5,
            make_regime(), 1.0,
        )
        text = conf.explain()
        assert "agreement" in text and "trend strength" in text and "live data" in text

    def test_confidence_is_bounded(self):
        best = _layers(technical=1.0, macro=1.0, sentiment=1.0, positioning=1.0)
        conf = confidence_of(best, EVEN, 1.0, make_regime(trend_strength=1.0), 1.0)
        assert 0.0 <= conf.value <= 1.0


class TestLevels:
    def test_no_levels_without_a_direction(self):
        for state in (SignalState.HOLD, SignalState.NO_SIGNAL):
            levels = levels_for(GOLD, state, 2400.0, 24.0, 0.6)
            assert levels.stop is None and levels.target is None
            assert "no directional call" in levels.basis

    def test_stop_is_an_atr_multiple_below_for_a_long(self):
        profile = METAL_PROFILES[GOLD]
        levels = levels_for(GOLD, SignalState.BUY, 2400.0, 20.0, 0.5)
        assert levels.stop == pytest.approx(2400.0 - 20.0 * profile.atr_stop_mult)
        assert levels.target > 2400.0

    def test_short_levels_are_mirrored(self):
        levels = levels_for(GOLD, SignalState.SELL, 2400.0, 20.0, 0.5)
        assert levels.stop > 2400.0
        assert levels.target < 2400.0

    def test_silver_gets_a_wider_stop_than_gold(self):
        assert (
            METAL_PROFILES[SILVER].atr_stop_mult > METAL_PROFILES[GOLD].atr_stop_mult
        )
        gold = levels_for(GOLD, SignalState.BUY, 100.0, 1.0, 0.5)
        silver = levels_for(SILVER, SignalState.BUY, 100.0, 1.0, 0.5)
        assert (100.0 - silver.stop) > (100.0 - gold.stop)

    def test_higher_confidence_earns_a_further_target(self):
        low = levels_for(GOLD, SignalState.BUY, 2400.0, 20.0, 0.1)
        high = levels_for(GOLD, SignalState.BUY, 2400.0, 20.0, 0.95)
        assert high.target > low.target
        assert high.risk_reward > low.risk_reward

    def test_missing_atr_falls_back_and_says_so(self):
        levels = levels_for(GOLD, SignalState.BUY, 2400.0, None, 0.6)
        assert levels.stop == pytest.approx(2400.0 - 24.0 * METAL_PROFILES[GOLD].atr_stop_mult)
        assert "ATR unavailable" in levels.basis

    def test_stop_is_placed_just_beyond_a_nearby_support_shelf(self):
        from datetime import datetime, timezone

        shelf = Level(
            price=2380.0, kind="support", touches=4, last_touch=datetime.now(timezone.utc)
        )
        levels = levels_for(GOLD, SignalState.BUY, 2400.0, 20.0, 0.6, structure=[shelf])
        assert levels.stop < 2380.0
        assert levels.stop > 2380.0 * 0.99
        assert "just beyond support" in levels.basis

    def test_target_is_trimmed_in_front_of_a_tested_wall(self):
        from datetime import datetime, timezone

        wall = Level(
            price=2420.0, kind="resistance", touches=3, last_touch=datetime.now(timezone.utc)
        )
        levels = levels_for(GOLD, SignalState.BUY, 2400.0, 20.0, 0.9, structure=[wall])
        assert levels.target < 2420.0
        assert "in front of resistance" in levels.basis

    def test_a_single_touch_level_does_not_move_the_target(self):
        from datetime import datetime, timezone

        weak = Level(
            price=2420.0, kind="resistance", touches=1, last_touch=datetime.now(timezone.utc)
        )
        trimmed = levels_for(GOLD, SignalState.BUY, 2400.0, 20.0, 0.9, structure=[weak])
        plain = levels_for(GOLD, SignalState.BUY, 2400.0, 20.0, 0.9)
        assert trimmed.target == pytest.approx(plain.target)


class TestFuse:
    def _fuse(self, layers, weights=None, **kwargs):
        return fuse(
            asset=GOLD,
            layers=layers,
            weights=weights or EVEN,
            indicators=make_indicators(),
            regime=make_regime(),
            **kwargs,
        )

    def test_composite_is_the_weighted_mean_of_live_layers(self):
        signal = self._fuse(_layers(technical=0.8, macro=0.4, sentiment=0.0, positioning=0.0))
        assert signal.composite_score == pytest.approx(0.3)

    def test_a_dark_layer_is_excluded_and_noted(self):
        layers = _layers(technical=0.8, macro=0.8, sentiment=0.8)
        layers["positioning"] = make_layer("positioning", 0.0, available=False)
        weights = {"technical": 1 / 3, "macro": 1 / 3, "sentiment": 1 / 3}
        signal = self._fuse(layers, weights, coverage=0.75)
        # A dark layer must not drag the composite toward zero as if it were neutral.
        assert signal.composite_score == pytest.approx(0.8)
        assert any("positioning" in note for note in signal.notes)

    def test_no_signal_when_the_data_is_too_stale_to_believe(self):
        # A strong, unanimous read on data nobody should trust. Every term that could
        # raise confidence is present and the freshness multiplier still takes it under
        # the floor -- which is the asymmetry the confidence design is built around.
        stale = {
            name: make_layer(name, 0.9, freshness=0.0)
            for name in ("technical", "macro", "sentiment", "positioning")
        }
        signal = self._fuse(stale)
        assert signal.state is SignalState.NO_SIGNAL
        assert any("below the" in note for note in signal.notes)
        assert signal.levels.stop is None and signal.levels.target is None

    def test_payload_matches_the_spec_contract(self):
        signal = self._fuse(_layers(technical=0.8, macro=0.7, sentiment=0.6, positioning=0.5))
        payload = signal.as_dict()
        # Spec section 6's example object, field for field.
        for key in (
            "asset",
            "signal",
            "confidence",
            "composite_score",
            "price_at_signal",
            "suggested_stop",
            "suggested_target",
            "reasoning",
            "timestamp",
        ):
            assert key in payload
        assert set(payload["reasoning"]) == {
            "technical", "macro", "sentiment", "positioning"
        }
        assert payload["reasoning"]["technical"].startswith("+0.50 — ") or payload[
            "reasoning"
        ]["technical"].startswith("+0.80 — ")
        # And the additions that make it defensible.
        assert payload["disclosure"]
        assert "confidence_detail" in payload and "regime" in payload

    def test_headline_is_never_imperative(self):
        signal = self._fuse(_layers(technical=0.8, macro=0.8, sentiment=0.8, positioning=0.8))
        text = signal.headline().lower()
        assert "you should" not in text
        assert "buy now" not in text
        assert signal.asset in signal.headline()

    def test_synthetic_flag_and_notes_propagate(self):
        signal = self._fuse(
            _layers(technical=0.5, macro=0.5, sentiment=0.5, positioning=0.5),
            synthetic=True,
            notes=("SYNTHETIC DATA — generated",),
        )
        assert signal.synthetic is True
        assert signal.as_dict()["synthetic"] is True
        assert any("SYNTHETIC" in note for note in signal.notes)

    def test_real_series_produces_a_coherent_signal(self):
        from bullion.indicators import compute
        from bullion.scoring import technical as tech
        from bullion.structure import levels as structure_levels

        series = trend_up(300)
        indicators = compute(series)
        layers = {
            "technical": tech.score(series, indicators),
            "macro": make_layer("macro", 0.4),
            "sentiment": make_layer("sentiment", 0.2),
            "positioning": make_layer("positioning", 0.3),
        }
        signal = fuse(
            asset=GOLD,
            layers=layers,
            weights=EVEN,
            indicators=indicators,
            regime=make_regime(),
            structure=structure_levels(series),
        )
        assert isinstance(signal, Signal)
        assert signal.state.direction == 1
        assert signal.levels.stop < signal.price_at_signal < signal.levels.target
