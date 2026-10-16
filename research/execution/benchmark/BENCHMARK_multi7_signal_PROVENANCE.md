# Provenance: multi7 in-sample signal-aware benchmark (v1.17.1 B7 preparation)

- Report `BENCHMARK_multi7_signal.md` / `benchmark_multi7_signal.json` (sha256 97797df340fba9e9695bc3e3156ac530c8a682aad2116fc8b2a5c2a5dcaacbd9).
- Dataset `data/real/multi7` (git-ignored), dataset_version `ee4c2187…`, 7 in-sample sessions 2019-01-30 … 2020-01-30, 44,718,339 events, AAPL / MSFT / QQQ. No holdout or fresh day read.
- Code: worktree `wt-v1171-b7` at 73f3ddb (v1.17.0 main) when launched 2026-10-16 12:59; finished 15:30 (EXIT 0, detached via `tools/jobs/watchdog.py launch_detached`, log `data/real/b2b3_multi7_signal.log`).
- Command: `python -m iap.research.execution_benchmark --data-dir data/real/multi7 --out research/execution/benchmark --tag multi7_signal --walk-depth 50 --window-min 30 --signal EQ10 --policies native,native+urgency,native+pacing,native+ac,native+ac_drift` (driver defaults otherwise: 5 slices, POV 10 %, max child 1000, seed 20260829, child impact model).
- Signal: EQ10 registered parameters (`configs/strategies/alpha_params.json`, beta -1.04e-7, synthetic fit), scored on `multi7/features`, standardised per instrument (AAPL, MSFT; QQQ outside the universe → `no_signal`); thresholds z 1.0, passive urgency 0.1, pacing gain 0.5 (clip 0.25–3), max age 10 s; AC: sigma = price × 0.015 per √day, lambda 1e-6.
- Parents per policy: {'native': 252, 'native+ac': 63, 'native+ac_drift': 63, 'native+pacing': 252, 'native+urgency': 252}.

## Headline (paired, signal-aware − plain, all-in bps, day-clustered 95 % CI)

| arm | TWAP 1 % | TWAP 2 % | VWAP 0.5 % | IS 0.5 % | IS 1 % | POV 1 % |
|---|---|---|---|---|---|---|
| urgency − native | +1.66 bps ± 0.54 [0.35, 2.98] | +1.95 bps ± 0.48 [0.72, 3.18] | -0.59 bps ± 2.71 [-7.57, 6.38] | -0.63 bps ± 0.39 [-1.58, 0.33] | -0.47 bps ± 0.31 [-1.22, 0.28] | +0.01 bps ± 0.02 [-0.05, 0.07] |
| pacing − native | +0.89 bps ± 0.55 [-0.45, 2.23] | -0.12 bps ± 0.07 [-0.30, 0.06] | +0.78 bps ± 0.29 [0.02, 1.53] | +0.84 bps ± 0.40 [-0.13, 1.81] | +0.12 bps ± 0.28 [-0.58, 0.82] | +0.41 bps ± 0.36 [-0.48, 1.30] |
| ac_drift − ac (IS only) | — | — | — | +0.00 bps ± 0.00 [-0.00, 0.00] | +0.00 bps ± 0.00 [-0.00, 0.00] | — |

No cell reaches the 2 bp minimum effect in the cheaper direction. Urgency: IS -0.2 to -0.6 bps (n.s.), POV within 0.1 bps, TWAP/VWAP DEARER at 1-2 % ADV (+1.7 ± 0.5 and +2.0 ± 0.5 bps TWAP, +0.8 to +2.5 bps VWAP) with 6-10 pp HIGHER fill rate: the adverse-crossing arm buys completion at a price. Pacing: within ±1 bp everywhere (VWAP 0.5 % +0.8 ± 0.3). AC drift: identical to plain AC in every cell (difference 0.00): at the registered beta (|expected_return| <= 4e-7) the drift is far below the temporary-impact scale, the arm is inert. Shares: no_signal 21-33 % (QQQ outside EQ10's universe), agree 13-46 %, adverse 10-28 %. The registered EQ10 sign is opposite to its real in-sample IC, so agree/adverse may be swapped relative to the true direction; an in-sample refit arm was not run. Expected effect for the gate under this configuration: none in the cheaper direction; the fresh test is expected to FAIL unless the alpha/thresholds decision changes the arm.

Negative result, in-sample, unsigned: nothing here is a gate outcome. The draft's `expected_effect` carries the full per-cell table.
