"""The metered HTTP surface: auth, ownership, the credit gate, billing.

These are the tests where a mistake is a security incident or a wrong charge
rather than a broken feature, so they are written from the outside -- real
requests through the real app, with a real session cookie -- rather than by
calling handlers directly. A check that only exists in a unit test is a check
that can be routed around.

The geometry is faked. What is under test is who is allowed to download what,
and running OCCT to find that out would make the suite too slow to run.
"""

from __future__ import annotations

import json
import time

import pytest

from formforge.accounts import OfflineProvider

# These imports come after `importorskip` on purpose: without the `api` extra
# installed they would be an ImportError at collection time, which fails the
# whole run instead of skipping this file. Hence the E402s.
fastapi = pytest.importorskip("fastapi", reason="needs the `api` extra")
from fastapi.testclient import TestClient  # noqa: E402

from formforge.api.app import create_app  # noqa: E402
from formforge.api.security import COOKIE_NAME  # noqa: E402

PASSWORD = "correct-horse-battery-staple"
OTHER = "another-long-enough-password"


class _Result:
    """The shape `record_generation` and the download path expect."""

    def __init__(self, model_id: str, artifacts: dict[str, str], status: str = "ok"):
        self.model_id = model_id
        self.status = status
        self.prompt = "a vase"
        self.template_id = "vessel_vase"
        self.route = "template"
        self.iterations = 1
        self.stats = {"bbox_mm": [10, 10, 10], "triangles": 12}
        self.validation = {"measurements": {}, "warnings": []}
        self.events = []
        self.artifacts = artifacts
        self.intent = {}
        self.params = {}
        self.source_code = "x = 1"
        self.language = "build123d"
        self.ok = status == "ok"
        self.message = ""
        self.usage = None
        self.duration_ms = 5

    def as_dict(self) -> dict:
        """What `GET /v1/models/{id}` returns.

        Carries the fields that must not reach a stranger, so the ownership
        tests can assert on their absence rather than only on a status code.
        """
        return {
            "model_id": self.model_id,
            "status": self.status,
            "prompt": self.prompt,
            "params": self.params,
            "source_code": self.source_code,
        }


@pytest.fixture
def files(tmp_path) -> dict[str, str]:
    made = {}
    for fmt, name in (
        ("stl", "model.stl"), ("3mf", "model.3mf"),
        ("step", "model.step"), ("report", "report.json"),
    ):
        path = tmp_path / name
        path.write_text(f"{fmt} bytes")
        made[fmt] = str(path)
    return made


@pytest.fixture
def provider() -> OfflineProvider:
    return OfflineProvider(secret="webhook-secret")


@pytest.fixture
def app(accounts, provider, tmp_path, monkeypatch):
    monkeypatch.setenv("FORMFORGE_ALLOW_UNSAFE_SANDBOX", "1")
    # Cookies over plain HTTP, which is what the test client speaks. The
    # production default is Secure; see formforge/api/security.py.
    monkeypatch.setattr("formforge.api.security.COOKIE_INSECURE", True, raising=False)
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


