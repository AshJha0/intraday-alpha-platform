# Event-driven passive quoter: development comparison with the grid quoter

Status: DEVELOPMENT. Exploratory, in-sample, one session, three names. No
edge claim, no promise of profit. Not a pre-registered test; the signed
quoting pre-registrations and results in `research/quoting_real/` are
untouched.

## 1. What the grid quoter actually does between decisions (verified)

The 2026-11-18 review said the quoter in `QUOTING_V2_RESULT.md` decides on a
fixed time grid and does not react between grid points. Line numbers below
are in `python/src/iap/backtest/quoting.py` as of commit `eb8405f3` (before
this work) and `python/src/iap/research/quoting_real.py`.

**Plain idea.** The claim is right. The quoter only thinks at score rows. Between
two rows the simulator keeps replaying the book and keeps filling the
resting quotes, but nothing cancels or moves them.

**Formal statement.**

- Decisions: `decide(r)` (quoting.py:394) is the only place a quote is
  submitted, cancelled or amended during the session. It is called only from
  the replay loop, `while r < n and ts[r] < t: decide(r)` (quoting.py:583),
  that is once per score row, before the first event later than that row.
- The grid: the real-data driver scores `second_grid` (quoting_real.py:205),
  the first feature row of each second between 09:31 and 15:59 New York
  (quoting_real.py:72, 363). So decisions are at most one per second, and
  fewer in a second with no feature row.
- Between rows: `sim.on_event(ev)` (quoting.py:621) applies the event, then the
  loop records our fills. No code path reads the book state to act, so:
  - touch moves away from or through our quote: no reaction until the next row;
  - queue ahead of our order depletes: no reaction (the simulator tracks
    `ahead_qty`, rule 4 of `execution/simulator.py`, but the quoter never reads it);
  - adverse top-of-book imbalance: no reaction; the quoter never reads sizes.
- The only other cancels are the flatten sweep (quoting.py:571-580), which starts
  60 s before 16:00 under the v2 config.
- What it can do at a row: reprice both sides from the touch (rule 8 tick mode
  under v2), skew by inventory (`inv_skew_ticks = 1` per 100 shares), cap
  size by the hard inventory limit (`max_inventory = 300`), and amend size down
  in place (`amend_size_down`). Every message takes the feed leg
  (`feed_latency_ns = 100 us`) plus the order latency path.

**Consequence for the task.** Part (c) of the brief (inventory skew and an
inventory cap) already exists in the grid quoter under the v2 config and is
recomputed at every decision; the event-driven mode reuses it unchanged
rather than adding a second skew. What is missing is the timing (a), the
guard (b) and pacing (d). The comparison below therefore isolates "when the
quoter decides", not "how it prices".

## 2. The event-driven mode (rule 10 of `quoting.py`)

**Plain idea.** Keep every grid decision, and also decide right after any
book event that moves the touch, fills us, or changes the guard state. Each
such decision is just as late as a grid decision would be: it sees the
book after the event and its messages leave at event time plus the feed leg,
then take the order latency to reach the venue.

**Formal statement.** `QuotingConfig(trigger_mode="events")`. After each
replayed event at time `t` (before the flatten, after the first score row),
an event decision runs when the touch prices changed, one of our quotes
filled, or the guard state of either side changed, or when a previous
decision was held back. It recomputes both wanted prices and sizes exactly
as a score-row decision does (using the `er` of the last row; the sigma
EWMA is updated only at rows) and sends messages with
`decision_ts = t + feed_latency_ns`. In events mode every decision also obeys:

- guard: a side whose wanted price is its own touch is not quoted while the
  size ahead of it is `<= ev_queue_ahead_max` and `opp / (own + opp) >= ev_imbalance`
  (displayed sizes at the two touches; size ahead = the resting order's
  `ahead_qty`, or the displayed level size for a new order);
- minimum resting time: no cancel, reprice or amend of an order still in
  flight or younger than `ev_min_rest_ns` since its venue arrival;
- message cap: no message that would make more than `ev_max_msgs_per_s` in
  the trailing second. A side held back by either rule retries at the next event.

**Worked example** (test `test_guard_cancels_before_sweep_iff_latency_allows`).
Our 100-share bid joins 10000 behind 300 shares. Executions take 250 of
those, leaving 50 ahead. Then 2000 shares join the ask: `2150 / 2200 = 0.98 >= 0.8`
and `50 <= 100`, so the guard fires at `tg` and sends a cancel that reaches
the venue at `tg + 100 us + 250 us`. A sweep that takes 10000 and 9999 at
`tg + 349 us` still fills us (100 shares, then the price is lower); at
`tg + 350 us` or later it does not. The grid quoter is filled in every case.

