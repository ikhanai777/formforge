"""Backing up the things that cannot be reconstructed, and putting them back.

What is worth backing up is a short list, and it is short for a reason: most of
this system is derivable. A model can be rebuilt from `source.py` and
`params.json`, which is the whole reproducibility argument, so artifacts are a
convenience rather than a correctness concern. What cannot be rebuilt is the
record of what happened:

``credit_ledger``    why somebody was charged. Append-only, never deleted, and
                     not derivable from the payment processor -- it holds
                     spends, expiries and adjustments the processor never sees.
``billing_events``   what the processor said, and the replay guard. Losing it
                     means a redelivered webhook is applied a second time.
``users``            identities. Losing them orphans everything else.
``audit_log``        what an operator did.
``password_resets``  short-lived, and included only so a restore does not
                     resurrect a spent token as a live one.

**Restore is the half that is usually untested**, so this module makes it
runnable rather than documented: `verify_backup` opens an archive and checks it
before anyone relies on it, and `restore_local` refuses to overwrite anything
it was not explicitly told to.

Two safety rules, both structural rather than advisory:

* **Nothing is overwritten without `confirm=True`.** Not a flag with a default
  that drifts -- the parameter has no default at all, so a caller has to say it.
* **A restore checks migration compatibility first.** An archive from a newer
  schema going into an older binary is the restore that appears to work and
  then fails on the first query against a column that is not there.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# Tables whose loss cannot be repaired, most costly first. Used by the
# verifier to say what an archive actually contains.
CRITICAL_TABLES = (
    "credit_ledger",
    "billing_events",
    "users",
    "audit_log",
    "artifacts",
    "sessions",
    "password_resets",
    "schema_migrations",
)

MANIFEST = "formforge-backup.json"


class BackupError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """What an archive turned out to hold."""

    path: Path
    created_at: str
    backend: str
    revisions: tuple[str, ...]
    rows: dict[str, int]
    formforge_version: str

    @property
    def summary(self) -> str:
        counts = ", ".join(f"{t}={self.rows.get(t, 0)}" for t in CRITICAL_TABLES
                           if t in self.rows)
        return f"{self.created_at} ({self.backend}) {counts}"


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in CRITICAL_TABLES:
        try:
            counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            # A table that does not exist is a fact about the archive, not an
            # error: an older backup predates a migration.
            continue
    return counts


def _revisions(conn: sqlite3.Connection) -> tuple[str, ...]:
    try:
        rows = conn.execute("SELECT id FROM schema_migrations ORDER BY id").fetchall()
    except sqlite3.Error:
        return ()
    return tuple(row[0] for row in rows)


def backup_local(database: Path | str, destination: Path | str) -> BackupInfo:
    """Back up a SQLite accounts database to a tar.gz with a manifest.

    Uses SQLite's own online backup API rather than copying the file. Copying
    a database that is being written produces an archive that restores into a
    corrupt database *sometimes*, which is the worst possible failure mode for
    a backup -- it passes casual inspection and fails when it is needed.
    """
    source = Path(database)
    if not source.exists():
        raise BackupError(f"no database at {source}")
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as work:
        staged = Path(work) / "accounts.db"
        live = sqlite3.connect(str(source))
        try:
            snapshot = sqlite3.connect(str(staged))
            try:
                # Consistent even while the application is writing.
                live.backup(snapshot)
                rows = _table_counts(snapshot)
                revisions = _revisions(snapshot)
            finally:
                snapshot.close()
        finally:
            live.close()

        from . import __version__ as version

        manifest = {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "backend": "sqlite",
            "revisions": list(revisions),
            "rows": rows,
            "formforge_version": version,
        }
        (Path(work) / MANIFEST).write_text(json.dumps(manifest, indent=2))

        with tarfile.open(target, "w:gz") as archive:
            archive.add(staged, arcname="accounts.db")
            archive.add(Path(work) / MANIFEST, arcname=MANIFEST)

    return BackupInfo(
        path=target,
        created_at=manifest["created_at"],
        backend="sqlite",
        revisions=revisions,
        rows=rows,
        formforge_version=version,
    )


def verify_backup(archive: Path | str) -> BackupInfo:
    """Open an archive and check it holds a usable database.

    The step that turns a backup from a hypothesis into a fact. It opens the
    database inside, runs an integrity check, and reads the row counts -- so a
    truncated download or an empty database is found now rather than during an
    incident.
    """
    path = Path(archive)
    if not path.exists():
        raise BackupError(f"no archive at {path}")
    with tempfile.TemporaryDirectory() as work:
        try:
            with tarfile.open(path, "r:gz") as tar:
                names = tar.getnames()
                if "accounts.db" not in names or MANIFEST not in names:
                    raise BackupError(
                        f"{path.name} is not a FormForge backup "
                        "(no accounts.db and manifest)"
                    )
                # Extract only the two members we expect, by name. Extracting
                # a whole archive trusts its member paths, and a tar can name
                # `../`.
                for member in ("accounts.db", MANIFEST):
                    tar.extract(member, path=work, filter="data")
        # A half-downloaded archive surfaces as EOFError from the gzip layer
        # rather than a TarError, and a wrong file as OSError. All three mean
        # the same thing to an operator, so they get the same answer.
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise BackupError(
                f"{path.name} is not a readable archive: {type(exc).__name__}"
            ) from exc

        try:
            manifest = json.loads((Path(work) / MANIFEST).read_text())
        except (OSError, ValueError) as exc:
            raise BackupError(f"{path.name} has an unreadable manifest") from exc

        conn = sqlite3.connect(str(Path(work) / "accounts.db"))
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise BackupError(f"the database inside {path.name} is corrupt")
            rows = _table_counts(conn)
            revisions = _revisions(conn)
        except sqlite3.DatabaseError as exc:
            # `integrity_check` on a file that is not a database raises rather
            # than answering. Same finding, and it must not reach a caller as
            # a driver traceback.
            raise BackupError(
                f"the database inside {path.name} is corrupt: {type(exc).__name__}"
            ) from exc
        finally:
            conn.close()

    return BackupInfo(
        path=path,
        created_at=manifest.get("created_at", "unknown"),
        backend=manifest.get("backend", "sqlite"),
        revisions=revisions,
        rows=rows,
        formforge_version=manifest.get("formforge_version", "unknown"),
    )


def migration_compatibility(info: BackupInfo) -> tuple[bool, str]:
    """Whether this binary can run against that archive.

    Two directions, and only one of them is safe:

    * the archive is *behind* the code -- fine, the missing migrations apply
      on the next startup;
    * the archive is *ahead* -- refuse. The database has columns and
      constraints this binary does not know about, and the restore appears to
      work right up until a query hits one.
    """
    from .accounts.dialect import REVISIONS

    known = set(REVISIONS)
    present = set(info.revisions)
    unknown = sorted(present - known)
    if unknown:
        return False, (
            f"the archive was taken from a newer schema ({', '.join(unknown)}); "
            "this build does not know those revisions"
        )
    outstanding = [r for r in REVISIONS if r not in present]
    if outstanding:
        return True, (
            f"the archive predates {len(outstanding)} revision(s) "
            f"({', '.join(outstanding)}); they will apply on the next start"
        )
    return True, "the archive matches this build's schema"


def restore_local(
    archive: Path | str,
    database: Path | str,
    *,
    confirm: bool,
) -> BackupInfo:
    """Restore a SQLite accounts database from an archive.

    `confirm` has **no default**. Overwriting a database is irreversible and a
    keyword with a default of False is one refactor away from being a default
    of True; requiring the caller to write it means the decision is visible at
    every call site.

    An existing database is moved aside rather than deleted, because the usual
    reason to restore is that something went wrong, and the second-worst
    outcome is discovering the backup was the wrong one after the original is
    gone.
    """
    info = verify_backup(archive)
    compatible, reason = migration_compatibility(info)
    if not compatible:
        raise BackupError(f"refusing to restore: {reason}")

    target = Path(database)
    if target.exists() and not confirm:
        raise BackupError(
            f"{target} already exists. Pass confirm=True (or --confirm) to "
            "replace it; the current file will be moved aside, not deleted."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    displaced: Path | None = None
    if target.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        displaced = target.with_suffix(target.suffix + f".displaced-{stamp}")
        shutil.move(str(target), str(displaced))

    with tempfile.TemporaryDirectory() as work:
        with tarfile.open(Path(archive), "r:gz") as tar:
            tar.extract("accounts.db", path=work, filter="data")
        shutil.move(str(Path(work) / "accounts.db"), str(target))

    if displaced:
        print(f"  the previous database was moved to {displaced}")
    return info


def post_restore_check(database: Path | str) -> dict[str, Any]:
    """What to look at before believing a restore.

    Deliberately reads through the real `AccountStore` rather than raw SQL:
    that runs the migrations, exercises the views, and would surface a schema
    the application cannot actually use -- which a `SELECT count(*)` would not.
    """
    from .accounts import AccountStore

    try:
        store = AccountStore(Path(database))
    except Exception as exc:  # noqa: BLE001 - any open failure is the finding
        raise BackupError(
            f"the restored database will not open: {type(exc).__name__}"
        ) from exc
    try:
        with store._db.reader() as conn:
            users = conn.execute("SELECT count(*) AS n FROM users").fetchone()
            ledger = conn.execute(
                "SELECT count(*) AS n, coalesce(sum(delta), 0) AS total FROM credit_ledger"
            ).fetchone()
            events = conn.execute(
                "SELECT count(*) AS n FROM billing_events"
            ).fetchone()
            # The balance view is the thing the paywall reads. If it errors,
            # the restore is not usable however many rows arrived.
            balances = conn.execute(
                "SELECT count(*) AS n FROM credit_balance"
            ).fetchone()
        return {
            "users": dict(users)["n"],
            "ledger_entries": dict(ledger)["n"],
            "credits_outstanding": dict(ledger)["total"],
            "billing_events": dict(events)["n"],
            "accounts_with_a_balance": dict(balances)["n"],
            "revisions": list(_applied(store)),
        }
    except Exception as exc:  # noqa: BLE001 - both drivers, one answer
        # The tables are there and the application still cannot read them --
        # a missing view, most likely. An operator needs this as a sentence,
        # not a traceback, because it is the answer to "did the restore work".
        raise BackupError(
            f"the restored database is not usable by this build: {exc}"
        ) from exc
    finally:
        store.close()


def _applied(store) -> list[str]:
    with store._db.reader() as conn:
        return [dict(r)["id"] for r in conn.execute(
            "SELECT id FROM schema_migrations ORDER BY id"
        ).fetchall()]


# ---------------------------------------------------------------------------
# PostgreSQL
#
# `pg_dump` and `pg_restore` rather than anything written here. A hand-rolled
# dumper is a second implementation of PostgreSQL's own type system that has to
# be kept correct across every extension and column type the schema ever grows;
# the vendor tools already are. What this module adds is the operator safety
# around them -- the password never reaching an argument vector, and the
# restore refusing to run without a spoken confirmation.
# ---------------------------------------------------------------------------

PG_DUMP_FORMAT = "custom"  # -Fc: what pg_restore reads, and it compresses.


def split_dsn(dsn: str) -> tuple[str, dict[str, str]]:
    """Split a DSN into a safe one and the environment carrying the password.

    A DSN on a command line is readable by every user on the box via `ps`, for
    as long as the dump runs. libpq reads `PGPASSWORD` from the environment
    instead, which is not in the process listing, so the password goes there
    and the argument vector gets a DSN with the password removed.
    """
    if not dsn.startswith(("postgresql://", "postgres://")):
        # A key=value DSN. Pull out the password the same way.
        parts, env = [], {}
        for token in dsn.split():
            if token.startswith("password="):
                env["PGPASSWORD"] = token.split("=", 1)[1]
            else:
                parts.append(token)
        return " ".join(parts), env

    url = urlsplit(dsn)
    if not url.password:
        return dsn, {}
    host = url.hostname or ""
    if url.port:
        host = f"{host}:{url.port}"
    netloc = f"{url.username}@{host}" if url.username else host
    safe = urlunsplit((url.scheme, netloc, url.path, url.query, url.fragment))
    return safe, {"PGPASSWORD": url.password}


def pg_tools_available() -> bool:
    return bool(shutil.which("pg_dump") and shutil.which("pg_restore"))


def _run_pg(command: list[str], dsn: str, *, what: str) -> str:
    safe_dsn, env = split_dsn(dsn)
    environ = {**os.environ, **env}
    try:
        completed = subprocess.run(
            [*command, "--dbname", safe_dsn],
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BackupError(
            f"{command[0]} is not installed. Install the PostgreSQL client "
            "tools (postgresql-client) to run this."
        ) from exc
    if completed.returncode != 0:
        # stderr, not the command: the command carries the DSN, and even the
        # sanitised one names hosts and users nobody needs in a log.
        detail = (completed.stderr or "").strip().splitlines()
        raise BackupError(
            f"{what} failed (exit {completed.returncode}): "
            + (detail[-1] if detail else "no output")
        )
    return completed.stderr


def backup_postgres(dsn: str, destination: Path | str) -> Path:
    """Dump a PostgreSQL accounts database with `pg_dump -Fc`.

    Custom format, so `restore_postgres` can use `pg_restore` and an operator
    can list the archive's contents without restoring it
    (`pg_restore --list`).
    """
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    _run_pg(
        ["pg_dump", f"--format={PG_DUMP_FORMAT}", "--no-owner", "--no-privileges",
         "--file", str(target)],
        dsn,
        what="pg_dump",
    )
    if not target.exists() or target.stat().st_size == 0:
        raise BackupError("pg_dump reported success but produced no output")
    return target


def verify_postgres_dump(archive: Path | str) -> tuple[str, ...]:
    """List what a custom-format dump contains, without restoring it.

    The PostgreSQL equivalent of opening the tarball: it proves the file is a
    readable dump rather than a truncated download, and says which tables are
    in it. Returns the table names found.
    """
    path = Path(archive)
    if not path.exists():
        raise BackupError(f"no archive at {path}")
    try:
        completed = subprocess.run(
            ["pg_restore", "--list", str(path)],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError as exc:
        raise BackupError("pg_restore is not installed") from exc
    if completed.returncode != 0:
        raise BackupError(f"{path.name} is not a readable pg_dump archive")
    # Lines look like `3350; 0 16408 TABLE DATA public users postgres`, and
    # `215; 1259 16408 TABLE public users postgres`. In both the table name is
    # the token before the owner.
    tables = set()
    for line in completed.stdout.splitlines():
        if line.startswith(";") or " TABLE" not in line:
            continue
        tokens = line.split()
        if len(tokens) >= 2:
            tables.add(tokens[-2])
    return tuple(sorted(tables))


def restore_postgres(
    dsn: str,
    archive: Path | str,
    *,
    confirm: bool,
) -> None:
    """Restore a PostgreSQL accounts database. Destructive, and gated.

    `--clean --if-exists` drops what is there before loading, which is the only
    way to restore over an existing schema and also the reason `confirm` has no
    default here either. There is no "move it aside" for a live server, so the
    gate is the whole safety mechanism: an operator has to have written
    `--confirm` on the command line.
    """
    if not confirm:
        raise BackupError(
            "restoring into PostgreSQL drops and recreates the objects in the "
            "dump. Pass confirm=True (or --confirm) to proceed, and take a "
            "`backup create` of the current database first."
        )
    path = Path(archive)
    if not path.exists():
        raise BackupError(f"no archive at {path}")
    _run_pg(
        ["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges",
         "--exit-on-error", str(path)],
        dsn,
        what="pg_restore",
    )
