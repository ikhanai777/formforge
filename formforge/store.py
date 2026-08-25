"""Persistence for the things that cannot be reconstructed later (spec §11).

Three of the tables here exist to be *read months from now*, and none of them
can be backfilled:

``generation_events``
    One row per loop step. Without it you can see that a generation took four
    iterations but not what changed between them, and every prompt or hint
    change becomes guesswork dressed up as judgement.

``print_feedback``
    Whether the thing actually printed. Every DFM constant in this system is a
    conventional maker value; this table is the only path from folklore to
    evidence.

``policy_events``
    Refusals, kept for content-policy review. A user working through the IP
    classifier one franchise at a time is a signal that only exists if it was
    written down at the time.

**SQLite, deliberately.** `docs/schema.sql` is Postgres and stays the target;
this module implements the same tables and the same column names on stdlib
sqlite3, so moving to Postgres is a dialect change rather than a redesign. The
reason to start here is not simplicity for its own sake: a persistence layer
that needs a running database is one that gets switched off in development, and
a table that is empty for the first six months is worth nothing. Collection has
to be the default, and only a file-backed store can be.

Where the two dialects differ, this module stores the Postgres *shape*:

* ``uuid`` and ``timestamptz`` become ``text`` -- ids are hex, timestamps are
  ISO-8601 UTC, both of which Postgres accepts on the way in.
* ``jsonb`` and ``text[]`` become JSON in a ``text`` column. `issues` is a JSON
  array so a row round-trips into the Postgres array unchanged.
* ``bigserial`` becomes ``integer primary key autoincrement``.
* The embedding column and its ivfflat index are omitted; vector search is not
  something SQLite should pretend to do.

Writes never raise into the caller. Losing a telemetry row is a bad day;
failing a generation the user is waiting on because telemetry could not be
written is a worse one. Failures are logged and counted, and `write_failures`
is what a health check should look at.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("formforge.store")

DEFAULT_PATH = Path(
    os.environ.get("FORMFORGE_DB", Path.home() / ".formforge" / "formforge.db")
)

# Mirrors the CHECK constraints in docs/schema.sql. Kept as Python sets as well
# as SQL constraints so a bad value is refused with a readable message rather
# than a sqlite3.IntegrityError from four frames down.
STATUSES = frozenset({"queued", "running", "ok", "failed", "refused", "needs_clarification"})
ROUTES = frozenset({"template", "template_seed", "freeform"})
PHASES = frozenset(
    {
        "policy", "intent", "route", "codegen", "execute", "validate", "render",
        "critique", "escalate", "slice", "failed", "done",
    }
)
DECISIONS = frozenset({"allow", "flag", "refuse"})

# The closed vocabulary for what went wrong on a print, from `docs/schema.sql`.
# Closed rather than free text on purpose: this column is the eventual input to
# tuning the DFM constants, and "warped a bit on the corner" and "warping" have
# to be the same row for that to be possible. Each term names a failure the
# validator has a corresponding check for, so an issue can be read back against
# what was measured at the time.
PRINT_ISSUES = frozenset(
    {
        "warping",          # plate contact / first-layer area
        "support_failure",  # overhang fraction
        "weak",             # wall thickness, feature size
        "dimension_off",    # dimensional fidelity
        "text_illegible",   # cap height, stroke width, relief depth
        "didnt_fit",        # build volume, or the user's own measurements
        "layer_shift",      # printer-side, kept so it is not filed as "other"
        "poor_adhesion",    # plate contact
        "other",
    }
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    id                text PRIMARY KEY,
    user_id           text,
    parent_id         text REFERENCES models(id) ON DELETE SET NULL,
    template_id       text,
    template_version  integer,
    prompt            text NOT NULL,
    parsed_intent     text NOT NULL DEFAULT '{}',
    params            text NOT NULL DEFAULT '{}',
    source_code       text NOT NULL DEFAULT '',
    language          text NOT NULL DEFAULT 'build123d',
    status            text NOT NULL
                      CHECK (status IN ('queued','running','ok','failed','refused',
                                        'needs_clarification')),
    route             text NOT NULL DEFAULT 'template'
                      CHECK (route IN ('template','template_seed','freeform')),
    iterations        integer NOT NULL DEFAULT 1,
    bbox_mm           text,
    volume_mm3        real,
    triangle_count    integer,
    validation        text,
    slice_summary     text,
    artifacts         text,
    model_used        text,
    tokens_in         integer,
    tokens_out        integer,
    cache_read_tokens integer,
    cost_usd          real,
    duration_ms       integer,
    is_public         integer NOT NULL DEFAULT 0,
    created_at        text NOT NULL
);

CREATE INDEX IF NOT EXISTS models_user_idx ON models (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS models_template_idx ON models (template_id);
CREATE INDEX IF NOT EXISTS models_parent_idx ON models (parent_id);

CREATE TABLE IF NOT EXISTS generation_events (
    id          integer PRIMARY KEY AUTOINCREMENT,
    model_id    text NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    step        integer NOT NULL,
    phase       text NOT NULL
                CHECK (phase IN ('policy','intent','route','codegen','execute',
                                 'validate','render','critique','escalate',
                                 'slice','failed','done')),
    ok          integer NOT NULL,
    error_class text,
    payload     text,
    duration_ms integer,
    created_at  text NOT NULL
);

CREATE INDEX IF NOT EXISTS generation_events_model_idx ON generation_events (model_id, step);
CREATE INDEX IF NOT EXISTS generation_events_errors_idx
    ON generation_events (error_class, created_at DESC) WHERE NOT ok;

CREATE TABLE IF NOT EXISTS print_feedback (
    id         text PRIMARY KEY,
    model_id   text NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    user_id    text,
    printed    integer NOT NULL,
    success    integer,
    printer    text,
    material   text,
    issues     text NOT NULL DEFAULT '[]',
    photo_uri  text,
    notes      text,
    created_at text NOT NULL
);

CREATE INDEX IF NOT EXISTS print_feedback_model_idx ON print_feedback (model_id);

CREATE TABLE IF NOT EXISTS policy_events (
    id         integer PRIMARY KEY AUTOINCREMENT,
    user_id    text,
    prompt     text NOT NULL,
    decision   text NOT NULL CHECK (decision IN ('allow','flag','refuse')),
    category   text,
    matched    text NOT NULL DEFAULT '[]',
    created_at text NOT NULL
);

CREATE INDEX IF NOT EXISTS policy_events_user_idx ON policy_events (user_id, created_at DESC);
"""

