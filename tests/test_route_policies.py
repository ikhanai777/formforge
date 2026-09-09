"""Every route has a declared policy, and the declaration is enforced.

Two bugs of the same shape got through: Phase 1 gated the download path and
left the model reads open; Phase 2 gated everything with an id in the *path*
and missed `POST /v1/feedback`, which takes one in the body. Both times a route
existed that nobody had decided the policy for.

So this file works from `formforge.api.policies.POLICIES` rather than from
reading the handlers:

* a route with no entry fails, so a new one cannot be added without deciding;
* an entry for a route that no longer exists fails, so the registry cannot rot;
* every entry claiming ownership is *driven as a real request* -- anonymous,
  cross-account, closed account, expired session, tampered identifier.

The last part is what stops the registry becoming a comment that used to be
true.
"""

from __future__ import annotations

import pytest

from formforge.accounts import OfflineProvider
from formforge.api.policies import (
    NEEDS_OWNERSHIP,
    NEEDS_SESSION,
    POLICIES,
    Policy,
    registered_routes,
    stale,
    undeclared,
)

fastapi = pytest.importorskip("fastapi", reason="needs the `api` extra")
from fastapi.testclient import TestClient  # noqa: E402

from formforge.api.app import create_app  # noqa: E402

PASSWORD = "correct-horse-battery-staple"
OWNED_MODEL = "owned-model"


class _Result:
    def __init__(self, model_id, artifacts):
        self.model_id = model_id
        self.status = "ok"
        self.prompt = "a vase"
        self.template_id = "vessel_vase"
        self.route = "template"
        self.iterations = 1
        self.stats = {"triangles": 12}
        self.validation = {"measurements": {}, "warnings": []}
        self.events = []
        self.artifacts = artifacts
        self.intent = {}
        self.params = {}
        self.source_code = "x = 1"
        self.language = "build123d"
        self.ok = True
        self.message = ""
        self.usage = None
        self.duration_ms = 1

    def as_dict(self):
        return {"model_id": self.model_id, "prompt": self.prompt}


@pytest.fixture
def app(accounts, tmp_path, monkeypatch):
    monkeypatch.setenv("FORMFORGE_ALLOW_UNSAFE_SANDBOX", "1")
    import formforge.api.security as security

    monkeypatch.setattr(security, "COOKIE_INSECURE", True)
    return create_app(
        store_dir=tmp_path / "store",
        accounts=accounts,
        billing_provider=OfflineProvider(secret="s"),
        allow_unsafe_sandbox=True,
    )


@pytest.fixture
def client(app):
    with TestClient(app) as made:
        yield made


