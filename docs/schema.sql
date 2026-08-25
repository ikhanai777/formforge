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

CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Accounts
-- ---------------------------------------------------------------------------

CREATE TABLE users (
    id            uuid PRIMARY KEY,
    email         citext UNIQUE NOT NULL,
    plan          text NOT NULL DEFAULT 'free',
    quota_month   int  NOT NULL DEFAULT 20,
    quota_used    int  NOT NULL DEFAULT 0,
    quota_reset_at timestamptz NOT NULL DEFAULT date_trunc('month', now()) + interval '1 month',
    created_at    timestamptz NOT NULL DEFAULT now()
);

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
