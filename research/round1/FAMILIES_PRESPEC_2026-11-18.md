# Three further bar families, specified before the pooled reversal result (2026-11-18)

Status: pre-specification. Written and committed before any code for these families exists
and before the pooled ten-session result of `tools/breadth_bars_pooled.py` was read by its
author. No bars file, ITCH file or pooled result was opened to write it: it rests on the
definitions in `python/src/iap/research/breadth_bars.py`, the one-session note
`BREADTH_BARS_20190130.md`, `docs/research/PNL_POWER_TABLE.md` and published market-structure
knowledge. The commit that adds this file dates the choices. Every parameter below has one
value; there are no grids. Changing any of them after a result exists makes a new family
with its own looks.

**The plain idea.** The round-1 study asks one question of one-minute bars: does a
cross-sectional 15-minute reversal earn more than the quoted spread? Three further questions
use the same bars, the same universe and the same book, and each costs one look: does the
last half hour continue (F1), does the opening move reverse (F2), and does the closing
auction's published imbalance move the price into the cross (F3)?

## 0. What all three share

Notation of `breadth_bars`: bar `m = 0..389` ends at 09:31 + m minutes, so bar 14 ends at
09:45, bar 44 at 10:15, bar 324 at 15:55, bar 325 at 15:56 and bar 389 at 16:00. `mid[m]` is
the end-of-bar mid, missing when a side is empty or the book is locked or crossed; `hs[m]` is
the quoted half spread over the mid.

- **Universe.** The 500 names fixed on 2019-01-30 (`research/round1/universe_20190130.json`),
  taken on every session with `breadth_bars.rows_for`; names missing on a day are counted,
  never replaced.
- **Sessions.** The ten in-sample full-market ITCH days (`ROUND0_GATES.md` section 8). Every
  session dated 2025-01-01 or later is the sealed holdout and is refused by the code.
- **Entry delay.** One bar is the primary: a decision at the end of bar `k` trades at the
  quote at the end of bar `k + 1`. Delay 0 is a declared diagnostic only.
- **Book.** Equal weight long the top decile (`q = 0.1`, `floor(q n)` names a side) and short
  the bottom decile of the score among names with a finite score, forward return and cost;
  no book when fewer than 10 such names. Full turnover at every rebalance. Return on gross
  `g = (mean_long f - mean_short f) / 2`. The book crosses the quoted spread to enter and to
  exit, each unit of gross paying its own name's cost: `net = g - (mean_long c + mean_short c) / 2`
  with `c_i = hs_i[entry] + hs_i[exit]`. This is `breadth_bars._book` itself, imported, not
  rewritten. F3 differs only where its exit is the cross (section 3).
- **Primary statistic.** For each session, the mean net bps per rebalance over that
  session's rebalances; the ten session numbers go to `breadth_bars.pool_sessions` (mean,
  standard error, Student t on sessions - 1 degrees of freedom, one-sided p for a positive
  mean, count of positive sessions). The one-sided direction is "net above zero" for every
  family: the book is signed by the expected sign, so a positive net is the claim.
- **Declared diagnostics (not tests).** Per family: mean rank IC across sessions, mean gross
  bps, median round-trip cost bps, median names used, the delay-0 version of the primary,
  and the family-specific items listed below. A diagnostic never promotes a family.
- **Not modelled.** Fees, rebates, impact, borrow, halts. Nasdaq's own quote is wider than
  the consolidated quote, which overstates the spread cost; missing fees and impact go the
  other way.

## 1. F1 intraday momentum (30 minutes)

**Plain idea.** Names that rose most against the cross-section over the last 30 minutes keep
rising against it over the next 30.

**Mechanism and literature.** Two forces pull in opposite directions at this horizon.
Continuation: slow diffusion of firm news and order splitting by large traders, whose
child orders keep pushing the price the same way (Hong and Stein 1999 on gradual
information diffusion; Kyle 1985 on a large trader spreading an order; the persistence of
order flow is documented by Lillo and Farmer 2004, to be verified). Reversal: the
compensation liquidity providers earn for absorbing order imbalances (Grossman and Miller
1988; Nagel 2012), which makes short-horizon cross-sectional returns revert (Lehmann 1990
and Jegadeesh 1990 at weekly and monthly horizons; at intraday half-hour intervals Heston,
Korajczyk and Sadka 2010 find reversal at short lags together with continuation at daily
multiples of the same half hour). Gao, Han, Li and Zhou 2018 report intraday momentum in
the market's time series (first half hour predicting the last), which is not a
cross-sectional result and is cited only to be clear it is a different claim. Who is on the
other side of a continuation trade: the liquidity providers who absorbed the first leg.