## 3. Pre-specification (fixed before any real-data run; this section is committed first)

Every value is chosen from first principles, without looking at any real
data from this work. There is no parameter search; the real-day run uses these
values once.

| parameter | value | reason |
|---|---|---|
| `ev_min_rest_ns` | 100 ms | One tenth of the grid period: the event quoter may react up to ten times faster than the grid quoter, no faster. About 300 times the modelled message path (100 us feed + about 250 us order leg), so a quote is at risk for a real interval before it can be pulled, and the policy cannot win by flickering. |
| `ev_max_msgs_per_s` | 10 per symbol | The grid quoter sends at most about 4 per second (cancel and new on each side). 10 lets the event quoter follow most touch changes in quiet periods and binds in busy ones; at most about 234,000 messages per symbol per session. |
| `ev_queue_ahead_max` | 100 shares | One order of our own size (`qty`): we are within one lot of the front, so the next trade at our price is likely to reach us. That is the moment adverse selection matters. |
| `ev_imbalance` | 0.8 | The opposite touch holds four times our touch's size. A round, strong threshold, so the guard fires on clear pressure only. |
| inventory skew and cap | unchanged: `inv_skew_ticks = 1` per 100 shares, `max_inventory = 300` | Already part of the v2 quoting config; shared by both quoters (section 1). |

Settings common to both quoters on the real day (the signed v2
`quoting_config` shape plus the v2 execution-realism opt-ins, with what one
day cannot supply): `skew_mode = "ticks"`, `h_min_ticks = 0` (join the touch),
`qty = 100`, `gamma = 0.1`, `tau_s = 10`, flatten at 16:00 with a 60 s lead,
`flatten_route = "sim"`, `amend_size_down`, hidden-print fills, regulatory fees,
`feed_latency_ns = 100 us`. Two deviations from the v2 runs, the same for both
quoters: (1) `expected_return = 0`, `alpha_weight = 0` (the no-skew control;
fitting an alpha needs earlier sessions); (2) no calibration, because there is
no earlier session on disk: the order latency comes from the dataset's venue
config and the adverse-selection floor term is 0, so both quoters join the touch.
Decision rows: the first event of each second from 09:31 to 15:59 New York.

Measurement, identical for both: net P&L and the rule-6 decomposition
(spread captured, markout at 1 s, inventory P&L, flatten cost) plus rebates,
fees and impact; markouts of every quote fill at 1 s, 10 s and 60 s;
fill rate; fills during cancel; message counts; share of time with a quote
resting at the touch and at any price; inventory distribution.

Looks: exactly two configurations (`grid`, `events`) per symbol, three
symbols, one session: six runs, run once each. The driver prints the count
before it runs: `python -m iap.research.quoter_event_dev`.

## 4. Synthetic results (mechanics, not economics)

Tests: `python/tests/test_quoting_event_driven.py`, 13 tests. They cover:

- grid mode byte-identical to the code before this work (two pinned digests, default and v2-shaped);
- `dev_diagnostics` changes no number;
- events mode is deterministic and keeps the P&L identity and the inventory limit;
- repricing on a touch move is decided at the event and arrives only after the feed and order latency, and the grid quoter never reprices;
- the minimum resting time defers a reprice;
- the guard cancels before a planted sweep exactly when `gap >= feed + latency`, and it does not fire without the imbalance;
- the message cap holds in every trailing second;
- the minimum resting time holds on every cancel.

The comparison below uses the repository's random MBO generator
(`synth_events`, 6000 steps). It has no informed flow, and its spreads are
often wider than one tick, so faster requoting is mechanically rewarded.
Read it as a check that the mode runs and reacts, not as evidence about
real markets.

