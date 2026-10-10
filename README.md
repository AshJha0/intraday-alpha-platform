# Intraday Alpha Platform

A reproducible, event-driven, research-to-production trading platform spanning
market data, microstructure alpha, ML research, portfolio construction, risk,
execution, SOR, TCA, deterministic replay, and low-latency engineering — built
to the institutional specification in [docs/SPECIFICATION.md](docs/SPECIFICATION.md)
(spec §1), deliberately polyglot per the spec's responsibility matrix (§3):

- **Python** — quant research and ML environment, the *reference
  implementation* every port must match, and the executable MVP that runs
  the whole loop (`python -m iap.mvp`);
- **Java** — institutional strategy/platform layer (portfolio, TCA, risk
  orchestration, backtest, paper trading, monitoring/API);
- **C++** — latency-critical HFT path (codec, book, features, alpha,
  execution simulator, SOR, replay);
- **Rust** — safety-critical infrastructure (hard risk engine, event bus,
  venue protocol simulation, telemetry, replay components).

The connecting principle (spec §1): alpha, execution, portfolio construction
and risk are separate concerns joined by explicit, versioned contracts, and
**no strategy is production-ready because of backtest Sharpe alone**.

The engineering principle, stated once and enforced everywhere: **Python
defines the semantics, C++/Rust/Java implement them, golden tests prove
equivalence.** Two subsystems qualify it honestly — the rule text of the
hard risk engine is owned by Rust and of the execution simulator by C++
(PLATFORM_CONVENTIONS.md §11) — and since 2026-09-19 both also have a Python
implementation (`iap.risk`, `iap.execution`) proven by the same golden files
the Java ports are proven by, so the loop the MVP runs is the reference
loop end to end.

## Current status (v1.11.0, release branch; v1.10.0 released 2026-10-10)

**Where the research stands.**

- **Synthetic data (bundled):** 24 alphas, 0 PROMOTE. Eighteen never
  forecast a move larger than their round-trip cost; the six that trade
  lose. The executable loop loses 81.53 USD on its golden session. Details
  below.
- **Real data (owner-supplied, not bundled):** seven Nasdaq TotalView-ITCH
  sessions, 2019-01-30 to 2020-01-30, AAPL / MSFT / QQQ. The signals are
  statistically strong (EQ01 microprice gate IC +0.105, t 21) and none
  survives taker costs: at 1 s the typical forecast is about 0.07 bp against
  a round trip of about 0.7 bp. Combining the equity alphas raises the t to
  25 and is still cost-negative. A power study on real-noise nulls flags
  nothing when nothing is planted and reliably detects planted ICs of about
  0.01-0.02 ([docs/REAL_DATA.md](docs/REAL_DATA.md) §3.1).
- **Out-of-time holdout (v1.8.0):** six hypotheses were pre-registered and
  pushed (commit `6723fd0`) before any 2026 feature existed. On 2026-05-15
  and 2026-05-18 all four confirmatory signals were confirmed (EQ01 t 7.74,
  EQ02 8.02, EQ10 8.08, EQ05 9.51) and both controls behaved as registered.
  No run traded at 1x costs. Those two days are now spent
  ([docs/REAL_DATA.md](docs/REAL_DATA.md) §3.2, [LEARN.md](LEARN.md) §33).
- **Maker side (v1.9-v1.10), in progress:** because a taker cannot pay
  0.7 bp for a 0.07 bp edge, the platform now has a calibrated maker
  backtest and a skewed two-sided quoter. Measured so far on synthetic data
  only: the maker backtest loses 6.7 bp per trip with a passive exit (11.4 bp
  crossing out); the quoter with a toy score nets +86.50 USD skewed against
  +120.20 USD unskewed. A pre-registered, in-sample maker study on the seven
  real sessions is **running**; it has no result yet, and by its own
  verdict rule a positive in-sample result would only justify an
  out-of-sample test ([LEARN.md](LEARN.md) §31, §34).
- **Auctions and quoting on real files (v1.11), in progress:** `AUC01` (the
  closing-cross strategy, with a declared 2026 holdout) and the M5 quoter
  are being run on the real sessions, pre-registered, exploratory and
  in-sample, on branch `research/step2`. No result is reported yet.

**New in v1.9.0** (all opt-in; no default, golden or published number moved):

- research validity R1-R6, the `v3` method bundle: FOMC / holiday calendar
  and seeded day sampling, day-aligned folds, day-clustered and
  day-block-bootstrap t, equal-weight per-instrument gate IC, single-venue
  labelling, causal label freshness
  ([docs/RESEARCH_VALIDITY.md](docs/RESEARCH_VALIDITY.md) §1a, COOKBOOK 44);
- parallel feature build, `--workers N`, byte-identical to serial (COOKBOOK 43);
- maker economics M1-M4: simulator calibration from the event stream, a
  maker backtest with queue fills, rebates, measured markout and a passive
  exit, maker labels, tail-conditional sizing
  ([API_TRADING.md](API_TRADING.md) §2.6, COOKBOOK 39, 45, 46).

**New in v1.10.0** (all opt-in, Python only):

- M5 skewed two-sided quoting with inventory limit and flatten
  ([API_TRADING.md](API_TRADING.md) §2.7, COOKBOOK 40);
- A1 auction imbalance: NOII decoding in a separate pass, the closing-cross
  strategy `AUC01` with a walk-forward, pre-registration required
  ([docs/REAL_DATA.md](docs/REAL_DATA.md) §3.3, COOKBOOK 42);
- X1-X3 Almgren-Chriss trajectory and frontier, alpha urgency, forecast
  VWAP curve ([API_TRADING.md](API_TRADING.md) §2.8, COOKBOOK 41, 48);
- G1-G4 governance: pre-registrations cost a look and commit to a code
  fingerprint, the blackboard is anchored to git, agent writes are
  Ed25519-signed (new dependency `cryptography`)
  ([docs/governance/GOVERNANCE.md](docs/governance/GOVERNANCE.md) §2a,
  COOKBOOK 47, [LEARN.md](LEARN.md) §35);
- a fix: `python -m iap.features --workers N` now works under the spawn
  start method (Windows, macOS).

**New in v1.11.0** (all opt-in; no default, golden, published number or
cross-language contract moved; no test or CI job calls a model):

- E2, the Rust feature engine from Python: a pyo3 extension
  (`iap_features_rs`, one abi3 wheel, built by the `rust-pyo3` CI job)
  behind `iap.features.native`, and `python -m iap.features --engine rust`.
  Measured in CI on the golden vectors: about 5,250 events/s through the
  Python reference against about 790,000 through Rust, **150x** on the 45
  native slots. The pipeline is not 150x faster: the other 160 features
  still run in Python. Comparing every row (not just the golden
  checkpoints) found that the Rust engine read a half-built book inside
  SNAPSHOT recovery bursts; that bug is corrected in this release
  ([API_FEATURES.md](API_FEATURES.md) §7.1, COOKBOOK 49 and 52,
  [LEARN.md](LEARN.md) §36);
- E3, the polyglot policy: [docs/POLYGLOT.md](docs/POLYGLOT.md) decides
  each duplicated copy, 18 CANONICAL and 20 FROZEN (24 paths; 2 of them
  RETIRE candidates, kept). Nothing was deleted. A pull request that
  changes a frozen copy fails without a `POLYGLOT-OVERRIDE: <reason>` line
  (`tests/harness/check_polyglot_policy.py`, COOKBOOK 53, LEARN.md §38);
- AI3, `iap.mlops`: an immutable, content-hashed model registry tied to a
  pre-registration, drift and calibration monitoring, and shadow mode for a
  candidate model ([API_ADAPTIVE.md](API_ADAPTIVE.md) §9, COOKBOOK 50 and
  54, LEARN.md §39);
- AI1-AI2, `iap.llm`: a Claude model that drafts, pre-registers (signed,
  one look each), runs gated studies and files findings through the v1.10
  governance; every number in a finding must appear in a cited artefact,
  and budgets cap spend, tokens, tool calls and pre-registrations. Four
  behaviour evals (p-hacking, prompt injection, hallucinated citations,
  task success) run mocked in CI with a control-removed ablation each. A
  live run on 2026-10-10 (`claude-haiku-5-5`, estimated $0.0096) passed 4
  of 4; the model behaved well, so the live run did not stress the
  controls, and the mocked adversarial evals remain the evidence that they
  work ([docs/governance/GOVERNANCE.md](docs/governance/GOVERNANCE.md) §2b,
  COOKBOOK 51 and 55, LEARN.md §37). The SDK is the optional `[llm]` extra;
  `.env` files are git-ignored.

**Deferred:** A2 (QQQ against its constituents; needs more symbols), A3
(futures lead-lag; on hold, the ES/NQ data would cost about $10-25), AI4
(deep order-book baselines); the real batch is not yet re-run under `v3`
([docs/ROADMAP.md](docs/ROADMAP.md) §3.6).

## Architecture in one line

Spec §4, realized end to end in this repository:

```
venues → feed gateways → normalization + sequence validation → canonical event bus
→ order-book reconstruction → feature engine → alpha ensemble → regime/confidence
→ portfolio construction → hard risk → execution optimizer → SOR/venue adapters
→ executions → TCA + attribution → decision trace (JSONL + SQLite, explain)
→ research feedback: ExperimentRunner → ledger → 7-state promotion lifecycle
```

