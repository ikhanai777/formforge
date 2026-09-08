-- Accounts, sessions, credits and billing -- the SQLite form.
--
-- The Postgres form in 0001_accounts.postgres.sql is the target and the two
-- must stay in step; tests/test_accounts.py runs the same suite against both,
-- which is what keeps them honest. Where the engines differ:
--
--   uuid, timestamptz   -> text. Ids are canonical UUID strings and timestamps
--                          are ISO-8601 UTC, both of which Postgres accepts.
--   citext              -> text, with the case folding done in Python by
--                          formforge.accounts.auth.normalise_email.
--   bigserial           -> integer primary key autoincrement.
--   jsonb               -> text holding JSON.
--   CREATE OR REPLACE VIEW -> CREATE VIEW IF NOT EXISTS. Not cosmetic: two
--                          workers opening the same file at once race between
--                          a DROP and a CREATE, and one of them loses.

CREATE TABLE IF NOT EXISTS users (
    id                  text PRIMARY KEY,
    email               text UNIQUE NOT NULL,
    password_hash       text,
    plan                text NOT NULL DEFAULT 'free'
                        CHECK (plan IN ('free','maker','studio')),
    plan_status         text NOT NULL DEFAULT 'active'
                        CHECK (plan_status IN ('active','past_due','cancelled')),
    billing_customer_id text UNIQUE,
    period_start        text,
    created_at          text NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash text PRIMARY KEY,
    user_id    text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at text NOT NULL,
    expires_at text NOT NULL,
    revoked_at text
);

CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions (user_id);

CREATE TABLE IF NOT EXISTS credit_ledger (
    id              integer PRIMARY KEY AUTOINCREMENT,
    user_id         text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    delta           integer NOT NULL CHECK (delta <> 0),
    reason          text NOT NULL
                    CHECK (reason IN ('grant','purchase','spend','refund',
                                      'expiry','adjustment')),
    model_id        text,
    idempotency_key text UNIQUE,
    note            text,
    created_at      text NOT NULL
);

CREATE INDEX IF NOT EXISTS credit_ledger_user_idx ON credit_ledger (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS credit_ledger_model_idx ON credit_ledger (model_id)
    WHERE model_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS billing_events (
    id          integer PRIMARY KEY AUTOINCREMENT,
    user_id     text,
    provider    text NOT NULL,
    event_id    text NOT NULL,
    event_type  text NOT NULL,
    payload     text NOT NULL DEFAULT '{}',
    handled_at  text,
    received_at text NOT NULL,
    UNIQUE (provider, event_id)
);

CREATE INDEX IF NOT EXISTS billing_events_unhandled_idx ON billing_events (received_at)
    WHERE handled_at IS NULL;

CREATE VIEW IF NOT EXISTS credit_balance AS
SELECT
    u.id                       AS user_id,
    u.email                    AS email,
    u.plan                     AS plan,
    u.plan_status              AS plan_status,
    coalesce(sum(l.delta), 0)  AS balance,
    max(l.created_at)          AS last_movement_at
FROM users u
LEFT JOIN credit_ledger l ON l.user_id = u.id
GROUP BY u.id, u.email, u.plan, u.plan_status;
