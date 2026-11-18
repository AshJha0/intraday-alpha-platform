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