Every hop is a typed, versioned contract (`schemas/`, `iap.contracts`); every
decision cycle is one `DecisionTrace`; every alpha's position in RESEARCH →
CANDIDATE → VALIDATING → PAPER → ACTIVE → WATCH → RETIRED is a gated,
ledgered transition. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for
the full design, data flow, and diagrams.

## Headline numbers (all verified against repo artifacts)

| what | number | artifact |
|---|---|---|
| Registered features | **205** (10 families; 40-feature native core set ported to C++/Rust/Java) | `data/reference/feature_registry.json` |
| Flagship alphas | **24** (EQ01–EQ12, FX01–FX12), each with an enforced `Economic rationale:` docstring | `python/src/iap/alpha/`, `research/alpha_reports/` |
| Promotion verdicts | **0 PROMOTE / 11 ITERATE / 13 REJECT** (gated on the pooled *uncrossed* IC and its HAC t against the ledger threshold, 4.365; the v1.5.0 default methods) | `research/alpha_reports/REPORT.md` |
| Lifecycle registry | **24 alphas at CANDIDATE, 0 beyond** — every one fails `net_pnl_after_costs` at 1× costs, its bootstrap bound `net_pnl_bootstrap_ci` and `capacity`; the cross-alpha correlation gate passes vacuously (7 states, 17 pinned edges, 20 gates) | `research/alpha_registry.json`, `research/lifecycle_transitions.jsonl`, `tests/golden/expected_lifecycle.json` |
| Experiments ledger | 5,156 recorded looks over **216 distinct configurations** (de-duplicated by alpha × kind × config × dataset: 70 configurations of the v1.3.0 dataset are kept as history, 146 are on the current one — 69 under the rules up to v1.4.0, 77 under the v1.5.0 defaults, eight of those signal combinations); expected max \|t\| under the global null ≈ 4.135, Bonferroni per-test \|t\| ≥ 4.424 | `research/experiments.json` |
| Contracts | **17** JSON Schemas (all `x-version` 1) mirrored by **22** typed Python contracts and **18** runtime-checkable Protocols; one pinned instance each | `schemas/`, `python/src/iap/contracts/`, `tests/golden/expected_contracts_examples.json` |
| Python reference ports proven by the ports' own goldens | risk: `expected_risk_decisions.json` exact, audit JSONL + snapshot **byte-identical**; execution: `expected_replay_fills.json` **bit-identical** | `python/tests/test_risk_golden.py`, `python/tests/test_execution_golden.py` |
| MVP golden run (`python -m iap.mvp run`, seed 12345) | **15,805** events · **800** decisions · **235** parent orders · **169** fills · P&L **−81.53 USD** (cost-negative: +0.022 bps alpha vs −0.41 bps execution cost) · trace digest `20d4ff76…` reproduced by run-twice and replay-from-capture | `tests/golden/expected_mvp.json` |
| Adaptive deployment study | 4 refit policies × 10 alphas; 88 drift-triggered refits; FX01 retired under every policy; 19 of 40 deployments make no trade, none ends above zero | `research/adaptive_reports/ADAPTIVE_REPORT.md` |
| Bundled dataset | 2 synthetic sessions, 19 instruments, 308,975 normalized events (`data_version` `116b7787…`; equity flow runs to the close since v1.4.0) | `data/normalized/qc_report.json` |
| Feature emission | 213,021 vectors at 100 ms cadence | `data/features/features_summary.json` |
| Rust feature engine from Python (v1.11, CI-measured) | about 5,250 events/s (Python reference, full 205-feature engine) against about 790,000 events/s (Rust, the 45 native slots): **150x** on the golden vectors, 123x on a seeded equity day at 100 ms; not a pipeline speed-up, the other 160 features still run in Python | `API_FEATURES.md` §7.1, `rust-pyo3` CI job summary |
| LLM agent behaviour evals (v1.11) | 4 of 4 pass mocked in CI, each control-bearing eval also failing with its control removed; live 2026-10-10 on `claude-haiku-5-5`: 4 of 4, estimated $0.0096 (the model behaved well, so the live run did not stress the controls) | `python -m iap.llm.evals`, GOVERNANCE.md §2b |
| C++ hot path | IAP1 decode 184.1 ns/event (CRC-32 verified); book update 26.4 ns; replay 27.2M events/s; one 5.6 KB decision trace serialised in 31.7 µs off the event loop | `benchmarks/results_cpp.md` |

The honesty is the point (spec §32): of 24 alphas on the bundled synthetic
data, **none** survives every promotion gate — leakage tests, OOS IC ≥ 0.01,
a HAC t of the pooled slope at or above the multiple-testing threshold of
the ledger (4.365 for this run, never below 3.0), fold consistency,
*hypothesis sign confirmed*, and
positive net P&L at 1× modeled costs. Eleven are statistically real enough
for ITERATE (EQ03: uncrossed IC 0.0189, t 5.16,
leakage-clean) and three clear the ledger threshold (EQ02, EQ03, EQ12), yet
none of the 24 makes money net of modeled costs at 1×: under the cost-aware
position policy 18 never forecast a move larger than their round-trip cost
and make no trade, and the 6 that trade lose.

A correctness review on 2026-09-20 moved several of these numbers, always
toward a harsher reading: `fold_sign_consistency` was measuring the
*beta-signed* signal, so an alpha backwards in every fold reported perfect
consistency; the walk-forward was training inside its own declared holdout;
turnover — a cost statistic — was divided by wall-clock span including dead
hours; and the execution simulator credited every resting child with the
full observed trade volume, fabricating liquidity that was never there.
`PLATFORM_CONVENTIONS.md` §14 pins the corrected semantics and
`schemas/MIGRATIONS.md` lists the goldens regenerated because of them.
Nothing was promoted before the review and nothing is promoted after it.

A second review, released as v1.3.0 on 2026-10-03
([CHANGELOG.md](CHANGELOG.md)), found the same class of defect in the
safety code itself. The hard risk engine trusted a mark stamped in the
future, let NaN through float comparisons, could overflow i64 position and
timestamp arithmetic, and let a venue-0 (SOR) order pass a venue kill; all
three engines now reject in each case and a new edge golden pins those
branches byte for byte. The execution simulator re-filled a resting order
against the same displayed size on every event and tracked events the book
had dropped. The Java paper platform could resume a cursor beside a risk
snapshot of another instant, drop an admin kill on a quiet feed, and bound
its admin listener to every interface. On the research side the corrected
statistics were added **opt-in** (they are the defaults since v1.5.0, see
below) — every default stayed the previously pinned
behaviour, so no committed number moved in that release — and a planted-signal power study
([research/power/POWER_REPORT.md](research/power/POWER_REPORT.md)) measures
what the validation chain can and cannot detect: on the v1.3.0 dataset it
flagged the planted order-flow signal in 3 of 3 seeds at the reference
effect size and the planted lead-lag in 0 of 3, promoted nothing at any
size, and no bootstrap P&L interval lay above zero (the current figures,
which are weaker, are in the next two paragraphs). Still 0 PROMOTE.

v1.4.0 (2026-10-03) corrects the dataset itself. Up to v1.3.0 a generator
bug ended each equity stream's continuous flow 37.6–43.2% of the way
through the 6.5-hour session, so all equity research had seen only about
the first 2 h 40 min after the open, with events packed about 2.5 times as
densely as they are now. The default calibration is now
`equities.flow.calibration = "session"` (generator config `x-version` 2):
flow runs to the close, and `"legacy_budget"` reproduces the v1.3.0 dataset
byte for byte. Everything derived from the dataset was regenerated in one
pass (`tools/regenerate_dataset_artifacts.py`, run by the manual
`regenerate` CI job); the FX files are byte-identical and every FX alpha
report is unchanged. What moved, all of it on the equity side:

- **Verdicts: 0 PROMOTE / 10 ITERATE / 14 REJECT** (was 0 / 11 / 13). EQ11
  falls from ITERATE to REJECT (uncrossed t 1.40). The equity statistics are
  weaker throughout — EQ03's uncrossed IC is 0.0190 at t 5.78, where the
  compressed flow gave 0.0298 at t 10.57 — and only four alphas (EQ02,
  EQ03, EQ12, FX04) now fail the cost gate alone; the others also fail a
  statistical or stability gate.
- **Labels.** Equity rows are now about 3.1–3.3 s apart (median quote gap
  about 2 s), so the 5 s floor of the label freshness rule binds and a
  material share of equity labels at horizons of 10 s and longer is invalid
  as stale: the valid fraction is 0.99–1.00 at 5 s, 0.79–0.81 at 10 s,
  0.75–0.77 at 1 m and 0.45–0.63 at 15 m.
- **ML.** The linear gate now **passes**: the best baseline (ridge) has a
  pooled OOS IC of +0.0081 against the mid-to-mid label (it was −0.0430, a
  fail), so xgboost, lightgbm and the MLP were fitted instead of skipped.
  None earns its costs — the conservative net is
  negative for every model (−0.110 to −2.612 bps per signal) — and the
  meta-label gate is still degenerate (zero trades).
