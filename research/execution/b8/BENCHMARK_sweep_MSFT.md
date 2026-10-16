# Implementation-shortfall benchmark (v1.17 B1)

Dataset `C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\data\real\r7_20251128`, sessions 20251128, 20251208 (3469171 events). Every cost below is **replay + linear child adder (rule 6)** — costs are replay + adder: simulated fills never move the replayed book. Walk depth 10 levels.

Grid: algos TWAP, VWAP, POV, IS; policies pin_nasdaq, pin_iex, sor, sor_sweep; parent sizes 0.1, 0.5, 1, 2 % of ADV; windows of 30 min after 5 min warm-up, 5 slices, POV 10%, seed 20260829.

Per-parent means in bps of the arrival notional, ± day-clustered SE [95 % Student-t CI, cluster-robust sandwich of the per-parent mean (CR1), Student-t with G-1 df]. Cells with fewer than 10 sessions are low-power.

## Cells (pooled over symbols)

| policy | algo | size % ADV | n | sessions | all-in | shortfall | spread | fees | sim impact | completion | fill rate | maker share | depth-exhausted parents | depth-exhausted qty share of unfilled | provenance shares of filled | urgency shares of children |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| pin_nasdaq | TWAP | 0.1 | 1 | 1 | 30.27 (1 session) | 30.21 (1 session) | -0.50 (1 session) | -0.04 (1 session) | 0.00 (1 session) | 0.09 (1 session) | 0.860 | 1.000 | 0 (0%) | 0.000 | cross 67%, exec_print 33% | — |
| pin_nasdaq | TWAP | 0.5 | 2 | 2 | 9.60 ± 3.42 [-33.87, 53.08] | 9.63 ± 3.44 [-34.02, 53.28] | -0.44 ± 0.13 [-2.14, 1.26] | -0.04 ± 0.00 [-0.06, -0.02] | 0.00 ± 0.00 [0.00, 0.00] | 0.01 ± 0.01 [-0.14, 0.17] | 0.967 | 1.000 | 0 (0%) | 0.000 | cross 77%, exec_print 23% | — |
| pin_nasdaq | TWAP | 1 | 1 | 1 | 7.01 (1 session) | 6.65 (1 session) | -0.23 (1 session) | -0.02 (1 session) | 0.00 (1 session) | 0.38 (1 session) | 0.512 | 1.000 | 0 (0%) | 0.000 | cross 84%, exec_print 16% | — |
| pin_nasdaq | TWAP | 2 | 1 | 1 | 7.33 (1 session) | 7.23 (1 session) | -0.07 (1 session) | -0.01 (1 session) | 0.00 (1 session) | 0.12 (1 session) | 0.293 | 1.000 | 0 (0%) | 0.000 | cross 77%, exec_print 23% | — |
| pin_nasdaq | VWAP | 0.1 | 2 | 2 | 21.77 ± 12.17 [-132.81, 176.35] | 21.64 ± 12.09 [-132.00, 175.27] | -0.38 ± 0.02 [-0.62, -0.15] | -0.03 ± 0.00 [-0.04, -0.02] | 0.00 ± 0.00 [0.00, 0.00] | 0.16 ± 0.07 [-0.78, 1.10] | 0.748 | 1.000 | 0 (0%) | 0.000 | cross 57%, exec_print 43% | — |
| pin_nasdaq | VWAP | 0.5 | 2 | 2 | -2.28 ± 7.62 [-99.10, 94.55] | -2.28 ± 7.58 [-98.57, 94.02] | -0.35 ± 0.08 [-1.36, 0.66] | -0.04 ± 0.00 [-0.08, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 0.04 ± 0.04 [-0.45, 0.53] | 0.918 | 1.000 | 0 (0%) | 0.000 | cross 77%, exec_print 23% | — |
| pin_nasdaq | VWAP | 1 | 1 | 1 | 12.91 (1 session) | 12.55 (1 session) | -0.21 (1 session) | -0.02 (1 session) | 0.00 (1 session) | 0.37 (1 session) | 0.446 | 1.000 | 0 (0%) | 0.000 | cross 80%, exec_print 20% | — |
| pin_nasdaq | POV | 0.1 | 2 | 2 | 3.49 ± 3.99 [-47.22, 54.21] | 3.43 ± 3.99 [-47.29, 54.15] | 0.54 ± 0.04 [0.00, 1.08] | 0.06 ± 0.00 [0.06, 0.06] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 1.000 | 0.000 | 0 (0%) | n/a | aggressive_walk 100% | — |
| pin_nasdaq | POV | 0.5 | 1 | 1 | 6.10 (1 session) | 5.84 (1 session) | 0.30 (1 session) | 0.04 (1 session) | 0.00 (1 session) | 0.21 (1 session) | 0.725 | 0.000 | 0 (0%) | 0.000 | aggressive_walk 100% | — |
| pin_nasdaq | POV | 1 | 1 | 1 | -2.82 (1 session) | -3.22 (1 session) | 0.13 (1 session) | 0.02 (1 session) | 0.00 (1 session) | 0.38 (1 session) | 0.339 | 0.000 | 0 (0%) | 0.000 | aggressive_walk 100% | — |
| pin_nasdaq | IS | 0.1 | 2 | 2 | 4.72 ± 4.63 [-54.14, 63.58] | 4.44 ± 4.62 [-54.21, 63.09] | 0.31 ± 0.00 [0.29, 0.32] | 0.04 ± 0.00 [0.01, 0.06] | 0.02 ± 0.00 [0.00, 0.04] | 0.22 ± 0.02 [-0.04, 0.47] | 0.617 | 0.000 | 2 (100%) | 1.000 | aggressive_walk 100% | — |
| pin_nasdaq | IS | 0.5 | 1 | 1 | -1.84 (1 session) | -2.63 (1 session) | 0.04 (1 session) | 0.01 (1 session) | 0.01 (1 session) | 0.77 (1 session) | 0.121 | 0.000 | 1 (100%) | 1.000 | aggressive_walk 100% | — |
| pin_nasdaq | IS | 1 | 1 | 1 | -9.27 (1 session) | -9.62 (1 session) | 0.03 (1 session) | 0.01 (1 session) | 0.00 (1 session) | 0.33 (1 session) | 0.090 | 0.000 | 1 (100%) | 1.000 | aggressive_walk 100% | — |
| pin_iex | TWAP | 0.1 | 1 | 1 | 44.18 (1 session) | 43.78 (1 session) | -0.18 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.40 (1 session) | 0.415 | 1.000 | 0 (0%) | 0.000 | cross 96%, exec_print 4% | — |
| pin_iex | TWAP | 0.5 | 2 | 2 | 9.35 ± 1.61 [-11.12, 29.81] | 9.22 ± 1.65 [-11.69, 30.12] | -0.35 ± 0.17 [-2.47, 1.76] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 0.13 ± 0.03 [-0.31, 0.57] | 0.689 | 1.000 | 0 (0%) | 0.000 | cross 98%, exec_print 2% | — |
| pin_iex | TWAP | 1 | 1 | 1 | 5.21 (1 session) | 4.65 (1 session) | -0.15 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.56 (1 session) | 0.277 | 1.000 | 0 (0%) | 0.000 | cross 100%, exec_print 0% | — |
| pin_iex | TWAP | 2 | 1 | 1 | 8.38 (1 session) | 8.24 (1 session) | -0.03 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.14 (1 session) | 0.123 | 1.000 | 0 (0%) | 0.000 | cross 97%, exec_print 3% | — |
| pin_iex | VWAP | 0.1 | 2 | 2 | 32.73 ± 22.69 [-255.52, 320.98] | 32.37 ± 22.43 [-252.58, 317.32] | -0.27 ± 0.11 [-1.65, 1.10] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 0.36 ± 0.26 [-2.94, 3.66] | 0.517 | 1.000 | 0 (0%) | 0.000 | cross 97%, exec_print 3% | — |
| pin_iex | VWAP | 0.5 | 2 | 2 | -3.62 ± 9.23 [-120.87, 113.62] | -3.78 ± 9.15 [-120.08, 112.51] | -0.25 ± 0.03 [-0.60, 0.11] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 0.16 ± 0.07 [-0.78, 1.11] | 0.625 | 1.000 | 0 (0%) | 0.000 | cross 97%, exec_print 3% | — |
| pin_iex | VWAP | 1 | 1 | 1 | 13.86 (1 session) | 13.30 (1 session) | -0.10 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.55 (1 session) | 0.179 | 1.000 | 0 (0%) | 0.000 | cross 99%, exec_print 1% | — |
| pin_iex | POV | 0.1 | 2 | 2 | 48.43 ± 9.71 [-74.99, 171.84] | 48.40 ± 9.72 [-75.14, 171.93] | 0.55 ± 0.02 [0.27, 0.84] | 0.02 ± 0.00 [0.02, 0.02] | 0.01 ± 0.01 [-0.10, 0.13] | 0.00 ± 0.00 [0.00, 0.00] | 1.000 | 0.000 | 2 (100%) | n/a | aggressive_walk 100% | — |
| pin_iex | POV | 0.5 | 1 | 1 | 8.51 (1 session) | 8.24 (1 session) | 0.33 (1 session) | 0.01 (1 session) | 0.01 (1 session) | 0.25 (1 session) | 0.675 | 0.000 | 1 (100%) | 125.259 | aggressive_walk 100% | — |
| pin_iex | POV | 1 | 1 | 1 | -2.72 (1 session) | -3.12 (1 session) | 0.14 (1 session) | 0.01 (1 session) | 0.00 (1 session) | 0.38 (1 session) | 0.330 | 0.000 | 1 (100%) | 11.277 | aggressive_walk 100% | — |
| pin_iex | IS | 0.1 | 2 | 2 | 45.69 ± 18.30 [-186.79, 278.16] | 45.17 ± 18.31 [-187.54, 277.87] | 0.05 ± 0.02 [-0.24, 0.33] | 0.00 ± 0.00 [-0.01, 0.01] | 0.00 ± 0.00 [-0.02, 0.03] | 0.51 ± 0.02 [0.25, 0.77] | 0.100 | 0.000 | 2 (100%) | 1.000 | aggressive_walk 100% | — |
| pin_iex | IS | 0.5 | 1 | 1 | -3.14 (1 session) | -4.00 (1 session) | 0.01 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.86 (1 session) | 0.018 | 0.000 | 1 (100%) | 1.000 | aggressive_walk 100% | — |
| pin_iex | IS | 1 | 1 | 1 | -9.96 (1 session) | -10.33 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.37 (1 session) | 0.006 | 0.000 | 1 (100%) | 1.000 | aggressive_walk 100% | — |
| sor | TWAP | 0.1 | 1 | 1 | 35.29 (1 session) | 35.08 (1 session) | -0.36 (1 session) | -0.02 (1 session) | 0.00 (1 session) | 0.23 (1 session) | 0.660 | 1.000 | 0 (0%) | 0.000 | cross 64%, exec_print 36% | — |
| sor | TWAP | 0.5 | 2 | 2 | 9.18 ± 3.00 [-28.99, 47.36] | 9.20 ± 3.01 [-29.01, 47.42] | -0.44 ± 0.14 [-2.19, 1.31] | -0.04 ± 0.00 [-0.07, -0.00] | 0.00 ± 0.00 [0.00, 0.00] | 0.02 ± 0.01 [-0.06, 0.09] | 0.956 | 1.000 | 0 (0%) | 0.000 | cross 80%, exec_print 20% | — |
| sor | TWAP | 1 | 1 | 1 | 7.01 (1 session) | 6.65 (1 session) | -0.23 (1 session) | -0.02 (1 session) | 0.00 (1 session) | 0.38 (1 session) | 0.512 | 1.000 | 0 (0%) | 0.000 | cross 84%, exec_print 16% | — |
| sor | TWAP | 2 | 1 | 1 | 7.79 (1 session) | 7.67 (1 session) | -0.05 (1 session) | -0.01 (1 session) | 0.00 (1 session) | 0.12 (1 session) | 0.249 | 1.000 | 0 (0%) | 0.000 | cross 75%, exec_print 25% | — |
| sor | VWAP | 0.1 | 2 | 2 | 32.56 ± 22.96 [-259.15, 324.27] | 32.23 ± 22.68 [-255.95, 320.41] | -0.28 ± 0.08 [-1.34, 0.77] | -0.02 ± 0.01 [-0.18, 0.14] | 0.00 ± 0.00 [0.00, 0.00] | 0.35 ± 0.27 [-3.02, 3.72] | 0.532 | 1.000 | 0 (0%) | 0.000 | cross 68%, exec_print 32% | — |
| sor | VWAP | 0.5 | 2 | 2 | -3.64 ± 9.02 [-118.27, 111.00] | -3.73 ± 8.99 [-117.95, 110.50] | -0.28 ± 0.06 [-1.02, 0.46] | -0.02 ± 0.00 [-0.05, 0.01] | 0.00 ± 0.00 [0.00, 0.00] | 0.11 ± 0.04 [-0.34, 0.56] | 0.748 | 1.000 | 0 (0%) | 0.000 | cross 82%, exec_print 18% | — |
| sor | VWAP | 1 | 1 | 1 | 13.20 (1 session) | 12.69 (1 session) | -0.11 (1 session) | -0.01 (1 session) | 0.00 (1 session) | 0.52 (1 session) | 0.223 | 1.000 | 0 (0%) | 0.000 | cross 90%, exec_print 10% | — |
| sor | POV | 0.1 | 2 | 2 | 8.77 ± 8.74 [-102.29, 119.83] | 8.72 ± 8.74 [-102.34, 119.77] | 0.54 ± 0.04 [0.00, 1.08] | 0.05 ± 0.00 [0.05, 0.06] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 1.000 | 0.000 | 0 (0%) | n/a | aggressive_walk 100% | — |
| sor | POV | 0.5 | 1 | 1 | 10.46 (1 session) | 10.21 (1 session) | 0.30 (1 session) | 0.04 (1 session) | 0.00 (1 session) | 0.21 (1 session) | 0.725 | 0.000 | 1 (100%) | 0.359 | aggressive_walk 100% | — |
| sor | POV | 1 | 1 | 1 | -2.76 (1 session) | -3.16 (1 session) | 0.13 (1 session) | 0.02 (1 session) | 0.00 (1 session) | 0.38 (1 session) | 0.339 | 0.000 | 1 (100%) | 0.206 | aggressive_walk 100% | — |
| sor | IS | 0.1 | 2 | 2 | 30.17 ± 20.82 [-234.34, 294.69] | 29.89 ± 20.84 [-234.85, 294.64] | 0.31 ± 0.00 [0.29, 0.32] | 0.04 ± 0.00 [0.03, 0.04] | 0.02 ± 0.00 [0.00, 0.04] | 0.22 ± 0.02 [-0.04, 0.47] | 0.617 | 0.000 | 2 (100%) | 1.000 | aggressive_walk 100% | — |
| sor | IS | 0.5 | 1 | 1 | -0.91 (1 session) | -1.71 (1 session) | 0.04 (1 session) | 0.01 (1 session) | 0.00 (1 session) | 0.79 (1 session) | 0.099 | 0.000 | 1 (100%) | 1.000 | aggressive_walk 100% | — |
| sor | IS | 1 | 1 | 1 | -9.59 (1 session) | -9.95 (1 session) | 0.02 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.35 (1 session) | 0.058 | 0.000 | 1 (100%) | 1.000 | aggressive_walk 100% | — |
| sor_sweep | TWAP | 0.1 | 1 | 1 | 35.29 (1 session) | 35.08 (1 session) | -0.36 (1 session) | -0.02 (1 session) | 0.00 (1 session) | 0.23 (1 session) | 0.660 | 1.000 | 0 (0%) | 0.000 | cross 64%, exec_print 36% | — |
| sor_sweep | TWAP | 0.5 | 2 | 2 | 9.18 ± 3.00 [-28.99, 47.36] | 9.20 ± 3.01 [-29.01, 47.42] | -0.44 ± 0.14 [-2.19, 1.31] | -0.04 ± 0.00 [-0.07, -0.00] | 0.00 ± 0.00 [0.00, 0.00] | 0.02 ± 0.01 [-0.06, 0.09] | 0.956 | 1.000 | 0 (0%) | 0.000 | cross 80%, exec_print 20% | — |
| sor_sweep | TWAP | 1 | 1 | 1 | 7.01 (1 session) | 6.65 (1 session) | -0.23 (1 session) | -0.02 (1 session) | 0.00 (1 session) | 0.38 (1 session) | 0.512 | 1.000 | 0 (0%) | 0.000 | cross 84%, exec_print 16% | — |
| sor_sweep | TWAP | 2 | 1 | 1 | 7.79 (1 session) | 7.67 (1 session) | -0.05 (1 session) | -0.01 (1 session) | 0.00 (1 session) | 0.12 (1 session) | 0.249 | 1.000 | 0 (0%) | 0.000 | cross 75%, exec_print 25% | — |
| sor_sweep | VWAP | 0.1 | 2 | 2 | 32.56 ± 22.96 [-259.15, 324.27] | 32.23 ± 22.68 [-255.95, 320.41] | -0.28 ± 0.08 [-1.34, 0.77] | -0.02 ± 0.01 [-0.18, 0.14] | 0.00 ± 0.00 [0.00, 0.00] | 0.35 ± 0.27 [-3.02, 3.72] | 0.532 | 1.000 | 0 (0%) | 0.000 | cross 68%, exec_print 32% | — |
| sor_sweep | VWAP | 0.5 | 2 | 2 | -3.64 ± 9.02 [-118.27, 111.00] | -3.73 ± 8.99 [-117.95, 110.50] | -0.28 ± 0.06 [-1.02, 0.46] | -0.02 ± 0.00 [-0.05, 0.01] | 0.00 ± 0.00 [0.00, 0.00] | 0.11 ± 0.04 [-0.34, 0.56] | 0.748 | 1.000 | 0 (0%) | 0.000 | cross 82%, exec_print 18% | — |
| sor_sweep | VWAP | 1 | 1 | 1 | 13.20 (1 session) | 12.69 (1 session) | -0.11 (1 session) | -0.01 (1 session) | 0.00 (1 session) | 0.52 (1 session) | 0.223 | 1.000 | 0 (0%) | 0.000 | cross 90%, exec_print 10% | — |
| sor_sweep | POV | 0.1 | 2 | 2 | 3.49 ± 3.99 [-47.27, 54.24] | 3.43 ± 3.99 [-47.31, 54.17] | 0.54 ± 0.04 [-0.01, 1.08] | 0.06 ± 0.00 [0.04, 0.07] | 0.00 ± 0.00 [-0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 1.000 | 0.000 | 0 (0%) | n/a | aggressive_walk 100% | — |
| sor_sweep | POV | 0.5 | 1 | 1 | 6.11 (1 session) | 5.86 (1 session) | 0.30 (1 session) | 0.04 (1 session) | 0.00 (1 session) | 0.21 (1 session) | 0.725 | 0.000 | 0 (0%) | 0.000 | aggressive_walk 100% | — |
| sor_sweep | POV | 1 | 1 | 1 | -2.82 (1 session) | -3.21 (1 session) | 0.13 (1 session) | 0.02 (1 session) | 0.00 (1 session) | 0.38 (1 session) | 0.339 | 0.000 | 0 (0%) | 0.000 | aggressive_walk 100% | — |
| sor_sweep | IS | 0.1 | 2 | 2 | 68.68 ± 20.96 [-197.68, 335.04] | 68.46 ± 20.98 [-198.12, 335.05] | 0.35 ± 0.01 [0.27, 0.43] | 0.04 ± 0.00 [0.01, 0.07] | 0.02 ± 0.00 [0.00, 0.03] | 0.16 ± 0.02 [-0.11, 0.43] | 0.721 | 0.000 | 1 (50%) | 0.158 | aggressive_walk 100% | — |
| sor_sweep | IS | 0.5 | 1 | 1 | -1.48 (1 session) | -2.25 (1 session) | 0.05 (1 session) | 0.01 (1 session) | 0.00 (1 session) | 0.75 (1 session) | 0.139 | 0.000 | 1 (100%) | 0.471 | aggressive_walk 100% | — |
| sor_sweep | IS | 1 | 1 | 1 | -9.23 (1 session) | -9.58 (1 session) | 0.04 (1 session) | 0.01 (1 session) | 0.00 (1 session) | 0.33 (1 session) | 0.093 | 0.000 | 1 (100%) | 0.305 | aggressive_walk 100% | — |

## Paired (same windows; `other − control`, bps; baseline `pin_nasdaq`, signal-aware arms also against their own plain control)

| comparison | algo | size % ADV | pairs | all-in diff | shortfall diff | completion diff | fill-rate diff |
|---|---|---|---|---|---|---|---|
| pin_iex-pin_nasdaq | TWAP | 0.1 | 1 | 13.91 (1 session) | 13.57 (1 session) | 0.30 (1 session) | -0.44 (1 session) |
| pin_iex-pin_nasdaq | TWAP | 0.5 | 2 | -0.25 ± 1.81 [-23.27, 22.76] | -0.41 ± 1.79 [-23.15, 22.33] | 0.12 ± 0.02 [-0.17, 0.41] | -0.28 ± 0.11 [-1.67, 1.11] |
| pin_iex-pin_nasdaq | TWAP | 1 | 1 | -1.80 (1 session) | -2.00 (1 session) | 0.18 (1 session) | -0.24 (1 session) |
| pin_iex-pin_nasdaq | TWAP | 2 | 1 | 1.04 (1 session) | 1.00 (1 session) | 0.03 (1 session) | -0.17 (1 session) |
| pin_iex-pin_nasdaq | VWAP | 0.1 | 2 | 10.96 ± 10.52 [-122.71, 144.63] | 10.73 ± 10.33 [-120.58, 142.05] | 0.20 ± 0.19 [-2.16, 2.56] | -0.23 ± 0.20 [-2.80, 2.33] |
| pin_iex-pin_nasdaq | VWAP | 0.5 | 2 | -1.34 ± 1.61 [-21.76, 19.07] | -1.51 ± 1.57 [-21.51, 18.50] | 0.12 ± 0.04 [-0.33, 0.58] | -0.29 ± 0.05 [-0.93, 0.35] |
| pin_iex-pin_nasdaq | VWAP | 1 | 1 | 0.95 (1 session) | 0.75 (1 session) | 0.18 (1 session) | -0.27 (1 session) |
| pin_iex-pin_nasdaq | POV | 0.1 | 2 | 44.93 ± 13.70 [-129.20, 219.07] | 44.96 ± 13.71 [-129.29, 219.21] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] |
| pin_iex-pin_nasdaq | POV | 0.5 | 1 | 2.41 (1 session) | 2.40 (1 session) | 0.04 (1 session) | -0.05 (1 session) |
| pin_iex-pin_nasdaq | POV | 1 | 1 | 0.10 (1 session) | 0.11 (1 session) | 0.01 (1 session) | -0.01 (1 session) |
| pin_iex-pin_nasdaq | IS | 0.1 | 2 | 40.97 ± 13.66 [-132.65, 214.58] | 40.73 ± 13.70 [-133.33, 214.78] | 0.30 ± 0.04 [-0.22, 0.81] | -0.52 ± 0.07 [-1.43, 0.40] |
| pin_iex-pin_nasdaq | IS | 0.5 | 1 | -1.30 (1 session) | -1.38 (1 session) | 0.09 (1 session) | -0.10 (1 session) |
| pin_iex-pin_nasdaq | IS | 1 | 1 | -0.69 (1 session) | -0.71 (1 session) | 0.03 (1 session) | -0.08 (1 session) |
| sor-pin_nasdaq | TWAP | 0.1 | 1 | 5.03 (1 session) | 4.87 (1 session) | 0.14 (1 session) | -0.20 (1 session) |
| sor-pin_nasdaq | TWAP | 0.5 | 2 | -0.42 ± 0.42 [-5.72, 4.89] | -0.43 ± 0.43 [-5.86, 5.01] | 0.01 ± 0.01 [-0.07, 0.08] | -0.01 ± 0.01 [-0.15, 0.13] |
| sor-pin_nasdaq | TWAP | 1 | 1 | 0.00 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| sor-pin_nasdaq | TWAP | 2 | 1 | 0.45 (1 session) | 0.44 (1 session) | 0.01 (1 session) | -0.04 (1 session) |
| sor-pin_nasdaq | VWAP | 0.1 | 2 | 10.79 ± 10.79 [-126.34, 147.92] | 10.59 ± 10.59 [-123.96, 145.13] | 0.19 ± 0.19 [-2.24, 2.62] | -0.22 ± 0.22 [-2.97, 2.54] |
| sor-pin_nasdaq | VWAP | 0.5 | 2 | -1.36 ± 1.40 [-19.17, 16.45] | -1.45 ± 1.41 [-19.37, 16.48] | 0.07 ± 0.00 [0.03, 0.11] | -0.17 ± 0.03 [-0.53, 0.19] |
| sor-pin_nasdaq | VWAP | 1 | 1 | 0.29 (1 session) | 0.13 (1 session) | 0.15 (1 session) | -0.22 (1 session) |
| sor-pin_nasdaq | POV | 0.1 | 2 | 5.28 ± 4.75 [-55.07, 65.62] | 5.28 ± 4.75 [-55.05, 65.62] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] |
| sor-pin_nasdaq | POV | 0.5 | 1 | 4.36 (1 session) | 4.37 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| sor-pin_nasdaq | POV | 1 | 1 | 0.06 (1 session) | 0.07 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| sor-pin_nasdaq | IS | 0.1 | 2 | 25.45 ± 25.45 [-297.93, 348.83] | 25.45 ± 25.45 [-297.95, 348.85] | 0.00 ± 0.00 [-0.00, 0.00] | -0.00 ± 0.00 [-0.00, 0.00] |
| sor-pin_nasdaq | IS | 0.5 | 1 | 0.93 (1 session) | 0.91 (1 session) | 0.02 (1 session) | -0.02 (1 session) |
| sor-pin_nasdaq | IS | 1 | 1 | -0.32 (1 session) | -0.33 (1 session) | 0.01 (1 session) | -0.03 (1 session) |
| sor_sweep-pin_nasdaq | TWAP | 0.1 | 1 | 5.03 (1 session) | 4.87 (1 session) | 0.14 (1 session) | -0.20 (1 session) |
| sor_sweep-pin_nasdaq | TWAP | 0.5 | 2 | -0.42 ± 0.42 [-5.72, 4.89] | -0.43 ± 0.43 [-5.86, 5.01] | 0.01 ± 0.01 [-0.07, 0.08] | -0.01 ± 0.01 [-0.15, 0.13] |
| sor_sweep-pin_nasdaq | TWAP | 1 | 1 | 0.00 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| sor_sweep-pin_nasdaq | TWAP | 2 | 1 | 0.45 (1 session) | 0.44 (1 session) | 0.01 (1 session) | -0.04 (1 session) |
| sor_sweep-pin_nasdaq | VWAP | 0.1 | 2 | 10.79 ± 10.79 [-126.34, 147.92] | 10.59 ± 10.59 [-123.96, 145.13] | 0.19 ± 0.19 [-2.24, 2.62] | -0.22 ± 0.22 [-2.97, 2.54] |
| sor_sweep-pin_nasdaq | VWAP | 0.5 | 2 | -1.36 ± 1.40 [-19.17, 16.45] | -1.45 ± 1.41 [-19.37, 16.48] | 0.07 ± 0.00 [0.03, 0.11] | -0.17 ± 0.03 [-0.53, 0.19] |
| sor_sweep-pin_nasdaq | VWAP | 1 | 1 | 0.29 (1 session) | 0.13 (1 session) | 0.15 (1 session) | -0.22 (1 session) |
| sor_sweep-pin_nasdaq | POV | 0.1 | 2 | -0.01 ± 0.00 [-0.05, 0.03] | -0.00 ± 0.00 [-0.02, 0.02] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] |
| sor_sweep-pin_nasdaq | POV | 0.5 | 1 | 0.01 (1 session) | 0.01 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| sor_sweep-pin_nasdaq | POV | 1 | 1 | 0.01 (1 session) | 0.01 (1 session) | 0.00 (1 session) | -0.00 (1 session) |
| sor_sweep-pin_nasdaq | IS | 0.1 | 2 | 63.96 ± 25.60 [-261.26, 389.18] | 64.02 ± 25.60 [-261.21, 389.26] | -0.06 ± 0.00 [-0.07, -0.05] | 0.10 ± 0.00 [0.08, 0.13] |
| sor_sweep-pin_nasdaq | IS | 0.5 | 1 | 0.36 (1 session) | 0.38 (1 session) | -0.02 (1 session) | 0.02 (1 session) |
| sor_sweep-pin_nasdaq | IS | 1 | 1 | 0.04 (1 session) | 0.04 (1 session) | -0.00 (1 session) | 0.00 (1 session) |

