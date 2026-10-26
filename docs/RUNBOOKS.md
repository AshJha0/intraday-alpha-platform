# Runbooks

Operational runbooks, one section each. They used to be twelve files in docs/runbooks/ (v2.1 Track 0).

Former file → section:

- `docs/RUNBOOKS.md`
- `docs/RUNBOOKS.md`
- `docs/RUNBOOKS.md`
- `docs/RUNBOOKS.md`
- `docs/RUNBOOKS.md`
- `docs/RUNBOOKS.md`
- `docs/RUNBOOKS.md`
- `docs/RUNBOOKS.md`
- `docs/RUNBOOKS.md`
- `docs/RUNBOOKS.md`
- `docs/RUNBOOKS.md`
- `docs/RUNBOOKS.md`

## RUNBOOK — Backtest & Deterministic Replay

**Scope:** the Python research backtester (`iap.backtest`), the
production-grade replay/execution simulators (C++/Rust/Java) and the
cross-language parity harness.
**Owner:** research + per-language owners. **Related alerts:** `GcPauseHigh`.

> **Shells.** Blocks tagged `bash` run in bash (Linux, the containers, Git Bash on Windows).
> The owner's Windows machine uses PowerShell: each `PYTHONPATH=src python3 ...` has a
> `powershell` variant below it (`$env:PYTHONPATH = "src"; python ...`; `python3` is `python`;
> set the variable once per window; `a && b` is `a; if ($?) { b }`). `bash tests/harness/...`
> and `bash build.sh` need Git Bash or WSL on PATH; `kubectl`, `docker` and `curl` lines
> are for the cluster/containers and are the same in PowerShell except for the text filters,
> which are given in PowerShell where the owner would run them locally.

### 1. Prerequisites

Fresh market data (backtests read `data/normalized/`):

```bash
cd python && PYTHONPATH=src python3 -m iap.marketdata     # see docs/RUNBOOKS.md
```

```powershell
Set-Location python; $env:PYTHONPATH = "src"; python -m iap.marketdata
```

### 2. Python research backtester

The fast engine (decision at t, execution at t+latency, spread/fees/impact
costs, exact accounting identity — `python/src/iap/backtest/engine.py`):

```bash
cd python
PYTHONPATH=src python3 -m pytest -q tests/test_backtester.py          # engine invariants
PYTHONPATH=src python3 -m pytest -q tests/test_model_economics.py     # cost-adjusted economics
```

```powershell
Set-Location python; $env:PYTHONPATH = "src"
python -m pytest -q tests/test_backtester.py          # engine invariants
python -m pytest -q tests/test_model_economics.py     # cost-adjusted economics
```

Defaults since v1.5.0 (PLATFORM_CONVENTIONS.md §13.6): `BacktestConfig()`
is the cost-aware position policy (`position_policy="cost_aware"`: enter
only when the expected return exceeds the round-trip spread and fee, hold
for the label horizon — it needs the horizon, `config.for_horizon(h)`),
fills capped at the displayed L1 size (`cap_fills_at_l1=True`), only the
rows the IC scores traded (`block_rows_column="auto"`), and the cost model
loaded from `configs/execution/execution.json` uses square-root impact
(`impact_model="sqrt"`). An alpha whose forecast never clears its
round-trip cost makes no trade under this policy and its net P&L is exactly
0. The rules up to v1.4.0 are selected by name: `BacktestConfig.legacy()`
(`"sign"`, uncapped, every row) and `CostModel.with_linear_impact()`;
`research/alpha_reports/run_all.py --methods legacy_v1 --out-dir <dir>`
reproduces the v1.4.0 alpha report with them. The Java `ResearchBacktester`
and `CostModel` implement both rule sets under the same names
(`Config.defaults(...).forHorizon(ns)` / `Config.legacy(...)`,
`CostModel.load` / `withLinearImpact()`); the Java run takes the scored-row
mask and the displayed L1 sizes as inputs.

Currency (conventions §11.6): `Backtester(..., reporting_ccy="USD")`
converts every instrument's P&L increment to USD at the prevailing mid of
the conversion pair (`configs/risk/risk.json` `currency.conversion`) before it
is summed; `InstrumentResult.total_pnl_native` /
`BacktestResult.total_pnl_native_by_ccy` keep the quote-currency figures.
A non-USD P&L increment on a row with no prevailing rate raises — run the
conversion pairs in the same frame set as the instruments you backtest
(the alpha reports do; `research/alpha_reports/run_all.py`). Reports
produced before 2026-09-06 summed quote-currency P&L as if USD (papers 2-4
errata).

Research runs go through the experiment tracker so every result carries a
manifest (git commit, data/feature/model versions, hardware —
`docs/GOVERNANCE.md`). Check the ledger after a run:

```bash
python3 -m json.tool research/models/ledger.json | head
python3 -m json.tool research/models/<run_id>/manifest.json
```

```powershell
python -m json.tool research/models/ledger.json | Select-Object -First 10
python -m json.tool research/models/<run_id>/manifest.json
```

#### 2.1 Row blocking and methods since v1.14

The research backtester blocks rows causally by default
(`block_rows_column="causal"`): it refuses to trade a row only for reasons
visible at the decision time. Up to v1.13 the default skipped rows whose
future label was invalid, which is look-ahead. A validation report shows
both: the causal net P&L and `net_pnl_1x_cost_label_block`. If a report
you produced before v1.14 disagrees with a new one, compare those two
fields first, then rerun with `--methods v2_v113` to separate the row
block from the other statistics changes (COOKBOOK recipe 72).

### 3. Production-grade simulators (per language)

```bash
# C++ — replay + execution sim + fills golden
cd cpp && bash build.sh && ctest --test-dir build --output-on-failure -R 'Replay|Exec|Golden'
./build/bench_all                       # full decode/book/replay/exec throughput+latency pass
./build/bench_all ../benchmarks/results_cpp.md   # ... persisting the methodology table

# Rust — deterministic replay demo over the golden vectors
cd rust && cargo run --release --bin demo

# Java — archived (git tag archive/java-v1.14.0, frozen at v1.14.0, not built; v1.17 retired its image)
# cd git tag archive/java-v1.14.0 && bash build.sh && java -cp out/main com.iap.replay.Demo ../../tests/golden
```

```powershell
# C++ (build.sh needs Git Bash or WSL; other routes: docs/BUILD_NOTES.md)
Set-Location cpp; bash build.sh; if ($?) { ctest --test-dir build --output-on-failure -R 'Replay|Exec|Golden' }
.\build\bench_all
Set-Location ..\rust; cargo run --release --bin demo
```

Containerized equivalents: `docker compose -f
deployment/docker/docker-compose.yml up cpp-replay rust-replay mvp-session platform-metrics`.

The Python reference ports of the same simulator and of the risk engine run
their goldens in the Python suite (`cd python && PYTHONPATH=src python3 -m
pytest -q tests/test_execution_golden.py tests/test_risk_golden.py`;
`docs/API_REFERENCE.md`), and the whole loop — including both — is one command:
`PYTHONPATH=src python3 -m iap.mvp run` (docs/MVP.md; PowerShell: `$env:PYTHONPATH = "src"; python -m iap.mvp run`).

Known optimisms of the simulators (documented, not bugs — `docs/SCENARIOS.md`
TRADING section, paper 05 §5 + erratum): no PEG/MID order types in the
C++/Java/Python simulator; the SOR ranks eligible venues by price/fee only (not
depth); full-amount cancel decrement in the queue model; simulated fills
never mutate the replayed book (rule 3b only prevents our own children from
re-using the same displayed liquidity). Pinned and tested: no fill through
HALT/AUCTION_CALL or a stale book, cancels have latency, children expire at
the parent `end_ts`, FX impact in base units.

### 4. Cross-language parity (the gate that matters)

```bash
bash tests/harness/run_all.sh        # full builds + tests, parity table
bash tests/harness/run_golden.sh     # golden groups only, parity table
```

```powershell
bash tests/harness/run_all.sh        # Git Bash / WSL on PATH; there is no PowerShell port
bash tests/harness/run_golden.sh
```

Both must show every language passing. Promotion gate 10 (GOVERNANCE.md)
requires the golden parity table attached to the PR.

### 5. Failure modes

#### A golden test fails in ONE language

That language deviates from the pinned semantics — fix the language, never
the golden. Diff the failing checkpoint (expected files in `tests/golden/`),
then look for: unordered-map iteration, wall-clock reads, float on an
integer path (conventions §3), or a missed pinned rule (e.g. MODIFY
increase→tail, marketable ADD executes — conventions §4).

#### Golden tests fail in ALL languages

The vectors changed. `git log tests/golden/` — if a regeneration commit
lacks the mandatory "golden change" note, treat as an incident
(GOVERNANCE.md §3): revert, then review. Regeneration is only ever done
deliberately via `cd python && PYTHONPATH=src python3 tools/make_golden.py` (PowerShell: `$env:PYTHONPATH = "src"; python tools/make_golden.py`).

#### Backtest results differ between runs

They must not (identical seed ⇒ identical outputs). Bisect for wall-clock or
map-ordering leaks; the determinism tests
(`pytest -k determinism`, `ctest -R Determinism`, `cargo test deterministic`)
localize the layer.

#### JVM latency issues {#jvm}

(Historical: `GcPauseHigh` and `jvm_gc_pause_ns` were retired with the Java
image in v1.17, F9; there is no JVM in the deployment.)

`GcPauseHigh` (p99 pause > 10 ms) during Java replay/backtest: check heap
sizing in `JAVA_OPTS` (compose/k8s set ZGC + 256m–1g), then allocation on hot
paths (conventions §8: primitive arrays, no boxing). GC logs go to stdout
(`-Xlog:gc*`); correlate pause timestamps with the order-path latency panel.

#### Runtime budget exceeded

Each language's suite must stay < 120 s (conventions §9). If a suite creeps
past, profile it in the PR that caused it — CI treats budget breaks as
failures, not warnings.

---

## RUNBOOK — Data Pipeline

**Scope:** the synthetic/raw → normalized market-data pipeline
(`python3 -m iap.marketdata`): generator → `data/raw/*.jsonl` → normalize →
`data/normalized/{*.normalized.jsonl, *.normalized.iap1, events.parquet,
qc_report.json}`.
**Owner:** market-data. **Related alerts:** `SequenceGapDetected`, `BookStale`, `StaleFeed`,
`FeedWallClockStall`, `TargetDown`.

> **Shells.** Blocks tagged `bash` run in bash (Linux, the containers, Git Bash on Windows).
> The owner's Windows machine uses PowerShell: each `PYTHONPATH=src python3 ...` has a
> `powershell` variant below it (`$env:PYTHONPATH = "src"; python ...`; `python3` is `python`;
> set the variable once per window; `a && b` is `a; if ($?) { b }`). `bash tests/harness/...`
> and `bash build.sh` need Git Bash or WSL on PATH; `kubectl`, `docker` and `curl` lines
> are for the cluster/containers and are the same in PowerShell except for the text filters,
> which are given in PowerShell where the owner would run them locally.

### 1. Normal operation

Local / on-demand run (deterministic — same seed ⇒ bit-identical files):

```bash
cd python
PYTHONPATH=src python3 -m iap.marketdata            # uses configs/marketdata/generator.json (seed 20260829)
PYTHONPATH=src python3 -m iap.marketdata --seed 42  # explicit seed override
# custom locations:
PYTHONPATH=src python3 -m iap.marketdata --configs-dir ../configs --out ../data
```

```powershell
Set-Location python; $env:PYTHONPATH = "src"
python -m iap.marketdata                  # uses configs/marketdata/generator.json (seed 20260829)
python -m iap.marketdata --seed 42
python -m iap.marketdata --configs-dir ../configs --out ../data
```

Container / scheduled:

```bash
# compose (runs once as the data-generator service, writes the iap-data volume)
docker compose -f deployment/docker/docker-compose.yml up data-generator

# k8s: daily CronJob at 07:30 UTC (deployment/k8s/cronjob-data-pipeline.yaml)
kubectl -n intraday-alpha get cronjob data-pipeline
kubectl -n intraday-alpha create job --from=cronjob/data-pipeline data-pipeline-manual-$(date +%s)   # ad-hoc run
kubectl -n intraday-alpha logs job/<job-name>
# PowerShell: the ad-hoc job name is "data-pipeline-manual-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
```

### 2. Verify a run succeeded

```bash
ls data/raw data/normalized
python3 -m json.tool data/normalized/qc_report.json | head -40
```

```powershell
Get-ChildItem data/raw, data/normalized
python -m json.tool data/normalized/qc_report.json | Select-Object -First 40
```

Expect in `qc_report.json`: per-stream event counts, and gap/duplicate/
out-of-order/invalid counters consistent with `configs/marketdata/generator.json`
`anomalies` rates (goldens run with anomalies disabled). For the committed
config (v1.4.0) the totals are 309,598 events in and 308,975 out, and every
equity stream trades up to the close auction (up to v1.3.0 equity flow
stopped about 40% of the way through each session; that dataset is
reproduced by `equities.flow.calibration = "legacy_budget"`). The **dataset
version** used by every experiment manifest is the content hash of the
normalized `.iap1` files, not of the QC report
(`docs/GOVERNANCE.md` §1–§2):

```bash
cd python && PYTHONPATH=src python3 -c "from iap.experiment.tracker import data_version; print(data_version())"
# 116b77873de422ddd3b4f875625a0d8bba7dc2850068aae6ed807e944fb34573   (committed config, v1.4.0)
```

```powershell
Set-Location python; $env:PYTHONPATH = "src"
python -c "from iap.experiment.tracker import data_version; print(data_version())"
```

Cross-check determinism after any pipeline code change:

```bash
cd python && PYTHONPATH=src python3 -m pytest -q tests/test_generator.py tests/test_normalize.py
```

```powershell
Set-Location python; $env:PYTHONPATH = "src"; python -m pytest -q tests/test_generator.py tests/test_normalize.py
```

### 3. Failure modes

#### Sequence gaps (`SequenceGapDetected`, `BookStale`) {#sequence-gaps}

0. **One gap is the incident.** `configs/risk/risk.json`
   `market_data.max_sequence_gap_before_halt = 1`: a single gap marks the book
   stale and fail-closes trading in that instrument until a SNAPSHOT recovers
   it. `SequenceGapDetected` therefore fires on `increase(md_sequence_gaps_total[5m]) > 0`
   (not a rate), and the counter is exported on **every** event, so it is
   visible on the next scrape. If no snapshot arrives, `BookStale` follows
   after 2 m and `book_stale{instrument="..."}` stays 1 — that instrument is
   silently not trading until you act.
   ```bash
   curl -s http://platform-metrics:8080/metrics | grep -E '^md_sequence_gaps_total'
   # (book_stale and the BookStale alert were retired with the Java image, v1.17 F9:
   #  the Python exporter reads a finished run's report, not a live book)
   ```
1. Confirm scope: one stream or all — Grafana "Market Data & Latency" →
   *Stream QC* panel; `qc_report.json` per-stream counters for batch runs.
2. Downstream effect: books mark `stale=true` on a gap and recover on a
   complete SNAPSHOT burst (conventions §4; a gap *inside* a burst breaks
   it, the next complete burst recovers); risk fail-closes on stale books —
   expect `risk_rejected_total` to rise. That is correct behavior, not a
   second bug. Live paths with A/B feeds or retransmission should run the
   books with a `reorder_window` (≤ 4096) so late retransmissions heal the
   hole instead of waiting for the next snapshot (`late_recovered` counts
   them; the window is checkpointed).
3. **Sequence lifecycle** (API_CORE §4): the first sequence of a stream is
   accepted whatever its value (0 is legal); a SNAPSHOT burst that restarts
   *below* the last sequence is a venue **sequence reset** (daily restart,
   partition fail-over) — `sequence_resets` increments, the book is stale
   until that burst completes; the normaliser keeps day-2 data as a new
   epoch (`sequence_resets` in `qc_report.json`), never as duplicates. If a
   venue restarts *without* sending a snapshot, call `reset_sequence()` /
   `ReplayEngine.reset_sequences()` from the session-roll handler.
