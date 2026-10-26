# U13 confirmation protocol for Track C (profile PF-3, the combined book)

Status: specification, revision 1 (2026-10-26), written for the reviewer panel's freeze decision. Nothing in it has
been frozen or signed. Implements plan section 15.6 (one general confirmation protocol for Tracks B and C), the
V2-C6 minimum contents (plan section 8, after stage 10) and U0 section 3, stage 10, "U13 confirmation framework with
profiles". It fills the PF-3 profile that [CONFIRMATION_FRAMEWORK.md](CONFIRMATION_FRAMEWORK.md) section 5 and
[CONFIRMATION_PROFILES.md](../../python/tests/spec/CONFIRMATION_PROFILES.md) section 8 left deferred, and answers
that section's seven domain questions (section 11 below).

| item | where |
|---|---|
| procedure, screens, accrual rule, registration checks | `iap.validation.pf3` ([pf3.py](../../python/src/iap/validation/pf3.py)) |
| synthetic generators | [pf3_generators.py](../../python/tests/spec/pf3_generators.py) |
| size and power study runner | [pf3_suite.py](../../python/tests/spec/pf3_suite.py) |
| evidence (the gate reads the JSON) | [PF-3_STUDY.json](confirmation_evidence/PF-3_STUDY.json), [PF-3_STUDY.md](confirmation_evidence/PF-3_STUDY.md) |
| tests | [test_pf3.py](../../python/tests/validation/test_pf3.py) |

R0 stays frozen and serves H1 alone. PF-1 and PF-2 are untouched. `iap.validation.profiles.decide` keeps refusing
every PF-3 registration until the panel freezes this protocol; the freeze PR wires `iap.validation.pf3.evaluate`
into that gate. Until then `evaluate` returns an inference record and never a PROMOTE verdict.

Evidence classes used below: **[spec]** a rule this document fixes; **[study]** a Monte Carlo result in the committed
evidence file; **[pilot]** a small exploratory run made before the study, recorded in section 10; **[plan]** a
requirement quoted from the plan of record or U0.

## 1. What a pass means

A size pass of a PF-3 cell means: under the predeclared synthetic data-generating process of that cell, the
procedure's simulated family-wise error rate met the predeclared Monte Carlo acceptance criterion (one-sided 95 %
Clopper-Pearson upper bound at or below 0.025 at operating level 0.020). It is a validated-domain Monte Carlo claim.
It is not a real-world false-positive rate and says nothing about processes outside the cells that passed. [spec]
This is the framework's wording unchanged.

## 2. Primary statistic

**Plain idea.** We test one number per configuration per session: what the whole combined book made or lost that
day, after costs, as a fraction of the capital it was registered with. Nothing per component, nothing per name.

**Formal statement.** For configuration `c` in the family and session `d` of the forward window,

    R_{c,d} = (net P&L of the combined book c on session d under the conservative cost column) / capital_registered

where net P&L is marked to market at the session close, after netting across components (U11) and after the
conservative column's fees, spread and impact. The series `R_c = (R_{c,1}, ..., R_{c,T})` in registered date order is
the observation unit; the statistic name in the registration is `combined_book_net_session_return`. The tested
hypothesis is one-sided, `H0_c: E[R_c] <= 0` against `E[R_c] > 0`. [plan, V2-C6]

**Worked example.** A book registered at USD 1,000,000 nets USD 380 on a session after USD 95 of conservative costs:
`R = 380 / 1,000,000 = 3.8 bps`. Two hundred and fifty such sessions form one column of the `(T, 3)` array that
`evaluate` reads.

## 3. The family `K_C` and the multiplicity rule

**Plain idea.** Three configurations (C0, C1, C2) were compared on development data and one was chosen. The forward
test carries all three, so the choice cannot quietly be changed after the window opens, and the multiplicity charge
for having had three candidates is paid up front.

