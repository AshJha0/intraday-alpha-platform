# Confirmation profiles PF-1 and PF-2: synthetic size and power evidence (U13)

Generated from the `<profile>_RESULTS.json` files in this directory by `python/tests/spec/pf_suite.py --write`. Specification: `python/tests/spec/CONFIRMATION_PROFILES.md`; framework: `research/v2/CONFIRMATION_FRAMEWORK.md`. Size acceptance k <= 463 of 20,000 per cell (one-sided 95 % Clopper-Pearson upper bound <= 0.025 at alpha_op 0.020, the R0 form); power acceptance >= 4,047 detections of 5,000 on the iid normal reference cells only. A pass means: under the predeclared synthetic data-generating process of the cell, the procedure's simulated family-wise error rate met the predeclared Monte Carlo acceptance criterion. It is a validated-domain Monte Carlo claim, not a real-world false-positive rate.

## PF-1: daily marked-to-market portfolio return of a 1-5 session horizon book

Run 2026-10-25T16:31:13Z, Python 3.12.10, numpy 2.4.6, SciPy 1.17.1; 1397.9 s on one core; K = 4; T grid [120, 250].

### Operating domain (size passes at every T of the grid)

| procedure | reference passes | generator cells in domain | acf1 max | GARCH | t3 |
|---|---|---|---:|---|---|
| P-HAC-L5 | True | iid_normal, garch11{"alpha": 0.08, "beta": 0.9}, t3 | 0.000 | True | True |
| P-CBB | False | t3 | 0.000 | False | True |

### size cells

| id | generator | T | n | P-HAC-L5 k / result / bound | P-CBB k / result / bound |
|---|---|---:|---:|---|---|
| PF-1/size/00/iid_normal_T120 | iid_normal | 120 | 20000 | 424 / pass / 0.0230 | 576 / fail / 0.0308 L=5 |
| PF-1/size/01/iid_normal_T250 | iid_normal | 250 | 20000 | 428 / pass / 0.0232 | 504 / fail / 0.0271 L=5 |
| PF-1/size/02/ar1_rho0.1_T120 | ar1 {"rho": 0.1} | 120 | 20000 | 457 / pass / 0.0247 | 638 / fail / 0.0340 L=5 |
| PF-1/size/03/ar1_rho0.1_T250 | ar1 {"rho": 0.1} | 250 | 20000 | 477 / fail / 0.0257 | 559 / fail / 0.0299 L=5 |
| PF-1/size/04/ar1_rho0.2_T120 | ar1 {"rho": 0.2} | 120 | 20000 | 589 / fail / 0.0315 | 770 / fail / 0.0408 L=5 |
| PF-1/size/05/ar1_rho0.2_T250 | ar1 {"rho": 0.2} | 250 | 20000 | 534 / fail / 0.0287 | 629 / fail / 0.0336 L=5 |
| PF-1/size/06/ar1_rho0.3_T120 | ar1 {"rho": 0.3} | 120 | 20000 | 695 / fail / 0.0370 | 944 / fail / 0.0497 L=5 |
| PF-1/size/07/ar1_rho0.3_T250 | ar1 {"rho": 0.3} | 250 | 20000 | 657 / fail / 0.0350 | 798 / fail / 0.0423 L=5 |
| PF-1/size/08/ar1_rho0.5_T120 | ar1 {"rho": 0.5} | 120 | 20000 | 1049 / fail / 0.0551 | 1639 / fail / 0.0852 L=20 |
| PF-1/size/09/ar1_rho0.5_T250 | ar1 {"rho": 0.5} | 250 | 20000 | 938 / fail / 0.0494 | 1182 / fail / 0.0619 L=5 |
| PF-1/size/10/garch11_alpha0.08_beta0.9_T120 | garch11 {"alpha": 0.08, "beta": 0.9} | 120 | 20000 | 403 / pass / 0.0219 | 511 / fail / 0.0275 L=5 |
| PF-1/size/11/garch11_alpha0.08_beta0.9_T250 | garch11 {"alpha": 0.08, "beta": 0.9} | 250 | 20000 | 379 / pass / 0.0206 | 403 / pass / 0.0219 L=5 |
| PF-1/size/12/t3_T120 | t3 | 120 | 20000 | 351 / pass / 0.0192 | 439 / pass / 0.0237 L=5 |
| PF-1/size/13/t3_T250 | t3 | 250 | 20000 | 318 / pass / 0.0174 | 328 / pass / 0.0180 L=5 |
| PF-1/size/14/overlap_ma_h2_T120 | overlap_ma {"h": 2} | 120 | 20000 | 581 / fail / 0.0311 | 1443 / fail / 0.0752 L=20 |
| PF-1/size/15/overlap_ma_h2_T250 | overlap_ma {"h": 2} | 250 | 20000 | 574 / fail / 0.0307 | 703 / fail / 0.0374 L=5 |
| PF-1/size/16/overlap_ma_h3_T120 | overlap_ma {"h": 3} | 120 | 20000 | 770 / fail / 0.0408 | 1445 / fail / 0.0753 L=20 |
| PF-1/size/17/overlap_ma_h3_T250 | overlap_ma {"h": 3} | 250 | 20000 | 692 / fail / 0.0368 | 951 / fail / 0.0501 L=20 |
| PF-1/size/18/overlap_ma_h5_T120 | overlap_ma {"h": 5} | 120 | 20000 | 1228 / fail / 0.0643 | 1793 / fail / 0.0930 L=5 |
| PF-1/size/19/overlap_ma_h5_T250 | overlap_ma {"h": 5} | 250 | 20000 | 1115 / fail / 0.0585 | 1286 / fail / 0.0672 L=6 |

