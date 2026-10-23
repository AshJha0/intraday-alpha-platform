# REL-08c is DROPPED (owner decision, 2026-10-20)

No redesign. No further REL-08c runs.

## Reasons

1. **The imbalance arm can never trade.** The pre-declaration assumed the closing NOII starts at 15:50. In
   the 2019-01-30 data every one of the 30,010 closing-cross NOII rows has `tod_ns >= 15:55:00.005`; none
   earlier. The registered decision times (15:51, 15:53) and the taker exit (15:54:55) all precede the first
   snapshot, so `level` is NaN, direction 0, n = 0. `min_trades = 30` cannot be met, so the rule can never
   return continue or reject on this feed (investigation 1b; consistent with the October 2018 Nasdaq MOC
   cutoff change).
2. **Redesign has no value.** The only workable variants trade the cross itself (after the cutoff), a
   different question from the pre-declared one.
3. **Printed metrics were also wrong** (investigation 1a, 1c): `gross_cross` 1,614 bps was a bug (zero-price
   cross rows for names with no Nasdaq cross), and the incremental +48.9 bps with CI [-inf, inf] is an
   arithmetic artefact (flat arm scored 0 against a tail-dominated baseline mean; median -3.75 bps).
   Neither affects the verdict: it was `underpowered` regardless (one session).

## Evidence

`ANOMALY_REL08_INVESTIGATION_2026-10-20.md` (sections 1a, 1b, 1c). The cross-price bug fix is prepared in the
same PR as a `python/src/iap` commit, held until the null v3 study has run.
