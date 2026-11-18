# DRAFT pre-registration: one look at the sealed breadth holdout on bars

**DRAFT. Not signed, not registered.** Machine-readable form:
[breadth_bars_holdout_DRAFT.json](breadth_bars_holdout_DRAFT.json). Every `<FILL ...>` field there
must be filled before binding, and the runner refuses a document that still carries one. How to go
from this draft to a signed, executed look, and the rule conflicts that need the owner first:
[HOLDOUT_LOOK_PROCEDURE.md](../../round1/HOLDOUT_LOOK_PROCEDURE.md).

## The plain idea

Round 1 built a light study path: one-minute bars for every Nasdaq symbol of an ITCH day, and a
decile book over a 500-name universe, net of the quoted spread. One family will be chosen in-sample
by a rule written beforehand (`research/round1/FAMILIES_PRESPEC_2026-11-18.md`). This document
registers one run of that family on the nine sealed 2025/26 sessions, for every universe name except
AAPL, MSFT and QQQ (already spent), with the verdict fixed in advance and computed by code.

## What is fixed here

| item | value |
|---|---|
| sessions | 2025-11-28, 2025-12-08, 2025-12-09, 2025-12-10, 2025-12-11, 2025-12-12, 2026-05-15, 2026-05-18, 2026-06-12 (from `research/holdout_registry.json`) |
| raw files | one Nasdaq ITCH 5.0 file per session; name, size and sha256 in the JSON, copied from `research/r7/r7_confirm_prereg.json` and `research/data_manifest.json`; checked after download |
| universe | `research/round1/universe_20190130.json` (rule `breadth_bars_universe/v1`, 500 names, sha256 `07b32400e244bcae7323b9ccdefb6574b6cb5c27b86b6dff1e90ced2f66b10fb`, LF-normalised) minus AAPL, MSFT, QQQ |
| family and parameters | `<FILL>` from the choice rule; lookback, step, horizon, delay, decile fraction |
| cost | each unit of gross pays its own names' round trip: entry half spread plus exit half spread of the end-of-bar Nasdaq quote. In-sample median round trip 4.29 bps (2019-01-30). No fee, no impact. |
| primary statistic | per session, the mean net bps per rebalance; across the sessions, the one-sided Student t on n - 1 df |
| decision (proposed) | **pass** if all 9 sessions give a value, the mean is above zero and the one-sided p is at most 0.025 (t at least 2.306 on 8 df); **incomplete** if fewer than 9 give a value (the look is spent anyway); **fail** otherwise |
| multiplicity | K = 1: one family, one parameter set, one statistic, one look. The in-sample choice among families is selection; it is disclosed in the choice record. |
| bars tool | `rust/itchbars`, commit and tree hash bound at binding |

## Decision threshold: proposal and reasons

The coordinator confirms these before signing.

- **alpha 0.025 one-sided.** This is the level the power table and the stop rule use. With one family
  registered there is nothing to divide.
- **all nine sessions required.** If a download or bars build fails, the verdict is `incomplete`, not
  a test on eight sessions. Otherwise a failure could choose which sessions count.

## What nine sessions can and cannot show

From `docs/research/PNL_POWER_TABLE.md` and `iap.research.pnl_power.mde_sharpe(9)`: nine sessions
detect, with 80 % power, an annualised net Sharpe of about **17** or more (16.94 at K = 1). They say
nothing about a realistic Sharpe of 1 to 5. A fail or an incomplete is therefore not evidence against
such a strategy. A pass would mean a very large effect, and the first suspect would be the cost model
or the data. The Round 0 stop rule allows a confirmatory registration only for a Sharpe the session
count can detect, so this registration says plainly that it looks for a net Sharpe of 17 or more.

## Open for the owner

1. The nine sessions are reserved for `research/preregs/breadth_book_prereg.json`. Releasing or
   extending that reservation is the owner's decision.
2. `docs/governance/OWNER_SIGNATURE_CHECKLIST.md` (U0 DR-2) says the owner's key is used by the owner
   alone. A session signing on the owner's behalf needs that decision changed first.
3. Confirm the threshold above.
4. Image digest: proposed `--no-image-digest`, because the bars tool runs in WSL, not in the
   reproduction image.
