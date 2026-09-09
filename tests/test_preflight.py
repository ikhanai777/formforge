"""Preflight: what it checks, what it refuses, and what it must never print.

The three properties that make it worth having rather than a checklist in a
document:

* **missing is not broken** -- "you have not configured S3" and "S3 is
  configured and refused us" are different findings with different fixes, and
  collapsing them turns a to-do list into an outage hunt;
* **nothing unconfigured is probed** -- it never invents a host to try;
* **no secret is printed**, in any output mode.

The last one gets the most tests, because it is the one that fails silently.
"""

from __future__ import annotations

import json
import os

import pytest

from formforge.config import Settings
from formforge.preflight import Status, render, run

STAGING = {
    "FORMFORGE_MODE": "staging",
    "FORMFORGE_SESSION_SECRET": "s" * 48,
    "FORMFORGE_LINK_SECRET": "l" * 48,
    "FORMFORGE_ALLOWED_ORIGINS": "https://staging.example",
    "FORMFORGE_SANDBOX_RUNTIME": "gvisor",
}


@pytest.fixture
def env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("FORMFORGE_", "STRIPE_", "AWS_")):
            monkeypatch.delenv(key, raising=False)

    def apply(**values):
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        return Settings.from_env()

    return apply


@pytest.fixture
def isolated_sandbox(monkeypatch):
    """Report the sandbox as isolated.

    Not papering over anything: in this container the runtime genuinely is
    `subprocess`, so the sandbox check genuinely FAILs, and that is the
    honest answer. Tests that are about *configuration* findings patch it so
    the environment's own posture does not drown the thing under test.
    `TestTheRestOfTheSurface` exercises the real check.
    """
    from formforge import preflight
    from formforge.preflight import Status

    def isolated(settings, report):
        report.add("sandbox", Status.OK, "patched: isolated for this test")

    monkeypatch.setattr(preflight, "_check_sandbox", isolated)


def find(report, check):
    for finding in report.findings:
        if finding.check == check:
            return finding
    return None


class TestMissingIsNotBroken:
    def test_an_unconfigured_environment_is_missing_not_failed(self, env, isolated_sandbox):
        """A staging box that has not been finished is not a broken one."""
        settings = env(**STAGING)
        report = run("staging", settings)
        for check in ("database", "storage", "email", "rate_limit", "billing"):
            finding = find(report, check)
            assert finding is not None, f"{check} was not checked"
            assert finding.status is Status.MISSING, (
                f"{check} reported {finding.status} for an unconfigured setting"
            )
        assert report.failed == 0

    def test_a_configured_but_wrong_integration_is_failed(self, env):
        settings = env(**STAGING, FORMFORGE_STORAGE="s3",
                       FORMFORGE_ARTIFACTS="/not/an/s3/url")
        report = run("staging", settings)
        assert find(report, "storage").status is Status.FAILED

    def test_the_summary_counts_them_separately(self, env):
        settings = env(**STAGING, FORMFORGE_STORAGE="s3",
                       FORMFORGE_ARTIFACTS="/not/an/s3/url")
        report = run("staging", settings)
        assert report.failed >= 1
        assert report.missing >= 1
        text = render(report)
        assert "failed verification" in text and "not configured yet" in text

    def test_ready_needs_both_zero(self, env):
        assert not run("staging", env(**STAGING)).ready

    def test_every_missing_or_failed_finding_says_what_to_set(self, env):
        """A finding without a fix is a puzzle."""
        report = run("staging", env(**STAGING))
        for finding in report.findings:
            if finding.status in (Status.MISSING, Status.FAILED):
                assert finding.fix, f"{finding.check} says what is wrong but not what to do"