# The two views the product actually reads, in SQLite dialect. Same names,
# same columns and the same meaning as the Postgres definitions -- a dashboard
# written against one runs against the other.
VIEWS = """
DROP VIEW IF EXISTS template_health;
CREATE VIEW template_health AS
SELECT
    m.template_id                                  AS template_id,
    m.template_version                             AS template_version,
    count(*)                                       AS generations,
    avg(CASE WHEN m.status = 'ok' THEN 1.0 ELSE 0.0 END) AS success_rate,
    avg(m.iterations)                              AS mean_iterations,
    avg(m.duration_ms)                             AS mean_duration_ms,
    sum(m.cost_usd)                                AS total_cost_usd,
    (SELECT count(*) FROM print_feedback p
      WHERE p.model_id = m.id AND p.printed = 1)   AS prints_reported,
    (SELECT avg(CASE WHEN p.success THEN 1.0 ELSE 0.0 END) FROM print_feedback p
      WHERE p.model_id = m.id AND p.printed = 1)   AS print_success_rate
FROM models m
WHERE m.template_id IS NOT NULL
GROUP BY m.template_id, m.template_version;

DROP VIEW IF EXISTS print_outcomes;
CREATE VIEW print_outcomes AS
SELECT
    pf.model_id                                             AS model_id,
    pf.success                                              AS success,
    pf.issues                                               AS issues,
    pf.printer                                              AS printer,
    pf.material                                             AS material,
    m.template_id                                           AS template_id,
    json_extract(m.validation, '$.measurements.min_wall_mm')          AS min_wall_mm,
    json_extract(m.validation, '$.measurements.overhang_fraction')    AS overhang_fraction,
    json_extract(m.validation, '$.measurements.max_bridge_mm')        AS max_bridge_mm,
    json_extract(m.validation, '$.measurements.plate_contact_fraction') AS plate_contact_fraction,
    json_array_length(coalesce(json_extract(m.validation, '$.warnings'), '[]')) AS warning_count
FROM print_feedback pf
JOIN models m ON m.id = pf.model_id
WHERE pf.printed = 1;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    """JSON for a column, never raising on something unserialisable.

    Telemetry containing one exotic object must not be the reason a row is
    lost, so anything json cannot handle is stringified.
    """
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value))


class Store:
    """The persistence layer. Thread-safe, and quiet when it fails.

    One connection guarded by a lock rather than a pool: writes here are a
    handful per generation against a local file, and the loop that produces
    them is already the slow part by four orders of magnitude.
    """

    def __init__(self, path: Path | str | None = None):
        self.path = ":memory:" if path == ":memory:" else Path(path or DEFAULT_PATH)
        self.write_failures = 0
        self._lock = threading.RLock()
        if self.path != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path if self.path == ":memory:" else str(self.path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        # WAL so a reader (a dashboard, a `formforge stats` run) never blocks
        # the generation that is writing. foreign_keys is off by default in
        # SQLite, and a generation_events row pointing at no model is exactly
        # the corruption this table exists to avoid.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.executescript(VIEWS)
        self._conn.commit()

    # -- lifecycle ---------------------------------------------------------
    @classmethod
    def memory(cls) -> "Store":
        """An in-process store, for tests and for `--no-store` runs."""
        return cls(":memory:")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _write(self, what: str) -> Iterator[sqlite3.Connection]:
        """Run a write, swallowing failure into a counter.

        The rule is in the module docstring and it is the whole reason this
        wrapper exists: a telemetry failure must never propagate into a
        generation the user is waiting on.
        """
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:  # noqa: BLE001
                self.write_failures += 1
                log.exception("store: %s failed", what)
                try:
                    self._conn.rollback()
                except Exception:  # noqa: BLE001
                    pass

    # -- models ------------------------------------------------------------
    def record_generation(self, result, *, user_id: str | None = None,
                          parent_id: str | None = None) -> None:
        """Persist one finished generation and every step that produced it.

        Called for failures and refusals as well as successes -- a store that
        only holds the runs that worked cannot answer any question worth
        asking.
        """
        stats = getattr(result, "stats", None) or {}
        usage = getattr(result, "usage", None)
        status = result.status if result.status in STATUSES else "failed"
        route = result.route if result.route in ROUTES else "freeform"

        with self._write("record_generation") as conn:
            # A parent that was never recorded costs the lineage, not the row.
            # The generation is the evidence; the edge between two of them is
            # a convenience, and losing the first to preserve the second would
            # be the wrong trade.
            if parent_id is not None and not conn.execute(
                "SELECT 1 FROM models WHERE id = ?", (parent_id,)
            ).fetchone():
                log.warning("store: parent %s not recorded; keeping the child", parent_id)
                parent_id = None
            # Re-recording the same id replaces it. Clear its steps first
            # rather than relying on the cascade, so a re-record cannot end up
            # with two copies of the loop it describes.
            conn.execute("DELETE FROM generation_events WHERE model_id = ?", (result.model_id,))
            conn.execute(
                """
                INSERT OR REPLACE INTO models (
                    id, user_id, parent_id, template_id, template_version, prompt,
                    parsed_intent, params, source_code, language, status, route,
                    iterations, bbox_mm, volume_mm3, triangle_count, validation,
                    slice_summary, artifacts, model_used, tokens_in, tokens_out,
                    cache_read_tokens, cost_usd, duration_ms, is_public, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    result.model_id,
                    user_id,
                    parent_id,
                    result.template_id,
                    stats.get("template_version"),
                    result.prompt,
                    _dumps(getattr(result, "intent", {}) or {}),
                    _dumps(getattr(result, "params", {}) or {}),
                    getattr(result, "source_code", "") or "",
                    getattr(result, "language", "build123d"),
                    status,
                    route,
                    getattr(result, "iterations", 0) or 0,
                    _dumps(stats.get("bbox_mm")) if stats.get("bbox_mm") else None,
                    stats.get("volume_mm3"),
                    stats.get("triangles"),
                    _dumps(result.validation) if result.validation else None,
                    _dumps(stats.get("slice")) if stats.get("slice") else None,
                    _dumps(getattr(result, "artifacts", {}) or {}),
                    ", ".join(usage.models_used) if usage and usage.models_used else None,
                    getattr(usage, "input_tokens", None),
                    getattr(usage, "output_tokens", None),
                    getattr(usage, "cache_read_tokens", None),
                    getattr(usage, "cost_usd", None),
                    getattr(result, "duration_ms", None),
                    0,
                    _now(),
                ),
            )
            rows = [
                (
                    result.model_id,
                    event.step,
                    event.phase if event.phase in PHASES else "failed",
                    1 if event.ok else 0,
                    _error_class(event),
                    _dumps(event.payload),
                    event.duration_ms,
                    _now(),
                )
                for event in getattr(result, "events", []) or []
            ]
            if rows:
                conn.executemany(
                    """
                    INSERT INTO generation_events
                        (model_id, step, phase, ok, error_class, payload, duration_ms, created_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    rows,
                )

    def get_model(self, model_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM models WHERE id = ?", (model_id,)
            ).fetchone()
        return _row_to_model(row) if row else None

    def recent_models(self, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM models"
        args: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_model(r) for r in rows]

    def events_for(self, model_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM generation_events WHERE model_id = ? ORDER BY id",
                (model_id,),
            ).fetchall()
        out = []
        for row in rows:
            event = dict(row)
            event["ok"] = bool(event["ok"])
            event["payload"] = json.loads(event["payload"] or "{}")
            out.append(event)
        return out

    # -- feedback ----------------------------------------------------------
    def record_feedback(self, payload: dict[str, Any], *, user_id: str | None = None) -> str:
        """Record a print outcome. Returns the feedback id.

        Unlike the telemetry writes, this one is allowed to fail loudly: the
        caller is a user telling us what happened to their print, and silently
        dropping that is worse than a 500 they can retry.
        """
        feedback_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO print_feedback
                    (id, model_id, user_id, printed, success, printer, material,
                     issues, photo_uri, notes, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    feedback_id,
                    payload["model_id"],
                    user_id,
                    1 if payload.get("printed") else 0,
                    None if payload.get("success") is None else int(bool(payload["success"])),
                    payload.get("printer"),
                    payload.get("material"),
                    _dumps(list(payload.get("issues") or [])),
                    payload.get("photo_uri"),
                    payload.get("notes"),
                    _now(),
                ),
            )
            self._conn.commit()
        return feedback_id

    def record_policy_event(
        self,
        prompt: str,
        decision: str,
        *,
        category: str | None = None,
        matched: list[str] | None = None,
        user_id: str | None = None,
    ) -> None:
        with self._write("record_policy_event") as conn:
            conn.execute(
                """
                INSERT INTO policy_events
                    (user_id, prompt, decision, category, matched, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    user_id,
                    prompt,
                    decision if decision in DECISIONS else "flag",
                    category,
                    _dumps(list(matched or [])),
                    _now(),
                ),
            )

    # -- the views ---------------------------------------------------------
    def template_health(self) -> list[dict[str, Any]]:
        """Which templates earn their place, and which are quietly failing.

        A template with a low success rate is a registry bug that traffic is
        still being routed to.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM template_health ORDER BY generations DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def print_outcomes(self) -> list[dict[str, Any]]:
        """What went wrong on real prints, against what the validator measured.

        The empirical basis for tuning the DFM constants -- which are, until
        this table has rows in it, conventional values rather than measured
        ones.
        """
        with self._lock:
            rows = self._conn.execute("SELECT * FROM print_outcomes").fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["issues"] = json.loads(record["issues"] or "[]")
            record["success"] = None if record["success"] is None else bool(record["success"])
            out.append(record)
        return out

    def failure_classes(self, limit: int = 10) -> list[dict[str, Any]]:
        """Which error classes dominate, most common first.

        The question this answers -- "are the failures we are fixing the ones
        that actually happen?" -- has no other source.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT error_class, count(*) AS occurrences,
                       max(created_at) AS last_seen
                FROM generation_events
                WHERE ok = 0 AND error_class IS NOT NULL
                GROUP BY error_class
                ORDER BY occurrences DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def totals(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    count(*) AS generations,
                    sum(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS succeeded,
                    sum(CASE WHEN status = 'refused' THEN 1 ELSE 0 END) AS refused,
                    sum(cost_usd) AS cost_usd,
                    avg(iterations) AS mean_iterations
                FROM models
                """
            ).fetchone()
            prints = self._conn.execute(
                "SELECT count(*) AS n, sum(success) AS ok FROM print_feedback WHERE printed = 1"
            ).fetchone()
        # An empty table makes every aggregate NULL. A dashboard showing a
        # blank where a zero belongs reads as "broken", not as "nothing yet".
        total = {k: (v if v is not None else 0) for k, v in dict(row).items()}
        total["cost_usd"] = round(total["cost_usd"] or 0.0, 5)
        total["prints_reported"] = prints["n"] or 0
        total["prints_successful"] = prints["ok"] or 0
        total["write_failures"] = self.write_failures
        return total


def _error_class(event) -> str | None:
    """The failure label for a step, from the event's own payload.

    Prefers whatever the producing step named the failure -- the sandbox's
    error classes and the validator's check ids are already the vocabulary
    everything else groups by. Falls back to the phase, so a failure is never
    recorded as unclassified.
    """
    if event.ok:
        return None
    payload = event.payload or {}
    for key in ("error_class", "error", "failed_check", "check"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    failures = payload.get("failures")
    if isinstance(failures, list) and failures:
        first = failures[0]
        if isinstance(first, dict):
            label = first.get("id") or first.get("check")
            if isinstance(label, str) and label:
                return label
        elif isinstance(first, str):
            return first
    return f"{event.phase}_failed"


_JSON_COLUMNS = ("parsed_intent", "params", "bbox_mm", "validation", "slice_summary", "artifacts")


def _row_to_model(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    for column in _JSON_COLUMNS:
        raw = record.get(column)
        if isinstance(raw, str):
            try:
                record[column] = json.loads(raw)
            except (TypeError, ValueError):
                pass
    record["is_public"] = bool(record.get("is_public"))
    return record
