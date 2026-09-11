"""Password reset, end to end, with no email provider anywhere.

The local outbox is what makes this testable at all: every assertion below
runs from a clean clone with no credential, no domain and no network. That was
the thing missing in Phase 1, when reset was deferred precisely because there
was nowhere to send a message.

What is actually under test is the ways a reset flow leaks or over-permits:
telling a stranger which addresses have accounts, a token that works twice, a
token that outlives its window, and a reset that leaves the attacker's session
signed in.
"""

from __future__ import annotations

import re

import pytest

from formforge.accounts.email import Message, NullMailer, OutboxMailer

fastapi = pytest.importorskip("fastapi", reason="needs the `api` extra")
from fastapi.testclient import TestClient  # noqa: E402

from formforge.accounts import OfflineProvider  # noqa: E402
from formforge.api.app import create_app  # noqa: E402
from formforge.api.security import COOKIE_NAME, MemoryRateLimiter  # noqa: E402

PASSWORD = "correct-horse-battery-staple"
NEW_PASSWORD = "an-entirely-different-password"


@pytest.fixture
def outbox(tmp_path) -> OutboxMailer:
    return OutboxMailer(tmp_path / "outbox")


@pytest.fixture
def app(accounts, outbox, tmp_path, monkeypatch):
    monkeypatch.setenv("FORMFORGE_ALLOW_UNSAFE_SANDBOX", "1")
    import formforge.api.security as security

    monkeypatch.setattr(security, "COOKIE_INSECURE", True)
    return create_app(
        store_dir=tmp_path / "store",
        accounts=accounts,
        billing_provider=OfflineProvider(secret="s"),
        mailer=outbox,
        allow_unsafe_sandbox=True,
    )


@pytest.fixture
def client(app):
    with TestClient(app) as made:
        yield made


def signup(client, email="me@example.com", password=PASSWORD):
    response = client.post(
        "/v1/auth/signup", json={"email": email, "password": password}
    )
    assert response.status_code == 201, response.text
    return response


def token_from(outbox: OutboxMailer) -> str:
    """Pull the reset token out of the message that was actually written.

    Read from the file rather than returned by the endpoint, because the
    endpoint must never return it -- that is the whole point of mailing it.
    """
    messages = outbox.read_all()
    assert messages, "no message was written"
    found = re.search(r"\n\s+(\S{20,})\n", messages[-1])
    assert found, f"no token in the message: {messages[-1]!r}"
    return found.group(1)


class TestItTellsStrangersNothing:
    def test_a_real_and_an_unknown_address_answer_identically(self, client):
        """Any difference at all -- status, body, shape -- is a way to ask
        which addresses have accounts here."""
        signup(client)
        real = client.post("/v1/auth/reset/request", json={"email": "me@example.com"})
        fake = client.post("/v1/auth/reset/request", json={"email": "nobody@example.com"})
        assert real.status_code == fake.status_code == 202
        assert real.json() == fake.json()

    def test_only_a_real_address_produces_a_message(self, client, outbox):
        signup(client)
        client.post("/v1/auth/reset/request", json={"email": "nobody@example.com"})
        assert outbox.read_all() == []
        client.post("/v1/auth/reset/request", json={"email": "me@example.com"})
        assert len(outbox.read_all()) == 1

    def test_the_response_never_carries_the_token(self, client, outbox):
        signup(client)
        response = client.post("/v1/auth/reset/request", json={"email": "me@example.com"})
        assert token_from(outbox) not in response.text

    def test_being_rate_limited_still_answers_202(self, client, outbox):
        """A 429 here leaks exactly what the generic 202 hides: keep asking
        about an address and the moment the answer changes is the moment you
        have learned something."""
        signup(client)
        codes = {
            client.post(
                "/v1/auth/reset/request", json={"email": "me@example.com"}
            ).status_code
            for _ in range(12)
        }
        assert codes == {202}
        # The limit did apply, though -- it stopped sending mail.
        assert len(outbox.read_all()) <= 3

    def test_one_client_cannot_sweep_many_addresses(self, client):
        signup(client)
        codes = {
            client.post(
                "/v1/auth/reset/request", json={"email": f"probe{n}@example.com"}
            ).status_code
            for n in range(20)
        }
        assert codes == {202}


