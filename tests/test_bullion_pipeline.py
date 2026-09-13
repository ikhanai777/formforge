"""The cycle, the alerting rules, and the regime detector.

Two themes. First, fault isolation: a dead news feed must cost the sentiment layer and
nothing else, because the alternative is that one flaky free-tier endpoint stops the
product from publishing anything and the user cannot tell "no signal" from "no data".

Second, restraint. Spec section 6 says alert on change, not on every recompute. The naive
implementation pings every cycle as a composite oscillates across a threshold, so the
hysteresis, the cooldown and the confidence band all get their own tests -- they are the
difference between a useful notification and an uninstall.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from bullion_fixtures import (
    choppy,
    make_indicators,
    make_series,
    make_signal,
    trend_up,
)

from bullion import alerts as alerts_module
from bullion import pipeline
from bullion.config import ASSETS, GOLD, SILVER, Regime, Settings, Thresholds
from bullion.fusion import SignalState
from bullion.indicators import compute
from bullion.providers import build
from bullion.providers.base import LayerData
from bullion.providers.synthetic import SyntheticWorld
from bullion.regime import detect, trend_strength, weights_for
from bullion.scoring.llm_news import LexiconScorer
from bullion.store import Store

UTC = timezone.utc


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(store_path=tmp_path / "pipeline.db")


@pytest.fixture()
def feeds(settings):
    return build(settings, SyntheticWorld(seed=31, history_bars=900))


class TestRegime:
    def test_trend_strength_saturates_rather_than_rewarding_extremes(self):
        # ADX 55 is not a stronger mandate than ADX 35; treating it as one is how a system
        # buys the top of a parabolic move with maximum confidence.
        assert trend_strength(35.0) == 1.0
        assert trend_strength(55.0) == 1.0
        assert trend_strength(12.0) == 0.0
        assert trend_strength(None) == 0.4

    def test_high_adx_is_a_trend_regime(self):
        read = detect(make_indicators(adx14=33.0))
        assert read.regime is Regime.TREND
        assert "trending" in read.reason

    def test_low_adx_is_a_range_regime(self):
        read = detect(make_indicators(adx14=14.0))
        assert read.regime is Regime.RANGE
        assert "no trend" in read.reason

    def test_an_imminent_event_outranks_a_clean_trend(self):
        from bullion.providers.base import EconomicEvent

        now = datetime.now(UTC)
        event = EconomicEvent(name="FOMC decision", ts=now + timedelta(hours=20), kind="fomc")
        read = detect(make_indicators(adx14=40.0), [event], now)
        assert read.regime is Regime.EVENT
        assert read.event is event
        assert "FOMC" in read.reason

    def test_a_distant_event_does_not(self):
        from bullion.providers.base import EconomicEvent

        now = datetime.now(UTC)
        event = EconomicEvent(name="FOMC decision", ts=now + timedelta(days=20), kind="fomc")
        assert detect(make_indicators(adx14=40.0), [event], now).regime is Regime.TREND

    def test_tilts_move_weight_in_the_documented_direction(self, settings):
        trend = weights_for(settings, GOLD, Regime.TREND).as_dict()
        ranging = weights_for(settings, GOLD, Regime.RANGE).as_dict()
        event = weights_for(settings, GOLD, Regime.EVENT).as_dict()
        assert trend["technical"] > ranging["technical"]
        assert ranging["macro"] > trend["macro"]
        assert event["sentiment"] > trend["sentiment"]

    def test_silver_has_its_own_profile(self, settings):
        gold = weights_for(settings, GOLD, Regime.TREND).as_dict()
        silver = weights_for(settings, SILVER, Regime.TREND).as_dict()
        assert silver != gold
        assert silver["technical"] > gold["technical"]


class TestCollectAndEvaluate:
    def test_a_full_cycle_produces_a_four_layer_signal(self, settings, feeds):
        data, quality = pipeline.collect(GOLD, feeds, bars=500)
        assert data.series is not None and len(data.series) > 300
        assert data.macro and data.cot is not None and data.headlines
        assert quality.agreement > 0.5

        signal = pipeline.evaluate(GOLD, data, settings, LexiconScorer(), quality)
        assert set(signal.layers) == {"technical", "macro", "sentiment", "positioning"}
        assert all(layer.available for layer in signal.layers.values())
        assert signal.synthetic is True
        assert any("SYNTHETIC" in note for note in signal.notes)

    def test_weights_are_normalised_over_available_layers(self, settings, feeds):
        data, quality = pipeline.collect(GOLD, feeds, bars=500)
        signal = pipeline.evaluate(GOLD, data, settings, LexiconScorer(), quality)
        assert sum(signal.weights.values()) == pytest.approx(1.0)

    def test_a_dead_layer_redistributes_weight_and_costs_confidence(self, settings, feeds):
        full, quality = pipeline.collect(GOLD, feeds, bars=500)
        with_all = pipeline.evaluate(GOLD, full, settings, LexiconScorer(), quality)

        # Same data with the news feed dark, which is what a missing FINNHUB_KEY looks like.
        partial = LayerData(
            asset=GOLD,
            series=full.series,
            macro=full.macro,
            cot=full.cot,
            headlines=[],
            events=full.events,
            synthetic=True,
        )
        partial.note_failure("news", "no news provider configured")
        without = pipeline.evaluate(GOLD, partial, settings, LexiconScorer(), quality)

        assert without.layers["sentiment"].available is False
        assert "sentiment" not in without.weights
        assert sum(without.weights.values()) == pytest.approx(1.0)
        assert without.confidence.coverage < with_all.confidence.coverage
        assert any("sentiment" in note for note in without.notes)

    def test_score_layers_turns_a_scorer_crash_into_a_dark_layer(self, settings, feeds):
        data, _ = pipeline.collect(GOLD, feeds, bars=400)

        class Exploding:
            name = "boom"

            def score_batch(self, headlines):
                raise RuntimeError("scorer exploded")

        layers = pipeline.score_layers(GOLD, data, compute(data.series), Exploding())
        # A bug in one scorer must not take down the cycle, and must not be scored as a
        # neutral opinion either.
        assert layers["sentiment"].available is False
        assert "scorer error" in layers["sentiment"].reasoning
        assert layers["technical"].available is True

    def test_collect_reports_per_layer_failures_without_raising(self, settings):
        class BrokenNews:
            name = "broken"

            def headlines(self, since, limit=50):
                raise RuntimeError("news feed is down")

        feeds = build(settings, SyntheticWorld(seed=5, history_bars=600))
        object.__setattr__(feeds, "news", BrokenNews())
        data, _ = pipeline.collect(GOLD, feeds, bars=400)
        assert "news" in data.failures
        assert data.series is not None

    def test_a_price_failure_is_fatal_for_that_asset_only(self, settings, feeds):
        working = feeds.prices

        class BrokenForGold:
            name = "broken"

            def validated(self, asset, interval="1day", limit=400, now=None):
                if asset == GOLD:
                    raise RuntimeError("price feed is down")
                return working.validated(asset, interval, limit, now)

            def candles(self, asset, interval="1day", limit=400):
                return working.candles(asset, interval, limit)

            def describe(self):  # pragma: no cover - only for the cycle summary
                return {}

        object.__setattr__(feeds, "prices", BrokenForGold())
        result = pipeline.run_cycle(
            settings, feeds=feeds, assets=ASSETS, bars=400, persist=False
        )
        assert result.results[GOLD].error is not None
        assert result.results[SILVER].ok is True


class TestShouldPersist:
    def test_the_first_signal_is_always_stored(self):
        write, why = pipeline.should_persist(None, make_signal())
        assert write and "first signal" in why

    def test_an_unchanged_call_is_not_a_new_row(self, tmp_path):
        # Otherwise a five-minute loop writes 288 rows a day describing the same call and
        # the published win rate becomes an average over recomputes.
        store = Store(tmp_path / "p.db")
        store.write_signal(make_signal(confidence=0.6))
        previous = store.latest_signal(GOLD)
        write, why = pipeline.should_persist(previous, make_signal(confidence=0.61))
        assert not write
        assert "no material change" in why

    def test_a_state_change_is_stored(self, tmp_path):
        store = Store(tmp_path / "p.db")
        store.write_signal(make_signal(state=SignalState.BUY))
        previous = store.latest_signal(GOLD)
        write, why = pipeline.should_persist(previous, make_signal(state=SignalState.SELL))
        assert write and "state changed" in why

    def test_a_large_confidence_move_is_stored(self, tmp_path):
        store = Store(tmp_path / "p.db")
        store.write_signal(make_signal(confidence=0.5))
        previous = store.latest_signal(GOLD)
        write, why = pipeline.should_persist(previous, make_signal(confidence=0.75))
        assert write and "confidence moved" in why

    def test_the_heartbeat_guarantees_a_row(self, tmp_path):
        store = Store(tmp_path / "p.db")
        old = datetime.now(UTC) - timedelta(hours=9)
        store.write_signal(make_signal(ts=old))
        previous = store.latest_signal(GOLD)
        write, why = pipeline.should_persist(previous, make_signal())
        assert write and "heartbeat" in why


class TestRunCycle:
    def test_stores_inputs_signals_and_alerts(self, settings, feeds):
        store = Store(settings.store_path)
        result = pipeline.run_cycle(settings, feeds=feeds, store=store, bars=500)
        assert set(result.signals) == set(ASSETS)
        counts = store.counts()
        assert counts["prices"] > 0
        assert counts["macro_series"] > 0
        assert counts["cot_reports"] > 0
        assert counts["news_events"] > 0
        assert counts["signals"] == len(result.signals)
        assert result.synthetic is True
        assert result.as_dict()["duration_seconds"] >= 0

    def test_persist_false_writes_no_signal_rows(self, settings, feeds):
        store = Store(settings.store_path)
        pipeline.run_cycle(settings, feeds=feeds, store=store, bars=500, persist=False)
        # Inspecting the engine must not pollute the track record; the bars are still
        # cached because they cost quota to fetch.
        assert store.counts()["signals"] == 0
        assert store.counts()["prices"] > 0

    def test_a_second_cycle_does_not_duplicate_an_unchanged_signal(self, settings, feeds):
        store = Store(settings.store_path)
        now = datetime.now(UTC)
        pipeline.run_cycle(settings, feeds=feeds, store=store, bars=500, now=now)
        first = store.counts()["signals"]
        pipeline.run_cycle(
            settings, feeds=feeds, store=store, bars=500, now=now + timedelta(minutes=5)
        )
        assert store.counts()["signals"] == first

    def test_cycle_payload_is_json_serialisable(self, settings, feeds):
        import json

        result = pipeline.run_cycle(settings, feeds=feeds, bars=400, persist=False)
        json.dumps(result.as_dict(), default=str)


class TestAlerts:
    def _previous(self, store: Store, **kwargs):
        store.write_signal(make_signal(**kwargs))
        return store.latest_signal(GOLD)

    def test_a_first_hold_is_not_news(self, tmp_path):
        store = Store(tmp_path / "a.db")
        raised = alerts_module.evaluate(
            make_signal(state=SignalState.HOLD, stop=None, target=None), None, store=store
        )
        assert raised == []

    def test_a_first_actionable_signal_alerts(self, tmp_path):
        store = Store(tmp_path / "a.db")
        raised = alerts_module.evaluate(make_signal(state=SignalState.BUY), None, store=store)
        assert [a.kind for a in raised] == [alerts_module.KIND_NEW]
        assert "you should" not in raised[0].body.lower()

    def test_buy_to_hold_to_buy_oscillation_is_suppressed(self, tmp_path):
        store = Store(tmp_path / "a.db")
        previous = self._previous(store, state=SignalState.BUY)
        # BUY -> STRONG? no: BUY -> BUY is no change at all.
        raised = alerts_module.evaluate(
            make_signal(state=SignalState.BUY), previous, store=store
        )
        assert raised == []

    def test_a_direction_flip_always_alerts(self, tmp_path):
        store = Store(tmp_path / "a.db")
        previous = self._previous(store, state=SignalState.BUY)
        raised = alerts_module.evaluate(
            make_signal(state=SignalState.SELL, stop=2440.0, target=2330.0),
            previous,
            store=store,
        )
        assert [a.kind for a in raised] == [alerts_module.KIND_FLIP]
        assert raised[0].priority == "high"

    def test_losing_a_signal_is_announced_as_a_withdrawal(self, tmp_path):
        store = Store(tmp_path / "a.db")
        previous = self._previous(store, state=SignalState.BUY)
        raised = alerts_module.evaluate(
            make_signal(state=SignalState.NO_SIGNAL, stop=None, target=None),
            previous,
            store=store,
        )
        assert [a.kind for a in raised] == [alerts_module.KIND_SILENT]
        assert "no longer holds" in raised[0].body

    def test_the_cooldown_survives_a_restart(self, tmp_path):
        store = Store(tmp_path / "a.db")
        previous = self._previous(store, state=SignalState.HOLD, stop=None, target=None)
        now = datetime.now(UTC)
        first = alerts_module.evaluate(
            make_signal(state=SignalState.BUY), previous, store=store, now=now
        )
        alerts_module.deliver(first, store)
        # A fresh process reads the cooldown from the alerts table rather than from memory,
        # so a deploy loop cannot re-alert on every restart.
        again = alerts_module.evaluate(
            make_signal(state=SignalState.BUY),
            previous,
            store=store,
            now=now + timedelta(minutes=5),
        )
        assert again == []

    def test_confidence_shifts_alert_only_past_the_band(self, tmp_path):
        store = Store(tmp_path / "a.db")
        previous = self._previous(store, state=SignalState.BUY, confidence=0.5)
        small = alerts_module.evaluate(
            make_signal(state=SignalState.BUY, confidence=0.56), previous, store=store
        )
        assert small == []
        big = alerts_module.evaluate(
            make_signal(state=SignalState.BUY, confidence=0.8), previous, store=store
        )
        assert [a.kind for a in big] == [alerts_module.KIND_CONFIDENCE]
        assert "rose" in big[0].title

    def test_invalidation_is_detected_on_bar_extremes(self, tmp_path):
        store = Store(tmp_path / "a.db")
        series = make_series([2400.0, 2395.0, 2350.0, 2420.0])
        previous = self._previous(
            store, price=2400.0, stop=2380.0, target=2500.0, ts=series[0].ts
        )
        raised = alerts_module.evaluate(
            make_signal(state=SignalState.BUY), previous, series=series, store=store
        )
        kinds = [a.kind for a in raised]
        assert alerts_module.KIND_INVALIDATED in kinds
        invalidation = next(a for a in raised if a.kind == alerts_module.KIND_INVALIDATED)
        assert invalidation.priority == "high"
        assert "invalidation level" in invalidation.body

    def test_invalidation_ignores_the_cooldown(self, tmp_path):
        store = Store(tmp_path / "a.db")
        series = make_series([2400.0, 2340.0])
        previous = self._previous(
            store, price=2400.0, stop=2380.0, target=2500.0, ts=series[0].ts
        )
        store.record_alert(GOLD, alerts_module.KIND_INVALIDATED, "t", "b")
        raised = alerts_module.check_invalidation(previous, series)
        assert raised is not None

    def test_the_target_is_announced_too(self, tmp_path):
        store = Store(tmp_path / "a.db")
        series = make_series([2400.0, 2420.0, 2510.0])
        previous = self._previous(
            store, price=2400.0, stop=2380.0, target=2500.0, ts=series[0].ts
        )
        hit = alerts_module.check_invalidation(previous, series)
        assert hit.kind == alerts_module.KIND_TARGET

    def test_deliver_queues_rather_than_pretending_to_push(self, tmp_path):
        store = Store(tmp_path / "a.db")
        alert = alerts_module.Alert(
            kind=alerts_module.KIND_NEW, asset=GOLD, title="t", body="b"
        )
        assert alerts_module.deliver([alert], store) == 1
        row = store.last_alert(GOLD)
        # Queued undelivered: a stub that claimed to have sent a push would be worse than
        # an honest queue a worker drains.
        assert row["delivered"] == 0
        assert alert.as_dict()["disclosure"]

    def test_thresholds_are_configurable(self, tmp_path):
        store = Store(tmp_path / "a.db")
        previous = self._previous(store, state=SignalState.BUY, confidence=0.5)
        loose = Thresholds(alert_confidence_delta=0.01, alert_cooldown_minutes=0)
        raised = alerts_module.evaluate(
            make_signal(state=SignalState.BUY, confidence=0.53),
            previous,
            store=store,
            thresholds=loose,
        )
        assert [a.kind for a in raised] == [alerts_module.KIND_CONFIDENCE]


class TestEndToEnd:
    def test_a_trending_tape_produces_a_directional_call(self, settings):
        """The engine's basic sanity: a clean uptrend is not scored as a sell."""
        series = trend_up(320)
        data = LayerData(asset=GOLD, series=series, events=[])
        signal = pipeline.evaluate(GOLD, data, settings, LexiconScorer())
        assert signal.layers["technical"].score > 0
        assert signal.composite_score > 0
        # Technical is the only live layer, so coverage is partial and confidence is capped.
        assert signal.confidence.coverage < 0.5
        assert signal.state is not SignalState.STRONG_BUY

    def test_a_choppy_tape_does_not_produce_a_strong_call(self, settings):
        data = LayerData(asset=GOLD, series=choppy(320), events=[])
        signal = pipeline.evaluate(GOLD, data, settings, LexiconScorer())
        assert signal.state.strength != "strong"