4. **Timestamps**: `receive_ts < exchange_ts` is clamped + counted
   (`ts_clamped`); an `exchange_ts` that goes backwards *within* a stream is
   dropped + counted (`ts_regression_dropped`) and shows up as a gap for the
   book — investigate the venue clock (PTP step, leap-second smear), do not
   re-sort by hand.
5. **Malformed records** never stop a replay: every class is dropped and
   counted per book (`invalid_payload_dropped`, `unknown_type_dropped`,
   `invalid_side_dropped`, `modify_price_mismatch`, `unknown_order_events`);
   a non-zero `modify_price_mismatch` means an adapter maps price changes
   to MODIFY instead of CANCEL+ADD. IAP1 v2 files are CRC-32 checked on
   load: a CRC error is corruption in transit/at rest — re-transfer, never
   partially accept.
6. Batch pipeline: a gap rate far above `configs/marketdata/generator.json`
   `anomalies.gap_prob` means a generator/normalizer regression — bisect with
   the determinism tests above; identical seed must reproduce identical
   counters.
7. Do NOT hand-edit normalized files (raw is immutable, normalized is
   regenerated); fix code/config and re-run.

#### Stale feed (`StaleFeed`) {#stale-feed}

0. **Which question are you answering?** Two metrics, two meanings
   (`PLATFORM_CONVENTIONS.md` §12.6):
   - `md_event_time_gap_seconds` — the gap between consecutive EVENTS. This is
     what `StaleFeed` alerts on, and it is correct whether the session is live
     or replaying an August capture.
   - `time() - md_last_event_wallclock_unixtime` — whether the PROCESS is
     still moving. That is `FeedWallClockStall`, scoped to
     `platform_mode{mode="realtime"}` so a replay never trips it.
   `md_last_event_unixtime` is the replayed event's own timestamp: never
   compare it to `time()` (the round-2 rule did, and paged on every historical
   replay the desk ran).
1. `kubectl -n intraday-alpha get pods` / `docker compose ps` — is the
   producer alive? `/ready` returns 503 on a stalled realtime feed, so k8s has
   already taken the pod out of the Service; `/health` stays 200 unless the
   loop itself is wedged for 30 s.
2. Check the last event age vs the 5 s budget
   (`configs/risk/risk.json` `market_data.stale_feed_timeout_ns`). A silent
   venue (no gap, the line just stops) is invisible to sequence-based
   `stale`; consumers use `OrderBook.is_fresh(now_ns, max_age_ns)` and the
   consolidated view excludes only gap-stale venues (`stale_venues()`).
3. If the producer is healthy but consumers see nothing: shared volume/PVC
   mounted? (`kubectl describe pod`, `Events:` section for mount errors).
4. Risk is already rejecting orders against stale books: the risk engine's
   mark carries the market-data event time of the venues at the
   consolidated touch (`PaperTrading.RiskWiring`), so a silent line
   surfaces as `STALE_PRICE` rejects once the last quote is older than
   `stale_feed_timeout_ns`, and a gap-stale venue as `SEQUENCE_GAP` until
   its SNAPSHOT recovery (`onFeedRecovered`). The execution simulator
   produces no fill on a stale or non-TRADING venue book and the SOR never
   routes to one (conventions §11.2-11.3). No manual trading halt needed
   unless recovery exceeds one session — then follow
   `docs/RUNBOOKS.md`.

#### Event-bus queue depth — CONTRACT ONLY {#queue-depth}

**No producer exports `eventbus_queue_depth`, and there is no alert or panel
for it any more.** The round-2 `QueueDepthHigh` alert (severity `critical`)
could never fire, and the dashboard panel said "No data" forever;
`PLATFORM_CONVENTIONS.md` §12.7 forbids a rule or panel over a metric nothing
exports. The NAME and threshold remain a design contract, recorded in
`deployment/grafana/README.md` "PLACEHOLDER metrics", to be wired together
with a producer.

The procedure once one exists: consumer slower than producer — check consumer
CPU throttling (`kubectl top pods`), GC pauses (dashboard panel), and recent
deploys. A sustained 100k backlog means the latency SLO is already gone;
prefer restarting the slow consumer over letting it drain for hours.

#### Real dataset refused: manifest x-version 1 (v1.14)

Every reader of an ingested real dataset (ingest-into, merge, feature
build, `--dataset-dir` research runs) refuses a manifest written before
v1.14, with a message naming the old ingest rules. This is deliberate: the
v1.14 rules sign hidden prints differently, round midpoint prints half to
even and recognise the half-penny tick, so an old store would mix two
definitions of the same column. The fix is to re-ingest from the raw ITCH
file with the current code (REAL_DATA.md §3 and §4). The new
`dataset_version` differs from the old one by construction (domain
`iap.real-dataset.v2`); the old one stays valid in the ledger as a
statement about the old rules.

#### Pipeline job failed

```bash
kubectl -n intraday-alpha logs job/<job-name>       # ValueError/RuntimeError messages are explicit
```

Common causes: missing `pyarrow` (image build issue — the pipeline needs it
for `events.parquet`), a config edit that failed validation, PVC full
(`kubectl -n intraday-alpha get pvc iap-data`). The job is idempotent and
`concurrencyPolicy: Forbid` prevents overlap — after fixing, just re-create
the job (§1). Re-runs are safe: same seed ⇒ same bytes.

### 4. Escalation

Data integrity affects everything downstream (features, alphas, risk).
If normalized data was consumed by research runs while corrupt: identify the
affected `data_version` hashes, mark those runs invalid in
`research/models/ledger.json` (append a correcting entry — never edit
history) and re-run after the fix.

---

## Runbook: process-and-delete (one free full-market day at a time)

v1.17 (plan A2). A full-market day is 4-18 GB of gzip and its raw plus
normalized files are 1-2x that again; this machine has 16 GB of memory and
28 GB of free disk. The breadth universe (50-100 names, plan A3) is therefore
ingested one day at a time and only what research reads is kept:
`features/`, `dataset.json`, `configs/`, `reference/`, `normalized/qc_report.json`
and `KEEP.json` (the sha256 of every kept file). `raw/`, `normalized/*.normalized.*`
and the vendor file are deleted after `KEEP.json` is verified.

Commands are PowerShell (a backtick continues a line; the square brackets mark
optional parts: delete the brackets, keep or drop the flag inside).

```powershell
$env:PYTHONPATH = "python\src"
python -m iap.marketdata process-day --manifest research\breadth\candidates.json `
    --date 2019-01-30 --vendor-dir data\vendor --out-root data\real\breadth `
    --symbols-file research\breadth\universe.json [--download --i-have-approval] `
    [--partition-instruments 10] [--engine rust] [--workers 1] [--keep-raw] [--keep-gz] `
    [--min-free-gb 10] [--dry-run]
```

### The chain and its markers

