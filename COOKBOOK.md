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
37. [Work a parent order passively and read its markouts](#37-work-a-parent-order-passively-and-read-its-markouts)
38. [Combine alphas out of sample and count the independent bets](#38-combine-alphas-out-of-sample-and-count-the-independent-bets)
39. [Calibrate the simulator and run a maker-side backtest (v1.9)](#39-calibrate-the-simulator-and-run-a-maker-side-backtest-v19)
40. [Quote both sides with an alpha-skewed reservation price (v1.10)](#40-quote-both-sides-with-an-alpha-skewed-reservation-price-v110)

Recipes 27–35 were added with v1.3.0. Every command block in them was run
as printed, from a clean checkout of the release, before it was written
down; the quoted outputs are what those runs printed. Recipes that write
anything write under `data/store/` (git-ignored), never into the committed
ledger or `research/experiments/`.

Recipe 36 was added with the real-data ingestion path ([docs/REAL_DATA.md](docs/REAL_DATA.md)). It writes under `data/vendor/` and
`data/real/` (both git-ignored); its block was run as printed.

Recipes 37 and 38 were added with the execution-quality and the
signal-combination work of v1.5.0. Their Python blocks only read the
repository; the `research combine` command that closes recipe 38 is the one
that writes (`research/combination/` and the ledger).

v1.4.0 (2026-10-03) regenerated the seeded dataset (`data_version`
`116b7787…`, was `203c8f54…`; recipe 1). Every quoted output below that
depends on the dataset was re-run on the new dataset, or re-read from the
regenerated artefact where running the command would rewrite a committed
file. Two exceptions, both Java, are marked in recipes 12 and 19.

v1.5.0 (2026-10-04) left the dataset alone and changed the rules: the
corrected research methods that recipes 28–31 introduced as opt-ins are
the defaults, and every old rule is selectable under a legacy name
(`iap.validation.methods`: bundle `"v2"`, the default, and `"legacy_v1"`,
which reproduces a v1.4.0 number). Calling `BacktestConfig()`,
`CostModel.load(...)` or `validate_alpha(...)` without naming a rule now
gets the corrected rule, so several recipes print different numbers from
the same commands, and three need an argument they did not need before
(the label horizon for the cost-aware backtest, the ledger threshold for
`validate_alpha`). The read-only recipes were re-run on the v1.5.0 tree and
their outputs pasted; recipes that would rewrite a committed report, a
golden or the ledger (5, 6, 18, the `bootstrap` lines of 25, the `run`
lines of 26) were not run, and their flags and quoted outputs were updated
from the committed artefacts. Recipes 12 and 19 are unchanged from v1.3.0
for the reason they give.

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
runs). The same chain is run after a change of the default methods: the
committed v1.5.0 artefacts, like the v1.4.0 ones before them, were produced
by the manual
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
# since v1.9.0: one process per day, byte-identical output to the serial run
PYTHONPATH=src python3 -m iap.features --workers 4
```

`--workers N` (default 1 = serial; 0 = auto) replays each input file in its
own process and stitches the cross-day session profile back together in the
parent, so the Parquet files are byte-for-byte those of `--workers 1`. N is
clamped to the file count, the CPU count and `physical RAM / --worker-mem-gb`
(default 5 GB per worker).

Inspect instrument 1 (columns = registry features, NaN where invalid, plus
`label_mid_<h>` / `label_cost_<h>` / `label_valid_<h>` and, since v1.5.0,
`label_reason_<h>` / `label_reopen_<h>` — the realised reopen return the
default IC row policy scores a BLACKOUT row at):

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
`585dd7b9…` recorded in `data/features/features_summary.json`; the frame of
instrument 1 is 14,609 rows × 263 columns. Frames written by v1.4.0 have no
`label_reopen_<h>` columns: the validation then falls back to valid labels
and says `label_reopen_available: false`, so regenerate them.

## 4. Run one alpha's full validation

The same gauntlet `run_all.py` applies — walk-forward + purge/embargo,
leakage tests, decay curve, cost/latency/regime stress, verdict — for a
single alpha (EQ03 here), first under the default rules and then under the
rules up to v1.4.0, named:

```bash
PYTHONPATH=python/src python3 - <<'EOF'
import json
from iap.alpha import build
from iap.alpha.data import load_features
from iap.backtest import Backtester, BacktestConfig, CostModel
from iap.validation import validate_alpha
from iap.validation.ledger import ExperimentLedger
from iap.validation.methods import methods

meta = {int(r["instrument_id"]): {"symbol": r["symbol"],
        "asset_class": r["asset_class"], "tick_size": float(r["tick_size"]),
        "lot_size": int(r["lot_size"]), "adv": float(r["adv"]),
        "ref_price": float(r.get("ref_price", 1.0))}
        for r in json.load(open("configs/instruments/instruments.json"))["instruments"]}
frames = load_features("data/features")
exec_cfg = json.load(open("configs/execution/execution.json"))
max_part = float(exec_cfg["defaults"]["max_participation"])
costs = CostModel.load("configs/execution/execution.json")

# the default rules (v2): the PROMOTE t threshold is derived from a look count
looks = json.load(open("research/alpha_reports/EQ03.json"))["ledger_looks"]
ledger_t = ExperimentLedger("research/experiments.json").bonferroni_t_threshold_at(looks)
rep = validate_alpha(lambda: build("EQ03"), frames,
                     Backtester(costs, meta, BacktestConfig()), meta, max_part,
                     ledger_t_threshold=ledger_t, ledger_looks=looks)
print("looks:", looks, "PROMOTE t >=", round(ledger_t, 3))
print({k: rep[k] for k in ("gate_ic", "gate_tstat", "oos_rank_ic", "oos_hit_rate",
                           "fold_sign_consistency", "trade_count_1x_cost",
                           "net_pnl_1x_cost", "verdict")})

# the rules up to v1.4.0, by name
old = validate_alpha(lambda: build("EQ03"), frames,
                     Backtester(costs.with_linear_impact(), meta, BacktestConfig.legacy()),
                     meta, max_part, **methods("legacy_v1").validate_kwargs())
print({k: old[k] for k in ("oos_ic", "nw_tstat", "fold_sign_consistency",
                           "net_pnl_1x_cost", "verdict")})
# looks: 3936 PROMOTE t >= 4.365
# default -> gate_ic 0.0189, gate_tstat 5.16, oos_rank_ic 0.0211, oos_hit_rate 0.513,
#            fold_sign_consistency 1.0, trade_count_1x_cost 0, net_pnl_1x_cost 0.0,
#            verdict ITERATE
# legacy  -> oos_ic 0.0190, nw_tstat 5.86, fold_sign_consistency 1.0,
#            net_pnl_1x_cost -161585.48, verdict ITERATE
EOF
```

The default call needs one thing the legacy call does not:
`ledger_t_threshold`. The default PROMOTE gate is `max(3.0, the Bonferroni
|t| at the run's look count)`, and `validate_alpha` refuses to guess the
count — without the argument it raises and names
`tstat_threshold="fixed"` as the legacy rule. Here the count is the one
the committed report was judged at (3,936, recorded in `EQ03.json` as
`ledger_looks`); opening the ledger this way only reads it. The label
horizon the cost-aware backtest needs is set by `validate_alpha` itself.

Compare against the committed `research/alpha_reports/EQ03.json`: the run
is deterministic and the default block matches it — gate IC, gate t, rank
IC, hit rate, fold consistency, verdict, and a net P&L of exactly 0 on no
trade. EQ03 clears the t threshold and is ITERATE because its forecast
never exceeds its round-trip cost: a strategy that makes no trade has a
net P&L of 0, and 0 does not pass `net P&L > 0`. (This demo uses the
default `BacktestConfig()` where the report's backtester adds the pinned
research execution model — `latency_ns=1 s, max_decision_age_ns=60 s,
flatten_at_session_end=True`; with no trade the two cannot differ.)

The second block is the v1.4.0 recipe, reproduced to the cent: the sign
policy trades every row and loses 161,585.48 on the last fold (the v1.4.0
report, under the research execution model, said −148,562). 5.86 is
`nw_tstat`, the within-bucket t over all rows; the legacy gate read its
uncrossed-row form, 5.85 in today's report (`t other`).

## 5. Run the full 24-alpha promotion report

```bash
PYTHONPATH=python/src python3 research/alpha_reports/run_all.py    # ~105 s; the default methods (v2)
PYTHONPATH=python/src python3 research/alpha_reports/run_all.py \
  --methods legacy_v1 --out-dir /tmp/alpha-reports-legacy          # the v1.4.0 report, reproduced elsewhere
```

(Neither line was re-run for this page — the first rewrites the committed
reports — and the figures below are read from the committed
`REPORT.md`.) Outputs of the first: `research/alpha_reports/REPORT.md`
(the honest master table — 0 PROMOTE / 11 ITERATE / 13 REJECT on the
bundled data; at 1× costs 18 alphas make no trade, 6 trade and lose, none
ends above zero), one JSON of full
evidence per alpha, the appending multiple-testing ledger
`research/experiments.json`, and refreshed day-1-fitted
`configs/strategies/alpha_params.json` (the port contract input —
regenerating goldens after a deliberate change is recipe territory for
API_ALPHA.md §6). The ledger is scoped by dataset since v1.4.0 and by
method bundle since v1.5.0: on a dataset the ledger has already seen under
the same bundle, a rerun is recorded as a rerun, adds no looks and is
judged at the look count recorded the first time; a first run adds
24 × 84 = 2,016 (84 per alpha: 83 for the validation at four folds, one
for the day-2 backtest; 24 × 28 = 672 under `legacy_v1`). The committed
report was judged at a look count of 3,936 — the ledger before the run
plus its own 2,016 — which sets the PROMOTE t threshold at 4.365. After a
dataset change run the whole chain instead (recipe 1).

`--methods legacy_v1` needs `--out-dir`: `research/alpha_reports/` and
`alpha_params.json` are written by the default bundle only, and a run with
`--out-dir` keeps its own ledger there unless `--ledger` says otherwise.
`python/tests/test_legacy_methods.py` compares the legacy report field by
field with `tests/golden/alpha_report_{EQ03,FX01}_v1.4.0.json`, pinned from
the v1.4.0 tag.

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
failed (ridge −0.0430) and the trees and the MLP were never fitted. The
model comparison is not affected by the v1.5.0 method defaults — the table
of the v1.5.0 report equals the v1.4.0 one — and only the meta-label stage
changed (recipe 7). Every
fit is a tracked run — 47 so far; runs 0041–0047 are the v1.5.0 pass
(the script was not re-run for this page; it appends model runs):

```bash
python3 -m json.tool research/models/ledger.json | head
python3 -m json.tool research/models/run_0044_xgboost/manifest.json
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

Committed result: primary `ridge`, AUC 0.670, Brier 0.0567, test base
rate of profitable signals 0.062; both τ = 0.5 and the calibration-chosen
best τ = 0.300 keep **zero** of the 4,100 test signals. The report flags
this `gate_degenerate: true` and does not present it as an economic
decision — a gate that never fires is no evidence either way (the gate-off
row shows −746.7 total net bps for the ungated primary). Calibration here
is Platt scaling, not isotonic: the calibration segment has 255 positives,
below the pinned isotonic minimum of 500. Thresholds are chosen on the
calibration segment, economically (LEARN.md §7.3). The latest meta-label
run directory is `research/models/run_0047_metalabel_ridge/`. Since v1.5.0
a missing meta-feature value stays NaN for the tree model
(`build_meta_features(impute_nan=False)`, the default; 275 of 131,880
values on this data) where v1.4.0 imputed it to zero (`impute_nan=True`,
the legacy rule): the AUC and Brier score above were 0.655 and 0.0568
under the old rule, and the gate was degenerate under both.

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
cd python && PYTHONPATH=src python3 -m pytest -q tests/test_risk_golden.py tests/test_risk_rules.py   # Python port: 9 golden + 89 rule tests (98 passed)
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
spread + fee + impact costs, exact accounting identity). `BacktestConfig()`
is the v1.5.0 rule set — cost-aware positions, fills capped at the
displayed L1 size, only the rows the IC scores — and the cost-aware policy
has to know the label horizon the forecast is over, hence `for_horizon`:

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

m = models["EQ01"]
bt = Backtester(CostModel.load("configs/execution/execution.json"), meta,
                BacktestConfig()).for_horizon(m.horizon)     # cost-aware needs the label horizon
scores = m.score({i: frames[i] for i in m.universe(list(frames))})
res = bt.run(frames=frames, scores=scores, asset_class="EQUITY")
mx = res.metrics(capital=1_000_000)
print({k: round(v, 2) if isinstance(v, float) else v
       for k, v in mx.items()
       if k in ("total_pnl", "gross_pnl", "total_costs", "trade_count",
                "sharpe_ann", "max_drawdown")})
EOF
# {'total_pnl': 0.0, 'gross_pnl': 0.0, 'total_costs': 0.0, 'sharpe_ann': nan, 'max_drawdown': 0.0, 'trade_count': 0}
```

That output is the result, not a failure of the recipe: EQ01's fitted
expected return never exceeds the round-trip spread and fee of its own
row, so the cost-aware policy makes no trade in two sessions (the Sharpe of
an empty P&L series is NaN). Replace `"EQ01"` with `"EQ11"`, one of the
six alphas that do trade, and the same lines print 720 trades, gross
−994.00, costs 11,119.40, net −12,113.40. Without `for_horizon` the run
raises `ValueError: position_policy 'cost_aware' needs horizon_ns` and
names the two ways out; `BacktestConfig.legacy()` with
`CostModel.load(...).with_linear_impact()` is the v1.4.0 backtest (sign
policy, every row, uncapped fills, linear impact), which needs no horizon
and trades every row — recipe 28 runs both side by side.

The identity `total_pnl = gross_pnl − total_costs` holds exactly, in USD:
for FX instruments add `base_currency` / `quote_currency` to the meta (as
`research/alpha_reports/run_all.py` does) and include the conversion pairs
in `frames` — every P&L increment is converted at the prevailing pair mid
(`InstrumentResult.total_pnl_native` keeps the quote-currency figure) and a
non-USD increment with no prevailing rate raises rather than being summed
as dollars (conventions §11.6). Note this demo scores the *full 2-day* frame — including the day the parameters were
fitted on — where the REPORT.md day-2 OOS table splits by day with
`iap.alpha.data.split_by_day` first; for an alpha that trades the two
differ (EQ11: −12,113 here, −5,948 on day 2 in the report), for EQ01 both
are zero. The
pinned EQ01 backtest on the golden frame is golden-tested cross-language
(`tests/golden/expected_backtest.json`; C++/Java golden groups); that
vector stays on the legacy rules, which its `config` names, because the
Java research backtester implements only those. Full
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
line has not been re-captured for v1.4.0 or v1.5.0: the Java toolchain was
not available where this recipe was re-checked. v1.5.0 changes two more
things a session reports: its `config_sha256` — `execution.json`,
`lifecycle.json`, `strategies.json` and `alpha_params.json` are among the
hashed files — and its lifecycle gauge, which follows the CUSUM rule on a
pair-count-weighted rolling IC.)

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
the first line. The v1.9.0 counts (CI): python 2114 / cpp 302 /
rust 358 / java 571 tests passed (golden groups 192/72/71/124), plus
`integration` (35) and `replay` (6) rows for the repo-level pytest suites, a
`deployment` row (26 structural checks passed in CI, where `promtool` and
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
# EQ01: static:rf=1,pnl=0 ... drift_triggered:rf=1,pnl=0 (...s)
# ...
# Done in 63s -> research/adaptive_reports/ADAPTIVE_REPORT.md
```

(Those lines are read from the committed v1.5.0 report, which the
`regenerate` CI job wrote; the script was not re-run here because it
rewrites the report, the baselines and the ledger.) Takes about a minute and writes `research/adaptive_reports/ADAPTIVE_REPORT.md`
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
there isn't one. The study runs under the v1.5.0 defaults: the `v2`
backtest rules, the two-sample HAC z (`adaptive.ic_z_method = "hac"`) and
the CUSUM retirement rule (`adaptive.lifecycle.breach_rule = "cusum"`),
all named in `strategies.json`; `"legacy"` and `"consecutive"` are the
rules up to v1.4.0. Under them the drift-triggered policy fits 88 times in
total — 78 refits after the ten initial fits, of which 65 name a PSI
breach and 14 the rolling-IC z — where the v1.4.0 rules gave 122 on the
same dataset (126 on the v1.3.0 dataset). FX01 is retired under every
policy, as before. None of the 40 deployments ends above zero: 21 trade
and lose and 19 make no trade at all, because the forecast never clears
the round-trip cost. All P&L is in USD; the v1.4.0 report summed
quote-currency FX P&L as if it were USD (its policy totals of −8.69
million were wrong in scale), and the static policy now totals −3,921 USD.
The FX rows of the v1.4.0 report had differed from
the v1.3.0 report by about one trade per deployment although the FX data is
byte-identical: the v1.3.0 report had last been generated before the
2026-09-20 backtester corrections, so that difference was a code effect, not
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
re-captured, for the reason given in recipe 12. Since v1.5.0 the Java
rolling IC behind `alpha_rolling_ic` is the pair-count-weighted mean of the
bucket ICs, not the unweighted one, which moves the value again.)

`alpha_live_vs_backtest_drift` is the pinned PSI (API_ADAPTIVE.md §2) of
a rolling 256-value window of the live EQ01 signal (recomputed every 32
confident signals) against `research/baselines/signal_eq01.json` — the
Java-parity baseline; `alpha_rolling_ic` is the mean of event-time bucket
ICs over the trailing window, matured (lookahead-free) rows only;
`alpha_lifecycle_state` encodes 0 ACTIVE / 1 WATCH / 2 RETIRED, driven by
the pinned rule of `configs/strategies/strategies.json` `adaptive.lifecycle`
(the CUSUM rule since v1.5.0; `LifecycleGauge.legacyConsecutive(...)` is
the six-consecutive-breaches rule up to v1.4.0).
Early in the session the drift and IC gauges are absent rather than zero —
until the PSI window fills, or with fewer than `min_ic_buckets` (4)
buckets, the monitors report no value, which is itself pinned behavior
(no data must never read as no drift). The `LiveVsBacktestDrift` alert
(`deployment/prometheus/alerts.yml`) fires on PSI > 0.25, and the
Trading & Risk Grafana dashboard plots all three. Monitoring is
observational only — it never alters a trading decision mid-session, so
the summary line stays bit-for-bit reproducible (recipe 12).

## 20. Build the platform store and query it

The store (`schemas/sql/iap_v2.sql`, [docs/DATA_MODEL.md](docs/DATA_MODEL.md))
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
# experiment_results       39
# experiments              39
# instruments              19
# ledger_entries          216
# ledger_scopes             3
# lifecycle_transitions   372
# model_runs               47
# store_scope               1
# tca_orders               36
# venues                    5
# ...
```

Every importer is idempotent — run `build` again and the counts do not
move — and total over its input: a record it cannot map (a NaN metric, a
malformed line, an absent optional artefact) is a warning on stderr, never
a crash. A database built by an earlier release (data-model x-version 1) is
refused with exit code 2 and left untouched; `build --rebuild` deletes it
and builds it again, which is the whole migration.

Every research row is filed under the **scope** it was computed in — the
dataset (`dataset_version`) and the method bundle (`methods`: `v2` or
`legacy_v1`) — and the scorecard is one row per alpha and scope. The
`*_current` views are the current dataset under the default bundle. Query
with `sql` (one canonical JSON line per row; add `ORDER BY` for
deterministic output), or print a scope's scorecard with `scorecard`:

```bash
PYTHONPATH=src python3 -m iap.store sql "SELECT alpha_id, current_state, verdict, ROUND(ic,4) AS ic, ledger_count FROM v_alpha_scorecard_current ORDER BY alpha_id"
# {"alpha_id":"EQ01","current_state":"CANDIDATE","ic":-0.0019,"ledger_count":172,"verdict":"REJECT"}
# {"alpha_id":"EQ02","current_state":"CANDIDATE","ic":0.0251,"ledger_count":84,"verdict":"ITERATE"}
# ...  (24 rows)
PYTHONPATH=src python3 -m iap.store sql "SELECT alpha_id, substr(dataset_version, 1, 8) AS dataset, methods, is_current, verdict, ROUND(ic, 4) AS ic, ROUND(t_stat, 2) AS t_stat, ROUND(promote_t_threshold, 3) AS t_threshold, ledger_count, scope_looks FROM v_alpha_scorecard WHERE alpha_id = 'EQ03' ORDER BY dataset_version, methods"
# {"alpha_id":"EQ03","dataset":"116b7787","ic":0.0271,"is_current":0,"ledger_count":88,"methods":"legacy_v1","scope_looks":852,"t_stat":4.84,"t_threshold":3.0,"verdict":"ITERATE"}
# {"alpha_id":"EQ03","dataset":"116b7787","ic":0.0132,"is_current":1,"ledger_count":256,"methods":"v2","scope_looks":3236,"t_stat":2.41,"t_threshold":4.37,"verdict":"ITERATE"}
# {"alpha_id":"EQ03","dataset":"203c8f54","ic":0.0165,"is_current":0,"ledger_count":88,"methods":"legacy_v1","scope_looks":1068,"t_stat":4.25,"t_threshold":3.0,"verdict":"ITERATE"}
PYTHONPATH=src python3 -m iap.store sql "SELECT substr(dataset_version, 1, 8) AS dataset, methods, n_entries, looks, ROUND(bonferroni_t_threshold, 3) AS bonferroni_t FROM ledger_scopes ORDER BY dataset_version, methods"
# {"bonferroni_t":4.018,"dataset":"116b7787","looks":852,"methods":"legacy_v1","n_entries":69}
# {"bonferroni_t":4.322,"dataset":"116b7787","looks":3236,"methods":"v2","n_entries":77}
# {"bonferroni_t":4.071,"dataset":"203c8f54","looks":1068,"methods":"legacy_v1","n_entries":70}
PYTHONPATH=src python3 -m iap.store sql "SELECT COUNT(*) AS distinct_experiments, SUM(count) AS total_experiments FROM ledger_entries"
# {"distinct_experiments":216,"total_experiments":5156}     -- the Bonferroni denominator of the whole ledger
PYTHONPATH=src python3 -m iap.store scorecard | wc -l                                   # 24: the current scope
PYTHONPATH=src python3 -m iap.store scorecard --methods legacy_v1 | wc -l               # 24: same dataset, legacy bundle
PYTHONPATH=src python3 -m iap.store scorecard --dataset-version 203c8f54 --methods legacy_v1 | wc -l   # 24: the earlier dataset
PYTHONPATH=src python3 -m iap.store scorecard --all-scopes | wc -l                      # 72: three scopes x 24 alphas
```

(Re-run on the v1.5.0 tree, with `--db` pointing at a scratch file.)

How to read a scorecard row. Each row is one scope and every number in it
is of that scope alone: `ic`, `t_stat` and `verdict` are the latest
experiment result recorded in the scope (by `created_ts`, then the largest
experiment id), `gate_looks` and `promote_t_threshold` the ledger look count
and \|t\| threshold that result was judged at, `ledger_count` the alpha's
looks in the scope and `scope_looks` every look made in it. EQ03's three
rows above add up to the count of 432 the version-1 view printed on a single
row next to a legacy-rules IC; nothing is pooled any more. Two things to
keep in mind. First, EQ01, EQ03 and EQ06 also have `ExperimentRunner`
experiments, and their latest result in a scope is one of those (EQ01's
current row is `6e4a3431a3acf8a5`, IC −0.0019, REJECT); the alpha-report
pipeline's verdict is in `pipeline_verdict` / `pipeline_nw_tstat` /
`pipeline_t_threshold`, and that is the column the headline "11 ITERATE /
13 REJECT" counts (recipe 33). Second, the threshold is the one the result
was judged at, not a re-judgement: `legacy_v1` rows read the fixed 3.0,
`v2` rows `max(3.0, Bonferroni |t| at gate_looks)`, where `gate_looks`
counts every look the ledger held at the time, across datasets;
`scope_bonferroni_t` is what the scope alone would demand. The ledger total
(5,156 looks over 216 entries) is the sum of the three scopes: 1,068 on the
v1.3.0 dataset, 852 on the current dataset under the old rules, and 3,236
under the default rules (24 × 84 for the promotion report, 5 × 84 for the
runner experiments, 40 adaptive deployments, 8 × 95 for the signal-combination
experiments of recipe 38).

`sql` binds `:dataset_version` and `:methods` to the scope the command line
selects (the current one by default), and `build --ledger <path>` indexes
the ledger an ingested dataset keeps in its own directory — its rows form
their own scopes and are never added to the synthetic dataset's counts:

```bash
PYTHONPATH=src python3 -m iap.store sql --methods legacy_v1 "SELECT pipeline_verdict, COUNT(*) AS n FROM v_alpha_scorecard WHERE dataset_version = :dataset_version AND methods = :methods GROUP BY pipeline_verdict ORDER BY pipeline_verdict"
# {"n":10,"pipeline_verdict":"ITERATE"}
# {"n":14,"pipeline_verdict":"REJECT"}
```

From Python the same store is `iap.store.Store` (`open`, `init`,
`insert_<type>` for every contract, `fetch(T, **where)`, `query`,
`export_jsonl`); `Store.export_jsonl(table, path)` writes canonical lines in
primary-key order, so two builds from the same files are byte-identical.
The DDL runs unchanged on PostgreSQL ≥ 13 (`psql -f schemas/sql/iap_v2.sql`).

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
# mvp run 58a10f2194a3c81c: events=15805 decisions=800 parents=235 children=348 fills=169 pnl=-81.531396 USD digest=20d4ff76af0b631c... out=.../data/mvp/58a10f2194a3c81c
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
v1.5.0 changed none of that. The loop does not use the research
backtester, so its events, decisions, parents, children, fills and P&L are
identical to v1.4.0; what moved is `config_version` (`439bbad5…`, was
`f293e7e7…`), which hashes `execution.json` and `alpha_params.json`, and
with it the trace digest printed above (`f51890da…` in v1.4.0), because
every trace carries `config_version`. The summary line was re-run for this
page with `--out` pointing at a scratch directory; `replay` (recipe 23)
and `explain` (recipe 24) were re-run against that directory.
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
`net_pnl_after_costs`, its bootstrap bound `net_pnl_bootstrap_ci` and
`capacity`; for three of them (EQ02, EQ03, EQ12)
those are the only failed gates.

```bash
cd python
PYTHONPATH=src python3 -m iap.lifecycle bootstrap --dry-run      # compute, print, touch nothing
PYTHONPATH=src python3 -m iap.lifecycle bootstrap --force        # rewrite research/alpha_registry.json + lifecycle_transitions.jsonl (identical bytes on an identical rerun);
                                                                  # without --force a non-empty transition log (an append-only audit with any HUMAN retire/reset lines) is refused, exit 3
PYTHONPATH=src python3 -m iap.lifecycle status
# alpha | state | since_ts | failed gates
# ----- | ----- | -------- | ------------
# EQ01 | CANDIDATE | 1787691480577291027 | statistical_significance, net_pnl_after_costs, net_pnl_bootstrap_ci, capacity, stability
# EQ03 | CANDIDATE | 1787691480577291027 | net_pnl_after_costs, net_pnl_bootstrap_ci, capacity
# EQ04 | CANDIDATE | 1787691480577291027 | oos_ic, statistical_significance, fold_consistency, hypothesis_sign, net_pnl_after_costs, net_pnl_bootstrap_ci, capacity, stability
# FX09 | CANDIDATE | 1787691480577291027 | oos_ic, statistical_significance, fold_consistency, hypothesis_sign, net_pnl_after_costs, net_pnl_bootstrap_ci, capacity, stability
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
(live); the golden `tests/golden/expected_lifecycle.json` scripts four
lives under the default policy (LC01–LC04) and one under the rules up to
v1.4.0 (LG01) that Python, Java and Rust reproduce step by step
(`PYTHONPATH=src python3 -m pytest -q tests/test_lifecycle_golden.py`).
Changing a threshold is a `lifecycle.json` x-version bump + `python3
tools/make_golden_lifecycle.py --force` + a MIGRATIONS entry, never an edit
of the registry by hand (GOVERNANCE.md §1). The MVP's `paper_evidence.json`
(recipe 22) is the `PaperEvidence` document the PAPER → ACTIVE gates read.
The `status` table above was re-run on the v1.5.0 tree; the `bootstrap`,
`retire` and `reset` lines write the registry and were not.
The committed registry (`x-version` 2: each record carries the CUSUM
statistic) and transition log were rebuilt for the v1.5.0 methods with
`bootstrap --force`, as they had been for the v1.4.0 dataset; the earlier
logs are kept at
`research/archive/lifecycle_transitions.dataset-203c8f54.jsonl` (the
v1.3.0 dataset) and
`research/archive/lifecycle_transitions.dataset-116b7787.methods-legacy_v1.jsonl`
(the current dataset under the v1.4.0 rules). The history of the failed
gates, on three readings of the same 24 alphas: on the v1.3.0 dataset eight
failed only the cost gate (EQ01, EQ02, EQ03, EQ05, EQ06, EQ11, EQ12, FX04);
on the v1.4.0 dataset under the old rules, four (EQ02, EQ03, EQ12, FX04);
under the v1.5.0 defaults every alpha also fails `capacity` — the
edge-breakeven capacity of an alpha that makes no trade, or loses, is
below the 1,000,000 USD gate — and FX04 fails `statistical_significance`
against the ledger threshold (gate t 4.24 against 4.365), so three alphas
fail cost and capacity alone. Across the registry the counts are cost 24,
capacity 24, significance 21, stability 14, IC 12, fold consistency 10,
hypothesis sign 9.

## 26. Run one alpha as a contract-driven experiment

`iap.research` ([research/experiments/README.md](research/experiments/README.md),
LEARN.md §6.8) turns "run EQ03 at 1 s" into a typed `ExperimentSpec` whose
id is the hash of the request, runs the same purged / embargoed walk-forward
the promotion report runs plus a holdout backtest, writes a typed
`ExperimentResult`, and enters the run in the multiple-testing ledger:

```bash
cd python
# since v1.7.0 `run` needs a pre-registration; add --no-prereg for exploratory runs.
# A registered run whose IC has the opposite sign to the registered one exits 3 (v1.7.1).
PYTHONPATH=src python3 -m iap.research run --alpha EQ03 --horizon 1s               # spec block, result table, VERDICT, the t threshold, the ledger note
PYTHONPATH=src python3 -m iap.research run --alpha EQ03 --horizon 1s --dry-run     # no experiment directory is written; the looks ARE debited in the ledger
PYTHONPATH=src python3 -m iap.research run --alpha EQ06 --config n_folds=3 --config cost_multiplier=2.0   # a different configuration = a different id
PYTHONPATH=src python3 -m iap.research run --alpha EQ03 --methods legacy_v1        # the rules up to v1.4.0: another id, 28 looks, not gate-eligible
PYTHONPATH=src python3 -m iap.research list                                        # every research/experiments/<id>/ with alpha, horizon, dataset, verdict
PYTHONPATH=src python3 -m iap.research show 838e0c2d75de4db6                       # the spec and result documents (EQ03 @ 1 s, default methods)
```

The `run` lines write the committed ledger and were not re-run for this
page (recipe 29 runs one against a scratch ledger); `list` and `show` only
read and were. `--methods {v2,legacy_v1}` names the method bundle (`v2` is
the default); the `--tstat-threshold` flag of v1.3.0 and v1.4.0 is gone,
because the threshold policy is part of the bundle and the bundle is part
of the experiment id.

`list` prints a `dataset` column since v1.4.0. The committed folder holds
fifteen experiments: the five pinned requests on the v1.3.0 dataset
(`203c8f54`, kept as history), the same five on the current dataset
(`116b7787`) under the v1.4.0 rules, and the same five again under the
v1.5.0 defaults:

```
experiment        alpha   horizon  dataset           IC      NW t     net bps  verdict
00ebeb2b537b5155  EQ03    5s       116b7787   +0.026570    +4.213     +0.0000  ITERATE
20f1b9093e7d0d04  EQ06    10s      116b7787   +0.034528    +3.598  -1148.3084  ITERATE
217fa0cb1d89a9c8  EQ03    5s       203c8f54   +0.036305   +10.449  -1172.9449  ITERATE
4a2900e4a6705542  EQ06    1s       203c8f54   +0.019676    +3.509   -531.1169  ITERATE
695e7b1e2bd2253e  EQ01    1s       116b7787   -0.001674    +0.771   -531.3535  REJECT
6e4a3431a3acf8a5  EQ01    1s       116b7787   -0.001917    -0.221     +0.0000  REJECT
838e0c2d75de4db6  EQ03    1s       116b7787   +0.013243    +2.414     +0.0000  ITERATE
852863faa44b7b07  EQ06    1s       116b7787   +0.004702    +0.918  -1148.2478  REJECT
876b08e20c46e6fd  EQ03    1s       116b7787   +0.013610    +3.488  -2174.7262  ITERATE
9d895cf7148c4a8d  EQ06    1s       116b7787   +0.003345    +0.690     +0.0000  REJECT
c73bb6294d226163  EQ01    1s       203c8f54   +0.027590    +3.044   -301.4984  ITERATE
d0dd1ab0711d33a1  EQ06    10s      203c8f54   +0.050475    +6.755   -531.1169  ITERATE
d7b554d0a3fa3b26  EQ03    1s       203c8f54   +0.016471    +4.250  -1173.0925  ITERATE
d87e34a9c67c1891  EQ06    10s      116b7787   +0.034331    +3.711     -1.0134  ITERATE
f0f6c49b553f6b59  EQ03    5s       116b7787   +0.027114    +4.840  -2174.7262  ITERATE
```

The listing has no column for the method bundle (`show <id>` prints it in
the `configuration` line). The five default-method experiments are
`00ebeb2b537b5155` (EQ03 @ 5 s), `838e0c2d75de4db6` (EQ03 @ 1 s),
`6e4a3431a3acf8a5` (EQ01 @ 1 s), `9d895cf7148c4a8d` (EQ06 @ 1 s) and
`d87e34a9c67c1891` (EQ06 @ 10 s); the `NW t` column is the gate t, which is
the pooled-slope HAC t for those five and the within-bucket t for the
other ten.

From the v1.3.0 to the v1.4.0 dataset every pair moved the same way: a
smaller IC, a smaller t and a larger holdout loss, and EQ01 @ 1 s and
EQ06 @ 1 s went from ITERATE to REJECT. From the v1.4.0 to the v1.5.0
rules, on the same dataset, the five verdicts are unchanged (three
ITERATE, two REJECT) and the reasons moved: four of the five holdouts make
no trade and net exactly 0, EQ06 @ 10 s trades and nets −1.01 bps, and the
gate t fell for the two EQ03 runs (4.84 → 4.21 at 5 s, 3.49 → 2.41 at
1 s). No run is net-positive under either rule set on either dataset. The
dataset version and the method bundle are part of the specification, so
the same request on a regenerated dataset, or under the other bundle, is a
new experiment id and new looks.

Where the files land: `research/experiments/<id>/{spec.json,result.json}`,
sorted keys, 2-space indent, no wall clock — an identical rerun is
byte-identical (only `git_commit` and `n_experiments_in_ledger` are
provenance and may legitimately move), and a rerun that reproduces
*different* evidence under the same id is refused, not overwritten. An empty
configuration takes the alpha's pinned horizon, the pinned protocol and
the default methods (EQ03 @ 5 s is `00ebeb2b537b5155`; under the v1.4.0
rules it was `f0f6c49b553f6b59`). Its statistics are not the ones in
`research/alpha_reports/EQ03.json`, and are not meant to be: the runner
walks forward over the window before its declared holdout and reports the
holdout separately (IC +0.0266, t 4.21), while `run_all.py` walks forward
over both sessions and declares no holdout (gate IC 0.0189, t 5.16).
`python/tests/test_research_runner.py::test_eq03_report_reproduces_through_the_runner`
pins both the difference and that the runner equals `validate_alpha` called
on the runner's own window. Every new configuration adds
84 looks to `research/experiments.json` under the default methods (83 for
the validation at four folds, one for the holdout backtest; 28 under
`legacy_v1`; a rerun of an identical spec adds none), and since v1.3.0 a
`--dry-run`
debits them too — it evaluates and prints every statistic, so it is a look
(`check_headline_numbers.py` reports the docs stale until they follow the
ledger). A run is judged against the PROMOTE t threshold at its own look
count — the ledger before it plus its own looks — and the result prints
it: `00ebeb2b537b5155` was judged at a count of 4,272, a threshold of
4.383, so its t of 4.21 is not significant where the v1.4.0 run's 4.84
cleared 3.0. Runs made since v1.3.0 also write `eligibility.json` beside
the result (recipe 35); the ten current-dataset experiments have it, the
five v1.3.0-dataset experiments predate it. To
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
`validate_alpha` on the result. The committed report is 3 levels × 20 seeds
on 4 sessions (about 17 minutes on four cores); this is the smallest grid
that still has a null row, a planted row, a break row and a session curve
(three generator runs of two sessions, a few minutes):

```bash
cd python
PYTHONPATH=src python3 -m iap.research power --no-prereg --levels 0,1 --seeds 1 \
  --sessions 1,2 --power-out-dir ../data/store/power-tiny
# (progress on stderr)
# stable level 0 seed 850875211 (2 sessions): lead_lag:EQ10@1s=REJECT, lead_lag:EQ10@5s=REJECT, order_flow:EQ04@10s=REJECT, order_flow:EQ04@5s=REJECT
# stable level 1 seed 850875211 (2 sessions): lead_lag:EQ10@1s=ITERATE, lead_lag:EQ10@5s=ITERATE, order_flow:EQ04@10s=ITERATE, order_flow:EQ04@5s=ITERATE
# break level 1 seed 850875211 (2 sessions): lead_lag:EQ10@1s=REJECT, lead_lag:EQ10@5s=REJECT, order_flow:EQ04@10s=REJECT, order_flow:EQ04@5s=REJECT
# wrote ../data/store/power-tiny/POWER_REPORT.md and .../POWER_REPORT.json
```

The first table it prints for this grid (re-run on the detection-power
tree) — the reference effect by number of sessions:

```
| detector | sessions | at gate | at fixed 3.0 | at study | mean t | sd t | evidence | promote |
| lead_lag:EQ10@1s | 1 | 0/1 [0.00, 0.79] | 0/1 [0.00, 0.79] | 0/1 [0.00, 0.79] | +1.87 | n/a | 1/1 [0.21, 1.00] | 0/1 [0.00, 0.79] |
| lead_lag:EQ10@1s | 2 | 0/1 [0.00, 0.79] | 0/1 [0.00, 0.79] | 0/1 [0.00, 0.79] | +2.30 | n/a | 1/1 [0.21, 1.00] | 0/1 [0.00, 0.79] |
| lead_lag:EQ10@5s | 1 | 0/1 [0.00, 0.79] | 1/1 [0.21, 1.00] | 1/1 [0.21, 1.00] | +3.34 | n/a | 1/1 [0.21, 1.00] | 0/1 [0.00, 0.79] |
| lead_lag:EQ10@5s | 2 | 0/1 [0.00, 0.79] | 1/1 [0.21, 1.00] | 1/1 [0.21, 1.00] | +3.88 | n/a | 1/1 [0.21, 1.00] | 0/1 [0.00, 0.79] |
| order_flow:EQ04@5s | 1 | 0/1 [0.00, 0.79] | 0/1 [0.00, 0.79] | 0/1 [0.00, 0.79] | +2.80 | n/a | 1/1 [0.21, 1.00] | 0/1 [0.00, 0.79] |
| order_flow:EQ04@5s | 2 | 0/1 [0.00, 0.79] | 0/1 [0.00, 0.79] | 0/1 [0.00, 0.79] | +2.12 | n/a | 1/1 [0.21, 1.00] | 0/1 [0.00, 0.79] |
| order_flow:EQ04@10s | 1 | 0/1 [0.00, 0.79] | 0/1 [0.00, 0.79] | 0/1 [0.00, 0.79] | +2.33 | n/a | 1/1 [0.21, 1.00] | 0/1 [0.00, 0.79] |
| order_flow:EQ04@10s | 2 | 0/1 [0.00, 0.79] | 1/1 [0.21, 1.00] | 1/1 [0.21, 1.00] | +3.33 | n/a | 1/1 [0.21, 1.00] | 0/1 [0.00, 0.79] |
```

A detector is `effect:alpha@label horizon`: each planted effect is scored
by its alpha at the declared horizon and at the horizon matched to the
planted mechanism. Every rate is `detections/runs [Wilson 95 % interval]`;
with one seed the interval is 0.00–0.79 or 0.21–1.00, which is the report
telling you that one run settles nothing. `at gate` is the pooled-slope t
against the PROMOTE threshold in force (4.365), `at fixed 3.0` the same t
against 3, `at study` against the Bonferroni threshold of this study's own
40 tests (3.227).

On this seed nothing reaches the gate threshold on two sessions. The
order-flow effect at its declared 5 s label has a pooled t of 2.12 — the
number the previous report had for this seed — and 3.33 at the matched
10 s label; the lead-lag has 2.30 at 1 s and 3.88 at 5 s. The report then
prints the null rows (no detection; the order-flow t is negative with
nothing planted), the break rows (no evidence from the chain; the break z
of the order-flow detector at 5 s is +4.64, above the gate threshold), a
diagnosis table that accounts for the t, and a power model that a
three-run grid cannot fit meaningfully. The committed reports say what
more runs and more sessions do: at the reference size the order-flow
effect is detected at the gate threshold in 2 of 20 runs on two sessions,
10 of 20 on four and 20 of 20 on eight
([research/power/extended/POWER_REPORT.md](research/power/extended/POWER_REPORT.md));
the lead-lag in none at its declared 1 s label and in 16 of 20 at 5 s on
eight sessions; nothing is detected on the null and nothing is promoted
anywhere.

Read it the way the report tells you to: level 0 is the false-positive row;
`stable` rows are power; `break` rows plant an effect that reverses
mid-sample. This run shows the mechanics; the committed twenty-seed reports
are the ones to quote. `--jobs N` sets the number of worker processes (the
report does not depend on it), `--sessions`, `--levels`, `--break-levels`
and `--seeds` the grid. Never pass `--power-out-dir research/power` unless
you mean to replace the committed report; the study itself never touches
`data/` or the research ledger. The reference effect is
`research/power/generator_planted.json` (deliberately outside `configs/`,
which is shipped to the pods).

## 28. Backtest with the cost-aware position policy and compare with the default

(The heading is the v1.3.0 one, kept because other documents link to it.
Since v1.5.0 the cost-aware policy *is* the default, and what it is
compared with below is the legacy sign policy.)

Up to v1.4.0 the research backtest took `sign(expected_return)` and
re-decided on every row, so an alpha with a real but small signal traded
constantly and paid the spread each time: its loss measured the policy as
much as the alpha. The `cost_aware` policy (`BacktestConfig`,
docs/RESEARCH_VALIDITY.md §1; added as an opt-in in v1.3.0, default since
v1.5.0) enters only when the
expected return exceeds the round-trip cost of that row, holds for the
label horizon, and renews only above `hysteresis ×` that cost (0.5). The
legacy rules have names — `BacktestConfig.legacy()` is the sign policy on
every row with uncapped fills, `CostModel.with_linear_impact()` the linear
impact — and together they reproduce a v1.4.0 number:

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
m = load_params_file("configs/strategies/alpha_params.json")["EQ03"]
scores = m.score({i: frames[i] for i in m.universe(list(frames))})
costs = CostModel.load("configs/execution/execution.json")     # impact_model "sqrt"

for name, cost_model, cfg in (
    ("cost_aware (default)", costs, BacktestConfig().for_horizon(m.horizon)),
    ("sign (legacy)", costs.with_linear_impact(), BacktestConfig.legacy()),
):
    res = Backtester(cost_model, meta, cfg).run(frames=frames, scores=scores, asset_class="EQUITY")
    print(f"{name:21s} trades={res.trade_count:6d} gross={res.gross_pnl:12.2f} "
          f"costs={res.total_costs:12.2f} net={res.total_pnl:12.2f}")
EOF
# cost_aware (default)  trades=     0 gross=        0.00 costs=        0.00 net=        0.00
# sign (legacy)         trades= 38222 gross=     4695.00 costs=   831621.36 net=  -826926.36
```

The honest reading: the legacy policy loses 826,926.36 because it pays
831,621.36 of costs to collect 4,695.00 of gross; the cost-aware policy
loses nothing because it never trades — EQ03's fitted expected return does
not clear its own round-trip cost on a single row in two sessions (on the
v1.3.0 dataset it did on four). That is not a profitable strategy found,
and a policy that makes no trade is no evidence that abstaining pays; it is
the same finding (real signal, smaller than the spread) stated from the
other side. It is also why the default changed: the six-figure loss was a
property of trading every row, and the report now states the finding
directly — in `REPORT.md` 18 of the 24 alphas make no trade at 1× costs,
and their net P&L of exactly 0 fails `net P&L > 0` just as the loss did.
The legacy line equals the v1.4.0 output of this recipe to the cent. Like
recipe 11 this scores the full two-day frame, including the day the
parameters were fitted on, so neither number is the report's OOS figure.
`cost_aware` needs the label horizon (`for_horizon`, or `horizon_ns`); the
two neighbouring defaults are
`cap_fills_at_l1=True` (no fill larger than the displayed L1 size) and
`block_rows_column="auto"` (trade only the rows the IC is
measured on at that horizon; a column name selects another mask, `None`
trades every row).

## 29. Judge an experiment against the ledger-derived t threshold

Up to v1.4.0 the PROMOTE gate asked for a t of 3.0 whatever the ledger
said, and the ledger-derived threshold was an opt-in
(`--tstat-threshold ledger`; that flag no longer exists). Since v1.5.0 it
is the default and needs no flag: the gate is `max(3.0, the Bonferroni t
at the run's look count)`, where the look count is the ledger before the
run plus the looks the run adds. It can only tighten. The fixed 3.0 is the
legacy rule, and it is selected with the bundle it belongs to,
`--methods legacy_v1`. Both runs below use a scratch copy of the ledger and
a scratch experiments folder, so nothing committed moves:

```bash
mkdir -p data/store/scratch && cp research/experiments.json data/store/scratch/ledger.json
cd python
PYTHONPATH=src python3 -m iap.research --out-dir ../data/store/scratch/experiments run \
  --alpha EQ03 --ledger ../data/store/scratch/ledger.json
PYTHONPATH=src python3 - <<'EOF'
from iap.validation.ledger import ExperimentLedger
from iap.validation.validate import effective_gates

ledger = ExperimentLedger("../data/store/scratch/ledger.json")
t = ledger.bonferroni_t_threshold()
print("looks:", ledger.total_experiments, "bonferroni |t|:", round(t, 3))
print("this run's count, 4272:", round(ledger.bonferroni_t_threshold_at(4272), 3))
print("ledger min_nw_tstat:", round(effective_gates("ledger", t)["min_nw_tstat"], 3))
print("fixed  min_nw_tstat:", effective_gates("fixed")["min_nw_tstat"])
EOF
PYTHONPATH=src python3 -m iap.research --out-dir ../data/store/scratch/experiments run \
  --alpha EQ03 --methods legacy_v1 --ledger ../data/store/scratch/ledger.json
# experiment         00ebeb2b537b5155
# ... t-stat (the gate's)             +4.2132 ... holdout net return (bps)        +0.0000 ...
# VERDICT: ITERATE
# PROMOTE t threshold: 4.3830 (ledger ...)
# gate eligible: yes
# looks: 5156 bonferroni |t|: 4.424
# this run's count, 4272: 4.383
# ledger min_nw_tstat: 4.424
# fixed  min_nw_tstat: 3.0
# experiment         47e6cdacc73e85fe
# ... t-stat (the gate's)             +4.8405 ... holdout net return (bps)        -2174.7262 ...
# VERDICT: ITERATE
# PROMOTE t threshold: 3.0000 (fixed)
# gate eligible: NO — recorded and ledgered, but not promotion evidence
#   - configuration.methods='legacy_v1' must be 'v2' for a gate-eligible result
```

(Re-run on the v1.5.0 tree with a scratch ledger and folder; the quoted
lines are excerpts of each run's output, and the parenthesis of the first
threshold line is shortened — in full it names the look count the
threshold was derived at.)

EQ03 at its pinned horizon under the default methods is the committed
experiment `00ebeb2b537b5155`, so the first run is a rerun: it adds no
looks and is judged at the look count recorded the first time, 4,272, not
at today's total — a result is not re-judged when the ledger grows. Its t
of 4.21 is below the 4.383 that count implies. So this experiment is not
significant under the default rule, where the same request cleared the
fixed 3.0 under the v1.4.0 rules with a t of 4.84; part of that fall is
the statistic (the gate now reads the pooled-slope t) and part is the
threshold. The verdict is ITERATE either way, and under the defaults the
holdout makes no trade. The promotion report's EQ03, walked forward over
both sessions, has a gate t of 5.16 against its own threshold of 4.365 and
does clear it (recipe 4).

The second run is a new experiment in the scratch ledger: the bundle name
is part of the configuration, so `--methods legacy_v1` is not a rerun of
the committed `f0f6c49b553f6b59` (which predates the bundles and names
none) although it reproduces every one of its statistics. It adds 28
looks — re-running the python block afterwards prints 4424 and 4.391 — and
its result is marked not gate-eligible: a legacy-rule result is history,
not promotion evidence. From Python the default is
`validate_alpha(..., ledger_t_threshold=t)` (recipe 4) and the legacy rule
`validate_alpha(..., tstat_threshold="fixed")`; for the runner the bundle
is a key of the specification,
`build_spec("EQ03", configuration={"methods": "legacy_v1"}, ...)`
(recipe 35). A new configuration is
judged against the threshold the ledger will have once its own 84 looks are
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
would never notice, and the recompute probe names the column. (Re-run on
the v1.5.0 tree; the output is unchanged.) Cost is one feature rebuild per
anchor, which is why it was opt-in up to v1.4.0. Since v1.5.0 it is part of
the standard suite: `LeakageTester.run(model, frames, recompute=source)`
runs it when it is given the raw events, and `validate_alpha` passes them
through (`recompute=`). The report pipelines keep the cost down by probing
three anchors on the first 12,000 events of the first normalized file per
asset class, memoised per run (`RecomputeSources`); all 24 alpha reports
say `recompute_ok: true`. Without a source the detector does not run, the
report says `recompute_ok: null`, and the experiment runner marks the
result not gate-eligible. Pass `model=None` to probe the features alone.

## 31. Per-fold diagnostics with a bootstrap interval

The gates read cost survival on the last walk-forward fold only, and one
number for net P&L. `iap.validation.diagnostics` computes cost survival,
decay and the regime split for every fold and puts a
stationary-bootstrap interval (Politis & Romano; SplitMix64, seeded) around
the pooled net P&L. Since v1.5.0 `validate_alpha` reports that block itself
(`fold_diagnostics`, `net_pnl_bootstrap`; the "Every fold" table of
`REPORT.md`), and its looks are counted — it is most of why a validation
is 83 looks and not 27. The interval is also what the lifecycle gate
`net_pnl_bootstrap_ci` reads (docs/LIFECYCLE.md §3: its lower bound must be
above zero, and an alpha that makes no trade fails); the per-fold rows stay
report-only. The standalone form below is for a caller that wants the diagnostics
without the rest of the validation; it is run once under the default rules
and once under the legacy ones:

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
costs = CostModel.load("configs/execution/execution.json")

for name, bt, ic_rows in (
    ("default rules", Backtester(costs, meta, BacktestConfig()).for_horizon("5s"),
     "blackout_reopen"),
    ("legacy rules", Backtester(costs.with_linear_impact(), meta, BacktestConfig.legacy()),
     "valid_only"),
):
    d = fold_diagnostics(lambda: build("EQ03"), frames, bt, seed=20260919, n_boot=300,
                         ic_rows=ic_rows)
    print(name)
    for f in d["folds"]:
        print("  fold", f["fold"], "pairs", f["n_test_pairs"],
              "net@1x", round(f["net_pnl_by_cost"]["x1"], 2),
              "survives 1x:", f["survives_1x_cost"])
    print("  folds surviving 1x cost:", d["n_folds_survive_1x_cost"], "of", d["n_folds_run"])
    b = d["net_pnl_bootstrap"]
    print("  pooled net P&L:", round(d["net_pnl_1x_pooled"], 2),
          "95% CI:", [round(b["ci_low"], 2), round(b["ci_high"], 2)],
          "mean block:", b["mean_block"], "resamples:", b["n_boot"])

# the bootstrap on its own: a seeded interval for the SUM of a dependent series
print(stationary_bootstrap_ci([1.0, -2.0, 0.5, 3.0, -1.0, 0.25, 2.0, -0.5, 1.5, -0.75],
                              seed=7, n_boot=200))
EOF
# default rules
#   fold 1 pairs 32029 net@1x 0.0 survives 1x: False
#   fold 2 pairs 31806 net@1x 0.0 survives 1x: False
#   fold 3 pairs 31982 net@1x 0.0 survives 1x: False
#   fold 4 pairs 31951 net@1x 0.0 survives 1x: False
#   folds surviving 1x cost: 0 of 4
#   pooled net P&L: 0.0 95% CI: [0.0, 0.0] mean block: 9.0 resamples: 300
# legacy rules
#   fold 1 pairs 31980 net@1x -174562.22 survives 1x: False
#   fold 2 pairs 31735 net@1x -168827.42 survives 1x: False
#   fold 3 pairs 31891 net@1x -165593.72 survives 1x: False
#   fold 4 pairs 31887 net@1x -161585.48 survives 1x: False
#   folds surviving 1x cost: 0 of 4
#   pooled net P&L: -670568.83 95% CI: [-692392.14, -646896.44] mean block: 9.0 resamples: 300
# {'estimate': 4.0, 'ci_low': -2.75, 'ci_high': 10.75, 'level': 0.95, 'n': 10, 'n_boot': 200, 'mean_block': 2.0, 'seed': 7, 'frac_resamples_le_zero': 0.175}
```

Under the default rules EQ03 makes no trade in any fold: every net is 0,
no fold "survives" (survival is `net > 0`, and 0 is not), and the
interval is the degenerate [0, 0]. That is a result with no dispersion to
bootstrap, not a narrow confidence interval, and it should be read as "no
trade", never as "break-even". The pair counts are a little larger than
under the legacy rules because the default IC row policy also scores the
BLACKOUT rows at their reopen return. Under the legacy rules — the v1.4.0
output of this recipe, reproduced — EQ03 loses money after costs in every
fold, not only the last, and the whole interval is below zero: the
negative result was not one unlucky fold. For an alpha that does trade
under the defaults, see the "Every fold" table of `REPORT.md` (EQ11:
pooled −3,174, interval −5,142 to −1,497).
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
PYTHONPATH=src python3 -m iap.research show 00ebeb2b537b5155 --json | python3 -c "import json,sys; d=json.load(sys.stdin); print(sorted(d)); print(d['gate_eligibility'])"
PYTHONPATH=src python3 -m iap.research --json-errors show 0000000000000000; echo "exit=$?"
PYTHONPATH=src python3 -m iap.research --json-errors run --horizon 1s; echo "exit=$?"
# 15 experiments, 0 skipped
# [('00ebeb2b537b5155', 'EQ03', 'ITERATE', True), ('20f1b9093e7d0d04', 'EQ06', 'ITERATE', False), ('217fa0cb1d89a9c8', 'EQ03', 'ITERATE', False), ('4a2900e4a6705542', 'EQ06', 'ITERATE', False), ('695e7b1e2bd2253e', 'EQ01', 'REJECT', False), ('6e4a3431a3acf8a5', 'EQ01', 'REJECT', True), ('838e0c2d75de4db6', 'EQ03', 'ITERATE', True), ('852863faa44b7b07', 'EQ06', 'REJECT', False), ('876b08e20c46e6fd', 'EQ03', 'ITERATE', False), ('9d895cf7148c4a8d', 'EQ06', 'REJECT', True), ('c73bb6294d226163', 'EQ01', 'ITERATE', False), ('d0dd1ab0711d33a1', 'EQ06', 'ITERATE', False), ('d7b554d0a3fa3b26', 'EQ03', 'ITERATE', False), ('d87e34a9c67c1891', 'EQ06', 'ITERATE', True), ('f0f6c49b553f6b59', 'EQ03', 'ITERATE', False)]
# ['experiment_id', 'gate_eligibility', 'result', 'spec']
# {'gate_eligible': True, 'methods': 'v2', 'periods_verified': True, 'reasons': [], 'significance_threshold': 4.383027751177569, 'threshold_looks': 4272}
# {"error": {"code": "experiment_not_found", "message": ".../research/experiments/0000000000000000: no such experiment"}}
# exit=1
# {"error": {"code": "usage_error", "message": "the following arguments are required: --alpha"}}
# exit=2
```

(Re-run on the v1.5.0 tree.) `list --json` is
`{"experiments": [...], "skipped": [...]}`: a directory
that cannot be loaded (half-written, corrupt) is named in `skipped` with
the reason instead of failing the listing. The eligibility block gained
three keys in v1.5.0: `methods`, and the `significance_threshold` the run
was judged at with the look count it came from (`threshold_looks`). Only
the five default-method experiments are gate-eligible. The other ten show
`gate_eligible: False` with the reason "the spec predates the v1.5.0
method bundles (no configuration.methods): its result was computed under
the legacy methods" — they were eligible when v1.4.0 was released; they
are history now, not evidence. Of those ten, the five on the current
dataset carry the `eligibility.json` sidecar and show
`periods_verified: True`; the five v1.3.0-dataset experiments (`show
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
PYTHONPATH=src python3 -m iap.store sql "SELECT current_state, COUNT(*) AS n FROM v_alpha_scorecard_current GROUP BY current_state ORDER BY current_state"
PYTHONPATH=src python3 -m iap.store sql "SELECT pipeline_verdict, COUNT(*) AS n FROM v_alpha_scorecard_current GROUP BY pipeline_verdict ORDER BY pipeline_verdict"
PYTHONPATH=src python3 -m iap.store sql "DELETE FROM alphas"; echo "exit=$?"
PYTHONPATH=src python3 -m iap.store sql "SELECT 1; SELECT 2"; echo "exit=$?"
PYTHONPATH=src python3 -m iap.store sql "SELECT * FROM no_such_table"; echo "exit=$?"
# {"current_state":"CANDIDATE","n":24}
# {"n":11,"pipeline_verdict":"ITERATE"}
# {"n":13,"pipeline_verdict":"REJECT"}
# error: attempt to write a readonly database (the store is opened read-only; use `build` to rebuild it)
# exit=1
# error: `sql` runs exactly one statement; several were given (You can only execute one statement at a time.)
# exit=1
# error: no such table: no_such_table
# exit=1
```

The first two rows are the platform's headline result as a query: every
alpha at CANDIDATE, none promoted. (Re-run on the v1.5.0 tree against a
scratch `--db`. The 11 / 13 split is the v1.5.0 promotion report's:
`pipeline_verdict` is the alpha-report pipeline's verdict in the current
scope — the current dataset under the default methods. The view's `verdict`
column is the alpha's latest experiment result in that scope, which for
EQ01, EQ03 and EQ06 is a runner experiment and reads 10 / 14 — recipe 20.)
The three failures are the three ways a
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
session flattening on, periods equal to the ones derived from the
dataset's session calendar and, since v1.5.0, the default method bundle
(`methods = "v2"`). Halving the costs, choosing the holdout by
hand, or running the legacy rules, still runs — and is marked:

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

legacy = build_spec("EQ03", configuration={"methods": "legacy_v1"}, frames=frames)
e = gate_eligibility(legacy, frames)                             # the v1.4.0 rules, by name
print(legacy.experiment_id, "eligible:", e.eligible)
for reason in e.reasons:
    print("  -", reason)

# a persisted run: the sidecar if the runner wrote one, else configuration bounds only
reg = ExperimentRegistry("research/experiments")
print(reg.gate_eligibility("00ebeb2b537b5155"))
print(reg.gate_eligibility("f0f6c49b553f6b59"))
EOF
# 00ebeb2b537b5155 GateEligibility(eligible=True, reasons=(), periods_verified=True, methods='v2', significance_threshold=None, threshold_looks=None)
# 917a7c8ee7eba7ff eligible: False
#   - configuration.n_folds=2 is below the gate-eligible minimum 4
#   - configuration.cost_multiplier=0.5 is below the gate-eligible minimum 1.0
# 47e6cdacc73e85fe eligible: False
#   - configuration.methods='legacy_v1' must be 'v2' for a gate-eligible result
# GateEligibility(eligible=True, reasons=(), periods_verified=True, methods='v2', significance_threshold=4.383027751177569, threshold_looks=4272)
# GateEligibility(eligible=False, reasons=('the spec predates the v1.5.0 method bundles (no configuration.methods): its result was computed under the legacy methods',), periods_verified=True, methods=None, significance_threshold=None, threshold_looks=None)
```

(Re-run on the v1.5.0 tree.) A specification has no threshold until it is
run, which is why the first line says `significance_threshold=None`; the
sidecar of the persisted run of the same specification (the fourth
`GateEligibility`) carries the threshold it was judged at and the look
count behind it. The last line is the same request as run under v1.4.0:
its sidecar said eligible then, and the reader now refuses it because the
spec names no method bundle. Asked
about a v1.3.0-dataset run (`217fa0cb1d89a9c8`), which has no sidecar, the
registry answers the same and `periods_verified=False`. The ids of the
second and third specifications differ from the ones v1.4.0 printed
because `methods` is hashed into the id.

The runner writes the determination to `eligibility.json` beside
`result.json`, `python -m iap.research run` prints it (`gate eligible: yes`
or `NO` with the reasons), and `show --json` / `list --json` carry it
(recipe 32). In the lifecycle, `Evidence(research_gate_eligible=False)`
makes every gate that reads the research block fail with a null value,
exactly as if the number were missing; the flag is serialised only when
false, so evidence that is eligible does not carry it.
The Java and Rust lifecycle ports do not read the key and reject a document
that carries it. (The evidence document did gain two required keys in
v1.5.0, `significance_threshold` and `live.new_fraction`, which all three
readers require.) LEARN.md §24 explains which ways of gaming the gate this
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
# --no-prereg: a demo, not a hypothesis (a real study registers it first, §26)
PYTHONPATH=src python3 -m iap.research run --alpha EQ03 --dataset-dir ../data/real/demo \
  --no-prereg | sed -n '1,5p;/VERDICT/p'
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
entries of its looks carry it, and the ledger they went into is
`data/real/demo/research/experiments.json` — `research/experiments.json`
in the checkout did not move. Ingesting the same bytes again gives the
same `dataset_version` and byte-identical files.

The REJECT means nothing: the encoder's order flow is uniform random, so
there is nothing to find. With real files the verdict is about that
dataset, under the reading rules of docs/REAL_DATA.md §8 — two sessions
are two sessions. `rm -rf data/vendor/demo_* data/real/demo` removes
everything this recipe wrote.

## 37. Work a parent order passively and read its markouts

`ParentOrder.policy` selects the child execution policy (API_TRADING.md
§2.5): `NATIVE` (default), `AGGRESSIVE`, or `PASSIVE` — post at the near
touch, rest for a patience set by `urgency`, reprice once, cross the rest.
`iap.tca.markout` then tells you what each fill was worth afterwards
(API_PORTFOLIO_TCA.md §2.9). One TWAP parent on the golden equity vector
under each policy:

```bash
PYTHONPATH=python/src python3 - <<'EOF'
from dataclasses import replace
from pathlib import Path
from iap.core.codec import read_jsonl
from iap.execution import (AlgoType, ExecConfig, ExecPolicy, ExecutionReplay,
                           InstrumentSpec, Liquidity, ParentOrder, load_venues)
from iap.tca.markout import MarkoutFill, build_gated_timeline, markout_report

SEC = 1_000_000_000
events = read_jsonl(Path("tests/golden/events_eq_mbo.jsonl"))
t0 = events[0].exchange_ts
cfg = ExecConfig(seed=20260829, venues=load_venues("configs/venues/venues.json"),
                 instruments={1: InstrumentSpec(1, 0.01, 1.0, 38_000_000.0)})
parent = ParentOrder(parent_id=1, instrument_id=1, venue_id=1, side=0, qty=1200,
                     algo=AlgoType.TWAP, start_ts=t0 + 60 * SEC, end_ts=t0 + 660 * SEC,
                     slices=12)
timeline = build_gated_timeline(events, 1, 0.01)
for policy in (ExecPolicy.AGGRESSIVE, ExecPolicy.NATIVE, ExecPolicy.PASSIVE):
    res = ExecutionReplay(cfg, [replace(parent, policy=policy, urgency=0.5)]).run(events)
    r = res.parents[1]
    fills = [MarkoutFill(f.ts, f.price_ticks * 0.01, f.qty, f.side,
                         "MAKER" if f.liquidity == Liquidity.MAKER else "TAKER",
                         f.venue_id, "TWAP") for f in res.fills]
    rep = markout_report(fills, timeline, min_fills=3)
    print(f"{policy.name:10s} filled {r.filled_qty:4d}/{parent.qty} avg {r.avg_price:.4f} "
          f"fees {r.fees:.3f} rebates {r.rebates:.3f}")
    for liq, cell in rep["by_liquidity"].items():
        row = cell["horizons"]
        print(f"   {liq} n={cell['n_fills']:2d} markout bps:",
              {h: None if row[h]["markout_bps"] is None else round(row[h]["markout_bps"], 2)
               for h in ("1s", "30s", "5min")})
    if res.passive:
        print("  ", res.passive[1].to_dict())
EOF
```

```
AGGRESSIVE filled 1200/1200 avg 24.5242 fees 3.600 rebates 0.000
   TAKER n=12 markout bps: {'1s': -5.27, '30s': -5.27, '5min': -2.89}
NATIVE     filled  600/1200 avg 24.5033 fees 0.000 rebates 1.200
   MAKER n= 6 markout bps: {'1s': -0.34, '30s': 1.02, '5min': 7.14}
PASSIVE    filled 1100/1200 avg 24.5191 fees 2.100 rebates 0.800
   MAKER n= 4 markout bps: {'1s': -1.02, '30s': -2.55, '5min': 4.08}
   TAKER n= 7 markout bps: {'1s': -4.37, '30s': -4.66, '5min': -3.49}
   {'posts': 15, 'reprices': 3, 'rest_extensions': 6, 'crosses_timeout': 7, 'crosses_behind': 0, 'crosses_immediate': 0}
```

`markout = s × (mid(t + h) − price)`: positive = the fill looks good `h`
later. A markout whose window runs past the data, or across a halt or a
quote gap, is `None`, and so is a cell with fewer than `min_fills` defined
fills. NATIVE has the best average price and half the quantity: the unfilled
600 shares are a cost the average does not show.
`python3 research/execution/run_execution_study.py` prices it (bundled
dataset required: `cd python && PYTHONPATH=src python3 -m iap.marketdata`),
and writes `research/execution/EXECUTION_REPORT.md`. The same policy for the
MVP: `MvpConfig.with_overrides(child_policy="passive", passive={...})`.
LEARN.md §30 explains adverse selection and how to read the table.

## 38. Combine alphas out of sample and count the independent bets

A combination is an alpha whose inputs are alphas (`iap.combine`,
API_ALPHA.md §8). `fit` estimates the weights inside the training rows it
is given — on the members' out-of-sample predictions from three inner
walk-forward folds — so fitting on day 1 and scoring day 2 is honest. Four
equity members, three of which are order-flow alphas:

```bash
PYTHONPATH=python/src python3 - <<'EOF'
import numpy as np
from iap.alpha.data import load_features, session_days, split_by_day
from iap.combine import CombinedAlpha, correlation_matrix, effective_bets, member_signal
from iap.validation.metrics import ic

frames = load_features("data/features")
train, test = split_by_day(frames, session_days(frames)[1])
members = ["EQ02", "EQ03", "EQ06", "EQ12"]
for method in ("equal_weight", "ridge"):
    model = CombinedAlpha(members, method)
    model.fit(train)  # weights from the members' out-of-sample stack inside day 1
    p = model.params()
    scores = model.score(test)
    z = np.concatenate([s["expected_return"].to_numpy() / p["beta"] for s in scores.values()])
    y = np.concatenate([test[i][f"label_mid_{model.horizon}"].to_numpy() for i in scores])
    w = " ".join(f"{a} {v:+.3f}" for a, v in p["weights"].items())
    print(f"{method:<13} horizon {model.horizon}  weights: {w}  day-2 IC {ic(z, y):+.4f}")
universe = model.universe(list(test))
signals = [member_signal(model.members[a], test, universe) for a in members]
stack = np.column_stack([np.concatenate([s[i] for i in universe]) for s in signals])
corr, _ = correlation_matrix(stack)
bets = effective_bets(corr)
print("signal correlation EQ02/EQ03 %.2f  EQ02/EQ12 %.2f  EQ02/EQ06 %.2f" % (corr[0, 1], corr[0, 3], corr[0, 2]))
print("effective independent bets: %.2f of %d" % (bets["n_effective"], bets["n_members"]))
EOF
```

```
equal_weight  horizon 5s  weights: EQ02 +0.250 EQ03 +0.250 EQ06 +0.250 EQ12 +0.250  day-2 IC +0.0255
ridge         horizon 5s  weights: EQ02 +0.418 EQ03 -0.228 EQ06 +0.265 EQ12 +0.090  day-2 IC +0.0338
signal correlation EQ02/EQ03 0.95  EQ02/EQ12 1.00  EQ02/EQ06 -0.16
effective independent bets: 1.64 of 4
```

Four members, 1.64 bets: EQ02, EQ03 and EQ12 are one signal three times.
Equal weights give that one signal three quarters of the blend; ridge sees
the correlation and spreads one weight across the cluster (one of the three
even goes negative) and gives EQ06 its own. This is an illustration on one
split, not a result — the judged version, through the whole validation
chain and with its looks debited, is

```bash
cd python && PYTHONPATH=src python3 -m iap.research combine     # writes research/combination/
```

which costs 95 looks per (asset class, method) experiment the first time
and nothing on a rerun. `--dry-run` writes no report and still debits the
looks; `--members` and `--method` narrow the run, and a narrower member
list is a different experiment with its own identity. The committed
verdict: 0 PROMOTE, 8 ITERATE — every combination fails the cost gate
(`research/combination/REPORT.md`).

## 39. Calibrate the simulator and run a maker-side backtest (v1.9)

The research backtester only takes liquidity. The v1.9 maker path
(API_TRADING.md §2.6) posts at the touch instead. It first calibrates fill
rates, queue-depletion hazards, latency, markouts and impact from the event
stream (M1). It then builds trade-matched labels (M3) and replays a score
series through the FIFO simulator, gated on expected edge > measured adverse
selection, with optional tail conditions (M2, M4). The exit is either a taker
cross (the default) or passive: post at the far touch, repost once, then
cross whatever is left. The example uses the golden equity vector and a toy
L1-imbalance score. Rows marked `*` turn the gate off (`margin_bps=-1e9`);
they are diagnostics, not strategies:

```bash
PYTHONPATH=python/src python3 - <<'EOF'
import numpy as np
import pandas as pd
from pathlib import Path
from iap.core.codec import read_jsonl
from iap.execution import ExecConfig, InstrumentSpec, load_venues
from iap.execution.calibration import ExecCalibration, estimate_calibration
from iap.labels.maker_labels import maker_labels
from iap.backtest.maker import MakerBacktester, MakerConfig
from iap.tca.markout import build_gated_timeline

events = read_jsonl(Path("tests/golden/events_eq_mbo.jsonl"))
doc = estimate_calibration(events, {1: 0.01}, source={"file": "events_eq_mbo.jsonl"})
cal = ExecCalibration.from_dict(doc)
fr = doc["fill_rates"]["all"]
print(f"touch joins {fr['n']}  P(any fill) {fr['p_any_fill']:.2f}  P(full) {fr['p_full_fill']:.2f}")
for h in ("100ms", "1s", "10s"):
    m = doc["markouts"]["all"][h]
    print(f"maker adverse selection {h:>5}: {m['mean_bps']:+.2f} bp (se {m['se_bps']:.2f}, n {m['n']})")

# a toy causal score: L1 queue imbalance, sampled every 10 s (a sparse stream)
tl = build_gated_timeline(events, 1, 0.01)
SEC = 10**9
ts = np.arange(tl.ts[0] + SEC, tl.ts[-1], 10 * SEC, dtype=np.int64)
idx = [tl.prevailing(int(t)) for t in ts]
imb = np.array([(tl.bid_sz[i] - tl.ask_sz[i]) / (tl.bid_sz[i] + tl.ask_sz[i]) for i in idx])
scores = pd.DataFrame({"exchange_ts": ts, "expected_return": 1e-4 * imb,
                       "confidence": np.abs(imb), "z": imb / imb.std()})

cfg = ExecConfig(seed=20260829, venues=load_venues("configs/venues/venues.json"),
                 instruments={1: InstrumentSpec(1, 0.01, 1.0, 38_000_000.0)})
lab = maker_labels(events, ts, instrument_id=1, exec_config=cfg, ttl_ns=60 * SEC)
print(f"labels: bid filled {lab['bid_filled'].mean():.2f}, not run over "
      f"{np.nanmean(lab['bid_not_run_over']):.2f}")
base = dict(qty=100, ttl_ns=60 * SEC, horizon_ns=10 * SEC, as_horizon="10s")
pas = dict(base, exit="passive", exit_timeout_ns=30 * SEC, exit_reprices=1)
for name, mc in (("taker", MakerConfig(**base)),
                 ("passive", MakerConfig(**pas)),
                 ("taker*", MakerConfig(**base, margin_bps=-1e9)),
                 ("passive*", MakerConfig(**pas, margin_bps=-1e9))):
    s = MakerBacktester(cfg, mc, cal).run_instrument(events, scores, 1).summary()
    line = f"{name:<8} posted {s['posted']:>3} filled {s['filled_orders']:>3} gated out {s['gated_out']:>3}"
    if s["n_trips"]:
        line += (f" | spread {s['mean_half_spread_earned_bps']:+.2f} AS {s['mean_adverse_selection_bps']:+.2f}"
                 f" exit {s['mean_exit_slippage_bps']:+.2f} net {s['mean_net_bps']:+.2f} bp/trip,"
                 f" {s['total_net']:+.2f} USD")
    if s["passive_exit_trips"]:
        line += (f" | exit filled passively {s['passive_exit_fill_rate']:.2f},"
                 f" timeout cross {s['timeout_cross_rate']:.2f}")
    print(line)
EOF
```

```
touch joins 280  P(any fill) 0.35  P(full) 0.12
maker adverse selection 100ms: +0.74 bp (se 0.16, n 234)
maker adverse selection    1s: +0.80 bp (se 0.21, n 233)
maker adverse selection   10s: +1.36 bp (se 0.40, n 214)
labels: bid filled 0.17, not run over 0.07
taker    posted   0 filled   0 gated out 698
passive  posted  83 filled  22 gated out  22 | spread +5.90 AS +7.13 exit +5.72 net -6.71 bp/trip, -35.70 USD | exit filled passively 0.32, timeout cross 0.68
taker*   posted 101 filled  27 gated out  38 | spread +3.36 AS +4.20 exit +10.16 net -11.41 bp/trip, -74.70 USD
passive* posted  83 filled  22 gated out  22 | spread +5.90 AS +7.13 exit +5.72 net -6.71 bp/trip, -35.70 USD | exit filled passively 0.32, timeout cross 0.68
```

How to read it.

With a taker exit, the gate admits nothing. The half-spread earned on entry
is paid back on exit, so the gate reduces to
`|er| + rebate - taker fee - impact > adverse selection`, and a 1 bp score
cannot clear 1.4 bp of measured adverse selection.

The passive exit changes the gate. It expects to earn the second
half-spread and rebate with the calibrated touch fill probability (0.35
here), and on these wide synthetic spreads it then admits nearly every row.
The outcome does not support that: only 32% of exits fill passively, 68%
time out and cross, and adverse selection on the trips (+7.1 bp) is far
above the calibrated +1.4 bp, because the trips that last are the ones the
market ran over. Net is -6.7 bp per trip. That is better than the -11.4 bp
of the ungated taker exit, but still a loss.

The decomposition `gross = spread earned - adverse selection - exit slippage`
holds for every row. With a passive exit, a fully passive exit shows
negative slippage (the second half-spread earned) and a timeout pays it back.
These are illustrations on a sparse synthetic stream (2,000 events in two
hours), not results. To calibrate a real session, run
`python -m iap.execution.calibration --events <day>.iap1 --tick 1=0.01 --out
calib.json` and pass `load_calibration("calib.json")` to the simulator and
the backtester.

## 40. Quote both sides with an alpha-skewed reservation price (v1.10)

`iap.backtest.quoting.QuotingBacktester` (API_TRADING.md §2.7) keeps a bid
and an ask resting through the FIFO simulator. Quotes sit around an
Avellaneda-Stoikov reservation price that the alpha shifts
(`r = mid + alpha_weight * er * mid - gamma * sigma^2 * q * tau`), with a
hard inventory limit, refresh through the latency path, a calibrated
adverse-selection floor on the half-spread and an end-of-session flatten.
It compares the alpha-skewed quoter with the no-skew (gamma only) baseline
on the golden equity vector and the toy L1-imbalance score of recipe 39:

```bash
PYTHONPATH=python/src python3 - <<'EOF'
from pathlib import Path

import numpy as np
import pandas as pd
from iap.backtest.quoting import QuotingBacktester, QuotingConfig
from iap.core.codec import read_jsonl
from iap.execution import ExecConfig, InstrumentSpec, load_venues
from iap.execution.calibration import ExecCalibration, estimate_calibration
from iap.tca.markout import build_gated_timeline

events = read_jsonl(Path("tests/golden/events_eq_mbo.jsonl"))
cal = ExecCalibration.from_dict(estimate_calibration(events, {1: 0.01}))
tl = build_gated_timeline(events, 1, 0.01)
SEC = 10**9
ts = np.arange(tl.ts[0] + SEC, tl.ts[-1], 2 * SEC, dtype=np.int64)
idx = [tl.prevailing(int(t)) for t in ts]
imb = np.array([(tl.bid_sz[i] - tl.ask_sz[i]) / (tl.bid_sz[i] + tl.ask_sz[i]) for i in idx])
scores = pd.DataFrame({"exchange_ts": ts, "expected_return": 1e-4 * imb})
cfg = ExecConfig(seed=20260829, venues=load_venues("configs/venues/venues.json"),
                 instruments={1: InstrumentSpec(1, 0.01, 1.0, 38_000_000.0)})
for name, w in (("skewed", 1.0), ("no-skew", 0.0)):
    qc = QuotingConfig(qty=100, max_inventory=300, gamma=0.1, tau_s=10.0, alpha_weight=w)
    s = QuotingBacktester(cfg, qc, cal).run_instrument(events, scores, 1).summary()
    print(f"{name:<8} fills {s['n_quote_fills']:>3} (bid {s['bid_fill_rate']:.2f} ask {s['ask_fill_rate']:.2f})"
          f" max|q| {s['max_abs_inventory']} mean|q| {s['mean_abs_inventory']:.0f}"
          f" AS floor {s['as_bps_floor']:+.2f} bp ({s['as_source']})")
    print(f"         spread {s['spread_captured']:+.2f} markout {s['markout']:+.2f}"
          f" inventory {s['inventory_pnl']:+.2f} flatten {s['flatten_cost']:+.2f}"
          f" rebates {s['rebates']:+.2f} fees {-s['taker_fees'] - s['impact']:+.2f}"
          f" = net {s['net']:+.2f} USD")
EOF
```

```
skewed   fills  97 (bid 0.17 ask 0.19) max|q| 300 mean|q| 151 AS floor +0.80 bp (calibration:1s)
         spread +205.50 markout -49.00 inventory -82.50 flatten -6.00 rebates +19.40 fees -0.90 = net +86.50 USD
no-skew  fills 104 (bid 0.19 ask 0.19) max|q| 300 mean|q| 142 AS floor +0.80 bp (calibration:1s)
         spread +218.50 markout -45.50 inventory -69.00 flatten -4.00 rebates +20.80 fees -0.60 = net +120.20 USD
```

How to read it. The five P&L terms add up to the gross exactly
(`spread + markout + inventory + flatten`; the identity is tested), and the
net adds rebates and subtracts taker fees and impact. On this vector the toy
imbalance score does not help: skewing moves the quotes, captures less
spread and does not reduce adverse selection, so the no-skew quoter nets
more. The tests show the opposite with an informative (look-ahead,
synthetic) signal: the skew cuts markout losses. Whether a real alpha does
that is an open question, and this recipe does not answer it. Both quoters
hit the 300-share limit; it is never exceeded.

To run a real session (not run for v1.10; the machine was busy):

```bash
python -m iap.execution.calibration --events <day>.iap1 --tick 1=0.01 --out calib.json
# then, in Python: QuotingBacktester(cfg, QuotingConfig(...), load_calibration("calib.json"))
#   .run_days({day: (events, scores)}, 1) for each day, and sharpe_per_day(table["net"])
```
