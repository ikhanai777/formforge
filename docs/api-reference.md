# API reference — accounts, credits and billing

The routes added in Phase 1. Everything here exists **only when the gateway is
started with an account store**; without one the API is what it always was and
none of these paths are registered. That is what keeps the CLI, the MCP server
and self-hosted copies unmetered.

```python
from formforge.accounts import AccountStore
from formforge.api import create_app

app = create_app(accounts=AccountStore("postgresql://…"))   # metered
app = create_app()                                          # free, as before
```

## Authentication

A session cookie, `formforge_session`. `HttpOnly` so script cannot read it,
`Secure` so it never crosses plain HTTP, `SameSite=Lax` so it is not sent on
cross-site POSTs — Lax rather than Strict because the checkout return is a
top-level GET and Strict would drop the cookie on exactly that navigation.

Set `FORMFORGE_COOKIE_INSECURE=1` to drop `Secure` on a plain-HTTP development
box. Do not set it anywhere else.

**No endpoint takes an account identifier.** The account is whatever the cookie
resolves to. There is therefore no id for a caller to substitute for somebody
else's, which makes "users only reach their own data" a property of the routing
rather than a check each handler has to remember.

### `POST /v1/auth/signup` → 201

```json
{"email": "maker@example.com", "password": "at least ten characters"}
```

Returns the account (see **The account shape**) and sets the session cookie.
`409` if the address is registered, `422` if the password is under ten
characters, `429` if rate limited.

### `POST /v1/auth/login` → 200

Same body, same response. `401` for both a wrong password and an unknown
account, with an identical message — the difference is an oracle for which
addresses have accounts here.

### `POST /v1/auth/logout` → 204

Revokes the session server-side *and* clears the cookie. Clearing the cookie
alone would leave a token that still authenticates anyone who kept a copy.

### `GET /v1/auth/me` → 200 · `401`

### The account shape

Every account-bearing response returns exactly this:

```json
{
  "email": "maker@example.com",
  "plan": "maker",
  "plan_name": "Maker",
  "plan_status": "active",
  "credits": 57,
  "formats": ["stl", "3mf", "step", "source"],
  "batch": false
}
```

Deliberately **not** present, anywhere, ever: the account id, the payment
processor's customer id, the password hash, session digests, ledger row ids and
idempotency keys. The idempotency key is the sharpest of these — it embeds both
the account id and the processor's event id.

## Credits and history

### `GET /v1/account/credits` → 200 · `401`

The account shape plus `history`, a redacted ledger:

```json
{"when": "2026-09-08T19:14:48+00:00", "change": -1,
 "kind": "spend", "description": "Model built", "model_id": "…"}
```

### `GET /v1/account/history` → 200 · `401`

This account's models. Scoped by the session in the SQL query, not filtered
afterwards — an ownership filter that runs in Python is one refactor away from
being dropped.

## Billing

### `POST /v1/billing/checkout` → 200 · `401` · `429` · `502`

```json
{"plan": "maker"}    →    {"checkout_url": "https://checkout.stripe.com/…",
                           "plan": "maker", "amount_usd": 9.0}
```

**This grants nothing.** A started checkout is not a settled payment, and the
browser returning to the success URL proves only that the browser returned.
Credits come from the webhook and from nowhere else.

### `POST /v1/billing/webhook` → 200 · `400`

Unauthenticated by design — the processor has no session — so **the signature
is the authentication**. Stripe's `Stripe-Signature` header, or
`X-FormForge-Signature` for the offline provider. `400` when it does not
verify, and nothing is applied.

Always `200` for a duplicate: a redelivery is a success from the processor's
point of view, and any other answer makes it retry something already done.

Handled events, and what each does:

| Stripe event | Effect |
|---|---|
| `checkout.session.completed` | Expire the remainder, grant the plan's credits, mark active |
| `invoice.paid` / `invoice.payment_succeeded` | Same, for a renewal |
| `customer.subscription.updated` with `cancel_at_period_end` | Mark cancelled. Credits kept — they were paid for |
| `customer.subscription.deleted` | Back to the free plan. Credits still kept; the remainder expires at the next period roll, visibly |
| `invoice.payment_failed` | Mark `past_due`. Credits kept **and still spendable** |
| `charge.refunded`, `charge.dispute.created` | Claw back what is unspent, floored at zero |

Anything else is refused with a `400` and left in the unhandled queue rather
than silently dropped.

**Idempotency and ordering.** Every event is recorded before it is acted on, so
a redelivery is recognised; every balance movement additionally carries its own
idempotency key, so even an event that slipped past that check cannot double a
grant. Status changes carry the processor's own timestamp, because a status is
last-write-wins and a cancellation delivered *after* the renewal it preceded
would otherwise leave a paid-up account cancelled.

## Model routes

**On a metered deployment every model route requires a session and checks
ownership** — `GET /v1/models/{id}`, `/status`, `/events`, the
`WS /stream` socket, `POST /modify`, `POST /slice` and both download routes.
A model that is not yours answers `404`, never `403`: a 403 confirms the id
names something real and turns any of these into an oracle for enumerating
other people's models. A model with no owner is refused too — on a metered
deployment that is a bug, and the safe reading of a bug is no.

