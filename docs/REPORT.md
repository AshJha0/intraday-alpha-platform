# Intraday Alpha Platform: technical report

Written 2026-10-26 for v2.1 (Track 0), on main after the v2.0.0 tag. Every number here is copied from
the file named beside it; the evidence class of each result follows the seven classes of
section 3. Nothing in this report is a new result.

## 1. What we built, and the question it answers

**The plain idea.** We wanted to know whether a systematic equity signal makes money after costs,
and how we would know if it did. Most backtests answer the first half and skip the second. So we
spent most of the effort on the second half: data that cannot see the future, a count of every
look we took, tests registered before the data is read, and a cost model that reconciles to cash.

**What exists.** A research and execution platform for US equities (and synthetic FX) that runs
from raw market data to a cash-reconciled ledger:

- market-data decoders (Nasdaq ITCH 5.0, IEX DEEP/TOPS, Databento MBO, LOBSTER) and a
  deterministic synthetic generator ([docs/REAL_DATA.md](REAL_DATA.md));
- order-book reconstruction, a 205-feature registry, 24 flagship alphas, event-time labels;
- portfolio construction, a fail-closed risk engine, execution algorithms (TWAP, VWAP, POV,
  implementation shortfall), smart order routing, a fill simulator and TCA;
- a v2.0 daily research stack: point-in-time universe, family specifications D1 to D5, a
  shared forecast engine, two reconciling ledgers and the E5 reports
  ([docs/ARCHITECTURE_v2.md](ARCHITECTURE_v2.md));
- the evidence machinery: an experiment ledger, signed pre-registrations, a sealed-data
  service, a registered runner and a release evidence manifest
  ([docs/GOVERNANCE.md](GOVERNANCE.md)).

Python is the reference implementation. C++ owns the latency path and Rust the risk engine;
golden tests hold the three equal ([docs/POLYGLOT.md](POLYGLOT.md)).

## 2. Results, negatives first

**Short answer: no alpha is confirmed.** Nothing on main is above *development-supported*
([release/v2.0.0_RELEASE_NOTES.md](release/v2.0.0_RELEASE_NOTES.md), "What this release is").

| # | result | class | source |
|---|---|---|---|
| 1 | The free daily route (Stooq) fails all five DD1 data tests: 315 of 372 index leavers missing (85 % against a 2 % limit), opens equal to prior close on 13.1 % of rows, 57 residual split jumps | rejected | [DD1 finding](release/evidence/DD1_FREE_ROUTE_FINDING_2026-10-26.md) |
| 2 | So daily families D1 to D4 cannot be tested on the data we hold | data-ineligible | v2.0.0 notes, I01 |
| 3 | Exploratory D1 and D3 on the incomplete 2026 partition: D1 -0.015 bps per session (se 0.467, 681 sessions), D3 -0.699 bps (se 0.248, 801 sessions), after costs, on survivor-only data | exploratory, data-ineligible | [exploratory run](release/evidence/DD1_2026_EXPLORATORY_RUN_2026-10-26.md) |
| 4 | The combined-book confirmation test PF-3 fails size for any component held 10 or more sessions | rejected | v2.0.0 notes, N02 |
| 5 | Intraday signals EQ10 and EQ05 predict 1-second returns on 7 holdout ITCH sessions (IC +0.0838 and +0.0409), but at 1x costs net P&L is zero; the verdict was re-issued as ineligible on 2026-10-24 | data-ineligible | [research/r7/](../research/r7/), [SHOWCASE.md](SHOWCASE.md) |
| 6 | As a cost-aware taker those signals trade in none of 480 grid cells: expected move 0.1 to 0.2 bp against a 0.8 to 1.2 bp round trip | rejected | v1.16 notes (archived), [SHOWCASE.md](SHOWCASE.md) |
| 7 | Two-sided quoting loses USD 5.0k to 6.9k per session; adverse selection is about 4x the spread captured | rejected | same |
| 8 | DeepLOB nets -0.69 bp per trade in-sample; linear and GBM baselines do no better | rejected | same |
| 9 | Smart order routing against pinned Nasdaq: all six B8 v2 comparisons underpowered on 3 sessions | exploratory (underpowered) | [research/execution/b8v2/REPORT.md](../research/execution/b8v2/REPORT.md) |
| 10 | The known-null calibration (v1.18) rejected about 4.3 % in two cells against a 2.5 % target, so no real-data error-rate control is claimed | rejected | [research/v118/](../research/v118/) |
| 11 | On the bundled synthetic data, 0 of 24 alphas pass the net-of-cost promotion gate | rejected | [research/alpha_reports/REPORT.md](../research/alpha_reports/REPORT.md) |

