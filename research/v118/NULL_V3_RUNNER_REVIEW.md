# Known-null protocol v3 (rev 4): runner and frozen configuration, submitted for review (UNSIGNED, NOT RUN)

Answers the outside reviewer's decision of 2026-10-20 (approve with named changes; build and review the runner and
configuration before signing). Nothing here is signed, no study was run, no market data was read. Thresholds (60/2,000,
55/1,854, 3.0 %), level 0.025, seeds and generators are unchanged.

## Deliverables

| reviewer item | where |
|---|---|
| runner source + config | `tools/rel06_known_null_v3.py` (driver), `python/src/iap/research/known_null_v3.py` (study logic), `research/v118/known_null_v3_config.json` (frozen configuration; every protocol-section-12 key) |
| binding code path | `known_null_v3.identity_args_v3` (what is signed; `tools/prereg_bind_identity.py bind --study-config` dispatches on `kind = known_null_v3`) and `known_null_v3.verify_binding` (what the runner checks before any trial) |
| centring invariant + B blocker | `known_null_diag.check_centring_invariant / verify_series_used / require_centring_invariant`, `b_residual_blocker`; called by `known_null_v3.build_a0_source` and `evaluate_size_arm` |
| 3.4 gate | `known_null_diag.understatement_ratio / lost_difficulty / primary_verdict` |
| verdict-level tests | `python/tests/test_known_null_v3.py` and `test_known_null_diag.py` |
| calibration measurement | `research/v118/known_null_v3_blocker_calibration.json` (`rel06_known_null_v3.py --blocker-calibration`) |
| clean-checkout test report | in the PR description, on the exact candidate commit |

## Binding (what makes "protocol + prereg + config + code" one signed object)

The signed `analysis_identity` binds the SHA-256 of: the protocol file, the runner configuration, `tools/rel06_known_null.py`,
`tools/rel06_known_null_v3.py`, `known_null.py`, `known_null_diag.py`, `known_null_v3.py`, plus the `python/src/iap` tree
hash, the exact commit, data version, family hash, metrics, and the whole decision content of the configuration as
`thresholds` (alpha, size arm, pass rule, power arm, residual-mean roles, centring, gate, economics). The prereg file itself
is covered by the maintainer signature (`protocol_sha256` inside it). `verify_binding` refuses unless: the signature
verifies; `prereg_id` is this file's path and the config's `prereg`; the protocol file hash equals the signed
`protocol_sha256` and the bound script hash; the configuration hash equals the bound one; the bound script set equals the
code's `BOUND_SCRIPTS`; `verify_identity` passes on the thresholds recomputed from the configuration in hand (a changed
seed, threshold, role or label is a mismatch); the bound commit is HEAD or its ancestor; the actor is not the signer.
`validate_config` additionally refuses any configuration whose frozen values differ from the code constants. Tests flip one
input at a time (protocol, config, code, threshold, prereg id, signer = runner, unsigned).

## A0 centring invariant (hard)

`build_a0_source` centres per (name, horizon, session), runs the invariant on the centred arrays, requires the number of
checked groups to equal the configuration's, builds the grids the derangement reads, and verifies the grids are
bit-identical to the grid of the checked copy; it raises `CentringInvariantError` (run aborted) otherwise. It is called in
the parent before any job and in every worker; the runner re-verifies after the trials, and the verdict is INVALID if that
fails. Tests: centred pass; uncentred input aborts; wrong group count aborts; tampering with the used array is detected.

## B residual-mean blocker and its calibration at the verdict's aggregation level

`b_residual_blocker` applies the unchanged statistic (mean of the per-trial S2 session mean), SE (sd / sqrt(2,000),
independent trials) and threshold (|t| > 4) to each of the 16 `K16` hypotheses and blocks PASS if **any** is flagged or
cannot be checked. `measure_b_blocker` runs that function and then `primary_verdict` (all rate cells passing, gate
NOT_TRIGGERED) on synthetic B datasets of 16 hypotheses x 2,000 independent trials, and counts verdicts `FAIL / b_flag`:
exactly P(at least one false flag across the 16). Measured (valid null):

| model | datasets | false-flag verdicts | rate |
|---|---|---|---|
| independent normal | 20,000 | 24 | 0.12 % |
| common factor, rho 0.5 | 20,000 | 25 | 0.125 % |
| common factor, rho 0.9 | 20,000 | 6 | 0.03 % |
| t(3) tails | 5,000 | 5 | 0.10 % |
| skewed (centred exponential) | 5,000 | 7 | 0.14 % |
| common factor 0.9, second seed | 5,000 | 2 | 0.04 % |

