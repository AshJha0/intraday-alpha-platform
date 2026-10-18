# Factor risk model (v1.18 REL-17)

Module: `python/src/iap/portfolio/factor_risk.py`. Tests: `python/tests/test_v118_factor_risk.py`.
Stress scenarios: `configs/risk/stress_scenarios.json` (x-version 1).

This is the research-side portfolio risk model. The order-level hard risk engine
(`iap.risk.engine`, the Python port of `rust/risk`) is unchanged and keeps its parity goldens.

## 1. Model

`r = B f + e`, `Sigma = B F B' + D`, `D` diagonal.

| piece | source |
|---|---|
| `market` column of `B` | beta of each name to its reference ETF under the REL-07 `xasset_v2` map (`beta_w5m_v1`); missing beta = 1.0, listed |
| `sector:<name>` columns | one-hot sector from the REL-07 map (`configs/reference/reference_etf_map_v1.json`) |
| `size:q2..`, `liquidity:q2..` | quantile-bucket dummies of a size proxy and of ADV notional; bucket q1 (smallest / least liquid) is the dropped baseline, because the sector dummies already sum to one |
| factor returns `f_t` | cross-sectional least squares per period, minimum-norm (`pinv`) when `B` is rank deficient |
| `F` | RiskMetrics EWMA (lambda 0.94, `iap.portfolio.covariance.ewma_covariance`) or sample covariance, shrunk with the estimated Ledoit-Wolf (2004) intensity (`iap.core.shrinkage.ledoit_wolf`) toward `mu I` |
| `D` | mean squared residual per name, floored at 1e-12 |

Outputs: ex-ante volatility `sqrt(w' Sigma w)`; factor / specific split; Euler contributions per
factor (`x_k (F x)_k`, `x = B'w`) and per name (`w_i (Sigma w)_i`), which sum exactly to the factor
and total variance; parametric normal VaR and ES; historical VaR (lower order statistic
`ceil(n (1 - a))`, no interpolation) and ES (mean of the observations at or beyond it); stress P&L
`w' B s` for named factor moves. Horizon scaling is `sqrt(h)` (`FactorRiskModel.scaled`), which
assumes independent periods.

## 2. Validation against known answers

| check | known answer | source | result |
|---|---|---|---|
| two-asset model-building VaR: $10M at daily sd 2 %, $5M at 1 %, correlation 0.3, 10-day 99 %, z = 2.33 | 1-day sd $220,227; VaRs $1,473,621 / $368,405 / portfolio $1,622,657; diversification benefit $219,369 | J. Hull, *Options, Futures, and Other Derivatives* and *Risk Management and Financial Institutions*, model-building approach worked example | reproduced to $1 (`test_hull_two_asset_var_reproduced`) |
| normal VaR and ES multipliers | VaR_0.99 = 2.3263, ES_0.99 = 2.6652, ES_0.975 = 2.3378 | standard-normal quantile and `phi(z)/(1-a)`; e.g. McNeil, Frey and Embrechts, *Quantitative Risk Management*, ch. 2; the FRTB 97.5 % ES vs 99 % VaR comparison | reproduced to 1e-4 (`test_normal_var_es_constants`) |
| single-index decomposition | betas (0.8, 1.0, 1.5), weights (0.5, 0.3, 0.2), market variance 0.0225, specific variances (0.04, 0.01, 0.09): factor variance 0.0225, specific 0.0145, total 0.037 | Grinold and Kahn, *Active Portfolio Management*, ch. 3 (`sigma^2 = beta_P^2 sigma_M^2 + sum w_i^2 omega_i^2`), by hand | exact; Euler contributions sum to the totals (`test_single_index_decomposition_by_hand`) |
| historical VaR / ES on a known sample | P&L -49..50, 95 %: VaR 45, ES 47 | definition in section 1 | exact |
| historical vs parametric on 400k normal draws | 2.3263 / 2.6652 | as above | within 0.03 |
| estimator recovery | 60 names, 4 factors, 4 000 periods simulated from a known `B`, `F`, `D` | synthetic, known by construction | portfolio vol within 6 %, specific variances within 15 % |

What this does not validate: the factor choice itself on real data (beta, sector and bucket factors
explain an unknown share of intraday variance in this universe), the sector map's accuracy (REL-07
limitations apply), and the `sqrt(h)` horizon rule under autocorrelated intraday returns.

## 3. Stress scenarios

Named factor moves in `configs/risk/stress_scenarios.json`. Market magnitudes for dated scenarios are
rounded one-day S&P 500 closes from public index history (1987-10-19 about -20.5 %, 2020-03-16 about
-12 %); sector, size and liquidity moves are hypothetical and labelled so. A factor the model does not
carry is skipped and listed in the result (`strict=True` refuses instead).
