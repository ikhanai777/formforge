"""The two databases the account store runs on, behind one interface.

The requirement this exists to satisfy is that Postgres preserve the SQLite
ledger semantics *exactly*: the same atomic deduction, the same expiry, the
same idempotency, the same refusal to go negative. Two hand-written backends
satisfy that on the day they are written and drift afterwards, and the drift is
invisible until it costs somebody money.

So the SQL is written once, in `store.py`, and a dialect supplies only the
things the two engines genuinely spell differently:

``placeholders``
    ``?`` against ``%s``. Statements are written with ``?`` and translated.

``schema``
    The migration files in `migrations/`, one pair per revision.

``the transaction``
    And specifically **how the read-then-write in `spend` is serialised**,
    which is the whole ballgame:

    - SQLite: ``BEGIN IMMEDIATE`` takes the database's single write lock up
      front, before the balance is read. One writer at a time, process-wide
      and file-wide.
    - Postgres: ``SELECT ... FROM users WHERE id = ? FOR UPDATE`` takes a row
      lock on the one account being charged. Two people spending their own
      credits do not block each other, which is both correct and the reason a
      row lock is the right tool rather than a table-wide one.

    Different mechanisms, identical guarantee: between reading a balance and
    writing against it, nobody else can do the same for that user.

``types``
    Postgres has `uuid`, `timestamptz` and `jsonb`; SQLite has text. Rows are
    normalised on the way out so callers see one representation -- ids as
    canonical UUID strings, timestamps as ISO-8601 UTC seconds -- whichever
    engine produced them. Without that the two backends return different
    Python types for the same column and every caller grows a branch.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid as _uuid
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol

MIGRATIONS = Path(__file__).parent / "migrations"

# Every revision, in order. Adding one means adding a `<id>.sqlite.sql` and a
# `<id>.postgres.sql` beside it; the runner applies whatever has not been
# applied and records it. Re-running is a no-op, which is what makes deploying
# the same migration set twice safe.
REVISIONS = ("0001_accounts", "0002_password_resets")


def new_id() -> str:
    """A canonical UUID string.

    Canonical (hyphenated) rather than `.hex`, because a Postgres `uuid` column
    normalises what it stores: write 32 bare hex characters and read back 36
    with hyphens. SQLite, storing text, gives back exactly what it was handed.
    Generating the canonical form is what makes an id round-trip identically on
    both, and an id that changes shape depending on the backend is the kind of
    thing that passes every test and breaks one join in production.
    """
    return str(_uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: Any) -> Any:
    """A timestamp as ISO-8601 UTC to the second, whatever the driver returned."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
    return value


class Dialect(Protocol):
    name: str

    def translate(self, sql: str) -> str: ...
    def close(self) -> None: ...
    @contextmanager
    def transaction(self) -> Iterator[Any]: ...
    @contextmanager
    def reader(self) -> Iterator[Any]: ...
    def lock_user(self, conn: Any, user_id: str) -> None: ...
    def stamp(self, moment: datetime) -> Any: ...
    def now(self) -> Any: ...
    def dumps(self, value: Any) -> Any: ...
    def loads(self, value: Any) -> Any: ...
    def columns(self, table: str) -> set[str]: ...


def _rows(cursor: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------
class SqliteDialect:
    """One connection behind a lock, which is what SQLite wants anyway.

    A pool would be worse here, not better: SQLite serialises writers at the
    file level regardless, so extra connections buy contention and a busier
    error path rather than throughput.
    """

    name = "sqlite"
    placeholder = "?"

    def __init__(self, path: Path | str):
        self.path = path
        self._lock = threading.RLock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            # The busy timeout: a BEGIN IMMEDIATE that collides with another
            # process waits rather than failing outright.
            timeout=10.0,
            # Explicit transactions. Python's implicit handling will not issue
            # BEGIN IMMEDIATE, and BEGIN IMMEDIATE is the guarantee.
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def translate(self, sql: str) -> str:
        return sql

    def close(self) -> None:
        with self._lock, suppress(Exception):
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._rollback()
                raise
            try:
                self._conn.execute("COMMIT")
            except BaseException:
                # SQLite leaves the transaction open when a COMMIT fails (a
                # full disk, a lost lock). Without this the connection is
                # poisoned and every later BEGIN IMMEDIATE fails with "cannot
                # start a transaction within a transaction" -- one failed write
                # turning into all of them failing, which reads as a far
                # stranger bug than the disk being full.
                self._rollback()
                raise

    def _rollback(self) -> None:
        with suppress(Exception):
            self._conn.execute("ROLLBACK")

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn

    def lock_user(self, conn: Any, user_id: str) -> None:
        """No-op: BEGIN IMMEDIATE already holds the only write lock there is."""

    def stamp(self, moment: datetime) -> str:
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds")

    def now(self) -> str:
        return self.stamp(utcnow())

    def dumps(self, value: Any) -> str:
        return json.dumps(value or {}, default=str)

    def loads(self, value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        return json.loads(value or "{}")

    def columns(self, table: str) -> set[str]:
        with self.reader() as conn:
            return {r["name"] for r in _rows(conn.execute(f"PRAGMA table_info({table})"))}

    def migrate(self) -> list[str]:
        """Apply what has not been applied.

        Not wrapped in `transaction()`, and that is forced rather than chosen:
        `executescript` issues an implicit COMMIT before it runs, which ends
        the BEGIN IMMEDIATE out from under the context manager, and the
        matching COMMIT then fails with "no transaction is active".

        Safe anyway, because of how the migration files are written: every
        statement in them is `IF NOT EXISTS`, and the record is `INSERT OR
        IGNORE`. A crash between applying a revision and recording it leaves
        the next run re-applying DDL that does nothing and then recording it.
        Repeatable by construction rather than by transaction.
        """
        applied: list[str] = []
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " id text PRIMARY KEY, applied_at text NOT NULL)"
            )
            done = {
                row["id"]
                for row in _rows(self._conn.execute("SELECT id FROM schema_migrations"))
            }
            for revision in REVISIONS:
                if revision in done:
                    continue
                self._conn.executescript((MIGRATIONS / f"{revision}.sqlite.sql").read_text())
                self._conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (id, applied_at) VALUES (?,?)",
                    (revision, self.now()),
                )
                applied.append(revision)
        return applied


