-- Bullion data model (spec sections 2 and 8).
--
-- This file is the TimescaleDB target. `bullion/store.py` implements the same tables and
-- the same column names on stdlib sqlite3, which is what actually runs today --
-- tests/test_bullion_store.py asserts the two agree column for column, so moving to
-- Postgres stays a dialect change rather than a redesign.
--
-- Two tables are load-bearing in a way that is easy to miss, and neither can be
-- backfilled:
--
--   signals          every call the engine has ever issued. Spec section 5 publishes a
--                    win rate computed from this table, which means it is the product's
--                    credibility rather than a log. A signal that was not written down at
--                    the time cannot be graded later, and a track record that starts the
--                    day you decide to measure is a track record with survivorship bias
--                    built in.
--
--   prices           the bars every signal was scored on. Free-tier providers do not serve
--                    deep history, so the only copy of the data a future backtest can
--                    replay is the one collected as it happened. Re-fetching in a year
--                    gets you a different, adjusted, possibly truncated series.
--
-- Start collecting both on day one, before anything consumes them.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------------
-- Market data
-- ---------------------------------------------------------------------------

CREATE TABLE prices (
    asset     text        NOT NULL,
    interval  text        NOT NULL,
    ts        timestamptz NOT NULL,
    open      double precision NOT NULL,
    high      double precision NOT NULL,
    low       double precision NOT NULL,
    close     double precision NOT NULL,
    volume    double precision NOT NULL DEFAULT 0,
    source    text        NOT NULL,
    PRIMARY KEY (asset, interval, ts)
);
-- The one place Timescale earns its keep: bars are append-mostly, queried by recent range,
-- and compress well. A week is the right chunk for a daily/hourly mix.
SELECT create_hypertable('prices', 'ts', chunk_time_interval => interval '7 days');

-- Indicator values as of each bar, stored rather than recomputed so that a historical
-- signal can be displayed with exactly the numbers it was made on. Recomputing for the UI
-- is how a dashboard ends up disagreeing with its own signal after a library upgrade.
CREATE TABLE indicators (
    asset     text        NOT NULL,
    interval  text        NOT NULL,
    ts        timestamptz NOT NULL,
    payload   jsonb       NOT NULL,
    PRIMARY KEY (asset, interval, ts)
);
SELECT create_hypertable('indicators', 'ts', chunk_time_interval => interval '30 days');

-- FRED series: real yields, the dollar index, CPI, the curve. One row per observation.
CREATE TABLE macro_series (
    series_id text NOT NULL,
    day       date NOT NULL,
    value     double precision NOT NULL,
    units     text NOT NULL DEFAULT '',
    source    text NOT NULL,
    PRIMARY KEY (series_id, day)
);

-- CFTC Commitment of Traders, weekly, published Friday for the preceding Tuesday.
CREATE TABLE cot_reports (
    asset          text NOT NULL,
    report_date    date NOT NULL,
    noncomm_long   double precision NOT NULL,
    noncomm_short  double precision NOT NULL,
    open_interest  double precision NOT NULL,
    source         text NOT NULL,
    PRIMARY KEY (asset, report_date)
);

