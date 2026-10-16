# Implementation-shortfall benchmark (v1.17 B1)

Dataset `C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\data\real\r7_20251128`, sessions 20251128, 20251208 (6632883 events). Every cost below is **replay + linear child adder (rule 6)** — costs are replay + adder: simulated fills never move the replayed book. Walk depth 10 levels.

Grid: algos TWAP, VWAP, POV, IS; policies pin_nasdaq, pin_iex, sor, sor_sweep; parent sizes 0.1, 0.5, 1, 2 % of ADV; windows of 30 min after 5 min warm-up, 5 slices, POV 10%, seed 20260829.

Per-parent means in bps of the arrival notional, ± day-clustered SE [95 % Student-t CI, cluster-robust sandwich of the per-parent mean (CR1), Student-t with G-1 df]. Cells with fewer than 10 sessions are low-power.

## Cells (pooled over symbols)

| policy | algo | size % ADV | n | sessions | all-in | shortfall | spread | fees | sim impact | completion | fill rate | maker share | depth-exhausted parents | depth-exhausted qty share of unfilled | provenance shares of filled | urgency shares of children |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| pin_nasdaq | TWAP | 0.1 | 1 | 1 | -19.23 (1 session) | -19.16 (1 session) | -0.72 (1 session) | -0.07 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 1.000 | 1.000 | 0 (0%) | n/a | cross 44%, exec_print 56% | — |
| pin_nasdaq | TWAP | 0.5 | 2 | 2 | -0.86 ± 9.19 [-117.66, 115.93] | -0.87 ± 9.12 [-116.81, 115.07] | -0.28 ± 0.06 [-1.11, 0.55] | -0.06 ± 0.01 [-0.23, 0.12] | 0.00 ± 0.00 [0.00, 0.00] | 0.06 ± 0.05 [-0.62, 0.74] | 0.793 | 1.000 | 0 (0%) | 0.000 | cross 79%, exec_print 21% | — |
| pin_nasdaq | TWAP | 1 | 1 | 1 | -14.71 (1 session) | -14.78 (1 session) | -0.28 (1 session) | -0.06 (1 session) | 0.00 (1 session) | 0.13 (1 session) | 0.797 | 1.000 | 0 (0%) | 0.000 | cross 79%, exec_print 21% | — |
| pin_nasdaq | TWAP | 2 | 1 | 1 | 13.61 (1 session) | 13.39 (1 session) | -0.05 (1 session) | -0.01 (1 session) | 0.00 (1 session) | 0.23 (1 session) | 0.203 | 1.000 | 0 (0%) | 0.000 | cross 73%, exec_print 27% | — |
| pin_nasdaq | VWAP | 0.1 | 2 | 2 | -18.02 ± 16.45 [-227.02, 190.99] | -17.94 ± 16.45 [-226.95, 191.06] | -0.47 ± 0.02 [-0.71, -0.23] | -0.07 ± 0.00 [-0.08, -0.07] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 1.000 | 1.000 | 0 (0%) | n/a | cross 51%, exec_print 49% | — |
| pin_nasdaq | VWAP | 0.5 | 2 | 2 | 4.52 ± 2.53 [-27.57, 36.60] | 4.52 ± 2.50 [-27.25, 36.30] | -0.24 ± 0.00 [-0.27, -0.21] | -0.06 ± 0.00 [-0.09, -0.04] | 0.00 ± 0.00 [0.00, 0.00] | 0.06 ± 0.02 [-0.23, 0.34] | 0.860 | 1.000 | 0 (0%) | 0.000 | cross 74%, exec_print 26% | — |
| pin_nasdaq | VWAP | 1 | 1 | 1 | 7.91 (1 session) | 7.68 (1 session) | -0.15 (1 session) | -0.03 (1 session) | 0.00 (1 session) | 0.26 (1 session) | 0.441 | 1.000 | 0 (0%) | 0.000 | cross 78%, exec_print 22% | — |
| pin_nasdaq | POV | 0.1 | 2 | 2 | -2.56 ± 3.30 [-44.44, 39.32] | -2.67 ± 3.30 [-44.54, 39.21] | 0.41 ± 0.01 [0.22, 0.60] | 0.11 ± 0.00 [0.11, 0.11] | 0.00 ± 0.00 [-0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 1.000 | 0.000 | 0 (0%) | n/a | aggressive_walk 100% | — |
| pin_nasdaq | POV | 0.5 | 1 | 1 | -6.73 (1 session) | -6.83 (1 session) | 0.32 (1 session) | 0.11 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 1.000 | 0.000 | 0 (0%) | n/a | aggressive_walk 100% | — |
| pin_nasdaq | POV | 1 | 1 | 1 | 4.47 (1 session) | 4.18 (1 session) | 0.16 (1 session) | 0.05 (1 session) | 0.00 (1 session) | 0.23 (1 session) | 0.504 | 0.000 | 0 (0%) | 0.000 | aggressive_walk 100% | — |
| pin_nasdaq | IS | 0.1 | 2 | 2 | -7.02 ± 4.37 [-62.58, 48.53] | -7.16 ± 4.38 [-62.76, 48.44] | 0.42 ± 0.03 [0.06, 0.77] | 0.11 ± 0.00 [0.09, 0.12] | 0.02 ± 0.00 [0.02, 0.02] | 0.01 ± 0.01 [-0.06, 0.07] | 0.992 | 0.000 | 1 (50%) | 1.000 | aggressive_walk 100% | — |
| pin_nasdaq | IS | 0.5 | 1 | 1 | 13.61 (1 session) | 13.10 (1 session) | 0.08 (1 session) | 0.03 (1 session) | 0.01 (1 session) | 0.46 (1 session) | 0.289 | 0.000 | 1 (100%) | 1.000 | aggressive_walk 100% | — |
| pin_nasdaq | IS | 1 | 1 | 1 | 4.00 (1 session) | 3.59 (1 session) | 0.06 (1 session) | 0.02 (1 session) | 0.00 (1 session) | 0.40 (1 session) | 0.153 | 0.000 | 1 (100%) | 1.000 | aggressive_walk 100% | — |
| pin_iex | TWAP | 0.1 | 1 | 1 | -22.96 (1 session) | -23.16 (1 session) | -0.52 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.20 (1 session) | 0.800 | 1.000 | 0 (0%) | 0.000 | cross 92%, exec_print 8% | — |
| pin_iex | TWAP | 0.5 | 2 | 2 | -3.36 ± 10.44 [-135.97, 129.24] | -3.40 ± 10.40 [-135.56, 128.76] | -0.32 ± 0.04 [-0.85, 0.20] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 0.04 ± 0.04 [-0.41, 0.48] | 0.879 | 1.000 | 0 (0%) | 0.000 | cross 96%, exec_print 4% | — |
| pin_iex | TWAP | 1 | 1 | 1 | -12.71 (1 session) | -12.71 (1 session) | -0.37 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 1.000 | 1.000 | 0 (0%) | n/a | cross 99%, exec_print 1% | — |
| pin_iex | TWAP | 2 | 1 | 1 | 13.52 (1 session) | 13.29 (1 session) | -0.04 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.23 (1 session) | 0.190 | 1.000 | 0 (0%) | 0.000 | cross 95%, exec_print 5% | — |
| pin_iex | VWAP | 0.1 | 2 | 2 | -19.87 ± 18.06 [-249.37, 209.63] | -19.96 ± 18.15 [-250.64, 210.72] | -0.45 ± 0.05 [-1.10, 0.21] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 0.09 ± 0.09 [-1.09, 1.28] | 0.800 | 1.000 | 0 (0%) | 0.000 | cross 98%, exec_print 2% | — |
| pin_iex | VWAP | 0.5 | 2 | 2 | 4.25 ± 2.76 [-30.79, 39.29] | 4.20 ± 2.73 [-30.45, 38.86] | -0.26 ± 0.03 [-0.65, 0.13] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 0.05 ± 0.03 [-0.34, 0.43] | 0.886 | 1.000 | 0 (0%) | 0.000 | cross 98%, exec_print 2% | — |
| pin_iex | VWAP | 1 | 1 | 1 | 8.75 (1 session) | 8.51 (1 session) | -0.19 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.23 (1 session) | 0.506 | 1.000 | 0 (0%) | 0.000 | cross 99%, exec_print 1% | — |
| pin_iex | POV | 0.1 | 2 | 2 | 40.16 ± 36.16 [-419.27, 499.58] | 40.12 ± 36.16 [-419.30, 499.55] | 0.41 ± 0.01 [0.25, 0.58] | 0.03 ± 0.00 [0.03, 0.03] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 1.000 | 0.000 | 1 (50%) | n/a | aggressive_walk 100% | — |
| pin_iex | POV | 0.5 | 1 | 1 | 35.02 (1 session) | 34.99 (1 session) | 0.32 (1 session) | 0.03 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 1.000 | 0.000 | 1 (100%) | n/a | aggressive_walk 100% | — |
| pin_iex | POV | 1 | 1 | 1 | 4.70 (1 session) | 4.45 (1 session) | 0.16 (1 session) | 0.02 (1 session) | 0.00 (1 session) | 0.23 (1 session) | 0.504 | 0.000 | 1 (100%) | 0.419 | aggressive_walk 100% | — |
| pin_iex | IS | 0.1 | 2 | 2 | 37.83 ± 9.43 [-81.98, 157.65] | 37.54 ± 9.41 [-81.96, 157.04] | 0.22 ± 0.05 [-0.38, 0.83] | 0.02 ± 0.00 [0.00, 0.04] | 0.02 ± 0.00 [0.00, 0.03] | 0.26 ± 0.03 [-0.08, 0.60] | 0.599 | 0.000 | 2 (100%) | 1.000 | aggressive_walk 100% | — |
| pin_iex | IS | 0.5 | 1 | 1 | 16.97 (1 session) | 16.36 (1 session) | 0.02 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.61 (1 session) | 0.067 | 0.000 | 1 (100%) | 1.000 | aggressive_walk 100% | — |
| pin_iex | IS | 1 | 1 | 1 | 4.42 (1 session) | 3.97 (1 session) | 0.01 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.45 (1 session) | 0.033 | 0.000 | 1 (100%) | 1.000 | aggressive_walk 100% | — |
| sor | TWAP | 0.1 | 1 | 1 | -22.95 (1 session) | -23.09 (1 session) | -0.51 (1 session) | -0.06 (1 session) | 0.00 (1 session) | 0.20 (1 session) | 0.800 | 1.000 | 0 (0%) | 0.000 | cross 51%, exec_print 49% | — |
| sor | TWAP | 0.5 | 2 | 2 | -2.65 ± 10.98 [-142.15, 136.85] | -2.67 ± 10.93 [-141.50, 136.16] | -0.28 ± 0.07 [-1.14, 0.58] | -0.04 ± 0.00 [-0.05, -0.03] | 0.00 ± 0.00 [0.00, 0.00] | 0.06 ± 0.05 [-0.62, 0.74] | 0.793 | 1.000 | 0 (0%) | 0.000 | cross 84%, exec_print 16% | — |
| sor | TWAP | 1 | 1 | 1 | -15.12 (1 session) | -15.21 (1 session) | -0.30 (1 session) | -0.04 (1 session) | 0.00 (1 session) | 0.13 (1 session) | 0.797 | 1.000 | 0 (0%) | 0.000 | cross 85%, exec_print 15% | — |
| sor | TWAP | 2 | 1 | 1 | 13.61 (1 session) | 13.39 (1 session) | -0.05 (1 session) | -0.01 (1 session) | 0.00 (1 session) | 0.23 (1 session) | 0.203 | 1.000 | 0 (0%) | 0.000 | cross 73%, exec_print 27% | — |
| sor | VWAP | 0.1 | 2 | 2 | -19.77 ± 18.20 [-251.07, 211.53] | -19.81 ± 18.31 [-252.48, 212.86] | -0.39 ± 0.10 [-1.62, 0.84] | -0.06 ± 0.01 [-0.24, 0.13] | 0.00 ± 0.00 [0.00, 0.00] | 0.09 ± 0.09 [-1.09, 1.28] | 0.800 | 1.000 | 0 (0%) | 0.000 | cross 48%, exec_print 52% | — |
| sor | VWAP | 0.5 | 2 | 2 | 4.52 ± 2.53 [-27.57, 36.60] | 4.52 ± 2.50 [-27.25, 36.30] | -0.24 ± 0.00 [-0.27, -0.21] | -0.06 ± 0.00 [-0.09, -0.04] | 0.00 ± 0.00 [0.00, 0.00] | 0.06 ± 0.02 [-0.23, 0.34] | 0.860 | 1.000 | 0 (0%) | 0.000 | cross 74%, exec_print 26% | — |
| sor | VWAP | 1 | 1 | 1 | 7.91 (1 session) | 7.68 (1 session) | -0.15 (1 session) | -0.03 (1 session) | 0.00 (1 session) | 0.26 (1 session) | 0.441 | 1.000 | 0 (0%) | 0.000 | cross 78%, exec_print 22% | — |
| sor | POV | 0.1 | 2 | 2 | -2.32 ± 3.33 [-44.62, 39.97] | -2.40 ± 3.33 [-44.75, 39.96] | 0.41 ± 0.01 [0.22, 0.60] | 0.07 ± 0.00 [0.01, 0.14] | 0.00 ± 0.00 [-0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 1.000 | 0.000 | 0 (0%) | n/a | aggressive_walk 100% | — |
| sor | POV | 0.5 | 1 | 1 | 3.39 (1 session) | 3.31 (1 session) | 0.32 (1 session) | 0.08 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 1.000 | 0.000 | 1 (100%) | n/a | aggressive_walk 100% | — |
| sor | POV | 1 | 1 | 1 | 4.56 (1 session) | 4.29 (1 session) | 0.16 (1 session) | 0.04 (1 session) | 0.00 (1 session) | 0.23 (1 session) | 0.504 | 0.000 | 1 (100%) | 0.009 | aggressive_walk 100% | — |
| sor | IS | 0.1 | 2 | 2 | 39.19 ± 8.07 [-63.40, 141.78] | 38.91 ± 8.04 [-63.23, 141.05] | 0.23 ± 0.06 [-0.53, 0.99] | 0.03 ± 0.01 [-0.09, 0.15] | 0.02 ± 0.00 [-0.00, 0.03] | 0.24 ± 0.05 [-0.35, 0.83] | 0.629 | 0.000 | 2 (100%) | 1.000 | aggressive_walk 100% | — |
| sor | IS | 0.5 | 1 | 1 | 15.35 (1 session) | 14.79 (1 session) | 0.04 (1 session) | 0.02 (1 session) | 0.00 (1 session) | 0.53 (1 session) | 0.177 | 0.000 | 1 (100%) | 1.000 | aggressive_walk 100% | — |
| sor | IS | 1 | 1 | 1 | 4.42 (1 session) | 3.97 (1 session) | 0.01 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.45 (1 session) | 0.033 | 0.000 | 1 (100%) | 1.000 | aggressive_walk 100% | — |
| sor_sweep | TWAP | 0.1 | 1 | 1 | -22.95 (1 session) | -23.09 (1 session) | -0.51 (1 session) | -0.06 (1 session) | 0.00 (1 session) | 0.20 (1 session) | 0.800 | 1.000 | 0 (0%) | 0.000 | cross 51%, exec_print 49% | — |
| sor_sweep | TWAP | 0.5 | 2 | 2 | -2.65 ± 10.98 [-142.15, 136.85] | -2.67 ± 10.93 [-141.50, 136.16] | -0.28 ± 0.07 [-1.14, 0.58] | -0.04 ± 0.00 [-0.05, -0.03] | 0.00 ± 0.00 [0.00, 0.00] | 0.06 ± 0.05 [-0.62, 0.74] | 0.793 | 1.000 | 0 (0%) | 0.000 | cross 84%, exec_print 16% | — |
| sor_sweep | TWAP | 1 | 1 | 1 | -15.12 (1 session) | -15.21 (1 session) | -0.30 (1 session) | -0.04 (1 session) | 0.00 (1 session) | 0.13 (1 session) | 0.797 | 1.000 | 0 (0%) | 0.000 | cross 85%, exec_print 15% | — |
| sor_sweep | TWAP | 2 | 1 | 1 | 13.61 (1 session) | 13.39 (1 session) | -0.05 (1 session) | -0.01 (1 session) | 0.00 (1 session) | 0.23 (1 session) | 0.203 | 1.000 | 0 (0%) | 0.000 | cross 73%, exec_print 27% | — |
| sor_sweep | VWAP | 0.1 | 2 | 2 | -19.77 ± 18.20 [-251.07, 211.53] | -19.81 ± 18.31 [-252.48, 212.86] | -0.39 ± 0.10 [-1.62, 0.84] | -0.06 ± 0.01 [-0.24, 0.13] | 0.00 ± 0.00 [0.00, 0.00] | 0.09 ± 0.09 [-1.09, 1.28] | 0.800 | 1.000 | 0 (0%) | 0.000 | cross 48%, exec_print 52% | — |
| sor_sweep | VWAP | 0.5 | 2 | 2 | 4.52 ± 2.53 [-27.57, 36.60] | 4.52 ± 2.50 [-27.25, 36.30] | -0.24 ± 0.00 [-0.27, -0.21] | -0.06 ± 0.00 [-0.09, -0.04] | 0.00 ± 0.00 [0.00, 0.00] | 0.06 ± 0.02 [-0.23, 0.34] | 0.860 | 1.000 | 0 (0%) | 0.000 | cross 74%, exec_print 26% | — |
| sor_sweep | VWAP | 1 | 1 | 1 | 7.91 (1 session) | 7.68 (1 session) | -0.15 (1 session) | -0.03 (1 session) | 0.00 (1 session) | 0.26 (1 session) | 0.441 | 1.000 | 0 (0%) | 0.000 | cross 78%, exec_print 22% | — |
| sor_sweep | POV | 0.1 | 2 | 2 | -2.60 ± 3.24 [-43.80, 38.60] | -2.67 ± 3.25 [-43.96, 38.63] | 0.41 ± 0.02 [0.22, 0.60] | 0.07 ± 0.01 [-0.03, 0.16] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 1.000 | 0.000 | 0 (0%) | n/a | aggressive_walk 100% | — |
| sor_sweep | POV | 0.5 | 1 | 1 | -6.74 (1 session) | -6.82 (1 session) | 0.32 (1 session) | 0.08 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 1.000 | 0.000 | 0 (0%) | n/a | aggressive_walk 100% | — |
| sor_sweep | POV | 1 | 1 | 1 | 4.44 (1 session) | 4.17 (1 session) | 0.16 (1 session) | 0.04 (1 session) | 0.00 (1 session) | 0.23 (1 session) | 0.504 | 0.000 | 0 (0%) | 0.000 | aggressive_walk 100% | — |
| sor_sweep | IS | 0.1 | 2 | 2 | -9.31 ± 2.84 [-45.33, 26.72] | -9.60 ± 2.84 [-45.65, 26.45] | 0.27 ± 0.01 [0.10, 0.44] | 0.06 ± 0.00 [0.01, 0.10] | 0.01 ± 0.00 [0.00, 0.02] | 0.23 ± 0.00 [0.20, 0.26] | 0.651 | 0.000 | 0 (0%) | 0.000 | aggressive_walk 100% | — |
| sor_sweep | IS | 0.5 | 1 | 1 | 15.57 (1 session) | 15.01 (1 session) | 0.04 (1 session) | 0.01 (1 session) | 0.00 (1 session) | 0.55 (1 session) | 0.150 | 0.000 | 0 (0%) | 0.000 | aggressive_walk 100% | — |
| sor_sweep | IS | 1 | 1 | 1 | 4.16 (1 session) | 3.71 (1 session) | 0.03 (1 session) | 0.01 (1 session) | 0.00 (1 session) | 0.44 (1 session) | 0.069 | 0.000 | 0 (0%) | 0.000 | aggressive_walk 100% | — |

## Paired (same windows; `other − control`, bps; baseline `pin_nasdaq`, signal-aware arms also against their own plain control)

| comparison | algo | size % ADV | pairs | all-in diff | shortfall diff | completion diff | fill-rate diff |
|---|---|---|---|---|---|---|---|
| pin_iex-pin_nasdaq | TWAP | 0.1 | 1 | -3.73 (1 session) | -4.01 (1 session) | 0.20 (1 session) | -0.20 (1 session) |
| pin_iex-pin_nasdaq | TWAP | 0.5 | 2 | -2.50 ± 1.24 [-18.31, 13.31] | -2.53 ± 1.28 [-18.75, 13.69] | -0.03 ± 0.02 [-0.26, 0.21] | 0.09 ± 0.07 [-0.79, 0.96] |
| pin_iex-pin_nasdaq | TWAP | 1 | 1 | 2.00 (1 session) | 2.07 (1 session) | -0.13 (1 session) | 0.20 (1 session) |
| pin_iex-pin_nasdaq | TWAP | 2 | 1 | -0.08 (1 session) | -0.10 (1 session) | 0.00 (1 session) | -0.01 (1 session) |
| pin_iex-pin_nasdaq | VWAP | 0.1 | 2 | -1.85 ± 1.61 [-22.35, 18.64] | -2.02 ± 1.71 [-23.69, 19.65] | 0.09 ± 0.09 [-1.09, 1.28] | -0.20 ± 0.20 [-2.74, 2.34] |
| pin_iex-pin_nasdaq | VWAP | 0.5 | 2 | -0.27 ± 0.23 [-3.22, 2.68] | -0.32 ± 0.23 [-3.20, 2.56] | -0.01 ± 0.01 [-0.10, 0.09] | 0.03 ± 0.03 [-0.31, 0.36] |
| pin_iex-pin_nasdaq | VWAP | 1 | 1 | 0.83 (1 session) | 0.83 (1 session) | -0.03 (1 session) | 0.07 (1 session) |
| pin_iex-pin_nasdaq | POV | 0.1 | 2 | 42.71 ± 39.45 [-458.59, 544.02] | 42.79 ± 39.45 [-458.51, 544.09] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] |
| pin_iex-pin_nasdaq | POV | 0.5 | 1 | 41.75 (1 session) | 41.82 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| pin_iex-pin_nasdaq | POV | 1 | 1 | 0.23 (1 session) | 0.26 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| pin_iex-pin_nasdaq | IS | 0.1 | 2 | 44.86 ± 13.80 [-130.51, 220.23] | 44.70 ± 13.78 [-130.41, 219.81] | 0.25 ± 0.02 [-0.02, 0.53] | -0.39 ± 0.03 [-0.83, 0.05] |
| pin_iex-pin_nasdaq | IS | 0.5 | 1 | 3.37 (1 session) | 3.26 (1 session) | 0.14 (1 session) | -0.22 (1 session) |
| pin_iex-pin_nasdaq | IS | 1 | 1 | 0.42 (1 session) | 0.38 (1 session) | 0.06 (1 session) | -0.12 (1 session) |
| sor-pin_nasdaq | TWAP | 0.1 | 1 | -3.72 (1 session) | -3.93 (1 session) | 0.20 (1 session) | -0.20 (1 session) |
| sor-pin_nasdaq | TWAP | 0.5 | 2 | -1.79 ± 1.79 [-24.50, 20.92] | -1.80 ± 1.80 [-24.69, 21.09] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] |
| sor-pin_nasdaq | TWAP | 1 | 1 | -0.42 (1 session) | -0.43 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| sor-pin_nasdaq | TWAP | 2 | 1 | 0.00 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| sor-pin_nasdaq | VWAP | 0.1 | 2 | -1.75 ± 1.75 [-24.05, 20.54] | -1.86 ± 1.86 [-25.53, 21.80] | 0.09 ± 0.09 [-1.09, 1.28] | -0.20 ± 0.20 [-2.74, 2.34] |
| sor-pin_nasdaq | VWAP | 0.5 | 2 | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] |
| sor-pin_nasdaq | VWAP | 1 | 1 | 0.00 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| sor-pin_nasdaq | POV | 0.1 | 2 | 0.23 ± 0.03 [-0.18, 0.65] | 0.27 ± 0.04 [-0.21, 0.75] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] |
| sor-pin_nasdaq | POV | 0.5 | 1 | 10.12 (1 session) | 10.15 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| sor-pin_nasdaq | POV | 1 | 1 | 0.09 (1 session) | 0.10 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| sor-pin_nasdaq | IS | 0.1 | 2 | 46.21 ± 12.45 [-111.93, 204.36] | 46.07 ± 12.41 [-111.68, 203.81] | 0.24 ± 0.04 [-0.29, 0.76] | -0.36 ± 0.06 [-1.18, 0.45] |
| sor-pin_nasdaq | IS | 0.5 | 1 | 1.74 (1 session) | 1.69 (1 session) | 0.07 (1 session) | -0.11 (1 session) |
| sor-pin_nasdaq | IS | 1 | 1 | 0.42 (1 session) | 0.38 (1 session) | 0.06 (1 session) | -0.12 (1 session) |
| sor_sweep-pin_nasdaq | TWAP | 0.1 | 1 | -3.72 (1 session) | -3.93 (1 session) | 0.20 (1 session) | -0.20 (1 session) |
| sor_sweep-pin_nasdaq | TWAP | 0.5 | 2 | -1.79 ± 1.79 [-24.50, 20.92] | -1.80 ± 1.80 [-24.69, 21.09] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] |
| sor_sweep-pin_nasdaq | TWAP | 1 | 1 | -0.42 (1 session) | -0.43 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| sor_sweep-pin_nasdaq | TWAP | 2 | 1 | 0.00 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| sor_sweep-pin_nasdaq | VWAP | 0.1 | 2 | -1.75 ± 1.75 [-24.05, 20.54] | -1.86 ± 1.86 [-25.53, 21.80] | 0.09 ± 0.09 [-1.09, 1.28] | -0.20 ± 0.20 [-2.74, 2.34] |
| sor_sweep-pin_nasdaq | VWAP | 0.5 | 2 | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] |
| sor_sweep-pin_nasdaq | VWAP | 1 | 1 | 0.00 (1 session) | 0.00 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| sor_sweep-pin_nasdaq | POV | 0.1 | 2 | -0.04 ± 0.05 [-0.72, 0.64] | -0.00 ± 0.05 [-0.58, 0.58] | 0.00 ± 0.00 [0.00, 0.00] | 0.00 ± 0.00 [0.00, 0.00] |
| sor_sweep-pin_nasdaq | POV | 0.5 | 1 | -0.02 (1 session) | 0.01 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| sor_sweep-pin_nasdaq | POV | 1 | 1 | -0.03 (1 session) | -0.01 (1 session) | 0.00 (1 session) | 0.00 (1 session) |
| sor_sweep-pin_nasdaq | IS | 0.1 | 2 | -2.28 ± 1.54 [-21.81, 17.25] | -2.44 ± 1.54 [-21.99, 17.11] | 0.22 ± 0.01 [0.13, 0.32] | -0.34 ± 0.01 [-0.48, -0.21] |
| sor_sweep-pin_nasdaq | IS | 0.5 | 1 | 1.97 (1 session) | 1.90 (1 session) | 0.09 (1 session) | -0.14 (1 session) |
| sor_sweep-pin_nasdaq | IS | 1 | 1 | 0.15 (1 session) | 0.13 (1 session) | 0.04 (1 session) | -0.08 (1 session) |

