# Execution algos and smart order routing: one page

This page shows the execution side of the platform in a form you can run in a
few seconds: three parent-order algorithms, a router comparison and one cost
model fit, all on a seeded synthetic tape. The last section is the one real
routing result the repository has, with its evidence class.

Run it:

```bash
PYTHONPATH=python/src python -m iap.execution.showcase all
```

The code is `python/src/iap/execution/showcase.py`; the tests are
`python/tests/execution/test_showcase.py`. The demo reuses the real simulator
(`iap.execution.replay.ExecutionReplay` over `iap.execution.simulator`), the
pinned router (`iap.execution.sor`) and the cost-aware router
(`iap.execution.sor_v2`). Only the tape is new.

**Every number in sections 2 to 4 is from synthetic data and carries no
evidence class.** It shows what the machinery computes, not how any strategy
would do in a real market.

## 1. The tape

Two lit venues, XV1 and XV2, with the fees of `configs/venues/venues.json`
(XV1 taker $0.0030 and rebate $0.0020 per share; XV2 taker $0.0028 and rebate
$0.0015). Once a second, for 1,800 seconds:

- each venue re-quotes one level per side around the mid. XV1 always shows a
  2-tick spread with 3,000 to 4,000 shares a side. XV2 shows either a 1-tick
  spread whose bid is one tick better than XV1, or a 4-tick spread, with 1,200
  to 2,000 shares a side;
- one market print of 100 to 2,000 shares, buy or sell with equal odds, hits
  the touch of XV1 (60 % of the time) or XV2;
- the mid moves by a planted permanent impact on that signed flow plus noise:
  `mid(t+1) = mid(t) + 0.0004 ticks/share * signed volume(t) + 0.6 ticks * z`.

The simulator charges a linear impact adder on taker fills
(`impact_coeff_bps_per_pct_adv` = 2.0, ADV 2,000,000 shares); it does not move
the replayed book. All draws come from one fixed seed (20261027), so every table
below is identical run to run, which the tests check.

## 2. Three parent algorithms on one order

What each algo does (the schedules are pinned in `iap/execution/algos.py`):

- **TWAP** splits the parent into equal slices at equal time steps. Children
  join the best bid (passive), so they earn the rebate and wait for a print.
- **VWAP** weights the slices by a U-shaped intraday volume curve,
  `w_i = 1 + x_i^2`, heavier at the start and end of the window. Same child
  style as TWAP.
- **Implementation shortfall (IS)** front-loads with exponential decay,
  `w_i = exp(-risk_aversion * i / (N - 1))`, and sends MARKET children. Higher
  risk aversion trades faster: less exposure to the price drifting away, more
  impact and spread paid. An Almgren-Chriss trajectory is an opt-in
  (`ISModel.ALMGREN_CHRISS`).

One buy parent of 6,000 shares on XV1, 10 slices over 600 seconds, starting
60 seconds into the tape:

| algo | filled / qty | children | slippage vs arrival (bps) | impact adder (bps) | fees net (bps) | fill rate | participation |
|---|---|---|---|---|---|---|---|
| TWAP | 6000 / 6000 | 10 | 0.59 | 0.00 | -0.20 | 100.0% | 1.0% |
| VWAP | 6000 / 6000 | 10 | 0.01 | 0.00 | -0.20 | 100.0% | 1.0% |
| IS (risk aversion 1.0) | 6000 / 6000 | 10 | 3.20 | 0.07 | 0.30 | 100.0% | 1.0% |
| IS (risk aversion 3.0) | 6000 / 6000 | 12 | 3.60 | 0.07 | 0.30 | 100.0% | 1.0% |

<!-- source: python -m iap.execution.showcase algos (seed 20261027), run 2026-10-27 -->

Slippage is the average fill price against the mid at arrival, positive when
worse for us. Fees net is fees minus rebates over filled notional, so negative
means a net rebate. Participation is our filled quantity over the market
volume printed in the window.

How to read it. The IS rows cost more because their children cross the
spread and pay the taker fee, while TWAP and VWAP children rest and earn the
rebate. On this tape the passive children always filled; that is a property of
a tape where prints hit the touch every second, not a general result. On a
real book a resting child can sit unfilled while the price moves away, and the
cost of finishing the remainder late (the completion charge) is what makes IS
the right choice when the price is trending against you. One parent on one
path is a single draw: the gap between TWAP and VWAP is noise, not a ranking.

