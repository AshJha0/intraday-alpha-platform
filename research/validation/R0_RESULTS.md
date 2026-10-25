# R0 adversarial Monte Carlo results (R3; v1.19 stage 1)

Generated from `R0_RESULTS.json` (run 2026-10-25T07:30:34Z, Python 3.12.10, numpy 2.4.6, SciPy 1.17.1, statsmodels None; `python/tests/spec/r3_suite.py --write`). Specification: `python/tests/spec/CONFIRMATORY_TEST_SPEC.md` (R0 revision 4, frozen). Seeds `20301000 + 100 * case + cell`; a re-run with other seeds is a new R0 revision, never a retry. Size acceptance k <= 463 of 20,000 (one-sided 95 % Clopper-Pearson upper bound <= 0.025 at alpha_op 0.020); power acceptance >= 4,047 detections of 5,000.

## Summary

- claimed-domain size cells: 29 of 29 pass; largest k 433
- power cells: 2 of 2 pass
- candidate domain D-BLK: 6 of 12 cells pass; `dblk_validated` = false -> **D-BLK is withdrawn for v1.19 in full** (R0 section 2; the gate refuses every D-BLK declaration; block length not re-optimised)
- stress cells: 23 reported, no claim, never fail CI
- R3-D selection: clean k 400 (pass), contaminated k 2069 (inflation recorded; the gate refuses the declaration)

A pass means: under the predeclared synthetic data-generating processes of the claimed domain, the procedure's simulated family-wise error rate met the predeclared Monte Carlo acceptance criterion. It is a validated-domain Monte Carlo claim, not a real-world false-positive rate (R0 section 1).

## Cells

