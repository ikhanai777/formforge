"""Accounts, credits and billing over HTTP.

Mounted only when the gateway is started with an account store. Without one
the API behaves exactly as it did before any of this existed, which is what
keeps the free self-hosted path free: no signup, no session, no credit check,
no billing routes at all. See `create_app`.

    POST   /v1/auth/signup        -> 201, sets the session cookie
    POST   /v1/auth/login         -> 200, sets the session cookie
    POST   /v1/auth/logout        -> 204, revokes the session
    GET    /v1/auth/me            -> the account, redacted
    GET    /v1/account/credits    -> balance and history, redacted
    GET    /v1/account/history    -> this account's models
    POST   /v1/billing/checkout   -> a processor checkout URL
    POST   /v1/billing/webhook    -> the processor's callback

**Password reset is deliberately absent.** It needs somewhere to send a mail,
and this project has no email-delivery direction -- no provider, no sender
identity, no template pipeline, nothing. The options were to build a reset flow
that cannot deliver anything, or to leave the gap visible. A reset endpoint
that silently fails is worse than no endpoint: it looks like a working recovery
path to everyone including the person who most needs it. See
`docs/api-reference.md` for what has to be decided before it can exist.

Two rules run through every handler here:

*Authorisation is by session, never by identifier.* No endpoint takes an
account id. The account is whatever the session cookie resolves to, so there
is no id for a caller to substitute for somebody else's.

*Errors say what the user can do about it and nothing else.* A database error,
a Stripe error and a bug all become the same 500 with a fixed message. The
detail goes to the log, where it is useful and not adversary-readable.
"""

# No `from __future__ import annotations` in this module, deliberately.
# FastAPI resolves endpoint annotations with `get_type_hints`, which evaluates
# string annotations against the *module* globals -- and the request models and
# `Request`/`Response` here are locals of `build_accounts_router`. With
# postponed evaluation they cannot be resolved, and FastAPI silently falls back
# to treating every one of them as a query parameter, so every endpoint 422s.

import logging
from typing import Any

from ..accounts import (
    AccountError,
    AccountStore,
    AuthError,
    BillingError,
    DuplicateEmail,
    SignatureError,
    apply_event,
    plans,
)
from ..accounts.auth import MIN_PASSWORD_LENGTH
from .security import (
    CHECKOUT_LIMIT,
    COOKIE_NAME,
    LOGIN_LIMIT,
    SIGNUP_LIMIT,
    RateLimiter,
    cookie_kwargs,
    public_account,
    public_ledger,
)

log = logging.getLogger("formforge.api.accounts")

GENERIC_ERROR = "Something went wrong. Please try again."


