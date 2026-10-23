# REL-08 / REL-08c anomaly investigation (2026-10-20)

Read-only. No code, prereg, run or PR changes. Files read: the result files named in the brief, `python/src/iap/auction/rel08c_{study,extract}.py` (worktree `wt-q-rel08c`), `python/src/iap/research/{rel08_screen,breadth_book}.py` and `alpha/data.py` (worktree `wt-rel08-run`), `research/v118/rel08_rule_signed.json`.

Data access: `research/holdout_registry.json` lists `2019-01-30` as `[{"state": "in_sample", "symbols": "*"}]`. I read only the small derived extract CSVs in `data/real/rel08c_2019-01-30` (noii/crosses/bbo_grid, about 220k rows). I did not read ITCH bytes. I did not call `guard_or_exit`; I imported only the pure functions `load_extract` and `decision_trades`, with `PYTHONDONTWRITEBYTECODE=1`. `data/real/ds_20190130_rel07` does not exist at that path, so nothing was read from it. The REL-08 analysis uses only the result JSON and the code.

## Summary

| # | Anomaly | Verdict | Affects stop/continue? |
|---|---|---|---|
| 1a | REL-08c `gross to cross` about 1.6e3 bps | **Bug** (printed metric only) | No: `decide()` uses only `net_taker_bps` |
| 1b | REL-08c imbalance n = 0 | **Design flaw in the pre-declaration** (the premise is false for 2019 data); not a code bug | Yes, structurally. The imbalance arm can never trade at the registered times, so the rule can never return continue or reject on this data |
| 1c | REL-08c incremental +48.9 bps with CI [-inf, inf] | Arithmetic artefact (flat arm scored 0, baseline mean tail-dominated) | Printed only; verdict is underpowered regardless |
| 2a | REL-08 V1/V2 net printed `-0.000`, costs 0.03 | **Not a scale bug**; units are mislabelled, and the book barely trades | See 2b |
| 2b | V1/V2 book trades about $3 per rebalance on $1M | Genuine optimiser behaviour under the registered parameters (cannot fully confirm without a re-run) | C2 passes vacuously |
| 2c | C1 fails for every variant (max gap 380-405 min vs limit 30) | **Probable bug** in the gap computation (leading gap counted) | Yes. It makes `continue` impossible on this dataset; the 7 verdicts are underpowered either way |

## 1. REL-08c

### 1a. gross_cross 1,614 bps: a bug, reproduced exactly

I re-ran `load_extract` and `decision_trades` on the extract with the registered config (decision times 57060e9 and 57180e9 ns). It reproduces the report to the last digit: n = 179 baseline trades, `net_taker` -48.909, `gross_taker` 2.260, `gross_cross` 1614.353, `net_cross` 1581.045.

Cause, `rel08c_study.py` `decision_trades`:

- The cross price is `cross_px = float(cx["price_px4"].iloc[-1]) if not cx.empty else math.nan` (about lines 205-206).
- The guard only handles a *missing* row. `crosses.csv` carries a `Q` row with `shares = 0, price_px4 = 0` for every name that had no Nasdaq closing cross. That is 58 of the 100 `C` rows, which are names whose primary listing is on another exchange (PCG, SQ, C, CHK, ABEV and others). I confirmed that `shares == 0` and `price == 0` coincide in all 58.
- Then `gross_cross = 1e4 * d * (cross_px - mid) / mid` with `cross_px = 0` gives exactly `-1e4 * d` = ∓10,000 bps. 97 of the 179 baseline trades are on such names.
- Their mean is +2,990 bps (63 shorts at +10,000, 34 longs at -10,000). The mix of signs gives the 1,614 average.
- On the 82 trades with a real cross price, `gross_cross` averages -12.6 bps (median -6.8) and `net_cross` averages -35.1 bps.
- It is not a unit bug. `PRICE_UNIT = 10_000` is applied consistently, `mid` and `cross_px` are both in 1/10000 dollars, and the fee conversion is right. (Check: `fee_bps = 1e4 * 0.003 * 1e4 / mid`, about 0.6 bps for a $50 stock.)
- The `loc_fill` metric (0.508) is contaminated the same way. `math.isfinite(0.0)` is true, so `cross_px = 0` yields `loc_fill = 1` for every sell-to-close and `0` for every buy-to-close.

