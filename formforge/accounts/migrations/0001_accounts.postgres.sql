-- Accounts, sessions, credits and billing. The Postgres form of the tables in
-- docs/schema.sql.
--
-- Kept deliberately close to the SQLite form in 0001_accounts.sqlite.sql: the
-- same table names, the same column names, the same constraints, and the same
-- meaning for every value. What differs is only what the two engines spell
-- differently. Anything else that drifts between these two files is a bug that
-- will present as "works in tests, wrong in production".

CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS users (
    id                  uuid PRIMARY KEY,
    -- citext, so two signups differing only in case are one account. The
    -- SQLite side has no citext and normalises in Python instead; both paths
    -- go through formforge.accounts.auth.normalise_email so the behaviour is
    -- the same either way.
    email               citext UNIQUE NOT NULL,
    password_hash       text,
    plan                text NOT NULL DEFAULT 'free'
                        CHECK (plan IN ('free','maker','studio')),
    plan_status         text NOT NULL DEFAULT 'active'
                        CHECK (plan_status IN ('active','past_due','cancelled')),
    billing_customer_id text UNIQUE,
    -- text, not timestamptz, and that is deliberate. This holds the *label*
    -- the processor uses for a billing period ("2026-09"), because its only
    -- job is to be half of an idempotency key. Typing it as a moment invites
    -- someone to do arithmetic on it, and a period is not an instant.
    period_start        text,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash text PRIMARY KEY,
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);

CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions (user_id);

CREATE TABLE IF NOT EXISTS credit_ledger (
    id              bigserial PRIMARY KEY,
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    delta           integer NOT NULL CHECK (delta <> 0),
    reason          text NOT NULL
                    CHECK (reason IN ('grant','purchase','spend','refund',
                                      'expiry','adjustment')),
    -- No REFERENCES models(id): that table belongs to the telemetry store,
    -- which may not live in this database at all. The property the reference
    -- would buy is ON DELETE SET NULL -- the charge outlives the model -- and
    -- a plain column has it already.
    model_id        text,
    idempotency_key text UNIQUE,
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS credit_ledger_user_idx ON credit_ledger (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS credit_ledger_model_idx ON credit_ledger (model_id)
    WHERE model_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS billing_events (
    id          bigserial PRIMARY KEY,
    user_id     uuid REFERENCES users(id) ON DELETE SET NULL,
    provider    text NOT NULL,
    event_id    text NOT NULL,
    event_type  text NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}',
    handled_at  timestamptz,
    -- When the *processor* says the event happened, as distinct from when we
    -- received it. Webhooks can arrive out of order -- a cancellation
    -- overtaking the renewal it followed -- and applying them in arrival order
    -- would leave the account in whichever state happened to land last. This
    -- is what lets a stale status change be recognised and skipped.
    event_created timestamptz,
    received_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, event_id)
);

CREATE INDEX IF NOT EXISTS billing_events_unhandled_idx ON billing_events (received_at)
    WHERE handled_at IS NULL;

-- The balance, defined once and read from nowhere else. Two independent SUMs
-- over the same ledger is how the account page and the paywall end up
-- disagreeing, and the customer sees one number and is refused by the other.
CREATE OR REPLACE VIEW credit_balance AS
SELECT
    u.id                              AS user_id,
    u.email                           AS email,
    u.plan                            AS plan,
    u.plan_status                     AS plan_status,
    coalesce(sum(l.delta), 0)::bigint AS balance,
    max(l.created_at)                 AS last_movement_at
FROM users u
LEFT JOIN credit_ledger l ON l.user_id = u.id
GROUP BY u.id, u.email, u.plan, u.plan_status;
