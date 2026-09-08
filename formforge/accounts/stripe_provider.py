"""Stripe, in test mode.

An adapter against the interface in `billing.py`, which is where all the
reasoning about what a renewal does to a ledger already lives and is already
tested. This file's whole job is translation: Stripe's vocabulary in, the
internal vocabulary out. Nothing downstream of `to_event` knows Stripe exists.

Three things here are security-relevant rather than merely functional:

**The signature is the authentication.** A webhook arrives with no session and
no credential; anyone can POST to the endpoint. `stripe.Webhook.construct_event`
verifies an HMAC over the exact raw body and rejects a timestamp outside its
tolerance window, which is what makes a forged `invoice.paid` -- free credits --
and a forged refund -- free money -- both impossible. The raw body must be
passed through byte-for-byte; re-serialising parsed JSON changes the bytes and
every signature then fails.

**Live keys are refused.** This phase is test mode only, so a key that does not
begin `sk_test_` is rejected at construction rather than at the first charge.
Getting this wrong means real customers' cards, which is not a thing to
discover from a support ticket.

**Credits are never granted from a redirect.** The browser coming back to a
success URL proves only that the browser came back. `checkout.session.completed`
arriving over a verified webhook is what proves payment, and it is the only
thing this adapter turns into a grant.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from . import plans
from .billing import BillingError, BillingEvent, SignatureError

log = logging.getLogger("formforge.accounts.stripe")

# Stripe's name for a thing -> ours. Anything not in here is acknowledged and
# ignored: Stripe sends dozens of event types, and a webhook endpoint that
# errors on the ones it does not care about looks like an outage on Stripe's
# dashboard and eventually gets disabled.
EVENT_MAP = {
    "checkout.session.completed": "subscription.activated",
    "invoice.paid": "subscription.renewed",
    "invoice.payment_succeeded": "subscription.renewed",
    "invoice.payment_failed": "payment.failed",
    "customer.subscription.deleted": "subscription.expired",
    "charge.refunded": "payment.refunded",
    "charge.dispute.created": "payment.refunded",
}

# `customer.subscription.updated` is only interesting when it reports a
# cancellation that has been scheduled but not yet taken effect.
UPDATED = "customer.subscription.updated"


class StripeProvider:
    """The Stripe adapter. Test mode only in this phase."""

    name = "stripe"

    def __init__(
        self,
        secret_key: str,
        webhook_secret: str,
        *,
        price_ids: dict[str, str] | None = None,
        success_url: str = "https://localhost/account?checkout=done",
        cancel_url: str = "https://localhost/pricing",
        allow_live: bool = False,
    ):
        import stripe

        if not secret_key:
            raise BillingError("Stripe needs a secret key")
        if not secret_key.startswith("sk_test_") and not allow_live:
            # Deliberately a hard failure. Phase 1 is test mode by decision,
            # and the failure mode of getting this wrong is charging real
            # cards, which no amount of care later undoes.
            raise BillingError(
                "refusing a non-test Stripe key: this phase is test mode only. "
                "Pass allow_live=True only when live billing has been signed off."
            )
        if not webhook_secret:
            # Without it `verify_webhook` cannot authenticate anything, and an
            # endpoint that accepts unverified billing events is worse than one
            # that does not exist.
            raise BillingError("Stripe needs a webhook signing secret")

        self._stripe = stripe
        self._client = stripe.StripeClient(secret_key)
        self.webhook_secret = webhook_secret
        self.test_mode = secret_key.startswith("sk_test_")
        self.price_ids = price_ids or _price_ids_from_env()
        self.success_url = success_url
        self.cancel_url = cancel_url

    # -- outbound ----------------------------------------------------------
    def create_customer(self, user_id: str, email: str) -> str:
        try:
            customer = self._client.customers.create(
                params={
                    "email": email,
                    # So a Stripe dashboard row can be traced back here without
                    # a database lookup. Metadata, not the customer id itself:
                    # our id should not be inferable from anything Stripe shows
                    # a support agent by default.
                    "metadata": {"formforge_user": user_id},
                }
            )
        except Exception as exc:
            raise BillingError(f"could not create a customer: {exc}") from exc
        return customer.id

    def start_subscription(self, customer_id: str, plan_id: str) -> dict[str, Any]:
        """A Checkout Session. We never see a card number.

        Checkout rather than an on-site card form is the whole PCI argument:
        the card details are entered on Stripe's page, in Stripe's iframe, and
        this process never touches them. That is worth more than the styling
        control an embedded form would buy.
        """
        tier = plans.get(plan_id)
        price = self.price_ids.get(tier.id)
        if not price:
            raise BillingError(f"no Stripe price configured for the {tier.id} plan")
        try:
            session = self._client.checkout.sessions.create(
                params={
                    "mode": "subscription",
                    "customer": customer_id,
                    "line_items": [{"price": price, "quantity": 1}],
                    "success_url": self.success_url,
                    "cancel_url": self.cancel_url,
                    # Echoed back on the completed event, so the handler knows
                    # which plan was bought without a second API call.
                    "metadata": {"formforge_plan": tier.id},
                    "subscription_data": {"metadata": {"formforge_plan": tier.id}},
                }
            )
        except Exception as exc:
            raise BillingError(f"could not start checkout: {exc}") from exc
        return {
            "checkout_url": session.url,
            "session_id": session.id,
            "plan_id": tier.id,
            "amount_usd": tier.price_usd,
            "test_mode": self.test_mode,
        }

    def cancel_subscription(self, customer_id: str) -> None:
        try:
            subs = self._client.subscriptions.list(
                params={"customer": customer_id, "status": "active", "limit": 1}
            )
            for subscription in subs.data:
                self._client.subscriptions.cancel(subscription.id)
        except Exception as exc:
            raise BillingError(f"could not cancel: {exc}") from exc

    # -- inbound -----------------------------------------------------------
    def verify_webhook(self, body: bytes, signature: str) -> BillingEvent:
        """Authenticate a delivery and normalise it.

        `body` must be the raw request bytes. FastAPI's `await request.body()`
        gives exactly that; anything that has been through `json.loads` and
        back has different bytes and will not verify.
        """
        try:
            event = self._stripe.Webhook.construct_event(
                body, signature or "", self.webhook_secret
            )
        except Exception as exc:
            raise SignatureError(f"webhook signature does not verify: {exc}") from exc
        return self.to_event(event)

    @staticmethod
    def to_event(event: Any) -> BillingEvent:
        """Stripe's event to ours, or a BillingError for one we do not act on."""
        kind = event["type"]
        obj = (event.get("data") or {}).get("object") or {}

        internal = EVENT_MAP.get(kind)
        # A scheduled cancellation. The subscription is still live and paid
        # up; `customer.subscription.deleted` is what says it has ended.
        if kind == UPDATED and obj.get("cancel_at_period_end"):
            internal = "subscription.cancelled"
        if internal is None:
            raise BillingError(f"no handler for Stripe event {kind!r}")

        customer = obj.get("customer") or obj.get("id")
        if not customer or not isinstance(customer, str):
            raise BillingError(f"Stripe {kind} names no customer")

        created = event.get("created")
        return BillingEvent(
            id=str(event["id"]),
            type=internal,
            customer_id=customer,
            plan_id=_plan_from(obj),
            credits=_credits_from(obj, internal),
            period_start=_period_from(obj),
            created=(
                datetime.fromtimestamp(created, tz=timezone.utc)
                if isinstance(created, (int, float))
                else None
            ),
            offline=False,
            raw=_trimmed(event),
        )