@pytest.fixture
def owner(app, client, tmp_path):
    """An account with one model that it owns, and a live session."""
    response = client.post(
        "/v1/auth/signup", json={"email": "owner@example.com", "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    state = app.state.formforge
    account = state["accounts"].get_user_by_email("owner@example.com")

    stl = tmp_path / "model.stl"
    stl.write_text("stl bytes")
    job = state["jobs"].create()
    job.model_id = OWNED_MODEL
    job.owner_id = account["id"]
    result = _Result(OWNED_MODEL, {"stl": str(stl), "report": str(stl)})
    job.result = result
    job.status = "ok"
    state["jobs"].by_model[OWNED_MODEL] = job
    state["db"].record_generation(result, user_id=account["id"])
    state["charge"](job, result)
    return account


def request_for(client, entry, model_id=OWNED_MODEL):
    """Drive one declared route, filling in whatever identifier it takes."""
    path = entry.path.replace("{model_id}", model_id).replace(
        "{token}", "not-a-real-token"
    ).replace("{template_id}", "vessel_vase")
    # STL because the free plan allows it; the default 3mf would answer 403
    # for a plan reason and hide the authorisation answer under it.
    if path.endswith("/download") or path.endswith("/download-link"):
        path += "?format=stl"
    body = None
    if entry.location == "body" and entry.resource == "model_id":
        body = {"model_id": model_id, "printed": True, "success": True}
    elif entry.method == "POST":
        # A *valid* body throughout. FastAPI validates before the handler
        # runs, so an invalid one answers 422 and never reaches the guard --
        # which would make this test prove nothing about the guard.
        if "modify" in path:
            body = {"param_changes": {}}
        elif path == "/v1/generate":
            body = {"prompt": "a vase"}
        elif "checkout" in path:
            body = {"plan": "maker"}
        elif "reset/request" in path:
            body = {"email": "someone@example.com"}
        elif "reset/confirm" in path:
            body = {"token": "x" * 40, "password": PASSWORD}
        elif "signup" in path or "login" in path:
            body = {"email": "someone@example.com", "password": PASSWORD}
        else:
            body = {}
    if entry.method == "WEBSOCKET":
        return None  # exercised separately; see TestTheStream
    return client.request(entry.method, path, json=body)


OWNED = [e for e in POLICIES if e.policy in NEEDS_OWNERSHIP and e.method != "WEBSOCKET"]
SESSIONED = [
    e for e in POLICIES
    if e.policy in NEEDS_SESSION and e.method != "WEBSOCKET"
]


class TestTheRegistryMatchesReality:
    def test_no_route_is_served_without_a_declared_policy(self, app):
        """The check that would have caught both bugs. A new route with no
        entry fails here rather than shipping unguarded."""
        missing = undeclared(app)
        assert not missing, (
            "these routes have no policy in formforge/api/policies.py: "
            f"{sorted(missing)}"
        )

    def test_the_registry_has_no_entries_for_routes_that_are_gone(self, app):
        """A registry describing a renamed route is one somebody will trust
        the rest of."""
        orphans = stale(app)
        assert not orphans, f"declared but not served: {sorted(orphans)}"

    def test_every_route_is_covered_exactly_once(self, app):
        paths = [(e.method, e.path) for e in POLICIES]
        assert len(paths) == len(set(paths)), "a route is declared twice"
        assert set(paths) == registered_routes(app)

    def test_the_walk_finds_routes_contributed_by_a_router(self, app):
        """A guard on the guard.

        The first version of `registered_routes` walked `app.routes` flatly.
        This FastAPI version inserts a wrapper object for an included router,
        so that walk saw the wrapper, found no path, and skipped all ten
        account and billing routes -- reporting full coverage while an entire
        area went unchecked. That is the exact failure this module exists to
        prevent, so it gets its own test rather than being implied.
        """
        served = {path for _, path in registered_routes(app)}
        for path in ("/v1/auth/signup", "/v1/auth/me", "/v1/billing/webhook",
                     "/v1/account/credits"):
            assert path in served, f"the route walk cannot see {path}"

    def test_anything_resolving_an_identifier_is_owner_or_signed(self):
        """The rule the two bugs broke: if a route takes somebody's id, a
        session alone is not enough."""
        for entry in POLICIES:
            if entry.resource and entry.resource != "template_id":
                assert entry.policy in (Policy.OWNER, Policy.SIGNED_TOKEN), (
                    f"{entry.method} {entry.path} resolves {entry.resource} "
                    f"under {entry.policy}"
                )


class TestAnonymousIsRefused:
    @pytest.mark.parametrize("entry", SESSIONED, ids=lambda e: f"{e.method} {e.path}")
    def test_no_session_is_refused(self, client, owner, entry):
        client.cookies.clear()
        response = request_for(client, entry)
        assert response.status_code in (401, 404), (
            f"{entry.method} {entry.path} answered {response.status_code} "
            "to a caller with no session"
        )
        assert "prompt" not in response.text
        assert "source_code" not in response.text


class TestCrossAccountIsRefused:
    @pytest.mark.parametrize("entry", OWNED, ids=lambda e: f"{e.method} {e.path}")
    def test_another_account_gets_404_not_403(self, client, owner, entry):
        """404 rather than 403: a 403 confirms the id names something real,
        which turns every one of these into an enumeration oracle."""
        client.post("/v1/auth/logout")
        client.cookies.clear()
        client.post(
            "/v1/auth/signup",
            json={"email": "intruder@example.com", "password": PASSWORD},
        )
        response = request_for(client, entry)
        assert response.status_code == 404, (
            f"{entry.method} {entry.path} answered {response.status_code} "
            "to a different account"
        )
        assert "stl bytes" not in response.text

    @pytest.mark.parametrize("entry", OWNED, ids=lambda e: f"{e.method} {e.path}")
    def test_a_tampered_identifier_is_refused(self, client, owner, entry):
        """Substituting an id that belongs to nobody must not answer
        differently from one that belongs to someone else."""
        response = request_for(client, entry, model_id="../../etc/passwd")
        assert response.status_code in (404, 422)


class TestClosedAndExpiredSessions:
    @pytest.mark.parametrize("entry", SESSIONED, ids=lambda e: f"{e.method} {e.path}")
    def test_a_closed_account_is_refused(self, client, app, owner, entry):
        app.state.formforge["accounts"].close_account(owner["id"])
        response = request_for(client, entry)
        assert response.status_code in (401, 404)

    @pytest.mark.parametrize("entry", SESSIONED, ids=lambda e: f"{e.method} {e.path}")
    def test_an_expired_session_is_refused(self, client, app, owner, entry):
        accounts = app.state.formforge["accounts"]
        from datetime import UTC, datetime

        past = accounts._db.stamp(datetime(2000, 1, 1, tzinfo=UTC))
        with accounts._db.transaction() as conn:
            conn.execute(
                accounts._db.translate(
                    "UPDATE sessions SET expires_at = ? WHERE user_id = ?"
                ),
                (past, owner["id"]),
            )
        response = request_for(client, entry)
        assert response.status_code in (401, 404)

    @pytest.mark.parametrize("entry", SESSIONED, ids=lambda e: f"{e.method} {e.path}")
    def test_a_revoked_session_is_refused(self, client, app, owner, entry):
        app.state.formforge["accounts"].revoke_all_sessions(owner["id"])
        response = request_for(client, entry)
        assert response.status_code in (401, 404)


class TestTheOwnerStillWorks:
    """The other half. A guard that refuses everybody is not a guard."""

    @pytest.mark.parametrize(
        "entry", [e for e in OWNED if e.method == "GET"],
        ids=lambda e: f"{e.method} {e.path}",
    )
    def test_the_owner_is_allowed(self, client, owner, entry):
        response = request_for(client, entry)
        assert response.status_code == 200, (
            f"{entry.method} {entry.path} refused its own owner: {response.text[:120]}"
        )


class TestTheStream:
    """The WebSocket, which `request_for` cannot drive."""

    def test_a_stranger_is_closed_out(self, client, owner):
        from starlette.websockets import WebSocketDisconnect

        client.post("/v1/auth/logout")
        client.cookies.clear()
        client.post(
            "/v1/auth/signup",
            json={"email": "intruder@example.com", "password": PASSWORD},
        )
        with (
            pytest.raises(WebSocketDisconnect) as caught,
            client.websocket_connect(f"/v1/models/{OWNED_MODEL}/stream") as ws,
        ):
            ws.receive_json()
            ws.receive_json()
        assert caught.value.code == 1008

    def test_the_owner_is_let_through(self, client, owner):
        with client.websocket_connect(f"/v1/models/{OWNED_MODEL}/stream") as ws:
            assert "error" not in ws.receive_json()


class TestCreditConsumingMutations:
    """Every mutation that spends a credit checks all three server-side:
    authenticated, owned where applicable, and affordable."""

    @pytest.mark.parametrize(
        "entry", [e for e in POLICIES if e.consumes_credit],
        ids=lambda e: f"{e.method} {e.path}",
    )
    def test_it_needs_a_session(self, client, owner, entry):
        client.cookies.clear()
        response = request_for(client, entry)
        assert response.status_code in (401, 404)

    @pytest.mark.parametrize(
        "entry", [e for e in POLICIES if e.consumes_credit],
        ids=lambda e: f"{e.method} {e.path}",
    )
    def test_it_refuses_when_the_balance_is_empty(self, client, app, owner, entry):
        accounts = app.state.formforge["accounts"]
        balance = accounts.balance(owner["id"])
        if balance:
            accounts.spend(owner["id"], balance, idempotency_key="drain")
        body = {"prompt": "a vase"} if entry.path == "/v1/generate" else {
            "param_changes": {"x": 1}
        }
        response = client.request(
            entry.method, entry.path.replace("{model_id}", OWNED_MODEL), json=body
        )
        assert response.status_code == 402, (
            f"{entry.method} {entry.path} did not refuse an empty balance"
        )

    def test_the_registry_lists_every_credit_spender(self):
        """If a new build route appears, it belongs here. Stated as a test so
        the list is checked rather than remembered."""
        spenders = {e.path for e in POLICIES if e.consumes_credit}
        assert spenders == {"/v1/generate", "/v1/models/{model_id}/modify"}


class TestOperatorRoutesAreNotServedWhenMetered:
    @pytest.mark.parametrize(
        "entry", [e for e in POLICIES if e.policy is Policy.OPERATOR_ONLY],
        ids=lambda e: e.path,
    )
    def test_they_answer_404(self, client, owner, entry):
        assert client.get(entry.path).status_code == 404


class TestTheFreeGatewayIsUnchanged:
    """None of this applies without an account store, and that is the point:
    the CLI, the MCP server and a self-hosted copy stay unmetered."""

    @pytest.fixture
    def open_app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORMFORGE_ALLOW_UNSAFE_SANDBOX", "1")
        return create_app(store_dir=tmp_path / "open", allow_unsafe_sandbox=True)

    def test_no_account_routes_exist(self, open_app):
        served = {path for _, path in registered_routes(open_app)}
        for path in ("/v1/auth/signup", "/v1/auth/me", "/v1/billing/webhook"):
            assert path not in served

    def test_the_operator_routes_are_open_there(self, open_app):
        with TestClient(open_app) as client:
            for path in ("/v1/stats", "/v1/meta"):
                assert client.get(path).status_code == 200, path

    def test_feedback_needs_no_session(self, open_app):
        """SEC-2 tightened the metered path. Self-hosted, the person posting
        feedback runs the instance, and requiring a login there would break
        the CLI's `formforge feedback`."""
        with TestClient(open_app) as client:
            response = client.post(
                "/v1/feedback", json={"model_id": "nope", "printed": True}
            )
            assert response.status_code == 404  # no such model, not "not signed in"
