"""Persistence. TimescaleDB is the target; stdlib sqlite3 is what actually runs.

Spec section 8 picks TimescaleDB, and ``docs/bullion-schema.sql`` is that target,
hypertables and all. This module implements the same tables and the same column names
on sqlite3, for the reason formforge's store gives and that applies twice over here: a
persistence layer that needs a database server is one that gets switched off in
development, and the ``signals`` table is worthless unless it has been collecting since
day one. ``tests/test_bullion_store.py`` asserts the two schemas agree column for
column, so the move to Postgres stays a dialect change rather than a redesign.

Where the dialects differ, the Postgres *shape* is stored: ``timestamptz`` becomes ISO-8601
UTC text, ``date`` becomes ``YYYY-MM-DD``, ``jsonb`` becomes JSON in a text column, and the
hypertable partitioning is simply absent (sqlite has no equivalent and does not need one
at this volume).

**One deliberate divergence from formforge's store: writes raise.** formforge swallows
telemetry failures because losing a metric is better than failing a user's generation.
Here the ``signals`` table *is* the product's credibility -- spec section 5 publishes a win
rate computed from it -- and a silently dropped signal row makes the track record a lie
that nobody can detect afterwards. A write that fails must be loud.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .series import Candle, Series

log = logging.getLogger("bullion.store")

SCHEMA: tuple[str, ...] = (
    # -- market data -------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS prices (
        asset     text    NOT NULL,
        interval  text    NOT NULL,
        ts        text    NOT NULL,
        open      real    NOT NULL,
        high      real    NOT NULL,
        low       real    NOT NULL,
        close     real    NOT NULL,
        volume    real    NOT NULL DEFAULT 0,
        source    text    NOT NULL,
        PRIMARY KEY (asset, interval, ts)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS indicators (
        asset     text NOT NULL,
        interval  text NOT NULL,
        ts        text NOT NULL,
        payload   text NOT NULL,
        PRIMARY KEY (asset, interval, ts)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS macro_series (
        series_id text NOT NULL,
        day       text NOT NULL,
        value     real NOT NULL,
        units     text NOT NULL DEFAULT '',
        source    text NOT NULL,
        PRIMARY KEY (series_id, day)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cot_reports (
        asset          text NOT NULL,
        report_date    text NOT NULL,
        noncomm_long   real NOT NULL,
        noncomm_short  real NOT NULL,
        open_interest  real NOT NULL,
        source         text NOT NULL,
        PRIMARY KEY (asset, report_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS news_events (
        id                  text PRIMARY KEY,
        ts                  text NOT NULL,
        title               text NOT NULL,
        source              text NOT NULL,
        provider            text NOT NULL,
        url                 text,
        summary             text,
        provider_sentiment  real,
        direction           real,
        relevance           real,
        scorer              text,
        rationale           text,
        assets              text NOT NULL DEFAULT '[]'
    )
    """,
    # -- signals and their grading ----------------------------------------
    """
    CREATE TABLE IF NOT EXISTS signals (
        id                 text PRIMARY KEY,
        asset              text NOT NULL,
        interval           text NOT NULL,
        ts                 text NOT NULL,
        state              text NOT NULL,
        strength           text NOT NULL,
        composite_score    real NOT NULL,
        confidence         real NOT NULL,
        price_at_signal    real NOT NULL,
        suggested_stop     real,
        suggested_target   real,
        regime             text NOT NULL,
        weights            text NOT NULL,
        layers             text NOT NULL,
        confidence_detail  text NOT NULL,
        data_quality       text NOT NULL DEFAULT '{}',
        synthetic          integer NOT NULL DEFAULT 0,
        payload            text NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signal_outcomes (
        signal_id    text PRIMARY KEY,
        resolved_at  text NOT NULL,
        outcome      text NOT NULL,
        bars_held    integer NOT NULL DEFAULT 0,
        exit_price   real,
        price_1d     real,
        price_3d     real,
        price_7d     real,
        return_1d    real,
        return_3d    real,
        return_7d    real,
        mfe_r        real,
        mae_r        real,
        notes        text
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alerts (
        id         text PRIMARY KEY,
        ts         text NOT NULL,
        asset      text NOT NULL,
        kind       text NOT NULL,
        signal_id  text,
        title      text NOT NULL,
        body       text NOT NULL,
        delivered  integer NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backtest_runs (
        id           text PRIMARY KEY,
        created_at   text NOT NULL,
        asset        text NOT NULL,
        interval     text NOT NULL,
        kind         text NOT NULL,
        train_start  text,
        train_end    text,
        test_start   text,
        test_end     text,
        weights      text NOT NULL,
        metrics      text NOT NULL,
        sample_size  integer NOT NULL DEFAULT 0
    )
    """,
    # Indexes the read paths actually use: the dashboard asks for the newest signal
    # per asset on every load, and the scorecard scans graded signals by asset.
    "CREATE INDEX IF NOT EXISTS idx_signals_asset_ts ON signals (asset, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_prices_asset_ts ON prices (asset, interval, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_alerts_asset_ts ON alerts (asset, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_news_ts ON news_events (ts DESC)",
)

