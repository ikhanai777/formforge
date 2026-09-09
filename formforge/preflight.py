"""Is this environment ready to be pointed at real users?

The question `Settings.problems()` cannot answer. That one reads the
environment and says whether the *shape* is right; this one goes and asks the
things it names whether they actually answer. A DSN that parses and a database
that accepts a connection are different facts, and the gap between them is
where a staging deploy fails at the first request rather than at startup.

Three rules run through it, and they are the reason it is worth having rather
than a checklist in a document:

**Missing and broken are different findings.** "You have not configured S3 yet"
is a staging deployment that is not finished. "S3 is configured and the bucket
refused us" is a staging deployment that is wrong. Reporting them the same way
turns a to-do list into an outage hunt, so they are separate statuses and the
summary counts them separately.

**Nothing is probed that is not configured.** Connectivity is only attempted
where the settings for it exist. Preflight never invents a host to try.

**No secret is ever printed.** Not the DSN, not the key, not the bucket
credentials. Findings say what and whether: "STRIPE_SECRET_KEY: test-mode
prefix", never the key. `Settings.describe()` already holds that line and this
follows it.

Live billing is refused outright. Preflight validates a staging environment;
verifying that real money moves is not something a command should do, and
saying so is more useful than a check that appears to have covered it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .config import BillingMode, EmailMode, Mode, Settings, StorageMode


class Status(StrEnum):
    OK = "ok"
    # Configured, and it answered wrongly or not at all. Something is broken.
    FAILED = "failed"
    # Not configured. Expected in staging, so it is a gap rather than a fault.
    MISSING = "missing"
    # Configured in a way that will not do harm now but will later.
    WARN = "warn"
    # Deliberately not checked here, and the reason is worth reading.
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    status: Status
    detail: str
    # What to set, when the answer is "set something". Never a value.
    fix: str = ""


@dataclass
class Report:
    environment: str
    findings: list[Finding] = field(default_factory=list)

    def add(self, check: str, status: Status, detail: str, fix: str = "") -> None:
        self.findings.append(Finding(check, status, detail, fix))

    def count(self, status: Status) -> int:
        return sum(1 for f in self.findings if f.status is status)

    @property
    def failed(self) -> int:
        return self.count(Status.FAILED)

    @property
    def missing(self) -> int:
        return self.count(Status.MISSING)

    @property
    def ready(self) -> bool:
        """Nothing broken and nothing absent."""
        return self.failed == 0 and self.missing == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "ready": self.ready,
            "failed": self.failed,
            "missing": self.missing,
            "findings": [
                {"check": f.check, "status": f.status.value, "detail": f.detail,
                 "fix": f.fix}
                for f in self.findings
            ],
        }


def _probe_postgres(dsn: str, report: Report) -> None:
    try:
        import psycopg
    except ImportError:
        report.add("database.driver", Status.FAILED,
                   "a PostgreSQL DSN is configured but psycopg is not installed",
                   "pip install 'formforge[accounts]'")
        return
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            version = conn.execute("SELECT version()").fetchone()[0]
            # Which migrations have run matters as much as whether it answers:
            # an empty database and a migrated one both connect fine.
            try:
                rows = conn.execute(
                    "SELECT id FROM schema_migrations ORDER BY id"
                ).fetchall()
                applied = [r[0] for r in rows]
            except Exception:
                applied = []
    except Exception as exc:
        # The DSN holds a password. Report the *class* of failure, never the
        # exception text, which routinely echoes the connection string back.
        report.add("database.connect", Status.FAILED,
                   f"could not connect ({type(exc).__name__})",
                   "check the host, database name and credentials")
        return
    report.add("database.connect", Status.OK, version.split(" on ")[0])
    from .accounts.dialect import REVISIONS

    outstanding = [r for r in REVISIONS if r not in applied]
    if not applied:
        report.add("database.migrations", Status.MISSING,
                   "no migrations have been applied to this database",
                   "run `formforge bootstrap` against it")
    elif outstanding:
        report.add("database.migrations", Status.MISSING,
                   f"{len(outstanding)} revision(s) not yet applied: "
                   f"{', '.join(outstanding)}",
                   "run `formforge bootstrap` against it")
    else:
        report.add("database.migrations", Status.OK,
                   f"{len(applied)} revision(s) applied")


def _check_database(settings: Settings, report: Report) -> None:
    if settings.accounts_is_postgres:
        _probe_postgres(settings.accounts_db, report)
        return
    report.add(
        "database", Status.MISSING,
        "SQLite is configured; a deployment needs PostgreSQL "
        "(a single-writer file does not survive a second worker)",
        "FORMFORGE_ACCOUNTS_DB=postgresql://…",
    )


def _check_storage(settings: Settings, report: Report) -> None:
    if settings.storage is not StorageMode.S3:
        report.add("storage", Status.MISSING,
                   "local filesystem storage; a deployment needs S3-compatible "
                   "(local files do not outlive a container)",
                   "FORMFORGE_STORAGE=s3 and FORMFORGE_ARTIFACTS=s3://bucket/prefix")
        return
    if not settings.artifacts.startswith("s3://"):
        report.add("storage", Status.FAILED,
                   "FORMFORGE_STORAGE is 's3' but FORMFORGE_ARTIFACTS is not an "
                   "s3:// URL", "FORMFORGE_ARTIFACTS=s3://bucket/prefix")
        return
    try:
        from .storage import open_storage

        storage = open_storage(settings.artifacts)
        # A HEAD on a key that will not exist. Proves credentials and
        # reachability without creating anything: preflight must not leave
        # objects behind in a bucket it was asked to inspect.
        storage.exists("formforge-preflight/does-not-exist")
    except Exception as exc:
        report.add("storage.reach", Status.FAILED,
                   f"the bucket did not answer ({type(exc).__name__})",
                   "check the bucket name, region, endpoint and credentials")
        return
    report.add("storage.reach", Status.OK, "bucket answered a HEAD")


def _check_email(settings: Settings, report: Report) -> None:
    if not settings.password_reset_enabled:
        report.add("email", Status.SKIPPED,
                   "password reset is disabled, so no mail is sent")
        return
    if settings.email is EmailMode.OUTBOX:
        report.add("email", Status.MISSING,
                   "the local outbox writes files nobody reads; password reset "
                   "would look like it works and not deliver",
                   "FORMFORGE_EMAIL=smtp, or FORMFORGE_PASSWORD_RESET=0")
        return
    if settings.email is EmailMode.DISABLED:
        report.add("email", Status.FAILED,
                   "email is disabled but password reset is enabled",
                   "FORMFORGE_PASSWORD_RESET=0, or configure SMTP")
        return
    if not settings.smtp_host:
        report.add("email.smtp", Status.MISSING, "FORMFORGE_SMTP_HOST is unset",
                   "FORMFORGE_SMTP_HOST=…")
        return
    import socket

    try:
        with socket.create_connection((settings.smtp_host, settings.smtp_port), 5):
            pass
    except Exception as exc:
        report.add("email.smtp", Status.FAILED,
                   f"could not reach the mail server ({type(exc).__name__})",
                   "check the host, port and any firewall")
        return
    # A TCP connection, not a send. Preflight does not deliver mail to
    # somebody's inbox to prove it can.
    report.add("email.smtp", Status.OK,
               f"port {settings.smtp_port} accepted a connection "
               "(not an authenticated send)")


def _check_rate_limiter(settings: Settings, report: Report) -> None:
    backend = (settings.rate_limit_backend or "memory").lower()
    if backend == "memory":
        report.add(
            "rate_limit", Status.MISSING,
            "the in-memory limiter is per process, so N workers means N "
            "budgets and no shared limit at all",
            "FORMFORGE_RATE_LIMIT_BACKEND=redis once a shared limiter exists",
        )
        return
    if backend == "redis":
        # Honest: the interface exists, the implementation does not, and
        # `open_rate_limiter` refuses rather than silently using memory.
        report.add("rate_limit", Status.FAILED,
                   "redis is configured but the shared limiter is not "
                   "implemented; the app will refuse to start",
                   "FORMFORGE_RATE_LIMIT_BACKEND=memory until it exists")
        return
    report.add("rate_limit", Status.FAILED, f"unknown backend {backend!r}",
               "FORMFORGE_RATE_LIMIT_BACKEND=memory")


def _check_billing(settings: Settings, report: Report) -> None:
    if settings.live_billing_armed or settings.billing is BillingMode.STRIPE_LIVE:
        # The one thing preflight refuses to do. Verifying live billing means
        # moving real money, which is not a command's decision.
        report.add(
            "billing", Status.FAILED,
            "live billing is configured; preflight validates staging only and "
            "will not verify anything against live mode",
            "FORMFORGE_BILLING=stripe_sandbox for a staging environment",
        )
        return
    if settings.billing is BillingMode.OFFLINE:
        report.add("billing", Status.MISSING,
                   "the offline provider keeps books but moves no money",
                   "FORMFORGE_BILLING=stripe_sandbox with a sk_test_ key")
        return

    key = settings.stripe_secret_key.reveal()
    if not key:
        report.add("billing.key", Status.MISSING, "STRIPE_SECRET_KEY is unset",
                   "STRIPE_SECRET_KEY=sk_test_…")
    elif key.startswith("sk_test_"):
        # The prefix, never the key.
        report.add("billing.key", Status.OK, "test-mode key (sk_test_ prefix)")
    elif key.startswith("sk_live_"):
        report.add("billing.key", Status.FAILED,
                   "a live key is configured for a sandbox environment",
                   "use a sk_test_ key in staging")
    else:
        report.add("billing.key", Status.FAILED,
                   "STRIPE_SECRET_KEY has neither a sk_test_ nor a sk_live_ prefix",
                   "check the key was copied whole")

    if not settings.stripe_webhook_secret:
        report.add("billing.webhook_secret", Status.MISSING,
                   "STRIPE_WEBHOOK_SECRET is unset, and the signature is the "
                   "only authentication a webhook has",
                   "STRIPE_WEBHOOK_SECRET=whsec_…")
    else:
        report.add("billing.webhook_secret", Status.OK, "set")

    configured = {plan for plan, value in settings.stripe_prices if value}
    for plan in ("maker", "studio"):
        if plan not in configured:
            report.add(f"billing.price.{plan}", Status.MISSING,
                       f"no Stripe price id for the {plan} plan",
                       f"STRIPE_PRICE_{plan.upper()}=price_…")
    if configured:
        report.add("billing.prices", Status.OK,
                   f"{len(configured)} price id(s) configured")

    for name, value in (("success", settings.stripe_success_url),
                        ("cancel", settings.stripe_cancel_url)):
        if not value:
            report.add(f"billing.{name}_url", Status.MISSING,
                       f"STRIPE_{name.upper()}_URL is unset",
                       f"STRIPE_{name.upper()}_URL=https://…")
        elif not value.startswith("https://"):
            report.add(f"billing.{name}_url", Status.WARN,
                       "not https; a browser returning from Checkout would be "
                       "downgraded", f"STRIPE_{name.upper()}_URL=https://…")


def _check_http(settings: Settings, report: Report) -> None:
    if settings.cookie_secure:
        report.add("cookies", Status.OK, "Secure is on; TLS must terminate in front")
    else:
        report.add("cookies", Status.FAILED,
                   "FORMFORGE_COOKIE_INSECURE is set, so session cookies would "
                   "cross plain HTTP", "unset FORMFORGE_COOKIE_INSECURE")

    if not settings.allowed_origins:
        report.add("origins", Status.MISSING, "FORMFORGE_ALLOWED_ORIGINS is unset",
                   "FORMFORGE_ALLOWED_ORIGINS=https://…")
    else:
        insecure = [o for o in settings.allowed_origins
                    if not o.startswith("https://") and "localhost" not in o]
        if insecure:
            report.add("origins", Status.WARN,
                       f"{len(insecure)} origin(s) are not https",
                       "use https origins outside local development")
        else:
            report.add("origins", Status.OK,
                       f"{len(settings.allowed_origins)} origin(s) configured")


def _check_secrets(settings: Settings, report: Report) -> None:
    for name, secret, purpose in (
        ("session", settings.session_secret, "signs sessions"),
        ("link", settings.link_secret,
         "signs download links; without it each worker invents its own and "
         "rejects the others'"),
    ):
        if not secret:
            report.add(f"secret.{name}", Status.MISSING,
                       f"FORMFORGE_{name.upper()}_SECRET is unset ({purpose})",
                       f"FORMFORGE_{name.upper()}_SECRET=$(python -c "
                       "'import secrets;print(secrets.token_urlsafe(48))')")
        elif len(secret) < 32:
            report.add(f"secret.{name}", Status.FAILED,
                       "shorter than 32 characters",
                       "generate a longer one")
        else:
            # Length, never the value.
            report.add(f"secret.{name}", Status.OK, f"set, {len(secret)} characters")

    if settings.session_secret and settings.session_secret == settings.link_secret:
        report.add("secret.distinct", Status.WARN,
                   "the session and link secrets are the same value; rotating "
                   "one then signs everybody out",
                   "generate two different secrets")


def _check_sandbox(settings: Settings, report: Report) -> None:
    if settings.allow_unsafe_sandbox:
        report.add("sandbox", Status.FAILED,
                   "FORMFORGE_ALLOW_UNSAFE_SANDBOX is set, and the sandbox "
                   "executes model-authored Python",
                   "FORMFORGE_SANDBOX_RUNTIME=gvisor")
        return
    try:
        from .sandbox import GeometrySandbox

        box = GeometrySandbox()
        if box.production_ready():
            report.add("sandbox", Status.OK, f"{box.runtime} isolates the host kernel")
        else:
            report.add("sandbox", Status.FAILED,
                       f"runtime {box.runtime!r} does not isolate the host kernel",
                       "FORMFORGE_SANDBOX_RUNTIME=gvisor")
    except Exception as exc:
        report.add("sandbox", Status.FAILED,
                   f"could not inspect the sandbox ({type(exc).__name__})")


def run(environment: str = "staging", settings: Settings | None = None) -> Report:
    """Check an environment, probing only what is configured."""
    config = settings or Settings.from_env()
    report = Report(environment=environment)

    declared = config.mode.value
    if environment == "staging" and config.mode is not Mode.STAGING:
        report.add("mode", Status.WARN,
                   f"checking as staging but FORMFORGE_MODE is {declared!r}",
                   "FORMFORGE_MODE=staging")
    elif environment == "production" and config.mode is not Mode.PRODUCTION:
        report.add("mode", Status.WARN,
                   f"checking as production but FORMFORGE_MODE is {declared!r}",
                   "FORMFORGE_MODE=production")
    else:
        report.add("mode", Status.OK, declared)

    _check_secrets(config, report)
    _check_http(config, report)
    _check_database(config, report)
    _check_storage(config, report)
    _check_email(config, report)
    _check_rate_limiter(config, report)
    _check_billing(config, report)
    _check_sandbox(config, report)

    report.add(
        "live_billing", Status.SKIPPED,
        "out of scope: preflight never verifies live billing, because doing so "
        "means moving real money",
    )
    return report


def render(report: Report) -> str:
    """The report as text. Contains no secret, by construction -- every finding
    was built from a description rather than from a value."""
    marks = {
        Status.OK: "ok  ", Status.FAILED: "FAIL", Status.MISSING: "----",
        Status.WARN: "warn", Status.SKIPPED: "skip",
    }
    lines = [f"preflight: {report.environment}", ""]
    for finding in report.findings:
        lines.append(f"  [{marks[finding.status]}] {finding.check:26s} {finding.detail}")
        if finding.fix and finding.status in (Status.MISSING, Status.FAILED, Status.WARN):
            lines.append(f"{'':33s}-> {finding.fix}")
    lines.append("")
    if report.ready:
        lines.append("Ready. Nothing configured is broken and nothing is missing.")
    else:
        # The distinction the whole command exists to draw.
        lines.append(
            f"{report.failed} configured integration(s) failed verification; "
            f"{report.missing} expected setting(s) not configured yet."
        )
        lines.append(
            "FAIL means something is wrong. ---- means something is not done."
        )
    return "\n".join(lines)