# --------------------------------------------------------------------------
# PostgreSQL
# --------------------------------------------------------------------------
class PostgresDialect:
    """A real pool, and per-user row locks instead of a global write lock."""

    name = "postgres"
    placeholder = "%s"

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8):
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.string import TextLoader
        from psycopg_pool import ConnectionPool

        def configure(conn: Any) -> None:
            # Hand back `uuid` columns as plain strings, which is what SQLite
            # returns for the same column. Without this the two backends give
            # callers different Python types for `user_id` -- a UUID here, a
            # str there -- and every comparison, dict key and JSON response
            # downstream grows a branch for which database answered. Doing it
            # at the driver rather than in each row-cleaning helper means a
            # query written later cannot forget.
            conn.adapters.register_loader("uuid", TextLoader)

        self._psycopg = psycopg
        self.dsn = dsn
        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=True,
            timeout=15.0,
            configure=configure,
            kwargs={"row_factory": dict_row, "autocommit": False},
        )
        self._pool.wait(timeout=15.0)

    def translate(self, sql: str) -> str:
        """`?` placeholders to `%s`.

        Safe because these statements are literals in `store.py` with no `?`
        inside any string constant, and because psycopg's own `%` escaping is
        not in play -- parameters are passed separately, never interpolated.
        """
        return _QMARK.sub("%s", sql)

    def close(self) -> None:
        with suppress(Exception):
            self._pool.close()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._pool.connection() as conn:
            # psycopg opens a transaction implicitly and commits on a clean
            # exit from this block, rolling back on an exception. Both are the
            # behaviour wanted here.
            yield conn

    @contextmanager
    def reader(self) -> Iterator[Any]:
        with self._pool.connection() as conn:
            yield conn

    def lock_user(self, conn: Any, user_id: str) -> None:
        """Serialise everything that touches this user's balance.

        This is the Postgres half of the concurrent-spend guarantee. Two
        transactions reaching here for the same user proceed one at a time; two
        for different users do not wait on each other at all.
        """
        conn.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))

    def stamp(self, moment: datetime) -> datetime:
        return moment

    def now(self) -> datetime:
        return self.stamp(utcnow())

    def dumps(self, value: Any) -> Any:
        from psycopg.types.json import Jsonb

        return Jsonb(value or {})

    def loads(self, value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        return json.loads(value or "{}")

    def columns(self, table: str) -> set[str]:
        with self.reader() as conn:
            rows = _rows(conn.execute(
                "SELECT column_name AS name FROM information_schema.columns"
                " WHERE table_name = %s AND table_schema = current_schema()",
                (table,),
            ))
        return {r["name"] for r in rows}

    def migrate(self) -> list[str]:
        """Apply what has not been applied, under an advisory lock.

        The lock matters: two workers starting at once both see an unapplied
        revision and both run it, and the loser gets a duplicate-object error
        at boot. This is the same class of bug as the view-creation race that
        the Phase 0 concurrency test caught, and it is cheaper to prevent than
        to diagnose at three in the morning.
        """
        applied: list[str] = []
        with self.transaction() as conn:
            # An arbitrary but fixed key: any two processes running this
            # function agree on it, and nothing else in the system uses it.
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (0x464F524D,))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " id text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {r["id"] for r in _rows(conn.execute("SELECT id FROM schema_migrations"))}
            for revision in REVISIONS:
                if revision in done:
                    continue
                sql = (MIGRATIONS / f"{revision}.postgres.sql").read_text()
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (id, applied_at) VALUES (%s, now())"
                    " ON CONFLICT (id) DO NOTHING",
                    (revision,),
                )
                applied.append(revision)
        return applied


_QMARK = re.compile(r"\?")


def open_dialect(target: Path | str | None = None) -> Dialect:
    """Pick a backend from what it looks like.

    A Postgres DSN (`postgresql://...`, `postgres://...`, or a libpq keyword
    string) gets the Postgres dialect; anything else is a SQLite path. Explicit
    enough to be predictable, and it means one configuration value selects the
    backend rather than two that can disagree.
    """
    if isinstance(target, str) and (
        target.startswith(("postgresql://", "postgres://"))
        or ("=" in target and "dbname" in target)
    ):
        return PostgresDialect(target)
    return SqliteDialect(target or ":memory:")
