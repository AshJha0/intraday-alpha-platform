# REL-10 volume-clock OFI: result under the signed pre-declaration

Declaration: `research/v118/rel10/predeclaration.json`. Family K = 24, level 0.9979. Taker and maker are evaluated separately; an execution input first.

inference rests on G = 4 evaluate session clusters (3 df); at G <= 5 the result is a size statement, not a power statement, and no generalisation beyond these sessions is claimed

## taker: **reject**

| signal | h (min) | symbol | verdict | mean net USD | family CI | family MDE | trips | missed USD |
|---|---|---|---|---|---|---|---|---|
| hid_imb | 1 | AAPL | reject | -322.36350000000016 | [-600.537060211484, -44.189939788516256] | 305.19402333301736 | 363 | -113.375 |
| flow | 1 | AAPL | underpowered | -172.90599999999992 | [-410.62358382337857, 64.81158382337873] | 260.80834486535656 | 168 | 157.67999999999998 |
| flow | 1 | MSFT | underpowered | -325.9805000000002 | [-2102.3370969761363, 1450.3760969761358] | 1948.9034698090372 | 321 | 682.895 |
| flow | 5 | AAPL | underpowered | -208.90200000000007 | [-1051.4826152456528, 633.6786152456526] | 924.424908512976 | 69 | 171.92000000000002 |
| flow | 5 | MSFT | underpowered | 39.52249999999996 | [-877.4954396552528, 956.5404396552526] | 1006.0927223247538 | 97 | 861.285 |
| hid_imb | 1 | MSFT | underpowered | -682.6365 | [-2893.1971242849077, 1527.9241242849075] | 2425.284021364751 | 377 | -133.695 |
| hid_imb | 5 | AAPL | underpowered | -51.37849999999996 | [-880.9302298546256, 778.1732298546258] | 910.1304588571235 | 143 | 158.21 |
| hid_imb | 5 | MSFT | underpowered | -197.05050000000006 | [-2342.412047208062, 1948.3110472080623] | 2353.7518145095996 | 138 | -259.565 |
| ofi | 1 | AAPL | underpowered | -119.54949999999995 | [-374.9072615341456, 135.8082615341457] | 280.1620059529359 | 125 | 67.03999999999999 |
| ofi | 1 | MSFT | underpowered | -431.7455000000002 | [-2059.01491402841, 1195.5239140284095] | 1785.3346634975746 | 193 | -334.18 |
| ofi | 5 | AAPL | underpowered | -87.92800000000005 | [-919.9313438753074, 744.0753438753075] | 912.8202110609731 | 55 | -179.3 |
| ofi | 5 | MSFT | underpowered | -13.129499999999993 | [-521.4485357061578, 495.18953570615787] | 557.6947411032829 | 53 | -487.71000000000004 |

## maker: **underpowered**

| signal | h (min) | symbol | verdict | mean net USD | family CI | family MDE | trips | missed USD |
|---|---|---|---|---|---|---|---|---|
| flow | 1 | AAPL | underpowered | -97.1315000000001 | [-697.9964185588508, 503.73341855885064] | 659.2300930225896 | 118 | 512.475 |
| flow | 1 | MSFT | underpowered | 287.2697499999992 | [-1458.7751879543562, 2033.3146879543547] | 1915.6474796864582 | 227 | 1517.215 |
| flow | 5 | AAPL | underpowered | -49.74999999999997 | [-593.2542705002191, 493.75427050021915] | 596.2977030833955 | 60 | 84.5 |
| flow | 5 | MSFT | underpowered | 202.21450000000024 | [-1325.3157487821134, 1729.744748782114] | 1675.9073077767375 | 84 | 454.61 |
| hid_imb | 1 | AAPL | underpowered | -207.45075000000054 | [-933.467505456739, 518.5660054567379] | 796.5385870482094 | 267 | 472.13 |
| hid_imb | 1 | MSFT | underpowered | -470.42999999999836 | [-3641.3337610716917, 2700.4737610716948] | 3478.910345424393 | 261 | 1156.675 |
| hid_imb | 5 | AAPL | underpowered | 57.04775000000012 | [-1019.5563859264059, 1133.6518859264063] | 1181.1803664249971 | 113 | 168.435 |
| hid_imb | 5 | MSFT | underpowered | -292.7672499999994 | [-3003.4577449229505, 2417.9232449229517] | 2973.994140662224 | 104 | 309.385 |
| ofi | 1 | AAPL | underpowered | -13.970499999999841 | [-355.3018209988284, 327.36082099882873] | 374.4866300952838 | 97 | 281.835 |
| ofi | 1 | MSFT | underpowered | -179.5312499999991 | [-1018.1712179828834, 659.1087179828852] | 920.1014854250801 | 148 | 863.38 |
| ofi | 5 | AAPL | underpowered | -78.78074999999961 | [-767.8393173829006, 610.2778173829013] | 755.9904554976137 | 50 | 66.1 |
| ofi | 5 | MSFT | underpowered | -43.838750000000616 | [-1595.4970872540453, 1507.819587254044] | 1702.379084571359 | 50 | -245.0 |

