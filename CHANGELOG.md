# Changelog

Release notes for tagged versions, newest first. The v1.3.0 to v1.6.0
entries are written in the repository; notes for v1.1.1 and v1.2.0 are copied from their GitHub
releases; v1.1.0 has a git tag but no GitHub release, so its entry comes from
the annotated tag message and the changes recorded in the repository for
that tag.

## Unreleased

**Agent layer, first slice (v1.7.0 in progress).** New `iap.agents` package,
standard library only, off the trading path (a test keeps every trading-path
package from importing it):

- `blackboard` / `broker` (AL01, AL02): append-only hash-chained JSONL of
  tasks, claims, findings and pre-registrations; content-hashed task ids,
  leases, findings need a live lease and citations that resolve; pre-register
  one hypothesis per (alpha, horizon); a tampered log refuses further writes.
- `mcp_server` (AG01, AL05 in part): read-only MCP server over stdio
  (`iap-mcp --root .`), six versioned tools, no write path (AST-tested).
  Authentication is the local process boundary; there is no network listener.
- `untrusted` (AL07): free text from artefacts is returned wrapped, sanitised
  and flagged for injection phrases.
- `evals` (AL06): planted leak, seeded bug, shuffled-label null, fabricated
  citation, each run with the control on and off.

- `reserve` (AL03): evaluator derives a hidden session seed per candidate
  and attempt (HMAC of a secret), returns only pass/fail and attempts left,
  caps attempts at 3, requires pre-registration, logs every attempt.
- `approvals` (AL04): HMAC-signed, expiring, single-use HUMAN approvals for
  the retire and reset lifecycle edges; agents hold no secret and the broker
  keeps trusted writers (evaluator, approvals) disjoint from agent ids.

Not done: wiring the reserve runner to the synthetic generator and research
runner, a CLI for issuing approvals, and making pre-registration a hard
requirement in the research runner. The evals are small synthetic checks, not
agent-in-the-loop runs.

Moved from v1.6.0 to v1.7.0 (the code ships here, the runs do not): the
real-data power study RUN (`power-real` on the 7-day dataset, ~6–20 h), the
signal-combination report on real data, the 2026-05-18 ingest (2026 days kept
as an out-of-time holdout), latency benchmarks in CI. v1.7.0 also carries the
agent layer and read-only MCP server.

## v1.6.0 — 2026-10-04