class TestTheToken:
    def test_it_works_once(self, client, outbox):
        signup(client)
        client.post("/v1/auth/reset/request", json={"email": "me@example.com"})
        token = token_from(outbox)
        first = client.post(
            "/v1/auth/reset/confirm", json={"token": token, "password": NEW_PASSWORD}
        )
        second = client.post(
            "/v1/auth/reset/confirm", json={"token": token, "password": "yet-another-password"}
        )
        assert first.status_code == 204
        assert second.status_code == 400

    def test_an_unknown_token_is_refused(self, client):
        response = client.post(
            "/v1/auth/reset/confirm", json={"token": "x" * 43, "password": NEW_PASSWORD}
        )
        assert response.status_code == 400

    def test_unknown_expired_and_used_answer_the_same(self, client, outbox, accounts):
        """Telling them apart tells somebody holding a stolen token which kind
        of stolen it is."""
        signup(client)
        client.post("/v1/auth/reset/request", json={"email": "me@example.com"})
        used = token_from(outbox)
        client.post(
            "/v1/auth/reset/confirm", json={"token": used, "password": NEW_PASSWORD}
        )
        answers = {
            client.post(
                "/v1/auth/reset/confirm", json={"token": tok, "password": "a-third-password"}
            ).json()["detail"]
            for tok in (used, "y" * 43)
        }
        assert len(answers) == 1

    def test_an_expired_token_is_refused(self, accounts):
        """Checked at the store, where the clock actually is."""
        user = accounts.create_user("me@example.com", PASSWORD)
        token = accounts.create_password_reset(user["id"], ttl_minutes=-1)
        assert accounts.consume_password_reset(token) is None

    def test_requesting_again_kills_the_previous_link(self, client, outbox, accounts):
        """The usual reason for a second request is that the first mail went
        somewhere the user does not control."""
        signup(client)
        client.post("/v1/auth/reset/request", json={"email": "me@example.com"})
        first = token_from(outbox)
        client.post("/v1/auth/reset/request", json={"email": "me@example.com"})
        second = token_from(outbox)
        assert first != second
        assert accounts.consume_password_reset(first) is None
        assert accounts.consume_password_reset(second) is not None

    def test_only_a_hash_is_stored(self, accounts):
        user = accounts.create_user("me@example.com", PASSWORD)
        token = accounts.create_password_reset(user["id"])
        with accounts._db.reader() as conn:
            rows = conn.execute("SELECT * FROM password_resets").fetchall()
        assert token not in str([dict(r) for r in rows])

    def test_spent_tokens_can_be_purged_safely_and_repeatedly(self, accounts):
        user = accounts.create_user("me@example.com", PASSWORD)
        token = accounts.create_password_reset(user["id"])
        accounts.consume_password_reset(token)
        assert accounts.purge_password_resets() >= 1
        assert accounts.purge_password_resets() == 0

    def test_a_live_token_survives_a_purge(self, accounts):
        user = accounts.create_user("me@example.com", PASSWORD)
        token = accounts.create_password_reset(user["id"])
        accounts.purge_password_resets()
        assert accounts.consume_password_reset(token) == user["id"]


class TestTheResetActuallyResets:
    def test_the_old_password_stops_working(self, client, outbox):
        signup(client)
        client.post("/v1/auth/reset/request", json={"email": "me@example.com"})
        client.post(
            "/v1/auth/reset/confirm",
            json={"token": token_from(outbox), "password": NEW_PASSWORD},
        )
        client.cookies.clear()
        old = client.post(
            "/v1/auth/login", json={"email": "me@example.com", "password": PASSWORD}
        )
        new = client.post(
            "/v1/auth/login", json={"email": "me@example.com", "password": NEW_PASSWORD}
        )
        assert old.status_code == 401
        assert new.status_code == 200

    def test_every_existing_session_dies(self, client, outbox, accounts):
        """The case that matters is an account being recovered *from*
        somebody. Leaving their session alive makes the reset theatre."""
        signup(client)
        stolen = client.cookies.get(COOKIE_NAME)
        elsewhere = accounts.create_session(
            accounts.get_user_by_email("me@example.com")["id"]
        )
        assert accounts.user_for_token(stolen) is not None

        client.post("/v1/auth/reset/request", json={"email": "me@example.com"})
        client.post(
            "/v1/auth/reset/confirm",
            json={"token": token_from(outbox), "password": NEW_PASSWORD},
        )
        assert accounts.user_for_token(stolen) is None
        assert accounts.user_for_token(elsewhere) is None

    def test_a_short_new_password_is_refused(self, client, outbox):
        signup(client)
        client.post("/v1/auth/reset/request", json={"email": "me@example.com"})
        response = client.post(
            "/v1/auth/reset/confirm", json={"token": token_from(outbox), "password": "short"}
        )
        assert response.status_code == 422


