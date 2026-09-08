"""The payment processor, behind an interface, and what its events mean.

Which processor this will be is `docs/monetization-site-spec.md` open question
4, and it is still open. That is a reason to build this way rather than a
reason to wait: everything below the adapter -- what a renewal does to a
ledger, what a failed payment does to a plan -- is the same whoever takes the
card, and it is the part that can be got wrong in ways that cost real money.
So the processor is a small interface with a normalised event vocabulary, and
answering question 4 means writing one adapter against a handler that is
already tested.

`OfflineProvider` is a working implementation with no credentials, in the same
spirit as `OfflineClient` in `formforge/llm.py`: the whole flow -- signup,
subscribe, renew, cancel, spend -- runs end to end in CI and on a laptop, and
signature verification is real HMAC rather than a stub that returns True. What
it cannot do is move money, and it says so on every event it produces.

The trust boundary is `verify_webhook`. Everything arriving there is attacker
controlled until the signature checks out: a forged `subscription.renewed` is
free credits, and a forged `credits.purchased` is free money. Treat any
implementation of that method as security code.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import plans
from .store import AccountStore

# The internal vocabulary. Adapters translate their processor's event names
# into these; nothing downstream of an adapter knows what a processor calls
# anything. Keeping this small is deliberate -- each one is a distinct thing
# that happens to a balance, and an event type nobody acts on is a webhook
# handler that silently drops a payment.
EVENT_TYPES = frozenset(
    {
        "subscription.activated",   # a plan started: grant its allowance
        "subscription.renewed",     # a period rolled: expire, then grant
        "subscription.cancelled",   # back to free at the end of the period
        "payment.failed",           # mark past_due; do not strip credits
        "credits.purchased",        # pay-as-you-go top-up
    }
)


class BillingError(Exception):
    pass


class SignatureError(BillingError):
    """The payload did not come from the processor, or was tampered with."""


@dataclass(frozen=True, slots=True)
class BillingEvent:
    """One normalised thing that happened, whoever reported it."""

    id: str
    type: str
    customer_id: str
    plan_id: str | None = None
    credits: int | None = None
    period_start: str | None = None
    # True when this came from the offline stand-in, so nothing downstream can
    # mistake a simulated payment for a real one.
    offline: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class BillingProvider(Protocol):
    """What the rest of the system needs from a payment processor."""

    name: str

    def create_customer(self, user_id: str, email: str) -> str:
        """Register the payer, returning the processor's own id for them."""

    def start_subscription(self, customer_id: str, plan_id: str) -> dict[str, Any]:
        """Begin checkout for a plan. Returns whatever the caller must show the
        user next (usually a redirect URL)."""

    def cancel_subscription(self, customer_id: str) -> None: ...

    def verify_webhook(self, body: bytes, signature: str) -> BillingEvent:
        """Authenticate a webhook and normalise it. Raises SignatureError if it
        does not verify -- never returns an unverified event."""


class OfflineProvider:
    """A processor that keeps the books but cannot move money.

    Every event it produces carries `offline=True`. The signature scheme is
    real HMAC-SHA256 over the raw body, because a stubbed-out verifier is
    exactly the kind of thing that survives into production behind a config
    flag, and this way the code path that runs in tests is the same shape as
    the code path that will run against a real processor.
    """

    name = "offline"

    def __init__(self, secret: str | None = None):
        # A random secret when none is given: an offline provider that
        # accepted a well-known default signature would be worse than one that
        # cannot be called from outside the process at all.
        self.secret = secret or secrets.token_hex(32)

    def create_customer(self, user_id: str, email: str) -> str:
        return f"offline_cus_{user_id[:12]}"

    def start_subscription(self, customer_id: str, plan_id: str) -> dict[str, Any]:
        tier = plans.get(plan_id)
        return {
            "offline": True,
            "customer_id": customer_id,
            "plan_id": tier.id,
            "amount_usd": tier.price_usd,
            # No redirect: there is nowhere to send anyone. The caller is
            # expected to feed `sign()`/`verify_webhook` the activation event
            # itself, which is what the tests do.
            "checkout_url": None,
        }

    def cancel_subscription(self, customer_id: str) -> None:
        return None

    # -- the signature scheme ---------------------------------------------
    def sign(self, body: bytes) -> str:
        return hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    def verify_webhook(self, body: bytes, signature: str) -> BillingEvent:
        if not hmac.compare_digest(self.sign(body), signature or ""):
            raise SignatureError("webhook signature does not verify")
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise BillingError(f"webhook body is not JSON: {exc}") from exc
        return self.to_event(payload)

    @staticmethod
    def to_event(payload: dict[str, Any]) -> BillingEvent:
        kind = payload.get("type")
        if kind not in EVENT_TYPES:
            raise BillingError(f"unknown event type {kind!r}")
        if not payload.get("customer_id"):
            raise BillingError("event names no customer")
        return BillingEvent(
            id=str(payload.get("id") or f"offline_{time.time_ns()}"),
            type=kind,
            customer_id=str(payload["customer_id"]),
            plan_id=payload.get("plan_id"),
            credits=payload.get("credits"),
            period_start=payload.get("period_start"),
            offline=True,
            raw=payload,
        )