-- Headlines with both scores kept side by side. `provider_sentiment` is whatever the feed
-- supplied; `direction`/`relevance` are what our own pass decided. Keeping both is how you
-- find out whether the LLM re-scoring pass earns its cost.
CREATE TABLE news_events (
    id                  text PRIMARY KEY,
    ts                  timestamptz NOT NULL,
    title               text NOT NULL,
    source              text NOT NULL,
    provider            text NOT NULL,
    url                 text,
    summary             text,
    provider_sentiment  double precision,
    direction           double precision,
    relevance           double precision,
    scorer              text,
    rationale           text,
    assets              jsonb NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX idx_news_ts ON news_events (ts DESC);

-- ---------------------------------------------------------------------------
-- Signals and their grading
-- ---------------------------------------------------------------------------

CREATE TABLE signals (
    id                 text PRIMARY KEY,
    asset              text NOT NULL,
    interval           text NOT NULL,
    ts                 timestamptz NOT NULL,
    -- STRONG_BUY | BUY | HOLD | SELL | STRONG_SELL | NO_SIGNAL
    state              text NOT NULL,
    -- strong | normal | none. Denormalised because the scorecard groups by it on every read.
    strength           text NOT NULL,
    composite_score    double precision NOT NULL,
    confidence         double precision NOT NULL,
    price_at_signal    double precision NOT NULL,
    suggested_stop     double precision,
    suggested_target   double precision,
    regime             text NOT NULL,
    weights            jsonb NOT NULL,
    layers             jsonb NOT NULL,
    confidence_detail  jsonb NOT NULL,
    data_quality       jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Set when any input came from the synthetic generator. The scorecard excludes these
    -- by default; without the flag a demo run would quietly corrupt the published record.
    synthetic          boolean NOT NULL DEFAULT false,
    -- The full API response as served. A track-record view then never has to re-derive a
    -- historical call from its parts, and a change to the payload shape cannot rewrite
    -- what users were actually shown.
    payload            jsonb NOT NULL
);
CREATE INDEX idx_signals_asset_ts ON signals (asset, ts DESC);

-- One row per graded signal. Outcomes resolve stop-first on ties inside a bar: without
-- tick data the order is unknowable, and resolving ties favourably is the most common way
-- a track record flatters itself.
CREATE TABLE signal_outcomes (
    signal_id    text PRIMARY KEY REFERENCES signals (id) ON DELETE CASCADE,
    resolved_at  timestamptz NOT NULL,
    -- target | stop | timeout
    outcome      text NOT NULL,
    bars_held    integer NOT NULL DEFAULT 0,
    exit_price   double precision,
    price_1d     double precision,
    price_3d     double precision,
    price_7d     double precision,
    return_1d    double precision,
    return_3d    double precision,
    return_7d    double precision,
    -- Maximum favourable and adverse excursion, in multiples of the risk taken to the
    -- stop. Together they say whether a timeout was nearly a win or nearly a loss.
    mfe_r        double precision,
    mae_r        double precision,
    notes        text
);

CREATE TABLE alerts (
    id         text PRIMARY KEY,
    ts         timestamptz NOT NULL,
    asset      text NOT NULL,
    -- invalidated | target_reached | new_signal | direction_change | confidence_shift |
    -- signal_withdrawn
    kind       text NOT NULL,
    signal_id  text REFERENCES signals (id) ON DELETE SET NULL,
    title      text NOT NULL,
    body       text NOT NULL,
    -- This table is the delivery queue. A push worker marks rows delivered; the engine
    -- never blocks on a transport it does not own.
    delivered  boolean NOT NULL DEFAULT false
);
CREATE INDEX idx_alerts_asset_ts ON alerts (asset, ts DESC);

-- ---------------------------------------------------------------------------
-- Backtests
-- ---------------------------------------------------------------------------

-- One row per walk-forward fold plus one for the pooled out-of-sample result, so the
-- in-sample/out-of-sample gap stays visible. A single "backtest result" row would let the
-- flattering number survive alone.
CREATE TABLE backtest_runs (
    id           text PRIMARY KEY,
    created_at   timestamptz NOT NULL,
    asset        text NOT NULL,
    interval     text NOT NULL,
    -- walk_forward_fold | walk_forward_oos
    kind         text NOT NULL,
    train_start  timestamptz,
    train_end    timestamptz,
    test_start   timestamptz,
    test_end     timestamptz,
    weights      jsonb NOT NULL,
    metrics      jsonb NOT NULL,
    sample_size  integer NOT NULL DEFAULT 0
);
CREATE INDEX idx_backtest_asset_created ON backtest_runs (asset, created_at DESC);

-- ---------------------------------------------------------------------------
-- Retention
-- ---------------------------------------------------------------------------

-- Deliberately no retention policy on `signals`, `signal_outcomes` or `backtest_runs`:
-- they are the track record and deleting them re-bases the published win rate. Compress
-- the bar data instead, which is where the volume actually is.
ALTER TABLE prices SET (timescaledb.compress, timescaledb.compress_segmentby = 'asset, interval');
SELECT add_compression_policy('prices', interval '90 days');