### power cells

| id | generator | T | n | P-HAC-L5 k / result / bound | P-CBB k / result / bound |
|---|---|---:|---:|---|---|
| PF-1/power/00/iid_normal_T120 | iid_normal delta=0.3571 | 120 | 5000 | 4387 / pass / 0.8695 | 4473 / pass / 0.8872 L=5 |
| PF-1/power/01/iid_normal_T250 | iid_normal delta=0.2456 | 250 | 5000 | 4402 / pass / 0.8726 | 4438 / pass / 0.8800 L=6 |
| PF-1/power/02/ar1_rho0.1_T120 | ar1 {"rho": 0.1} delta=0.3571 | 120 | 5000 | 4020 / reported / 0.7945 | 4140 / reported / 0.8190 L=5 |
| PF-1/power/03/ar1_rho0.1_T250 | ar1 {"rho": 0.1} delta=0.2456 | 250 | 5000 | 4060 / reported / 0.8027 | 4113 / reported / 0.8135 L=5 |
| PF-1/power/04/ar1_rho0.2_T120 | ar1 {"rho": 0.2} delta=0.3571 | 120 | 5000 | 3582 / reported / 0.7057 | 3785 / reported / 0.7468 L=5 |
| PF-1/power/05/ar1_rho0.2_T250 | ar1 {"rho": 0.2} delta=0.2456 | 250 | 5000 | 3729 / reported / 0.7355 | 3811 / reported / 0.7521 L=5 |
| PF-1/power/06/ar1_rho0.3_T120 | ar1 {"rho": 0.3} delta=0.3571 | 120 | 5000 | 3109 / reported / 0.6104 | 3360 / reported / 0.6609 L=5 |
| PF-1/power/07/ar1_rho0.3_T250 | ar1 {"rho": 0.3} delta=0.2456 | 250 | 5000 | 3217 / reported / 0.6321 | 3307 / reported / 0.6502 L=5 |
| PF-1/power/08/ar1_rho0.5_T120 | ar1 {"rho": 0.5} delta=0.3571 | 120 | 5000 | 2330 / reported / 0.4543 | 2484 / reported / 0.4851 L=14 |
| PF-1/power/09/ar1_rho0.5_T250 | ar1 {"rho": 0.5} delta=0.2456 | 250 | 5000 | 2300 / reported / 0.4483 | 2154 / reported / 0.4192 L=12 |
| PF-1/power/10/garch11_alpha0.08_beta0.9_T120 | garch11 {"alpha": 0.08, "beta": 0.9} delta=0.3571 | 120 | 5000 | 4310 / reported / 0.8537 | 4420 / reported / 0.8763 L=5 |
| PF-1/power/11/garch11_alpha0.08_beta0.9_T250 | garch11 {"alpha": 0.08, "beta": 0.9} delta=0.2456 | 250 | 5000 | 4428 / reported / 0.8779 | 4452 / reported / 0.8829 L=5 |
| PF-1/power/12/t3_T120 | t3 delta=0.3571 | 120 | 5000 | 4417 / reported / 0.8757 | 4529 / reported / 0.8987 L=5 |
| PF-1/power/13/t3_T250 | t3 delta=0.2456 | 250 | 5000 | 4492 / reported / 0.8911 | 4511 / reported / 0.8950 L=5 |
| PF-1/power/14/overlap_ma_h2_T120 | overlap_ma {"h": 2} delta=0.3571 | 120 | 5000 | 2874 / reported / 0.5632 | 3103 / reported / 0.6092 L=5 |
| PF-1/power/15/overlap_ma_h2_T250 | overlap_ma {"h": 2} delta=0.2456 | 250 | 5000 | 2957 / reported / 0.5798 | 3002 / reported / 0.5889 L=20 |
| PF-1/power/16/overlap_ma_h3_T120 | overlap_ma {"h": 3} delta=0.3571 | 120 | 5000 | 2133 / reported / 0.4150 | 2373 / reported / 0.4629 L=20 |
| PF-1/power/17/overlap_ma_h3_T250 | overlap_ma {"h": 3} delta=0.2456 | 250 | 5000 | 2139 / reported / 0.4162 | 2130 / reported / 0.4144 L=20 |
| PF-1/power/18/overlap_ma_h5_T120 | overlap_ma {"h": 5} delta=0.3571 | 120 | 5000 | 1518 / reported / 0.2929 | 1504 / reported / 0.2901 L=20 |
| PF-1/power/19/overlap_ma_h5_T250 | overlap_ma {"h": 5} delta=0.2456 | 250 | 5000 | 1615 / reported / 0.3121 | 1440 / reported / 0.2775 L=20 |

