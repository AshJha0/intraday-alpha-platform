# Round 0 gates (2026-11-18): what must be true before any new study is run

Status: written on branch `round0-gates`. No market data was read and nothing was downloaded.
Two items are new work of this round (sections 2 and 4); the rest records what the repository
already holds and what it does not. Owner's rules in force: zero spend, no payment card, free
sources only.

## 1. Owner decisions recorded (2026-11-18)

| # | decision | consequence |
|---|---|---|
| R0-1 | Live LLM API calls are off. The capped path (`configs/agents/llm.json`) stays in the code, unused | repeated agent evals run on a local open-weights model; any pass rate is labelled with that model, never with a pinned Claude model |
| R0-2 | Tiingo free tier is a background lane; the owner creates the account and stops if a card is asked | the daily families do not block the intraday route |
| R0-3 | The research graph is written in-house and tested in CI; a LangGraph adapter is an optional extra outside the required lock | no new dependency on the trading or research path (PLATFORM_CONVENTIONS section 13.7 unchanged) |

## 2. Power gate (new): sessions needed against net Sharpe

[docs/research/PNL_POWER_TABLE.md](../../docs/research/PNL_POWER_TABLE.md), from
`iap.research.pnl_power` (tests: `python/tests/test_pnl_power.py`, which check the closed form
against a simulation of the test). Three readings that change the plan:

- On the 7 and 19 session samples used so far, only a net Sharpe above about 20 and 11 was
  detectable. Every "rejected" verdict on those samples is a statement about power as much as
  about the signal.
- 250 sessions detect a net Sharpe of about 2.8 or more (one registered test), 500 sessions about
  2.0. The intraday cross-sectional route is therefore a test for a high-Sharpe effect only, and
  K must stay small: at K = 10 the 250-session floor is 3.7.
- A Sharpe near 1 needs about 2,000 sessions, eight years. Only the daily route has that much
  history. So the daily families are the only place a moderate Sharpe can be confirmed.

Stop rule: a confirmatory study is registered only if its sealed session count reaches the
table's entry for the Sharpe it looks for and the K it registers.

## 3. Economic statistic: already in the repository, adopted as is

No new label was written. The repository already has what the review asked for:

- a spread-netted label and a net-of-cost book at a registered notional (`iap.research.rel08_screen`,
  rule `research/v118/rel08/rel08_rule_v2.json`);
- a session as the resampling unit (`iap.research.rel08_screen_v2`: sessions fixed, block
  bootstrap inside a session);
- small-cluster inference with Student t and a wild bootstrap for fewer than ten sessions
  (`research/v118/NULL_PROTOCOL_v3.md`, statistic S2).

What was missing was not the statistic but the sessions: the one signed REL-08 screen ran on one
session (`research/v118/rel08/SCORECARD_ds_20190130.md`). Decision for round 1: the registered
statistic of any new family is the net mean per rebalance of the REL-08 v2 rule, with the verdict
taken from the session-level P&L test of section 2. The bucket-level HAC t stays a diagnostic.

## 4. The known-null "over-rejection" (new): the FAIL does not show over-rejection

Record: `research/v118/known_null_v3_RESULT.md`. A0 enumerated the 1,854 derangements of 7 real
sessions and counted 79 and 80 rejections (4.3 %) against a 2.5 % target; generator B, with
independent trials, was at 2.50 % and 2.45 %.

The 1,854 outcomes reuse the same seven sessions. Their rejection share is one draw of a random
quantity, and the protocol says so (section 3.1), but the rate rule still compared it with 2.5 %.
`tools/a0_conditional_rate_sim.py` measures how far that share moves between worlds in which the
test is exactly valid (4,000 simulated worlds, seed 20261118, result in
`a0_conditional_rate_sim.json`):

| world | mean share | sd of share | 95th percentile | worlds with a count of 79 or more |
|---|---:|---:|---:|---:|
| i.i.d. session payoffs (test exact for every derangement) | 2.54 % | 2.82 % | 7.98 % | 18.4 % |
| with a decision-session effect (sd 1) | 2.67 % | 6.86 % | 14.35 % | 16.4 % |

Reading: with a correctly sized test, about one valid-null world in six shows 79 or more
rejections out of 1,854. The observed 4.3 % is inside the ordinary range. The A0 arm on seven
sessions cannot tell a 2.5 % test from a 4.3 % one, so its FAIL is not evidence that the
inference over-rejects, and it would not have been evidence of correct size had it passed. The
informative arm was B, and B was on target.

Limits of this reading: the simulation uses a one-sample Student t on seven session values, not
the S2 pipeline or the K-family rule, and K3 and K6 are treated as one observation because they
share their data. It bounds what the derangement design can tell; it does not re-run the study.
The recorded verdict stands as recorded (owner decision 2026-10-19: no tuning, no re-run).

Consequence: gate F3 is not closed by this. A replacement protocol needs independent trials on
real data, which needs many more sessions, which is the same prerequisite as everything else here.

## 5. Data actually on disk

`research/data_manifest.json` lists 39 ingested datasets. On this machine today only one raw file
remains: `data/vendor/01302019.NASDAQ_ITCH50.gz` (4.76 GB, the 2019-01-30 Nasdaq sample day). The
normalised ITCH and IEX datasets are gone. So:

- the market-making rerun needs the free Nasdaq sample days fetched and ingested again;
- the 100-name breadth day (2019-01-30) can be rebuilt from the file on disk.

## 6. Intraday breadth on IEX HIST: go, with one prerequisite

Measured on 2026-10-24 (`archive/docs/review/DATA_ROUTE_PROPOSAL_v1.19.md`, not re-measured):
TOPS and DEEP listed back to at least 2024-10-01 (about 500 sessions); 13 to 16 GB per feed per
day now; the Python reader takes about 9 to 10 minutes per GB, so 250 sessions are 500 to 650
hours of this laptop.

Prerequisite: a native TOPS reader that writes bars directly. Design, to be built first in round 1:

- a Rust binary in the existing workspace: gzip stream, pcap and pcapng framing, UDP payload,
  IEX-TP segments, TOPS quote update and trade report messages, trading status and halts;
- output per symbol per minute: closing bid, ask and sizes, time-weighted quoted spread, quote
  update count, trade count, volume, VWAP, last trade; one compressed file per day; the raw file
  is deleted after the day is written (process and delete, as today);
- parity: the same capture through the Python reader (`iap.marketdata.iex`) and the Rust reader
  must give identical bars, tested on a synthetic capture in CI and on one real day by hand;
- target: ten times the Python reader, which makes 250 sessions about 50 to 70 hours.

Still to measure, on the first real day through the new reader: the number of names that pass a
liquidity screen and their median IEX spread. IEX quotes are one venue's, so its spread is wider
than the consolidated one; costs are then overstated, which is the safe direction. The IEX terms
note (D-22, `docs/DATA_SOURCES.md` section 13) is still an open owner item.

## 7. Route status after round 0

| route | status | next step |
|---|---|---|
| A: cross-sectional intraday book on IEX HIST | go, powered for a net Sharpe near 3 or more at 250 sessions | native TOPS reader, then one measured day |
| B: event-driven market making on Nasdaq ITCH sample days | method work only: too few sessions for any Sharpe claim (section 2) | re-ingest the sample days; event-driven quoter; markout curves |
| C: daily families on Tiingo free tier | the only route powered for a Sharpe near 1; waits on the owner's sign-up | owner creates the account; DD1 tests on the first 500 symbols |
| D: filing-text signal for D4 | waits on route C's universe and on EDGAR access from the owner's machine | local model, look-ahead control by filing date against model cut-off |
