-- Single-use password reset tokens.
--
-- Only a hash is stored, for the same reason sessions store only a hash: a
-- database backup, a log line or a support engineer with read access must not
-- yield anything replayable. A reset token *is* the password for as long as it
-- lives, so it is the last thing that should sit in a table in the clear.
--
-- SHA-256 rather than a KDF, deliberately, and for the same reason as sessions:
-- the token is 256 bits from the system CSPRNG, so there is nothing to guess
-- and a slow hash would buy no security while costing latency on a path that
-- is already rate limited.

CREATE TABLE IF NOT EXISTS password_resets (
    token_hash text PRIMARY KEY,
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    -- Set the moment it is redeemed. Single use is enforced by checking and
    -- setting this inside one transaction, so two requests racing with the
    -- same token cannot both win.
    used_at    timestamptz
);

CREATE INDEX IF NOT EXISTS password_resets_user_idx ON password_resets (user_id);
CREATE INDEX IF NOT EXISTS password_resets_live_idx ON password_resets (expires_at)
    WHERE used_at IS NULL;