def apply_event(
    accounts: AccountStore, event: BillingEvent, *, provider: str = "offline"
) -> bool:
    """Turn a verified event into whatever it means for an account.

    Returns True if this call did the work, False if the event had already been
    applied. The order is load-bearing: the event is *recorded first*, then
    acted on, then marked handled. Acting first means a crash in the middle
    turns the processor's redelivery into a second charge; recording first
    means the redelivery is recognised and skipped, and a row left unhandled is
    visible to `unhandled_billing_events` rather than lost.

    The ledger writes underneath are independently idempotent on their own
    keys, so even a redelivery that slipped past the event check cannot double
    a grant.
    """
    user = accounts.user_for_billing_customer(event.customer_id)
    row_id, is_new = accounts.record_billing_event(
        provider,
        event.id,
        event.type,
        event.raw,
        user_id=user["id"] if user else None,
    )
    if not is_new:
        return False
    if user is None:
        # Left deliberately unhandled rather than dropped: a payment for a
        # customer id we do not recognise is a real problem -- a half-finished
        # signup, or two environments pointed at one processor account -- and
        # it should show up in the unhandled queue where someone will see it.
        raise BillingError(f"no account for billing customer {event.customer_id!r}")

    user_id = user["id"]
    # An adapter is expected to supply the period the processor is billing
    # for; the current month is a fallback for one that does not. Note what
    # that fallback costs: two renewals in the same calendar month with no
    # period_start collapse onto one idempotency key, and the second grants
    # nothing. That is the safe direction to fail -- a missed grant is a
    # support ticket, a doubled one is money -- but an adapter that relies on
    # it is wrong, and this is the line to look at when a customer says a
    # renewal did not land.
    period = event.period_start or time.strftime("%Y-%m", time.gmtime())

    if event.type in ("subscription.activated", "subscription.renewed"):
        accounts.start_period(user_id, period, plan_id=event.plan_id or user["plan"])
        accounts.set_plan_status(user_id, "active")
    elif event.type == "subscription.cancelled":
        # Plan drops to free, credits already granted are left alone. They were
        # paid for; taking them back at cancellation would be charging for a
        # month and then confiscating it.
        accounts.set_plan_status(user_id, "cancelled")
    elif event.type == "payment.failed":
        # Marked, not stripped. A failed renewal is usually an expired card,
        # and deleting the balance of someone who is about to fix it is how a
        # recoverable billing problem becomes a cancelled account.
        accounts.set_plan_status(user_id, "past_due")
    elif event.type == "credits.purchased":
        credits = int(event.credits or 0)
        if credits <= 0:
            raise BillingError("credits.purchased names no credits")
        accounts.grant(
            user_id,
            credits,
            reason="purchase",
            idempotency_key=f"purchase:{provider}:{event.id}",
            note=f"pay-as-you-go, ${credits * plans.PAY_AS_YOU_GO_USD:.2f}",
        )
    else:  # pragma: no cover - EVENT_TYPES is checked before we get here
        raise BillingError(f"no handler for {event.type!r}")

    accounts.mark_billing_event_handled(row_id)
    return True
