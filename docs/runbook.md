# Operator runbook

Everything here runs against the database directly through the CLI. There is
no HTTP admin panel, deliberately: an authenticated admin endpoint is a second
privilege tier, a second auth path and a permanent target on the public
surface, and a command that already needs the database needs none of it.
Having the database *is* the access control.

Revisit when there is a support team rather than an operator.

---

## From a clean clone to a running server

```sh
git clone … && cd formforge
pip install -e ".[dev,api,accounts]"
python -m formforge.cli bootstrap --demo-user you@example.com
python -m formforge.cli serve --accounts
```

`bootstrap` creates the directories, applies the migrations and prints the demo
password once. It is safe to re-run: migrations are recorded, directories are
made only if absent, and an existing account is left alone.

Nothing above needs a credential, a network or a payment provider. Billing is
the offline adapter, mail goes to a local outbox, storage is a directory,
the database is SQLite.

```sh
python -m pytest tests/ -q                     # everything
FORMFORGE_TEST_PG="host=… dbname=…" pytest -q  # plus the PostgreSQL half
```

---

## Recovering a locked-out account

The path when the reset mail never arrives — or when email is not configured at
all, which locally it is not.

```sh
python -m formforge.cli account reset-password them@example.com
```

Prints a new password once and revokes every session. Hand it over out of band;
it is not stored anywhere and not logged. The account should change it.

To check that the reset flow itself works locally, drive it and read the
outbox:

```sh
curl -X POST localhost:8000/v1/auth/reset/request \
  -H 'content-type: application/json' -d '{"email":"them@example.com"}'
python -m formforge.cli outbox -n 1
```

---

## Inspecting an account

```sh
python -m formforge.cli account show them@example.com
```

Plan, status, balance, the last fifteen ledger entries and the audit trail. The
ledger is the answer to "why is my balance this number" — it is append-only,
so every movement is there, including expiries and reversals.

```sh
python -m formforge.cli account create them@example.com --plan maker
python -m formforge.cli account grant them@example.com 20 --note "goodwill, ticket 412"
```

A manual grant is a normal ledger row with a note, and it is audited. There is
no way to change a balance that does not leave a trace, which is the point.

---

## Replaying a webhook safely

Locally, billing is the offline provider and its webhook signature is real
HMAC, so a replay is a signed POST:

```python
import json, requests
from formforge.accounts import OfflineProvider
provider = OfflineProvider(secret="…")          # the app's provider secret
body = json.dumps({"type": "subscription.renewed", "id": "evt_replay_1",
                   "customer_id": "offline_cus_…", "plan_id": "maker",
                   "period_start": "2026-10"}).encode()
requests.post("http://localhost:8000/v1/billing/webhook", data=body,
              headers={"x-formforge-signature": provider.sign(body)})
```

**Replaying the same event id twice is safe** and is worth confirming: the
response says `{"applied": false}` the second time and the balance does not
move. Against Stripe sandbox the equivalent is `stripe trigger invoice.paid`;
see `docs/accounts-operations.md`.

An event that arrives and is never acted on is the failure nothing else
catches — the webhook returned 200 and the work did not happen. Look for it:

```python
from formforge.accounts import AccountStore
AccountStore().unhandled_billing_events()
```

A non-empty answer means somebody paid and did not get what they paid for.

---

## Deleting an artifact

Two steps, on purpose. Marking is reversible; removing the bytes is not.

```sh
python -m formforge.cli artifacts show   <model-id>
python -m formforge.cli artifacts mark   <model-id>     # reversible
python -m formforge.cli artifacts sweep                 # dry run: lists, deletes nothing
python -m formforge.cli artifacts sweep --confirm       # removes the bytes
```

`sweep` without `--confirm` never deletes. With retention off — which is the
default, `FORMFORGE_RETENTION_DAYS=0` — it marks nothing new and only offers to
remove what an operator already marked by hand.

The database row survives the file and is marked `deleted`. That is what lets a
download answer `410 Gone` rather than `404`, which is a different conversation
with the customer. A model somebody was **charged for is never a retention
candidate**; deleting what was paid for on a timer is a refund request, not a
policy.

---

## Rotating secrets

