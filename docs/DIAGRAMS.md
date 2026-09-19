# Architecture diagrams

Every diagram on this page renders natively on GitHub. Raw sources live in
[`docs/diagrams/`](diagrams/); the first three are also embedded in
[ARCHITECTURE.md](ARCHITECTURE.md) and are kept byte-identical to their `.mmd` sources.
Numbers shown (event counts, test counts, seeds) are the repository's actual values.

## 1. End-to-end pipeline (spec §4, realized)

The full path from synthetic venues to the research feedback loop. Solid arrows are
data flow; the dashed arrow is the research-to-production promotion loop.

```mermaid
flowchart TD
    subgraph GEN["Synthetic venues — seeded generator (SplitMix64, seed 20260829)"]
        V1["Equity MBO streams<br/>11 instruments @ venue XV1<br/>regimes, self-exciting flow, halt, auctions"]
        V2["FX QUOTE+TRADE streams<br/>8 G10 pairs @ LP1/LP2/PRI<br/>venue latency profiles"]
    end
    V1 --> RAW["data/raw/*.jsonl<br/>immutable raw feed files"]
    V2 --> RAW
    RAW --> NORM["Normalization + sequence validation<br/>python iap.marketdata.normalize<br/>gaps / dups / late / resets / ts-regressions / invalid counted -> qc_report.json"]
    NORM --> CANON["Canonical event stream<br/>JSONL + IAP1 v2 binary (72-byte LE records + CRC-32 trailer)<br/>+ Parquet research dataset — schemas x-version 1"]
    CANON --> BOOK["Order-book reconstruction<br/>Python ref / C++ / Rust / Java<br/>MBO FIFO, status-gated matching, synthetic ids, dup-drop,<br/>gap->stale, reorder window, sequence resets, SNAPSHOT recovery"]
    BOOK --> FEAT["Feature engine<br/>205-feature registry (hash = feature_version)<br/>native 40 in C++/Rust/Java — validity bitset, NaN never valid"]
    FEAT --> LBL["Event-time labels (Python-owned)<br/>11 horizons, mid-to-mid + cost-adjusted<br/>at-or-before rule, no lookahead"]
    FEAT --> ALPHA["Alpha ensemble<br/>24 flagship alphas (EQ01-FX12)<br/>linear_z_v1 scoring; 6 golden alphas ported"]
    ALPHA --> PORT["Portfolio construction<br/>PGD + prox, 7 constraint families, EWMA cov<br/>Python reference, Java production service"]
    PORT --> RISK["Hard risk engine — FAIL-CLOSED<br/>Rust reference, Java orchestration<br/>limits, throttles, gap/stale gates, kill switches"]
    RISK --> EXEC["Execution algos + event-driven simulator<br/>VWAP / TWAP / POV / IS; deterministic queue model<br/>C++ reference, Java port"]
    EXEC --> SOR["SOR / venue adapters<br/>cpp/sor, rust venue protocol + sim"]
    SOR --> FILLS["Executions (fills, fees, rebates, impact)"]
    FILLS --> TCA["TCA + attribution<br/>Perold IS = delay + trading + opportunity (exact)<br/>Python reference, Java service"]
    LBL --> RESEARCH
    TCA --> RESEARCH["Research feedback / alpha factory<br/>REPORT.md, ML_REPORT.md, experiments ledger,<br/>model manifests, promotion gates"]
    RESEARCH -. "promotion gates (spec §20)<br/>PROMOTE / ITERATE / REJECT" .-> ALPHA
```

## 2. Cross-language golden-test topology

How one validated Python reference pins four implementations. The parity table is
printed by `tests/harness/run_all.sh` (python 626 · cpp 243 · rust 254 · java 449
tests; 65/45/47/85 in the golden groups — the Java gate runs all ten
`*GoldenTest` classes).

