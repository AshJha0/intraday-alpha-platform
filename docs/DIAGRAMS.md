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
    RAW --> NORM["Normalization + sequence validation<br/>python iap.marketdata.normalize<br/>gaps / dups / out-of-order / invalid counted -> qc_report.json"]
    NORM --> CANON["Canonical event stream<br/>JSONL + IAP1 binary (72-byte LE records)<br/>+ Parquet research dataset — schemas x-version 1"]
    CANON --> BOOK["Order-book reconstruction<br/>Python ref / C++ / Rust / Java<br/>MBO FIFO, marketable ADDs, dup-drop, gap->stale, SNAPSHOT recovery"]
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
printed by `tests/harness/run_all.sh` (python 443 · cpp 175 · rust 181 · java 291
tests; 45/37/36/13 in the golden groups).

```mermaid
flowchart LR
    subgraph REF["Python reference (validated first)"]
        MG["python/tools/make_golden.py<br/>make_golden_features.py<br/>make_golden_alpha.py"]
        BF["independent brute-force checks<br/>naive book, brute-force features,<br/>SLSQP portfolio optimum"]
        MG <--> BF
    end
    MG --> GV[("tests/golden/<br/>events_eq_mbo.jsonl (2,000 ev)<br/>events_fx_quote.jsonl (800 ev)<br/>+ splitmix64.json")]
    MG --> EXP[("expected_*.json<br/>codec sha256 | book states | features<br/>alpha | backtest | risk decisions<br/>replay fills | portfolio | tca")]
    CPPTOOL["cpp/tools/make_replay_fills_golden<br/>(C++ is the fills reference)"] --> EXP
    GV --> PY["python: pytest -k golden<br/>45 tests"]
    GV --> CPP["cpp: ctest -R Golden<br/>37 tests"]
    GV --> RS["rust: 6 golden test targets<br/>36 tests"]
    GV --> JV["java: *GoldenTest (JUnitCore)<br/>13 golden-group tests"]
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
    PT->>MX: bind /metrics /health /status

    loop every MarketEvent (event-time order)
        PT->>BK: apply(event) — sequence check, book update
        BK-->>BK: feature refresh (validity bitset)
        BK-->>AL: FeatureVector
        AL-->>PF: AlphaSignal {expected_return, confidence}
        PF-->>RK: OrderRequest (target position delta)
        alt risk ALLOW
            RK-->>EX: forward child order
            EX-->>PT: Fill(s) {price_ticks, qty, fee, impact}
            PT->>PT: position / P&L accounting
        else risk REJECT
            RK-->>PT: RiskEvent {rule_id, severity, decision}
        end
        PT->>MX: update counters + latency histograms
    end

    PR->>MX: GET /metrics (scrape, 15s interval)
    Op->>MX: curl /health -> {"status":"ok"}
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
        ccore["codec · book · replay<br/>3.5 / 17.4 ns per event"]
        cfeat["48-feature native engine ≈450 ns"]
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
    OR["OrderRequest"] --> KG{"kill switches?<br/>global > strategy > instrument > venue"}
    KG -- engaged --> REJ["REJECT + RiskEvent<br/>(rule_id, severity, audit JSONL)"]
    KG -- clear --> MAL{"malformed / unknown instrument?"}
    MAL -- yes --> REJ
    MAL -- no --> DUP{"duplicate order_id?"}
    DUP -- yes --> REJ
    DUP -- no --> FF{"fat-finger qty / notional?"}
    FF -- breach --> REJ
    FF -- ok --> PB{"price band vs last mid?<br/>stale price age? sequence gap?<br/>venue disconnected?"}
    PB -- breach --> REJ
    PB -- ok --> SM{"self-match vs own resting orders?<br/>(conservative for unpriced MARKET)"}
    SM -- would cross --> REJ
    SM -- ok --> RT{"order-rate token bucket<br/>(event-time refill)"}
    RT -- exhausted --> REJ
    RT -- ok --> LIM{"worst-case position / instrument /<br/>gross / net notional projections"}
    LIM -- breach --> REJ
    LIM -- ok --> PNL{"daily / strategy loss limits<br/>(avg-cost realized PnL, latched)"}
    PNL -- breached --> KILL["engage kill switch +<br/>REJECT"]
    PNL -- ok --> ALLOW["ALLOW -> execution"]
    ALLOW --> FILLS["fills feed back:<br/>positions, PnL, resting-order set"]
    FILLS -.-> SM
    FILLS -.-> LIM
    FILLS -.-> PNL
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
```

## 7. Where to go deeper

| topic | document |
|---|---|
| Full architecture narrative, per-language engineering notes | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Governing institutional specification (verbatim) | [SPECIFICATION.md](SPECIFICATION.md) |
| Teaching walkthrough of every subsystem | [../LEARN.md](../LEARN.md) |
| 17 runnable recipes | [../COOKBOOK.md](../COOKBOOK.md) |
| Six research papers from the platform's own numbers | [papers/INDEX.md](papers/INDEX.md) |
| Benchmark methodology + results | [../benchmarks/RESULTS.md](../benchmarks/RESULTS.md) |