Real data first. v1.5.0 could ingest real Nasdaq ITCH files but no alpha had
ever run on them: the feature engine did not finish one real day in an hour,
the batch report read only the synthetic dataset, and one dataset held one
ingest at a time. This release fixes all three and runs the 24-alpha batch on
seven real sessions. Pull request
[#28](https://github.com/AshJha0/intraday-alpha-platform/pull/28).

**Result on real data** (7 Nasdaq sessions, 2019-01-30 … 2020-01-30, AAPL,
MSFT, QQQ; 3,100,181 feature rows; the data stays outside git):
**0 PROMOTE / 8 ITERATE / 4 REJECT, and not one trade survives costs in any
alpha.** The order-book signals are real — pooled HAC t from 3.3 (EQ06) to
26.2 (EQ12), EQ01 microprice IC 0.105 at 1 s, EQ10 ETF lead-lag t 13.2 — but
every predicted 1–10 s move is smaller than half-spread plus fee, so the
cost-aware policy never trades and the equal-weight ensemble makes no trade
either. EQ07 has the wrong sign; EQ11 (cross-sectional) cannot be judged with
two constituents; the twelve FX alphas have nothing to trade on an equity feed.
The t statistics count heavily overlapping 1 s rows; how much power seven days
really have is what the real-data power study (shipped, not yet run) measures.
This is a pipeline check on a thin sample, not a research conclusion.

**Changes**

- **Feature engine ~6× faster on real books.** The reference order book had no
  price index: every depth / best-price query sorted or scanned all resting
  levels (~700 on a real Nasdaq book, ~80 % of feature time). A sorted price
  list per side now serves the top N; the consolidated book merges each
  venue's top N, which is exact for the merged top N. A full 3-symbol day
  (5.4 M events) builds in 11 minutes (previously unfinished after an hour);
  identical output on the golden and feature tests.
- **`python -m iap.marketdata merge`** combines ingested single-day datasets
  into one multi-day dataset without re-parsing the source files. Its output
  is byte-identical to ingesting the sessions into one directory (tested);
  configs are rewritten from the current execution template.
- **24-alpha batch on an ingested dataset:** `research/alpha_reports/run_all.py
  --dataset-dir DIR --out-dir OUT` (`--out-dir` required — the committed
  reports stay synthetic). Alphas with nothing to trade are skipped and
  listed; the report describes the real dataset.
- **Fix: the alphas hard-coded the synthetic universe** (index ETF = instrument
  11). An ingested dataset numbers its own instruments (QQQ is 3), so the
  ETF-relative alphas would have traded QQQ as a constituent — including in
  v1.5.0's `research run --dataset-dir`. The universe now comes from the
  dataset's `instruments.json` (`iap.alpha.configure_universe`); the synthetic
  default is unchanged.
- **`research combine --dataset-dir`.**
- **`python -m iap.research power-real`** (`iap.research.power_real`): the
  planted-signal power study on real data. Effects of known information
  coefficient are planted in the stored labels, on a real-noise null (labels
  circularly shifted within each session, seeded) and on the labels as they
  are, and judged by the same chain and thresholds as the synthetic study.
  Statistical detection only — fills are priced at real mids. Not yet run.
- **Docs:** `docs/REAL_DATA.md` §3.1 (merge, batch, combine, power-real),
  §10 (feature-engine and 2026-session performance), §11; an audit of the docs
  against the committed artefacts fixed the landing page and ROADMAP, which
  still quoted the v1.4.0 three-seed power calibration, and real-data status
  in ARCHITECTURE / ROADMAP / the landing page.

**Known limitations.** Seven sessions of one venue's book, three symbols;
indicative fees; no corporate actions applied (none fall in the window); the
batch peaks at ~9.5 GB and loads every feature column; a 2026 session is
~8× a 2019 one (40.5 M events, ~2 h of features); the real-data power study
has not been run, so the real-data REJECTs are not yet interpretable.

**Versions:** package 1.6.0; image tags v1.6.0. No schema or golden change.

## v1.5.0 — 2026-10-04

The corrected research methods are the defaults. v1.3.0 added eleven of
them as opt-ins and v1.4.0 kept them opt-in, so every committed research
number was still computed under rules the repository itself described as
deficient. From this release the corrected rule is what runs when nothing
is named, each old rule stays selectable under an explicit legacy name, and
`legacy_v1` is tested against two alpha reports pinned from the v1.4.0 tag.
The dataset did not change. Every artefact that derives from it was
regenerated in one pass on the CI runner and every document that quotes a
number was re-derived from the new artefacts. Nothing was tuned to recover
a v1.4.0 conclusion. The release guard that failed the v1.4.0 release once
on an API error now retries. Pull request
[#21](https://github.com/AshJha0/intraday-alpha-platform/pull/21).

The same release carries six further pieces of work, each developed on its
own branch and merged into the release branch: real historical data
ingestion (ITCH 5.0, LOBSTER; #20), a passive execution policy and markout
analysis (#22), a faster Python CI job and an observable STOPPED session
state (#23), the dataset- and bundle-aware store schema `iap_v2.sql` and
the Java research backtester on the default rules (#24), signal combination
with two new lifecycle gates (#25), and differential fuzzing of the three
risk engines (#26). Their sections are below; none of them changes a
default of the research methods, and none of them promotes an alpha.

### Changed

The eleven methods, default first, legacy name second
(PLATFORM_CONVENTIONS.md §13.6 is the normative table;
`iap.validation.methods` bundles them as `"v2"` and `"legacy_v1"`):

1. **Row-latency stress grid**: `stress_version=2` (the base backtest
   config is carried into every stressed run) — `stress_version=1`.
2. **Position policy**: `BacktestConfig(position_policy="cost_aware")` —
   enter only when the expected return exceeds the round-trip spread and
   fee, hold for the label horizon — `"sign"`, `BacktestConfig.legacy()`.
3. **Gate statistic**: the HAC t of the pooled slope
   (`significance="pooled_slope"`) — `"within_bucket"`.
4. **PROMOTE t threshold**: `tstat_threshold="ledger"`,
   `max(3.0, Bonferroni |t| at the run's gate look count)` — `"fixed"`. The
   look count is deterministic: the ledger total before the run plus the
   looks the run adds, declared before the first alpha is evaluated and
   recorded on the ledger entries (`gate_looks`); a rerun is judged at the
   recorded count. One validation is 83 looks at four folds
   (`looks_per_validation`), 84 with the day-2 backtest; `legacy_v1` debits
   28 as before.
5. **Costs and capacity**: square-root impact (`impact_model="sqrt"`, named
   by `execution.json`), fills capped at the displayed L1 size
   (`cap_fills_at_l1`), edge-breakeven capacity (`capacity="breakeven"`) —
   `"linear"` / `CostModel.with_linear_impact()`, uncapped,
   `"participation"`.
6. **Drift and retirement**: the two-sample HAC z of the pair-weighted
   rolling IC (`adaptive.ic_z_method="hac"`) and the CUSUM retirement rule
   (`adaptive.lifecycle.breach_rule="cusum"`, slack 0.0025, threshold 0.01)
   — `"legacy"` and `"consecutive"`. The CUSUM rule was tightened when it
   became the default: it retires only on a reading that is itself a
   breach, never on the reading that entered WATCH.
7. **Leakage**: the recompute-from-raw-events probe runs in the standard
   `LeakageTester.run()` when the raw events are available (three anchors on
   the first 12,000 events of the first normalized file per asset class,
   memoised per run). A report without events says `recompute_ok: null`
   and the runner marks the result not gate-eligible.
8. **Per-fold diagnostics and the bootstrap interval** of the net P&L are
   reported fields of every validation. They are **report-only**: making
   either a gate would add a row to the lifecycle gate table, which is
   pinned across Python, Java and Rust.
9. **Rows**: a label invalid for BLACKOUT alone is scored at its realised
   reopen return (`ic_rows="blackout_reopen"`, columns `label_reopen_<h>`),
   and the backtest trades only the rows the IC scores
   (`block_rows_column="auto"`) — `"valid_only"`, `None`.
10. **Headline ICs**: the per-instrument mean IC and the vol-scaled IC are
    columns beside the pooled IC. **The gate reads the pooled uncrossed
    IC**: the gate t is the significance of that slope and of no other, and
    the lifecycle's holdout and paper gates compare a pooled IC
    (schemas/MIGRATIONS.md has the argument).
11. **Meta-labels**: `build_meta_features(impute_nan=False)` — missing
    values stay missing for the tree model — `impute_nan=True`.

Also changed:

- **Cross-language.** Rules on the paper path or in the lifecycle
  evaluation are ported, with the old rule selectable by name: the CUSUM
  rule and the pair-weighted rolling IC (Java `LifecycleGauge`, `RollingIc`;
  Rust `lifecycle::tracker`), the ledger significance threshold (Java
  `PolicyConfig` / `Gates`, Rust `PolicyConfig::threshold_for`). The
  research backtest rules are ported to Java as well: `ResearchBacktester`
  and `CostModel` default to the v1.5.0 rules and keep the old ones under
  the same names as Python (`Config.legacy`, `withLinearImpact`), and
  `expected_backtest.json` pins both rule sets for both languages. Rust and
  C++ have no research backtester.
- **The store is dataset- and bundle-aware** (`schemas/sql/iap_v2.sql`,
  data-model x-version 2). `experiments`, `experiment_results`,
  `ledger_entries` and `lifecycle_transitions` carry `dataset_version` and
  `methods`; `v_alpha_scorecard` and `v_experiment_ledger_summary` are one
  row per scope with the looks of that scope and the \|t\| threshold each
  result was judged at; `v_alpha_scorecard_current` /
  `v_experiment_ledger_summary_current` are the current dataset under the
  default bundle. A store built by an earlier release is refused untouched
  (exit code 2) and rebuilt with `python -m iap.store build --rebuild` —
  the store is derived, so the rebuild is the migration.
- **`x-version` bumps**: `configs/execution/execution.json` 1 → 2,
  `configs/strategies/lifecycle.json` 1 → 2, `strategies.json` `adaptive`
  1 → 2, `research/experiments.json` 2 → 3, `research/alpha_registry.json`
  1 → 2, `eligibility.json` 1 → 2, `POWER_REPORT.json` 1 → 2, goldens
  `expected_lifecycle.json` 1 → 3, `expected_adaptive.json` 1 → 2,
  `expected_backtest.json` 1 → 3, the SQL data model `iap_v1.sql` →
  `iap_v2.sql` (1 → 2). Every loader rejects the older document instead of
  reading it under a new default.
- **Evidence document**: required keys `significance_threshold` (number or
  null) and `live.new_fraction`.
- **`python -m iap.research run`**: `--methods {v2,legacy_v1}` and
  `--normalized-dir`; `--tstat-threshold` is removed (the policy is part of
  the method bundle, and the bundle is part of the experiment id).
- **Lifecycle transition log** rebuilt by `bootstrap --force`; the log of
  the legacy methods is archived as
  `research/archive/lifecycle_transitions.dataset-116b7787.methods-legacy_v1.jsonl`.
- Versions: `python/pyproject.toml` and `iap.__version__` 1.5.0; image
  references `v1.5.0`.

### Fixed

- **The Python CI job is 3.5 times faster.** The `pytest` step ran the
  1672 tests serially under coverage in 524 s on the `ubuntu-24.04` runner
  (319 s and 358 s on the v1.3.0 suite); it now runs
  `pytest -n auto --dist loadfile` with `pytest-xdist` 3.8.0 (pinned in
  `python/requirements-ci.txt`, declared in the `dev` extra) and takes 149 s,
  coverage still produced and uploaded. Every test already wrote under
  `tmp_path` or `tmp_path_factory` and only read the repository's `data/`,
  `configs/` and `research/`, so none had to change and none is serial;
  `--dist loadfile` keeps each module's fixtures on one worker. The PR run
  executed both ways and compared them test by test: 1672 collected, the
  same outcome for every one. **The 120 s target is still not met by the
  instrumented run (149 s); the uninstrumented serial run takes 110 s**
  (`docs/BUILD_NOTES.md` has the table and where the time goes). Those are
  the numbers of the 1672-test suite. The tests of the work merged into the
  release afterwards bring it to 1988 tests and the same step to 306 s and
  359 s in two runs of the release pull request: faster than serial, and
  further from the target than before.
- **A clean stop is observable after the process is gone.** The paper
  platform sets `platform_session_state` 4 (`STOPPED`) and exits right after
  its checkpoint, so Prometheus almost never scraped the 4 and
  `SessionStoppedNotResumed` rarely fired (a known limitation of v1.3.0 and
  v1.4.0). The trading thread now also writes `<state-dir>/session_exit.json`
  (`RUNNING` at start, then `STOPPED`, `FINISHED` or `FAILED`) after the
  checkpoint it describes, and a small exporter that outlives the JVM
  (`com.iap.platform.SessionStateExporter`; compose service and k8s sidecar
  `java-platform-state`, port 9102, state mounted read-only, its own k8s
  Service with `publishNotReadyAddresses`) serves it as
  `platform_persisted_session_state`. The alert is now
  `platform_persisted_session_state == 4 unless on(service)
  (platform_session_state == 1)` for 10 m: it fires for a stop nobody
  resumed, clears on resume, and stays silent after `FINISHED` and after a
  crash (which leaves the `RUNNING` marker; `TargetDown` covers it). The
  Session-state panel falls back to the persisted value. The shutdown hook
  is unchanged and `session_state.json` keeps its schema and single commit
  point. Covered by `SessionMarkerTest` and promtool tests in
  `deployment/prometheus/tests/alerts_test.yml`.
- **Release guard (`release.yml`, `verify-ci`).** The guard asked the API
  once and read an empty or failed answer as "no successful run": the
  v1.4.0 release failed on it although CI was green. It is now
  `tests/harness/verify_ci_green.py`, which separates three cases: an API
  error is retried with backoff (10 attempts, 15 s steps capped at 120 s,
  about 11 minutes) and ends as "undetermined" (exit 2); a CI run that
  completed without success fails at once with the run URL (exit 1); a run
  in progress is polled every 30 s for at most 40 minutes, then fails with
  a message that says so; a commit with no CI run at all fails after a
  120 s grace. Every attempt is printed. `workflow_dispatch` with `dry_run`
  runs only this step against a given SHA. Both outcomes were exercised on
  the real API: the v1.4.0 commit passes (run 37163663508) and a commit
  with no CI run fails with "no ci run exists for …; do not release an
  untested tree" (run 37163665262). 18 tests pin the decision table.
- **Adaptive study P&L was not in USD.** `run_adaptive.py` built its
  instrument meta without currencies, so the FX rows summed quote-currency
  P&L (JPY for USD/JPY) as if it were USD. The alpha reports were corrected
  for this on 2026-09-06; the adaptive runner was not. The policy totals of
  v1.4.0 (−8.69 million) were therefore wrong in scale.
- **Three fixed sentences of `ADAPTIVE_REPORT.md`** are now derived from
  the results: which trigger fired the drift refits, how many deployments
  lose and how many do not trade, and how far apart the policies land.
- **`REPORT.md` quotes the look count the run was judged at**, not the
  ledger total at render time.

### Added

- `iap.validation.methods` (`ResearchMethods`, `methods("v2" | "legacy_v1")`),
  `iap.labels.frames` (`scored_labels`, `scored_rows`),
  `BacktestConfig.legacy()` / `.for_horizon()`, `CostModel.with_linear_impact()`,
  `LifecycleConfig.legacy()`, `ExperimentLedger.batch_total` /
  `gate_looks_for`, `RecomputeSources`.
- `run_all.py --methods legacy_v1 --out-dir <dir>`: the v1.4.0 report,
  reproduced. `python/tests/test_legacy_methods.py` compares it field by
  field with `tests/golden/alpha_report_{EQ03,FX01}_v1.4.0.json`.
- Lifecycle golden scenarios LC04 (what the CUSUM rule changes) and LG01
  (the legacy policy); the backtest golden's `default_rules` block.
- **Java research backtester on the default rules.**
  `ResearchBacktester.Config.defaults(...).forHorizon(ns)` is the cost-aware
  position policy (enter only above the round-trip spread and fee, hold for
  the label horizon, hysteresis on renewal), fills capped at the displayed
  L1 size and decisions only on scored rows; `CostModel` is square-root
  impact by default with `roundTripCostReturn`, `breakevenSize` and
  `capacityBreakeven`, and `CostModel.load` rejects a config block that
  names no `impact_model`. `Config.legacy(...)` and `withLinearImpact()`
  name the v1.4.0 rules. Java has no label engine, so the scored-row mask
  is an input of `run(...)`. `expected_backtest.json` (x-version 3) adds the
  mask and every position change to `default_rules`, the no-trade run at
  full costs (`default_rules_1x`) and scalar `cost_model_cases` for both
  impact rules; the legacy vector is unchanged. Replayed by
  `python/tests/test_alpha_golden.py` and the new Java `BacktestGoldenTest`
  at 1e-9, counts and positions exact.
- **`schemas/sql/iap_v2.sql`** and the scope-aware store: tables
  `store_scope` (the current scope) and `ledger_scopes` (looks and
  Bonferroni \|t\| per scope), ledger columns `experiment_id`, `gate_looks`
  and `promote_t_threshold`, views `v_alpha_scopes`,
  `v_alpha_scorecard_current` and `v_experiment_ledger_summary_current`;
  `python -m iap.store scorecard [--dataset-version V] [--methods M]
  [--all-scopes]`, `build --rebuild / --dataset-version / --methods /
  --ledger PATH` (the ledger an ingested dataset keeps in its own
  directory is indexed under its own scopes), `sql` binding
  `:dataset_version` / `:methods`; `import_lifecycle_archive` files the
  archived lifecycle ledgers under the scope their name states;
  `StoreVersionError`.

### Added — real historical data ingestion

The only dataset is a seeded synthetic generator with no real signal in it,
so the research results say nothing about markets. This release adds the
path by which files the owner obtains flow through the existing pipeline —
normalisation and QC, canonical events, order book, features, research
runner — exactly as the synthetic data does. No market data is downloaded
or committed; Python only (backlog epic E25: XD01 and XD03–XD06 done).

- `iap.marketdata.itch50`: a streaming Nasdaq TotalView-ITCH 5.0 reader
  (length framing, gzip by magic, symbol filter, typed errors with byte
  offsets; unknown message types skipped by length and counted) and its
  mapper to canonical events. `iap.marketdata.lobster`: the LOBSTER message
  reader, pre-open seeding and a level-by-level check of the real
  `OrderBook` against the vendor's orderbook file.
- `python -m iap.marketdata ingest --format {itch50,lobster} --input …
  --date … --symbols … --out …` writes the generator's layout (raw →
  normalized → QC), the dataset configs and a `dataset.json` manifest
  (source, input sha256, counts per message type, skipped counts). The
  `dataset_version` is disjoint from the synthetic one and the output is
  byte-deterministic.
- `iap.reference.secmaster` / `corpactions`: a point-in-time security
  master (`as_of`, never a later date) and an owner-supplied
  corporate-actions table with a point-in-time adjustment API.
- `python -m iap.research run --dataset-dir DIR`: the spec, the experiment
  id and the ledger entries carry the ingested dataset's version, and the
  ledger and experiments default to the dataset's own directory, so
  `research/experiments.json` does not move. `python -m iap.store build
  --ledger PATH` indexes such a ledger under its own scopes.
- Tests run on bytes they synthesise from the published layouts
  (`python/tests/itch50_encoder.py`, `python/tests/lobster_fixture.py`);
  two tests are skipped unless environment variables point at real files.
- Docs: `docs/REAL_DATA.md` (sources, commands, the mapping table, how to
  read a first study, limitations), COOKBOOK.md recipe 36.

### Added — differential fuzzing of the risk engines

The hard risk engine exists three times (Rust, normative; Java; Python) and
was held byte-identical by hand-written step scripts only. The v1.3.0 review
found fail-open bugs none of them exercised. Backlog issues FZ01, FZ03 and
FZ05 of epic E31 are done.

- **Generator** (`python/tools/risk_fuzz.py`): deterministic (SplitMix64 only,
  no wall clock, no hash-ordered iteration) and state-aware — it drives a live
  Python engine while generating, so quantities land at the remaining headroom
  of every limit, prices at the band edge, timestamps at the staleness and
  future-stamp boundaries, and an engaged kill is cleared instead of blocking
  the rest of the script. Every step type of the two risk goldens plus
  `bad_override`; i64 / u64 extremes, clock regressions and jumps, venue 0,
  unknown ids, duplicate and >= 2^63 order ids, empty / oversized / non-ASCII
  ids; thirteen profiles, one of them a set of 50 configuration mutations.
- **Corpus** (`tests/golden/risk_fuzz/`, `make_risk_fuzz_corpus.py`): 89
  scripts, 4,607 steps, 2,741 audit lines, 1.5 MB. The Python engine is the
  oracle. `COVERAGE.txt` names the script that reaches each reason-string
  branch of the engine: 63 of 64 (`strategy daily pnl undeterminable` cannot
  be reached, check 21 rejects first). CI regenerates the corpus on Linux and
  fails on a stale one; the tool refuses to change files without `--force`.
- **Replays**: Rust `rust/risk/tests/golden_risk_fuzz.rs`, Java
  `RiskFuzzGoldenTest`, Python `python/tests/test_risk_fuzz_golden.py` — every
  order's decision, rule, severity, scope, scope id and reason, the audit JSONL
  byte for byte, the final snapshot, a snapshot -> JSON text -> restore ->
  continue check at each cut point, and refusal of corrupt and truncated
  snapshots. In the golden group of `run_golden.sh`.
- **Nightly / manual job** (`.github/workflows/risk-fuzz.yml`, or `ci.yml`
  `workflow_dispatch` with `risk_fuzz=true`; driver
  `python/tools/risk_fuzz_nightly.py`): fresh scripts from a fresh seed on the
  runner, Rust and Java driven through their own golden tests
  (`IAP_RISK_FUZZ_DIR`, `IAP_RISK_FUZZ_OUT`), the first divergence minimised by
  a delta-debugging shrinker and uploaded as an artifact. Not on the pull
  request path.
- **Properties and mutation tests** (`python/tests/test_risk_fuzz_properties.py`):
  kill precedence derived from the audit log alone, no ALLOW under the GLOBAL
  kill, positions equal the sum of applied fills, determinism, canonical
  append-only audit lines, snapshot identity; nine planted bugs and a
  UTF-16-ordered port are each noticed by the committed corpus. NaN and
  Infinity cannot be written in JSON: they are pinned by a unit test in each
  language, not by the corpus.

Divergences found, all fixed (PLATFORM_CONVENTIONS.md §11.1 "Pinned by the
fuzzer"; no existing golden changed):

1. **A conversion rate stamped `i64::MAX` was trusted — Rust and Python
   (fail-open), found by the first nightly run** (ci run 37186349594: seed
   20261004, 400 scripts, 38,666 steps; Rust 400 OK, Java 2 diverged; reduced
   from 120 steps to 3: mark USD/JPY at `ts = i64::MAX`, mark a JPY
   instrument, send an order in it). The pre-trade rate check recognised the
   reporting currency by a `mark_ts == i64::MAX` placeholder, so a pair really
   stamped `i64::MAX` skipped the age and the future-stamp check and stayed
   trusted for the session. §11.1 says such a rate rejects `FX_RATE_MISSING`;
   Java did. The normative engine was the wrong one.
2. **Order of strategy ids — Java.** `TreeMap<String, _>` orders by UTF-16
   code unit, the reference's `BTreeMap<String, _>` by code point; they
   disagree between U+E000..U+FFFF and the astral planes. Two strategies
   breached by one mark latched in the opposite order, and the snapshot's
   lots were listed in the opposite order.
3. **A subnormal float in a reason — Java.** `urgency = -5e-324` printed
   `…049` (from `Double.toString`'s two-digit `4.9E-324`) where Rust prints
   `…05`.
4. **`CONFIG_MISSING` reason — Java.** The parser read the sequence-gap
   threshold before every other key, reported a missing section as such,
   printed a non-positive float as `0.0`, walked the conversion table in
   document order and had one message for three conversion errors. A
   fail-closed engine now names the same first offending key, in the same
   words, as the reference.

### Added — execution quality

Costs are what kill every alpha here, so this release adds a way to pay
fewer of them and a way to measure whether it worked. No default changed.

- **Execution policies** (C++ reference, Java and Python ports, identical
  fills): `ParentOrder.policy` = `NATIVE` (default, the existing child
  styles under an explicit name), `AGGRESSIVE` (every child MARKET) or
  `PASSIVE` — POST at the near touch (one tick inside when the spread is at
  least three ticks, never at or through the opposite touch) → REST for
  `floor(max_rest_ns × (1 − urgency) × (e^−risk_aversion for IS))` ns or
  until the schedule is more than `max_behind_fraction` of the order behind
  → REPRICE once → CROSS the cancelled remainder. The simulator's nine rules
  are untouched. New golden `tests/golden/expected_replay_fills_passive.json`
  (written by the Python port in the bytes of the C++ generator; the C++ test
  re-renders both replay goldens byte for byte);
  `expected_replay_fills.json` is byte-identical.
- **Markout analysis** (`iap.tca.markout`, Java `com.iap.tca.Markout`):
  per fill, the signed move of the mid at or before `fill_ts + h` for
  100 ms / 1 s / 5 s / 30 s / 60 s / 5 min (configurable), in bps and
  currency; `effective half-spread = realised half-spread + price impact`;
  adverse selection on passive fills; splits by liquidity, venue, algo, side
  and time bucket with counts and standard errors; `null` — never zero — past
  the end of the data, before the first quote, across a halt / auction /
  quote gap, and for a cell with too few fills. Passive-order fill rate and
  time to fill, overall and by queue position at entry
  (`ChildOrder.entry_ahead_qty`, now recorded by all three simulators).
  Golden `tests/golden/expected_markout.json`.
- **Evidence** (`research/execution/EXECUTION_REPORT.md`, step `execution`
  of `tools/regenerate_dataset_artifacts.py`): the same 836 parent orders on
  the bundled equities under each policy, with the unfilled quantity priced
  at the arrival-to-end move plus the cost of completing it; and the MVP
  session per child policy (`execution.child_policy`, an optional key of
  `mvp.json`; the pinned run is unchanged). The report's last section says
  which part of the result is a simulator assumption.
- Docs: API_TRADING.md §2.5, API_PORTFOLIO_TCA.md §2.9,
  PLATFORM_CONVENTIONS.md §11.3 / §14.5, docs/HOW_IT_WORKS.md §3.5,
  LEARN.md §30, COOKBOOK.md recipe 37.

### Added — signal combination and the correlation gate

Pull request [#25](https://github.com/AshJha0/intraday-alpha-platform/pull/25).

- **`iap.combine`** — many weak signals into one forecast per instrument
  and timestamp. `CombinedAlpha` is an `AlphaModel` whose inputs are
  alphas; its weights for a walk-forward fold are fitted inside that fold's
  training window, on the members' out-of-sample predictions from three
  inner purged and embargoed walk-forward folds (stacking). Four methods by
  name: `equal_weight` (the default and the baseline), `ic_weighted`,
  `ridge` (penalty chosen by forward-chained CV inside the training
  window) and `shrinkage_mv` (Ledoit–Wolf shrinkage, numpy only). `ridge`
  and `shrinkage_mv` account for member correlation explicitly; the other
  two do not, and nothing is orthogonalised (API_ALPHA.md §8,
  PLATFORM_CONVENTIONS.md §13.8).
- **Leakage tests** (`python/tests/test_combine.py`): for every fold and
  method the fitted parameters are bit-identical when the data is
  truncated at the test start and when every later label is garbled or
  shifted or every later feature rescaled; a combiner that reads test rows
  fails the same test. The platform's truncation and recompute probes
  found one real defect during development — a BLAS matrix product
  rounding a row differently depending on the rows after it — and the
  blend is now accumulated member by member.
- **The combination as a research object**: `python -m iap.research
  combine` validates each (asset class, method) combination with
  `validate_alpha` under the `v2` bundle, debits `83 + K` looks per
  experiment (760 for 12 members, 4 methods, 2 asset classes — all
  declared before the first is evaluated), and writes
  `research/combination/{REPORT.md, COMBINATION.json, reports/,
  signal_correlation.json}`: IC and gate t of each member against the
  combination, the signal and P&L correlation matrices, the effective
  number of independent bets, weights per fold and their stability, net
  P&L after costs, and the breadth arithmetic next to the measurement.
- **`cross_alpha_correlation`** (lifecycle gate; backlog AF03, closed):
  on CANDIDATE → VALIDATING, the largest absolute out-of-sample signal
  correlation with any alpha at or beyond `cross_alpha_min_state`
  (`VALIDATING`) must be at most `max_cross_alpha_correlation` (0.7).
  No such alpha: vacuous pass at 0.0. No evidence: fail closed.
- **`net_pnl_bootstrap_ci`** (lifecycle gate): on the same edge, the lower
  bound of the 95 % stationary-bootstrap interval of the pooled 1×-cost
  net P&L must be above zero. An alpha that makes no trade has no
  interval and fails. This closes the item the release first shipped as
  report-only. Absent, by name, under the legacy policy
  (`net_pnl_ci_gate = "absent"`).
- Both gates are rows of the gate table in **Python, Java and Rust** (20
  gates, 17 edges) and are evaluated from the evidence document alone
  (`cross_alpha`, `pnl_bootstrap`); `lifecycle.json` and the lifecycle
  golden move to `x-version` 3, with scenarios LC05 and LC06. C++ has no
  lifecycle port.
- `tools/regenerate_dataset_artifacts.py` gains the `combination` step;
  the manual `regenerate` CI job takes `regenerate_only` (a subset of
  steps).

**Result.** 0 PROMOTE / 8 ITERATE / 0 REJECT over the eight combinations.
The equal-weight blends measure what the breadth arithmetic predicts
(equities: gate IC 0.0105 against an expected 0.0099, 6.5 effective bets
of 12; FX: 0.0315 against 0.0348, 9.9 of 12). The fitted equity blends
clear the significance threshold of 4.42 (ridge: gate IC 0.0429, t 7.06)
and every combination fails the cost gate: the equity blends make 0 to 6
trades in four folds, the FX blends that trade lose (ridge −1 627 USD).
In the registry all 24 alphas stay at CANDIDATE; all 24 fail
`net_pnl_bootstrap_ci` (17 make no trade, 7 have a negative lower bound)
and pass `cross_alpha_correlation` vacuously. No verdict changed. The
correlation document shows EQ02 / EQ03 / EQ12 correlated 0.95–1.00: with
one of them at VALIDATING the gate would hold the other two.

### Added — detection power

The planted-signal power study (`python -m iap.research power`) was three
seeds on two sessions: one detection in three has a 95 % interval of 0.06
to 0.79. It now measures a rate. `POWER_REPORT.json` `x-version` 2 → 3.

- **Sessions.** Each run is generated once at the largest session count and
  scored on its first 1, 2, 4 (extended grid: 8) sessions — exact, because
  without a break session *k* does not depend on what follows it (tested on
  the bytes). The study writes its own reference tree with as many weekdays
  as it needs (`extended_trading_days`); `configs/` lists five and is
  untouched. `research/power/generator_planted.json` `sessions` 2 → 4;
  `--sessions` overrides.
- **Seeds and intervals.** 20 seeds per cell; every rate is
  `detections/runs [Wilson 95 % interval]`.
- **Thresholds.** The pooled-slope t against 3.0, against the PROMOTE
  threshold in force (4.365, Bonferroni at the 3,936 looks the committed
  promotion reports were judged at; `--gate-looks` overrides) and against
  the Bonferroni threshold of the study's own tests. Verdicts are computed
  at the largest of the three. No threshold was lowered and no promotion
  gate changed.
- **Detectors.** Each effect is scored by its alpha at the declared label
  horizon and at the horizon matched to the planted block
  (`matched_horizon`: EQ04 at 10 s, EQ10 at 5 s) — a rule on the planted
  config, counted in the study's Bonferroni denominator. The reference
  effect sizes are unchanged.
- **New statistics** (`iap.research.power_stats`; none is read by a gate):
  `wilson_interval`; `pooled_slope_session_hac` (the gate estimator with the
  Bartlett cross-products restricted to buckets of the same session, and
  the effective sample beside it); `slope_break_z` (HAC z of the slope
  before against after a split); `ideal_ic_order_flow` /
  `ideal_ic_lead_lag` (the IC of each planted mechanism on the efficient
  price); `fit_t_model`, `minimum_detectable_level`, `sessions_needed`
  (`t ~ N((κ₀ + κ·level)·√sessions, sd)`).
- **Report.** Detection by sessions and by effect size, the null, the
  break, a diagnosis table (ideal against measured IC, expected against
  measured t, one idealisation at a time), the minimum detectable effect at
  80 % power and the sessions the reference needs, and a plain statement
  per detector.
- **Runtime.** Runs are worker processes (`--jobs`; the document does not
  depend on it). The default grid (3 sizes, 4 sessions, one break level; 80
  runs) took 994.5 s in the CI `regenerate` job; the extended grid (4 sizes,
  8 sessions; 100 runs) 2,461.1 s and is the opt-in `power_extended` step of
  `tools/regenerate_dataset_artifacts.py`, written to
  `research/power/extended/`
  (`-f regenerate_only=power,power_extended` on the `regenerate` job).

Result at the reference size, gate threshold (detections of 20):

| detector | 2 sessions | 4 sessions | 8 sessions |
|---|---:|---:|---:|
| order flow, EQ04 @ 5 s (declared) | 2 | 10 | 20 |
| order flow, EQ04 @ 10 s (matched) | 11 | 20 | 20 |
| lead-lag, EQ10 @ 1 s (declared) | 0 | 0 | 0 |
| lead-lag, EQ10 @ 5 s (matched) | 1 | 6 | 16 |
| null, each detector | 0 | 0 | 0 |

At t ≥ 3 the first row reads 12, 19 and 20. Minimum detectable effect at
80 % power on eight sessions: 0.84 of the reference (EQ04 @ 5 s), 0.66
(EQ04 @ 10 s), 1.18 (EQ10 @ 5 s), 2.51 (EQ10 @ 1 s). EQ10 as declared cannot
detect the planted two-second lead at a practical session count: its
one-second label ends before the follower moves. The mid-sample reversal is
never reported as evidence and the break z flags it in 20 of 20 runs for
order flow. PROMOTE is still reached in no cell. The two power rows of the
Results table below are the version-2 report (3 seeds, 2 sessions, t ≥ 3).

### Results

Same dataset, new rules. Nothing is promoted, before or after.

| | v1.4.0 | v1.5.0 |
|---|---|---|
| promotion verdicts | 0 PROMOTE / 10 ITERATE / 14 REJECT | 0 PROMOTE / 11 ITERATE / 13 REJECT |
| verdicts that moved | | FX03 ITERATE → REJECT; FX10, FX11 REJECT → ITERATE |
| PROMOTE t threshold; looks it is derived at | 3.0 (fixed); — | 4.365; 3,936 |
| alphas with gate t above the threshold | 6 (EQ02, EQ03, EQ06, EQ12, FX01, FX04) | 3 (EQ02, EQ03, EQ12) |
| alphas failing the cost gate alone | 4 (EQ02, EQ03, EQ12, FX04) | 3 (EQ02, EQ03, EQ12) |
| net P&L at 1× costs, last fold | 24 of 24 lose | 18 make no trade, 6 trade and lose, 0 above zero |
| largest loss at 1× costs | −300,873 USD (EQ05) | −733 USD (FX11) |
| edge-breakeven capacity above zero | not computed | 3 of 24 (EQ11 38,248, FX08 1,561, FX10 61,135 USD) |
| recompute leakage probe | not run | 24 of 24 pass |
| lifecycle registry | 24 CANDIDATE, 0 beyond | 24 CANDIDATE, 0 beyond |
| registry gates failed (alphas) | cost 24, significance 18, stability 13, IC 11 | cost 24, capacity 24, significance 21, stability 14, IC 12 |
| ledger | 1920 looks, 139 entries | 4396 looks, 208 entries (1920 carried + 2476) |
| expected max \|t\| under the null; Bonferroni \|t\| (whole ledger) | 3.888; 4.206 | 4.096; 4.389 |
| ML gate (best linear pooled OOS IC vs the mid label) | PASSED (ridge +0.0081) | PASSED (ridge +0.0081); unchanged |
| meta-labeling gate | degenerate (0 trades) | degenerate (0 trades); 275 of 131,880 meta-feature values missing, not imputed |
| adaptive study, drift-triggered refits; retired under every policy | 122; FX01 | 88; FX01 |
| adaptive study, deployments above zero; that make no trade | 0 of 40; 0 | 0 of 40; 19 |
| adaptive study, total net P&L of the static policy | −8,693,703 (not USD, see Fixed) | −3,921 USD |
| power study, planted order flow at the reference size: significant / evidence | 1 of 3 / 3 of 3 seeds | 1 of 3 / 3 of 3 seeds |
| power study, planted order flow at twice the reference size: significant; mean trades at 1× | 3 of 3; — | 3 of 3; 14 |
| power study, planted lead-lag: significant at any size; evidence at 0.5× / 1× / 2× | 0; 0 / 0 / 0 | 0; 1 / 1 / 2 of 3 seeds |
| power study, PROMOTE on any planted effect; any detection at level 0 | 0; 0 | 0; 0 |
| MVP session: events, decisions, parents, fills, P&L | 15,805, 800, 235, 169, −81.53 USD | unchanged |
| MVP `config_version`; trace digest | `f293e7e7…`; `f51890da…` | `439bbad5…`; `20d4ff76…` |

- **What the cost-aware policy shows.** Under the sign policy every alpha
  traded every row and lost five or six figures; that loss measured the
  policy. Under the default, 18 of the 24 forecasts never exceed their own
  round-trip cost and make no trade, which is the same finding stated
  directly: the predicted move is smaller than the spread. A net P&L of
  exactly 0 does not pass `net P&L > 0`, so the cost gate still fails for
  all 24. The six that trade (EQ11 and five FX alphas) lose between 22 and
  733 USD on the last fold.
- **Significance.** The gate t is now the significance of the gated IC.
  It is lower than the within-bucket t for the equity ITERATE alphas (EQ03
  5.85 → 5.16, EQ01 2.72 → 1.59) and higher for several FX alphas whose
  signal is between buckets (FX08 2.18 → 3.84). Against the ledger
  threshold of 4.365, three alphas are significant where six cleared 3.0.
  EQ06 misses it at 4.36 and FX04 at 4.24; the threshold was not moved.
  FX10 and FX11 become ITERATE on a pooled t of 1.55 and 2.22 (ITERATE
  needs 1.5); FX03 drops to REJECT at 1.13. Seventeen alphas have a gate t
  below 4.07, the expected largest |t| under the null at 3,936 looks.
- **Blackout rows.** Scoring BLACKOUT rows at their reopen return changes
  one alpha materially: EQ11 (15-minute horizon), 23,413 such rows, gate IC
  0.0261 → 0.0038. The valid-only IC had dropped exactly the rows on which
  a 15-minute forecast is wrong, a selection effect; it is reported beside
  the gate IC (`gate_ic_valid_only`). For the other 23 alphas the two ICs
  agree to within 0.003.
- **Hypothesis signs.** Two alphas are now significantly wrong-signed at
  the ledger threshold (EQ08 t −4.57, FX09 t −5.75). They were REJECT
  before and are REJECT now.
- **Adaptive study.** With the CUSUM rule and the HAC z the drift policy
  refits 78 times after the initial fits instead of 112, and 65 of those
  refits name a PSI breach against 14 that name the IC z — the report
  used to say the opposite in a fixed sentence. FX01 is retired under
  every policy, as before. 19 of the 40 deployments make no trade.
- **Power study.** The detection rates of the planted order flow are the
  same under the pooled t as under the within-bucket t. The cost-aware
  backtest does not trade the planted effect at the reference size at all
  and trades it 14 times on average at twice that size, where no fold survives costs: the chain can see
  an effect it cannot monetise. The planted lead-lag now reaches ITERATE in
  one or two seeds of three; it is never significant.
- **MVP.** The loop does not use the research backtester; its counts and
  P&L are identical. `config_version` hashes `execution.json`, which
  changed, and `alpha_params.json`, whose header names the regeneration
  commit; the trace digest covers `config_version`. The per-alpha
  `ic_gap` moves in the fourth decimal because the research IC it is
  measured against is now the gate IC of the v2 report.
- **Fitted parameters.** `alpha_params.json` differs from v1.4.0 by at most
  2.3e-15 relative (the frames carry more columns and numpy sums a strided
  column in a different order). `expected_alpha.json` follows at 2.6e-13.
  The legacy backtest vector of `expected_backtest.json` is identical to
  the v1.4.0 vector in every value.

### Migration notes

- Code that constructs `BacktestConfig()`, `CostModel(...)`,
  `LifecycleConfig(...)`, calls `validate_alpha(...)` or
  `build_meta_features(...)` without naming a rule now gets the corrected
  rule. To keep a v1.4.0 number, name the legacy rule or use
  `methods("legacy_v1")`. The cost-aware policy needs the label horizon
  (`config.for_horizon(h)`); the ledger threshold needs
  `ledger_t_threshold`.
- `execution.json`, `lifecycle.json` and the `adaptive` block of
  `strategies.json` written for v1.4.0 are rejected until they name
  `impact_model`, `tstat_threshold`, `ic_z_method` and
  `lifecycle.breach_rule` and carry the new `x-version`.
- An evidence document without `significance_threshold` or
  `live.new_fraction` is rejected by the Python, Java and Rust readers. A
  registry written by v1.4.0 (`x-version` 1) is rejected; rebuild it with
  `python -m iap.lifecycle bootstrap --force` after archiving the log.
- A reader of `research/experiments.json` must accept `x-version` 3 and the
  entry field `gate_looks`; an alpha has one `promotion_pipeline` entry per
  dataset and method bundle. Experiments recorded before v1.5.0 name no
  method bundle and are history, not gate evidence.
- The five runner experiments have new ids (`methods` is hashed into the
  id); the directories of the earlier ids are kept.
- A Java paper session's `config_sha256` changes (`execution.json`,
  `lifecycle.json`, `strategies.json` and `alpha_params.json` are among the
  hashed files). Its lifecycle gauge follows the CUSUM rule and its rolling
  IC is pair-weighted; the state stays observational. State directories
  written by v1.4.0 resume as before.
- A store file built before v1.5.0 (data-model x-version 1) is refused by
  `Store.init()` and by every `python -m iap.store` command; rebuild it with
  `python -m iap.store build --rebuild`. A query against
  `v_alpha_scorecard` now returns one row per alpha and scope: use
  `v_alpha_scorecard_current` for the old one-row-per-alpha shape.
  `Store.insert_experiment_result` needs the experiment's spec in the store
  (or `methods=`).
- Java callers: `new ResearchBacktester(costModel, maxPos, confMin, latency)`
  is `new ResearchBacktester(costModel, Config.legacy(maxPos, confMin,
  latency))`; `new CostModel(a, b, c, multiplier)` is square-root impact now
  — add `.withLinearImpact()` for the old rule. The constants
  `POSITION_POLICY`, `CAP_FILLS_AT_L1`, `BLOCKS_ROWS` and `IMPACT_MODEL` are
  gone (the configuration says which rules run).
- Feature frames written by v1.4.0 have no `label_reopen_<h>` columns:
  the default row policy then falls back to valid labels and the report
  says `label_reopen_available: false`. Regenerate with
  `python -m iap.features`.

### Known limitations

Those of v1.4.0 stand. New or restated:

- **"0 PROMOTE" is now mostly "no trade".** The cost gate is failed by
  forecasts that never clear their costs, not by measured losses. That is a
  statement about the spread of the synthetic book relative to the
  predicted move; it says nothing about what a passive execution policy
  would earn, which the research backtester does not model.
- **The per-fold diagnostics gate nothing, and the bootstrap interval gates
  the lifecycle only.** `net_pnl_bootstrap_ci` holds an alpha at CANDIDATE
  when the interval's lower bound is not positive; the report verdict
  (PROMOTE / ITERATE / REJECT) does not read the interval.
- **The store's scorecard row is the latest result of a scope, not a
  judgement.** `v_alpha_scorecard` shows the threshold each result was
  judged at (`promote_t_threshold`) and the scope's own Bonferroni \|t\|
  beside it; it re-judges nothing, and the gate statistics other than the
  t-stat stay inside `ledger_entries.result_json`.
- **The Java research backtester takes the scored-row mask as an input**
  (there is no Java label engine) and runs rows-mode latency only; TIME-mode
  latency, the decision-age bound, the session flatten and the per-row
  currency conversion exist in Python alone. None is a default.
- **The ledger threshold is not retroactive.** Results recorded before
  v1.5.0 were judged at 3.0 and are not re-judged.
- **Real data: ingestion only.** One real ITCH day has been ingested by the
  owner; the LOBSTER path has not been run on a vendor file and no study on
  real data is recorded (XD10). The batch runners (`run_all.py`, the
  adaptive study, `research combine`, the power study) read the bundled
  dataset only; `research run --dataset-dir` is the one research entry
  point that takes an ingested dataset. Single venue per file, no
  auctions, no hidden liquidity until it trades, corporate actions not
  applied by the feature pipeline (docs/REAL_DATA.md §11). XD02 and
  XD07–XD10 stay backlog.
- **The passive-execution result is a simulator result.** Our quote never
  changes the replayed book, nothing reacts to it, there is no hidden
  liquidity and cancels are frictionless
  (`research/execution/EXECUTION_REPORT.md`, last section). The `TCAResult`
  wire contract was not extended with markouts; the Java `BacktestEngine`
  and `PaperTrading` keep the NATIVE child style (the passive MVP run is
  Python only); there are no PEG / MID / post-only order types (X08); FX
  is excluded from the execution study. T05 stays backlog.
- **A combination is a research object, not a registered alpha.** The
  registry holds the 24 flagship alphas (PLATFORM_CONVENTIONS.md §13.8); the
  eight combination experiments are in the ledger — their 760 looks count
  in the scope and in every later threshold — and in
  `research/combination/`, and the store's `v_alpha_scorecard`, which is
  one row per registered alpha, does not list them
  (`v_experiment_ledger_summary` does, as kind `combination`). The alpha
  reports were not re-run for the two new gates: a report renders the
  validation gates of its verdict, and lifecycle gate results live in the
  registry, which was rebuilt. `cross_alpha_correlation` passes vacuously
  while no alpha is at or beyond VALIDATING. C++ has no lifecycle port.
- **Risk fuzzing does not cover NaN and Infinity**, which JSON cannot
  carry: one unit test per language does. One of 64 reason-string branches
  is unreachable from a script. FZ02 (simulator properties) and FZ04
  (crash injection on the Java paper path) stay backlog.
- **The Python CI job misses the 120 s target under coverage by more than
  before**: 306-359 s for the merged 1988-test suite under xdist (149 s for
  the 1672 tests it was measured on). The slowest additions are the
  combination report test, the MVP child-policy fixture and the ingest
  end-to-end test; nothing was removed or marked slow.
- **Rust and C++ have no research backtester** and do not read
  `expected_backtest.json`.

## v1.4.0 — 2026-10-03

The bundled dataset is regenerated. Up to v1.3.0 the continuous flow of every
equity stream stopped 37.6–43.2% of the way through its session; the
generator now calibrates the flow so that it reaches the close, and that is
the default. Every artefact that derives from the dataset — the alpha
reports and fitted parameters, the ledger, the experiments, the registry,
the ML, adaptive and power reports, the baselines, four goldens — was
regenerated in one pass, and every document that quotes a number was
re-derived from the new artefacts. No promotion gate, threshold, cost
model or alpha definition was changed, and nothing was tuned to recover a
v1.3.0 conclusion. Pull request
[#19](https://github.com/AshJha0/intraday-alpha-platform/pull/19).

### Fixed

- **Equity flow reaches the close (issue M08).** `slots_per_stream` was a
  hard budget of flow slots, the base rate was `slots / duration × 1.30`,
  and the self-exciting multiplier `(1 + excitation)` — which about halves
  the mean inter-arrival time — was not in the calibration, so the budget
  was spent 40% of the way through the session. The base rate is now
  `slots_per_stream × E[1 / (1 + excitation)] / duration` (the factor is
  0.522 for the pinned flow parameters; `excitation_time_factor`, a pinned
  SplitMix64 estimate) and there is no budget: flow runs until the close,
  and `slots_per_stream` is the expected number of slots per stream and
  session. Every equity stream now ends 99.8–100.0% of the way through the
  session. The event count is deliberately the old one (raw equity events
  211,359 → 210,175), so the flow is about 2.5 times sparser in time than
  the compressed flow of v1.3.0 was inside its window.
- **Report prose that described the old data shape** is derived from the
  data or states the session length: the row-gap sentences of `REPORT.md`,
  the "~2.6 dense hours" of `ADAPTIVE_REPORT.md` and `ML_REPORT.md`, the
  "every alpha is net-negative" sentence of `ADAPTIVE_REPORT.md`, and
  conclusion 2 of `ML_REPORT.md`, which asserted "no directional alpha"
  whatever the table above it said.
- **Two lines of the v1.3.0 entry below** that the published release notes
  had already corrected: ruff runs rules E, W, F, I, UP and B plus
  `ruff format --check` (not a "correctness-only rule set"), and the
  session-state dashboard panel maps all five values 0–4 (not 0–3). The
  same two statements are corrected in PLATFORM_CONVENTIONS.md §12.7,
  docs/governance/SECURITY.md and deployment/grafana/README.md.

### Changed

- **`equities.flow.calibration`** (generator config, new): `"session"` — the
  default — is the rule above; `"legacy_budget"` is the v1.3.0 rule and
  reproduces the v1.3.0 dataset byte for byte. `equities.fill_session`, the
  v1.3.0 opt-in, belongs to the legacy rule and is an error with the
  default. The golden-vector builders select `"legacy_budget"` explicitly,
  so every golden event vector is unchanged.
- **Generator config `x-version` 1 → 2** (`configs/marketdata/generator.json`,
  `configs/mvp/generator.json`, `configs/mvp/generator_tiny.json`,
  `research/power/generator_planted.json`). `load_generator_config` rejects
  an `x-version` 1 document that does not name its calibration.
- **Dataset identity.** `data_version`
  `203c8f54…` → `116b7787…`. `feature_version` is unchanged (no feature
  definition changed). `tests/replay/test_generator_determinism.py` pins the
  raw-file hashes of both datasets; the FX files are the same bytes in both.
- **The multiple-testing ledger is dataset-scoped (`x-version` 1 → 2) and
  keeps its history.** An entry is identified by (alpha, kind, config,
  dataset) and carries `dataset_version`. The 1068 looks recorded on the
  v1.3.0 dataset stay in the file and in the denominator; the regenerated
  pipelines add 852. `research/migrate_ledger_dataset_scope.py` stamped the
  existing entries without changing a key. The lifecycle bootstrap and the
  store read, per alpha, the entry of the dataset `alpha_params.json` names.
- **Lifecycle transition log.** Rebuilt by `bootstrap --force` for the new
  dataset; the log bootstrapped on the v1.3.0 dataset is archived as
  `research/archive/lifecycle_transitions.dataset-203c8f54.jsonl`.
- **`python -m iap.research list`** prints the dataset of each experiment:
  the folder now holds the five runner experiments of each dataset.
- Versions: `python/pyproject.toml` and `iap.__version__` 1.4.0; image
  references `v1.4.0`.
- Parity table: python 1573 / cpp 289 / rust 323 / java 510 tests, golden
  groups 166/68/64/104; `replay` 6 (was 4).

### Added

- **`tools/regenerate_dataset_artifacts.py`**: the whole regeneration chain
  in dependency order with per-step timing, and a guard against running the
  report pipelines twice on one dataset.
- **CI job `regenerate`** (manual: `workflow_dispatch` with
  `regenerate=true`): runs the script on the CI runner and uploads the
  changed files. The committed artefacts of this release were produced by
  it (run 37152702105, 346 s), because the last digits of the float
  artefacts depend on platform and library versions and some suites compare
  them exactly. CONTRIBUTING.md §4.1 has the procedure.
- Tests: session coverage of the default calibration, the legacy rule and
  its `fill_session` opt-in, the calibration factor, the config `x-version`
  gate, the raw-file hashes of both datasets, the dataset scope of the
  ledger, the per-dataset selection of ledger entries.

### Results

What the new data says, against what v1.3.0 reported. Nothing is promoted,
before or after.

| | v1.3.0 | v1.4.0 |
|---|---|---|
| equity flow ends (fraction of the session, per stream) | 37.6–43.2% (mean 40.5%) | 99.8–100.0% |
| normalized events | 310,159 | 308,975 |
| feature vectors (100 ms cadence) | 208,437 | 213,021 |
| promotion verdicts | 0 PROMOTE / 11 ITERATE / 13 REJECT | 0 PROMOTE / 10 ITERATE / 14 REJECT |
| alphas that lose money at 1× costs | 24 of 24 | 24 of 24 |
| lifecycle registry | 24 CANDIDATE, 0 beyond | 24 CANDIDATE, 0 beyond |
| alphas failing the cost gate alone | 8 | 4 (EQ02, EQ03, EQ12, FX04) |
| ledger | 1068 looks, 70 entries | 1920 looks, 139 entries (1068 carried + 852) |
| expected max \|t\| under the null; Bonferroni \|t\| | 3.735; 4.071 | 3.888; 4.206 |
| ML gate (best linear pooled OOS IC vs the mid label) | FAILED (ridge −0.0430); trees and MLP never fitted | PASSED (ridge +0.0081); xgboost, lightgbm and MLP fitted; no model earns its costs |
| meta-labeling gate | degenerate (0 trades) | degenerate (0 trades) |
| adaptive study, drift-triggered refits; retired under every policy | 126; FX01 | 122; FX01 |
| power study, planted order flow at the reference size: significant / evidence | 3 of 3 / 3 of 3 seeds | 1 of 3 / 3 of 3 seeds |
| power study, planted lead-lag at twice the reference size: significant (within) | 1 of 3 seeds | 0 of 3 seeds |
| power study, PROMOTE on any planted effect | 0 | 0 |
| MVP session: events, decisions, parents, fills | 16,578, 355, 66, 55 | 15,805, 800, 235, 169 |
| MVP session: shares filled; P&L | 3,126; −22.65 USD | 8,229; −81.53 USD |
| MVP trace digest | `d938eeae…` | `f51890da…` |

- **Alphas.** Every equity alpha that was ITERATE has a lower IC on the full
  session (EQ03: uncrossed IC 0.0298 → 0.0190, Newey–West t 10.57 → 5.78;
  EQ01: 0.0273 → 0.0102, t 4.77 → 2.67). EQ11 (15-minute horizon) falls from
  ITERATE to REJECT (t 3.04 → 1.40). The FX reports are unchanged: the FX
  data is byte-identical. EQ01 and EQ05 keep ITERATE but no longer clear the
  PROMOTE significance gate. The net losses of the equity ITERATE alphas at
  1× costs are roughly twice as large (EQ03: −70,651 → −148,562 USD).
- **ML.** The gate that blocked the tree and MLP tiers passes, on a pooled
  IC of +0.0081. Decomposed for this release (the numbers are in the pull
  request), the equity part of that IC is positive in three folds and
  negative in one, and the FX part is the same in both datasets; the move
  from −0.0430 is entirely the equity part. The models the gate lets
  through do no better (xgboost +0.0046,
  lightgbm +0.0078, MLP −0.0024 against the mid label) and every model's
  conservative net is negative. The report's conclusion is the same as
  before; the reason is now measured rather than gated away.
- **Power study.** The validation chain detects less on the sparser flow:
  the planted order flow at the reference size clears t ≥ 3 in one seed of
  three, the planted lead-lag in none at any size. The chain still promotes
  nothing, and at level 0 it reports nothing.
- **MVP.** The loop trades to the close: 2.6 times the shares, 3.6 times the
  loss, the same cost per share against about half the alpha contribution
  (+0.022 bps of filled notional, was +0.039; cost −0.41 bps in both). The
  realized IC of the session no longer collapses under a one-decision shift
  (EQ01 +0.217 → +0.149): the signals persist for several seconds on the
  slower flow. docs/MVP.md §7.1 has the audit and why this is persistence
  and not a leak; `paper_ic_tracking` now fails all three MVP alphas.
- **Adaptive study.** The FX deployments move by one trade each although the
  FX data is unchanged. The reports committed with v1.3.0 had last been
  generated before the backtester corrections of 2026-09-20; re-running the
  v1.3.0 code on the v1.3.0 dataset reproduces the new FX numbers. The
  statement in the v1.3.0 entry that "no committed number can move" was
  therefore not true of `research/adaptive_reports/`.

### Migration notes

- A generator config written for v1.3.0 (`x-version` 1) is rejected until it
  names `equities.flow.calibration`: `"legacy_budget"` to keep the data it
  was written for, or `"session"` with `x-version` 2. `fill_session: true`
  needs `"legacy_budget"`.
- Anything stored under the old `data_version` — feature parquet files,
  baselines, fitted parameters, experiment directories — describes the old
  dataset. Regenerate with `python -m iap.marketdata`, `python -m iap.features`
  and, for the committed artefacts, `tools/regenerate_dataset_artifacts.py`.
  `alpha_params.json` loaders check `feature_version`, which did not change,
  so an old parameter file still loads: check its `data_version` header.
- A reader of `research/experiments.json` must accept `x-version` 2, the
  entry field `dataset_version` and the top-level `datasets`; an alpha now
  has one `promotion_pipeline` entry per dataset.
- The five experiment directories of the v1.3.0 dataset are kept. Their
  results can be reproduced only on that dataset (`"legacy_budget"`).
- A Java paper session scores with `alpha_params.json`: on the same event
  stream its equity signals, and therefore its orders, differ from a v1.3.0
  session's, and its `config_sha256` changes (`alpha_params.json` and
  `generator.json` are among the hashed files). State directories written
  by v1.3.0 resume as before.

### Known limitations

Those of v1.3.0 stand, except the equity-flow one, which is fixed. New or
restated:

- **The equity flow is sparse relative to the label freshness floor.** Rows
  are about 3.2 s apart and the pinned freshness bound is
  `max(5 s, 2 × median quote gap)` = 5 s, so 19–21% of equity labels at a
  10 s horizon and 23–25% at one minute are invalid as `forward_stale`, and
  37–55% at fifteen minutes (with blackouts). Raising `slots_per_stream`
  would restore the density the v1.3.0 research ran on at 2.5 times the
  events; it was not done, so that the slot count keeps its documented
  meaning and the fix is the calibration alone.
- **FX flow still ends 92–100% of the way through its session** (a slot
  budget with a 1.05 margin and no excitation). It was left alone so that
  the FX data stays byte-identical.
- **The cold-path benchmark rows were not re-measured.**
  `benchmarks/results_cpp.md` and the Java cold table in
  `benchmarks/RESULTS.md` were measured over the v1.3.0 file
  `eq_20260824.normalized.jsonl` (105,640 events; the v1.4.0 file has
  105,282) on the baseline machine, which is not available to CI.
- **The regenerated artefacts carry `git_dirty: true`** after the first
  step: the chain writes tracked files as it goes, so every later step sees
  a modified tree. The commit they name (`29f0822`) is the one the chain
  ran on.

## v1.3.0 — 2026-10-03

A review of the safety code — the hard risk engine, the execution simulator
and the Java paper platform — and of the research statistics. Five pull
requests: [#7](https://github.com/AshJha0/intraday-alpha-platform/pull/7)
risk engine, [#10](https://github.com/AshJha0/intraday-alpha-platform/pull/10)
execution simulator, [#9](https://github.com/AshJha0/intraday-alpha-platform/pull/9)
paper platform, [#8](https://github.com/AshJha0/intraday-alpha-platform/pull/8)
governance and deployment, [#12](https://github.com/AshJha0/intraday-alpha-platform/pull/12)
research validity. No golden that existed before the release was
regenerated, no wire schema changed, and the research results are what they
were: 0 PROMOTE, 24 alphas at CANDIDATE, an MVP session that loses 22.65 USD.

### Fixed

**Hard risk engine — Rust (normative), Java and Python, identical decisions
and reason text (PLATFORM_CONVENTIONS.md §11.1, API_TRADING.md §1.4)**

- *Future-stamped market data was trusted.* A mark stamped after the order
  had a negative age, which never exceeded the stale timeout, so a corrupt
  future mark was used for as long as it stayed ahead while genuine updates
  behind it were dropped as regressions. A mark stamped more than
  `stale_feed_timeout_ns` beyond the engine's event clock now rejects
  `STALE_PRICE`; a conversion rate stamped that far ahead rejects
  `FX_RATE_MISSING`. The event clock is the latest order event time the
  engine knows (this order's timestamp or the newest throttle-bucket time).
- *NaN passed float limit checks.* Every float limit comparison is now
  written so that NaN fails it. Invalid reference data (`tick_size` or
  `qty_unit` not finite and positive, empty `quote_ccy`) fails closed: Java
  and Python refuse to construct it (Java accepted `+Infinity`), and Rust
  lands the engine on `CONFIG_MISSING` for every order.
- *Integer overflow.* Position accounting and the position projection are
  checked in the symmetric i64 domain: an overflowing projection rejects
  `MALFORMED_ORDER`; a fill that cannot be booked is not applied and latches
  the GLOBAL kill. Timestamp differences (mark age, rate age, duplicate
  window, throttle elapsed) are checked and reject `MALFORMED_ORDER`. A
  `bid + ask` that leaves i64 is treated as no mark. Before, Python raised
  `OverflowError`, the Rust test profile panicked, and the Rust release
  build and Java wrapped.
- *Venue 0 (route via SOR) bypassed venue controls.* A venue-0 order now
  rejects `KILL_VENUE` while any venue kill is engaged, and
  `VENUE_DISCONNECTED` when every known venue is disconnected.
- *Audit text parity in Java.* `urgency` is printed with Rust `f64`
  `Display` semantics; kill scope ids and snapshot keys parse like
  `u16::from_str` / `u32::from_str`; the `CONFIG_MISSING` reason carries the
  reference's `invalid argument: ` prefix.

**Execution simulator — C++ (normative), Java and Python (§11.2, §14.1,
API_TRADING.md §2.4)**

- *Fills beyond displayed size.* The post-apply crossing check and the
  reopen check rebuilt their pool from the displayed opposite best on every
  event, so a resting buy 1000 @ 100 against a static ask 50 @ 100 filled 50
  on each unrelated event. The pool is now displayed minus consumed, and the
  check debits the overlay.
- *Queue tracking trusted raw events.* Tracking now runs after the book
  update and only for events the book reports `APPLIED`: a retransmitted
  duplicate or an EXECUTE for an unknown order no longer fills us.
- *Execute cap.* An applied EXECUTE trades at most the book order's
  remaining size, at the book order's side and price.
- *Cancel rule.* A CANCEL advances our queue position only when the
  cancelled order is known to be ahead of us, by the displayed size the book
  actually removed; orders that joined behind us and synthetic
  QUOTE / SNAPSHOT ids never do.

**Java paper platform (§11.4, §12.3, §12.5) and the Python MVP engine**

- *Venue kill bypass under SOR.* The pre-trade request now names the venue
  the child is actually routed to; a child that leaves for another venue is
  counted and cancelled.
- *Resume consistency.* `session_state.json` is the single commit point of a
  checkpoint and records the sha256 of its risk snapshot, verified on
  `--resume`; an interrupted checkpoint is rolled forward. The resumed
  engine's account is seeded from the restored positions (no second buy of
  the same position) and the snapshot's orphaned open orders are released.
  Every checkpoint persists cumulative total and gross P&L.
- *Shutdown hook race.* The hook only raises a stop flag and waits (10 s);
  the trading thread checkpoints at its next event boundary. New session
  state `STOPPED` (gauge value 4).
- *Admin kill dropped on a quiet feed.* A kill latches the moment it is
  accepted, is never dequeued on timeout (response `202`, a second audit
  line when applied), is drained in the order path and between realtime
  pacing slices, and requests cancels for working child orders.
- *Gap gate.* `onFeedRecovered` fires only when no venue of the instrument
  is stale — in the Java wiring and in `iap.mvp.engine`.
- *Sizing solver names.* Constants that fed `stepDecay` / `projPasses` were
  named as a step size and a patience; they and the hashed keys are renamed.
  Solver behaviour is unchanged; a paper session's `portfolio_version` hash
  value changes.

**Research (no committed number can move)**

- Cost and time stress configurations are built with `dataclasses.replace`
  instead of a four-field rebuild; a row-latency stress IC that cannot be
  computed is `null`, not NaN; the meta-label `auc_test` is `null` on a
  single-class test segment instead of 0.5.
- Documentation said 21 looks per experiment; the code records 28.

**Documentation**

- Stale MVP figures corrected against `tests/golden/expected_mvp.json`
  (3,126 shares, −0.41 bps execution cost, the fill-rate, routing and
  per-algorithm rows), which several documents had not followed after the
  2026-09-20 regeneration.
- The 31 table-of-contents links of docs/EPICS.md resolve. The generator
  collapsed the two spaces around a dash into one hyphen; GitHub keeps both
  (`e01--architecture-and-contracts`). `create_issues.py` now has
  `github_slug`, which follows GitHub's rule, and the issue-plan test checks
  every anchor against the rendered headings.

### Added

- **Risk edge golden**: `tests/golden/expected_risk_edge_decisions.json` and
  `expected_risk_edge_audit.jsonl` — eight independent scenarios, each with
  its own engine, generated by `python/tools/make_golden_risk_edge.py` and
  replayed by Rust, Java and Python.
- **Planted-signal power study**: a generator `planted` block (off by
  default; the pinned dataset is byte-identical with or without it),
  `python -m iap.research power`, and `research/power/POWER_REPORT.{md,json}`.
- **Opt-in research methods**, each beside its pinned default
  (docs/RESEARCH_VALIDITY.md): `stress_version=2`; the cost-aware position
  policy; the L1 fill cap; blocked rows; square-root impact and breakeven
  capacity; the ledger-derived t threshold; the two-sample HAC drift z and
  the CUSUM retirement rule; the recompute leakage probe; the blackout-reopen
  IC; meta-label `impute_nan=False`.
- **Additive statistics** read by no gate: the pooled-slope HAC t,
  per-instrument and vol-scaled IC, per-fold diagnostics, a seeded
  stationary-bootstrap interval for net P&L.
- **Research store safe for parallel writers**: ledger lock file with a
  locked read-modify-write and atomic replace; run-directory creation as the
  atomic claim; staged experiment directories; listings that skip and report
  corrupt directories.
- **Gate eligibility**: `eligibility.json` beside each new result;
  lifecycle research gates refuse evidence that is not eligible.
- **Research CLI for tools**: `list --json`, `show --json`, `--json-errors`
  with stable codes; `python -m iap.store sql` refuses several statements.
- **Import-policy test** (`python/tests/test_import_policy.py`): no module of
  the guarded Python packages imports a network or LLM client.
- **Paper platform**: per-operator admin tokens (`IAP_ADMIN_TOKENS_FILE`),
  `operator` and `remote` in the admin audit, safety counters
  (`risk_routed_venue_mismatch_total`, `risk_resume_open_orders_released_total`,
  `exec_orders_blocked_kill_pending_total`, `admin_auth_rate_limited_total`,
  `admin_audit_suppressed_total`).
- **CI and release**: CodeQL, Dependabot configuration, a blocking C++
  ASan+UBSan job, blocking `cargo clippy -D warnings`, blocking `ruff check`
  (rules E, W, F, I, UP and B, `ruff.toml`) and `ruff format --check` (the
  tree was made lint-clean and formatted for the release), non-blocking
  `pip-audit` / `cargo audit`, a
  tag-triggered release workflow (images to GHCR, build-provenance
  attestation, `release-manifest.json`).
- **Deployment**: Alertmanager (compose and Kubernetes) with a `Watchdog`
  heartbeat; NetworkPolicies for default-deny egress and an operator ingress
  rule; a separate state PVC for the Java platform and a Grafana PVC.
- **Alert rules for the paper-platform safety signals** (22 alerts and the
  heartbeat, from 16): `SessionStoppedNotResumed` on the `STOPPED` session
  state, `RoutedVenueMismatch`, `KillPendingNotRecorded`,
  `ResumeReleasedOpenOrders`, `AdminAuthRateLimited` and
  `AdminAuditSuppressed` on the five safety counters, each with a
  `promtool` unit test and a runbook section. The counters are in the
  deployment harness's exported-metrics list.
- **Generator `equities.fill_session`** (off by default; the pinned dataset
  is byte-identical with the key absent or false): equity flow continues to
  the close instead of stopping when the slot budget is spent.
- **Documentation**: docs/HOW_IT_WORKS.md, docs/RESEARCH_VALIDITY.md,
  docs/governance/REPO_SETTINGS.md, this changelog; new LEARN chapters,
  COOKBOOK recipes 27–35 and nine diagrams; seven backlog epics (E25–E31).

### Changed

- **`--dry-run` debits the ledger.** A dry run writes no experiment
  directory but its looks are recorded: it evaluates and prints every
  statistic, so it is a look.
- **The admin listener binds `127.0.0.1` by default.** Containers set
  `IAP_BIND_ADDR=0.0.0.0`; a deployment that relied on the old behaviour
  must set it.
- **Admin responses**: `202` for a latched kill, `429` after 10 failed
  authentications in a 60 s window, `503` for a withdrawn non-kill command.
- `images` CI job also runs on pull requests that touch image inputs.
- `CODEOWNERS` names `@AshJha0` (the `@iap/*` teams never existed).
- Versions: `python/pyproject.toml` 1.3.0; image references `v1.3.0`.
- Parity table: python 1565 / cpp 289 / rust 323 / java 510 tests, golden
  groups 166/68/64/104.

### Security

- Every GitHub Action is pinned to a commit SHA; runners are pinned
  (`ubuntu-24.04`); workflow permissions are `contents: read` with
  per-job additions; cargo runs `--locked`.
- Python dependencies carry bounds in `pyproject.toml` and exact versions in
  `python/requirements-ci.txt`; the Rust toolchain is pinned
  (`rust/rust-toolchain.toml`) with `overflow-checks = true` in the release
  profile; Docker base images are pinned by digest.
- Admin API: loopback bind by default, failed-authentication rate limit,
  capped audit of rejected requests, token hashes rather than tokens in the
  multi-operator file, operator and remote address in every audit line.
- Deployment: read-only root filesystem, no service-account token mounts,
  default-deny egress, loopback-only published ports in compose.

### Known limitations

- **Repository controls are files, not settings.** Branch protection,
  required checks, required reviews, Dependabot alerts and secret scanning
  are not configured; docs/governance/REPO_SETTINGS.md has the commands.
  Signed tags are a practice, not an enforced control.
- **The release workflow has never run.** It can only be exercised by a tag;
  its first run is its test. Until it has run and the manifests are pinned
  from its `release-manifest.json`, the `v1.3.0` image references are
  tag-only and no such image exists in the registry.
- **Alerts are routed but not delivered** until an operator supplies a
  webhook URL; audit logs are not shipped off-host; no image vulnerability
  scan is wired in.
- **The `STOPPED` state is rarely observed downstream.** (Fixed in v1.5.0:
  the persisted `platform_persisted_session_state` outlives the process.)
  The session-state
  dashboard panel maps all five values, 0–4, and `SessionStoppedNotResumed`
  reads the value 4, but a stopped process exits right after its
  checkpoint, so the value is scraped only when a scrape lands in that
  instant; otherwise `TargetDown` is the only alert
  (deployment/grafana/README.md).
- **Equity flow in the bundled dataset stops about 40% into each session.**
  Every equity stream's continuous flow ends 38–43% of the way through the
  6.5-hour session (mean 40.5%, about 2 h 38 min after the open) and
  nothing follows until the close auction; FX is not affected in the same
  way (its flow ends 92–100% of the way through, mean 95%). The cause is in
  `python/src/iap/marketdata/generator.py` `_eq_session_stream`:
  `slots_per_stream` is a hard budget of flow slots, the base rate is
  `slots / duration * 1.30` — the margin multiplies the rate, so the budget
  would be spent at 77% even without clustering — and the self-exciting
  multiplier `(1 + excitation)`, which about halves the mean inter-arrival
  time, is not in the calibration. Every equity research number is
  therefore a statement about roughly the first 2 h 40 min of each of the
  two sessions (about 5.3 hours of continuous flow in total, not 13), and
  the walk-forward folds, cut by row mass, partition that window. The
  default is unchanged because every golden, report and headline number
  derives from it; `equities.fill_session: true` generates flow to the
  close (about 2.5 times the equity events). Backlog issue M08.
- **Ports implement the pinned research defaults only.** The opt-in HAC z,
  the CUSUM rule and the gate-eligibility flag exist in the Python reference
  alone; the Java and Rust lifecycle readers reject evidence carrying
  `research_gate_eligible`.
- **The import-policy test covers Python only**, and not `iap.replay`; the
  Rust, C++ and Java trees are enforced in review.
- **The edge golden does not cover every new branch.** It pins the future
  mark, the timestamp and position overflows and the venue-0 rules; the NaN
  limit comparisons, the invalid-reference-data landing and the
  future-stamped conversion rate are covered by per-language rule tests
  only. Differential fuzzing of the three engines is backlog (E31).
- **The power study is three seeds per cell.** A rate moves in steps of
  0.33; it calibrates the chain and is not a power curve.
- **The Python suite exceeds its 120 s target** (1565 tests, five to six minutes in
  CI under coverage). (v1.5.0: 149 s under xdist with coverage for 1672 tests, 306-359 s for the 1988 of the merged release; see above.)
- **`iap.__version__` still reads 1.0.0**; the package metadata says 1.3.0.
- **There is no LLM, agent or MCP code.** The agent layer is a backlog epic
  (E24, E30); what exists is the foundation it would need.
- Everything else that was out of scope remains so: real exchange data,
  measured latency, simulator calibration to live fills, book-level risk
  (E25–E28).

## v1.2.0 - Risk engine split into submodules (2026-09-27)

Follow-up to the code review that produced v1.1.1 — completes the last
deferred P1 item: splitting the safety-critical, byte-identical-port risk
engine into cohesive submodules.

**rust/risk/src/**
- `engine.rs` — core `RiskEngine` state machine (constructors, market/fill
  state-in, P&L, `check_order`)
- `limits_eval.rs` — the pinned rule-0..22 limit evaluation
- `killswitch.rs` — kill/unkill/override_loss_limit/roll_session
- `audit.rs` — audit-log emission and snapshot/restore
- wired via `mod` declarations in `lib.rs`; crate's public surface unchanged

**java/src/main/java/com/iap/risk/**
- `RiskEngine.java` — same shape as the Rust core, public methods now delegate
  to the new classes
- `LimitsEvaluator.java`, `KillSwitch.java`, `RiskAudit.java` — same-package
  classes operating on the `RiskEngine` instance
- Every public method signature on `RiskEngine` unchanged

Both splits were done as pure mechanical, line-for-line moves — no logic
changed, only visibility and cross-file glue. Verified behavior-identical by
CI, including `tests/golden`'s cross-language risk-parity fixtures
(`expected_risk_decisions.json`, `expected_risk_audit.jsonl`,
`expected_risk_snapshot.json`) — all checks passed on the first CI run.

See [PR #6](https://github.com/AshJha0/intraday-alpha-platform/pull/6).

## v1.1.1 - Supply-chain and error-handling hardening (2026-09-27)

Follow-up to a full-repo code review (PR #5).

**CI / supply chain**
- `promtool` and `kubeconform` downloads now verified against their published
  SHA256 checksums before install.
- Added report-only coverage instrumentation: `pytest-cov` (python),
  `cargo-llvm-cov` for the `risk` crate (rust), gcov/lcov (cpp). Java coverage
  intentionally deferred — no Maven/Gradle or vendored JaCoCo jar yet.
- The `images` job (4 Docker builds) now runs only on push to `main`, not on
  every PR, while remaining the required merge-to-main promotion gate.

**Error handling**
- `rust/alpha/src/fx_exposure.rs`: currency-lookup `.expect()` panics replaced
  with `Result<_, IapError>` propagation.
- `rust/risk/src/bin/make_risk_golden.rs`: ~30 `.unwrap()` calls on JSON field
  access replaced with helpers that return actionable errors naming the
  offending step and field, instead of bare panics on malformed golden-fixture
  input.

**Documentation / logging**
- `python/src/iap/experiment/tracker.py`: documented the pickle trust boundary
  on `save_model`/`load_model` (deserialization is an RCE vector — only ever
  load a `model.pkl` this platform produced locally).
- Swallowed `OSError`/`SubprocessError` in the git-metadata lookup is now
  logged at debug level instead of silently passed.

**Not included** (from the same review, intentionally deferred): splitting
`rust/risk/src/engine.rs` / `java/.../risk/RiskEngine.java` into submodules —
judged too risky without a local toolchain to verify zero behavioral change
against the golden risk fixtures; a `java/lib/` checksum manifest — there are
no vendored jars in the repo today.

## v1.1.0 - Round-3 hardening (2026-09-06)

Annotated tag message: "Round-3 hardening: real-world scenario correctness,
1,571 tests across four languages". No GitHub release was created for this tag.
Changes recorded in the repository for this round: real-world scenario
correctness across all four languages (`docs/SCENARIOS.md`), the CI workflow
(`.github/workflows/ci.yml`), the deployment validation harness
(`tests/harness/check_deployment.py`, `check_docker_build.py`), and the Java
paper-trading vertical's health, readiness and kill-switch admin API
(`PLATFORM_CONVENTIONS.md` section 12).