def signup(client, email: str = "maker@example.com", password: str = PASSWORD):
    response = client.post("/v1/auth/signup", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    return response


def finish_build(app, client, model_id: str, files: dict, *, status: str = "ok"):
    """Pretend a generation finished, exactly as the background task would.

    Reaches into the app's own job store and charging function rather than
    reimplementing them, so the thing under test is the real code path.
    """
    state = app.state.formforge
    job = state["jobs"].create()
    job.model_id = model_id
    state["jobs"].by_model[model_id] = job
    user = state["accounts"].user_for_token(client.cookies.get(COOKIE_NAME))
    job.owner_id = user["id"] if user else None
    result = _Result(model_id, files, status=status)
    job.result = result
    job.status = status
    state["db"].record_generation(result, user_id=job.owner_id)
    state["charge"](job, result)
    return job


class TestAuth:
    def test_signup_returns_the_account_and_sets_a_session(self, client):
        body = signup(client).json()
        assert body["email"] == "maker@example.com"
        assert body["credits"] == 3
        assert body["plan"] == "free"
        assert COOKIE_NAME in client.cookies

    @pytest.mark.parametrize(
        "leak", ["user_id", "id", "billing_customer_id", "password_hash", "period_start"]
    )
    def test_the_browser_is_never_told_an_internal_identifier(self, client, leak):
        """Account ids and processor ids are not secrets exactly, but they are
        the things an attacker probing the API would rather have than not."""
        body = signup(client).json()
        assert leak not in body
        assert leak not in client.get("/v1/auth/me").json()

    def test_a_short_password_is_refused_before_an_account_exists(self, client):
        response = client.post(
            "/v1/auth/signup", json={"email": "x@example.com", "password": "short"}
        )
        assert response.status_code == 422

    def test_signing_up_twice_is_refused(self, client):
        signup(client)
        client.cookies.clear()
        again = client.post(
            "/v1/auth/signup", json={"email": "MAKER@example.com", "password": PASSWORD}
        )
        assert again.status_code == 409

    def test_login_restores_a_session(self, client):
        signup(client)
        client.cookies.clear()
        assert client.get("/v1/auth/me").status_code == 401
        assert client.post(
            "/v1/auth/login", json={"email": "maker@example.com", "password": PASSWORD}
        ).status_code == 200
        assert client.get("/v1/auth/me").json()["email"] == "maker@example.com"

    def test_a_wrong_password_and_an_unknown_account_answer_the_same(self, client):
        signup(client)
        wrong = client.post(
            "/v1/auth/login", json={"email": "maker@example.com", "password": OTHER}
        )
        missing = client.post(
            "/v1/auth/login", json={"email": "nobody@example.com", "password": OTHER}
        )
        assert wrong.status_code == missing.status_code == 401
        assert wrong.json() == missing.json()

    def test_logout_invalidates_the_session_server_side(self, client, accounts):
        signup(client)
        token = client.cookies.get(COOKIE_NAME)
        assert client.post("/v1/auth/logout").status_code == 204
        # The cookie is cleared, but the point is the token itself is dead --
        # anyone who kept a copy must not still be signed in.
        assert accounts.user_for_token(token) is None

    def test_the_session_cookie_is_httponly_and_samesite(self, client):
        header = signup(client).headers["set-cookie"].lower()
        assert "httponly" in header
        assert "samesite=lax" in header

    def test_an_unknown_cookie_is_not_a_session(self, client):
        client.cookies.set(COOKIE_NAME, "not-a-real-token")
        assert client.get("/v1/auth/me").status_code == 401


class TestRateLimits:
    def test_login_attempts_are_capped(self, client):
        signup(client)
        codes = [
            client.post(
                "/v1/auth/login", json={"email": "maker@example.com", "password": OTHER}
            ).status_code
            for _ in range(15)
        ]
        assert 429 in codes, "an unlimited login endpoint is a password oracle"
        assert codes.count(401) <= 10

    def test_signups_are_capped(self, client):
        codes = []
        for n in range(8):
            client.cookies.clear()
            codes.append(
                client.post(
                    "/v1/auth/signup",
                    json={"email": f"u{n}@example.com", "password": PASSWORD},
                ).status_code
            )
        assert 429 in codes


class TestCreditGate:
    def test_a_successful_build_costs_exactly_one_credit(self, app, client, files):
        signup(client)
        finish_build(app, client, "model-1", files)
        assert client.get("/v1/auth/me").json()["credits"] == 2

    def test_a_failed_build_costs_nothing(self, app, client, files):
        """The rule the CLI already follows: a rejection is not a billable
        event. Charging for a build that produced nothing is the single
        fastest way to lose a customer."""
        signup(client)
        finish_build(app, client, "model-bad", files, status="failed")
        assert client.get("/v1/auth/me").json()["credits"] == 3

    def test_recording_the_same_build_twice_charges_once(self, app, client, files):
        signup(client)
        finish_build(app, client, "model-1", files)
        finish_build(app, client, "model-1", files)
        assert client.get("/v1/auth/me").json()["credits"] == 2

    def test_a_paid_model_downloads_and_refreshing_is_free(self, app, client, files):
        signup(client)
        finish_build(app, client, "model-1", files)
        for _ in range(3):
            response = client.get("/v1/models/model-1/download?format=stl")
            assert response.status_code == 200
            assert response.content == b"stl bytes"
        assert client.get("/v1/auth/me").json()["credits"] == 2, (
            "downloading is not what costs a credit; building is"
        )

    def test_an_unpaid_model_is_refused_without_delivering_the_file(
        self, app, client, files, accounts
    ):
        """The race the pre-check cannot close: two builds finishing against
        one last credit. The second is kept but unpaid, and must not be
        downloadable until it is paid for."""
        signup(client)
        user = accounts.get_user_by_email("maker@example.com")
        accounts.spend(user["id"], 3, idempotency_key="drain")
        finish_build(app, client, "model-broke", files)
        response = client.get("/v1/models/model-broke/download?format=stl")
        assert response.status_code == 402
        assert b"stl bytes" not in response.content

    def test_the_free_plan_cannot_export_the_paid_formats(self, app, client, files):
        signup(client)
        finish_build(app, client, "model-1", files)
        assert client.get("/v1/models/model-1/download?format=stl").status_code == 200
        for fmt in ("step", "3mf"):
            refused = client.get(f"/v1/models/model-1/download?format={fmt}")
            assert refused.status_code == 403
            assert b"bytes" not in refused.content

    def test_the_report_is_not_paywalled(self, app, client, files, accounts):
        """A user arguing about a charge needs to see what the build measured.
        Paywalling the evidence makes that argument unanswerable."""
        signup(client)
        user = accounts.get_user_by_email("maker@example.com")
        accounts.spend(user["id"], 3, idempotency_key="drain")
        finish_build(app, client, "model-1", files)
        assert client.get("/v1/models/model-1/download?format=report").status_code == 200

    def test_generating_with_no_credits_is_refused_before_the_sandbox_runs(
        self, client, accounts
    ):
        signup(client)
        user = accounts.get_user_by_email("maker@example.com")
        accounts.spend(user["id"], 3, idempotency_key="drain")
        response = client.post("/v1/generate", json={"prompt": "a vase"})
        assert response.status_code == 402

    def test_generating_signed_out_is_refused(self, client):
        assert client.post("/v1/generate", json={"prompt": "a vase"}).status_code == 401


class TestOwnership:
    def test_another_users_model_is_not_downloadable(self, app, client, files):
        signup(client)
        finish_build(app, client, "mine", files)
        client.post("/v1/auth/logout")
        client.cookies.clear()
        signup(client, "intruder@example.com")
        response = client.get("/v1/models/mine/download?format=stl")
        # 404 rather than 403: a 403 confirms the id names something real,
        # which turns this into an oracle for enumerating model ids.
        assert response.status_code == 404
        assert b"stl bytes" not in response.content

    def test_another_users_model_is_not_in_your_history(self, app, client, files):
        signup(client)
        finish_build(app, client, "mine", files)
        assert [m["model_id"] for m in client.get("/v1/account/history").json()["models"]] == [
            "mine"
        ]
        client.post("/v1/auth/logout")
        client.cookies.clear()
        signup(client, "intruder@example.com")
        assert client.get("/v1/account/history").json()["models"] == []

    def test_history_and_credits_need_a_session(self, client):
        for path in ("/v1/account/history", "/v1/account/credits", "/v1/auth/me"):
            assert client.get(path).status_code == 401

    def test_the_credit_history_hides_ledger_internals(self, app, client, files):
        signup(client)
        finish_build(app, client, "model-1", files)
        history = client.get("/v1/account/credits").json()["history"]
        assert history[0]["change"] == -1
        for row in history:
            assert "idempotency_key" not in row
            assert "id" not in row


class TestBillingOverHttp:
    def _hook(self, client, provider, payload):
        body = json.dumps(payload).encode()
        return client.post(
            "/v1/billing/webhook",
            content=body,
            headers={"x-formforge-signature": provider.sign(body)},
        )

    def test_checkout_does_not_grant_anything(self, client, provider):
        """A started checkout is not a settled payment. Only the webhook is."""
        signup(client)
        response = client.post("/v1/billing/checkout", json={"plan": "maker"})
        assert response.status_code == 200
        assert client.get("/v1/auth/me").json()["credits"] == 3

    def test_a_forged_webhook_grants_nothing(self, client, accounts):
        signup(client)
        client.post("/v1/billing/checkout", json={"plan": "maker"})
        user = accounts.get_user_by_email("maker@example.com")
        body = json.dumps(
            {
                "type": "credits.purchased",
                "id": "evil",
                "customer_id": user["billing_customer_id"],
                "credits": 10_000,
            }
        ).encode()
        response = client.post(
            "/v1/billing/webhook", content=body,
            headers={"x-formforge-signature": "not-the-signature"},
        )
        assert response.status_code == 400
        assert accounts.balance(user["id"]) == 3

    def test_a_verified_activation_grants_and_a_replay_does_not(
        self, client, provider, accounts
    ):
        signup(client)
        client.post("/v1/billing/checkout", json={"plan": "maker"})
        user = accounts.get_user_by_email("maker@example.com")
        event = {
            "type": "subscription.activated",
            "id": "evt_1",
            "customer_id": user["billing_customer_id"],
            "plan_id": "maker",
            "period_start": "2026-09",
            "created": int(time.time()),
        }
        assert self._hook(client, provider, event).json()["applied"] is True
        assert client.get("/v1/auth/me").json()["credits"] == 60
        # Stripe redelivers whenever it is unsure. Twice applied is two months
        # of credits for one payment.
        assert self._hook(client, provider, event).json()["applied"] is False
        assert client.get("/v1/auth/me").json()["credits"] == 60

    def test_a_paid_plan_unlocks_the_paid_formats(self, app, client, provider, accounts, files):
        signup(client)
        client.post("/v1/billing/checkout", json={"plan": "maker"})
        user = accounts.get_user_by_email("maker@example.com")
        self._hook(client, provider, {
            "type": "subscription.activated", "id": "evt_1",
            "customer_id": user["billing_customer_id"], "plan_id": "maker",
            "period_start": "2026-09", "created": int(time.time()),
        })
        finish_build(app, client, "model-1", files)
        for fmt in ("stl", "3mf", "step"):
            assert client.get(f"/v1/models/model-1/download?format={fmt}").status_code == 200

    def test_a_failed_payment_does_not_strip_credits(self, client, provider, accounts):
        signup(client)
        client.post("/v1/billing/checkout", json={"plan": "maker"})
        user = accounts.get_user_by_email("maker@example.com")
        now = int(time.time())
        self._hook(client, provider, {
            "type": "subscription.activated", "id": "evt_1",
            "customer_id": user["billing_customer_id"], "plan_id": "maker",
            "period_start": "2026-09", "created": now,
        })
        self._hook(client, provider, {
            "type": "payment.failed", "id": "evt_2",
            "customer_id": user["billing_customer_id"], "created": now + 10,
        })
        body = client.get("/v1/auth/me").json()
        assert body["plan_status"] == "past_due"
        assert body["credits"] == 60, "past_due must not confiscate a paid month"

    def test_past_due_still_allows_spending(self, app, client, provider, accounts, files):
        """The approved policy: a failed renewal is usually a card about to be
        fixed, and those credits were paid for in a period that settled."""
        signup(client)
        client.post("/v1/billing/checkout", json={"plan": "maker"})
        user = accounts.get_user_by_email("maker@example.com")
        now = int(time.time())
        self._hook(client, provider, {
            "type": "subscription.activated", "id": "evt_1",
            "customer_id": user["billing_customer_id"], "plan_id": "maker",
            "period_start": "2026-09", "created": now,
        })
        self._hook(client, provider, {
            "type": "payment.failed", "id": "evt_2",
            "customer_id": user["billing_customer_id"], "created": now + 10,
        })
        finish_build(app, client, "model-1", files)
        assert client.get("/v1/models/model-1/download?format=step").status_code == 200

    def test_a_refund_takes_back_only_what_is_unspent(
        self, app, client, provider, accounts, files
    ):
        signup(client)
        client.post("/v1/billing/checkout", json={"plan": "maker"})
        user = accounts.get_user_by_email("maker@example.com")
        now = int(time.time())
        self._hook(client, provider, {
            "type": "subscription.activated", "id": "evt_1",
            "customer_id": user["billing_customer_id"], "plan_id": "maker",
            "period_start": "2026-09", "created": now,
        })
        for n in range(5):
            finish_build(app, client, f"model-{n}", files)
        assert client.get("/v1/auth/me").json()["credits"] == 55
        self._hook(client, provider, {
            "type": "payment.refunded", "id": "evt_r",
            "customer_id": user["billing_customer_id"], "plan_id": "maker",
            "created": now + 100,
        })
        assert client.get("/v1/auth/me").json()["credits"] == 0
        # And what they already built stays theirs. They paid for those five.
        assert client.get("/v1/models/model-1/download?format=stl").status_code == 200

    def test_an_expired_subscription_returns_the_account_to_free(
        self, client, provider, accounts
    ):
        signup(client)
        client.post("/v1/billing/checkout", json={"plan": "maker"})
        user = accounts.get_user_by_email("maker@example.com")
        now = int(time.time())
        self._hook(client, provider, {
            "type": "subscription.activated", "id": "evt_1",
            "customer_id": user["billing_customer_id"], "plan_id": "maker",
            "period_start": "2026-09", "created": now,
        })
        self._hook(client, provider, {
            "type": "subscription.expired", "id": "evt_x",
            "customer_id": user["billing_customer_id"], "created": now + 50,
        })
        body = client.get("/v1/auth/me").json()
        assert body["plan"] == "free"
        assert body["credits"] == 60, "the paid month expires at the next roll, not now"

    def test_an_overtaken_status_change_is_not_applied(self, client, provider, accounts):
        """Out-of-order delivery. A cancellation that Stripe emitted *before*
        the renewal, but delivered after it, must not leave the account
        cancelled while it is paid up."""
        signup(client)
        client.post("/v1/billing/checkout", json={"plan": "maker"})
        user = accounts.get_user_by_email("maker@example.com")
        now = int(time.time())
        self._hook(client, provider, {
            "type": "subscription.activated", "id": "evt_new",
            "customer_id": user["billing_customer_id"], "plan_id": "maker",
            "period_start": "2026-09", "created": now + 100,
        })
        self._hook(client, provider, {
            "type": "subscription.cancelled", "id": "evt_old",
            "customer_id": user["billing_customer_id"], "created": now,
        })
        assert client.get("/v1/auth/me").json()["plan_status"] == "active"

    def test_an_event_for_an_unknown_customer_is_kept_not_dropped(
        self, client, provider, accounts
    ):
        signup(client)
        response = self._hook(client, provider, {
            "type": "credits.purchased", "id": "evt_ghost",
            "customer_id": "cus_nobody", "credits": 5, "created": int(time.time()),
        })
        assert response.status_code == 400
        assert [e["event_id"] for e in accounts.unhandled_billing_events()] == ["evt_ghost"]


class TestTheFreePathIsStillFree:
    """The gateway without an account store must behave exactly as it did
    before any of this existed. This is what keeps the self-hosted and CLI
    paths unmetered."""

    @pytest.fixture
    def open_app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORMFORGE_ALLOW_UNSAFE_SANDBOX", "1")
        return create_app(store_dir=tmp_path / "open", allow_unsafe_sandbox=True)

    def test_there_are_no_account_routes(self, open_app):
        paths = {route.path for route in open_app.routes}
        for path in (
            "/v1/auth/signup", "/v1/auth/login", "/v1/auth/me",
            "/v1/billing/checkout", "/v1/billing/webhook", "/v1/account/credits",
        ):
            assert path not in paths

    def test_downloading_needs_no_session_and_no_credit(self, open_app, files):
        with TestClient(open_app) as client:
            state = open_app.state.formforge
            job = state["jobs"].create()
            job.model_id = "free-model"
            state["jobs"].by_model["free-model"] = job
            job.result = _Result("free-model", files)
            job.status = "ok"
            for fmt in ("stl", "3mf", "step"):
                assert client.get(
                    f"/v1/models/free-model/download?format={fmt}"
                ).status_code == 200


class TestSignedDownloadLinks:
    """A link that carries its own authorisation. Within its few minutes it is
    a bearer credential for one model in one format, so what is tested here is
    that it is exactly that and not one inch more."""

    def test_a_link_serves_the_file_without_a_cookie(self, app, client, files):
        signup(client)
        finish_build(app, client, "model-1", files)
        minted = client.post("/v1/models/model-1/download-link?format=stl")
        assert minted.status_code == 200
        url = minted.json()["url"]
        client.cookies.clear()
        response = client.get(url)
        assert response.status_code == 200
        assert response.content == b"stl bytes"

    def test_a_link_cannot_be_minted_for_a_format_the_plan_excludes(self, app, client, files):
        """Entitlement is checked at minting time, so a token can only exist
        for a file its holder was already allowed to fetch."""
        signup(client)
        finish_build(app, client, "model-1", files)
        assert client.post(
            "/v1/models/model-1/download-link?format=step"
        ).status_code == 403

    def test_a_link_cannot_be_minted_for_someone_elses_model(self, app, client, files):
        signup(client)
        finish_build(app, client, "mine", files)
        client.post("/v1/auth/logout")
        client.cookies.clear()
        signup(client, "intruder@example.com")
        assert client.post("/v1/models/mine/download-link?format=stl").status_code == 404

    def test_a_link_cannot_be_minted_for_an_unpaid_model(self, app, client, files, accounts):
        signup(client)
        user = accounts.get_user_by_email("maker@example.com")
        accounts.spend(user["id"], 3, idempotency_key="drain")
        finish_build(app, client, "model-broke", files)
        assert client.post(
            "/v1/models/model-broke/download-link?format=stl"
        ).status_code == 402

    def test_a_forged_or_expired_token_is_refused(self, app, client, files):
        signup(client)
        finish_build(app, client, "model-1", files)
        for token in ("nonsense", "a.b", ""):
            assert client.get(f"/v1/download/{token}").status_code in (403, 404)

    def test_a_link_stops_working_when_the_model_stops_being_paid_for(
        self, app, client, provider, accounts, files
    ):
        """Entitlement is rechecked when the link is redeemed, not only when it
        was minted. A subscription can lapse and a refund can land in the few
        minutes in between, and a token is not a promise about the future."""
        signup(client)
        finish_build(app, client, "model-1", files)
        url = client.post("/v1/models/model-1/download-link?format=stl").json()["url"]
        assert client.get(url).status_code == 200
        # Reach into the ledger to undo the charge, which is what a reversal
        # amounts to as far as entitlement is concerned.
        raw_delete = "DELETE FROM credit_ledger WHERE idempotency_key = ?"
        with accounts._db.transaction() as conn:
            conn.execute(accounts._db.translate(raw_delete), ("spend:model-1",))
        assert client.get(url).status_code == 402

    def test_the_free_gateway_issues_no_links_at_all(self, tmp_path, monkeypatch, files):
        monkeypatch.setenv("FORMFORGE_ALLOW_UNSAFE_SANDBOX", "1")
        open_app = create_app(store_dir=tmp_path / "open", allow_unsafe_sandbox=True)
        with TestClient(open_app) as free:
            assert free.post("/v1/models/x/download-link?format=stl").status_code == 404
            assert free.get("/v1/download/anything").status_code == 404


class TestTheWholeJourney:
    def test_signup_to_export_to_history(self, app, client, files):
        """The flow a first customer actually walks, in one test.

        Each step is covered in isolation above; this exists because the
        interesting failures in a system like this live between the steps --
        a balance that is right after a build but wrong on the account page, a
        model that downloads but never appears in history.
        """
        account = signup(client).json()
        assert account["credits"] == 3 and account["plan"] == "free"

        # Sign out and back in: the session, not the signup, is what carries.
        client.post("/v1/auth/logout")
        client.cookies.clear()
        client.post(
            "/v1/auth/login", json={"email": "maker@example.com", "password": PASSWORD}
        )

        finish_build(app, client, "journey-1", files)

        assert client.get("/v1/models/journey-1/download?format=stl").content == b"stl bytes"

        credits = client.get("/v1/account/credits").json()
        assert credits["credits"] == 2
        assert credits["history"][0]["kind"] == "spend"
        assert credits["history"][0]["change"] == -1

        history = client.get("/v1/account/history").json()["models"]
        assert [(m["model_id"], m["paid"]) for m in history] == [("journey-1", True)]

    def test_upgrade_to_export_journey(self, app, client, provider, accounts, files):
        """The flow that produces revenue: hit the free ceiling, subscribe,
        and get the formats the free tier withheld."""
        signup(client)
        user = accounts.get_user_by_email("maker@example.com")
        for n in range(3):
            finish_build(app, client, f"free-{n}", files)
        assert client.get("/v1/auth/me").json()["credits"] == 0
        assert client.post("/v1/generate", json={"prompt": "one more"}).status_code == 402

        client.post("/v1/billing/checkout", json={"plan": "studio"})
        body = json.dumps({
            "type": "subscription.activated", "id": "evt_up",
            "customer_id": accounts.get_user(user["id"])["billing_customer_id"],
            "plan_id": "studio", "period_start": "2026-09", "created": int(time.time()),
        }).encode()
        client.post(
            "/v1/billing/webhook", content=body,
            headers={"x-formforge-signature": provider.sign(body)},
        )

        me = client.get("/v1/auth/me").json()
        assert (me["plan"], me["credits"], me["batch"]) == ("studio", 300, True)
        finish_build(app, client, "paid-1", files)
        assert client.get("/v1/models/paid-1/download?format=step").status_code == 200


class TestConcurrentSpendOverHttp:
    def test_simultaneous_builds_cannot_overspend_a_balance(self, app, client, files):
        """The same race as the store-level test, but driven through the app's
        own charging path -- because a guarantee that holds in the store and is
        bypassed by the caller is not a guarantee."""
        import threading

        signup(client)
        state = app.state.formforge
        accounts = state["accounts"]
        user = accounts.get_user_by_email("maker@example.com")
        user_id = user["id"]
        assert accounts.balance(user_id) == 3

        attempts = 8
        start = threading.Barrier(attempts)
        errors: list[BaseException] = []

        def build(n: int) -> None:
            try:
                job = state["jobs"].create()
                job.model_id = f"race-{n}"
                job.owner_id = user_id
                state["jobs"].by_model[job.model_id] = job
                result = _Result(job.model_id, files)
                job.result = result
                start.wait(timeout=10)
                state["db"].record_generation(result, user_id=user_id)
                state["charge"](job, result)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=build, args=(n,)) for n in range(attempts)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, f"unexpected failures: {errors!r}"
        assert accounts.balance(user_id) == 0, "a balance must never go negative"

        # Exactly three models were paid for; the rest are kept but unpaid and
        # must not be downloadable.
        paid = [n for n in range(attempts) if accounts.spend_for_model(f"race-{n}")]
        assert len(paid) == 3
        for n in range(attempts):
            expected = 200 if n in paid else 402
            assert client.get(
                f"/v1/models/race-{n}/download?format=stl"
            ).status_code == expected