- **Power study.** The chain is less sensitive than the v1.3.0 run
  suggested. The planted order-flow effect at the reference size reaches
  t ≥ 3 in 1 of 3 seeds (ITERATE-level evidence in 3 of 3), at twice the
  size in 3 of 3, and at half the size in none; the planted lead-lag is not
  detected at any size. Nothing is flagged at the null level, nothing is
  promoted, and no bootstrap P&L interval lies above zero.
- **MVP.** The golden session trades through all 15 minutes instead of the
  first part of them: 800 decisions and 169 fills (was 355 and 55), and it
  loses 81.53 USD (was 22.65).
- **Ledger.** The looks already spent on the v1.3.0 dataset (1068) are kept
  and the regenerated pipelines added 852 more, so the multiple-testing
  yardstick is harsher (the Bonferroni threshold rose to 4.206), not reset.

The conclusions did not change: 0 PROMOTE, 24 alphas held at CANDIDATE by
`net_pnl_after_costs`, a cost-negative MVP.

v1.5.0 (2026-10-04) makes the corrected research methods the defaults. The
eleven methods v1.3.0 added as opt-ins and v1.4.0 kept opt-in — so that
every committed research number was still computed under rules the
repository itself described as deficient — are now what runs when nothing
is named, and each old rule stays selectable under an explicit legacy name
(`iap.validation.methods`: bundle `"v2"`, the default, and `"legacy_v1"`;
`python -m iap.research run --methods legacy_v1`,
`research/alpha_reports/run_all.py --methods legacy_v1 --out-dir <dir>`;
PLATFORM_CONVENTIONS.md §13.6 has the table). The dataset did not change
(`data_version` `116b7787…`); everything derived from it was regenerated in
one pass by the same CI job, and nothing was tuned to recover a v1.4.0
conclusion. What moved:

- **Verdicts: 0 PROMOTE / 11 ITERATE / 13 REJECT** (was 0 / 10 / 14). FX03
  falls to REJECT; FX10 and FX11 become ITERATE on a pooled t of 1.55 and
  2.22. The gate t is now the HAC t of the pooled slope — the significance
  of the IC the gate reads — and the PROMOTE threshold is the ledger's
  multiple-testing threshold at the run's look count, 4.365, instead of a
  fixed 3.0. Three alphas clear it (EQ02, EQ03, EQ12) where six cleared
  3.0; EQ06 misses it at 4.36 and FX04 at 4.24. EQ03 is at t 5.16 (5.85
  within-bucket).
- **Costs.** Under the sign policy every alpha traded every row and lost
  five or six figures on the last fold (the largest loss was 300,873 USD);
  that measured the policy. Under the cost-aware default 18 of the 24 make
  no trade — the forecast never exceeds the round-trip spread and fee — and
  the six that trade lose between 22 and 733 USD. A net P&L of exactly 0
  does not pass `net P&L > 0`, so the cost gate still fails for all 24. The
  edge-breakeven capacity is above zero for three alphas (EQ11 38,248 USD,
  FX08 1,561, FX10 61,135) and below the 1,000,000 USD lifecycle gate for
  every one, so `capacity` now fails for all 24 as well.
- **Rows.** A label invalid for BLACKOUT alone is scored at its realised
  reopen return. That changes one alpha materially: EQ11 (15-minute
  horizon, 23,413 such rows), gate IC 0.0261 → 0.0038; the valid-only IC
  had dropped exactly the rows on which the forecast is wrong.
- **Hypothesis signs.** Two alphas are significantly wrong-signed at the
  ledger threshold (EQ08 t −4.57, FX09 t −5.75). They were REJECT before
  and are REJECT now.
- **Leakage.** The recompute-from-raw-events probe runs in the standard
  suite; 24 of 24 pass.
- **ML.** Unchanged: the linear gate passes (ridge +0.0081), no model
  earns its costs, the meta-label gate is degenerate (zero trades). 275 of
  131,880 meta-feature values are missing and are no longer imputed.
- **Adaptive study.** 88 drift-triggered refits (was 122) under the HAC
  drift z and the CUSUM retirement rule; FX01 is retired under every
  policy, as before; 19 of the 40 deployments make no trade and none ends
  above zero. The v1.4.0 policy totals were not in USD (the runner summed
  quote-currency P&L for the FX rows); that is fixed, and the static policy
  now totals −3,921 USD.
- **Power study.** The detection rates of the planted order flow are the
  same under the pooled t (1 of 3 seeds at the reference size, 3 of 3 at
  twice it, none at half), but the cost-aware backtest makes no trade on it
  at the reference size and 14 on average at twice that size, with no fold
  surviving costs: the chain can see an effect it cannot monetise. The
  planted lead-lag reaches ITERATE in one or two seeds of three and is
  never significant. Nothing is promoted and nothing is flagged at the null
  level. Those are three seeds on two sessions; the detection-power rework
  (20 seeds, a session grid, the gate threshold of 4.365) measures the
  order-flow effect at 2 of 20 runs on two sessions, 10 of 20 on four and
  20 of 20 on eight, and the lead-lag at 0 of 20 at EQ10's declared 1 s
  label and 16 of 20 at a 5 s label on eight sessions, with 0 of 20 on the
  null throughout and still nothing promoted
  ([extended report](research/power/extended/POWER_REPORT.md)).
- **MVP.** Events, decisions, fills and P&L are identical (the loop does
  not use the research backtester). `config_version` and the trace digest
  changed (`f293e7e7…` → `439bbad5…`, `f51890da…` → `20d4ff76…`) because
  `execution.json`, `alpha_params.json` and the registry are among the
  hashed configuration documents.
- **Ledger.** The 1920 already recorded are kept; the regenerated pipelines
  added 2,476 (84 per alpha per run under the default methods, 28 under the
  legacy ones), for 4,396; the eight signal-combination experiments added
  760, for 5,156.
- **Ports.** Java and Rust implement the CUSUM and the consecutive
  retirement rule and the ledger and the fixed significance threshold, by
  name; the Java `ResearchBacktester` and `CostModel` default to the v1.5.0
  research backtest rules and keep the legacy ones by name.

The conclusion is the same and is stated more directly: 0 PROMOTE, 24
alphas held at CANDIDATE, a cost-negative MVP. The predicted moves are
smaller than the spread.

Two conditioning rules do most of the culling, and both were added after a
round-3 audit found the earlier numbers were measuring the wrong thing. IC is
now computed **only on uncrossed cross-sections** (`spread_ticks_v1 >= 0`)
with no stale venue in the instrument, because a crossed merged book is an
artifact of two venues disagreeing, not a price anyone could trade: on FX,
where ~29-33 % of cross-sections are crossed, this is the difference between
FX08 at IC 0.117 (all rows) and **0.041** (uncrossed), and it withdraws
FX09's former "strongest statistics in the study" standing (−0.128 → −0.047).
And walk-forward folds are cut at quantiles of **row mass** rather than wall
span, so the equity calendar can no longer hand two of four folds ~0 rows and
call the empty ones a pass. Every report prints the crossed/uncrossed split
and the degenerate-fold count (currently 0 of 96 folds). Statistically
significant and cost-negative is still the platform's central, truthfully
reported finding (see
[research paper 1](docs/papers/01_ofi_predictability_equities.md), and the
dated errata appended to all four papers). The two newest artefacts say the
same thing from two more directions: the promotion lifecycle
([docs/LIFECYCLE.md](docs/LIFECYCLE.md)) bootstraps all 24 alphas to
CANDIDATE and advances none, because the `net_pnl_after_costs` gate fails
for every one; and the executable MVP ([docs/MVP.md](docs/MVP.md)) runs the
full loop on one synthetic equity and loses 81.53 USD on 8,229 shares — an
alpha contribution of +0.022 bps against −0.41 bps of modelled execution
cost. Its realized mid-to-mid IC (0.22 for EQ01 at 1 s) is about twenty
times the research IC (0.011); the audit of that gap (2026-09-20, on the
v1.3.0 session) attributed it to the synthetic generator's mean-reverting
venue noise, not a leak — and the cost-adjusted IC (−0.024) is negative
(docs/MVP.md §7.1).

**Models decay, and the platform now treats that as a first-class
concern.** The adaptability layer (`python/src/iap/adaptive` — the
reference; `com.iap.adaptive` — the live Java port; contract in
[API_ADAPTIVE.md](API_ADAPTIVE.md)) measures decay with PSI/KS drift
monitors and a rolling realized-vs-research IC, refits models when drift
crosses the pinned triggers, and moves decaying alphas through an
IC-gated ACTIVE → WATCH → RETIRED lifecycle (FX01 finishes RETIRED under
every policy) — the live sub-machine of the full seven-state promotion
lifecycle in [docs/LIFECYCLE.md](docs/LIFECYCLE.md). RETIRED verifiably halts allocation in the *backtest*
(`iap.backtest.adaptive`); in the live Java loop the gauge is
**observational** — a RETIRED alpha keeps trading at full size and
`AlphaLifecycleRetired` pages a human, who reduces the allocation by
decision. That divergence is deliberate and pinned
([API_ADAPTIVE.md](API_ADAPTIVE.md) §6). The comparison
study ([ADAPTIVE_REPORT.md](research/adaptive_reports/ADAPTIVE_REPORT.md))
is reported with the same honesty as the promotion report: on the bundled
two synthetic sessions, **no refit policy demonstrably beats static** —
weekly scheduling cannot even fire once, 19 of the 40 deployments make no
trade under the cost-aware policy, none ends above zero, and the policy
totals (−3,921 USD static, −5,595 daily, −5,975 drift-triggered) are set by
whether two or three FX alphas trade at all.
What the study does establish is that the machinery is deterministic,
leak-free, and behaves exactly as pinned; ranking the policies would take
months of sessions, and the report says so in print.

