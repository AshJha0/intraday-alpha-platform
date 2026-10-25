# Confirmation framework (v2.0 stage 10, U13; G1 of the ninth review)

Status: specification, revision 1 (2026-10-25), written for the reviewer's freeze decision per profile. Implements U0
section 3 "Stage 10" U13, the plan of record section 15.6 (one general confirmation protocol for Tracks B and C) and
V2-C6 (Track C design contents and the five ruled-out practices). R0 (`python/tests/spec/CONFIRMATORY_TEST_SPEC.md`)
stays frozen and serves H1 alone; nothing here changes it. **Every registration other than H1 names a profile of this
framework or is refused.**

The framework is the part that is written once. A **profile** (`python/tests/spec/CONFIRMATION_PROFILES.md`,
implemented in `iap.validation.profiles`) is the part that is validated separately for each observation unit, horizon
class and dependence domain: its candidate procedures, its synthetic null generators, its own size and power study,
its declared operating domain and its refusal fixtures. A procedure is valid for a registration only inside the
operating domain of the profile the registration names; the framework confers nothing by itself.

## 1. What a pass means (wording fixed, as R0 section 1)

A size pass of a profile cell means: **under the predeclared synthetic data-generating process of that cell, the
procedure's simulated family-wise error rate met the predeclared Monte Carlo acceptance criterion (one-sided 95 %
Clopper-Pearson upper bound <= 0.025 at operating level 0.020).** It is a validated-domain Monte Carlo claim. It is not a
universal guarantee, not a real-world false-positive rate, and not evidence about any data-generating process outside
the cells that passed. The reference cases (section 7) show agreement with reference computations; they do not extend
the domain.

## 2. Framework elements (common to every profile)

| element | rule |
|---|---|
| observation unit | per registration, declared by its profile; default the **daily marked-to-market portfolio return** of the registered book on the registered capital under the conservative cost column, one series per hypothesis, in registered date order, `T` sessions x `K` hypotheses |
| decision level | one-sided family-wise **alpha_op = 0.020**, claim **FWER <= 0.025** (R0 construction) |
| family and multiplicity | the registered family (`K` hypotheses, named before the window opens) under **Holm step-down**; Holm is valid for arbitrary dependence between valid p-values and repairs nothing about an individually invalid p-value, which is why validity is established per profile on the series distribution |
| economic screen | on the **fixed registered capital** with the **common margin**: a hypothesis is promoted only if Holm rejects it AND its mean net return on the registered capital is at or above the registered hurdle times `(1 + margin)`; the hurdle and margin are registration fields, never computed from the confirmation series |
| sample-size rule | a registered `T_min` at or above the profile's smallest validated `T`; the window extends only by the pre-declared rule "sessions after T0 until `T_min` usable sessions are reached, with a cap of 1.25 x `T_min` sessions attempted"; reaching the cap without the count is `confirmation pending` with nothing examined |
| m_min coverage | R0 section 3's form reused with the period as the unit: (a) a minimum count of usable units per session (one unit of the registered analysis grid passing the registered data-quality filter); (b) coverage: usable units span at least 60 % of the session by time and at least 50 % of the registered grid points; a session failing (a) or (b) is dropped by the mechanical rule, listed with the failing criterion, counts toward neither `T` nor the window, identically for development and window sessions, never depending on an outcome; the numbers are fixed per registration before T0 |
| single read | the series is read ONCE, at completion, by the **registered runner** (CR1, identity `iap-runner`) through the data-read choke point `iap.core.guard` with the purpose `confirmation:<profile>:<registration hash>`; `decide` refuses a real-data series read by any other identity or not read through `read_series` |
| fail-closed refusal | `refused: <reason>` is terminal; no verdict path converts it to PROMOTE; refusal precedes every statistic, in the order of `iap.validation.profiles` (profile, evidence, registration record, read identity, series shape and finiteness, domain diagnostics) |
| the PROMOTE constructor | `iap.validation.profiles.decide` is the only constructor of PROMOTE for a profile registration (its token is mintable only inside the module); together with `iap.validation.confirmatory.decide` (H1) these are the two constructors the R1 enumeration test admits |
| deterministic tolerance | relative 1e-10 on statistics, absolute 1e-12 on p-values; rejection sets exact |
| random streams | synthetic study: SplitMix64 as `python/tests/spec/r3_generators.py`, cell seed `20320000 + 10000 * profile_index + 100 * kind + cell_index` (kind 0 size, 1 power), development series at `+ 5000`; bootstrap resample indices from numpy PCG64 `default_rng(cell seed)`; a registration under a bootstrap procedure names its own `bootstrap_seed`; disjoint from R0's 20301000-20301500 |
| size acceptance | per cell `n = 20,000`; pass iff `k <= 463` (`scipy.stats.beta.ppf(0.95, 464, 19537) = 0.02498`); P(pass) 0.999 at true 0.020, 0.048 at 0.025 |
| power acceptance | per reference cell `n = 5,000`; pass iff detections `>= 4,047` (one-sided 95 % lower bound 0.800) at the planted effect with theoretical power 0.90 under the profile's reference model; every other cell's power is reported with its lower bound and no acceptance |
| operating domain | per procedure: the generator cells whose size cells pass at EVERY `T` of the profile's grid; a procedure whose reference (iid normal) cells do not all pass the size acceptance, or whose reference power cells do not all pass the power acceptance (an underpowered procedure passes size trivially), has no domain |
| evidence | `research/v2/confirmation_evidence/<profile>_RESULTS.json` and `CONFIRMATION_EVIDENCE.md`, written once by `python/tests/spec/pf_suite.py --write`; the gate reads the JSON; a re-run with other seeds is a new profile revision, never a retry |

