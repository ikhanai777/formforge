"""Configuration: local runs, production refuses, secrets do not print.

The behaviour under test is mostly *refusal*, which is the part nobody
exercises by hand. A production deployment missing its link secret used to
start cleanly and then break every download link the moment a second worker
existed; the point of these tests is that it now stops instead, and says which
variable.
"""

from __future__ import annotations

import pytest

from formforge.config import (
    BillingMode,
    ConfigError,
    EmailMode,
    Mode,
    Secret,
    Settings,
    StorageMode,
)

# A production configuration with nothing wrong with it. Each test below breaks
# exactly one thing, so a failure names the rule that fired.
SOUND = {
    "FORMFORGE_MODE": "production",
    "FORMFORGE_SESSION_SECRET": "s" * 40,
    "FORMFORGE_LINK_SECRET": "l" * 40,
    "FORMFORGE_ALLOWED_ORIGINS": "https://formforge.app",
    "FORMFORGE_ACCOUNTS_DB": "postgresql://user:pw@db/formforge",
    "FORMFORGE_STORAGE": "s3",
    "FORMFORGE_ARTIFACTS": "s3://bucket/prefix",
    "FORMFORGE_PASSWORD_RESET": "0",
}


@pytest.fixture
def env(monkeypatch):
    """A clean environment. Nothing leaks in from the developer's shell."""
    for key in list(__import__("os").environ):
        if key.startswith(("FORMFORGE_", "STRIPE_", "AWS_")):
            monkeypatch.delenv(key, raising=False)

    def apply(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key, value)

    return apply


class TestLocalIsUsable:
    def test_a_clean_clone_needs_no_environment_at_all(self, env):
        """The headline property: clone, run, nothing to configure."""
        settings = Settings.load()
        assert settings.mode is Mode.LOCAL
        assert settings.billing is BillingMode.OFFLINE
        assert settings.storage is StorageMode.LOCAL
        assert settings.email is EmailMode.OUTBOX
        assert settings.retention_days == 0, "retention must ship off"
        assert not settings.live_billing_armed

    def test_local_reports_problems_without_refusing(self, env):
        """The same list production dies on. Locally it is advice."""
        settings = Settings.load()
        assert settings.problems(), "a local config is not production-ready"

    def test_cookies_are_secure_unless_asked_otherwise(self, env):
        assert Settings.from_env().cookie_secure is True
        env(FORMFORGE_COOKIE_INSECURE="1")
        assert Settings.from_env().cookie_secure is False


