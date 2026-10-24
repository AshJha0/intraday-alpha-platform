# R7-CONFIRM-1 results (v1.15)

Confirmatory test of hypothesis H4 (tier 1) from the owner-signed
pre-registration `research/r7/r7_confirm_prereg.json`: EQ10 @1s and EQ05 @1s
on the 7 unseen holdout sessions (AAPL, MSFT, QQQ), one look each.
Every number below is copied from `research/r7/r7_confirm_result.json` /
`research/r7/r7_confirm_result.md` (in-sample ICs from the prereg's
`gate_ic` fields); nothing is recomputed here.

- Dataset: merged holdout, `dataset_version`
  `ca5d079831be4e63aa76e350bb840a29a6964c0152f4bbbcce5010269a193854`
- Code: git commit `ed98ff41a86d15484c2a7db9389c0485b69ca898`; run
  2026-10-12T19:55:25Z (owner-run, `tools/r7_confirm.py --i-am-the-owner`)
- Gate (prereg): HAC t >= ledger threshold in force and deflated Sharpe > 0.95,
  both on the per-session IC series; net-of-cost P&L descriptive only.
- DSR trial count: the DSR column below was recorded at the holdout dataset's
  own ledger count (N = 1 for EQ10, 2 for EQ05), not the registered N = 297
  (the repo ledger's 296 configurations at the look plus the look itself).
  Recomputed at N = 297 without a new look: EQ10 0.99886, EQ05 0.99996, both
  verdicts unchanged (`research/r7/R7_DEVIATION_dsr_trials.md`,
  `research/r7/r7_confirm_result_dsr_recompute.md`; v1.17 WS0 item 0.1).
- Threshold: **4.432** = max(run's ledger threshold, repo-ledger Bonferroni
  |t|), from `research/experiments.json` (5,361 experiments over 296
  configurations; `tests/harness/check_headline_numbers.py`).

## Verdicts

| alpha | experiment | holdout IC | in-sample IC | HAC t | threshold | DSR | sessions > 0 | verdict |
|---|---|---|---|---|---|---|---|---|
| EQ10@1s | `fefeac6aa15f9e34` | +0.0838 | 0.0756 | 13.66 | 4.432 | 0.99995 | 7/7 | **confirmed** |
| EQ05@1s | `c39e188e5d82da00` | +0.0409 | 0.0627 | 13.20 | 4.432 | 0.9999998 | 7/7 | **confirmed** |

EQ10's holdout IC is above its in-sample IC; EQ05's is about two thirds of
its in-sample IC. Both clear the gate by a wide margin.

## v1.18 re-issue (2026-10-24, gate F5)

`research/r7/r7_confirm_result_reissue.json` re-issues the result at the registered
N = 297 with the v1.18 verdict-eligibility rule applied (`tools/r7_confirm.py verdict()`,
REL-02d). It is computed only from recorded outputs (the per-session IC series and
`r7_confirm_result_dsr_recompute.json`; source sha256 in its `reissue` block). No data
was read, no ledger entry was appended, and `r7_confirm_result.json` is unchanged.

| alpha | DSR before (N) | DSR after (N = 297) | gate | verdict before | verdict after |
|---|---|---|---|---|---|
| EQ10@1s | 0.99995 (1) | 0.99886 | passed | confirmed | **ineligible** |
| EQ05@1s | 0.9999998 (2) | 0.99996 | passed | confirmed | **ineligible** |

What changed: the DSR trial count, and the verdict label. The recorded eligibility
(`gate_eligible = false`, because `configuration.methods = v4` must be `v2`) was already
in the original file, but its verdict predates REL-02d; under that rule a pass by a method
that is not gate-eligible is `ineligible`, never `confirmed`. What did not change: IC,
HAC t, thresholds, per-session IC, the eligibility records, and the fact that the numeric
gate (HAC t and DSR > 0.95) passes at N = 297. The tables above are the original
recorded values; read the verdict column as superseded by this note. No outcome is
recorded on the blackboard by the re-issue, and its `record_commands` are removed.

## Per-session IC

| session | EQ10@1s | EQ05@1s |
|---|---|---|
| 2025-11-28 | 0.0636 | 0.0337 |
| 2025-12-08 | 0.0717 | 0.0562 |
| 2025-12-09 | 0.0617 | 0.0507 |
| 2025-12-10 | 0.0739 | 0.0393 |
| 2025-12-11 | 0.0774 | 0.0529 |
| 2025-12-12 | 0.0994 | 0.0477 |
| 2026-06-12 | 0.0603 | 0.0319 |

## Sensitivity (descriptive, never decides)

Registered: with and without 2025-12-10 (FOMC) and 2025-11-28 (holiday-thin).

| alpha | variant | raw IC | raw HAC t | mean session IC | DSR |
|---|---|---|---|---|---|
| EQ10@1s | all sessions | 0.0712 | 12.37 | 0.0726 | 0.99995 |
| EQ10@1s | excl. 12-10, 11-28 | 0.0712 | 10.39 | 0.0741 | 0.999995 |
| EQ05@1s | all sessions | 0.0414 | 17.87 | 0.0446 | 0.9999998 |
| EQ05@1s | excl. 12-10, 11-28 | 0.0427 | 16.02 | 0.0479 | 0.988 |

## Net P&L (descriptive only)

At the primary 1x cost the net P&L block is 0 for both alphas (gross,
cost, net, Sharpe and drawdown all 0.0 bps). The signals are **predictive but
not yet tradable**: the registered verdict is IC-only, and this result says
nothing about monetisation.

## Holdout drift (`research/r7/drift/drift_report.md`)

Descriptive, run after ingest and before the look (reference
`data/real/multi7`, target the 7 holdout sessions). Many depth / intensity
features drift (PSI alert 0.25): pooled 122 of 205 features flagged.
Per-instrument group 2 (MSFT by the prereg symbol order AAPL, MSFT, QQQ)
shows the queue-rate features in drift, e.g. `queue_depletion_rate_bid_w10s_v1`
PSI 2.70, `queue_depletion_rate_ask_w10s_v1` 2.50,
`queue_replenish_rate_bid_w10s_v1` 2.35, `queue_replenish_rate_ask_w10s_v1`
2.08 (mean shift about -0.75 to -0.81 reference std). EQ05 reads the 1s
versions of these queue rates, so its lower holdout IC is consistent with
input drift, but the report does not establish cause. The round-lot change
(next section) is a known structural change in the same period.

## Deviations (all recorded before the relevant look)

- **lot_size merge** (`research/r7/R7_DEVIATION_lot_size.md`, owner option A):
  holdout lot sizes differ (AAPL 100 -> 40 on 2026-06-12; MSFT and QQQ 40 vs
  100 in-sample, round-lot reform); sessions merged with the lot_size
  consistency check relaxed. Features do not read `lot_size`; the gate is
  IC-only.
- **Infrastructure crash, retry 1** (`research/r7/R7_DEVIATION_crash_retry.md`,
  #118): the first EQ10 look crashed on an 800 MiB allocation in the
  report-only CPCV block before any statistic; no result seen, no ledger
  debit; EQ05 not started. Fixed (column-narrowed CPCV frames, output
  identical), one retry. Record: `research/r7/r7_confirm_result_crash1.json`.
- **Infrastructure crash, retry 2, EQ05 only**
  (`research/r7/R7_DEVIATION_crash_retry2.md`, #119): on retry 1 EQ10
  completed (confirmed, carried over unchanged); EQ05 crashed on a 1.04 GiB
  allocation in the walk-forward splitter before any statistic, with no EQ05
  experiment or ledger entry. Fixed (sliced windows, column-narrowed train
  frames, output identical), one EQ05-only retry. Record:
  `research/r7/r7_confirm_result_crash2.json`.
- **DSR trial count** (`research/r7/R7_DEVIATION_dsr_trials.md`, recorded
  2026-10-15 after the looks, v1.17): the recorded DSR used the dataset
  ledger's N (1 / 2) instead of the registered N = 297; recomputed from the
  recorded per-session IC series at N = 297 and N = 308 (no new look):
  EQ10 0.99886, EQ05 0.99996, gate passed, verdicts unchanged.
- Registered code hash restored before the look (#117): the report-only
  `beta_stability` addition to `alpha/base.py` was reverted so
  `prereg_gate.require` passed.

The outcomes are recorded on the blackboard only by the owner
(`tools/r7_record_outcome.py --i-am-the-owner`); see #120.

## AI4 deferred

AI4-DEEPLOB-1 (`research/r7/ai4_deeplob_prereg.json`) was signed but is not
run in v1.15 (`research/r7/AI4_DEFERRED.md`): no real-data path, windows and
labels cross sessions, static price z-score. A new registration in v1.16 will
supersede it; its spent look stays counted, and the holdout sessions must not
be used for AI4 development before then.

## What it means for v1.16

EQ10 and EQ05 are now confirmed predictors on unseen data, but with 0 net P&L
at 1x cost they are not a strategy. v1.16 is about monetisation: execution
and sizing that turn a 1s IC of 0.04-0.08 into net P&L, tested under a new
pre-registration (net-of-cost as the decision rule), plus the AI4 rebuild.
