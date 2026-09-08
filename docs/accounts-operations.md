# Running the accounts database

Everything here concerns `formforge/accounts/` only. The CLI, the MCP server
and the offline template path do not use it and never need any of this.

## Choosing a backend

One environment variable selects it, and the value's shape decides which
engine is used:

```sh
# SQLite (default) -- a path
export FORMFORGE_ACCOUNTS_DB=~/.formforge/formforge.db

# PostgreSQL -- a DSN
export FORMFORGE_ACCOUNTS_DB="postgresql://user:pass@host:5432/formforge"
```

One setting rather than two (a driver name plus a location) because two
settings can disagree about which database is live, and the way that presents
is an application that starts cleanly against the wrong one.

`FORMFORGE_DB` is used as a fallback so a development box that already sets it
for the telemetry store needs no second variable.

## Migrations

Revisions live in `formforge/accounts/migrations/` as one pair of files per
revision — `<id>.sqlite.sql` and `<id>.postgres.sql` — and are listed in order
in `REVISIONS` in `formforge/accounts/dialect.py`.

They are applied automatically when an `AccountStore` is constructed. There is
no separate migrate command to forget to run before a deploy.

**Repeatable.** Applying the same set twice does nothing the second time. Two
mechanisms, because they fail differently:

- Every statement in every migration file is `IF NOT EXISTS` (or `CREATE OR
  REPLACE` for views). Re-applying a revision is a no-op even if the record of
  it having been applied was lost.
- Applied revisions are recorded in `schema_migrations`, so the normal path
  does not re-run them at all.

**Safe under concurrent startup.** Two workers booting at once would otherwise
both see an unapplied revision and both run it, and the loser gets a
duplicate-object error at boot. Postgres takes a transaction-scoped advisory
lock around the whole migration step; SQLite serialises writers at the file
level and the `IF NOT EXISTS` clauses cover the rest.

**Adding a revision.** Write both files, append the id to `REVISIONS`, and add
the new tables to the parity test in `tests/test_accounts.py::TestSchemaParity`
so the two dialects and `docs/schema.sql` cannot drift apart. Do not edit a
revision that has shipped — an already-migrated database will never re-run it.

## Running the tests against PostgreSQL

The accounts suite is parametrised over both backends. It runs the *same*
tests against each, which is what makes "Postgres preserves the SQLite ledger
semantics" a test result rather than a claim.

Postgres is **skipped** rather than failed when `FORMFORGE_TEST_PG` is unset,
so a contributor without a server still gets the SQLite half:

```sh
export FORMFORGE_TEST_PG="host=127.0.0.1 port=5432 user=postgres dbname=formforge_test"
python -m pytest tests/test_accounts.py -q
```

CI (`.github/workflows/tests.yml`) runs a `postgres:16` service and sets that
variable, and then asserts the Postgres parameter actually collected tests —
a skip there would mean the backend handling real money is never exercised,
while CI stays green.

Any local PostgreSQL will do. With Docker:

```sh
docker run --rm -d -p 5432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=formforge_test \
  --name ff-pg postgres:16
```

Without Docker, on a machine with the server binaries installed:

```sh
PGDIR=$(mktemp -d)
initdb -D "$PGDIR/data" -A trust -U postgres
pg_ctl -D "$PGDIR/data" -o "-p 5433" -l "$PGDIR/log" start
createdb -h 127.0.0.1 -p 5433 -U postgres formforge_test
export FORMFORGE_TEST_PG="host=127.0.0.1 port=5433 user=postgres dbname=formforge_test"
```

Each test drops and recreates the `public` schema, so the database it points at
must be a scratch one. Do not aim `FORMFORGE_TEST_PG` at anything you care
about.

## What differs between the two backends

Nothing that callers can observe — that is the property the parametrised suite
exists to hold. Internally:

| | SQLite | PostgreSQL |
|---|---|---|
| Connections | one, behind a lock | a pool (`psycopg_pool`) |
| Spend serialisation | `BEGIN IMMEDIATE`, database-wide | `SELECT … FOR UPDATE` on the user row |
| `uuid`, `timestamptz`, `jsonb` | text | native, normalised to text on read |
| Case-insensitive email | folded in Python | `citext`, and folded in Python too |

The Postgres row lock is the better of the two: two people spending their own
credits do not block each other, where SQLite serialises every writer in the
database. The guarantee either way is that between reading a balance and
writing against it, nobody else can do the same for that user.