```mermaid
flowchart LR
    subgraph REF["Python reference (validated first)"]
        MG["python/tools/make_golden.py<br/>make_golden_features.py<br/>make_golden_alpha.py"]
        BF["independent brute-force checks<br/>naive book, brute-force features,<br/>SLSQP portfolio optimum"]
        MG <--> BF
    end
    MG --> GV[("tests/golden/<br/>events_eq_mbo.jsonl (2,000 ev)<br/>events_fx_quote.jsonl (800 ev)<br/>+ splitmix64.json")]
    MG --> EXP[("expected_*.json<br/>codec sha256 | book states | features<br/>alpha | backtest | risk decisions + audit + snapshot<br/>replay fills | portfolio | tca (+ timeline cases) | adaptive")]
    CPPTOOL["cpp/tools/make_replay_fills_golden<br/>(C++ is the fills reference)"] --> EXP
    RSTOOL["rust/risk/src/bin/make_risk_golden<br/>(Rust is the risk reference)"] --> EXP
    GV --> PY["python: pytest -k golden<br/>65 tests"]
    GV --> CPP["cpp: ctest -R Golden<br/>45 tests"]
    GV --> RS["rust: 6 golden test targets<br/>47 tests"]
    GV --> JV["java: all ten *GoldenTest (JUnitCore)<br/>85 golden-group tests"]
    EXP --> PY
    EXP --> CPP
    EXP --> RS
    EXP --> JV
    PY --> TAB["tests/harness/run_all.sh<br/>cross-language parity table<br/>exit 0 iff all four PASS"]
    CPP --> TAB
    RS --> TAB
    JV --> TAB
    TOL["Tolerance policy (conventions §5):<br/>IAP1 encoding — byte-identical SHA-256<br/>integer state (ticks/sizes/counts/seq) — EXACT<br/>floats (features/alpha/portfolio/tca) — abs 1e-9 + rel 1e-9"] -.-> EXP
    MIG["regeneration: deliberate only,<br/>+ schemas/MIGRATIONS.md entry"] -.-> MG
```

## 3. Paper-trading session (Java platform vertical)

The live loop behind `java/paper.sh`: every subsystem the platform ships, wired
end-to-end with metrics served on `:8080`.

```mermaid
sequenceDiagram
    autonumber
    actor Op as Operator
    participant PT as PaperTrading<br/>(com.iap.platform)
    participant BK as Book + FeatureEngine<br/>(com.iap.orderbook / features)
    participant AL as Alpha scorers<br/>(com.iap.alpha, params from configs)
    participant PF as PortfolioOptimizer<br/>(com.iap.portfolio)
    participant RK as RiskEngine<br/>(com.iap.risk, fail-closed)
    participant EX as ExecutionSimulator<br/>(com.iap.execution)
    participant MX as MetricsServer :8080<br/>(com.iap.api)
    participant PR as Prometheus/Grafana

    Op->>PT: java/paper.sh [--mode realtime --speed 60]
    PT->>PT: load configs/ (instruments, venues,<br/>strategies, risk, execution, alpha_params)
    PT->>MX: bind /metrics /health /ready /status /admin/*

    loop every MarketEvent (event-time order)
        PT->>BK: apply(event) — sequence check, book update
        BK-->>BK: feature refresh (validity bitset)
        BK-->>AL: FeatureVector
        AL-->>PF: AlphaSignal {expected_return, confidence}
        PF-->>RK: OrderRequest (target position delta)
        alt risk ALLOW
            RK-->>EX: forward child order (open-order tracked)
            EX-->>PT: Fill(s) {price_ticks, qty, fee, impact}
            PT->>RK: onFill / onOrderDone (before the next decision)
            PT->>PT: position / P&L accounting (reporting ccy)
        else risk REJECT
            RK-->>PT: RiskEvent {rule_id, severity, decision}
        end
        PT->>MX: update counters + latency histograms
    end

    PR->>MX: GET /metrics (scrape, 15s interval)
    Op->>MX: curl /status -> live events_processed, kill state
    PT->>Op: summary line + out/paper_session_report.json<br/>(events, orders, fills, pnl, risk allowed/rejected)
```

