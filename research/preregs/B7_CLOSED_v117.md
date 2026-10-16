# B7 signal-aware execution: CLOSED on its in-sample result (fresh test not run)

Owner decision 2026-10-16 (owner's words: "go ahead with your recommendation", after the
in-sample report of PR #215). No fresh test, no registration, no refit run. This supersedes the
"B7 fresh test ships in v1.17.1" statements of the v1.17.0 release.

## What was done
- **B2 / B3 built** (PR #176, v1.17.0): signal-aware urgency re-evaluated at every child decision
  (`iap.execution.urgency.AlphaUrgencyPolicy`), signal-aware pacing (`iap.execution.pacing`:
  POV participation, slice tilt, Almgren-Chriss drift), a signal source over the real flagship
  alphas (`iap.execution.signal.AlphaUrgencySource`), all opt-in and byte-identical off, and the
  B1 benchmark driver's `base+variant` policies paired with their plain control on the same windows.
- **In-sample run** (2026-10-16 12:59-15:30, detached, code at 73f3ddb = v1.17.0):
  `python -m iap.research.execution_benchmark --data-dir data/real/multi7 --tag multi7_signal
  --walk-depth 50 --window-min 30 --signal EQ10 --policies
  native,native+urgency,native+pacing,native+ac,native+ac_drift`. Dataset `data/real/multi7`
  (AAPL, MSFT, QQQ; 7 in-sample sessions 2019-01-30 .. 2020-01-30; 44,718,339 events), EQ10
  registered parameters (`configs/strategies/alpha_params.json`, beta -1.04e-7, synthetic fit),
  thresholds z 1.0, passive urgency 0.1, pacing gain 0.5 (clip 0.25-3), max age 10 s; 252 parents
  per policy (63 for the IS-only AC arms).
- Artefacts: `research/execution/benchmark/BENCHMARK_multi7_signal.md`,
  `benchmark_multi7_signal.json`, `BENCHMARK_multi7_signal_PROVENANCE.md`; the draft
  `research/execution/benchmark/prereg_DRAFT.json` (`expected_effect`, `in_sample_run`).

## Result (in-sample; paired signal-aware minus plain, all-in bps, day-clustered 95 % CI)

| arm | TWAP 1 % | TWAP 2 % | VWAP 0.5 % | VWAP 2 % | IS 0.5 % | IS 1 % | POV 1 % |
|---|---|---|---|---|---|---|---|
| urgency - native | +1.66 ± 0.54 [0.35, 2.98] | +1.95 ± 0.48 [0.72, 3.18] | -0.59 ± 2.71 | +2.47 ± 0.99 | -0.63 ± 0.39 | -0.47 ± 0.31 | +0.01 ± 0.02 |
| pacing - native | +0.89 ± 0.55 | -0.12 ± 0.07 | +0.78 ± 0.29 [0.02, 1.53] | -0.18 ± 0.20 | +0.84 ± 0.40 | +0.12 ± 0.28 | +0.41 ± 0.36 |
| ac_drift - ac (IS only) | — | — | — | — | 0.00 | 0.00 | — |

The full per-cell table (16 cells per arm) is the draft's `expected_effect`.

## Why the gate cannot be met with this configuration
- **No cell is cheaper by the 2 bp minimum effect** (the gate's one-sided condition); the only cells
  that clear 2 bp go the wrong way.
- **The urgency arm makes TWAP / VWAP dearer at 1-2 % ADV** (+1.7 and +2.0 ± 0.5 bps TWAP, +0.8 to
  +2.5 bps VWAP) while filling 6-10 pp more: the adverse-crossing children buy completion at a price.
  IS is 0.2-0.6 bps cheaper, not significant; POV within 0.1 bps.
- **The registered EQ10 sign is opposite to its real in-sample IC** (beta < 0 from the synthetic fit;
  corr(ref_ret_1s, 1 s label) is +0.07 on AAPL / MSFT in multi7), so the agree / adverse labels may be
  swapped relative to the true direction. The registered parameters are what the plan named.
- **The AC drift arm is inert at the registered beta**: with |expected_return| <= 4e-7 the drift is far
  below the temporary-impact scale and `native+ac_drift` equals `native+ac` in every cell (0.00).
- **`no_signal` is 21-33 % of children**: QQQ is outside EQ10's universe (the ETF is the reference),
  so a third of the parents ran the plain style under the signal-aware label.

## What is NOT concluded
- Seven sessions of one dataset (multi7, 2019-20), one alpha (EQ10 registered), one threshold set
  (defaults, untuned). The clusters are few; most CIs are wide.
- A refit signal (`--signal-refit`, in-sample, sign flipped) and EQ05 were NOT tested. A regime- or
  horizon-matched signal (B2's thresholds are in 1 s signal units applied to 30-minute parents) was
  not tested.
- Nothing here says signal-aware execution cannot work; it says this configuration does not meet
  the gate in-sample, so the fresh days are not spent on it.
- A future pre-registration (a new draft, a new signature) can reopen B7 with another arm.

## The fresh days
- **The nine Nasdaq / IEX overlap days were NOT used as B7 test data.** No benchmark, replay or
  signal source read them for B7. They stay available, unread by B7, to B8 (the two-venue router)
  and to future work.
- The B7 draft stays as the record with status CLOSED: not registered, not signed, not on the
  blackboard, no ledger look spent. `tools/b7_shortfall_register.py` refuses a CLOSED draft.
- The code (B2 / B3, the benchmark variants, the registration tool) stays in the platform, opt-in,
  as released in v1.17.0.