Verdict impact: none. `decide()` and `session_table()` use only `net_taker_bps`. Only `gross_cross_bps`, `net_cross_bps` and `loc_fill`, which are reported separately, are wrong.

Proposed fix (not applied), in `decision_trades`:

```python
cx = close[(close["symbol"] == sym) & (close["shares"] > 0) & (close["price_px4"] > 0)]
cross_px = float(cx["price_px4"].iloc[-1]) if not cx.empty else math.nan
```

`gross_cross`, `net_cross` and `loc_fill` then become NaN for names with no Nasdaq cross, and the `_m` helper already skips NaN via the pandas mean. Add a unit test with a `shares = 0, price = 0` cross row. The names without a Nasdaq cross should also be reported as a count. Strictly, the whole "cross" measure should be restricted to names whose closing cross is on Nasdaq.

### 1b. Imbalance policy n = 0: the pre-declaration's premise is false

- `PREDECLARATION.md` line 41 says "the closing NOII starts at 15:50; 15:51 has six 10 s snapshots", and line 57 says "none before 15:50".
- In the 2019-01-30 file, **every** one of the 30,010 closing-cross (`C`) NOII rows has `tod_ns >= 57,300.005e9`, i.e. **15:55:00.005 or later**. The per-symbol earliest snapshot ranges from 15:55:00.005 to 15:55:00.0075, and there are none before 15:55:00.
- `distance_to_close_s` starts at 299.99. The opening NOII does start 2 minutes early (09:28), as expected.
- This fits the October 2018 Nasdaq change: the MOC cutoff moved to 15:55 and the closing NOII stream now starts at 15:55.
- `features_at(noii, sym, t)` for t = 15:51 and 15:53 therefore finds no snapshot. `level = NaN`, and `_direction` returns 0 (`not math.isfinite(lvl)`). I checked that no symbol has a qualifying snapshot at either decision time. At t = 15:55:00 or later, 2,655 snapshot rows would qualify (|level| ≥ 0.2 and persistence ≥ 2), but those times fall outside the registered window.
- The registered taker exit is 15:54:55, **before the first NOII snapshot exists**. Even a decision time of 15:54:55 could not use imbalance information. The REL-08c design (imbalance as a signal for a taker round trip that ends before the MOC cutoff) cannot be executed with this feed.
- Not a code bug. The thresholds are in the right units (level in [-1, 1], threshold 0.2), and the join is not empty. The noii file has full coverage (42,075 rows, 100 names).

Verdict impact: structural. The verdict is "underpowered" regardless, but also for any future session of this kind. The imbalance arm will always have 0 trades, so `min_trades = 30` can never be met and the rule can never yield continue or reject. A multi-session extension of the same design would not fix that.

Proposed fix: this needs a **new pre-declaration**, not a code patch, and the owner decides which of three directions:

- (a) Define the signal as the 15:55:00-15:55:05 snapshot and trade the cross (MOC). That is no longer an entry-before-cutoff taker round trip, and the entry would be the closing cross itself.
- (b) Use only the first snapshot to decide an after-the-fact classification.
- (c) Drop REL-08c.

Whichever is chosen, the declaration should be tested first on a few seconds of NOII data (the first-snapshot time per day) before signing.

### 1c. Incremental +48.9 bps and CI [-inf, inf]

