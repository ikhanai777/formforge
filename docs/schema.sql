-- FormForge data model (spec section 11).
--
-- Two of these tables are load-bearing in a way that is easy to miss, and both
-- collect data that cannot be recovered retroactively:
--
--   generation_events  is how the agent loop gets debugged. Without a row per
--                      step you can see that a generation took four iterations
--                      but not what changed between them, and tuning a prompt
--                      becomes guesswork.
--
--   print_feedback     is the only ground truth that exists for whether any of
--                      this works. Every DFM constant in the system is currently
--                      a conventional maker value; this table is what eventually
--                      lets them be set from evidence instead.
--
-- Start collecting both on day one, before anything consumes them.
--
-- This file is the Postgres target. `formforge/store.py` implements the same
-- tables and column names on stdlib sqlite3, which is what actually runs
-- today: a persistence layer that needs a database server is one that gets
-- switched off in development, and a table that is empty for the first six
-- months is worth nothing. Keep the two in step -- tests/test_store.py checks
-- that every column declared here exists there.
--
-- The accounts and billing tables are implemented in `formforge/accounts/`
-- rather than in `formforge/store.py`, and the split is deliberate: a
-- telemetry write that fails is swallowed into a counter so it cannot break a
-- generation someone is waiting on, and applying that same policy to a credit
-- deduction would silently give models away. Opposite failure policies belong
-- in different objects, where the difference is structural rather than a
-- comment someone has to remember.

CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Accounts
-- ---------------------------------------------------------------------------

-- What a user *is* here is deliberately thin: an identity, a plan, and a
-- pointer at whoever holds their card. How many generations they have left is
-- not a column -- see `credit_ledger` below for why.
CREATE TABLE users (
    id            uuid PRIMARY KEY,
    email         citext UNIQUE NOT NULL,
    -- Null for an account that has never set one (an OAuth identity, or a
    -- pre-provisioned row). Never the password itself: see formforge/accounts/auth.py.
    password_hash text,
    plan          text NOT NULL DEFAULT 'free'
                  CHECK (plan IN ('free','maker','studio')),
    plan_status   text NOT NULL DEFAULT 'active'
                  CHECK (plan_status IN ('active','past_due','cancelled')),
    -- Deliberately not named after a processor. Which one this is has not been
    -- decided, and a column called `stripe_customer_id` is how that decision
    -- gets made by accident, six months before anyone notices they made it.
    billing_customer_id text UNIQUE,
    -- The start of the billing period the current grant belongs to. What makes
    -- the monthly grant idempotent under webhook replay -- a second delivery
    -- for a period already granted lands on the ledger's unique key and is
    -- refused, rather than doubling someone's credits.
    period_start  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Sessions hold a *hash* of the bearer token, never the token. The threat is
-- specific and ordinary: a database backup, a log line, or a support engineer
-- with read access should not yield anything that can be replayed as a login.
CREATE TABLE sessions (
    token_hash text PRIMARY KEY,
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);

CREATE INDEX sessions_user_idx ON sessions (user_id);

-- ---------------------------------------------------------------------------
-- Template registry
-- ---------------------------------------------------------------------------

CREATE TABLE templates (
    id             text NOT NULL,
    version        int  NOT NULL,
    category       text NOT NULL,
    display_name   text NOT NULL,
    description    text NOT NULL,
    language       text NOT NULL,
    param_schema   jsonb NOT NULL,
    -- Postconditions over measured geometry, checked after generation.
    invariants     jsonb NOT NULL DEFAULT '[]',
    -- Preconditions over parameters, checked before it. Kept separate because
    -- a violated precondition is a parameter error, not a broken model.
    preconditions  jsonb NOT NULL DEFAULT '[]',
    source         text NOT NULL,
    embedding      vector(1536),
    -- {status: untested|passed|failed, target_printer, target_material,
    --  rationale, date}. An untested template must never be presented as
    --  print-tested anywhere in the product.
    print_test     jsonb NOT NULL DEFAULT '{"status": "untested"}',
    usage_count    bigint NOT NULL DEFAULT 0,
    success_rate   real,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, version)
);

CREATE INDEX templates_embedding_idx ON templates
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX templates_category_idx ON templates (category);

-- ---------------------------------------------------------------------------
-- Models
-- ---------------------------------------------------------------------------

