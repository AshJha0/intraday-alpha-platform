# B8 v2 (REL-04b): pre-declaration, written before any B8 v2 result exists

Date: 2026-10-18. Status: **declared, not run, not signed.** The machine copy is
`predeclaration.json` (same directory); where the two differ, the JSON governs, and both
are fingerprinted by the prereg `research/preregs/b8v2_prereg.json` (REL-02a analysis
identity). No B8 v2 number may be published before that prereg is signed (gate F1).

## What changes from the v1.17.1 oos6 run, and what does not

Unchanged, so the headline stays comparable with `research/execution/b8/REPORT_oos6.md`:
the six days and the split, the four policies, the two symbols, the venue fees, the
router calibration (`research/execution/b8/calibration_train3/`, reused byte for byte and
fingerprinted; the calibrate days are not read again), the grid (TWAP, VWAP, POV, IS x
0.1, 0.5, 1, 2 % venue ADV), windows (30 min, 5 min warm-up), seed 20260829.

Changed (the v1.18 foundation): the REL-03 v2 metrics as the decision statistic,
`impact_model: parent`, `adv_table: session` (each dataset's `adv_by_session`), all 16
cells every session (`cover_all_cells`), size labels in % of consolidated ADV, REL-04
fidelity (`hidden_prints` on; the IEXG 350 microsecond speed bump from
`research/execution/b8/venues_xnas_iexg_v2.json`), a family correction over K = 6.

## Days

- Calibrate (not read by this run): 2025-11-28, 2025-12-08, 2025-12-09.
- Evaluate (headline): 2025-12-10, 2026-05-15, 2026-05-18. Nasdaq `r7_20251210` and
  `holdout2026` (one dataset, both May sessions), IEX `iex/iex_<date>`; each
  `dataset_version` is in the JSON and checked on disk before the run.
- **Supplementary, NOT in the headline: 2025-12-11, 2025-12-12 and 2026-06-12.** On
  2026-10-18 the IEX side of 12-11 and 06-12 is ingested but not yet registered in
  `research/holdout_registry.json`, and 12-12 is not ingested. They are covered by a
  separate supplementary pre-declaration and prereg, written once each day is ingested and
  registered, that reuses this design unchanged. It is reported beside the headline,
  never pooled into it, so the headline stays the same three-day comparison as oos6.

The headline days are SPENT in the holdout registry. This run is a re-measurement of the
same spent days with a better instrument, not a new confirmation; the registry lists this
prereg id in `spent_by` for the three evaluation days only.

## Policies, symbols, run order

`pin_nasdaq` (baseline), `pin_iex`, `sor`, `sor_sweep`; AAPL then MSFT, one process at a
time, with chain B (the IEX ingest) suspended for the duration.

## Statistics

- **Primary:** REL-03 trading cost per FILLED share, bps of the arrival mid
  (`trading_cost_per_filled_share_bps`, positive = worse).
- **Co-primary:** completion charge on the residual (`completion_charge_bps`, the explicit
  price of not finishing) and fill rate (`fill_rate`, quantity-weighted).
- **Secondary:** Perold shortfall per target share; all-in cost per filled share.
- **Contrast:** policy minus `pin_nasdaq` on the same parents (session, symbol, window,
  algo, size), pooled over all 16 cells, per symbol: the difference of
  quantity-weighted pooled ratios with a CR1 cluster-linearised SE, one cluster per
  session, Student-t at G - 1 df (`iap.research.execution_metrics.ratio_diff_stats`;
  per-session sums are written so anyone can recompute it).
- **Family:** K = 6 = three policies (pin_iex, sor, sor_sweep) x two symbols, the same K
  as the signed known-null protocol. The decision interval is family-adjusted:
  two-sided 1 - 0.05/6 (Bonferroni). The 95 % interval is printed beside it.
- **MDE beside every interval:** `mde_t` (95 %) and `mde_t_family` at 80 % power, plus
  n parents, G and the per-session sums.

## Decision rule (per policy and symbol)

- **better:** the family interval of d(trading cost per filled share) lies entirely below
  zero, AND the family interval of d(fill rate) does not lie entirely below zero, AND the
  family interval of d(completion charge) does not lie entirely above zero (a policy that
  is cheaper per filled share because it fills less is not better).
- **worse:** the family interval of d(trading cost per filled share) lies entirely above
  zero, OR that of d(fill rate) lies entirely below zero.
- **no evidence:** anything else, reported with sign and size; marked **underpowered**
  when `mde_t_family` of the primary exceeds 1.0 bps or that of fill rate exceeds 0.02.
- A policy is "better than pinned Nasdaq" only if it is better on BOTH symbols.
- Negative results are stated first. Nothing in this file, the code at the bound commit,
  the data or the calibration changes after a result is seen; a change is a new,
  separately labelled declaration.

## Small-G statement

G = 3 evaluation sessions in 2 regimes (December 2025, May 2026). The interval has 2 df:
t(0.975, 2) = 4.30 and t(1 - 0.025/6, 2) = 10.89, so the family interval is about 2.5
times the 95 % one. The inference rests on 3 clusters; the run reports size, not power.
**Underpowered is the expected verdict** for effects near 1 bps, and that is stated here
before the run.

## Sensitivity (reported beside the headline, never part of the decision)

- `s1_no_hidden`: `hidden_prints` off (fill assumption: no non-displayed prints).
- `s2_child_impact`: `impact_model: child` (impact assumption).
- Completion cost: read from the co-primary `completion_charge_bps` and `residual_qty`; no
  extra run.

## Latency assumption (L0)

Exchange timestamps (`iex_clock: exchange`), venue order-entry legs 0 ns on both venues,
the IEXG 350 microsecond inbound speed bump, the internal decision / risk / wire legs of
`ExecConfig.latency` on both venues, market-data delay 0. **The REL-04 IEX
capture-timestamp re-ingest has not run, so no feed or order-entry latency is measured.**
A re-run with measured latency is a separate declaration and prereg.

## Size labels

Cells keep the venue-ADV keys (comparable with v1); every parent also carries
`size_pct_consolidated_adv` from `consolidated_adv.json`, which the owner fills from a
free public source (value and `_source`) before binding. The bind helper and the runner
refuse while a value is null. The labels do not enter the decision.

## Reproducibility contract (programme section 9.6)

The benchmark's canonical writer (`json.dumps(sort_keys=True, indent=2)` + LF), rows in
its stable order, dependencies from the CI lock (`.venv-ci`) at the bound commit, the
deterministic seed above. Tolerance for the economic result: bps to two decimals (0.01),
fractions to 0.0001, counts exact. "Reproduced" means byte-identical JSON where
achievable, otherwise within that tolerance with every difference listed. Reproduction:
check out the bound commit, run `run_b8v2.py run` against the same dataset versions, then
`run_b8v2.py analyse`; `DECISION.json` carries the rounded statistics.

## Not claimed

Three evaluation days cannot support a general claim. Fees are indicative list prices.
The fill model is validated at Level 1 only (REL-05). Venue latency is assumed, not
measured.
