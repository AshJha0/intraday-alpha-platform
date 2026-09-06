# API_ADAPTIVE — the adaptability contract (drift, refit, lifecycle)

Scope: the adaptability layer (spec §20 steps 12-13 — continuous
monitoring and retirement criteria).  The Python package `iap.adaptive`
(`python/src/iap/adaptive/`) plus `iap.backtest.adaptive` is the
reference.  Any production port that monitors live alphas — in
particular the **Java live-metric port** (`com.iap.adaptive`) — must
implement the SAME PSI formula against the SAME serialized baseline
files and reproduce the applicable parts of
`tests/golden/expected_adaptive.json` for the monitors it implements:

- PSI values: abs tolerance **1e-10** (all ports);
- lifecycle state sequences: **exact states** (all ports);
- KS values (abs 1e-10) and refit-trigger decision sequences (exact
  booleans): required only of ports that implement the KS monitor or
  refit policies. These are research-side by design; the Java port
  implements PSI + rolling IC + lifecycle only.

Normative companions: `PLATFORM_CONVENTIONS.md` §3/§7, `/API_ALPHA.md`
(the scoring semantics that produce the monitored signal),
`configs/strategies.json` `adaptive` block (every pinned threshold),
`python/tools/make_golden_adaptive.py` (golden generation, brute-force
validated before writing).

## 1. Baseline files — `research/baselines/*.json`

One JSON file per monitored distribution, captured from a research
window and consumed unchanged by live monitors.  Distribution baseline
(`"kind": "signal" | "feature"`), `x-version` 2:

```json
{
  "x-version": 2,
  "kind": "signal",
  "name": "signal_eq01",
  "alpha_id": "EQ01",
  "source": "human-readable provenance",
  "feature_version": "<64-hex feature-registry hash>",
  "n": 1935,
  "edges": [9 floats, ascending],
  "expected_frac": [10 floats, summing to 1],
  "mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0,
  "psi_eps": 1e-6,
  "n_buckets": 10
}
```

- `edges` are the 9 interior decile edges of the baseline sample:
  `edges[k-1] = quantile(x, k/10)` for k = 1..9, **linear-interpolation
  quantiles** (numpy default: with sorted x of size n, the q-quantile
  interpolates between the floor/ceil neighbours of index `q*(n-1)`).
- `expected_frac[i]` is the baseline sample's own fraction in bucket i
  (NOT assumed 0.1 — ties can concentrate mass).
- `mean/std/min/max` are diagnostics (std is population, ddof = 0).
- `feature_version` (pinned, round 3) is the feature-registry hash the
  baseline was captured against.  A baseline captured under a different
  registry describes a feature whose **semantics may have changed under the
  same name**, so a PSI or IC computed against it is meaningless rather than
  merely stale.  Every loader MUST therefore reject a baseline whose
  `feature_version` differs from the registry hash of the engine that will
  produce the live values, and MUST reject a baseline that carries no
  `feature_version` at all (fail closed: absence cannot be shown to match).
  A loader MAY offer an explicit opt-out for offline tooling that inspects a
  historic file — Python `expected_feature_version=None`, Java by not
  calling `BaselineLoader.requireFeatureVersion` — but the opt-out must
  never be the default.
- A reader MUST reject a file whose `x-version`, `n_buckets` or
  `psi_eps` differ from the pinned values.

