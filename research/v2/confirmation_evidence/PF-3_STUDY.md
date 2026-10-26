# PF-3 combined-book confirmation: synthetic size and power evidence (U13)

Assembled 2026-10-26T08:19:35Z by `python/tests/spec/pf3_suite.py --assemble` (Python 3.12.10, numpy 2.4.6, SciPy 1.17.1); status **full**; 1135.3 s of cell time. Protocol: `research/v2/U13_CONFIRMATION_PROTOCOL.md`. The JSON beside this file is authoritative. Size: pass iff k <= 463 of 20,000 (one-sided 95 % Clopper-Pearson upper bound <= 0.025 at alpha_op 0.020). Power: >= 4,047 of 5,000 on the iid normal reference cells only. A pass is a validated-domain Monte Carlo claim, not a real-world false-positive rate.

Operating domain: `{"generators": ["garch11", "iid_normal", "t3"], "bounds": {"250": {"dep_length": 56.0, "vol_cluster": 0.33138265881203827, "excess_kurtosis": 151.4527098228916}, "500": {"dep_length": 57.0, "vol_cluster": 0.3268029625686939, "excess_kurtosis": 264.588512989742}}, "max_component_horizon": 0}`

| id | T | L (L_dep dev) | blocks | n | k | result | bound | screens (q99.5: dep_length / vol_cluster / kurtosis) |
|---|---:|---|---:|---:|---:|---|---:|---|
| PF-3/size/00/iid_normal_T250 | 250 | 5 (0) | 50 | 20000 | 337 | pass | 0.0184 | 54 / 0.173 / 1.07 |
| PF-3/size/01/iid_normal_T500 | 500 | 5 (0) | 100 | 20000 | 311 | pass | 0.0171 | 51 / 0.124 / 0.70 |
| PF-3/size/02/garch11_T250 | 250 | 5 (0) | 50 | 20000 | 344 | pass | 0.0188 | 56 / 0.331 / 4.01 |
| PF-3/size/03/garch11_T500 | 500 | 5 (0) | 100 | 20000 | 355 | pass | 0.0194 | 57 / 0.327 / 4.67 |
| PF-3/size/04/t3_T250 | 250 | 5 (0) | 50 | 20000 | 438 | pass | 0.0237 | 53 / 0.225 / 151.45 |
| PF-3/size/05/t3_T500 | 500 | 5 (0) | 100 | 20000 | 453 | pass | 0.0245 | 52 / 0.175 / 264.59 |
| PF-3/size/06/mix_w0.5_h_short3_h_long10_T250 | 250 | 60 (59) | 5 | 20000 | 461 | pass | 0.0249 | 60 / 0.155 / 1.39 |
| PF-3/size/07/mix_w0.5_h_short3_h_long10_T500 | 500 | 60 (60) | 9 | 20000 | 475 | fail | 0.0256 | 60 / 0.118 / 1.03 |
| PF-3/size/08/mix_w0.5_h_short3_h_long20_T250 | 250 | 60 (60) | 5 | 20000 | 666 | fail | 0.0355 | 60 / 0.157 / 1.33 |
| PF-3/size/09/mix_w0.5_h_short3_h_long20_T500 | 500 | 60 (58) | 9 | 20000 | 576 | fail | 0.0308 | 60 / 0.118 / 1.03 |
| PF-3/size/10/mix_w0.5_h_short3_h_long40_T250 | 250 | 60 (32) | 5 | 20000 | 960 | fail | 0.0506 | 60 / 0.170 / 1.23 |
| PF-3/size/11/mix_w0.5_h_short3_h_long40_T500 | 500 | 60 (54) | 9 | 20000 | 851 | fail | 0.0450 | 60 / 0.126 / 0.98 |
| PF-3/size/12/mix_w0.5_h_short3_h_long60_T250 | 250 | 60 (49) | 5 | 20000 | 1373 | fail | 0.0717 | 60 / 0.180 / 1.16 |
| PF-3/size/13/mix_w0.5_h_short3_h_long60_T500 | 500 | 60 (60) | 9 | 20000 | 1162 | fail | 0.0609 | 60 / 0.135 / 0.91 |
| PF-3/size/14/mix_garch_w0.5_h_short3_h_long10_T250 | 250 | 60 (31) | 5 | 20000 | 465 | fail | 0.0251 | 60 / 0.176 / 4.11 |
| PF-3/size/15/mix_garch_w0.5_h_short3_h_long10_T500 | 500 | 60 (60) | 9 | 20000 | 494 | fail | 0.0266 | 60 / 0.161 / 4.97 |
| PF-3/size/16/mix_t3_w0.5_h_short3_h_long10_T250 | 250 | 60 (60) | 5 | 20000 | 433 | pass | 0.0234 | 60 / 0.224 / 31.55 |
| PF-3/size/17/mix_t3_w0.5_h_short3_h_long10_T500 | 500 | 60 (32) | 9 | 20000 | 516 | fail | 0.0277 | 60 / 0.211 / 52.96 |
| PF-3/size/18/mix_regime_w_from0.8_w_to0.2_h_short3_h_long10_T250 | 250 | 54 (27) | 5 | 20000 | 350 | pass | 0.0191 | 60 / 0.163 / 1.44 |
| PF-3/size/19/mix_regime_w_from0.8_w_to0.2_h_short3_h_long10_T500 | 500 | 60 (46) | 9 | 20000 | 501 | fail | 0.0269 | 60 / 0.132 / 1.07 |
| PF-3/size/20/mix_ar_w0.5_rho0.3_h_long10_T250 | 250 | 60 (49) | 5 | 20000 | 470 | fail | 0.0253 | 60 / 0.218 / 1.14 |
| PF-3/size/21/mix_ar_w0.5_rho0.3_h_long10_T500 | 500 | 60 (53) | 9 | 20000 | 453 | pass | 0.0245 | 60 / 0.173 / 0.81 |
| PF-3/power/00/iid_normal_T250 | 250 | 5 (0) | 50 | 5000 | 4244 | pass | 0.8402 |  |
| PF-3/power/01/iid_normal_T500 | 500 | 5 (0) | 100 | 5000 | 4301 | pass | 0.8519 |  |
| PF-3/power/02/garch11_T250 | 250 | 54 (27) | 5 | 5000 | 2530 | reported | 0.4943 |  |
| PF-3/power/03/garch11_T500 | 500 | 5 (0) | 100 | 5000 | 4281 | reported | 0.8478 |  |
| PF-3/power/04/t3_T250 | 250 | 5 (0) | 50 | 5000 | 4264 | reported | 0.8443 |  |
| PF-3/power/05/t3_T500 | 500 | 5 (0) | 100 | 5000 | 4304 | reported | 0.8525 |  |
| PF-3/power/06/mix_w0.5_h_short3_h_long10_T250 | 250 | 60 (47) | 5 | 5000 | 500 | reported | 0.0931 |  |
| PF-3/power/07/mix_w0.5_h_short3_h_long10_T500 | 500 | 60 (49) | 9 | 5000 | 619 | reported | 0.1162 |  |
| PF-3/power/08/mix_w0.5_h_short3_h_long20_T250 | 250 | 60 (43) | 5 | 5000 | 353 | reported | 0.0647 |  |
| PF-3/power/09/mix_w0.5_h_short3_h_long20_T500 | 500 | 60 (59) | 9 | 5000 | 377 | reported | 0.0693 |  |
| PF-3/power/10/mix_w0.5_h_short3_h_long40_T250 | 250 | 60 (34) | 5 | 5000 | 349 | reported | 0.0640 |  |
| PF-3/power/11/mix_w0.5_h_short3_h_long40_T500 | 500 | 60 (59) | 9 | 5000 | 321 | reported | 0.0586 |  |
| PF-3/power/12/mix_w0.5_h_short3_h_long60_T250 | 250 | 60 (60) | 5 | 5000 | 397 | reported | 0.0732 |  |
| PF-3/power/13/mix_w0.5_h_short3_h_long60_T500 | 500 | 60 (54) | 9 | 5000 | 318 | reported | 0.0580 |  |
| PF-3/power/14/mix_garch_w0.5_h_short3_h_long10_T250 | 250 | 60 (55) | 5 | 5000 | 610 | reported | 0.1145 |  |
| PF-3/power/15/mix_garch_w0.5_h_short3_h_long10_T500 | 500 | 38 (19) | 14 | 5000 | 819 | reported | 0.1552 |  |
| PF-3/power/16/mix_t3_w0.5_h_short3_h_long10_T250 | 250 | 60 (38) | 5 | 5000 | 553 | reported | 0.1034 |  |
| PF-3/power/17/mix_t3_w0.5_h_short3_h_long10_T500 | 500 | 60 (33) | 9 | 5000 | 679 | reported | 0.1279 |  |
| PF-3/power/18/mix_regime_w_from0.8_w_to0.2_h_short3_h_long10_T250 | 250 | 60 (60) | 5 | 5000 | 507 | reported | 0.0945 |  |
| PF-3/power/19/mix_regime_w_from0.8_w_to0.2_h_short3_h_long10_T500 | 500 | 60 (58) | 9 | 5000 | 589 | reported | 0.1104 |  |
| PF-3/power/20/mix_ar_w0.5_rho0.3_h_long10_T250 | 250 | 12 (6) | 21 | 5000 | 917 | reported | 0.1744 |  |
| PF-3/power/21/mix_ar_w0.5_rho0.3_h_long10_T500 | 500 | 60 (48) | 9 | 5000 | 602 | reported | 0.1129 |  |
