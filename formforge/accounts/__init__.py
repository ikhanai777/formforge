"""Accounts, credits and billing -- the orchestrator tier's, and only its.

Nothing in here may be imported by the geometry tier. That tier executes
generated code, and the entire reason it holds no credentials is that there is
nothing there to take; a billing secret reachable from inside the sandbox
would undo that in one import (`docs/architecture.md` section 4).

Phase 1 of `docs/monetization-site-spec.md`. What exists: identities,
sessions, an append-only credit ledger with the concurrent-spend and
replayed-webhook hazards guarded, plan definitions, a payment-processor
interface with both a working offline implementation and a Stripe adapter,
and a choice of SQLite or PostgreSQL underneath -- the same suite runs against
both, so the ledger semantics cannot drift between them.

The Stripe adapter is **test mode only** and refuses a live key unless
explicitly overridden. See `stripe_provider.StripeProvider`.
"""

from .auth import AuthError, hash_password, verify_password
from .billing import (
    BillingError,
    BillingEvent,
    BillingProvider,
    OfflineProvider,
    SignatureError,
    apply_event,
)
from .plans import PLANS, Plan
from .stripe_provider import StripeProvider, provider_from_env
from .store import (
    AccountError,
    AccountStore,
    DuplicateEmail,
    InsufficientCredits,
    LedgerEntry,
)

__all__ = [
    "PLANS",
    "AccountError",
    "AccountStore",
    "AuthError",
    "BillingError",
    "BillingEvent",
    "BillingProvider",
    "DuplicateEmail",
    "InsufficientCredits",
    "LedgerEntry",
    "OfflineProvider",
    "Plan",
    "SignatureError",
    "StripeProvider",
    "apply_event",
    "provider_from_env",
    "hash_password",
    "verify_password",
]
