# Cross-Venue Lead-Lag Effects in FX: A Carefully Measured Null

> Dated record. The figures below are those of the dataset in force when the paper was written; the 2026-10-03 update restates them on the v1.4.0 dataset and re-checks each conclusion, and the 2026-10-04 update at the end restates them under the v1.5.0 default research methods (same dataset) and re-checks each conclusion again.

*Intraday Alpha Platform research series, paper 3 of 6 (spec §28). Generated 2026-08-29 from the repository's committed research artifacts.*

---

## Abstract

We search for exploitable cross-venue lead-lag structure on the platform's
synthetic three-venue FX dataset using flagship alphas FX03 (multi-venue
order-flow imbalance, 1 m horizon) and FX04 (cross-venue lead-lag, 30 s
horizon), with FX12 (venue toxicity) as a related diagnostic. The result is
a null, and we report it as one. FX03 is rejected: pooled out-of-sample IC
0.0060 with Newey-West t = 0.62 and an unconfirmed hypothesis sign. FX04
earns a marginal ITERATE on the letter of the gates (IC 0.0074, t = 1.71,
4/4 positive folds) but fails every deeper probe: its IC turns *negative*
(-0.0095) under a single event of execution lag, its high-volatility IC is
indistinguishable from zero (+0.0038), and with 1,224 experiments in the
ledger a t of 1.71 is far below the ~3.77 expected maximum |t| under the
global null — pure selection can explain it. Venue-feature diagnostics
computed from the committed feature frames explain *why* the effect is
unidentifiable here: the median cross-venue staleness spread is ~81 s
against a 30 s prediction horizon, and in the median 10 s window a single
venue accounts for 100% of quote updates. Both alphas are also severely
cost-negative (≈ -636k and -677k at 1x costs). We additionally note the
structural reason the true effect is near zero: the generator drives all
venues of a pair from one shared mid. All data is synthetic.

---

## 1. Introduction

Fragmented FX trading motivates a classic question: does one venue's quote
stream lead the others, and can the lag be traded? The platform poses it
twice (spec §12): FX03 aggregates order-flow imbalance across the
consolidated multi-LP book on the theory that broad, multi-venue imbalance
is more informative than any single venue's noise; FX04 looks for moments
when venues disagree and bets that the consolidated imbalance will be
validated as laggards converge. FX12 (venue-specific liquidity/toxicity)
probes an adjacent hypothesis — that *which* venue is quoting predicts
short-horizon direction quality.

Honest nulls are a stated deliverable of this research series
(spec §32: "optimize for research truth, not backtest cosmetics"). This
paper documents one, together with the diagnostics that make the null
interpretable rather than merely disappointing.

## 2. Data

**All data is synthetic** (`python/src/iap/marketdata/generator.py`, pinned
seed): two days (2026-08-24/25) of QUOTE + TRADE streams for 8 synthetic
G10 pairs (ids 101-108) on three venues (LP1, LP2, PRI). Each pair's
venues share one regime-switching mid; venues differ in quoted spread and
in receive-timestamp latency. QC (`data/normalized/qc_report.json`):
49,668 / 49,557 FX events per day within 310,159 normalized events, 46
streams monitored.