**What the negatives have in common.** Where a signal exists, it is smaller than the cost of
trading it. Rows 5 to 8 show this four different ways on intraday data. Rows 1 to 3 show why
we could not even ask the daily question honestly on free data: survivor-only prices make a
reversal strategy look like it trades recoveries.

## 3. Method

### 3.1 Evidence classes

Every performance statement carries one class (v2.0.0 notes, "How to read these notes"):

| class | meaning |
|---|---|
| independently confirmed | a registered single-shot decision run under custody separate from the developer |
| mechanically isolated | the same, run by the registered runner under the developer's custody |
| development-supported | positive on development data; not confirmed |
| exploratory | reported, no claim attached |
| rejected | the test or screen failed |
| data-ineligible | the data to test it does not exist, or failed its suitability gate |
| pending | registered; not yet decided |

*Worked example.* Row 3 above is a real number from real prices, but the prices failed DD1, so
the row is "exploratory, data-ineligible". It may be printed. It may not be quoted as evidence
that D1 does or does not work.

### 3.2 Counting looks

**The plain idea.** If you try 640 configurations, the best one will look good by luck. You have
to know how many you tried.

**The formal statement.** `research/experiments.json` records every look. It holds 6,826 looks
over 640 distinct configurations. Under the null, the expected maximum |t| over that many
looks is 4.202, and a Bonferroni per-test bar is |t| >= 4.484 ([SHOWCASE.md](SHOWCASE.md),
"Numbers a CI check re-derives"). The ledger is a lower bound: known gaps are listed in
[research/v118/LEDGER_INVENTORY.md](../research/v118/LEDGER_INVENTORY.md).

*Worked example.* The R7 holdout HAC t statistics (13.66 and 13.20) clear 4.484 easily. That is
why the statistical result stood, and also why it did not matter: the economic result (row 5)
was zero.

### 3.3 Pre-registration and sealed data

A confirmatory test is written down, signed and bound to a script hash, a commit, a dataset
and thresholds before the data is read. The test data sits behind a sealed service the
developer's account cannot read. The confirmatory gate `iap.validation.confirmatory` is the only
code that can return PROMOTE ([docs/GOVERNANCE.md](GOVERNANCE.md);
[python/tests/spec/CONFIRMATORY_TEST_SPEC.md](../python/tests/spec/CONFIRMATORY_TEST_SPEC.md)).

### 3.4 Leakage controls

Point-in-time universe membership, event-time labels taken at or before the decision, purged
walk-forward splits, and leakage canaries: features built to leak on purpose, which a test
must catch ([docs/RESEARCH_VALIDITY.md](RESEARCH_VALIDITY.md)).

### 3.5 Costs

Every simulated fill carries three cost columns (spread, fee, impact), and the cost waterfall
must reconcile to the cash identity (`python/tests/execution/test_sim_waterfall.py`).
A strategy is never judged on gross returns.

## 4. Architecture