- `session_table` scores a flat policy as 0.0 net bps (documented: "0 for a flat policy"). Incremental = 0 - (-48.909) = +48.909, which is just the negative of the baseline mean cost. It carries no information about the imbalance signal.
- The CI is [-inf, inf] because there is one session (`s < 2`), as designed.
- The baseline mean of -48.9 bps is real arithmetic but tail-dominated: HMNY -6,009 bps, OHGI -835 and -551, SGYP -507 and -465, RWLK -136. The median is -3.75 bps. Dropping only the 3 worst trades gives -7.7 bps. These are wide-quote microcaps in the "breadth universe". The mean is not a robust statistic here, but it is the registered one.

Optional fix: report the median or a trimmed mean as a diagnostic next to the mean. For a flat arm, set the incremental to NaN, or define it over decisions where the imbalance arm trades, rather than 0.

## 2. REL-08 screen

### 2a. Not a scale bug; two real defects in presentation

- Section 4 mixes units under a heading that says "bps per rebalance". The first five columns (net mean, SE, lower, MDE) are bps. **The last three columns (gross, costs, net P&L) are cumulative dollars** from `res.gross_pnl`, `res.spread_cost + fee_cost + impact_cost` and `res.net_pnl` of the whole run (`rel08_screen.py` about lines 770-785). `to_bps = 1e4 / notional` is applied only to the `net_bps` series (line 482).
- The `_fmt` default is 3 decimals, so the net-mean values (V1 -7.37e-6 bps) print as `-0.000`. The JSON has the real values.
- The notional is the same for all variants: **$1,000,000** (`rel08_rule_signed.json` book.notional). No variant has a different unit.

Arithmetic check from the JSON for V1: net P&L -$0.01843 over 52 rebalances is -$3.5e-4 per decision. × 1e4/1e6 = -3.5e-6 bps. The recorded `net_bps.mean` over the 25 usable decisions is -7.4e-6 bps, the same order. For V5: -$9.47 / 52 = -$0.18, which maps to -1.8e-3 bps against a recorded -3.9e-3. The difference is the usable subset (25 of 52) against all decisions. The number is consistent, not wrong.

### 2b. Why V1/V2 cost $0.03 and V9-V11 cost $140-260

The whole difference is how much the book trades (`mean_turnover`, fraction of $1M per rebalance):

| variant | mean turnover | traded about | fee+spread+impact ($) |
|---|---|---|---|
| V1 | 3.3e-6 | $3 / rebalance | 0.03 |
| V2 | 2e-6 | $2 | 0.04 |
| V5 | 1.5e-4 | $150 | 5.95 |
| V6 | 2.8e-4 | $275 | 5.81 |
| V9 | 2.5e-3 | $2.5k | 227 |

(V9-V11 also have 155 rebalances, 3x as many as V1-V6.)

- Scores are demeaned and studentised in `compute_scores`, and the weights are rank-linear, so the score *scale* does not matter. A scale bug in the scores is therefore not the explanation.
- In `breadth_book.py` (about line 530) `alpha = alpha_bps * 1e-4 * target/peak`, so the extreme name carries a 5 bps expected return and the rest proportionally less. The registered optimiser (`risk_aversion = 1000`, `alpha_bps = 5`) only trades a name when its alpha beats its one-way cost (half-spread plus 0.003/px). Most of the cross-section sits inside that no-trade band. The book is almost flat, with a $1M notional and essentially $0-$3 of trade per rebalance for V1/V2.
- Why V1/V2 (100 names, `vwap_deviation`) trade about 50x less than V5/V6 (71 names, `residual_reversion`) I cannot determine from the recorded numbers. Candidates are the wider universe (the extra 29 names have no reference ETF), covariance conditioning, and rank structure. A re-run with a debug print of the per-name alpha-minus-cost is needed. **Cannot tell**.
- Consequence for the pre-declared criteria:
  - **C2 (MDE ≤ 2 bps) passes vacuously** for every variant. The MDE is about 1e-5 bps because the P&L itself is about 1e-5 bps, not because the estimate is precise.
  - **C3 (net ≥ 1 bps)** can never hold at the registered alpha and notional. The rule is calibrated such that the verdict would be `stop` once C1 and C4 pass.
  - In effect the screen tests a book that is nearly flat.

