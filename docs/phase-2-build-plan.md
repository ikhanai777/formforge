# Phase 2 build plan — complete the locally runnable product

Scope: finish the product so it runs end to end from a clean clone with no
external service, while making every production integration configurable and
**off by default**. No deployment, no live billing, no real credentials.

Baseline measured before planning, not assumed:

```
pytest tests/            560 passed, 0 failed   (SQLite + real PostgreSQL 16.13)
ruff check .             ~200 findings, all pre-existing; no new ones from Phase 1
type checker             none configured
```

---

## 1. Current state

Working and tested: the account store on either SQLite or PostgreSQL behind one
dialect, repeatable migrations, the append-only credit ledger with the
concurrent-spend and replay hazards guarded, session auth, the credit gate on
the export path, Stripe test-mode adapter, signed expiring download links, CI
with a real Postgres service.

## 2. What is missing, wrong, or dead

Ordered by severity. The first is a live security hole, reproduced rather than
inferred.

### SEC-1 — model reads and modify are unauthenticated on a metered deployment (critical)

Phase 1 gated `download`. It did not gate anything else. Reproduced against a
metered app with **no account and no session at all**:

| Anonymous request | Result |
|---|---|
| `GET /v1/models/{id}` | **200** — full record: `prompt`, `params`, `source_code` |
| `GET /v1/models/{id}/status` | **200** |
| `GET /v1/models/{id}/events` | **200** |
| `WS /v1/models/{id}/stream` | ungated |
| `GET /v1/stats` | **200** — business aggregates |
| `POST /v1/models/{id}/modify` | **202** — starts a *new generation* from someone else's model: spends sandbox CPU, with no owner and no credit check |

Model ids are UUID4 and not guessable, but ids leak — URLs, logs, referrer
headers, support email — and unguessability is not authorisation. `modify` is
the worst of these: it is an unauthenticated compute amplifier that also
launders another account's design into a new model.

### CFG-1 — no configuration layer

23 environment variables read ad hoc across 8 modules with inline defaults.
There is no settings object, no environment mode, no startup validation, and no
`.env.example`. A production deployment missing `FORMFORGE_LINK_SECRET` today
silently generates a per-process random one, so download links break the moment
a second worker exists — and nothing says so.

### STO-1 — the storage abstraction is not wired to anything

`formforge/storage.py` defines `LocalStorage` and `S3Storage`, and **nothing
imports them**. Only the signing helpers are used. Artifacts are still served
by `FileResponse` straight off `job.result.artifacts` paths. There is no
artifact metadata, no status, no retention, no delete path. The abstraction is
real but currently dead code.

### ACC-1 — no password reset

Deferred in Phase 1 for lack of an email direction. A locked-out user needs an
operator. Now in scope with a local outbox so the whole flow is testable.

### ACC-2 — no account deletion or deactivation

No way to close an account, and no policy for what happens to its ledger
(a financial record) versus its artifacts (a storage cost).

### BIL-1 — live billing is guarded, but by one flag too few

`allow_live=True` plus `FORMFORGE_ALLOW_LIVE_BILLING=1` gate live keys, but
provider selection is implicit: set two Stripe variables and the gateway
silently switches from offline to Stripe. Mode should be named, not inferred.

### BIL-2 — no audit trail outside billing

`billing_events` records webhooks. Nothing records a password change, a session
revocation, a plan change made by an operator, or a manual credit adjustment.

### BIL-3 — no operator surface

No way to inspect an account, its balance or its ledger without a Python REPL.

### RL-1 — the rate limiter is a class, not an interface

Works, honestly documented as per-process, but there is no seam for a shared
implementation later.

### DX-1 — no bootstrap, no serve command, no readiness probe, no structured logging

`FORMFORGE_AUTO_APP=1` is the only way to get an app object. `/healthz` mixes
liveness and dependency state and reports sandbox internals.

### QA-1 — no type checker; noisy lint baseline

~200 pre-existing findings mean a new one is invisible. CI already lints only
changed files, which is the right call; keep it.

---

## 3. Risks

| Risk | Mitigation |
|---|---|
| Fixing SEC-1 changes responses on the metered path (200 → 401/404) | The unmetered path is untouched and has a test class asserting it. Every changed response gets a regression test. It is a security fix, so it ships regardless. |
| A settings refactor breaks the existing 560 tests | Settings resolve from the same env vars with the same defaults. Full suite after every commit. |
| Fail-closed production startup locks someone out of their own deployment | Only `production` mode fails closed. `local` is permissive and is the default. Every refusal names the variable and how to set it. |
| Retention deletes something a customer paid for | Retention is **off** unless explicitly configured, deletes nothing on a paid account, is soft-delete-first, and never touches the ledger. |
| Password-reset tokens become an enumeration oracle | Generic response regardless of whether the address exists; the timing difference is bounded by doing the same hashing work either way. |
| Live billing enabled by accident | Three independent things required: mode named `live`, a live key present, and an explicit opt-in flag. Any one missing → refuse at startup. |