**All bundled market data is synthetic** (seeded generator,
`python/src/iap/marketdata/generator.py`). Every research result is a
statement about this dataset and pipeline, not about real markets. Real
historical files you obtain yourself (Nasdaq TotalView-ITCH 5.0, LOBSTER)
can be ingested into the same pipeline: [docs/REAL_DATA.md](docs/REAL_DATA.md).

## Repository map

```
intraday-alpha-platform/
  PLATFORM_CONVENTIONS.md   binding cross-language engineering conventions
  API_CORE.md               contract: events / codec / order book / replay
  API_FEATURES.md           contract: feature engine (native 40 + registry 205)
  API_ALPHA.md              contract: the 6 golden production alphas
  API_PORTFOLIO_TCA.md      contract: portfolio optimizer + TCA (Java services)
  API_ADAPTIVE.md           contract: drift monitors / refit policies / live lifecycle
  API_CONTRACTS.md          contract: typed contracts, Protocols, schema index, validation
  API_TRADING.md            contract: the Python risk / execution reference ports + goldens
  LEARN.md                  textbook-style walkthrough of the whole platform
  COOKBOOK.md               task-oriented recipes (runnable commands)
  CONTRIBUTING.md           branching, parity harness, golden regeneration, promotion-gate rule
  docs/                     SPECIFICATION.md, ARCHITECTURE.md, DIAGRAMS.md, BUILD_NOTES.md,
                            HOW_IT_WORKS.md, POLYGLOT.md, REAL_DATA.md, RESEARCH_VALIDITY.md,
                            SCENARIOS.md, MVP.md, LIFECYCLE.md, DECISION_TRACE.md,
                            DATA_MODEL.md, ROADMAP.md, EPICS.md (generated),
                            runbooks/ governance/ papers/ diagrams/ index.html
  schemas/                  versioned JSON Schema contracts by domain (market/
                            features/ alpha/ order/ execution/ risk/ portfolio/
                            tca/ research/ trace/) + sql/iap_v2.sql (portable DDL)
                            + README.md index, FORMAT.md (wire layout), MIGRATIONS.md
  configs/                  by domain: instruments/ venues/ marketdata/ risk/
                            execution/ strategies/ (strategies.json, alpha_params.json,
                            lifecycle.json) mvp/ (the MVP session + its 3-venue universe)
  data/                     raw/ normalized/ features/ reference/ (generated, seeded);
                            mvp/<run_id>/ and store/ are run outputs (git-ignored)
  tests/README.md           the six-level testing strategy + exact commands
  tests/golden/             cross-language golden vectors + expected outputs
  tests/integration/        cross-component end-to-end runs (pytest, repo root)
  tests/replay/             determinism: same seed => identical bytes (pytest)
  tests/harness/            run_all.sh / run_golden.sh / check_deployment.py /
                            check_headline_numbers.py — one-command CI
  benchmarks/               per-language benchmarks + methodology
  python/  src/iap/         reference implementation + research stack:
                            core marketdata orderbook replay features labels alpha
                            validation experiment models portfolio tca backtest adaptive
                            contracts (types/Protocols/validation) risk (Rust-equivalent
                            port) execution (C++-equivalent port) lifecycle (7 states)
                            trace (DecisionTrace, sinks, digest) store (SQLite index)
                            research (ExperimentRunner) mvp (the traced loop)
                            agents (broker, blackboard, prereg, signing) llm (the
                            optional LLM research agent + evals, extra [llm])
                            mlops (model registry, monitoring, shadow mode)
  cpp/                      CMake project: codec, book, features, alpha, execution, SOR,
                            replay, contracts (canonical JSON + decision trace) (+ bench_all)
  rust/                     cargo workspace (11 crates): marketdata, orderbook, eventbus,
                            features, alpha, risk, venue, replay, telemetry,
                            contracts (canonical JSON / DecisionTrace / trace digest),
                            lifecycle (alpha promotion state machine + registry);
                            rust/features_py: the pyo3 wheel iap_features_rs, built
                            outside the workspace (v1.11)
  java/                     javac build: com.iap.* — full platform layer + paper trading
                            (+ contracts, trace, lifecycle; PaperTrading emits decision traces)
  research/                 alpha_reports/, ml_reports/, adaptive_reports/, baselines/,
                            tca/, models/, experiments/<id>/{spec,result}.json
                            (+ eligibility.json for runs made since v1.3.0),
                            experiments.json (the ledger), alpha_registry.json,
                            lifecycle_transitions.jsonl, lifecycle_log.jsonl,
                            power/ (planted-signal power study: generator config + report)
  deployment/               docker/, k8s/, grafana/, prometheus/, alertmanager/
  tools/github/             issues.yaml (epics/issues source of truth) + create_issues.py
```

## Quick start

Prerequisites are already the environment baseline: Python 3.11 (+ pyarrow),
g++ 13 / CMake / GoogleTest, Rust (the toolchain is pinned to 1.98.1 in
`rust/rust-toolchain.toml`), Java 21 (JUnit4 jar at
`/usr/share/java/junit4.jar` — there is deliberately **no Maven**; see
[docs/BUILD_NOTES.md](docs/BUILD_NOTES.md)).

```bash
# 1. Generate the seeded synthetic dataset (raw → normalized → Parquet + QC)
cd python && PYTHONPATH=src python3 -m iap.marketdata && cd ..

# 2. Run each language's test suite
cd python && PYTHONPATH=src python3 -m pytest -q && cd ..
cd cpp    && bash build.sh && ctest --test-dir build --output-on-failure && cd ..
cd rust   && cargo test && cd ..
cd java   && bash build.sh && bash test.sh && cd ..

# 3. Or all four + the repo-level integration/replay suites + the parity table
bash tests/harness/run_all.sh              # add --golden-only for the fast parity check
python3 -m pytest -q tests/integration tests/replay   # the two repo-level suites alone

# 4. Run the whole platform loop on one instrument, fully traced (docs/MVP.md; under a minute)
cd python && PYTHONPATH=src python3 -m iap.mvp run                    # -> ../data/mvp/<run_id>/
PYTHONPATH=src python3 -m iap.mvp verify                              # run twice, identical bytes
PYTHONPATH=src python3 -m iap.mvp replay --run ../data/mvp/<run_id>   # same digest from the capture
PYTHONPATH=src python3 -m iap.mvp explain --run ../data/mvp/<run_id> 5   # one order's chain
cd ..

# 5. Research → ledger → lifecycle → store
cd python && PYTHONPATH=src python3 -m iap.research run --alpha EQ03 --horizon 1s   # one ledgered experiment
PYTHONPATH=src python3 -m iap.lifecycle bootstrap --dry-run           # 24 alphas -> CANDIDATE, 0 beyond
PYTHONPATH=src python3 -m iap.lifecycle status                        # the registry table
PYTHONPATH=src python3 -m iap.store build                             # SQLite index of every artefact
cd ..

# 6. Run the research pipelines (features → alphas → ML → TCA)
cd python && PYTHONPATH=src python3 -m iap.features && cd ..     # ~1 min
PYTHONPATH=python/src python3 research/alpha_reports/run_all.py  # 24-alpha promotion report (default methods, v2)
PYTHONPATH=python/src python3 research/alpha_reports/run_all.py --methods legacy_v1 --out-dir /tmp/legacy_v1   # the v1.4.0 report, reproduced
PYTHONPATH=python/src python3 research/ml_reports/run_ml.py      # gated ML + meta-labeling
PYTHONPATH=python/src python3 research/adaptive_reports/run_adaptive.py  # adaptive policy study
cd python && PYTHONPATH=src python3 -m iap.tca && cd ..          # TCA report

# 7. Paper trading (Java platform: book → features → alphas → portfolio →
#    risk → execution → decision trace, with /metrics, /health, /ready, /status on :8080)
bash java/paper.sh                          # asap replay of the golden vector
bash java/paper.sh --mode realtime --speed 60   # paced session you can scrape
bash java/paper.sh --resume                 # continue from java/out/state
#   (positions, realized P&L, any latched kill switch and the decision-trace
#    digest survive a restart — PLATFORM_CONVENTIONS.md §12.3)

# 8. Validate the deployment the way CI does, and the build plan
python3 tests/harness/check_deployment.py
python3 tests/harness/check_headline_numbers.py   # every number in this README vs its artefact
python3 tools/github/create_issues.py --dry-run   # the epics/issues plan (docs/EPICS.md)
```

## Cross-language parity (the `tests/harness/run_all.sh` table, counts from CI)