## 4. Polyglot responsibility matrix, realized (spec §3)

Which language owns which subsystem in this repository, and which direction the
reference/port relationships run.

```mermaid
flowchart LR
    subgraph PYL["Python — research + reference"]
        pcore["core / codec / book / replay<br/>(reference for all ports)"]
        pfeat["205-feature factory + labels"]
        presearch["24 alphas · validation · ML ·<br/>portfolio ref · TCA ref · backtester"]
    end
    subgraph CPPL["C++ — latency-critical path"]
        ccore["codec · book · replay<br/>174.4 / 25.7 ns per event"]
        cfeat["48-feature native engine ≈530 ns"]
        cexec["execution simulator + algos + SOR<br/>(FILLS REFERENCE)"]
        cbench["bench_all — published methodology"]
    end
    subgraph RSL["Rust — safety-critical infrastructure"]
        rcore["codec · book · replay · SPSC eventbus"]
        rrisk["fail-closed risk engine<br/>(RISK REFERENCE, audit JSONL)"]
        rven["venue wire codec + simulated venue"]
        rtel["telemetry: counters/gauges/histograms<br/>Prometheus exposition"]
    end
    subgraph JVL["Java — institutional platform layer"]
        jcore["codec · book · replay · features · alphas"]
        jplat["event-driven backtester · portfolio service ·<br/>risk orchestration · execution/SOR · TCA service"]
        jops["monitoring /metrics /health · config service ·<br/>paper trading"]
    end
    pcore -->|"golden vectors + expected_*"| ccore
    pcore -->|"golden"| rcore
    pcore -->|"golden"| jcore
    cexec -->|"expected_replay_fills.json"| jplat
    rrisk -->|"expected_risk_decisions.json<br/>byte-identical audit"| jplat
    presearch -->|"expected_portfolio / tca / alpha"| jplat
    rtel -.->|"metric naming contract"| jops
```

## 5. Hard-risk decision flow (fail-closed)

The pinned check order shared by the Rust reference and the Java port. Any missing
or malformed limit configuration rejects (CONFIG_MISSING) — the engine never
"fails open."

```mermaid
flowchart TD
    OR["OrderRequest"] --> BS{"config loaded and<br/>state bootstrapped?"}
    BS -- no --> REJ["REJECT + RiskEvent<br/>(rule_id, severity, audit JSONL —<br/>money via fmt_fixed, byte-identical Rust/Java)"]
    BS -- yes --> KG{"kill switches?<br/>global > strategy > instrument > venue"}
    KG -- engaged --> REJ
    KG -- clear --> MAL{"malformed / unknown instrument?<br/>duplicate order_id?"}
    MAL -- yes --> REJ
    MAL -- no --> PB{"venue disconnected? sequence gap?<br/>stale mark (market-data time)?"}
    PB -- breach --> REJ
    PB -- ok --> FF{"fat-finger qty?<br/>FX rate present + fresh?<br/>fat-finger notional (qty x qty_unit x price x fx)?<br/>price band vs last mid?"}
    FF -- breach --> REJ
    FF -- ok --> RT{"order-rate token bucket<br/>(event-time refill, never backwards)"}
    RT -- exhausted --> REJ
    RT -- ok --> SM{"self-match vs ALL own open orders<br/>(any strategy/venue; unpriced = crosses)"}
    SM -- would cross --> REJ
    SM -- ok --> LIM{"worst-case projections incl. every open order:<br/>position / instrument / gross / net notional"}
    LIM -- breach --> REJ
    LIM -- ok --> PNL{"daily / strategy loss limits<br/>(realized + unrealized MTM, reporting ccy)"}
    PNL -- breached --> REJ
    PNL -- ok --> ALLOW["ALLOW -> execution<br/>(order tracked open until on_order_done / full fill)"]
    ALLOW --> FILLS["fills + terminal reports feed back:<br/>positions, lots, open-order set"]
    MK["market updates (mid, ts):<br/>marks held lots"] --> LATCH{"daily P&L <= -limit?"}
    FILLS --> LATCH
    LATCH -- yes --> KILL["latch STRATEGY then GLOBAL kill<br/>(no fill required)"]
    KILL --> KG
    RE["re-arm: override_loss_limit (audited)<br/>then clear_kill; roll_session keeps kills;<br/>snapshot/restore for restarts"] -.-> KG
    FILLS -.-> SM
    FILLS -.-> LIM
```