| step | what | marker (in `<out-root>\ds_<yyyymmdd>.markers\`) |
|---|---|---|
| disk guard | refuses unless free space >= estimated peak + `--min-free-gb` (vendor file if absent + dataset + partition temp + features); never deletes to make room | none (checked every run) |
| download | only with `--download --i-have-approval`: `tools/r7_download.py` (allow-listed hosts, size then sha256 check); a present file with the right size is used as is | `_DONE_download` |
| ingest | `iap.marketdata.ingest --slim` (no `*.normalized.jsonl` twin; `--no-slim` writes it) with `--partition-instruments N` (the normaliser routes packed records to on-disk partitions of N instruments, sorts each, merges in event-time windows: memory is one partition plus one window, the bytes are identical to the in-memory path) | `_DONE_ingest` |
| delete_raw | `raw/` deleted right after normalisation (the IAP1 holds the events); `--keep-raw` keeps it | `_DONE_delete_raw` |
| features | `python -m iap.features --engine rust --workers 1` into `features/`; log in `features.log` | `_DONE_features` |
| keep | `KEEP.json` written and re-verified | `_DONE_keep` |
| delete | `raw/`, `normalized/*.normalized.*` and the vendor file, after `KEEP.json` verifies again; `--keep-raw` / `--keep-gz` keep them | `_DONE_delete`, `_DONE_ALL` |

A step with a marker is skipped, so a crashed day resumes from the step that
failed. A failed features build stops the chain before anything is deleted.
`process_report.json` in the dataset directory holds the timings, the
estimate, the guard reading and what was deleted.

### Detached run and the watchdog

```powershell
python -m iap.marketdata process-day ... --render-cmd data\real\run_process_20190130.cmd `
    --jobs-registry data\real\jobs.json
```

writes the `.cmd` wrapper (`START` / the call / `EXIT n` into
`run_process_20190130.log`) and registers `process_20190130` in the watchdog
registry (`restart_safe: true`, two restarts, 120 min stall; the process
pattern uses the bracket trick, `process-da[y] .*--date 2019-01-30`). Launch
it detached the usual way (docs/RUNBOOKS.md, `Win32_Process.Create`),
one heavy job at a time.

### Sizing (measured R7 rates, upper ends)

Peak disk = vendor file (if it must be downloaded) + dataset (1.7x gzip x
symbols/3) + partition temp (2x dataset) + features (0.6 GB x symbols/3).
For a 5 GB day and 100 names that is roughly 5 + 283 + 567 + 20 GB: far more
than the free disk, so the first breadth days go through with a SMALLER
universe or `--keep-raw` off and the download already present, and the
estimate's rates are re-measured on the first day (`process_report.json`)
before the next. The guard refuses rather than guesses; `--peak-gb` overrides the estimate with a measured value (the first day's `process_report.json` gives it).

### What is never done here

Nothing is downloaded without the approval flag; nothing outside the day's
dataset directory and its vendor file is deleted; the holdout sessions
(`data/real/r7_holdout`, the 2026 holdout) are not inputs of this chain.

### IEX HIST days: `process-iex-day` (v1.17, A4)

An IEX day is two captures (TOPS = the BBO oracle, DEEP+ = the displayed
orders; `--feed deep` takes the DEEP price-level feed instead) and the owner's
rule is one vendor file on disk at a time, so the chain is: disk guard
(the larger capture + dataset + partition temp + features + `--min-free-gb`,
default 30) -> `download_tops` -> `tops_extract` (the universe's quotes to
`<out-root>\iex_<yyyymmdd>.markers\<yyyymmdd>.tops.json.gz`) -> `delete_tops`
-> `download_book` -> `ingest --format iex-deep-plus --tops <extract>` (a
divergent book refuses unless `--allow-book-divergence`; the report lands in
`book_divergence.json` beside the markers) -> `delete_raw` -> `features` ->
`keep` -> `delete` (the book capture, the extract, `normalized/*.normalized.jsonl`;
with `--keep-normalized`, the default and the owner's A6 rule, the IAP1 event
files and the Parquet stay and are hashed into `KEEP.json` so the two-venue
router (B8) can replay both venues; `--delete-normalized` is the A2 behaviour;
`process_report.json` records `kept_normalized_gb` and `kept_dataset_gb`). The
manifest comes from the HIST index with the owner's approval sentence:

```powershell
python tools\iex_hist.py index --date 2025-11-28 ... --out research\v117\iex_overlap_manifest.json --approval "<sentence>"
python -m iap.marketdata process-iex-day --manifest research\v117\iex_overlap_manifest.json `
    --date 2025-11-28 --vendor-dir data\vendor --out-root data\real\iex --symbols AAPL,MSFT,QQQ `
    --download --i-have-approval [--feed deep-plus] [--round-lots-from data\real\r7_20251128\dataset.json] `
    [--partition-instruments 10] [--min-free-gb 30] [--peak-gb N]
# one chain, days in order; a failed day is logged and the next runs:
python -m iap.marketdata process-iex-day ... --date D1 --date D2 ... --render-cmd data\real\run_iex_overlap.cmd `
    --jobs-registry data\real\jobs.json
# the rolling list; NOT a download:
python tools\iex_hist.py recent --n 5 --out research\v117\iex_recent_manifest.json
```

**Round lots.** TOPS quotes the first price at which the displayed quantity accumulated
from the top reaches a round lot, with that cumulative size (measured on 2025-11-28: AAPL
sizes never below 100, MSFT / QQQ never below 40; the rule matched 113,807 of 113,807
sampled quotes), so the oracle compares the book's cumulative round-lot quote. `--round-lots-from` takes the paired Nasdaq day's
`dataset.json` (`lot_size` per symbol); without it the smallest quoted TOPS size per symbol
is used and `process_report.json` says `round_lots_source: tops_min_quoted_size`.

`tools/iex_download.py` (one feed per call) refuses without `--i-have-approval`
and without an `approval` sentence in the manifest, so the `recent` manifest
cannot be downloaded until the owner approves it. Results:
docs/RESEARCH_NOTES.md.

---

## Runbook: long-job watchdog

Long real-data jobs run detached through WMI-launched `.cmd` wrappers in
`data/real/` (each appends `START ...` and finally `EXIT n` to its log). A job
that dies without writing `EXIT` is otherwise silent. `tools/jobs/watchdog.py`
checks every registered job on each tick and tells you.

### What it does per tick

| state | meaning | action |
|---|---|---|
| `running` | matching process alive, log grew within `stall_minutes` | none |
| `stalled` | process alive, log unchanged for `stall_minutes` | log + toast (never kills) |
| `done` | no process, last marker matches `done_regex` (`EXIT 0`) | log + toast |
| `failed` | no process, last marker `EXIT n`, n != 0 | log + toast |
| `died` | no process, no `EXIT` after the last `START` | restart if `restart_safe` and under `max_restarts`; log + toast |
| `missing` | log file does not exist | log only |

Notifications fire on state *changes* (and on every restart), so a finished job
toasts once. Toast route: BurntToast module if installed, else the built-in
`Windows.UI.Notifications` API via PowerShell, else `msg.exe`.

The watchdog **never kills or signals a process**. Restarts use the same
`Win32_Process.Create` (WMI) detached launch as the manual flow, so restarted
jobs survive the watchdog exiting. Only mark a job `restart_safe` when its
wrapper is idempotent / resumable — a non-resumable study should stay `false`
(you get a `died` toast instead).

### Registry

Copy `tools/jobs/jobs.example.json` to `data/real/jobs.json` (git-ignored)
and edit. Relative paths resolve against the registry file's directory.
Per job: `name`, `wrapper`, `log`, `process_pattern` (regex over process
command lines), `done_regex`, `restart_safe`, `max_restarts`, `stall_minutes`,
optional `enabled`, `exit_regex`, `start_regex`. Restart counts and last state
live in `state_file` (`watchdog_state.json`); delete a job's entry to reset its
restart budget. Transitions are appended to `watchdog_log`.

### Running

```powershell
python tools\jobs\watchdog.py --registry data\real\jobs.json --once --dry-run   # look only
python tools\jobs\watchdog.py --registry data\real\jobs.json --once
python tools\jobs\watchdog.py --registry data\real\jobs.json --loop --interval 600
.\tools\jobs\run_watchdog.cmd [registry]                                       # one tick
```

### Enabling it with Task Scheduler (owner does this; nothing registers it automatically)

Every 10 minutes, as the logged-in user (needed for toasts), only while logged on:

```powershell
schtasks /Create /TN "IAP Job Watchdog" /SC MINUTE /MO 10 /IT /TR "C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\tools\jobs\run_watchdog.cmd"
```

(One line, no inner quotes: the path has no spaces. The cmd-style `^` continuation and the
`\"` escapes do not work in PowerShell.)

Check / run now / disable / remove:

```powershell
schtasks /Query /TN "IAP Job Watchdog" /V /FO LIST
schtasks /Run /TN "IAP Job Watchdog"
schtasks /Change /TN "IAP Job Watchdog" /DISABLE
schtasks /Delete /TN "IAP Job Watchdog" /F
```

(Or Task Scheduler GUI: Create Task -> Triggers: Daily, repeat every 10 minutes
indefinitely -> Actions: Start a program, `run_watchdog.cmd` -> "Run only when
user is logged on".) A cmd window flashes each tick; to avoid that, point the
action at `pythonw.exe` with arguments
`tools\jobs\watchdog.py --registry data\real\jobs.json --once` and the repo as
"Start in".

---

## Runbook: forward paper trade (daily, real sessions)

`python -m iap.paper.daily` (v1.16 plan item 9) runs the **current rule** on each
new real Nasdaq session through the MVP loop and writes a dated report.

> **Infrastructure, not a money claim.** The v1.16 in-sample study (#138) found
> EQ10 / EQ05 never clear taker costs (0 trades in 480 cells). The daily P&L is
> reported, never promoted. The point is the plumbing: realism, TCA against the
> research cost model, drift / alpha-decay monitors and a kill-switch drill
> every day, so a future rule that does clear costs lands on a tested path.
>
> Not to be confused with docs/RUNBOOKS.md (the Java shadow stack on the
> golden replay vector).

### What one run does

For session `D` (config `configs/paper/paper_trade.json`):

1. **Locate / ingest.** `--dataset DIR`, else the `data\real` dataset whose
   `dataset.json` lists `D` (`data\real\paper_<yyyymmdd>` wins), else `--itch FILE`
   is ingested into `data\real\paper_<yyyymmdd>` (`python -m iap.marketdata ingest`)
   and its feature store built (`python -m iap.features`). Datasets matching
   `forbidden_datasets` (`r7_holdout`, `r7_2025*`, `r7_20260612`, `holdout2026`)
   are refused: the R7 holdout is never paper-traded.
2. **MVP run** per symbol (`iap.mvp.real.run_symbol`) with `configs/prereg/v3.json`
   realism (venue latency, fee tier `mid`, ITCH exec prints, hidden prints,
   regulatory fees, feed latency) and the opt-in end-of-day flatten
   `execution.eod_flatten_lead_ns` (config: 300 s before the close).
3. **Live-vs-backtest TCA** (`iap.paper.tca_compare`): per parent, the MVP's
   simulated implementation shortfall / spread / fee / impact (bps, `iap.tca`)
   against the research `CostModel` (`configs/execution/execution.json` + dated
   regulatory fees) for the same filled quantity at the decision mid and half
   spread; per algo and aggregated (filled-qty weighted). The MVP impact is the
   linear rule 6, the research model square-root: an impact gap is expected.
4. **Monitors** (`iap.paper.monitors`), when the dataset has `features/`:
   feature drift (PSI / KS / mean shift / std ratio, `iap.research.drift`) against
   the in-sample stores in `drift.reference_features`; realised EQ10 / EQ05 IC at
   1s (research definitions of `tools/r7_confirm.py`) against the holdout ICs
   0.0838 / 0.0409, for the day and as a rolling mean over earlier reports.
5. **Kill-switch drill** (`iap.paper.drill`), dry: a separate engine replays the
   day until a child rests at a venue, engages the strategy kill, and checks every
   working child is cancelled and nothing is submitted after it (v1.15 #115).

Output `data\real\paper\<D>\`: `mvp\<SYMBOL>\` (MVP artefacts + `run_manifest.json`),
`tca_compare.json`, `monitors.json`, `drill.json`, `report.md`, and `report.json`
(written last, `status: complete`).

**Idempotent.** A completed date is a no-op; `--force` deletes and rebuilds that
one date directory. **Exit codes:** `0` done / nothing to do, `10` done with
alerts (read `report.md` → Alerts), `2` error (no dataset, forbidden dataset,
ingest failure).

Thresholds (`configs/paper/paper_trade.json`): `tca.is_gap_alert_bps` (2.0),
`drift.thresholds` (the `DriftThresholds` defaults), `ic.min_ratio` (0.25 of the
holdout IC), `ic.rolling_window` (20) / `ic.min_rolling_days` (5),
`drill.max_events_after_kill` (5000). Alerts are descriptive: they never change a
pre-registration, a gate or a result.

### Disk

A normalized real day is 10-25 GB plus features. Check free space before an
ingest (`--itch`); remove `data\real\paper_<yyyymmdd>\raw` after a good run if
space is short. The report directories are small.

### Run by hand

```powershell
Set-Location C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = "python\src"
python -m iap.paper.daily --date 2026-10-15 --itch data\real\itch\10152026.NASDAQ_ITCH50.gz
python -m iap.paper.daily --pending
python -m iap.paper.daily --date 2026-10-15 --force
```

`--limit-events N` truncates each symbol's stream (smoke runs only).

### New sessions: from the Nasdaq watch to a report

1. The Nasdaq watch (docs/RUNBOOKS.md) reports a usable new day; it never
   downloads. Download it as usual (owner approval, `tools/r7_download.py`).
2. Run `python -m iap.paper.daily --date D --itch <file>` once by hand (ingest +
   features + report), or ingest into `data\real\paper_<yyyymmdd>` yourself.
3. The scheduled task (below) then picks up any `paper_*` session still without a
   complete report (`--pending`); it does not ingest.

### Daily schedule (not registered; the owner registers it)

`tools\run_paper_daily.cmd` runs `--pending` and appends to
`data\real\paper\paper_daily.log` (`START` / `EXIT <rc>` markers).

```powershell
schtasks /Create /TN "IAP paper trade daily" /SC DAILY /ST 10:00 /TR "C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\tools\run_paper_daily.cmd" /F
```

This is one line that works in PowerShell and in cmd: the path has no spaces, so it needs no inner quotes (the cmd-style escape for inner quotes does not work in PowerShell). 10:00 is after the 09:00 Nasdaq watch. Remove with
`schtasks /Delete /TN "IAP paper trade daily" /F`; run once now with
`schtasks /Run /TN "IAP paper trade daily"`.

### Job watchdog entry

Add to `data\real\jobs.json` (docs/RUNBOOKS.md; paths relative to that file):

```json
{
  "name": "paper_daily",
  "wrapper": "..\\..\\tools\\run_paper_daily.cmd",
  "log": "paper\\paper_daily.log",
  "process_pattern": "iap\\.paper\\.dail[y]",
  "done_regex": "^EXIT (0|10)\\s*$",
  "restart_safe": true,
  "max_restarts": 1,
  "stall_minutes": 240
}
```

`restart_safe` is true: a rerun skips completed dates and rebuilds an incomplete
one from scratch.

### When it alerts

- **TCA gap:** compare `tca_compare.json` → `per_algo`; a persistent impact gap is
  the linear-vs-sqrt model difference, a spread / fee gap points at the fee tier or
  exec-print fills.
- **Feature drift:** `monitors.json` → `drift.groups`; descriptive, check the
  ingest QC (`dataset.json` → `qc_totals`) first.
- **IC below 0.25 x holdout / sign flip:** one day is noise; the rolling alert
  (≥ 5 days) is the alpha-decay signal. Report it; do not retune.
- **Drill fail:** a working child survived the kill or a child was submitted after
  it. Treat as a kill-switch regression: docs/RUNBOOKS.md.

---

## Runbook: Track C forward shadow book (daily, paper only)

`python -m iap.paper.shadow_book` (v2.0 U0 stage 10) fills the forward window of the
Track C confirmation one session at a time, under the accrual rule of
`research/v2/U13_CONFIRMATION_PROTOCOL.md` section 8.

> **Paper only.** The module imports no broker, OMS, socket or HTTP code (a test scans
> its imports) and refuses to run in a process that has loaded a broker client. Live
> orders are not authorised (U-13).
>
> Not to be confused with [docs/RUNBOOKS.md](RUNBOOKS.md) (the v1.16 MVP
> paper trade) or [docs/RUNBOOKS.md](RUNBOOKS.md) (the Java
> shadow stack).

### What one run does

1. Takes the next session of the registered calendar that has no row yet.
2. Reads `<inbox>\<session>.json`: the frozen combined-book configuration's target
   weights per decision, the decision mids, the market at arrival per name, and the
   close mids. The file must carry the frozen configuration's SHA-256 from the config.
   The path passes the data-read guard (`iap.core.guard.check`) and the forbidden-dataset
   list; a sealed partition is refused.
3. Turns each decision's targets into paper orders against the paper position and
   prices them with the one EN4 fill convention (`iap.execution.sim.simulate`, 500 ms
   arrival latency) under the three cost columns. Each column (base, conservative,
   stress) is its own paper book. The U13 series is the conservative column.
4. Applies the usability rule (`m_min` usable units, time and grid coverage; thresholds
   in the config) and appends one row to `chain.jsonl`. The row holds counts, the input
   file's SHA-256 and the SHA-256 of `sealed\<session>.json`, which holds the P&L. Each row
   carries the previous row's hash, so an edit, deletion or reorder anywhere breaks
   `verify`.

The job prints counts and the accrual state (`accruing`, `complete`, `pending`) only.

### Configuration (fixed before T0)

`configs/paper/shadow_book.json` and its calendar `configs/paper/shadow_book_calendar.json`
are templates. The registration replaces `frozen_configuration_sha256`, `capital`, `T_min`,
`m_min`, `grid_points` and the calendar. With the shipped placeholders (empty calendar) the
job refuses to start.

### Commands

```bash
python -m iap.paper.shadow_book run --config configs/paper/shadow_book.json --inbox data/shadow_book_inbox --out data/shadow_book
```

```bash
python -m iap.paper.shadow_book status --config configs/paper/shadow_book.json --out data/shadow_book
```

```bash
python -m iap.paper.shadow_book verify --config configs/paper/shadow_book.json --out data/shadow_book
```

```bash
python -m iap.paper.shadow_book mark-missing --config configs/paper/shadow_book.json --out data/shadow_book --session 2027-01-05 --reason "feed outage, no recoverable data"
```

Exit codes: `0` appended, already recorded, not yet due, or calendar exhausted; `2` refused
(the reason is on stderr and in `shadow_book.log`).

### Refusals and what to do

| Message | Meaning | Action |
|---|---|---|
| `input for D is missing` | the inbox has no file for the next session | produce it; if the data is truly lost, `mark-missing` with a reason (counts as attempted, not usable) |
| `D is out of order` | a later session was asked for | run the next session first; nothing is ever skipped |
| `already recorded from different input bytes` | the inbox file of a recorded day changed | investigate; the chain is append-only, the first record stands |
| `not produced by the frozen configuration` | the file's configuration hash differs | the producer is not running the frozen configuration |
| `chain broken` / `altered` | a row or sealed file was edited | stop; restore from backup; record the incident |
| `the window is complete` / `pending` | accrual has ended | no more rows; the runner does the single read |

Re-running a recorded day with the same input is a no-op.

### No peeking and the single read

Until completion, only counts are visible (`status`). `iap.paper.shadow_book.read_series`
serves the identity `iap-runner` only, only when the state is `complete`, and only once: it
appends a `read` row with the series hash before it returns. Keep `sealed\` readable by the
runner account only (an NTFS ACL the owner sets); the code does not set file permissions.

### Scheduling (owner)

`tools\install_shadow_book_task.ps1` registers the task `IAP-TrackC-ShadowBook`, which runs
`tools\run_shadow_book.cmd` Monday to Friday at 17:30 local time. Run it with `-WhatIf` first.
Nothing installs it automatically.

```bash
powershell -ExecutionPolicy Bypass -File tools/install_shadow_book_task.ps1 -WhatIf
```

### Known gaps

- The producer of the inbox files (the frozen configuration run on real family forecasts
  each session) is not built: it needs the families wired through `BookPanel.forecasts`.
- `accrual_state` mirrors `iap.validation.pf3.accrual_status` and should call it once that
  module is on main.
- Carried inventory across a `mark-missing` session is valued from the last recorded close.
- The single read here does not yet go through `iap.validation.profiles.read_series`.

---

## RUNBOOK — Paper Trading / Shadow Deployment

> **Retired flow (v2.0 docs audit, 2026-10-25).** This runbook describes the Java shadow stack, archived at git tag `archive/java-v1.14.0` and no longer in the tree; none of its commands runs on `main`. It stays at this path because the Prometheus alert annotations in `deployment/k8s/configmap-prometheus.yaml` name it as their runbook and `tests/harness/check_deployment.py` validates those annotations. The maintained equivalents are `docs/RUNBOOK_v2.md`, `docs/RUNBOOKS.md` and `deployment/grafana/README.md` (archive/docs/DOC_AUDIT_v2.md).

> **Shells.** These procedures run on the Linux side (the cluster, the containers, WSL or
> Git Bash): the `bash` blocks are bash. From the owner's Windows PowerShell, translate
> as follows: `curl` is `curl.exe` (in PowerShell `curl` is an alias of `Invoke-WebRequest`);
> `export NAME=value` is `$env:NAME = "value"`; `PYTHONPATH=python/src python ...` is
> `$env:PYTHONPATH = "python/src"; python ...`; `python3` is `python`; `grep X file` is
> `Select-String -Path file -Pattern X`; `| head -N` is `| Select-Object -First N`;
> `tail -N file` is `Get-Content file -Tail N`; `cat file` is `Get-Content file`;
> `ls -l dir` is `Get-ChildItem dir`; `$(...)` command substitution is `$(...)` too but
> with PowerShell inside (for example `$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())`
> for `$(date +%s)`); `a && b` is `a; if ($?) { b }`; a trailing `\` continuation is a
> backtick. `kubectl` and `docker` take the same arguments in both shells.

> Renamed from `RUNBOOK_paper_trading.md` (v1.17 WS0). This is the Java shadow stack
> on the golden replay vector. The forward paper trade on real sessions
> (`python -m iap.paper.daily`) is [docs/RUNBOOKS.md](RUNBOOKS.md).

**Scope:** promotion gate 11 (spec §20): running strategies against live-shaped
event flow with the real risk engine, real execution simulator and full
observability — no capital at risk.
**Owner:** trading platform. **Related alerts:** `SignalRateCollapse`,
`LiveVsBacktestDrift`, `AlphaLifecycleRetired`, `FillRateDrop`,
`PreTradeRejectRatioHigh`, `LossLimitUtilizationHigh`,
`GrossNotionalUtilizationHigh`, `KillSwitchEngaged`,
`PlatformSessionFailed`, `SessionStoppedNotResumed`,
`SessionRestartsClimbing`, `FeedWallClockStall`, `RoutedVenueMismatch`,
`ResumeReleasedOpenOrders`, and the `Watchdog` heartbeat. Delivery is through Alertmanager
(`deployment/alertmanager/`, since v1.3.0; `page` / `ticket` / `Watchdog`
routing since v1.12). Compose delivers to the local `alert-sink` echo service
until `ALERT_PAGE_URL_FILE` / `ALERT_TICKET_URL_FILE` name real receivers; on
Kubernetes the Secret `iap-alertmanager-webhook` must carry both `page_url`
and `ticket_url` (docs/RUNBOOKS.md §2).

> **v1.17 (WS-F item F9): this runbook describes the RETIRED Java shadow
> deployment** (`java-platform`, `java-platform-state`, the admin API,
> `--resume`, `Dockerfile.java`). The deployed stack is now the Python
> `platform-metrics` + `mvp-session` pair (`deployment/docker/docker-compose.yml`,
> `deployment/k8s/platform-metrics.yaml`; `deployment/grafana/README.md` for
> the metric contract). The procedure below is kept for the record until the
> Python paper path (`python -m iap.paper.daily`, docs/RUNBOOKS.md) has
> its own shadow runbook.
>
> **Status (historical):** the paper-trading loop was LIVE — `com.iap.platform.PaperTrading`
> (started by `archive/java/paper.sh (at git tag archive/java-v1.14.0)` (archived, frozen at v1.14.0) locally, or as the `java-platform` compose
> service) runs the full vertical over deterministic replay sessions and
> serves `/metrics`, `/health`, `/ready`, `/status` and the kill-switch admin
> API on :8080 (`com.iap.api.MetricsServer`). Replay, paper and production
> share contracts (spec §1), so the stack, the dashboards and this procedure
> are identical across modes.

> **What the shipped stack actually is — read this first.**
> The `java-platform` container replays the pinned **golden equity vector**
> (2,000 events spanning 6,974 s of event time) at `--speed 60`, so a session
> lasts about two minutes and then **ends**: report written, final checkpoint,
> `platform_session_state = 2 (FINISHED)`, exit 0. Compose runs it with
> `restart: on-failure` and k8s as a single-replica `Recreate` Deployment, so a
> completed session is NOT re-run in a loop.
> Consequences for anyone reading the dashboards:
> - counters (`md_events_total`, `risk_*_total`, `exec_*_total`) cover ONE
>   session; `offset 1h` baselines (`SignalRateCollapse`, `FillRateDrop`) are
>   meaningless on a two-minute demo and only become meaningful against a
>   continuously fed session;
> - to watch a longer window, lower `--speed` or point `--events` at a full
>   normalized day (`data/normalized/eq_*.normalized.jsonl`);
> - the *risk* state is NOT reset by a restart — it is checkpointed and
>   restored (§5). "Daily" limits are daily, not per-restart;
> - a session can also end **STOPPED** (`platform_session_state = 4`, since
>   v1.3.0): a SIGTERM or `docker compose stop` makes the trading thread
>   checkpoint at its next event boundary and leave. That is a resumable
>   mid-session checkpoint, not a finished session — no report is written
>   (§5, §6);
> - the monitoring/admin listener binds `127.0.0.1` unless `IAP_BIND_ADDR`
>   says otherwise (since v1.3.0). `archive/java/paper.sh (at git tag archive/java-v1.14.0)` (archived, frozen at v1.14.0) on a workstation is
>   reachable from that workstation only; the image, compose and the k8s
>   manifest set `IAP_BIND_ADDR=0.0.0.0` and restrict access with the
>   loopback-only published port (compose) or the NetworkPolicies (k8s).

### 1. Start the stack

```bash
# from the repo root — requires GRAFANA_ADMIN_PASSWORD in the environment
export GRAFANA_ADMIN_PASSWORD='<from your secret store>'
docker compose -f deployment/docker/docker-compose.yml up --build -d

docker compose -f deployment/docker/docker-compose.yml ps    # all healthy / exited(0)
```

Order of operations (compose enforces it): `data-generator` completes →
replay services (`cpp-replay`, `rust-replay`) + `java-platform` run →
`prometheus` scrapes and sends alerts to `alertmanager` → `grafana` at
http://localhost:3000 (dashboards: **Market Data & Latency**, **Trading &
Risk**). The java-platform, Prometheus and Alertmanager ports are published
on the host's loopback only (`127.0.0.1:8080`, `:9090`, `:9093`).

Out of the box Alertmanager delivers to the local `alert-sink` echo service
(`docker logs -f iap-alert-sink`). To deliver to real receivers, keep the URL
files outside the repo (docs/RUNBOOKS.md §2):

```bash
export ALERT_PAGE_URL_FILE=/path/outside/the/repo/page_url
export ALERT_TICKET_URL_FILE=/path/outside/the/repo/ticket_url
```

Kubernetes equivalent:

```bash
python3 deployment/k8s/generate_configmaps.py     # if configs changed (reviewed commit only!)
kubectl apply -f deployment/k8s/
kubectl -n intraday-alpha get pods -w
# alert delivery: receiver URLs are a Secret, created out of band
# (docs/RUNBOOKS.md §2 — keys page_url and ticket_url)
kubectl -n intraday-alpha create secret generic iap-alertmanager-webhook \
  --from-file=page_url=/secure/page_url --from-file=ticket_url=/secure/ticket_url
```

The namespace is default-deny for ingress and egress
(`deployment/k8s/networkpolicy.yaml`); the allowed flows are Prometheus →
java-platform and Alertmanager, Grafana → Prometheus, the ingress controller
→ Grafana, pods labelled `iap.role=operator` → the java-platform admin port,
DNS, and Alertmanager → HTTPS on non-private addresses. A CNI without
NetworkPolicy support silently ignores all of it.

### 2. Session checklist (start of paper session)

1. Dashboards green: feed status non-zero, gap rate ~0, kill switch ARMED.
2. `configs/risk/risk.json` limits are the intended paper limits — any change went
   through review + audit log (GOVERNANCE.md §3).
3. Strategy allow-list in `configs/strategies/strategies.json` matches the promotion
   decision; alpha params (`configs/strategies/alpha_params.json`) match the
   promoted `model_version`'s manifest.
4. Record the session header in the ops log: git commit, image tags/digests,
   config commit — this is the session's reproducibility manifest.
5. Config resolution: the process reads `--configs`, else `$IAP_CONFIG_DIR`,
   else the built-in default (`PLATFORM_CONVENTIONS.md` §12.2). In compose the
   repo's `configs/` is bind-mounted read-only at `$IAP_CONFIG_DIR`; in k8s the
   `iap-configs` ConfigMap is mounted there. Confirm the session loaded what
   you think it did:
   ```bash
   curl -s localhost:8080/status | python3 -m json.tool   # config_sha256
   head -2 <state-dir>/config_audit.jsonl                 # per-file sha256
   ```
6. `configs/execution/execution.json` controls are live, not decorative:
   `max_participation`, `min_slice_interval_ns`, `latency_budget_ns` and
   `sor.{prefer_rebate, max_venue_latency_ns}` are read by
   `PaperTrading`/`BacktestEngine` and enforced per decision
   (`PLATFORM_CONVENTIONS.md` §11.3-11.4); confirm the values are the
   intended paper values.
7. Restart from a snapshot, never cold: a risk engine that starts with
   `require_bootstrap` rejects everything with `NOT_BOOTSTRAPPED` until
   `restore(snapshot)` or `bootstrap_positions(fills)` — check the audit log
   shows `STATE_RESTORED` / `BOOTSTRAP_COMPLETE` before the first `ALLOW`.

### 3. Monitoring during the session

Watch the **Trading & Risk** dashboard:

- **Signal rate** vs backtest expectation. {#signal-rate}
  `SignalRateCollapse` (rate < 20% of 1h baseline while data flows) usually
  means feature validity flags (stale books ⇒ invalid ⇒ no signals — check
  gap rate first), warmup after a restart, or a wedged engine.
- **Live-vs-backtest drift**. {#drift}
  `alpha_live_vs_backtest_drift{alpha=...}` (LIVE from java,
  `com.iap.adaptive.DriftMonitor`) is the Population Stability Index of the
  rolling live signal window vs the research baseline
  (`research/baselines/*.json`; pinned formula in `/API_ADAPTIVE.md`). The
  0.25 alert threshold is the standard industry PSI rule of thumb (< 0.1
  stable, 0.1–0.25 moderate, > 0.25 significant shift). PSI is a monitoring
  signal only: it does not move `alpha_lifecycle_state` (that gauge is
  IC-gated — see below), and nothing in `PaperTrading` changes sizing on it.
  Persistent drift is a no-go for gate 12 and a retirement trigger for live
  strategies (gate 13) — both human decisions, not automatic ones.
- **Alpha lifecycle**. {#lifecycle}
  `alpha_lifecycle_state{alpha=...}` is **IC-gated, not PSI-gated**: WATCH when
  the rolling realized IC (`alpha_rolling_ic`, the pair-count-weighted mean
  of the bucket ICs since v1.5.0) drops below `watch_ic_gate`; RETIRED by the
  retirement rule the config names — `breach_rule` `"cusum"`, the default
  since v1.5.0: a breach reading in WATCH once the CUSUM of the shortfall
  below the gate (slack `cusum_k` 0.0025, each reading weighted by the new
  share of its window) has reached `cusum_h` 0.01, never on the reading that
  entered WATCH; or the legacy `"consecutive"`: `retire_breach_evals`
  consecutive breaches — and re-activation only
  back to WATCH (`configs/strategies/strategies.json` `adaptive.lifecycle`,
  docs/API_REFERENCE.md §6; docs/LIFECYCLE.md §2). PSI never moves it. Under the
  default rule a run of shallow breaches inside the slack does not retire
  and one or two deep ones can, so do not read the gauge by counting
  breaches. A session's `config_sha256` differs from a v1.4.0 session's
  because `execution.json`, `lifecycle.json`, `strategies.json` and
  `alpha_params.json` changed; state directories written by v1.4.0 resume
  as before.
  **And in the live loop it is OBSERVATIONAL** — a RETIRED alpha keeps trading
  at full size; nothing in `PaperTrading` reduces the target on this signal
  (pinned and tested: `PaperObservabilityTest.lifecycleGaugeIsIcGatedAndObservational`).
  `AlphaLifecycleRetired` therefore pages a human: the allocation is reduced by
  a decision (reduce `--max-pos`, or engage a STRATEGY-scope kill —
  `docs/RUNBOOKS.md` §2), never automatically. The adaptive
  *backtest* (`iap.backtest.adaptive`) does flatten on RETIRED; live does not,
  and that difference is deliberate until it is promoted through gate 13.
- **Fill rate**. {#fill-rate}
  `job:exec_fill_rate:ratio5m` = `exec_fills_total / exec_orders_submitted_total`
  — the PLATFORM's own execution counters. (Before round 3 these panels read
  rust venue-simulator counters that no deployed component ever wrote, so they
  were permanently empty while this runbook told you to act on them.)
  `FillRateDrop` below half of baseline: inspect the pre-trade reject counter
  (`exec_child_orders_rejected_total`), queue-position assumptions, and whether
  slippage moved with it — `exec_slippage_bps` is |fill − mark| in bps ×100
  (spread regime change is expected in high-vol regimes; see
  `configs/marketdata/generator.json` vol_regimes).
- **Pre-trade rejects**. {#rejects}
  `PreTradeRejectRatioHigh` (> 50% of decisions rejected for 10 m) means the
  strategy is fighting a limit or a stale book. Read the rule distribution
  before touching any limit:
  ```bash
  python3 - <<'EOF'
  import collections, json, sys
  c = collections.Counter(json.loads(l)["rule_id"]
                          for l in open("<state-dir>/risk_audit.jsonl"))
  print(c.most_common())
  EOF
  ```
- **Feed stall**. {#feed-stall}
  Two different questions, two different metrics
  (`PLATFORM_CONVENTIONS.md` §12.6):
  `md_event_time_gap_seconds` is the gap between consecutive EVENTS (what
  `StaleFeed` alerts on — correct on a historical replay); `time() -
  md_last_event_wallclock_unixtime` is whether the process is still moving
  (`FeedWallClockStall`, scoped to `platform_mode{mode="realtime"}`).
  A wedged loop also turns `/health` 503 after 30 s and a stalled realtime feed
  turns `/ready` 503 after `stale_feed_timeout_ns`, so k8s takes the pod out of
  the Service without killing it.
- **Failed session**. {#failed-session}
  `PlatformSessionFailed` (`platform_session_state == 3`): the session aborted
  (config load, decode, accounting). `/health` is 503 with the reason and the
  process exits non-zero. Check the pod logs and
  `<state-dir>/config_audit.jsonl`; a config change is the usual cause.
- **Stopped session**. {#stopped-session}
  `platform_session_state == 4` (STOPPED, since v1.3.0): a stop was requested
  and the session checkpointed mid-stream. `/status` reads
  `"status":"stopped"`, `/ready` is 503 (`session stopped`), `/health` stays
  200 — but the process exits 0 right after the checkpoint and takes the
  listener with it, so a scrape will usually not see the value. What does
  outlive it (since v1.5.0) is `<state-dir>/session_exit.json`, written by the
  trading thread after the checkpoint (`"state":"STOPPED","code":4`), and the
  `java-platform-state` exporter (compose service, k8s sidecar and Service;
  port 9102) that serves it as `platform_persisted_session_state`. The
  other evidence is the final stdout line (`status=STOPPED`) and the
  checkpoint; **no session report is written**. The dashboard's session-state
  panel falls back to the persisted value while no trading process runs. It
  is the expected result of a deliberate stop; continue the session with
  `--resume` (§5).
  `SessionStoppedNotResumed` (warning) fires when
  `platform_persisted_session_state == 4` for 10 m and no live trading
  process reports `platform_session_state == 1`. When it fires: confirm the
  stop was intended (`status=STOPPED` on stdout, a fresh
  `session_state.json`), then either resume (§5) or record why the session
  stays down — positions are as the checkpoint left them and nothing
  restarts the container (exit 0). It clears when the resumed session starts
  (the marker becomes `RUNNING`, 1) or finishes (`FINISHED`, 2). It stays
  silent for a FINISHED session (2) and for a crash, which leaves the
  `RUNNING` marker (1) behind: a crash is `TargetDown` on `java-platform`
  (critical), with the persisted value 1 and no stdout `status=` line. To
  read the marker by hand: `cat <state-dir>/session_exit.json`, or
  `curl -s java-platform-state:9102/metrics` from inside the network.
- **Safety counters** (since v1.3.0). {#safety-counters}
  Five counters, each with an alert rule. Two are handled here, three in
  `docs/RUNBOOKS.md`:
  `exec_orders_blocked_kill_pending_total` (`KillPendingNotRecorded`,
  §3 there) and `admin_auth_rate_limited_total` /
  `admin_audit_suppressed_total` (`AdminAuthRateLimited`,
  `AdminAuditSuppressed`, §0 there).
- **Routed venue mismatch**. {#routed-venue-mismatch}
  `RoutedVenueMismatch` (page) — `risk_routed_venue_mismatch_total > 0`: a
  child left for a venue other than the one the pre-trade check approved
  and was cancelled. Any non-zero value is a wiring defect between the SOR
  and the risk request, not a market event, and a venue kill or disconnect
  may have been checked against the wrong venue. Engage the GLOBAL kill
  (`docs/RUNBOOKS.md` §2), keep the state directory, and
  compare `venue_id` of the child in `decision_traces.jsonl` with the venue
  of its decision in `risk_audit.jsonl`. The alert stays up until the
  process ends: the counter never decreases.
- **Resume released open orders**. {#resume-released-orders}
  `ResumeReleasedOpenOrders` (warning) —
  `risk_resume_open_orders_released_total > 0` after a `--resume`: the
  restored risk snapshot tracked open children whose simulator no longer
  exists, and they were released (§5). Positions and realized P&L are
  restored; the working orders are gone. Reconcile with §5's commands
  before resuming size. The alert stays up for the life of the resumed
  process — silence it once the reconciliation is recorded.
- **Exposure**. {#exposure}
  `GrossNotionalUtilizationHigh` at 90% of `risk_limit{limit="max_gross_notional"}`
  — the alert divides by the LIVE limit gauge, so it follows a GOVERNANCE §3
  limit change automatically. All money figures are one unit:
  `qty × qty_unit × price_ticks × tick_size`, converted to USD (§12.1).
- **Loss-limit utilization**: at 75% you get paged; at 100% the risk engine
  latches the kill switch itself. Never race it — if utilization is climbing
  fast, engage the kill switch early (`docs/RUNBOOKS.md`).
  Utilization is on **realized + unrealized** daily P&L in USD (positions
  are marked at the last consolidated mid on every market update), so a
  latch can fire with no fill at all — a large open position in a moving
  market is the usual cause.
- **Risk wiring invariants** (`PaperTrading.RiskWiring`, tested by
  `PaperRiskWiringTest`): the mark the risk engine sees is stamped with the
  market-data event time of the venues at the consolidated touch, so a
  feed stall shows up as `STALE_PRICE` rejects (`risk_rejected_total` with
  that rule) after `stale_feed_timeout_ns` — not as trading against a
  frozen price; a sequence gap on a venue closes the `SEQUENCE_GAP` gate
  until the snapshot recovery — and, since v1.3.0, reopens it only when no
  venue of that instrument is stale any more; under SOR (session venue 0)
  the pre-trade request names the venue the child is actually routed to, so
  a venue kill or disconnect applies to SOR-routed flow; every fill reaches
  the engine before the
  next decision and every terminal child releases its open-order slot
  (`risk.openOrderCount()` never exceeds the simulator's live children).
  If `risk_rejected_total{rule=SELF_MATCH}` or `POSITION_LIMIT` climbs
  with few live children, a terminal report is being lost — treat as an
  OMS bug, not a limit problem.
- **Execution controls**: `BacktestEngine.Counters`
  (`participationBlocked`, `sliceIntervalBlocked`, `latencyBudgetBlocked`,
  `sorNoRoute`) are reported in the session `Result`; a rising
  `sorNoRoute` means every eligible venue was stale/halted/too slow — the
  SOR never falls back to a stale venue (conventions §11.3).

Raw metric queries when Grafana is down:

```bash
curl -s http://localhost:9090/api/v1/query --data-urlencode 'query=job:exec_fill_rate:ratio5m'
curl -s http://localhost:8080/metrics | grep -E 'risk_|alpha_|exec_'   # java monitoring API
curl -s http://localhost:8080/status  | python3 -m json.tool           # live progress
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/ready  # readiness
```

There is no rust telemetry endpoint: the `rust.prom` file target was removed in
round 3 because nothing ever wrote it (deployment/grafana/README.md).

### 4. End of session

A session ends by itself (the vector runs out): report written, final
checkpoint, `platform_session_state = 2`, `/status` `"status":"finished"`,
exit 0. (A session that was stopped instead ends as STOPPED, state 4, with
no report: resume it — §5 — before doing any of the steps below.)

1. Export the session's risk audit log and archive it with the session header
   from §2. It is written for you, continuously:
   ```bash
   ls -l <state-dir>/            # risk_audit.jsonl, risk_snapshot.json,
                                 # session_state.json (x-version 2), config_audit.jsonl,
                                 # admin_audit.jsonl, decision_traces.jsonl
   python3 -c "import json;d=json.load(open('<report>.json'));print(d['risk']);print(d['trace'])"
   #   -> audit_jsonl (path) and audit_sha256 (the archive's checksum);
   #      trace: {count, digest, jsonl} (the decision-trace stream digest)
   sha256sum <state-dir>/risk_audit.jsonl   # must equal report.risk.audit_sha256
   cd python && PYTHONPATH=src python3 -c "from iap.trace import TraceDigest; print(TraceDigest.of_jsonl('<state-dir>/decision_traces.jsonl').hexdigest())"
   #   -> must equal report.trace.digest (and /status trace_digest at session end)
   ```
   The audit file is byte-identical to `RiskEngine.auditJsonl()` and two
   identical sessions produce identical bytes (`PaperStateRecoveryTest`);
   `cd rust && cargo test -p risk` includes the cross-language replay proof.
   `decision_traces.jsonl` holds one `DecisionTrace` per pre-trade risk
   decision (`docs/DECISION_TRACE.md` §7), appended and fsynced with the
   audit at every checkpoint; it is ~3 KB per decision cycle (an equity day
   at 500 decisions/s is ≈ 130 GB/h), so archive it with its digest before
   the directory is reused, exactly like the audit. "Explain an order": load
   the line whose `stages.parent_orders[0].parent_order_id` is the risk
   order id and render it with `com.iap.trace.Explain.render(trace,
   venueNames)` (Python: `iap.trace.explain_jsonl(path, parent_order_id)`,
   COOKBOOK recipe 21); the incident flow is
   `docs/RUNBOOKS.md`.
2. Compare realized paper P&L/fill quality against the backtest for the same
   events; file the comparison in `research/tca/`. Honest reporting rule
   applies: degradation is reported, not explained away (conventions §7).
3. Gate 12 (limited capital) needs N clean paper sessions (risk owner sets N,
   ≥ 5 recommended) with drift and fill-rate inside agreed bands, plus
   written risk approval recorded in the audit log.

### 5. Restart and recovery {#restart-recovery}

**What survives a restart** (`PLATFORM_CONVENTIONS.md` §12.3): positions,
lots, realized and gross P&L, the strategy-side order-id sequence, every
loss-limit override, **every latched kill switch** and the decision-trace
digest (rebuilt from `decision_traces.jsonl` with `TraceDigest.ofJsonl`) —
restored from `<state-dir>/risk_snapshot.json` + `session_state.json`, with
`STATE_RESTORED` appended to the audit log and `risk_session_restarts_total`
incremented.

**One checkpoint, one commit point (since v1.3.0).** `session_state.json`
is the commit point of a checkpoint and records `risk_snapshot_sha256`, the
hash of the risk snapshot it belongs to. The platform writes the new
snapshot as `risk_snapshot.json.next`, replaces `session_state.json`, then
renames the snapshot into place. `--resume` accepts only the snapshot whose
hash matches: a checkpoint interrupted between the commit and the rename is
rolled forward from the `.next` file; any other mismatch is refused with
both hashes in the message. So a `risk_snapshot.json.next` left in the
state directory after a hard kill is normal — do not delete it. A state
file written before v1.3.0 has no hash and resumes unverified.

**What resume does with the restored state (since v1.3.0).** The fresh
engine's account is seeded with the restored risk positions, so the
strategy does not buy the position it already holds a second time. The open
orders recorded in the snapshot are released through
`RiskEngine.onOrderDone` (`risk_resume_open_orders_released_total`): they
were children of a simulator that no longer exists and can never report, so
keeping them would inflate every position projection for the rest of the
session.
A restart is **not** a re-arm path; clearing a latch still needs §5 of
`docs/RUNBOOKS.md`.

**What does not survive**: the market-data book, the feature warm-up and the
execution simulator's in-flight children. They rebuild from the stream after
the checkpoint cursor, so expect a warm-up window with fewer signals
(`SignalRateCollapse` may flicker) and no resting orders immediately after a
restart. Reconcile before resuming size:

```bash
# 1. What did the checkpoint carry?
python3 -m json.tool <state-dir>/session_state.json      # cursor, P&L, restarts
python3 -m json.tool <state-dir>/risk_snapshot.json | head -40

# 2. Resume INTO it (the container does this when --resume is in the command)
java -cp /app/classes com.iap.platform.PaperTrading \
  --configs "$IAP_CONFIG_DIR" --state-dir "$IAP_STATE_DIR" --resume ...

# 3. Verify the first decisions are against the restored state
grep -m3 STATE_RESTORED <state-dir>/risk_audit.jsonl
curl -s localhost:8080/status | python3 -m json.tool     # restarts >= 1
```

Failure is closed, not silent: a missing checkpoint, malformed JSON, an
unknown snapshot version, a risk snapshot whose sha256 is not the one
`session_state.json` commits to, a checkpoint for a different
instrument/alpha, a
cursor at/past the end of the stream, a `risk_audit.jsonl` whose line
count disagrees with `session_state.json`'s `audit_lines`, or a
`decision_traces.jsonl` whose line count disagrees with `trace_lines`, all
abort the process with the offending file named. `SessionRestartsClimbing` (> 2
resumes in 15 m) is a crash loop — read the logs before restarting again.

**"risk_audit.jsonl has N lines but session_state.json records
audit_lines=M".** This is the torn-append case, and it is expected after a
hard kill: the audit is appended and flushed *before* the state file is
atomically renamed, so a process killed between the two leaves N = M + k.
Do NOT hand-edit either file to make them agree — the audit is the evidence.

```bash
tail -n $((N - M)) <state-dir>/risk_audit.jsonl   # the k decisions past the checkpoint
```

Those decisions happened and are recorded; the checkpoint simply predates
them. Archive the whole state directory (audit and traces included, per §4
step 1),
then resume from the archived copy with the audit truncated to M lines *by
the recovery operator, with the discrepancy noted in the incident record* —
or, preferably, restart the session from the last clean checkpoint and let
the stream replay those events. If N &lt; M the audit was truncated or
belongs to another session: do not resume at all, escalate per
`docs/RUNBOOKS.md`.

#### 5.1 The risk audit chain across restarts (v1.14)

Each checkpoint records the risk audit chain head (`riskAuditSeq`,
`riskAuditHeadSha256` in `session_state.json`), and `--resume` restores the
engine with `RiskEngine.restoreChained`, so `risk_audit.jsonl` stays one
chain across restarts. A checkpoint written before v1.14 has no head and
the resumed engine starts a new chain. To check a state directory, verify
the audit file and compare the head with `session_state.json`
(docs/RUNBOOKS.md §6).

### 6. Stopping

```bash
docker compose -f deployment/docker/docker-compose.yml down          # keep volumes
docker compose -f deployment/docker/docker-compose.yml down -v      # discard generated data (reproducible)
```

Stopping a running session is a clean operation since v1.3.0. SIGTERM runs
the JVM shutdown hook, which only raises a stop flag and waits up to 10 s;
the trading thread — the only writer of trading state — sees the flag at
its next event boundary (or within 20 ms while a realtime session is
pacing), writes a checkpoint and ends the session as STOPPED (exit 0). If
the trading thread does not get there within the wait, the last periodic
checkpoint stands and at most one checkpoint interval (1024 events) is
replayed on resume. The hook's wait equals Docker's default stop timeout
(10 s; the compose file sets no `stop_grace_period`), so on a host where
the loop is slow to reach an event boundary the container can be killed
before the final checkpoint — the same bounded loss.

Because STOPPED exits 0, compose (`restart: on-failure`) does not restart
the container. To continue the session, start it again with `--resume`
added to the command — the image's default entrypoint does not pass it
(`archive/java/Dockerfile.java (at git tag archive/java-v1.14.0)`, retired).

---

## RUNBOOK — alert delivery, latency SLOs and trading-state backups (v1.12.0)

> **Shells.** These procedures run on the Linux side (the cluster, the containers, WSL or
> Git Bash): the `bash` blocks are bash. From the owner's Windows PowerShell, translate
> as follows: `curl` is `curl.exe` (in PowerShell `curl` is an alias of `Invoke-WebRequest`);
> `export NAME=value` is `$env:NAME = "value"`; `PYTHONPATH=python/src python ...` is
> `$env:PYTHONPATH = "python/src"; python ...`; `python3` is `python`; `grep X file` is
> `Select-String -Path file -Pattern X`; `| head -N` is `| Select-Object -First N`;
> `tail -N file` is `Get-Content file -Tail N`; `cat file` is `Get-Content file`;
> `ls -l dir` is `Get-ChildItem dir`; `$(...)` command substitution is `$(...)` too but
> with PowerShell inside (for example `$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())`
> for `$(date +%s)`); `a && b` is `a; if ($?) { b }`; a trailing `\` continuation is a
> backtick. `kubectl` and `docker` take the same arguments in both shells.

Owner: platform on-call. Scope: `deployment/alertmanager/`,
`deployment/prometheus/slo.yml`, `deployment/k8s/cronjob-state-backup.yaml`.
Contracts: PLATFORM_CONVENTIONS.md §12.7 ("Alerting, SLOs and state backup").

### 1. How alerts are routed

| `severity` label | receiver | meaning | repeat |
|---|---|---|---|
| `page` | `page` (secret `page_url`) | capital at risk or trading correctness; wake someone up | 1h |
| `critical`, `warning` | `ticket` (secret `ticket_url`) | act during the session / investigate the same day | 12h |
| `none` (`Watchdog`) | `watchdog` (no-op) | always-firing heartbeat | 1m |
| anything else | `ticket` | catch-all: an unlabelled alert is never dropped | 4h |

Grouping: `alertname` + `service`. Inhibition (one page per incident, always
scoped to the same `service`):

- `KillSwitchEngaged` mutes `FillRateDrop`, `SignalRateCollapse`,
  `PreTradeRejectRatioHigh`, `LossLimitUtilizationHigh` and every latency SLO
  alert: they are consequences of the halt.
- `StaleFeed`, `FeedWallClockStall`, `BookStale`, `SequenceGapDetected` mute
  `SignalRateCollapse` and `FillRateDrop`.
- an SLO `page` mutes the `warning` of the same `slo`.

Every alert carries `severity`, `runbook` and `runbook_url`;
`tests/harness/check_deployment.py` (`alert_rule_metadata`) fails the build
otherwise.

### 2. Setting the receiver URLs (operator)

Receiver URLs embed tokens: they are **never committed**.
`alertmanager.yml` only names files (`url_file`), and
`check_deployment.py` (`alerting_secret_free`) fails on an inline URL, a
token-looking string, a non-local `.url` file or a committed `Secret` with
data under `deployment/`.

**Kubernetes** — before the first `kubectl apply` (the Alertmanager pod stays
in `ContainerCreating` until the Secret exists):

```bash
umask 077
printf '%s' 'https://pager.example/hook/<token>'  > /secure/page_url
printf '%s' 'https://tickets.example/hook/<token>' > /secure/ticket_url
kubectl -n intraday-alpha create secret generic iap-alertmanager-webhook \
  --from-file=page_url=/secure/page_url --from-file=ticket_url=/secure/ticket_url
shred -u /secure/page_url /secure/ticket_url
```

Rotation: re-create the Secret with `--dry-run=client -o yaml | kubectl apply -f -`.
Alertmanager re-reads `url_file` on every send, so no restart is needed once
the kubelet has refreshed the mounted Secret (up to about a minute).

Migrating from v1.11 (single key `url`): add the two new keys, e.g.
`kubectl -n intraday-alpha get secret iap-alertmanager-webhook -o jsonpath='{.data.url}' | base64 -d > /secure/page_url`
and create `ticket_url` as above. The pod will not start until both keys exist.

Network: `allow-alertmanager-webhook-egress` (networkpolicy.yaml) allows only
TCP 443 to non-private addresses. An internal receiver needs that ipBlock
changed to its CIDR.

**docker compose** — with nothing set, both URLs point at the in-compose
`alert-sink` echo service (`deployment/alertmanager/local-sink.url`, which
holds no secret), so `docker compose up` works out of the box:

```bash
docker logs -f iap-alert-sink     # Watchdog arrives within ~1 minute
```

To deliver for real, keep the files **outside the repo**:

```bash
export ALERT_PAGE_URL_FILE=$HOME/.iap/page_url
export ALERT_TICKET_URL_FILE=$HOME/.iap/ticket_url
docker compose -f deployment/docker/docker-compose.yml up -d alertmanager
```

```powershell
$env:ALERT_PAGE_URL_FILE = "$HOME\.iap\page_url"
$env:ALERT_TICKET_URL_FILE = "$HOME\.iap\ticket_url"
docker compose -f deployment/docker/docker-compose.yml up -d alertmanager
```

`ALERT_WEBHOOK_URL_FILE` from v1.11 is still honoured as the page URL.
`*.url` files under `deployment/alertmanager/` (other than `local-sink.url`)
are git-ignored as a backstop.

**Verify delivery**: `Watchdog` is always firing. If the receiver sees no
Watchdog within 2 minutes, the pipeline is broken: check
`kubectl -n intraday-alpha logs deploy/alertmanager` for `notify` errors
(DNS, TLS, 4xx from the receiver, NetworkPolicy drops).

<a id="latency-slo"></a>
### 3. Latency SLO alerts

| SLO | objective (30 days) | source histogram |
|---|---|---|
| `order-path-latency` | 99% of orders decision-to-wire < 1,048,575 ns (about 1.05 ms) | `order_path_latency_ns` |

(v1.17 F9: the `event-path-latency` SLO on `book_update_latency_ns` was
retired with the Java image; the Python exporter builds `order_path_latency_ns`
from the MVP run's traces, per parent order `arrival_ts - decision_ts`.)

The threshold is a log2 bucket bound (`le="1048575"`): the histograms only
have 2^i - 1 ns bounds, so the SLO is exact at the bound, with no
interpolation. Error ratios are recorded per window as
`job:order_path_latency_slo_errors:ratio_rate{5m,30m,1h,2h,6h,1d,3d}`.

| alert | severity | condition (budget 1%) | budget spent |
|---|---|---|---|
| `*LatencySLOFastBurn` | page | 14.4x over 1h and 5m, **or** 6x over 6h and 30m | 2% in 1h / 5% in 6h |
| `*LatencySLOSlowBurn` | warning | 3x over 1d and 2h, **or** 1x over 3d and 6h | 10% in 1d / 10% in 3d |

When it fires:

1. Is it real load or one bad stretch? Compare `job:order_path_latency_ns:p99`
   and `job:book_update_latency_ns:p99` (Grafana "Market Data & Latency") with
   the cold reference in `benchmarks/RESULTS.md`.
2. GC: `job:jvm_gc_pause_ns:p99`, and `GcPauseHigh`, which usually fires with it.
3. Host contention: CPU throttling on the pod (`limits.cpu`), a noisy
   neighbour, or a node that is too small.
4. A burn with **no traffic** cannot fire (the ratio is NaN); a finished
   paper session is not an SLO breach.
5. Do **not** raise the threshold to silence it: the objective is a contract.
   Change it with a reviewed PR to `slo.yml` and its promtool tests.

Limits, stated plainly: the SLO measures in-process latency as the JVM sees
it, not venue round-trip. A JVM restart resets the cumulative histograms;
`rate()` handles the reset, but samples in flight at the crash are lost.

### 4. Trading-state HA, backups and restore

Since v1.14 a backup of a state directory includes a hash-chained
`risk_audit.jsonl` whose head is recorded in `session_state.json`. Back up
both together; restoring one without the other leaves a chain that does
not match its checkpoint (docs/RUNBOOKS.md §5).

What this deployment provides is **restart-and-resume, plus backups**. It is
not active-active and it has no hot standby:

- `platform-metrics` is a singleton (replicas 1, `Recreate`, `ReadWriteOnce`
  claim `iap-platform-state`; v1.17 F9, was `java-platform` / `iap-java-state`).
  The RWO attach plus `Recreate` is the fence. No Lease-based leader election
  exists. A second trading loop would duplicate orders once a venue adapter is
  wired.
- On pod or node loss Kubernetes reschedules the single pod, which re-attaches
  the claim with the session marker and the MVP artefacts intact (§12.3). The
  MVP loop is deterministic and finite: the `session` container simply reruns
  it (there is no `--resume`). Expect a gap of minutes on node loss (volume
  detach timeout), not seconds.
- There is no PodDisruptionBudget, on purpose: with one replica it would block
  every node drain.
- The `state-backup` CronJob archives `/data/state` and `/data/mvp` every 30
  minutes to the `iap-platform-state-backup` claim and keeps 96 archives (2
  days). It mounts the state claim **read-only** and is pinned by a required
  pod affinity to the node running `platform-metrics` (RWO is per node). When the pod is not
  running, the job stays Pending until `activeDeadlineSeconds` reaps it, and
  no backup is taken. `check_deployment.py` (`k8s_state_backup`) pins this
  shape.

**Check backups**

```bash
kubectl -n intraday-alpha get jobs -l app.kubernetes.io/name=state-backup
kubectl -n intraday-alpha logs job/<latest state-backup job>   # "backup ok: state-<ts>.tgz"
```

**Restore** (the state volume is lost or corrupt):

1. Engage the kill switch if the platform is up, then scale to zero:
   `kubectl -n intraday-alpha scale deploy/platform-metrics --replicas=0`.
2. Recreate `iap-platform-state` if it is gone (`kubectl apply -f deployment/k8s/pvc.yaml`).
3. Run a one-off pod that mounts `iap-platform-state-backup` read-only and
   `iap-platform-state` read-write, and extract the newest archive:
   `tar -xzf /backup/state-<ts>.tgz -C /data` (this restores `/data/state` and `/data/mvp`).
4. Scale back to 1; the session container reruns the deterministic MVP loop.
5. If resume **refuses** (audit or trace line count, or the risk snapshot's
   sha256, does not match the checkpoint), the archive was taken while a
   checkpoint was being written. The refusal is the
   fail-closed guard working, not a bug. Try the previous archive; archives
   taken while the session was quiescent always match. Never edit the audit
   to make the counts agree. If no archive resumes, start a fresh session with
   the kill switch re-engaged and file an incident
   (docs/RUNBOOKS.md).

Off-cluster copies (object storage) are the operator's responsibility and are
not in this repo. In-cluster backups do not survive loss of the cluster's
storage.

---

## RUNBOOK — Incident: Kill Switch

> **Shells.** These procedures run on the Linux side (the cluster, the containers, WSL or
> Git Bash): the `bash` blocks are bash. From the owner's Windows PowerShell, translate
> as follows: `curl` is `curl.exe` (in PowerShell `curl` is an alias of `Invoke-WebRequest`);
> `export NAME=value` is `$env:NAME = "value"`; `PYTHONPATH=python/src python ...` is
> `$env:PYTHONPATH = "python/src"; python ...`; `python3` is `python`; `grep X file` is
> `Select-String -Path file -Pattern X`; `| head -N` is `| Select-Object -First N`;
> `tail -N file` is `Get-Content file -Tail N`; `cat file` is `Get-Content file`;
> `ls -l dir` is `Get-ChildItem dir`; `$(...)` command substitution is `$(...)` too but
> with PowerShell inside (for example `$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())`
> for `$(date +%s)`); `a && b` is `a; if ($?) { b }`; a trailing `\` continuation is a
> backtick. `kubectl` and `docker` take the same arguments in both shells.

**Scope:** engaging, verifying and clearing kill switches (global / strategy /
instrument / venue — spec §16), and the incident procedure around them.
**Owner:** risk. **Related alerts:** `KillSwitchEngaged`,
`LossLimitUtilizationHigh`, `GrossNotionalUtilizationHigh`,
`SequenceGapDetected`, `PlatformSessionFailed`, `TargetDown`,
and the `Watchdog` heartbeat (`StaleFeed`, `KillPendingNotRecorded`,
`AdminAuthRateLimited` and `AdminAuditSuppressed` were retired with the Java
image, v1.17 F9). Alerts reach a human only through Alertmanager
(`deployment/alertmanager/alertmanager.yml`), and **only once the receiver URLs
have been supplied** (docs/RUNBOOKS.md §2) — by default compose delivers
to a local echo sink, not a person. Check that before relying on a page.

**First principle: the risk engine is fail-closed.** When in doubt, engage.
An unnecessary halt costs basis points; a missing halt costs the loss limit.

### 0. The manual halt path since v1.17 (F9): the config path — there is no admin API

The Java platform's authenticated `POST /admin/{kill,clear,override,roll}`
API was retired with the Java image (DECISION_N7 closed 2026-10-15). The
deployed Python process (`python -m iap.metrics serve`, compose
`platform-metrics`, k8s `deployment/k8s/platform-metrics.yaml`) serves
`/metrics`, `/health`, `/ready` and `/status` only and has **no write
surface**. The manual halt is therefore the **config path** of §2 step 1:
set `global.kill_switch_engaged` in `configs/risk/risk.json`, regenerate the
ConfigMap (k8s) or rely on the read-only bind mount (compose), and restart
the session container — the MVP loop reads the master switch at start and the
risk engine then rejects every order pre-trade (fail-closed). Programmatic
engagement during a running Python session is `iap.risk` API
(`engage_kill`, §1), exercised by the dry kill-switch drill
`python -m iap.paper.drill` of the forward paper trade. Everything in this
runbook that names `/admin/*`, an admin token, `admin_audit.jsonl`, `202`
"kill latched", `KillPendingNotRecorded` or the admin-auth alerts describes
the retired Java path and is kept for the record only.

### 1. How the kill switch works (rust/risk engine, `com.iap.risk` port)

- Scopes: Global, Strategy, Instrument, Venue. Engaged state **latches** —
  it never clears itself, even if PnL recovers.
- Automatic latching (`PLATFORM_CONVENTIONS.md` §11.1): daily P&L =
  **realized + unrealized** (mark-to-market at the last consolidated mid,
  converted to the reporting currency USD) ≤ −`per_strategy.max_daily_loss`
  (50k) latches that strategy; global daily P&L ≤ −`global.max_daily_loss`
  (250k) latches globally (`configs/risk/risk.json`). The check runs after
  every fill AND after every market update of a held instrument — a
  position that is marked through the limit latches **with no fill at
  all**. Every latch and every rejected order is a `RiskEvent` in the
  JSONL audit log (`KILL_STRATEGY` / `KILL_GLOBAL`, decision KILL).
- A latch is not the only guard: while daily P&L is still at/below the
  limit, `DAILY_LOSS` / `STRATEGY_LOSS` (checks 21/22) reject every order
  in scope on their own — clearing the switch alone does not resume trading
  (§5).
- While engaged, every order in scope is rejected pre-trade
  (`risk_rejected_total` climbs); working orders must be cancelled by the
  execution layer (verify in §3).
- Config-level master switch: `configs/risk/risk.json`
  `global.kill_switch_engaged: true` boots the engine already halted — the
  deploy-time hard stop.
- Programmatic API (paper/live wiring, identical in `rust/risk` and
  `com.iap.risk`): `engage_kill(scope, scope_id, ts, reason)` /
  `clear_kill(...)` — both emit audit events with the reason string
  verbatim; `override_loss_limit(scope, scope_id, new_limit, ts, approver)`
  (audited `LOSS_LIMIT_OVERRIDE`, old → new limit, approver);
  `roll_session(ts, reason)` (audited `SESSION_ROLLED`); `snapshot()` /
  `restore(...)` / `bootstrap_positions(...)` for restarts.
- **Operator API on the running Java platform** (`PLATFORM_CONVENTIONS.md`
  §12.5): `POST /admin/{kill,clear,override,roll}` maps 1:1 onto those calls.
  The HTTP handler never touches risk state itself — it enqueues the command
  and the **trading thread** applies it at the next event boundary, so the
  resulting `RiskEvent` carries the current **event time** and sorts into the
  audit log exactly where a programmatic call would. Every accepted
  request appends a line to `<state-dir>/admin_audit.jsonl` with the
  `operator` id, the `remote` address and the token's **sha256** (never the
  token) and increments `admin_requests_total{action=...}`; rejected
  requests are audited too, up to 100 lines per 60 s window, with one
  `audit_summary` line per window counting what was suppressed.
- **A kill latches before it is recorded (since v1.3.0).** The moment the
  platform accepts a `kill` it raises a pending-kill latch and the order
  path sends nothing new, whether or not the trading thread has reached an
  event boundary. The trading thread drains admin commands at every event
  boundary, before every pre-trade check, and — in realtime mode — between
  pacing slices while the feed is quiet. Once the risk engine holds the
  kill, the platform requests a cancel for every working (in-flight or
  resting) child order through the simulator's cancel path.
- **Venue kills and SOR (since v1.3.0).** While any venue kill is engaged,
  an order that names venue 0 ("route via SOR") is rejected with
  `KILL_VENUE` (`venue 0 (SOR) order rejected: venue <id> kill switch
  engaged`), and the paper platform's pre-trade request names the venue the
  child is actually routed to. A venue kill therefore halts SOR-routed flow
  as well as flow pinned to that venue; it used not to.
- **Restart semantics (Java platform).** The latch is CHECKPOINTED: it is in
  `<state-dir>/risk_snapshot.json` and a `--resume` restart comes back
  latched, with positions and realized P&L intact
  (`PLATFORM_CONVENTIONS.md` §12.3, tested by
  `PaperStateRecoveryTest.scenarioRestartDoesNotClearALatchedKillSwitch`).
  Restarting is NOT a way to clear a kill switch, and an OOM-kill or node
  drain does not resume trading a killed strategy.
- **Restart semantics (engine).** A restarted engine built with `require_bootstrap`
  rejects everything with `NOT_BOOTSTRAPPED` until it is either `restore`d
  from the last `snapshot()` (positions, lots, open orders, kill switches,
  overrides, throttles — `STATE_RESTORED`; decisions and audit lines
  continue bit-identically) or `bootstrap_positions` is fed the session's
  fills (`BOOTSTRAP_COMPLETE`). A restart does NOT clear a latch that was
  snapshotted — it is not a re-arm path.

### 2. ENGAGE — immediate actions (minutes 0–5)

1. **Engage** at the narrowest sufficient scope; global if unsure.
   - Automatic latch already engaged? Skip to verification.

   **(a) API path — RETIRED (v1.17 F9).** The Java `POST /admin/kill`
   call no longer exists; there is no Python admin API (§0). Use (b).

   **(b) Config path — THE manual halt (minutes, needs a restart).**
   ```bash
   # configs/risk/risk.json: "global": {"kill_switch_engaged": true, ...}
   python3 deployment/k8s/generate_configmaps.py
   kubectl apply -f deployment/k8s/configmap-configs.yaml
   kubectl -n intraday-alpha rollout restart deployment/platform-metrics
   # compose: edit configs/risk/risk.json, then
   docker compose -f deployment/docker/docker-compose.yml restart mvp-session platform-metrics
   ```
   This path is real because both containers read `$IAP_CONFIG_DIR` — the
   mounted ConfigMap (k8s) or the read-only bind mount of `configs/` (compose),
   not a copy baked into the image (`PLATFORM_CONVENTIONS.md` §12.2). Confirm
   the exporter sees the live limits and the restarted session's state:
   ```bash
   curl -s http://platform-metrics:8080/metrics | grep -E '^(risk_limit|risk_kill_switch_engaged|platform_session_state)'
   curl -s http://platform-metrics:8080/status | python3 -m json.tool  # run_dir, session_state
   ```
   Incident edits to `configs/risk/risk.json` are the ONE allowed
   review-after-the-fact change: commit within the hour with an
   `INCIDENT:` message; the audit-log entry still gets written
   (GOVERNANCE.md §3).
2. **Verify it took** (§3) before doing anything else.
3. **Page** risk owner + platform owner; open the incident channel; start a
   timeline (UTC timestamps).

### 3. Verify the halt

**The one check that decides it**: `risk_kill_switch_engaged == 1`.

```bash
curl -s http://platform-metrics:8080/metrics | grep '^risk_kill_switch_engaged'
# risk_kill_switch_engaged 1        <- HALTED (read from the run's report.json risk.kill_switch_engaged)
grep -c '"decision":"KILL"\|KILL_SWITCH' /data/mvp/golden/risk_audit.jsonl   # the audit trail
```

Then confirm it is having the intended effect:

- `risk_allowed_total` **stops climbing** and `risk_rejected_total` climbs;
  `job:risk_rejected:ratio5m` → 1.
  ```bash
  curl -s http://prometheus:9090/api/v1/query \
    --data-urlencode 'query=increase(risk_allowed_total[2m])'   # must be 0
  ```
- Dashboard **Trading & Risk**: kill-switch stat shows **ENGAGED — HALTED**;
  order flow drops to zero submits/fills.
- Audit log: the latch event with the right scope and the approval reference —
  ```bash
  grep -E 'KILL_SWITCH_ENGAGED|KILL_GLOBAL|KILL_STRATEGY' \
       <state-dir>/risk_audit.jsonl | tail -5 | python3 -m json.tool
  tail -3 <state-dir>/admin_audit.jsonl        # who called, with what status
  ```
- After a `202`: the gauge turns to 1 when the trading thread applies the
  kill. Until then the halt is the pending latch — orders the strategy tries
  to send are counted in `exec_orders_blocked_kill_pending_total` rather
  than rejected by the risk engine. If the session ends first, the audit
  trail is closed with a `503` line (`session ended before the latched kill
  reached the risk engine`).
- **A kill that stays pending.** {#kill-pending}
  `KillPendingNotRecorded` (critical) fires when
  `exec_orders_blocked_kill_pending_total` has kept climbing for 3 m: orders
  are being withheld, so nothing new is sent, but the kill is still only the
  admin service's pending latch. It is not in the risk engine, so it is not
  in `risk_snapshot.json` and **a restart would lose it**. Do not restart.
  Check `/status` (`events_processed` must be advancing — a wedged trading
  thread cannot drain the command; `/health` turns 503 after 30 s) and the
  tail of `admin_audit.jsonl` for the `202` line without its
  `applied after the request returned 202` follow-up. If the thread is
  wedged, set the config master switch (§2 step 1) so the next start comes
  up halted, then stop the process.
- A halted platform is deliberately **still healthy and still ready**
  (`/health` and `/ready` return 200 with `"trading":"halted"`) — do not read
  a green probe as "the halt did not take"; read the gauge.
- Confirm no working orders remain at venues. In the paper platform the
  kill requests the cancels itself and the `200` message (or the second
  audit line after a `202`) states how many working children were asked to
  cancel; the cancels travel the simulator's latency path, so the terminal
  reports arrive on the following events. If the message says the resting
  children were **not** cancelled, the halt still stops new orders, and the
  open children are an explicit item for the risk owner.

### 4. During the halt

- Diagnose with the halt in place — never "clear to see if it happens again".
- Typical triggers and their runbooks: loss limit (this doc), stale
  feed/sequence gaps (`docs/RUNBOOKS.md`), signal/fill anomalies
  (`docs/RUNBOOKS.md`).
- Positions: the halt stops NEW orders. Risk owner decides explicitly whether
  existing exposure is flattened (manual, risk-approved orders outside the
  killed strategy scope) or carried.

### 5. CLEAR — only with written risk approval

Preconditions (all required):

1. Root cause identified and fixed or positively ruled out.
2. Risk owner's written approval (name + time) in the incident channel.
3. Post-incident limits agreed (often reduced size for the next session).

Procedure:

Re-arm precedence is pinned in `PLATFORM_CONVENTIONS.md` §11.1 and tested
(`risk_clear_kill_after_loss_latch_resumes_with_override`,
`scenario_session_roll_rebases_daily_pnl_and_keeps_latches` in Rust and
Java) — the steps below are the ONLY order that works:

On the running Java platform each step below is one authenticated call
(`PLATFORM_CONVENTIONS.md` §12.5) — same ORDER, same semantics:

```bash
TOKEN="$IAP_ADMIN_TOKEN"
A=http://java-platform:8080/admin
# 2a: override FIRST, then clear
curl -sS -X POST $A/override -H "Authorization: Bearer $TOKEN" \
  -d 'scope=global' -d 'limit=400000' --data-urlencode 'reason=approval INC-... <risk owner>'
curl -sS -X POST $A/clear    -H "Authorization: Bearer $TOKEN" \
  -d 'scope=global'          --data-urlencode 'reason=approval INC-...'
# 2b: new session instead
curl -sS -X POST $A/roll  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'reason=session roll 2026-09-07'
curl -sS -X POST $A/clear -H "Authorization: Bearer $TOKEN" \
  -d 'scope=global' --data-urlencode 'reason=approval INC-...'
```

```bash
# 1. Revert configs/risk/risk.json master switch if it was set (reviewed commit).
# 2. Decide what the approval covers, then act in this order:
#    a) Loss-limit latch, SAME session, trading resumes at a larger limit:
#         override_loss_limit(scope, id, new_limit, ts, "approver <name> <ref>")
#         clear_kill(scope, id, ts, "approval <ref>")
#       -- override FIRST: clear_kill alone leaves DAILY_LOSS/STRATEGY_LOSS
#          rejecting (P&L still below the limit) and the next fill or mark
#          re-latches the switch (audited again). The override never clears
#          a latch by itself; it is audited with old/new limit + approver.
#    b) Loss-limit latch, NEW session (day roll):
#         roll_session(ts, "session roll <date>")   # realized P&L -> 0,
#                                                   # lots re-based to mark,
#                                                   # overrides cleared,
#                                                   # kills KEPT
#         clear_kill(scope, id, ts, "approval <ref>")
#       -- roll_session never clears a switch; clear_kill after the roll
#          resumes trading at the configured limits.
#    c) Manual / venue / instrument / stale-feed engagement (no loss breach):
#         clear_kill(scope, id, ts, "approval <ref>")
# 3. Restart instead of API? It does NOT clear anything: the Java platform
#    checkpoints the latch and --resume restores it (conventions 12.3), and a
#    restart without a snapshot is NOT_BOOTSTRAPPED and needs
#    bootstrap_positions. Neither path clears a latch on its own. A SIGTERM
#    during the halt ends the session as STOPPED (platform_session_state 4)
#    with a resumable checkpoint that still carries the latch.
# 4. Redeploy config as in §2 step 1.
```

Verify: kill-switch stat back to **ARMED / TRADING**; first orders flow and
`risk_allowed_total` climbs; audit log shows `LOSS_LIMIT_OVERRIDE` and/or
`SESSION_ROLLED` BEFORE the CLEAR event, each with the approval reference;
no `DAILY_LOSS` / `STRATEGY_LOSS` rejects after the clear (if there are,
the override was too small or missing — go back to step 2a).

### 6. Postmortem (within 48h)

Blameless write-up in `docs/RUNBOOKS.md` sibling incident record: timeline,
trigger, detection latency (alert → engage), what the latched audit log
shows, config/limit changes made, and at least one prevention item. The
session's audit JSONL replays byte-identically — attach its hash so the
postmortem is reproducible (docs/GOVERNANCE.md).

Before attaching it, check the audit log is intact. Since v1.14 every line
carries `seq` and `prev_sha256`, and the Java paper session records the
chain head in `session_state.json`:

```bash
PYTHONPATH=python/src python -c "import sys; from iap.risk.events import verify_audit_chain; print(verify_audit_chain(open(sys.argv[1], encoding='utf-8').read()))" <state-dir>/risk_audit.jsonl
# (last_seq, head_sha256)   -- compare the head with session_state.json
```

```powershell
$env:PYTHONPATH = "python/src"
python -c "import sys; from iap.risk.events import verify_audit_chain; print(verify_audit_chain(open(sys.argv[1], encoding='utf-8').read()))" <state-dir>\risk_audit.jsonl
```

A `ValueError` names the first line whose recorded hash does not match the
line before it. Treat that as a separate incident: the log was edited,
truncated or rewritten (a CRLF conversion is enough). A matching head
shows the log is the one the engine wrote; it does not show the engine
was right.

---

## RUNBOOK — Incident replay: capture → replay → reproduce → debug → fix → regression test

**Trigger**: a decision needs explaining after the fact — a fill nobody
expected, an order rejected for a rule nobody predicted, a P&L figure that
disagrees with the dashboard, a postmortem (`docs/RUNBOOKS.md`
§6) — and the answer must be reproducible, not recollected.

> **Shells.** Blocks tagged `bash` run in bash (Linux, the containers, Git Bash on Windows).
> The owner's Windows machine uses PowerShell: each `PYTHONPATH=src python3 ...` has a
> `powershell` variant below it (`$env:PYTHONPATH = "src"; python ...`; `python3` is `python`;
> set the variable once per window; `a && b` is `a; if ($?) { b }`). `bash tests/harness/...`
> and `bash build.sh` need Git Bash or WSL on PATH; `kubectl`, `docker` and `curl` lines
> are for the cluster/containers and are the same in PowerShell except for the text filters,
> which are given in PowerShell where the owner would run them locally.

**What makes it possible** (PLATFORM_CONVENTIONS.md §3, §13; docs/MVP.md §5):
everything downstream of the captured market-data stream is a pure function
of that stream plus the configuration in force. Every run keeps both, every
decision is a `DecisionTrace` line whose stream digest a replay must
reproduce, and every risk decision is an audit line. The flow is the same
for the Python MVP and the Java paper vertical; the commands differ, both
are below.

### 1. Capture — what a run already keeps

**Python MVP** (`python -m iap.mvp run`, `data/mvp/<run_id>/`, git-ignored):

| file | what |
|---|---|
| `events.jsonl`, `events.iap1`, `feed.json` | the captured stream (canonical JSONL + its IAP1 twin; `data_version` = sha256 of the IAP1 stream), the generator seed and QC totals |
| `config.json` | the `mvp.json` document as run plus the sha256 of every reference document in force (`config_version`) |
| `traces.jsonl` | one canonical-JSON `DecisionTrace` per decision, in decision order (the durable trace record) |
| `iap.sqlite` | the store: every trace decomposed into the stage tables + the MVP reference data (the index, rebuildable) |
| `risk_audit.jsonl` | every `RiskEvent` (pre-trade decisions, gaps, recoveries) |
| `report.json` / `report.md` | counts, P&L identity, risk by rule, routing, controls, TCA aggregates, realized IC, `trace.digest` |
| `paper_evidence.json` | the `PaperEvidence` document the lifecycle's PAPER gates read |

```bash
cd python && PYTHONPATH=src python3 -m iap.mvp run                 # -> ../data/mvp/<run_id>/
# mvp run 58a10f2194a3c81c: events=15805 decisions=800 parents=231 children=347 fills=163 pnl=-82.837333 USD digest=a7f1ee6066f6eba9... out=.../data/mvp/58a10f2194a3c81c
```

```powershell
Set-Location python; $env:PYTHONPATH = "src"; python -m iap.mvp run     # -> ..\data\mvp\<run_id>\
```

**Java paper vertical** (`archive/java/paper.sh (at git tag archive/java-v1.14.0)` (archived, frozen at v1.14.0), state directory `--state-dir` /
`$IAP_STATE_DIR`, `/data/state` in the deployments; `docs/RUNBOOKS.md`
§4–§5, PLATFORM_CONVENTIONS.md §12.3):

| file | what |
|---|---|
| `decision_traces.jsonl` | one `DecisionTrace` per pre-trade risk decision, appended and fsynced at every checkpoint (1,024 events), at session end and at a requested stop (since v1.3.0 the shutdown hook only raises a stop flag; the trading thread writes the checkpoint and the session ends `STOPPED`); never rotated |
| `risk_audit.jsonl` | every `RiskEvent`, same cadence |
| `risk_snapshot.json`, `session_state.json` (x-version 2: `trace_lines`, `audit_lines`, the event cursor and, since v1.3.0, `risk_snapshot_sha256`) | the restart state. `session_state.json` is the commit point of a checkpoint and names its risk snapshot by hash; a `risk_snapshot.json.next` beside them is a checkpoint that was interrupted after the commit and is rolled forward on `--resume` — archive it with the rest |
| `config_audit.jsonl`, `admin_audit.jsonl` | which configuration ran; who called the kill-switch API (since v1.3.0 each line carries `operator`, `remote` and the token's sha256; a kill answered `202` has a second line when the risk engine recorded it) |
| the session report (x-version 3) | `trace: {count, digest, jsonl}`, `risk.audit_sha256`, `config_sha256`; `/status` carries `trace_count`, `trace_digest` live |

The input stream of a paper session is the file the session was started on
(the golden vector or a generated `data/normalized/*.normalized.jsonl`,
identified by `data_version` in every trace header). Archive the whole
state directory with the report; `sha256sum decision_traces.jsonl
risk_audit.jsonl` must match the report before the directory is reused.

### 2. Replay — reproduce the digest from the capture, not from the generator

```bash
cd python
PYTHONPATH=src python3 -m iap.mvp replay --run ../data/mvp/<run_id>          # -> <run_id>/replay/
# replay OK: ../data/mvp/<run_id> reproduces its trace digest and report
```

```powershell
Set-Location python; $env:PYTHONPATH = "src"
python -m iap.mvp replay --run ../data/mvp/<run_id>
```

`replay` re-runs the loop over `events.jsonl`, compares the trace digest,
`report.json` and the stream sha256 with the captured ones and exits 1 with
a line-per-difference diff otherwise. A reference document that changed
since the run (`configs/risk/risk.json`, `configs/execution/execution.json`,
`configs/strategies/alpha_params.json`, `research/alpha_registry.json`,
`configs/mvp/*`) is reported as a `config_version` mismatch **before**
anything runs: the incident must be replayed under the configuration that
produced it (`git checkout` the commit the report names, or the archived
`configs/` tree). A run captured by v1.4.0 is such a case under v1.5.0:
`execution.json`, `alpha_params.json` and the registry changed, so its `config_version`
(`f293e7e7…`) no longer matches the v1.5.0 tree's (`439bbad5…`) and it
replays only from the v1.4.0 tag. The same holds across v1.14.0, whose
`config_version` is `62d78a3b…`. Outside the checkout — the Python image bakes `configs/`
and `research/alpha_registry.json` under `/app` — add `--repo-root /app`.

For a Java session the equivalent is a second run over the same input and
configuration: `bash archive/java/paper.sh (at git tag archive/java-v1.14.0)` on the same vector produces the same
`decision_traces.jsonl` bytes and the same `trace.digest`
(`PaperTraceTest`: emission is a pure function of the event stream).
Re-derive the digest of an archived file with `TraceDigest.ofJsonl` (Java)
or `python -c 'from iap.trace import TraceDigest; print(TraceDigest.of_jsonl("decision_traces.jsonl").hexdigest())'`
— the Python reference reads Java's lines unchanged (same canonical JSON).

A replay that does **not** reproduce is itself the finding: either the
capture is not the input the run saw (check `data_version` in the trace
headers against `sha256sum events.iap1`), the configuration differs
(`config_version`), or a component is non-deterministic — which is a bug
with a test to write (§6).

### 3. Reproduce — find the decision

```bash
# the chain of one parent order, with venue names, from the store
PYTHONPATH=src python3 -m iap.mvp explain --run ../data/mvp/<run_id> 17
# Order 17
# Alpha:      EQ01-EQ03-EQ06  expected return = -0.0 bps  confidence = 0.10   <- acting (ensemble) signal
# Alpha:      EQ01  expected return = +0.0 bps  confidence = 0.06             <- its components
# Alpha:      EQ03  expected return = -0.0 bps  confidence = 0.25
# Alpha:      EQ06  expected return = +0.0 bps  confidence = 0.00
# Portfolio:  target = -250 shares
# Risk:       ALLOW
# Risk:       ALLOW
# Execution:  TWAP
# SOR:        XV1 = 100%
# Fills:      0 / 250 (0.0%)
# TCA:        IS = -0.0 bps
# Attribution: alpha = +0.0 bps  spread = -0.0 bps  impact = -0.0 bps  fees = -0.0 bps

# every order as one row: signal -> risk -> children -> fills -> TCA -> attribution
PYTHONPATH=src python3 -m iap.store sql --db ../data/mvp/<run_id>/iap.sqlite \
  "SELECT parent_order_id, signal_model_version, risk_decision, risk_rule_id, n_child_orders, filled_qty, implementation_shortfall_bps FROM v_order_chain ORDER BY parent_order_id"

# the rejected ones, with the deciding rule and reason
PYTHONPATH=src python3 -m iap.store sql --db ../data/mvp/<run_id>/iap.sqlite \
  "SELECT trace_id, order_id, rule_id, rule_index, reason FROM risk_decisions WHERE decision <> 1 ORDER BY timestamp_ns"
grep -c '"decision":2' ../data/mvp/<run_id>/risk_audit.jsonl              # REJECTs in the audit

# the exact trace line (the pinned renderer shows 0.1 bp; the line holds the exact values)
grep '"parent_order_id":17,' ../data/mvp/<run_id>/traces.jsonl | python3 -m json.tool | less
```

```powershell
# from python\ with $env:PYTHONPATH = "src"
python -m iap.mvp explain --run ../data/mvp/<run_id> 17
python -m iap.store sql --db ../data/mvp/<run_id>/iap.sqlite "SELECT trace_id, order_id, rule_id, rule_index, reason FROM risk_decisions WHERE decision <> 1 ORDER BY timestamp_ns"
(Select-String -Path ../data/mvp/<run_id>/risk_audit.jsonl -Pattern '"decision":2' -SimpleMatch).Count   # REJECTs in the audit
Select-String -Path ../data/mvp/<run_id>/traces.jsonl -Pattern '"parent_order_id":17,' -SimpleMatch | ForEach-Object { $_.Line } | python -m json.tool
```

For a Java session: the line whose `stages.parent_orders[0].parent_order_id`
is the risk order id, rendered with `com.iap.trace.Explain.render(trace,
venueNames)`; or load `decision_traces.jsonl` into a store
(`iap.trace.JsonlTraceSink` lines are the same contract:
`Store.insert_trace(DecisionTrace.from_dict(json.loads(line)))`) and use the
queries above. The risk audit is byte-identical across Rust, Java and Python
engines, so `risk_audit.jsonl` can be replayed through `iap.risk` under a
debugger with the same decisions (docs/API_REFERENCE.md §1.2).

A session that ended `STOPPED` (a requested stop, `platform_session_state`
4) has no session report: its evidence is the state directory. Resume it to
completion first (`docs/RUNBOOKS.md` §5) if the incident needs the
report's digests, or work from `decision_traces.jsonl` and
`risk_audit.jsonl` directly — both are complete up to the checkpoint.

#### 3.1 Check the audit chain before trusting the replay (v1.14)

A replay proves the captured stream reproduces the trace digest. It says
nothing about whether `risk_audit.jsonl` is the file the engine wrote.
Since v1.14 it is a hash chain; verify it before reading decisions out of
it (docs/RUNBOOKS.md §6). A broken chain is its own
incident.

### 4. Debug — step the engine

The engine is a plain Python object; drive it over the captured stream with
an in-memory sink and inspect the books, the risk engine, the simulator and
the TCA timeline at every event:

```python
from iap.mvp.config import load_config
from iap.mvp.feed import load_feed
from iap.mvp.engine import MvpEngine
from iap.trace import MemoryTraceSink
from pathlib import Path
cfg = load_config("../configs/mvp/mvp.json", repo_root=Path(".."))   # the configuration that produced the run
feed = load_feed("../data/mvp/<run_id>")                               # the captured stream (identity re-derived from bytes)
sink = MemoryTraceSink()
eng = MvpEngine(cfg, feed, sink)                                       # MvpEngine(cfg, feed, sink, *, ref=None)
for ev in feed.events:
    if ev.exchange_ts >= T_INCIDENT:                                   # stop right before the decision
        break
    eng.on_event(ev)
eng.risk_engine.position(12), eng.risk_engine.open_order_count()       # iap.risk.RiskEngine
eng.sim.simulator.orders, eng.sim.simulator.fills                      # iap.execution.ExecutionSimulator
sink.traces                                                            # every trace emitted so far
```

`python/tests/test_mvp.py::test_engaged_kill_switch_rejects_every_child_and_nothing_fills`
is the pattern (a scripted engine, a changed risk state, assertions on what
the trace shows). The truncation probe
(`test_shift_by_one_and_truncation_leakage_probes`) is the model for "does
the state at event k depend on anything after k?": run the engine on
`events[:k]` and compare every earlier signal and target bit for bit.

### 5. Fix — change the component, let `replay` show the diff

After the change, `python -m iap.mvp replay --run <run_id>` no longer
reproduces the captured report: its diff (digest, counts, P&L, per-rule
decisions) is the evidence of what the fix changed, and it belongs in the
PR next to the reason. A fix that changes a pinned semantics also changes a
golden (CONTRIBUTING.md §4): regenerate with the owning tool, one `golden:`
commit, a `schemas/MIGRATIONS.md` entry, and every port re-matched in the
same PR. A fix in `iap.risk` or `iap.execution` is a fix in the
reference-equivalent port only; the normative text is `rust/risk` /
`cpp/execution`, and the golden the three languages share decides
(docs/API_REFERENCE.md).

### 6. Regression test — pin it

A captured stream + its expected digest is a test, exactly like
`tests/golden/expected_mvp.json`:

```bash
cd python && PYTHONPATH=src python3 tools/make_golden_mvp.py --force        # deliberate, versioned change only
PYTHONPATH=src python3 -m pytest -q tests/test_mvp_golden.py               # reproduces it from scratch AND from the capture
python3 -m pytest -q tests/replay/test_mvp_replay_determinism.py           # repo root: run twice, replay, identical bytes
```

```powershell
Set-Location python; $env:PYTHONPATH = "src"
python tools/make_golden_mvp.py --force         # deliberate, versioned change only
python -m pytest -q tests/test_mvp_golden.py
Set-Location ..; python -m pytest -q tests/replay/test_mvp_replay_determinism.py
```

For a smaller pin, `configs/mvp/mvp_tiny.json` + `generator_tiny.json` are
the fast fixtures the tests use; a scenario-shaped incident (a kill switch
engaged mid-session, a venue halted, a NO_ROUTE child) becomes a scripted
test in `python/tests/test_mvp.py` in the style of the kill-switch case,
and its row in `docs/SCENARIOS.md`. A risk-engine incident that needs its
own engine state — a fail-closed configuration, an engine awaiting
bootstrap, a restore, a kill command that does not parse — is pinned as a
scenario of the edge golden instead (`tests/golden/expected_risk_edge_decisions.json`,
generator `python/tools/make_golden_risk_edge.py`; COOKBOOK recipe 34
replays it): each scenario builds a fresh engine, and Rust, Java and Python
must reproduce its decisions and audit bytes. For a Java incident, the equivalent pin
is a `PaperTraceTest`-style case over the same vector asserting the
`trace.digest`.

### 7. Escalation and records

- A replay that reproduces the incident bit for bit is attached to the
  postmortem with the run id / state directory, its `data_version`,
  `config_version` and trace digest (GOVERNANCE.md §3: audit logs are never
  edited; corrections are new entries).
- A kill switch engaged during the incident follows
  `docs/RUNBOOKS.md` — a replay is never a re-arm path.
- A fix to a risk rule needs the risk owner (CODEOWNERS `rust/risk/`,
  `python/src/iap/risk/`); a fix to a fill rule the execution owner
  (`cpp/include/iap/execution/`, `python/src/iap/execution/`).

---

## Runbook: register pre-registrations in the CI environment

A pre-registration's fingerprint records `deps` (installed numpy, pandas, scipy).
`iap.agents.prereg_gate.require` refuses a registered run whose current deps differ
("deps changed since pre-registration"). CI installs `python/requirements-ci.txt`, so a
prereg registered with other versions is refused in CI (this happened to R7 in v1.15).
Register from a venv built from that lock. Nothing here changes existing registrations.

### Check (any registration script built on the helper)

Registration scripts call `iap.agents.fingerprint.check_registration_env`. It prints a
warning when the local numpy/pandas/scipy differ from the lock; `--require-ci-env`
refuses (exit 2) instead:

```powershell
python tools\quoting_v2_register.py . --dry-run --require-ci-env
```

### One-time: build the CI venv (owner, from the repo root, Windows)

Python 3.11 or 3.12 (CI tests both; the CI-env check compares the numpy/pandas/scipy
versions only, not the interpreter) and the same install command as `.github/workflows/ci.yml`.

```powershell
py -3.12 -m venv .venv-ci    # or: py -3.11 -m venv .venv-ci
.\.venv-ci\Scripts\python -m pip install --upgrade pip
.\.venv-ci\Scripts\python -m pip install --require-hashes -r python\requirements-ci.txt
```

The lock includes `cryptography` (signing). The package itself is not installed: the
registration scripts put `python/src` on `sys.path`, as CI does with `PYTHONPATH`.

### Each registration

```powershell
$env:PYTHONUTF8 = "1"
.\.venv-ci\Scripts\python tools\<script>_register.py . --dry-run --require-ci-env
.\.venv-ci\Scripts\python tools\<script>_register.py . --require-ci-env
```

The first line must print `[ci-env] deps match requirements-ci.txt: OK`. When the lock
is bumped, rebuild the venv (`Remove-Item -Recurse -Force .venv-ci`, then the steps above) before
registering. `.venv-ci/` is git-ignored.

### C8 breadth book (v1.17, option a)

Run these from the repository root in PowerShell. First build the one-time `.venv-ci` (see above), then:

```powershell
git pull
$env:PYTHONUTF8 = "1"
.\.venv-ci\Scripts\python tools\breadth_book_register.py . --dry-run --require-ci-env
.\.venv-ci\Scripts\python tools\breadth_book_register.py . --require-ci-env
git add research\preregs\breadth_book_prereg.json
git commit -m "C8: register the breadth book (option a, screen refused)"
```

A good dry run prints the following lines. The sha256 values are whatever is on disk:

```
[ci-env] deps match requirements-ci.txt: OK
draft option (a) ok; screen research/breadth/screen/screen_multi7.json sha256 matches the pin
[dry-run] draft sha256 <64 hex>
[dry-run] screen gate for EQ11/15m/linear_z_v1: EQ11/15m/linear_z_v1 was NOT screened by the net-of-cost screen ...: no confirmatory run (v1.17 C6)
[dry-run] would call WriteBroker.preregister(EQ11/15m:breadth-book-c8, screen=...) -> refused (expected under option a); would write research/preregs/breadth_book_prereg.json
```

A good registration prints `maintainer key matches trust root ashjha0`, then
`C8-BREADTH-BOOK: screen gate REFUSED: EQ11/15m/linear_z_v1 was NOT screened ...`, then
`C8-BREADTH-BOOK: wrote research/preregs/breadth_book_prereg.json (sha256 ...)`.

That file is signed. Its status reads "REGISTERED under option (a) ... no confirmatory
look", `on_blackboard` is false and `registration.holdout_read` is false. The refusal
happens before any ledger debit, so no look is charged. No data is read and no holdout
dataset is built or downloaded.

The tool exits with an error, and writes nothing, in any of these cases:
- the draft's decision is not option (a)
- the screen's sha256 differs from the pinned value
- the output file already exists

### B7 shortfall gate: signal-aware vs plain execution (SUPERSEDED 2026-10-16)

> **Superseded.** B7 was closed on its in-sample result on 2026-10-16
> (`research/preregs/B7_CLOSED_v117.md`): the draft carries status CLOSED, nothing was
> registered or signed, the nine overlap days were not read. `tools/b7_shortfall_register.py`
> refuses the CLOSED draft. The steps below are kept as the procedure a NEW B7 draft would
> follow; they do not apply to the closed one.

The draft is `research/execution/benchmark/prereg_DRAFT.json` (the B1 grid, the owner's
2026-10-15 decisions, the B2/B3 signal arm, walk depth 50, the fresh-test day list and the
in-sample `expected_effect` from `BENCHMARK_multi7_signal.md`). Before registering, fill the
two OPEN fields in the draft by hand and commit them: `signal_arm.alpha_id` (EQ10 or EQ05) and
`signal_arm.thresholds` (threshold_z, passive_urgency, gain); drop any `fresh_test.sessions`
row whose IEX day is not ingested (its `iex_dataset_version` still says "to be filled"). The
tool refuses, and writes nothing, while any of those is open, while
`simulator.walk_depth_levels` is not 50, or when the `configs/prereg/v2.json` fingerprint
differs from the file on disk.

Run these from the repository root in PowerShell, with the one-time `.venv-ci` built as above:

```powershell
git pull
$env:PYTHONUTF8 = "1"
.\.venv-ci\Scripts\python tools\b7_shortfall_register.py . --dry-run --require-ci-env
.\.venv-ci\Scripts\python tools\b7_shortfall_register.py . --require-ci-env
git add research\preregs\b7_shortfall_prereg.json research\experiments.json research\agents\blackboard.jsonl
git commit -m "B7: register the shortfall gate (signal-aware vs plain)"
```

A good dry run prints `[ci-env] deps match requirements-ci.txt: OK`, then
`draft complete: N fresh day(s), walk depth 50, v2 fingerprint ok`, then the
`[dry-run] would call WriteBroker.preregister(EQ10/shortfall:b7-signal-aware, sign -1)` line.
A good registration prints `maintainer key matches trust root ashjha0`, then
`B7-SHORTFALL-GATE: registered EQ10/shortfall:b7-signal-aware as <id>` and the
`wrote research/preregs/b7_shortfall_prereg.json (sha256 ...)` line. The registration debits one
ledger look; no market data is read and the fresh days stay unread until the signed run, which
is the `fresh_test.command` of the draft, run once.

---

## Runbook: R7 Nasdaq ITCH sample-directory watch

`tools/r7_watch_nasdaq.py` checks Nasdaq's free sample directory
`https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/` for new full-day ITCH 5.0 files.

- One read-only GET of the listing (30 s timeout, user-agent names the project).
  It **never downloads**; downloads stay manual with
  `tools/r7_download.py --i-have-approval` (docs/R7_DATA_PLAN.md §4-5).
- Compares to the committed known-list `research/r7/nasdaq_listing_known.json`
  (46 entries as of 2026-10-11, every file listed that day).
- New `S*-v50.txt.gz`, `*.NASDAQ_ITCH50.gz` and `itch50_MM_DD.gz` entries get a
  session date, weekday, `spent` (`r7plan.SPENT_DAYS`) and `candidate`
  (`research/r7/r7_candidates.json`) flags. A weekday that is neither spent nor a
  candidate is a *usable new day*. Size changes and removed entries are reported too.
- Exit codes: `0` nothing new, `10` usable new day(s), `1` error (network, parse).
- **IEX HIST (v1.17 WS0).** The same run also reads the IEX HIST index
  `https://iextrading.com/api/1.0/hist?date=YYYYMMDD` for UTC today minus 1..7
  (`--iex-days`, `--no-iex` to skip): one small JSON GET per day, same
  user-agent and timeout, never following a `link`. It reports which days have
  DEEP / TOPS files and their sizes (`iex` in the JSON report, `IEX HIST:` lines
  in the text). A day with data that the known-list's `iex_days` has not seen sets
  `iex_new` and goes into the toast; it is informational and does not change the
  exit code, which is Nasdaq's alone. A failed IEX read is recorded per day, not
  an error exit.

### Run by hand

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = "python\src"
python tools\r7_watch_nasdaq.py --json-out data\real\r7_watch\last.json
```

`--listing-file saved.html` parses a saved listing (no network for Nasdaq; add
`--no-iex` for none at all). `--notify` raises a Windows toast through
`tools/jobs/watchdog.py`'s notifier when usable Nasdaq days or new IEX days appear.

### Daily schedule (not registered; the owner registers it)

`tools\run_r7_watch_nasdaq.cmd` runs one check with `--notify`, writing
`data\real\r7_watch\last.json` and appending to `data\real\r7_watch\watch.log`.
`data\real\` is git-ignored (`.gitignore`: `/data/real/`), so the directory is not
committed; the `.cmd` creates `data\real\r7_watch` on its first run.

The task registered with the earlier `schtasks /Create` line never ran: its Last
Result was `-2147020576` (`0x800710E0`, "the operator or administrator has refused
the request") because the task was interactive-only and set not to start on
battery, and this is a laptop. Register it from an **elevated PowerShell** with the
settings below instead (runs whether the user is logged on or not, no password
stored, starts on battery, catches up a missed 09:00, 20 min limit). Replace the
existing task first (`Unregister-ScheduledTask` or the `schtasks /Delete` line).

```powershell
$cmd = 'C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\tools\run_r7_watch_nasdaq.cmd'
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c "' + $cmd + '"')
$trigger = New-ScheduledTaskTrigger -Daily -At 09:00
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
Register-ScheduledTask -TaskName 'IAP R7 Nasdaq watch' -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force
```

Remove with `schtasks /Delete /TN "IAP R7 Nasdaq watch" /F`.

Check it ran (expect `Last Result: 0` nothing new, or `10` usable new day(s);
anything else is a failure, see `data\real\r7_watch\watch.log`):

```powershell
schtasks /query /tn "IAP R7 Nasdaq watch" /v /fo LIST | Select-String "Last Result"
```

### When it reports something

1. Read `last.json`. Check the new day against docs/R7_DATA_PLAN.md §2 (selection).
   An IEX day (`iex.new_days`) is a second, free venue: note it in the plan; nothing
   is downloaded by the watcher.
2. If it is wanted, add it to `research/r7/r7_candidates.json` (status stays DRAFT)
   and ask the owner to approve the download.
3. After review, `python tools\r7_watch_nasdaq.py --update-known` appends the new
   entries (and the IEX days seen, under `iex_days`) to the known-list; commit it so
   the next run is quiet.