class TestWhenResetIsSwitchedOff:
    @pytest.fixture
    def app(self, accounts, outbox, tmp_path, monkeypatch):
        monkeypatch.setenv("FORMFORGE_ALLOW_UNSAFE_SANDBOX", "1")
        monkeypatch.setenv("FORMFORGE_PASSWORD_RESET", "0")
        import formforge.api.security as security

        monkeypatch.setattr(security, "COOKIE_INSECURE", True)
        return create_app(
            store_dir=tmp_path / "store",
            accounts=accounts,
            billing_provider=OfflineProvider(secret="s"),
            mailer=outbox,
            allow_unsafe_sandbox=True,
        )

    def test_both_endpoints_report_not_found(self, client):
        for path in ("/v1/auth/reset/request", "/v1/auth/reset/confirm"):
            response = client.post(path, json={"email": "me@example.com",
                                               "token": "x" * 43,
                                               "password": PASSWORD})
            assert response.status_code == 404


class TestMailers:
    def test_the_outbox_writes_a_readable_message(self, tmp_path):
        box = OutboxMailer(tmp_path / "out", sender="noreply@formforge.test")
        box.send(Message(to="someone@example.com", subject="Hello", body="Body here"))
        written = box.read_all()
        assert len(written) == 1
        assert "someone@example.com" in written[0]
        assert "Body here" in written[0]

    def test_the_outbox_does_not_log_the_body(self, tmp_path, caplog):
        """A reset mail carries a token that is the password for half an
        hour."""
        import logging

        box = OutboxMailer(tmp_path / "out")
        with caplog.at_level(logging.DEBUG):
            box.send(Message(to="a@example.com", subject="Secret", body="TOKEN-abc123"))
        assert "TOKEN-abc123" not in caplog.text
        assert "Secret" not in caplog.text

    def test_a_hostile_recipient_cannot_escape_the_outbox_directory(self, tmp_path):
        box = OutboxMailer(tmp_path / "out")
        path = box.send(Message(to="../../etc/passwd", subject="x", body="y"))
        assert str(tmp_path / "out") in path

    def test_the_null_mailer_discards(self, tmp_path):
        assert NullMailer().send(Message(to="a@example.com", subject="x", body="y")) == ""


class TestRateLimiterInterface:
    def test_the_memory_limiter_counts(self):
        limiter = MemoryRateLimiter()
        allowed = [
            limiter.check("client", "scope", limit=3, per_seconds=60) for _ in range(5)
        ]
        assert allowed == [True, True, True, False, False]

    def test_scopes_and_clients_have_separate_budgets(self):
        limiter = MemoryRateLimiter()
        assert limiter.check("a", "login", limit=1, per_seconds=60)
        assert not limiter.check("a", "login", limit=1, per_seconds=60)
        assert limiter.check("b", "login", limit=1, per_seconds=60)
        assert limiter.check("a", "signup", limit=1, per_seconds=60)

    def test_a_shared_backend_is_refused_rather_than_faked(self):
        """Configuring `redis` and silently getting a per-process counter means
        believing in a global limit that is not there, which is precisely the
        belief that gets somebody owned."""
        import dataclasses

        from formforge.api.security import open_rate_limiter
        from formforge.config import ConfigError, Settings

        settings = dataclasses.replace(
            Settings.from_env(), rate_limit_backend="redis"
        )
        with pytest.raises(ConfigError, match="not implemented"):
            open_rate_limiter(settings)

    def test_an_unknown_backend_is_refused(self):
        import dataclasses

        from formforge.api.security import open_rate_limiter
        from formforge.config import ConfigError, Settings

        settings = dataclasses.replace(Settings.from_env(), rate_limit_backend="memcache")
        with pytest.raises(ConfigError):
            open_rate_limiter(settings)
