"""Structured logging, and a filter that keeps secrets out of it.

Security- and billing-sensitive events are worth logging: a failed login, a
webhook that did not verify, a credit charged, an account closed. The problem
is that the natural way to log them is to log the thing that happened, and the
thing that happened frequently contains a password, a session token, a reset
link or a card.

So there are two layers, and the important one is the second:

1. `event()` emits a structured line -- an event name plus named fields --
   rather than an interpolated sentence. Structure is what makes a log
   searchable six months later, and it is also what makes the next part
   possible: fields can be inspected, a formatted string cannot.

2. `Redactor` is a logging *filter*, installed on the root logger, that
   rewrites anything matching a credential pattern in any record from
   anywhere -- including a library's, including a traceback, including a
   message somebody added last week without reading this file. A rule that
   depends on every caller remembering it is not a rule.

The second layer exists because the first is not enough. `event()` is
convention; the filter is enforcement.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

# What a credential looks like when it escapes. Deliberately broad: a false
# positive costs a redacted log line, a false negative costs a credential.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Stripe and similar prefixed keys.
    (re.compile(r"\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]{4,}"), r"\1_\2_***"),
    (re.compile(r"\bwhsec_[A-Za-z0-9_\-]{4,}"), "whsec_***"),
    # AWS access key ids.
    (re.compile(r"\bAKIA[0-9A-Z]{8,}"), "AKIA***"),
    # Bearer credentials in a header. BEFORE the key=value rule below, which
    # would otherwise match `Authorization: Bearer` and helpfully redact the
    # word "Bearer" while leaving the token beside it. Order is load-bearing.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 ***"),
    # Anything that calls itself a secret in a key=value or JSON pair.
    (re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|cookie)"
        r"(\"?\s*[:=]\s*\"?)([^\s,\"'}\]]{3,})"
    ), r"\1\2***"),
    # A long URL-safe blob following `token=`, which is what a reset link is.
    (re.compile(r"(?i)([?&]token=)[A-Za-z0-9._\-]{8,}"), r"\1***"),
)


# Field names whose *value* is a secret whatever it looks like. Scrubbing by
# pattern cannot help here: a password of "correct-horse" matches nothing, and
# the only thing that marks it as sensitive is what it was called. A structured
# logger makes this checkable, which is half the reason to have one.
_SENSITIVE_FIELDS = frozenset({
    "password", "passwd", "secret", "token", "reset_token", "session",
    "session_token", "api_key", "apikey", "authorization", "cookie",
    "card", "card_number", "cvc", "webhook_secret", "signature",
})


def scrub(text: str) -> str:
    """Redact anything that looks like a credential."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scrub_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Redact by name as well as by shape.

    A field called `password` is redacted whatever its value, because the value
    of a real password looks exactly like an ordinary string. This is the check
    that pattern matching structurally cannot do, and the reason `event()`
    takes named fields rather than a formatted sentence.
    """
    cleaned: dict[str, Any] = {}
    for key, value in fields.items():
        if key.lower() in _SENSITIVE_FIELDS:
            cleaned[key] = "***"
        elif isinstance(value, str):
            cleaned[key] = scrub(value)
        elif isinstance(value, dict):
            cleaned[key] = scrub_fields(value)
        else:
            cleaned[key] = value
    return cleaned


class Redactor(logging.Filter):
    """Rewrites every record on its way out.

    Installed on the root logger, so it applies to this package, to libraries,
    and to a line somebody adds next month without reading any of this. That
    is the point: keeping secrets out of logs by asking every author to
    remember is a policy that holds until the first tired afternoon.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = scrub(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: scrub(v) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
                else:
                    record.args = tuple(
                        scrub(a) if isinstance(a, str) else a for a in record.args
                    )
            fields = getattr(record, "fields", None)
            if isinstance(fields, dict):
                record.fields = scrub_fields(fields)
        except Exception:  # pragma: no cover - a filter must never break logging
            pass
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    For a deployment, where a log is read by a query rather than by eye. Local
    development gets the plain formatter, because a wall of JSON is worse than
    a sentence when you are looking at it directly.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            # Scrubbed as well: a traceback frame can hold a local variable
            # whose repr is a key.
            payload["error"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def event(
    logger: logging.Logger,
    name: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Log one structured event.

    `event(log, "credit.spent", model_id=..., credits=1)` rather than an
    f-string, so the fields survive as fields. Every value still passes through
    the redactor, because a caller will eventually pass one that should not.

    Do not put a password, a token, a reset link, an API key or a card into
    `fields`. The redactor is a net, not a licence.
    """
    logger.log(level, name, extra={"fields": fields})


def configure(*, json_output: bool | None = None, level: str | None = None) -> None:
    """Install the formatter and the redactor on the root logger.

    Idempotent: calling it twice does not stack handlers or filters, which
    matters because a test suite and an app startup will both call it.
    """
    from .config import Mode, Settings

    settings = Settings.from_env()
    if json_output is None:
        json_output = settings.mode.is_deployed
    resolved = (level or os.environ.get("FORMFORGE_LOG_LEVEL") or "INFO").upper()

    root = logging.getLogger()
    root.setLevel(resolved)

    for existing in list(root.handlers):
        if getattr(existing, "_formforge", False):
            root.removeHandler(existing)

    handler = logging.StreamHandler()
    handler._formforge = True  # type: ignore[attr-defined]
    handler.setFormatter(
        JsonFormatter() if json_output else logging.Formatter(
            "%(levelname)s %(name)s: %(message)s"
        )
    )
    root.addHandler(handler)

    if not any(isinstance(f, Redactor) for f in root.filters):
        root.addFilter(Redactor())
    # A filter on the logger does not apply to records from *child* loggers,
    # so it goes on the handler too. Belt and braces, and the handler is the
    # one thing every record passes through.
    if not any(isinstance(f, Redactor) for f in handler.filters):
        handler.addFilter(Redactor())

    if settings.mode is Mode.PRODUCTION:
        # Nothing at DEBUG in production: debug lines are where the unredacted
        # detail lives, and the redactor is a net rather than a guarantee.
        root.setLevel(max(root.level, logging.INFO))
