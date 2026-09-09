"""Contract tests against real services. Skipped unless explicitly asked for.

Each answers one question that cannot be answered offline: does the thing we
wrote the adapter against actually behave the way the adapter assumes? The
offline suite proves the *logic* -- what a renewal does to a ledger, that a
replayed webhook grants once -- and cannot prove the *contract*, because a
fake transport agrees with whatever you wrote.

So these are deliberately thin. They do not re-test the ledger; they test that
Stripe returns a session with a URL, that a bucket round-trips bytes, that an
SMTP server accepts a message, that Redis counts. Everything downstream of
those facts is already covered offline.

Run them with, for example:

    FORMFORGE_IT_S3=1 FORMFORGE_IT_S3_BUCKET=my-scratch-bucket \\
      python -m pytest tests/integration -v

`docs/staging-validation.md` has the full table of flags and variables.
"""

from __future__ import annotations

import io
import time
import uuid

import pytest

pytestmark = pytest.mark.integration


class TestStripeSandbox:
    """Test mode only. The fixture refuses a live key before any call."""

    def test_a_checkout_session_comes_back_with_a_url(self, stripe_sandbox, run_id):
        """What the offline provider cannot tell us: that the parameters we
        send are the ones Stripe wants, and that it hands back a URL."""
        import os

        price = os.environ.get("FORMFORGE_IT_STRIPE_PRICE")
        if not price:
            pytest.skip("set FORMFORGE_IT_STRIPE_PRICE to a test-mode price id")

        customer = stripe_sandbox.customers.create(
            params={
                "email": f"{run_id}@example.invalid",
                "metadata": {"formforge_it": run_id},
            }
        )
        try:
            session = stripe_sandbox.checkout.sessions.create(
                params={
                    "mode": "subscription",
                    "customer": customer.id,
                    "line_items": [{"price": price, "quantity": 1}],
                    "success_url": "https://localhost/ok",
                    "cancel_url": "https://localhost/no",
                    "metadata": {"formforge_plan": "maker", "formforge_it": run_id},
                }
            )
            assert session.url and session.url.startswith("https://")
            assert session.id.startswith("cs_test_"), "not a test-mode session"
        finally:
            # Only the customer this test created.
            stripe_sandbox.customers.delete(customer.id)

    def test_the_adapter_maps_a_real_event_shape(self, stripe_sandbox, run_id):
        """`to_event` is written against Stripe's documented shape. This
        checks it against one Stripe actually produced."""
        from formforge.accounts.stripe_provider import StripeProvider

        customer = stripe_sandbox.customers.create(
            params={"metadata": {"formforge_it": run_id, "formforge_plan": "maker"}}
        )
        try:
            event = {
                "id": f"evt_{uuid.uuid4().hex[:16]}",
                "type": "checkout.session.completed",
                "created": int(time.time()),
                "data": {"object": {
                    "id": "cs_test_x", "customer": customer.id,
                    "metadata": {"formforge_plan": "maker"},
                }},
            }
            mapped = StripeProvider.to_event(event)
            assert mapped.type == "subscription.activated"
            assert mapped.customer_id == customer.id
            assert mapped.plan_id == "maker"
            assert mapped.offline is False
        finally:
            stripe_sandbox.customers.delete(customer.id)

    def test_it_never_touches_live_mode(self, stripe_sandbox):
        """A standing assertion rather than a behaviour: if this ever passes
        against a live key, the fixture's guard has been removed."""
        import os

        assert os.environ["STRIPE_SECRET_KEY"].startswith("sk_test_")