**Formal statement.** The family is fixed at `K_C = 3`: the registered configurations C0, C1, C2, in that order.
Holm step-down at one-sided `alpha_op = 0.020` over the three p-values (claim FWER <= 0.025, the R0 construction).
Holm is valid under arbitrary dependence between valid p-values; the three series share components and are
positively correlated, which makes Holm conservative here, not anti-conservative. The registration names the
configuration `selected` on development data before registration (U12). [spec]

**Strategy-selection adjustment for the C0-C2 choice.** Only the selected configuration can be promoted, and only
if Holm over all three rejects it AND it passes the economic screen (section 7). A Holm rejection of a non-selected
configuration is recorded as a result and is not a promotion. The adjustment is the family size: the selected
configuration's first Holm threshold is `0.020 / 3`, the price of having chosen among three, whichever was chosen.
Because selection used development data that the forward window does not contain, this charge is conservative; it
also covers the practice it is meant to stop, a switch of the selection after the forward series is seen. [spec]

**Worked example.** p-values (C0, C1, C2) = (0.004, 0.006, 0.400), selected = C1. Holm sorts: 0.004 <= 0.020/3 =
0.00667, reject C0; 0.006 <= 0.020/2 = 0.010, reject C1; 0.400 > 0.020, stop. C1 is promotable if its mean clears
the hurdle. C0's rejection is reported and not promoted.

## 4. Dependence model and the diagnostics that decide the domain

**Plain idea.** A combined book's daily return is not a sequence of independent draws. A 20-session holding in one
component makes today's return overlap with the next nineteen; volatility comes in clusters; tails are fat; the
allocation between short and long components can shift. The procedure is only trusted on the kinds of dependence
the synthetic study actually tested, and three diagnostics on the real series check that it looks like one of them.

**Formal statement.** The dependence model is the class of the study's generators (section 5): a sum of
overlapping-position moving averages at the component horizons, optionally multiplied by a common GARCH(1,1)
volatility, with Student t3 innovations, with an allocation switch, or with an AR(1) short component. The
procedure's **operating domain** is the set of generator cells whose size cells pass at every `T` of the grid,
conditional on the iid normal reference cells passing both size and power. Three screens are computed on each
configuration's confirmation series, after the single read and before any statistic: [spec]

| screen | what it measures | definition | bound |
|---|---|---|---|
| `dep_length` | serial dependence and overlapping exposures | `L_dep`, the smallest lag beyond which the Ljung-Box tail statistic over lags `L+1..60` is at or below its 95 % chi-square quantile (the PF-1/PF-2 rule) | the largest 99.5 % quantile of `dep_length` over in-domain size cells at that `T` |
| `vol_cluster` | volatility clustering | lag-1 autocorrelation of squares minus the squared lag-1 autocorrelation (zero at lag 1 for a Gaussian linear process, so overlap alone does not trip it) | the same quantile rule |
| `excess_kurtosis` | non-normal tails | bias-corrected excess kurtosis | the same quantile rule |

A registration also declares `component_horizons`; the largest must not exceed the largest `h_long` among the
in-domain generators (the overlapping-exposure bound, a book-level check that a series diagnostic cannot replace).
A series beyond any bound is refused, not tested. The screens establish only that no known disqualifying condition
was detected; they never widen the domain.

**Worked example.** At `T = 250` suppose the in-domain bounds are `dep_length` 26, `vol_cluster` 0.21 and
`excess_kurtosis` 3.9 (the numbers come from the evidence file; these are illustrative). A C2 series with
`dep_length = 33` is refused with `refused: dep_length outside the validated domain in C2`, and no p-value is
computed for any configuration.

## 5. Synthetic domain (the study's generators)

Every generator has mean 0 and unit variance at every session. `overlap_ma(h)` is the return of a book that holds
each position `h` sessions: `x_t = sum_{j<h} e_{t+j} / sqrt(h)`, lag-k autocorrelation `(h - k) / h`. [spec]

