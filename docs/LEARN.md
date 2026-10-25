# LEARN: the method behind the platform (v2.0)

For a reader with a quantitative background and no history with this project. Each section says
what the platform does, why, and which file is the authority; the files, not this page, decide.
Nothing here is tuning advice and no performance number appears. The v1.x long-form tutorial is
frozen as a historical reference at [../LEARN.md](../LEARN.md) (12,000 lines on the intraday
engines); the v2.0 vocabulary below is the one every current document uses (section 8, glossary).

Status on `main` as of 2026-10-25: **no alpha is confirmed**. Everything produced so far is at most
*development-supported* (section 6); the data on which a claim could be made has not yet been
ingested ([governance/DD1_INGESTION_RUNBOOK.md](governance/DD1_INGESTION_RUNBOOK.md)).

Contents: 1 point-in-time data and sealed partitions · 2 selection accounting · 3 the confirmatory
gate · 4 why a profile needs its own validated domain · 5 the economic chain · 6 evidence classes ·
7 negative results · 8 glossary.

---

## 1. Point-in-time data and sealed partitions

**The problem.** A backtest that uses, at decision time, anything that was not knowable at decision
time is measuring the future, not a strategy. The three usual leaks are survivorship (the universe
is today's index members, so every name that failed is missing), look-ahead (a close used at that
close; a filing used before it was published) and restatement (a vendor's revised series replacing
the one that was available).

**What the platform does.** Every dataset and every feature carries a contract declaring its
source, its availability rule, its adjustment convention, its missing-value policy, its route and
the leakage test that guards it; a feature function without a contract cannot be imported. The
availability rule is one convention for every route: a record of period `p` with lag `L` is usable
only for decisions at index `p + L` or later, with the basis fixing the lower bound of `L`
(`receive_ts` for intraday records, `period_end` for a session's close, `publication_date` for a
reference file). The contract check is a detector, not a declaration: it recomputes the feature on
the frame truncated at every probe index and refuses a feature that reads a later row whatever its
contract says.

The universe is point in time: a constituent file dated `D` is usable from the session at which it
was published, a name dropped by a later file leaves only when that file becomes available, a name
that later delists stays a member through its last traded session, and a delisted name is retained
for a declared number of sessions so open positions are closed out against the delisting valuation
instead of disappearing.

Three leakage canaries run in CI on every pull request, each with a positive control so that a
detector that passes everything cannot pass: a feature shifted forward by one period must score at
chance while the unshifted feature scores above it; a permuted label must score at chance; a
deliberately leaked feature must be caught by the contract check.

**Sealed partitions.** The daily data set is cut by calendar before any development fit into four
partitions: development (fits, diagnostics, selection), validation (walk-forward selection and
admission), the Track B test year and the Track C confirmation window. The last two are owned by
operating-system accounts (`iap-sealed-b`, `iap-sealed-c`) that the development identity cannot
read, list or write; the only route to a sealed byte is a local service that authenticates by
kernel-reported account name, serves one read per signed registration, and refuses a second. The
sealing happens at ingestion, before any script other than the integrity check touches the data, and
no summary statistic of a sealed partition is ever produced for the development identity. The
controls stop the development identity's processes, not the workstation's administrator; the
document says so plainly and the evidence class (section 6) follows from it.

Authorities: [`iap/data/contracts.py`](../python/src/iap/data/contracts.py) (contracts and the
causality check), [`iap/data/pit.py`](../python/src/iap/data/pit.py) (point-in-time universe),
[`iap/data/leakage.py`](../python/src/iap/data/leakage.py) and
[`python/tests/leakage/`](../python/tests/leakage/) (canaries),
[governance/SEALED_DATA.md](governance/SEALED_DATA.md) (identities, partitions, the service, the
single-machine limit), [governance/DATA_ACCESS_PATHS.md](governance/DATA_ACCESS_PATHS.md) and
[`iap/core/guard.py`](../python/src/iap/core/guard.py) (the read choke point), U0 section 2 of the
unified specification (the partition calendar), [governance/DD1_INGESTION_RUNBOOK.md](governance/DD1_INGESTION_RUNBOOK.md)
(how the partitions are filled).

