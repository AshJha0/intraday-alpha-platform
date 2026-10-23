# B8 v2 (REL-04b): result under the signed pre-declaration

Declaration: `research/execution/b8v2/predeclaration.json`. Latency assumption: L0: exchange timestamps, no measured order-entry latency.
Contrast: policy - pin_nasdaq, pooled over all 16 cells, per symbol; intervals at 95 % and family-adjusted (0.9917, K = 6).

G = 3 evaluation sessions (2 regimes, Dec 2025 and May 2026): the interval has 2 df, t(0.975, 2) = 4.30 and t(1 - 0.025/6, 2) = 10.89. The inference rests on 3 clusters; the run reports size, not power; 'underpowered' is the expected verdict for effects near 1 bps and is stated here before the run.

Negative results first.

| policy | symbol | verdict | d trading_cost_per_filled_share_bps [family CI] (MDE) | d fill rate [family CI] (MDE) | d completion charge [family CI] | G |
|---|---|---|---|---|---|---|
| pin_iex | AAPL | no_evidence (underpowered) | -0.78 [-2.25, 0.68] (1.61) | 0.0068 [-0.21, 0.2236] (0.2379) | -0.0 [-0.03, 0.03] | 3 |
| pin_iex | MSFT | no_evidence (underpowered) | -7.55 [-57.46, 42.37] (54.78) | -0.1257 [-0.4585, 0.2072] (0.3653) | 0.0 [-0.08, 0.09] | 3 |
| sor_sweep | AAPL | no_evidence (underpowered) | -0.18 [-5.7, 5.34] (6.06) | -0.0015 [-0.0668, 0.0638] (0.0717) | -0.01 [-0.05, 0.04] | 3 |
| sor_sweep | MSFT | no_evidence (underpowered) | 1.71 [-18.5, 21.92] (22.18) | -0.0202 [-0.0888, 0.0484] (0.0753) | 0.0 [-0.05, 0.06] | 3 |
| sor | AAPL | no_evidence (underpowered) | -0.57 [-6.5, 5.37] (6.51) | -0.0054 [-0.0981, 0.0874] (0.1018) | -0.0 [-0.05, 0.04] | 3 |
| sor | MSFT | no_evidence (underpowered) | -0.67 [-12.49, 11.15] (12.97) | -0.0255 [-0.0901, 0.0391] (0.0709) | 0.0 [-0.05, 0.06] | 3 |

Better than pinned Nasdaq on both symbols: `pin_iex` no, `sor` no, `sor_sweep` no
