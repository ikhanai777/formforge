# Staging validation

The script a person runs, in order, on a private staging deployment, before
anyone outside the team is invited. It exists because everything up to this
point has been validated against fakes — an offline billing adapter, an email
outbox on disk, a local directory standing in for object storage — and a fake
transport agrees with whatever you wrote. This is where the real ones get
asked.

**Nothing here is automated, and that is deliberate.** Each step ends in a
judgement a person makes by looking. A script that decided "the reset email
arrived" would be checking that the code sent something, which is already
covered offline; the point of this pass is that a human read the mail.

> **Live billing is out of scope for this phase.** Every step below runs
> against Stripe **test mode**. `formforge preflight` refuses to validate a
> live configuration, and the integration tests refuse a key that is not
> `sk_test_`. Live billing needs its own pass, and its own decision.

---

## Before you start

You will need, and this document does not contain any of them:

- a Stripe **test-mode** secret key, webhook signing secret and price ids;
- S3-compatible bucket credentials and, for a non-AWS provider, an endpoint;
- SMTP credentials and an address you can actually read;
- a PostgreSQL DSN for staging, plus a second, **disposable** one;
- a domain with TLS, and somewhere to see the logs.

None of these belong in the repository. Put them in the deployment's own
secret store; `.env.example` names every variable and holds no value.

---

## 1. Configuration, before anything is running

```sh
python -m formforge.cli preflight --environment staging
```

Read every line. The command separates two things a checklist usually
conflates:

| Status | Means | Do |
|---|---|---|
| `missing` | not configured yet | finish the deployment |
| `FAILED` | configured, and the thing it names refused us | fix it now |
| `WARN` | legal but probably not what you meant | decide |
| `SKIPPED` | deliberately not checked here | nothing |

It exits non-zero only on `FAILED`. Re-run until `ready` is true.

Preflight probes nothing that is not configured — it never invents a host to
try — and prints no secret in either output mode. If you see a key, a password
or a DSN in its output, stop and report it; that is a bug in preflight, not a
finding about your environment.

**Checkpoint.** `preflight --environment staging` exits 0 and reports ready.

---

## 2. The database, and getting back from losing it

```sh
python -m formforge.cli backup create
python -m formforge.cli backup verify backups/….dump
```

Then rehearse the restore against the **disposable** database — never the
staging one:

```sh
FORMFORGE_IT_PG_RESTORE=1 \
FORMFORGE_IT_DISPOSABLE_PG="$DISPOSABLE_DSN" \
  python -m pytest tests/integration/test_restore_rehearsal.py -v
```

This dumps, drops the schema, restores, and reads the ledger back through the
application's own store. `FORMFORGE_IT_DISPOSABLE_PG` must name a database with
`test` or `disposable` in it or the fixture refuses to run — it drops schemas.

**Checkpoint.** Five rehearsal tests pass, and you have a verified archive of
staging you would be willing to restore from.

---

## 3. Object storage

```sh
FORMFORGE_IT_S3=1 \
FORMFORGE_IT_S3_BUCKET="$BUCKET" \
FORMFORGE_IT_S3_ENDPOINT="$ENDPOINT_OR_EMPTY" \
  python -m pytest tests/integration/test_contracts.py::TestS3CompatibleStorage -v
```

`S3Storage` has been unverified against a real endpoint since it was written.
This is the pass that changes that: a round trip, a missing key raising rather
than returning empty, an idempotent delete, and a key that cannot escape its
prefix.

Then do it through the product, because the test exercises the adapter and not
the wiring:

1. sign in, generate a model, download the STL;
2. confirm the object appears in the bucket under the expected prefix;
3. `formforge artifacts show <model_id>` agrees with what is in the bucket.

**Checkpoint.** A file you generated in a browser is a file you can see in the
bucket.

---

## 4. Email

```sh
FORMFORGE_IT_SMTP=1 \
FORMFORGE_IT_SMTP_HOST="$HOST" FORMFORGE_IT_SMTP_PORT=587 \
FORMFORGE_IT_SMTP_USER="$USER" FORMFORGE_IT_SMTP_PASSWORD="$PASSWORD" \
FORMFORGE_IT_SMTP_TO="you@example.com" \
  python -m pytest tests/integration/test_contracts.py::TestSmtp -v
```

Then the flow that matters, by hand:

1. request a password reset for an account you own;
2. **read the mail in a real client.** Check the sender, the subject and that
   the link is clickable and points at the staging domain;
3. use the link. The password changes and every session dies, including the
   one you were using;
4. use the same link a second time. It must be refused.

Also request a reset for an address that has no account. The response, its
timing and its shape must be indistinguishable from step 1 — that is the whole
design of the endpoint, and it is the one property a unit test cannot fully
confirm because latency is part of it.

**Checkpoint.** You have read the mail, used the link once, and been refused
the second time.

---

## 5. Billing, in test mode

```sh
FORMFORGE_IT_STRIPE=1 \
STRIPE_SECRET_KEY="sk_test_…" \
FORMFORGE_IT_STRIPE_PRICE="price_…" \
  python -m pytest tests/integration/test_contracts.py::TestStripeSandbox -v
```

Then the end-to-end path, which is the only way to see the webhook arrive over
the real network:

1. sign in as a fresh account. Note the balance (3 on the free plan);
2. start a checkout and pay with a Stripe test card;
3. wait for the webhook. The balance rises to the plan's allowance;
4. check the ledger: `formforge account show them@example.com`. There is one
   `purchase` row, and its idempotency key names the Stripe event;
5. **redeliver the same event** from the Stripe dashboard. The balance does
   not move, and the log shows `billing.webhook.duplicate`;
