"""What guards every route, declared once so nothing can be added unguarded.

Phase 1 gated the download path and left the model reads open. Phase 2 caught
that, gated every route with a model id *in the path*, and missed
`POST /v1/feedback`, which takes one in the body -- so an anonymous caller
could still write to the one table this system cannot reconstruct. Twice, the
same shape of mistake: a route existed that nobody had decided the policy for.

Grepping for guards does not fix that. It finds what is there and says nothing
about what should be, and it is exactly as good as the regex on the day it was
written -- when this file was being planned, a scan reported the WebSocket as
unguarded (it is not) and `/v1/feedback` as guarded (it was not).

So the policy is declared, not inferred, and the declaration is load-bearing in
two directions:

* A route with no entry here **fails the test suite**. Adding one means
  deciding what protects it, in a file review will notice.
* An entry with `resource` set is **driven as a real request** -- anonymous,
  cross-account, closed-account, expired-session, tampered-identifier -- so the
  declaration cannot drift into a comment that used to be true.

The entries are about *authorisation*, not about what else a route does. That a
build also costs a credit is recorded in `consumes_credit`, and tested, but it
is a separate question from who is allowed to ask.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Policy(StrEnum):
    """How a route decides whether the caller may proceed."""

    # Open on every deployment, deliberately. The catalogue and the probes:
    # nothing here is about a particular account.
    PUBLIC = "public"
    # Unauthenticated because the caller cannot yet have a session -- signing
    # up, signing in, asking for a reset. Rate limited rather than
    # authenticated, and each has its own enumeration defence.
    SELF_SERVICE = "self_service"
    # Requires a session. No user-owned identifier is involved, so the account
    # is whatever the cookie resolves to and there is nothing to substitute.
    SESSION = "session"
    # Requires a session *and* ownership of the identifier in the request.
    # The one that matters: everything under here answers 404 rather than 403
    # for somebody else's resource, so it is not an existence oracle.
    OWNER = "owner"
    # The bearer token carries its own authorisation. Ownership was checked
    # when it was minted and is rechecked when it is redeemed.
    SIGNED_TOKEN = "signed_token"
    # No session exists -- the processor has none -- so the signature is the
    # authentication.
    WEBHOOK_SIGNATURE = "webhook_signature"
    # Not served at all on a metered deployment. Business aggregates and
    # deployment description belong to whoever runs the instance; hosted, the
    # operator uses the CLI.
    OPERATOR_ONLY = "operator_only"


@dataclass(frozen=True, slots=True)
class RoutePolicy:
    method: str
    path: str
    policy: Policy
    # The user-owned identifier this route resolves, and where it arrives.
    # `None` means the route resolves none, which is itself a claim the tests
    # check: a route that grows one and forgets to update this fails.
    resource: str | None = None
    location: str = "path"          # path | body | token
    consumes_credit: bool = False
    note: str = ""


# Every route the metered gateway serves. Ordered by area rather than by path,
# because the question a reader has is "what protects the model routes", not
# "what is alphabetically first".
POLICIES: tuple[RoutePolicy, ...] = (
    # -- generation and models ------------------------------------------
    RoutePolicy("POST", "/v1/generate", Policy.SESSION, consumes_credit=True,
                note="balance checked before the sandbox runs; charged after validation"),
    RoutePolicy("GET", "/v1/models/{model_id}", Policy.OWNER, "model_id",
                note="returns prompt, params and generated source"),
    RoutePolicy("GET", "/v1/models/{model_id}/status", Policy.OWNER, "model_id"),
    RoutePolicy("GET", "/v1/models/{model_id}/events", Policy.OWNER, "model_id",
                note="the per-step log, including validator findings"),
    RoutePolicy("WEBSOCKET", "/v1/models/{model_id}/stream", Policy.OWNER, "model_id",
                note="closes 1008 with the same message for unknown and not-yours"),
    RoutePolicy("POST", "/v1/models/{model_id}/modify", Policy.OWNER, "model_id",
                consumes_credit=True,
                note="a build: runs the sandbox, so it costs a credit like any other"),
    RoutePolicy("POST", "/v1/models/{model_id}/slice", Policy.OWNER, "model_id"),
    RoutePolicy("GET", "/v1/models/{model_id}/download", Policy.OWNER, "model_id",
                note="also requires a spend row for the model, and a plan "
                     "that allows the format"),
    RoutePolicy("POST", "/v1/models/{model_id}/download-link", Policy.OWNER, "model_id",
                note="entitlement checked at minting, so a token cannot exist for a file "
                     "its holder could not already fetch"),
    RoutePolicy("GET", "/v1/download/{token}", Policy.SIGNED_TOKEN, "token", "token",
                note="entitlement rechecked on redemption: a refund can land in between"),
    RoutePolicy("POST", "/v1/feedback", Policy.OWNER, "model_id", "body",
                note="SEC-2. The id is in the body, which is how this was missed twice; "
                     "print_feedback is the one table that cannot be reconstructed"),

    # -- accounts ---------------------------------------------------------
    RoutePolicy("POST", "/v1/auth/signup", Policy.SELF_SERVICE,
                note="rate limited; 409 discloses only that an address is taken"),
    RoutePolicy("POST", "/v1/auth/login", Policy.SELF_SERVICE,
                note="one message for a wrong password and an unknown account"),
    RoutePolicy("POST", "/v1/auth/logout", Policy.SELF_SERVICE,
                note="idempotent, and safe without a session"),
    RoutePolicy("POST", "/v1/auth/reset/request", Policy.SELF_SERVICE,
                note="202 whether or not the account exists, including when rate limited"),
    RoutePolicy("POST", "/v1/auth/reset/confirm", Policy.SELF_SERVICE,
                note="the token is the credential; unknown, expired and spent answer alike"),
    RoutePolicy("GET", "/v1/auth/me", Policy.SESSION),
    RoutePolicy("GET", "/v1/account/credits", Policy.SESSION,
                note="ledger redacted: no row ids, no idempotency keys"),
    RoutePolicy("GET", "/v1/account/history", Policy.SESSION,
                note="scoped in the SQL query, not filtered afterwards"),

    # -- billing ----------------------------------------------------------
    RoutePolicy("POST", "/v1/billing/checkout", Policy.SESSION,
                note="grants nothing; only a verified webhook does"),
    RoutePolicy("POST", "/v1/billing/webhook", Policy.WEBHOOK_SIGNATURE,
                note="attacker-controlled until construct_event verifies the raw body"),

    # -- catalogue and probes ---------------------------------------------
    RoutePolicy("GET", "/v1/templates", Policy.PUBLIC, note="the catalogue is the product"),
    RoutePolicy("GET", "/v1/templates/{template_id}", Policy.PUBLIC,
                note="template_id names a shipped template, not anybody's resource"),
    RoutePolicy("POST", "/v1/templates/{template_id}/build", Policy.SESSION,
                resource="template_id", consumes_credit=True,
                note="builds a shipped template from parameters the caller "
                     "chose; the id names a template, not anybody's model"),
    RoutePolicy("GET", "/v1/profiles", Policy.PUBLIC, note="printer profiles, static"),
    RoutePolicy("GET", "/healthz", Policy.PUBLIC, note="one bit when metered"),
    RoutePolicy("GET", "/readyz", Policy.PUBLIC, note="reports whether, never what"),

    # -- operator ---------------------------------------------------------
    RoutePolicy("GET", "/v1/meta", Policy.OPERATOR_ONLY),
    RoutePolicy("GET", "/v1/stats", Policy.OPERATOR_ONLY, note="business aggregates"),
    RoutePolicy("GET", "/v1/stats/prints", Policy.OPERATOR_ONLY),

    # -- the browser front end -------------------------------------------
    # Markup only. Every one of these returns the same bytes to everybody,
    # signed in or not, and holds no data: the pages fetch from the JSON API
    # above, which does the authorising. `/models/{model_id}` carries an
    # identifier in its path and deliberately declares `resource=None`,
    # because it does not resolve it -- it ignores it. `tests/test_web.py`
    # holds that claim to account by asking for two different ids and
    # comparing the responses byte for byte.
    RoutePolicy("GET", "/", Policy.PUBLIC, note="markup only"),
    RoutePolicy("GET", "/signup", Policy.PUBLIC, note="markup only"),
    RoutePolicy("GET", "/login", Policy.PUBLIC, note="markup only"),
    RoutePolicy("GET", "/dashboard", Policy.PUBLIC,
                note="markup only; the data behind it needs a session"),
    RoutePolicy("GET", "/create", Policy.PUBLIC,
                note="markup only; the data behind it needs a session"),
    RoutePolicy("GET", "/studio", Policy.PUBLIC,
                note="markup only; its build button needs a session"),
    RoutePolicy("GET", "/models/{model_id}", Policy.PUBLIC,
                note="markup only; identical for every id, and resolves none"),
    RoutePolicy("GET", "/forgot-password", Policy.PUBLIC, note="markup only"),
    RoutePolicy("GET", "/reset-password", Policy.PUBLIC, note="markup only"),
    RoutePolicy("GET", "/account", Policy.PUBLIC,
                note="markup only; the data behind it needs a session"),
    RoutePolicy("GET", "/static/app.css", Policy.PUBLIC, note="stylesheet"),
    RoutePolicy("GET", "/static/app.js", Policy.PUBLIC, note="script"),
)

BY_ROUTE: dict[tuple[str, str], RoutePolicy] = {
    (entry.method, entry.path): entry for entry in POLICIES
}

# Policies that must refuse a caller with no session. Everything else is
# either deliberately open or authenticated by something other than a cookie.
NEEDS_SESSION = frozenset({Policy.SESSION, Policy.OWNER, Policy.OPERATOR_ONLY})

# Policies that must refuse a caller who holds a *valid* session for a
# different account. This is the cross-account case, and it is the one a
# session check alone does not cover.
NEEDS_OWNERSHIP = frozenset({Policy.OWNER})


def _iter_routes(routes):
    """Flatten `app.routes`, following anything that wraps other routes.

    Necessary, not defensive. FastAPI does not always put an included router's
    routes directly into `app.routes` -- some versions insert a wrapper object
    holding the original router -- and a naive walk therefore sees the wrapper,
    finds no `path` on it, and skips *every route the router contributed*.

    That was not hypothetical: the first version of this walked `app.routes`
    flatly and silently missed all ten account and billing routes, which is
    precisely the "a whole area is unchecked while the coverage test reports
    green" failure this module exists to prevent. There is a test below that
    the walk finds them.
    """
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _iter_routes(getattr(original, "routes", ()))
            continue
        if hasattr(route, "path"):
            yield route
        elif hasattr(route, "routes"):
            yield from _iter_routes(route.routes)


def registered_routes(app) -> set[tuple[str, str]]:
    """Every (method, path) the app actually serves.

    Read off the running application rather than parsed out of the source, so
    a route added through a router, a decorator or anything else is still seen.
    """
    found: set[tuple[str, str]] = set()
    for route in _iter_routes(app.routes):
        path = getattr(route, "path", None)
        if not path or path.startswith(("/openapi", "/docs", "/redoc")):
            continue
        methods = getattr(route, "methods", None)
        if methods:
            for method in methods:
                if method not in ("HEAD", "OPTIONS"):
                    found.add((method, path))
        else:
            # A WebSocket route has no `methods`.
            found.add(("WEBSOCKET", path))
    return found


def undeclared(app) -> set[tuple[str, str]]:
    """Routes the app serves that nobody has declared a policy for."""
    return registered_routes(app) - set(BY_ROUTE)


def stale(app) -> set[tuple[str, str]]:
    """Declared policies for routes that no longer exist.

    Worth failing on too: a registry describing a route that was renamed is a
    registry somebody will trust the rest of.
    """
    return set(BY_ROUTE) - registered_routes(app)