class TestProductionFailsClosed:
    def test_a_sound_production_config_starts(self, env):
        env(**SOUND)
        assert Settings.load().mode is Mode.PRODUCTION

    @pytest.mark.parametrize(
        "missing,expected",
        [
            ("FORMFORGE_SESSION_SECRET", "FORMFORGE_SESSION_SECRET"),
            ("FORMFORGE_LINK_SECRET", "FORMFORGE_LINK_SECRET"),
            ("FORMFORGE_ALLOWED_ORIGINS", "FORMFORGE_ALLOWED_ORIGINS"),
            ("FORMFORGE_ACCOUNTS_DB", "FORMFORGE_ACCOUNTS_DB"),
            ("FORMFORGE_STORAGE", "FORMFORGE_STORAGE"),
        ],
    )
    def test_each_critical_setting_is_required(self, env, missing, expected):
        env(**{k: v for k, v in SOUND.items() if k != missing})
        with pytest.raises(ConfigError) as caught:
            Settings.load()
        # The message has to name the variable. "Invalid configuration" costs
        # an hour that this does not.
        assert expected in str(caught.value)

    def test_a_short_secret_is_refused(self, env):
        env(**{**SOUND, "FORMFORGE_SESSION_SECRET": "tooshort"})
        with pytest.raises(ConfigError, match="32 characters"):
            Settings.load()

    def test_insecure_cookies_are_refused(self, env):
        env(**{**SOUND, "FORMFORGE_COOKIE_INSECURE": "1"})
        with pytest.raises(ConfigError, match="COOKIE_INSECURE"):
            Settings.load()

    def test_sqlite_is_refused(self, env):
        """A single-writer file does not survive a second worker."""
        env(**{**SOUND, "FORMFORGE_ACCOUNTS_DB": "/var/lib/formforge.db"})
        with pytest.raises(ConfigError, match="PostgreSQL"):
            Settings.load()

    def test_an_unsafe_sandbox_is_refused(self, env):
        env(**{**SOUND, "FORMFORGE_ALLOW_UNSAFE_SANDBOX": "1"})
        with pytest.raises(ConfigError, match="SANDBOX"):
            Settings.load()

    def test_password_reset_without_a_mail_server_is_refused(self, env):
        """Reset mail written to a local outbox in production is a recovery
        path that looks like it works and does not."""
        env(**{**SOUND, "FORMFORGE_PASSWORD_RESET": "1", "FORMFORGE_EMAIL": "outbox"})
        with pytest.raises(ConfigError, match="password reset"):
            Settings.load()

    def test_it_reports_every_problem_at_once(self, env):
        """One restart per missing variable is a bad afternoon."""
        env(FORMFORGE_MODE="production")
        with pytest.raises(ConfigError) as caught:
            Settings.load()
        assert str(caught.value).count(";") >= 4

    def test_staging_warns_rather_than_refusing(self, env):
        env(FORMFORGE_MODE="staging")
        settings = Settings.load()
        assert settings.problems()
        assert settings.mode is Mode.STAGING


class TestLiveBillingNeedsThreeGates:
    """A Stripe live key on its own must never be able to move money."""

    LIVE = (
        ("STRIPE_SECRET_KEY", "sk_live_placeholder"),
        ("STRIPE_WEBHOOK_SECRET", "whsec_placeholder"),
        ("STRIPE_PRICE_MAKER", "price_placeholder"),
        ("STRIPE_PRICE_STUDIO", "price_placeholder"),
    )

    def test_a_live_key_alone_is_a_startup_error(self, env):
        """Not a silent fallback to offline: somebody believes they configured
        billing and they have not."""
        env(**{**SOUND, **dict(self.LIVE)})
        with pytest.raises(ConfigError, match="offline"):
            Settings.load()

    def test_the_mode_alone_is_not_enough(self, env):
        env(**{**SOUND, **dict(self.LIVE), "FORMFORGE_BILLING": "stripe_live"})
        with pytest.raises(ConfigError, match="ALLOW_LIVE_BILLING"):
            Settings.load()

    def test_all_three_together_arm_it(self, env):
        env(**{
            **SOUND, **dict(self.LIVE),
            "FORMFORGE_BILLING": "stripe_live",
            "FORMFORGE_ALLOW_LIVE_BILLING": "1",
        })
        assert Settings.load().live_billing_armed is True

    def test_a_live_key_in_sandbox_mode_is_refused(self, env):
        env(**{**SOUND, **dict(self.LIVE), "FORMFORGE_BILLING": "stripe_sandbox"})
        with pytest.raises(ConfigError, match="live key"):
            Settings.load()

    def test_sandbox_with_a_test_key_is_fine_and_arms_nothing(self, env):
        env(**{
            **SOUND, **dict(self.LIVE),
            "STRIPE_SECRET_KEY": "sk_test_placeholder",
            "FORMFORGE_BILLING": "stripe_sandbox",
        })
        settings = Settings.load()
        assert settings.billing is BillingMode.STRIPE_SANDBOX
        assert settings.live_billing_armed is False

    def test_stripe_without_a_webhook_secret_is_refused(self, env):
        """The signature is the only authentication a webhook has."""
        env(**{
            **SOUND, **dict(self.LIVE),
            "STRIPE_SECRET_KEY": "sk_test_placeholder",
            "STRIPE_WEBHOOK_SECRET": "",
            "FORMFORGE_BILLING": "stripe_sandbox",
        })
        with pytest.raises(ConfigError, match="WEBHOOK_SECRET"):
            Settings.load()

    def test_the_offline_default_arms_nothing(self, env):
        assert Settings.load().live_billing_armed is False


