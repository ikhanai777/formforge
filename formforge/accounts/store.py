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
    Guarded by taking the user's lock before reading the balance and holding it
    through the write -- `BEGIN IMMEDIATE` on SQLite, `SELECT ... FOR UPDATE`
    on Postgres. See `dialect.py`; the guarantee is identical, the mechanism is
    not.

``the replayed write``
    A retried request, or a payment processor redelivering a webhook it is not
    sure landed. Guarded by ``idempotency_key``: the second attempt hits the
    unique index and is answered with the row the first one wrote, which is
    what makes a retry safe rather than expensive.

The SQL below is written once, with `?` placeholders, and the dialect
translates it. That is what keeps the two backends from drifting: there is only
one implementation of `spend` to be right about.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
from .dialect import Dialect, new_id, open_dialect, to_iso, utcnow

# One value selects the backend: a Postgres DSN or a SQLite path. Two settings
# that can disagree about which database is live is a class of outage.
#
# Resolved through `Settings.from_env()` rather than read here, so there is one
# place that knows what the default is. `from_env` never validates and never
# raises, which matters because this module is imported by the CLI, where a
# production-only requirement must not be a failure to import.


def _default_target() -> str:
    from ..config import Settings

    return Settings.from_env().accounts_db


REASONS = frozenset({"grant", "purchase", "spend", "refund", "expiry", "adjustment"})
PLAN_STATUSES = frozenset({"active", "past_due", "cancelled"})

# Columns holding a moment. Postgres hands these back as datetimes and SQLite
# as text; they are normalised to ISO-8601 seconds on the way out so a caller
# never has to know which engine answered.
log = logging.getLogger("formforge.accounts")

_TIME_COLUMNS = (
    "created_at", "expires_at", "revoked_at", "received_at",
    "handled_at", "last_movement_at", "applied_at", "closed_at",
    "marked_at", "deleted_at",
)


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