```
===================== cross-language parity table =====================
language | tests passed | golden passed  | time   | status
---------+--------------+----------------+--------+-------
python   | 2185         | 193            |    -s | PASS
cpp      | 302          | 72             |    -s | PASS
rust     | 358          | 71             |    -s | PASS
java     | 571          | 124            |    -s | PASS
integration | 35           | -              |    -s | PASS
replay   | 6            | -              |    -s | PASS
deployment | -            | -              |    -s | PASS
numbers  | -            | -              |    -s | PASS
=======================================================================
deployment checks: 26 passed, 0 failed, 0 skipped
headline numbers: all headline numbers match their artefacts
(a '-' count means the suite did not run in this mode, or has no golden
 group (integration/replay); '?' means it ran but its count could not be
 parsed — a '?' or FAIL anywhere fails the run.)
>> PARITY OK — all languages passed (full suites).
```

The policy of [docs/POLYGLOT.md](docs/POLYGLOT.md) (v1.11.0) froze 20 of the
duplicated language copies and retired none, so these counts are unchanged by it.

(Counts for v1.5.0, 2026-10-04. They are taken from the CI jobs of the
release line rather than from one local harness run: each CI job runs the
canonical commands of the matching `run_all.sh` row, and CI installs
`promtool` and `kubeconform`, so no deployment check is skipped there — a
local run without those tools reports them as skipped and validates the
rule files structurally instead. The time column is left blank because the
CI jobs also build coverage and sanitizer trees, so their durations are not
the harness's. The Java and Rust counts are additionally re-derived from
the source tree (`@Test` / `#[test]`) by `check_headline_numbers.py`. The
v1.3.0 growth is the new regression tests: fail-closed risk rules and the
edge golden in three languages, the simulator fill rules, the Java
`PlatformSafetyTest`, and the research-validity and research-store suites
in Python. The v1.5.0 growth is the tests of the default and legacy
research methods in Python (among them the comparison of `legacy_v1` with
two v1.4.0 reports), the CUSUM and ledger-threshold rules with the
lifecycle scenarios LC04 and LG01 in Python, Java and Rust, the
release-guard tests in `integration`, and the tests of the work merged into
the release: the ITCH 5.0 / LOBSTER readers and the ingest path, the
passive execution policy and markouts (Python, Java, C++), signal
combination and the two lifecycle gates with scenarios LC05 and LC06
(Python, Java, Rust), the scope-aware store, the Java research backtester
golden, the session-exit marker, and the risk differential-fuzz corpus
replayed by Rust, Java and Python. The `golden passed`
column counts each language's golden-group tests: byte-exact IAP1 SHA-256
codec parity, exact-integer book states, 1e-9-tolerance
feature/alpha/portfolio/TCA/fill comparisons, exact risk decisions with
byte-identical audit and snapshot, the adaptability goldens — PSI/KS at
1e-10, exact refit-decision booleans and lifecycle state sequences — and,
since 2026-09-19, the cross-language contract goldens: canonical JSON (2663
float reprs incl. 612 rounding-tie and 17-digit cases, 24 escapes, 9 documents), the trace id and trace digests, one
pinned instance per contract with the `explain()` block, the 7-state
lifecycle scenarios and the registry bytes, the experiment golden frame and
the MVP session — all against `tests/golden/`. The Java golden column runs
**all seventeen** `com.iap.*GoldenTest` classes and the Rust column ten
golden targets; a harness case fails if either gate list ever drifts from
the files on disk. Python's golden group now includes the risk and fills
goldens that Rust and C++ generate, consumed by `iap.risk` and
`iap.execution` exactly as the Java ports consume them.)

The `integration` and `replay` rows are the repo-level pytest suites
`tests/integration` (the pipeline smoke chain, the GitHub issue plan, and
`python -m iap.mvp` end to end as a subprocess) and `tests/replay`
(generator determinism; the MVP run twice and replayed from its capture —
same seed ⇒ identical bytes), run from the repository root with no
`PYTHONPATH` (`tests/conftest.py`); they have no golden group and count
toward the verdict like the language rows. The six-level testing strategy —
unit, golden, replay, integration, research validation, deployment — is laid
out with the exact commands in [tests/README.md](tests/README.md).

The last two rows are not test counts: `deployment` is
`tests/harness/check_deployment.py` (promtool rules/config/unit tests,
`docker compose config`, Dockerfile COPY sources against a clean checkout,
k8s manifests + singleton shape, ConfigMap sync and the ConfigMap `items[]`
that project the nested `configs/<domain>/` tree — including `configs/mvp/`
and `strategies/lifecycle.json` — dashboard metric provenance, Java
golden-gate completeness), and `numbers` is
`tests/harness/check_headline_numbers.py`, which re-derives every headline
figure in this README — test and golden counts, the ledger denominator, the
24 alphas / 24 CANDIDATE, the MVP counts and P&L, the schema, contract and
Protocol counts, the benchmark figures — from the artefact that produces it.

Four independent implementations of one pinned semantics, held identical by
golden tests — the engineering discipline this repo is built around
(spec §21; [paper 6](docs/papers/06_cpp_vs_rust_vs_java_event_driven.md)).

## Documentation index