**Formal definition.**
- Inputs: `mid` only.
- Decision bars: `k = 30, 60, ..., 330` (`breadth_bars.decision_bars(30, 30, 1, 30)`,
  11 rebalances a session).
- Score at `k`: `r_i = mid_i[k] / mid_i[k - 30] - 1`; `s_i = r_i - mean_j r_j` over the
  universe names with finite `r` (cross-sectionally demeaned; higher = buy).
- Entry `e = k + 1`, exit `x = k + 31` (horizon 30 bars); `f_i = mid_i[x] / mid_i[e] - 1`.
- Cost and book as in section 0.

**Expected sign.** Positive under continuation, but the stated prior leans negative: the
literature above finds reversal at sub-hour cross-sectional horizons. F1 is the direct test
of the opposite of the existing `residual_reversion` score at a longer lookback, and its
result is not independent of that score's (diagnostic: the correlation of F1's session
series with the pooled `residual_reversion` session series is reported, not tested).

**Ends the family.** Pooled mean net bps at or below zero, or one-sided p above 0.025
(unadjusted). A negative gross is not re-read as a reversal win: flipping the sign is a new
family with its own looks.

**Looks.** One primary statistic; ten session looks in the ledger's counting.

## 2. F2 opening reversal

**The overnight-gap version and why it is not the primary.** The natural definition is the
gap `G_i = mid_i(first valid bar end at or after 09:35, today) / mid_i(last valid bar end at
or before 16:00, previous session) - 1`, reversed over the morning. It needs the previous
trading session's bars. The ten in-sample sessions are scattered over 2019 to 2021 and the
code accepts a pair only when the earlier file is dated the previous weekday (a holiday in
between makes the pair undefined rather than risk a wrong pair; no calendar is consulted);
on that sample there are few or no such pairs, so the gap version would rest on almost no
sessions. It is implemented, returns "undefined" for a session without its previous
session, is reported only as a count of defined sessions, and costs no look here.

**Primary: the within-day opening move.** The 09:31 to 09:45 move, reversed by 10:15.
Justification: it carries the same economic content (the opening is where overnight
information and the opening cross's order flow are absorbed, and the first quarter hour is
where spreads and volatility are highest, so price pressure is largest), it is defined on
every session from that session's bars alone, and its start at 09:31 avoids the open cross
print itself, which the bars do not contain.

**Mechanism and literature.** Liquidity providers absorb the order flow that arrives with
the open and are paid by a partial reversal (Grossman and Miller 1988; Nagel 2012).
Opening-period price pressure and its reversal: Berkman, Koch, Tuttle and Zhang 2012 (on
attention-driven buying at the open and the subsequent intraday reversal; details to be
verified); Lou, Polk and Skouras 2019 (overnight versus intraday returns, a related but
daily-frequency result); Heston, Korajczyk and Sadka 2010 (half-hour reversal). Who is on
the other side: traders who must transact near the open (overnight news, opening
rebalancing) and pay for immediacy.

**Formal definition.**
- Inputs: `mid` only.
- Decision bar: `k = 14` (09:45), one rebalance a session.
- Score: `r_i = mid_i[14] / mid_i[0] - 1`; `s_i = -(r_i - mean_j r_j)` (higher = buy the
  names that fell most).
- Entry `e = 15` (09:46), exit `x = 44` (10:15): the exit is fixed in clock time, so the
  horizon is 29 bars at the primary delay (30 at the delay-0 diagnostic).
- Cost and book as in section 0.
- Overnight version (no look): score `-(G_i - mean_j G_j)` at the bar used for the today
  mid, same entry delay and exit at 10:15.

**Expected sign.** Positive (reversal).

**Ends the family.** As F1.

**Looks.** One primary statistic; ten session looks. The overnight version: zero looks on
this sample, by construction undefined or nearly so.

## 3. F3 closing-auction imbalance