| Secret | Effect of rotating | Notes |
|---|---|---|
| `FORMFORGE_SESSION_SECRET` | — | Sessions live in the database, not in the cookie, so rotating does not sign anyone out. |
| `FORMFORGE_LINK_SECRET` | Outstanding download links stop working | They live five minutes. Rotate freely; a user re-clicks. |
| `STRIPE_WEBHOOK_SECRET` | Webhooks fail until both sides match | Rotate in Stripe first, deploy, then retry the failed deliveries from Stripe's dashboard. Events are idempotent, so a retry is safe. |
| `STRIPE_SECRET_KEY` | API calls fail until deployed | Checkout stops; existing subscriptions are unaffected. |
| Database password | Everything stops | Roll with the connection pool drained. |

To sign every user out deliberately — a suspected session-store compromise —
revoke rather than rotate:

```python
from formforge.accounts import AccountStore
store = AccountStore()
for row in store.audit_trail(user_id):   # or iterate accounts
    ...
store.revoke_all_sessions(user_id)
```

---

## Offline → Stripe sandbox

1. Get a `sk_test_…` key and two recurring prices from the Stripe test
   dashboard.
2. `stripe listen --forward-to localhost:8000/v1/billing/webhook` and copy the
   `whsec_…` it prints.
3. Set `FORMFORGE_BILLING=stripe_sandbox`, `STRIPE_SECRET_KEY`,
   `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_MAKER`, `STRIPE_PRICE_STUDIO`.
4. Restart. Startup refuses if any of those are missing, and refuses a
   `sk_live_` key in sandbox mode.
5. Walk the flow in `docs/accounts-operations.md` § "Stripe, in test mode".

Setting a Stripe key while `FORMFORGE_BILLING` is still `offline` is a startup
error, not a silent fallback — somebody believes they configured billing and
they have not.

## Sandbox → live

**Not in scope for this phase, and guarded three ways.** All of these, or
startup refuses:

1. `FORMFORGE_BILLING=stripe_live`
2. a key beginning `sk_live_`
3. `FORMFORGE_ALLOW_LIVE_BILLING=1`

A live key on its own can never move money. Before flipping it, work the
launch checklist in the Phase 2 report: real prices, real webhook endpoint,
tested refund path, and a rate limiter that is actually shared.

---

## Backup and restore

**What cannot be reconstructed**, in order of how much it hurts to lose:

1. `credit_ledger` — the answer to "why was I charged". Append-only, never
   deleted by anything in the codebase, and not derivable from Stripe (it
   holds spends, expiries and adjustments Stripe never sees).
2. `billing_events` — what the processor said, and the replay guard. Losing it
   means a redelivered webhook is applied a second time.
3. `users` — identities. Losing it orphans everything else.
4. `print_feedback` (telemetry store) — the only ground truth for whether any
   of this prints, and not backfillable.
5. `audit_log` — what an operator did.

Artifacts are **not** in that list: they are regenerable from
`source.py` + `params.json`, which is the whole reproducibility argument. Back
them up for convenience, not for correctness.

```sh
python -m formforge.cli backup create                 # backups/<timestamp>.tar.gz
python -m formforge.cli backup verify backups/….tar.gz
```

`backup create` reads `FORMFORGE_ACCOUNTS_DB` and picks the right mechanism.
SQLite gets an online snapshot through SQLite's own backup API rather than a
file copy — copying a database that is being written produces an archive that
restores into a corrupt database *sometimes*, which is the worst failure mode
a backup can have. PostgreSQL gets `pg_dump --format=custom`, with the DSN
password moved into `PGPASSWORD` so it never appears in `ps` while the dump
runs.

**Verify on a schedule, not when you need it.** `backup verify` opens the
archive, runs an integrity check on the database inside, and prints the row
counts and the schema revisions it holds. An unverified backup is a
hypothesis.

### Restoring

```sh
python -m formforge.cli backup restore backups/….tar.gz            # refuses
python -m formforge.cli backup restore backups/….tar.gz --confirm  # does it
python -m formforge.cli backup check                               # then this
```

Restore does nothing without `--confirm`, and exits non-zero when it refuses,
so a script cannot mistake the refusal for a completed restore. For SQLite the
current database is *moved aside* rather than deleted — the usual reason to
restore is that something went wrong, and the second-worst outcome is finding
out the archive was the wrong one after the original is gone. For PostgreSQL
`pg_restore --clean --if-exists` drops and recreates what the dump contains;
there is no moving aside on a live server, so take a `backup create` first.

