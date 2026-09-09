# Configuration

Every setting the application reads, what uses it, and whether each mode
requires it. `formforge/config.py` is the only place the environment is read;
nothing else calls `os.environ` for these.

Copy `.env.example` to `.env` to start. **A clean clone needs none of it** —
every variable has a local-safe default.

## Modes

| Mode | Behaviour on a bad or missing setting |
|---|---|
| `local` *(default)* | Permissive. Everything defaults to something that runs offline: SQLite, local files, offline billing, an email outbox on disk. |
| `staging` | A real deployment. Problems are reported, startup continues. |
| `production` | A real deployment. Startup **refuses**, naming the variable. |

Fail-closed in production is deliberate. The alternative is what this codebase
had before: a missing `FORMFORGE_LINK_SECRET` silently became a per-process
random one, every download link broke the moment a second worker existed, and
nothing said so. A setting that quietly defaults to something unsafe is worse
than one that is absent, because absent is loud.

## Reference

Legend: **R** required · **o** optional · **—** ignored/refused

| Variable | local | staging | prod | Used by | Notes |
|---|:--:|:--:|:--:|---|---|
| `FORMFORGE_MODE` | o | R | R | `config` | `local`\|`staging`\|`production` |
| `FORMFORGE_HOME` | o | o | o | `config` | Base for the defaults below. Default `~/.formforge` |
| **Secrets** ||||||
| `FORMFORGE_SESSION_SECRET` | o | R | **R** | sessions | ≥32 chars. Generated per process locally |
| `FORMFORGE_LINK_SECRET` | o | R | **R** | signed download links | ≥32 chars. Without it, links do not verify across workers |
| **HTTP** ||||||
| `FORMFORGE_ALLOWED_ORIGINS` | o | R | **R** | CORS | Comma-separated |
| `FORMFORGE_COOKIE_INSECURE` | o | — | **—** | session cookie | `1` drops `Secure`. Refused in production |
| **Data** ||||||
| `FORMFORGE_ACCOUNTS_DB` | o | R | **R** | `accounts` | Path → SQLite, DSN → PostgreSQL. Production requires a DSN |
| `FORMFORGE_DB` | o | o | o | telemetry store | Fallback for the above |
| `FORMFORGE_STORE` | o | o | o | generated bundles | |
| `FORMFORGE_STORAGE` | o | R | **R** | artifacts | `local`\|`s3`. Production requires `s3` |
| `FORMFORGE_ARTIFACTS` | o | R | R | artifacts | Directory, or `s3://bucket/prefix` |
| `FORMFORGE_S3_ENDPOINT` | o | o | o | artifacts | For MinIO/R2/Spaces |
| **Billing** ||||||
| `FORMFORGE_BILLING` | o | R | R | billing | `offline`\|`stripe_sandbox`\|`stripe_live` |
| `STRIPE_SECRET_KEY` | — | R¹ | R¹ | Stripe adapter | ¹ if billing is not `offline` |
| `STRIPE_WEBHOOK_SECRET` | — | R¹ | R¹ | webhook verification | The signature is a webhook's only authentication |
| `STRIPE_PRICE_MAKER` / `_STUDIO` | — | R¹ | R¹ | checkout | |
| `STRIPE_SUCCESS_URL` / `CANCEL_URL` | o | R¹ | R¹ | checkout | |
| `FORMFORGE_ALLOW_LIVE_BILLING` | — | — | R² | billing | ² only with `stripe_live` |
| **Email** ||||||
| `FORMFORGE_EMAIL` | o | R | R | password reset | `outbox`\|`smtp`\|`disabled` |
| `FORMFORGE_EMAIL_OUTBOX` | o | — | — | outbox adapter | Directory of `.eml` files |
| `FORMFORGE_EMAIL_FROM` | o | R | R | email | |
| `FORMFORGE_PASSWORD_RESET` | o | o | o | reset routes | `0` disables the feature |
| `FORMFORGE_SMTP_*` | — | R³ | R³ | SMTP adapter | ³ if email is `smtp` |
| **Limits and lifecycle** ||||||
| `FORMFORGE_RATE_LIMIT_BACKEND` | o | o | o | rate limiting | `memory`\|`redis` |
| `FORMFORGE_REDIS_URL` | — | R⁴ | R⁴ | rate limiting | ⁴ if backend is `redis` |
| `FORMFORGE_RETENTION_DAYS` | o | o | o | artifact cleanup | `0` = **off**. Nothing is deleted on a schedule unless set |
| **Sandbox** ||||||
| `FORMFORGE_ALLOW_UNSAFE_SANDBOX` | o | — | **—** | sandbox | Refused in production; it executes model-authored Python |
| `FORMFORGE_SANDBOX_RUNTIME` | o | R | R | sandbox | `gvisor` in a deployment |
| `FORMFORGE_SANDBOX_IMAGE`, `FORMFORGE_SECCOMP_PROFILE` | o | R | R | sandbox | |
| **Generation** ||||||
| `ANTHROPIC_API_KEY` | o | o | o | model client | Absent → the offline template path |
| `FORMFORGE_OFFLINE`, `FORMFORGE_MODEL_*`, `FORMFORGE_LOG_LEVEL` | o | o | o | model client, logging | |
| **Tests** ||||||
| `FORMFORGE_TEST_PG` | o | — | — | test suite | Scratch database; each test drops the `public` schema |

## Live billing takes three deliberate settings

A Stripe live key on its own can never move money. All three of these, or
startup refuses:

1. `FORMFORGE_BILLING=stripe_live`
2. a key beginning `sk_live_`
3. `FORMFORGE_ALLOW_LIVE_BILLING=1`

Checked in the other direction too: a live key present while the mode says
anything else is a startup error, not a quiet fallback to offline. Somebody
believes they configured billing and they have not, and finding that out from a
missing-revenue report is expensive.

`Settings.live_billing_armed` is the single property that answers "can real
money move", and it is false unless all three hold.

## Secrets never print

`config.Secret` wraps every credential. `repr`, `str` and f-string
interpolation all give `***`; reading the value takes an explicit `.reveal()`.
A settings object that ends up in a log line or a traceback — which eventually
it will — takes nothing with it. `Settings.describe()` is the shape safe to log
or serve: it says *whether* each secret is set, never what it is.

## What the rate-limit backend actually does

`memory` is **per process**. Four workers means four independent budgets and an
effective limit four times the configured one. It is a floor against a script
pointed at the login endpoint, not a rate limit in the sense a deployment
needs. `redis` is the interface seam for a real one; the implementation is
deferred to staging, and this document is where that is recorded rather than
being implied by the setting existing.

## Retention is off

`FORMFORGE_RETENTION_DAYS=0` is the default and means nothing is ever deleted
on a schedule. The lifecycle machinery and an operator entry point exist; no
timer runs them. Deletion of a customer's files is destructive and irreversible
and does not get switched on by a default.