6. refund the payment in Stripe. Unspent credits are clawed back, floored at
   zero — never into a debt. A user who already spent them keeps what they
   built.

Then confirm the two failure paths:

- point the deployment at the **wrong** webhook secret briefly and send an
  event. It must be refused with `billing.webhook.signature_failed`, and the
  body must not appear in the log;
- check `unhandled_billing_events` is empty. A non-empty answer is the billing
  failure no error rate catches — the endpoint returned 200 and the work never
  happened.

**Checkpoint.** A payment granted credits exactly once, a redelivery granted
nothing, and a refund took back only what was unspent.

---

## 6. Rate limiting

The shared limiter is **not implemented**. Configuring `redis` fails closed at
startup rather than silently falling back to a per-process counter, which is
the safe direction: a limit believed in and not present is worse than no limit,
because it is the reason nobody looks.

```sh
FORMFORGE_IT_REDIS=1 FORMFORGE_IT_REDIS_URL="$REDIS_URL" \
  python -m pytest tests/integration/test_contracts.py::TestRedisSharedRateLimit -v
```

Those tests describe the contract the limiter must meet and prove Redis
provides it. Until it exists, **run staging as a single process**, or accept
that each process holds its own counter and the effective limit is the
configured one multiplied by the process count.

**Checkpoint.** Either one process, or a written decision to accept the
multiplied limit.

---

## 7. Access control, from the outside

With two accounts, A and B, and a model belonging to A:

| Try | Expect |
|---|---|
| B reads A's model | `404`, never `403` — a 403 confirms the id names something real |
| B calls `POST /modify` on A's model | `404` |
| Anonymous reads A's model | `401` |
| Anonymous posts feedback on A's model | `404` |
| B downloads A's artifact | `404` |
| A downloads with an expired signed link | `403` |
| A downloads with an altered signed link | `403` |
| Anyone reads `/v1/meta` on a metered deployment | `404` |

Every one of these is covered by `tests/test_route_policies.py`, which drives
the whole registry. Doing a handful by hand against the deployed instance is
still worth it: the tests prove the application refuses, and this proves
nothing in front of it — a proxy, a cache, a CDN — answers differently.

**Checkpoint.** No `403` where the table says `404`, and no `200` anywhere.

---

## 8. The logs

```sh
python -m formforge.cli events
```

Now read the actual staging logs after doing all of the above, and confirm:

- the events you caused are there — `auth.login.succeeded`, `credit.spent`,
  `billing.webhook.accepted`, `artifact.served`;
- **no secret is in them.** Search the log for the password you typed, the
  session cookie in your browser, the reset token from your email, and the
  Stripe webhook secret. None should appear;
- no email address appears in any event's fields;
- `_incomplete` appears nowhere. If it does, an emission is missing a declared
  field and any alert filtering on that field is matching nothing.

**Checkpoint.** You have grepped the staging log for your own password and
found nothing.

---

## 9. Probes and restart behaviour

- `/healthz` answers `{"ok": true}` and nothing else;
- `/readyz` answers 200, and answers 503 with the database stopped;
- with the database stopped, the process **stays up**. If it restarts, the
  liveness probe is pointed at `/readyz`; fix that before launch — a database
  blip would otherwise restart every process at once and turn a recoverable
  failure into an outage;
- a deploy with a schema change comes up clean, and a second instance starting
  at the same time does not collide (PostgreSQL migrations take an advisory
  lock).

**Checkpoint.** The deployment survives its dependencies being unavailable and
comes back without intervention.

---

## Reference: integration flags

Every one needs its `FORMFORGE_IT_*=1` flag **and** its credentials. The flag
is the consent; the credentials are the means. A developer with `AWS_PROFILE`
exported has not agreed to let a test suite write to a bucket.

| Flag | Also needs | Runs |
|---|---|---|
| `FORMFORGE_IT_STRIPE=1` | `STRIPE_SECRET_KEY` (must be `sk_test_`), `FORMFORGE_IT_STRIPE_PRICE` | `TestStripeSandbox` |
| `FORMFORGE_IT_S3=1` | `FORMFORGE_IT_S3_BUCKET`, optional `FORMFORGE_IT_S3_ENDPOINT` | `TestS3CompatibleStorage` |
| `FORMFORGE_IT_SMTP=1` | `FORMFORGE_IT_SMTP_HOST`, `FORMFORGE_IT_SMTP_TO`, optional `_PORT`/`_USER`/`_PASSWORD` | `TestSmtp` |
| `FORMFORGE_IT_REDIS=1` | `FORMFORGE_IT_REDIS_URL` | `TestRedisSharedRateLimit` |
| `FORMFORGE_IT_PG_RESTORE=1` | `FORMFORGE_IT_DISPOSABLE_PG` (name must contain `test` or `disposable`) | the restore rehearsal — **destructive** |

Everything under `tests/integration` skips without these, so `pytest tests/`
stays offline and credential-free. Skipped is visible on purpose: a suite that
hid them would let somebody assume a contract is covered when it has never
run.

---

## What this pass does not cover

Say these out loud rather than discovering them later.

- **Live billing.** Test mode only. Real cards, real refunds and real disputes
  are a separate pass with a separate decision.
- **A shared rate limiter.** Not implemented; see step 6.
- **Load.** Nothing here says what happens at concurrency. The sandbox is the
  obvious first limit.
- **Data deletion on request.** `account close` is soft by design — the ledger
  is a financial record and outlives the account. A hard delete is a separate,
  explicit operator action and has not been exercised here.
- **Multi-region, failover, or a read replica.** One instance, one database.