### 2c. C1 fails for every variant: probable bug

Every row fails C1 with the same max gap profile (390, 405, 380-385 min vs the rule's `max_gap_min = 30`; `usable_floor`, `names_usable_min` and the usable counts are met).

Code, `evaluate_variant`:

```python
gaps = np.diff(np.concatenate([[grid[0] - cfg.rebalance_ns], grid[usable_cols]]))
max_gap_min = gaps.max() / NS_MIN
```

- The first gap runs from the grid origin to the first usable rebalance. `make_grid` (`alpha/data.py:127`) covers the union span of **all** frames: `n_rebalances = 52` at 15 min is 780 minutes, i.e. 13 hours. That is about twice a 6.5 h session, so the span evidently includes extended hours (my reading, based on the numbers; I did not inspect the frame timestamps).
- Evidence for the leading-gap hypothesis: the reported max gaps vary with the variant's warm-up (V9 1-min horizon 380, V10 380, V11 385, V5 405, V1 390), which is what a "time until the first usable rebalance" would do, not a mid-session hole.
- So the check measures leading idle time, not data holes inside the session.

Verdict impact: with this bug, no variant can pass C1 on this dataset, so none can reach `continue` or `stop`. The result of "underpowered for all 7" would follow either way for a **single session** (the rule says one session cannot promote). For REL-08b or later screens on multiple sessions, the same check would block everything.

Proposed fix (not applied): measure the gap only between usable rebalances within the same session day (drop the prepended `grid[0] - reb` element and any gap spanning a session boundary), or measure it over the regular-hours window only. This needs a **rule amendment**, since `max_gap_min` is a signed criterion. The owner and reviewer should decide whether the signed rule text means "within-session gap". Confirming the span hypothesis needs only reading the frame `exchange_ts` min/max in the ds_20190130 dataset (in_sample), and I did not do that.

Also fix the labels: either convert gross/costs/net P&L to bps of notional or relabel the columns "$ (cumulative)", and print at least 6 significant digits for net bps (or use scientific notation) so that `-0.000` cannot hide a value.

## Pre-declaration / continuation rule

| Item | Could it change a stop/continue verdict? |
|---|---|
| 1a cross-price zero | No (not used in `decide`) |
| 1b NOII start time | Yes in principle. The imbalance arm is structurally empty, so `underpowered` is forced forever for this design |
| 1c flat arm = 0 | Only the printed incremental; underpowered anyway |
| 2a labels/rounding | No |
| 2b near-flat book | Yes in principle. C2 passes vacuously and C3 cannot hold, so the screen cannot say "continue" for these parameters |
| 2c leading gap in C1 | Yes. It forces C1 = fail, which forces `underpowered` for every variant |

With one session all verdicts are `underpowered` under the signed rule whatever these bugs do, so **no signed verdict is wrong today**. For anything beyond one session, 1b, 2b and 2c would make the rules uninformative. A new signed declaration would be needed to change them. The code fixes 1a and the label fixes in 2a do not touch the signed criteria. Note that `python/src/iap` is bound by the signed null v3 prereg, so any patch needs to wait for it or be coordinated.

## Suggested follow-ups (not done)

1. Fix 1a in `rel08c_study.py` and add a regression test.
2. Owner decision on REL-08c: redesign (new pre-declaration) or drop.
3. Check whether the first closing NOII appears at 15:55:00 for other 2018-10+ days (a one-line check on any day's extract).
4. REL-08: confirm the 2c leading-gap hypothesis from the dataset's `exchange_ts` span, then amend the rule's gap definition; investigate why V1/V2 turn over 50x less than V5/V6.