| seed | drift | quoter | net | spread | markout 1 s | markout 10 s | inventory P&L | flatten | rebates | fills | messages | at touch (bid) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 0.0 | grid | +642.74 | +683.25 | -22.75 | -34.75 | -29.75 | -6.75 | +18.90 | 142 | 156 | 0.15 |
| 1 | 0.0 | events | +1655.18 | +1667.50 | -28.50 | -52.00 | -10.00 | -13.50 | +40.00 | 263 | 615 | 0.21 |
| 1 | 0.15 | grid | +2119.04 | +2189.50 | -28.50 | -25.75 | -35.50 | -26.00 | +19.70 | 137 | 148 | 0.11 |
| 1 | 0.15 | events | +4519.24 | +4527.25 | +6.50 | -29.00 | -30.25 | -26.00 | +41.90 | 285 | 624 | 0.18 |
| 2 | 0.0 | grid | +904.00 | +887.00 | +5.50 | -1.75 | -8.50 | +0.00 | +20.00 | 138 | 150 | 0.09 |
| 2 | 0.0 | events | +1786.60 | +1777.00 | -16.00 | -23.75 | -13.00 | +0.00 | +38.60 | 241 | 628 | 0.19 |
| 2 | 0.15 | grid | +1915.52 | +2142.00 | -43.25 | -74.00 | -68.75 | -133.00 | +19.20 | 125 | 154 | 0.11 |
| 2 | 0.15 | events | +4528.24 | +4546.25 | -39.25 | -42.25 | +14.75 | -33.25 | +39.90 | 267 | 628 | 0.17 |
| 3 | 0.0 | grid | +294.60 | +287.50 | -31.00 | -10.50 | +20.50 | +0.00 | +17.60 | 123 | 163 | 0.11 |
| 3 | 0.0 | events | +652.94 | +642.00 | -17.25 | -22.00 | -8.50 | -0.25 | +37.10 | 251 | 611 | 0.21 |
| 3 | 0.15 | grid | +1134.84 | +1171.00 | +5.00 | -19.50 | -53.75 | -6.75 | +19.50 | 138 | 155 | 0.05 |
| 3 | 0.15 | events | +2311.14 | +2326.25 | -24.50 | -41.50 | -22.00 | -6.75 | +38.30 | 246 | 633 | 0.16 |

## 5. Real day: 2019-01-30, AAPL, MSFT, QQQ (in-sample, development)

Data: ingested from the raw Nasdaq ITCH file with the plain `ingest` command,
which keeps the raw file; the book check was clean. Output is in
`data/real/quoter_dev/ds_20190130` (git-ignored). Per-symbol JSON:
`research/round1/quoter_event_dev/`. The driver printed
`configurations: 2 per symbol x 3 symbols = 6` before running. An earlier
launch stopped while loading the fee schedule, before any configuration
ran.

USD, one session. Markouts are per quote fill, against the pre-fill mid, in bps of quote notional (negative is adverse). Total bps columns pool the three names by quote notional.