class TestItProbesOnlyWhatIsConfigured:
    def test_no_smtp_connection_is_attempted_when_email_is_the_outbox(self, env, monkeypatch):
        """Preflight must not invent a host to try."""
        import socket

        def explode(*args, **kwargs):
            raise AssertionError("preflight tried to open a socket")

        monkeypatch.setattr(socket, "create_connection", explode)
        report = run("staging", env(**STAGING))
        assert find(report, "email").status is Status.MISSING

    def test_no_database_connection_is_attempted_for_sqlite(self, env, monkeypatch):
        called = []
        try:
            import psycopg

            monkeypatch.setattr(
                psycopg, "connect", lambda *a, **k: called.append(1)
            )
        except ImportError:
            pytest.skip("psycopg not installed")
        run("staging", env(**STAGING, FORMFORGE_ACCOUNTS_DB="/tmp/x.db"))
        assert not called

    def test_a_configured_postgres_is_actually_probed(self, env):
        """The point of preflight over `Settings.problems()`: a DSN that
        parses and a database that answers are different facts."""
        dsn = os.environ.get("FORMFORGE_TEST_PG")
        if not dsn:
            pytest.skip("set FORMFORGE_TEST_PG to exercise the live probe")
        report = run("staging", env(**STAGING, FORMFORGE_ACCOUNTS_DB=dsn))
        assert find(report, "database.connect").status is Status.OK
        assert "PostgreSQL" in find(report, "database.connect").detail

    def test_an_unreachable_postgres_is_failed_not_missing(self, env):
        settings = env(
            **STAGING,
            FORMFORGE_ACCOUNTS_DB="postgresql://nobody@127.0.0.1:1/nothing",
        )
        report = run("staging", settings)
        assert find(report, "database.connect").status is Status.FAILED


class TestSecretsNeverAppear:
    SECRETS = (
        ("FORMFORGE_SESSION_SECRET", "session-" + "S" * 40),
        ("FORMFORGE_LINK_SECRET", "link-" + "L" * 40),
        ("STRIPE_SECRET_KEY", "sk_test_" + "K" * 30),
        ("STRIPE_WEBHOOK_SECRET", "whsec_" + "W" * 30),
        ("FORMFORGE_SMTP_PASSWORD", "smtp-" + "P" * 30),
    )

    def _report(self, env):
        return run("staging", env(
            **{**STAGING, **dict(self.SECRETS)},
            FORMFORGE_BILLING="stripe_sandbox",
            FORMFORGE_ACCOUNTS_DB="postgresql://user:hunter2@127.0.0.1:1/db",
        ))

    def test_the_text_output_carries_no_secret(self, env):
        text = render(self._report(env))
        for _, value in self.SECRETS:
            assert value not in text
        assert "hunter2" not in text, "the DSN password leaked through an error"

    def test_the_json_output_carries_no_secret(self, env):
        blob = json.dumps(self._report(env).as_dict())
        for _, value in self.SECRETS:
            assert value not in blob
        assert "hunter2" not in blob

    def test_it_reports_the_key_prefix_rather_than_the_key(self, env):
        report = self._report(env)
        finding = find(report, "billing.key")
        assert finding.status is Status.OK
        assert "sk_test_" in finding.detail
        assert "K" * 30 not in finding.detail

    def test_a_failed_connection_does_not_echo_the_dsn(self, env):
        """The usual leak: a driver's exception text contains the connection
        string, and reporting `str(exc)` prints the password."""
        detail = find(self._report(env), "database.connect").detail
        assert "hunter2" not in detail
        assert "postgresql://" not in detail


