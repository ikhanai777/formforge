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
