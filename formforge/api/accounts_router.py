"""Accounts, credits and billing over HTTP.

Mounted only when the gateway is started with an account store. Without one
the API behaves exactly as it did before any of this existed, which is what
keeps the free self-hosted path free: no signup, no session, no credit check,
no billing routes at all. See `create_app`.

    POST   /v1/auth/signup        -> 201, sets the session cookie
    POST   /v1/auth/login         -> 200, sets the session cookie
    POST   /v1/auth/logout        -> 204, revokes the session
    POST   /v1/auth/reset/request -> 202 always, whether or not the account exists
    POST   /v1/auth/reset/confirm -> 204, then every session is dead
    GET    /v1/auth/me            -> the account, redacted
    GET    /v1/account/credits    -> balance and history, redacted
    GET    /v1/account/history    -> this account's models
    POST   /v1/billing/checkout   -> a processor checkout URL
    POST   /v1/billing/webhook    -> the processor's callback

**Password reset** runs against whatever `Mailer` the configuration selects.
Locally that is an outbox writing `.eml` files, so the whole flow -- request,
receive a link, use it once, watch the old sessions die -- works from a clean
clone with no provider and no credential. Production refuses to start with
reset enabled and email set to anything but SMTP, because a reset written to a
local outbox nobody reads is a recovery path that looks like it works.

The request endpoint answers the same way whether or not the address has an
account. That is the whole design: any difference -- status, body, or an
obviously different latency -- turns it into a way to ask which addresses have
accounts here.

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
from ..accounts.email import Mailer, Message, open_mailer
from ..config import Settings
from ..events import (
    CHECKOUT_FAILED,
    CHECKOUT_STARTED,
    LOGIN_FAILED,
    LOGIN_SUCCEEDED,
    LOGOUT,
    RATE_LIMITED,
    RESET_COMPLETED,
    RESET_REJECTED,
    RESET_REQUESTED,
    RESET_UNDELIVERABLE,
    SESSION_REJECTED,
    SIGNUP_REJECTED,
    SIGNUP_SUCCEEDED,
    WEBHOOK_NOT_APPLIED,
    WEBHOOK_SIGNATURE_FAILED,
    WEBHOOK_UNUSABLE,
    emit,
)
from .security import (
    CHECKOUT_LIMIT,
    COOKIE_NAME,
    LOGIN_LIMIT,
    RESET_ADDRESS_LIMIT,
    RESET_CONFIRM_LIMIT,
    RESET_REQUEST_LIMIT,
    SIGNUP_LIMIT,
    RateLimiter,
    cookie_kwargs,
    open_rate_limiter,
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
    mailer: Mailer | None = None,
    settings: Settings | None = None,
    reset_base_url: str = "",
):
    """The account/billing routes, as a FastAPI router."""
    from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
    from pydantic import BaseModel, Field

    router = APIRouter()
    config = settings or Settings.from_env()
    limits = limiter or open_rate_limiter(config)
    post = mailer or open_mailer(config)

    class Credentials(BaseModel):
        email: str = Field(max_length=320)
        # Validated for length here as well as in `hash_password`, so the
        # message a user sees names the rule rather than being a generic 500
        # from four frames down.
        password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)

    class CheckoutRequest(BaseModel):
        plan: str = Field(pattern="^(maker|studio)$")

    class ResetRequest(BaseModel):
        email: str = Field(max_length=320)

    class ResetConfirm(BaseModel):
        token: str = Field(min_length=16, max_length=512)
        password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def guard(request: Request, scope: str, budget: tuple[int, float]) -> None:
        limit, per = budget
        if not limits.check(client_key(request), scope, limit=limit, per_seconds=per):
            # The bucket, not the client address. Which bucket is saturated is
            # what an operator acts on; who saturated it is in the access log
            # and does not need a second copy in the security stream.
            emit(log, RATE_LIMITED, bucket=scope, route=str(request.url.path))
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
            # `reason` is deliberately coarse. The store answers None for an
            # absent cookie, an unknown token, an expired one and a closed
            # account alike, and inventing a finer reason here would mean
            # telling them apart -- which the endpoint refuses to do.
            emit(log, SESSION_REJECTED,
                 reason="absent" if not formforge_session else "not_current")
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
            emit(log, SIGNUP_REJECTED, reason="duplicate_address")
            # Deliberately the same shape of answer as a successful signup
            # would give a *different* address: 409 with no detail about the
            # existing account. It still discloses that the address is taken,
            # which is unavoidable for a signup form; what it must not do is
            # say anything about the account itself.
            raise HTTPException(
                status_code=409, detail="That email address is already registered."
            ) from None
        except AuthError as exc:
            emit(log, SIGNUP_REJECTED, reason="password_rejected")
            raise HTTPException(status_code=400, detail=str(exc)) from None
        emit(log, SIGNUP_SUCCEEDED, user_id=user["id"], plan=user["plan"])
        return sign_in(response, user)

    @router.post("/v1/auth/login")
    async def login(body: Credentials, request: Request, response: Response):
        guard(request, "login", LOGIN_LIMIT)
        try:
            user = accounts.authenticate(body.email, body.password)
        except AuthError:
            # One message for a wrong password and for no such account. The
            # difference is an oracle for which addresses have accounts here --
            # and the log must not become the oracle the response refuses to
            # be, so `reason` is the same word for both and the address is not
            # a field.
            emit(log, LOGIN_FAILED, reason="rejected")
            raise HTTPException(
                status_code=401, detail="Email or password is incorrect."
            ) from None
        emit(log, LOGIN_SUCCEEDED, user_id=user["id"])
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
            user = accounts.user_for_token(formforge_session)
            with _quiet("revoking a session"):
                accounts.revoke_session(formforge_session)
            if user is not None:
                emit(log, LOGOUT, user_id=user["id"])
        response.delete_cookie(COOKIE_NAME, path="/")
        return Response(status_code=204)

    @router.post("/v1/auth/reset/request", status_code=202)
    async def request_reset(body: ResetRequest, request: Request):
        """Start a password reset. Answers 202 whether or not the account exists.

        Two rate limits rather than one, because the two attacks are different.
        Many addresses from one client is somebody harvesting which addresses
        have accounts. Many attempts at one address is somebody burying a real
        reset mail under noise, or hoping a token lands somewhere they can see.

        Both limits answer 202 as well. A 429 here would leak exactly what the
        generic 202 exists to hide -- keep asking about an address and the
        moment the answer changes is the moment you have learned something.
        """
        if not config.password_reset_enabled:
            raise HTTPException(
                status_code=404, detail="Password reset is not enabled."
            )
        generic = {"status": "accepted"}
        address = (body.email or "").strip().lower()
        if not limits.check(client_key(request), "reset", limit=RESET_REQUEST_LIMIT[0],
                            per_seconds=RESET_REQUEST_LIMIT[1]):
            return generic
        if not limits.check(address, "reset-address", limit=RESET_ADDRESS_LIMIT[0],
                            per_seconds=RESET_ADDRESS_LIMIT[1]):
            return generic

        user = accounts.get_user_by_email(address)
        if user is None:
            # Deliberately no early return above this point: the answer, the
            # status and the shape are identical for an address with no
            # account. Logged without the address, because a log of every
            # address somebody probed for is the same list the endpoint is
            # refusing to hand out.
            emit(log, RESET_REQUESTED, known=False)
            return generic

        try:
            token = accounts.create_password_reset(user["id"])
            link = f"{reset_base_url}/reset?token={token}" if reset_base_url else token
            post.send(Message(
                to=user["email"],
                subject="Reset your FormForge password",
                body=(
                    "Somebody asked to reset the password for this FormForge "
                    "account.\n\n"
                    f"Use this within 30 minutes:\n\n    {link}\n\n"
                    "It works once. If this was not you, nothing has changed "
                    "and you can ignore this message."
                ),
            ))
            emit(log, RESET_REQUESTED, known=True, user_id=user["id"])
        except Exception as exc:
            # Never surfaced: a failure here that reached the caller would
            # distinguish "we tried to mail this account" from "there is no
            # account", which is the distinction the endpoint exists to hide.
            # It is loud in the log instead, because the user is now holding a
            # reset token they will never see.
            emit(log, RESET_UNDELIVERABLE, error=type(exc).__name__,
                 user_id=user["id"])
            log.exception("could not send a password reset")
        return generic

    @router.post("/v1/auth/reset/confirm", status_code=204)
    async def confirm_reset(body: ResetConfirm, request: Request, response: Response):
        """Redeem a token and set a new password.

        On success every session for the account is revoked, including the one
        making this request. That is the point rather than a side effect: the
        case that matters is an account being recovered *from* somebody, and
        leaving their session alive would make the whole thing theatre.
        """
        if not config.password_reset_enabled:
            raise HTTPException(
                status_code=404, detail="Password reset is not enabled."
            )
        guard(request, "reset-confirm", RESET_CONFIRM_LIMIT)
        user_id = accounts.consume_password_reset(body.token)
        if user_id is None:
            # Unknown, expired and already-used all answer the same. Telling
            # them apart tells somebody holding a stolen token which kind of
            # stolen it is -- so the log does not tell them apart either, and
            # the token is not a field on this event.
            emit(log, RESET_REJECTED, reason="invalid_or_expired")
            raise HTTPException(
                status_code=400, detail="This reset link is invalid or has expired."
            )
        try:
            killed = accounts.reset_password(user_id, body.password)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        emit(log, RESET_COMPLETED, user_id=user_id, sessions_revoked=killed)
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
            emit(log, CHECKOUT_FAILED, error=type(exc).__name__, user_id=user["id"])
            log.warning("checkout failed for an account: %s", exc)
            raise HTTPException(
                status_code=502,
                detail="Could not reach the payment provider. Please try again.",
            ) from None
        emit(log, CHECKOUT_STARTED, user_id=user["id"], plan=tier.id)
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
            # Neither the body nor the signature is a field: the body is
            # attacker-controlled and unverified, and logging it would put
            # whatever somebody chose to POST into the security stream.
            emit(log, WEBHOOK_SIGNATURE_FAILED, provider=provider.name)
            raise HTTPException(status_code=400, detail="Invalid signature.") from None
        except BillingError as exc:
            emit(log, WEBHOOK_UNUSABLE, provider=provider.name,
                 error=type(exc).__name__)
            raise HTTPException(status_code=400, detail="Unusable event.") from None

        try:
            applied = apply_event(accounts, event, provider=provider.name)
        except BillingError as exc:
            # The event is recorded and left unhandled, which is what
            # `unhandled_billing_events` exists to surface. A 400 tells the
            # processor to stop retrying something we will never accept; a 500
            # asks it to retry, which is right when the failure might be
            # transient. This one is not.
            emit(log, WEBHOOK_NOT_APPLIED, provider=provider.name,
                 event_id=event.id, error=type(exc).__name__)
            raise HTTPException(status_code=400, detail="Event could not be applied.") from None
        except Exception as exc:
            emit(log, WEBHOOK_NOT_APPLIED, provider=provider.name,
                 event_id=event.id, error=type(exc).__name__)
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
