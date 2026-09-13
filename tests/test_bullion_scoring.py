"""The four scoring layers.

What is worth testing in a scorer is not the arithmetic, it is the *judgements*: that ADX
gates rather than votes, that RSI 75 means opposite things in a trend and a range, that a
rise in real yields is bearish for gold, that crowded positioning flips from confirming to
contrarian, and that a thin news day produces a small score rather than a confident one.
Each of those is a decision someone could plausibly have implemented backwards, and each
one would produce a system that is confidently wrong rather than visibly broken.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from bullion_fixtures import (
    choppy,
    make_cot,
    make_headline,
    make_indicators,
    make_macro,
    ramp,
    trend_down,
    trend_up,
)

from bullion.config import GOLD, SILVER
from bullion.indicators import compute
from bullion.scoring import macro as macro_layer
from bullion.scoring import positioning as pos_layer
from bullion.scoring import sentiment as sent_layer
from bullion.scoring import technical as tech_layer
from bullion.scoring.layer import LayerScore, norm, unavailable, weighted
from bullion.scoring.llm_news import LexiconScorer, NewsScore

UTC = timezone.utc


class TestLayerScore:
    def test_scores_are_clamped_to_the_contract(self):
        assert LayerScore("x", 5.0, "r").score == 1.0
        assert LayerScore("x", -5.0, "r").score == -1.0

    def test_unavailable_is_not_neutral(self):
        layer = unavailable("macro", "FRED is down")
        assert layer.available is False
        assert layer.freshness == 0.0
        assert "FRED is down" in layer.reasoning

    def test_signed_label_matches_the_spec_format(self):
        assert LayerScore("technical", 0.62, "r").as_text().startswith("+0.62 — ")

    def test_weighted_renormalises_over_present_parts(self):
        assert weighted({"a": (1.0, 0.5), "b": (-1.0, 0.5)}) == pytest.approx(0.0)
        assert weighted({"a": (1.0, 0.5)}) == pytest.approx(1.0)
        assert weighted({}) == 0.0

    def test_norm_saturates_at_the_scale(self):
        assert norm(0.005, 0.005) == pytest.approx(1.0)
        assert norm(0.05, 0.005) == pytest.approx(1.0)
        assert norm(0.0, 0.005) == 0.0


class TestTechnical:
    def test_uptrend_scores_positive_and_downtrend_negative(self):
        up = trend_up(300)
        down = trend_down(300)
        assert tech_layer.score(up, compute(up)).score > 0.3
        assert tech_layer.score(down, compute(down)).score < -0.3

    def test_adx_gates_the_trend_components(self):
        # Same EMA stack, different trend strength. The gated version must be closer to
        # neutral -- that is the entire purpose of the gate.
        strong = make_indicators(adx14=35.0)
        weak = make_indicators(adx14=11.0)
        series = choppy(200)
        strong_score = tech_layer.score(series, strong).score
        weak_score = tech_layer.score(series, weak).score
        assert abs(weak_score) < abs(strong_score)
        assert tech_layer.adx_gate(35.0) == 1.0
        assert tech_layer.adx_gate(11.0) == pytest.approx(0.2)
        assert tech_layer.adx_gate(None) == 0.6

    def test_overbought_rsi_is_continuation_in_a_trend_and_exhaustion_in_a_range(self):
        trending = make_indicators(rsi14=75.0, adx14=33.0)
        ranging = make_indicators(rsi14=75.0, adx14=12.0)
        series = choppy(200)
        trend_read = tech_layer.score(series, trending)
        range_read = tech_layer.score(series, ranging)
        assert trend_read.components["rsi"] > 0
        assert range_read.components["rsi"] < 0
        assert "trending" in trend_read.reasoning
        assert "in a range" in range_read.reasoning

    def test_oversold_rsi_in_a_downtrend_is_not_a_buy(self):
        read = tech_layer.score(choppy(200), make_indicators(rsi14=22.0, adx14=33.0))
        assert read.components["rsi"] <= 0

    def test_volume_confirms_direction_and_cannot_create_one(self):
        up_on_volume = make_indicators(close=2400.0, prev_close=2380.0, volume=200_000.0)
        down_on_volume = make_indicators(close=2360.0, prev_close=2380.0, volume=200_000.0)
        series = choppy(200)
        assert tech_layer.score(series, up_on_volume).components["volume"] > 0
        assert tech_layer.score(series, down_on_volume).components["volume"] < 0
        # No volume proxy at all is no opinion, not a bearish one.
        none = make_indicators(volume=0.0, volume_sma20=None)
        assert tech_layer.score(series, none).components["volume"] == 0.0

    def test_thin_volume_counts_for_less_than_heavy_volume(self):
        series = choppy(200)
        thin = make_indicators(close=2400.0, prev_close=2380.0, volume=40_000.0)
        heavy = make_indicators(close=2400.0, prev_close=2380.0, volume=250_000.0)
        assert (
            tech_layer.score(series, thin).components["volume"]
            < tech_layer.score(series, heavy).components["volume"]
        )

    def test_reasoning_names_the_numbers_a_trader_would_check(self):
        read = tech_layer.score(trend_up(300), compute(trend_up(300)))
        assert "EMA" in read.reasoning
        assert "RSI" in read.reasoning
        assert "ADX" in read.reasoning


class TestMacro:
    def _macro(self, real: list[float], dollar: list[float]) -> dict:
        return {
            "DFII10": make_macro("DFII10", real, units="percent"),
            "DTWEXBGS": make_macro("DTWEXBGS", dollar, units="index"),
        }

    def test_rising_real_yields_are_bearish_for_gold(self):
        # The single most important sign in the system. Getting it backwards would make
        # the engine confidently wrong in exactly the conditions that matter most.
        rising = self._macro(ramp(1.6, 2.2, 150, 0.03), [120.0] * 150)
        read = macro_layer.score(GOLD, rising)
        assert read.score < 0
        assert read.components["real_yield"] < 0
        assert "rising" in read.reasoning

    def test_falling_real_yields_are_bullish_for_gold(self):
        falling = self._macro(ramp(2.2, 1.6, 150, 0.03), [120.0] * 150)
        read = macro_layer.score(GOLD, falling)
        assert read.score > 0
        assert read.components["real_yield"] > 0

    def test_a_rising_dollar_is_bearish(self):
        read = macro_layer.score(
            GOLD, self._macro([2.0] * 150, ramp(118.0, 126.0, 150, 0.4))
        )
        assert read.components["dollar"] < 0

    def test_unavailable_when_both_load_bearing_series_are_missing(self):
        read = macro_layer.score(GOLD, {"CPIAUCSL": make_macro("CPIAUCSL", [300.0] * 40)})
        assert read.available is False
        assert "real-yield" in read.reasoning

    def test_silver_and_gold_weight_the_curve_differently(self):
        macro = self._macro(ramp(2.2, 1.6, 150, 0.03), [120.0] * 150)
        macro["T10Y2Y"] = make_macro("T10Y2Y", ramp(-0.4, 0.3, 150, 0.02), units="percent")
        gold = macro_layer.score(GOLD, macro)
        silver = macro_layer.score(SILVER, macro)
        # Same data, different profile: silver's curve conviction is halved because
        # steepening helps gold through the cuts channel and silver only through growth.
        assert gold.components["curve"] != silver.components["curve"]
        assert abs(silver.components["curve"]) < abs(gold.components["curve"])

    def test_a_stale_series_costs_freshness_not_score(self):
        old = datetime.now(UTC) - timedelta(days=30)
        stale = {
            "DFII10": make_macro("DFII10", ramp(2.2, 1.6, 150, 0.03), end=old),
            "DTWEXBGS": make_macro("DTWEXBGS", [120.0] * 150, end=old),
        }
        fresh = self._macro(ramp(2.2, 1.6, 150, 0.03), [120.0] * 150)
        stale_read = macro_layer.score(GOLD, stale)
        fresh_read = macro_layer.score(GOLD, fresh)
        assert stale_read.freshness < fresh_read.freshness
        # The opinion is unchanged; only the trust in it moved.
        assert stale_read.score == pytest.approx(fresh_read.score)


class TestPositioning:
    def test_too_little_history_is_unavailable(self):
        assert pos_layer.score(GOLD, make_cot(nets=[100_000.0] * 10)).available is False
        assert pos_layer.score(GOLD, None).available is False

    def test_adding_length_from_a_neutral_base_is_confirming(self):
        # A history that has seen both extremes, with the latest reading mid-range and
        # rising. That combination -- momentum without crowding -- is the one the layer
        # should read as fuel.
        nets = [20_000.0, 120_000.0] * 26 + [30_000.0, 45_000.0, 55_000.0, 65_000.0, 75_000.0]
        read = pos_layer.score(GOLD, make_cot(nets=nets))
        assert 0.2 < read.detail["percentile"] < 0.8
        assert read.components["crowding_room"] == pytest.approx(1.0)
        assert read.components["momentum"] > 0
        assert read.score > 0
        assert "adding net length" in read.reasoning
        assert "not yet crowded" in read.reasoning

    def test_crowding_damps_momentum_and_turns_contrarian_at_the_extreme(self):
        # A history whose latest reading is the highest in three years, still rising.
        crowded = [float(n) for n in range(10_000, 10_000 + 60 * 2000, 2000)]
        read = pos_layer.score(GOLD, make_cot(nets=crowded))
        assert read.detail["percentile"] == pytest.approx(1.0)
        assert read.components["crowding_room"] == pytest.approx(0.0)
        assert read.components["contrarian"] < 0
        assert read.score < 0
        assert "crowded long" in read.reasoning

    def test_crowded_short_is_a_contrarian_long(self):
        crowded_short = [float(n) for n in range(100_000, 100_000 - 60 * 2000, -2000)]
        read = pos_layer.score(GOLD, make_cot(nets=crowded_short))
        assert read.components["contrarian"] > 0
        assert "crowded short" in read.reasoning

    def test_silver_extremes_are_treated_as_less_conclusive(self):
        crowded = [float(n) for n in range(10_000, 10_000 + 60 * 2000, 2000)]
        gold = pos_layer.score(GOLD, make_cot(asset=GOLD, nets=crowded))
        silver = pos_layer.score(SILVER, make_cot(asset=SILVER, nets=crowded))
        assert abs(silver.components["contrarian"]) < abs(gold.components["contrarian"])

    def test_report_age_decays_freshness(self):
        fresh = pos_layer.score(GOLD, make_cot(nets=[50_000.0] * 60, age_days=3))
        stale = pos_layer.score(GOLD, make_cot(nets=[50_000.0] * 60, age_days=25))
        assert fresh.freshness > stale.freshness
        assert stale.freshness < 0.6

    def test_ordinal_rendering(self):
        assert pos_layer._ordinal(1) == "1st"
        assert pos_layer._ordinal(2) == "2nd"
        assert pos_layer._ordinal(3) == "3rd"
        assert pos_layer._ordinal(4) == "4th"
        assert pos_layer._ordinal(11) == "11th"
        assert pos_layer._ordinal(21) == "21st"


class TestLexicon:
    def setup_method(self):
        self.scorer = LexiconScorer()

    def test_hawkish_is_bearish_and_dovish_is_bullish(self):
        hawkish = self.scorer.score_one(make_headline("Hawkish Fed minutes lift the dollar"))
        dovish = self.scorer.score_one(make_headline("Dovish Fed signals a rate cut"))
        assert hawkish.direction < 0
        assert dovish.direction > 0

    def test_good_economic_news_is_bad_for_gold(self):
        # The case a generic sentiment model gets backwards.
        read = self.scorer.score_one(
            make_headline("Stronger payrolls push real yields higher; bullion slides")
        )
        assert read.direction < 0

    def test_a_metal_falling_is_scored_negative_even_without_keywords(self):
        read = self.scorer.score_one(make_headline("Gold slips for a third session"))
        assert read.direction < 0

    def test_relevance_is_capped_so_the_lexicon_cannot_pose_as_the_llm_pass(self):
        read = self.scorer.score_one(make_headline("Gold rallies on dovish Fed"))
        assert read.relevance <= self.scorer.max_relevance

    def test_irrelevant_headlines_get_near_zero_relevance(self):
        read = self.scorer.score_one(make_headline("Local team wins the cup final"))
        assert read.relevance == pytest.approx(0.0)


class _FixedScorer:
    """A scorer that returns the same read for every headline, for aggregation tests."""

    name = "fixed"

    def __init__(self, direction: float, relevance: float = 1.0) -> None:
        self.direction = direction
        self.relevance = relevance

    def score_batch(self, headlines):
        return {
            h.id: NewsScore(h.id, self.direction, self.relevance, "llm", "fixed")
            for h in headlines
        }


class TestSentiment:
    def test_no_headlines_is_unavailable_not_neutral(self):
        read = sent_layer.score(GOLD, [], _FixedScorer(0.8))
        assert read.available is False

    def test_stale_headlines_are_excluded_entirely(self):
        old = [make_headline("Dovish Fed", hours_ago=200)]
        assert sent_layer.score(GOLD, old, _FixedScorer(0.8)).available is False

    def test_thin_coverage_is_shrunk_toward_zero(self):
        # The most important line in the sentiment layer: unshrunk, its loudest readings
        # come from its thinnest data.
        scorer = _FixedScorer(1.0)
        two = [make_headline(f"Dovish Fed {i}", hours_ago=1) for i in range(2)]
        twenty = [make_headline(f"Dovish Fed {i}", hours_ago=1) for i in range(20)]
        thin = sent_layer.score(GOLD, two, scorer)
        thick = sent_layer.score(GOLD, twenty, scorer)
        assert thin.score < thick.score
        assert thin.components["shrinkage"] < thick.components["shrinkage"]
        assert thick.score < 1.0  # never fully unshrunk

    def test_recent_headlines_dominate_older_ones(self):
        scorer = _FixedScorer(1.0)
        fresh = sent_layer.score(GOLD, [make_headline("a", hours_ago=0.5)], scorer)
        old = sent_layer.score(GOLD, [make_headline("a", hours_ago=48)], scorer)
        assert fresh.score > old.score

    def test_other_metal_headlines_count_at_half_weight(self):
        scorer = _FixedScorer(1.0)
        own = sent_layer.score(GOLD, [make_headline("g", assets=(GOLD,))], scorer)
        other = sent_layer.score(GOLD, [make_headline("s", assets=(SILVER,))], scorer)
        macro_wide = sent_layer.score(GOLD, [make_headline("fed", assets=())], scorer)
        assert other.score < own.score
        assert macro_wide.score == pytest.approx(own.score)

    def test_provider_sentiment_is_blended_not_ignored(self):
        scorer = _FixedScorer(1.0)
        agreeing = sent_layer.score(
            GOLD, [make_headline("a", provider_sentiment=1.0)], scorer
        )
        disagreeing = sent_layer.score(
            GOLD, [make_headline("a", provider_sentiment=-1.0)], scorer
        )
        assert disagreeing.score < agreeing.score

    def test_dispersion_is_reported(self):
        class Alternating:
            name = "alt"

            def score_batch(self, headlines):
                return {
                    h.id: NewsScore(h.id, 1.0 if i % 2 else -1.0, 1.0, "llm", "")
                    for i, h in enumerate(headlines)
                }

        headlines = [make_headline(f"h{i}", hours_ago=1) for i in range(10)]
        read = sent_layer.score(GOLD, headlines, Alternating())
        assert read.components["dispersion"] > 0.5
        assert "divided" in read.reasoning or "split" in read.reasoning

    def test_detail_carries_the_loudest_headlines_for_the_ui(self):
        headlines = [make_headline(f"h{i}", hours_ago=1) for i in range(8)]
        read = sent_layer.score(GOLD, headlines, _FixedScorer(0.7))
        assert len(read.detail["top_headlines"]) <= 6
        assert read.detail["headline_count"] == 8