## Not yet moved to PostgreSQL

The telemetry store (`formforge/store.py` — `models`, `generation_events`,
`print_feedback`, `policy_events`) is still SQLite-only. It is unrelated to
billing and nothing in the payment path reads it.

`pgvector` is not required by anything here; the templates embedding column in
`docs/schema.sql` remains unimplemented on both backends.

## Stripe, in test mode

Nothing in this phase may talk to live Stripe. `StripeProvider` refuses a key
that does not begin `sk_test_` unless `FORMFORGE_ALLOW_LIVE_BILLING=1` is set
*and* `allow_live=True` is passed — two deliberate steps, because the failure
mode of getting this wrong is charging real cards.

### Configuration

```sh
export STRIPE_SECRET_KEY=sk_test_...          # test mode; anything else is refused
export STRIPE_WEBHOOK_SECRET=whsec_...        # from `stripe listen` or the dashboard
export STRIPE_PRICE_MAKER=price_...           # a $9/month recurring price
export STRIPE_PRICE_STUDIO=price_...          # a $29/month recurring price
export STRIPE_SUCCESS_URL=https://localhost:8000/account?checkout=done
export STRIPE_CANCEL_URL=https://localhost:8000/pricing
```

With `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` both set, the gateway
picks Stripe up automatically. With either missing it falls back to the offline
provider — deliberately that way round, so a misconfiguration cannot silently
start talking to a payment processor.

### Walkthrough

1. **Create the prices** in the Stripe test dashboard: two recurring monthly
   products at $9 and $29. Copy the `price_...` ids into the variables above.

2. **Forward webhooks to the local gateway.** Stripe cannot reach a laptop, so
   the CLI tunnels for it:

   ```sh
   stripe listen --forward-to localhost:8000/v1/billing/webhook
   ```

   It prints a `whsec_...` — that is `STRIPE_WEBHOOK_SECRET` for this session,
   and it changes each time `stripe listen` restarts.

3. **Start the gateway with accounts on:**

   ```python
   from formforge.accounts import AccountStore
   from formforge.api import create_app
   app = create_app(accounts=AccountStore("postgresql://…"))
   ```

4. **Sign up and start a checkout:**

   ```sh
   curl -c jar -X POST localhost:8000/v1/auth/signup \
     -H 'content-type: application/json' \
     -d '{"email":"you@example.com","password":"a-long-enough-password"}'
   # -> {"plan":"free","credits":3,...}

   curl -b jar -X POST localhost:8000/v1/billing/checkout \
     -H 'content-type: application/json' -d '{"plan":"maker"}'
   # -> {"checkout_url":"https://checkout.stripe.com/..."}
   ```

5. **Pay with a test card.** Open the checkout URL and use `4242 4242 4242
   4242`, any future expiry, any CVC. Stripe's own test cards cover the other
   cases: `4000 0000 0000 0341` fails at charge time, `4000 0000 0000 9995`
   declines for insufficient funds.

6. **Watch the grant arrive.** `stripe listen` logs
   `checkout.session.completed`, the gateway turns it into a period start, and:

   ```sh
   curl -b jar localhost:8000/v1/auth/me
   # -> {"plan":"maker","credits":60,...}
   ```

   If credits do not appear, the webhook is the place to look — check
   `unhandled_billing_events()`, which is exactly the "they paid and did not
   get it" signal.

### Exercising the rest without waiting for real events

`stripe trigger` replays any event type against the local endpoint, signed
properly:

```sh
stripe trigger invoice.paid                    # a renewal
stripe trigger invoice.payment_failed          # -> past_due, credits kept
stripe trigger customer.subscription.deleted   # -> back to free
stripe trigger charge.refunded                 # -> claw back what is unspent
```

Deliver the same event twice to confirm the second is a no-op — the response
says `{"applied": false}` and the balance does not move.

### Verified and unverified

- **Verified here**, against the automated suite: signature verification and
  rejection, every event's effect on the ledger, idempotency under replay,
  out-of-order status handling, refund arithmetic, and the credit gate on
  exports. These run against the offline provider, which uses the same code
  path and a real HMAC.
- **Not verified here**: calls to Stripe's own API — creating a customer and a
  Checkout Session — because this environment has no `sk_test_` key. The
  request shapes are written against the current SDK but have not been round
  tripped against Stripe. Step 4 above is the smoke test that closes that gap,
  and it needs a key.