CREATE TABLE models (
    id               uuid PRIMARY KEY,
    user_id          uuid REFERENCES users(id) ON DELETE SET NULL,
    -- Remix lineage. A modification is a new model with a parent, never an
    -- edit in place: the old one may already have been downloaded and printed.
    parent_id        uuid REFERENCES models(id) ON DELETE SET NULL,
    template_id      text,
    template_version int,
    prompt           text NOT NULL,
    parsed_intent    jsonb NOT NULL,
    params           jsonb NOT NULL,
    source_code      text NOT NULL,
    language         text NOT NULL,
    status           text NOT NULL
                     CHECK (status IN ('queued','running','ok','failed','refused',
                                       'needs_clarification')),
    route            text NOT NULL DEFAULT 'template'
                     CHECK (route IN ('template','template_seed','freeform')),
    iterations       int NOT NULL DEFAULT 1,
    bbox_mm          real[3],
    volume_mm3       real,
    triangle_count   int,
    validation       jsonb,
    slice_summary    jsonb,
    artifacts        jsonb,
    -- Cost accounting, per generation. `model_used` is the Claude model id;
    -- pin exact ids in config rather than trusting a floating alias.
    model_used       text,
    tokens_in        int,
    tokens_out       int,
    cache_read_tokens int,
    cost_usd         numeric(10,5),
    duration_ms      int,
    is_public        boolean NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (template_id, template_version) REFERENCES templates(id, version)
);

CREATE INDEX models_user_idx ON models (user_id, created_at DESC);
CREATE INDEX models_template_idx ON models (template_id) WHERE template_id IS NOT NULL;
CREATE INDEX models_public_idx ON models (created_at DESC) WHERE is_public;
CREATE INDEX models_parent_idx ON models (parent_id) WHERE parent_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Credits and billing
-- ---------------------------------------------------------------------------

-- A balance is a sum over this table, never a column someone updates.
--
-- The reason is not purity. A credit is the unit a customer paid for, and the
-- questions that get asked about it are all historical: why is this number
-- lower than I expect, what did I spend it on, did that failed build charge
-- me. A mutable counter answers none of them -- it holds the current value and
-- destroys the evidence for how it got there, which is precisely what a
-- support ticket needs. `quota_used int` was the first draft of this table and
-- it was the wrong shape for the same reason a bank does not store your
-- balance and throw away the transactions.
--
-- The rules that make it work:
--
--   * Append only. No UPDATE, no DELETE. A correction is a new row with the
--     opposite sign and a reason of 'adjustment' -- visible, not retroactive.
--   * `idempotency_key` is the concurrency and replay defence. A retried debit
--     for a build and a redelivered renewal webhook both carry a key that has
--     already been written, and the unique index refuses the second one. This
--     is what stops a customer being charged twice for one model.
--   * No rollover (unused credits expire at period end) is expressed as an
--     'expiry' row for the remainder, not as a reset. The expiry is then a
--     thing that happened at a time, which is what a customer arguing about it
--     is entitled to see.
CREATE TABLE credit_ledger (
    id         bigserial PRIMARY KEY,
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- Positive grants, negative spends. Never zero: a row that moves nothing
    -- is a bug that would otherwise sit in the history looking deliberate.
    delta      int NOT NULL CHECK (delta <> 0),
    reason     text NOT NULL
               CHECK (reason IN ('grant','purchase','spend','refund','expiry','adjustment')),
    -- Which generation this paid for. ON DELETE SET NULL rather than CASCADE:
    -- purging an old model must not erase the fact that someone was charged
    -- for it. The money outlives the geometry.
    model_id   uuid REFERENCES models(id) ON DELETE SET NULL,
    idempotency_key text UNIQUE,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX credit_ledger_user_idx ON credit_ledger (user_id, created_at DESC);
CREATE INDEX credit_ledger_model_idx ON credit_ledger (model_id) WHERE model_id IS NOT NULL;

-- Every webhook the payment processor sends, stored raw before it is acted on.
-- Two jobs: replay protection via the unique (provider, event_id), and an
-- answer to "the processor says they told us, did we hear it?" that does not
-- depend on the processor's own dashboard.
CREATE TABLE billing_events (
    id          bigserial PRIMARY KEY,
    user_id     uuid REFERENCES users(id) ON DELETE SET NULL,
    provider    text NOT NULL,
    event_id    text NOT NULL,
    event_type  text NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}',
    -- Null until the event has been turned into whatever it implies (a grant,
    -- a plan change). A row sitting here unhandled is the thing a health check
    -- should be looking for.
    handled_at  timestamptz,
    received_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, event_id)
);

CREATE INDEX billing_events_unhandled_idx ON billing_events (received_at)
    WHERE handled_at IS NULL;

-- ---------------------------------------------------------------------------
-- Agent-loop telemetry
-- ---------------------------------------------------------------------------

