"""Persistence, and the parity between the sqlite store and the Timescale target.

The parity test is the one that matters most here. `docs/bullion-schema.sql` is the
production target and `bullion/store.py` is what runs today; if they drift, the eventual
migration is a redesign discovered under pressure. Comparing the column sets catches that
on the commit that causes it.

The rest of the suite is about the track record. A dropped signal row makes a published
win rate wrong in a way nobody can detect afterwards, so writes must raise rather than
swallow -- the opposite posture from formforge's telemetry store, and worth asserting
because the next person to copy that file will copy its error handling too.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from bullion_fixtures import make_series, make_signal

from bullion.config import GOLD, SILVER
from bullion.fusion import SignalState
from bullion.store import SCHEMA, TABLES, Store

UTC = timezone.utc
SCHEMA_SQL = Path(__file__).resolve().parent.parent / "docs" / "bullion-schema.sql"


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "bullion.db")


def _postgres_columns() -> dict[str, set[str]]:
    """Parse CREATE TABLE blocks out of the Timescale schema."""
    text = SCHEMA_SQL.read_text()
    tables: dict[str, set[str]] = {}
    for match in re.finditer(r"CREATE TABLE (\w+)\s*\((.*?)\n\);", text, re.DOTALL):
        name, body = match.group(1), match.group(2)
        columns: set[str] = set()
        for raw in body.split("\n"):
            line = raw.strip()
            if not line or line.startswith("--"):
                continue
            first = line.split()[0]
            if first.upper() in {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"}:
                continue
            columns.add(first)
        tables[name] = columns
    return tables


class TestSchemaParity:
    def test_the_target_schema_declares_every_table_the_store_has(self):
        declared = _postgres_columns()
        assert set(TABLES) <= set(declared), (
            f"tables missing from docs/bullion-schema.sql: {set(TABLES) - set(declared)}"
        )

    def test_columns_match_table_for_table(self, store):
        declared = _postgres_columns()
        for table in TABLES:
            assert store.columns(table), f"{table} is missing from the sqlite store"
            assert set(store.columns(table)) == declared[table], (
                f"{table}: sqlite has {sorted(set(store.columns(table)) - declared[table])} "
                f"extra and {sorted(declared[table] - set(store.columns(table)))} missing "
                "relative to docs/bullion-schema.sql"
            )

    def test_schema_is_idempotent(self, tmp_path):
        # The constructor runs the schema on every open; a second open must be a no-op.
        path = tmp_path / "twice.db"
        Store(path)
        second = Store(path)
        assert second.counts()["signals"] == 0
        assert all(statement.strip().startswith("CREATE") for statement in SCHEMA)


class TestPrices:
    def test_candles_round_trip(self, store):
        series = make_series([100.0, 101.0, 102.0])
        assert store.write_candles(series) == 3
        loaded = store.load_candles(GOLD, "1day", 10)
        assert loaded.closes == [100.0, 101.0, 102.0]
        assert loaded[0].ts == series[0].ts

    def test_a_refetched_bar_is_a_corrected_bar(self, store):
        store.write_candles(make_series([100.0, 101.0]))
        # Same timestamps, revised closes: the provider's latest word wins rather than
        # raising a conflict, because a revision is the normal case.
        store.write_candles(make_series([100.0, 105.0]))
        assert store.load_candles(GOLD, "1day").closes == [100.0, 105.0]
        assert store.counts()["prices"] == 2

    def test_assets_and_intervals_do_not_collide(self, store):
        store.write_candles(make_series([100.0, 101.0], asset=GOLD))
        store.write_candles(make_series([20.0, 21.0], asset=SILVER))
        assert store.load_candles(GOLD, "1day").closes == [100.0, 101.0]
        assert store.load_candles(SILVER, "1day").closes == [20.0, 21.0]

    def test_load_until_is_a_point_in_time_view(self, store):
        series = make_series([100.0, 101.0, 102.0, 103.0])
        store.write_candles(series)
        early = store.load_candles(GOLD, "1day", 10, until=series[1].ts)
        assert early.closes == [100.0, 101.0]

    def test_empty_write_is_not_an_error(self, store):
        assert store.write_candles(make_series([100.0]).tail(0)) == 0


class TestSignals:
    def test_signal_round_trips_with_its_payload(self, store):
        signal = make_signal(state=SignalState.STRONG_BUY, confidence=0.78)
        row_id = store.write_signal(signal)
        stored = store.latest_signal(GOLD)
        assert stored is not None
        assert stored.id == row_id
        assert stored.state == "STRONG_BUY"
        assert stored.strength == "strong"
        assert stored.confidence == pytest.approx(0.78)
        assert stored.direction == 1
        # The payload is the response as served, so a historical call renders without
        # being re-derived from parts.
        assert stored.payload["signal"] == "STRONG_BUY"
        assert stored.payload["disclosure"]

    def test_latest_signal_is_the_newest_not_the_last_written(self, store):
        now = datetime.now(UTC)
        store.write_signal(make_signal(ts=now))
        store.write_signal(make_signal(ts=now - timedelta(hours=5), confidence=0.9))
        assert store.latest_signal(GOLD).confidence == pytest.approx(0.6)

    def test_signals_filters_by_asset_window_and_actionability(self, store):
        now = datetime.now(UTC)
        store.write_signal(make_signal(asset=GOLD, ts=now))
        store.write_signal(make_signal(asset=SILVER, ts=now))
        store.write_signal(
            make_signal(asset=GOLD, state=SignalState.HOLD, ts=now - timedelta(days=10))
        )
        assert len(store.signals(asset=GOLD)) == 2
        assert len(store.signals(asset=GOLD, since=now - timedelta(days=1))) == 1
        assert len(store.signals(actionable_only=True)) == 2

    def test_ungraded_excludes_hold_and_already_graded(self, store):
        graded = store.write_signal(make_signal())
        store.write_signal(make_signal(state=SignalState.HOLD, stop=None, target=None))
        # The HOLD is not in the queue at all: it has no levels, so there is nothing to
        # grade it against, and counting it would put an unresolvable row in the record.
        assert [row.id for row in store.ungraded_signals()] == [graded]
        store.write_outcome(
            {
                "signal_id": graded,
                "resolved_at": datetime.now(UTC).isoformat(),
                "outcome": "target",
                "bars_held": 3,
                "exit_price": 2470.0,
                "price_1d": None,
                "price_3d": None,
                "price_7d": None,
                "return_1d": None,
                "return_3d": None,
                "return_7d": None,
                "mfe_r": 1.8,
                "mae_r": 0.2,
                "notes": None,
            }
        )
        remaining = store.ungraded_signals()
        # The HOLD never had levels, so it is not in the grading queue either.
        assert [row.id for row in remaining] == []

    def test_outcomes_upsert_rather_than_duplicating(self, store):
        signal_id = store.write_signal(make_signal())
        outcome = {
            "signal_id": signal_id,
            "resolved_at": datetime.now(UTC).isoformat(),
            "outcome": "stop",
            "bars_held": 2,
            "exit_price": 2360.0,
            "price_1d": 2390.0,
            "price_3d": None,
            "price_7d": None,
            "return_1d": -0.004,
            "return_3d": None,
            "return_7d": None,
            "mfe_r": 0.3,
            "mae_r": 1.0,
            "notes": None,
        }
        store.write_outcome(outcome)
        store.write_outcome({**outcome, "outcome": "target", "exit_price": 2470.0})
        graded = store.graded()
        assert len(graded) == 1
        assert graded[0]["outcome"] == "target"

    def test_graded_joins_signal_and_outcome_fields(self, store):
        signal_id = store.write_signal(make_signal())
        store.write_outcome(
            {
                "signal_id": signal_id,
                "resolved_at": datetime.now(UTC).isoformat(),
                "outcome": "target",
                "bars_held": 4,
                "exit_price": 2470.0,
                "price_1d": 2410.0,
                "price_3d": 2440.0,
                "price_7d": 2470.0,
                "return_1d": 0.004,
                "return_3d": 0.016,
                "return_7d": 0.029,
                "mfe_r": 1.9,
                "mae_r": 0.3,
                "notes": None,
            }
        )
        row = store.graded()[0]
        assert row["state"] == "BUY" and row["outcome"] == "target"
        assert row["regime"] == "trend"
        assert row["return_7d"] == pytest.approx(0.029)


class TestAlertsAndBacktests:
    def test_alerts_record_and_read_back_newest_first(self, store):
        now = datetime.now(UTC)
        store.record_alert(GOLD, "new_signal", "t1", "b1", ts=now - timedelta(hours=2))
        store.record_alert(GOLD, "new_signal", "t2", "b2", ts=now)
        assert store.last_alert(GOLD, "new_signal")["title"] == "t2"
        assert store.last_alert(SILVER) is None
        assert len(store.alerts()) == 2

    def test_backtest_runs_round_trip_their_json(self, store):
        store.write_backtest_run(
            {
                "asset": GOLD,
                "kind": "walk_forward_oos",
                "weights": {"technical": 0.3},
                "metrics": {"oos": {"win_rate": 0.52, "trades": 80}},
                "sample_size": 80,
            }
        )
        runs = store.backtest_runs(GOLD)
        assert runs[0]["metrics"]["oos"]["win_rate"] == 0.52
        assert runs[0]["weights"]["technical"] == 0.3


class TestFailureMode:
    def test_a_failed_write_raises_rather_than_being_swallowed(self, store):
        # The divergence from formforge's telemetry store, asserted so nobody copies the
        # wrong error handling in: a dropped signal row corrupts the published win rate
        # silently, which is worse than a failed cycle.
        with store.connection() as conn:
            conn.execute("DROP TABLE signals")
        with pytest.raises(RuntimeError, match="store write failed"):
            store.write_signal(make_signal())

    def test_naive_timestamps_are_rejected_at_the_boundary(self, store):
        signal = make_signal(ts=datetime.now(UTC))
        object.__setattr__(signal, "timestamp", datetime(2026, 1, 1))
        with pytest.raises(ValueError, match="timezone-aware"):
            store.write_signal(signal)

    def test_counts_cover_every_table(self, store):
        counts = store.counts()
        assert set(counts) == set(TABLES)
        assert all(value == 0 for value in counts.values())

    def test_wal_is_enabled_so_reads_and_writes_do_not_block(self, store):
        with store.connection() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


class TestHeadlines:
    def test_rescoring_updates_scores_and_keeps_the_story(self, store):
        row = {
            "id": "h1",
            "ts": datetime.now(UTC).isoformat(),
            "title": "Dovish Fed",
            "source": "wire",
            "provider": "test",
            "url": None,
            "summary": None,
            "provider_sentiment": 0.2,
            "direction": 0.4,
            "relevance": 0.5,
            "scorer": "lexicon",
            "rationale": "dovish",
            "assets": json.dumps([GOLD]),
        }
        store.write_headlines([row])
        store.write_headlines([{**row, "direction": 0.8, "scorer": "llm"}])
        stored = store.recent_headlines(datetime.now(UTC) - timedelta(hours=1))
        assert len(stored) == 1
        assert stored[0]["direction"] == 0.8
        assert stored[0]["scorer"] == "llm"
        assert stored[0]["title"] == "Dovish Fed"

    def test_recent_headlines_respects_the_window(self, store):
        old = {
            "id": "h2",
            "ts": (datetime.now(UTC) - timedelta(days=10)).isoformat(),
            "title": "Old news",
            "source": "wire",
            "provider": "test",
            "url": None,
            "summary": None,
            "provider_sentiment": None,
            "direction": 0.0,
            "relevance": 0.1,
            "scorer": "lexicon",
            "rationale": "",
            "assets": "[]",
        }
        store.write_headlines([old])
        assert store.recent_headlines(datetime.now(UTC) - timedelta(days=1)) == []
        assert len(store.recent_headlines(datetime.now(UTC) - timedelta(days=30))) == 1
