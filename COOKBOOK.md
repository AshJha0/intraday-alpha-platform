# COOKBOOK — task-oriented recipes

Every recipe below is runnable from a fresh checkout of this repository in
the standard environment (Python 3.11 + pyarrow, g++ 13/CMake, Rust 1.98.1,
Java 21; see [docs/BUILD_NOTES.md](docs/BUILD_NOTES.md)). Paths are relative
to the repo root unless a recipe says otherwise. Recipes that read
`data/features/` need recipes 1 and 3 to have run first (the repo ships with
their outputs already generated).

Contents:

1. [Regenerate the dataset from scratch](#1-regenerate-the-dataset-from-scratch)
2. [Replay and inspect an order book (all four languages)](#2-replay-and-inspect-an-order-book-all-four-languages)
3. [Compute features (and inspect one instrument)](#3-compute-features-and-inspect-one-instrument)
4. [Run one alpha's full validation](#4-run-one-alphas-full-validation)
5. [Run the full 24-alpha promotion report](#5-run-the-full-24-alpha-promotion-report)
6. [Train the ML zoo with tracked manifests](#6-train-the-ml-zoo-with-tracked-manifests)
7. [Run the meta-label gate and read the calibration](#7-run-the-meta-label-gate-and-read-the-calibration)
8. [Optimize a portfolio with the golden problem](#8-optimize-a-portfolio-with-the-golden-problem)
9. [Run risk checks against the golden decisions](#9-run-risk-checks-against-the-golden-decisions)
10. [Run the execution simulator and the fills golden](#10-run-the-execution-simulator-and-the-fills-golden)
11. [Run a backtest end-to-end](#11-run-a-backtest-end-to-end)
12. [Run paper trading and scrape /metrics](#12-run-paper-trading-and-scrape-metrics)
13. [Run the benchmarks](#13-run-the-benchmarks)
14. [Verify cross-language parity in one command](#14-verify-cross-language-parity-in-one-command)
15. [Generate the TCA report](#15-generate-the-tca-report)
16. [Add a NEW alpha (full walkthrough)](#16-add-a-new-alpha-full-walkthrough)
17. [Add a new feature to the registry](#17-add-a-new-feature-to-the-registry)
18. [Run the adaptive policy comparison (drift → refit → retire)](#18-run-the-adaptive-policy-comparison-drift--refit--retire)
19. [Watch live drift in paper trading](#19-watch-live-drift-in-paper-trading)
20. [Build the platform store and query it](#20-build-the-platform-store-and-query-it)
21. [Explain an order (the decision trace)](#21-explain-an-order-the-decision-trace)
22. [Run the MVP loop (one command, one instrument, fully traced)](#22-run-the-mvp-loop-one-command-one-instrument-fully-traced)
23. [Replay an incident from the captured stream](#23-replay-an-incident-from-the-captured-stream)
24. [Explain an MVP order](#24-explain-an-mvp-order)
25. [Bootstrap and inspect the alpha promotion lifecycle](#25-bootstrap-and-inspect-the-alpha-promotion-lifecycle)
26. [Run one alpha as a contract-driven experiment](#26-run-one-alpha-as-a-contract-driven-experiment)
27. [Run the planted-signal power study on a tiny grid](#27-run-the-planted-signal-power-study-on-a-tiny-grid)
28. [Backtest with the cost-aware position policy and compare with the default](#28-backtest-with-the-cost-aware-position-policy-and-compare-with-the-default)
29. [Judge an experiment against the ledger-derived t threshold](#29-judge-an-experiment-against-the-ledger-derived-t-threshold)
30. [Run the recompute leakage probe](#30-run-the-recompute-leakage-probe)
31. [Per-fold diagnostics with a bootstrap interval](#31-per-fold-diagnostics-with-a-bootstrap-interval)
32. [Read experiments as JSON, and get machine-readable errors](#32-read-experiments-as-json-and-get-machine-readable-errors)
33. [Query the store read-only, and see what a refused statement looks like](#33-query-the-store-read-only-and-see-what-a-refused-statement-looks-like)
34. [Replay the risk edge golden in Python](#34-replay-the-risk-edge-golden-in-python)
35. [Check whether a result is gate-eligible](#35-check-whether-a-result-is-gate-eligible)
36. [Ingest an ITCH 5.0 file and run an alpha on it (no real data needed)](#36-ingest-an-itch-50-file-and-run-an-alpha-on-it-no-real-data-needed)

Recipes 27–35 were added with v1.3.0. Every command block in them was run
as printed, from a clean checkout of the release, before it was written
down; the quoted outputs are what those runs printed. Recipes that write
anything write under `data/store/` (git-ignored), never into the committed
ledger or `research/experiments/`.

Recipe 36 was added with the real-data ingestion path ([docs/REAL_DATA.md](docs/REAL_DATA.md)). It writes under `data/vendor/` and
`data/real/` (both git-ignored); its block was run as printed.

v1.4.0 (2026-10-03) regenerated the seeded dataset (`data_version`
`116b7787…`, was `203c8f54…`; recipe 1). Every quoted output below that
depends on the dataset was re-run on the new dataset, or re-read from the
regenerated artefact where running the command would rewrite a committed
file. Two exceptions, both Java, are marked in recipes 12 and 19.

---

## 1. Regenerate the dataset from scratch

The whole pipeline — seeded generator → `data/raw/*.jsonl` → normalization
(QC, event-time reorder) → `data/normalized/*.{jsonl,iap1}` + Parquet + QC
report:

```bash
cd python
PYTHONPATH=src python3 -m iap.marketdata            # configs/marketdata/generator.json, seed 20260829
PYTHONPATH=src python3 -m iap.marketdata --seed 42  # explicit seed override
```

Identical seed ⇒ bit-identical output files. Check the QC audit afterward:

```bash
python3 -m json.tool ../data/normalized/qc_report.json | head -30
```

Expect (seed 20260829): 309,598 events in, 308,975 out; 262 gaps, 450
duplicates, 128 out-of-order, 0 sequence resets, 0 in-stream timestamp
regressions, 173 invalid events counted per stream (`qc_report.json`
x-version 2). Generated data is never committed (BUILD_NOTES.md). The
normalized `.iap1` files carry the IAP1 v2 CRC-32 trailer; older v1 files
still load (unverified).

**The v1.4.0 generator.** `configs/marketdata/generator.json` (x-version 2)
sets `equities.flow.calibration = "session"`, the default since v1.4.0.
Equity flow is self-exciting: the multiplier `(1 + excitation)` shortens
every inter-arrival time. The `"session"` rule divides that out — the base
rate is `slots_per_stream × excitation_time_factor / duration`, where
`excitation_time_factor` = E[1 / (1 + excitation)] (0.522 for the pinned
`flow` and `mix`) — and has no slot budget, so `slots_per_stream` is the
expected number of flow slots per stream and session and the flow runs to
the close. Up to v1.3.0 the slots were a hard budget at an uncalibrated
rate, and every equity stream went quiet 38–43% of the way through the
session. To reproduce that dataset byte for byte (`data_version`
`203c8f54…`, 310,159 normalized events), generate from a copy of the config
with the legacy rule:

```bash
python3 - <<'EOF'
import json
cfg = json.load(open("../configs/marketdata/generator.json"))
cfg["equities"]["flow"]["calibration"] = "legacy_budget"
json.dump(cfg, open("/tmp/generator_legacy.json", "w"), indent=2)
EOF
PYTHONPATH=src python3 -m iap.marketdata --config /tmp/generator_legacy.json --out /tmp/data-v1.3.0
```

`equities.fill_session: true` (the v1.3.0 opt-in) belongs to
`"legacy_budget"` only; with the default calibration it is a configuration
error. `tests/replay/test_generator_determinism.py` pins the raw-file hashes
of both datasets. The FX files are byte-identical under either rule.

**After a dataset change, regenerate everything derived from it in one
pass.** The alpha reports, the ledger, the runner experiments, the lifecycle
registry, the ML and adaptive reports, the power study and four goldens are
all computed from this dataset, and they must not be regenerated piecemeal:

```bash
python3 tools/regenerate_dataset_artifacts.py --list     # the steps, in dependency order
python3 tools/regenerate_dataset_artifacts.py            # the whole chain; rewrites research/, tests/golden/, alpha_params.json and its ConfigMap
```

It refuses to run twice on one dataset unless `--allow-rerun` is given (a
second pass would be ledgered as reruns and append a second set of model
runs). The committed v1.4.0 artefacts were produced by the manual
`regenerate` job of `.github/workflows/ci.yml`; a run on another platform
reproduces them to the documented tolerances, not to the byte
(docs/governance/REPRODUCIBILITY.md). Recipes 5, 6, 18 and 27 are the
individual steps; use them one at a time only for a deliberate,
single-report change.

## 2. Replay and inspect an order book (all four languages)

**Python** — the reference `ReplayEngine` over the golden equity vector:

```bash
cd python && PYTHONPATH=src python3 - <<'EOF'
from iap.core.codec import read_jsonl
from iap.replay.replay import ReplayEngine

events = read_jsonl("../tests/golden/events_eq_mbo.jsonl")
eng = ReplayEngine(snapshot_every=0)
for ev in events:
    eng.apply(ev)
book = eng.book_states()["instruments"]["1"]["1"]   # instrument 1, venue 1
print("best bid:", book["best_bid_ticks"], "x", book["best_bid_size"])
print("best ask:", book["best_ask_ticks"], "x", book["best_ask_size"])
print("depth bid top5:", book["depth_bid_top5"])
print("trade_flow:", book["trade_flow"], "sequence:", book["sequence"])
# after 2000 events: bid 2425 x 800, ask 2429 x 1500, flow -2700 — matching
# tests/golden/expected_book_states.json exactly
EOF
```

**Rust** — the replay demo binary (decoder thread → bounded SPSC event bus
→ engine; book summary + throughput):

```bash
cd rust && cargo run --release --bin demo          # defaults to tests/golden/
```

**Feed anomalies, reorder windows, resets, checkpoints across languages** —
the anomaly goldens are the executable guide (see `docs/SCENARIOS.md`):

```bash
cd python && PYTHONPATH=src python3 - <<'PY'
import json
from iap.core.codec import read_jsonl
from iap.orderbook.book import ConsolidatedBook
from iap.replay.replay import ReplayEngine

events = read_jsonl("../tests/golden/events_eq_anomalies.jsonl")
for window in (0, 4):                      # hold-back buffer for late retransmissions
    cons = ConsolidatedBook(1, reorder_window=window)
    for ev in events:
        cons.apply(ev)
    for vid, b in sorted(cons.books.items()):
        print(window, vid, "stale" if b.stale else "ok", b.counters())
    print(window, cons.consolidated_summary())   # non-stale venues only
    cons.reset_sequences()                       # explicit venue sequence reset (session roll)

eng = ReplayEngine(reorder_window=4, keep_snapshots=2)
eng.run(events)
cp = eng.checkpoint()                            # x-version 2 JSON, loadable by C++/Rust/Java
print(json.dumps(cp)[:200])
PY
```

**Java** — the replay demo (golden vectors, codec SHA-256s, throughput):

```bash
bash java/demo.sh
```

**C++** — book/replay correctness runs as tests; throughput via the bench:

```bash
cd cpp && bash build.sh
ctest --test-dir build --output-on-failure -R 'Book|Replay'
./build/bench_all           # includes book-update and replay-engine passes
```

## 3. Compute features (and inspect one instrument)

Replay the normalized data through the 205-feature engine, writing one
Parquet per instrument plus per-horizon labels and a summary:

```bash
cd python
PYTHONPATH=src python3 -m iap.features --cadence-ms 100
# narrower run: one day's file only
PYTHONPATH=src python3 -m iap.features --files eq_20260824.normalized.iap1
```

Inspect instrument 1 (columns = registry features, NaN where invalid, plus
`label_mid_<h>` / `label_cost_<h>` / `label_valid_<h>`):

```bash
PYTHONPATH=src python3 - <<'EOF'
import pandas as pd
df = pd.read_parquet("../data/features/features_1.parquet")
print(df.shape)
print(df[["exchange_ts", "mid_price_v1", "microprice_v1",
          "ofi_l5_w1s_v1", "imbalance_l1_v1", "label_mid_5s"]].tail())
print("valid fraction:", df["mid_price_v1"].notna().mean().round(4))
EOF
```

The bundled run: 308,975 events → 213,021 vectors, registry hash
`585dd7b9…` recorded in `data/features/features_summary.json`.

## 4. Run one alpha's full validation

The same gauntlet `run_all.py` applies — walk-forward + purge/embargo,
leakage tests, decay curve, cost/latency/regime stress, verdict — for a
single alpha (EQ03 here):

```bash
PYTHONPATH=python/src python3 - <<'EOF'
import json
from iap.alpha import build
from iap.alpha.data import load_features
from iap.backtest import Backtester, BacktestConfig, CostModel
from iap.validation import validate_alpha

meta = {int(r["instrument_id"]): {"symbol": r["symbol"],
        "asset_class": r["asset_class"], "tick_size": float(r["tick_size"]),
        "lot_size": int(r["lot_size"]), "adv": float(r["adv"]),
        "ref_price": float(r.get("ref_price", 1.0))}
        for r in json.load(open("configs/instruments/instruments.json"))["instruments"]}
frames = load_features("data/features")
exec_cfg = json.load(open("configs/execution/execution.json"))
bt = Backtester(CostModel.load("configs/execution/execution.json"), meta, BacktestConfig())

rep = validate_alpha(lambda: build("EQ03"), frames, bt, meta,
                     float(exec_cfg["defaults"]["max_participation"]))
print({k: rep[k] for k in ("oos_ic", "oos_rank_ic", "nw_tstat",
                           "oos_hit_rate", "fold_sign_consistency",
                           "net_pnl_1x_cost", "verdict")})
# EQ03 -> oos_ic 0.0190, nw_tstat 5.86, fold_sign_consistency 1.0,
#         net_pnl_1x_cost -161585.48, verdict ITERATE
EOF
```

Compare against the committed `research/alpha_reports/EQ03.json`: the run
is deterministic, and the walk-forward statistics (IC, rank IC, t, hit rate,
fold consistency, verdict) match it. (5.86 is `nw_tstat`, over all rows;
REPORT.md prints the uncrossed-row t the gate reads, 5.78.) The net P&L does
not (the report says −148,562): the report's backtester runs under the
pinned research execution
model — `BacktestConfig(latency_ns=1 s, max_decision_age_ns=60 s,
flatten_at_session_end=True)` — and this demo uses the default
`BacktestConfig()`. Both are negative.

## 5. Run the full 24-alpha promotion report

```bash
PYTHONPATH=python/src python3 research/alpha_reports/run_all.py    # ~20 s
```

Outputs: `research/alpha_reports/REPORT.md` (the honest master table —
0 PROMOTE / 10 ITERATE / 14 REJECT on the bundled data), one JSON of full
evidence per alpha, the appending multiple-testing ledger
`research/experiments.json`, and refreshed day-1-fitted
`configs/strategies/alpha_params.json` (the port contract input —
regenerating goldens after a deliberate change is recipe territory for
API_ALPHA.md §6). The ledger is scoped by dataset since v1.4.0: on a
dataset the ledger has already seen, a rerun is recorded as a rerun and
adds no looks; on a new dataset it adds 24 × 28 = 672. After a dataset
change run the whole chain instead (recipe 1).

## 6. Train the ML zoo with tracked manifests

```bash
python3 research/ml_reports/run_ml.py        # < 5 min budget; ~1 min typical
```

This runs the gated comparison (linear baselines always; XGBoost/LightGBM/MLP
only if the best linear pooled OOS IC against the mid-to-mid label is
positive — on the bundled data it is +0.0081 for ridge, so the gate passes
and all three are fitted) and writes `research/ml_reports/ML_REPORT.md`.
Passing the gate is not a result: the mid-label IC of every fitted model is
between −0.0024 and +0.0081, and the conservative net is negative for all
six (−0.110 to −2.612 bps per signal). On the v1.3.0 dataset the same gate
failed (ridge −0.0430) and the trees and the MLP were never fitted. Every
fit is a tracked run — 40 so far; runs 0034–0040 are the v1.4.0 pass:

```bash
python3 -m json.tool research/models/ledger.json | head
python3 -m json.tool research/models/run_0037_xgboost/manifest.json
```

The manifest pins experiment id, git commit, data version (QC-report hash),
feature version (registry hash), model version, hyperparameters, train/test
windows and hardware — the spec §14 reproducibility contract. Before
trusting any IC in the report, read its "Honest read of these numbers"
section (crossed-book artifact; see LEARN.md §7.2).

## 7. Run the meta-label gate and read the calibration

The meta-labeling stage runs inside `run_ml.py` (recipe 6) on the best
primary model's pooled OOS predictions. To study its output:

```bash
grep -A 8 "Meta-labeling" research/ml_reports/ML_REPORT.md
python3 - <<'EOF'
import json
cc = json.load(open("research/ml_reports/calibration_curve.json"))
print("primary:", cc["primary_model"], "AUC:", cc["auc_test"],
      "Brier:", cc["brier_test"], "base rate:", cc["base_rate_test"])
for row in cc["curve"]:            # predicted-prob bin vs observed frequency
    print(row)
EOF
```

Committed result: primary `ridge`, AUC 0.655, Brier 0.0568, test base
rate of profitable signals 0.062; both τ = 0.5 and the calibration-chosen
best τ = 0.300 keep **zero** of the 4,100 test signals. The report flags
this `gate_degenerate: true` and does not present it as an economic
decision — a gate that never fires is no evidence either way (the gate-off
row shows −746.7 total net bps for the ungated primary). Calibration here
is Platt scaling, not isotonic: the calibration segment has 255 positives,
below the pinned isotonic minimum of 500. Thresholds are chosen on the
calibration segment, economically (LEARN.md §7.3). The latest meta-label
run directory is `research/models/run_0040_metalabel_ridge/`.

## 8. Optimize a portfolio with the golden problem

Solve the pinned 8-FX-pair problem (all seven constraint families active)
with the reference optimizer and check it against the golden:

```bash
cd python && PYTHONPATH=src python3 - <<'EOF'
import json, numpy as np
from iap.portfolio.optimizer import Constraints, solve

g = json.load(open("../tests/golden/expected_portfolio.json"))
p, c = g["problem"], g["problem"]["constraints"]
cons = Constraints(
    w_min=np.array(c["w_min"]), w_max=np.array(c["w_max"]),
    gross_cap=c["gross_cap"], net_cap=c["net_cap"],
    participation=np.array(c["participation"]),
    turnover_cap=c["turnover_cap"], vol_target=c["vol_target"],
    currency_matrix=np.array(p["currency_matrix"]),
    currency_bounds=np.array(c["currency_bounds"]))
res = solve(np.array(p["alpha"]), np.array(p["sigma"]), np.array(p["w_prev"]),
            p["risk_aversion"], np.array(p["tc_linear"]), cons, **p["solver"])
want = np.array(g["expected"]["weights"])
print("weights:", np.round(res.weights, 6))
print("max |diff| vs golden:", np.max(np.abs(res.weights - want)))
print("objective:", res.objective, "best_iteration:", res.best_iteration)  # 1500
EOF
```

Cross-language versions of the same check:

```bash
cd python && PYTHONPATH=src python3 -m pytest -q tests/test_portfolio_golden.py
cd java && bash build.sh && bash test.sh          # includes PortfolioGoldenTest
```

## 9. Run risk checks against the golden decisions

The Rust engine is the reference; Java and Python (`iap.risk`,
[API_TRADING.md](API_TRADING.md) §1) port it. The golden replays a full
step sequence (orders, fills, market moves, gaps, venue outages, kill
switches, loss-limit overrides, session rolls, a mid-stream snapshot) and
pins every decision, deciding rule and severity, the notification events,
the audit log byte-for-byte (`expected_risk_audit.jsonl`) and the state
snapshot (`expected_risk_snapshot.json`, restored by Java):

```bash
cd rust && cargo test -p risk --test golden_risk     # reference
cd rust && cargo test -p risk --test rules           # per-rule unit tests
cd java && bash build.sh && rm -rf out/test && mkdir -p out/test && \
  find src/test/java -name '*.java' | sort > out/test-sources.txt && \
  javac -cp "out/main:/usr/share/java/junit4.jar:/usr/share/java/hamcrest-core.jar" \
        -d out/test @out/test-sources.txt && \
  java -cp "out/test:out/main:/usr/share/java/junit4.jar:/usr/share/java/hamcrest-core.jar" \
       org.junit.runner.JUnitCore com.iap.RiskGoldenTest
cd python && PYTHONPATH=src python3 -m pytest -q tests/test_risk_golden.py tests/test_risk_rules.py   # Python port: 7 golden + 79 rule tests
```

To understand a decision, read the step in
`tests/golden/expected_risk_decisions.json` — the deciding `rule_id` is the
first failing rule in the engine's pinned check order
(`PLATFORM_CONVENTIONS.md` §11.1), limits come from `configs/risk/risk.json`
(x-version 3, incl. the `currency` conversion table) and per-instrument
reference data (`tick_size`, `qty_unit`, `quote_ccy`) from
`configs/instruments/instruments.json`. Regenerate deliberately only:

```bash
cd rust && cargo run -p risk --bin make_risk_golden -- ../tests/golden --force
```

Kill-switch operations in production — including the re-arm order
(`override_loss_limit` → `clear_kill`, `roll_session` keeps kills) — follow
`docs/runbooks/RUNBOOK_incident_kill_switch.md`; the scenario tests
(`rust/risk/tests/rules.rs`, `RiskScenarioTest`) are listed per scenario in
`docs/SCENARIOS.md`.

## 10. Run the execution simulator and the fills golden

C++ owns the pinned fill semantics (`cpp/include/iap/execution/execution.hpp`
— latency, aggressive depth walk, liquidity-consumption overlay,
deterministic queue position, cancel latency and expiry, venue trading-state
gate, fees/impact); the golden (v2) pins a passive VWAP parent and an
aggressive IS parent on the golden equity vector:

```bash
cd cpp && bash build.sh
ctest --test-dir build --output-on-failure -R 'ReplayFillsGolden|Exec'
```

Java and Python must reproduce the same fills to the tick (Python's golden
test additionally asserts every money field bit-identical —
[API_TRADING.md](API_TRADING.md) §2):

```bash
cd java && bash build.sh && bash test.sh    # includes ReplayFillsGoldenTest, ExecutionSimTest, AlgosTest
cd python && PYTHONPATH=src python3 -m pytest -q tests/test_execution_golden.py tests/test_execution_rules.py tests/test_exec_algos.py tests/test_sor.py
```

Read the expected economics (patience vs urgency — the passive parent earns
−$0.658 in rebates on 329 of 400 shares and leaves 71 unfilled at `end_ts`;
the aggressive one completes 600 paying $1.80 + impact):

```bash
python3 -m json.tool tests/golden/expected_replay_fills.json | head -30
```

Regeneration (deliberate changes only, per conventions §5):
`cpp/tools/make_replay_fills_golden.cpp` builds as
`cpp/build/make_replay_fills_golden`.

## 11. Run a backtest end-to-end

Score the bundled feature frames with the fitted production parameters and
run the research backtester (decision at row i, execution at row i+latency,
spread + fee + impact costs, exact accounting identity):

```bash
PYTHONPATH=python/src python3 - <<'EOF'
import json
from iap.alpha import load_params_file
from iap.alpha.data import load_features
from iap.backtest import Backtester, BacktestConfig, CostModel

meta = {int(r["instrument_id"]): {"symbol": r["symbol"],
        "asset_class": r["asset_class"], "tick_size": float(r["tick_size"]),
        "lot_size": int(r["lot_size"]), "adv": float(r["adv"]),
        "ref_price": float(r.get("ref_price", 1.0))}
        for r in json.load(open("configs/instruments/instruments.json"))["instruments"]}
frames = load_features("data/features")
models = load_params_file("configs/strategies/alpha_params.json")

bt = Backtester(CostModel.load("configs/execution/execution.json"), meta, BacktestConfig())
m = models["EQ01"]
scores = m.score({i: frames[i] for i in m.universe(list(frames))})
res = bt.run(frames=frames, scores=scores, asset_class="EQUITY")
mx = res.metrics(capital=1_000_000)
print({k: round(v, 2) if isinstance(v, float) else v
       for k, v in mx.items()
       if k in ("total_pnl", "gross_pnl", "total_costs", "trade_count",
                "sharpe_ann", "max_drawdown")})
EOF
```

The identity `total_pnl = gross_pnl − total_costs` holds exactly, in USD:
for FX instruments add `base_currency` / `quote_currency` to the meta (as
`research/alpha_reports/run_all.py` does) and include the conversion pairs
in `frames` — every P&L increment is converted at the prevailing pair mid
(`InstrumentResult.total_pnl_native` keeps the quote-currency figure) and a
non-USD increment with no prevailing rate raises rather than being summed
as dollars (conventions §11.6). Note this demo scores the *full 2-day* frame — including the day the parameters were
fitted on — so it will not match the REPORT.md day-2 OOS table (net −93,699
for EQ01), which splits by day with `iap.alpha.data.split_by_day` first. The
pinned EQ01 backtest on the golden frame is golden-tested cross-language
(`tests/golden/expected_backtest.json`; C++/Java golden groups). Full
procedure and troubleshooting: `docs/runbooks/RUNBOOK_backtest.md`.

## 12. Run paper trading and scrape /metrics

The Java platform streams events through book → features → alphas →
portfolio → risk → execution with the monitoring endpoints live:

```bash
bash java/paper.sh                              # asap replay of the golden EQ vector
# prints e.g.:
# paper session: events=2000 orders=420 fills=421 pnl=-100.801250 \
#   risk[allowed=420 rejected=5] status=FINISHED port=8080 \
#   state=out/state report=out/paper_session_report.json
```

(That summary line was printed by the v1.3.0 release. The golden equity
vector it replays is unchanged in v1.4.0, but the session scores EQ01 with
`configs/strategies/alpha_params.json`, which was refitted on the
regenerated dataset, so the order, fill and P&L figures may differ. The
line has not been re-captured for v1.4.0: the Java toolchain was not
available where this recipe was re-checked.)

A session is FINITE: it ends with `status=FINISHED` and exit 0 after the last
event (PLATFORM_CONVENTIONS.md §12.3). It leaves its durable state behind in
`out/state/` — `risk_snapshot.json`, `session_state.json`, `risk_audit.jsonl`
(every RiskEvent, byte-identical to `RiskEngine.auditJsonl()` and hashed in the
report), `config_audit.jsonl`, `admin_audit.jsonl`:

```bash
sha256sum java/out/state/risk_audit.jsonl
python3 -c "import json;print(json.load(open('java/out/paper_session_report.json'))['risk']['audit_sha256'])"
bash java/paper.sh --resume     # continue from the checkpoint (positions,
                                # realized P&L and any latched kill switch)
```

For a session you can scrape while it runs, pace it in real time (60×):

```bash
bash java/paper.sh --mode realtime --speed 60 &
sleep 5
curl -s localhost:8080/health              # {"status":"ok"}
curl -s localhost:8080/status | python3 -m json.tool
curl -s localhost:8080/metrics | grep -E '_total|latency' | head -20
# e.g. md_events_total, alpha_signals_total, risk_decisions_total /
# risk_rejected_total, portfolio gauges, latency histograms, GC metrics.
# (Gap/duplicate counters appear once a gap/dup is actually seen — the golden
# vector has none, and they are exported on EVERY event so a single gap is
# visible on the next scrape. Execution flow comes from the PLATFORM's own
# counters: exec_orders_submitted_total, exec_fills_total,
# exec_child_orders_rejected_total, exec_slippage_bps — the rust venue
# counters were never written by any deployed service and their rules and
# panels were removed in round 3. The live adaptability gauges — drift PSI,
# rolling IC, lifecycle state — are scraped in recipe 19.)

curl -s localhost:8080/ready                # 503 when the feed is stale
curl -s localhost:8080/metrics | grep -E '^(platform_|md_event_time_gap|risk_limit|book_stale)'
```

Halt it the way the runbook does (needs a token, else the routes are 404):

```bash
IAP_ADMIN_TOKEN=dev-token bash java/paper.sh --mode realtime --speed 60 &
sleep 5
curl -sS -X POST localhost:8080/admin/kill \
  -H "Authorization: Bearer dev-token" \
  -d 'scope=global' --data-urlencode 'reason=COOKBOOK demo'
curl -s localhost:8080/metrics | grep '^risk_kill_switch_engaged'   # 1
tail -1 java/out/state/admin_audit.jsonl                            # audited
```

The port comes from `configs/execution/execution.json` `monitoring.port` (default
8080 — the Grafana/Prometheus contract; `deployment/prometheus/` scrapes it
in the docker-compose stack). The session report lands in
`java/out/paper_session_report.json`. Full ops procedure:
`docs/runbooks/RUNBOOK_paper_trading.md`. Note the honest P&L: paper
trading the golden vector *loses* money — consistent with the promotion
report, not an embarrassment to be tuned away.

## 13. Run the benchmarks

```bash
cd cpp && bash build.sh && ./build/bench_all                       # full C++ pass
./build/bench_all ../benchmarks/results_cpp.md                  # persist the table
cd rust && cargo run --release --bin demo                       # replay throughput
bash java/demo.sh                                                    # Java replay throughput
```

Methodology matters more than the numbers (spec §22): the committed
`benchmarks/results_cpp.md` states hardware (2-CPU Xeon container, no
pinning), compiler, flags, workload and the mean-only caveat next to every
figure (`benchmarks/RESULTS.md` is the cross-language index). Reference
points from the committed run (regenerated 2026-09-19): IAP1 decode
184.1 ns/event, book update 26.4 ns, replay 27.2M events/s, feature engine
514.1 ns/event, alpha scoring 33.6 ns/row; serialising one 5.6 KB decision
trace 31.7 µs/trace (+ 30.3 µs to hash), off the event loop. The table also
carries a **cold** reference — one single pass over a full generated session
— next to the hot rows, because every hot figure is cache-resident by
construction (see the caveat in `results_cpp.md`, which `bench_all` emits
itself so a regeneration cannot drop it), and a trace-path table.

The decode figure moved from 3.5 to ~180 ns/event in round 3 and that is
not a regression to fix: IAP1 v2 added a mandatory CRC-32 integrity
trailer, and a byte-at-a-time table CRC over the 144 KB body costs
~5 cycles/byte. Paying ~180 ns/event to detect a corrupted capture is the
right trade; quoting the pre-CRC number afterwards was not. Every document
that quotes the table is checked against it by
`tests/harness/check_headline_numbers.py`.

## 14. Verify cross-language parity in one command

```bash
bash tests/harness/run_all.sh                 # full suites + parity table
bash tests/harness/run_all.sh --golden-only   # golden groups only (fast)
```

Exit code 0 iff every language passed; logs land in a temp dir printed on
the first line. The v1.4.0 counts (CI, 2026-10-03): python 1573 / cpp 289 /
rust 323 / java 510 tests passed (golden groups 166/68/64/104), plus
`integration` (17) and `replay` (6) rows for the repo-level pytest suites, a
`deployment` row (25 structural checks passed in CI, where `promtool` and
`kubeconform` are installed; a machine without them reports those checks as
skipped) and a `numbers` row (every headline figure re-derived from its
artefact), all PASS.

## 15. Generate the TCA report

```bash
cd python && PYTHONPATH=src python3 -m iap.tca      # writes research/tca/TCA_REPORT.md
```

The report simulates a pinned 36-parent order set (SplitMix64 seed 20260829)
over the golden vectors and reports the full Perold decomposition, VWAP/TWAP
slippage, spread/impact/timing attribution and post-fill markouts (with the
number of fills whose markout is *defined* — a horizon past the end of the
timeline or across a halt is `null`, never the last mid). The exact §2.2
records and the timeline cases (crossed states skipped, MAKER fills against
the pre-event state, undefined markouts) are golden-tested
(`tests/golden/expected_tca.json` v2, regenerate with
`python/tools/make_golden_tca.py --force`) in Python and Java.

## 16. Add a NEW alpha (full walkthrough)

Say you want `EQ13`, a spread-revert alpha. The interface
(`python/src/iap/alpha/base.py`, documented in API_ALPHA.md) forces the
discipline:

**Step 1 — write the class** in `python/src/iap/alpha/equity.py`:

```python
class EQ13SpreadRevert(LinearAlpha):
    """Spread-reversion alpha.

    Economic rationale: an abnormally wide spread reflects transient
    liquidity withdrawal; as liquidity replenishes, the mid reverts toward
    the pre-widening level, so wide-spread states predict reversion of the
    latest mid move.
    """

    alpha_id = "EQ13"
    name = "spread reversion"
    asset_class = "EQUITY"
    horizon = "5s"                     # one of the 11 pinned label horizons
    features = ("spread_bps_v1", "ret_log_1s_v1")   # ALL inputs, declared

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        # oriented, causal, same-row registry features only
        return -col(df, "ret_log_1s_v1") * col(df, "spread_bps_v1")
```

The `Economic rationale:` section is **enforced at class-definition time** —
omit it and the import raises `TypeError`. The `features` tuple is enforced
too: the harness masks every undeclared column and requires identical
`score()` output (this is also the label-leakage guard).

**Step 2 — register it** in `ALPHA_CLASSES` in
`python/src/iap/alpha/__init__.py` (and import it there).

**Step 3 — validate it** with recipe 4 (`build("EQ13")`). You get the full
treatment automatically: 4 purged/embargoed walk-forward folds, shift-by-one
leakage test, decay curve, cost ×{0.5,1,2} and latency +{0,1,5} stress,
regime split — and a verdict under the pinned gates: PROMOTE needs leakage
pass, OOS IC ≥ 0.01, NW t ≥ 3.0, fold consistency ≥ 0.7, **hypothesis
confirmed** (the fitted sign agrees with your stated rationale), and net
P&L > 0 at 1× costs. If your fitted beta comes out opposite to your
rationale, the framework ships it as-is with `hypothesis_confirmed=false` —
you may not "fix" the sign (spec §32; ports inherit this rule verbatim).

**Step 4 — book the looks**: run through `run_all.py` (recipe 5) so the
experiment ledger counts your new looks; the multiple-testing yardstick in
the report moves accordingly.

**Step 5 — production, only if promoted** (spec §20): port the scorer
against `configs/strategies/alpha_params.json` (ports never fit —
API_ALPHA.md §2), add golden cases via
`python/tools/make_golden_alpha.py` + a `schemas/MIGRATIONS.md` entry, and
make the C++/Rust/Java golden groups reproduce your cases before any paper
deployment.

## 17. Add a new feature to the registry

Features live in family modules under `python/src/iap/features/`; the
registry pins their order and hashes their definitions.

**Step 1 — add the spec** to the family's `specs()` (e.g.
`volatility.py`), from pinned parameter grids — name it
`<stem>_<params>_v1` and write the `doc` and `depends_on` honestly.

**Step 2 — implement the update rule** in the family module/engine following
the pinned semantics of API_FEATURES.md §2: sample at the right refresh kind
(mid-change vs every book_ok refresh vs every refresh), half-open windows
`(t−w, t]`, integer sums for integer inputs, at-or-before history lookups,
and explicit validity (warmup, book_ok, defined inputs). **NaN must never
appear with valid=true** — `tests/test_feature_validity.py` will hold you to
it.

**Step 3 — regenerate the registry** (the pipeline writes it):

```bash
cd python && PYTHONPATH=src python3 -m iap.features
grep -m1 '"registry_hash"' ../data/reference/feature_registry.json
```

(The quotes matter: a bare `registry_hash` pattern first matches the
registry's *description* line, which mentions the field, not the hash.)

The registry hash — and therefore `FeatureVector.feature_version`
everywhere — **changes by design**: golden files, feature parquet output and
model manifests all pin it, so:

**Step 4 — regenerate goldens deliberately** with
`python/tools/make_golden_features.py` (it cross-validates against
brute-force recomputation before writing) and record the change in
`schemas/MIGRATIONS.md`. Run `python/tests/test_feature_engine.py` and the
brute-force comparison suite.

**Step 5 — parity**: if the feature belongs in the native 40 (an alpha input
for a production port), implement it in `cpp/src/features/`,
`rust/features/` and `java/.../features/` and re-run
`tests/harness/run_all.sh` — the feature golden groups in all four languages
must agree at 1e-9. Otherwise it remains a Python research feature until its
family is ported (API_FEATURES.md §1).

## 18. Run the adaptive policy comparison (drift → refit → retire)

Replay the bundled 2-session data as a deployment of 10 alphas (the 6
golden alphas + the 4 best remaining by walk-forward OOS IC) under four
refit policies — static, scheduled weekly, scheduled daily,
drift-triggered — with PSI/rolling-IC drift monitoring and the pinned
IC-gated lifecycle (`configs/strategies/strategies.json` `adaptive`):

```bash
PYTHONPATH=python/src python3 research/adaptive_reports/run_adaptive.py
# alpha subset: ['EQ01', 'EQ03', 'EQ06', 'FX01', 'FX05', 'FX09', 'FX08', 'FX10', 'FX11', 'FX06']
# EQ01: static:rf=1,pnl=-166750 ... drift_triggered:rf=1,pnl=-166750 (...s)
# ...
# Done in 27s -> research/adaptive_reports/ADAPTIVE_REPORT.md
```

(Those lines are read from the committed v1.4.0 report, which the
`regenerate` CI job wrote; the script was not re-run here because it
rewrites the report, the baselines and the ledger.) Takes ~30 s and writes `research/adaptive_reports/ADAPTIVE_REPORT.md`
(the honest comparison + the FX10 false-positive case), per-alpha
evidence JSONs alongside it, drift baselines to `research/baselines/`
(the same files the Java live monitor consumes), every lifecycle
transition to `research/lifecycle_log.jsonl`, and one ledger entry per
deployment (10 alphas x 4 policies = 40) to `research/experiments.json`.
Deterministic: a rerun reproduces every number except the runtime line.
Since round 3 the ledger is **de-duplicated by (alpha, kind, canonical
config)** — and, since v1.4.0, by dataset — so rerunning this script on the
same dataset bumps a `reruns` counter but does NOT inflate the Bonferroni
denominator — and one deployment counts as one
experiment rather than as its 211 monitoring evaluations. Read the report's
"READ THIS FIRST"
section before quoting any policy ranking: on two synthetic sessions
there isn't one. On the v1.4.0 dataset the drift-triggered policy refits
122 times in total (126 on the v1.3.0 dataset), FX01 is retired under every
policy, and all 40 deployments are net-negative. The FX rows differ from
the v1.3.0 report by about one trade per deployment although the FX data is
byte-identical: the v1.3.0 report had last been generated before the
2026-09-20 backtester corrections, so that difference is a code effect, not
a data effect. Contract: `/API_ADAPTIVE.md`; concepts: LEARN.md §14.

## 19. Watch live drift in paper trading

The Java platform ships the live half of the adaptability layer
(`com.iap.adaptive`): it loads the research baselines from
`research/baselines/` (override with `--baselines <dir>`) and exports
three per-alpha gauges alongside the usual metrics. Run a paced session
and scrape them:

```bash
bash java/paper.sh --mode realtime --speed 60 &
sleep 25                # drift PSI appears once its 256-signal window fills;
                        # rolling IC once >= 4 matured event-time buckets exist
curl -s localhost:8080/health               # {"status":"ok"}
curl -s localhost:8080/metrics | grep -E 'alpha_(live_vs_backtest_drift|rolling_ic|lifecycle_state)'
# alpha_lifecycle_state{alpha="EQ01"} 0
# alpha_live_vs_backtest_drift{alpha="EQ01"} 0.23972904275579857
# alpha_rolling_ic{alpha="EQ01"} 0.22802197463720542
```

(The two gauge values were captured on the v1.3.0 release. v1.4.0
regenerated both of their inputs — `research/baselines/signal_eq01.json`
and the EQ01 parameters — so expect different values; they have not been
re-captured, for the reason given in recipe 12.)

`alpha_live_vs_backtest_drift` is the pinned PSI (API_ADAPTIVE.md §2) of
a rolling 256-value window of the live EQ01 signal (recomputed every 32
confident signals) against `research/baselines/signal_eq01.json` — the
Java-parity baseline; `alpha_rolling_ic` is the mean of event-time bucket
ICs over the trailing window, matured (lookahead-free) rows only;
`alpha_lifecycle_state` encodes 0 ACTIVE / 1 WATCH / 2 RETIRED, driven by
the pinned IC hysteresis of `configs/strategies/strategies.json` `adaptive.lifecycle`.
Early in the session the drift and IC gauges are absent rather than zero —
until the PSI window fills, or with fewer than `min_ic_buckets` (4)
buckets, the monitors report no value, which is itself pinned behavior
(no data must never read as no drift). The `LiveVsBacktestDrift` alert
(`deployment/prometheus/alerts.yml`) fires on PSI > 0.25, and the
Trading & Risk Grafana dashboard plots all three. Monitoring is
observational only — it never alters a trading decision mid-session, so
the summary line stays bit-for-bit reproducible (recipe 12).

## 20. Build the platform store and query it

The store (`schemas/sql/iap_v1.sql`, [docs/DATA_MODEL.md](docs/DATA_MODEL.md))
is a derived, rebuildable SQLite index over the research artefacts and the
decision traces; the JSON / JSONL / Parquet files stay the source of truth.
Building it takes about a second and prints one row per table:

```bash
cd python
PYTHONPATH=src python3 -m iap.store build            # -> data/store/iap.sqlite (git-ignored)
# table                  rows
# alpha_signals             0
# alphas                   24
# drift_baselines          36
# experiment_results       34
# experiments              34
# instruments              19
# ledger_entries          139
# lifecycle_transitions   284
# model_runs               40
# tca_orders               36
# venues                    5
# ...
```

Every importer is idempotent — run `build` again and the counts do not
move — and total over its input: a record it cannot map (a NaN metric, a
malformed line, an absent optional artefact) is a warning on stderr, never
a crash. Query with `sql` (one canonical JSON line per row; add `ORDER BY`
for deterministic output) or with the views:

```bash
PYTHONPATH=src python3 -m iap.store sql "SELECT alpha_id, current_state, verdict, ROUND(ic,4) AS ic, ledger_count FROM v_alpha_scorecard ORDER BY alpha_id"
# {"alpha_id":"EQ01","current_state":"CANDIDATE","ic":0.0276,"ledger_count":120,"verdict":"ITERATE"}
# {"alpha_id":"EQ02","current_state":"CANDIDATE","ic":0.0253,"ledger_count":56,"verdict":"ITERATE"}
# ...  (24 rows)
PYTHONPATH=src python3 -m iap.store sql "SELECT COUNT(*) AS distinct_experiments, SUM(count) AS total_experiments FROM ledger_entries"
# {"distinct_experiments":139,"total_experiments":1920}     -- the Bonferroni denominator
```

Read the scorecard's `ic` and `verdict` with care after v1.4.0. The view
shows each alpha's *latest* experiment result, chosen by `created_ts` and
then by the largest experiment id, and it does not know about datasets.
The runner experiments of both datasets share one `created_ts`, so for the
three alphas that have them the tie-break decides: EQ01's row above is
`c73bb6294d226163`, a v1.3.0-dataset experiment (IC 0.0276), not the
v1.4.0 one (`695e7b1e2bd2253e`, IC −0.0017, REJECT); EQ06's row is likewise
a v1.3.0-dataset experiment. EQ02 has no runner experiment and shows the
v1.4.0 alpha report. For a per-dataset reading use
`python -m iap.research list` (recipe 26), which prints the dataset of each
experiment. The ledger counts (1,920 looks over 139 entries) cover both
datasets on purpose: of the 1,920, the 1,068 taken on the v1.3.0 dataset
are kept and 852 were added on the v1.4.0 one.

From Python the same store is `iap.store.Store` (`open`, `init`,
`insert_<type>` for every contract, `fetch(T, **where)`, `query`,
`export_jsonl`); `Store.export_jsonl(table, path)` writes canonical lines in
primary-key order, so two builds from the same files are byte-identical.
The DDL runs unchanged on PostgreSQL ≥ 13 (`psql -f schemas/sql/iap_v1.sql`).

## 21. Explain an order (the decision trace)

Every decision the loop makes is a `DecisionTrace` (signal → portfolio →
risk → parent order → child orders → routing → fills → TCA → attribution),
built with `iap.trace.TraceBuilder` and emitted to sinks. Write one to a
JSONL file and to the store, then ask why the order happened:

```bash
cd python && mkdir -p ../data/store
PYTHONPATH=src python3 - <<'PY'
from iap.contracts.examples import example_trace          # the pinned golden decision
from iap.store import Store
from iap.trace import JsonlTraceSink, MultiSink, StoreTraceSink, explain_jsonl
store = Store.open("../data/store/iap.sqlite"); store.init()
with MultiSink(JsonlTraceSink("../data/store/traces.jsonl"), StoreTraceSink(store)) as sink:
    sink.emit(example_trace())
    print("digest", sink.sinks[0].digest.hexdigest())       # replay-determinism digest
print(explain_jsonl("../data/store/traces.jsonl", 12345, {1: "XV1", 2: "XV2", 3: "XV3"}))
PY
PYTHONPATH=src python3 -m iap.store explain 12345         # venue names from the venues table
# Order 12345
# Alpha:      EQ03  expected return = +4.2 bps  confidence = 0.81
# Portfolio:  target = +20,000 shares
# Risk:       ALLOW
# Execution:  POV 15%
# SOR:        XV1 = 45%  XV2 = 35%  3 = 20%
# Fills:      18,000 / 20,000 (90.0%)
# TCA:        IS = 2.1 bps
# Attribution: alpha = +6.2 bps  spread = -0.8 bps  impact = -2.1 bps  fees = -0.4 bps
```

(The store names venues from `configs/venues/venues.json`, which has XV1 and
XV2; the golden example's third venue renders as its id.) A rejected order
renders its rule: `Risk: REJECT  rule = FAT_FINGER_NOTIONAL  reason = …`,
and the same chain is one row of `v_order_chain`:

```bash
PYTHONPATH=src python3 -m iap.store sql "SELECT parent_order_id, alpha_id, risk_decision, risk_rule_id, n_child_orders, filled_qty, implementation_shortfall_bps, attribution_total_bps FROM v_order_chain ORDER BY parent_order_id"
# {"alpha_id":"EQ03","attribution_total_bps":2.7,"filled_qty":18000,"implementation_shortfall_bps":2.1,"n_child_orders":3,"parent_order_id":12345,"risk_decision":1,"risk_rule_id":""}
```

The digest printed above is `sha256` over `canonical_json(trace) + "\n"`
per emitted trace: replaying the same session with the same seed reproduces
it, and `TraceDigest.of_jsonl(path)` recomputes it from the file
(`bf60a300d151c9…` for the one-trace stream of the golden example).

## 22. Run the MVP loop (one command, one instrument, fully traced)

The MVP (`python -m iap.mvp`, [docs/MVP.md](docs/MVP.md)) runs the whole
loop — seeded market data → books → features → EQ01/EQ03/EQ06 ensemble →
portfolio → hard risk → TWAP/POV/IS → SOR over XV1/XV2/XV3 → execution
simulator → TCA → attribution → decision trace → SQLite → report — on the
synthetic equity `SYN.EQ.AAPL` and writes every artefact under
`data/mvp/<run_id>/` (git-ignored). About 7 seconds:

```bash
cd python
PYTHONPATH=src python3 -m iap.mvp run                       # configs/mvp/mvp.json, seed 12345
# mvp run 58a10f2194a3c81c: events=15805 decisions=800 parents=235 children=348 fills=169 pnl=-81.531396 USD digest=f51890da0c3c66cd... out=.../data/mvp/58a10f2194a3c81c
PYTHONPATH=src python3 -m iap.mvp run --seed 7 --out /tmp/mvp7          # another seed, explicit directory
PYTHONPATH=src python3 -m iap.mvp verify                                  # run twice from scratch, compare digest/report/stream
cat ../data/mvp/58a10f2194a3c81c/report.md                                # the honest numbers (cost-negative)
```

`report.json` (canonical, sorted keys) carries the versions
(`config_version` over every config document in force, `data_version` =
sha256 of the captured stream, `feature_version`, `model_version`), counts,
risk decisions by rule, routing shares, control counters, the §12.1 P&L
identity, per-alpha realized IC (the research label definition —
`iap.labels.compute_labels` on the same stream — mid-to-mid and
cost-adjusted, with the shift-by-one IC, at the 1 s holding horizon and at
the alpha's fitted horizon next to the registry's research IC), TCA
aggregates and the trace digest; `paper_evidence.json` is the
`PaperEvidence` the lifecycle's PAPER → ACTIVE gates read (the registry
itself is not touched). The golden `tests/golden/expected_mvp.json` pins
the run above. The run id is the same as in v1.3.0 because `mvp.json` and
the seed are unchanged; the session is not — the v1.4.0 generator spreads
the flow over the whole 15-minute session, the three alphas were refitted,
and the loop makes 800 decisions (355 before) and loses 81.53 USD (22.65
before): +0.022 bps of alpha against −0.41 bps of cost on filled notional.
Outside the checkout (the Python image bakes `configs/` and
`research/alpha_registry.json` under `/app`) add `--repo-root /app` to
`run` / `replay` / `verify`.

## 23. Replay an incident from the captured stream

Every run captures its input (`events.jsonl` + IAP1 twin + `feed.json`) and
its configuration (`config.json`). `replay` re-runs the loop from that
capture — never from the generator — and asserts that the trace digest, the
report and the stream sha256 reproduce (exit 1 with a line-per-difference
diff otherwise; a reference document that changed since the run is
reported as a `config_version` mismatch before anything runs):

```bash
cd python
PYTHONPATH=src python3 -m iap.mvp replay --run ../data/mvp/58a10f2194a3c81c      # -> <run>/replay/
# replay OK: ../data/mvp/58a10f2194a3c81c reproduces its trace digest and report
```

Capture → replay → reproduce (`explain`, recipe 24; `risk_audit.jsonl`;
`traces.jsonl`) → debug (drive `iap.mvp.engine.MvpEngine` over
`iap.mvp.feed.load_feed(run_dir)` event by event) → fix → regression test
(a captured stream + its expected digest, the pattern of
`python/tests/test_mvp_golden.py`) is the incident flow of docs/MVP.md §6.

## 24. Explain an MVP order

The run's SQLite store holds every decision trace decomposed
(`decision_traces`, `alpha_signals`, `portfolio_targets`, `risk_decisions`,
`parent_orders`, `child_orders`, `venue_decisions`, `executions`,
`tca_results`, `attribution`) plus the MVP reference data, so `explain`
renders the chain with venue names:

```bash
cd python
PYTHONPATH=src python3 -m iap.mvp explain --run ../data/mvp/58a10f2194a3c81c 5
# Order 5
# Alpha:      EQ01-EQ03-EQ06  expected return = +0.0 bps  confidence = 0.38
# Alpha:      EQ01  expected return = +0.0 bps  confidence = 0.14
# Alpha:      EQ03  expected return = +0.1 bps  confidence = 1.00
# Alpha:      EQ06  expected return = +0.0 bps  confidence = 0.00
# Portfolio:  target = +5 shares
# Risk:       ALLOW
# Risk:       ALLOW
# Execution:  IS
# SOR:        XV3 = 100%
# Fills:      250 / 250 (100.0%)
# TCA:        IS = 0.5 bps
# Attribution: alpha = +0.0 bps  spread = -0.5 bps  impact = +0.0 bps  fees = -0.1 bps
PYTHONPATH=src python3 -m iap.store sql --db ../data/mvp/58a10f2194a3c81c/iap.sqlite \
  "SELECT parent_order_id, alpha_id, signal_model_version, risk_rule_id, n_child_orders, filled_qty, implementation_shortfall_bps FROM v_order_chain ORDER BY parent_order_id LIMIT 3"
```

The first `Alpha:` line is the ACTING signal — the ensemble the portfolio
sized on, `signal[0]` of the trace, the row `v_order_chain` joins
(`signal_model_version = EQ01-EQ03-EQ06`) — labelled with the order's
`alpha_id`; the next three are its components (EQ01, EQ03, EQ06 in
ensemble order), each labelled by its own `model_version`. There is one
`Risk:` line per child order the engine decided on (this parent had two).
Expected returns of a fitted alpha are hundredths of a basis point, a tenth
at most, which the pinned one-decimal renderer shows as `+0.0 bps` or
`+0.1 bps` — read `traces.jsonl` / `alpha_signals` for the exact values.

## 25. Bootstrap and inspect the alpha promotion lifecycle

The seven-state machine ([docs/LIFECYCLE.md](docs/LIFECYCLE.md)) reads the
24 alpha reports, the ledger and `alpha_params.json`, registers every alpha
at RESEARCH at the pinned bootstrap event time (the latest fold `test_end`)
and advances it until it stops moving. On the bundled data that is one
step: every alpha reaches CANDIDATE and holds there. All 24 fail
`net_pnl_after_costs`; for four of them (EQ02, EQ03, EQ12, FX04) it is the
only failed gate.

```bash
cd python
PYTHONPATH=src python3 -m iap.lifecycle bootstrap --dry-run      # compute, print, touch nothing
PYTHONPATH=src python3 -m iap.lifecycle bootstrap --force        # rewrite research/alpha_registry.json + lifecycle_transitions.jsonl (identical bytes on an identical rerun);
                                                                  # without --force a non-empty transition log (an append-only audit with any HUMAN retire/reset lines) is refused, exit 3
PYTHONPATH=src python3 -m iap.lifecycle status
# alpha | state | since_ts | failed gates
# ----- | ----- | -------- | ------------
# EQ01 | CANDIDATE | 1787691480577291027 | statistical_significance, net_pnl_after_costs, stability
# EQ03 | CANDIDATE | 1787691480577291027 | net_pnl_after_costs
# EQ04 | CANDIDATE | 1787691480577291027 | oos_ic, statistical_significance, fold_consistency, hypothesis_sign, net_pnl_after_costs, stability
# FX09 | CANDIDATE | 1787691480577291027 | oos_ic, statistical_significance, fold_consistency, hypothesis_sign, net_pnl_after_costs, stability
# ...  (24 rows, all CANDIDATE)
PYTHONPATH=src python3 -m iap.lifecycle retire FX09 --reason "desk decision: rationale contradicted by the fitted sign"   # HUMAN edge -> RETIRED
PYTHONPATH=src python3 -m iap.lifecycle reset FX09 --reason "re-run the evidence chain after the refit"                # HUMAN edge RETIRED -> RESEARCH
git checkout -- ../research/alpha_registry.json ../research/lifecycle_transitions.jsonl                                 # the committed state is the bundled result
```

Every transition is one canonical-JSON `LifecycleTransition` line in
`research/lifecycle_transitions.jsonl` (gates, policy `lifecycle_v1`,
actor, reason); the registry records each alpha's last evaluation with its
`failed_gates`. A manual `retire` / `reset` needs a non-empty reason and is
HUMAN-only; a SYSTEM `advance` on a RETIRED alpha records `TERMINAL` and
moves nothing. Thresholds live in `configs/strategies/lifecycle.json`
(promotion) and `configs/strategies/strategies.json` `adaptive.lifecycle`
(live); the golden `tests/golden/expected_lifecycle.json` scripts three
whole lives that Python, Java and Rust reproduce step by step
(`PYTHONPATH=src python3 -m pytest -q tests/test_lifecycle_golden.py`).
Changing a threshold is a `lifecycle.json` x-version bump + `python3
tools/make_golden_lifecycle.py --force` + a MIGRATIONS entry, never an edit
of the registry by hand (GOVERNANCE.md §1). The MVP's `paper_evidence.json`
(recipe 22) is the `PaperEvidence` document the PAPER → ACTIVE gates read.
The committed registry and transition log were rebuilt for the v1.4.0
dataset with `bootstrap --force`; the log of the v1.3.0 dataset is kept at
`research/archive/lifecycle_transitions.dataset-203c8f54.jsonl`. On that
dataset eight alphas failed only the cost gate (EQ01, EQ02, EQ03, EQ05,
EQ06, EQ11, EQ12, FX04). With the sparser v1.4.0 equity flow EQ01, EQ05 and
EQ11 also fail `statistical_significance`, EQ05 fails `oos_ic`, and EQ01,
EQ05 and EQ06 fail `stability`.

## 26. Run one alpha as a contract-driven experiment

`iap.research` ([research/experiments/README.md](research/experiments/README.md),
LEARN.md §6.8) turns "run EQ03 at 1 s" into a typed `ExperimentSpec` whose
id is the hash of the request, runs the same purged / embargoed walk-forward
the promotion report runs plus a holdout backtest, writes a typed
`ExperimentResult`, and enters the run in the multiple-testing ledger:

```bash
cd python
PYTHONPATH=src python3 -m iap.research run --alpha EQ03 --horizon 1s               # spec block, result table, VERDICT, the ledger note
PYTHONPATH=src python3 -m iap.research run --alpha EQ03 --horizon 1s --dry-run     # no experiment directory is written; the looks ARE debited in the ledger
PYTHONPATH=src python3 -m iap.research run --alpha EQ06 --config n_folds=3 --config cost_multiplier=2.0   # a different configuration = a different id
PYTHONPATH=src python3 -m iap.research list                                        # every research/experiments/<id>/ with alpha, horizon, dataset, verdict
PYTHONPATH=src python3 -m iap.research show 876b08e20c46e6fd                       # the spec and result documents (EQ03 @ 1 s, v1.4.0 dataset)
```

`list` prints a `dataset` column since v1.4.0. The committed folder holds
ten experiments: the five pinned runs on the v1.3.0 dataset (`203c8f54`,
kept as history) and the same five requests on the v1.4.0 dataset
(`116b7787`):

```
experiment        alpha   horizon  dataset           IC      NW t     net bps  verdict
20f1b9093e7d0d04  EQ06    10s      116b7787   +0.034528    +3.598  -1148.3084  ITERATE
217fa0cb1d89a9c8  EQ03    5s       203c8f54   +0.036305   +10.449  -1172.9449  ITERATE
4a2900e4a6705542  EQ06    1s       203c8f54   +0.019676    +3.509   -531.1169  ITERATE
695e7b1e2bd2253e  EQ01    1s       116b7787   -0.001674    +0.771   -531.3535  REJECT
852863faa44b7b07  EQ06    1s       116b7787   +0.004702    +0.918  -1148.2478  REJECT
876b08e20c46e6fd  EQ03    1s       116b7787   +0.013610    +3.488  -2174.7262  ITERATE
c73bb6294d226163  EQ01    1s       203c8f54   +0.027590    +3.044   -301.4984  ITERATE
d0dd1ab0711d33a1  EQ06    10s      203c8f54   +0.050475    +6.755   -531.1169  ITERATE
d7b554d0a3fa3b26  EQ03    1s       203c8f54   +0.016471    +4.250  -1173.0925  ITERATE
f0f6c49b553f6b59  EQ03    5s       116b7787   +0.027114    +4.840  -2174.7262  ITERATE
```

Every pair moved the same way: a smaller IC, a smaller t and a larger
holdout loss on the v1.4.0 dataset. EQ01 @ 1 s and EQ06 @ 1 s went from
ITERATE to REJECT; no run is net-positive on either dataset. The dataset
version is part of the specification, so the same request on a regenerated
dataset is a new experiment id and a new 28 looks.

Where the files land: `research/experiments/<id>/{spec.json,result.json}`,
sorted keys, 2-space indent, no wall clock — an identical rerun is
byte-identical (only `git_commit` and `n_experiments_in_ledger` are
provenance and may legitimately move), and a rerun that reproduces
*different* evidence under the same id is refused, not overwritten. An empty
configuration takes the alpha's pinned horizon and the pinned protocol
(EQ03 @ 5 s is `f0f6c49b553f6b59`). Its statistics are not the ones in
`research/alpha_reports/EQ03.json`, and are not meant to be: the runner
walks forward over the window before its declared holdout and reports the
holdout separately (IC +0.0271, t 4.84), while `run_all.py` walks forward
over both sessions and declares no holdout (IC 0.0190, t 5.78).
`python/tests/test_research_runner.py::test_eq03_report_reproduces_through_the_runner`
pins both the difference and that the runner equals `validate_alpha` called
on the runner's own window. Every new configuration adds
28 looks to `research/experiments.json` (`iap.research.LOOKS_PER_EXPERIMENT`;
a rerun of an identical spec adds none), and since v1.3.0 a `--dry-run`
debits them too — it evaluates and prints every statistic, so it is a look
(`check_headline_numbers.py` reports the docs stale until they follow the
ledger). Runs made since v1.3.0 also write `eligibility.json` beside the
result (recipe 35); the five v1.4.0-dataset experiments have it, the five
v1.3.0-dataset experiments predate it. To
experiment without moving the committed ledger, point `--ledger` and
`--out-dir` at scratch files, as recipe 29 does. The golden
`tests/golden/expected_experiment_golden_frame.json` pins one spec / result
pair over the golden equity vector.

## 27. Run the planted-signal power study on a tiny grid

The validation chain reports 0 PROMOTE on the bundled data. That is only
informative if the chain can find an effect when one exists. The power
study ([research/power/POWER_REPORT.md](research/power/POWER_REPORT.md),
LEARN.md §23) plants effects of known size in the generator — informed
order flow and an ETF lead-lag — and runs the real feature pipeline and
`validate_alpha` on the result. The committed report is 4 levels × 3 seeds;
this is the smallest grid that still has a null row, a planted row and a
break row (three generator runs, a few minutes):

```bash
cd python
PYTHONPATH=src python3 -m iap.research power --levels 0,1 --seeds 1 \
  --power-out-dir ../data/store/power-tiny
# (progress on stderr)
# stable level 0 seed 850875211: lead_lag=REJECT, order_flow=REJECT
# stable level 1 seed 850875211: lead_lag=REJECT, order_flow=ITERATE
# break level 1 seed 850875211: lead_lag=REJECT, order_flow=REJECT
# wrote ../data/store/power-tiny/POWER_REPORT.md and .../POWER_REPORT.json
```

The detection table it prints for this grid:

```
| effect | alpha | scenario | level | runs | sig (within) | sig (pooled) | sig (ledger) | evidence | promote | P&L CI > 0 |
| lead_lag | EQ10 | break | 1 | 1 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| lead_lag | EQ10 | stable | 0 | 1 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| lead_lag | EQ10 | stable | 1 | 1 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| order_flow | EQ04 | break | 1 | 1 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| order_flow | EQ04 | stable | 0 | 1 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| order_flow | EQ04 | stable | 1 | 1 | 0.00 | 0.00 | 0.00 | 1.00 | 0.00 | 0.00 |
```

On this seed the planted order-flow effect at the reference size is
reported as evidence (ITERATE) and is not significant: the within-bucket t
is 1.90 and the pooled t 1.97, against a gate of 3. The planted lead-lag is
not detected at all. With the v1.3.0 generator the same command flagged the
order-flow row significant on every statistic and gave the lead-lag row
ITERATE. The seed and the planted effect are the same in both runs; what
changed is the flow calibration (recipe 1), which spreads about the same
number of equity events over the whole session instead of its first 40%,
and the chain detects the same effect less often on the sparser flow. The
committed three-seed report says the same thing
with more runs: at the reference size the order-flow effect is significant
in 1 of 3 seeds (evidence in 3 of 3), at twice the reference in 3 of 3, at
half the reference in none; the lead-lag is detected at no size; nothing is
promoted anywhere.

Read it the way the report tells you to: level 0 is the false-positive row;
`stable` rows are power; `break` rows plant an effect that reverses
mid-sample. With one seed a rate is 0 or 1 — this run shows the mechanics,
the committed three-seed report is the one to quote, and even that moves in
steps of 0.33. Never pass `--power-out-dir research/power` unless you mean
to replace the committed report; the study itself never touches `data/` or
the research ledger. The reference effect is
`research/power/generator_planted.json` (deliberately outside `configs/`,
which is shipped to the pods).

## 28. Backtest with the cost-aware position policy and compare with the default

The default research backtest takes `sign(expected_return)` and re-decides
on every row, so an alpha with a real but small signal trades constantly
and pays the spread each time. The opt-in `cost_aware` policy
(`BacktestConfig`, docs/RESEARCH_VALIDITY.md §1) enters only when the
expected return exceeds the round-trip cost of that row, holds for the
label horizon, and renews only above `hysteresis ×` that cost:

```bash
PYTHONPATH=python/src python3 - <<'EOF'
import json
from iap.alpha import load_params_file
from iap.alpha.data import load_features
from iap.backtest import Backtester, BacktestConfig, CostModel
from iap.validation.metrics import HORIZONS_NS

meta = {int(r["instrument_id"]): {"symbol": r["symbol"],
        "asset_class": r["asset_class"], "tick_size": float(r["tick_size"]),
        "lot_size": int(r["lot_size"]), "adv": float(r["adv"]),
        "ref_price": float(r.get("ref_price", 1.0))}
        for r in json.load(open("configs/instruments/instruments.json"))["instruments"]}
frames = load_features("data/features")
m = load_params_file("configs/strategies/alpha_params.json")["EQ03"]
scores = m.score({i: frames[i] for i in m.universe(list(frames))})
costs = CostModel.load("configs/execution/execution.json")

for name, cfg in (
    ("sign (default)", BacktestConfig()),
    ("cost_aware", BacktestConfig(position_policy="cost_aware",
                                  horizon_ns=HORIZONS_NS[m.horizon], hysteresis=0.5)),
):
    res = Backtester(costs, meta, cfg).run(frames=frames, scores=scores, asset_class="EQUITY")
    print(f"{name:15s} trades={res.trade_count:6d} gross={res.gross_pnl:12.2f} "
          f"costs={res.total_costs:12.2f} net={res.total_pnl:12.2f}")
EOF
# sign (default)  trades= 38222 gross=     4695.00 costs=   831621.36 net=  -826926.36
# cost_aware      trades=     0 gross=        0.00 costs=        0.00 net=        0.00
```

The honest reading: the default policy loses 826,926.36 because it pays
831,621.36 of costs to collect 4,695.00 of gross; the cost-aware policy
loses nothing because it never trades — EQ03's fitted expected return does
not clear its own round-trip cost on a single row in two sessions (on the
v1.3.0 dataset it did on four). That is not a profitable strategy found,
and a policy that makes no trade is no evidence that abstaining pays; it is
the same finding (real signal, smaller than the spread) stated from the
other side. Like
recipe 11 this scores the full two-day frame, including the day the
parameters were fitted on, so neither number is the report's OOS figure.
`cost_aware` needs `horizon_ns`; the two neighbouring options are
`cap_fills_at_l1=True` (no fill larger than the displayed L1 size) and
`block_rows_column="label_valid_<h>"` (trade only the rows the IC is
measured on).

## 29. Judge an experiment against the ledger-derived t threshold

The PROMOTE gate asks for a Newey–West t of 3.0. The ledger has always
computed what the count of looks implies — a Bonferroni |t| — and no gate
read it. `--tstat-threshold ledger` makes the gate
`max(3.0, the ledger's Bonferroni |t|)`: it can only tighten. This runs on
a scratch copy of the ledger so the committed one does not move:

```bash
mkdir -p data/store/scratch && cp research/experiments.json data/store/scratch/ledger.json
cd python
PYTHONPATH=src python3 -m iap.research --out-dir ../data/store/scratch/experiments run \
  --alpha EQ03 --tstat-threshold ledger --ledger ../data/store/scratch/ledger.json
PYTHONPATH=src python3 - <<'EOF'
from iap.validation.ledger import ExperimentLedger
from iap.validation.validate import GATES, effective_gates

ledger = ExperimentLedger("../data/store/scratch/ledger.json")
t = ledger.bonferroni_t_threshold()
print("looks:", ledger.total_experiments, "bonferroni |t|:", round(t, 3))
print("fixed  min_nw_tstat:", GATES["min_nw_tstat"])
print("ledger min_nw_tstat:", round(effective_gates("ledger", t)["min_nw_tstat"], 3))
EOF
# ... NW t-stat  +4.8405 ... VERDICT: ITERATE
# gate eligible: yes
# looks: 1920 bonferroni |t|: 4.206
# fixed  min_nw_tstat: 3.0
# ledger min_nw_tstat: 4.206
```

EQ03 at its pinned horizon is the committed experiment `f0f6c49b553f6b59`,
so the run is a rerun and adds no looks; its t of 4.84 clears either
threshold — by much less than on the v1.3.0 dataset, where it was 10.45
against 4.071 — and the verdict stays ITERATE because the alpha loses money
after costs, not because of significance. The policy matters for results
whose t sits between 3.0 and 4.21: on this dataset the committed
EQ03 @ 1 s (3.49) and EQ06 @ 10 s (3.60) runs. From Python the same switch is
`validate_alpha(..., tstat_threshold="ledger", ledger_t_threshold=t)` and
`ExperimentRunner(..., tstat_threshold="ledger")`; a new configuration is
judged against the threshold the ledger will have once its own 28 looks are
in it.

## 30. Run the recompute leakage probe

The truncation probe re-scores a model on truncated *feature frames*. It
cannot see look-ahead that is already baked into the frame — a centred
window, a full-sample normalisation. The recompute probe truncates the
*raw events* instead: it rebuilds the features from an event prefix at a
few anchors and requires the last row to equal the same row of the full
run, bit for bit:

```bash
PYTHONPATH=python/src python3 - <<'EOF'
from iap.alpha import load_params_file
from iap.core.codec import read_jsonl
from iap.validation.leakage import LeakageTester, engine_frame_builder

events = read_jsonl("tests/golden/events_eq_mbo.jsonl")
build = engine_frame_builder("configs")
model = load_params_file("configs/strategies/alpha_params.json")["EQ01"]

clean = LeakageTester().recompute_probe(model, events, build, n_probes=3)
print("reference engine:", clean.ok, "anchors:", clean.n_anchors, "leaky:", clean.leaky_columns)

def leaky_build(evs):                       # a feature built with look-ahead:
    frames = build(evs)                     # normalised by the FULL-sample mean
    for f in frames.values():
        f["dev_demeaned"] = f["micro_mid_dev_bps_v1"] - f["micro_mid_dev_bps_v1"].mean()
    return frames

leaky = LeakageTester().recompute_probe(None, events, leaky_build, n_probes=3)
print("leaky pipeline:  ", leaky.ok, "anchors:", leaky.n_anchors, "leaky:", leaky.leaky_columns)
EOF
# reference engine: True anchors: 3 leaky: []
# leaky pipeline:   False anchors: 3 leaky: ['dev_demeaned']
```

The reference feature engine passes on the golden equity vector (and the
model's score at each anchor is unchanged). The second pipeline adds one
column demeaned by the mean of everything it was given; the frame probe
would never notice, and the recompute probe names the column. Cost is one
feature rebuild per anchor, which is why it is opt-in and not part of
`LeakageTester.run`; pass `model=None` to probe the features alone.

## 31. Per-fold diagnostics with a bootstrap interval

`validate_alpha` computes cost survival, decay and the regime split on the
last walk-forward fold only and reports net P&L as one number.
`iap.validation.diagnostics` computes them for every fold and puts a
stationary-bootstrap interval (Politis & Romano; SplitMix64, seeded) around
the pooled net P&L. No gate reads it:

```bash
PYTHONPATH=python/src python3 - <<'EOF'
import json
from iap.alpha import build
from iap.alpha.data import load_features
from iap.backtest import Backtester, BacktestConfig, CostModel
from iap.validation.diagnostics import fold_diagnostics, stationary_bootstrap_ci

meta = {int(r["instrument_id"]): {"symbol": r["symbol"],
        "asset_class": r["asset_class"], "tick_size": float(r["tick_size"]),
        "lot_size": int(r["lot_size"]), "adv": float(r["adv"]),
        "ref_price": float(r.get("ref_price", 1.0))}
        for r in json.load(open("configs/instruments/instruments.json"))["instruments"]}
frames = load_features("data/features")
bt = Backtester(CostModel.load("configs/execution/execution.json"), meta, BacktestConfig())

d = fold_diagnostics(lambda: build("EQ03"), frames, bt, seed=20260919, n_boot=300)
for f in d["folds"]:
    print("fold", f["fold"], "pairs", f["n_test_pairs"],
          "net@1x", round(f["net_pnl_by_cost"]["x1"], 2),
          "survives 1x:", f["survives_1x_cost"])
print("folds surviving 1x cost:", d["n_folds_survive_1x_cost"], "of", d["n_folds_run"])
b = d["net_pnl_bootstrap"]
print("pooled net P&L:", round(d["net_pnl_1x_pooled"], 2),
      "95% CI:", [round(b["ci_low"], 2), round(b["ci_high"], 2)],
      "mean block:", b["mean_block"], "resamples:", b["n_boot"])

# the bootstrap on its own: a seeded interval for the SUM of a dependent series
print(stationary_bootstrap_ci([1.0, -2.0, 0.5, 3.0, -1.0, 0.25, 2.0, -0.5, 1.5, -0.75],
                              seed=7, n_boot=200))
EOF
# fold 1 pairs 31980 net@1x -174562.22 survives 1x: False
# fold 2 pairs 31735 net@1x -168827.42 survives 1x: False
# fold 3 pairs 31891 net@1x -165593.72 survives 1x: False
# fold 4 pairs 31887 net@1x -161585.48 survives 1x: False
# folds surviving 1x cost: 0 of 4
# pooled net P&L: -670568.83 95% CI: [-692392.14, -646896.44] mean block: 9.0 resamples: 300
# {'estimate': 4.0, 'ci_low': -2.75, 'ci_high': 10.75, 'level': 0.95, 'n': 10, 'n_boot': 200, 'mean_block': 2.0, 'seed': 7, 'frac_resamples_le_zero': 0.175}
```

EQ03 loses money after costs in every fold, not only the last, and the
whole interval is below zero: the negative result is not one unlucky fold.
The interval is pinned by its seed — pass `ExperimentSpec.seed` and two
runs agree to the last digit; `n_boot` defaults to 1,000 (300 here to keep
the recipe quick).

## 32. Read experiments as JSON, and get machine-readable errors

For a script or a tool the research CLI prints one JSON document on stdout
(`--json`) and, with the top-level `--json-errors`, exactly one JSON object
on stderr per failure with a stable `code` (`iap.research.errors`):

```bash
cd python
PYTHONPATH=src python3 -m iap.research list --json > ../data/store/experiments.json
python3 -c "import json; d=json.load(open('../data/store/experiments.json')); print(len(d['experiments']), 'experiments,', len(d['skipped']), 'skipped'); print([(e['experiment_id'], e['spec']['alpha_id'], e['result']['verdict'], e['gate_eligibility']['gate_eligible']) for e in d['experiments']])"
PYTHONPATH=src python3 -m iap.research show f0f6c49b553f6b59 --json | python3 -c "import json,sys; d=json.load(sys.stdin); print(sorted(d)); print(d['gate_eligibility'])"
PYTHONPATH=src python3 -m iap.research --json-errors show 0000000000000000; echo "exit=$?"
PYTHONPATH=src python3 -m iap.research --json-errors run --horizon 1s; echo "exit=$?"
# 10 experiments, 0 skipped
# [('20f1b9093e7d0d04', 'EQ06', 'ITERATE', True), ('217fa0cb1d89a9c8', 'EQ03', 'ITERATE', True), ('4a2900e4a6705542', 'EQ06', 'ITERATE', True), ('695e7b1e2bd2253e', 'EQ01', 'REJECT', True), ('852863faa44b7b07', 'EQ06', 'REJECT', True), ('876b08e20c46e6fd', 'EQ03', 'ITERATE', True), ('c73bb6294d226163', 'EQ01', 'ITERATE', True), ('d0dd1ab0711d33a1', 'EQ06', 'ITERATE', True), ('d7b554d0a3fa3b26', 'EQ03', 'ITERATE', True), ('f0f6c49b553f6b59', 'EQ03', 'ITERATE', True)]
# ['experiment_id', 'gate_eligibility', 'result', 'spec']
# {'gate_eligible': True, 'periods_verified': True, 'reasons': []}
# {"error": {"code": "experiment_not_found", "message": ".../research/experiments/0000000000000000: no such experiment"}}
# exit=1
# {"error": {"code": "usage_error", "message": "the following arguments are required: --alpha"}}
# exit=2
```

`list --json` is `{"experiments": [...], "skipped": [...]}`: a directory
that cannot be loaded (half-written, corrupt) is named in `skipped` with
the reason instead of failing the listing. The five v1.4.0-dataset
experiments carry the `eligibility.json` sidecar and show
`periods_verified: True`. The five v1.3.0-dataset experiments (`show
217fa0cb1d89a9c8 --json`, for one) show `periods_verified: False`, which is
honest: they predate the sidecar, so only their configuration bounds can be
checked (recipe 35).
Usage errors exit 2, everything else 1. Both commands only read.

## 33. Query the store read-only, and see what a refused statement looks like

`python -m iap.store sql` opens the SQLite file read-only and takes exactly
one statement, so it is safe to hand to a tool — the index can only change
through `build`:

```bash
cd python
PYTHONPATH=src python3 -m iap.store build > /dev/null
PYTHONPATH=src python3 -m iap.store sql "SELECT current_state, COUNT(*) AS n FROM v_alpha_scorecard GROUP BY current_state ORDER BY current_state"
PYTHONPATH=src python3 -m iap.store sql "SELECT verdict, COUNT(*) AS n FROM v_alpha_scorecard GROUP BY verdict ORDER BY verdict"
PYTHONPATH=src python3 -m iap.store sql "DELETE FROM alphas"; echo "exit=$?"
PYTHONPATH=src python3 -m iap.store sql "SELECT 1; SELECT 2"; echo "exit=$?"
PYTHONPATH=src python3 -m iap.store sql "SELECT * FROM no_such_table"; echo "exit=$?"
# {"current_state":"CANDIDATE","n":24}
# {"n":10,"verdict":"ITERATE"}
# {"n":14,"verdict":"REJECT"}
# error: attempt to write a readonly database (the store is opened read-only; use `build` to rebuild it)
# exit=1
# error: `sql` runs exactly one statement; several were given (You can only execute one statement at a time.)
# exit=1
# error: no such table: no_such_table
# exit=1
```

The first two rows are the platform's headline result as a query: every
alpha at CANDIDATE, none promoted. (The 10 / 14 split equals the v1.4.0
promotion report's, but the view takes each verdict from the alpha's latest
experiment result, which for EQ01 and EQ06 is a runner experiment on the
v1.3.0 dataset — recipe 20. Quote verdict counts from
`research/alpha_reports/REPORT.md`.) The three failures are the three ways a
statement is refused: a write (with the rebuild hint, which is printed only
for a write), several statements (refused rather than half-run), and a
plain SQL error (the engine's own message, no hint). Add `--db <file>` to
query an MVP run's store (recipe 24).

## 34. Replay the risk edge golden in Python

The main risk golden drives one engine through one script. The edge golden
(`tests/golden/expected_risk_edge_decisions.json`, v1.3.0) is eight
independent scenarios, each with its own engine, that reach what one script
cannot: a venue kill and a venue-0 (SOR) order, kill commands that do not
parse, every venue disconnected, a mark stamped in the future, timestamp
and position overflow, unvaluable exposure, a missing config key, bootstrap
and restore. Rust, Java and Python must reproduce every decision and the
concatenated audit log byte for byte:

```bash
cd python
PYTHONPATH=src python3 -m pytest -q tests/test_risk_golden.py -k edge
PYTHONPATH=src python3 - <<'EOF'
import json, sys
sys.path.insert(0, "tests")                        # the shared replay driver lives in the test module
from test_risk_golden import replay_edge

edge = json.load(open("../tests/golden/expected_risk_edge_decisions.json"))
config = json.load(open("../configs/risk/risk.json"))
audit = replay_edge(edge, config, check=True)       # raises on any decision mismatch
want = open("../tests/golden/expected_risk_edge_audit.jsonl", encoding="utf-8", newline="").read()
print("scenarios:", len(edge["scenarios"]), "audit lines:", len(audit.splitlines()),
      "byte-identical:", audit == want)
for line in audit.splitlines():
    ev = json.loads(line)
    if "fail-closed" in ev["reason"]:
        print(ev["rule_id"], "|", ev["reason"])
EOF
# 2 passed, 7 deselected
# scenarios: 8 audit lines: 50 byte-identical: True
# MALFORMED_KILL | kill scope id "AAPL" is not a valid INSTRUMENT id: escalated to GLOBAL (fail-closed): ops halt by ticker
# MALFORMED_KILL | kill scope id "-0" is not a valid VENUE id: nothing cleared (fail-closed): ops clear
# MALFORMED_ORDER | timestamp arithmetic overflows i64 (fail-closed)
# GROSS_NOTIONAL | position in instrument 2 has no mark price (fail-closed)
# GROSS_NOTIONAL | open order 3 in instrument 2 has no mark price (fail-closed)
# MALFORMED_ORDER | projected position overflows i64 (fail-closed)
# KILL_SWITCH_ENGAGED | fill for order 7 overflows i64 position accounting (fail-closed)
# CONFIG_MISSING | fail-closed: invalid argument: risk.json: missing/non-integer per_order.max_order_qty
# CONFIG_MISSING | fail-closed: invalid argument: risk.json: missing/non-integer per_order.max_order_qty
# NOT_BOOTSTRAPPED | positions not bootstrapped (fail-closed)
```

The other two engines replay the same file: `cd rust && cargo test -p risk
--test golden_risk` and Java `RiskGoldenTest` (recipe 9). To add a scenario,
edit `python/tools/make_golden_risk_edge.py` and regenerate
(`PYTHONPATH=python/src python3 python/tools/make_golden_risk_edge.py` from
the repo root) — a deliberate `golden:` commit with a MIGRATIONS entry,
after which all three engines must match again. LEARN.md §21 walks
through the four bugs these scenarios pin.

## 35. Check whether a result is gate-eligible

Any valid specification can be run, and its looks are ledgered. Only a
result produced under the pinned protocol or something stricter is
promotion evidence: `cost_multiplier >= 1.0`, `latency_ns >= 1 s`,
`embargo_ns >= 60 s`, `n_folds >= 4`, `max_decision_age_ns <= 60 s`,
session flattening on, and periods equal to the ones derived from the
dataset's session calendar. Halving the costs, or choosing the holdout by
hand, still runs — and is marked:

```bash
PYTHONPATH=python/src python3 - <<'EOF'
from iap.alpha.data import load_features
from iap.research import build_spec
from iap.research.registry import ExperimentRegistry
from iap.research.specs import gate_eligibility

frames = load_features("data/features")

pinned = build_spec("EQ03", frames=frames)                       # the pinned protocol
print(pinned.experiment_id, gate_eligibility(pinned, frames))

cheap = build_spec("EQ03", configuration={"cost_multiplier": 0.5, "n_folds": 2},
                   frames=frames)                                # valid, runnable, NOT evidence
e = gate_eligibility(cheap, frames)
print(cheap.experiment_id, "eligible:", e.eligible)
for reason in e.reasons:
    print("  -", reason)

# a persisted run: the sidecar if the runner wrote one, else configuration bounds only
print(ExperimentRegistry("research/experiments").gate_eligibility("f0f6c49b553f6b59"))
EOF
# f0f6c49b553f6b59 GateEligibility(eligible=True, reasons=(), periods_verified=True)
# 4e674d748dd5d2c8 eligible: False
#   - configuration.n_folds=2 is below the gate-eligible minimum 4
#   - configuration.cost_multiplier=0.5 is below the gate-eligible minimum 1.0
# GateEligibility(eligible=True, reasons=(), periods_verified=True)
```

The last line reads the sidecar of a persisted v1.4.0-dataset run. Asked
about a v1.3.0-dataset run (`217fa0cb1d89a9c8`), which has no sidecar, the
registry answers `periods_verified=False`.

The runner writes the determination to `eligibility.json` beside
`result.json`, `python -m iap.research run` prints it (`gate eligible: yes`
or `NO` with the reasons), and `show --json` / `list --json` carry it
(recipe 32). In the lifecycle, `Evidence(research_gate_eligible=False)`
makes every gate that reads the research block fail with a null value,
exactly as if the number were missing; the flag is serialised only when
false, so the committed evidence and the lifecycle golden are unchanged.
The Java and Rust lifecycle ports do not read the key and reject a document
that carries it. LEARN.md §24 explains which ways of gaming the gate this
closes.


## 36. Ingest an ITCH 5.0 file and run an alpha on it (no real data needed)

`python -m iap.marketdata ingest` ([docs/REAL_DATA.md](docs/REAL_DATA.md))
turns a Nasdaq TotalView-ITCH 5.0 file — or LOBSTER files — that **you
obtained** into a dataset directory with the generator's layout, so the
feature pipeline and the research runner consume it unchanged. No vendor
data is in this repository, so this recipe writes two days of valid ITCH 5.0
bytes with the test-only encoder and runs the whole chain on them. Replace
the first block with your own files and the rest is the real procedure.

```bash
mkdir -p data/vendor
cd python
PYTHONPATH=src python3 - <<'EOF'
import sys
sys.path.insert(0, "tests")                      # the test-only encoder lives beside the tests
from itch50_encoder import build_session

for seed, date in ((7, "2019-12-30"), (8, "2019-12-31")):
    enc, _ = build_session(seed, symbols=("AAPL", "MSFT", "QQQ"), etp_symbols=("QQQ",),
                           n_actions=6000, spacing_ns=10**9)
    path = enc.write(f"../data/vendor/demo_{date}.itch.gz", compress=True)
    print(path, enc.messages, "messages")
EOF
# ../data/vendor/demo_2019-12-30.itch.gz 6082 messages
# ../data/vendor/demo_2019-12-31.itch.gz 6082 messages

for d in 2019-12-30 2019-12-31; do
  PYTHONPATH=src python3 -m iap.marketdata ingest --format itch50 \
    --input ../data/vendor/demo_$d.itch.gz --date $d \
    --symbols AAPL,MSFT,QQQ --out ../data/real/demo > /dev/null
done
PYTHONPATH=src python3 - <<'EOF'
import json
m = json.load(open("../data/real/demo/dataset.json"))
print(m["dataset_version"][:16], list(m["sessions"]), [u["symbol"] for u in m["universe"]])
s = m["sessions"]["2019-12-30"]
print(s["messages"]["by_type"])
print("events", s["raw"]["events"], "book check clean:", s["book_check"]["clean"],
      "qc:", {k: v for k, v in m["normalized"]["qc_totals"].items() if v})
EOF
# 70ddb405e78c2356 ['2019-12-30', '2019-12-31'] ['AAPL', 'MSFT', 'QQQ']
# {'A': 3061, 'C': 220, 'D': 886, 'E': 820, 'F': 10, 'H': 4, 'I': 4, 'P': 164, 'Q': 8, 'R': 4, 'S': 6, 'U': 419, 'X': 472, 'Y': 4}
# events 5576 book check clean: True qc: {'events_in': 11255, 'events_out': 11255, 'sequence_resets': 3}

PYTHONPATH=src python3 -m iap.features --data-dir ../data/real/demo/normalized \
  --out-dir ../data/real/demo/features --configs ../data/real/demo/configs \
  --registry-out ../data/real/demo/reference/feature_registry.json 2> /dev/null
PYTHONPATH=src python3 -m iap.research run --alpha EQ03 --dataset-dir ../data/real/demo \
  | sed -n '1,5p;/VERDICT/p'
# experiment         240bdd3b53899e7f
# alpha              EQ03
# horizon            5s
# dataset_version    70ddb405e78c2356c2a6f15a85f8dfe520cc27bf427ec2532d4257c480e8e29e
# feature_version    585dd7b92b738f9da7df863dba1ac049ff10b75b60a8559baac1e49b6f3062ac
# VERDICT: REJECT
```

What to read in the output. The message counts are per ITCH type, counted
over the whole file before the symbol filter (the file also carries a
fourth symbol the universe does not ask for). `sequence_resets: 3` is one
per instrument: sequences restart at 1 every session, and the normaliser
counts that; gaps, duplicates and invalid events are absent because they
are 0. `book check clean` means the mapped stream replayed through the
real order book without a dropped event or a crossing displayed order.

The `dataset_version` in the experiment is the manifest's, not the bundled
dataset's `116b7787…`: the spec, the experiment id and the ledger
entries of its 28 looks carry it, and the ledger they went into is
`data/real/demo/research/experiments.json` — `research/experiments.json`
in the checkout did not move. Ingesting the same bytes again gives the
same `dataset_version` and byte-identical files.

The REJECT means nothing: the encoder's order flow is uniform random, so
there is nothing to find. With real files the verdict is about that
dataset, under the reading rules of docs/REAL_DATA.md §8 — two sessions
are two sessions. `rm -rf data/vendor/demo_* data/real/demo` removes
everything this recipe wrote.
