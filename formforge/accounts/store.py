"""Accounts, sessions and the credit ledger.

Separate from `formforge.store` on purpose, and the reason is a failure policy
rather than tidiness. That module swallows write failures into a counter,
because losing a telemetry row must never break a generation someone is
waiting on. Applying the same rule here would mean a credit deduction that
failed to write became a free model, silently, at scale. **Every write in this
module raises.** Putting the two policies in one class would leave that
distinction as a comment somebody has to remember; putting them in two makes it
structural.

The ledger is append-only. There is no method here that updates or deletes a
`credit_ledger` row, and that is the design, not an omission -- a balance is a
sum over history, so the history has to still be there. A correction is a new
row with the opposite sign.

Two hazards get explicit defences, because both produce *wrong money* rather
than an error anyone would notice:

``the concurrent spend``
    Two builds finishing at once for a user with one credit left. Read the
    balance, both see 1, both debit, the balance is -1 and one model was free.
    Guarded by doing the read and the write inside one ``BEGIN IMMEDIATE``
    transaction, so the second waits for the first and then sees the truth.

``the replayed write``
    A retried request, or a payment processor redelivering a webhook it is not
    sure landed. Guarded by ``idempotency_key``: the second attempt hits the
    unique index and is answered with the row the first one wrote, which is
    what makes a retry safe rather than expensive.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import plans
from .auth import (
    AuthError,
    hash_password,
    hash_token,
    needs_rehash,
    new_session_token,
    normalise_email,
    session_expiry,
    verify_password,
)

DEFAULT_PATH = Path(
    os.environ.get("FORMFORGE_DB", Path.home() / ".formforge" / "formforge.db")
)

# Mirrors the CHECK constraints in docs/schema.sql.
REASONS = frozenset({"grant", "purchase", "spend", "refund", "expiry", "adjustment"})
PLAN_STATUSES = frozenset({"active", "past_due", "cancelled"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                  text PRIMARY KEY,
    email               text UNIQUE NOT NULL,
    password_hash       text,
    plan                text NOT NULL DEFAULT 'free'
                        CHECK (plan IN ('free','maker','studio')),
    plan_status         text NOT NULL DEFAULT 'active'
                        CHECK (plan_status IN ('active','past_due','cancelled')),
    billing_customer_id text UNIQUE,
    period_start        text,
    created_at          text NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash text PRIMARY KEY,
    user_id    text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at text NOT NULL,
    expires_at text NOT NULL,
    revoked_at text
);

CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions (user_id);

CREATE TABLE IF NOT EXISTS credit_ledger (
    id              integer PRIMARY KEY AUTOINCREMENT,
    user_id         text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    delta           integer NOT NULL CHECK (delta <> 0),
    reason          text NOT NULL
                    CHECK (reason IN ('grant','purchase','spend','refund',
                                      'expiry','adjustment')),
    -- No REFERENCES models(id) here, unlike the Postgres schema, and it is not
    -- an oversight. `models` is written by formforge.store, which is a
    -- different object and may not have created its tables in this file yet --
    -- an accounts store that cannot record a charge until the telemetry store
    -- has been constructed would be a startup-order bug waiting to happen. The
    -- property the Postgres FK provides is ON DELETE SET NULL, i.e. the charge
    -- outlives the model; a plain column has that property already.
    model_id        text,
    idempotency_key text UNIQUE,
    note            text,
    created_at      text NOT NULL
);

CREATE INDEX IF NOT EXISTS credit_ledger_user_idx ON credit_ledger (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS credit_ledger_model_idx ON credit_ledger (model_id);

CREATE TABLE IF NOT EXISTS billing_events (
    id          integer PRIMARY KEY AUTOINCREMENT,
    user_id     text,
    provider    text NOT NULL,
    event_id    text NOT NULL,
    event_type  text NOT NULL,
    payload     text NOT NULL DEFAULT '{}',
    handled_at  text,
    received_at text NOT NULL,
    UNIQUE (provider, event_id)
);

CREATE INDEX IF NOT EXISTS billing_events_unhandled_idx ON billing_events (received_at)
    WHERE handled_at IS NULL;
"""

