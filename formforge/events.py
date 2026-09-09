"""The catalogue of security- and billing-sensitive events.

Every event this system emits about *who did what with money or access* is
declared here, once, as an object -- not as a string at the call site. That is
the whole design, and it buys three things a documented list cannot:

**A typo is an import error.** `emit(log, LOGIN_FAILED, ...)` cannot be
misspelled into an event nobody is watching for. `emit(log, "auth.login.faled")`
can, and the failure mode is a dashboard that reads zero and looks healthy.

**The catalogue cannot drift from the code**, because it *is* the code. There
is no second list to update. `tests/test_events.py` walks this module and
asserts every declared event is referenced from somewhere in `formforge/`, so
a spec that stops being emitted is a test failure rather than a quiet gap.

**Fields are declared, so a missing one is visible.** An `authz.denied` without
the route it denied is not an alertable event; it is a number. `emit` marks an
emission that is missing a required field rather than dropping it, because a
logging call that raises in production turns an observability gap into an
outage.

What is deliberately *not* here: request logs, generation progress, anything
about geometry. Those live at DEBUG and INFO through the ordinary logger. This
module is for the events an operator would be asked about after an incident --
"who logged in", "what was charged", "what was refused" -- and the list is
short on purpose, because a catalogue that includes everything is a catalogue
nobody reads.

Every value still passes through `logs.Redactor` on the way out. The rule for
authors is unchanged and this module does not relax it: **never put a
password, session token, reset token, API key, signature or card into an
event.** See `tests/test_events.py::TestNoEventCarriesASecret`, which drives
the real HTTP paths and greps the emitted lines.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .logs import event as _event


@dataclass(frozen=True, slots=True)
class EventSpec:
    """One declared event.

    `alert` is the operator-facing half: it says what a rise in this event
    means, or is empty when the event is for reconstructing what happened
    rather than for waking anybody. An event with no answer to "and then what"
    is noise, so writing it down is a check on whether the event is worth
    emitting at all.
    """

    name: str
    what: str
    fields: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    level: int = logging.INFO
    alert: str = ""

    def __str__(self) -> str:  # so an f-string in a message reads correctly
        return self.name


# ---------------------------------------------------------------------------
# Accounts and sessions
# ---------------------------------------------------------------------------

SIGNUP_SUCCEEDED = EventSpec(
    "auth.signup.succeeded",
    "an account was created and its opening credits granted",
    fields=("user_id", "plan"),
    alert="a spike is either a launch or an abuse run; correlate with"
          " credit.granted volume",
)

SIGNUP_REJECTED = EventSpec(
    "auth.signup.rejected",
    "a signup was refused (duplicate address, or a password that is too short)",
    fields=("reason",),
    level=logging.INFO,
)

LOGIN_SUCCEEDED = EventSpec(
    "auth.login.succeeded",
    "a password was accepted and a session issued",
    fields=("user_id",),
    alert="not on its own; the pair with auth.login.failed is what matters",
)

LOGIN_FAILED = EventSpec(
    "auth.login.failed",
    "a password was rejected, or the address had no account, or the account"
    " is closed -- the log does not distinguish them, and neither does the"
    " response",
    fields=("reason",),
    level=logging.WARNING,
    alert="a sustained rise in failures against a flat auth.login.succeeded is"
          " credential stuffing",
)

LOGOUT = EventSpec(
    "auth.logout",
    "a session was revoked at its owner's request",
    fields=("user_id",),
)

SESSION_REJECTED = EventSpec(
    "auth.session.rejected",
    "a request presented a session token that is unknown, expired, or belongs"
    " to a closed account",
    fields=("reason",),
    alert="a rise after a deploy usually means sessions were invalidated"
          " unintentionally",
)

ACCOUNT_CLOSED = EventSpec(
    "account.closed",
    "an account was soft-closed: logins refused, sessions revoked, ledger kept",
    fields=("user_id", "actor", "sessions_revoked"),
    level=logging.WARNING,
    alert="any closure by actor=operator is worth a look; users close their own",
)

ACCOUNT_REOPENED = EventSpec(
    "account.reopened",
    "a closed account was reopened by an operator",
    fields=("user_id", "actor"),
    level=logging.WARNING,
    alert="always. Reopening is an operator action on somebody else's account",
)


# ---------------------------------------------------------------------------
# Password reset
#
# The reset token itself never appears in any of these. It is a bearer
# credential for one account, and a log that holds it is a log that grants
# access to whoever can read it -- which, for a shipped log pipeline, is more
# people than can read the database.
# ---------------------------------------------------------------------------

RESET_REQUESTED = EventSpec(
    "password_reset.requested",
    "a reset was asked for. Emitted for an unknown address too, with"
    " known=false, because the HTTP response cannot distinguish them and the"
    " log should not be the thing that does",
    fields=("known",),
    optional=("user_id",),
    alert="many requests with known=false is address enumeration",
)

RESET_COMPLETED = EventSpec(
    "password_reset.completed",
    "a reset token was spent and the password changed",
    fields=("user_id", "sessions_revoked"),
    level=logging.WARNING,
    alert="one per genuine reset. A second for the same user_id means the"
          " token was reused, which the store refuses -- so it would be a bug",
)

RESET_REJECTED = EventSpec(
    "password_reset.rejected",
    "a reset token was refused: unknown, already spent, or past its expiry",
    fields=("reason",),
    level=logging.WARNING,
    alert="a rise is somebody guessing tokens",
)

RESET_UNDELIVERABLE = EventSpec(
    "password_reset.undeliverable",
    "the token was issued and the email did not send. The user is now holding"
    " a reset they cannot see",
    fields=("error",),
    level=logging.ERROR,
    alert="always. This is a support ticket that has not been filed yet",
)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

AUTHZ_DENIED = EventSpec(
    "authz.denied",
    "a request was refused for want of ownership or a session. Answered to the"
    " caller as 404, not 403 -- a 403 confirms the id names something real --"
    " but recorded here as what it was",
    fields=("route", "reason"),
    optional=("user_id", "model_id"),
    level=logging.WARNING,
    alert="one account generating many of these across ids it does not own is"
          " enumeration",
)

RATE_LIMITED = EventSpec(
    "rate_limit.denied",
    "a request was refused by the limiter",
    fields=("bucket",),
    optional=("route",),
    level=logging.WARNING,
    alert="sustained denials on the auth buckets is an attack; on the build"
          " bucket it is usually a limit set too low",
)


# ---------------------------------------------------------------------------
# Credits
#
# The ledger is the record of what happened to money; these events are the
# record of *when the application decided* it should. They are not the source
# of truth -- `credit_ledger` is -- and a reconciliation reads the table, not
# the log. What the log adds is the timing and the request context the table
# does not hold.
# ---------------------------------------------------------------------------

CREDIT_GRANTED = EventSpec(
    "credit.granted",
    "credits were added: a signup grant, a renewal, a purchase, or an"
    " operator adjustment",
    fields=("user_id", "credits", "reason"),
    optional=("idempotency_key",),
    alert="grants with reason=adjustment are hand-made; they should be rare",
)

CREDIT_SPENT = EventSpec(
    "credit.spent",
    "a validated build was charged",
    fields=("user_id", "credits", "model_id"),
    alert="not on its own. A build finishing with no matching credit.spent is"
          " the interesting case, and it is a gap rather than an event",
)

CREDIT_REFUSED = EventSpec(
    "credit.refused",
    "a build was refused for an empty balance. Not an error: it is the"
    " paywall working",
    fields=("user_id", "balance", "requested"),
    alert="a rise right after a renewal date means renewals are not landing",
)

CREDIT_REFUNDED = EventSpec(
    "credit.refunded",
    "credits were returned, or clawed back on a refunded payment. Clawing"
    " back takes unspent credits only and floors at zero -- it never creates"
    " a debt",
    fields=("user_id", "credits", "reason"),
    level=logging.WARNING,
    alert="always worth a look; a refund is a customer who was unhappy",
)

CREDIT_EXPIRED = EventSpec(
    "credit.expired",
    "unused credits were expired at a period boundary. A visible ledger row,"
    " not a silent reset",
    fields=("user_id", "credits", "period"),
    alert="a large total on the first of the month is normal; at any other"
          " time it is a bug in period rollover",
)


# ---------------------------------------------------------------------------
# Billing
#
# `payload` is never a field on any of these. A processor's webhook body holds
# customer names, addresses and card metadata; the raw body is what the
# signature is computed over and is kept only long enough to verify it.
# ---------------------------------------------------------------------------

CHECKOUT_STARTED = EventSpec(
    "billing.checkout.started",
    "a checkout session was created for a user",
    fields=("user_id", "plan"),
)

CHECKOUT_FAILED = EventSpec(
    "billing.checkout.failed",
    "the processor refused to create a checkout session",
    fields=("error",),
    level=logging.ERROR,
    alert="always. Nobody can pay while this is happening",
)

WEBHOOK_ACCEPTED = EventSpec(
    "billing.webhook.accepted",
    "a webhook verified, was recorded, and was applied",
    fields=("provider", "event_id", "event_type"),
    optional=("user_id",),
)

WEBHOOK_DUPLICATE = EventSpec(
    "billing.webhook.duplicate",
    "a webhook was recognised as one already handled and was not applied"
    " again. Expected: processors redeliver on purpose",
    fields=("provider", "event_id"),
    alert="no. This event firing is the replay guard working",
)

WEBHOOK_STALE = EventSpec(
    "billing.webhook.stale",
    "a status-changing webhook arrived after a newer one and was skipped."
    " Balance movements are order-safe on their own; statuses are"
    " last-write-wins and need this",
    fields=("provider", "event_id", "event_type"),
)

WEBHOOK_SIGNATURE_FAILED = EventSpec(
    "billing.webhook.signature_failed",
    "a webhook did not verify against the signing secret and was refused"
    " before it was parsed",
    fields=("provider",),
    level=logging.WARNING,
    alert="always. It is either a forged webhook or the wrong signing secret"
          " deployed, and the second one means real payments are being dropped",
)

WEBHOOK_UNUSABLE = EventSpec(
    "billing.webhook.unusable",
    "a webhook verified and could not be understood -- an event shape this"
    " build does not map",
    fields=("provider", "error"),
    level=logging.WARNING,
    alert="a processor changed a shape, or a new event type was enabled",
)

WEBHOOK_NOT_APPLIED = EventSpec(
    "billing.webhook.not_applied",
    "a webhook verified and could not be applied. Somebody may have paid and"
    " not received what they paid for",
    fields=("provider", "event_id", "error"),
    level=logging.ERROR,
    alert="always, and it is the one billing failure no error rate catches --"
          " the endpoint returned 200 and the work never happened. Cross-check"
          " with `unhandled_billing_events`",
)


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

ARTIFACT_SERVED = EventSpec(
    "artifact.served",
    "a generated file was handed to a caller",
    fields=("model_id", "fmt", "via"),
    optional=("user_id",),
)

ARTIFACT_LINK_REJECTED = EventSpec(
    "artifact.link_rejected",
    "a signed download link was refused: expired, altered, or for another"
    " model. The token itself is not logged",
    fields=("reason",),
    level=logging.WARNING,
    alert="a rise is somebody editing links; a steady trickle is people"
          " clicking old ones",
)

ARTIFACT_GONE = EventSpec(
    "artifact.gone",
    "a file was requested that retention has already deleted. Answered 410,"
    " because 404 would suggest it never existed",
    fields=("model_id", "fmt"),
)

ARTIFACT_MARKED = EventSpec(
    "artifact.marked",
    "files were marked for deletion. Reversible: nothing is removed until a"
    " confirmed sweep",
    fields=("model_id", "count", "actor"),
)

ARTIFACT_DELETED = EventSpec(
    "artifact.deleted",
    "the bytes of a marked artifact were removed. The record stays, marked"
    " deleted, which is what stops it being reconsidered every sweep",
    fields=("model_id", "fmt"),
    level=logging.WARNING,
    alert="a large batch outside a scheduled sweep is a retention setting"
          " somebody changed",
)


def catalogue() -> tuple[EventSpec, ...]:
    """Every declared event, in declaration order.

    Read from this module's own globals rather than a hand-maintained tuple,
    so a spec that exists is in the catalogue and there is no second list to
    forget.
    """
    return tuple(
        value for value in globals().values() if isinstance(value, EventSpec)
    )


def by_name() -> dict[str, EventSpec]:
    return {spec.name: spec for spec in catalogue()}


def emit(logger: logging.Logger, spec: EventSpec, **fields: Any) -> None:
    """Log one catalogued event.

    A missing required field is *marked*, not raised. A logging call that
    raises turns a gap in observability into an outage, and the emission with
    `_incomplete` on it is both still useful and impossible to miss in a
    search. `tests/test_events.py` asserts no path emits an incomplete event,
    so the marker should never appear in a real deployment.
    """
    missing = [name for name in spec.fields if name not in fields]
    if missing:
        fields = {**fields, "_incomplete": ",".join(missing)}
    _event(logger, spec.name, level=spec.level, **fields)


def describe() -> str:
    """The catalogue as text, for `formforge events` and the docs.

    Generated rather than transcribed: a documented list of events that is
    maintained separately from the events is a list that is wrong.
    """
    lines = []
    for spec in catalogue():
        lines.append(spec.name)
        lines.append(f"    {spec.what}")
        shape = ", ".join(spec.fields)
        if spec.optional:
            shape += f" [{', '.join(spec.optional)}]"
        lines.append(f"    fields: {shape}")
        lines.append(f"    level:  {logging.getLevelName(spec.level)}")
        if spec.alert:
            lines.append(f"    alert:  {spec.alert}")
        lines.append("")
    return "\n".join(lines)