| generator | definition | what it represents |
|---|---|---|
| `iid_normal` | reference | the power reference |
| `garch11` | `z_t sqrt(g_t)`, `g_t = 0.02 + 0.08 u_{t-1}^2 + 0.90 g_{t-1}` on its own shocks | clustering without overlap |
| `t3` | Student t3 scaled to unit variance | heavy tails without overlap |
| `mix` `h_long` in {10, 20, 40, 60} | `sqrt(0.5) overlap_ma(3) + sqrt(0.5) overlap_ma(h_long)` | the overlapping-exposure ladder of a short plus long book |
| `mix_garch` | `mix(h_long 10)` times the common GARCH volatility | cross-component volatility co-movement |
| `mix_t3` | `mix(h_long 10)` with t3 innovations | heavy tails with overlap |
| `mix_regime` | weight on the short component 0.8 then 0.2 from a uniform switch point in `[T/4, 3T/4)`, `h_long 10` | a regime shift in the allocation |
| `mix_ar` | `sqrt(0.5) AR1(0.3) + sqrt(0.5) overlap_ma(10)` | decaying short-horizon dependence |

Grid `T` in {250, 500}: about one and two years of forward sessions, the lengths Track C can reach. `K = 3`
independent hypotheses per dataset (independence is the least favourable case for Holm). 11 generators x 2 `T` =
22 size cells and 22 power cells.