-- One row per loop step. This is what makes a four-iteration generation
-- explicable after the fact: which phase failed, what the validator said, and
-- what changed on the next attempt.
CREATE TABLE generation_events (
    id           bigserial PRIMARY KEY,
    model_id     uuid NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    step         int NOT NULL,
    phase        text NOT NULL
                 CHECK (phase IN ('policy','intent','route','codegen','execute',
                                  'validate','render','critique','escalate',
                                  'slice','failed','done')),
    ok           boolean NOT NULL,
    error_class  text,
    payload      jsonb,
    duration_ms  int,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX generation_events_model_idx ON generation_events (model_id, step);
-- Failure-mode analysis: which error classes dominate, and are they getting
-- rarer as the hint table grows?
CREATE INDEX generation_events_errors_idx ON generation_events (error_class, created_at DESC)
    WHERE NOT ok;

-- ---------------------------------------------------------------------------
-- Ground truth
-- ---------------------------------------------------------------------------

-- Did it actually print? Nothing else in this schema can answer that, and no
-- amount of validation substitutes for it.
CREATE TABLE print_feedback (
    id           uuid PRIMARY KEY,
    model_id     uuid NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    user_id      uuid REFERENCES users(id) ON DELETE SET NULL,
    printed      boolean NOT NULL,
    success      boolean,
    printer      text,
    material     text,
    -- warping | support_failure | weak | dimension_off | text_illegible |
    -- didnt_fit | layer_shift | poor_adhesion | other
    issues       text[] NOT NULL DEFAULT '{}',
    photo_uri    text,
    notes        text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX print_feedback_model_idx ON print_feedback (model_id);
CREATE INDEX print_feedback_issues_idx ON print_feedback USING gin (issues);

-- Refused requests, kept for content-policy review and rate limiting. A user
-- repeatedly probing the IP classifier is a signal worth having.
CREATE TABLE policy_events (
    id           bigserial PRIMARY KEY,
    user_id      uuid REFERENCES users(id) ON DELETE SET NULL,
    prompt       text NOT NULL,
    decision     text NOT NULL CHECK (decision IN ('allow','flag','refuse')),
    category     text,
    matched      text[],
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX policy_events_user_idx ON policy_events (user_id, created_at DESC)
    WHERE decision <> 'allow';

-- ---------------------------------------------------------------------------
-- Views the product actually reads
-- ---------------------------------------------------------------------------

-- What a user can spend right now. Defined once, here, so the paywall and the
-- account page cannot disagree about it -- two independent SUMs over the same
-- ledger is how a customer ends up seeing one number and being refused by
-- another.
CREATE VIEW credit_balance AS
SELECT
    u.id                          AS user_id,
    u.email                       AS email,
    u.plan                        AS plan,
    u.plan_status                 AS plan_status,
    coalesce(sum(l.delta), 0)::bigint AS balance,
    max(l.created_at)             AS last_movement_at
FROM users u
LEFT JOIN credit_ledger l ON l.user_id = u.id
GROUP BY u.id, u.email, u.plan, u.plan_status;

-- Which templates earn their place, and which are quietly failing. A template
-- with a low success rate is a registry bug that traffic is still being routed
-- to.
CREATE VIEW template_health AS
SELECT
    m.template_id,
    m.template_version,
    count(*)                                              AS generations,
    avg((m.status = 'ok')::int)::real                     AS success_rate,
    avg(m.iterations)::real                               AS mean_iterations,
    avg(m.duration_ms)::real                              AS mean_duration_ms,
    sum(m.cost_usd)                                       AS total_cost_usd,
    count(pf.id) FILTER (WHERE pf.printed)                AS prints_reported,
    avg((pf.success)::int) FILTER (WHERE pf.printed)::real AS print_success_rate
FROM models m
LEFT JOIN print_feedback pf ON pf.model_id = m.id
WHERE m.template_id IS NOT NULL
GROUP BY m.template_id, m.template_version;

-- The empirical basis for tuning DFM constants: what actually went wrong, cross
-- referenced against what the validator measured at the time.
CREATE VIEW print_outcomes AS
SELECT
    pf.model_id,
    pf.success,
    pf.issues,
    pf.printer,
    pf.material,
    m.template_id,
    m.validation -> 'measurements' ->> 'min_wall_mm'            AS min_wall_mm,
    m.validation -> 'measurements' ->> 'overhang_fraction'      AS overhang_fraction,
    m.validation -> 'measurements' ->> 'max_bridge_mm'          AS max_bridge_mm,
    m.validation -> 'measurements' ->> 'plate_contact_fraction' AS plate_contact_fraction,
    jsonb_array_length(coalesce(m.validation -> 'warnings', '[]'::jsonb)) AS warning_count
FROM print_feedback pf
JOIN models m ON m.id = pf.model_id
WHERE pf.printed;