IC baselines use `"kind": "ic"` and carry
`{ic_mean, ic_std, n_buckets_baseline, bucket_ns, horizon, baseline_kind}`
instead of `edges`/`expected_frac` (`ic_std` is the population std of the
research window's bucket ICs); they carry the same `x-version` and
`feature_version` provenance.  `baseline_kind` MUST be `"oos"` — see §4.

When the held-out warmup tail yields fewer than the required IC buckets, no
IC baseline is written at all.  That is not an error, but it MUST be
reported: the rolling-IC gauge is then unavailable for that alpha, so its
drift events come from PSI alone and its lifecycle can never observe decay.
Writing an in-sample baseline instead is forbidden (§4).

### The pinned Java parity file — `research/baselines/signal_eq01.json`

The EQ01 **signal distribution** on the golden EQ frame: replay
`tests/golden/events_eq_mbo.jsonl` (instrument 1, cadence 0 — one
emission per event, exactly as in `/API_ALPHA.md`), score EQ01 with the
params in `configs/strategies/alpha_params.json`, keep the
`expected_return` of every row with `confidence > 0` (1935 values), and
capture the baseline from those values.  Parity targets in
`expected_adaptive.json` → `"signal_eq01"`:

- PSI of the full sample against its own baseline is **exactly 0.0**;
- PSI and KS (D, p) of the sample's second half (elements `n//2 ..`)
  are pinned at 1e-10.

### 1.1 Live monitor windows (pinned)

A live PSI monitor uses the SAME window as research: `monitor_window_ns`
(1 h) of **event time** with at least `min_psi_samples` (200) samples, from
`configs/strategies.json`.  A fixed ring of N signals is not the pinned
statistic — 256 signals is ~13 minutes on an equity stream and hours on a
sparse FX one, so the live gauge and the research number were never
comparable.  Feed the signal's `exchange_ts`
(`DriftMonitor.onSignal(alphaId, exchangeTs, value)`); a backwards
timestamp is dropped.

## 2. PSI — pinned formula

Given a baseline (edges `e_1..e_9`, `expected_frac`) and current finite
values `y` (drop NaN/inf first):

1. **Bucket assignment** (pinned): bucket of `v` = number of edges
   **strictly less than** `v` (i.e. `searchsorted(edges, v,
   side='left')`).  Bucket 0 = `(-inf, e_1]`, bucket i = `(e_i,
   e_{i+1}]`, bucket 9 = `(e_9, +inf)`.  A value exactly equal to an
   edge falls in the LOWER bucket.
2. `a_i = max(count_y(i) / n_y, 1e-6)`,
   `E_i = max(expected_frac[i], 1e-6)`  — the epsilon guard is applied
   to BOTH sides; clamped fractions are **not renormalized**.
3. `PSI = sum_{i=0..9} (a_i - E_i) * ln(a_i / E_i)`.

A monitor with fewer than `min_psi_samples` (config; 200 pinned) finite
current values reports **no value** (null) — never 0.  The conventional
reading (0.1 modest / 0.25 significant) is a convention; the trigger
threshold is pinned in config, and the trigger comparison is strict
`PSI > psi_threshold`.

## 3. Two-sample KS — pinned formula

`D = max over all pooled sample values v of |F_a(v) - F_b(v)|` with
`F(v) = (# sample values <= v) / n` (right-continuous empirical CDF).
Asymptotic p-value (diagnostic only — KS never triggers a refit):

```
en  = sqrt(n_a * n_b / (n_a + n_b))
lam = (en + 0.12 + 0.11/en) * D
p   = clamp( 2 * sum_{j=1..100} (-1)^(j-1) * exp(-2 j^2 lam^2), 0, 1 )
```

(Numerical Recipes form; exactly 100 series terms, pinned.)

## 4. Rolling realized-vs-research IC

Research window pins an IC baseline: Pearson ICs per fixed event-time
bucket (`bucket_ns` = 300 s pinned; a bucket needs >= 8 finite pairs and
nondegenerate variance to count), `ic_mean`/`ic_std` over those buckets.

**The baseline MUST be out of sample (pinned, round-3).**  Scoring the
deployed model on its own warmup rows makes `ic_mean` optimistic, so live
`ic_z` is biased negative and every drift-triggered refit fires on the
IS/OOS gap instead of on drift (20-40 "drift" refits per alpha in 1.5 days
on the bundled data; 3 with an OOS baseline).  The reference splits the
warmup: fit on its first `BASELINE_FIT_FRAC` (2/3) and take the baseline
bucket ICs from the purged held-out tail.  The serialized baseline carries
`"baseline_kind": "oos"` and **a loader rejects any other value** (including
its absence).

Live, at evaluation time T:

- take rows in `[T - ic_window_ns, T)` that are **matured**:
  `ts + horizon_ns <= T` (a label is knowable only after its horizon);
- scores with `confidence <= 0` are excluded (invalid signal rows);
- compute bucket ICs the same way; with fewer than `min_ic_buckets`
  (4 pinned) buckets, or `ic_std <= 1e-12`, report **null**;
- else `z = (mean(live bucket ICs) - ic_mean) / (ic_std / sqrt(n_live))`.

**The realized leg is the RESEARCH label** (API_FEATURES §6): a signal at
`t` with prevailing mid `m0` realizes `m(t + h) / m0 - 1` where `m(x)` is
the prevailing mid **at-or-before** `x` from the instrument's mid series —
one sample per `book_ok` book refresh.  A live port therefore needs TWO
feeds: every book refresh (`RollingIc.onMid`) and the confident signal rows
(`RollingIc.onSignal`).  Realizing at "the next confident signal emission"
— as the Java gauge used to — runs the forward leg many seconds past
`t + h` on a sparse FX stream and skips it entirely while the alpha is
unconfident, so the live number and `research/baselines/run_*_ic.json` are
not comparable and the lifecycle gauge compares apples to oranges.

Golden: `expected_adaptive.json` -> `"rolling_ic"` embeds the mid series,
the signal rows and the rolling IC at 5 pinned evaluation times (EQ01 on
the golden EQ frame); every port that implements the gauge reproduces them
at 1e-10.

## 5. Refit policies (configs/strategies.json `adaptive.policies`)

All decisions are pure functions of `(now_ns, last_fit_ns, PSI values,
ic_z)` — no wall-clock, no RNG.  `null` monitor values never trigger.

- **static** — never refits.
- **scheduled** (`period_ns`) — refit when
  `now_ns // period_ns > last_fit_ns // period_ns` (epoch-aligned
  calendar cadence; a boundary landing exactly on an evaluation fires at
  that evaluation).  Pinned instances: weekly 604800e9 ns, daily 86400e9 ns.
- **drift_triggered** (`psi_threshold` 0.25, `ic_z_threshold` -2.0,
  `min_refit_gap_ns` 3600e9) — refit when
  `now_ns - last_fit_ns >= min_refit_gap_ns` AND
  (any monitored `PSI > psi_threshold` [strict] OR
  `ic_z < ic_z_threshold` [strict]).

A refit sets `last_fit_ns = now_ns`.  Golden decision sequence (with
edge-exact threshold inputs): `expected_adaptive.json` →
`"drift_trigger"` — exact booleans.

## 6. Lifecycle (configs/strategies.json `adaptive.lifecycle`)

States: `ACTIVE -> WATCH -> RETIRED`, evaluated once per adaptive block
on the rolling OOS IC (section 4's `mean(live bucket ICs)`;
`null` = no transition, counters unchanged):

**Informative evaluations (pinned, round-3).**  An evaluation counts only
when its MATURED set gained at least `min_new_rows` (1) new rows since the
last counted evaluation; otherwise it is *uninformative* and is treated
exactly like `null`: no transition, no counter moves, and it cannot trigger
a drift refit either.  Blocks are 15 minutes and the IC window is 2 hours,
so after a feed goes quiet the window content is frozen — six re-reads of
one bad reading used to retire an alpha (EQ03 went WATCH -> RETIRED on six
copies of the identical rolling IC -0.04115436621771814 and stayed retired
through the next session's open). Silence is not evidence, and neither is
re-reading.

Ports take the flag explicitly:
`LifecycleGauge.update(rollingIc, informative)`; the golden `ic_path`
carries a parallel `ic_informative` array whose final six entries are
uninformative breaches that must move nothing.

- ACTIVE: `ic < watch_ic_gate` (0.0) -> WATCH (the entering breach
  counts as breach #1).
- WATCH: `retire_breach_evals` (6) CONSECUTIVE breaches -> RETIRED;
  `reactivate_evals` (3) consecutive evals with
  `ic >= reactivate_ic_gate` (0.005, inclusive) -> ACTIVE; a reading in
  the neutral zone `[watch_ic_gate, reactivate_ic_gate)` resets BOTH
  counters.
- RETIRED: allocation halted (adaptive backtest forces flat; shadow
  scoring continues for monitoring).  `reactivate_evals` consecutive
  recoveries -> WATCH (probation — never straight to ACTIVE).

Every transition appends one sorted-key JSON line to
`research/lifecycle_log.jsonl`:

```json
{"alpha_id": "...", "policy": "...", "event_ts": 0, "from": "ACTIVE",
 "to": "WATCH", "reason": "...", "rolling_ic": 0.0, "eval_index": 0}
```

Golden state sequence (breach, neutral-zone reset, retirement, recovery,
re-activation, relapse): `expected_adaptive.json` → `"lifecycle"` —
exact states and transitions.

## 7. Adaptive walk-forward backtest (reference semantics)

`iap.backtest.adaptive.AdaptiveDeployment`: warmup `warmup_ns` (fit +
baselines; warmup rows never trade), then evaluations every `block_ns`.
Refits train on the trailing `train_window_ns` **purged** window:
`ts + horizon_ns + embargo_ns < fit_time` (violation raises — the
no-lookahead property is asserted at runtime and shift-tested in
`python/tests/test_adaptive.py`).  Deployed scores in
`[T_k, T_{k+1})` come from the model fitted at or before `T_k`; the
assembled series runs through the standard research backtester
(`iap.backtest.engine`), preserving its exact accounting identity
`net = gross - costs`.

## 8. Golden synthetic PSI/KS vectors

`expected_adaptive.json` → `"psi_ks"`: one continuous SplitMix64 stream
(conventions §3), seed 20260830 — 4000 `uniform()` baseline values, then
2000 `uniform()` ("same_dist"), then 2000 `0.25 + 0.75*uniform()`
("shifted").  The captured baseline (edges + expected_frac) and both
cases' PSI / KS D / KS p are pinned at 1e-10.  Any port's PSI, KS and
baseline-capture code must reproduce all of them from the recipe alone.