| document | what it covers |
|---|---|
| [LEARN.md](LEARN.md) | textbook walkthrough: microstructure, generator, book, features, honest alpha research, ML/meta-labeling, portfolio, risk, execution, TCA, parity, latency economics, adaptability, contracts & Protocols, the Python risk/execution reference, the 7-state lifecycle, the decision trace, the data model, the MVP walkthrough with its honest numbers, the v1.3.0 review as six case-study chapters (fail-closed risk bugs, simulator realism, statistical power, gate gaming, crash consistency, supply-chain hygiene), pitfalls, interview Q&A, combining signals, adverse selection, five v1.9-v1.10 case studies (why taker-only research cannot pay at 1 s, the FOMC sampling flaw and day-clustered inference, the 2026 holdout as pre-registration, what the synthetic maker/quoting results show, governance keys and code hashes), and four from v1.11 (parity proves agreement, not correctness: the snapshot-burst bug; why an LLM must never compute a number; freezing copies instead of deleting them; model registries as evidence) |
| [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) | how the quant, algo and AI sides work — a guided explanation for a newcomer: the pipeline on one page, the research statistics and gates, the execution algorithms and simulator rules, the fail-closed risk engine, the ML layer with its negative results, the LLM/agent boundary (what exists, what is backlog, what would be theatre on this data), determinism and replay; since v1.10 also the real-data path and the `v3` validity bundle, the maker path (calibrate, post or quote, decompose), auctions, optimal execution and the governance chain; since v1.11 the LLM research agent and its controls, the Rust feature engine from Python, the model registry and the polyglot policy; every section ends with where to look and a command that runs |
| [COOKBOOK.md](COOKBOOK.md) | 55 task-oriented recipes with runnable commands (39-55: maker backtest, quoting, optimal execution, auctions, parallel features, the `v3` validity block, calibrating and backtesting a real session, signed pre-registration, the Almgren-Chriss frontier, the Rust feature engine from Python and building its wheel with maturin, the model registry, shadow mode and week-by-week monitoring, the LLM research agent, its evals and a guarded session with the scripted client, the `POLYGLOT-OVERRIDE` workflow) |
| [docs/REAL_DATA.md](docs/REAL_DATA.md) | real historical data: what `python -m iap.marketdata ingest` reads (Nasdaq TotalView-ITCH 5.0, LOBSTER), how to obtain files yourself (nothing is bundled), the commands from a downloaded file to an alpha report, the mapping table to canonical events, the point-in-time security master and corporate-actions table, known limitations, and what a first real-data study can and cannot conclude |
| [docs/RESEARCH_VALIDITY.md](docs/RESEARCH_VALIDITY.md) + [research/power/POWER_REPORT.md](research/power/POWER_REPORT.md) | the corrected research methods (the defaults since v1.5.0, each with its named legacy rule), the research store under parallel writers, gate eligibility; the planted-signal power study of the validation chain |
| [CHANGELOG.md](CHANGELOG.md) | release notes, newest first (v1.11.0: the Rust feature engine via pyo3, the polyglot policy, the model registry, the LLM research agent and its evals; v1.10.0: skewed quoting, auctions, optimal execution, governance G1-G4; v1.9.0: research validity options, parallel features, maker economics; v1.8.0: the 2026 holdout and the ingest memory fix; v1.6.0-v1.7.2: real data, the agent layer, signal combination, real-data power; v1.5.0: the corrected research methods become the defaults, every old rule keeps a legacy name, every dataset-derived artefact regenerated; v1.4.0: the generator's equity flow calibration fixed so flow reaches the close, and every dataset-derived artefact regenerated; v1.3.0: fail-closed risk, simulator fill rules, paper-platform safety, governance and deployment hardening, research validity) |
| [docs/MVP.md](docs/MVP.md) | the executable MVP (`python -m iap.mvp run / replay / verify / explain`): one deterministic, fully traced trading loop on a synthetic equity — the loop module by module, the §11.4 wiring rules with code references, the determinism contract, the incident replay flow, the honest golden-run results (cost-negative) with the realized-IC audit, and the success-criteria table |
| [docs/LIFECYCLE.md](docs/LIFECYCLE.md) | the 7-state promotion lifecycle: states, the 17-edge transition table, the 20 gates with config keys and defaults, evidence documents, registry and transition-log formats, the bootstrap result (24 CANDIDATE / 0 beyond), the golden, the Java/Rust ports, the RETIRED-is-observational caveat |
| [docs/DECISION_TRACE.md](docs/DECISION_TRACE.md) | the decision trace: the record, ids, canonical JSON rules, the stream digest with its known answers, sinks, the pinned `explain()` block, store views, emission points in Python / Java / C++ / Rust, incident replay |
| [API_CONTRACTS.md](API_CONTRACTS.md) | the contract layer: 22 typed contracts field by field, ids and canonical JSON, validation, the 18 Protocols and what satisfies them, versioning, the 17-schema index |
| [API_TRADING.md](API_TRADING.md) | the Python reference ports of the hard risk engine (`iap.risk`) and the execution stack (`iap.execution`): public APIs, golden parity statements, what is pinned about each port |
| [docs/ROADMAP.md](docs/ROADMAP.md) | the six-week plan (Phase 0 → Week 6) and Phase 2/3 mapped to what exists with evidence, what is backlog, the MVP success criteria; §3.6 is v1.11 (done, running, deferred) |
| [docs/POLYGLOT.md](docs/POLYGLOT.md) | which language copy of each duplicated component is canonical and which is frozen (v1.11.0, plan E3): inventory, golden pins, consumers, the `POLYGLOT-OVERRIDE:` rule enforced by `tests/harness/check_polyglot_policy.py` (COOKBOOK 53) |
| [docs/governance/GOVERNANCE.md](docs/governance/GOVERNANCE.md) §2b | the LLM research agent's controls (v1.11.0, AI1-AI2): tool allowlist, a signing key the model never sees, pre-registration caps, the numbers rule, budgets, persisted transcripts, the evals and what they do not cover |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | system design, per-language responsibilities, contracts, the research → trading → execution → adaptive loop with lifecycle and trace, determinism, golden topology, hot-path notes, observability, the MVP vertical, deployment, the AI / agent boundary (what is pinned, what exists, what is backlog), failure modes and fail-closed design, the research-store concurrency model, and the distance to a production system |
| [docs/DIAGRAMS.md](docs/DIAGRAMS.md) | all twenty-nine architecture diagrams on one page (pipeline, golden topology, paper trading, responsibility matrix, risk decision flow, queue-position model, data model, lifecycle state machine, decision-trace chain, MVP loop; and, since v1.3.0, the fail-closed risk branches, the simulator fill/queue flow, the paper-platform checkpoint commit point and resume, the admin kill latch, the research run with ledger lock and eligibility, the power study, the CI/release pipeline, the deployment topology, the agent layer as planned at v1.3.0; and, since v1.9-v1.10, the simulator calibration flow, the maker and quoting P&L decomposition, the auction pipeline, the governance (prereg, anchoring, signing) sequence, the v3 validity flow and the v1.9-v1.11 roadmap; and, since v1.11, the LLM agent loop with its controls, the model registry and shadow flow, the polyglot canonical/frozen map and the Rust/Python feature backends) |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | the relational data model (`schemas/sql/iap_v2.sql`, SQLite + PostgreSQL): every table, the dataset- and bundle-scoped views, portability rules, how the store indexes the flat-file artefacts, query cookbook |
| [docs/index.html](docs/index.html) + [docs/GITHUB_PAGES.md](docs/GITHUB_PAGES.md) | the GitHub Pages landing site and how to publish it (Settings → Pages → main branch, /docs folder) |
| [docs/SPECIFICATION.md](docs/SPECIFICATION.md) | the governing institutional specification (verbatim) |
| [PLATFORM_CONVENTIONS.md](PLATFORM_CONVENTIONS.md) | binding conventions: types, serialization, determinism, book semantics, golden rules, trading contracts (§11: risk engine, execution simulator, SOR/algos, paper wiring, currency), platform/deployment (§12), contracts / lifecycle / trace / data model (§13) |
| [docs/SCENARIOS.md](docs/SCENARIOS.md) | real-life scenarios (feed gaps, halts, clock regressions, loss latches, re-arms, FX notionals, lifecycle demotions, trace replay mismatches, NO_ROUTE, …) → pinned behaviour → contract clause → the tests in each language |
| [API_CORE.md](API_CORE.md) / [API_FEATURES.md](API_FEATURES.md) / [API_ALPHA.md](API_ALPHA.md) / [API_PORTFOLIO_TCA.md](API_PORTFOLIO_TCA.md) / [API_ADAPTIVE.md](API_ADAPTIVE.md) | the five original port contracts (API_CONTRACTS.md and API_TRADING.md above make seven) |
| [docs/BUILD_NOTES.md](docs/BUILD_NOTES.md) | per-language build/test commands; the no-Maven rationale and pom-equivalent table |
| [docs/papers/INDEX.md](docs/papers/INDEX.md) | six flagship research papers/case studies (spec §28) with dated errata |
| [research/alpha_reports/REPORT.md](research/alpha_reports/REPORT.md) | the honest 24-alpha promotion report |
| [research/ml_reports/ML_REPORT.md](research/ml_reports/ML_REPORT.md) | gated model comparison + meta-labeling (incl. the crossed-book artifact story) |
| [research/adaptive_reports/ADAPTIVE_REPORT.md](research/adaptive_reports/ADAPTIVE_REPORT.md) | the honest adaptive-deployment study: static vs scheduled vs drift-triggered refits, lifecycle retirements, and what two sessions cannot prove |
| [research/tca/TCA_REPORT.md](research/tca/TCA_REPORT.md) | simulated parent-order TCA |
| [research/experiments/README.md](research/experiments/README.md) | the ExperimentRunner's spec / result documents and the fifteen committed experiments (five under the default methods, five under the rules up to v1.4.0, five kept from the v1.3.0 dataset) |
| [benchmarks/RESULTS.md](benchmarks/RESULTS.md) | benchmark index; C++ table in [results_cpp.md](benchmarks/results_cpp.md) + methodology |
| [docs/runbooks/](docs/runbooks/) | data pipeline, backtest, paper trading, kill-switch incident, incident replay runbooks |
| [docs/governance/](docs/governance/) | governance, reproducibility, security |
| [docs/EPICS.md](docs/EPICS.md) + [CONTRIBUTING.md](CONTRIBUTING.md) | the build plan as epics/issues with honest done / backlog status (generated from `tools/github/issues.yaml`; nothing in progress as of 2026-10-04); how to contribute |
| [schemas/README.md](schemas/README.md) / [schemas/FORMAT.md](schemas/FORMAT.md) / [schemas/MIGRATIONS.md](schemas/MIGRATIONS.md) | the 17-schema index + the SQL DDL; normative wire layout (JSONL + IAP1 binary); every versioned change |
| [tests/README.md](tests/README.md) | the six-level testing strategy with exact commands and the golden inventory |
| [deployment/grafana/README.md](deployment/grafana/README.md) | dashboards and observability stack (incl. the trace metrics) |
| [python/src/iap/README.md](python/src/iap/README.md) | the Python package map, module by module |
| [Real-world usage notes](#real-world-usage-notes) / [References](#references) | scope, units and out-of-scope items for a live deployment; the literature and standards the platform implements |

## Real-world usage notes

What this repository is, and is not, if you are evaluating it against a
live deployment.

**Data.** Every event in `data/` is produced by the seeded synthetic
generator (`python/src/iap/marketdata/generator.py`: regime-switching
efficient price, AR(1) venue noise, Hawkes-style clustered order flow, real
FIFO queue dynamics, auctions/halt, injected QC anomalies). No real venue
data, symbols, or fee schedules are included — instruments are `SYN.EQ.*` /
`SYN.ETF.IDX` / eight synthetic G10 pairs on venues `XV1`, `XV2`, `LP1`,
`LP2`, `PRI`. Every number in the reports is a statement about this
generator and this pipeline, not about any market.

**Equity flow covers the whole session since v1.4.0.** The generator's
default flow calibration is `equities.flow.calibration = "session"`
(`configs/marketdata/generator.json`, `x-version` 2): the base rate of the
self-exciting flow is `slots_per_stream × excitation_time_factor /
duration`, where `excitation_time_factor` = E[1 / (1 + excitation)] (0.522
for the pinned config), there is no slot budget, and each equity stream
trades up to the close auction. FX flow is unchanged and runs to 92–100%
of its 21-hour session. Up to v1.3.0 a slot budget was spent early and
equity flow stopped 37.6–43.2% of the way through each 6.5-hour session;
`"legacy_budget"` reproduces that dataset (`data_version` `203c8f54…`) byte
for byte, and `equities.fill_session` belongs to the legacy rule only (it
is an error with the default). `tests/replay/test_generator_determinism.py`
pins the raw-file hashes of both datasets; backlog issue M08 is done
(docs/EPICS.md). The consequence to keep in mind when reading the equity
numbers: about the same number of events is now spread over the whole
session, so equity rows are 3.1–3.3 s apart (median quote gap about 2 s)
and, under the label freshness rule `max(5 s, 2 × median quote gap)`, a
share of equity labels at horizons of 10 s and longer is invalid as stale
(valid fraction 0.79–0.81 at 10 s, 0.45–0.63 at 15 m;
`data/features/features_summary.json`).

**Units and conventions (binding, `PLATFORM_CONVENTIONS.md` §1).**

| quantity | representation |
|---|---|
| prices | `int64 price_ticks`; real price = ticks × `tick_size` (per instrument, `configs/instruments/instruments.json`); never a float on a contract or hot path |
| quantities | `int64 qty` in base units (equity shares; FX 1 unit = 1,000 base currency, `lot_size`) |
| timestamps | `int64` nanoseconds since the Unix epoch, `exchange_ts` (event time — all windows, labels, splits) and `receive_ts` (arrival; `receive_ts ≥ exchange_ts`) |
| costs, slippage, IC-scale returns | basis points of mid / notional; fees per share (equities, negative = maker rebate) or per million notional (FX), `configs/venues/venues.json` |
| P&L, research metrics | `double`, compared across languages at 1e-9 absolute/relative tolerance |
| currency | every aggregated P&L / notional / cost figure is in the reporting currency (USD, `configs/risk/risk.json` `currency`); FX quote-currency figures are converted per increment at the prevailing conversion-pair mid, never summed as dollars (`PLATFORM_CONVENTIONS.md` §11.6) |
| randomness | one pinned RNG (SplitMix64, `PLATFORM_CONVENTIONS.md` §3); same seed ⇒ bit-identical files |
| calendar | a five-day synthetic calendar in UTC (`configs/instruments/instruments.json`); no exchange holidays, DST, or session-time rules |

**What is validated.** Cross-language parity of the pinned semantics
(codec bytes, book states, features, alphas, portfolio, TCA, risk
decisions, fills, drift/refit/lifecycle, canonical JSON, decision traces,
the 7-state lifecycle) via `tests/golden/`; leakage tests (label-column
guard, shift-by-one) on every alpha; purged and embargoed walk-forward
statistics with a recorded multiple-testing denominator; deterministic
replay; fail-closed risk gating; the MVP loop run twice and replayed from
its capture; and the build/test commands in `docs/BUILD_NOTES.md`.
Benchmarks are mean-only figures from a two-CPU container without pinning
(`benchmarks/RESULTS.md`).

**The MVP is synthetic, and so is its third venue.** `python -m iap.mvp`
trades one synthetic instrument (`SYN.EQ.AAPL`, instrument 12, not part of
the bundled universe) over one 15-minute synthetic session generated by the
same seeded generator; its venues are XV1 and XV2 from
`configs/venues/venues.json` plus **XV3**, a synthetic third venue defined
only in `configs/mvp/venues.json` (cheapest taker fee, lowest rebate,
slowest latency) so that the SOR has a real choice to make. The golden
example trace in `tests/golden/expected_contracts_examples.json` uses the
same synthetic venue 3. Nothing about XV3 describes any real venue.

**The realized-IC audit (docs/MVP.md §7.1).** The MVP's realized IC (EQ01
0.217, EQ03 0.110 at 1 s) sits well above the research IC of the same
fitted alphas (0.011 / 0.019) — about twenty times for EQ01, six for EQ03 —
and EQ06's is negative on this session (−0.079 at 1 s against a research
IC of 0.028). The audit of 2026-09-20, made on the v1.3.0 session,
concluded that the gap is a property of the data, not a leak: the generator
quotes every venue around one shared efficient price with a bounded AR(1)
venue noise (ρ 0.9 per slot) and cancels resting orders the efficient price
has moved through, so the displayed book leans towards the efficient price
and the mid converges to it — which is exactly what microprice and OFI
measure. The leak evidence is the pinned label definition (the realized IC
is computed by `iap.labels.compute_labels`) and the truncation probe, which
reproduces every earlier signal bit for bit. The shift-by-one probe is not
part of that evidence on the current session: it no longer collapses the
IC (EQ01 0.149 shifted against 0.217, EQ03 0.116 against 0.110), because
with flow spread over the whole session the signals persist from one
decision to the next. That is persistence, not look-ahead, and the probe
cannot tell the two apart at a cadence equal to the horizon. The
cost-adjusted IC (−0.024 / −0.029) and the −81.53 USD session say the same
thing the research reports say: a real feed would not be this kind, and
even this one does not pay the spread.

