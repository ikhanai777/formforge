-- Artifact lifecycle, account closure, and an audit trail.
--
-- NOTE ON REPEATABILITY: unlike 0001 and 0002, this revision contains an
-- ALTER, and `ADD COLUMN IF NOT EXISTS` exists in Postgres but not in SQLite.
-- So on SQLite this revision is idempotent by `schema_migrations` alone rather
-- than by construction. That is the contract every migration tool actually
-- runs on; it is called out because 0001's docstring claims the stronger
-- property and this one does not have it.

-- What has been produced, where it lives, and whether it is still there.
--
-- Separate from `models` (which belongs to the telemetry store and may be in a
-- different database entirely) because this is about *bytes on a disk we pay
-- for*, not about what was generated. A model row outlives its artifacts by
-- design: deleting the file must not erase the record that the build happened,
-- because `print_feedback` joins to it and that table is the only ground truth
-- this system has for whether any of it prints.
CREATE TABLE IF NOT EXISTS artifacts (
    id          integer PRIMARY KEY AUTOINCREMENT,
    model_id    text NOT NULL,
    user_id     text REFERENCES users(id) ON DELETE SET NULL,
    fmt         text NOT NULL,
    storage_key text NOT NULL,
    bytes       integer NOT NULL DEFAULT 0,
    -- present -> the bytes are there.
    -- pending_delete -> marked, not yet removed. The soft step, so a mistake
    --   is recoverable and a retention sweep is reviewable before it bites.
    -- deleted -> the bytes are gone; the row stays as the record that they
    --   existed and when they went.
    status      text NOT NULL DEFAULT 'present'
                CHECK (status IN ('present','pending_delete','deleted')),
    created_at  text NOT NULL DEFAULT (datetime('now')),
    marked_at   text,
    deleted_at  text,
    UNIQUE (model_id, fmt)
);

CREATE INDEX IF NOT EXISTS artifacts_user_idx ON artifacts (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS artifacts_sweep_idx ON artifacts (status, created_at)
    WHERE status <> 'deleted';

-- Closure is soft: login is refused and sessions are revoked, but the row --
-- and with it the credit ledger, which is a financial record -- stays. Hard
-- deletion is a separate, explicit operator action. Reversible beats tidy.
ALTER TABLE users ADD COLUMN closed_at text;

-- Security- and billing-sensitive transitions, append-only.
--
-- `billing_events` records what the processor said. This records what *we*
-- did: a password changed, every session revoked, a plan moved by an operator,
-- credits adjusted by hand, an account closed. Those are the things somebody
-- asks about afterwards, and none of them was written down anywhere before.
--
-- Never holds a secret, a token, a password or a card. `detail` is for
-- "how many sessions" and "which plan", not for what changed.
CREATE TABLE IF NOT EXISTS audit_log (
    id         integer PRIMARY KEY AUTOINCREMENT,
    user_id    text REFERENCES users(id) ON DELETE SET NULL,
    action     text NOT NULL,
    -- Who did it: 'user', 'system', or an operator's name. Not an id, because
    -- an operator is not an account here.
    actor      text NOT NULL DEFAULT 'system',
    detail     text NOT NULL DEFAULT '{}',
    created_at text NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS audit_log_user_idx ON audit_log (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_log_action_idx ON audit_log (action, created_at DESC);