## PF-2: daily marked-to-market portfolio return of a 20-60 session horizon book with overlapping positions

Run 2026-10-25T16:31:16Z, Python 3.12.10, numpy 2.4.6, SciPy 1.17.1; 1727.7 s on one core; K = 4; T grid [250, 500].

### Operating domain (size passes at every T of the grid)

| procedure | reference passes | generator cells in domain | acf1 max | GARCH | t3 |
|---|---|---|---:|---|---|
| P-HAC-L20 | True | iid_normal, ar1{"rho": 0.1}, ar1{"rho": 0.2}, ar1{"rho": 0.3}, garch11{"alpha": 0.08, "beta": 0.9}, t3 | 0.300 | True | True |
| P-HAC-L60 | False | (none) | 0.000 | False | False |
| P-CBB | False | (none) | 0.000 | False | False |

### size cells

| id | generator | T | n | P-HAC-L20 k / result / bound | P-HAC-L60 k / result / bound | P-CBB k / result / bound |
|---|---|---:|---:|---|---|---|
| PF-2/size/00/iid_normal_T250 | iid_normal | 250 | 20000 | 363 / pass / 0.0198 | 220 / pass / 0.0123 | 553 / fail / 0.0296 L=10 |
| PF-2/size/01/iid_normal_T500 | iid_normal | 500 | 20000 | 376 / pass / 0.0205 | 331 / pass / 0.0181 | 466 / fail / 0.0251 L=10 |
| PF-2/size/02/ar1_rho0.1_T250 | ar1 {"rho": 0.1} | 250 | 20000 | 391 / pass / 0.0212 | 237 / pass / 0.0132 | 589 / fail / 0.0315 L=10 |
| PF-2/size/03/ar1_rho0.1_T500 | ar1 {"rho": 0.1} | 500 | 20000 | 376 / pass / 0.0205 | 349 / pass / 0.0191 | 469 / fail / 0.0253 L=10 |
| PF-2/size/04/ar1_rho0.2_T250 | ar1 {"rho": 0.2} | 250 | 20000 | 425 / pass / 0.0230 | 252 / pass / 0.0140 | 690 / fail / 0.0367 L=10 |
| PF-2/size/05/ar1_rho0.2_T500 | ar1 {"rho": 0.2} | 500 | 20000 | 420 / pass / 0.0227 | 365 / pass / 0.0199 | 553 / fail / 0.0296 L=10 |
| PF-2/size/06/ar1_rho0.3_T250 | ar1 {"rho": 0.3} | 250 | 20000 | 398 / pass / 0.0216 | 229 / pass / 0.0128 | 641 / fail / 0.0342 L=10 |
| PF-2/size/07/ar1_rho0.3_T500 | ar1 {"rho": 0.3} | 500 | 20000 | 459 / pass / 0.0248 | 364 / pass / 0.0198 | 617 / fail / 0.0329 L=10 |
| PF-2/size/08/ar1_rho0.5_T250 | ar1 {"rho": 0.5} | 250 | 20000 | 504 / fail / 0.0271 | 295 / pass / 0.0162 | 885 / fail / 0.0467 L=10 |
| PF-2/size/09/ar1_rho0.5_T500 | ar1 {"rho": 0.5} | 500 | 20000 | 531 / fail / 0.0285 | 407 / pass / 0.0221 | 1036 / fail / 0.0545 L=56 |
| PF-2/size/10/garch11_alpha0.08_beta0.9_T250 | garch11 {"alpha": 0.08, "beta": 0.9} | 250 | 20000 | 346 / pass / 0.0189 | 189 / pass / 0.0107 | 476 / fail / 0.0256 L=10 |
| PF-2/size/11/garch11_alpha0.08_beta0.9_T500 | garch11 {"alpha": 0.08, "beta": 0.9} | 500 | 20000 | 350 / pass / 0.0191 | 276 / pass / 0.0152 | 388 / pass / 0.0211 L=10 |
| PF-2/size/12/t3_T250 | t3 | 250 | 20000 | 351 / pass / 0.0192 | 223 / pass / 0.0125 | 463 / pass / 0.0250 L=10 |
| PF-2/size/13/t3_T500 | t3 | 500 | 20000 | 368 / pass / 0.0200 | 319 / pass / 0.0175 | 477 / fail / 0.0257 L=16 |
| PF-2/size/14/overlap_ma_h20_T250 | overlap_ma {"h": 20} | 250 | 20000 | 1511 / fail / 0.0787 | 573 / fail / 0.0307 | 2244 / fail / 0.1159 L=32 |
| PF-2/size/15/overlap_ma_h20_T500 | overlap_ma {"h": 20} | 500 | 20000 | 1412 / fail / 0.0737 | 624 / fail / 0.0333 | 1716 / fail / 0.0891 L=60 |
| PF-2/size/16/overlap_ma_h40_T250 | overlap_ma {"h": 40} | 250 | 20000 | 4175 / fail / 0.2135 | 1123 / fail / 0.0589 | 4707 / fail / 0.2403 L=60 |
| PF-2/size/17/overlap_ma_h40_T500 | overlap_ma {"h": 40} | 500 | 20000 | 3735 / fail / 0.1913 | 1075 / fail / 0.0564 | 2461 / fail / 0.1269 L=60 |
| PF-2/size/18/overlap_ma_h60_T250 | overlap_ma {"h": 60} | 250 | 20000 | 6689 / fail / 0.3400 | 1785 / fail / 0.0926 | 5941 / fail / 0.3024 L=60 |
| PF-2/size/19/overlap_ma_h60_T500 | overlap_ma {"h": 60} | 500 | 20000 | 6112 / fail / 0.3110 | 1656 / fail / 0.0861 | 3381 / fail / 0.1735 L=60 |

