# COOKBOOK — task-oriented recipes

Every recipe below is runnable from a fresh checkout of this repository in
the standard environment (Python 3.11 + pyarrow, g++ 13/CMake, Rust 1.95,
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

---

## 1. Regenerate the dataset from scratch

The whole pipeline — seeded generator → `data/raw/*.jsonl` → normalization
(QC, event-time reorder) → `data/normalized/*.{jsonl,iap1}` + Parquet + QC
report:

```bash
cd python
PYTHONPATH=src python3 -m iap.marketdata            # configs/generator.json, seed 20260829
PYTHONPATH=src python3 -m iap.marketdata --seed 42  # explicit seed override
```

Identical seed ⇒ bit-identical output files. Check the QC audit afterward:

```bash
python3 -m json.tool ../data/normalized/qc_report.json | head -30
```

Expect (seed 20260829): 310,782 events in, 310,159 out; 322 gaps, 450
duplicates, 182 out-of-order, 173 invalid events counted per stream.
Generated data is never committed (BUILD_NOTES.md).

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

**Rust** — the replay demo binary (book summary + throughput):

```bash
cd rust && cargo run --release --bin demo          # defaults to tests/golden/
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

The bundled run: 310,159 events → 208,437 vectors, registry hash
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
        for r in json.load(open("configs/instruments.json"))["instruments"]}
frames = load_features("data/features")
exec_cfg = json.load(open("configs/execution.json"))
bt = Backtester(CostModel.load("configs/execution.json"), meta, BacktestConfig())

rep = validate_alpha(lambda: build("EQ03"), frames, bt, meta,
                     float(exec_cfg["defaults"]["max_participation"]))
print({k: rep[k] for k in ("oos_ic", "oos_rank_ic", "nw_tstat",
                           "oos_hit_rate", "fold_sign_consistency",
                           "net_pnl_1x_cost", "verdict")})
# EQ03 -> oos_ic 0.0256, nw_tstat 7.24, verdict ITERATE
EOF
```

Compare against the committed `research/alpha_reports/EQ03.json` — the run
is deterministic, so the numbers must match.

## 5. Run the full 24-alpha promotion report

```bash
PYTHONPATH=python/src python3 research/alpha_reports/run_all.py    # ~20 s
```

Outputs: `research/alpha_reports/REPORT.md` (the honest master table —
0 PROMOTE / 12 ITERATE / 12 REJECT on the bundled data), one JSON of full
evidence per alpha, the appending multiple-testing ledger
`research/experiments.json`, and refreshed day-1-fitted
`configs/strategies/alpha_params.json` (the port contract input —
regenerating goldens after a deliberate change is recipe territory for
API_ALPHA.md §6).

## 6. Train the ML zoo with tracked manifests

```bash
python3 research/ml_reports/run_ml.py        # < 5 min budget; ~1 min typical
```

This runs the gated comparison (linear baselines always; XGBoost/LightGBM/MLP
only if the best linear OOS IC is positive) and writes
`research/ml_reports/ML_REPORT.md`. Every fit is a tracked run:

```bash
python3 -m json.tool research/models/ledger.json | head
python3 -m json.tool research/models/run_0004_xgboost/manifest.json
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

Committed result: AUC 0.552, Brier 0.0683, test base rate of profitable
signals 0.073; both τ = 0.5 and the calibration-chosen best τ = 0.300 keep
**zero** trades — the calibrated gate declines every test signal, which at
this base rate is the economically defensible call (the gate-off row in
ML_REPORT.md shows −941.7 total net bps was left untraded). Thresholds are
chosen on the calibration segment, economically (LEARN.md §7.3). The pinned
meta model is `research/models/run_0021_metalabel_lightgbm/`.

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

The Rust engine is the reference; Java ports it. The golden replays a full
step sequence (orders, fills, market moves, gaps, venue outages, kill
switches) and pins every decision, deciding rule and severity:

```bash
cd rust && cargo test -p risk --test golden_risk     # reference
cd rust && cargo test -p risk --test rules           # per-rule unit tests
cd java && bash build.sh && rm -rf out/test && mkdir -p out/test && \
  find src/test/java -name '*.java' | sort > out/test-sources.txt && \
  javac -cp "out/main:/usr/share/java/junit4.jar:/usr/share/java/hamcrest-core.jar" \
        -d out/test @out/test-sources.txt && \
  java -cp "out/test:out/main:/usr/share/java/junit4.jar:/usr/share/java/hamcrest-core.jar" \
       org.junit.runner.JUnitCore com.iap.RiskGoldenTest
```

To understand a decision, read the step in
`tests/golden/expected_risk_decisions.json` — the deciding `rule_id` is the
first failing rule in the engine's pinned check order, and limits come from
`configs/risk.json` (x-version 2). Kill-switch operations in production
follow `docs/runbooks/RUNBOOK_incident_kill_switch.md`.

## 10. Run the execution simulator and the fills golden

C++ owns the pinned fill semantics (`cpp/include/iap/execution/execution.hpp`
— latency, aggressive depth walk, deterministic queue position, fees/impact);
the golden pins a passive VWAP parent and an aggressive IS parent on the
golden equity vector:

```bash
cd cpp && bash build.sh
ctest --test-dir build --output-on-failure -R 'ReplayFillsGolden|Exec'
```

Java must reproduce the same fills to the tick:

```bash
cd java && bash build.sh && bash test.sh    # includes ReplayFillsGoldenTest, ExecutionSimTest, AlgosTest
```

Read the expected economics (patience vs urgency — the passive parent earns
−$0.80 in rebates; the aggressive one pays $1.80 + impact):

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
        for r in json.load(open("configs/instruments.json"))["instruments"]}
frames = load_features("data/features")
models = load_params_file("configs/strategies/alpha_params.json")

bt = Backtester(CostModel.load("configs/execution.json"), meta, BacktestConfig())
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

The identity `total_pnl = gross_pnl − total_costs` holds exactly. Note this
demo scores the *full 2-day* frame — including the day the parameters were
fitted on — so it will not match the REPORT.md day-2 OOS table (net −60,061
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
# paper session: events=2000 orders=405 fills=600 pnl=-634420.053607 risk[allowed=405 rejected=33] port=8080 ...
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
# vector has none. There is NO fill counter on the Java endpoint; fill
# metrics — venue_fills_total etc. — come from the rust venue sim.
# The live adaptability gauges — drift PSI, rolling IC, lifecycle state —
# are scraped in recipe 19.)
```

The port comes from `configs/execution.json` `monitoring.port` (default
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
points from the committed run: IAP1 decode 3.5 ns/event, book update
17.4 ns, replay 37.1M events/s, feature engine ~450 ns/event, alpha scoring
32.3 ns/row.

## 14. Verify cross-language parity in one command

```bash
bash tests/harness/run_all.sh                 # full suites + parity table
bash tests/harness/run_all.sh --golden-only   # golden groups only (fast)
```

Exit code 0 iff every language passed; logs land in a temp dir printed on
the first line. A full-suite run: python 489 / cpp 175 /
rust 181 / java 315 tests passed (golden groups 49/37/36/13), all PASS.

## 15. Generate the TCA report

```bash
cd python && PYTHONPATH=src python3 -m iap.tca      # writes research/tca/TCA_REPORT.md
```

The report simulates a pinned 36-parent order set (SplitMix64 seed 20260829)
over the golden vectors and reports the full Perold decomposition, VWAP/TWAP
slippage, spread/impact/timing attribution and post-fill markouts. The exact
§2.2 records are separately golden-tested (`tests/golden/expected_tca.json`,
including the all-opportunity-cost unfilled case) in Python and Java.

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
IC-gated lifecycle (`configs/strategies.json` `adaptive`):

```bash
PYTHONPATH=python/src python3 research/adaptive_reports/run_adaptive.py
# alpha subset: ['EQ01', 'EQ03', 'EQ06', 'FX01', 'FX05', 'FX09', 'FX08', 'EQ09', 'FX11', 'FX10']
# EQ01: static:rf=1,pnl=-62365 ... drift_triggered:rf=1,pnl=-62365 (2.7s)
# ...
# Done in 30s -> research/adaptive_reports/ADAPTIVE_REPORT.md
```

Takes ~30 s and writes `research/adaptive_reports/ADAPTIVE_REPORT.md`
(the honest comparison + the FX10 false-positive case), per-alpha
evidence JSONs alongside it, drift baselines to `research/baselines/`
(the same files the Java live monitor consumes), every lifecycle
transition to `research/lifecycle_log.jsonl`, and 6,041 counted looks per run (12,082 recorded across the two committed study runs)
to `research/experiments.json`. Deterministic: a rerun reproduces every
number except the runtime line — but note the experiments ledger is
**append-only by design**, so reruns grow it (that is the multiple-testing
discipline working, not a bug). Read the report's "READ THIS FIRST"
section before quoting any policy ranking: on two synthetic sessions
there isn't one. Contract: `/API_ADAPTIVE.md`; concepts: LEARN.md §14.

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

`alpha_live_vs_backtest_drift` is the pinned PSI (API_ADAPTIVE.md §2) of
a rolling 256-value window of the live EQ01 signal (recomputed every 32
confident signals) against `research/baselines/signal_eq01.json` — the
Java-parity baseline; `alpha_rolling_ic` is the mean of event-time bucket
ICs over the trailing window, matured (lookahead-free) rows only;
`alpha_lifecycle_state` encodes 0 ACTIVE / 1 WATCH / 2 RETIRED, driven by
the pinned IC hysteresis of `configs/strategies.json` `adaptive.lifecycle`.
Early in the session the drift and IC gauges are absent rather than zero —
until the PSI window fills, or with fewer than `min_ic_buckets` (4)
buckets, the monitors report no value, which is itself pinned behavior
(no data must never read as no drift). The `LiveVsBacktestDrift` alert
(`deployment/prometheus/alerts.yml`) fires on PSI > 0.25, and the
Trading & Risk Grafana dashboard plots all three. Monitoring is
observational only — it never alters a trading decision mid-session, so
the summary line stays bit-for-bit reproducible (recipe 12).