## Markout by fill type and regime (v1.17 B6; bps, ± day-clustered SE)

| policy | cell | fills | qty | markout 1s | markout 5s | markout 30s | price impact 1s | price impact 5s | price impact 30s |
|---|---|---|---|---|---|---|---|---|---|
| pin_nasdaq | all | 19675 | 403715 | -1.13 ± 0.09 [-2.31, 0.06] | -1.15 ± 0.11 [-2.52, 0.23] | -1.14 ± 0.10 [-2.40, 0.12] | -0.06 ± 0.01 [-0.14, 0.01] | -0.08 ± 0.01 [-0.19, 0.03] | -0.07 ± 0.00 [-0.07, -0.07] |
| pin_nasdaq | type passive | 5921 | 274182 | -2.68 ± 0.83 [-13.18, 7.82] | -2.68 ± 0.90 [-14.06, 8.69] | -2.63 ± 0.83 [-13.24, 7.97] | -0.18 ± 0.04 [-0.70, 0.35] | -0.18 ± 0.03 [-0.54, 0.17] | -0.13 ± 0.03 [-0.56, 0.29] |
| pin_nasdaq | type aggressive | 13754 | 129533 | -0.46 ± 0.00 [-0.51, -0.41] | -0.48 ± 0.01 [-0.66, -0.30] | -0.49 ± 0.01 [-0.57, -0.42] | -0.01 ± 0.03 [-0.34, 0.31] | -0.03 ± 0.02 [-0.23, 0.17] | -0.04 ± 0.02 [-0.34, 0.26] |
| pin_nasdaq | regime plain | 19675 | 403715 | -1.13 ± 0.09 [-2.31, 0.06] | -1.15 ± 0.11 [-2.52, 0.23] | -1.14 ± 0.10 [-2.40, 0.12] | -0.06 ± 0.01 [-0.14, 0.01] | -0.08 ± 0.01 [-0.19, 0.03] | -0.07 ± 0.00 [-0.07, -0.07] |
| pin_iex | all | 15041 | 402629 | -27.85 ± 6.62 [-112.00, 56.29] | -27.88 ± 6.63 [-112.16, 56.40] | -27.97 ± 6.63 [-112.19, 56.25] | -0.02 ± 0.03 [-0.42, 0.38] | -0.05 ± 0.04 [-0.59, 0.50] | -0.13 ± 0.04 [-0.61, 0.35] |
| pin_iex | type passive | 3099 | 296644 | -1.99 ± 0.51 [-8.45, 4.47] | -2.00 ± 0.59 [-9.47, 5.47] | -2.28 ± 0.55 [-9.33, 4.76] | 0.02 ± 0.02 [-0.26, 0.31] | 0.01 ± 0.10 [-1.28, 1.30] | -0.27 ± 0.07 [-1.14, 0.59] |
| pin_iex | type aggressive | 11942 | 105985 | -34.56 ± 6.54 [-117.65, 48.52] | -34.60 ± 6.53 [-117.58, 48.39] | -34.63 ± 6.55 [-117.80, 48.54] | -0.03 ± 0.03 [-0.43, 0.37] | -0.06 ± 0.02 [-0.37, 0.25] | -0.10 ± 0.04 [-0.59, 0.40] |
| pin_iex | regime plain | 15041 | 402629 | -27.85 ± 6.62 [-112.00, 56.29] | -27.88 ± 6.63 [-112.16, 56.40] | -27.97 ± 6.63 [-112.19, 56.25] | -0.02 ± 0.03 [-0.42, 0.38] | -0.05 ± 0.04 [-0.59, 0.50] | -0.13 ± 0.04 [-0.61, 0.35] |
| sor | all | 19145 | 380254 | -5.24 ± 1.06 [-18.76, 8.28] | -5.24 ± 1.07 [-18.84, 8.37] | -5.26 ± 1.05 [-18.54, 8.03] | -0.06 ± 0.01 [-0.20, 0.09] | -0.06 ± 0.00 [-0.11, 0.00] | -0.07 ± 0.03 [-0.46, 0.31] |
| sor | type passive | 5477 | 269623 | -2.56 ± 0.79 [-12.57, 7.45] | -2.51 ± 0.82 [-12.96, 7.94] | -2.56 ± 0.72 [-11.68, 6.56] | -0.16 ± 0.07 [-0.99, 0.67] | -0.11 ± 0.03 [-0.50, 0.28] | -0.16 ± 0.13 [-1.87, 1.55] |
| sor | type aggressive | 13668 | 110631 | -6.31 ± 0.79 [-16.34, 3.72] | -6.33 ± 0.78 [-16.22, 3.56] | -6.33 ± 0.79 [-16.36, 3.69] | -0.01 ± 0.02 [-0.33, 0.30] | -0.03 ± 0.01 [-0.21, 0.15] | -0.04 ± 0.02 [-0.35, 0.27] |
| sor | regime plain | 19145 | 380254 | -5.24 ± 1.06 [-18.76, 8.28] | -5.24 ± 1.07 [-18.84, 8.37] | -5.26 ± 1.05 [-18.54, 8.03] | -0.06 ± 0.01 [-0.20, 0.09] | -0.06 ± 0.00 [-0.11, 0.00] | -0.07 ± 0.03 [-0.46, 0.31] |
| sor_sweep | all | 16394 | 382283 | -1.16 ± 0.09 [-2.35, 0.03] | -1.16 ± 0.10 [-2.47, 0.14] | -1.19 ± 0.07 [-2.13, -0.25] | -0.09 ± 0.02 [-0.30, 0.12] | -0.09 ± 0.01 [-0.18, 0.01] | -0.12 ± 0.04 [-0.57, 0.34] |
| sor_sweep | type passive | 5477 | 269623 | -2.56 ± 0.79 [-12.57, 7.45] | -2.51 ± 0.82 [-12.96, 7.94] | -2.56 ± 0.72 [-11.68, 6.56] | -0.16 ± 0.07 [-0.99, 0.67] | -0.11 ± 0.03 [-0.50, 0.28] | -0.16 ± 0.13 [-1.87, 1.55] |
| sor_sweep | type aggressive | 10917 | 112660 | -0.46 ± 0.00 [-0.47, -0.46] | -0.48 ± 0.01 [-0.63, -0.33] | -0.50 ± 0.00 [-0.51, -0.49] | -0.06 ± 0.02 [-0.32, 0.20] | -0.08 ± 0.01 [-0.19, 0.04] | -0.09 ± 0.02 [-0.37, 0.18] |
| sor_sweep | regime plain | 16394 | 382283 | -1.16 ± 0.09 [-2.35, 0.03] | -1.16 ± 0.10 [-2.47, 0.14] | -1.19 ± 0.07 [-2.13, -0.25] | -0.09 ± 0.02 [-0.30, 0.12] | -0.09 ± 0.01 [-0.18, 0.01] | -0.12 ± 0.04 [-0.57, 0.34] |