## 3. Smart order routing against a single venue

The same 24 parents (alternating buy and sell, one minute each, 70 seconds
apart) under each router. The aggressive block is IS with 4 slices of one
child each (8,000 shares a parent, so the first child is about 3,100 shares,
more than XV2 ever shows at the touch). The passive block is TWAP with
2,000 shares a parent.

| router | filled / target | slippage (bps) | fees (bps) | all-in cost (bps) | share on XV2 |
|---|---|---|---|---|---|
| naive: all on XV1 | 191921 / 192000 | 0.59 | 0.30 | 1.11 | 0.0% |
| pinned SOR (best price, fee tie-break) | 160144 / 192000 | 0.29 | 0.29 | 0.80 | 46.4% |
| cost-aware SOR (all-in cost) | 160144 / 192000 | 0.29 | 0.29 | 0.80 | 46.4% |
| cost-aware sweep (both venues' depth) | 192000 / 192000 | 0.39 | 0.29 | 0.86 | 38.7% |
| passive naive: all on XV1 | 28000 / 48000 | -1.21 | -0.20 | -1.41 | 0.0% |
| passive pinned SOR (best rebate) | 28000 / 48000 | -1.21 | -0.20 | -1.41 | 0.0% |
| passive cost-aware SOR (fill prob, toxicity) | 28000 / 48000 | -1.21 | -0.20 | -1.41 | 0.0% |
| passive cost-aware SOR, XV2-favouring fill model | 29500 / 48000 | -1.73 | -0.15 | -1.88 | 100.0% |

<!-- source: python -m iap.execution.showcase sor (seed 20261027), run 2026-10-27 -->

All-in cost is slippage plus fees plus the simulator's impact adder, weighted
by filled quantity. It is computed on filled shares only, so **a row that
fills less is not better because its cost is lower**: read the fill column
first.

What the table shows:

- **Routing each child to the best touch** cuts slippage from 0.59 to 0.29 bps
  but leaves 31,856 shares (17 %) unfilled, because a large child sent to the
  thin venue exhausts its displayed depth. The cost-aware router makes the same
  choices here: for a marketable child its cost is price plus taker fee, and
  the two venues' taker fees are close.
- **Sweeping** splits each child across both venues' displayed depth. It fills
  everything at 0.86 bps all-in, below the naive 1.11 bps at the same fill.
  That is the fair comparison in this table.
- **Passive routing** depends on the fill model. The pinned router picks the
  higher rebate (XV1); the cost-aware router with the default model (XV1 fill
  probability 0.6, XV2 0.35, XV2 more toxic) also picks XV1. A model that
  believes XV2 fills far more often (0.9 against 0.35) routes everything to
  XV2, and on this tape it fills 1,500 more shares at a better price, because
  the XV2 bid is often one tick better. The router can only be as good as the
  fill probabilities it is given, and those have to be measured on real
  queues, not assumed.

## 4. One cost-model calibration

Method: ordinary least squares through the origin of the one-second mid change
on the signed market volume in that second (a Kyle lambda), over 1,799
intervals of the tape (`fit_impact` in `showcase.py`).

| quantity | value |
|---|---|
| fitted lambda | 0.000410 ticks per share |
| standard error | 0.000012 |
| planted lambda | 0.000400 |
| R squared | 0.394 |
| in bps of mid per 1 % of ADV | 8.21 |

<!-- source: python -m iap.execution.showcase impact (seed 20261027), run 2026-10-27 -->

The fit recovers the planted value within one standard error; the test checks
it is within three. The last row converts the slope to the unit the simulator
uses: 1 % of a 2,000,000-share ADV is 20,000 shares, times 0.000410 ticks is
8.2 ticks, or 8.21 bps of a $100.00 mid.

Limits, all of which matter on real data:

- The tape was built with a linear, permanent, instantaneous impact, so a
  linear fit is the right model by construction. Real impact is concave in size
  (closer to a square root) and partly temporary, decaying over seconds to
  minutes.
- Signed flow here is independent from second to second. Real order flow is
  autocorrelated (large orders are split), which biases a one-lag regression;
  the usual fixes are to regress on the unexpected part of the flow or to fit a
  propagator with a decay kernel.
- The fit is on market flow, not on our own orders. Our own impact is what a
  cost model needs, and it is only seen in our own fills
  (`iap.execution.calibration` fits that residual from fills when they exist).
- One tape, one seed: the standard error describes this sample, not model
  error.

## 5. The real routing result (B8 v2)

The repository has one signed, pre-registered routing comparison on real data:
B8 v2 (v1.18 REL-04b), `research/execution/b8v2/REPORT.md`. It compared
`pin_iex`, `sor` and `sor_sweep` against pinned Nasdaq on AAPL and MSFT over
G = 3 evaluation sessions (2025-12-10, 2026-05-15, 2026-05-18).

**Evidence class: development-supported** (signed, bound, reproduced in the
pinned image by CI; no independent rerun, gate F7 open;
`archive/docs/REVIEW_PACK.md` section 0). **Verdict: all six comparisons
`no_evidence` (underpowered), none better than pinned Nasdaq.**

Difference in trading cost per filled share against pinned Nasdaq, bps, with
the family interval (K = 6) and the minimum detectable effect:

| policy | symbol | difference | family CI | MDE |
|---|---|---:|---|---:|
| sor | AAPL | -0.57 | [-6.5, 5.37] | 6.51 |
| sor | MSFT | -0.67 | [-12.49, 11.15] | 12.97 |
| sor_sweep | AAPL | -0.18 | [-5.7, 5.34] | 6.06 |
| sor_sweep | MSFT | 1.71 | [-18.5, 21.92] | 22.18 |

<!-- source: research/execution/b8v2/REPORT.md, results table -->

With three sessions the interval has 2 degrees of freedom, so an effect near
1 bps cannot be detected; the report said so before the run. The honest summary
is that the real data neither supports nor rules out a routing gain. The
synthetic table in section 3 shows the mechanisms; it is not evidence that they
pay on a real tape. The full account is in
[EXECUTION_QUALITY.md](EXECUTION_QUALITY.md) section 11.

## 6. Questions to check your understanding

**IS against VWAP: when do you pick which?** VWAP is a benchmark-tracking
algo: it matches the market's volume profile so the fill price lands near the
day's VWAP, with low tracking error and little regard for urgency. IS minimises
the cost against the arrival price, trading off impact (trade slowly) against
timing risk (trade fast). Pick IS when the order carries alpha that decays, or
when the price is likely to move against you; pick VWAP when the client is
measured against VWAP or the order has no view. In section 2 the IS rows pay
more on a flat tape; on a tape trending against the buyer they would win.

**What is Almgren-Chriss?** A mean-variance model of the execution schedule.
With linear temporary impact `eta`, permanent impact `gamma`, volatility
`sigma` and risk aversion `lambda`, the optimal holdings decay as
`x(t) = X sinh(kappa (T - t)) / sinh(kappa T)` with
`kappa = sqrt(lambda sigma^2 / eta)` (permanent impact drops out of the
schedule). `lambda = 0` gives TWAP; a large `lambda` trades almost everything
at once. The platform's default IS is a simpler exponential decay; the closed
form is in `iap/execution/optimal.py`.

**Why does queue position matter?** A passive order at a price level fills
only after the orders ahead of it trade or cancel. Two orders at the same price
can have very different fill probabilities and different adverse selection:
the back of a long queue mostly fills when a large aggressor clears the level,
which is when the price is about to move through you. That is why the
cost-aware router takes a fill probability per venue and queue size
(`hazard_fill_prob` in `sor_v2.py`), and why section 3's passive rows depend
on it.

**What is adverse selection in execution?** The tendency of passive fills to
happen just before the price moves against you: you are filled because an
informed trader wanted the other side. It shows up as a negative markout (the
mid one second or one minute after your buy fill is lower than your price). A
rebate that is smaller than the average markout loses money. The router's
`toxicity_bps` per venue is a markout estimate for that reason.

**How do venue fees change routing?** Maker-taker venues pay a rebate to
resting orders and charge takers; inverted venues do the reverse. For a
marketable order the all-in price is the touch plus the taker fee, so a venue
one tick worse can still be cheaper only if its fee is lower by more than a
tick, which at $0.0030 against a $0.01 tick it never is. For a passive order
the rebate is a reason to rest on a venue, but the higher-rebate venue usually
has the longer queue and the more toxic flow, so the rebate is only worth the
fill probability times the rebate minus the expected markout. Section 3 shows
the fee alone (pinned router) and the fill model (cost-aware router) can pick
different venues.
