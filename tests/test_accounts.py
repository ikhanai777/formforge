"""Accounts, credits and billing (`docs/monetization-site-spec.md` Phase 0).

What is worth testing here is not that a row round-trips. It is the handful of
places where the system can be *wrong about money* while looking like it
worked: a credit spent twice, a credit spent that was not there, a webhook
delivered twice granting two months, an expiry that quietly eats a balance
without saying so. Each of those produces no error and no alert -- the only
thing that catches them is a test that sets up the race on purpose.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest

from formforge.accounts import (
    AccountError,
    AccountStore,
    DuplicateEmail,
    InsufficientCredits,
    OfflineProvider,
    apply_event,
    plans,
)
from formforge.accounts.auth import (
    AuthError,
    hash_password,
    needs_rehash,
    normalise_email,
    verify_password,
)
from formforge.accounts.billing import BillingError, SignatureError

PASSWORD = "correct-horse-battery-staple"

# Lines inside a CREATE TABLE that declare something other than a column.
IGNORED = {"check", "foreign", "primary", "constraint", "unique"}


def raw(store: AccountStore, sql: str, params: tuple = ()):
    """Reach past the store's API to set up a state it will not create.

    Written through the dialect rather than a raw connection so these tests
    run unchanged on both backends -- which is the whole point of the fixture
    being parametrised.
    """
    with store._db.transaction() as conn:
        cursor = conn.execute(store._db.translate(sql), params)
        try:
            return [dict(r) for r in cursor.fetchall()]
        except Exception:
            return []


@pytest.fixture
def user(accounts: AccountStore) -> dict:
    return accounts.create_user("maker@example.com", PASSWORD)


def _provider_and_customer(accounts: AccountStore, user: dict):
    provider = OfflineProvider(secret="test-secret")
    customer = provider.create_customer(user["id"], user["email"])
    accounts.set_billing_customer(user["id"], customer)
    return provider, customer


def _webhook(provider: OfflineProvider, payload: dict):
    """Round-trip an event through signing and verification, the way a real
    delivery arrives -- never by constructing a BillingEvent directly, which
    would skip the boundary this is supposed to exercise."""
    body = json.dumps(payload).encode()
    return provider.verify_webhook(body, provider.sign(body))


class TestCreditsCannotGoWrong:
    def test_the_last_credit_is_only_spent_once_under_concurrency(self, accounts_target):
        """The race that gives models away.

        Eight threads, each on its own connection, all trying to spend against
        four credits. Read the balance outside a transaction and every one of
        them sees four and proceeds; the balance ends negative and the
        difference was free. Exactly four may win.
        """
        db = accounts_target
        with AccountStore(db) as setup:
            account = setup.create_user("race@example.com", PASSWORD)
            user_id = account["id"]
            setup.grant(user_id, 4 - setup.balance(user_id), note="level to four")
            assert setup.balance(user_id) == 4

        attempts = 8
        start = threading.Barrier(attempts)
        won: list[int] = []
        refused: list[int] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def attempt(n: int) -> None:
            # A separate store per thread means a separate connection, so the
            # only thing serialising these is the database. An in-process lock
            # would make this test pass while a second web worker still lost
            # money.
            try:
                with AccountStore(db) as store:
                    start.wait(timeout=10)
                    try:
                        # Distinct keys on purpose: idempotency must not be
                        # what saves us here, or the test proves the wrong
                        # thing.
                        store.spend(user_id, idempotency_key=f"race-{n}")
                    except InsufficientCredits:
                        with lock:
                            refused.append(n)
                    else:
                        with lock:
                            won.append(n)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(attempts)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, f"unexpected failures: {errors!r}"
        with AccountStore(db) as store:
            final = store.balance(user_id)
        assert len(won) == 4, f"{len(won)} spends won against four credits"
        assert len(refused) == 4
        assert final == 0, f"balance ended at {final}"

    def test_a_balance_never_goes_negative(self, accounts, user):
        for n in range(3):
            accounts.spend(user["id"], model_id=f"m{n}")
        with pytest.raises(InsufficientCredits) as caught:
            accounts.spend(user["id"], model_id="m3")
        assert caught.value.balance == 0
        assert caught.value.requested == 1
        assert accounts.balance(user["id"]) == 0

    def test_recording_the_same_build_twice_charges_once(self, accounts, user):
        """The ordinary retry: the same finished generation recorded again."""
        first = accounts.spend(user["id"], model_id="model-1")
        second = accounts.spend(user["id"], model_id="model-1")
        assert first.applied and not second.applied
        assert first.id == second.id
        assert accounts.balance(user["id"]) == 2

    def test_spending_more_than_one_credit_is_all_or_nothing(self, accounts, user):
        """A batch that cannot be afforded must not half-charge for itself."""
        with pytest.raises(InsufficientCredits):
            accounts.spend(user["id"], 5, idempotency_key="batch-1")
        assert accounts.balance(user["id"]) == 3
        assert accounts.ledger(user["id"])[0].reason == "grant"

    def test_the_ledger_is_the_balance(self, accounts, user):
        accounts.spend(user["id"], model_id="m1")
        accounts.refund(user["id"], 1, model_id="m1", note="build was not delivered")
        entries = accounts.ledger(user["id"])
        assert [e.reason for e in entries] == ["refund", "spend", "grant"]
        assert sum(e.delta for e in entries) == accounts.balance(user["id"]) == 3

    @pytest.mark.parametrize("credits", [0, -1])
    def test_a_move_of_nothing_is_refused(self, accounts, user, credits):
        """A zero-delta row would sit in the history looking deliberate."""
        with pytest.raises(AccountError):
            accounts.grant(user["id"], credits)
        with pytest.raises(AccountError):
            accounts.spend(user["id"], credits)


class TestPeriods:
    def test_unused_credits_expire_visibly_rather_than_being_reset(self, accounts, user):
        """No rollover is a promise to the customer *and* a thing they will
        ask about. It has to be a row with a timestamp, not a number that
        changed."""
        accounts.spend(user["id"], model_id="m1")
        accounts.start_period(user["id"], "2026-10", plan_id="maker")
        entries = accounts.ledger(user["id"])
        assert [e.reason for e in entries[:2]] == ["grant", "expiry"]
        assert entries[1].delta == -2, "the two unused free credits should expire"
        assert accounts.balance(user["id"]) == plans.get("maker").credits

    def test_a_redelivered_renewal_does_not_grant_twice(self, accounts, user):
        accounts.start_period(user["id"], "2026-10", plan_id="maker")
        before = accounts.balance(user["id"])
        accounts.start_period(user["id"], "2026-10", plan_id="maker")
        assert accounts.balance(user["id"]) == before

    def test_a_negative_balance_is_never_forgiven_by_a_rollover(self, accounts, user):
        """`spend` cannot create a debt, so this forces one the only way left:
        writing the row directly.

        The guard being tested is the `balance > 0` on the expiry. Without it,
        a negative balance expires as a *positive* delta -- arithmetically
        consistent, and it silently writes off whatever was owed. That is worth
        a test precisely because the condition should be unreachable: if it
        ever is reached, the rollover must not be what quietly cleans it up.
        """
        raw(
            accounts,
            "INSERT INTO credit_ledger (user_id, delta, reason, created_at)"
            " VALUES (?, -10, 'adjustment', ?)",
            (user["id"], accounts._db.now()),
        )
        assert accounts.balance(user["id"]) == -7

        accounts.start_period(user["id"], "2026-10", plan_id="free")

        expiries = [e for e in accounts.ledger(user["id"]) if e.reason == "expiry"]
        assert not expiries, "a debt must not be expired away"
        # The new period's grant lands on top of the debt rather than clearing
        # it: -7 owed, 3 granted.
        assert accounts.balance(user["id"]) == -4


class TestBilling:
    def test_a_forged_webhook_is_refused(self, accounts, user):
        provider, customer = _provider_and_customer(accounts, user)
        body = json.dumps(
            {
                "type": "credits.purchased",
                "id": "e1",
                "customer_id": customer,
                "credits": 10_000,
            }
        ).encode()
        with pytest.raises(SignatureError):
            provider.verify_webhook(body, "not-the-signature")
        with pytest.raises(SignatureError):
            provider.verify_webhook(body, "")
        assert accounts.balance(user["id"]) == 3

    def test_a_replayed_delivery_is_applied_once(self, accounts, user):
        """The processor redelivers whenever it is unsure we got it. Twice
        applied is two months of credits for one payment."""
        provider, customer = _provider_and_customer(accounts, user)
        event = _webhook(
            provider,
            {
                "type": "subscription.activated",
                "id": "evt_1",
                "customer_id": customer,
                "plan_id": "maker",
                "period_start": "2026-09",
            },
        )
        assert apply_event(accounts, event) is True
        assert accounts.balance(user["id"]) == 60
        assert apply_event(accounts, event) is False
        assert accounts.balance(user["id"]) == 60

    def test_the_ledger_key_stops_a_double_grant_on_its_own(self, accounts, user):
        """The two defences are independent, and this is the second one.

        `billing_events` catches a redelivery first. If that row is ever gone
        -- a retention job, a restore from a backup taken before it -- the
        event looks new and gets applied again. The ledger's own idempotency
        key is what holds then, so it is worth knowing it holds *alone*:
        the event row is deleted here to take the first defence away.
        """
        provider, customer = _provider_and_customer(accounts, user)
        event = _webhook(
            provider,
            {
                "type": "credits.purchased",
                "id": "evt_a",
                "customer_id": customer,
                "credits": 20,
            },
        )
        assert apply_event(accounts, event) is True
        assert accounts.balance(user["id"]) == 23

        raw(accounts, "DELETE FROM billing_events WHERE event_id = 'evt_a'")
        assert apply_event(accounts, event) is True, "the event should look new again"
        assert accounts.balance(user["id"]) == 23, "but the credits must not be granted twice"

    def test_two_genuinely_different_purchases_both_land(self, accounts, user):
        """The mirror of the test above: idempotency must not be so eager that
        a customer who buys twice is only charged for -- and credited with --
        one of them."""
        provider, customer = _provider_and_customer(accounts, user)
        for event_id in ("evt_a", "evt_b"):
            apply_event(
                accounts,
                _webhook(
                    provider,
                    {
                        "type": "credits.purchased",
                        "id": event_id,
                        "customer_id": customer,
                        "credits": 20,
                    },
                ),
            )
        assert accounts.balance(user["id"]) == 43

    def test_a_failed_payment_marks_the_account_without_stripping_credits(
        self, accounts, user
    ):
        """An expired card is usually about to be fixed. Deleting the balance
        turns a recoverable problem into a cancelled customer."""
        provider, customer = _provider_and_customer(accounts, user)
        apply_event(
            accounts,
            _webhook(
                provider,
                {
                    "type": "subscription.activated",
                    "id": "evt_1",
                    "customer_id": customer,
                    "plan_id": "maker",
                    "period_start": "2026-09",
                },
            ),
        )
        apply_event(
            accounts,
            _webhook(
                provider,
                {"type": "payment.failed", "id": "evt_2", "customer_id": customer},
            ),
        )
        assert accounts.get_user(user["id"])["plan_status"] == "past_due"
        assert accounts.balance(user["id"]) == 60

    def test_cancelling_leaves_the_credits_already_paid_for(self, accounts, user):
        provider, customer = _provider_and_customer(accounts, user)
        apply_event(
            accounts,
            _webhook(
                provider,
                {
                    "type": "subscription.activated",
                    "id": "evt_1",
                    "customer_id": customer,
                    "plan_id": "maker",
                    "period_start": "2026-09",
                },
            ),
        )
        apply_event(
            accounts,
            _webhook(
                provider,
                {"type": "subscription.cancelled", "id": "evt_2", "customer_id": customer},
            ),
        )
        assert accounts.get_user(user["id"])["plan_status"] == "cancelled"
        assert accounts.balance(user["id"]) == 60

    def test_an_event_for_an_unknown_customer_is_kept_for_someone_to_look_at(
        self, accounts, user
    ):
        """A payment we cannot attribute is a real problem -- a half-finished
        signup, or two environments sharing one processor account. It belongs
        in a queue a human reads, not in a log line."""
        provider = OfflineProvider(secret="test-secret")
        event = _webhook(
            provider,
            {
                "type": "credits.purchased",
                "id": "evt_x",
                "customer_id": "cus_nobody",
                "credits": 5,
            },
        )
        with pytest.raises(BillingError):
            apply_event(accounts, event)
        pending = accounts.unhandled_billing_events()
        assert [e["event_id"] for e in pending] == ["evt_x"]

    def test_an_unknown_event_type_is_refused_rather_than_ignored(self, accounts, user):
        """Silently dropping an event type nobody handles is how a payment
        goes missing."""
        provider, customer = _provider_and_customer(accounts, user)
        body = json.dumps({"type": "subscription.upgraded", "customer_id": customer}).encode()
        with pytest.raises(BillingError):
            provider.verify_webhook(body, provider.sign(body))

    def test_offline_events_are_marked_as_such(self, accounts, user):
        provider, customer = _provider_and_customer(accounts, user)
        event = _webhook(
            provider, {"type": "payment.failed", "id": "e", "customer_id": customer}
        )
        assert event.offline is True, "a simulated payment must never look real"


class TestAuth:
    def test_a_password_is_never_stored_in_the_clear(self, accounts, user):
        stored = accounts.get_user(user["id"])["password_hash"]
        assert PASSWORD not in stored
        assert stored.startswith("scrypt$")
        assert verify_password(PASSWORD, stored)
        assert not verify_password("wrong-" + PASSWORD, stored)

    def test_the_same_password_hashes_differently_for_two_users(self, accounts, user):
        """Per-password salt: without it, equal hashes tell an attacker with a
        database dump which accounts to try first."""
        other = accounts.create_user("second@example.com", PASSWORD)
        assert (
            accounts.get_user(user["id"])["password_hash"]
            != accounts.get_user(other["id"])["password_hash"]
        )

    def test_an_account_with_no_password_cannot_be_logged_into(self, accounts):
        accounts.create_user("oauth@example.com", None)
        assert not verify_password("", None)
        with pytest.raises(AuthError):
            accounts.authenticate("oauth@example.com", "")

    def test_a_wrong_password_and_a_missing_account_look_the_same(self, accounts, user):
        """Different messages here are an oracle for which addresses have
        accounts."""
        with pytest.raises(AuthError) as wrong:
            accounts.authenticate("maker@example.com", "not-the-password")
        with pytest.raises(AuthError) as missing:
            accounts.authenticate("nobody@example.com", "not-the-password")
        assert str(wrong.value) == str(missing.value)

    def test_a_short_password_is_refused_at_the_point_it_is_set(self, accounts):
        with pytest.raises(AuthError):
            hash_password("short")

    def test_a_stored_hash_that_cannot_be_parsed_fails_closed(self):
        for junk in ("", "not-a-hash", "scrypt$bad", "argon2$1$2$3$4$5"):
            assert not verify_password(PASSWORD, junk)

    def test_raising_the_work_factor_is_detected_on_old_hashes(self):
        assert needs_rehash("scrypt$1024$8$1$aabb$ccdd")
        assert not needs_rehash(hash_password(PASSWORD))

    @pytest.mark.parametrize(
        "given,expected",
        [
            ("Maker@Example.COM", "maker@example.com"),
            ("  maker@example.com  ", "maker@example.com"),
        ],
    )
    def test_addresses_are_normalised_the_way_citext_would(self, given, expected):
        """SQLite has no citext. Without this the two dialects disagree about
        whether two signups are the same person."""
        assert normalise_email(given) == expected

    @pytest.mark.parametrize("junk", ["nobody", "@example.com", "maker@", 42, None])
    def test_a_non_address_is_refused(self, junk):
        with pytest.raises(AuthError):
            normalise_email(junk)

    def test_signing_up_twice_is_refused_case_insensitively(self, accounts, user):
        with pytest.raises(DuplicateEmail):
            accounts.create_user("MAKER@example.com", PASSWORD)


class TestSessions:
    def test_a_session_token_is_not_stored(self, accounts, user):
        token = accounts.create_session(user["id"])
        rows = raw(accounts, "SELECT token_hash FROM sessions")
        assert token not in {row["token_hash"] for row in rows}
        assert accounts.user_for_token(token)["id"] == user["id"]

    def test_a_revoked_token_stops_working(self, accounts, user):
        token = accounts.create_session(user["id"])
        assert accounts.revoke_session(token) is True
        assert accounts.user_for_token(token) is None
        assert accounts.revoke_session(token) is False

    def test_revoking_everything_signs_out_every_device(self, accounts, user):
        tokens = [accounts.create_session(user["id"]) for _ in range(3)]
        assert accounts.revoke_all_sessions(user["id"]) == 3
        assert all(accounts.user_for_token(t) is None for t in tokens)

    def test_an_expired_session_is_not_accepted(self, accounts, user):
        token = accounts.create_session(user["id"])
        from datetime import datetime, timezone
        past = datetime(2000, 1, 1, tzinfo=timezone.utc)
        raw(
            accounts,
            "UPDATE sessions SET expires_at = ? WHERE user_id = ?",
            (accounts._db.stamp(past), user["id"]),
        )
        assert accounts.user_for_token(token) is None

    @pytest.mark.parametrize("junk", ["", "nonsense", None, 7])
    def test_a_junk_token_is_rejected_without_raising(self, accounts, junk):
        """An auth check that throws on a malformed token is a denial of
        service handed to anyone with curl."""
        assert accounts.user_for_token(junk) is None


class TestPlans:
    def test_an_unknown_plan_is_loud_rather_than_silently_free(self):
        """Falling back to free on a typo downgrades a paying customer."""
        with pytest.raises(KeyError):
            plans.get("premium")

    def test_the_free_tier_cannot_export_the_paid_formats(self):
        assert plans.allows_format("free", "stl")
        assert not plans.allows_format("free", "step")
        assert plans.allows_format("maker", "step")

    def test_only_studio_gets_batch(self):
        assert not plans.get("maker").batch
        assert plans.get("studio").batch


class TestSchemaParity:
    """The Postgres schema in docs/schema.sql stays the target for these
    tables too -- the same rule `tests/test_store.py` applies to the telemetry
    ones. A column in one dialect and not the other turns the migration into a
    rewrite."""

    @pytest.mark.parametrize(
        "table", ["users", "sessions", "credit_ledger", "billing_events"]
    )
    def test_every_table_carries_the_columns_the_postgres_schema_declares(
        self, accounts, table
    ):
        sql = Path("docs/schema.sql").read_text()
        block = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.DOTALL)
        assert block, f"{table} not found in docs/schema.sql"
        declared = {
            match.group(1)
            for match in (
                re.match(r"    ([a-z_]+)\s+\S", line) for line in block.group(1).splitlines()
            )
            if match and match.group(1) not in IGNORED
        }
        actual = accounts._db.columns(table)
        assert declared <= actual, f"{table} is missing {sorted(declared - actual)}"

    def test_the_balance_view_exists_in_both_dialects(self, accounts, user):
        assert "CREATE VIEW credit_balance AS" in Path("docs/schema.sql").read_text()
        row = raw(
            accounts, "SELECT * FROM credit_balance WHERE user_id = ?", (user["id"],)
        )[0]
        assert row["balance"] == 3
        assert row["email"] == "maker@example.com"


class TestMigrations:
    """Repeatable and safe to re-apply, which is what makes deploying the same
    migration set to an already-migrated database a non-event."""

    def test_applying_twice_changes_nothing(self, accounts, user):
        assert accounts._db.migrate() == [], "a second run must apply nothing"
        assert accounts.balance(user["id"]) == 3

    def test_a_second_store_on_the_same_database_migrates_cleanly(self, accounts_target, user):
        """Two workers booting against one database. The first applied the
        revisions; the second must find them applied and carry on, not race
        the first into a duplicate-object error."""
        with AccountStore(accounts_target) as second:
            assert second._db.migrate() == []
            assert second.get_user_by_email("maker@example.com") is not None

    def test_the_revision_is_recorded(self, accounts):
        rows = raw(accounts, "SELECT id FROM schema_migrations")
        assert "0001_accounts" in {r["id"] for r in rows}


class TestOpeningGrantVersusPeriodGrant:
    def test_subscribing_in_the_signup_month_still_grants(self, accounts, user):
        """A regression with teeth.

        The opening grant and a period's allowance were briefly keyed the same
        way, so a user who signed up and subscribed inside one calendar month
        collided with their own signup key. The subscription granted nothing,
        and it did so *silently* -- a duplicate idempotency key is meant to be
        a no-op, so there was no error anywhere. Someone would have paid nine
        dollars for zero credits.
        """
        from formforge.accounts.store import current_period

        accounts.start_period(user["id"], current_period(), plan_id="maker")
        assert accounts.balance(user["id"]) == 60
        reasons = [e.reason for e in accounts.ledger(user["id"])]
        assert reasons == ["grant", "expiry", "grant"]

    def test_a_free_account_is_rolled_into_the_new_month_lazily(self, accounts, user):
        """Nothing bills a free account, so no renewal webhook ever arrives for
        one. Without a lazy roll a free user gets three credits once, ever."""
        accounts.spend(user["id"], model_id="m1")
        assert accounts.balance(user["id"]) == 2
        # Pretend they last transacted in a previous month.
        raw(accounts, "UPDATE users SET period_start = '2020-01' WHERE id = ?", (user["id"],))
        moved = accounts.roll_to_current_period(user["id"])
        assert [e.reason for e in moved] == ["expiry", "grant"]
        assert accounts.balance(user["id"]) == 3
        # And again in the same month is a no-op, however often it is called.
        assert accounts.roll_to_current_period(user["id"]) == []
        assert accounts.balance(user["id"]) == 3

    def test_rolling_does_not_touch_a_paid_account(self, accounts, user):
        """Paid periods are the processor's to declare. Rolling one locally
        would grant a month nobody was charged for."""
        accounts.start_period(user["id"], "2020-01", plan_id="maker")
        assert accounts.roll_to_current_period(user["id"]) == []


class TestClawBack:
    """Refund policy: take back what is unspent, never create a debt."""

    def test_it_takes_only_what_is_left(self, accounts, user):
        accounts.start_period(user["id"], "2026-10", plan_id="maker")
        for n in range(40):
            accounts.spend(user["id"], model_id=f"m{n}")
        assert accounts.balance(user["id"]) == 20
        entry = accounts.claw_back(user["id"], 60, idempotency_key="refund:evt_1")
        assert entry is not None and entry.delta == -20
        assert accounts.balance(user["id"]) == 0

    def test_it_never_creates_a_debt(self, accounts, user):
        """The rule the rest of this module depends on: a balance is a number
        of credits somebody can actually spend."""
        for n in range(3):
            accounts.spend(user["id"], model_id=f"m{n}")
        assert accounts.claw_back(user["id"], 3, idempotency_key="refund:evt_2") is None
        assert accounts.balance(user["id"]) == 0

    def test_it_is_idempotent(self, accounts, user):
        first = accounts.claw_back(user["id"], 2, idempotency_key="refund:evt_3")
        second = accounts.claw_back(user["id"], 2, idempotency_key="refund:evt_3")
        assert first.delta == -2 and first.applied
        assert second.id == first.id and not second.applied
        assert accounts.balance(user["id"]) == 1


class TestEntitlement:
    def test_a_model_is_entitled_because_it_was_paid_for(self, accounts, user):
        """Entitlement is a ledger fact, not a flag somebody set."""
        assert accounts.spend_for_model("m1") is None
        accounts.spend(user["id"], model_id="m1")
        paid = accounts.spend_for_model("m1")
        assert paid is not None and paid.user_id == user["id"] and paid.delta == -1
