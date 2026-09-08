# Phase 1 implementation plan

Scope: make the Phase 0 account and billing foundation usable through a real
HTTP surface. No UI redesign, no marketing site, no marketplace, no B2B, no
team accounts, no live charging.

## Environment (verified before planning, not assumed)

| Need | Status |
|---|---|
| PostgreSQL server | **16.13 running locally**, `citext` available |
| `psycopg` 3.3.5, `fastapi`, `stripe`, `boto3`, `httpx` | installed |
| `pgvector` | **not available** — only affects the templates embedding column, which is out of scope here |
| Stripe test-mode API key | **absent** — see "What cannot be verified here" |

## Decisions taken (2026-09-08, confirmed)

- **Refund** → claw back *unspent only, floored at zero*, as an `adjustment`
  ledger row carrying the Stripe event id. Never creates a debt.
- **past_due** → does **not** block spending credits already granted. No new
  grant until payment succeeds. Consistent with the cancellation rule.

## The load-bearing design choice

**One `AccountStore` over a pluggable dialect, not two implementations.**

The requirement is that Postgres preserve the SQLite ledger semantics exactly.
Two hand-written backends satisfy that on the day they are written and drift
afterwards, and the drift is invisible until it costs money. So the SQL is
written once and a small dialect object supplies the four things that actually
differ: placeholder style, the schema DDL, how a transaction starts, and how a
user's balance is locked for the read-then-write.

That lets the **entire Phase 0 test suite run against both backends** from one
fixture. "Postgres preserves the semantics" then stops being a claim and
becomes a test result.

Concurrency, per dialect:

- SQLite: `BEGIN IMMEDIATE` — one writer at a time, database-wide.
- Postgres: `BEGIN` plus `SELECT ... FROM users WHERE id = %s FOR UPDATE` —
  serialises spends *per user* rather than globally, which is both correct and
  strictly better under load. The row lock is what the concurrent-spend test
  exercises.

## Credit gate: when a credit is actually spent

One rule, which resolves retries, refreshes and mid-flight crashes together:

> A credit is spent **once per model**, at successful validation, under the
> idempotency key `spend:{model_id}`. A model's artifacts are downloadable iff
> a `spend` row exists for it and the requester owns it.

Consequences, all of them intended:

- A failed, refused, or timed-out build never reaches the deduction. Free.
- Re-downloading, refreshing, or retrying delivery costs nothing — the model is
  already paid for, and the ledger key makes a second attempt a no-op.
- A crash between validation and file delivery leaves the user charged *and*
  entitled: the model row and its artifacts persist, so the file is still
  theirs to fetch. That is the safe side to fail on.
- Balance is checked at submit (so compute is not spent on a user who cannot
  pay) and deducted after success. If a concurrent build takes the last credit
  in between, the second model is built but unpaid — retained, not deleted, and
  downloadable as soon as they have credits. Nobody is overcharged and nobody
  gets a free paid export.

Format entitlement is separate and comes from the plan: STL on free, all
formats on paid.

## Commits

1. **Postgres backend + migrations.** Dialect layer, numbered repeatable
   migrations with a `schema_migrations` ledger, `citext` for email. Phase 0's
   suite parametrised over both backends.
2. **Auth over HTTP.** signup / login / logout / me, session cookies
   (HttpOnly, Secure, SameSite), rate limiting on the credential endpoints,
   ownership checks. Password reset **deferred** — the project has no
   email-delivery direction, and a fake one is worse than an absent one.
3. **Stripe, test mode only.** Checkout (no card handling), verified webhooks,
   idempotent handling of activation / renewal / cancellation / payment
   failure / expiration / refund / duplicate / out-of-order.
4. **Credit gate, storage and downloads.** S3-compatible abstraction with a
   local implementation for tests, files outside any web root, authenticated
   per-owner expiring download tokens.
5. **Test-environment fix.** The FastAPI-dependent test currently fails
   unexplained; make it a declared optional-dependency skip.
6. **Docs.** architecture.md, monetization-site-spec.md, an API reference, a
   retention *proposal* (no automatic deletion).

## What cannot be verified in this environment

Stated now so no report later overclaims:

- **Stripe API calls need a `sk_test_...` key**, which this environment does
  not have. Webhook signature verification, event handling, idempotency and
  refund arithmetic are all HMAC and database work and are fully tested
  offline. Creating a real Checkout Session against Stripe's test API is
  tested against a fake transport, and will be marked *unverified against live
  test-mode* until a key is supplied.
- **pgvector** is unavailable, so the templates embedding column stays out of
  the migration set. Unrelated to billing.