# The balance, defined once per dialect and read from nowhere else. Two
# independent SUMs over the same ledger is how the account page and the paywall
# end up disagreeing, and the customer sees one number and is refused by the
# other.
#
# IF NOT EXISTS rather than the DROP-then-CREATE that `formforge/store.py` uses
# for its views, and the difference is not stylistic. Two of these opening the
# same database at once -- which is what starting a second web worker *is* --
# race between the DROP and the CREATE: one of them fails outright with "view
# already exists", and in the window between the two statements the other's
# balance query has no view to read. A concurrency test caught it on the second
# run. The cost is that changing this definition needs a migration rather than a
# restart, which is true of the Postgres side already.
VIEWS = """
CREATE VIEW IF NOT EXISTS credit_balance AS
SELECT
    u.id                       AS user_id,
    u.email                    AS email,
    u.plan                     AS plan,
    u.plan_status              AS plan_status,
    coalesce(sum(l.delta), 0)  AS balance,
    max(l.created_at)          AS last_movement_at
FROM users u
LEFT JOIN credit_ledger l ON l.user_id = u.id
GROUP BY u.id, u.email, u.plan, u.plan_status;
"""


class AccountError(Exception):
    """Something about an account was refused."""


class DuplicateEmail(AccountError):
    pass


class InsufficientCredits(AccountError):
    """Carries the numbers, so the paywall can say what it actually needs."""

    def __init__(self, balance: int, requested: int):
        self.balance = balance
        self.requested = requested
        super().__init__(f"needs {requested} credit(s), balance is {balance}")


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    id: int
    user_id: str
    delta: int
    reason: str
    model_id: str | None
    idempotency_key: str | None
    note: str | None
    created_at: str
    # False when this call found the work already done under the same
    # idempotency key. The caller usually does not care -- that is the point of
    # idempotency -- but a webhook handler deciding whether to send a receipt
    # very much does.
    applied: bool = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AccountStore:
    """Users, sessions and credits. Every write raises on failure."""

    def __init__(self, path: Path | str | None = None):
        self.path = ":memory:" if path == ":memory:" else Path(path or DEFAULT_PATH)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path if self.path == ":memory:" else str(self.path),
            check_same_thread=False,
            # The busy timeout. With more than one process on the same file,
            # a BEGIN IMMEDIATE that collides waits rather than failing.
            timeout=10.0,
            # Explicit transactions: `BEGIN IMMEDIATE` is what serialises the
            # read-then-write in `spend`, and Python's implicit transaction
            # handling will not issue one.
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.executescript(VIEWS)

    @classmethod
    def memory(cls) -> "AccountStore":
        return cls(":memory:")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "AccountStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """One serialised read-modify-write.

        IMMEDIATE rather than DEFERRED: a deferred transaction takes its write
        lock at the first write, which is *after* the balance has been read,
        which is exactly the window two concurrent spends need to both see the
        same credit. Taking the lock up front closes it.
        """
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
                # A COMMIT can fail -- a full disk, an I/O error, a lock lost
                # under contention -- and SQLite leaves the transaction open
                # when it does. Without this the connection is poisoned: every
                # later BEGIN IMMEDIATE on it fails with "cannot start a
                # transaction within a transaction", so one failed write turns
                # into every subsequent write failing, which reads as a much
                # stranger bug than the disk being full.
                self._rollback()
                raise

    def _rollback(self) -> None:
        # A rollback that itself fails leaves nothing useful to do, and raising
        # here would replace the real failure with this one.
        with suppress(Exception):
            self._conn.execute("ROLLBACK")

    # -- users -------------------------------------------------------------
    def create_user(
        self,
        email: str,
        password: str | None = None,
        *,
        plan: str = plans.DEFAULT_PLAN,
        period_start: str | None = None,
    ) -> dict[str, Any]:
        """Create an account and grant its opening credits.

        The grant happens here rather than on first build so that a new signup
        has a balance immediately -- a paywall that appears before the free
        tier has been handed over reads as broken.
        """
        address = normalise_email(email)
        tier = plans.get(plan)
        user_id = uuid.uuid4().hex
        now = _now()
        stamp = period_start or now
        encoded = hash_password(password) if password is not None else None
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM users WHERE email = ?", (address,)).fetchone():
                raise DuplicateEmail(f"an account already exists for {address}")
            conn.execute(
                """
                INSERT INTO users (id, email, password_hash, plan, plan_status,
                                   billing_customer_id, period_start, created_at)
                VALUES (?,?,?,?,'active',NULL,?,?)
                """,
                (user_id, address, encoded, tier.id, stamp, now),
            )
            if tier.credits:
                self._append(
                    conn,
                    user_id=user_id,
                    delta=tier.credits,
                    reason="grant",
                    idempotency_key=f"grant:{user_id}:{stamp}",
                    note=f"{tier.name} plan, opening balance",
                )
        return self.get_user(user_id)  # type: ignore[return-value]

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        try:
            address = normalise_email(email)
        except AuthError:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE email = ?", (address,)
            ).fetchone()
        return dict(row) if row else None

    def authenticate(self, email: str, password: str) -> dict[str, Any]:
        """Check credentials. Raises AuthError with one message for every kind
        of failure.

        Deliberately not distinguishing "no such account" from "wrong
        password": the difference is an oracle for whether an address has an
        account here, which is worth something to whoever is asking and
        nothing to a legitimate user who mistyped.
        """
        user = self.get_user_by_email(email)
        if user is None or not verify_password(password, user["password_hash"]):
            raise AuthError("email or password is incorrect")
        # The one moment the plaintext exists and the cost factor can be
        # raised on an old account.
        if needs_rehash(user["password_hash"]):
            self.set_password(user["id"], password)
            user = self.get_user(user["id"])  # type: ignore[assignment]
        return user

    def set_password(self, user_id: str, password: str) -> None:
        encoded = hash_password(password)
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?", (encoded, user_id)
            ).rowcount
            if not changed:
                raise AccountError(f"no such user: {user_id}")

    def set_billing_customer(self, user_id: str, customer_id: str) -> None:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE users SET billing_customer_id = ? WHERE id = ?",
                (customer_id, user_id),
            ).rowcount
            if not changed:
                raise AccountError(f"no such user: {user_id}")

    def user_for_billing_customer(self, customer_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE billing_customer_id = ?", (customer_id,)
            ).fetchone()
        return dict(row) if row else None

    # -- sessions ----------------------------------------------------------
    def create_session(self, user_id: str) -> str:
        """Start a session. Returns the bearer token, which is the only time it
        exists in readable form."""
        if self.get_user(user_id) is None:
            raise AccountError(f"no such user: {user_id}")
        token, digest = new_session_token()
        now = _now()
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO sessions (token_hash, user_id, created_at, expires_at, revoked_at)
                VALUES (?,?,?,?,NULL)
                """,
                (digest, user_id, now, session_expiry().isoformat(timespec="seconds")),
            )
        return token

    def user_for_token(self, token: str) -> dict[str, Any] | None:
        """The account a token belongs to, or None if it is unknown, expired or
        revoked. Never raises -- an authentication check that throws on a
        malformed token is a denial-of-service handed to anyone with curl."""
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT u.* FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?
                """,
                (hash_token(token), _now()),
            ).fetchone()
        return dict(row) if row else None

    def revoke_session(self, token: str) -> bool:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE sessions SET revoked_at = ?"
                " WHERE token_hash = ? AND revoked_at IS NULL",
                (_now(), hash_token(token)),
            ).rowcount
        return bool(changed)

    def revoke_all_sessions(self, user_id: str) -> int:
        """Sign a user out everywhere. What a password change should call, and
        what a compromised-account report needs."""
        with self._transaction() as conn:
            return conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (_now(), user_id),
            ).rowcount

    # -- credits -----------------------------------------------------------
    def balance(self, user_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT balance FROM credit_balance WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            raise AccountError(f"no such user: {user_id}")
        return int(row["balance"])

    def _append(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        delta: int,
        reason: str,
        model_id: str | None = None,
        idempotency_key: str | None = None,
        note: str | None = None,
    ) -> LedgerEntry:
        """Write one ledger row inside an open transaction.

        Private because every caller has to have already decided the move is
        allowed; this does not check a balance.
        """
        if delta == 0:
            raise AccountError("a ledger entry that moves nothing is a bug, not a record")
        if reason not in REASONS:
            raise AccountError(f"unknown ledger reason {reason!r}")
        if idempotency_key is not None:
            existing = conn.execute(
                "SELECT * FROM credit_ledger WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                return _entry(existing, applied=False)
        cursor = conn.execute(
            """
            INSERT INTO credit_ledger
                (user_id, delta, reason, model_id, idempotency_key, note, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (user_id, delta, reason, model_id, idempotency_key, note, _now()),
        )
        row = conn.execute(
            "SELECT * FROM credit_ledger WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _entry(row)

    def grant(
        self,
        user_id: str,
        credits: int,
        *,
        reason: str = "grant",
        idempotency_key: str | None = None,
        note: str | None = None,
    ) -> LedgerEntry:
        """Add credits. Positive `credits` only -- taking them away is `spend`
        or an explicit `adjust`, both of which have their own guards."""
        if credits <= 0:
            raise AccountError("grant takes a positive number of credits")
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
                raise AccountError(f"no such user: {user_id}")
            return self._append(
                conn,
                user_id=user_id,
                delta=credits,
                reason=reason,
                idempotency_key=idempotency_key,
                note=note,
            )

    def spend(
        self,
        user_id: str,
        credits: int = 1,
        *,
        model_id: str | None = None,
        idempotency_key: str | None = None,
        note: str | None = None,
    ) -> LedgerEntry:
        """Charge for a generation. Raises InsufficientCredits rather than
        going negative.

        Call this **after** validation passes, never on receipt of the request:
        a build rejected by its preconditions or its DFM checks is not a credit,
        which is the rule the CLI and API already follow for what counts as a
        billable event.

        `idempotency_key` defaults to the model id when one is given, so the
        natural retry -- the same finished build being recorded twice -- is
        free rather than double-charged.
        """
        if credits <= 0:
            raise AccountError("spend takes a positive number of credits")
        if idempotency_key is None and model_id is not None:
            idempotency_key = f"spend:{model_id}"
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT balance FROM credit_balance WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                raise AccountError(f"no such user: {user_id}")
            # Checked inside the transaction, which is the entire point: read
            # it outside and two concurrent spends both see the same credit.
            if idempotency_key is not None:
                already = conn.execute(
                    "SELECT * FROM credit_ledger WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if already is not None:
                    return _entry(already, applied=False)
            available = int(row["balance"])
            if available < credits:
                raise InsufficientCredits(available, credits)
            return self._append(
                conn,
                user_id=user_id,
                delta=-credits,
                reason="spend",
                model_id=model_id,
                idempotency_key=idempotency_key,
                note=note,
            )

    def refund(
        self,
        user_id: str,
        credits: int,
        *,
        model_id: str | None = None,
        note: str | None = None,
    ) -> LedgerEntry:
        """Give a credit back. Its own reason code so that a refund is never
        mistaken for a grant when someone asks where the credits came from."""
        if credits <= 0:
            raise AccountError("refund takes a positive number of credits")
        key = f"refund:{model_id}" if model_id else None
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
                raise AccountError(f"no such user: {user_id}")
            return self._append(
                conn,
                user_id=user_id,
                delta=credits,
                reason="refund",
                model_id=model_id,
                idempotency_key=key,
                note=note,
            )

    def start_period(
        self,
        user_id: str,
        period_start: str,
        *,
        plan_id: str | None = None,
    ) -> list[LedgerEntry]:
        """Roll a user into a new billing period: expire what is left, grant
        the new allowance.

        Credits do not roll over, and this is where that is enforced -- as an
        'expiry' row for the remainder rather than as a reset, so the customer
        who asks what happened to their credits gets an answer with a timestamp
        on it instead of a number that changed.

        Idempotent on `(user_id, period_start)`. A payment processor that
        redelivers a renewal because it never saw our 200 must not be able to
        grant a second month.
        """
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise AccountError(f"no such user: {user_id}")
            tier = plans.get(plan_id or row["plan"])
            written: list[LedgerEntry] = []
            balance = int(
                conn.execute(
                    "SELECT balance FROM credit_balance WHERE user_id = ?", (user_id,)
                ).fetchone()["balance"]
            )
            # Only a positive remainder expires. A negative balance should be
            # impossible -- `spend` refuses to create one -- and if it somehow
            # exists, wiping it here would be forgiving a debt by accident.
            if balance > 0:
                written.append(
                    self._append(
                        conn,
                        user_id=user_id,
                        delta=-balance,
                        reason="expiry",
                        idempotency_key=f"expiry:{user_id}:{period_start}",
                        note=f"unused at the end of the period before {period_start}",
                    )
                )
            if tier.credits:
                written.append(
                    self._append(
                        conn,
                        user_id=user_id,
                        delta=tier.credits,
                        reason="grant",
                        idempotency_key=f"grant:{user_id}:{period_start}",
                        note=f"{tier.name} plan allowance",
                    )
                )
            conn.execute(
                "UPDATE users SET plan = ?, period_start = ? WHERE id = ?",
                (tier.id, period_start, user_id),
            )
            return written

    def set_plan_status(self, user_id: str, status: str) -> None:
        if status not in PLAN_STATUSES:
            raise AccountError(f"unknown plan status {status!r}")
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE users SET plan_status = ? WHERE id = ?", (status, user_id)
            ).rowcount
            if not changed:
                raise AccountError(f"no such user: {user_id}")

    def ledger(self, user_id: str, limit: int = 100) -> list[LedgerEntry]:
        """The history behind a balance, newest first. This is what a support
        conversation reads."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM credit_ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [_entry(r) for r in rows]

    # -- billing events ----------------------------------------------------
    def record_billing_event(
        self,
        provider: str,
        event_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        user_id: str | None = None,
    ) -> tuple[int, bool]:
        """Store a webhook before acting on it. Returns `(row_id, is_new)`.

        `is_new` False means this exact event has been seen before and the
        caller should not act on it again. Recording first and acting second is
        what makes the replay check meaningful: act first and a crash between
        the two turns every redelivery into a second charge.
        """
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM billing_events WHERE provider = ? AND event_id = ?",
                (provider, event_id),
            ).fetchone()
            if existing is not None:
                return int(existing["id"]), False
            cursor = conn.execute(
                """
                INSERT INTO billing_events
                    (user_id, provider, event_id, event_type, payload, handled_at, received_at)
                VALUES (?,?,?,?,?,NULL,?)
                """,
                (
                    user_id,
                    provider,
                    event_id,
                    event_type,
                    json.dumps(payload or {}, default=str),
                    _now(),
                ),
            )
            return int(cursor.lastrowid), True

    def mark_billing_event_handled(self, row_id: int) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE billing_events SET handled_at = ? WHERE id = ?", (_now(), row_id)
            )

    def unhandled_billing_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Events that arrived and were never acted on.

        A non-empty answer here is the signal that someone paid and did not get
        what they paid for, which is the billing failure that no error rate
        catches -- the webhook returned 200, the work never happened.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM billing_events WHERE handled_at IS NULL
                ORDER BY received_at LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            event = dict(row)
            event["payload"] = json.loads(event["payload"] or "{}")
            out.append(event)
        return out


def _entry(row: sqlite3.Row, *, applied: bool = True) -> LedgerEntry:
    return LedgerEntry(
        id=int(row["id"]),
        user_id=row["user_id"],
        delta=int(row["delta"]),
        reason=row["reason"],
        model_id=row["model_id"],
        idempotency_key=row["idempotency_key"],
        note=row["note"],
        created_at=row["created_at"],
        applied=applied,
    )