**Out of scope for a live deployment** (each would be a project of its own):

- real feed handlers and venue protocols (ITCH/OUCH/FIX and vendor APIs) —
  the "venue protocol" here is a simulator (`rust/venue`) speaking the
  platform's own length-prefixed `IAPV1` order/report framing;
- exchange certification, order-entry conformance testing, drop copy,
  and clearing/settlement integration;
- regulatory compliance controls (pre-trade risk checks in the sense of
  SEC 15c3-5 / MiFID II RTS 6, best-execution reporting, surveillance,
  audit retention) — the risk engine implements the platform's own pinned
  limits, not a regulatory rulebook;
- real trading calendars, corporate actions, symbology and reference-data
  feeds, and fee schedules;
- real cost and impact models — the research cost model is half-spread +
  fee + square-root impact in ADV (`configs/execution/execution.json`,
  `impact_model` `"sqrt"`; `"linear"` is the legacy rule, and the execution
  simulator's impact stays linear), and the queue-position
  fill model is a documented simplification
  ([paper 5](docs/papers/05_queue_aware_execution_adverse_selection.md));
- production hardening: the read endpoints (`/metrics` `/health` `/ready`
  `/status`) are unauthenticated and rely on the NetworkPolicy — only the
  write surface (`POST /admin/*`, the kill switch) is token-authenticated,
  rate-limited on failed authentication and audited, and the listener binds
  loopback unless `IAP_BIND_ADDR` says otherwise; and there is no HA/failover (the trading vertical is a deliberate
  singleton) or kernel-bypass networking. The C++ latency figures are
  single-threaded in-memory measurements, not tick-to-trade on a real network.

## References

Works the platform implements, follows, or documents. Only items actually
used in the code or the write-ups are listed; where the implementation is a
deliberate simplification of the cited method the note says so. The
2026-09-19/20 release (contracts, Python risk/execution ports, lifecycle,
trace, store, ExperimentRunner, MVP) cites nothing new beyond the standards
already listed here.

### Market microstructure and alpha

1. Cont, R., Kukanov, A., & Stoikov, S. (2014). The Price Impact of Order
   Book Events. *Journal of Financial Econometrics*, 12(1), 47–88.
   <https://doi.org/10.1093/jjfinec/nbt003> (preprint:
   <https://arxiv.org/abs/1011.6402>). — Order-flow imbalance (OFI); the
   `ofi_*` feature family (`python/src/iap/features/orderflow.py`) and
   alphas EQ02/EQ03; paper 1.
2. Stoikov, S. (2018). The Micro-Price: A High-Frequency Estimator of
   Future Prices. *Quantitative Finance*, 18(12), 1959–1966.
   <https://doi.org/10.1080/14697688.2018.1489139> (preprint:
   <https://ssrn.com/abstract=2970694>). — The size-weighted microprice
   `microprice_v1` (`python/src/iap/features/microstructure.py`), alphas
   EQ01/FX01; paper 2. Note: the platform uses the one-level size-weighted
   estimator, not Stoikov's Markov-chain refinement.
3. Hawkes, A. G. (1971). Spectra of Some Self-Exciting and Mutually
   Exciting Point Processes. *Biometrika*, 58(1), 83–90.
   <https://doi.org/10.1093/biomet/58.1.83>. — The generator's self-exciting
   ("Hawkes-style") order-flow intensity with exponential decay
   (`python/src/iap/marketdata/generator.py`); a discretized simplification.
4. Almgren, R., Thum, C., Hauptmann, E., & Li, H. (2005). Direct Estimation
   of Equity Market Impact. *Risk*, 18(7), 58–62. — The square-root shape
   behind the `expected_impact_bps_v1` proxy
   (`python/src/iap/features/execution.py`); a proxy only, not the fitted
   model.

### Execution and transaction-cost analysis

5. Perold, A. F. (1988). The Implementation Shortfall: Paper versus
   Reality. *Journal of Portfolio Management*, 14(3), 4–9.
   <https://doi.org/10.3905/jpm.1988.409150>. — The pinned IS decomposition
   (delay + trading + opportunity) in `python/src/iap/tca/tca.py`, the Java
   TCA service, and `API_PORTFOLIO_TCA.md` §2.2.
6. Almgren, R., & Chriss, N. (2000). Optimal Execution of Portfolio
   Transactions. *Journal of Risk*, 3(2), 5–39.
   <https://doi.org/10.21314/JOR.2001.041>. — The impact/urgency trade-off
   that motivates the IS algorithm's `risk_aversion` parameter
   (`cpp/include/iap/execution/algos.hpp`). The pinned schedule is a
   front-loaded exponential decay, not the closed-form Almgren–Chriss
   trajectory.

### Portfolio construction and risk

7. Markowitz, H. (1952). Portfolio Selection. *Journal of Finance*, 7(1),
   77–91. <https://doi.org/10.1111/j.1540-6261.1952.tb01525.x>. — The
   mean–variance objective `alpha'w − λ w'Σw − Σ tc·|w − w_prev|` solved by
   the pinned projected-gradient optimizer
   (`python/src/iap/portfolio/optimizer.py`, `API_PORTFOLIO_TCA.md` §1).
8. J.P. Morgan/Reuters (1996). *RiskMetrics — Technical Document*, 4th ed.
   New York. — The EWMA covariance recursion with λ = 0.94
   (`python/src/iap/portfolio/covariance.py`).

### Validation, multiple testing, and machine learning

9. López de Prado, M. (2018). *Advances in Financial Machine Learning*.
   Wiley. ISBN 978-1-119-48208-6. — Purging and embargo in walk-forward
   splits (`python/src/iap/validation/splits.py`, `python/src/iap/models/splits.py`;
   ch. 7), meta-labeling (`python/src/iap/models/metalabel.py`; ch. 3), and
   the multiple-testing / deflated-Sharpe discipline of the experiments
   ledger (ch. 14).
10. Bailey, D. H., & López de Prado, M. (2014). The Deflated Sharpe Ratio:
    Correcting for Selection Bias, Backtest Overfitting, and Non-Normality.
    *Journal of Portfolio Management*, 40(5), 94–107.
    <https://doi.org/10.3905/jpm.2014.40.5.094> (preprint:
    <https://ssrn.com/abstract=2460551>). — The "expected max |t| under the
    global null ≈ √(2 ln n)" selection yardstick printed in every report
    (`python/src/iap/validation/ledger.py`); a deflated-Sharpe-*style* note,
    not the full DSR with skew/kurtosis terms.
11. Newey, W. K., & West, K. D. (1987). A Simple, Positive Semi-Definite,
    Heteroskedasticity and Autocorrelation Consistent Covariance Matrix.
    *Econometrica*, 55(3), 703–708. <https://doi.org/10.2307/1913610>. — The
    Bartlett-weighted long-run variance in the pinned "Newey–West-lite"
    t-statistic (`python/src/iap/validation/metrics.py`; fixed lag L = 2
    rather than a bandwidth rule).
12. Bonferroni, C. E. (1936). Teoria statistica delle classi e calcolo delle
    probabilità. *Pubblicazioni del R. Istituto Superiore di Scienze
    Economiche e Commerciali di Firenze*, 8, 3–62. — The per-test threshold
    `alpha / n_experiments` in the experiments ledger.
13. Harvey, C. R., Liu, Y., & Zhu, H. (2016). … and the Cross-Section of
    Expected Returns. *Review of Financial Studies*, 29(1), 5–68.
    <https://doi.org/10.1093/rfs/hhv059>. — Context for the spec's
    t ≥ 3.0 promotion hurdle and for reporting the number of trials; the
    repository does not implement their Bayesianized p-values.
14. Zadrozny, B., & Elkan, C. (2002). Transforming Classifier Scores into
    Accurate Multiclass Probability Estimates. *Proceedings of KDD '02*,
    694–699. <https://doi.org/10.1145/775047.775151>. — Isotonic probability
    calibration of the meta-label gate (via scikit-learn's
    `CalibratedClassifierCV(method="isotonic")`).
15. Brier, G. W. (1950). Verification of Forecasts Expressed in Terms of
    Probability. *Monthly Weather Review*, 78(1), 1–3.
    <https://doi.org/10.1175/1520-0493(1950)078%3C0001:VOFEIT%3E2.0.CO;2>.
    — The Brier score reported alongside AUC in `ML_REPORT.md`.
16. Chen, T., & Guestrin, C. (2016). XGBoost: A Scalable Tree Boosting
    System. *Proceedings of KDD '16*, 785–794.
    <https://doi.org/10.1145/2939672.2939785>. — Tier-1 model in the gated
    zoo (`python/src/iap/models/zoo.py`).
17. Ke, G., Meng, Q., Finley, T., Wang, T., Chen, W., Ma, W., Ye, Q., &
    Liu, T.-Y. (2017). LightGBM: A Highly Efficient Gradient Boosting
    Decision Tree. *Advances in Neural Information Processing Systems*, 30,
    3146–3154.
    <https://papers.nips.cc/paper/6907-lightgbm-a-highly-efficient-gradient-boosting-decision-tree>.
    — Tier-1 model in the gated zoo and the meta-label classifier.

### Drift monitoring

18. Kolmogorov, A. N. (1933). Sulla determinazione empirica di una legge di
    distribuzione. *Giornale dell'Istituto Italiano degli Attuari*, 4,
    83–91; and Smirnov, N. V. (1948). Table for Estimating the Goodness of
    Fit of Empirical Distributions. *Annals of Mathematical Statistics*,
    19(2), 279–281. <https://doi.org/10.1214/aoms/1177730256>. — The
    two-sample Kolmogorov–Smirnov statistic in
    `python/src/iap/adaptive/drift.py` (diagnostic only — KS never
    triggers a refit; the Java port implements PSI, rolling IC and
    lifecycle, `API_ADAPTIVE.md`).
19. Press, W. H., Teukolsky, S. A., Vetterling, W. T., & Flannery, B. P.
    (2007). *Numerical Recipes: The Art of Scientific Computing*, 3rd ed.
    Cambridge University Press, §14.3 (Kolmogorov–Smirnov test). — The
    asymptotic two-sample KS p-value form
    `λ = (√Nₑ + 0.12 + 0.11/√Nₑ)·D`, pinned at 100 series terms
    (`API_ADAPTIVE.md` §3).
20. Siddiqi, N. (2006). *Credit Risk Scorecards: Developing and
    Implementing Intelligent Credit Scoring*. Wiley. ISBN
    978-0-471-75451-0; and Yurdakul, B. (2018). *Statistical Properties of
    Population Stability Index*. PhD dissertation, Western Michigan
    University. <https://scholarworks.wmich.edu/dissertations/3208>. — The
    Population Stability Index (10 quantile buckets, ε = 1e-6) used by the
    drift monitors and the `alpha_live_vs_backtest_drift` gauge.

### Determinism and infrastructure

21. Steele, G. L., Lea, D., & Flood, C. H. (2014). Fast Splittable
    Pseudorandom Number Generators. *Proceedings of OOPSLA '14* (ACM SIGPLAN
    Notices 49(10)), 453–472. <https://doi.org/10.1145/2660193.2660195>. —
    SplitMix64, the single pinned RNG in all four languages
    (`python/src/iap/core/rng.py`, `cpp/include/iap/marketdata/rng.hpp`,
    `java/src/main/java/com/iap/core/SplitMix64.java`, `rust/marketdata`;
    `tests/golden/splitmix64.json`).
