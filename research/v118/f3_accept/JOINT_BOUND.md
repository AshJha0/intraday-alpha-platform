# F3 acceptance study: joint false-fail bound (UNSIGNED DRAFT; arithmetic only)

n = 50000 datasets per cell; pass iff the one-sided 95 % Clopper-Pearson UCB <= FWER_target, i.e. count <= 1192 (rate 0.02384). Mandatory evaluations E = 27 (3 regimes x 3 G x 3 families). Union bound, no independence assumed.

| true FWER (all evaluations) | P(one evaluation fails) | union bound over E |
|---|---|---|
| 0.0180 | 3.255e-21 | 8.787e-20 |
| 0.0190 | 1.018e-14 | 2.749e-13 |
| 0.0200 | 1.151e-09 | 3.107e-08 |
| 0.0210 | 6.615e-06 | 1.786e-04 |
| 0.0215 | 1.813e-04 | 4.894e-03 |
| 0.0220 | 2.655e-03 | 7.168e-02 |
| 0.0225 | 2.165e-02 | 5.845e-01 |
| 0.0230 | 1.029e-01 | 1.000e+00 |
| 0.0250 | 9.510e-01 | 1.000e+00 |

Largest uniform true FWER with union bound <= 0.05: 0.02193.
