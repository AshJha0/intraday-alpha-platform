# Confirmation profiles PF-1, PF-2 (and the deferred PF-3): specification (U13, revision 1, 2026-10-25)

For the reviewer's freeze decision per profile. Framework: `research/v2/CONFIRMATION_FRAMEWORK.md`. Implementation:
`iap.validation.profiles`. Generators and suite: `python/tests/spec/pf_generators.py`, `pf_suite.py`. Evidence:
`research/v2/confirmation_evidence/`. Nothing here is adjustable after an outcome has been observed on real data; a
change is a new revision of the profile and needs a new freeze decision and a note of what was observed first. The
pilot-stage changes made before the confirmatory synthetic run (section 9) are recorded as such.

## 1. Observation unit and claim (both profiles)

The observation unit is the **daily marked-to-market portfolio return** of the registered book on the registered
capital under the conservative cost column, one series per hypothesis, `T` sessions in registered date order, `K`
hypotheses. The economic reporting unit and the inference unit coincide. The claim is the framework's section 1
wording per cell; the operating domain of a procedure is the set of generator cells whose size cells pass at every
`T` of the profile's grid.

## 2. Candidate procedures

| id | statistic | reference / p-value | pre-fixed parameters |
|---|---|---|---|
| `P-HAC-L<lag>` | `t = mean / sqrt(LRV / T)`, LRV the Newey-West (Bartlett) long-run variance at the profile's **pre-fixed** lag with biased autocovariances times the finite-sample factor `T / (T - lag - 1)` | one-sided (greater) Student t with `df = min(T - 1, floor(3 T / (2 lag)))`, the fixed-smoothing t-approximation for the Bartlett kernel (its long-run variance estimator at bandwidth `b = lag / T` has asymptotic variance about `(4/3) b` times the squared long-run variance, the variability of a chi-square with `3 / (2 b)` df) | lag per profile; no data-dependent bandwidth ever |
| `P-CBB` | circular block bootstrap of the mean: `ceil(T / L)` whole circular blocks of **fixed** length `L` per resample, `B = 999` resamples, resample statistic the mean of the `ceil(T / L) L` values, centred resample means scaled by the finite-sample factor `sqrt(T / (T - L))` | one-sided percentile `p = (1 + #{scaled(m*_b - m) >= m}) / (B + 1)` | `L` by the block-length rule (section 3) on development data only; resample indices from numpy PCG64 `default_rng(bootstrap_seed + j)` for hypothesis `j`, the seed registered |

Holm step-down over `K` at `alpha_op = 0.020`; the economic screen per hypothesis as the framework states.

## 3. The block-length rule (P-CBB)

