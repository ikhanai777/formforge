"""Accounts, credits and billing -- the orchestrator tier's, and only its.

Nothing in here may be imported by the geometry tier. That tier executes
generated code, and the entire reason it holds no credentials is that there is
nothing there to take; a billing secret reachable from inside the sandbox
would undo that in one import (`docs/architecture.md` section 4).

Phase 0 of `docs/monetization-site-spec.md`. What exists: identities, sessions,
an append-only credit ledger with the concurrent-spend and replayed-webhook
hazards guarded, plan definitions, and a payment-processor interface with a
working offline implementation. What does not: an adapter for a real
processor, which needs open question 4 answered first.
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
    "apply_event",
    "hash_password",
    "verify_password",
]