An archive from a *newer* schema is refused outright. That restore appears to
work right up until a query hits a column this build does not know about. An
archive that is behind is allowed, and says which migrations will apply on the
next start.

`backup check` is the post-restore validation, and it deliberately reads
through the real `AccountStore` — migrations, views and all — rather than
running `SELECT count(*)`. A restored database missing the `credit_balance`
view has all its rows and cannot serve a single paywall decision; this is what
catches that.

Restoring the accounts database without the artifact store leaves rows
pointing at files that are gone. Downloads then answer `410`, which is correct
and survivable, but it should be a decision rather than a discovery.

### Rehearsing the PostgreSQL restore

The procedure above is exercised end to end — dump, drop the schema, restore,
read the ledger back — by an opt-in test that requires a database it is
explicitly allowed to destroy:

```sh
FORMFORGE_IT_PG_RESTORE=1 \
FORMFORGE_IT_DISPOSABLE_PG=postgresql://…/formforge_disposable \
  python -m pytest tests/integration/test_restore_rehearsal.py -v
```

The variable is deliberately **not** `FORMFORGE_TEST_PG`, and its value must
name a database with `test` or `disposable` in it, so the database the
ordinary suite uses can never be the one that gets dropped. Run it before
staging and again whenever the schema changes.

---

## Probes

| Endpoint | Answers | Metered deployment |
|---|---|---|
| `GET /healthz` | Is the process alive | `{"ok": true}` only — a probe from a stranger gets one bit |
| `GET /readyz` | Can it serve: database, account store, sandbox | `503` when a dependency is down, so an orchestrator drops it from rotation |
| `GET /v1/meta` | What this deployment is | **Not served** — an operator uses the CLI |

None of them reports a DSN, a path, a key or whether a particular account
exists.

### Wiring them to a load balancer

The two are for different jobs and wiring them the same way defeats both.

| | `/healthz` | `/readyz` |
|---|---|---|
| Question | is this process alive | should traffic go to it *now* |
| Use for | the restart/liveness probe | the load-balancer health check |
| Touches | nothing | database, account store, sandbox |
| Suggested interval | 10s | 5s |
| Suggested threshold | 3 failures before restarting | 2 failures before removing from rotation |
| Timeout | 2s | 5s — it does real work |

Never point a liveness probe at `/readyz`. A database blip would then restart
every process at once, turning a recoverable dependency failure into an
outage; `/readyz` failing is exactly the case where the process should stay up
and stop taking traffic.

Give a new instance a startup grace period longer than the migrations take —
`AccountStore` applies them on construction, and on PostgreSQL takes an
advisory lock so concurrent starts serialise rather than race.

### Before serving anything

```sh
python -m formforge.cli preflight --environment staging
```

Exits non-zero only when something *configured* is broken. A setting that is
merely absent reports `missing`, because "not finished" and "wrong" are
different problems with different fixes and a gate that conflates them turns a
to-do list into an outage hunt.

---

## Reading the security event stream

```sh
python -m formforge.cli events            # the catalogue, with what to alert on
python -m formforge.cli events --json     # the same, for a dashboard
```

Logs are JSON in staging and production (`FORMFORGE_MODE`), one object per
line, with the event name in `message` and its declared fields alongside. The
catalogue is generated from `formforge/events.py`, so it cannot drift from
what the code emits.

The four worth an alert on day one:

| Event | Why |
|---|---|
| `billing.webhook.not_applied` | somebody paid and the work did not happen. The endpoint returned 200, so no error rate catches it. Cross-check with `unhandled_billing_events` |
| `billing.webhook.signature_failed` | a forged webhook, or the wrong signing secret deployed — and the second one means real payments are being dropped |
| `password_reset.undeliverable` | a token was issued and the mail did not send. A support ticket that has not been filed yet |
| `auth.login.failed` rising against a flat `auth.login.succeeded` | credential stuffing |

**Nothing in this stream carries a secret.** No password, session token, reset
token, API key, signature, card, or raw webhook body — and no email address
either, because the security log is copied further than the database is and an
address list is what most of these endpoints exist to avoid handing out. That
is enforced twice: `logs.Redactor` rewrites every record from anywhere on its
way out, and `tests/test_events.py::TestNoEventCarriesASecret` plants known
secrets, drives the real HTTP paths and greps the formatted output.

If `_incomplete` ever appears on an event, an emission is missing a declared
field and an alert filtering on that field is silently matching nothing.