Computed on the DEVELOPMENT series of the registered book (never the confirmation series): sample autocorrelations
`acf_1 .. acf_max_lag`; `L_dep` = the smallest `L` in `0 .. max_lag` such that the Ljung-Box statistic of the tail lags
`T (T + 2) sum_{k = L + 1}^{max_lag} acf_k^2 / (T - k)` is at or below the 95 % chi-square quantile with `max_lag - L`
df (a joint test of no residual dependence beyond `L`; `L_dep = max_lag` if none qualifies); block length
`L = clip(multiplier * L_dep, floor, cap)` with `multiplier = 2`. The development series must have at least
`2 max_lag + 2` sessions. The registration binds the diagnostic record (`acf`, `band`, `T_dev`, `max_lag`, the
development data's sha256) and the block length; `decide` recomputes the rule from the record and refuses a block
length that differs. In the synthetic study each cell's development series is one series of length `T` from the
cell's generator at the development seed (section 5).

| profile | HAC lag(s) | P-CBB floor | cap | max_lag |
|---|---|---|---|---|
| PF-1 | 5 | 5 | 20 | 20 |
| PF-2 | 20 and 60 (two candidates) | 10 | 60 | 60 |

## 4. Synthetic null generators (both profiles; all stationary, mean 0, unit unconditional variance)

| generator | definition | theoretical lag-1 autocorrelation |
|---|---|---|
| `iid_normal` | reference model | 0 |
| `ar1` rho in {0.1, 0.2, 0.3, 0.5} | `x_1 ~ N(0, 1)`, `x_t = rho x_{t-1} + sqrt(1 - rho^2) e_t` | rho |
| `garch11` | `h_t = omega + 0.08 x_{t-1}^2 + 0.90 h_{t-1}`, `omega = 0.02`, `h_1 = 1`, normal innovations, 300 burn-in sessions dropped; excess kurtosis about 1.43, lag-1 autocorrelation of squares about 0.205 | 0 |
| `t3` | Student t3 scaled to unit variance (`z / sqrt(chi2_3 / 3) / sqrt(3)`, the F3 b1 construction) | 0 |
| `overlap_ma` h | `x_t = sum_{j=0}^{h-1} e_{t+j} / sqrt(h)`: the dependence a holding horizon of `h` sessions induces between overlapping windows; lag-k autocorrelation `(h - k) / h` | `(h - 1) / h` |

PF-1: `h in {2, 3, 5}`, `T in {120, 250}`. PF-2: `h in {20, 40, 60}`, `T in {250, 500}`. `K = 4` independent
hypotheses per dataset (independence is the least favourable dependence between valid p-values for Holm's FWER).
Each profile has 10 generator cells x 2 `T` = 20 size cells and 20 power cells; every candidate procedure reads every
cell's panel.

## 5. Streams, seeds and counts

| item | value |
|---|---|
| stream | SplitMix64 as `python/tests/spec/r3_generators.py` (bit for bit `iap.core.rng.SplitMix64`), one stream per cell, consumed chunk by chunk, never reseeded |
| cell seed | `20320000 + 10000 * profile_index + 100 * kind + cell_index` (`kind` 0 size, 1 power; `profile_index` 1, 2); PF-1 size 20330000-20330019, PF-1 power 20330100-20330119, PF-2 size 20340000-20340019, PF-2 power 20340100-20340119 |
| development seed | cell seed + 5000 |
| bootstrap resample indices | numpy PCG64 `default_rng(cell seed)`, one generator per cell per bootstrap procedure, `integers(0, T)` of shape `(datasets, K, B * ceil(T / L))` per sub-chunk |
| size cell | `n = 20,000`; `k` = datasets with at least one Holm rejection; pass iff `k <= 463` (the R0 form: `scipy.stats.beta.ppf(0.95, 464, 19537) = 0.02498 <= 0.025`) |
| power cell | `n = 5,000`; hypothesis 0 carries the planted mean `delta(T)` with theoretical power 0.90 under the iid normal reference at Bonferroni level `alpha_op / K` with `T - 1` df (the one noncentral-t inversion `confirmatory.mde(T, 0.005, 0.90)`); detections = datasets where Holm rejects hypothesis 0; acceptance `>= 4,047` on the `iid_normal` cells only; every other cell reported with its 95 % lower bound and no acceptance |
| joint false-fail | union bound <= 0.001 per in-domain size cell at true 0.020 |
| budget | measured on one core (section 10) |

## 6. Operating domain and the gate's screens

A procedure's operating domain is the set of generator cells (name and parameters) whose size cells pass at EVERY `T`
of the grid; a procedure whose `iid_normal` size cells do not all pass, or whose `iid_normal` power cells do not all
pass the power acceptance, has no domain and every registration naming it is refused. The gate reads the committed evidence file and nothing else.

Real series are mapped to the domain by three declared diagnostics computed on each hypothesis's confirmation series
(they establish only that no known disqualifying condition was detected, R0-2):

| diagnostic | bound | refused when |
|---|---|---|
| `acf1` lag-1 autocorrelation | max theoretical lag-1 autocorrelation over the in-domain generator cells + `2 / sqrt(T)` | above |
| `sq_acf1` lag-1 autocorrelation of squares | (0.205 if `garch11` is in domain, else 0) + `2 / sqrt(T)` | above |
| `excess_kurtosis` bias-corrected | (30 if `t3` is in domain; else 1.43 if `garch11` is in domain; else 0) + 1.0 | above |

`T` must be at least the registered `T_min`, itself at least the profile's smallest validated `T`.

## 7. Refusal fixtures (deterministic; `python/tests/validation/test_profiles.py`)

Each returns the named `refused:` outcome whatever the data: (a) unknown profile; (b) PF-3 (deferred); (c) a profile
with no evidence file (`profile not validated`); (d) registration naming another profile; (e) statistic other than
`daily_mtm_portfolio_return`; (f) horizon outside the class; (g) empty or duplicated family; (h) `P-IND` or any
non-candidate procedure; (i) a candidate procedure with no operating domain; (j) `alpha_op` not 0.020; (k) HAC lag
differing from the profile's; (l) P-CBB without a development diagnostic, with a diagnostic not from development data,
or with a block length that is not the rule's value for the bound diagnostic; (m) `T_min` below the validated `T`;
(n) refusal conditions not the profile's three diagnostics; (o) economic screen missing; (p) spent window or looks on
the confirmation dataset; (q) real-data series passed as an array (not a runner read); (r) real-data series read by an
identity other than `iap-runner`; (s) a real-data file the guard refuses (no hook registered); (t) series shape differing
from the family; (u) fewer sessions than `T_min`; (v) non-finite values; (w) zero variance; (x) lag-1 autocorrelation
above the bound (an AR(1) 0.8 series); (y) squared-return autocorrelation above the bound; (z) excess kurtosis above
the bound. No field a caller controls produces eligibility without the evidence file and the development-bound
diagnostic.

## 8. PF-3, the combined multi-horizon book: domain questions only (deferred to a later PR once C0-C2 exist)

Every PF-3 registration is refused until a PF-3 revision is validated and frozen. Its design must answer, before any
forward accrual counts (V2-C6): (1) what the combined book's net session return on the registered capital is when
the components hold at 1-5 and 20-60 session horizons at once (one series, marked daily, with the netting of
positions across components applied); (2) which dependence structures the mixture induces that neither PF-1 nor PF-2
covered (a sum of an MA(h-1)-type component and a short-horizon component; cross-component volatility co-movement;
regime shifts in the allocation); (3) which synthetic generators represent them (a mixture of the PF-1 and PF-2
generators with declared weights; a common GARCH factor; the allocation rule applied to synthetic components); (4) which
procedure and resampling design can be validated at the accrual lengths Track C can reach (the HAC lag and block
rules of PF-1/PF-2 do not carry over; a validated size pass at the Track C `T` is required); (5) the family `K_C`
(the registered C0-C2 configurations) and the strategy-selection adjustment for the C0-C2 choice; (6) the
sample-accrual rule (m_min analogue, coverage, cap, single read); (7) whether the combined book's economic screen is on
the combined capital or per component. The five ruled-out practices apply in full.

## 9. Pilot-stage record (before the confirmatory synthetic run; nothing observed on real data)

Three design changes were made after pilot smoke runs of at most 4,000 datasets per cell and before the 20,000-dataset
run: (1) the block-length rule's per-lag `2 / sqrt(T)` band over a 60-lag window raised a false dependence alarm at
most lags under iid data (the cap was reached routinely), replaced by the Ljung-Box tail rule of section 3; (2) the
finite-sample factors `T / (T - lag - 1)` (HAC) and `sqrt(T / (T - L))` (bootstrap) were added for the known downward
bias of both estimators under independence; (3) the HAC reference changed from `T - 1` df to the fixed-smoothing df of
section 2 after the iid cells at lag 20-60 showed the expected inflation under a `T - 1` reference. Defects D-68 to
D-73 record these as reviewer-freeze-pending defaults.

## 10. Results (filled from the evidence files after the run; the JSONs are authoritative)

Run 2026-10-25 (`pf_suite.py --write`, CPython 3.12.10, numpy 2.4.6, SciPy 1.17.1); every cell in
`research/v2/confirmation_evidence/CONFIRMATION_EVIDENCE.md`; the JSONs are what the gate reads.

| profile | procedure | operating domain (size passes at every T) | largest in-domain k | reference power (T grid) | verdict |
|---|---|---|---:|---|---|
| PF-1 | `P-HAC-L5` | `iid_normal`, `garch11`, `t3` | 428 | 4,387 / 4,402 pass | validated for series with no detectable serial dependence (`acf1` bound `0 + 2 / sqrt(T)`); every AR(1) cell fails (rho 0.1 at T = 250: 477; rho 0.2: 534-589; rho 0.5: 938-1,049) and every overlapping-position cell fails (h = 2: 574-581; h = 5: 1,115-1,228) |
| PF-1 | `P-CBB` | none (iid normal 504-576) | - | 4,473 / 4,438 | no domain: the percentile bootstrap's own resampling variability (D-73) |
| PF-2 | `P-HAC-L20` | `iid_normal`, `ar1` rho 0.1-0.3, `garch11`, `t3` | 459 | 4,136 / 4,318 pass | validated up to `acf1` 0.3 + `2 / sqrt(T)`; rho 0.5 fails (504, 531); every overlapping-position cell fails (h = 20: 1,412-1,511; h = 60: 6,112-6,689) |
| PF-2 | `P-HAC-L60` | none (size passes on every non-overlap cell incl. rho 0.5, 220-407, but the reference power cells fail: 3,001 / 3,862 < 4,047) | - | fail | no domain: underpowered (12-18 reference df); overlap cells 573-1,785 |
| PF-2 | `P-CBB` | none (iid normal 466-553) | - | 4,445 / 4,439 | no domain (D-73) |

Budget, measured with the two profiles running at once on a shared machine: PF-1 1,398 s, PF-2 1,728 s of cell time
(about 23 and 29 minutes); the smoke subset in CI is 4 cells at n = 200 per profile (seconds).

Consequence for registrations: PF-1 admits a 1-5 session book only when its daily series shows no detectable serial
dependence; PF-2 admits a 20-60 session book only up to AR(1)-like dependence 0.3 and never the MA dependence that
overlapping holding-period returns carry. A family whose series fails these screens is refused and needs a profile
revision (D-73), not a longer lag chosen after the fact.