This design has an immediate consequence for lead-lag research, which we
state up front: **in event (exchange) time, no venue truly leads** — the
common mid is the driver and venue quotes are noisy samplings of it. Venue
latency asymmetries exist only in `receive_ts`, while the feature engine and
labels operate in `exchange_ts` (the platform's causality convention). A
correct pipeline should therefore find approximately nothing, and the value
of this study is that it does, without the machinery inventing structure
that is not there.

## 3. Methodology

Validation protocol as in papers 1-2 (spec §13): 4 expanding purged and
embargoed walk-forward folds, pooled OOS IC / Rank IC / Newey-West t,
automated shift-by-one leakage test, cost stress {0.5, 1, 2}x, latency
stress {+0, +1, +5} events, volatility-regime split
(`python/src/iap/validation/stress.py`).

Signal definitions (`python/src/iap/alpha/fx.py`):

- **FX03** (`fx_multivenue_ofi`): pinned 0.6/0.4 blend of depth-normalized
  L1 and L5 OFI over 30 s on the merged book
  (`ofi_norm_l1_w30s_v1`, `ofi_norm_l5_w30s_v1`), 1 m horizon.
- **FX04** (`fx_cross_venue_leadlag`):
  `imbalance_l1_v1 * venue_imbalance_divergence_v1` — consolidated L1
  imbalance amplified by the unsigned max-minus-min of per-venue L1
  imbalances. The class docstring states the data limitation honestly: the
  feature frames carry no per-venue mid series, so the leader's *direction*
  is proxied by the consolidated imbalance rather than identified per venue.
- **FX12** (`fx_venue_toxicity`): venue-concentration/toxicity proxy,
  1 m horizon (diagnostic only in this paper).

Venue features are defined in `python/src/iap/features/venue.py`: active
venue count, per-venue depth HHI, 10 s update-share HHI and top share,
staleness min/max/spread in ms, cross-venue imbalance divergence, and
best-depth ownership shares — all iterated in sorted venue order for
determinism.

## 4. Results

### 4.1 Headline statistics

From `research/alpha_reports/REPORT.md` and per-alpha JSONs:

| alpha | horizon | OOS IC | Rank IC | NW t | hit | folds+ | hyp | leak | net P&L (1x) | flips/h | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| FX03 | 1m | 0.0060 | -0.0209 | 0.62 | 0.492 | 3/4 | no | pass | -635,830 | 74 | REJECT |
| FX04 | 30s | 0.0074 | 0.0206 | 1.71 | 0.517 | 4/4 | yes | pass | -676,524 | 63 | ITERATE |
| FX12 | 1m | 0.0046 | 0.0155 | 0.09 | 0.504 | 3/4 | yes | pass | -666,864 | 52 | REJECT |

FX04's per-fold ICs are +0.0040, +0.0047, +0.0153, +0.0076 (`FX04.json`) —
consistently positive but consistently tiny. Rank IC disagrees with IC in
sign for FX03 (-0.0209 vs +0.0060), a familiar signature of a
non-signal being pushed around by tails.

### 4.2 Decay: no horizon works

Last-fold OOS IC by horizon (REPORT.md):

| alpha | 50ms | 100ms | 500ms | 1s | 5s | 10s | 30s | 1m | 5m | 15m |
|---|---|---|---|---|---|---|---|---|---|---|
| FX03 | -0.003 | +0.004 | -0.000 | +0.009 | -0.003 | -0.003 | -0.013 | +0.000 | +0.003 | -0.002 |
| FX04 | -0.004 | +0.013 | +0.020 | +0.006 | +0.015 | +0.025 | +0.021 | +0.013 | +0.007 | -0.003 |

FX03's decay curve oscillates around zero at every horizon — there is no
window at which multi-venue OFI predicts the consolidated mid here. FX04
shows a low, broad ridge (≈ +0.02 at 500 ms-30 s) that never reaches even
half the PROMOTE IC gate.

### 4.3 The latency probe kills the survivor

Latency stress (`FX04.json`, `stress.latency`):

| shift | FX04 IC | FX03 IC |
|---|---|---|
| +0 events | +0.0076 | +0.0036 |
| +1 event | **-0.0095** | +0.0015 |
| +5 events | -0.0036 | +0.0067 |

A genuine lead-lag effect at 30 s should comfortably survive one event of
lag (median FX inter-emission gap ≈ 15 s, `data/features/features_101.parquet`).
FX04's IC instead flips sign. A signal that cannot be traded one event late
is, at this horizon and sampling rate, indistinguishable from noise aligned
by luck.

### 4.4 Regime split

| alpha | high-vol IC | low-vol IC |
|---|---|---|
| FX03 | +0.0118 | -0.0059 |
| FX04 | +0.0038 | +0.0129 |
| FX12 | +0.0137 | -0.0108 |

FX04's residual IC lives in the *low*-volatility regime — the opposite of
most genuine microstructure effects on this dataset (compare FX08/FX09 in
paper 2), and consistent with a small mechanical artifact rather than
information transmission.

### 4.5 Venue-feature diagnostics: why identification fails

Descriptive statistics pooled across all 8 FX pairs, computed directly from
the committed feature frames (`data/features/features_10{1..8}.parquet`,
columns from `python/src/iap/features/venue.py`; n ≈ 52.6k emissions):

| feature | mean | median | p90 |
|---|---|---|---|
| `venue_count_active_v1` | 3.00 | 3 | 3 |
| `venue_imbalance_divergence_v1` | 0.76 | 0.74 | 1.29 |
| `venue_staleness_spread_ms_v1` | 99,436 | 81,014 | 198,053 |
| `venue_depth_hhi_v1` | 0.37 | 0.36 | 0.43 |
| `venue_update_share_top_w10s_v1` | 0.90 | 1.00 | 1.00 |

Two numbers decide the paper. First, the **median staleness spread between
the freshest and stalest venue is ~81 seconds** — nearly 3x FX04's entire
30 s prediction horizon. By the time a "lagging" venue updates, the label
window is over. Second, the **median 10 s window sees 100% of updates from
a single venue** (`venue_update_share_top_w10s_v1` median 1.0): at this
quote sparsity there is usually no second venue moving within any window
that could confirm or deny a lead. Cross-venue imbalance divergence is
meanwhile large and persistent (median 0.74 on a [-2, 2]-range construct) —
venues *disagree all the time* as a static property of their spread noise,
not as transient lead-lag events. The alpha's conditioning variable
therefore measures a stationary artifact, and the measured near-null is
exactly what these diagnostics predict.

### 4.6 Multiple testing and costs

The ledger records 1,224 experiments (REPORT.md): Bonferroni per-test |t|
threshold 4.10; expected max |t| under the global null ≈ 3.77. FX04's
t = 1.71 is not even within sight of either bar — its ITERATE verdict is a
mechanical consequence of the lenient iterate gate (t ≥ 1.5), and this
paper's reading is that it is selection noise.

Costs remove any residual ambiguity: at 1x, FX03 nets -635,830 and FX04
-676,524 (x0.5: -327,480 / -319,942; day-2 backtests: -1,651,005 and
-1,814,903 with costs of 1.63M / 1.83M — REPORT.md). There is no cost
regime in which these signals trade.

### 4.7 The lead-lag family as a whole

Two further flagship alphas test lead-lag hypotheses on this dataset and
complete the picture (REPORT.md; `python/src/iap/alpha/fx.py`,
`python/src/iap/alpha/equity.py`):

| alpha | hypothesis | OOS IC | NW t | verdict |
|---|---|---|---|---|
| FX07 | futures-to-spot lead-lag (deepest pair proxies the absent futures leg — limitation stated in the class docstring) | -0.0003 | 0.16 | REJECT |
| EQ10 | index-constituent lead-lag (ETF leads single names) | 0.0036 | 1.28 | REJECT |

Every lead-lag construct in the 24-alpha book — cross-venue (FX04),
multi-venue flow (FX03), futures-spot proxy (FX07) and index-constituent
(EQ10) — lands within noise of zero on this generator. The family-wide
consistency of the null is itself informative: four differently-built
signals agreeing on "nothing here" is much stronger evidence about the
dataset than any single rejection, and matches the shared-mid /
independent-instrument construction of the generator exactly.

### 4.8 Capacity is not the constraint

For completeness: per-instrument capacity estimates for FX03/FX04 run
$122M-271M per pair ($29.5B for the deepest instrument 103;
`research/alpha_reports/FX04.json`, `capacity_usd_by_instrument`). As
throughout this series, the binding constraints are signal and cost, not
capacity.

## 5. Limitations

1. **The null is partly by construction.** The generator shares one mid per
   pair across venues; true event-time lead-lag is designed to be ≈ 0. This
   study validates the *pipeline's refusal to hallucinate* structure; it
   says nothing about lead-lag in real fragmented FX, where inventory,
   last-look and geographic latency create genuine leaders. The proper
   next experiment — a generator mode with an explicit leading venue and a
   calibrated lag — is future work.
2. **No per-venue mid features.** FX04's docstring concedes the leader's
   direction is proxied by consolidated imbalance. A per-venue mid/return
   feature family would make the test sharper; the venue family currently
   exposes staleness and share diagnostics but not signed per-venue moves.
3. **Sparse quotes, two days.** ≈ 6.6k emissions per pair; sub-minute
   lead-lag has limited resolution when the median venue refresh gap is
   tens of seconds.
4. **Synthetic cost model** as in papers 1-2; conclusions about tradability
   are relative to `configs/execution/execution.json`.

## 6. Conclusions

On this dataset, cross-venue lead-lag in FX is a null result, and the
platform's diagnostics make it an *informative* null: the effect is absent
where the data-generating process implies it should be absent, the one
alpha that limps past the lenient gate (FX04) fails latency, regime and
multiple-testing scrutiny, and the venue-feature statistics quantify the
identification problem (81 s median venue staleness spread against a 30 s
horizon; single-venue update dominance in the median window). FX03, FX04
and FX12 are all deeply cost-negative besides.

The methodological conclusion is the one worth exporting to real data: a
lead-lag study should publish its venue staleness and update-share
diagnostics *next to* its IC table, because those two numbers alone
determine whether the experiment could have detected the effect it claims
to test.

## Artifact provenance

| claim | artifact |
|---|---|
| headline stats, decay, regime, costs, day-2 backtests | `research/alpha_reports/REPORT.md` |
| FX03/FX04/FX12 folds, stress, leakage | `research/alpha_reports/FX03.json`, `FX04.json`, `FX12.json` |
| signal definitions incl. stated data limitations | `python/src/iap/alpha/fx.py` |
| venue feature definitions | `python/src/iap/features/venue.py` |
| venue diagnostics table (§4.5) | computed from `data/features/features_101..108.parquet` (committed artifacts; pooled finite values) |
| shared-mid generator design | `python/src/iap/marketdata/generator.py` |
| QC volumes | `data/normalized/qc_report.json` |
| stress axes | `python/src/iap/validation/stress.py` |

## Erratum / Update — 2026-09-06 (round-3 trading fixes: FX P&L currency)

- The net-P&L column of the §4.1 table and the cost-stress numbers in §4.4
  ("FX03 nets -635,830 and FX04 -676,524 …; day-2 backtests -1,651,005 and
  -1,814,903 with costs of 1.63M / 1.83M") came from a backtester that
  summed quote-currency P&L across pairs as if USD (JPY-dominated). The
  Python backtester now converts every P&L increment to USD at the
  prevailing conversion-pair mid before aggregating
  (`PLATFORM_CONVENTIONS.md` §11.6, `API_PORTFOLIO_TCA.md` §4).
- Re-derived (USD, `research/alpha_reports/REPORT.md`, 2026-09-06): FX03
  nets **-30,572** at 1x (x0.5: -16,012; x2: -59,693), FX04 **-35,488**
  (x0.5: -18,397; x2: -69,671); day-2 backtests **-77,960** and
  **-87,143** with costs of 76,182 / 87,125 USD. The statistical columns
  (IC, t, hit, leakage, flips/h) are currency-free and unchanged; both
  verdicts (FX03 REJECT, FX04 ITERATE-by-lenient-gate) and the §6
  conclusion — there is no cost regime in which these signals trade —
  stand.


## Erratum / Update — 2026-09-06 (round-3 research fixes)

1. **Crossed consolidated books.** ~29 % of the FX rows this paper studies
   have a CROSSED merged book (a stale LP quote inside the aggregation
   window). Cross-venue lead-lag measured on those rows is partly measuring
   the aggregation artefact, not information flow between venues. Reported
   split (current run): FX03 pooled -0.0065 /
   uncrossed 0.0108 /
   crossed -0.0284; FX04 pooled
   0.0100 / uncrossed 0.0297 /
   crossed -0.0075 (crossed fraction
   0.287). The promotion gates read the uncrossed
   numbers; FX04's verdict on the current run is ITERATE.
2. **FX05 is a triangle, not an eight-pair cross-section.** With USD pinned,
   AUD, CAD, CHF, JPY and NZD each appear in exactly ONE pair of this
   universe, so their factor absorbs that pair's whole return and the
   residual is 0 by construction. Those five pairs now score NaN (confidence
   0) instead of a constant, and only EUR/USD-GBP/USD-EUR/GBP carries
   cross-pair information (API_ALPHA §5). FX05's current IC is
   -0.0376 pooled / -0.0065
   uncrossed, verdict REJECT.
3. **Labels, folds and execution** were fixed as described in paper 01's
   erratum (items 1, 4, 6); every IC, t-statistic and P&L figure above is
   superseded by the current `research/alpha_reports/REPORT.md`.

The multiple-testing ledger was **reset and re-derived** for this round: an
experiment is now identified by (alpha, kind, canonical configuration) and
re-running a script no longer increases the count, and one adaptive
deployment counts as ONE experiment instead of its 211 monitoring
evaluations. Every "1,224 experiments" / "19,347 experiments" figure in the
body above is superseded by **760 experiments
(65 distinct configurations)**: Bonferroni
per-test |t| **3.99**, expected max |t| under
the global null **3.64**. Reports now read the
ledger at render time, so a report and the ledger can never disagree again.

## Erratum / Update — 2026-09-20 (ledger denominator)

The multiple-testing ledger `research/experiments.json` moved on 2026-09-19
when the contract-driven `ExperimentRunner` (`python -m iap.research run`,
`research/experiments/<id>/`) registered five new experiments — EQ01 @ 1 s,
EQ03 @ 1 s and @ 5 s, EQ06 @ 1 s and @ 10 s, 21 looks each, kind
`experiment_runner`, deliberately **not** de-duplicated against the
`promotion_pipeline` entries even where the computation coincides, so the
denominator only grows. The 2026-09-06 erratum's **760 looks over 65 distinct
configurations (Bonferroni per-test |t| 3.99, expected max |t| under the global
null 3.64)** are superseded by **865 looks over 70 distinct configurations:
Bonferroni per-test |t| ≥ 4.02 (p ≤ 5.78e-05), expected max |t| ≈ 3.68**.
No verdict, IC, t-statistic or P&L figure in this paper changes; the five
new experiments are all ITERATE and net-negative at 1× costs, like every
alpha before them. The analysis above is left as written and the research
reports quote the ledger at their own render time (`ledger_n_at_report`).

## Erratum / Update — 2026-10-03 (v1.4.0: the equity flow now reaches the close)

**What changed in the data.** Up to v1.3.0 the generator stopped the equity
continuous flow about 40 % of the way through each session; v1.4.0
calibrates the flow rate to the session, so equities trade to the close
(paper 01's update of the same date has the detail). The FX raw and
normalized files are byte-identical to v1.3.0. The FX statistics in this
paper therefore did not move; the ledger denominator did, and so did EQ10,
the one equity alpha in §4.7. This section supersedes the "current" figures
quoted in the earlier errata, which stay as dated records.

**FX figures: unchanged by this release, restated.** Every field of
`FX03.json`, `FX04.json`, `FX05.json`, `FX07.json` and `FX12.json` that
existed in v1.3.0 has the same value now. Current values
(`research/alpha_reports/REPORT.md`):

| alpha | horizon | IC | IC unc | IC crs | crs% | NW t unc | folds+ | hyp | net P&L 1x | flips/h | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| FX03 | 1m | -0.0065 | 0.0108 | -0.0284 | 0.291 | 1.86 | 1/4 | no | -27,063 | 74 | ITERATE |
| FX04 | 30s | 0.0100 | 0.0297 | -0.0075 | 0.287 | 4.79 | 4/4 | yes | -32,566 | 63 | ITERATE |
| FX12 | 1m | 0.0036 | 0.0083 | -0.0002 | 0.291 | 1.22 | 2/4 | yes | -29,666 | 52 | REJECT |
| FX07 | 1m | -0.0053 | -0.0121 | -0.0010 | 0.295 | 0.22 | 2/4 | no | -9,757 | 35 | REJECT |
| FX05 | 5m | -0.0376 | -0.0065 | -0.0681 | 0.330 | 0.53 | 1/4 | no | -3,980 | 11 | REJECT |

Note that FX03 is an ITERATE, not a REJECT: its uncrossed IC and t clear the
lenient gate (IC >= 0.005, t >= 1.5). It cannot be promoted — its fitted
sign contradicts its rationale and one fold of four is positive. That was
already the case on v1.3.0; the "FX03 REJECT" of the 2026-09-06 currency
erratum predates the gates that read the uncrossed IC.

Cost stress at 0.5x / 1x / 2x: FX03 -13,843 / -27,063 / -53,503; FX04
-16,725 / -32,566 / -64,250. Day 2: FX03 net -69,720 (costs 68,618); FX04
net -79,775 (costs 80,445). Latency probe, last-fold IC at +0 / +1 / +5
rows: FX04 +0.0071 / -0.0102 / -0.0047; FX03 -0.0035 / +0.0019 / -0.0106.
Regime split: FX04 +0.0052 high-vol / +0.0124 low-vol. Capacity $122-271M
per pair, $29.5B for instrument 103. The venue diagnostics of §4.5 were
recomputed from the current feature frames and are the same (52,568 rows;
median staleness spread 81,014 ms; median top-venue update share 1.00).

**Ledger.** At v1.4.0 `research/experiments.json` held 1920 looks over 139
entries (the 1068 looks of the v1.3.0 dataset are kept; the regenerated
pipelines added 852); it holds 5156 over 216 since v1.5.0 (see the
2026-10-04 update). At the v1.4.0 count the expected max |t| under the
global null was 3.888 and the Bonferroni per-test threshold 4.206. FX04's t of 4.79 is above both.
FX03 (1.86), FX12 (1.22), FX07 (0.22) and FX05 (0.53) are below the
yardstick, as are all other FX alphas.

**EQ10 (§4.7), moved.** Index-constituent lead-lag on equities: uncrossed IC
0.0041 at Newey-West t 0.78, 3 of 4 folds positive, hypothesis sign
confirmed, -29,541 at 1x; REJECT (v1.3.0: -0.0034 / -0.58).

**Conclusions, re-checked.**

1. *Cross-venue lead-lag in FX has no economic content on this dataset;
   FX03, FX04 and FX12 are cost-negative at every multiplier.* **Holds**,
   unchanged.
2. *FX04 passes the statistical gates and is held at ITERATE by the cost
   gate; a significant t appears on rows whose true lead-lag is zero, so the
   gates are necessary and not sufficient.* **Holds**, unchanged, and it
   survives the larger denominator: 4.79 against a Bonferroni threshold of
   4.206. In the lifecycle registry the cost gate is the only one FX04
   fails. (The body's reading of FX04 as selection noise at t 1.71 was
   already superseded by the round-3 erratum.)
3. *FX04's IC changes sign under one row of lag and is larger in the
   low-volatility regime.* **Holds**, unchanged. These are pooled-row
   last-fold figures.
4. *The venue diagnostics explain why no effect should be identifiable
   (staleness spread of about 81 s against a 30 s horizon; one venue
   supplies all updates in the median 10 s window).* **Holds**, unchanged.
5. *Every lead-lag construct in the book lands within noise of zero, and
   the agreement of four differently built signals makes the null
   informative.* The first part **holds, weaker**: three of the four are
   within noise (FX03 t 1.86, FX07 0.22, EQ10 0.78), and FX04 is not (item
   2). The second part **no longer holds for the equity leg**. The
   planted-signal study (`research/power/POWER_REPORT.md`) plants an ETF
   lead-lag of known size in data generated with the v1.4.0 flow and scores
   it with EQ10: it is detected in 0 of 3 seeds at every level, including
   twice the reference size (mean t 0.88). On v1.3.0 it was detected in 1 of
   3 seeds at that level. A test that does not find a planted effect cannot
   make its null informative, so EQ10's REJECT says little about whether
   index-constituent lead-lag is present. The planted lead is two
   one-second steps and equity rows are now about 3.2 s apart, which is the
   likely reason; that was not tested. No such study exists for the FX
   alphas.
6. *Capacity is not the constraint.* **Holds**, unchanged.
7. *A lead-lag study should publish its staleness and update-share
   diagnostics next to its IC table.* **Holds**, and item 5 adds the same
   point for power: publish whether the test can detect a planted effect.

## Erratum / Update — 2026-10-04 (v1.5.0: the corrected research methods are the defaults)

**What changed.** Not the data: the dataset is the v1.4.0 one. The eleven
corrected research methods that v1.3.0 added and v1.4.0 kept as selectable
alternatives are the defaults since v1.5.0 (`iap.validation.methods`, bundle
`"v2"`; PLATFORM_CONVENTIONS.md §13.6; paper 01's update of the same date
lists them), and every old rule stays selectable under a legacy name
(bundle `"legacy_v1"`). For this paper three matter most. The gate t is the
HAC t of the pooled slope, where it was the Newey-West t of within-bucket
ICs. The PROMOTE t threshold is the Bonferroni |t| at the run's recorded
gate look count, 4.365 at 3,936 looks, where it was 3.0. The backtest takes
a position only when the expected return exceeds the round-trip spread and
fee, where it held one on every signal sign. `run_all.py --methods
legacy_v1 --out-dir <dir>` reproduces the v1.4.0 report
(`python/tests/test_legacy_methods.py` pins that against the v1.4.0 reports
of EQ03 and FX01). The figures of the 2026-10-03 update are the `legacy_v1`
figures of the current dataset; this section supersedes them as the current
ones.

**FX figures, current** (`research/alpha_reports/REPORT.md` and the
per-alpha JSONs; keys `gate_ic`, `gate_tstat`, `nw_tstat_uncrossed`,
`trade_count_1x_cost`, `net_pnl_1x_cost`):

| alpha | horizon | IC | gate IC | IC crs | crs% | gate t | t within-bucket | folds+ | hyp | trades 1x | net P&L 1x | flips/h | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| FX03 | 1m | -0.0062 | 0.0112 | -0.0283 | 0.291 | 1.13 | 1.95 | 1/4 | no | 0 | 0 | 74 | REJECT |
| FX04 | 30s | 0.0097 | 0.0298 | -0.0082 | 0.287 | 4.24 | 4.82 | 4/4 | yes | 0 | 0 | 63 | ITERATE |
| FX12 | 1m | 0.0036 | 0.0086 | -0.0006 | 0.291 | 1.05 | 1.29 | 2/4 | yes | 0 | 0 | 52 | REJECT |
| FX07 | 1m | -0.0052 | -0.0119 | -0.0011 | 0.296 | -1.37 | 0.25 | 2/4 | no | 0 | 0 | 35 | REJECT |
| FX05 | 5m | -0.0396 | -0.0072 | -0.0712 | 0.331 | -0.42 | 0.35 | 0/4 | no | 137 | -64 | 11 | REJECT |

FX03 moved from ITERATE to REJECT: its gate t of 1.13 is below the ITERATE
floor of 1.5 that its within-bucket t (1.95) cleared. FX04 stays ITERATE
and now fails two PROMOTE gates, `significance` and `cost`
(`promote_gates`): 4.24 is below the threshold of 4.365. Headline ICs the
gate does not read, FX04: vol-scaled 0.0323, per-instrument mean 0.0317;
valid-only gate IC 0.0297 (the v1.4.0 value), 53 BLACKOUT rows scored at
the reopen return.

Cost stress at 0.5x / 1x / 2x: FX03, FX04, FX07 and FX12 make no trade at
any multiplier; FX05 -151 / -64 / 0 USD over 511 / 137 / 0 trades. Day 2:
no trade for any of the five. Latency probe, last-fold IC at +0 / +1 / +5
rows: FX04 +0.0076 / -0.0098 / -0.0040; FX03 -0.0025 / +0.0027 / -0.0107.
Regime split: FX04 +0.0055 high-vol / +0.0129 low-vol. Edge-breakeven
capacity is 0 for all five; the participation line is unchanged ($122-271M
per pair, $29.5B for instrument 103, `capacity_proxy_usd_by_instrument`).
The venue diagnostics of §4.5 derive from the feature frames, which this
release did not change; they were not recomputed here.

**Ledger.** `research/experiments.json` holds 5156 looks over 216 entries
(1068 on the v1.3.0 dataset, 852 on the v1.4.0 dataset under the legacy
methods, 3,236 under the default methods). Expected max |t| under the
global null is 4.135 and the Bonferroni per-test threshold 4.424; the alpha
report was judged at its own recorded count of 3,936 (threshold 4.365,
selection yardstick 4.07). FX04's gate t of 4.24 is above both yardsticks
and below both thresholds. FX03, FX12, FX07 and FX05 are below all four.

**EQ10 (§4.7), current.** Gate IC 0.0040 at gate t 0.91 (within-bucket
0.77), 3 of 4 folds positive, hypothesis sign confirmed, no trade; REJECT.

**Conclusions, re-checked under the default methods.**

1. *Cross-venue lead-lag in FX has no economic content on this dataset;
   FX03, FX04 and FX12 are cost-negative at every multiplier.* The first
   half **holds**. The second **does not carry over as worded**: none of
   the three makes a trade at any cost multiplier, so none shows a loss.
   Their forecasts never exceed one round trip of spread and fee, which is
   the same finding without the six-figure number. The v1.4.0 losses
   (FX04 -32,566 at 1x) are those of the legacy `"sign"` policy.
2. *FX04 passes the statistical gates and is held at ITERATE by the cost
   gate; a significant t appears on rows whose true lead-lag is zero, so
   the gates are necessary and not sufficient.* **Weaker.** Under the
   default methods FX04 does not pass the significance gate: 4.24 against
   4.365. The margin is 0.12, the statistic is above what selection alone
   is expected to produce (4.07), and under the legacy rule it is 4.82
   against 3.0. So the corrected gate stops this false positive, narrowly,
   and the legacy gate did not. The diagnostics of §4.5 remain what
   explains why the effect is not there; the paper's lesson stands as a
   statement about a fixed threshold of 3.0. In the lifecycle registry FX04
   now fails `statistical_significance`, `net_pnl_after_costs` and
   `capacity`.
3. *FX04's IC changes sign under one row of lag and is larger in the
   low-volatility regime.* **Unchanged.**
4. *The venue diagnostics explain why no effect should be identifiable.*
   **Unchanged**; not recomputed.
5. *Every lead-lag construct lands within noise of zero, and the agreement
   of four differently built signals makes the null informative.* The first
   part is **closer to holding than on 2026-10-03**: FX03 (1.13), FX07
   (-1.37) and EQ10 (0.91) are within noise and FX04 no longer clears the
   significance gate, though it is above the selection yardstick. The
   second part **still does not hold for the equity leg**. The
   planted-signal study (`research/power/POWER_REPORT.md`, report version
   2, default methods) detects a planted ETF lead-lag in 0 of 3 seeds at
   every level by the pooled-slope t, the within-bucket t and the ledger
   threshold alike; at twice the reference size the mean pooled-slope t is
   1.75 and the mean within-bucket t 0.87. EQ10 reaches ITERATE in 2 of 3
   seeds at that level and in 1 of 3 at the reference size.
6. *Capacity is not the constraint.* **No longer holds as stated.** By the
   participation line it is unchanged; by the default edge-breakeven
   measure the capacity of every alpha in this paper is 0.
7. *A lead-lag study should publish its staleness and update-share
   diagnostics next to its IC table.* **Holds.**