| id | class | procedure | G | n | k / detections | estimate | 95 % bound | result | gate |
|---|---|---|---:|---:|---:|---:|---:|---|---|
| R3-A/00/ar1_rho0_G10_P-IND | claimed | P-IND | 10 | 20000 | 395 | 0.0198 | upper 0.0214 | pass | admitted |
| R3-A/01/ar1_rho0_G20_P-IND | claimed | P-IND | 20 | 20000 | 400 | 0.0200 | upper 0.0217 | pass | admitted |
| R3-A/02/ar1_rho0_G20_P-BLK | candidate | P-BLK | 20 | 20000 | 389 | 0.0194 | upper 0.0211 | pass | refused: dependence assumptions unsupported (D-BLK withdrawn for v1.19) |
| R3-A/03/ar1_rho0_G30_P-IND | claimed | P-IND | 30 | 20000 | 348 | 0.0174 | upper 0.0190 | pass | admitted |
| R3-A/04/ar1_rho0_G30_P-BLK | candidate | P-BLK | 30 | 20000 | 405 | 0.0203 | upper 0.0220 | pass | refused: dependence assumptions unsupported (D-BLK withdrawn for v1.19) |
| R3-A/05/ar1_rho0_G60_P-IND | claimed | P-IND | 60 | 20000 | 433 | 0.0216 | upper 0.0234 | pass | admitted |
| R3-A/06/ar1_rho0_G60_P-BLK | candidate | P-BLK | 60 | 20000 | 377 | 0.0188 | upper 0.0205 | pass | refused: dependence assumptions unsupported (D-BLK withdrawn for v1.19) |
| R3-A/07/ar1_rho0.1_G10_P-IND | stress | P-IND | 10 | 20000 | 754 | 0.0377 | upper 0.0400 | reported | refused: dependence assumptions unsupported |
| R3-A/08/ar1_rho0.1_G20_P-IND | stress | P-IND | 20 | 20000 | 851 | 0.0425 | upper 0.0450 | reported | refused: dependence assumptions unsupported |
| R3-A/09/ar1_rho0.1_G20_P-BLK | candidate | P-BLK | 20 | 20000 | 447 | 0.0223 | upper 0.0241 | pass | refused: dependence assumptions unsupported (D-BLK withdrawn for v1.19) |
| R3-A/10/ar1_rho0.1_G30_P-IND | stress | P-IND | 30 | 20000 | 868 | 0.0434 | upper 0.0458 | reported | refused: dependence assumptions unsupported |
| R3-A/11/ar1_rho0.1_G30_P-BLK | candidate | P-BLK | 30 | 20000 | 460 | 0.0230 | upper 0.0248 | pass | refused: dependence assumptions unsupported (D-BLK withdrawn for v1.19) |
| R3-A/12/ar1_rho0.1_G60_P-IND | stress | P-IND | 60 | 20000 | 900 | 0.0450 | upper 0.0475 | reported | refused: dependence assumptions unsupported |
| R3-A/13/ar1_rho0.1_G60_P-BLK | candidate | P-BLK | 60 | 20000 | 429 | 0.0215 | upper 0.0232 | pass | refused: dependence assumptions unsupported (D-BLK withdrawn for v1.19) |
| R3-A/14/ar1_rho0.2_G10_P-IND | stress | P-IND | 10 | 20000 | 1269 | 0.0635 | upper 0.0664 | reported | refused: dependence assumptions unsupported |
| R3-A/15/ar1_rho0.2_G20_P-IND | stress | P-IND | 20 | 20000 | 1736 | 0.0868 | upper 0.0901 | reported | refused: dependence assumptions unsupported |
| R3-A/16/ar1_rho0.2_G20_P-BLK | candidate | P-BLK | 20 | 20000 | 468 | 0.0234 | upper 0.0252 | fail | refused: dependence assumptions unsupported (D-BLK withdrawn for v1.19) |
| R3-A/17/ar1_rho0.2_G30_P-IND | stress | P-IND | 30 | 20000 | 1766 | 0.0883 | upper 0.0917 | reported | refused: dependence assumptions unsupported |
| R3-A/18/ar1_rho0.2_G30_P-BLK | candidate | P-BLK | 30 | 20000 | 477 | 0.0238 | upper 0.0257 | fail | refused: dependence assumptions unsupported (D-BLK withdrawn for v1.19) |
| R3-A/19/ar1_rho0.2_G60_P-IND | stress | P-IND | 60 | 20000 | 1933 | 0.0966 | upper 0.1002 | reported | refused: dependence assumptions unsupported |
| R3-A/20/ar1_rho0.2_G60_P-BLK | candidate | P-BLK | 60 | 20000 | 522 | 0.0261 | upper 0.0280 | fail | refused: dependence assumptions unsupported (D-BLK withdrawn for v1.19) |
| R3-A/21/ar1_rho0.3_G10_P-IND | stress | P-IND | 10 | 20000 | 2312 | 0.1156 | upper 0.1194 | reported | refused: dependence assumptions unsupported |
| R3-A/22/ar1_rho0.3_G20_P-IND | stress | P-IND | 20 | 20000 | 3109 | 0.1555 | upper 0.1597 | reported | refused: dependence assumptions unsupported |
| R3-A/23/ar1_rho0.3_G20_P-BLK | candidate | P-BLK | 20 | 20000 | 528 | 0.0264 | upper 0.0283 | fail | refused: dependence assumptions unsupported (D-BLK withdrawn for v1.19) |
| R3-A/24/ar1_rho0.3_G30_P-IND | stress | P-IND | 30 | 20000 | 3334 | 0.1667 | upper 0.1711 | reported | refused: dependence assumptions unsupported |
| R3-A/25/ar1_rho0.3_G30_P-BLK | candidate | P-BLK | 30 | 20000 | 556 | 0.0278 | upper 0.0298 | fail | refused: dependence assumptions unsupported (D-BLK withdrawn for v1.19) |
| R3-A/26/ar1_rho0.3_G60_P-IND | stress | P-IND | 60 | 20000 | 3503 | 0.1752 | upper 0.1796 | reported | refused: dependence assumptions unsupported |
| R3-A/27/ar1_rho0.3_G60_P-BLK | candidate | P-BLK | 60 | 20000 | 623 | 0.0312 | upper 0.0332 | fail | refused: dependence assumptions unsupported (D-BLK withdrawn for v1.19) |
| R3-A/28/ar1_rho0.5_G10_P-IND | stress | P-IND | 10 | 20000 | 6687 | 0.3343 | upper 0.3399 | reported | refused: dependence assumptions unsupported |
| R3-A/29/ar1_rho0.5_G20_P-IND | stress | P-IND | 20 | 20000 | 8529 | 0.4264 | upper 0.4322 | reported | refused: dependence assumptions unsupported |
| R3-A/30/ar1_rho0.5_G20_P-BLK | stress | P-BLK | 20 | 20000 | 656 | 0.0328 | upper 0.0349 | reported | refused: dependence assumptions unsupported |
| R3-A/31/ar1_rho0.5_G30_P-IND | stress | P-IND | 30 | 20000 | 9088 | 0.4544 | upper 0.4602 | reported | refused: dependence assumptions unsupported |
| R3-A/32/ar1_rho0.5_G30_P-BLK | stress | P-BLK | 30 | 20000 | 799 | 0.0399 | upper 0.0423 | reported | refused: dependence assumptions unsupported |
| R3-A/33/ar1_rho0.5_G60_P-IND | stress | P-IND | 60 | 20000 | 9378 | 0.4689 | upper 0.4747 | reported | refused: dependence assumptions unsupported |
| R3-A/34/ar1_rho0.5_G60_P-BLK | stress | P-BLK | 60 | 20000 | 1096 | 0.0548 | upper 0.0575 | reported | refused: dependence assumptions unsupported |
| R3-B/00/equicorr_rhoh0_G10 | claimed | P-IND | 10 | 20000 | 383 | 0.0192 | upper 0.0208 | pass | admitted |
| R3-B/01/equicorr_rhoh0_G30 | claimed | P-IND | 30 | 20000 | 393 | 0.0197 | upper 0.0213 | pass | admitted |
| R3-B/02/equicorr_rhoh0.3_G10 | claimed | P-IND | 10 | 20000 | 396 | 0.0198 | upper 0.0215 | pass | admitted |
| R3-B/03/equicorr_rhoh0.3_G30 | claimed | P-IND | 30 | 20000 | 361 | 0.0181 | upper 0.0197 | pass | admitted |
| R3-B/04/equicorr_rhoh0.6_G10 | claimed | P-IND | 10 | 20000 | 320 | 0.0160 | upper 0.0175 | pass | admitted |
| R3-B/05/equicorr_rhoh0.6_G30 | claimed | P-IND | 30 | 20000 | 273 | 0.0137 | upper 0.0151 | pass | admitted |
| R3-B/06/equicorr_rhoh0.9_G10 | claimed | P-IND | 10 | 20000 | 151 | 0.0076 | upper 0.0086 | pass | admitted |
| R3-B/07/equicorr_rhoh0.9_G30 | claimed | P-IND | 30 | 20000 | 117 | 0.0059 | upper 0.0068 | pass | admitted |
| R3-C/00/normal_homoskedastic_G10 | claimed | P-IND | 10 | 20000 | 404 | 0.0202 | upper 0.0219 | pass | admitted |
| R3-C/01/normal_homoskedastic_G15 | claimed | P-IND | 15 | 20000 | 425 | 0.0213 | upper 0.0230 | pass | admitted |
| R3-C/02/normal_homoskedastic_G20 | claimed | P-IND | 20 | 20000 | 404 | 0.0202 | upper 0.0219 | pass | admitted |
| R3-C/03/normal_homoskedastic_G30 | claimed | P-IND | 30 | 20000 | 412 | 0.0206 | upper 0.0223 | pass | admitted |
| R3-C/04/normal_homoskedastic_G45 | claimed | P-IND | 45 | 20000 | 387 | 0.0193 | upper 0.0210 | pass | admitted |
| R3-C/05/normal_homoskedastic_G60 | claimed | P-IND | 60 | 20000 | 413 | 0.0207 | upper 0.0224 | pass | admitted |
| R3-C/06/normal_homoskedastic_G90 | claimed | P-IND | 90 | 20000 | 388 | 0.0194 | upper 0.0211 | pass | admitted |
| R3-C/07/normal_heteroskedastic_G10 | claimed | P-IND | 10 | 20000 | 174 | 0.0087 | upper 0.0099 | pass | admitted |
| R3-C/08/normal_heteroskedastic_G15 | claimed | P-IND | 15 | 20000 | 198 | 0.0099 | upper 0.0111 | pass | admitted |
| R3-C/09/normal_heteroskedastic_G20 | claimed | P-IND | 20 | 20000 | 238 | 0.0119 | upper 0.0132 | pass | admitted |
| R3-C/10/normal_heteroskedastic_G30 | claimed | P-IND | 30 | 20000 | 258 | 0.0129 | upper 0.0143 | pass | admitted |
| R3-C/11/normal_heteroskedastic_G45 | claimed | P-IND | 45 | 20000 | 334 | 0.0167 | upper 0.0183 | pass | admitted |
| R3-C/12/normal_heteroskedastic_G60 | claimed | P-IND | 60 | 20000 | 292 | 0.0146 | upper 0.0161 | pass | admitted |
| R3-C/13/normal_heteroskedastic_G90 | claimed | P-IND | 90 | 20000 | 369 | 0.0185 | upper 0.0201 | pass | admitted |
| R3-C/14/t3_heteroskedastic_G10 | stress | P-IND | 10 | 20000 | 107 | 0.0053 | upper 0.0063 | reported | refused: distribution outside validated domain |
| R3-C/15/t3_heteroskedastic_G30 | stress | P-IND | 30 | 20000 | 149 | 0.0075 | upper 0.0085 | reported | refused: distribution outside validated domain |
| R3-C/16/lognormal_heteroskedastic_G10 | stress | P-IND | 10 | 20000 | 0 | 0.0000 | upper 0.0001 | reported | refused: distribution outside validated domain |
| R3-C/17/lognormal_heteroskedastic_G30 | stress | P-IND | 30 | 20000 | 0 | 0.0000 | upper 0.0001 | reported | refused: distribution outside validated domain |
| R3-C/18/power_normal_homoskedastic_G10 | power | P-IND | 10 | 5000 | 4487 | 0.8974 | lower 0.8901 | pass | admitted |
| R3-C/19/power_normal_homoskedastic_G30 | power | P-IND | 30 | 5000 | 4486 | 0.8972 | lower 0.8899 | pass | admitted |
| R3-D/00/clean_selection_best3_G20 | claimed | P-IND | 20 | 20000 | 400 | 0.0200 | upper 0.0217 | pass | admitted |
| R3-E/00/missing_10pct_dropped_G20 | claimed | P-IND | 20 | 20000 | 403 | 0.0202 | upper 0.0219 | pass | admitted |
| R3-E/01/unequal_usable_counts_G20 | claimed | P-IND | 20 | 20000 | 379 | 0.0190 | upper 0.0206 | pass | admitted |