## Markout by fill type and regime (v1.17 B6; bps, ± day-clustered SE)

| policy | cell | fills | qty | markout 1s | markout 5s | markout 30s | price impact 1s | price impact 5s | price impact 30s |
|---|---|---|---|---|---|---|---|---|---|
| pin_nasdaq | all | 16805 | 217024 | -1.27 ± 0.11 [-2.70, 0.15] | -1.29 ± 0.20 [-3.79, 1.20] | -1.11 ± 0.36 [-5.66, 3.45] | -0.08 ± 0.03 [-0.48, 0.32] | -0.10 ± 0.12 [-1.57, 1.38] | 0.09 ± 0.28 [-3.43, 3.62] |
| pin_nasdaq | type passive | 5545 | 164278 | -2.75 ± 0.29 [-6.42, 0.92] | -2.84 ± 0.18 [-5.17, -0.51] | -2.70 ± 0.21 [-5.36, -0.05] | -0.20 ± 0.02 [-0.43, 0.04] | -0.29 ± 0.12 [-1.86, 1.29] | -0.14 ± 0.52 [-6.70, 6.41] |
| pin_nasdaq | type aggressive | 11260 | 52746 | -0.55 ± 0.04 [-1.04, -0.05] | -0.53 ± 0.10 [-1.81, 0.74] | -0.32 ± 0.14 [-2.10, 1.46] | -0.02 ± 0.02 [-0.22, 0.18] | -0.00 ± 0.08 [-0.98, 0.98] | 0.21 ± 0.12 [-1.27, 1.69] |
| pin_nasdaq | regime plain | 16805 | 217024 | -1.27 ± 0.11 [-2.70, 0.15] | -1.29 ± 0.20 [-3.79, 1.20] | -1.11 ± 0.36 [-5.66, 3.45] | -0.08 ± 0.03 [-0.48, 0.32] | -0.10 ± 0.12 [-1.57, 1.38] | 0.09 ± 0.28 [-3.43, 3.62] |
| pin_iex | all | 4776 | 137253 | -23.06 ± 5.92 [-98.33, 52.21] | -23.17 ± 6.00 [-99.34, 53.01] | -23.06 ± 6.14 [-101.03, 54.92] | -0.08 ± 0.01 [-0.15, -0.00] | -0.18 ± 0.07 [-1.01, 0.65] | -0.07 ± 0.21 [-2.70, 2.55] |
| pin_iex | type passive | 2264 | 96792 | -2.98 ± 0.05 [-3.57, -2.39] | -3.13 ± 0.08 [-4.09, -2.16] | -3.30 ± 0.23 [-6.18, -0.42] | -0.13 ± 0.03 [-0.47, 0.22] | -0.27 ± 0.00 [-0.31, -0.23] | -0.45 ± 0.15 [-2.40, 1.50] |
| pin_iex | type aggressive | 2512 | 40461 | -41.16 ± 17.87 [-268.26, 185.95] | -41.23 ± 17.97 [-269.54, 187.09] | -40.86 ± 18.01 [-269.67, 187.95] | -0.03 ± 0.00 [-0.07, 0.00] | -0.10 ± 0.09 [-1.27, 1.07] | 0.26 ± 0.13 [-1.40, 1.93] |
| pin_iex | regime plain | 4776 | 137253 | -23.06 ± 5.92 [-98.33, 52.21] | -23.17 ± 6.00 [-99.34, 53.01] | -23.06 ± 6.14 [-101.03, 54.92] | -0.08 ± 0.01 [-0.15, -0.00] | -0.18 ± 0.07 [-1.01, 0.65] | -0.07 ± 0.21 [-2.70, 2.55] |
| sor | all | 15043 | 190121 | -3.97 ± 0.17 [-6.18, -1.76] | -4.00 ± 0.12 [-5.49, -2.51] | -3.79 ± 0.00 [-3.84, -3.74] | -0.07 ± 0.02 [-0.37, 0.24] | -0.10 ± 0.08 [-1.12, 0.93] | 0.12 ± 0.20 [-2.45, 2.68] |
| sor | type passive | 4414 | 139345 | -2.62 ± 0.15 [-4.48, -0.77] | -2.74 ± 0.13 [-4.44, -1.05] | -2.63 ± 0.11 [-4.00, -1.27] | -0.17 ± 0.00 [-0.21, -0.13] | -0.29 ± 0.02 [-0.49, -0.09] | -0.18 ± 0.26 [-3.44, 3.08] |
| sor | type aggressive | 10629 | 50776 | -4.53 ± 0.03 [-4.96, -4.11] | -4.53 ± 0.09 [-5.73, -3.33] | -4.27 ± 0.15 [-6.17, -2.38] | -0.02 ± 0.02 [-0.22, 0.17] | -0.02 ± 0.08 [-0.99, 0.96] | 0.24 ± 0.13 [-1.43, 1.90] |
| sor | regime plain | 15043 | 190121 | -3.97 ± 0.17 [-6.18, -1.76] | -4.00 ± 0.12 [-5.49, -2.51] | -3.79 ± 0.00 [-3.84, -3.74] | -0.07 ± 0.02 [-0.37, 0.24] | -0.10 ± 0.08 [-1.12, 0.93] | 0.12 ± 0.20 [-2.45, 2.68] |
| sor_sweep | all | 13747 | 193547 | -2.27 ± 1.08 [-15.95, 11.41] | -2.31 ± 1.13 [-16.67, 12.06] | -2.13 ± 1.25 [-18.04, 13.77] | -0.08 ± 0.02 [-0.38, 0.22] | -0.11 ± 0.08 [-1.09, 0.87] | 0.06 ± 0.20 [-2.46, 2.58] |
| sor_sweep | type passive | 4414 | 139345 | -2.62 ± 0.15 [-4.48, -0.77] | -2.74 ± 0.13 [-4.44, -1.05] | -2.63 ± 0.11 [-4.00, -1.27] | -0.17 ± 0.00 [-0.21, -0.13] | -0.29 ± 0.02 [-0.49, -0.09] | -0.18 ± 0.26 [-3.44, 3.08] |
| sor_sweep | type aggressive | 9333 | 54202 | -2.11 ± 1.59 [-22.29, 18.08] | -2.10 ± 1.65 [-23.02, 18.82] | -1.90 ± 1.70 [-23.49, 19.69] | -0.04 ± 0.02 [-0.24, 0.16] | -0.03 ± 0.07 [-0.97, 0.90] | 0.17 ± 0.13 [-1.43, 1.77] |
| sor_sweep | regime plain | 13747 | 193547 | -2.27 ± 1.08 [-15.95, 11.41] | -2.31 ± 1.13 [-16.67, 12.06] | -2.13 ± 1.25 [-18.04, 13.77] | -0.08 ± 0.02 [-0.38, 0.22] | -0.11 ± 0.08 [-1.09, 0.87] | 0.06 ± 0.20 [-2.46, 2.58] |