def _plan_from(obj: dict[str, Any]) -> str | None:
    metadata = obj.get("metadata") or {}
    plan = metadata.get("formforge_plan")
    return plan if plan in plans.PLANS else None


def _credits_from(obj: dict[str, Any], internal: str) -> int | None:
    """How many credits a refund should try to reverse.

    Only meaningful for a refund; every other event's credit count comes from
    the plan. Derived from the plan the payment was for rather than from the
    refunded amount, because a partial refund of a subscription is not
    proportionally a partial month of credits and pretending otherwise invents
    an arithmetic nobody agreed to.
    """
    if internal != "payment.refunded":
        return None
    plan = _plan_from(obj)
    return plans.get(plan).credits if plan else None


def _period_from(obj: dict[str, Any]) -> str | None:
    """The label for the period being billed.

    Stripe's own period start, so a renewal for a given month carries the same
    label however many times it is delivered -- which is what makes the grant
    idempotent across a redelivery.
    """
    for key in ("period_start", "current_period_start"):
        value = obj.get(key)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m")
    lines = (obj.get("lines") or {}).get("data") or []
    if lines:
        period = (lines[0] or {}).get("period") or {}
        start = period.get("start")
        if isinstance(start, (int, float)):
            return datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m")
    return None


# Fields worth keeping from a Stripe event. The full object is large, contains
# a good deal that is irrelevant, and is stored in a column somebody will read
# during a billing dispute -- so it is trimmed to what answers that question.
_KEEP = ("id", "type", "created", "livemode")
_KEEP_OBJECT = (
    "id", "customer", "status", "amount", "amount_refunded", "currency",
    "subscription", "cancel_at_period_end", "metadata",
)


def _trimmed(event: Any) -> dict[str, Any]:
    obj = (event.get("data") or {}).get("object") or {}
    return {
        **{k: event.get(k) for k in _KEEP if event.get(k) is not None},
        "object": {k: obj.get(k) for k in _KEEP_OBJECT if obj.get(k) is not None},
    }


def _price_ids_from_env() -> dict[str, str]:
    return {
        plan: os.environ[key]
        for plan, key in (
            ("maker", "STRIPE_PRICE_MAKER"),
            ("studio", "STRIPE_PRICE_STUDIO"),
        )
        if os.environ.get(key)
    }


def provider_from_env() -> StripeProvider | None:
    """Build a provider if the environment configures one, else None.

    Returning None rather than raising is what lets the gateway fall back to
    the offline provider: a deployment that has not configured Stripe is a
    development box, not a broken production one.
    """
    key = os.environ.get("STRIPE_SECRET_KEY")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not key or not secret:
        return None
    return StripeProvider(
        key,
        secret,
        success_url=os.environ.get(
            "STRIPE_SUCCESS_URL", "https://localhost/account?checkout=done"
        ),
        cancel_url=os.environ.get("STRIPE_CANCEL_URL", "https://localhost/pricing"),
        allow_live=os.environ.get("FORMFORGE_ALLOW_LIVE_BILLING") == "1",
    )