TABLES: tuple[str, ...] = (
    "prices",
    "indicators",
    "macro_series",
    "cot_reports",
    "news_events",
    "signals",
    "signal_outcomes",
    "alerts",
    "backtest_runs",
)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _parse_ts(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class SignalRow:
    """A stored signal, rehydrated enough to grade and display without re-scoring."""

    id: str
    asset: str
    interval: str
    ts: datetime
    state: str
    strength: str
    composite_score: float
    confidence: float
    price_at_signal: float
    suggested_stop: float | None
    suggested_target: float | None
    regime: str
    synthetic: bool
    payload: dict[str, Any]

    @property
    def direction(self) -> int:
        if self.state in ("BUY", "STRONG_BUY"):
            return 1
        if self.state in ("SELL", "STRONG_SELL"):
            return -1
        return 0


class Store:
    """SQLite-backed persistence for everything the engine needs to remember.

    Thread-safe by way of one connection per thread (sqlite3 connections are not
    shareable across threads), which is what the FastAPI app needs.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.connection() as conn:
            for statement in SCHEMA:
                conn.execute(statement)

    # -- plumbing ----------------------------------------------------------

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            # WAL so the API can read while the ingestion cycle writes, which is the
            # normal operating state rather than an edge case.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        try:
            yield conn
        except sqlite3.Error as exc:
            raise RuntimeError(f"store write failed: {exc}") from exc

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def columns(self, table: str) -> list[str]:
        with self.connection() as conn:
            return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]

    # -- market data -------------------------------------------------------

    def write_candles(self, series: Series) -> int:
        rows = [
            (
                series.asset,
                series.interval,
                _iso(candle.ts),
                candle.open,
                candle.high,
                candle.low,
                candle.close,
                candle.volume,
                series.source,
            )
            for candle in series
        ]
        if not rows:
            return 0
        with self.connection() as conn:
            conn.executemany(
                "INSERT INTO prices "
                "(asset, interval, ts, open, high, low, close, volume, source) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                # A re-fetched bar is a corrected bar; the provider's latest word wins.
                "ON CONFLICT (asset, interval, ts) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, "
                "close=excluded.close, volume=excluded.volume, source=excluded.source",
                rows,
            )
        return len(rows)

    def load_candles(
        self,
        asset: str,
        interval: str = "1day",
        limit: int = 400,
        until: datetime | None = None,
    ) -> Series:
        clause = "WHERE asset=? AND interval=?"
        params: list[Any] = [asset, interval]
        if until is not None:
            clause += " AND ts<=?"
            params.append(_iso(until))
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM prices {clause} ORDER BY ts DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        candles = [
            Candle(
                ts=_parse_ts(row["ts"]),
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
            )
            for row in reversed(rows)
        ]
        source = rows[0]["source"] if rows else "store"
        return Series.of(asset, interval, candles, source=source)

    def write_indicators(self, asset: str, interval: str, ts: datetime, payload: dict) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO indicators (asset, interval, ts, payload) VALUES (?,?,?,?) "
                "ON CONFLICT (asset, interval, ts) DO UPDATE SET payload=excluded.payload",
                (asset, interval, _iso(ts), json.dumps(payload, default=str)),
            )

    def write_macro(
        self,
        series_id: str,
        points: Sequence[tuple[date, float]],
        units: str,
        source: str,
    ) -> int:
        rows = [(series_id, day.isoformat(), value, units, source) for day, value in points]
        if not rows:
            return 0
        with self.connection() as conn:
            conn.executemany(
                "INSERT INTO macro_series (series_id, day, value, units, source) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT (series_id, day) DO UPDATE SET value=excluded.value, "
                "units=excluded.units, source=excluded.source",
                rows,
            )
        return len(rows)

    def write_cot(self, rows: Sequence[tuple[str, date, float, float, float, str]]) -> int:
        payload = [
            (asset, report_date.isoformat(), long_, short, oi, source)
            for asset, report_date, long_, short, oi, source in rows
        ]
        if not payload:
            return 0
        with self.connection() as conn:
            conn.executemany(
                "INSERT INTO cot_reports (asset, report_date, noncomm_long, noncomm_short, "
                "open_interest, source) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT (asset, report_date) DO UPDATE SET "
                "noncomm_long=excluded.noncomm_long, noncomm_short=excluded.noncomm_short, "
                "open_interest=excluded.open_interest, source=excluded.source",
                payload,
            )
        return len(payload)

    def write_headlines(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with self.connection() as conn:
            conn.executemany(
                "INSERT INTO news_events (id, ts, title, source, provider, url, summary, "
                "provider_sentiment, direction, relevance, scorer, rationale, assets) "
                "VALUES (:id,:ts,:title,:source,:provider,:url,:summary,:provider_sentiment,"
                ":direction,:relevance,:scorer,:rationale,:assets) "
                # Re-scoring an existing headline updates its scores; the story itself
                # is immutable, which is what makes the id a stable dedupe key.
                "ON CONFLICT (id) DO UPDATE SET direction=excluded.direction, "
                "relevance=excluded.relevance, scorer=excluded.scorer, "
                "rationale=excluded.rationale",
                rows,
            )
        return len(rows)

    def recent_headlines(self, since: datetime, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM news_events WHERE ts>=? ORDER BY ts DESC LIMIT ?",
                (_iso(since), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- signals -----------------------------------------------------------

    def write_signal(self, signal: Any, signal_id: str | None = None) -> str:
        """Persist a ``fusion.Signal``. Returns the row id.

        Takes the object rather than a dict so there is exactly one place that decides
        how a signal is serialised -- the payload column holds the full API response, so
        a track-record view never has to re-derive a historical call from parts.
        """
        row_id = signal_id or uuid.uuid4().hex
        payload = signal.as_dict()
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO signals (id, asset, interval, ts, state, strength, "
                "composite_score, confidence, price_at_signal, suggested_stop, "
                "suggested_target, regime, weights, layers, confidence_detail, "
                "data_quality, synthetic, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    signal.asset,
                    signal.interval,
                    _iso(signal.timestamp),
                    signal.state.value,
                    signal.state.strength,
                    signal.composite_score,
                    signal.confidence.value,
                    signal.price_at_signal,
                    signal.levels.stop,
                    signal.levels.target,
                    signal.regime.regime.value,
                    json.dumps(signal.weights),
                    json.dumps({k: v.as_dict() for k, v in signal.layers.items()}),
                    json.dumps(signal.confidence.as_dict()),
                    json.dumps(signal.data_quality),
                    1 if signal.synthetic else 0,
                    json.dumps(payload, default=str),
                ),
            )
        return row_id

    def _row_to_signal(self, row: sqlite3.Row) -> SignalRow:
        return SignalRow(
            id=row["id"],
            asset=row["asset"],
            interval=row["interval"],
            ts=_parse_ts(row["ts"]),
            state=row["state"],
            strength=row["strength"],
            composite_score=row["composite_score"],
            confidence=row["confidence"],
            price_at_signal=row["price_at_signal"],
            suggested_stop=row["suggested_stop"],
            suggested_target=row["suggested_target"],
            regime=row["regime"],
            synthetic=bool(row["synthetic"]),
            payload=json.loads(row["payload"]),
        )

    def latest_signal(self, asset: str, interval: str | None = None) -> SignalRow | None:
        clause = "WHERE asset=?" + (" AND interval=?" if interval else "")
        params = [asset] + ([interval] if interval else [])
        with self.connection() as conn:
            row = conn.execute(
                f"SELECT * FROM signals {clause} ORDER BY ts DESC, rowid DESC LIMIT 1", params
            ).fetchone()
        return self._row_to_signal(row) if row else None

    def signals(
        self,
        asset: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
        actionable_only: bool = False,
    ) -> list[SignalRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if asset:
            clauses.append("asset=?")
            params.append(asset)
        if since:
            clauses.append("ts>=?")
            params.append(_iso(since))
        if actionable_only:
            clauses.append("state IN ('BUY','STRONG_BUY','SELL','STRONG_SELL')")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM signals {where} ORDER BY ts DESC LIMIT ?", [*params, limit]
            ).fetchall()
        return [self._row_to_signal(row) for row in rows]

    def ungraded_signals(self, asset: str | None = None, limit: int = 500) -> list[SignalRow]:
        """Actionable signals with no outcome row yet -- the grader's work queue."""
        clause = (
            "WHERE o.signal_id IS NULL "
            "AND s.state IN ('BUY','STRONG_BUY','SELL','STRONG_SELL')"
        )
        params: list[Any] = []
        if asset:
            clause += " AND s.asset=?"
            params.append(asset)
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT s.* FROM signals s LEFT JOIN signal_outcomes o ON o.signal_id=s.id "
                f"{clause} ORDER BY s.ts ASC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [self._row_to_signal(row) for row in rows]

    def write_outcome(self, outcome: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO signal_outcomes (signal_id, resolved_at, outcome, bars_held, "
                "exit_price, price_1d, price_3d, price_7d, return_1d, return_3d, return_7d, "
                "mfe_r, mae_r, notes) VALUES (:signal_id,:resolved_at,:outcome,:bars_held,"
                ":exit_price,:price_1d,:price_3d,:price_7d,:return_1d,:return_3d,:return_7d,"
                ":mfe_r,:mae_r,:notes) "
                "ON CONFLICT (signal_id) DO UPDATE SET resolved_at=excluded.resolved_at, "
                "outcome=excluded.outcome, bars_held=excluded.bars_held, "
                "exit_price=excluded.exit_price, price_1d=excluded.price_1d, "
                "price_3d=excluded.price_3d, price_7d=excluded.price_7d, "
                "return_1d=excluded.return_1d, return_3d=excluded.return_3d, "
                "return_7d=excluded.return_7d, mfe_r=excluded.mfe_r, mae_r=excluded.mae_r, "
                "notes=excluded.notes",
                outcome,
            )

    def graded(self, asset: str | None = None, limit: int = 2000) -> list[dict[str, Any]]:
        """Signals joined to their outcomes. The scorecard's only input."""
        clause = "WHERE 1=1"
        params: list[Any] = []
        if asset:
            clause += " AND s.asset=?"
            params.append(asset)
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT s.id, s.asset, s.interval, s.ts, s.state, s.strength, s.regime, "
                "s.confidence, s.composite_score, s.price_at_signal, s.synthetic, "
                "o.outcome, o.bars_held, o.exit_price, o.return_1d, o.return_3d, o.return_7d, "
                "o.mfe_r, o.mae_r "
                "FROM signals s JOIN signal_outcomes o ON o.signal_id=s.id "
                f"{clause} ORDER BY s.ts DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [dict(row) for row in rows]

    # -- alerts ------------------------------------------------------------

    def record_alert(
        self,
        asset: str,
        kind: str,
        title: str,
        body: str,
        signal_id: str | None = None,
        ts: datetime | None = None,
        delivered: bool = False,
    ) -> str:
        row_id = uuid.uuid4().hex
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO alerts (id, ts, asset, kind, signal_id, title, body, delivered) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    _iso(ts or datetime.now(timezone.utc)),
                    asset,
                    kind,
                    signal_id,
                    title,
                    body,
                    1 if delivered else 0,
                ),
            )
        return row_id

    def last_alert(self, asset: str, kind: str | None = None) -> dict[str, Any] | None:
        clause = "WHERE asset=?" + (" AND kind=?" if kind else "")
        params = [asset] + ([kind] if kind else [])
        with self.connection() as conn:
            row = conn.execute(
                f"SELECT * FROM alerts {clause} ORDER BY ts DESC LIMIT 1", params
            ).fetchone()
        return dict(row) if row else None

    def alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    # -- backtests ---------------------------------------------------------

    def write_backtest_run(self, run: dict[str, Any]) -> str:
        row_id = run.get("id") or uuid.uuid4().hex
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO backtest_runs "
                "(id, created_at, asset, interval, kind, train_start, "
                "train_end, test_start, test_end, weights, metrics, sample_size) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    run.get("created_at") or _iso(datetime.now(timezone.utc)),
                    run["asset"],
                    run.get("interval", "1day"),
                    run.get("kind", "walk_forward"),
                    run.get("train_start"),
                    run.get("train_end"),
                    run.get("test_start"),
                    run.get("test_end"),
                    json.dumps(run.get("weights", {})),
                    json.dumps(run.get("metrics", {})),
                    int(run.get("sample_size", 0)),
                ),
            )
        return row_id

    def backtest_runs(self, asset: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        clause = "WHERE asset=?" if asset else ""
        params: list[Any] = [asset] if asset else []
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM backtest_runs {clause} ORDER BY created_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["weights"] = json.loads(record["weights"])
            record["metrics"] = json.loads(record["metrics"])
            out.append(record)
        return out

    # -- housekeeping ------------------------------------------------------

    def counts(self) -> dict[str, int]:
        """Row counts per table. What a health check should look at."""
        with self.connection() as conn:
            return {
                table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
                for table in TABLES
            }