Ownership is read from the in-flight job *and* the persisted row, because a
generation that is still running has not been written to `models` yet.

`POST /modify` is a build: it runs the sandbox and produces a downloadable
model, so it takes a credit and is checked exactly like `POST /v1/generate`.
The child model inherits the parent's owner.

`GET /v1/stats` and `/v1/stats/prints` are **not served** on a metered
deployment (`404`). They are business aggregates — totals, per-template health,
cost — and belong to whoever runs the instance rather than to whoever can reach
it. Self-hosted, those are the same person and the routes stay open; hosted,
the operator reads them from the CLI.

Fixed in Phase 2: before it, all of the above were open to an anonymous
request, and `modify` would start a generation from a stranger's model with no
owner and no credit check.

## Downloads

Two routes, one entitlement rule.

**A model's paid artifacts are downloadable iff a `spend` ledger row exists for
it and the requester owns it.** Entitlement is a ledger fact, not a flag.

`report` and `params` are never paywalled: a user arguing about a charge needs
to see what the build measured, and paywalling the evidence makes that argument
unanswerable.

### `GET /v1/models/{id}/download?format=…` → 200

`401` not signed in · `404` not yours (deliberately not `403`, which would
confirm the id names something real and turn this into an enumeration oracle) ·
`402` not paid for · `403` format not in your plan.

### `POST /v1/models/{id}/download-link?format=…` → 200

```json
{"url": "/v1/download/<token>", "expires_in": 300, "format": "stl"}
```

For handing to a download manager, or to a bucket. All the entitlement checks
above run here, at minting time, so a token can only exist for a file its
holder was already allowed to fetch.

### `GET /v1/download/{token}` → 200 · `403` · `402` · `404`

No cookie required — that is the point of the token, and also its cost. **It is
a bearer credential**: anyone holding the URL inside its five minutes can fetch
that one file. The mitigations are the ones that matter — unguessable without
the signing key, naming exactly one account, one model and one format inside
the signature, and short-lived. Entitlement is rechecked on redemption, because
a refund can land in between and a token is not a promise about the future.

Set `FORMFORGE_LINK_SECRET` to run more than one worker; unset, each process
generates its own and a restart invalidates outstanding links.

## Errors

A database failure, a Stripe failure and a bug all become the same `500` with
one fixed message. The detail goes to the log, where it is useful and not
adversary-readable. `4xx` messages say what the user can do about it and
nothing about how the system works.

## Rate limits

| Endpoint | Budget |
|---|---|
| `POST /v1/auth/signup` | 5 per hour per address |
| `POST /v1/auth/login` | 10 per five minutes per address |
| `POST /v1/billing/checkout` | 20 per hour per address |

`429` when exceeded. **What this does not do**, so nobody relies on it: it is
per-process, so N workers means an effective limit of N times the configured
one; it keys on the client address, so a distributed attempt gets a budget per
address; and a fixed window permits a burst across a boundary. It is the floor.
A deployment needing a real limit needs a shared one — Redis, or the load
balancer's.

## Password reset — deferred, not missing

There is no reset endpoint, deliberately. It needs somewhere to send a mail and
this project has no email-delivery direction: no provider, no sender identity,
no domain authentication, no template pipeline. The choice was between a flow
that cannot deliver anything and a visible gap, and a reset that silently fails
is worse — it looks like a working recovery path to everyone including the
person who most needs it.

To build it, these have to be decided first:

1. A delivery provider, and who owns the sending domain.
2. SPF/DKIM/DMARC on that domain, or the mail lands in spam and the feature is
   nominally present and actually broken.
3. Token lifetime and single-use policy.
4. Whether a reset revokes existing sessions. It should — an account recovered
   from someone else's hands is the case that matters — and `revoke_all_sessions`
   is already there for it.

Until then, a locked-out user needs an operator. That is a real limitation and
belongs on the launch checklist, not in a backlog.

## Retention — a proposal, not a policy

**Nothing is deleted automatically. No code in this phase removes an artifact.**
`Storage.delete` exists and is called by nothing. What follows is a proposal for
approval.

| Class | Proposed | Why |
|---|---|---|
| Free-tier artifacts | 30 days | Bounds the cost of the tier that pays nothing, and a free user is not building a library |
| Paid artifacts | Kept while the account is open | It is what they paid for; `/account/history` is a listed feature |
| Artifacts of a closed account | 30 days after closure, then deleted | Long enough to undo a mistaken closure, short enough not to hold data nobody asked us to keep |
| `credit_ledger` | Never deleted | It is the answer to "why was I charged", and a deleted ledger row cannot be reconstructed |
| `billing_events` | 7 years | Financial records; align with whatever the eventual tax jurisdiction requires |
| `sessions` | Purge revoked and expired rows after 90 days | No value after expiry, and they name accounts |

Three things to settle before any of this is implemented:

1. **Warning before deletion.** Deleting a customer's file without telling them
   is the kind of thing that ends up in a review. A mail before expiry needs the
   same email infrastructure the password reset is waiting on.
2. **Does deletion mean the model row too?** It should not. The row is what
   `print_feedback` joins to, and that table is the only ground truth this
   system has for whether any of it prints. Delete the bytes, keep the record.
3. **Legal hold.** A disputed charge should freeze deletion for that account
   until it resolves.
