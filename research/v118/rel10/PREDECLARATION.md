# REL-10 volume-clock OFI and hidden-print imbalance: pre-declaration

**Status: parameters SET 2026-10-18 (by Claude for the coordinator, under the owner's delegation of
signing to the coordinator). NOT SIGNED, NOT RUN.** The machine copy is
`research/v118/rel10/predeclaration.json`; the runner `research/v118/rel10/run_rel10.py` binds it
(`bind`, `bind --dry-run`) and refuses to run unless the prereg `research/preregs/rel10_prereg.json`
is signed and matches this checkout. If this text and the JSON disagree, the JSON is what is bound
and the disagreement is a defect to fix BEFORE signing. No real-data read happens before the
signature; every read is one ledger look; an edit after the first look voids the signature. Study
code: `python/src/iap/research/volume_clock_ofi.py`.

**Registry step before the run (coordinator).** Every session below is `spent` for AAPL / MSFT in
`research/holdout_registry.json`. The guard allows a read only when this prereg's id
(`research/preregs/rel10_prereg.json`) is listed in `spent_by` for each session and symbol. Until
then `run` refuses. That refusal is the intended behaviour.

## 1. Hypothesis (alpha review hypothesis 5)

Signed flow, order-flow imbalance (OFI) and hidden-print imbalance over the trailing 1 or 5 minutes,
on volume-clock rows, carry the sign of the next 1 or 5 minute mid move net of the spread. Treated as
an **execution input first** (a maker's side choice, a taker's timing). The taker use is the least
plausible (alpha review); taker and maker are evaluated separately, and neither rescues the other.

## 2. Data

| parameter | value | reason |
|---|---|---|
| sessions | the six Nasdaq/IEX overlap days with both sides registered (as REL-09) | the merged stream carries both venues' hidden prints (REL-04) |
| **calibrate** | 2025-11-28, 2025-12-08 (G = 2) | bucket size and thresholds only: rows and signals, no label, fill or P&L |
| **evaluate** | 2025-12-09, 2025-12-10, 2026-05-15, 2026-05-18 (G = 4) | no day used for both; the decision rests on these alone |
| prior use | these days were read by B8 / B8 v2 for a router question | a new question on spent days; no confirmation claim |
| symbols | AAPL, MSFT | the names registered on these days (QQQ left out for memory and runtime) |
| venues | XNAS and IEXG merged, `iex_clock = exchange`, consolidated top of book | one tape for flow, OFI and hidden prints across both venues |
| trade signs | EXECUTE: the book's resting side; TRADE (hidden) print: `side` read as the resting side | the ITCH `P` and IEX trade-report convention used by the readers; a wrong convention flips `hid_imb` only and is visible in the calibrate rows before the evaluate days are read |

## 3. Signals, bucket and target

| parameter | value | reason |
|---|---|---|
| signals | `ofi`, `flow`, `hid_imb` | the three named in REL-10; persistence `corr(ofi_prev, ofi)` is descriptive only |
| horizons | 1 and 5 minutes, window = horizon | the two ends of the hypothesis range; tying the window to the horizon keeps K small |
| bucket `n_shares` | per symbol: calibrate traded shares (both venues, displayed and hidden) / (2 x 390), rounded to 100, at least 100 | about one row per minute on average, so 1-5 minute windows hold several rows |
| thresholds | per symbol, signal and window: the 0.8 quantile of the absolute signal over the calibrate rows | a fixed rule computed on days the decision never reads |
| **registered target** | the spread-netted label `sign(signal) * gross_h - cost_h` (bps); the decision unit is the trip P&L in USD | the target the REL-10 item names, taken through to dollars |

## 4. Execution (priced, not assumed)

| parameter | value | reason |
|---|---|---|
| size | 100 shares | one round lot, for taker and maker alike |
| taker | touch entry capped at displayed depth, exit at the far touch after h, no overlapping trips | partial fills and their missed part are visible |
| maker | passive entry behind the displayed queue for 30 s, passive exit behind the far-touch queue for 30 s, remainder crossed | entry and exit windows short against the 1-5 m horizons |
| fees | taker USD 0.003 per share, maker rebate USD 0.002 (XNAS list prices) | `research/execution/b8/venues_xnas_iexg_v2.json`; the conservative venue for both legs |
| missed opportunity | unfilled quantity times the gross move, reported per cell beside P&L, never inside it | the cost of not trading is shown, not hidden |

## 5. Metrics and decision rule

* Unit: per-session net USD per cell (use x signal x horizon x symbol). Every evaluate session counts
  (no trips = 0).
* **Per cell**: candidate if the family-adjusted interval (level 1 - 0.05/K, Student-t, 3 df) lies
  above 0; reject if it lies below 0, or if it contains 0 while the family MDE is at most
  **USD 20 per session** (economic threshold: about 0.2 bp on a day's taker turnover at this size);
  otherwise underpowered.
* **Per use** (taker, maker separately): candidate if some (signal, horizon) is a candidate on both
  symbols; iterate if some cell is a candidate; underpowered if every cell is; otherwise reject. A
  positive rank IC or gross label is not by itself a continuation reason. No promotion in v1.18.

## 6. Family, MDE and small-G statements

* **Family K = 24** (2 uses x 3 signals x 2 horizons x 2 symbols). Bonferroni level 1 - 0.05/24.
* **MDE** at G = 4 in units of a cell's per-session sd: 1.40 sd (normal), 2.01 sd (Student-t, 3 df),
  **5.53 sd** after the family correction. "Underpowered" is the expected verdict for most cells; the
  output states the MDE at the observed sd and G for each one. Rows inside a session overlap; the
  session is the cluster and no row-count standard error is used.
* **Small-G**: inference rests on G = 4 session clusters (3 df). At G <= 5 the result is a size
  statement, not a power statement. No generalisation beyond these sessions is claimed.

## 7. Not claimed

Reconstructed passive fills (back of the displayed queue, no cancel-ahead credit, Level-1 fill model
only, F8). Hidden prints are seen only after they print. No latency in the taker path (exchange
timestamps). Fees are list prices. The days are spent for other questions.