class TestEveryModelRouteIsOwned:
    """SEC-1. Phase 1 gated the download path and left the rest open.

    An anonymous request could read another account's prompt, parameters and
    generated source; the WebSocket replayed the whole build; and `modify`
    started a fresh generation from somebody else's model with no owner and no
    credit check -- an unauthenticated way to spend the sandbox.

    Model ids are UUID4 and hard to guess. That is not the point: ids leak
    through URLs, logs and referrer headers, and unguessable is not authorised.
    These tests are written as the probe that found it.
    """

    READ_ROUTES = (
        "/v1/models/{id}",
        "/v1/models/{id}/status",
        "/v1/models/{id}/events",
    )

    @pytest.fixture
    def victim_model(self, app, client, files):
        signup(client, "victim@example.com")
        finish_build(app, client, "victim-model", files)
        return "victim-model"

    @pytest.mark.parametrize("route", READ_ROUTES)
    def test_an_anonymous_stranger_is_refused(self, client, victim_model, route):
        client.cookies.clear()
        response = client.get(route.format(id=victim_model))
        assert response.status_code == 401
        assert "prompt" not in response.text
        assert "source_code" not in response.text

    @pytest.mark.parametrize("route", READ_ROUTES)
    def test_another_signed_in_account_is_refused(self, client, victim_model, route):
        client.post("/v1/auth/logout")
        client.cookies.clear()
        signup(client, "intruder@example.com")
        response = client.get(route.format(id=victim_model))
        # 404, not 403: a 403 confirms the id names something real.
        assert response.status_code == 404
        assert "prompt" not in response.text

    @pytest.mark.parametrize("route", READ_ROUTES)
    def test_the_owner_still_gets_their_own_model(self, client, victim_model, route):
        assert client.get(route.format(id=victim_model)).status_code == 200

    def test_modify_is_refused_anonymously(self, client, victim_model):
        client.cookies.clear()
        response = client.post(
            f"/v1/models/{victim_model}/modify", json={"param_changes": {"x": 1}}
        )
        assert response.status_code == 401

    def test_modify_is_refused_for_another_account(self, client, victim_model):
        client.post("/v1/auth/logout")
        client.cookies.clear()
        signup(client, "intruder@example.com")
        response = client.post(
            f"/v1/models/{victim_model}/modify", json={"param_changes": {"x": 1}}
        )
        assert response.status_code == 404

    def test_modify_needs_a_credit_like_any_other_build(
        self, app, client, files, accounts
    ):
        """It runs the sandbox and produces a downloadable model, so it is a
        build. Before this it was neither owned nor charged."""
        signup(client)
        finish_build(app, client, "mine", files)
        user = accounts.get_user_by_email("maker@example.com")
        accounts.spend(user["id"], accounts.balance(user["id"]), idempotency_key="drain")
        response = client.post(
            "/v1/models/mine/modify", json={"param_changes": {"x": 1}}
        )
        assert response.status_code == 402

    def test_slice_is_refused_for_another_account(self, client, victim_model):
        client.post("/v1/auth/logout")
        client.cookies.clear()
        signup(client, "intruder@example.com")
        assert client.post(
            f"/v1/models/{victim_model}/slice", json={}
        ).status_code == 404

    def test_the_event_stream_is_refused_for_another_account(self, client, victim_model):
        """The socket replays the prompt, the parameters and the validator's
        findings, so it needs the same rule as the HTTP routes."""
        from starlette.websockets import WebSocketDisconnect as WSDisconnect

        client.post("/v1/auth/logout")
        client.cookies.clear()
        signup(client, "intruder@example.com")
        with (
            pytest.raises(WSDisconnect) as caught,
            client.websocket_connect(f"/v1/models/{victim_model}/stream") as ws,
        ):
            ws.receive_json()
            ws.receive_json()
        assert caught.value.code == 1008

    def test_the_owner_can_still_stream_their_own_build(self, client, victim_model):
        with client.websocket_connect(f"/v1/models/{victim_model}/stream") as ws:
            assert "error" not in ws.receive_json()

    def test_business_aggregates_are_not_public_on_a_metered_deployment(
        self, client, victim_model
    ):
        """Totals, per-template health and cost belong to whoever runs the
        instance, not to whoever can reach it."""
        for path in ("/v1/stats", "/v1/stats/prints"):
            assert client.get(path).status_code == 404, path

    def test_an_ownerless_model_is_refused_rather_than_shared(self, app, client, files):
        """On a metered deployment a model with no owner is a bug, and the
        safe reading of a bug is no."""
        signup(client)
        state = app.state.formforge
        job = state["jobs"].create()
        job.model_id = "orphan"
        job.owner_id = None
        job.result = _Result("orphan", files)
        job.status = "ok"
        state["jobs"].by_model["orphan"] = job
        assert client.get("/v1/models/orphan").status_code == 404