## Selection and repeated looks (R3-D)

- (i) clean: 16 candidates on a calibration panel, the best 3 registered, tested on an independent confirmation panel (K = 3): k = 400, pass.
- (ii) contaminated: selection and test on the confirmation panel: k = 2069 (reported); gate: `refused: looks on confirmation data by the same family`.
- (iii) repeated look on a spent window: gate: `refused: confirmation window spent` before any statistic.

## Power report identification (R0 section 7)

- R3-C/18/power_normal_homoskedastic_G10: P-IND, G = 10, K = 16, planted H00 at 1.8419 sd (theoretical power 0.9, one-sided Student t at alpha_op / K (the Holm floor), G - 1 df), generator normal_homoskedastic, n = 5000, detections 4487, lower bound 0.8901. Not carried over to P-BLK, tier 2 or the real-data H1 experiment (E4).
- R3-C/19/power_normal_homoskedastic_G30: P-IND, G = 30, K = 16, planted H00 at 0.8543 sd (theoretical power 0.9, one-sided Student t at alpha_op / K (the Holm floor), G - 1 df), generator normal_homoskedastic, n = 5000, detections 4486, lower bound 0.8899. Not carried over to P-BLK, tier 2 or the real-data H1 experiment (E4).

## References (defect D-36)

SciPy and the textbook implementations in `python/tests/test_confirmatory.py` agree with the gate at the R0 tolerances. The statsmodels comparison was not obtained locally (the cp312 statsmodels 0.15.0 wheel's compiled extension failed to import in the scratch environment); CI skips it with a reason. It stays owed.