| metric | AAPL grid | AAPL events | MSFT grid | MSFT events | QQQ grid | QQQ events | total grid | total events |
|---|---|---|---|---|---|---|---|---|
| net | -6,465.60 | -6,426.82 | -1,733.31 | -2,500.32 | -1,956.30 | -2,274.38 | -10,155.22 | -11,201.51 |
| spread captured | 3,265.70 | 2,685.49 | 1,649.64 | 1,243.55 | 1,272.49 | 795.58 | 6,187.84 | 4,724.63 |
| markout at 1 s (rule 6) | -8,309.25 | -7,838.08 | -3,645.27 | -4,038.92 | -4,096.36 | -3,752.57 | -16,050.87 | -15,629.57 |
| inventory P&L | -2,141.84 | -1,760.15 | -453.16 | -222.67 | 69.13 | 14.49 | -2,525.86 | -1,968.33 |
| flatten cost | -1.23 | -0.25 | -0.24 | -0.40 | -0.69 | -0.50 | -2.16 | -1.15 |
| rebates | 735.73 | 757.63 | 728.06 | 848.66 | 829.89 | 919.57 | 2,293.68 | 2,525.86 |
| taker fees (incl. regulatory) | 14.62 | 269.96 | 12.29 | 328.88 | 30.50 | 248.76 | 57.41 | 847.60 |
| impact | 0.09 | 1.49 | 0.06 | 1.66 | 0.27 | 2.20 | 0.42 | 5.35 |
| markout 1 s, bps | -0.61 | -0.52 | -0.59 | -0.50 | -0.26 | -0.20 | -0.45 | -0.37 |
| markout 10 s, bps | -0.70 | -0.60 | -0.69 | -0.53 | -0.26 | -0.23 | -0.50 | -0.42 |
| markout 60 s, bps | -0.81 | -0.65 | -0.67 | -0.54 | -0.25 | -0.22 | -0.54 | -0.44 |
| spread captured, bps | 0.24 | 0.18 | 0.27 | 0.15 | 0.08 | 0.04 | 0.17 | 0.11 |
| net, bps | -0.47 | -0.42 | -0.28 | -0.31 | -0.12 | -0.12 | -0.29 | -0.27 |
| quote fills | 11,844 | 12,386 | 7,311 | 9,425 | 11,442 | 12,646 | 30,597 | 34,457 |
| quote shares filled | 839,114 | 929,638 | 584,049 | 764,319 | 960,120 | 1,125,190 | 2,383,283 | 2,819,147 |
| maker share of quote fills | 0.997 | 0.945 | 0.995 | 0.906 | 0.993 | 0.951 | | |
| fill rate, bid / ask (filled / posted) | 0.33 / 0.33 | 0.14 / 0.15 | 0.38 / 0.38 | 0.20 / 0.21 | 0.41 / 0.41 | 0.17 / 0.17 | | |
| fills during cancel (flatten) | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 1 |
| messages (submits + cancels) | 43,876 | 122,992 | 26,028 | 72,367 | 37,702 | 125,995 | 107,606 | 321,354 |
| time at touch, bid / ask | 0.40 / 0.38 | 0.41 / 0.40 | 0.54 / 0.53 | 0.57 / 0.55 | 0.47 / 0.46 | 0.49 / 0.58 | | |
| time quoted, bid / ask | 0.86 / 0.86 | 0.85 / 0.84 | 0.91 / 0.91 | 0.93 / 0.93 | 0.86 / 0.85 | 0.89 / 0.87 | | |
| mean abs inventory (shares, time-weighted) | 48.3 | 45.8 | 41.6 | 45.0 | 41.2 | 39.9 | | |
| abs inventory p90 / p99 / max | 149 / 265 / 300 | 140 / 242 / 300 | 124 / 218 / 300 | 134 / 225 / 300 | 120 / 209 / 300 | 118 / 207 / 300 | | |
| event decisions | - | 1,071,756 | - | 763,664 | - | 1,875,551 | | |
| guard pulls | - | 13,054 | - | 12,374 | - | 24,751 | | 50,179 |
| event retries held by min rest / by rate cap | - | 719,417 / 821,105 | - | 528,692 / 444,009 | - | 887,588 / 1,627,841 | | |

## 6. Reading

**Plain statement.** On this one day, reacting to the book did not rescue
the quoter. Both lose: about 10.2 thousand USD (grid) against 11.2 thousand
USD (events) over the three names. Per dollar, the event quoter's fills are
less adversely selected: the 1 s markout is -0.37 bps against -0.45 bps,
and the gap holds at 10 s and 60 s. It gives that gain back, and more, in
spread captured (0.11 against 0.17 bps) and in taker fees.

**What the difference shows.**

- The mechanism the review named is real, but it is small here. Repricing
  and the guard cut the adverse markout by about 0.08 bps per filled
  dollar. Most of the markout loss (about 16 thousand USD) remains in both
  quoters. So being picked off between grid points is not the main source
  of the loss. The quoter joins the touch with no alpha and no
  adverse-selection floor, and touch fills on these names are adverse at
  these horizons either way.
- The event quoter paid for its speed. It sent three times the messages and
  about halved the fill rate per posted share. It also turned 5 to 9 per cent
  of its quote fills into taker fills: a reprice decided on a touch move can
  arrive after the book has moved through its price, and without `post_only`
  it then takes. That cost about 850 USD in fees, against 57 USD for the grid
  quoter. Turning `post_only` on would be a second look if done after this
  table, so it is listed as a follow-up and not applied.
- Pacing bound the policy hard. The rate cap and the minimum resting time
  held back more than a million event retries in total. These numbers
  describe a paced quoter, not an unconstrained one.

**What it does not show.** One session, three names, in sample. Both
quoters ran without a calibration from an earlier day, without an alpha,
and with an adverse-selection floor of 0. One day gives no variance
estimate. A difference of about 1 thousand USD on one day says nothing
about which quoter is better on average. Nothing here bears on the signed
v2 result, and nothing here says any quoter would earn money.

**Looks taken:** 6 (2 configurations x 3 symbols x 1 session), each run
once with the parameters of section 3. These are for the coordinator to enter in the
experiment ledger. `research/experiments.json` was not edited here.
