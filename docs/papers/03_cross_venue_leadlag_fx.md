# Cross-Venue Lead-Lag Effects in FX: A Carefully Measured Null

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
   are relative to `configs/execution.json`.

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