The pipeline, from [docs/diagrams/pipeline.mmd](diagrams/pipeline.mmd): raw feed files,
normalisation with sequence checks, a canonical binary event stream (72-byte records with a
CRC-32 trailer), book reconstruction, features, labels, alphas, portfolio, risk, execution,
routing, fills, TCA and a per-decision trace. The whole loop runs on one instrument with
`python -m iap.mvp run`. Run twice, or replayed from its capture, it produces the same trace
digest ([docs/MVP.md](MVP.md)).

**Three languages, one semantics.** Each ported component has golden vectors produced by
the Python reference. The C++ and Rust copies must match them byte for byte. On the v1.18.0
CI run the table counted 4,062 Python, 333 C++ and 376 Rust tests, with 217, 74 and 75 golden
([README.md](../README.md), parity table).

## 5. Engineering numbers

All in-process, measured on shared CI hardware. These are not colocation numbers and are not
evidence of a latency edge.

| what | figure | environment | source |
|---|---|---|---|
| C++ tick-to-trade, decoded frame to order intent | p50 1,279 ns, p99 2,559 ns, p99.9 11,263 ns | CI runner AMD EPYC 7763, no core pinning, `steady_clock` | [benchmarks/results_tick_to_trade.md](../benchmarks/results_tick_to_trade.md) |
| C++ components | decode 184.1 ns, book update 26.4 ns, replay 27.2M events/s, features 514.1 ns | CI runner | [benchmarks/results_cpp.md](../benchmarks/results_cpp.md) |
| Rust feature engine called from Python | about 790,000 events/s, against about 5,250 for the Python reference (150x) | CI runner ubuntu-24.04 | [docs/API_REFERENCE.md](API_REFERENCE.md) |

## 6. What we would do differently

We over-invested in governance relative to what the data could support. By v1.18 there
were signed pre-declarations, an outside reviewer, analysis identities, a sealed service with
separate operating-system accounts and a release evidence manifest. All of it works and is
tested. But the first registered run never happened, because the free daily data failed its
first quality gate (row 1). A probe of the data's survivorship, run in the first week, would
have told us that before the custody machinery was built. The lesson we take: test the data
before building the controls that protect it.

## 7. Incident: the DD1 ingestion chain, 2026-10-25 and 26

The DD1 daily ingestion failed several times in a row before it produced the finding in row 1.
The fixes, in the order they landed, are recorded as defects D-96 to D-100 (PR #400) and DR-18
(PR #403): retries with back-off, EDGAR quarters cached from 1993 Q1, the sealed services asked
before the long walk instead of after it, re-runs that resume a partial seal, a log written on
every exit, and a probe mode that writes no data
([tools/ingest/dd1_daily.py](../tools/ingest/dd1_daily.py), v2.0.0 notes S03). One of the
failures was ours in a plainer way: a pushed test hung on a rate-limiter clock that never
advanced, and CI ran to its 30-minute timeout (PR #398). Since then, local tests finish before any push,
and injected clocks must advance.

## 8. Limitations

- No independent reproduction (gate F7 part P2 not attempted) and no separate custody, so
  nothing can reach the top two classes.
- The fill model is validated by deterministic replay only. No real orders or fills exist
  ([docs/SIM_TO_REAL.md](SIM_TO_REAL.md)).
- Intraday results rest on a few sessions of three symbols (AAPL, MSFT, QQQ).
- A survivorship-clean point-in-time daily source would change rows 1 to 3; we do not hold one.

## 9. Reproduce

```bash
python -m venv .venv-ci
.venv-ci/bin/pip install --require-hashes -r python/requirements-ci.txt
cd python && PYTHONPATH=src ../.venv-ci/bin/python -m pytest -q tests/leakage tests/portfolio tests/execution/test_sim_waterfall.py tests/reports tests/equivalence
```

The full parity table is `bash tests/harness/run_all.sh` (needs the toolchains of
[docs/BUILD_NOTES.md](BUILD_NOTES.md)); the pinned reproduction image is described in
[docs/REPRODUCTION.md](REPRODUCTION.md).