class TestSecretsDoNotPrint:
    """A settings object will end up in a log line or a traceback. When it
    does, it must take nothing with it."""

    @pytest.mark.parametrize("render", [repr, str, lambda s: f"{s}", "{}".format])
    def test_a_secret_never_renders_its_value(self, render):
        assert "hunter2" not in render(Secret("hunter2"))
        assert "***" in render(Secret("hunter2"))

    def test_a_secret_inside_a_container_still_hides(self):
        assert "hunter2" not in repr({"key": Secret("hunter2"), "n": [Secret("hunter2")]})

    def test_the_whole_settings_object_hides_its_secrets(self, env):
        env(**{**SOUND, "FORMFORGE_SESSION_SECRET": "topsecret" * 5})
        assert "topsecret" not in repr(Settings.load())

    def test_reading_a_secret_is_explicit(self):
        assert Secret("hunter2").reveal() == "hunter2"

    def test_an_unset_secret_is_falsey(self):
        assert not Secret("")
        assert Secret("x")

    def test_describe_says_whether_not_what(self, env):
        env(**SOUND)
        described = Settings.load().describe()
        assert described["session_secret_set"] is True
        assert described["link_secret_set"] is True
        blob = repr(described)
        assert "s" * 40 not in blob and "l" * 40 not in blob


class TestParsing:
    def test_an_unknown_mode_names_the_alternatives(self, env):
        env(FORMFORGE_MODE="prod")
        with pytest.raises(ConfigError, match="local, staging, production"):
            Settings.from_env()

    def test_a_non_numeric_retention_is_refused(self, env):
        env(FORMFORGE_RETENTION_DAYS="soon")
        with pytest.raises(ConfigError, match="whole number"):
            Settings.from_env()

    @pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("YES", True),
                                              ("0", False), ("no", False), ("", True)])
    def test_flags_accept_what_people_actually_type(self, env, raw, expected):
        # Default True for this one, so an empty value means "unset".
        if raw:
            env(FORMFORGE_PASSWORD_RESET=raw)
        assert Settings.from_env().password_reset_enabled is expected

    def test_origins_split_and_strip(self, env):
        env(FORMFORGE_ALLOWED_ORIGINS=" https://a.example , https://b.example ")
        assert Settings.from_env().allowed_origins == (
            "https://a.example", "https://b.example"
        )

    def test_a_dsn_is_recognised_as_postgres(self, env):
        for dsn in ("postgresql://u@h/db", "postgres://u@h/db", "host=h dbname=formforge"):
            env(FORMFORGE_ACCOUNTS_DB=dsn)
            assert Settings.from_env().accounts_is_postgres, dsn

    def test_a_path_is_not(self, env):
        env(FORMFORGE_ACCOUNTS_DB="/var/lib/formforge/accounts.db")
        assert not Settings.from_env().accounts_is_postgres


class TestTheExampleFileStaysHonest:
    def test_every_documented_variable_is_one_the_code_reads(self):
        """A `.env.example` that drifts from the code is worse than none: it
        is a list of things somebody will set expecting an effect."""
        import re
        from pathlib import Path

        example = Path(".env.example").read_text()
        documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", example, re.M))
        source = "\n".join(
            p.read_text() for p in Path("formforge").rglob("*.py")
        ) + Path("docs/configuration.md").read_text()
        unknown = {name for name in documented if name not in source}
        assert not unknown, f".env.example documents variables nothing reads: {unknown}"

    def test_it_contains_no_plausible_real_secret(self):
        """Placeholders only. A committed example file is the classic place a
        real key ends up."""
        from pathlib import Path

        text = Path(".env.example").read_text()
        for pattern in ("sk_live_", "whsec_1", "AKIA"):
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or "=" not in stripped:
                    continue
                assert pattern not in stripped, f"{pattern} in an active line: {line}"