def build_accounts_router(
    accounts: AccountStore,
    provider: Any,
    *,
    limiter: RateLimiter | None = None,
    store: Any = None,
):
    """The account/billing routes, as a FastAPI router."""
    from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
    from pydantic import BaseModel, Field

    router = APIRouter()
    limits = limiter or RateLimiter()

    class Credentials(BaseModel):
        email: str = Field(max_length=320)
        # Validated for length here as well as in `hash_password`, so the
        # message a user sees names the rule rather than being a generic 500
        # from four frames down.
        password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)

    class CheckoutRequest(BaseModel):
        plan: str = Field(pattern="^(maker|studio)$")

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def guard(request: Request, scope: str, budget: tuple[int, float]) -> None:
        limit, per = budget
        if not limits.check(client_key(request), scope, limit=limit, per_seconds=per):
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Please wait a few minutes and try again.",
            )

    def current_user(
        formforge_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    ) -> dict[str, Any]:
        """The account behind the session cookie, or 401.

        Every protected endpoint depends on this and on nothing else. There is
        no path that takes an account identifier from the caller, which is what
        makes "users can only reach their own data" a property of the routing
        rather than a check each handler has to remember.
        """
        user = accounts.user_for_token(formforge_session or "")
        if user is None:
            raise HTTPException(status_code=401, detail="Not signed in.")
        return user

    def sign_in(response: Response, user: dict[str, Any]) -> dict[str, Any]:
        token = accounts.create_session(user["id"])
        response.set_cookie(value=token, **cookie_kwargs())
        # A free account that has not been touched this month is rolled here,
        # since nothing bills it and no renewal webhook will ever arrive.
        # Idempotent on the period, so signing in twice grants once.
        with _quiet("rolling the free period"):
            accounts.roll_to_current_period(user["id"])
        return public_account(accounts.account(user["id"]))

    # -- auth --------------------------------------------------------------
    @router.post("/v1/auth/signup", status_code=201)
    async def signup(body: Credentials, request: Request, response: Response):
        guard(request, "signup", SIGNUP_LIMIT)
        try:
            user = accounts.create_user(body.email, body.password)
        except DuplicateEmail:
            # Deliberately the same shape of answer as a successful signup
            # would give a *different* address: 409 with no detail about the
            # existing account. It still discloses that the address is taken,
            # which is unavoidable for a signup form; what it must not do is
            # say anything about the account itself.
            raise HTTPException(
                status_code=409, detail="That email address is already registered."
            ) from None
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return sign_in(response, user)

    @router.post("/v1/auth/login")
    async def login(body: Credentials, request: Request, response: Response):
        guard(request, "login", LOGIN_LIMIT)
        try:
            user = accounts.authenticate(body.email, body.password)
        except AuthError:
            # One message for a wrong password and for no such account. The
            # difference is an oracle for which addresses have accounts here.
            raise HTTPException(
                status_code=401, detail="Email or password is incorrect."
            ) from None
        return sign_in(response, user)

    @router.post("/v1/auth/logout", status_code=204)
    async def logout(
        response: Response,
        formforge_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    ):
        """Revoke the session server-side, then clear the cookie.

        Both halves matter. Clearing the cookie alone leaves a token that still
        authenticates anyone who kept a copy -- which is the whole point of
        logging out on a shared machine.
        """
        if formforge_session:
            with _quiet("revoking a session"):
                accounts.revoke_session(formforge_session)
        response.delete_cookie(COOKIE_NAME, path="/")
        return Response(status_code=204)

    @router.get("/v1/auth/me")
    async def me(
        user: dict[str, Any] = Depends(current_user),
    ):
        return public_account(accounts.account(user["id"]))

    # -- credits -----------------------------------------------------------
    @router.get("/v1/account/credits")
    async def credits(
        user: dict[str, Any] = Depends(current_user),
        limit: int = 50,
    ):
        account = accounts.account(user["id"])
        return {
            **public_account(account),
            "history": public_ledger(accounts.ledger(user["id"], limit=min(limit, 200))),
        }

    @router.get("/v1/account/history")
    async def history(
        user: dict[str, Any] = Depends(current_user),
        limit: int = 50,
    ):
        """This account's models. Scoped by the session, never by a parameter."""
        if store is None:
            return {"models": []}
        return {"models": _own_models(store, accounts, user["id"], min(limit, 200))}

    # -- billing -----------------------------------------------------------
    @router.post("/v1/billing/checkout")
    async def checkout(
        body: CheckoutRequest,
        request: Request,
        user: dict[str, Any] = Depends(current_user),
    ):
        """Start a subscription. Returns somewhere to send the browser.

        No credits are granted here, and none are granted when the user comes
        back to the success page either. A checkout that has been *started* is
        not a payment that has *settled*, and the only thing that knows the
        difference is the processor's signed webhook.
        """
        guard(request, "checkout", CHECKOUT_LIMIT)
        tier = plans.get(body.plan)
        try:
            customer = user["billing_customer_id"]
            if not customer:
                customer = provider.create_customer(user["id"], user["email"])
                accounts.set_billing_customer(user["id"], customer)
            session = provider.start_subscription(customer, tier.id)
        except BillingError as exc:
            log.warning("checkout failed for an account: %s", exc)
            raise HTTPException(
                status_code=502,
                detail="Could not reach the payment provider. Please try again.",
            ) from None
        return {
            "checkout_url": session.get("checkout_url"),
            "plan": tier.id,
            "amount_usd": tier.price_usd,
        }

    @router.post("/v1/billing/webhook")
    async def webhook(request: Request):
        """The processor's callback. The only thing that grants credits.

        Unauthenticated by design -- the processor has no session -- so the
        signature *is* the authentication. Everything in the body is attacker
        controlled until `verify_webhook` says otherwise: a forged
        `subscription.renewed` is free credits and a forged `credits.purchased`
        is free money.
        """
        body = await request.body()
        signature = request.headers.get("stripe-signature") or request.headers.get(
            "x-formforge-signature", ""
        )
        try:
            event = provider.verify_webhook(body, signature)
        except SignatureError:
            log.warning("rejected a webhook whose signature did not verify")
            raise HTTPException(status_code=400, detail="Invalid signature.") from None
        except BillingError as exc:
            log.warning("rejected an unusable webhook: %s", exc)
            raise HTTPException(status_code=400, detail="Unusable event.") from None

        try:
            applied = apply_event(accounts, event, provider=provider.name)
        except BillingError as exc:
            # The event is recorded and left unhandled, which is what
            # `unhandled_billing_events` exists to surface. A 400 tells the
            # processor to stop retrying something we will never accept; a 500
            # asks it to retry, which is right when the failure might be
            # transient. This one is not.
            log.error("could not apply a verified webhook: %s", exc)
            raise HTTPException(status_code=400, detail="Event could not be applied.") from None
        except Exception:
            log.exception("unexpected failure applying a webhook")
            raise HTTPException(status_code=500, detail=GENERIC_ERROR) from None
        # 200 either way: a duplicate delivery is a success from the
        # processor's point of view, and telling it otherwise makes it retry
        # something already done.
        return {"received": True, "applied": applied}

    router.current_user = current_user  # type: ignore[attr-defined]
    return router


def _own_models(store: Any, accounts: AccountStore, user_id: str, limit: int) -> list[dict]:
    """Models belonging to one account, with their paid status.

    Reads through the telemetry store but filters on `user_id` in the query
    rather than after it, so a bug in this function cannot leak somebody else's
    row into the response.
    """
    rows = []
    for record in store.models_for_user(user_id, limit=limit):
        paid = accounts.spend_for_model(record["id"])
        rows.append(
            {
                "model_id": record["id"],
                "template": record.get("template_id"),
                "status": record.get("status"),
                "created_at": record.get("created_at"),
                "paid": paid is not None,
            }
        )
    return rows


class _quiet:
    """Swallow a failure in something that must not break the request around it.

    Used only where the operation is a convenience -- rolling a free period,
    revoking a session that is about to be discarded anyway. Never around a
    credit movement.
    """

    def __init__(self, what: str):
        self.what = what

    def __enter__(self) -> None:
        return None

    def __exit__(self, kind, value, tb) -> bool:
        if value is not None and isinstance(value, (AccountError, AuthError)):
            log.warning("ignored a failure while %s: %s", self.what, value)
            return True
        return False