## MDE correction (v1.19 R2, 2026-10-25)

The `family MDE` column above was computed by the two-quantile Student rule `(t_{1-alpha/(2K)} + t_{power}) sd / sqrt(G)` (defect D-03). The column below is the exact noncentral-t MDE at 80 % power, G - 1 df, one-sided alpha / (2K) (`iap.validation.confirmatory.mde`, the one MDE implementation since R2), recomputed from the committed `DECISION.json` (`G`, `sd_usd`) by `research/v119/reemit_mde.py`. The signed JSON and the bound run script are untouched; the verdicts do not change (a larger MDE can only confirm `underpowered`).

Family K = 24, two-sided alpha = 0.05 (one-sided 0.00104167 per hypothesis), power = 0.8.

| cell | G | sd USD | family MDE as reported (two-quantile) | family MDE, noncentral t (R2) | ratio |
|---|---|---|---|---|---|
| maker flow|1|AAPL | 4 | 119.30 | 659.23 | 753.60 | 1.143 |
| maker flow|1|MSFT | 4 | 346.67 | 1915.65 | 2189.89 | 1.143 |
| maker flow|5|AAPL | 4 | 107.91 | 596.30 | 681.66 | 1.143 |
| maker flow|5|MSFT | 4 | 303.28 | 1675.91 | 1915.83 | 1.143 |
| maker hid_imb|1|AAPL | 4 | 144.15 | 796.54 | 910.57 | 1.143 |
| maker hid_imb|1|MSFT | 4 | 629.57 | 3478.91 | 3976.95 | 1.143 |
| maker hid_imb|5|AAPL | 4 | 213.75 | 1181.18 | 1350.28 | 1.143 |
| maker hid_imb|5|MSFT | 4 | 538.19 | 2973.99 | 3399.75 | 1.143 |
| maker ofi|1|AAPL | 4 | 67.77 | 374.49 | 428.10 | 1.143 |
| maker ofi|1|MSFT | 4 | 166.51 | 920.10 | 1051.82 | 1.143 |
| maker ofi|5|AAPL | 4 | 136.81 | 755.99 | 864.22 | 1.143 |
| maker ofi|5|MSFT | 4 | 308.07 | 1702.38 | 1946.09 | 1.143 |
| taker flow|1|AAPL | 4 | 47.20 | 260.81 | 298.15 | 1.143 |
| taker flow|1|MSFT | 4 | 352.69 | 1948.90 | 2227.91 | 1.143 |
| taker flow|5|AAPL | 4 | 167.29 | 924.42 | 1056.76 | 1.143 |
| taker flow|5|MSFT | 4 | 182.07 | 1006.09 | 1150.12 | 1.143 |
| taker hid_imb|1|AAPL | 4 | 55.23 | 305.19 | 348.89 | 1.143 |
| taker hid_imb|1|MSFT | 4 | 438.90 | 2425.28 | 2772.49 | 1.143 |
| taker hid_imb|5|AAPL | 4 | 164.70 | 910.13 | 1040.42 | 1.143 |
| taker hid_imb|5|MSFT | 4 | 425.95 | 2353.75 | 2690.71 | 1.143 |
| taker ofi|1|AAPL | 4 | 50.70 | 280.16 | 320.27 | 1.143 |
| taker ofi|1|MSFT | 4 | 323.09 | 1785.33 | 2040.92 | 1.143 |
| taker ofi|5|AAPL | 4 | 165.19 | 912.82 | 1043.50 | 1.143 |
| taker ofi|5|MSFT | 4 | 100.92 | 557.69 | 637.53 | 1.143 |
