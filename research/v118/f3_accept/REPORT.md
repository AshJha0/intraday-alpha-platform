# F3 acceptance study result

Mandatory verdict (27 evaluations: a0, b0, b1 x G 5, 7, 10 x K 3, 6, 16): **PASS**. alpha_op 0.02, FWER target 0.025, an evaluation meets the rule iff count <= 1192.

Scope: calibration under the declared synthetic regimes only; no real-data claim; no market data consumed.
**a statistical pass does NOT automatically close F3 or authorise real-data claims; the outside reviewer decides.**

## Mandatory results (27)

| regime | G | K | count | n | FWER observed | UCB95 | meets UCB rule |
|---|---|---|---|---|---|---|---|
| a0 | 5 | 3 | 967 | 50000 | 0.01934 | 0.02038 | True |
| a0 | 5 | 6 | 1010 | 50000 | 0.02020 | 0.02127 | True |
| a0 | 5 | 16 | 1013 | 50000 | 0.02026 | 0.02133 | True |
| a0 | 7 | 3 | 953 | 50000 | 0.01906 | 0.02010 | True |
| a0 | 7 | 6 | 989 | 50000 | 0.01978 | 0.02084 | True |
| a0 | 7 | 16 | 976 | 50000 | 0.01952 | 0.02057 | True |
| a0 | 10 | 3 | 1002 | 50000 | 0.02004 | 0.02110 | True |
| a0 | 10 | 6 | 984 | 50000 | 0.01968 | 0.02073 | True |
| a0 | 10 | 16 | 957 | 50000 | 0.01914 | 0.02018 | True |
| b0 | 5 | 3 | 410 | 50000 | 0.00820 | 0.00889 | True |
| b0 | 5 | 6 | 354 | 50000 | 0.00708 | 0.00773 | True |
| b0 | 5 | 16 | 312 | 50000 | 0.00624 | 0.00685 | True |
| b0 | 7 | 3 | 536 | 50000 | 0.01072 | 0.01151 | True |
| b0 | 7 | 6 | 457 | 50000 | 0.00914 | 0.00987 | True |
| b0 | 7 | 16 | 328 | 50000 | 0.00656 | 0.00719 | True |
| b0 | 10 | 3 | 602 | 50000 | 0.01204 | 0.01287 | True |
| b0 | 10 | 6 | 524 | 50000 | 0.01048 | 0.01126 | True |
| b0 | 10 | 16 | 372 | 50000 | 0.00744 | 0.00810 | True |
| b1 | 5 | 3 | 207 | 50000 | 0.00414 | 0.00464 | True |
| b1 | 5 | 6 | 161 | 50000 | 0.00322 | 0.00367 | True |
| b1 | 5 | 16 | 148 | 50000 | 0.00296 | 0.00339 | True |
| b1 | 7 | 3 | 125 | 50000 | 0.00250 | 0.00290 | True |
| b1 | 7 | 6 | 66 | 50000 | 0.00132 | 0.00162 | True |
| b1 | 7 | 16 | 54 | 50000 | 0.00108 | 0.00135 | True |
| b1 | 10 | 3 | 124 | 50000 | 0.00248 | 0.00288 | True |
| b1 | 10 | 6 | 90 | 50000 | 0.00180 | 0.00214 | True |
| b1 | 10 | 16 | 53 | 50000 | 0.00106 | 0.00133 | True |

## Sensitivity results, a1 rho 0.6 (9; reported, NOT part of the verdict, never used to redefine the operating domain)

| regime | G | K | count | n | FWER observed | UCB95 | meets UCB rule |
|---|---|---|---|---|---|---|---|
| a1 | 5 | 3 | 938 | 50000 | 0.01876 | 0.01979 | True |
| a1 | 5 | 6 | 942 | 50000 | 0.01884 | 0.01987 | True |
| a1 | 5 | 16 | 876 | 50000 | 0.01752 | 0.01852 | True |
| a1 | 7 | 3 | 926 | 50000 | 0.01852 | 0.01954 | True |
| a1 | 7 | 6 | 855 | 50000 | 0.01710 | 0.01808 | True |
| a1 | 7 | 16 | 812 | 50000 | 0.01624 | 0.01720 | True |
| a1 | 10 | 3 | 894 | 50000 | 0.01788 | 0.01889 | True |
| a1 | 10 | 6 | 839 | 50000 | 0.01678 | 0.01776 | True |
| a1 | 10 | 16 | 772 | 50000 | 0.01544 | 0.01638 | True |
