-- ===========================================================================
-- Intraday Alpha Platform — relational data model, x-version 1
-- ===========================================================================
--
-- Portable DDL: this file runs UNCHANGED on SQLite 3 and PostgreSQL >= 13.
-- The portability rules (docs/DATA_MODEL.md section 5) are:
--
--   * BIGINT for every int64 / u32 / u16 / enum-as-int column (SQLite maps it
--     to INTEGER affinity; PostgreSQL to int8).  u64 ids fit: the platform
--     never allocates above 2^63-1 on a contract (conventions section 1).
--   * DOUBLE PRECISION for every double.  TEXT for ids, hashes, enum names
--     and JSON documents (canonical JSON, iap.contracts.versions).
--   * Booleans are BIGINT with CHECK (x IN (0, 1)) — SQLite has no BOOLEAN
--     type and PostgreSQL rejects integer literals for one.
--   * No AUTOINCREMENT / SERIAL / IDENTITY: every id is allocated by the
--     platform (order-id sequence, content hashes, make_trace_id).
--   * No wall clock: *_ts columns are event / ledger time supplied by the
--     writer (int64 ns since the Unix epoch), never NOW().
--   * CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS are idempotent
--     on both engines; views are DROP VIEW IF EXISTS + CREATE VIEW because
--     CREATE OR REPLACE VIEW is not SQLite and CREATE VIEW IF NOT EXISTS is
--     not PostgreSQL.  The one seed row uses INSERT ... SELECT ... WHERE NOT
--     EXISTS, since INSERT OR REPLACE is SQLite-only and ON CONFLICT differs.
--     (The Python Store's own upserts are SQLite INSERT OR REPLACE; a
--     PostgreSQL writer uses INSERT ... ON CONFLICT (pk) DO UPDATE.)
--   * Only correlated scalar subqueries, COALESCE, CASE, COUNT/SUM/MAX/MIN
--     and standard joins in views (no LATERAL, no DISTINCT ON, no ::casts).
--   * Statements are terminated by ';' and never contain a ';' inside a
--     string literal, so a trivial splitter (iap.store.ddl) works.
--
-- Foreign keys are declared only where the writer guarantees the parent row
-- in the same transaction: every decomposed decision-trace stage row refers
-- to decision_traces(trace_id).  Cross-artefact references (alpha_id,
-- experiment_id, instrument_id, venue_id) are logical: the artefacts are
-- imported independently and an index over a subset must not fail.
--
-- The store is a DERIVED, REBUILDABLE INDEX of the flat-file artefacts
-- (research/*.json, *.jsonl audits, Parquet feature store, JSON goldens).
-- Those files remain the source of truth; `python -m iap.store build`
-- recreates this database from them at any time.

-- ---------------------------------------------------------------------------
-- schema_version — the x-version of this DDL (one row)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    x_version   BIGINT NOT NULL PRIMARY KEY CHECK (x_version >= 1),
    description TEXT NOT NULL
);
INSERT INTO schema_version (x_version, description)
    SELECT 1, 'iap_v1: contracts x-version 1 (Phase 0, 2026-09-19)'
    WHERE NOT EXISTS (SELECT 1 FROM schema_version WHERE x_version = 1);

-- ---------------------------------------------------------------------------
-- Reference data (configs/instruments/instruments.json, configs/venues/venues.json)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instruments (
    instrument_id   BIGINT NOT NULL PRIMARY KEY CHECK (instrument_id >= 0),
    symbol          TEXT NOT NULL UNIQUE,
    asset_class     TEXT NOT NULL CHECK (asset_class IN ('EQUITY', 'ETF', 'FX')),
    currency        TEXT NOT NULL,                 -- quote currency (FX: quote_currency)
    base_currency   TEXT,                          -- FX only
    tick_size       DOUBLE PRECISION NOT NULL CHECK (tick_size > 0),
    lot_size        BIGINT NOT NULL CHECK (lot_size >= 1),
    ref_price       DOUBLE PRECISION NOT NULL CHECK (ref_price > 0),
    adv             DOUBLE PRECISION NOT NULL CHECK (adv >= 0),
    pip             DOUBLE PRECISION,              -- FX only
    venues_json     TEXT NOT NULL,                 -- JSON array of venue names
    underlying_json TEXT                           -- ETF: JSON array of constituent symbols
);

CREATE TABLE IF NOT EXISTS venues (
    venue_id               BIGINT NOT NULL PRIMARY KEY CHECK (venue_id >= 1 AND venue_id <= 65535),
    venue                  TEXT NOT NULL UNIQUE,   -- display name (XV1, LP1, ...)
    asset_class            TEXT NOT NULL CHECK (asset_class IN ('EQUITY', 'ETF', 'FX')),
    taker_fee_per_share    DOUBLE PRECISION,       -- equity venues
    maker_rebate_per_share DOUBLE PRECISION,       -- equity venues
    commission_per_million DOUBLE PRECISION,       -- FX venues
    latency_mean_ns        BIGINT NOT NULL CHECK (latency_mean_ns >= 0),
    latency_jitter_ns      BIGINT NOT NULL CHECK (latency_jitter_ns >= 0),
    supports_json          TEXT NOT NULL           -- JSON array of event kinds
);

-- ---------------------------------------------------------------------------
-- Sessions — one row per replay / paper / backtest session (written by the runner)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT NOT NULL PRIMARY KEY,
    data_version   TEXT NOT NULL,                  -- sha256 of the normalized dataset
    config_version TEXT NOT NULL,                  -- content_hash of the configs in force
    seed           BIGINT NOT NULL CHECK (seed >= 0),
    start_ts       BIGINT NOT NULL,
    end_ts         BIGINT NOT NULL CHECK (end_ts >= start_ts),
    n_events       BIGINT NOT NULL CHECK (n_events >= 0)
);

-- ---------------------------------------------------------------------------
-- Feature registry versions (data/reference/feature_registry.json)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feature_versions (
    feature_version TEXT NOT NULL PRIMARY KEY,     -- the hash stamped on FeatureVectors (tracker.feature_version)
    registry_hash   TEXT NOT NULL,                 -- the registry document's own registry_hash field
    x_version       BIGINT NOT NULL CHECK (x_version >= 1),
    n_features      BIGINT NOT NULL CHECK (n_features >= 0),
    registry_json   TEXT NOT NULL                  -- canonical JSON of the feature list
);

-- ---------------------------------------------------------------------------
-- Alphas — the flagship registry (research/alpha_reports + iap.alpha)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alphas (
    alpha_id           TEXT NOT NULL PRIMARY KEY,
    asset_class        TEXT NOT NULL CHECK (asset_class IN ('EQUITY', 'FX')),
    family             TEXT NOT NULL,              -- the alpha's pinned human name (ofi_multilevel, ...)
    horizon            TEXT NOT NULL,              -- pinned label horizon (1s, 5s, ...)
    economic_rationale TEXT NOT NULL,
    current_state      TEXT NOT NULL CHECK (current_state IN
        ('RESEARCH', 'CANDIDATE', 'VALIDATING', 'PAPER', 'ACTIVE', 'WATCH', 'RETIRED'))
);

-- ---------------------------------------------------------------------------
-- Experiments — ExperimentSpec (schemas/research/experiment_spec.schema.json)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id       TEXT NOT NULL PRIMARY KEY,
    alpha_id            TEXT NOT NULL,
    dataset_version     TEXT NOT NULL,
    feature_version     TEXT NOT NULL,
    model_version       TEXT,                      -- NULL for an unfitted alpha
    configuration_json  TEXT NOT NULL,             -- free-form protocol, canonical JSON
    train_start_ts      BIGINT NOT NULL,
    train_end_ts        BIGINT NOT NULL CHECK (train_end_ts >= train_start_ts),
    validation_start_ts BIGINT NOT NULL CHECK (validation_start_ts >= train_end_ts),
    validation_end_ts   BIGINT NOT NULL CHECK (validation_end_ts >= validation_start_ts),
    test_start_ts       BIGINT NOT NULL CHECK (test_start_ts >= validation_end_ts),
    test_end_ts         BIGINT NOT NULL CHECK (test_end_ts >= test_start_ts),
    seed                BIGINT NOT NULL CHECK (seed >= 0),
    horizon             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_experiments_alpha ON experiments (alpha_id);

-- ---------------------------------------------------------------------------
-- Experiment results — ExperimentResult (schemas/research/experiment_result.schema.json)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS experiment_results (
    experiment_id            TEXT NOT NULL PRIMARY KEY,
    alpha_id                 TEXT NOT NULL,
    dataset_version          TEXT NOT NULL,
    feature_version          TEXT NOT NULL,
    model_version            TEXT,
    ic                       DOUBLE PRECISION NOT NULL,
    rank_ic                  DOUBLE PRECISION NOT NULL,
    t_stat                   DOUBLE PRECISION NOT NULL,
    nw_lags                  BIGINT NOT NULL CHECK (nw_lags >= 0),
    hit_rate                 DOUBLE PRECISION NOT NULL CHECK (hit_rate >= 0 AND hit_rate <= 1),
    turnover                 DOUBLE PRECISION NOT NULL CHECK (turnover >= 0),
    gross_return_bps         DOUBLE PRECISION NOT NULL,
    transaction_cost_bps     DOUBLE PRECISION NOT NULL CHECK (transaction_cost_bps >= 0),
    net_return_bps           DOUBLE PRECISION NOT NULL,
    max_drawdown_bps         DOUBLE PRECISION NOT NULL CHECK (max_drawdown_bps >= 0),
    sharpe                   DOUBLE PRECISION NOT NULL,
    fold_consistency         DOUBLE PRECISION NOT NULL CHECK (fold_consistency >= 0 AND fold_consistency <= 1),
    n_folds                  BIGINT NOT NULL CHECK (n_folds >= 0),
    leakage_passed           BIGINT NOT NULL CHECK (leakage_passed IN (0, 1)),
    leakage_detail_json      TEXT NOT NULL,
    hypothesis_sign_confirmed BIGINT CHECK (hypothesis_sign_confirmed IN (0, 1)),
    verdict                  TEXT NOT NULL CHECK (verdict IN ('PROMOTE', 'ITERATE', 'REJECT')),
    n_experiments_in_ledger  BIGINT NOT NULL CHECK (n_experiments_in_ledger >= 0),
    git_commit               TEXT NOT NULL,
    created_ts               BIGINT NOT NULL CHECK (created_ts >= 0),
    CHECK (leakage_passed = 1 OR verdict = 'REJECT')
);
CREATE INDEX IF NOT EXISTS ix_experiment_results_alpha ON experiment_results (alpha_id, created_ts);

-- ---------------------------------------------------------------------------
-- Multiple-testing ledger (research/experiments.json, spec section 13)
-- One row per distinct (alpha_id, kind, canonical config); `count` is the
-- number of looks that key represents, so SUM(count) is the Bonferroni
-- denominator and COUNT(*) the number of distinct experiments.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledger_entries (
    ledger_key  TEXT NOT NULL PRIMARY KEY,         -- sha256 of (alpha_id, kind, config)
    alpha_id    TEXT NOT NULL,                     -- 'ALL' for program-wide scans
    kind        TEXT NOT NULL,
    config_json TEXT NOT NULL,
    count       BIGINT NOT NULL CHECK (count >= 1),
    n           BIGINT NOT NULL CHECK (n >= 0),    -- ledger position (running total) at registration
    reruns      BIGINT NOT NULL CHECK (reruns >= 0),
    oos_ic      DOUBLE PRECISION,                  -- promotion_pipeline entries
    nw_tstat    DOUBLE PRECISION,
    verdict     TEXT CHECK (verdict IN ('PROMOTE', 'ITERATE', 'REJECT')),
    result_json TEXT NOT NULL                      -- the entry's full result object
);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_alpha ON ledger_entries (alpha_id, kind);

-- ---------------------------------------------------------------------------
-- Lifecycle transitions — LifecycleTransition (schemas/alpha/lifecycle_transition.schema.json)
-- `source` names the artefact the row came from (lifecycle_log = research
-- policy comparison, lifecycle_transitions = the lifecycle service ledger,
-- api = written through Store.insert_lifecycle_transition).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lifecycle_transitions (
    alpha_id   TEXT NOT NULL,
    policy     TEXT NOT NULL,
    event_ts   BIGINT NOT NULL,
    from_state TEXT NOT NULL CHECK (from_state IN
        ('RESEARCH', 'CANDIDATE', 'VALIDATING', 'PAPER', 'ACTIVE', 'WATCH', 'RETIRED')),
    to_state   TEXT NOT NULL CHECK (to_state IN
        ('RESEARCH', 'CANDIDATE', 'VALIDATING', 'PAPER', 'ACTIVE', 'WATCH', 'RETIRED')),
    source     TEXT NOT NULL,
    reason     TEXT NOT NULL,
    gates_json TEXT NOT NULL,                      -- {gate name: GateResult}
    actor      TEXT NOT NULL CHECK (actor IN ('SYSTEM', 'HUMAN')),
    eval_index BIGINT,                             -- lifecycle_log only
    PRIMARY KEY (alpha_id, policy, event_ts, from_state, to_state, source),
    CHECK (from_state <> to_state)
);
CREATE INDEX IF NOT EXISTS ix_lifecycle_transitions_alpha_ts ON lifecycle_transitions (alpha_id, event_ts);

-- ---------------------------------------------------------------------------
-- Decision traces — DecisionTrace (schemas/trace/decision_trace.schema.json)
-- The row holds the header columns plus the validated `stages` document;
-- the stage tables below are its normalized decomposition, all keyed by
-- trace_id and rewritten together by Store.insert_trace.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_traces (
    trace_id        TEXT NOT NULL PRIMARY KEY,     -- 32 hex, make_trace_id(...)
    session_id      TEXT NOT NULL,
    instrument_id   BIGINT NOT NULL CHECK (instrument_id >= 0),
    event_ts        BIGINT NOT NULL,
    sequence        BIGINT NOT NULL CHECK (sequence >= 0),
    data_version    TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    config_version  TEXT NOT NULL,
    stages_json     TEXT NOT NULL                  -- canonical JSON of TraceStages
);
CREATE INDEX IF NOT EXISTS ix_decision_traces_session ON decision_traces (session_id, event_ts, sequence);
CREATE INDEX IF NOT EXISTS ix_decision_traces_instrument ON decision_traces (instrument_id, event_ts);

-- AlphaSignal (schemas/alpha/alpha_signal.schema.json); signal_index = position in stages.signal
CREATE TABLE IF NOT EXISTS alpha_signals (
    trace_id        TEXT NOT NULL REFERENCES decision_traces (trace_id),
    signal_index    BIGINT NOT NULL CHECK (signal_index >= 0),
    timestamp       BIGINT NOT NULL,
    instrument_id   BIGINT NOT NULL CHECK (instrument_id >= 0),
    expected_return DOUBLE PRECISION NOT NULL,
    confidence      DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    horizon_ns      BIGINT NOT NULL,
    direction       BIGINT NOT NULL CHECK (direction IN (-1, 0, 1)),
    model_version   TEXT NOT NULL,
    PRIMARY KEY (trace_id, signal_index)
);
CREATE INDEX IF NOT EXISTS ix_alpha_signals_model ON alpha_signals (model_version, instrument_id, timestamp);

-- PortfolioTarget (schemas/portfolio/portfolio_target.schema.json); at most one per trace
CREATE TABLE IF NOT EXISTS portfolio_targets (
    trace_id          TEXT NOT NULL PRIMARY KEY REFERENCES decision_traces (trace_id),
    strategy_id       TEXT NOT NULL,
    timestamp_ns      BIGINT NOT NULL,
    portfolio_version TEXT NOT NULL,
    feature_version   TEXT NOT NULL,
    model_version     TEXT NOT NULL,
    solver_status     TEXT NOT NULL CHECK (solver_status IN ('OPTIMAL', 'INFEASIBLE', 'MAX_ITER')),
    objective_value   DOUBLE PRECISION NOT NULL,
    turnover          DOUBLE PRECISION NOT NULL CHECK (turnover >= 0),
    targets_json      TEXT NOT NULL                -- JSON array of PortfolioLeg
);

-- PortfolioLeg — one row per (trace, instrument) target
CREATE TABLE IF NOT EXISTS portfolio_legs (
    trace_id            TEXT NOT NULL REFERENCES decision_traces (trace_id),
    instrument_id       BIGINT NOT NULL CHECK (instrument_id >= 0),
    target_qty          BIGINT NOT NULL,
    target_weight       DOUBLE PRECISION NOT NULL,
    expected_return_bps DOUBLE PRECISION NOT NULL,
    prev_qty            BIGINT NOT NULL,
    PRIMARY KEY (trace_id, instrument_id)
);

-- RiskDecision (schemas/risk/risk_decision.schema.json); risk_index = position in stages.risk
CREATE TABLE IF NOT EXISTS risk_decisions (
    trace_id      TEXT NOT NULL REFERENCES decision_traces (trace_id),
    risk_index    BIGINT NOT NULL CHECK (risk_index >= 0),
    order_id      BIGINT NOT NULL CHECK (order_id >= 0),
    strategy_id   TEXT NOT NULL,
    instrument_id BIGINT NOT NULL CHECK (instrument_id >= 0),
    timestamp_ns  BIGINT NOT NULL,
    decision      BIGINT NOT NULL CHECK (decision IN (1, 2, 3)),   -- ALLOW=1 REJECT=2 KILL=3
    rule_id       TEXT NOT NULL,
    rule_index    BIGINT NOT NULL CHECK (rule_index >= -1 AND rule_index <= 65535),
    reason        TEXT NOT NULL,
    PRIMARY KEY (trace_id, risk_index),
    CHECK ((decision = 1 AND rule_index = -1) OR (decision <> 1 AND rule_index >= 0))
);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_order ON risk_decisions (order_id, timestamp_ns);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_decision ON risk_decisions (decision, rule_id);

-- ParentOrder (schemas/order/parent_order.schema.json)
CREATE TABLE IF NOT EXISTS parent_orders (
    parent_order_id   BIGINT NOT NULL PRIMARY KEY CHECK (parent_order_id >= 0),
    trace_id          TEXT NOT NULL REFERENCES decision_traces (trace_id),
    strategy_id       TEXT NOT NULL,
    alpha_id          TEXT NOT NULL,
    instrument_id     BIGINT NOT NULL CHECK (instrument_id >= 0),
    side              BIGINT NOT NULL CHECK (side IN (0, 1)),      -- BID=0 ASK=1
    qty               BIGINT NOT NULL CHECK (qty >= 1),
    algo              TEXT NOT NULL CHECK (algo IN ('TWAP', 'VWAP', 'POV', 'IS')),
    decision_ts       BIGINT NOT NULL,
    arrival_ts        BIGINT NOT NULL CHECK (arrival_ts >= decision_ts),
    end_ts            BIGINT NOT NULL CHECK (end_ts >= arrival_ts),
    urgency           DOUBLE PRECISION NOT NULL CHECK (urgency >= 0 AND urgency <= 1),
    limit_price_ticks BIGINT NOT NULL CHECK (limit_price_ticks >= 0),
    params_json       TEXT NOT NULL                -- {name: double}
);
CREATE INDEX IF NOT EXISTS ix_parent_orders_trace ON parent_orders (trace_id);
CREATE INDEX IF NOT EXISTS ix_parent_orders_alpha ON parent_orders (alpha_id, decision_ts);
CREATE INDEX IF NOT EXISTS ix_parent_orders_algo ON parent_orders (algo);

-- ChildOrder (schemas/order/child_order.schema.json)
CREATE TABLE IF NOT EXISTS child_orders (
    child_order_id  BIGINT NOT NULL PRIMARY KEY CHECK (child_order_id >= 0),
    trace_id        TEXT NOT NULL REFERENCES decision_traces (trace_id),
    parent_order_id BIGINT NOT NULL CHECK (parent_order_id >= 0),
    instrument_id   BIGINT NOT NULL CHECK (instrument_id >= 0),
    venue_id        BIGINT NOT NULL CHECK (venue_id >= 0 AND venue_id <= 65535),  -- 0 = SOR decides
    side            BIGINT NOT NULL CHECK (side IN (0, 1)),
    qty             BIGINT NOT NULL CHECK (qty >= 1),
    price_ticks     BIGINT NOT NULL CHECK (price_ticks >= 0),
    order_type      BIGINT NOT NULL CHECK (order_type >= 1 AND order_type <= 6),  -- MARKET..MID
    submit_ts       BIGINT NOT NULL,
    expire_ts       BIGINT NOT NULL,               -- 0 = parent end_ts
    slice_index     BIGINT NOT NULL CHECK (slice_index >= 0),
    CHECK (expire_ts = 0 OR expire_ts >= submit_ts),
    CHECK (order_type <> 1 OR price_ticks = 0)
);
CREATE INDEX IF NOT EXISTS ix_child_orders_parent ON child_orders (parent_order_id, slice_index);
CREATE INDEX IF NOT EXISTS ix_child_orders_trace ON child_orders (trace_id);

-- VenueDecision (schemas/execution/venue_decision.schema.json); one per child order
CREATE TABLE IF NOT EXISTS venue_decisions (
    child_order_id  BIGINT NOT NULL PRIMARY KEY CHECK (child_order_id >= 0),
    trace_id        TEXT NOT NULL REFERENCES decision_traces (trace_id),
    venue_id        BIGINT NOT NULL CHECK (venue_id >= 0 AND venue_id <= 65535),  -- 0 = NO_ROUTE
    reason          TEXT NOT NULL,
    candidates_json TEXT NOT NULL                  -- JSON array of VenueScore
);
CREATE INDEX IF NOT EXISTS ix_venue_decisions_trace ON venue_decisions (trace_id, venue_id);

-- ExecutionReport (schemas/execution/execution_report.schema.json)
-- parent_order_id is the parent link resolved through child_orders (NULL if
-- the child order is not part of the same trace).
CREATE TABLE IF NOT EXISTS executions (
    execution_id     BIGINT NOT NULL PRIMARY KEY CHECK (execution_id >= 0),
    trace_id         TEXT NOT NULL REFERENCES decision_traces (trace_id),
    order_id         BIGINT NOT NULL CHECK (order_id >= 0),           -- the child order
    parent_order_id  BIGINT,
    status           BIGINT NOT NULL CHECK (status >= 1 AND status <= 6),  -- NEW..EXPIRED
    filled_qty       BIGINT NOT NULL CHECK (filled_qty >= 0),
    fill_price_ticks BIGINT NOT NULL CHECK (fill_price_ticks >= 0),
    venue_id         BIGINT NOT NULL CHECK (venue_id >= 0 AND venue_id <= 65535),
    exchange_ts      BIGINT NOT NULL,
    receive_ts       BIGINT NOT NULL CHECK (receive_ts >= exchange_ts),
    fees             DOUBLE PRECISION NOT NULL,    -- negative = rebate
    CHECK ((status IN (2, 3) AND filled_qty > 0) OR (status NOT IN (2, 3) AND filled_qty = 0))
);
CREATE INDEX IF NOT EXISTS ix_executions_order ON executions (order_id, exchange_ts);
CREATE INDEX IF NOT EXISTS ix_executions_parent ON executions (parent_order_id);
CREATE INDEX IF NOT EXISTS ix_executions_trace ON executions (trace_id);

-- TCAResult (schemas/tca/tca_result.schema.json); one per parent order
CREATE TABLE IF NOT EXISTS tca_results (
    parent_order_id              BIGINT NOT NULL PRIMARY KEY CHECK (parent_order_id >= 0),
    trace_id                     TEXT NOT NULL REFERENCES decision_traces (trace_id),
    instrument_id                BIGINT NOT NULL CHECK (instrument_id >= 0),
    side                         BIGINT NOT NULL CHECK (side IN (0, 1)),
    qty                          BIGINT NOT NULL CHECK (qty >= 1),
    filled_qty                   BIGINT NOT NULL CHECK (filled_qty >= 0),
    fill_rate                    DOUBLE PRECISION NOT NULL CHECK (fill_rate >= 0 AND fill_rate <= 1),
    arrival_price_ticks          BIGINT NOT NULL CHECK (arrival_price_ticks >= 0),
    avg_fill_price               DOUBLE PRECISION NOT NULL,          -- ticks
    interval_vwap                DOUBLE PRECISION NOT NULL,
    interval_twap                DOUBLE PRECISION NOT NULL,
    implementation_shortfall_bps DOUBLE PRECISION NOT NULL,
    delay_cost_bps               DOUBLE PRECISION NOT NULL,
    trading_cost_bps             DOUBLE PRECISION NOT NULL,
    opportunity_cost_bps         DOUBLE PRECISION NOT NULL,
    spread_cost_bps              DOUBLE PRECISION NOT NULL,
    impact_bps                   DOUBLE PRECISION NOT NULL,
    fees_bps                     DOUBLE PRECISION NOT NULL,
    timing_cost_bps              DOUBLE PRECISION NOT NULL,
    slippage_bps                 DOUBLE PRECISION NOT NULL,
    participation_rate           DOUBLE PRECISION NOT NULL CHECK (participation_rate >= 0 AND participation_rate <= 1),
    n_fills                      BIGINT NOT NULL CHECK (n_fills >= 0),
    venue_contribution_json      TEXT NOT NULL,    -- {decimal venue id: bps}
    algo                         TEXT NOT NULL CHECK (algo IN ('TWAP', 'VWAP', 'POV', 'IS')),
    latency_min_ns               BIGINT NOT NULL CHECK (latency_min_ns >= 0),
    latency_mean_ns              DOUBLE PRECISION NOT NULL,
    latency_max_ns               BIGINT NOT NULL,
    latency_p50_ns               BIGINT NOT NULL,
    latency_p99_ns               BIGINT NOT NULL,
    CHECK (latency_min_ns <= latency_p50_ns AND latency_p50_ns <= latency_p99_ns AND latency_p99_ns <= latency_max_ns)
);
CREATE INDEX IF NOT EXISTS ix_tca_results_trace ON tca_results (trace_id);
CREATE INDEX IF NOT EXISTS ix_tca_results_algo ON tca_results (algo, instrument_id);

-- Attribution (schemas/trace/decision_trace.schema.json#/$defs/Attribution); one per trace
CREATE TABLE IF NOT EXISTS attribution (
    trace_id        TEXT NOT NULL PRIMARY KEY REFERENCES decision_traces (trace_id),
    parent_order_id BIGINT,                        -- first parent order of the trace, if any
    alpha_bps       DOUBLE PRECISION NOT NULL,
    spread_bps      DOUBLE PRECISION NOT NULL,
    impact_bps      DOUBLE PRECISION NOT NULL,
    fees_bps        DOUBLE PRECISION NOT NULL,
    timing_bps      DOUBLE PRECISION NOT NULL,
    total_bps       DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attribution_parent ON attribution (parent_order_id);

-- ---------------------------------------------------------------------------
-- Research TCA harness orders (research/tca/tca_orders.json, iap.tca.report)
-- These are the simulated parent orders of TCA_REPORT.md: research-layer
-- doubles in real price units (conventions section 1), NOT TCAResult rows —
-- the harness records neither an algo nor latency, so the contract cannot
-- be filled honestly.  The Perold identity holds per row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tca_orders (
    order_id                    BIGINT NOT NULL PRIMARY KEY CHECK (order_id >= 0),
    instrument_id               BIGINT NOT NULL CHECK (instrument_id >= 0),
    side                        TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty_target                  DOUBLE PRECISION NOT NULL CHECK (qty_target > 0),
    qty_filled                  DOUBLE PRECISION NOT NULL CHECK (qty_filled >= 0),
    fill_rate                   DOUBLE PRECISION NOT NULL CHECK (fill_rate >= 0 AND fill_rate <= 1),
    n_fills                     BIGINT NOT NULL CHECK (n_fills >= 0),
    decision_mid                DOUBLE PRECISION NOT NULL,
    arrival_mid                 DOUBLE PRECISION NOT NULL,
    end_mid                     DOUBLE PRECISION NOT NULL,
    fill_vwap                   DOUBLE PRECISION,  -- NULL when nothing filled
    total_is_bps                DOUBLE PRECISION NOT NULL,
    delay_bps                   DOUBLE PRECISION NOT NULL,
    trading_bps                 DOUBLE PRECISION NOT NULL,
    opportunity_bps             DOUBLE PRECISION NOT NULL,
    total_is                    DOUBLE PRECISION NOT NULL,   -- currency
    delay_cost                  DOUBLE PRECISION NOT NULL,
    trading_cost                DOUBLE PRECISION NOT NULL,
    opportunity_cost            DOUBLE PRECISION NOT NULL,
    spread_cost                 DOUBLE PRECISION NOT NULL,
    impact_cost                 DOUBLE PRECISION NOT NULL,
    timing_cost                 DOUBLE PRECISION NOT NULL,
    arrival_slippage_bps        DOUBLE PRECISION NOT NULL,
    vwap_slippage_bps           DOUBLE PRECISION,  -- NULL when no interval VWAP
    twap_slippage_bps           DOUBLE PRECISION,
    execution_alpha_vs_vwap_bps DOUBLE PRECISION,
    adverse_selection_json      TEXT NOT NULL      -- {horizon: bps} markouts
);
CREATE INDEX IF NOT EXISTS ix_tca_orders_instrument ON tca_orders (instrument_id, order_id);

-- ---------------------------------------------------------------------------
-- Model runs (research/models/ledger.json + <run_id>/manifest.json, metrics.json)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_runs (
    run_id          TEXT NOT NULL PRIMARY KEY,     -- == manifest.experiment_id
    name            TEXT NOT NULL,                 -- ledger name (ols, ridge, ...)
    model_version   TEXT NOT NULL,
    data_version    TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    git_commit      TEXT NOT NULL,
    git_dirty       BIGINT CHECK (git_dirty IN (0, 1)),
    train_start_ts  BIGINT NOT NULL,
    train_end_ts    BIGINT NOT NULL CHECK (train_end_ts >= train_start_ts),
    test_start_ts   BIGINT NOT NULL,
    test_end_ts     BIGINT NOT NULL CHECK (test_end_ts >= test_start_ts),
    hyperparams_json TEXT NOT NULL,
    hardware_json   TEXT NOT NULL,
    manifest_json   TEXT NOT NULL,                 -- the whole manifest, canonical JSON
    metrics_json    TEXT,                          -- the whole metrics document, if present
    mean_ic         DOUBLE PRECISION,              -- scalar metrics lifted for queries
    mean_rank_ic    DOUBLE PRECISION,
    ic_tstat        DOUBLE PRECISION,
    pooled_ic       DOUBLE PRECISION,
    auc_test        DOUBLE PRECISION,              -- meta-label runs
    brier_test      DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS ix_model_runs_version ON model_runs (model_version, data_version);

-- ---------------------------------------------------------------------------
-- Drift baselines (research/baselines/*.json, API_ADAPTIVE)
-- kind = feature | signal (PSI histograms) or ic (rolling-IC baseline)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS drift_baselines (
    name               TEXT NOT NULL PRIMARY KEY,
    alpha_id           TEXT NOT NULL,
    kind               TEXT NOT NULL CHECK (kind IN ('feature', 'signal', 'ic')),
    x_version          BIGINT NOT NULL CHECK (x_version >= 1),
    feature_version    TEXT NOT NULL,
    source             TEXT NOT NULL,
    n                  BIGINT CHECK (n >= 0),      -- histogram baselines
    n_buckets          BIGINT CHECK (n_buckets >= 1),
    mean               DOUBLE PRECISION,
    std                DOUBLE PRECISION,
    min_value          DOUBLE PRECISION,
    max_value          DOUBLE PRECISION,
    psi_eps            DOUBLE PRECISION,
    edges_json         TEXT,
    expected_frac_json TEXT,
    horizon            TEXT,                       -- ic baselines
    baseline_kind      TEXT,
    bucket_ns          BIGINT,
    ic_mean            DOUBLE PRECISION,
    ic_std             DOUBLE PRECISION,
    n_buckets_baseline BIGINT
);
CREATE INDEX IF NOT EXISTS ix_drift_baselines_alpha ON drift_baselines (alpha_id, kind);

-- ===========================================================================
-- Views
-- ===========================================================================

-- v_order_chain: one row per parent order — the observability chain
-- signal -> portfolio -> risk -> parent -> children -> fills -> TCA -> attribution.
-- The signal is the first signal of the trace for the order's instrument, the
-- risk decision the last one recorded for the order.
DROP VIEW IF EXISTS v_order_chain;
CREATE VIEW v_order_chain AS
SELECT
    po.parent_order_id,
    po.trace_id,
    dt.session_id,
    dt.event_ts,
    dt.sequence,
    po.strategy_id,
    po.alpha_id,
    po.instrument_id,
    po.side,
    po.qty,
    po.algo,
    po.urgency,
    sg.expected_return          AS signal_expected_return,
    sg.confidence               AS signal_confidence,
    sg.model_version            AS signal_model_version,
    pt.solver_status            AS portfolio_solver_status,
    pl.target_qty               AS portfolio_target_qty,
    rd.decision                 AS risk_decision,
    rd.rule_id                  AS risk_rule_id,
    rd.reason                   AS risk_reason,
    (SELECT COUNT(*) FROM child_orders c
       WHERE c.trace_id = po.trace_id AND c.parent_order_id = po.parent_order_id)
                                AS n_child_orders,
    (SELECT COUNT(DISTINCT v.venue_id) FROM venue_decisions v
       JOIN child_orders c2 ON c2.child_order_id = v.child_order_id AND c2.trace_id = v.trace_id
       WHERE v.trace_id = po.trace_id AND c2.parent_order_id = po.parent_order_id AND v.venue_id <> 0)
                                AS n_venues_routed,
    (SELECT COUNT(*) FROM executions e
       WHERE e.trace_id = po.trace_id AND e.parent_order_id = po.parent_order_id)
                                AS n_fills,
    (SELECT COALESCE(SUM(e2.filled_qty), 0) FROM executions e2
       WHERE e2.trace_id = po.trace_id AND e2.parent_order_id = po.parent_order_id)
                                AS filled_qty,
    (SELECT COALESCE(SUM(e3.fees), 0.0) FROM executions e3
       WHERE e3.trace_id = po.trace_id AND e3.parent_order_id = po.parent_order_id)
                                AS fees,
    t.fill_rate                 AS tca_fill_rate,
    t.implementation_shortfall_bps,
    t.delay_cost_bps,
    t.trading_cost_bps,
    t.opportunity_cost_bps,
    t.spread_cost_bps,
    t.impact_bps                AS tca_impact_bps,
    t.fees_bps                  AS tca_fees_bps,
    t.timing_cost_bps,
    a.alpha_bps                 AS attribution_alpha_bps,
    a.spread_bps                AS attribution_spread_bps,
    a.impact_bps                AS attribution_impact_bps,
    a.fees_bps                  AS attribution_fees_bps,
    a.timing_bps                AS attribution_timing_bps,
    a.total_bps                 AS attribution_total_bps
FROM parent_orders po
JOIN decision_traces dt ON dt.trace_id = po.trace_id
LEFT JOIN alpha_signals sg
       ON sg.trace_id = po.trace_id
      AND sg.signal_index = (SELECT MIN(s2.signal_index) FROM alpha_signals s2
                              WHERE s2.trace_id = po.trace_id AND s2.instrument_id = po.instrument_id)
LEFT JOIN portfolio_targets pt ON pt.trace_id = po.trace_id
LEFT JOIN portfolio_legs pl ON pl.trace_id = po.trace_id AND pl.instrument_id = po.instrument_id
LEFT JOIN risk_decisions rd
       ON rd.trace_id = po.trace_id
      AND rd.risk_index = (SELECT MAX(r2.risk_index) FROM risk_decisions r2
                            WHERE r2.trace_id = po.trace_id AND r2.order_id = po.parent_order_id)
LEFT JOIN tca_results t ON t.parent_order_id = po.parent_order_id AND t.trace_id = po.trace_id
LEFT JOIN attribution a ON a.trace_id = po.trace_id;

-- v_alpha_scorecard: per alpha, the latest experiment result (max created_ts,
-- ties broken by the greatest experiment_id), its lifecycle state and its
-- share of the multiple-testing ledger.
DROP VIEW IF EXISTS v_alpha_scorecard;
CREATE VIEW v_alpha_scorecard AS
SELECT
    al.alpha_id,
    al.asset_class,
    al.family,
    al.horizon,
    al.current_state,
    r.experiment_id             AS latest_experiment_id,
    r.created_ts                AS latest_created_ts,
    r.ic,
    r.rank_ic,
    r.t_stat,
    r.hit_rate,
    r.fold_consistency,
    r.n_folds,
    r.leakage_passed,
    r.hypothesis_sign_confirmed,
    r.net_return_bps,
    r.verdict,
    (SELECT COUNT(*) FROM experiment_results r5 WHERE r5.alpha_id = al.alpha_id)
                                AS n_results,
    (SELECT COUNT(*) FROM ledger_entries l WHERE l.alpha_id = al.alpha_id)
                                AS ledger_entries,
    (SELECT COALESCE(SUM(l2.count), 0) FROM ledger_entries l2 WHERE l2.alpha_id = al.alpha_id)
                                AS ledger_count,
    (SELECT COUNT(*) FROM lifecycle_transitions lt WHERE lt.alpha_id = al.alpha_id)
                                AS n_transitions
FROM alphas al
LEFT JOIN experiment_results r
       ON r.alpha_id = al.alpha_id
      AND r.created_ts = (SELECT MAX(r3.created_ts) FROM experiment_results r3
                           WHERE r3.alpha_id = al.alpha_id)
      AND r.experiment_id = (SELECT MAX(r4.experiment_id) FROM experiment_results r4
                              WHERE r4.alpha_id = al.alpha_id AND r4.created_ts = r.created_ts);

-- v_experiment_ledger_summary: the multiple-testing ledger per kind.
-- SUM(total_count) over the view is the Bonferroni denominator
-- (research/experiments.json total_experiments), SUM(n_entries) the number
-- of distinct experiments.
DROP VIEW IF EXISTS v_experiment_ledger_summary;
CREATE VIEW v_experiment_ledger_summary AS
SELECT
    kind,
    COUNT(*)                    AS n_entries,
    COUNT(DISTINCT alpha_id)    AS n_alphas,
    SUM(count)                  AS total_count,
    SUM(reruns)                 AS total_reruns,
    SUM(CASE WHEN verdict = 'PROMOTE' THEN 1 ELSE 0 END) AS n_promote,
    SUM(CASE WHEN verdict = 'ITERATE' THEN 1 ELSE 0 END) AS n_iterate,
    SUM(CASE WHEN verdict = 'REJECT'  THEN 1 ELSE 0 END) AS n_reject,
    MAX(nw_tstat)               AS max_nw_tstat,
    MAX(oos_ic)                 AS max_oos_ic
FROM ledger_entries
GROUP BY kind;
