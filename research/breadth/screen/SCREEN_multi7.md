# Net-of-cost screen (v1.17 C6)

Status: in-sample screen; a filter before any confirmatory look, not evidence. Pre-registration: `research/preregs/net_of_cost_screen_prereg_DRAFT.json` (sha256 `7d4c25664a181b23524541f41aed6dc86db574013fc1245c3d66c6d46f2e0848`, DRAFT, unsigned).

Methods `v2` at statistics revision `v1.17`; size 100 units per row; 4 folds, embargo 60 s; impact `sqrt`. Dataset: `ee4c218738683e5bc4249aa4a9e7302f32261c5855e97b3d337862b334333fb4`.

**0 pass, 22 fail, 1 not screened** of 23 candidates.

| candidate | gross IC | net IC | net t (HAC) | net t (day) | net return / trip | cost / trip | days | pairs | result |
|---|---|---|---|---|---|---|---|---|---|
| EQ01/1s/linear_z_v1 | 0.1244 | -0.7618 | -48.77 | -17.94 | -1.830 bps | 1.901 bps | 6 | 2314119 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ01/1s/linear_z_v2 | 0.1304 | -0.7574 | -75.33 | -57.23 | -1.830 bps | 1.901 bps | 6 | 2313747 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ02/5s/linear_z_v1 | 0.0460 | -0.5469 | -66.95 | -21.90 | -1.837 bps | 1.900 bps | 6 | 2313485 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ02/5s/linear_z_v2 | 0.0480 | -0.5449 | -74.22 | -41.80 | -1.838 bps | 1.900 bps | 6 | 2313113 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ03/5s/linear_z_v1 | 0.0510 | -0.5563 | -66.81 | -22.19 | -1.828 bps | 1.900 bps | 6 | 2313485 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ03/5s/linear_z_v2 | 0.0532 | -0.5540 | -73.18 | -41.44 | -1.828 bps | 1.900 bps | 6 | 2313113 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ04/5s/linear_z_v1 | 0.0134 | -0.6613 | -53.14 | -23.26 | -1.885 bps | 1.905 bps | 6 | 2245540 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ04/5s/linear_z_v2 | 0.0137 | -0.6630 | -100.53 | -51.52 | -1.885 bps | 1.905 bps | 6 | 2245120 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ05/1s/linear_z_v1 | 0.0596 | -0.6713 | -38.80 | -15.11 | -1.866 bps | 1.901 bps | 6 | 2314119 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ05/1s/linear_z_v2 | 0.0593 | -0.6885 | -79.94 | -41.89 | -1.866 bps | 1.901 bps | 6 | 2313747 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ06/10s/linear_z_v1 | 0.0276 | -0.4947 | -50.31 | -24.73 | -1.862 bps | 1.900 bps | 6 | 2312684 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ06/10s/linear_z_v2 | 0.0256 | -0.5019 | -59.76 | -33.93 | -1.866 bps | 1.900 bps | 6 | 2312312 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ07/10s/linear_z_v1 | -0.0098 | -0.4344 | -40.17 | -14.69 | -1.919 bps | 1.900 bps | 6 | 2312684 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ07/10s/linear_z_v2 | -0.0094 | -0.4389 | -47.53 | -24.56 | -1.914 bps | 1.900 bps | 6 | 2312312 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ08/10s/linear_z_v1 | -0.0054 | -0.5040 | -17.81 | -10.34 | -1.913 bps | 1.900 bps | 6 | 2312641 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ08/10s/linear_z_v2 | -0.0012 | -0.5366 | -53.63 | -42.44 | -1.896 bps | 1.900 bps | 6 | 2312227 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ09/10s/linear_z_v1 | 0.0058 | -0.4612 | -15.12 | -8.93 | -1.987 bps | 2.015 bps | 6 | 1464987 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ09/10s/linear_z_v2 | 0.0189 | -0.4838 | -38.50 | -29.61 | -1.986 bps | 2.015 bps | 6 | 1464739 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ10/1s/linear_z_v1 | 0.0757 | -0.5922 | -39.11 | -21.49 | -1.972 bps | 2.015 bps | 6 | 1465920 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ10/1s/linear_z_v2 | 0.0702 | -0.5932 | -39.13 | -19.12 | -1.975 bps | 2.015 bps | 6 | 1465672 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ11/15m/linear_z_v1 | | | | | | | | | not screened: ScreenError: EQ11/15m/linear_z_v1: no scored (signal, label, cost) row on any fold - not screened, not failed |
| EQ12/5s/linear_z_v1 | 0.0463 | -0.5473 | -67.95 | -22.09 | -1.837 bps | 1.900 bps | 6 | 2313485 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |
| EQ12/5s/linear_z_v2 | 0.0482 | -0.5462 | -77.66 | -43.44 | -1.838 bps | 1.900 bps | 6 | 2313113 | fail (net_ic_min, net_return_mean_min, net_return_tstat_min, net_tstat_day_cluster_min) |

Rules (every one must hold): `n_days_min >= 4`, `n_pairs_min >= 1000`, `net_ic_min >= 0`, `net_return_mean_min >= 0`, `net_return_tstat_min >= 2`, `net_tstat_day_cluster_min >= 2`.

Reading: a pass means the candidate is worth ONE confirmatory look under its own pre-registration; a fail means no confirmatory run at this size on these days. Nothing here is evidence of an edge, nothing is signed, no holdout was read.

Ledger: 23 looks of kind `net_of_cost_screen` charged to `research/experiments.json` (total 5599).