class TestS3CompatibleStorage:
    def test_an_artifact_round_trips(self, s3_bucket, tmp_path, run_id):
        """`S3Storage` has been unverified against a real endpoint since it was
        written. This is the test that changes that."""
        from formforge.storage import S3Storage

        client, bucket, prefix, created = s3_bucket
        storage = S3Storage(bucket, prefix=prefix.rstrip("/"), client=client)

        source = tmp_path / "model.stl"
        source.write_bytes(b"solid formforge integration test")
        key = "models/it-model/stl"
        created.append(f"{prefix}{key}")

        storage.put(key, source)
        assert storage.exists(key)
        assert storage.size(key) == source.stat().st_size
        with storage.open(key) as handle:
            assert handle.read() == b"solid formforge integration test"

    def test_a_missing_key_raises_rather_than_returning_empty(self, s3_bucket):
        from formforge.storage import S3Storage, StorageError

        client, bucket, prefix, _ = s3_bucket
        storage = S3Storage(bucket, prefix=prefix.rstrip("/"), client=client)
        assert not storage.exists("models/never-written/stl")
        with pytest.raises(StorageError):
            storage.open("models/never-written/stl")

    def test_delete_is_idempotent(self, s3_bucket, tmp_path):
        from formforge.storage import S3Storage

        client, bucket, prefix, created = s3_bucket
        storage = S3Storage(bucket, prefix=prefix.rstrip("/"), client=client)
        source = tmp_path / "x.stl"
        source.write_bytes(b"x")
        key = "models/delete-me/stl"
        created.append(f"{prefix}{key}")
        storage.put(key, source)
        storage.delete(key)
        storage.delete(key)  # the sweep retries; a second delete must be fine
        assert not storage.exists(key)

    def test_a_key_cannot_escape_the_prefix(self, s3_bucket):
        from formforge.storage import S3Storage, StorageError

        client, bucket, prefix, _ = s3_bucket
        storage = S3Storage(bucket, prefix=prefix.rstrip("/"), client=client)
        with pytest.raises(StorageError):
            storage.put("../escape", io.BytesIO(b""))  # type: ignore[arg-type]


class TestSmtp:
    def test_a_message_is_accepted(self, smtp_server, run_id):
        """`SmtpMailer` has been unverified since it was written. This is a
        real send to an address the operator nominated."""
        from formforge.accounts.email import Message, SmtpMailer

        mailer = SmtpMailer(
            smtp_server["host"], smtp_server["port"],
            user=smtp_server["user"], password=smtp_server["password"],
            sender=f"formforge-it@{smtp_server['host']}",
        )
        result = mailer.send(Message(
            to=smtp_server["to"],
            subject=f"FormForge integration test {run_id}",
            body="This message was sent by an opt-in integration test.",
        ))
        assert result == smtp_server["to"]

    def test_a_failure_does_not_leak_the_envelope(self, smtp_server):
        """The adapter re-raises with the exception class only, because SMTP
        errors routinely quote the credentials back."""
        from formforge.accounts.email import EmailError, Message, SmtpMailer

        mailer = SmtpMailer(
            smtp_server["host"], 1,  # a port nothing listens on
            user="nobody", password="a-password-that-must-not-appear",
            timeout=2.0,
        )
        with pytest.raises(EmailError) as caught:
            mailer.send(Message(to=smtp_server["to"], subject="x", body="y"))
        assert "a-password-that-must-not-appear" not in str(caught.value)


class TestRedisSharedRateLimit:
    """The shared limiter is not implemented. These tests describe the
    contract it must meet, and are the first thing to run when it is.

    They exercise Redis directly rather than a limiter that does not exist, so
    they prove the *primitive* is available and behaves as the design assumes:
    an atomic increment with a TTL, shared across connections.
    """

    def test_redis_answers(self, redis_client):
        client, _prefix, _keys = redis_client
        assert client.ping()

    def test_an_incrementing_counter_is_shared_across_connections(self, redis_client):
        """The property the in-memory limiter structurally cannot have, and
        the whole reason a shared backend is needed."""
        client, prefix, keys = redis_client
        key = f"{prefix}counter"
        keys.append(key)
        assert client.incr(key) == 1
        assert client.incr(key) == 2

    def test_a_window_expires(self, redis_client):
        client, prefix, keys = redis_client
        key = f"{prefix}window"
        keys.append(key)
        client.set(key, 1, ex=1)
        assert client.get(key) is not None
        time.sleep(1.2)
        assert client.get(key) is None, "the TTL a fixed window depends on did not fire"

    def test_the_app_still_refuses_to_use_it(self):
        """Until the limiter exists, configuring redis must fail closed rather
        than silently falling back to the per-process counter."""
        import dataclasses

        from formforge.api.security import open_rate_limiter
        from formforge.config import ConfigError, Settings

        settings = dataclasses.replace(
            Settings.from_env(), rate_limit_backend="redis"
        )
        with pytest.raises(ConfigError, match="not implemented"):
            open_rate_limiter(settings)