22. NIST (2015). *Secure Hash Standard (SHS)*, FIPS PUB 180-4.
    <https://doi.org/10.6028/NIST.FIPS.180-4>. — SHA-256 for the IAP1 codec
    parity digests, `feature_version`, `data_version`, `content_hash` /
    `config_version`, the trace id and the decision-trace stream digest
    (own streaming implementations in `rust/contracts/src/sha256.rs` and
    `cpp/include/iap/util/sha256.hpp`; `hashlib` / `MessageDigest` in
    Python and Java).
23. Wright, A., Andrews, H., Hutton, B., & Dennis, G. (2022). *JSON Schema:
    A Media Type for Describing JSON Documents*, draft 2020-12.
    <https://json-schema.org/draft/2020-12/json-schema-core>. — The
    versioned contracts in `schemas/<domain>/*.schema.json` (index:
    `schemas/README.md`), validated offline by `iap.contracts.validate`
    (python-jsonschema + `referencing`).
24. Apache Software Foundation. *Apache Parquet Format Specification*.
    <https://parquet.apache.org/docs/file-format/>. — The research dataset
    and feature store (`data/normalized/*.parquet`, `data/features/`).
25. Prometheus Authors. *Exposition Formats* (text-based format).
    <https://prometheus.io/docs/instrumenting/exposition_formats/>. — The
    `/metrics` endpoint of the Java platform and the Rust telemetry crate.

### Background texts cited in LEARN.md (context, not implemented)

- Harris, L. (2003). *Trading and Exchanges: Market Microstructure for
  Practitioners*. Oxford University Press.
- O'Hara, M. (1995). *Market Microstructure Theory*. Blackwell.
- Hasbrouck, J. (2007). *Empirical Market Microstructure*. Oxford
  University Press.
- Avellaneda, M., & Stoikov, S. (2008). High-Frequency Trading in a Limit
  Order Book. *Quantitative Finance*, 8(3), 217–224.
  <https://doi.org/10.1080/14697680701381228>. — Inventory-aware quoting;
  background only, no market-making quoter is implemented.
- Grinold, R. C., & Kahn, R. N. (2000). *Active Portfolio Management*, 2nd
  ed. McGraw-Hill. — IC and the fundamental law, context for the
  portfolio chapter.

## License

MIT License, Copyright (c) 2026 Ashish Jha — see [LICENSE](LICENSE). That
covers this repository's own code. The toolchain, the test libraries and the
container images pull in third-party software under its own terms (JUnit EPL-1.0,
Eigen MPL-2.0, GoogleTest BSD-3, OpenJDK GPL-2.0-with-classpath-exception,
Prometheus Apache-2.0, Grafana AGPL-3.0) — [NOTICE](NOTICE) records what,
where, and under which licence.

## Disclaimer

This platform is for **education and research engineering practice only**. All
market data is synthetically generated; no result here is a claim about real
markets, and nothing in this repository is investment advice or a solicitation
to trade. The honest-reporting standard (spec §32) exists precisely because
research truth — including negative results — is the product.