## 3. The five ruled-out practices (V2-C6; each is refused or impossible by construction)

1. **Treating IID Student-t inference as automatically valid for a combined book.** The combined book is PF-3, deferred
   until C0-C2 exist; no profile admits a registration outside its validated domain, and PF-3 has none yet, so a
   combined-book registration is refused (`refused: profile not validated (PF-3 deferred)`).
2. **Choosing a bootstrap block length after seeing significance.** The block length is the declared rule applied to
   the dependence diagnostic computed on DEVELOPMENT data only; the registration binds the diagnostic record and the
   development data's hash; `decide` recomputes the rule from the bound record and refuses a block length that differs
   (`refused: block length is not the declared rule applied to the development diagnostic`). A block length chosen
   beforehand is NOT valid merely for being chosen beforehand: the procedure at that rule must have passed the
   profile's size study (G1).
3. **Repeated forward-window peeking.** Single read by the registered runner at completion; a registry showing a spent
   window or any look on the confirmation dataset is refused before any statistic.
4. **Reusing Track B's sealed test as Track C's confirmation.** The registration's `data` block names its partition
   and the runner's registration ledger refuses a second read for the same registration; a Track C registration
   names `iap-sealed-c`, never `iap-sealed-b`.
5. **Reusing R0's validation as if it covered another domain.** R0 validated P-IND on session values in D-IND for H1.
   A profile registration cannot name P-IND, D-IND or R0's evidence; its validity comes from its own profile's
   evidence file alone, and a profile without a committed evidence file refuses everything.

Also ruled out (G1): **non-overlapping windows are not assumed independent.** A profile's domain is established by its
synthetic study on the series as the gate reads it; whether windows overlap is irrelevant to validity, the measured
dependence is what decides. Each profile needs its own validated operating domain, resampling design and size/power
behaviour.

## 4. The registration record

A registration under this framework is a signed prereg block with exactly these fields; `decide` refuses a record
missing or contradicting any of them.