class TestArtifactsGoThroughStorage:
    """STO-1. The storage abstraction existed through Phase 1 and nothing
    imported it: artifacts were served straight off local paths, with no
    record of what existed, whose it was, or whether it was still there."""

    def test_a_finished_build_is_recorded_in_the_artifact_store(
        self, app, client, files, accounts
    ):
        signup(client)
        finish_build(app, client, "model-1", files)
        state = app.state.formforge
        # `finish_build` stands in for the background task, so record the
        # artifacts the way the real path does.
        for fmt, path in files.items():
            key = f"models/model-1/{fmt}"
            state["artifacts"].put(key, __import__("pathlib").Path(path))
            accounts.record_artifact(
                "model-1", fmt, key,
                user_id=accounts.get_user_by_email("maker@example.com")["id"],
            )
        rows = accounts.artifacts_for("model-1")
        assert {r["fmt"] for r in rows} == set(files)
        assert all(r["status"] == "present" for r in rows)

    def test_a_deleted_artifact_answers_410_rather_than_404(
        self, app, client, files, accounts
    ):
        """The model existed and the file is gone. That is a different thing
        from never having had one, and the difference is what a support
        conversation turns on."""
        signup(client)
        finish_build(app, client, "model-1", files)
        user = accounts.get_user_by_email("maker@example.com")
        key = "models/model-1/stl"
        state = app.state.formforge
        state["artifacts"].put(key, __import__("pathlib").Path(files["stl"]))
        accounts.record_artifact("model-1", "stl", key, user_id=user["id"])
        assert client.get("/v1/models/model-1/download?format=stl").status_code == 200

        accounts.mark_artifacts("model-1")
        row = accounts.artifacts_pending_delete()[0]
        state["artifacts"].delete(row["storage_key"])
        accounts.finish_artifact_delete(row["id"])

        gone = client.get("/v1/models/model-1/download?format=stl")
        assert gone.status_code == 410
        assert b"stl bytes" not in gone.content

    def test_a_missing_stored_file_falls_back_rather_than_failing(
        self, app, client, files, accounts
    ):
        """A storage hiccup must not lose a model the bundle directory still
        has."""
        signup(client)
        finish_build(app, client, "model-1", files)
        user = accounts.get_user_by_email("maker@example.com")
        # Recorded as present, but never actually written to storage.
        accounts.record_artifact("model-1", "stl", "models/model-1/stl", user_id=user["id"])
        response = client.get("/v1/models/model-1/download?format=stl")
        assert response.status_code == 200
        assert response.content == b"stl bytes"

    def test_a_closed_account_loses_access_immediately(
        self, app, client, files, accounts
    ):
        signup(client)
        finish_build(app, client, "model-1", files)
        assert client.get("/v1/models/model-1/download?format=stl").status_code == 200
        accounts.close_account(accounts.get_user_by_email("maker@example.com")["id"])
        assert client.get("/v1/models/model-1/download?format=stl").status_code == 401
        assert client.get("/v1/auth/me").status_code == 401