**Plain idea.** From 15:50 Nasdaq publishes, for each security in the closing cross, how many
shares are paired and how many are left over on one side. A large buy imbalance has to be
met by someone; the price between 15:55 and the cross should move toward the imbalance.
Buy the names with the largest buy imbalance relative to their volume, sell those with the
largest sell imbalance, and close out in the cross.

**Mechanism and literature.** Market-on-close demand (index funds, benchmarked managers)
is price-insensitive and concentrated in the cross; liquidity providers who offset it ask
a premium, so the cross price leans toward the imbalance (Cushing and Madhavan 2000 on
returns and trading at the close; Bogousslavsky and Muravyev, on who trades at the close
and closing price pressure, year and venue to be verified). The imbalance message
itself is described in Nasdaq's TotalView-ITCH 5.0 specification (Net Order Imbalance
Indicator, message type `I`) and Nasdaq's closing cross rules; the dissemination schedule
in the sample years (from 15:50, with a faster cadence near 15:55) is to be verified
against the Nasdaq rulebook of those years. Who is on the other side: the continuous-market
sellers at 15:56 and the price-insensitive close buyers in the cross. Against it: the
imbalance is public from 15:50, so fast liquidity providers may already have moved the
15:55 quote; the expected effect after one minute's delay is small.

**Data.** Two ITCH messages decoded by `itchbars --auction <path>` (offsets in section 6):
the `I` rows of the closing cross (cross type `C`) from 15:50:00 to 16:00:00, and the
closing cross print `Q` (cross type `C`).

**Formal definition.**
- Decision time: 15:55:00, the end of bar 324. For each name the last closing `I` message
  with timestamp at or before 15:55:00.000000000 is used; a name with none has no score.
- Normaliser (chosen: trailing displayed volume, not paired shares).
  `V_i = sum of displayed vol over bars 0..324`. Paired shares are zero or tiny for many
  names at 15:55 (the book has not yet attracted its offsetting interest), which makes the
  ratio explode or undefined exactly where the information is; the day's displayed volume
  is always defined for a universe name, measures how much liquidity the imbalance must
  draw on, and uses only data up to the decision.
- Score: `s_i = d_i * imbalance_shares_i / V_i`, with `d = +1` for direction `B`, `-1` for
  `S`, `0` for `N` (no imbalance); direction `O` (insufficient orders) or `V_i = 0` gives no
  score. Ties (many zeros) are broken by the stable sort on universe order, which is fixed.
- Entry: the quote at the end of bar 325 (15:56) at the primary delay; `e = 325`.
- Exit: the closing cross price `P_i` from the `Q` row; `f_i = P_i / mid_i[325] - 1`.
- Cost: `c_i = hs_i[325]` only. The exit trades at the single cross price, so no spread is
  paid on it. The book arithmetic is otherwise section 0's.
- One rebalance a session; a name without a cross print has no forward return.

**What is and is not tradable about the exit.** The cross price is a real, single price at
which the whole cross executes, and the trade exits in the direction that offsets the
published imbalance (a long bought on a buy imbalance is sold into the cross), which is the
side the cross needs. But in the sample years Nasdaq accepted market-on-close and
limit-on-close orders only up to a cut-off before 16:00 and, after it, only
imbalance-only orders that offset the imbalance (the cut-off times in each sample year are
to be verified). An exit decided at 15:55 may therefore have to be an imbalance-only order,
which is not guaranteed to fill, may be priced away by the cross, and fills only if the
imbalance is still on the same side at the cross. The study assumes a full fill at the cross
price: an upper bound on what was tradable. The entry at the 15:56 quote is tradable in
the ordinary sense, with the caveats of section 0.

**Declared diagnostics.** Section 0's, plus: exit at the 16:00 bar-end mid instead of the
cross (continuous-market version, paying `hs[325] + hs[389]`); the fraction of the
universe with a score; the mean `|s|` of the long and short legs.

**Expected sign.** Positive (move toward the imbalance into the cross).

**Ends the family.** As F1; also ended, without a look being spent, if fewer than 100
universe names have a closing `I` message at or before 15:55 on a majority of sessions
(a coverage fact, read before any return is computed).

**Looks.** One primary statistic; ten session looks. F3 needs the `--auction` output for
each session, which requires the raw ITCH file of each day again (the bars chain deletes
it after the bars are built). Fetching those files is the owner's decision; until then F3
has no data and no look is spent.

