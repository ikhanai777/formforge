"""Session cookies, rate limiting, and what a browser is allowed to be told.

Three small things that are easy to get subtly wrong and expensive to get
wrong quietly.

**Cookies.** The session cookie is `HttpOnly` so a script cannot read it,
`Secure` so it never crosses plain HTTP, and `SameSite=Lax` so it is not sent
on cross-site POSTs. Lax rather than Strict because the Stripe checkout return
is a top-level GET back to us, and Strict would drop the cookie on exactly that
navigation -- the user would come back from paying and appear logged out.

**Rate limiting.** A fixed-window counter per client per endpoint. Its limits
are honest about what it is: see `RateLimiter` for what it does not protect
against, because a rate limiter that is believed to do more than it does is
worse than none.

**Redaction.** `public_account` and `public_ledger` are the only shapes that
reach a browser. Account ids, billing-processor customer ids, password hashes,
session digests, ledger row ids and idempotency keys are all deliberately
absent. None of them is a secret the user could not eventually infer, and all
of them are things an attacker enumerating the API would rather have than not.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..accounts import plans

# Set on a development box serving plain HTTP. Off by default: a session cookie
# without Secure is one downgraded request away from being someone else's.
COOKIE_INSECURE = os.environ.get("FORMFORGE_COOKIE_INSECURE") == "1"
COOKIE_NAME = "formforge_session"

# Matches auth.SESSION_TTL. The cookie should not outlive the session it names,
# or a browser keeps presenting a token the server has already forgotten.
COOKIE_MAX_AGE = 30 * 24 * 3600


def cookie_kwargs() -> dict[str, Any]:
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "secure": not COOKIE_INSECURE,
        "samesite": "lax",
        "path": "/",
        "max_age": COOKIE_MAX_AGE,
    }


@dataclass
class _Window:
    started: float
    count: int = 0


class RateLimiter:
    """A fixed-window counter, per client and endpoint.

    What it stops: someone pointing a script at `/v1/auth/login` and working
    through a password list, or signing up ten thousand accounts from one host.

    **What it does not stop, stated so nobody relies on it:**

    - *A distributed attempt.* The key is the client address, so a thousand
      addresses get a thousand budgets.
    - *Anything, across more than one process.* The counters live in this
      process's memory. Four gunicorn workers mean four independent budgets and
      an effective limit four times the configured one. A deployment that needs
      a real limit needs a shared one (Redis, or the load balancer's); this is
      the floor, not the ceiling.
    - *A burst at a window boundary.* A fixed window allows up to twice the
      limit across two adjacent windows. A sliding window or a token bucket
      would not, at the cost of more state. Against credential stuffing, the
      difference does not matter; against a precise rate ceiling it would.

    It is here because the alternative -- no limit at all on the endpoints that
    take a password -- is indefensible, not because it is sufficient on its own.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[tuple[str, str], _Window] = {}

    def check(self, key: str, scope: str, *, limit: int, per_seconds: float) -> bool:
        """True if this request is within budget. Consumes one unit if so."""
        now = time.monotonic()
        with self._lock:
            window = self._windows.get((scope, key))
            if window is None or now - window.started >= per_seconds:
                window = _Window(started=now)
                self._windows[(scope, key)] = window
            if window.count >= limit:
                return False
            window.count += 1
            # Opportunistic sweep. Without it the dict grows one entry per
            # distinct client address forever, which is a slow memory leak that
            # only shows up in production.
            if len(self._windows) > 4096:
                self._windows = {
                    k: w for k, w in self._windows.items()
                    if now - w.started < per_seconds
                }
            return True

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


# Budgets. Deliberately generous enough that a person who forgot their password
# is not locked out, and tight enough that a script is.
LOGIN_LIMIT = (10, 300.0)      # ten attempts per five minutes
SIGNUP_LIMIT = (5, 3600.0)     # five new accounts an hour from one address
CHECKOUT_LIMIT = (20, 3600.0)


@dataclass
class Redacted:
    """The account, as a browser is allowed to see it."""

    data: dict[str, Any] = field(default_factory=dict)


def public_account(account: dict[str, Any]) -> dict[str, Any]:
    """What `/v1/auth/me` returns.

    No `user_id`: the browser never needs it -- every endpoint resolves the
    account from the session cookie -- and an id the client knows is an id an
    attacker can try substituting into a request. No `billing_customer_id`
    either; that one names the user inside the payment processor.
    """
    tier = plans.get(account["plan"])
    return {
        "email": account["email"],
        "plan": tier.id,
        "plan_name": tier.name,
        "plan_status": account["plan_status"],
        "credits": int(account["balance"]),
        "formats": list(tier.formats),
        "batch": tier.batch,
    }


# How a ledger reason is described to the person it happened to. The internal
# vocabulary leaks implementation ("adjustment" means a payment reversal here,
# and nothing to a customer), so it is translated rather than passed through.
_REASON_TEXT = {
    "grant": "Plan credits added",
    "purchase": "Credits purchased",
    "spend": "Model built",
    "refund": "Credit returned",
    "expiry": "Unused credits expired",
    "adjustment": "Billing adjustment",
}


def public_ledger(entries: list[Any]) -> list[dict[str, Any]]:
    """The credit history, as a browser is allowed to see it.

    Row ids and idempotency keys are dropped. The idempotency key in
    particular is a giveaway: it embeds the account id and the processor's
    event id, which is exactly the internal detail that should not leave the
    server.
    """
    return [
        {
            "when": entry.created_at,
            "change": entry.delta,
            "kind": entry.reason,
            "description": _REASON_TEXT.get(entry.reason, "Adjustment"),
            "model_id": entry.model_id,
        }
        for entry in entries
    ]