---

## 2. Selection accounting: why every look is counted

**The problem.** Try forty configurations, report the best one, and the reported statistic is a
maximum, not a sample. The correction for that is only honest if the forty are counted, including
the ones that were abandoned, the ones run on a different dataset and the ones run by a script that
"was only a diagnostic".

**What the platform does.** Every evaluated configuration is one recorded experiment in a selection
ledger. An experiment is identified by its alpha, its kind and the hash of its canonical
configuration, so re-running the same configuration five times is one look and a changed threshold
is a new one; a statistic computed on a different dataset is a different look. The ledger is
append-only and hash-chained, so the denominator can only grow and a rewritten history is
detectable. The research loop refuses to run without a ledger: a run that leaves no entry is not a
research run. Each entry records its **outcome exposure**, whether the person or process that
chose the next step saw the result; the registration of a hypothesis for confirmation pays its
multiplicity from that count.

The denominator that matters for a claim about markets is the number of distinct configurations
tried on real data; synthetic looks stay recorded and visible but do not enter that count.
Execution studies and model comparisons are looks like any other.

Authorities: [`iap/validation/ledger.py`](../python/src/iap/validation/ledger.py) (identity,
de-duplication, dataset scope, the real-data family), H1 specification section 12
([../research/v119/H1_ALPHA_IMPROVEMENT_SPEC.md](../research/v119/H1_ALPHA_IMPROVEMENT_SPEC.md)),
the research loop `iap.research.loop` and `tools/research/run_family.py` (pull request #389; see
[COOKBOOK.md](COOKBOOK.md) recipe 1), plan of record section 0 standard S3.

---

## 3. The confirmatory gate, in plain words

R0 is the frozen specification of the one procedure that can say "confirmed" about H1
([../python/tests/spec/CONFIRMATORY_TEST_SPEC.md](../python/tests/spec/CONFIRMATORY_TEST_SPEC.md);
its bytes are hashed by a test and any change is a new reviewer-approved revision). The
implementation is [`iap/validation/confirmatory.py`](../python/src/iap/validation/confirmatory.py),
the only module that constructs a PROMOTE verdict; every other path in the repository is capped at
"iterate" or "ineligible", and a test enumerates the constructors.

**The session is the unit.** Within a trading session observations are dependent in ways no one
can fully model; across sessions, under the registered design (no overlapping sessions, no label
crossing a session boundary, neutralisation within the session), the per-session value is the thing
that can be treated as one draw. So the statistic is a one-sample Student t on the `G` session
values, one-sided, `G - 1` degrees of freedom, and nothing finer. Sessions that fail the registered
coverage rule are dropped by a mechanical rule that never looks at an outcome, and a window that
cannot reach the registered `G` is "pending", never a reduced analysis.

**Holm over the registered family.** A registration names `K` hypotheses before the window opens.
The family-wise error is controlled by Holm's step-down at one-sided operating level 0.020 with a
claimed bound of 0.025; the gap is there because a passing Monte Carlo acceptance at the claimed
level would be a weak test of an exactly sized procedure. Holm is valid under arbitrary dependence
between valid p-values; it repairs nothing about an individually invalid one, which is why the next
paragraph exists.

**Declared domains.** A p-value from a Student t is valid only when the session values satisfy its
assumptions. R0 therefore states the domain in which the procedure was validated by adversarial
Monte Carlo (homoskedastic normal session values at any `G` of at least 10; one heteroskedastic
arrangement at a listed grid of `G`) and refuses everything else: serial dependence at any
coefficient, heavy-tailed tiers, an untested `G`. Real data is admitted to the domain only with a
hypothesis-specific domain-justification memo, reviewed and bound by hash, whose explicit
conclusion is `eligible`; diagnostics establish that no disqualifying condition was detected and
nothing more.