### power cells

| id | generator | T | n | P-HAC-L20 k / result / bound | P-HAC-L60 k / result / bound | P-CBB k / result / bound |
|---|---|---:|---:|---|---|---|
| PF-2/power/00/iid_normal_T250 | iid_normal delta=0.2456 | 250 | 5000 | 4136 / pass / 0.8182 | 3001 / fail / 0.5887 | 4445 / pass / 0.8814 L=10 |
| PF-2/power/01/iid_normal_T500 | iid_normal delta=0.1731 | 500 | 5000 | 4318 / pass / 0.8554 | 3862 / fail / 0.7624 | 4439 / pass / 0.8802 L=10 |
| PF-2/power/02/ar1_rho0.1_T250 | ar1 {"rho": 0.1} delta=0.2456 | 250 | 5000 | 3667 / reported / 0.7229 | 2466 / reported / 0.4815 | 4102 / reported / 0.8112 L=10 |
| PF-2/power/03/ar1_rho0.1_T500 | ar1 {"rho": 0.1} delta=0.1731 | 500 | 5000 | 3950 / reported / 0.7803 | 3356 / reported / 0.6601 | 4099 / reported / 0.8106 L=10 |
| PF-2/power/04/ar1_rho0.2_T250 | ar1 {"rho": 0.2} delta=0.2456 | 250 | 5000 | 3208 / reported / 0.6303 | 2019 / reported / 0.3923 | 3701 / reported / 0.7298 L=10 |
| PF-2/power/05/ar1_rho0.2_T500 | ar1 {"rho": 0.2} delta=0.1731 | 500 | 5000 | 3410 / reported / 0.6710 | 2858 / reported / 0.5600 | 3620 / reported / 0.7134 L=10 |
| PF-2/power/06/ar1_rho0.3_T250 | ar1 {"rho": 0.3} delta=0.2456 | 250 | 5000 | 2671 / reported / 0.5225 | 1552 / reported / 0.2996 | 3183 / reported / 0.6253 L=10 |
| PF-2/power/07/ar1_rho0.3_T500 | ar1 {"rho": 0.3} delta=0.1731 | 500 | 5000 | 2925 / reported / 0.5734 | 2358 / reported / 0.4599 | 3304 / reported / 0.6496 L=60 |
| PF-2/power/08/ar1_rho0.5_T250 | ar1 {"rho": 0.5} delta=0.2456 | 250 | 5000 | 1664 / reported / 0.3218 | 924 / reported / 0.1758 | 2240 / reported / 0.4364 L=10 |
| PF-2/power/09/ar1_rho0.5_T500 | ar1 {"rho": 0.5} delta=0.1731 | 500 | 5000 | 1774 / reported / 0.3436 | 1314 / reported / 0.2526 | 2213 / reported / 0.4310 L=60 |
| PF-2/power/10/garch11_alpha0.08_beta0.9_T250 | garch11 {"alpha": 0.08, "beta": 0.9} delta=0.2456 | 250 | 5000 | 4154 / reported / 0.8218 | 3200 / reported / 0.6287 | 4406 / reported / 0.8734 L=10 |
| PF-2/power/11/garch11_alpha0.08_beta0.9_T500 | garch11 {"alpha": 0.08, "beta": 0.9} delta=0.1731 | 500 | 5000 | 4314 / reported / 0.8545 | 3931 / reported / 0.7764 | 4419 / reported / 0.8761 L=10 |
| PF-2/power/12/t3_T250 | t3 delta=0.2456 | 250 | 5000 | 4234 / reported / 0.8382 | 3172 / reported / 0.6230 | 4485 / reported / 0.8897 L=10 |
| PF-2/power/13/t3_T500 | t3 delta=0.1731 | 500 | 5000 | 4366 / reported / 0.8652 | 3946 / reported / 0.7795 | 4470 / reported / 0.8866 L=10 |
| PF-2/power/14/overlap_ma_h20_T250 | overlap_ma {"h": 20} delta=0.2456 | 250 | 5000 | 538 / reported / 0.1005 | 212 / reported / 0.0378 | 914 / reported / 0.1738 L=60 |
| PF-2/power/15/overlap_ma_h20_T500 | overlap_ma {"h": 20} delta=0.1731 | 500 | 5000 | 547 / reported / 0.1022 | 274 / reported / 0.0496 | 575 / reported / 0.1076 L=60 |
| PF-2/power/16/overlap_ma_h40_T250 | overlap_ma {"h": 40} delta=0.2456 | 250 | 5000 | 815 / reported / 0.1545 | 211 / reported / 0.0376 | 862 / reported / 0.1637 L=60 |
| PF-2/power/17/overlap_ma_h40_T500 | overlap_ma {"h": 40} delta=0.1731 | 500 | 5000 | 748 / reported / 0.1414 | 225 / reported / 0.0403 | 481 / reported / 0.0894 L=60 |
| PF-2/power/18/overlap_ma_h60_T250 | overlap_ma {"h": 60} delta=0.2456 | 250 | 5000 | 1030 / reported / 0.1966 | 279 / reported / 0.0506 | 898 / reported / 0.1707 L=60 |
| PF-2/power/19/overlap_ma_h60_T500 | overlap_ma {"h": 60} delta=0.1731 | 500 | 5000 | 999 / reported / 0.1905 | 304 / reported / 0.0553 | 566 / reported / 0.1059 L=60 |

