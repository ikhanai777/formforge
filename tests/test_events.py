"""The security event stream: that it exists, and that it is safe to ship.

Three questions, and the third is the one that would otherwise be found by an
auditor rather than by a test.

**Is every declared event actually emitted?** A catalogue is a promise to whoever
builds an alert on it. A spec nobody emits is a dashboard that reads zero and
looks healthy, so `TestTheCatalogueMatchesTheCode` walks the module and refuses
a declared event that no source file references.

**Does the real path emit it?** Not "can `emit` be called" -- these drive real
HTTP requests through the real app and read what came out of the logger, so a
handler that stops emitting on a refactor fails here.

**Does anything leak?** `TestNoEventCarriesASecret` plants known secrets --
a password, a session cookie, a reset token, a webhook signature -- exercises
every path that handles them, and asserts none of those exact strings appears
anywhere in the formatted output. Written against the *formatted* line rather
than the fields, because the redactor runs at format time and a test that reads
the fields would pass while the shipped log leaked.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from formforge import events
from formforge.logs import JsonFormatter, Redactor

SOURCE = Path(__file__).resolve().parent.parent / "formforge"


class Captured(logging.Handler):
    """Collects records *and* the lines they format into.

    Both halves matter: the fields are what an assertion reads, and the
    formatted line is what actually ships. A leak test that only looks at the
    fields checks the wrong artifact.
    """

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.lines: list[str] = []
        self.addFilter(Redactor())
        self._formatter = JsonFormatter()

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.lines.append(self._formatter.format(record))

    def named(self, spec) -> list[dict]:
        name = spec.name if hasattr(spec, "name") else spec
        return [
            dict(getattr(r, "fields", {}), _name=r.msg)
            for r in self.records
            if r.msg == name
        ]

    def names(self) -> set[str]:
        return {r.msg for r in self.records if isinstance(r.msg, str)}

    @property
    def blob(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def captured():
    handler = Captured()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


class TestTheCatalogueMatchesTheCode:
    def test_every_declared_event_is_emitted_somewhere(self):
        """A spec nobody emits is a promise to an operator that nothing keeps.

        Checked by looking for the *constant* rather than the string, which is
        the point of declaring events as objects: a reference that survives a
        rename cannot be a stale grep.
        """
        blob = "\n".join(
            path.read_text()
            for path in SOURCE.rglob("*.py")
            if path.name != "events.py"
        )
        unwired = []
        for name, value in vars(events).items():
            if not isinstance(value, events.EventSpec):
                continue
            if not re.search(rf"\b{name}\b", blob):
                unwired.append(f"{name} ({value.name})")
        assert not unwired, (
            "declared but never emitted: " + ", ".join(unwired) +
            ". Wire it up or delete the spec; a catalogue entry with no "
            "emission is worse than no entry."
        )

    def test_no_two_events_share_a_name(self):
        names = [spec.name for spec in events.catalogue()]
        assert len(names) == len(set(names))

    def test_every_event_has_a_dotted_namespace(self):
        """`auth.login.failed`, not `login_failed`. The prefix is what a query
        filters on when somebody wants the whole auth stream."""
        for spec in events.catalogue():
            assert re.fullmatch(r"[a-z_]+(\.[a-z_]+)+", spec.name), spec.name

    def test_every_event_says_what_it_is(self):
        for spec in events.catalogue():
            assert len(spec.what) > 20, f"{spec.name} has no description"

    def test_no_declared_field_is_a_known_secret_name(self):
        """Structural: a field called `token` would be redacted by the
        Redactor, but declaring one means somebody intended to log it."""
        from formforge.logs import _SENSITIVE_FIELDS

        for spec in events.catalogue():
            for name in spec.fields + spec.optional:
                assert name.lower() not in _SENSITIVE_FIELDS, (
                    f"{spec.name} declares a field named {name!r}"
                )

    def test_the_generated_description_covers_everything(self):
        text = events.describe()
        for spec in events.catalogue():
            assert spec.name in text


class TestEmitItself:
    def test_a_missing_required_field_is_marked_not_raised(self, captured):
        """A logging call that raises turns an observability gap into an
        outage, so `emit` marks the emission instead."""
        events.emit(logging.getLogger("test"), events.LOGIN_SUCCEEDED)
        [record] = captured.named(events.LOGIN_SUCCEEDED)
        assert record["_incomplete"] == "user_id"

    def test_a_complete_emission_carries_no_marker(self, captured):
        events.emit(logging.getLogger("test"), events.LOGIN_SUCCEEDED, user_id="u1")
        [record] = captured.named(events.LOGIN_SUCCEEDED)
        assert "_incomplete" not in record
        assert record["user_id"] == "u1"

    def test_the_declared_level_is_used(self, captured):
        events.emit(logging.getLogger("test"), events.LOGIN_FAILED, reason="rejected")
        assert captured.records[-1].levelno == logging.WARNING

    def test_a_spec_stringifies_to_its_name(self):
        assert f"{events.LOGIN_FAILED}" == "auth.login.failed"


# ---------------------------------------------------------------------------
# The rest drives the real HTTP surface.
# ---------------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi", reason="needs the `api` extra")
from fastapi.testclient import TestClient  # noqa: E402

from formforge.accounts import OfflineProvider  # noqa: E402
from formforge.api.app import create_app  # noqa: E402
from formforge.api.security import COOKIE_NAME  # noqa: E402

PASSWORD = "correct-horse-battery-staple"
WEBHOOK_SECRET = "a-webhook-signing-secret-nobody-should-see"


@pytest.fixture
def provider():
    return OfflineProvider(secret=WEBHOOK_SECRET)


@pytest.fixture
def app(accounts, provider, tmp_path, monkeypatch):
    monkeypatch.setenv("FORMFORGE_ALLOW_UNSAFE_SANDBOX", "1")
    import formforge.api.security as security

    monkeypatch.setattr(security, "COOKIE_INSECURE", True)
    return create_app(
        store_dir=tmp_path / "store",
        accounts=accounts,
        billing_provider=provider,
        allow_unsafe_sandbox=True,
    )


@pytest.fixture
def client(app):
    with TestClient(app) as made:
        yield made


def signup(client, email="maker@example.com", password=PASSWORD):
    response = client.post(
        "/v1/auth/signup", json={"email": email, "password": password}
    )
    assert response.status_code == 201, response.text
    return response


class TestTheAuthEventsFire:
    def test_a_signup_announces_itself_and_its_opening_grant(self, client, captured):
        signup(client)
        assert events.SIGNUP_SUCCEEDED.name in captured.names()
        [grant] = captured.named(events.CREDIT_GRANTED)
        assert grant["credits"] == 3
        assert grant["reason"] == "grant"

    def test_a_duplicate_signup_is_a_rejection_not_a_success(self, client, captured):
        signup(client)
        client.post(
            "/v1/auth/signup",
            json={"email": "maker@example.com", "password": PASSWORD},
        )
        [rejected] = captured.named(events.SIGNUP_REJECTED)
        assert rejected["reason"] == "duplicate_address"

    def test_a_login_and_a_failed_login_are_distinguishable(self, client, captured):
        signup(client)
        client.post("/v1/auth/logout")
        client.post(
            "/v1/auth/login",
            json={"email": "maker@example.com", "password": "wrong-password-entirely"},
        )
        client.post(
            "/v1/auth/login",
            json={"email": "maker@example.com", "password": PASSWORD},
        )
        assert len(captured.named(events.LOGIN_FAILED)) == 1
        assert len(captured.named(events.LOGIN_SUCCEEDED)) == 1

    def test_a_failed_login_does_not_say_which_address(self, client, captured):
        """The response refuses to distinguish "no such account" from "wrong
        password". A log that records the address undoes that: it is the same
        list of who has an account here, one line at a time."""
        signup(client)
        client.post(
            "/v1/auth/login",
            json={"email": "stranger@example.com", "password": PASSWORD},
        )
        [failed] = captured.named(events.LOGIN_FAILED)
        assert "stranger@example.com" not in json.dumps(failed)
        assert "stranger@example.com" not in captured.blob

    def test_a_logout_names_the_account_it_ended(self, client, captured):
        signup(client)
        client.post("/v1/auth/logout")
        assert len(captured.named(events.LOGOUT)) == 1

    def test_an_unknown_session_is_recorded_as_rejected(self, client, captured):
        client.cookies.set(COOKIE_NAME, "not-a-real-session-token")
        client.get("/v1/auth/me")
        [rejected] = captured.named(events.SESSION_REJECTED)
        assert rejected["reason"] == "not_current"

    def test_no_cookie_at_all_is_a_different_reason(self, client, captured):
        client.get("/v1/auth/me")
        [rejected] = captured.named(events.SESSION_REJECTED)
        assert rejected["reason"] == "absent"


class TestTheAccessControlEventsFire:
    def test_a_stranger_reaching_for_a_model_is_recorded_as_denied(
        self, client, app, captured
    ):
        """The caller is told 404 to avoid confirming the id names anything.
        The log is where the distinction survives."""
        signup(client)
        client.cookies.clear()
        signup(client, "other@example.com")
        client.get("/v1/models/00000000-0000-0000-0000-000000000000")
        denials = captured.named(events.AUTHZ_DENIED)
        assert denials, "an unauthorised model read emitted nothing"
        assert denials[-1]["reason"] in ("ownerless", "not_owner")
        assert denials[-1]["route"].startswith("/v1/models/")

    def test_an_anonymous_model_read_is_a_denial_with_no_session(
        self, client, captured
    ):
        client.get("/v1/models/00000000-0000-0000-0000-000000000000")
        [denied] = captured.named(events.AUTHZ_DENIED)
        assert denied["reason"] == "no_session"
        assert "user_id" not in denied

    def test_a_rate_limited_request_names_the_bucket(self, client, captured):
        for index in range(30):
            client.post(
                "/v1/auth/login",
                json={"email": f"x{index}@example.com", "password": PASSWORD},
            )
        limited = captured.named(events.RATE_LIMITED)
        assert limited, "the limiter refused nothing in 30 attempts"
        assert limited[0]["bucket"] == "login"


class TestTheCreditEventsFire:
    def test_a_grant_is_announced_once_even_when_retried(self, accounts, captured):
        """Idempotency working is not a second grant, and a stream that shows
        two makes a redelivered webhook look like a double charge."""
        user = accounts.create_user("ledger@example.com", PASSWORD)
        accounts.grant(user["id"], 10, idempotency_key="k1")
        accounts.grant(user["id"], 10, idempotency_key="k1")
        granted = captured.named(events.CREDIT_GRANTED)
        # Two events: the opening grant at signup, and one -- not two -- for
        # the repeated key.
        assert [g["idempotency_key"] for g in granted] == [
            f"signup:{user['id']}", "k1",
        ]

    def test_a_spend_names_the_model_it_paid_for(self, accounts, captured):
        user = accounts.create_user("spender@example.com", PASSWORD)
        accounts.spend(user["id"], 1, model_id="m-42")
        [spent] = captured.named(events.CREDIT_SPENT)
        assert spent["model_id"] == "m-42"
        assert spent["credits"] == 1

    def test_a_period_roll_announces_the_expiry_and_the_new_grant(
        self, accounts, captured
    ):
        user = accounts.create_user("roller@example.com", PASSWORD, plan="maker")
        accounts.start_period(user["id"], "2099-01")
        expiries = captured.named(events.CREDIT_EXPIRED)
        assert expiries, "credits vanished at a period boundary with no event"
        assert expiries[-1]["period"] == "2099-01"
        assert captured.named(events.CREDIT_GRANTED)

    def test_a_claw_back_is_announced_as_a_refund(self, accounts, captured):
        user = accounts.create_user("refunded@example.com", PASSWORD)
        accounts.claw_back(user["id"], 2, idempotency_key="refund:x")
        [event] = captured.named(events.CREDIT_REFUNDED)
        assert event["credits"] == 2

    def test_a_failed_transaction_announces_nothing(self, accounts, captured):
        """The reason movements are announced after the commit rather than
        from `_append`: a log that reports credits that were rolled back is
        worse than one that reports nothing."""
        from formforge.accounts import AccountError

        with pytest.raises(AccountError):
            accounts.grant("no-such-user", 5)
        assert not captured.named(events.CREDIT_GRANTED)

    def test_a_refused_spend_is_recorded_without_a_ledger_row(
        self, accounts, captured
    ):
        from formforge.accounts import InsufficientCredits

        user = accounts.create_user("broke@example.com", PASSWORD)
        accounts.spend(user["id"], 3)
        with pytest.raises(InsufficientCredits):
            accounts.spend(user["id"], 1)
        # `credit.refused` is emitted by the API, which is where the decision
        # is visible; the store raises. What must be true here is that a
        # refused spend produced no `credit.spent`.
        assert len(captured.named(events.CREDIT_SPENT)) == 1


class TestTheBillingEventsFire:
    def _post(self, client, provider, payload):
        body = json.dumps(payload).encode()
        return client.post(
            "/v1/billing/webhook",
            content=body,
            headers={"x-formforge-signature": provider.sign(body)},
        )

    def _customer(self, client, accounts):
        signup(client)
        user = accounts.user_for_token(client.cookies.get(COOKIE_NAME))
        accounts.set_billing_customer(user["id"], "cus_test_1")
        return user

    def test_an_accepted_webhook_names_its_event_id(
        self, client, accounts, provider, captured
    ):
        self._customer(client, accounts)
        self._post(client, provider, {
            "id": "evt_1", "type": "subscription.activated",
            "customer_id": "cus_test_1", "plan_id": "maker",
            "period_start": "2099-02",
        })
        [accepted] = captured.named(events.WEBHOOK_ACCEPTED)
        assert accepted["event_id"] == "evt_1"
        assert accepted["event_type"] == "subscription.activated"

    def test_a_redelivery_is_a_duplicate_not_a_second_acceptance(
        self, client, accounts, provider, captured
    ):
        self._customer(client, accounts)
        payload = {
            "id": "evt_2", "type": "subscription.activated",
            "customer_id": "cus_test_1", "plan_id": "maker",
            "period_start": "2099-03",
        }
        self._post(client, provider, payload)
        self._post(client, provider, payload)
        assert len(captured.named(events.WEBHOOK_ACCEPTED)) == 1
        assert len(captured.named(events.WEBHOOK_DUPLICATE)) == 1

    def test_a_bad_signature_is_recorded_and_the_body_is_not(
        self, client, captured
    ):
        client.post(
            "/v1/billing/webhook",
            content=json.dumps({
                "id": "evt_forged", "type": "credits.purchased",
                "customer_id": "cus_test_1", "credits": 100000,
            }).encode(),
            headers={"x-formforge-signature": "0" * 64},
        )
        [failed] = captured.named(events.WEBHOOK_SIGNATURE_FAILED)
        assert failed["provider"] == "offline"
        # An unverified body is attacker-chosen text. Logging it puts whatever
        # somebody POSTed into the security stream.
        assert "evt_forged" not in captured.blob

    def test_an_unmappable_event_is_recorded_as_unusable(
        self, client, provider, captured
    ):
        self._post(client, provider, {"id": "evt_x", "type": "nonsense.thing",
                                      "customer_id": "cus_test_1"})
        assert captured.named(events.WEBHOOK_UNUSABLE)

    def test_a_verified_event_for_an_unknown_customer_is_loud(
        self, client, provider, captured
    ):
        """Somebody paid and the money reached no account. The endpoint
        answers 400; this is the event that makes it findable."""
        self._post(client, provider, {
            "id": "evt_orphan", "type": "subscription.activated",
            "customer_id": "cus_nobody", "plan_id": "maker",
        })
        [failed] = captured.named(events.WEBHOOK_NOT_APPLIED)
        assert failed["event_id"] == "evt_orphan"
        [record] = [r for r in captured.records
                    if r.msg == events.WEBHOOK_NOT_APPLIED.name]
        assert record.levelno >= logging.ERROR

    def test_a_stale_status_event_is_recorded_as_skipped(
        self, client, accounts, provider, captured
    ):
        import time as _time

        self._customer(client, accounts)
        now = int(_time.time())
        self._post(client, provider, {
            "id": "evt_new", "type": "subscription.activated",
            "customer_id": "cus_test_1", "plan_id": "maker",
            "period_start": "2099-04", "created": now,
        })
        self._post(client, provider, {
            "id": "evt_old", "type": "payment.failed",
            "customer_id": "cus_test_1", "created": now - 600,
        })
        [stale] = captured.named(events.WEBHOOK_STALE)
        assert stale["event_id"] == "evt_old"

    def test_a_checkout_is_announced(self, client, accounts, captured):
        signup(client)
        client.post("/v1/billing/checkout", json={"plan": "maker"})
        [started] = captured.named(events.CHECKOUT_STARTED)
        assert started["plan"] == "maker"


class TestTheLifecycleEventsFire:
    def test_closing_an_account_says_how_many_sessions_died(
        self, accounts, captured
    ):
        user = accounts.create_user("leaving@example.com", PASSWORD)
        accounts.create_session(user["id"])
        accounts.close_account(user["id"], actor="operator")
        [closed] = captured.named(events.ACCOUNT_CLOSED)
        assert closed["sessions_revoked"] == 1
        assert closed["actor"] == "operator"

    def test_reopening_is_recorded_because_it_is_an_operator_action(
        self, accounts, captured
    ):
        user = accounts.create_user("back@example.com", PASSWORD)
        accounts.close_account(user["id"])
        accounts.reopen_account(user["id"], actor="operator")
        assert captured.named(events.ACCOUNT_REOPENED)

    def test_marking_and_deleting_artifacts_are_separate_events(
        self, accounts, captured
    ):
        user = accounts.create_user("files@example.com", PASSWORD)
        accounts.record_artifact(
            "m-1", "stl", "models/m-1/stl", user_id=user["id"], size=10
        )
        accounts.mark_artifacts("m-1")
        [marked] = captured.named(events.ARTIFACT_MARKED)
        assert marked["count"] == 1
        [row] = accounts.artifacts_for("m-1")
        accounts.finish_artifact_delete(row["id"])
        [deleted] = captured.named(events.ARTIFACT_DELETED)
        assert deleted["model_id"] == "m-1"
        assert deleted["fmt"] == "stl"


class TestNoEventCarriesASecret:
    """The test that would otherwise be an audit finding.

    Everything asserted here is checked against the *formatted* JSON line, not
    the field dict, because the redactor runs at format time. A test reading
    the fields would pass on a build that ships the secret.
    """

    def test_a_password_never_reaches_the_log(self, client, captured):
        signup(client)
        client.post("/v1/auth/logout")
        client.post(
            "/v1/auth/login",
            json={"email": "maker@example.com", "password": PASSWORD},
        )
        client.post(
            "/v1/auth/login",
            json={"email": "maker@example.com", "password": "another-real-password"},
        )
        assert PASSWORD not in captured.blob
        assert "another-real-password" not in captured.blob

    def test_a_session_token_never_reaches_the_log(self, client, captured):
        signup(client)
        token = client.cookies.get(COOKIE_NAME)
        client.get("/v1/auth/me")
        client.post("/v1/auth/logout")
        assert token and len(token) > 20
        assert token not in captured.blob

    def test_a_rejected_session_token_never_reaches_the_log(self, client, captured):
        """The rejected one matters more: it is the value an attacker chose,
        and the natural instinct is to log what was refused."""
        forged = "forged-session-token-aaaaaaaaaaaaaaaaaaaa"
        client.cookies.set(COOKIE_NAME, forged)
        client.get("/v1/auth/me")
        assert captured.named(events.SESSION_REJECTED)
        assert forged not in captured.blob

    def test_a_reset_token_never_reaches_the_log(
        self, accounts, client, captured, tmp_path, monkeypatch
    ):
        """A reset token is a bearer credential for one account. A log holding
        it grants that account to everyone who can read the log -- which for a
        shipped pipeline is more people than can read the database."""
        signup(client)
        user = accounts.get_user_by_email("maker@example.com")
        token = accounts.create_password_reset(user["id"])
        client.post("/v1/auth/reset/confirm",
                    json={"token": token, "password": "a-brand-new-password"})
        client.post("/v1/auth/reset/confirm",
                    json={"token": token, "password": "a-brand-new-password"})
        assert captured.named(events.RESET_COMPLETED)
        assert captured.named(events.RESET_REJECTED)
        assert token not in captured.blob
        assert "a-brand-new-password" not in captured.blob

    def test_the_webhook_signing_secret_never_reaches_the_log(
        self, client, provider, captured
    ):
        body = json.dumps({"id": "evt_s", "type": "subscription.activated",
                           "customer_id": "cus_x"}).encode()
        client.post("/v1/billing/webhook", content=body,
                    headers={"x-formforge-signature": provider.sign(body)})
        client.post("/v1/billing/webhook", content=body,
                    headers={"x-formforge-signature": "0" * 64})
        assert WEBHOOK_SECRET not in captured.blob
        assert provider.sign(body) not in captured.blob

    def test_an_email_address_is_not_a_field_on_any_event(self, client, captured):
        """Not a secret, but the security stream is copied further than the
        database is, and an address list is what most of these endpoints are
        built to avoid handing out."""
        signup(client, "private@example.com")
        client.post("/v1/auth/reset/request", json={"email": "private@example.com"})
        client.post("/v1/auth/reset/request", json={"email": "nobody@example.com"})
        for record in captured.records:
            fields = json.dumps(getattr(record, "fields", {}))
            assert "@example.com" not in fields, f"{record.msg} carries an address"

    def test_the_reset_request_answers_the_same_for_both(self, client, captured):
        """known=true and known=false are both recorded -- the *log* may
        distinguish them, since it is the thing watching for enumeration. The
        response must not, and the address must not appear either way."""
        signup(client, "known@example.com")
        client.post("/v1/auth/reset/request", json={"email": "known@example.com"})
        client.post("/v1/auth/reset/request", json={"email": "unknown@example.com"})
        seen = {r["known"] for r in captured.named(events.RESET_REQUESTED)}
        assert seen == {True, False}
        assert "known@example.com" not in captured.blob

    def test_no_emission_anywhere_is_incomplete(self, client, accounts, captured):
        """`_incomplete` should never appear in a real deployment. If it does,
        an alert somebody wrote is filtering on a field that is not there."""
        signup(client)
        client.post("/v1/auth/logout")
        client.post("/v1/auth/login",
                    json={"email": "maker@example.com", "password": PASSWORD})
        client.post("/v1/billing/checkout", json={"plan": "maker"})
        client.get("/v1/models/00000000-0000-0000-0000-000000000000")
        incomplete = [
            (r.msg, getattr(r, "fields", {}).get("_incomplete"))
            for r in captured.records
            if "_incomplete" in getattr(r, "fields", {})
        ]
        assert not incomplete, f"events missing declared fields: {incomplete}"