## 6. Passive queue-position model (execution simulator)

The deterministic rule set (C++ reference headers `cpp/include/iap/execution/*.hpp`,
mirrored by Java) that decides when a resting passive order fills during replay.

```mermaid
flowchart TD
    SUB["Child LIMIT order submitted<br/>arrival = decision_ts + fixed latency + seeded jitter (one draw)"] --> JOIN["Join level at price p:<br/>ahead_qty = displayed depth at p on arrival"]
    JOIN --> OBS{"next market event at level p"}
    OBS -->|"EXECUTE qty q ahead"| DEC1["ahead_qty -= q<br/>(leftover after ahead exhausted fills US)"]
    OBS -->|"CANCEL qty q ahead"| DEC2["ahead_qty -= q"]
    OBS -->|"marketable ADD consumes level"| DEC3["treated like EXECUTE volume<br/>(marketable-ADD expansion)"]
    OBS -->|"trade-through: opposite side<br/>crosses our price"| FILL["FILL (maker) at our price<br/>+ rebate - none of the spread"]
    DEC1 --> CHK{"ahead_qty <= 0 and<br/>incoming volume remains?"}
    DEC2 --> OBS
    DEC3 --> OBS
    CHK -- yes --> FILL
    CHK -- no --> OBS
    FILL --> ACC["Fill record: price_ticks, qty, ts,<br/>maker/taker flag, fee/rebate, impact<br/>(golden: expected_replay_fills.json)"]
    CROSS["crossing exemption: displayed liquidity a<br/>marketable limit already consumed is not<br/>double-counted against ahead_qty"] -.-> DEC3
    GATE["venue gate (rule 8): book missing / stale / not TRADING<br/>=> no fill of any kind; MARKET/IOC/FOK cancelled VENUE_NOT_TRADING,<br/>LIMIT rests; re-open fills crossed resting orders at the touch"] -.-> OBS
    TIF["cancel (rule 7): same latency path, effective at<br/>max(cancel arrival, order arrival); expire_ts = parent end_ts<br/>expires pending or resting before activation"] -.-> OBS
    OVL["overlay (rule 3b): displayed liquidity our earlier child<br/>consumed is not re-used by a later child"] -.-> SUB
```

## 7. Platform data model (`schemas/sql/iap_v1.sql`)

The relational index over every contract and research artefact, as built by
`python -m iap.store build` (SQLite; the same DDL runs on PostgreSQL). Solid
edges are declared foreign keys — every normalized stage row of a decision
trace hangs off `decision_traces`; dotted edges are logical references between
independently imported artefacts. Column lists are abridged; the full model,
the views and the query cookbook are in [DATA_MODEL.md](DATA_MODEL.md).