## 4. Multiplicity

Primary statistics on the same ten sessions: the two existing reversal scores at two entry
delays (`breadth_bars_pooled.py` counts both delays as looks), 4, plus the three family
primaries, 3: **K = 7**. Declared diagnostics are not counted because none can promote a
family.

- Bonferroni session-level threshold: one-sided p below 0.025 / 7 = **0.00357**, which on
  9 degrees of freedom is a session-level t above **3.46**.
- Ledger counting: 10 session looks per family, 30 for the three families on top of the
  existing 40.
- Power (`PNL_POWER_TABLE.md`, `iap.research.pnl_power.mde_sharpe`): ten sessions at K = 7
  detect with 80 % power only an annualised net Sharpe above about **22** (about 16 at
  K = 1). So each family can show: a gross effect against its cost, its sign, whether it is
  consistent across sessions, and whether it is enormous. It cannot show a realistic
  Sharpe (1 to 3); a failure to reject says almost nothing about an effect of that size,
  and a rejection on ten sessions is a reason for the holdout, not a confirmation.

## 5. The sealed nine-session holdout: at most one goes forward

Rule, fixed now:

1. Candidates are the three families' primaries. If either existing reversal score at the
   1-minute delay also passes step 2, it joins the same race, so at most one idea in total
   leaves this round.
2. A candidate qualifies if, on the ten in-sample sessions, its pooled mean net bps is above
   zero, its one-sided p is below 0.00357 (section 4), and at least 7 of the 10 sessions
   have a positive net.
3. If more than one qualifies, the one with the largest session-level t goes forward;
   ties by the smaller one-sided p, then by the order F1, F2, F3, the existing scores.
4. If none qualifies, nothing goes forward and the holdout stays sealed.
5. The one that goes forward is run once on the nine holdout sessions with exactly these
   parameters, the same universe file and the same code at a tagged commit, as a one-sided
   Student t on 8 degrees of freedom at 0.025 (K = 1). Reading the holdout bars needs the
   owner's decision (the bar tools refuse 2025 sessions), and F3 also needs the holdout
   auction files.

## 6. Bar tool extension (for F3)

`itchbars --auction <path>` writes a second CSV beside the bars; without the flag the
program and its bars output are unchanged, byte for byte (tested). Offsets, counted from
the message-type byte at offset 0, from the Nasdaq TotalView-ITCH 5.0 specification and
cross-checked against `_I` and `_Q` in `python/src/iap/marketdata/itch50.py`:

| `I` (length 50) | offset | width | `Q` (length 40) | offset | width |
|---|---:|---:|---|---:|---:|
| stock locate | 1 | 2 | stock locate | 1 | 2 |
| tracking number | 3 | 2 | tracking number | 3 | 2 |
| timestamp (ns) | 5 | 6 | timestamp (ns) | 5 | 6 |
| paired shares | 11 | 8 | shares | 11 | 8 |
| imbalance shares | 19 | 8 | stock | 19 | 8 |
| imbalance direction | 27 | 1 | cross price | 27 | 4 |
| stock | 28 | 8 | match number | 31 | 8 |
| far price | 36 | 4 | cross type | 39 | 1 |
| near price | 40 | 4 | | | |
| current reference price | 44 | 4 | | | |
| cross type | 48 | 1 | | | |
| price variation indicator | 49 | 1 | | | |

Kept rows: `I` with cross type `C` and timestamp in [15:50:00, 16:00:00]; `Q` with cross
type `C` at any time.

## 7. Worked example (F3, made-up numbers)

A name has displayed volume 2,000,000 shares over bars 0..324 and its last closing `I` at
15:54:58 says imbalance 50,000 shares, direction `B`: `s = 50,000 / 2,000,000 = 0.025`. If
that is in the top decile, it is bought at the 15:56 ask. With a 15:56 mid of 40.00, a
half spread of 0.0005 (2 cents on 40.00) and a cross at 40.06, `f = 40.06 / 40.00 - 1 =
15 bps` and the name's cost is `5 bps`. Over a book with mean long `f` of 6 bps, mean short
`f` of -2 bps and mean cost 4 bps on each side, `g = (6 + 2) / 2 = 4 bps` and
`net = 4 - (4 + 4) / 2 = 0 bps`: no edge, even though the move went the expected way.