**Fail closed.** The gate refuses, in a fixed order and before computing any statistic, a
malformed family, a spent window or a look on the confirmation data, a missing power table, an
undeclared or unsupported domain, duplicated or disordered sessions, non-finite values and
degenerate variance. A refusal is terminal: no verdict path converts it to PROMOTE, and nothing a
caller controls can produce eligibility without the evidence fields.

**What a pass means** is fixed wording: under the predeclared synthetic data-generating processes
of the claimed domain, the simulated family-wise error rate met the acceptance criterion. It is a
validated-domain Monte Carlo claim, not a universal guarantee and not a real-world false-positive
rate.

---

## 4. Why a profile needs its own validated domain

R0 validated one procedure, on one observation unit (H1's session values), in one declared domain.
A daily family with a 20-session holding period produces a different series with different
dependence (overlapping positions, volatility clustering) and a different horizon; applying R0's
Student t to it would be reusing a validation that never covered it.

The confirmation framework (U13; `research/v2/CONFIRMATION_FRAMEWORK.md` and
`python/tests/spec/CONFIRMATION_PROFILES.md`, implemented in `iap.validation.profiles`, landing
with the stage 10 pull request) keeps the framework elements fixed (Holm at the same levels, the
economic screen on fixed capital, the coverage rule, a single read by the registered runner, the
fail-closed order) and makes everything that depends on the series a **profile**: candidate
procedures, synthetic null generators, a size and power study of its own, a declared operating
domain and refusal fixtures. A procedure is valid for a registration only inside the operating
domain of the profile the registration names, and that domain is exactly the set of synthetic cells
whose size cells passed at every tested length; a procedure whose reference cells fail size or
power has no domain at all. Five practices are ruled out by construction: treating independent-ish
Student t as valid for a combined book, choosing a bootstrap block length after seeing
significance, repeated peeking at a forward window, reusing one track's sealed test as another's
confirmation, and reusing R0's validation for a domain it never covered. Non-overlapping windows
are not assumed independent; the measured dependence decides.

Authorities: U0 section 3 "Stage 10", plan of record section 15.6, the two U13 documents above.

---

## 5. The economic chain: signal to net P&L on fixed capital

A forecast is not a return. The platform makes every claim net, on a fixed registered capital `C`,
from a ledger that reconciles cash, positions and costs, after netting across families, and reports
it under three cost columns. The chain, with the authority for each link:

1. **Forecast record.** One record per name and decision with seven fields (family, name,
   available timestamp, horizon, expected return in bps, uncertainty in bps, registry id). A record
   with a missing or malformed field is not consumed; a record is not consumed before its available
   timestamp; forecasts are never combined across horizons. The registry id is the content hash of
   what produced it (model, parameters, feature set, training window, software), so a forecast
   artefact is reproducible by identity. [`iap/alpha/forecast.py`](../python/src/iap/alpha/forecast.py),
   [`iap/alpha/registry.py`](../python/src/iap/alpha/registry.py).
2. **Projection and risk.** The raw inverse-volatility book is repaired by one constrained
   least-squares projection (dollar neutral, beta tolerance, sector and name caps, sign
   preservation, gross cap, liquidity cap). The objective is strictly convex so the solution is
   unique; a solver that does not converge or violates a constraint beyond tolerance is a refusal
   carrying the active constraint set, never a silently relaxed book. The no-trade band is chosen by
   an outcome-blind rule. [`iap/portfolio/project.py`](../python/src/iap/portfolio/project.py).
3. **Netting and the two ledgers.** All cohort intents in one name at one decision time become one
   net order; opposite intents cross internally at the decision mid with no cost; the external fill
   is allocated back to cohorts by a deterministic rule (mandatory orders first in expiry order,
   then pro rata by largest remainder). The primary identity holds per session:
   `NetPnL = Cash_close + sum(q_close * V_close) - Equity_open`, and the waterfall
   (midpoint-reference P&L + latency drift - half-spread - impact - fees + execution residual -
   financing) must reproduce it term by term, exactly. A failed close-out is carried at a flagged
   valuation so a loss is charged once. [`iap/portfolio/ledger.py`](../python/src/iap/portfolio/ledger.py),
   [`python/tests/portfolio/test_ledger_conservation.py`](../python/tests/portfolio/test_ledger_conservation.py).
4. **Execution and the three cost columns.** One fill-price convention:
   `P_fill = M_arrival + side * (half_spread_arrival + impact)` with
   `t_arrival = t_decision + latency`, the latency declared in a configuration file and stepped only
   as a sensitivity. The mid movement between decision and arrival is not a cost; it is reported as
   latency drift from the same fill record so the waterfall reconciles. Impact is a deterministic
   function of size over displayed depth and trailing volume with fixed coefficients labelled
   **estimated**, never calibrated to an outcome. The three columns differ **only** by the impact
   multiplier and the displayed-depth haircut: base (1.0x, 100 %), conservative (1.5x, 75 %), stress
   (2.0x, 50 %). Failure cases (vanished quote, insufficient depth, halt, data gap) are deterministic,
   named and recorded; daily families execute at the auction with a square-root impact model whose
   coefficient is likewise labelled estimated. [`iap/execution/sim.py`](../python/src/iap/execution/sim.py),
   H1 specification section 9.
5. **Report.** The E5 report carries, per block, units, the counts it rests on and an uncertainty
   field. A usable session with no trades enters the series as zero and is counted; a coverage
   failure is dropped and listed; an unpriced position at the close is valued at stress and flagged.
   The report builder refuses the known ways of inflating a Sharpe ratio: an omitted session, a
   second denominator, a credited refused order, annualisation over active periods only. The
   economic screen is applied on the conservative column on `C`. [`iap/reports/e5.py`](../python/src/iap/reports/e5.py),
   H1 specification sections 10, 11 and 13.

---

## 6. Evidence classes, and what "mechanically isolated" means

Every performance statement in this repository carries exactly one of seven classes (U0 section
10, final wording):

| class | meaning |
|---|---|
| **independently confirmed** | a registered single-shot decision executed under custody separate from the developer |
| **mechanically isolated** | the same, executed by the registered runner under the developer's sole custody |
| **development-supported** | positive E5-class economics on development data; not confirmed |
| **exploratory** | reported, no claim attached (synthetic fixtures, out-of-domain cells, diagnostics) |
| **rejected** | the registered test or screen failed |
| **data-ineligible** | the data needed to test it does not exist on the route, or failed its suitability gate |
| **pending** | registered; the sealed window is accruing or the run has not happened |

**Mechanically isolated** is the honest name for what one workstation can deliver. The registered
runner (`iap-runner`) verifies the signed registration, the pinned image digest and the dataset
hashes before any read; asks the sealed service for one read, which the service logs and never
repeats; runs the registered command once in a private cache; commits the result to an append-only
branch and publishes a checkpoint that anchors the service's audit chain in the repository. Every
one of those controls is a file permission, a pipe identity check or a repository setting that the
workstation's administrator could change, and the administrator is the developer. The class
therefore upgrades to *independently confirmed* only when an approval or custody authority other
than the developer exists. A signature proves integrity (the document did not change after
signing) and nothing about who ran what.

As of 2026-10-25 nothing on `main` is in the first two classes: no registered run has been executed
and no independent party has reproduced a result. The runner, the services and the image exist and
are tested on synthetic data.

Authorities: [governance/TRUST_ROOTS.md](governance/TRUST_ROOTS.md) (the four controls a class
rests on), [governance/RUNNER_THREAT_MODEL.md](governance/RUNNER_THREAT_MODEL.md) (the six
properties and their tests), [governance/SEALED_DATA.md](governance/SEALED_DATA.md) section 4,
[REPRODUCTION.md](REPRODUCTION.md) (the pinned image and the status vocabulary),
[REVIEW_PACK.md](REVIEW_PACK.md) section 0 (the class of every result so far).

---

## 7. Why a negative result is a legitimate outcome

A platform that can only report successes is a selection mechanism. Here a hypothesis that is
rejected, underpowered, ineligible or pending is reported with the same prominence as one that
passes (plan of record standard S9), for three reasons that are mechanical, not cultural:

- the selection ledger counts it, so the next registration's multiplicity is right only if it is
  recorded;
- the gate fails closed, so "refused" and "pending" are real outcomes with their reasons attached,
  not an absence of news;
- the family specifications declare a falsifier before any fit, so a negative E5 report on the
  development partition is the family doing what it said it would do. The order-flow and ETF
  lead-lag families are data-ineligible on the free route, close pressure is ineligible on daily
  data, and the record says so rather than quietly dropping them.

Every v1.x signed run was negative, inconclusive or underpowered, and the known-null calibration
study failed and was rejected by the reviewer as a gate. Those records are kept, in their own words,
with their class ([REVIEW_PACK.md](REVIEW_PACK.md) section 0,
[../docs/release/](release/)).

---

## 8. Glossary

The terms every current document uses. British spelling throughout (artefact, neutralisation).

| term | meaning | authority |
|---|---|---|
| **session** | one trading day on the exchange calendar of the data manifest; the unit of inference | R0 section 2 |
| **partition** | one of development, validation, Track B test, Track C confirmation; cut by calendar at ingestion | U0 section 2, SEALED_DATA.md |
| **identity** | an operating-system account with a role: the development identity (`iap-dev`), the sealed owners (`iap-sealed-a/b/c`), the runner (`iap-runner`) | SEALED_DATA.md section 1 |
| **registration, prereg** | a signed document fixing hypothesis, family, thresholds, data and the bound code before the window opens | ANALYSIS_IDENTITY.md, TRUST_ROOTS.md |
| **look** | one evaluated configuration on one dataset, recorded in the selection ledger with its outcome exposure | `iap/validation/ledger.py` |
| **selection ledger** | the append-only, hash-chained record of looks (`research/experiments.json`) | `iap/validation/ledger.py` |
| **cohort** | the positions opened by one family at one decision time, carried to their scheduled expiry | H1 specification section 7 |
| **executable ledger** | the cash-and-positions ledger that nets across cohorts and families and reconciles to the identity | `iap/portfolio/ledger.py` |
| **family** | a hypothesis class with a specification file: H1 (Track A), D1 to D5 and H2 to H4 (Track B); also the `K` hypotheses of one registration | `research/v2/families/README.md` (PR #389), R0 section 2 |
| **horizon** | the declared holding period of a forecast (`15m`, `5d`, `c2o`); never mixed | `iap/alpha/forecast.py` |
| **track** | A (H1 under R0), B (families and ML under U13 profiles), C (the combined book) | plan section 2 |
| **gate** | a fail-closed decision procedure; R0 for H1, a U13 profile for everything else | section 3, section 4 |
| **domain** | the set of data-generating processes in which a procedure's size was validated; outside it the gate refuses | R0 section 2, CONFIRMATION_PROFILES.md section 6 |
| **profile** | a validated bundle of procedure, generators, domain and refusals for one observation unit and horizon class (PF-1, PF-2; PF-3 deferred) | U13 |
| **cost column** | base, conservative or stress: one impact multiplier and depth haircut each | `iap/execution/sim.py` |
| **E5 report** | the five-block development report with screen and decision tree; the only route from pending to development-supported | `iap/reports/e5.py` |
| **guard** | the in-process choke point every data reader calls before opening a file | `iap/core/guard.py` |
| **runner** | the registered-run workflow executed as `iap-runner`; one run per registration | RUNNER_THREAT_MODEL.md |
| **manifest** | `research/data_manifest.json`: every dataset a record may cite, with its integrity verdict | `tools/data_manifest.py` |
| **evidence class** | one of the seven words of section 6 | U0 section 10 |
| **route** | where data comes from: the free daily route (Stooq or Tiingo, Wikipedia membership proxy, EDGAR, French factors), IEX HIST, synthetic fixtures | DATA_SOURCES.md, review/DATA_COVERAGE_FREE_ROUTE.md |