```mermaid
erDiagram
    %% schemas/sql/iap_v1.sql — x-version 1. Solid lines are declared foreign
    %% keys (every stage row of a decision trace); dotted lines are logical
    %% references between independently imported artefacts.

    instruments {
        BIGINT instrument_id PK
        TEXT symbol
        TEXT asset_class
        TEXT currency
        DOUBLE_PRECISION tick_size
        BIGINT lot_size
        DOUBLE_PRECISION adv
        TEXT venues_json
    }
    venues {
        BIGINT venue_id PK
        TEXT venue
        TEXT asset_class
        DOUBLE_PRECISION taker_fee_per_share
        DOUBLE_PRECISION maker_rebate_per_share
        DOUBLE_PRECISION commission_per_million
        BIGINT latency_mean_ns
        TEXT supports_json
    }
    sessions {
        TEXT session_id PK
        TEXT data_version
        TEXT config_version
        BIGINT seed
        BIGINT start_ts
        BIGINT end_ts
        BIGINT n_events
    }
    feature_versions {
        TEXT feature_version PK
        TEXT registry_hash
        BIGINT x_version
        BIGINT n_features
        TEXT registry_json
    }
    alphas {
        TEXT alpha_id PK
        TEXT asset_class
        TEXT family
        TEXT horizon
        TEXT economic_rationale
        TEXT current_state
    }
    experiments {
        TEXT experiment_id PK
        TEXT alpha_id
        TEXT dataset_version
        TEXT feature_version
        TEXT model_version
        TEXT configuration_json
        BIGINT test_start_ts
        BIGINT test_end_ts
        BIGINT seed
        TEXT horizon
    }
    experiment_results {
        TEXT experiment_id PK
        TEXT alpha_id
        DOUBLE_PRECISION ic
        DOUBLE_PRECISION t_stat
        DOUBLE_PRECISION net_return_bps
        BIGINT leakage_passed
        TEXT verdict
        BIGINT n_experiments_in_ledger
        BIGINT created_ts
    }
    ledger_entries {
        TEXT ledger_key PK
        TEXT alpha_id
        TEXT kind
        TEXT config_json
        BIGINT count
        BIGINT n
        DOUBLE_PRECISION oos_ic
        DOUBLE_PRECISION nw_tstat
        TEXT verdict
    }
    lifecycle_transitions {
        TEXT alpha_id PK
        TEXT policy PK
        BIGINT event_ts PK
        TEXT from_state PK
        TEXT to_state PK
        TEXT source PK
        TEXT reason
        TEXT gates_json
        TEXT actor
    }
    decision_traces {
        TEXT trace_id PK
        TEXT session_id
        BIGINT instrument_id
        BIGINT event_ts
        BIGINT sequence
        TEXT data_version
        TEXT feature_version
        TEXT model_version
        TEXT config_version
        TEXT stages_json
    }
    alpha_signals {
        TEXT trace_id PK, FK
        BIGINT signal_index PK
        BIGINT timestamp
        BIGINT instrument_id
        DOUBLE_PRECISION expected_return
        DOUBLE_PRECISION confidence
        TEXT model_version
    }
    portfolio_targets {
        TEXT trace_id PK, FK
        TEXT strategy_id
        TEXT portfolio_version
        TEXT solver_status
        DOUBLE_PRECISION turnover
        TEXT targets_json
    }
    portfolio_legs {
        TEXT trace_id PK, FK
        BIGINT instrument_id PK
        BIGINT target_qty
        DOUBLE_PRECISION target_weight
    }
    risk_decisions {
        TEXT trace_id PK, FK
        BIGINT risk_index PK
        BIGINT order_id
        BIGINT decision
        TEXT rule_id
        BIGINT rule_index
        TEXT reason
    }
    parent_orders {
        BIGINT parent_order_id PK
        TEXT trace_id FK
        TEXT strategy_id
        TEXT alpha_id
        BIGINT instrument_id
        BIGINT side
        BIGINT qty
        TEXT algo
        DOUBLE_PRECISION urgency
        TEXT params_json
    }
    child_orders {
        BIGINT child_order_id PK
        TEXT trace_id FK
        BIGINT parent_order_id
        BIGINT venue_id
        BIGINT qty
        BIGINT price_ticks
        BIGINT order_type
        BIGINT slice_index
    }
    venue_decisions {
        BIGINT child_order_id PK
        TEXT trace_id FK
        BIGINT venue_id
        TEXT reason
        TEXT candidates_json
    }
    executions {
        BIGINT execution_id PK
        TEXT trace_id FK
        BIGINT order_id
        BIGINT parent_order_id
        BIGINT status
        BIGINT filled_qty
        BIGINT fill_price_ticks
        BIGINT venue_id
        DOUBLE_PRECISION fees
    }
    tca_results {
        BIGINT parent_order_id PK
        TEXT trace_id FK
        DOUBLE_PRECISION implementation_shortfall_bps
        DOUBLE_PRECISION spread_cost_bps
        DOUBLE_PRECISION impact_bps
        DOUBLE_PRECISION fees_bps
        DOUBLE_PRECISION timing_cost_bps
        TEXT venue_contribution_json
        TEXT algo
        BIGINT latency_p99_ns
    }
    attribution {
        TEXT trace_id PK, FK
        BIGINT parent_order_id
        DOUBLE_PRECISION alpha_bps
        DOUBLE_PRECISION spread_bps
        DOUBLE_PRECISION impact_bps
        DOUBLE_PRECISION fees_bps
        DOUBLE_PRECISION timing_bps
        DOUBLE_PRECISION total_bps
    }
    tca_orders {
        BIGINT order_id PK
        BIGINT instrument_id
        TEXT side
        DOUBLE_PRECISION total_is_bps
        DOUBLE_PRECISION delay_bps
        DOUBLE_PRECISION trading_bps
        DOUBLE_PRECISION opportunity_bps
    }
    model_runs {
        TEXT run_id PK
        TEXT name
        TEXT model_version
        TEXT data_version
        TEXT feature_version
        TEXT git_commit
        TEXT manifest_json
        TEXT metrics_json
        DOUBLE_PRECISION mean_ic
    }
    drift_baselines {
        TEXT name PK
        TEXT alpha_id
        TEXT kind
        TEXT feature_version
        TEXT edges_json
        TEXT expected_frac_json
        DOUBLE_PRECISION ic_mean
    }

    decision_traces ||--o{ alpha_signals : "trace_id"
    decision_traces ||--o| portfolio_targets : "trace_id"
    decision_traces ||--o{ portfolio_legs : "trace_id"
    decision_traces ||--o{ risk_decisions : "trace_id"
    decision_traces ||--o{ parent_orders : "trace_id"
    decision_traces ||--o{ child_orders : "trace_id"
    decision_traces ||--o{ venue_decisions : "trace_id"
    decision_traces ||--o{ executions : "trace_id"
    decision_traces ||--o{ tca_results : "trace_id"
    decision_traces ||--o| attribution : "trace_id"

    parent_orders ||..o{ child_orders : "parent_order_id"
    child_orders ||..o| venue_decisions : "child_order_id"
    child_orders ||..o{ executions : "order_id"
    parent_orders ||..o| tca_results : "parent_order_id"
    parent_orders ||..o{ risk_decisions : "order_id"
    sessions ||..o{ decision_traces : "session_id"
    instruments ||..o{ decision_traces : "instrument_id"
    venues ||..o{ child_orders : "venue_id"
    venues ||..o{ executions : "venue_id"
    alphas ||..o{ parent_orders : "alpha_id"
    alphas ||..o{ experiments : "alpha_id"
    alphas ||..o{ ledger_entries : "alpha_id"
    alphas ||..o{ lifecycle_transitions : "alpha_id"
    experiments ||..o| experiment_results : "experiment_id"
    feature_versions ||..o{ experiments : "feature_version"
    feature_versions ||..o{ model_runs : "feature_version"
    feature_versions ||..o{ drift_baselines : "feature_version"
    instruments ||..o{ tca_orders : "instrument_id"
```

## 8. Where to go deeper

| topic | document |
|---|---|
| Full architecture narrative, per-language engineering notes | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Governing institutional specification (verbatim) | [SPECIFICATION.md](SPECIFICATION.md) |
| Teaching walkthrough of every subsystem | [../LEARN.md](../LEARN.md) |
| 24 runnable recipes | [../COOKBOOK.md](../COOKBOOK.md) |
| Data model, views, SQLite/PostgreSQL portability, query cookbook | [DATA_MODEL.md](DATA_MODEL.md) |
| Six research papers from the platform's own numbers | [papers/INDEX.md](papers/INDEX.md) |
| Benchmark methodology + results | [../benchmarks/RESULTS.md](../benchmarks/RESULTS.md) |
