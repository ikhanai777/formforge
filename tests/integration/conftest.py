"""Opt-in integration tests. Every one is skipped unless asked for.

These are the only tests in the repository that talk to something outside the
process, and the rules that govern them are stricter than for the rest:

**Two keys, not one.** Each needs an explicit `FORMFORGE_IT_<NAME>=1` flag *and*
the credentials for the service. Credentials alone are not consent -- a
developer with `AWS_PROFILE` exported in their shell has not agreed to let a
test suite write to a bucket. The flag is the agreement; the credentials are
the means.

**Namespaced, and cleaning up only their own.** Everything written goes under a
prefix carrying a run id, and teardown removes exactly what the run created. A
test that deletes by pattern eventually matches something a person cared about.

**Never live.** The Stripe fixture refuses a key that is not `sk_test_`, and
refuses it at fixture setup rather than at the first call, so a
misconfiguration cannot get as far as an API request.

**Nothing here runs in the normal suite**, so `pytest tests/` stays credential
free and offline. `docs/staging-validation.md` lists the flags and variables.
"""

from __future__ import annotations

import os
import secrets

import pytest

# One prefix per run. Everything an integration test creates carries it, which
# is what makes "delete only what we made" mechanically true rather than a
# careful habit.
RUN_ID = f"ffit-{secrets.token_hex(4)}"


def _enabled(name: str) -> bool:
    return os.environ.get(f"FORMFORGE_IT_{name.upper()}") == "1"


def require(name: str, *variables: str) -> dict[str, str]:
    """Skip unless the flag is set *and* every named variable is present.

    The message names what is missing, because "skipped" with no reason is a
    test somebody assumes ran.
    """
    if not _enabled(name):
        pytest.skip(
            f"set FORMFORGE_IT_{name.upper()}=1 to run the {name} integration tests"
        )
    missing = [v for v in variables if not os.environ.get(v)]
    if missing:
        pytest.skip(f"{name} integration needs: {', '.join(missing)}")
    return {v: os.environ[v] for v in variables}


@pytest.fixture(scope="session")
def run_id() -> str:
    return RUN_ID


@pytest.fixture
def stripe_sandbox():
    """A Stripe client, test mode only.

    The live-key refusal is here rather than in the test bodies so that no
    test can be written that reaches an API call with a live key. It fails at
    setup, before anything is sent.
    """
    env = require("stripe", "STRIPE_SECRET_KEY")
    key = env["STRIPE_SECRET_KEY"]
    if not key.startswith("sk_test_"):
        pytest.fail(
            "the Stripe integration tests refuse a key that is not sk_test_. "
            "This suite never makes live-mode calls."
        )
    stripe = pytest.importorskip("stripe")
    return stripe.StripeClient(key)


@pytest.fixture
def s3_bucket():
    """An S3-compatible bucket, plus a prefix this run owns."""
    env = require("s3", "FORMFORGE_IT_S3_BUCKET")
    boto3 = pytest.importorskip("boto3")
    client = boto3.client(
        "s3", endpoint_url=os.environ.get("FORMFORGE_IT_S3_ENDPOINT") or None
    )
    bucket = env["FORMFORGE_IT_S3_BUCKET"]
    prefix = f"{RUN_ID}/"
    created: list[str] = []

    yield client, bucket, prefix, created

    # Only keys this run recorded. Not a prefix sweep: a sweep eventually
    # matches something somebody cared about.
    for key in created:
        try:
            client.delete_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - teardown must not mask a failure
            print(f"integration teardown: could not delete {key}: {type(exc).__name__}")


@pytest.fixture
def smtp_server():
    env = require(
        "smtp", "FORMFORGE_IT_SMTP_HOST", "FORMFORGE_IT_SMTP_TO"
    )
    return {
        "host": env["FORMFORGE_IT_SMTP_HOST"],
        "port": int(os.environ.get("FORMFORGE_IT_SMTP_PORT", "587")),
        "user": os.environ.get("FORMFORGE_IT_SMTP_USER", ""),
        "password": os.environ.get("FORMFORGE_IT_SMTP_PASSWORD", ""),
        "to": env["FORMFORGE_IT_SMTP_TO"],
    }


@pytest.fixture
def redis_client():
    env = require("redis", "FORMFORGE_IT_REDIS_URL")
    redis = pytest.importorskip("redis")
    client = redis.Redis.from_url(env["FORMFORGE_IT_REDIS_URL"])
    keys: list[str] = []
    yield client, f"{RUN_ID}:", keys
    for key in keys:
        try:
            client.delete(key)
        except Exception as exc:  # noqa: BLE001
            print(f"integration teardown: could not delete {key}: {type(exc).__name__}")


@pytest.fixture
def disposable_postgres():
    """A PostgreSQL database this suite is allowed to destroy.

    Deliberately **not** `FORMFORGE_TEST_PG`, and deliberately a separate
    opt-in: the restore rehearsal drops and recreates a schema, and pointing
    that at a database somebody was using is the kind of mistake that has no
    undo. Requiring a second variable whose name contains DISPOSABLE means
    nobody sets it by accident.
    """
    env = require("pg_restore", "FORMFORGE_IT_DISPOSABLE_PG")
    dsn = env["FORMFORGE_IT_DISPOSABLE_PG"]
    if "disposable" not in dsn.lower() and "test" not in dsn.lower():
        pytest.fail(
            "FORMFORGE_IT_DISPOSABLE_PG must name a database with 'test' or "
            "'disposable' in it. This fixture drops schemas."
        )
    return dsn