## Two venues: router vs pinned (v1.17 B8)

Second dataset `C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\data\real\iex\iex_20251128` (IEX clock `exchange`); alignment: both streams on ns since the session's New York midnight, exchange (UTC) clock; Nasdaq ITCH as stamped; IEX: exchange_ts from the messages' own UTC send times (iex_clock='exchange', no shift) or, for a capture-clock (TAI) stream, exchange_ts/receive_ts - 37 s (iex_clock='capture'); merge order (exchange_ts, venue_id), each stream's own order kept inside a tie. Routes: `pin_iex` -> IEXG, `pin_nasdaq` -> XNAS, `sor` -> sor_v2 router, `sor_sweep` -> sor_v2 router. Router calibration: `research\execution\b8\calibration`.

| policy | filled qty | venue share | markout 1s by venue | markout 5s by venue | markout 30s by venue |
|---|---:|---|---|---|---|
| pin_nasdaq | 217024 | XNAS 100.0% | XNAS -1.27 ± 0.11 [-2.70, 0.15] | XNAS -1.29 ± 0.20 [-3.79, 1.20] | XNAS -1.11 ± 0.36 [-5.66, 3.45] |
| pin_iex | 137253 | IEXG 100.0% | IEXG -23.06 ± 5.92 [-98.33, 52.21] | IEXG -23.17 ± 6.00 [-99.34, 53.01] | IEXG -23.06 ± 6.14 [-101.03, 54.92] |
| sor | 190121 | IEXG 15.9%, XNAS 84.1% | IEXG -15.60 ± 0.95 [-27.67, -3.53]; XNAS -1.18 ± 0.15 [-3.02, 0.66] | IEXG -15.64 ± 0.93 [-27.40, -3.87]; XNAS -1.21 ± 0.21 [-3.87, 1.46] | IEXG -15.32 ± 0.93 [-27.08, -3.55]; XNAS -1.02 ± 0.36 [-5.58, 3.53] |
| sor_sweep | 193547 | IEXG 15.1%, XNAS 84.9% | IEXG -7.33 ± 5.51 [-77.30, 62.64]; XNAS -1.21 ± 0.16 [-3.21, 0.78] | IEXG -7.37 ± 5.52 [-77.55, 62.80]; XNAS -1.25 ± 0.22 [-4.02, 1.53] | IEXG -7.26 ± 5.52 [-77.41, 62.89]; XNAS -1.06 ± 0.37 [-5.70, 3.58] |

