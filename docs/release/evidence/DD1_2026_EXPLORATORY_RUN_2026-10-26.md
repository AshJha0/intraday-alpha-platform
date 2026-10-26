# Exploratory D1-D4 run on the DD1 2026 development partition (2026-10-26)

**Evidence class: exploratory, data-ineligible (U0 DR-19).** The DD1 2026 record failed all five data tests
([DD1_FREE_ROUTE_FINDING_2026-10-26.md](DD1_FREE_ROUTE_FINDING_2026-10-26.md)); the price history lacks most delisted
names, which flatters any long-run result. Nothing below is a finding, a frozen candidate or a registration.

## What was run

`tools/research/dd1_chain.py --dataset <dev-root>/dd1_2026 --exploratory` on main `54df7808` plus the `--exploratory`
flag (this PR), with its own selection ledger under the run's working directory (the committed ledger is untouched).
Development partition `26d9d888948dd16eb492c3b16a205e4156739f33b8708640aaf47a5a24f0df3c`; S&P 500 proxy universe,
892 names, 2,295,019 panel rows, 4,315 sessions of which 1,626 are trading sessions after the 300-session warm-up.
The four family runs took 95, 66, 98 and 79 seconds.

## Results (conservative cost column, net bps per session on registered capital)

| family | sessions traded | fills | screen mean (bps) | standard error (bps) | report sha256 |
|---|---|---|---|---|---|
| D1 short-term reversal | 681 | 19,642 | -0.015 | 0.467 | `ac8a4db5a3bcf061b87c221d84d73c140179ec9deb34e0a9afabdc8f20b9a03e` |
| D2 residual momentum | 0 | 0 | none | none | `06bb8ae97405440f2853a8e68c2079db3afbfd4a904f40029c3b7064f86f621a` |
| D3 overnight / close-to-open | 801 | 42,360 | -0.699 | 0.248 | `ff91d126ef84d3daae117896ad97da01e2bce93cfe456e51e9f8a056b4c4b66f` |
| D4 post-announcement drift | 0 | 0 | none | none | `142bd71cef85479d8ad19b458cc83f8845def22817b216843cadf0d4e57a5861` |

M0 vs M1 did not run: the frozen ML protocol (revision 1) fits on the synthetic route only (D-92).

## How to read it

In plain words: on this data, the two families that traded lost money after conservative costs, and two families
never traded at all. The data is biased in their favour (survivors only), so a real edge would be expected to look
better here than it is, not worse.

Formally: D1's mean is 0.03 standard errors below zero; D3's is 2.8 standard errors below zero. Neither screen passed.
D2 and D4 opened no cohort in any session: every session was refused with "fewer than the minimum names per leg"
(D2: 77 sessions refused at construction; D4: all 1,626). That is a pipeline defect, not a market result (D-106,
D-107), and says nothing about momentum or post-announcement drift.

Worked example: D1 traded on 681 of 1,626 sessions; on the other 945 the hurdle (expected gain above 1.5 times the
round-trip cost) refused every name, which is the cost-aware rule working as designed.