def _clean(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    record = dict(row)
    for column in _TIME_COLUMNS:
        if column in record:
            record[column] = to_iso(record[column])
    return record


class AccountStore:
    """Users, sessions and credits. Every write raises on failure."""

    def __init__(self, target: Path | str | None = None, *, dialect: Dialect | None = None):
        self._db = dialect or open_dialect(
            target if target is not None else _default_target()
        )
        self.backend = self._db.name
        # An audit write that fails is counted rather than raised -- see
        # `audit`. This is what a health check reads to notice.
        self.audit_failures = 0
        self._db.migrate()

    @classmethod
    def memory(cls) -> "AccountStore":
        return cls(":memory:")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "AccountStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- plumbing ----------------------------------------------------------
    def _run(self, conn: Any, sql: str, params: tuple = ()) -> Any:
        return conn.execute(self._db.translate(sql), params)

    def _one(self, conn: Any, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        return _clean(self._run(conn, sql, params).fetchone())

    def _all(self, conn: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [_clean(r) for r in self._run(conn, sql, params).fetchall()]  # type: ignore[misc]

    def _read_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._db.reader() as conn:
            return self._one(conn, sql, params)

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
        user_id = new_id()
        now = self._db.now()
        period = period_start or current_period()
        encoded = hash_password(password) if password is not None else None
        with self._db.transaction() as conn:
            if self._one(conn, "SELECT 1 AS hit FROM users WHERE email = ?", (address,)):
                raise DuplicateEmail(f"an account already exists for {address}")
            self._run(
                conn,
                "INSERT INTO users (id, email, password_hash, plan, plan_status,"
                " billing_customer_id, period_start, created_at)"
                " VALUES (?,?,?,?,'active',NULL,?,?)",
                (user_id, address, encoded, tier.id, period, now),
            )
            if tier.credits:
                self._append(
                    conn,
                    user_id=user_id,
                    delta=tier.credits,
                    reason="grant",
                    # `signup:` and not `grant:{user}:{period}`, which is what
                    # `start_period` uses. Sharing that key namespace meant a
                    # user who signed up and subscribed in the same calendar
                    # month collided with their own opening grant, and the
                    # subscription granted *nothing* -- silently, because a
                    # duplicate idempotency key is supposed to be a no-op. The
                    # opening balance and a period's allowance are two
                    # different grants that happen to land in the same month,
                    # so they get two different keys.
                    idempotency_key=f"signup:{user_id}",
                    note=f"{tier.name} plan, opening balance",
                )
        return self.get_user(user_id)  # type: ignore[return-value]

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        return self._read_one("SELECT * FROM users WHERE id = ?", (user_id,))

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        try:
            address = normalise_email(email)
        except AuthError:
            return None
        return self._read_one("SELECT * FROM users WHERE email = ?", (address,))

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
        if self.is_closed(user):
            # The same message as a wrong password, deliberately. "This account
            # is closed" tells somebody probing addresses that one exists.
            raise AuthError("email or password is incorrect")
        # The one moment the plaintext exists and the cost factor can be
        # raised on an old account.
        if needs_rehash(user["password_hash"]):
            self.set_password(user["id"], password)
            user = self.get_user(user["id"])  # type: ignore[assignment]
        return user

    def set_password(self, user_id: str, password: str) -> None:
        encoded = hash_password(password)
        with self._db.transaction() as conn:
            if not self._run(
                conn, "UPDATE users SET password_hash = ? WHERE id = ?", (encoded, user_id)
            ).rowcount:
                raise AccountError(f"no such user: {user_id}")

    def set_billing_customer(self, user_id: str, customer_id: str) -> None:
        with self._db.transaction() as conn:
            if not self._run(
                conn,
                "UPDATE users SET billing_customer_id = ? WHERE id = ?",
                (customer_id, user_id),
            ).rowcount:
                raise AccountError(f"no such user: {user_id}")

    def user_for_billing_customer(self, customer_id: str) -> dict[str, Any] | None:
        return self._read_one(
            "SELECT * FROM users WHERE billing_customer_id = ?", (customer_id,)
        )

    # -- sessions ----------------------------------------------------------
    def create_session(self, user_id: str) -> str:
        """Start a session. Returns the bearer token, which is the only time it
        exists in readable form."""
        if self.get_user(user_id) is None:
            raise AccountError(f"no such user: {user_id}")
        token, digest = new_session_token()
        with self._db.transaction() as conn:
            self._run(
                conn,
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, revoked_at)"
                " VALUES (?,?,?,?,NULL)",
                (digest, user_id, self._db.now(), self._db.stamp(session_expiry())),
            )
        return token

    def user_for_token(self, token: str) -> dict[str, Any] | None:
        """The account a token belongs to, or None if it is unknown, expired or
        revoked. Never raises -- an authentication check that throws on a
        malformed token is a denial-of-service handed to anyone with curl."""
        if not isinstance(token, str) or not token:
            return None
        user = self._read_one(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?",
            (hash_token(token), self._db.now()),
        )
        # Closing revokes every session, so a live token on a closed account
        # should be impossible. Checked anyway: "should be impossible" is how
        # a closed account keeps working, and the cost is one comparison.
        return None if self.is_closed(user) else user

    def revoke_session(self, token: str) -> bool:
        with self._db.transaction() as conn:
            return bool(
                self._run(
                    conn,
                    "UPDATE sessions SET revoked_at = ?"
                    " WHERE token_hash = ? AND revoked_at IS NULL",
                    (self._db.now(), hash_token(token)),
                ).rowcount
            )

    def revoke_all_sessions(self, user_id: str) -> int:
        """Sign a user out everywhere. What a password change should call, and
        what a compromised-account report needs."""
        with self._db.transaction() as conn:
            return self._run(
                conn,
                "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (self._db.now(), user_id),
            ).rowcount

    # -- password reset ----------------------------------------------------
    def create_password_reset(self, user_id: str, *, ttl_minutes: int = 30) -> str:
        """Mint a reset token. Returns it once; only its hash is stored.

        Short-lived on purpose. For as long as it lives the token *is* the
        password, so the window is the exposure: half an hour is enough for
        somebody to read a mail and long enough to be useless to a token found
        in a log tomorrow.

        Any outstanding reset for the same account is invalidated first. Two
        live tokens means a link mailed an hour ago still works after a second
        request, and the usual reason for a second request is that the first
        mail went somewhere the user does not control.
        """
        if self.get_user(user_id) is None:
            raise AccountError(f"no such user: {user_id}")
        token, digest = new_session_token()
        expires = utcnow() + timedelta(minutes=ttl_minutes)
        with self._db.transaction() as conn:
            self._run(
                conn,
                "UPDATE password_resets SET used_at = ?"
                " WHERE user_id = ? AND used_at IS NULL",
                (self._db.now(), user_id),
            )
            self._run(
                conn,
                "INSERT INTO password_resets"
                " (token_hash, user_id, created_at, expires_at, used_at)"
                " VALUES (?,?,?,?,NULL)",
                (digest, user_id, self._db.now(), self._db.stamp(expires)),
            )
        return token

    def consume_password_reset(self, token: str) -> str | None:
        """Redeem a token, returning whose account it was for, or None.

        Single use, and the check and the mark happen inside one transaction:
        two requests arriving together with the same token cannot both come
        back with a user id. Without that, a leaked token could be redeemed by
        the attacker *and* the owner, and only one of them would notice.

        None covers unknown, expired and already-used alike -- the caller
        answers all three the same way, because telling them apart tells
        somebody holding a stolen token which kind of stolen it is.
        """
        if not isinstance(token, str) or not token:
            return None
        digest = hash_token(token)
        with self._db.transaction() as conn:
            row = self._one(
                conn,
                "SELECT user_id FROM password_resets"
                " WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
                (digest, self._db.now()),
            )
            if row is None:
                return None
            self._run(
                conn,
                "UPDATE password_resets SET used_at = ? WHERE token_hash = ?",
                (self._db.now(), digest),
            )
            return str(row["user_id"])

    def purge_password_resets(self, *, before: datetime | None = None) -> int:
        """Delete spent and expired tokens. Safe to run repeatedly.

        The only DELETE in this module. A reset token has no evidentiary value
        once it is dead -- unlike a ledger row, which is why that one is never
        deleted -- and keeping rows that name accounts for no reason is a
        liability rather than an asset.
        """
        cutoff = before or utcnow()
        with self._db.transaction() as conn:
            return self._run(
                conn,
                "DELETE FROM password_resets WHERE used_at IS NOT NULL OR expires_at <= ?",
                (self._db.stamp(cutoff),),
            ).rowcount

    def reset_password(self, user_id: str, password: str) -> int:
        """Set a new password and sign the account out everywhere.

        Revoking every session is the point rather than a side effect. The case
        that matters is an account being recovered *from* somebody: leaving
        their session alive would make the reset theatre. Returns how many
        sessions were killed, which is what an audit entry wants.
        """
        self.set_password(user_id, password)
        return self.revoke_all_sessions(user_id)

    # -- credits -----------------------------------------------------------
    def balance(self, user_id: str) -> int:
        row = self._read_one(
            "SELECT balance FROM credit_balance WHERE user_id = ?", (user_id,)
        )
        if row is None:
            raise AccountError(f"no such user: {user_id}")
        return int(row["balance"])

    def account(self, user_id: str) -> dict[str, Any]:
        """Plan, status and balance in one read, from the one view that
        defines the balance."""
        row = self._read_one(
            "SELECT * FROM credit_balance WHERE user_id = ?", (user_id,)
        )
        if row is None:
            raise AccountError(f"no such user: {user_id}")
        row["balance"] = int(row["balance"])
        return row

    def _append(
        self,
        conn: Any,
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
            existing = self._one(
                conn, "SELECT * FROM credit_ledger WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            if existing is not None:
                return _entry(existing, applied=False)
        row = self._one(
            conn,
            "INSERT INTO credit_ledger"
            " (user_id, delta, reason, model_id, idempotency_key, note, created_at)"
            " VALUES (?,?,?,?,?,?,?) RETURNING *",
            (user_id, delta, reason, model_id, idempotency_key, note, self._db.now()),
        )
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
        or `claw_back`, both of which have their own guards."""
        if credits <= 0:
            raise AccountError("grant takes a positive number of credits")
        with self._db.transaction() as conn:
            self._db.lock_user(conn, user_id)
            if not self._one(conn, "SELECT 1 AS hit FROM users WHERE id = ?", (user_id,)):
                raise AccountError(f"no such user: {user_id}")
            return self._append(
                conn, user_id=user_id, delta=credits, reason=reason,
                idempotency_key=idempotency_key, note=note,
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
        with self._db.transaction() as conn:
            # Before the balance is read, not after: the window between reading
            # and writing is exactly what two concurrent spends need to both
            # see the same last credit.
            self._db.lock_user(conn, user_id)
            row = self._one(
                conn, "SELECT balance FROM credit_balance WHERE user_id = ?", (user_id,)
            )
            if row is None:
                raise AccountError(f"no such user: {user_id}")
            if idempotency_key is not None:
                already = self._one(
                    conn, "SELECT * FROM credit_ledger WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
                if already is not None:
                    return _entry(already, applied=False)
            available = int(row["balance"])
            if available < credits:
                raise InsufficientCredits(available, credits)
            return self._append(
                conn, user_id=user_id, delta=-credits, reason="spend",
                model_id=model_id, idempotency_key=idempotency_key, note=note,
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
        with self._db.transaction() as conn:
            self._db.lock_user(conn, user_id)
            if not self._one(conn, "SELECT 1 AS hit FROM users WHERE id = ?", (user_id,)):
                raise AccountError(f"no such user: {user_id}")
            return self._append(
                conn, user_id=user_id, delta=credits, reason="refund",
                model_id=model_id,
                idempotency_key=f"refund:{model_id}" if model_id else None,
                note=note,
            )

    def claw_back(
        self,
        user_id: str,
        credits: int,
        *,
        idempotency_key: str,
        note: str | None = None,
    ) -> LedgerEntry | None:
        """Reverse a grant, taking back only what is still unspent.

        The policy, decided deliberately: a payment reversal takes back at most
        the balance the user still has, never more. Someone who was granted 60,
        spent 40 and then had the payment refunded loses the 20 that are left.

        The alternative -- reversing the full 60 into a -40 debt -- was
        rejected. It locks the account behind a balance no ordinary action can
        clear, and it needs a deliberate exception to the rule that nothing in
        this module may create a negative balance. That rule is load-bearing:
        every other guard here assumes a balance is a number of credits
        somebody can actually spend.

        Returns None when there was nothing left to take, which is a real
        outcome and not a failure -- the refund still gets its billing_events
        row either way.
        """
        if credits <= 0:
            raise AccountError("claw_back takes a positive number of credits")
        with self._db.transaction() as conn:
            self._db.lock_user(conn, user_id)
            row = self._one(
                conn, "SELECT balance FROM credit_balance WHERE user_id = ?", (user_id,)
            )
            if row is None:
                raise AccountError(f"no such user: {user_id}")
            existing = self._one(
                conn, "SELECT * FROM credit_ledger WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            if existing is not None:
                return _entry(existing, applied=False)
            take = min(credits, max(int(row["balance"]), 0))
            if take <= 0:
                return None
            return self._append(
                conn, user_id=user_id, delta=-take, reason="adjustment",
                idempotency_key=idempotency_key,
                note=note or f"reversal, {take} of {credits} recoverable",
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
        with self._db.transaction() as conn:
            self._db.lock_user(conn, user_id)
            user = self._one(conn, "SELECT * FROM users WHERE id = ?", (user_id,))
            if user is None:
                raise AccountError(f"no such user: {user_id}")
            tier = plans.get(plan_id or user["plan"])
            written: list[LedgerEntry] = []
            balance = int(
                self._one(
                    conn, "SELECT balance FROM credit_balance WHERE user_id = ?", (user_id,)
                )["balance"]
            )
            # Only a positive remainder expires. A negative balance should be
            # impossible -- `spend` refuses to create one -- and if one somehow
            # exists, wiping it here would be forgiving a debt by accident.
            if balance > 0:
                written.append(
                    self._append(
                        conn, user_id=user_id, delta=-balance, reason="expiry",
                        idempotency_key=f"expiry:{user_id}:{period_start}",
                        note=f"unused at the end of the period before {period_start}",
                    )
                )
            if tier.credits:
                written.append(
                    self._append(
                        conn, user_id=user_id, delta=tier.credits, reason="grant",
                        idempotency_key=f"grant:{user_id}:{period_start}",
                        note=f"{tier.name} plan allowance",
                    )
                )
            self._run(
                conn, "UPDATE users SET plan = ?, period_start = ? WHERE id = ?",
                (tier.id, period_start, user_id),
            )
            return written

    def roll_to_current_period(self, user_id: str) -> list[LedgerEntry]:
        """Bring a free account into the current month if it is behind.

        Paid plans are rolled by the processor's renewal webhook. Nothing bills
        a free account, so nothing would ever renew it -- without this, a free
        user gets three credits once and never again. Doing it lazily on access
        rather than from a scheduled job means there is no clock to keep
        running, and `start_period` is idempotent on the period, so two
        requests arriving together cannot grant twice.
        """
        user = self.get_user(user_id)
        if user is None:
            raise AccountError(f"no such user: {user_id}")
        period = current_period()
        if user["plan"] != "free" or user["period_start"] == period:
            return []
        return self.start_period(user_id, period, plan_id="free")

    def set_plan(self, user_id: str, plan_id: str) -> None:
        """Move an account between plans without touching its balance.

        Separate from `start_period`, which grants. Used when a subscription
        ends: the plan reverts to free, and whatever is left of the paid month
        expires the ordinary way at the next roll rather than being confiscated
        at the moment the subscription lapses.
        """
        tier = plans.get(plan_id)
        with self._db.transaction() as conn:
            if not self._run(
                conn, "UPDATE users SET plan = ? WHERE id = ?", (tier.id, user_id)
            ).rowcount:
                raise AccountError(f"no such user: {user_id}")

    def has_newer_status_event(
        self,
        user_id: str,
        moment: Any,
        *,
        provider: str,
        exclude_id: str | None = None,
    ) -> bool:
        """Whether a later status-changing event has already been applied.

        Webhooks arrive out of order. A balance movement does not care -- each
        is idempotent on its own key -- but an account *status* is
        last-write-wins, so a cancellation delivered after the renewal that
        followed it would leave the account cancelled while it is paid up.

        An event with no timestamp is treated as *not* stale: refusing to act
        on an event because the processor told us nothing about when it
        happened would mean dropping it, and a dropped status change is worse
        than a possibly-reordered one.
        """
        if moment is None:
            return False
        row = self._read_one(
            "SELECT count(*) AS newer FROM billing_events"
            " WHERE user_id = ? AND provider = ? AND event_created > ?"
            " AND event_id <> ? AND handled_at IS NOT NULL",
            (user_id, provider, self._db.stamp(moment), exclude_id or ""),
        )
        return bool(row and int(row["newer"]))

    def set_plan_status(self, user_id: str, status: str) -> None:
        if status not in PLAN_STATUSES:
            raise AccountError(f"unknown plan status {status!r}")
        with self._db.transaction() as conn:
            if not self._run(
                conn, "UPDATE users SET plan_status = ? WHERE id = ?", (status, user_id)
            ).rowcount:
                raise AccountError(f"no such user: {user_id}")

    def ledger(self, user_id: str, limit: int = 100) -> list[LedgerEntry]:
        """The history behind a balance, newest first. This is what a support
        conversation reads."""
        with self._db.reader() as conn:
            rows = self._all(
                conn,
                "SELECT * FROM credit_ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            )
        return [_entry(r) for r in rows]

    def spend_for_model(self, model_id: str) -> LedgerEntry | None:
        """The charge for a model, if it has been paid for.

        This is the entitlement check behind every paid download: a model is
        downloadable because somebody was charged for it, not because a flag
        was set somewhere.
        """
        row = self._read_one(
            "SELECT * FROM credit_ledger WHERE idempotency_key = ?", (f"spend:{model_id}",)
        )
        return _entry(row) if row else None

    # -- audit -------------------------------------------------------------
    def audit(
        self,
        action: str,
        *,
        user_id: str | None = None,
        actor: str = "system",
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Record something that happened to an account.

        `billing_events` records what the processor said; this records what
        *we* did -- a password changed, every session revoked, a plan moved by
        an operator, credits adjusted by hand, an account closed. Those are the
        things somebody asks about afterwards and none of them was written
        down anywhere before.

        **Never carries a secret.** `detail` is for "how many sessions" and
        "which plan", not for what the password became. The caller decides
        what goes in, so this is a convention rather than an enforcement, and
        it is stated here because that is where somebody adding a field will
        look.

        Unlike every other write in this module, an audit failure is swallowed.
        The reasoning is the same one that governs telemetry: refusing a
        password reset because the audit row would not write is a worse outcome
        than an incomplete audit trail. It is counted, not silent.
        """
        try:
            with self._db.transaction() as conn:
                self._run(
                    conn,
                    "INSERT INTO audit_log (user_id, action, actor, detail, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (user_id, action, actor, self._db.dumps(detail or {}), self._db.now()),
                )
        except Exception:
            self.audit_failures += 1
            log.warning("could not write an audit row for %s", action, exc_info=True)

    def audit_trail(self, user_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._db.reader() as conn:
            rows = self._all(
                conn,
                "SELECT * FROM audit_log WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            )
        for row in rows:
            row["detail"] = self._db.loads(row["detail"])
        return rows

    # -- account lifecycle -------------------------------------------------
    def close_account(self, user_id: str, *, actor: str = "user") -> int:
        """Close an account: refuse logins, revoke sessions, keep the ledger.

        Soft by decision. The credit ledger is a financial record and outlives
        the account; the row stays so a later question about a charge has an
        answer. Hard deletion is a separate, explicit operator action --
        `purge_account` -- because reversible beats tidy and a closure is
        usually a mistake or a change of mind rather than a demand.

        Returns how many sessions were revoked.
        """
        if self.get_user(user_id) is None:
            raise AccountError(f"no such user: {user_id}")
        with self._db.transaction() as conn:
            self._run(
                conn, "UPDATE users SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
                (self._db.now(), user_id),
            )
        killed = self.revoke_all_sessions(user_id)
        self.audit("account.closed", user_id=user_id, actor=actor,
                   detail={"sessions_revoked": killed})
        return killed

    def reopen_account(self, user_id: str, *, actor: str = "operator") -> None:
        """Undo a closure. The reason closure is soft."""
        with self._db.transaction() as conn:
            if not self._run(
                conn, "UPDATE users SET closed_at = NULL WHERE id = ?", (user_id,)
            ).rowcount:
                raise AccountError(f"no such user: {user_id}")
        self.audit("account.reopened", user_id=user_id, actor=actor)

    def is_closed(self, user: dict[str, Any] | None) -> bool:
        return bool(user and user.get("closed_at"))

    # -- artifacts ---------------------------------------------------------
    def record_artifact(
        self,
        model_id: str,
        fmt: str,
        storage_key: str,
        *,
        user_id: str | None = None,
        size: int = 0,
    ) -> None:
        """Note that a file exists. Idempotent on `(model_id, fmt)`.

        Re-recording the same artifact -- a rebuild, a retried write -- updates
        the row rather than adding a second one, and revives a row that had
        been marked for deletion: the bytes are back, so the mark is wrong.
        """
        with self._db.transaction() as conn:
            existing = self._one(
                conn,
                "SELECT id FROM artifacts WHERE model_id = ? AND fmt = ?",
                (model_id, fmt),
            )
            if existing:
                self._run(
                    conn,
                    "UPDATE artifacts SET storage_key = ?, bytes = ?, status = 'present',"
                    " marked_at = NULL, deleted_at = NULL WHERE id = ?",
                    (storage_key, int(size), existing["id"]),
                )
                return
            self._run(
                conn,
                "INSERT INTO artifacts"
                " (model_id, user_id, fmt, storage_key, bytes, status, created_at)"
                " VALUES (?,?,?,?,?,'present',?)",
                (model_id, user_id, fmt, storage_key, int(size), self._db.now()),
            )

    def artifacts_for(self, model_id: str) -> list[dict[str, Any]]:
        with self._db.reader() as conn:
            return self._all(
                conn, "SELECT * FROM artifacts WHERE model_id = ? ORDER BY fmt", (model_id,)
            )

    def mark_artifacts(self, model_id: str, *, actor: str = "operator") -> int:
        """The soft step: mark for deletion without removing anything.

        Two steps rather than one because deletion of a customer's file is
        irreversible and a retention sweep should be reviewable before it
        bites. Marking is what a sweep does; removing is a second, deliberate
        pass.
        """
        with self._db.transaction() as conn:
            changed = self._run(
                conn,
                "UPDATE artifacts SET status = 'pending_delete', marked_at = ?"
                " WHERE model_id = ? AND status = 'present'",
                (self._db.now(), model_id),
            ).rowcount
        if changed:
            self.audit("artifact.marked", actor=actor,
                       detail={"model_id": model_id, "count": changed})
        return changed

    def finish_artifact_delete(self, artifact_id: int) -> None:
        """Record that the bytes are gone. The row stays.

        Deliberately keeping the row: it is the record that the file existed
        and when it went, which is what answers "where did my model go".
        """
        with self._db.transaction() as conn:
            self._run(
                conn,
                "UPDATE artifacts SET status = 'deleted', deleted_at = ? WHERE id = ?",
                (self._db.now(), artifact_id),
            )

    def artifacts_pending_delete(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._db.reader() as conn:
            return self._all(
                conn,
                "SELECT * FROM artifacts WHERE status = 'pending_delete'"
                " ORDER BY marked_at LIMIT ?",
                (limit,),
            )

    def artifacts_older_than(
        self, days: int, *, only_unpaid: bool = True, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Candidates for retention. Never returns anything on `days <= 0`.

        `days <= 0` means retention is off, and off has to mean *nothing*
        rather than everything -- a sweep that treats a missing setting as "age
        zero" deletes the lot.

        `only_unpaid` skips models somebody was charged for. Deleting what a
        customer paid for on a timer is not a retention policy, it is a
        refund request.
        """
        if days <= 0:
            return []
        cutoff = self._db.stamp(utcnow() - timedelta(days=days))
        sql = (
            "SELECT a.* FROM artifacts a WHERE a.status = 'present' AND a.created_at < ?"
        )
        if only_unpaid:
            sql += (
                " AND NOT EXISTS (SELECT 1 FROM credit_ledger l"
                " WHERE l.model_id = a.model_id AND l.reason = 'spend')"
            )
        sql += " ORDER BY a.created_at LIMIT ?"
        with self._db.reader() as conn:
            return self._all(conn, sql, (cutoff, limit))

    # -- billing events ----------------------------------------------------
    def record_billing_event(
        self,
        provider: str,
        event_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        user_id: str | None = None,
        event_created: Any = None,
    ) -> tuple[int, bool]:
        """Store a webhook before acting on it. Returns `(row_id, is_new)`.

        `is_new` False means this exact event has been seen before and the
        caller should not act on it again. Recording first and acting second is
        what makes the replay check meaningful: act first and a crash between
        the two turns every redelivery into a second charge.
        """
        with self._db.transaction() as conn:
            existing = self._one(
                conn,
                "SELECT id FROM billing_events WHERE provider = ? AND event_id = ?",
                (provider, event_id),
            )
            if existing is not None:
                return int(existing["id"]), False
            row = self._one(
                conn,
                "INSERT INTO billing_events"
                " (user_id, provider, event_id, event_type, payload, handled_at,"
                "  event_created, received_at)"
                " VALUES (?,?,?,?,?,NULL,?,?) RETURNING id",
                (
                    user_id, provider, event_id, event_type,
                    self._db.dumps(payload or {}),
                    self._db.stamp(event_created) if event_created else None,
                    self._db.now(),
                ),
            )
            return int(row["id"]), True

    def mark_billing_event_handled(self, row_id: int) -> None:
        with self._db.transaction() as conn:
            self._run(
                conn, "UPDATE billing_events SET handled_at = ? WHERE id = ?",
                (self._db.now(), row_id),
            )

    def unhandled_billing_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Events that arrived and were never acted on.

        A non-empty answer here is the signal that someone paid and did not get
        what they paid for, which is the billing failure that no error rate
        catches -- the webhook returned 200, the work never happened.
        """
        with self._db.reader() as conn:
            rows = self._all(
                conn,
                "SELECT * FROM billing_events WHERE handled_at IS NULL"
                " ORDER BY received_at LIMIT ?",
                (limit,),
            )
        for row in rows:
            row["payload"] = self._db.loads(row["payload"])
        return rows


def current_period() -> str:
    """The label for the month now in progress.

    A label, not an instant. Its whole job is to be half of an idempotency key,
    so what matters is that two calls in the same month agree.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _entry(row: dict[str, Any], *, applied: bool = True) -> LedgerEntry:
    return LedgerEntry(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        delta=int(row["delta"]),
        reason=row["reason"],
        model_id=row["model_id"],
        idempotency_key=row["idempotency_key"],
        note=row["note"],
        created_at=to_iso(row["created_at"]),
        applied=applied,
    )