Pooled over every cell (same parents, `other - baseline`, bps; ± day-clustered SE):

| pair | n | all-in | shortfall | spread | fees net | fill rate diff |
|---|---:|---|---|---|---|---|
| pin_iex-pin_nasdaq | 18 | 11.40 ± 3.39 [-31.68, 54.47] | 11.26 ± 3.38 [-31.64, 54.17] | 0.04 ± 0.00 [0.02, 0.05] | 0.00 ± 0.00 [-0.05, 0.05] | -0.22 ± 0.04 [-0.73, 0.28] |
| sor-pin_nasdaq | 18 | 5.02 ± 2.62 [-28.29, 38.33] | 4.96 ± 2.63 [-28.45, 38.38] | 0.03 ± 0.00 [0.02, 0.04] | 0.00 ± 0.00 [-0.00, 0.01] | -0.07 ± 0.00 [-0.13, -0.02] |
| sor_sweep-pin_nasdaq | 18 | 8.45 ± 4.57 [-49.57, 66.48] | 8.41 ± 4.58 [-49.73, 66.54] | 0.04 ± 0.00 [0.03, 0.05] | 0.00 ± 0.00 [-0.00, 0.01] | -0.06 ± 0.01 [-0.13, 0.02] |

Shortfall, fill rate and the paired router-vs-pinned differences are in the cells and paired tables above (same parents and windows for every route).

