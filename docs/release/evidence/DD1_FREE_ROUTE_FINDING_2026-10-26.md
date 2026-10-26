# DD1 free-route data finding (2026-10-26)

This file records, for the v2.0.0 evidence manifest, the result of the first complete DD1 run on the free daily
route. It transcribes owner decision DR-19 (U0 specification section 12), which lives outside the repository; the
run's own redacted log is a GitHub Actions artefact of run 40328224317 (main 7c755bc1), not a repository file. No
sealed partition was read to write it: the counts are the ones DR-19 states.

## The plain idea

The free daily prices (Stooq) do not keep most of the companies that left the S&P 500 or the Nasdaq-100. A backtest
on that data sees mainly the companies that survived, which flatters any strategy that buys past losers or holds
names through bad years. The ingestion job's five DD1 tests exist to catch this, and all five failed.

## The formal statement

Run 40328224317 completed the DD1 pipeline end to end and failed every DD1 test on the development partition:

| DD1 test | measured | threshold | result |
|---|---|---|---|
| survivorship through the retention horizon | 315 of 372 index leavers missing (85 %); 268 of them absent from the Stooq archive | 2 % | fail |
| open/close convention | opens equal to the prior close on 13.1 % of rows | 5 % | fail |
| split-day reconciliation | 57 residual split jumps | none | fail |
| membership intervals priced | 323 members without prices | none | fail |
| calendar equality with the French dates | 2 of 15,356 sessions missing | none | fail |

A no-key check of Tiingo's public ticker list found 101 of the 372 leavers still missing (27 %), so Tiingo alone would
also fail the survivorship test, and mixing two vendors breaks the one-route rule.

Consequence under DR-6 and DR-19: the daily families D1-D4 are data-ineligible on this route, and no test is relaxed.
Any run on the `incomplete` DD1 2026 development partition is labelled `exploratory, data-ineligible`; it is not a
finding, a frozen candidate or a registration.

## A worked example

Take a stock that left the S&P 500 after a long decline. A short-term reversal family (D1) buys recent losers. If the
data keeps only names that still trade, that stock's last losing weeks and its delisting are not in the panel, and the
losers the strategy does see are disproportionately ones that recovered. With 85 % of leavers missing, the bias is not
a correction term to estimate; it is most of the population the test is about.
