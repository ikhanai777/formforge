"""Every setting, in one typed place, with production failing closed.

Before this, twenty-three environment variables were read ad hoc across eight
modules, each with its own inline default. That has a specific failure mode
rather than being merely untidy: a production deployment that forgot
`FORMFORGE_LINK_SECRET` got a per-process random one, so every download link
broke the moment a second worker existed, and nothing anywhere said so. A
setting that silently defaults to something unsafe is worse than one that is
missing, because missing is loud.

So:

* **Local is the default and is permissive.** A clean clone runs with no
  environment at all -- SQLite, local files, offline billing, an email outbox
  that writes to disk. Nothing here requires a credential.
* **Production fails closed.** `Settings.load()` in production mode refuses to
  return when anything security-critical is missing or unsafe, and the refusal
  names the variable and what to set it to.
* **Staging warns.** It is a real deployment, so it wants the real settings,
  but blocking a staging box on a missing origin allowlist helps nobody.

**Secrets are wrapped, not stored bare.** A `Secret` prints as `***` from
`repr`, `str` and f-strings, so a settings object that lands in a log line or a
traceback -- which is exactly where it will eventually land -- takes its
secrets nowhere. Reading the value takes an explicit `.reveal()`, which is
greppable in review.

**Stdlib only.** The CLI and the account store import this and neither requires
pydantic, so it is dataclasses and hand-written parsing rather than a settings
library. That constraint is worth the fifty extra lines.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from pathlib import Path

DEFAULT_HOME = Path.home() / ".formforge"


class ConfigError(Exception):
    """A setting is missing, or is set to something this mode will not run on.

    Raised at startup and nowhere else. The message names the variable, because
    an error that says "invalid configuration" costs an hour that an error
    saying "set FORMFORGE_SESSION_SECRET" does not.
    """


class Mode(StrEnum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_deployed(self) -> bool:
        return self in (Mode.STAGING, Mode.PRODUCTION)


class BillingMode(StrEnum):
    """Which payment provider is live, named rather than inferred.

    Inferring it from "are the Stripe variables set?" is how a deployment
    starts taking real money because someone exported a key to try something.
    The mode is a separate, deliberate statement.
    """

    OFFLINE = "offline"
    STRIPE_SANDBOX = "stripe_sandbox"
    STRIPE_LIVE = "stripe_live"


class EmailMode(StrEnum):
    """Where mail goes.

    `OUTBOX` writes a file per message to a directory, which is what makes the
    whole password-reset flow runnable and testable with no provider, no
    domain and no credential.
    """

    OUTBOX = "outbox"
    SMTP = "smtp"
    DISABLED = "disabled"


class StorageMode(StrEnum):
    LOCAL = "local"
    S3 = "s3"


class Secret:
    """A string that does not print itself.

    `repr`, `str` and f-string interpolation all give `***`. Getting at the
    value is `.reveal()`, which is one grep away in review and impossible to do
    by accident in a log call.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str | None):
        self._value = value or ""

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return "Secret(***)" if self._value else "Secret(unset)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc


def _enum(kind: type[Enum], name: str, default: Enum) -> Enum:
    raw = _env(name)
    if not raw:
        return default
    try:
        return kind(raw.lower())
    except ValueError as exc:
        allowed = ", ".join(m.value for m in kind)  # type: ignore[attr-defined]
        raise ConfigError(f"{name} must be one of: {allowed}. Got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Everything the application reads from its environment."""

    mode: Mode = Mode.LOCAL

    # -- secrets ---------------------------------------------------------
    # Signs sessions and anything else that needs one process-wide key.
    session_secret: Secret = field(default_factory=lambda: Secret(""))
    # Signs download links. Distinct from the session secret so that rotating
    # one does not sign every user out, and so a leak of one is not a leak of
    # both.
    link_secret: Secret = field(default_factory=lambda: Secret(""))

    # -- http ------------------------------------------------------------
    cookie_secure: bool = True
    allowed_origins: tuple[str, ...] = ()

    # -- data ------------------------------------------------------------
    accounts_db: str = ""
    telemetry_db: str = ""
    model_dir: Path = field(default_factory=lambda: DEFAULT_HOME / "models")
    storage: StorageMode = StorageMode.LOCAL
    artifacts: str = ""

    # -- billing ---------------------------------------------------------
    billing: BillingMode = BillingMode.OFFLINE
    stripe_secret_key: Secret = field(default_factory=lambda: Secret(""))
    stripe_webhook_secret: Secret = field(default_factory=lambda: Secret(""))
    stripe_prices: tuple[tuple[str, str], ...] = ()
    stripe_success_url: str = ""
    stripe_cancel_url: str = ""
    allow_live_billing: bool = False

    # -- email -----------------------------------------------------------
    email: EmailMode = EmailMode.OUTBOX
    email_outbox: Path = field(default_factory=lambda: DEFAULT_HOME / "outbox")
    email_from: str = "formforge@localhost"
    password_reset_enabled: bool = True
    # Only read when `email` is SMTP. Carried here rather than in the adapter
    # so there is one place that knows what the environment holds -- and so
    # `.env.example` cannot document a variable nothing reads.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: Secret = field(default_factory=lambda: Secret(""))

    # -- limits and lifecycle --------------------------------------------
    rate_limit_backend: str = "memory"
    # 0 means retention is off. Nothing is ever deleted on a schedule unless
    # this is set to something above zero, deliberately.
    retention_days: int = 0
    allow_unsafe_sandbox: bool = False

    # -- construction ----------------------------------------------------
    @classmethod
    def from_env(cls) -> "Settings":
        """Read the environment. Does not validate -- see `load`."""
        mode = _enum(Mode, "FORMFORGE_MODE", Mode.LOCAL)
        home = Path(_env("FORMFORGE_HOME") or DEFAULT_HOME)
        default_db = str(home / "formforge.db")
        telemetry = _env("FORMFORGE_DB") or default_db
        return cls(
            mode=mode,  # type: ignore[arg-type]
            session_secret=Secret(_env("FORMFORGE_SESSION_SECRET")),
            link_secret=Secret(_env("FORMFORGE_LINK_SECRET")),
            # Secure cookies unless explicitly turned off, and turning them off
            # is only honoured outside production -- see `problems()`.
            cookie_secure=not _flag("FORMFORGE_COOKIE_INSECURE"),
            allowed_origins=tuple(
                o.strip() for o in _env("FORMFORGE_ALLOWED_ORIGINS").split(",") if o.strip()
            ),
            accounts_db=_env("FORMFORGE_ACCOUNTS_DB") or telemetry,
            telemetry_db=telemetry,
            model_dir=Path(_env("FORMFORGE_STORE") or home / "models"),
            storage=_enum(StorageMode, "FORMFORGE_STORAGE", StorageMode.LOCAL),  # type: ignore[arg-type]
            artifacts=_env("FORMFORGE_ARTIFACTS") or str(home / "artifacts"),
            billing=_enum(BillingMode, "FORMFORGE_BILLING", BillingMode.OFFLINE),  # type: ignore[arg-type]
            stripe_secret_key=Secret(_env("STRIPE_SECRET_KEY")),
            stripe_webhook_secret=Secret(_env("STRIPE_WEBHOOK_SECRET")),
            stripe_prices=tuple(
                (plan, _env(var))
                for plan, var in (("maker", "STRIPE_PRICE_MAKER"),
                                  ("studio", "STRIPE_PRICE_STUDIO"))
                if _env(var)
            ),
            stripe_success_url=_env("STRIPE_SUCCESS_URL"),
            stripe_cancel_url=_env("STRIPE_CANCEL_URL"),
            allow_live_billing=_flag("FORMFORGE_ALLOW_LIVE_BILLING"),
            email=_enum(EmailMode, "FORMFORGE_EMAIL", EmailMode.OUTBOX),  # type: ignore[arg-type]
            email_outbox=Path(_env("FORMFORGE_EMAIL_OUTBOX") or home / "outbox"),
            email_from=_env("FORMFORGE_EMAIL_FROM") or "formforge@localhost",
            smtp_host=_env("FORMFORGE_SMTP_HOST"),
            smtp_port=_int("FORMFORGE_SMTP_PORT", 587),
            smtp_user=_env("FORMFORGE_SMTP_USER"),
            smtp_password=Secret(_env("FORMFORGE_SMTP_PASSWORD")),
            password_reset_enabled=_flag("FORMFORGE_PASSWORD_RESET", default=True),
            rate_limit_backend=_env("FORMFORGE_RATE_LIMIT_BACKEND") or "memory",
            retention_days=_int("FORMFORGE_RETENTION_DAYS", 0),
            allow_unsafe_sandbox=_flag("FORMFORGE_ALLOW_UNSAFE_SANDBOX"),
        )

    @classmethod
    def load(cls, *, strict: bool | None = None) -> "Settings":
        """Read and validate. Raises ConfigError in production.

        `strict` overrides the mode's own policy, which is what lets a test
        assert production's refusals without setting FORMFORGE_MODE globally.
        """
        settings = cls.from_env()
        enforce = settings.mode is Mode.PRODUCTION if strict is None else strict
        problems = settings.problems()
        if problems and enforce:
            raise ConfigError(
                "refusing to start: "
                + "; ".join(problems)
                + ". See docs/configuration.md."
            )
        return settings

    # -- validation ------------------------------------------------------
    def problems(self) -> list[str]:
        """Everything wrong with this configuration for a real deployment.

        Returned rather than raised so a warning path and a failing path can
        share one implementation, and so a health check can report the list.
        """
        found: list[str] = []

        if not self.session_secret:
            found.append(
                "FORMFORGE_SESSION_SECRET is unset (needed to sign sessions)"
            )
        elif len(self.session_secret) < 32:
            found.append("FORMFORGE_SESSION_SECRET is shorter than 32 characters")

        if not self.link_secret:
            # The specific failure: without it each process invents its own, so
            # download links minted by one worker are rejected by the next.
            found.append(
                "FORMFORGE_LINK_SECRET is unset (download links would not "
                "verify across workers)"
            )
        elif len(self.link_secret) < 32:
            found.append("FORMFORGE_LINK_SECRET is shorter than 32 characters")

        if not self.cookie_secure:
            found.append(
                "FORMFORGE_COOKIE_INSECURE is set (session cookies would cross "
                "plain HTTP)"
            )

        if not self.allowed_origins:
            found.append("FORMFORGE_ALLOWED_ORIGINS is unset")

        if not self.accounts_db.startswith(("postgresql://", "postgres://")) and (
            "dbname" not in self.accounts_db
        ):
            found.append(
                "FORMFORGE_ACCOUNTS_DB is not a PostgreSQL DSN (SQLite is a "
                "single-writer file and will not survive more than one worker)"
            )

        if self.storage is not StorageMode.S3:
            found.append(
                "FORMFORGE_STORAGE is not 's3' (local files do not outlive a "
                "container)"
            )

        if self.email is EmailMode.SMTP and not self.smtp_host:
            found.append("FORMFORGE_EMAIL is 'smtp' but FORMFORGE_SMTP_HOST is unset")

        if self.password_reset_enabled and self.email is not EmailMode.SMTP:
            found.append(
                "password reset is enabled but FORMFORGE_EMAIL is not 'smtp' "
                "(reset mail would be written to a local outbox nobody reads; "
                "set FORMFORGE_PASSWORD_RESET=0 to disable the feature instead)"
            )

        if self.allow_unsafe_sandbox:
            found.append(
                "FORMFORGE_ALLOW_UNSAFE_SANDBOX is set (the sandbox executes "
                "model-authored Python)"
            )

        found.extend(self.billing_problems())
        return found

    def billing_problems(self) -> list[str]:
        """Whether billing is configured coherently.

        Live billing needs **three independent things** and refuses without all
        of them: the mode named `stripe_live`, a live key, and an explicit
        opt-in flag. A live key on its own must never be enough -- that is how
        a deployment starts charging real cards because somebody exported a
        variable to try something.

        The reverse is checked too: a live key present while the mode says
        anything else is a misconfiguration, not a safe default. Somebody
        believes they configured billing and they have not.
        """
        found: list[str] = []
        key = self.stripe_secret_key.reveal()
        looks_live = key.startswith("sk_live_")

        if self.billing is BillingMode.OFFLINE:
            if key:
                found.append(
                    "STRIPE_SECRET_KEY is set but FORMFORGE_BILLING is 'offline' "
                    "(no payment would be taken; set FORMFORGE_BILLING to "
                    "'stripe_sandbox' or unset the key)"
                )
            return found

        if not key:
            found.append(f"FORMFORGE_BILLING is {self.billing.value!r} but "
                         "STRIPE_SECRET_KEY is unset")
        if not self.stripe_webhook_secret:
            found.append(
                "STRIPE_WEBHOOK_SECRET is unset (webhooks could not be "
                "authenticated, and the signature is the only authentication "
                "a webhook has)"
            )
        if not self.stripe_prices:
            found.append("no Stripe price ids configured (STRIPE_PRICE_MAKER, "
                         "STRIPE_PRICE_STUDIO)")

        if self.billing is BillingMode.STRIPE_SANDBOX and looks_live:
            found.append(
                "FORMFORGE_BILLING is 'stripe_sandbox' but STRIPE_SECRET_KEY is "
                "a live key"
            )
        if self.billing is BillingMode.STRIPE_LIVE:
            if not self.allow_live_billing:
                found.append(
                    "FORMFORGE_BILLING is 'stripe_live' but "
                    "FORMFORGE_ALLOW_LIVE_BILLING is not set -- live billing "
                    "takes three deliberate settings, not two"
                )
            if key and not looks_live:
                found.append(
                    "FORMFORGE_BILLING is 'stripe_live' but STRIPE_SECRET_KEY "
                    "is not a live key"
                )
        return found

    @property
    def live_billing_armed(self) -> bool:
        """Whether real money can move. All three gates, or nothing."""
        return (
            self.billing is BillingMode.STRIPE_LIVE
            and self.allow_live_billing
            and self.stripe_secret_key.reveal().startswith("sk_live_")
        )

    # -- reporting -------------------------------------------------------
    def describe(self) -> dict[str, object]:
        """A shape safe to log or serve from a health endpoint.

        Names what each thing *is*, never what it is set to. "session_secret":
        true says the deployment is configured; printing the secret says
        considerably more.
        """
        return {
            "mode": self.mode.value,
            "billing": self.billing.value,
            "live_billing_armed": self.live_billing_armed,
            "storage": self.storage.value,
            "email": self.email.value,
            "password_reset": self.password_reset_enabled,
            "database": "postgres" if self.accounts_is_postgres else "sqlite",
            "cookie_secure": self.cookie_secure,
            "origins_configured": bool(self.allowed_origins),
            "session_secret_set": bool(self.session_secret),
            "link_secret_set": bool(self.link_secret),
            "rate_limit_backend": self.rate_limit_backend,
            "retention_days": self.retention_days,
        }

    @property
    def accounts_is_postgres(self) -> bool:
        return self.accounts_db.startswith(("postgresql://", "postgres://")) or (
            "dbname" in self.accounts_db
        )


_cached: Settings | None = None


def settings() -> Settings:
    """The process-wide settings, read once.

    Cached because reading is cheap but *inconsistency* is not: two components
    disagreeing about which database is live is a class of outage, and that is
    exactly what re-reading a mutable environment invites.
    """
    global _cached
    if _cached is None:
        _cached = Settings.load()
    return _cached


def reset_cache() -> None:
    """Drop the cached settings. For tests that change the environment."""
    global _cached
    _cached = None