| item | value |
|---|---|
| stream | SplitMix64 (`r3_generators.Stream`), one stream per cell, consumed in chunks, never reseeded |
| cell seed | `20350000 + 100 * kind + index` (kind 0 size, 1 power): size 20350000-20350021, power 20350100-20350121; disjoint from R0 (20301000-20301500), PF-1 (2033xxxx), PF-2 (2034xxxx) |
| development seed | cell seed + 5000 (the series that fixes the cell's block length) |
| bootstrap indices | numpy PCG64 `default_rng(cell seed)` |
| size cell | `n = 20,000`; `k` = datasets with at least one Holm rejection; pass iff `k <= 463` |
| power cell | `n = 5,000`; hypothesis 0 carries `delta(T) = profiles.planted_effect(T, 3)`, theoretical power 0.90 under iid normal at `0.020 / 3` with `T - 1` df; acceptance `>= 4,047` on the iid normal cells only; other cells reported with their 95 % lower bound |

**Acceptance stated up front, as F3 and R0 did.** Size: `P(pass)` is 0.999 at a true FWER of 0.020 and 0.048 at
0.025 (`scipy.stats.beta.ppf(0.95, 464, 19537) = 0.02498`). Power: the one-sided 95 % lower bound of 4,047 / 5,000
is 0.800. The union bound of false failures over 22 in-domain size cells at true 0.020 is at most 0.022. [spec]

## 6. Inference procedure `P-SCBB` and the block-length rule

**Plain idea.** Cut the return series into blocks long enough to carry its dependence, rebuild many artificial
histories by drawing blocks at random, and ask how often an artificial history looks as good as the real one. The
block length is decided from development data before anything forward is seen. The test statistic carries its own
standard error, so the bootstrap reproduces the uncertainty of the error estimate too.

**Formal statement.** [spec]

1. *Block length, before registration.* On the DEVELOPMENT series of the registered book (never the forward
   series), compute `acf_1..acf_60` and `L_dep` by the Ljung-Box tail rule (section 4); the block length is
   `L = clip(2 * L_dep, 5, 60)`. The development series must have at least 122 sessions. The registration binds
   the diagnostic record (`acf`, `band`, `T_dev`, `max_lag`, the development data's sha256) and `L`;
   `registration_refusals` recomputes `L` from the record and refuses a mismatch.
2. *Statistic.* `cb_t`, `t = 1..T`, the circular block means of length `L`; `m` the sample mean; `nb = ceil(T/L)`;
   `g0 = mean((cb_t - m)^2)`; `g1 = mean((cb_t - m)(cb_{t+L} - m))`, the covariance of abutting blocks;
   `s^2 = g0 * max(1, 1 + 2 g1 / g0) / nb`; `t = m / s`.
3. *Resamples.* `B = 999`. Resample `b` draws `nb` block means from `cb` uniformly with replacement (block starts
   `rng.integers(0, T)`, `rng = default_rng(bootstrap_seed)`, `bootstrap_seed` registered) and forms
   `t*_b = (mean(b*) - m) / (sd(b*, ddof=1) / sqrt(nb))`.
4. *p-value.* `p = (1 + #{b : t*_b >= t}) / (B + 1)`. Then Holm over `K_C = 3` at 0.020.

Why the correction in step 2: when the true dependence is longer than `L`, abutting blocks covary and the plain
block variance understates the variance of the mean. The pilot (section 10) showed the plain bootstrap-t at about 2-6
times its nominal level on the `mix` cells; the correction adds the lag-1 covariance between abutting blocks
(a two-term Bartlett estimate on the block means). The floor at 1 keeps sampling noise in `g1` from shrinking `s`
under independence. The bootstrap side needs no correction because resampled blocks are independent by
construction.

**Worked example.** `T = 250`, development rule gives `L_dep = 9`, so `L = 18` and `nb = 14`. Say the forward series
has `m = 2.1` bps, `g0 = 30.0` bps^2 and `g1 = 6.0`: the factor is `1 + 2 * 6 / 30 = 1.4`, `s = sqrt(30 * 1.4 / 14)
= 1.73` bps, `t = 1.21`. If 61 of 999 resamples give `t* >= 1.21`, then `p = 62 / 1000 = 0.062`, well above the
first Holm threshold 0.00667: not promoted. Section 12 runs the same steps on seeded synthetic series.

## 7. Economic screen

On the **combined** registered capital, one hurdle for the book: a configuration is promotable only if Holm rejects
it and its mean `R_c` is at least `hurdle_bps * (1 + margin)` (both registration fields, never computed from the
forward series). Per-component attribution is reported for information and gates nothing. [spec; answers section
8 question 7 of the profiles specification]

## 8. Sample-accrual rule

**Plain idea.** The forward window fills one session at a time. Nobody looks at returns while it fills; the only
thing anyone may know is how many usable sessions there are. When the count reaches the registered target, the
runner reads the series once.

**Formal statement.** [spec, framework section 2]

- `T_min >= 250` (the smallest validated `T`), registered before the window opens; the window cap is
  `1.25 * T_min` sessions attempted.
- *m_min and coverage.* A forward session is usable only if the shadow book recorded at least the registered
  minimum number of analysis-grid units and the usable units cover at least 60 % of the session by time and 50 % of
  the registered grid points. A failing session is dropped by this mechanical rule, listed with the failing
  criterion, and counts toward neither `T` nor the window. The numbers are fixed per registration before T0.
- *State.* `iap.validation.pf3.accrual_status(usable_sessions, attempted_sessions, T_min)` takes counts only and
  returns `accruing`, `complete` (the single read is due) or `pending` (the cap was reached first; nothing is
  examined and the result is reported as pending, never as a failure).
- *Single read.* At `complete`, the registered runner (CR1, identity `iap-runner`) reads the series once through
  `iap.validation.profiles.read_series` (the `iap.core.guard` choke point). `evaluate` refuses a real-data series
  without that read record or read by another identity, and refuses a registration whose registry shows a spent
  window or any look on the confirmation dataset.

**Worked example.** `T_min = 250`, cap 312 sessions. After 300 attempted sessions with 248 usable (52 dropped for
coverage), the state is `accruing`. At 305 attempted and 250 usable it is `complete` and the read happens. Had 312
sessions passed with 249 usable, the state would be `pending` and the series would never be read.

## 9. Fail-closed refusal

`evaluate` returns `refused: <reason>` before any statistic, in this order, and nothing converts a refusal into a
pass: no full study with passing reference cells; any registration defect (`registration_refusals`: profile,
statistic, family, selection, procedure, `alpha_op`, block-length record, sample-size rule, component horizons,
refusal-condition list, economic screen on the combined capital, registry, data origin and partition, bootstrap
seed); real data without the runner's read or read by another identity; series shape, length, finiteness or zero
variance; any of the three screens outside its bound. [spec; the tests enumerate each]

## 10. Pilot-stage record (before the study; synthetic only)

Three design choices were made after pilot runs of at most 3,000 datasets per setting, before the 20,000-dataset
study, on synthetic data only: [pilot]

1. The plain studentized block bootstrap (no abutting-block correction) rejected at about 2-6 times the nominal
   single-test level on `mix(h_long 20)` at `L` from 10 to 60 (for example 0.041 at `L = 10`, 0.012 at `L = 60`,
   against 0.0067). The abutting-block correction of section 6 was added; an unfloored version was tried and
   rejected because the noise in `g1` inflated size under independence (0.012 at `L = 10` on iid normal against
   0.0067).
2. The floor of the block-length rule was lowered from 10 to 5 to recover power on the iid normal reference
   (single-test power about 0.79 at `L = 10` against 0.83 at `L = 5` at `T = 250`).
3. The four structured mixtures (`garch`, `t3`, `regime`, `ar`) were set at `h_long = 10`, and `garch11` and `t3`
   alone and `mix(h_long 10)` were added, after the pilot showed `mix(h_long 20)` near or above the size threshold:
   without them the study could only report "iid normal or nothing" and would not locate the boundary.

No real data was read at any stage.

## 11. Answers to the seven PF-3 domain questions (profiles specification section 8)

1. **Statistic:** the combined book's net session return on the registered capital, netted across components,
   marked daily, conservative column (section 2).
2. **Dependence structures the mixture induces:** summed overlapping MAs at two horizons, common volatility,
   allocation shifts, decaying short dependence, heavy tails (section 5).
3. **Generators:** section 5, with declared weights.
4. **Procedure:** `P-SCBB` at the Track C lengths `T` in {250, 500}; the PF-1/PF-2 HAC lags and percentile block
   bootstrap do not carry over, and validity comes from this study alone (section 12).
5. **`K_C` and selection adjustment:** `K_C = 3`, Holm, only the pre-selected configuration promotable (section 3).
6. **Accrual rule:** section 8.
7. **Economic screen:** on the combined capital (section 7).

## 12. Results

Filled from the evidence file after the run; the JSON is authoritative.

Run 2026-10-26, `pf3_suite.py --shard 0/2` and `--shard 1/2` then `--assemble`; 1,135 s of cell time on two
processes while the DD1 ingestion ran (about 12 minutes wall). Every cell is in
[PF-3_STUDY.md](confirmation_evidence/PF-3_STUDY.md). [study]

| generator | size k at T 250 / 500 (pass <= 463) | in domain | power detections at T 250 / 500 (of 5,000) |
|---|---|---|---|
| `iid_normal` | 337 / 311 | yes | 4,244 / 4,301, **pass** (>= 4,047) |
| `garch11` | 344 / 355 | yes | 2,530 (L = 54) / 4,281 |
| `t3` | 438 / 453 | yes | 4,264 / 4,304 |
| `mix` `h_long` 10 | 461 / 475 | no | 500 / 619 |
| `mix` `h_long` 20 | 666 / 576 | no | 353 / 377 |
| `mix` `h_long` 40 | 960 / 851 | no | 349 / 321 |
| `mix` `h_long` 60 | 1,373 / 1,162 | no | 397 / 318 |
| `mix_garch` | 465 / 494 | no | 610 / 819 |
| `mix_t3` | 433 / 516 | no | 553 / 679 |
| `mix_regime` | 350 / 501 | no | 507 / 589 |
| `mix_ar` | 470 / 453 | no | 917 / 602 |

**Operating domain of `P-SCBB`: `iid_normal`, `garch11`, `t3`.** Screen bounds (99.5 % quantiles, largest over the
domain): `dep_length` 56 / 57, `vol_cluster` 0.331 / 0.327, `excess_kurtosis` 151 / 265 at `T` 250 / 500 (the t3
cells set the kurtosis bound, so that screen is weak). The validated overlapping-exposure horizon is 1 session.

**What this means for Track C.** The procedure is validated for a combined book whose daily series shows clustering
and fat tails but no overlapping exposure. Every cell with a component held 10 or more sessions fails size at one
`T` or both, and its power at the reference effect is 6-18 %: at one to two years of forward data a book carrying a
20-60 session component has too few effectively independent observations for any block bootstrap here to hold its
level. Under this revision a C0-C2 registration with any component horizon above 1 session is refused. Making
Track C confirmable needs a profile revision before any forward accrual counts, for example inference on
non-overlapping holding-period returns at the long horizon, a longer `T`, or a component-level design; the panel
decides. This is a negative result for the procedure's reach, recorded as such.

**Worked example on seeded synthetic data** (reproducible from `python/tests/validation/test_pf3.py` helpers).
(a) `mix(h_long 10)`, development series seed 20359500 (400 sessions) gives `L_dep = 36`, `L = 60`; confirmation
series seed 20359501, `T = 250`, `+0.30` on C1; registration declares `component_horizons = [3, 10]`. Outcome:
`refused: a component horizon lies outside the validated overlapping-exposure domain`, before any statistic.
(b) `garch11`, development seed 20359502 gives `L_dep = 0`, `L = 5`, `nb = 50`; confirmation seed 20359503 with
`+0.30` on C1 and `component_horizons = [1]`. Screens inside bounds (C2 `dep_length` 29 <= 56). Means (C0, C1, C2) =
(-0.020, 0.284, -0.099), `s` = (0.064, 0.057, 0.049), p = (0.615, 0.001, 0.983); Holm rejects C1 only (0.001 <=
0.00667); C1 is selected and clears a zero hurdle: `promotable = true` (an inference record, not a verdict, until the
freeze).

## 13. The five ruled-out practices (V2-C6, verbatim) and how each is stopped

Ruled out by the reviewer and by the plan: [plan]

1. "treating IID Student-t inference as automatically valid for the combined book" — `registration_refusals`
   admits only `P-SCBB`; `P-IND`, `P-HAC-*` and `P-CBB` are refused, and `P-SCBB` itself is valid only inside the
   operating domain its own study established.
2. "choosing a bootstrap block length after seeing significance" — the block length is the declared rule on the
   development diagnostic bound into the registration, recomputed and compared by `registration_refusals`; the
   forward series can refuse a test (screens) but never change `L`.
3. "repeated forward-window peeking" — `accrual_status` takes counts only; the runner reads once at completion; a
   registry with a spent window or any look is refused.
4. "reusing Track B's sealed test as Track C's confirmation" — a real-data registration must name
   `iap-sealed-c`; `iap-sealed-b` is refused.
5. "reusing R0's validation as if it covered this domain" — PF-3's domain comes from `PF-3_STUDY.json` alone; with
   no full study, or with failing reference cells, every registration is refused.

Also carried from the framework: non-overlapping windows are not assumed independent; the measured dependence
decides.

## 14. Open decisions for the panel

The abutting-block correction and its floor; the block-rule floor 5; the screen quantile 0.995; the `T` grid; the
generator ladder and its weights; whether the noisy Ljung-Box rule (which picks long blocks on short development
series, costing power) should be replaced by a more stable rule in a later revision. Each has a chosen default
here. A change after any forward outcome has been observed is a new revision with a note of what was seen first.