| field | content | refusal when |
|---|---|---|
| `profile` | `PF-1`, `PF-2` (or a later validated profile) | not the profile `decide` is called with; deferred or unknown |
| `statistic` | `daily_mtm_portfolio_return` (the profile's observation unit) | any other value (`refused: unregistered statistic`) |
| `horizon_sessions` | the book's holding horizon | outside the profile's horizon class |
| `family` | the K hypothesis ids | empty, duplicated |
| `procedure` | one of the profile's candidate procedure ids | not a candidate; no operating domain |
| `alpha_op` | 0.020 | anything else |
| `hac_lag` (HAC procedures) | the profile's pre-fixed lag | differs |
| `block_length` (bootstrap procedures) | `{block_length, bootstrap_seed, diagnostic: {source: "development", development_sha256, acf, band, T_dev, max_lag}}` | diagnostic missing, not from development data, short of the profile's lags, or the block length not the rule's value |
| `sample_size` | `{T_min, cap: 1.25}` | `T_min` below the validated `T`; another cap |
| `refusal_conditions` | `["acf1", "sq_acf1", "excess_kurtosis"]`, the profile's declared diagnostics | any other list |
| `economic_screen` | `{capital_usd, hurdle_bps, margin}` | missing or non-positive capital |
| `registry` | `{window_state: "fresh", confirmation_dataset_looks: 0}` | spent, or looks > 0 |
| `data` | `{origin: "synthetic" or "real", partition, dataset}` | origin undeclared; real origin without a runner read |
| `m_min` | the section 2 form with its numbers | (applied by the runner's session drop before the read) |

The verdict carries the registration's sha256, the read record (path, sha256, identity, guard purpose), the per
hypothesis statistics, the diagnostics against their bounds, the economic screen per hypothesis, and the inference
object's sha256 that the PROMOTE token certifies.

## 5. Profiles (summary; the specification is `python/tests/spec/CONFIRMATION_PROFILES.md`)

| profile | observation unit | horizon class | candidate procedures | synthetic domain | status |
|---|---|---|---|---|---|
| PF-1 | daily marked-to-market portfolio return | 1-5 sessions | `P-HAC-L5` (Newey-West lag 5, fixed-smoothing t reference), `P-CBB` (circular block bootstrap, block from the development diagnostic, floor 5, cap 20) | iid normal; AR(1) rho in {0.1, 0.2, 0.3, 0.5}; GARCH(1,1) 0.08/0.90; Student t3; overlapping-position MA dependence h in {2, 3, 5}; T in {120, 250} | validated by its evidence file; operating domain = the passing cells |
| PF-2 | the same, book with overlapping positions | 20-60 sessions | `P-HAC-L20`, `P-HAC-L60`, `P-CBB` (floor 10, cap 60) | the same families with h in {20, 40, 60}; T in {250, 500} | validated by its evidence file; non-overlapping windows are not assumed independent |
| PF-3 | the combined multi-horizon book's net session return on the registered capital | mixed | to be designed with C0-C2 | its domain questions are written (profiles specification section 8) | deferred; every PF-3 registration is refused |

## 6. Track C confirmation design, minimum contents (V2-C6; filled when PF-3 is designed)

Primary statistic = the combined book's net session return on the registered capital under the conservative column;
the confirmation family (the registered configurations, `K_C`, fixed) and its Holm allocation; the dependence model and
the diagnostics that decide its domain (volatility clustering, serial dependence, non-normal tails, overlapping
exposures); the inference procedure with its own reference cases, adversarial size and power study on declared
synthetic domains, and fail-closed refusal outside them; the sample-accrual rule (m_min, coverage, window cap, single
read at completion); the strategy-selection adjustment for the C0-C2 choice. No forward accrual counts for Track C
until PF-3 is validated and frozen.

## 7. Reference cases and tests

`python/tests/validation/test_profiles.py` (fast): the Newey-West long-run variance against a textbook reference and,
when importable, `statsmodels` (`OLS(x, 1).fit(cov_type="HAC", cov_kwds={"maxlags": L, "use_correction": False})`);
the circular block bootstrap p-value against an explicit resample-by-resample reference on fixed seeds; the
fixed-smoothing reference df; the block-length rule on an AR(1) and an MA development series; `holm_many` row by row
against `confirmatory.holm`; the refusal fixtures of the profiles specification section 7; the single-read and identity
rules through `iap.core.guard`; a planted effect on synthetic data promotes with the inference hash; the smoke subset of
the synthetic study (n = 200 per cell). `python/tests/validation/test_profiles_evidence.py` (slow): recomputes a
declared subset of cells from the committed seeds and asserts the counts equal the committed evidence.

## 8. Open decisions (defect register D-68 to D-73, "reviewer freeze pending")

The block-length rule (Ljung-Box tail test at 5 %, multiplier 2, floor and cap per profile), the HAC lag per profile
and its fixed-smoothing reference, the finite-sample factors, the effect-size grid of the power study, the domain
screen bounds, and the consequence of the percentile bootstrap's own variability. Each carries a chosen default; a
change after an outcome has been observed on real data is a new profile revision.