---

## 4. Implementation order

Each step is a commit, tests green before the next.

1. **SEC-1 first.** Ownership and session checks on every model route; `modify`
   re-checks credits and inherits the owner. Regression tests reproducing the
   anonymous-access probe.
2. **Config layer.** `formforge/config.py` with typed settings, three modes,
   fail-closed production validation, `.env.example`, a documented variable
   table. Everything else reads settings from here.
3. **Rate-limiter interface** + local implementation, applied to the endpoints
   that need it, with the multi-worker limitation restated where it is used.
4. **Password reset.** Email interface, local outbox adapter, request/confirm
   endpoints, single-use hashed tokens, session revocation on success,
   migration for the token table.
5. **Account lifecycle.** Deactivate/close, operator reactivation, ledger
   preserved.
6. **Artifact lifecycle.** Wire `Storage` in for real, artifact metadata table,
   soft then hard delete, idempotent, an operator cleanup entry point,
   retention off by default.
7. **Audit log** for security- and billing-sensitive transitions.
8. **Operator surface.** CLI commands, not an HTTP admin panel (see §7).
9. **Developer experience.** `formforge serve`, `formforge bootstrap`,
   `/healthz` split from `/readyz`, structured logging with redaction.
10. **Docs + runbook**, CI additions, final full run.

---

## 5. Acceptance criteria

- An anonymous or wrong-account request to **any** model route on a metered
  deployment is refused, proven by a test that reproduces today's probe.
- `create_app()` with no arguments still starts, still needs no account store,
  and the free CLI/MCP/offline paths remain unmetered — existing test class
  still passes untouched.
- `Settings.for_production()` raises, naming the variable, when any of: session
  secret, link secret, insecure cookies, allowed origins, database, storage, or
  (if reset is enabled) email is unsafe or missing.
- Live billing cannot be reached without all three of mode, key and opt-in.
- A full password reset runs end to end against the local outbox with no
  network. Token is single-use, hashed at rest, short-lived, and revokes
  sessions on success.
- Artifact delete is idempotent; retention deletes nothing unless configured;
  the ledger is never deleted.
- `formforge bootstrap` works from a clean clone: database, directories,
  migrations, optional demo user, and the command to run the tests.
- Suite green on SQLite and on PostgreSQL; Postgres skips cleanly when absent.
- No new lint findings on changed files.

---

## 6. Local / staging / production separation

| Concern | local (default) | staging | production |
|---|---|---|---|
| Mode | `local` | `staging` | `production` |
| Database | SQLite file | PostgreSQL | PostgreSQL, required |
| Storage | local filesystem | S3-compatible | S3-compatible, required |
| Billing | offline provider | Stripe **sandbox**, explicit | Stripe live, three explicit gates |
| Email | local outbox (writes files) | real provider | real provider, required if reset enabled |
| Cookies | `Secure` off permitted | `Secure` required | `Secure` required |
| Secrets | generated per process | required | required, startup fails without |
| Rate limiting | in-memory | shared store expected | shared store expected |
| Origins | permissive | allowlist | allowlist, required |
| Retention | off | off unless set | explicit setting |
| Startup validation | warns | warns | **fails closed** |

Everything in the staging and production columns is configuration only. None of
it is exercised in this phase, and nothing here will claim a third-party
integration is verified without having run against that service.

---

## 7. Decisions taken without asking

Stated so they are easy to overturn:

1. **Operator surface is CLI, not an HTTP admin panel.** An authenticated admin
   endpoint is a second privilege tier, a second auth path and a permanent
   target; a CLI against the same database needs none of that and is enough for
   one operator. Revisit when there is a support team.
2. **Account closure is soft by default.** Login disabled, sessions revoked,
   ledger retained (it is a financial record), artifacts marked for retention.
   Hard delete is a separate, explicit operator command. Reversible beats tidy.
3. **Password reset revokes all sessions.** The case that matters is an account
   being recovered *from* somebody.
4. **Retention ships off.** Machinery and an operator entry point, no schedule,
   no default deletion.
5. **`mypy` is not introduced.** The codebase is untyped in places and a new
   type gate would either be trivially permissive or a large unrelated
   refactor. Lint on changed files stays the gate.