## Two venues: router vs pinned (v1.17 B8)

Second dataset `C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\data\real\iex\iex_20251128` (IEX clock `exchange`); alignment: both streams on ns since the session's New York midnight, exchange (UTC) clock; Nasdaq ITCH as stamped; IEX: exchange_ts from the messages' own UTC send times (iex_clock='exchange', no shift) or, for a capture-clock (TAI) stream, exchange_ts/receive_ts - 37 s (iex_clock='capture'); merge order (exchange_ts, venue_id), each stream's own order kept inside a tie. Routes: `pin_iex` -> IEXG, `pin_nasdaq` -> XNAS, `sor` -> sor_v2 router, `sor_sweep` -> sor_v2 router. Router calibration: `research\execution\b8\calibration`.

| policy | filled qty | venue share | markout 1s by venue | markout 5s by venue | markout 30s by venue |
|---|---:|---|---|---|---|
| pin_nasdaq | 403715 | XNAS 100.0% | XNAS -1.13 ± 0.09 [-2.31, 0.06] | XNAS -1.15 ± 0.11 [-2.52, 0.23] | XNAS -1.14 ± 0.10 [-2.40, 0.12] |
| pin_iex | 402629 | IEXG 100.0% | IEXG -27.85 ± 6.62 [-112.00, 56.29] | IEXG -27.88 ± 6.63 [-112.16, 56.40] | IEXG -27.97 ± 6.63 [-112.19, 56.25] |
| sor | 380254 | IEXG 21.4%, XNAS 78.6% | IEXG -13.67 ± 2.46 [-44.90, 17.56]; XNAS -1.28 ± 0.10 [-2.61, 0.05] | IEXG -13.71 ± 2.44 [-44.76, 17.34]; XNAS -1.26 ± 0.12 [-2.78, 0.27] | IEXG -13.76 ± 2.42 [-44.53, 17.01]; XNAS -1.26 ± 0.09 [-2.43, -0.10] |
| sor_sweep | 382283 | IEXG 20.4%, XNAS 79.6% | IEXG -0.63 ± 0.04 [-1.15, -0.10]; XNAS -1.42 ± 0.14 [-3.25, 0.41] | IEXG -0.65 ± 0.02 [-0.90, -0.41]; XNAS -1.40 ± 0.17 [-3.52, 0.72] | IEXG -0.71 ± 0.01 [-0.89, -0.53]; XNAS -1.42 ± 0.13 [-3.01, 0.18] |

Pooled over every cell (same parents, `other - baseline`, bps; ± day-clustered SE):

| pair | n | all-in | shortfall | spread | fees net | fill rate diff |
|---|---:|---|---|---|---|---|
| pin_iex-pin_nasdaq | 18 | 11.70 ± 4.63 [-47.08, 70.48] | 11.65 ± 4.64 [-47.34, 70.65] | -0.03 ± 0.01 [-0.11, 0.06] | 0.00 ± 0.01 [-0.09, 0.09] | -0.07 ± 0.01 [-0.16, 0.02] |
| sor-pin_nasdaq | 18 | 5.22 ± 0.11 [3.77, 6.68] | 5.18 ± 0.12 [3.67, 6.70] | -0.01 ± 0.01 [-0.14, 0.13] | -0.01 ± 0.00 [-0.05, 0.02] | -0.09 ± 0.00 [-0.12, -0.06] |
| sor_sweep-pin_nasdaq | 18 | -0.77 ± 0.34 [-5.05, 3.52] | -0.81 ± 0.35 [-5.20, 3.58] | -0.00 ± 0.01 [-0.08, 0.08] | -0.01 ± 0.00 [-0.05, 0.04] | -0.08 ± 0.01 [-0.15, -0.01] |

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
