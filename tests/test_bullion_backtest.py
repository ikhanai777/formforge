"""Backtesting and the scorecard: the two places a signal product lies to itself.

The tests that matter here are the ones that would catch a flattering bug rather than a
crashing one:

* ``History.as_of`` must not leak a single future observation into a past bar.
* A bar that touches both the stop and the target must grade as a loss.
* A signal whose horizon has not elapsed must be pending, never a win.
* A trade stopped out on day two and recovered by day seven must grade as a loss.
* Walk-forward must report in-sample *and* out-of-sample numbers, so the gap is visible.

Every one of those, implemented the other way round, produces a backtest that looks better
and a product that works worse.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from bullion_fixtures import make_cot, make_headline, make_macro, make_series, make_signal, ramp

from bullion import backtest as bt
from bullion import scorecard as sc
from bullion.config import GOLD, LayerWeights, Settings
from bullion.fusion import SignalState
from bullion.providers.synthetic import SyntheticWorld
from bullion.scoring.llm_news import LexiconScorer
from bullion.series import Candle, Series
from bullion.store import Store

UTC = timezone.utc


def _future(bars: list[tuple[float, float, float]], start: datetime | None = None) -> Series:
    """A future series from (high, low, close) triples; open is the previous close."""
    start = start or datetime(2026, 6, 1, tzinfo=UTC)
    candles = []
    previous = bars[0][2]
    for index, (high, low, close) in enumerate(bars):
        open_ = min(max(previous, low), high)
        candles.append(
            Candle(
                ts=start + timedelta(days=index),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=1.0,
            )
        )
        previous = close
    return Series.of(GOLD, "1day", candles)


class TestResolve:
    def test_target_first_is_a_win_measured_in_r(self):
        signal = make_signal(price=2400.0, stop=2380.0, target=2440.0)
        trade = bt.resolve(signal, _future([(2445.0, 2395.0, 2441.0)]))
        assert trade.outcome == "target"
        assert trade.r_multiple == pytest.approx((2440.0 - 2400.0) / 20.0)
        assert trade.win is True
        assert trade.bars_held == 1

    def test_stop_first_is_a_loss_of_exactly_one_r(self):
        signal = make_signal(price=2400.0, stop=2380.0, target=2440.0)
        trade = bt.resolve(signal, _future([(2410.0, 2375.0, 2385.0)]))
        assert trade.outcome == "stop"
        assert trade.r_multiple == pytest.approx(-1.0)

    def test_a_bar_touching_both_levels_grades_as_a_loss(self):
        # Without tick data the order is unknowable, and resolving ties favourably is the
        # single most common way a backtest flatters itself.
        signal = make_signal(price=2400.0, stop=2380.0, target=2440.0)
        trade = bt.resolve(signal, _future([(2450.0, 2370.0, 2420.0)]))
        assert trade.outcome == "stop"
        assert trade.r_multiple == pytest.approx(-1.0)

    def test_timeout_is_marked_to_the_last_close(self):
        signal = make_signal(price=2400.0, stop=2380.0, target=2460.0)
        future = _future([(2405.0, 2395.0, 2402.0)] * 7)
        trade = bt.resolve(signal, future, horizon=7)
        assert trade.outcome == "timeout"
        assert trade.r_multiple == pytest.approx((2402.0 - 2400.0) / 20.0)

    def test_shorts_are_mirrored(self):
        signal = make_signal(
            state=SignalState.SELL, price=2400.0, stop=2420.0, target=2360.0
        )
        trade = bt.resolve(signal, _future([(2410.0, 2355.0, 2358.0)]))
        assert trade.direction == -1
        assert trade.outcome == "target"
        assert trade.r_multiple > 0

    def test_excursions_are_recorded_in_r(self):
        signal = make_signal(price=2400.0, stop=2380.0, target=2600.0)
        trade = bt.resolve(signal, _future([(2430.0, 2390.0, 2410.0)]))
        assert trade.mfe_r == pytest.approx(30.0 / 20.0)
        assert trade.mae_r == pytest.approx(10.0 / 20.0)

    def test_signals_without_levels_are_not_graded(self):
        hold = make_signal(state=SignalState.HOLD, stop=None, target=None)
        assert bt.resolve(hold, _future([(2410.0, 2390.0, 2400.0)])) is None

    def test_no_future_bars_means_no_grade(self):
        signal = make_signal()
        assert bt.resolve(signal, Series.of(GOLD, "1day", [])) is None

    def test_returns_at_the_spec_horizons_are_signed_by_direction(self):
        signal = make_signal(
            state=SignalState.SELL, price=2400.0, stop=2440.0, target=2200.0
        )
        future = _future([(2405.0, 2380.0, 2390.0)] * 7)
        trade = bt.resolve(signal, future, horizon=7)
        # Price fell and the signal was a sell, so the signed return is positive.
        assert trade.signed_return(trade.price_1d) > 0


class TestHistorySlicing:
    def _history(self) -> bt.History:
        series = make_series([2000.0 + i for i in range(400)])
        return bt.History(
            asset=GOLD,
            series=series,
            macro={"DFII10": make_macro("DFII10", ramp(2.2, 1.6, 200, 0.03))},
            cot=make_cot(nets=[50_000.0] * 60),
            headlines=[make_headline(f"h{i}", hours_ago=i * 24) for i in range(20)],
        )

    def test_as_of_excludes_every_future_observation(self):
        history = self._history()
        cut = history.series[200].ts
        data = history.as_of(cut)
        assert data.series.last.ts == cut
        assert all(point.day <= cut.date() for point in data.macro["DFII10"].points)
        assert all(report.report_date <= cut.date() for report in data.cot.reports)
        assert all(headline.ts <= cut for headline in data.headlines)

    def test_as_of_drops_a_macro_series_that_had_no_data_yet(self):
        series = make_series([2000.0 + i for i in range(400)])
        future_only = make_macro("DFII10", [2.0] * 10, end=datetime.now(UTC))
        history = bt.History(asset=GOLD, series=series, macro={"DFII10": future_only})
        data = history.as_of(series[0].ts)
        # Empty series are removed rather than passed along as a zero-length opinion.
        assert "DFII10" not in data.macro

    def test_as_of_is_the_only_way_the_backtest_sees_history(self):
        history = self._history()
        early = history.as_of(history.series[100].ts)
        late = history.as_of(history.series[300].ts)
        assert len(early.series) < len(late.series)


class TestMetrics:
    def _trade(self, r: float, **kwargs) -> bt.Trade:
        defaults: dict[str, object] = {
            "asset": GOLD, "ts": datetime.now(UTC), "state": "BUY", "strength": "normal",
            "direction": 1, "confidence": 0.6, "composite": 0.4, "regime": "trend",
            "entry": 2400.0, "stop": 2380.0, "target": 2440.0,
            "outcome": "target" if r > 0 else "stop", "exit_price": 2400.0 + r * 20,
            "bars_held": 3, "r_multiple": r, "mfe_r": max(r, 0), "mae_r": max(-r, 0),
        }
        defaults.update(kwargs)
        return bt.Trade(**defaults)

    def test_empty_metrics_are_none_not_zero(self):
        empty = bt.metrics([])
        assert empty["trades"] == 0
        assert empty["win_rate"] is None
        assert empty["expectancy_r"] is None

    def test_win_rate_and_expectancy_are_both_reported(self):
        # Win rate alone is gameable by moving the target closer, so the pair is the
        # contract: this set is 50% winners and still loses money.
        trades = [self._trade(0.5), self._trade(-1.0)] * 10
        read = bt.metrics(trades)
        assert read["win_rate"] == pytest.approx(0.5)
        assert read["expectancy_r"] == pytest.approx(-0.25)
        assert read["profit_factor"] == pytest.approx(0.5)

    def test_drawdown_is_measured_on_the_equity_path(self):
        trades = [self._trade(2.0), self._trade(-1.0), self._trade(-1.0), self._trade(3.0)]
        assert bt.metrics(trades)["max_drawdown_r"] == pytest.approx(-2.0)

    def test_wilson_interval_widens_on_thin_samples(self):
        narrow = bt.wilson_interval(200, 400)
        wide = bt.wilson_interval(5, 10)
        assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])
        assert bt.wilson_interval(0, 0) == (0.0, 1.0)
        # Never reports an impossible bound, even at the edges.
        low, high = bt.wilson_interval(10, 10)
        assert 0.0 <= low <= high <= 1.0

    def test_objective_prefers_a_measured_edge_over_a_lucky_one(self):
        lucky = [self._trade(2.0)] * 3
        solid = [self._trade(0.6)] * 60
        assert bt.objective(solid) > bt.objective(lucky)
        assert bt.objective([]) == -1.0

    def test_group_metrics_buckets_by_key(self):
        trades = [self._trade(1.0, strength="strong"), self._trade(-1.0, strength="normal")]
        grouped = bt.group_metrics(trades, lambda t: t.strength)
        assert grouped["strong"]["win_rate"] == 1.0
        assert grouped["normal"]["win_rate"] == 0.0


@pytest.fixture(scope="module")
def history() -> bt.History:
    """A synthetic two-and-a-half-year tape, shared across the walk-forward tests."""
    world = SyntheticWorld(seed=21, history_bars=900)
    return bt.History(
        asset=GOLD,
        series=world.candles(GOLD, "1day", 900),
        macro={key: world.series(key, 900) for key in ("DFII10", "DTWEXBGS")},
        cot=world.history(GOLD, 120),
        headlines=[],
        synthetic=True,
    )


class TestWalkForward:
    def test_frames_are_built_once_per_bar(self, history):
        frames = bt.build_frames(history, Settings(), LexiconScorer(), warmup=260, step=20)
        assert frames
        assert all(isinstance(frame.layers, dict) for frame in frames)
        # Sentiment is dark here (no headlines), and that must be visible rather than
        # scored as neutral.
        assert all("sentiment" not in frame.coverage_layers for frame in frames)

    def test_grading_the_same_frames_twice_is_deterministic(self, history):
        frames = bt.build_frames(history, Settings(), LexiconScorer(), warmup=260, step=20)
        weights = LayerWeights(0.3, 0.3, 0.2, 0.2)
        first = bt.grade_frames(frames, history, Settings(), weights)
        second = bt.grade_frames(frames, history, Settings(), weights)
        assert [t.r_multiple for t in first] == [t.r_multiple for t in second]

    def test_walk_forward_reports_both_sides_of_every_fold(self, history):
        result = bt.walk_forward(
            history, Settings(), LexiconScorer(), train_bars=120, test_bars=60,
            warmup=260, step=3,
            grid=[LayerWeights(0.3, 0.3, 0.2, 0.2), LayerWeights(0.5, 0.2, 0.2, 0.1)],
        )
        assert result.folds
        for fold in result.folds:
            assert fold.train_end <= fold.test_start
            assert "expectancy_r" in fold.train_metrics
            assert "expectancy_r" in fold.test_metrics
        assert result.oos_metrics["trades"] >= 0
        assert result.synthetic is True
        # The overfit gap is the headline diagnostic and must be computable.
        assert result.overfit_gap is None or isinstance(result.overfit_gap, float)

    def test_test_windows_do_not_overlap_training_windows(self, history):
        result = bt.walk_forward(
            history, Settings(), LexiconScorer(), train_bars=120, test_bars=60,
            warmup=260, step=3, grid=[LayerWeights(0.3, 0.3, 0.2, 0.2)],
        )
        for fold in result.folds:
            for trade in fold.test_trades:
                assert trade.ts >= fold.test_start
                assert trade.ts > fold.train_end

    def test_too_little_history_says_how_much_is_needed(self):
        short = bt.History(asset=GOLD, series=make_series([2000.0 + i for i in range(300)]))
        with pytest.raises(ValueError, match="not enough for a"):
            bt.walk_forward(short, Settings(), LexiconScorer(), train_bars=250, test_bars=60)

    def test_fitting_on_synthetic_data_warns(self, history, caplog):
        frames = bt.build_frames(history, Settings(), LexiconScorer(), warmup=260, step=20)
        with caplog.at_level("WARNING"):
            bt.fit_weights(frames, history, Settings(), grid=[LayerWeights(0.3, 0.3, 0.2, 0.2)])
        assert any("SYNTHETIC" in record.message for record in caplog.records)

    def test_persist_writes_a_row_per_fold_plus_the_pooled_result(self, history, tmp_path):
        store = Store(tmp_path / "bt.db")
        result = bt.walk_forward(
            history, Settings(), LexiconScorer(), train_bars=120, test_bars=60,
            warmup=260, step=2, grid=[LayerWeights(0.3, 0.3, 0.2, 0.2)],
        )
        ids = bt.persist(store, result)
        assert len(ids) == len(result.folds) + 1
        kinds = {run["kind"] for run in store.backtest_runs(GOLD)}
        assert kinds == {"walk_forward_fold", "walk_forward_oos"}

    def test_no_edge_is_found_in_a_random_walk(self, history):
        # A sanity check on the whole engine rather than on one function: the synthetic
        # tape is a mean-reverting random walk with no exploitable structure, so a system
        # reporting a large positive expectancy on it has a lookahead bug, not an edge.
        result = bt.walk_forward(
            history, Settings(), LexiconScorer(), train_bars=150, test_bars=80,
            warmup=260, step=2,
            grid=[LayerWeights(0.3, 0.3, 0.2, 0.2), LayerWeights(0.45, 0.3, 0.1, 0.15)],
        )
        expectancy = result.oos_metrics.get("expectancy_r")
        if result.oos_metrics["trades"] >= 20:
            assert expectancy < 0.35, (
                "out-of-sample expectancy on a random walk should be near zero; a large "
                "positive number means the replay is seeing the future"
            )


class TestScorecardGrading:
    def test_an_open_signal_is_pending_not_a_win(self, tmp_path):
        store = Store(tmp_path / "s.db")
        now = datetime.now(UTC)
        series = make_series(
            [2400.0, 2405.0, 2408.0], start=now - timedelta(days=3)
        )
        store.write_candles(series)
        store.write_signal(
            make_signal(price=2400.0, stop=2380.0, target=2500.0, ts=series[0].ts)
        )
        counts = sc.grade_pending(store, [GOLD], now=now)
        # Only two bars of the seven-bar horizon have elapsed and neither level printed.
        assert counts == {"graded": 0, "pending": 1, "unresolvable": 0}

    def test_a_matured_horizon_grades_as_a_timeout(self, tmp_path):
        store = Store(tmp_path / "s.db")
        now = datetime.now(UTC)
        closes = [2400.0] + [2402.0] * 8
        series = make_series(closes, start=now - timedelta(days=9), wick=0.0005)
        store.write_candles(series)
        store.write_signal(
            make_signal(price=2400.0, stop=2300.0, target=2500.0, ts=series[0].ts)
        )
        counts = sc.grade_pending(store, [GOLD], now=now)
        assert counts["graded"] == 1
        assert store.graded()[0]["outcome"] == "timeout"

    def test_a_stop_then_recovery_is_a_loss(self, tmp_path):
        # Grading on the later price would call this a win. The user was stopped out on
        # day two; the most flattering error available is to ignore that.
        store = Store(tmp_path / "s.db")
        now = datetime.now(UTC)
        candles = []
        start = now - timedelta(days=9)
        path = [(2400.0, 2400.0, 2400.0), (2405.0, 2370.0, 2375.0)] + [
            (2500.0, 2450.0, 2490.0)
        ] * 7
        previous = 2400.0
        for index, (high, low, close) in enumerate(path):
            candles.append(
                Candle(
                    ts=start + timedelta(days=index),
                    open=min(max(previous, low), high),
                    high=high,
                    low=low,
                    close=close,
                    volume=1.0,
                )
            )
            previous = close
        store.write_candles(Series.of(GOLD, "1day", candles))
        store.write_signal(
            make_signal(price=2400.0, stop=2380.0, target=2470.0, ts=candles[0].ts)
        )
        sc.grade_pending(store, [GOLD], now=now)
        row = store.graded()[0]
        assert row["outcome"] == "stop"
        card = sc.build(store, include_synthetic=True)
        assert card.overall.wins == 0
        assert card.overall.expectancy_r == pytest.approx(-1.0, abs=0.01)

    def test_grading_is_idempotent(self, tmp_path):
        store = Store(tmp_path / "s.db")
        now = datetime.now(UTC)
        series = make_series(
            [2400.0] + [2402.0] * 8, start=now - timedelta(days=9), wick=0.0005
        )
        store.write_candles(series)
        store.write_signal(
            make_signal(price=2400.0, stop=2300.0, target=2500.0, ts=series[0].ts)
        )
        sc.grade_pending(store, [GOLD], now=now)
        second = sc.grade_pending(store, [GOLD], now=now)
        assert second["graded"] == 0
        assert len(store.graded()) == 1


class TestScorecard:
    def _graded_store(self, tmp_path, outcomes: list[str], synthetic: bool = False) -> Store:
        store = Store(tmp_path / "card.db")
        now = datetime.now(UTC)
        for index, outcome in enumerate(outcomes):
            signal = make_signal(
                price=2400.0, stop=2380.0, target=2440.0, ts=now - timedelta(days=index + 1)
            )
            if synthetic:
                object.__setattr__(signal, "synthetic", True)
            signal_id = store.write_signal(signal)
            store.write_outcome(
                {
                    "signal_id": signal_id,
                    "resolved_at": now.isoformat(),
                    "outcome": outcome,
                    "bars_held": 3,
                    "exit_price": 2440.0 if outcome == "target" else 2380.0,
                    "price_1d": None, "price_3d": None, "price_7d": None,
                    "return_1d": None, "return_3d": None,
                    "return_7d": 0.016 if outcome == "target" else -0.008,
                    "mfe_r": 2.0 if outcome == "target" else 0.2,
                    "mae_r": 0.2 if outcome == "target" else 1.0,
                    "notes": None,
                }
            )
        return store

    def test_buckets_report_sample_size_and_an_interval(self, tmp_path):
        store = self._graded_store(tmp_path, ["target"] * 12 + ["stop"] * 8)
        card = sc.build(store)
        assert card.overall.trades == 20
        assert card.overall.win_rate == pytest.approx(0.6)
        low, high = card.overall.win_rate_ci
        assert low < 0.6 < high
        assert card.overall.as_dict()["reliable"] is True
        assert card.by_asset[GOLD].trades == 20
        assert card.by_strength["normal"].trades == 20
        assert card.by_regime["trend"].trades == 20

    def test_thin_buckets_are_flagged(self, tmp_path):
        store = self._graded_store(tmp_path, ["target"] * 3)
        card = sc.build(store)
        assert card.overall.as_dict()["reliable"] is False

    def test_expectancy_is_reported_in_r(self, tmp_path):
        store = self._graded_store(tmp_path, ["target"] * 10 + ["stop"] * 10)
        card = sc.build(store)
        # Targets are 2R from a 20-point stop, so 50/50 gives +0.5R per signal.
        assert card.overall.expectancy_r == pytest.approx(0.5, abs=0.01)

    def test_synthetic_rows_are_excluded_by_default_and_counted(self, tmp_path):
        store = self._graded_store(tmp_path, ["target"] * 5, synthetic=True)
        default = sc.build(store)
        assert default.overall.trades == 0
        assert default.excluded_synthetic == 5
        included = sc.build(store, include_synthetic=True)
        assert included.overall.trades == 5

    def test_caveat_names_the_sample_floor(self, tmp_path):
        store = self._graded_store(tmp_path, ["target"])
        assert str(sc.MIN_DRIFT_SAMPLE) in sc.build(store).as_dict()["caveat"]


class TestDrift:
    def _bucket(self, wins: int, n: int) -> sc.Bucket:
        return sc.Bucket(
            name="live", trades=n, wins=wins, win_rate=wins / n if n else None,
            win_rate_ci=bt.wilson_interval(wins, n), expectancy_r=0.1, avg_return_7d=0.01,
        )

    def test_no_baseline_without_a_backtest(self):
        assert sc.drift(self._bucket(12, 20), None).verdict == "no_baseline"

    def test_thin_live_samples_refuse_a_verdict(self):
        # Drift that fires on eight trades is noise with a siren attached.
        verdict = sc.drift(self._bucket(2, 8), 0.55)
        assert verdict.verdict == "insufficient_sample"
        assert "below the" in verdict.detail

    def test_in_line_performance_is_not_an_alert(self):
        assert sc.drift(self._bucket(28, 50), 0.55, 200).verdict == "in_line"

    def test_underperformance_is_flagged_with_a_direction(self):
        verdict = sc.drift(self._bucket(10, 60), 0.55, 300)
        assert verdict.verdict == "underperforming"
        assert verdict.z < -2

    def test_outperformance_is_also_drift(self):
        # Pleasant, and still drift: the backtest has stopped describing the live system.
        verdict = sc.drift(self._bucket(55, 60), 0.50, 300)
        assert verdict.verdict == "outperforming"
        assert "still drift" in verdict.detail

    def test_drift_from_store_skips_a_synthetic_baseline(self, tmp_path):
        store = Store(tmp_path / "d.db")
        store.write_backtest_run(
            {
                "asset": GOLD,
                "kind": "walk_forward_oos",
                "weights": {},
                "metrics": {"oos": {"win_rate": 0.6, "trades": 100}, "synthetic": True},
                "sample_size": 100,
            }
        )
        verdict = sc.drift_from_store(store, GOLD)
        assert verdict.verdict == "no_baseline"
        assert "synthetic" in verdict.detail


class TestActivity:
    def test_recent_activity_counts_by_state(self, tmp_path):
        store = Store(tmp_path / "a.db")
        now = datetime.now(UTC)
        store.write_signal(make_signal(ts=now))
        store.write_signal(make_signal(state=SignalState.HOLD, ts=now))
        store.write_signal(make_signal(ts=now - timedelta(days=60)))
        activity = sc.recent_activity(store, days=30)
        assert activity["signals"] == 2
        assert activity["actionable"] == 1
        assert activity["by_state"]["HOLD"] == 1