class TestBilling:
    def test_live_billing_is_refused_outright(self, env):
        settings = env(
            **STAGING,
            FORMFORGE_BILLING="stripe_live",
            STRIPE_SECRET_KEY="sk_live_placeholder",
            STRIPE_WEBHOOK_SECRET="whsec_placeholder",
            FORMFORGE_ALLOW_LIVE_BILLING="1",
        )
        report = run("staging", settings)
        assert find(report, "billing").status is Status.FAILED
        assert "live" in find(report, "billing").detail

    def test_it_says_live_validation_is_out_of_scope(self, env):
        report = run("staging", env(**STAGING))
        finding = find(report, "live_billing")
        assert finding.status is Status.SKIPPED
        assert "out of scope" in finding.detail

    def test_a_live_key_in_sandbox_mode_is_failed(self, env):
        settings = env(**STAGING, FORMFORGE_BILLING="stripe_sandbox",
                       STRIPE_SECRET_KEY="sk_live_placeholder")
        assert find(run("staging", settings), "billing.key").status is Status.FAILED

    def test_a_malformed_key_is_failed(self, env):
        settings = env(**STAGING, FORMFORGE_BILLING="stripe_sandbox",
                       STRIPE_SECRET_KEY="pasted-half-of-it")
        assert find(run("staging", settings), "billing.key").status is Status.FAILED

    def test_a_missing_webhook_secret_is_missing(self, env):
        settings = env(**STAGING, FORMFORGE_BILLING="stripe_sandbox",
                       STRIPE_SECRET_KEY="sk_test_placeholder")
        finding = find(run("staging", settings), "billing.webhook_secret")
        assert finding.status is Status.MISSING


class TestTheRestOfTheSurface:
    def test_insecure_cookies_are_failed(self, env):
        settings = env(**STAGING, FORMFORGE_COOKIE_INSECURE="1")
        assert find(run("staging", settings), "cookies").status is Status.FAILED

    def test_a_shared_limiter_that_does_not_exist_is_failed_not_ok(self, env):
        """Configuring redis and getting a per-process counter is a limit
        believed in and not present."""
        settings = env(**STAGING, FORMFORGE_RATE_LIMIT_BACKEND="redis")
        assert find(run("staging", settings), "rate_limit").status is Status.FAILED

    def test_reusing_one_secret_for_both_purposes_warns(self, env):
        same = "x" * 48
        settings = env(**{**STAGING, "FORMFORGE_SESSION_SECRET": same,
                          "FORMFORGE_LINK_SECRET": same})
        assert find(run("staging", settings), "secret.distinct").status is Status.WARN

    def test_a_short_secret_is_failed(self, env):
        settings = env(**{**STAGING, "FORMFORGE_SESSION_SECRET": "short"})
        assert find(run("staging", settings), "secret.session").status is Status.FAILED

    def test_an_unsafe_sandbox_is_failed(self, env):
        settings = env(**STAGING, FORMFORGE_ALLOW_UNSAFE_SANDBOX="1")
        assert find(run("staging", settings), "sandbox").status is Status.FAILED

    def test_a_mode_mismatch_warns_without_failing(self, env):
        settings = env(**{**STAGING, "FORMFORGE_MODE": "local"})
        report = run("staging", settings)
        assert find(report, "mode").status is Status.WARN

    def test_non_https_origins_warn(self, env):
        settings = env(**{**STAGING,
                          "FORMFORGE_ALLOWED_ORIGINS": "http://staging.example"})
        assert find(run("staging", settings), "origins").status is Status.WARN

    def test_localhost_over_http_does_not_warn(self, env):
        settings = env(**{**STAGING,
                          "FORMFORGE_ALLOWED_ORIGINS": "http://localhost:8000"})
        assert find(run("staging", settings), "origins").status is Status.OK

    def test_reset_disabled_skips_email_entirely(self, env):
        settings = env(**STAGING, FORMFORGE_PASSWORD_RESET="0")
        assert find(run("staging", settings), "email").status is Status.SKIPPED


class TestTheCommand:
    def test_it_exits_nonzero_only_when_something_configured_is_broken(
        self, env, isolated_sandbox
    ):
        """A CI gate has to tell "not finished" from "wrong"."""
        from formforge.cli import main

        env(**STAGING)
        assert main(["preflight", "--environment", "staging"]) == 0

        env(**STAGING, FORMFORGE_STORAGE="s3", FORMFORGE_ARTIFACTS="/nope")
        assert main(["preflight", "--environment", "staging"]) == 1

    def test_json_output_parses(self, env, capsys):
        from formforge.cli import main

        env(**STAGING)
        main(["preflight", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["environment"] == "staging"
        assert payload.get("findings")
