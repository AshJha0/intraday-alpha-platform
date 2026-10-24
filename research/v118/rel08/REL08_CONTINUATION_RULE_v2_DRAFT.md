# REL-08 continuation rule v2 (DRAFT, UNSIGNED, 2026-10-20)

> **Superseded 2026-10-24 by `REL08_CONTINUATION_RULE_v2.md` / `rel08_rule_v2.json` (still unsigned).** Kept for the record.

**Status: draft for later signing before any multi-session screen.** Companion data file:
`rel08_rule_v2_DRAFT.json`. The signed v1 (`research/v118/REL08_CONTINUATION_RULE.md`,
`rel08_rule_signed.json`) is untouched. **Rule v1 results stay valid as recorded**: the single-session
REL-08 screen (all 7 variants `underpowered`) is what v1 said, and this amendment re-labels nothing.

## Amendment (only this)

v1 section 2 requires "no gap longer than 30 minutes without a usable rebalance". The v1 code
(`evaluate_variant`, `rel08_screen.py`) computes the gap from `grid[0] - rebalance_ns`, i.e. it counts the
**leading** idle time from the grid origin to the first usable rebalance, and would count gaps across
session boundaries on a multi-day grid. The investigation (`ANOMALY_REL08_INVESTIGATION_2026-10-20.md`, 2c)
found max gaps of 380-405 min, which vary with warm-up and not with mid-session holes, so C1 failed for
every variant by construction.

**v2 definition.** `max_gap_min` is the largest gap between consecutive usable rebalances **inside the same
session day**:

* the leading gap (grid origin to first usable rebalance) is excluded;
* gaps across a session boundary are excluded;
* only the regular-hours grid (09:30-16:00 ET) counts. The 52-rebalance / 780-minute span at 15 min in the
  2019-01-30 run is about twice a 6.5 h session, so the grid evidently includes extended hours (this is a
  reading of the numbers; confirm from the frame `exchange_ts` min/max of `ds_20190130` before signing);
* a day with one usable rebalance contributes no gap; a run with no usable rebalance has `max_gap_min = inf`.

The threshold stays **30 minutes**. No other threshold, criterion or decision-map entry changes.

## Before signing (needs owner decisions)

1. Confirm the regular-hours grid hypothesis (read-only check of `exchange_ts` span).
2. Decide whether the grid itself should be restricted to regular hours in the screen code (a
   `python/src/iap` change, blocked by the null v3 prereg until the study has run), or only the C1 gap.
3. Implement the v2 gap in `rel08_screen.py` with a regression test; sign JSON and code together.
4. Known and **not addressed** here (separate decision): V1/V2 trade about $3 per rebalance on $1M, so C2
   passes vacuously and C3 cannot hold at the registered alpha/notional (investigation 2b).