Analytic (normal statistics; mean/SE is exactly t with 1,999 df): per hypothesis 6.6e-5; for any dependence among the 16 the
union bound is **0.105 %**, equal to it if independent. So the verdict-level false-block rate is about 0.1 % (about
1 in 1,000), not the 1e-5 per-hypothesis figure quoted before; the 0.12-0.14 % rows are within Monte Carlo error of 0.105 %.
Injected bias (kept, now at verdict level; per-trial-mean bias in units of the trial sd; one hypothesis / all 16):
0.05 sd: 3.8 % / 46 %; 0.09 sd: 49 % / 100 %; 0.15 sd: 99.6 % / 100 %; 0.20 sd: 100 % / 100 % (2,000 datasets each).
**What it detects:** a mean of an S2 session-mean statistic across B trials more than 4 SE from zero in at least one of the
16 hypotheses (about 0.09 sd of the per-trial statistic detected half the time). **A non-flag is not proof of unbiasedness**;
it says nothing on dependence structure, tails or inference validity. The synthetic models are mean-zero by construction;
the real B statistic's distribution is not known to match them.

## Section 3.4 gate and labels (verdict level)

* Missing / non-finite m or SE, fewer than two trials, zero or non-finite mean(SE^2) -> U = NaN -> gate UNDETERMINED, even
  for one of 16 hypotheses; the offending id is listed; the 16-value median is not computed on 15 (tests: NaN SE in one A0 or
  one B trial, zero SE, missing mean).
* Label for TRIGGERED, UNDETERMINED, **and any missing or malformed gate**: "PASS (B-supported only; A0 non-informative)" with
  machine verdict `PASS_B_ONLY`, which never equals `PASS`; only a gate whose status is exactly `NOT_TRIGGERED` gives the scoped
  label with verdict `PASS`. (The earlier code treated any status other than TRIGGERED/UNDETERMINED as plain, so a malformed
  gate gave the scoped label; now fails closed.)
* Every result carries `claim_scope` ("neither alone or combined proves FWER control at 2.5 % on real trading data") and an
  `a0` block: gate status, reason, invalid ids, both medians, the A0 counts per cell, and the descriptive A0 residual-mean
  report. An A0 rate failure is FAIL whatever the gate says (tested).
* Fail-closed inputs: non-integer / boolean / negative / missing counts -> FAIL; blocker not exactly `False` -> FAIL; centring
  not exactly `True` -> INVALID; wrong trial counts -> INCOMPLETE (not a verdict).

## Points for the reviewer / coordinator (decisions I made or discrepancies found)

1. **Centring groups: 63, not 49.** The protocol text and the prereg draft say 49 (7 pairs, omitting QQQ at 10 s and 1 m),
   but K3 trades AAPL+MSFT+QQQ at 1 s, 10 s and 1 m, so 9 pairs x 7 sessions = 63 groups are used. The configuration derives and
   asserts 63 (stricter; the runner aborts on any other count). The protocol and the prereg draft were **not** edited (the text
   the reviewer approved); they need a one-line reconciliation (49 -> 63) before signing.
2. **Blocker with an unchecked hypothesis** (missing / non-finite series): blocks PASS (FAIL, `b_flag`), not silently ignored.
   This is stricter than the gate's UNDETERMINED treatment of the same condition; confirm.
3. **Machine verdict `PASS_B_ONLY`** is new (protocol text says "PASS labelled by the gate"); chosen so no consumer testing
   `verdict == "PASS"` can read a B-only result as plain.
4. Descriptive only: after collapsing 100 ms cells (a cell holding several rows keeps one), the grid mean of a group need not
   be exactly zero although the row mean is; `grid_collapse` reports the worst relative value, no verdict role.
5. R0 and the power-arm R0 are re-read from the stored Holm-adjusted p-values at 0.05 on the same draws (no extra compute).
6. The protocol text still says "the v3 runner does not exist yet" and quotes the 1e-5 B figure (per hypothesis; the
   verdict-level figure is 0.1 %); wording reconciliation is for the coordinator with the 49 -> 63 fix.
7. Not exercised on real data (by instruction): the data path (`load_panel`, pricer) is reused unchanged from the v2 runner;
   the new code is exercised on synthetic panels and fabricated records. `--timing` was not run.