## Reading it

- `all_in = shortfall + fees_net + sim_impact + completion` per parent; the unfilled quantity is priced at the mid move and the cost of crossing at the window end, so not trading is never free.
- `sim_impact` is the configured adder (replay + linear child adder (rule 6)), not a measured reaction; the replayed book does not move for our fills.
- `depth-exhausted` (X-5): aggressive children cancelled because the walk ran out of displayed depth within the configured levels — the silent under-fill on thin names.
- provenance (X-7): which simulator rule produced the filled quantity (aggressive_walk, exec_print, marketable_add, hidden_print, cross, reopen_touch).
- Standard errors cluster by session; with few sessions the Student-t CI is wide by design and `low_power` is set in the JSON.
- urgency shares (B2 / B3): why each child of a signal-aware parent got its urgency (agree = posted on a favourable signal, adverse = crossed on an adverse one, neutral = inside the threshold, no_signal / stale = no usable reading, plain = a pacing-only arm).
- fades saved vs fills missed (B4 / B5): for every quantity a rule pulled, whether the market traded THROUGH the pulled price inside the horizon (the proxy for 'it would have filled') and the markout of that forgone fill — `saved` = it would have been adverse, `missed` = it would have been favourable; `lost race` = the fill landed before the cancel arrived (simulator rule 7). A counterfactual on a book our order never moved.
- markout by fill type (B6): passive = a first post that rested and filled, aggressive = a taker fill, faded = a maker fill that landed after a pull cancel was sent, cancelled_replaced = a maker fill of a child re-sent after a cancel; regimes are the B2 urgency regimes (plain without a signal). SEs cluster by session.
- dark (B10): fill type `dark` and every dark row above are MODELLED, NOT MEASURED — a logistic fill probability fitted on Nasdaq inside-spread hidden prints as the proxy (no dark-pool data exists); the negated price impact of the `dark` type is the adverse selection of those modelled fills.
