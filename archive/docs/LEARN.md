# LEARN: how we decide whether a signal is real

This page explains the method the platform follows, for two readers at once: a practitioner who
will check each claim against the code, and a student meeting these ideas for the first time.
Each section opens with the idea in plain words, then states what we built and where the rule
lives. Every rule is cited to a file or a test; the file, not this page, is the authority. There
is no tuning advice here and no performance figure, because as of 2026-10-25 we have none to
report: nothing on `main` is confirmed, and the real daily data has not yet been ingested.

The v1.x tutorial on the intraday engines is kept at [../LEARN.md](../LEARN.md) (frozen, 12,000
lines). The vocabulary below is the one every current document uses; section 8 defines each term
once.

Sections: 1 point-in-time data and sealed partitions · 2 counting every look · 3 the confirmatory
gate · 4 why a profile needs its own domain · 5 from a forecast to net P&L · 6 the seven evidence
classes · 7 negative results · 8 glossary.

---

## 1. Point-in-time data and sealed partitions

**The idea.** A backtest answers the question "what would this rule have earned?", and it can
only answer honestly if, at each simulated decision, it knows exactly what a trader could have
known at that moment and nothing more. Three leaks spoil the answer in practice. Survivorship: the
universe is today's index members, so every company that failed has quietly been removed from
history. Look-ahead: a day's closing price is used to make a decision at that same close, or a
filing is used before it was published. Restatement: a vendor's corrected series replaces the one
that was actually on screen at the time.

**A worked example.** An index vendor publishes a membership file dated Saturday 10 January 2026.
We declare a publication lag of two sessions for that source. The first session on or after the
file date is Monday 12 January; two sessions on is Wednesday 14 January. So a backtest may use
that file from 14 January, and a decision on 12 or 13 January must use the previous file. A name
the new file dropped stays in the universe until the 14th. This is the test case in
[`python/tests/leakage/test_pit_universe.py`](../python/tests/leakage/test_pit_universe.py); the
constant it uses is `AvailabilityRule("publication_date", 2)`.

**What we built.** Every dataset and every feature carries a contract that declares its source,
its availability rule, its adjustment convention, its missing-value policy, its route and the
leakage test that guards it. A feature function without a contract cannot be imported
([`iap/data/contracts.py`](../python/src/iap/data/contracts.py); the module's `enforce_module`
raises at import time). The availability rule is one convention for every route: a record of
period `p` with lag `L` is usable for a decision at index `p + L` or later, and the basis fixes the
lower bound of `L` (zero for a record stamped with the time we received it, at least one for a
session's close, at least one for a dated reference file). The contract check is a detector rather
than a declaration: `check_causality` recomputes the feature on the frame truncated at each probe
index and refuses a feature whose value changes, whatever its contract says.

The universe follows the same rules ([`iap/data/pit.py`](../python/src/iap/data/pit.py)): a file
is usable from its availability date, a dropped name leaves only when the dropping file becomes
available, a name that later delists stays a member through its last traded session, and a
delisted name is retained for a declared number of sessions so an open position is closed out
against the delisting valuation rather than vanishing.

Three leakage canaries run in CI on every pull request
([`iap/data/leakage.py`](../python/src/iap/data/leakage.py),
[`python/tests/leakage/`](../python/tests/leakage/)). Each has a positive control, so a detector
that passes everything cannot pass: a feature shifted forward by one period must score at chance
while the unshifted feature scores above it; a permuted label must score at chance; a feature we
deliberately leak must be caught by the contract check. "At chance" means a correlation within
three null standard deviations of zero, about `3 / sqrt(n)` for `n` pairs.

**Sealed partitions.** Leak-proof features are not enough if the researcher can look at the test
data. We cut the daily data set by calendar before any fit into four partitions: development,
validation, a Track B test year and a Track C confirmation window (the calendar is U0 section 2;
the job that cuts it is [`tools/ingest/dd1_daily.py`](../tools/ingest/dd1_daily.py)). The last two
are owned by separate operating-system accounts (`iap-sealed-b`, `iap-sealed-c`). The development
identity, which is the owner's normal user and every research script, cannot read, list or write
those directories. The only route to a sealed byte is a local service that authenticates by the
kernel-reported account name, serves one read per signed registration and refuses a second
([governance/SEALED_DATA.md](governance/SEALED_DATA.md)). We seal at ingestion, before any script
other than the integrity check touches the data, and we never compute a summary statistic of a
sealed partition for the development identity. These controls stop the development identity's
processes; they do not stop the workstation's administrator, who is the same person. Section 6
explains what that does to the evidence class, and SEALED_DATA.md section 4 says it in full.

---

## 2. Counting every look

**The idea.** If you try forty variants of a rule and report the best one, the number you report
is the maximum of forty noisy draws, not an estimate of anything. The standard correction, a
stricter threshold per test, only works if the forty are counted, including the variants that
were abandoned, the ones run on a different data set, and the one somebody ran "just as a
diagnostic". A research programme that counts its looks can say how surprised to be by its best
result. One that does not count them cannot.

**A worked example.** Suppose a hypothesis is tested with a one-sided t statistic and we would
call a result significant at the 2.5 % level if it were the only test. The one-sided 2.5 %
threshold is `t = 1.96` with many degrees of freedom. The selection ledger of this repository
records 640 distinct configurations tried so far (`research/experiments.json`, the
`distinct_experiments` field; `tests/harness/check_headline_numbers.py` re-derives it). The
Bonferroni threshold for 640 tests at the same family level is the quantile at `0.025 / 640`,
which is `|t| >= 4.484`; the expected largest `|t|` among 640 null tests is about 4.2. A result at
`t = 3.5` would look impressive on its own and is unremarkable as the best of 640. The ledger is
what makes the second reading available.

**What we built.** Every evaluated configuration is one recorded experiment in the selection
ledger ([`iap/validation/ledger.py`](../python/src/iap/validation/ledger.py)). An experiment is
identified by its alpha, its kind and the hash of its canonical configuration. Re-running the same
configuration five times is still one look; changing a threshold is a new one; the same statistic
on a different data set is a different look. The ledger is append-only and hash-chained, so the
denominator can only grow and a rewritten history is detectable. The research loop refuses to run
without a ledger: a run that leaves no entry is not a research run
([`tools/research/run_family.py`](../tools/research/run_family.py); [COOKBOOK.md](COOKBOOK.md) recipe 1). Each entry records its **outcome
exposure**, whether whoever chose the next step saw the result, and the registration of a
hypothesis for confirmation pays its multiplicity from that count (H1 specification section 12,
[../research/v119/H1_ALPHA_IMPROVEMENT_SPEC.md](../research/v119/H1_ALPHA_IMPROVEMENT_SPEC.md)).

The denominator that matters for a claim about markets is the number of distinct configurations
tried on real data. Synthetic looks stay in the file where a reader can see them, but they do not
enter that count. Execution studies and model comparisons are looks like any other.

An honest aside. Before the de-duplication rule was pinned (round 3 of the v1.x reviews, recorded
in the ledger module's docstring), the denominator every report quoted was a function of how often
a script had been run, which made the correction meaningless. The fix was to identify an
experiment by its content rather than by the act of running it.

---

## 3. The confirmatory gate, in plain words

**The idea.** We separate finding a signal from confirming it. Finding happens on development
data, as many times as we like, counted. Confirming happens once, on data nobody has looked at,
with a test whose false-positive rate we have measured in advance under conditions we can state,
and with every way of talking ourselves into a "yes" closed off before the data arrive.

R0 is the frozen specification of that test for the first hypothesis, H1
([../python/tests/spec/CONFIRMATORY_TEST_SPEC.md](../python/tests/spec/CONFIRMATORY_TEST_SPEC.md)).
Its bytes are hashed by a test, so any change is a new revision that needs the reviewer's approval.
The implementation is [`iap/validation/confirmatory.py`](../python/src/iap/validation/confirmatory.py),
the only module in the repository that can construct a PROMOTE verdict; every other path is capped
at "iterate" or "ineligible", and a test enumerates the constructors.

**The session is the unit.** Within a trading day, observations are dependent in ways no one can
fully model. Across days, under the registered design (no overlapping sessions, no label that
crosses a day boundary, neutralisation done within the day), the per-session value can be treated
as one draw. So the statistic is a one-sample Student t on the `G` session values, one-sided, with
`G - 1` degrees of freedom, and nothing finer. A session that fails the registered coverage rule is
dropped by a mechanical rule that never looks at an outcome, and a window that cannot reach the
registered `G` is "pending", never a reduced analysis.

**A worked example.** H1 registers two hypotheses (`K = 2`) and a window of `G = 30` sessions. At
one-sided level 0.025 with 29 degrees of freedom the smallest mean effect the test can detect
with 80 % power is 0.53 standard deviations of the session value; with `G = 10` it is 1.00
standard deviations, and with `G = 60` it is 0.37 (these are the reference values of R0 section
6, case R1-D, which [`mde`](../python/src/iap/validation/confirmatory.py) reproduces to `1e-8`).
When the window closes, Holm's step-down orders the two p-values: the smaller must be below
`0.020 / 2 = 0.010` to be rejected, and only then is the larger compared with `0.020`. If the
smaller p-value is 0.012, neither hypothesis is promoted, whatever the larger one says.

**Holm over the registered family.** The family of `K` hypotheses is named before the window
opens. We control the family-wise error with Holm's step-down at one-sided operating level 0.020
and claim a bound of 0.025. The gap is deliberate: an exactly sized procedure passes a Monte Carlo
acceptance at the claimed level only about 5 % of the time, so a test run at the claimed level
would prove little. Holm is valid under arbitrary dependence between valid p-values. It repairs
nothing about an individually invalid one, which is why the next paragraph exists.

**Declared domains.** A Student t p-value is valid only when the session values satisfy its
assumptions. R0 therefore states the domain in which the procedure was validated by adversarial
Monte Carlo (homoskedastic normal session values at any `G` of at least 10; one heteroskedastic
arrangement at a listed grid of `G`) and refuses everything else: serial dependence at any
coefficient, heavy tails, an untested `G`. Real data enters the domain only with a
hypothesis-specific domain-justification memo, reviewed and bound by hash, whose explicit
conclusion field reads `eligible`. Diagnostics establish that no disqualifying condition was
detected and nothing more.

**Fail closed.** The gate refuses, in a fixed order and before computing any statistic: a
malformed family, a spent window or a look on the confirmation data, a missing power table, an
undeclared or unsupported domain, duplicated or disordered sessions, non-finite values, degenerate
variance. A refusal is terminal. No verdict path converts it to PROMOTE, and nothing a caller
controls can produce eligibility without the evidence fields
([`python/tests/test_confirmatory_refusals.py`](../python/tests/test_confirmatory_refusals.py)).

**What a pass means** is fixed wording: under the predeclared synthetic data-generating processes
of the claimed domain, the simulated family-wise error rate met the acceptance criterion. It is a
validated-domain Monte Carlo claim, not a universal guarantee and not a real-world false-positive
rate.

An honest aside. The predecessor of this gate, the v1.18 known-null protocol, was run on
2026-10-23 and failed its own calibration: two cells rejected 79 and 80 times out of 1,854 null
draws, about 4.3 % against a 2.5 % target, and the outside reviewer rejected the method as a gate
([REVIEW_PACK.md](REVIEW_PACK.md) section 1). A second review found the acceptance criterion
itself ill-posed. R0 was written after that, with the domain declared up front and the acceptance
counts recomputed independently by the reviewer. The audit also kept two embarrassments visible:
the first pre-flight evidence file of the F3 acceptance study was overwritten, and the signer's
key file sat on the same machine as the runner
([release/REVIEWER_F3_POSTEXEC_DECISION_2026-10-24.md](release/REVIEWER_F3_POSTEXEC_DECISION_2026-10-24.md)).

---

## 4. Why a profile needs its own domain

**The idea.** A statistical test is validated for a kind of data, not for all data. R0 validated
one procedure on one observation unit (H1's per-session values) in one declared domain. A daily
strategy that holds positions for twenty sessions produces a different series, with overlapping
positions and clustered volatility. Running R0's Student t on it would borrow a validation that
never covered it.

**What we built.** The confirmation framework
([../research/v2/CONFIRMATION_FRAMEWORK.md](../research/v2/CONFIRMATION_FRAMEWORK.md)) keeps the
parts that do not depend on the series fixed: Holm at the same levels, the economic screen on
fixed capital, the coverage rule, a single read by the registered runner, the fail-closed order.
Everything that does depend on the series becomes a **profile**
([../python/tests/spec/CONFIRMATION_PROFILES.md](../python/tests/spec/CONFIRMATION_PROFILES.md),
implemented in [`iap/validation/profiles.py`](../python/src/iap/validation/profiles.py)):
candidate procedures, synthetic null generators, a size and power study of its own, a declared
operating domain and refusal fixtures. A procedure is valid for a registration only inside the
operating domain of the profile the registration names. That domain is exactly the set of
synthetic cells whose size cells passed at every tested series length, and a procedure whose
reference cells fail size or power has no domain at all.

**A worked example.** Profile PF-1 covers books held for one to five sessions. Its candidate
`P-HAC-L5` (a t statistic with a Newey-West variance at lag 5) was run on 20,000 synthetic null
series per cell; a cell passes if it rejects at most 463 times, the 95 % upper bound that keeps
the error rate under 2.5 %. On independent normal, GARCH and Student-t(3) series it passed
(largest count 428). On an AR(1) series with autocorrelation 0.1 at 250 sessions it rejected 477
times, so first-order autocorrelation of 0.1 is outside PF-1's domain, and a registration whose
development diagnostic shows it is refused before any statistic
(CONFIRMATION_PROFILES.md section 10). The block-bootstrap candidate `P-CBB` failed even the
independent normal cells (466 to 576) and has no domain in either profile. We report that rather
than tune it.

Five practices are ruled out by construction (CONFIRMATION_FRAMEWORK.md section 3): treating a
combined book as if its daily returns were independent; choosing a bootstrap block length after
seeing significance; peeking repeatedly at a forward window; reusing one track's sealed test as
another's confirmation; and reusing R0's validation for a domain it never covered. Whether windows
overlap is irrelevant to validity; the measured dependence decides.

---

## 5. From a forecast to net P&L on fixed capital

**The idea.** A forecast is not a return. Between "this stock should go up 12 basis points" and
money in an account sit position sizing under constraints, netting against other positions,
trading at a price worse than the midpoint, fees, and the occasional order that cannot be filled.
We make every claim net of all of that, on a fixed registered capital `C`, from a ledger that
reconciles cash, positions and costs, and we report it three times under three cost assumptions.
Each link below has one file as its authority.

1. **Forecast record** ([`iap/alpha/forecast.py`](../python/src/iap/alpha/forecast.py),
   [`iap/alpha/registry.py`](../python/src/iap/alpha/registry.py)). One record per name and
   decision with seven fields: family, name, the timestamp at which every input was available,
   horizon, expected return in basis points, uncertainty in basis points, and the content hash of
   the registry entry that produced it (model, parameters, feature set, training window,
   software). A record with a missing or malformed field is refused, a record is not consumed
   before its available timestamp, and forecasts are never combined across horizons.
2. **Projection and risk** ([`iap/portfolio/project.py`](../python/src/iap/portfolio/project.py)).
   The raw inverse-volatility book is repaired by one constrained least-squares projection:
   dollar neutral, beta within tolerance, sector and single-name caps, a long never becomes a
   short, a gross cap, a liquidity cap. The objective is strictly convex, so the answer is unique
   and no tie-break exists. A solver that does not converge or violates a constraint beyond
   `1e-9` is a refusal carrying the active constraint set, never a quietly relaxed book.
3. **Netting and the two ledgers** ([`iap/portfolio/ledger.py`](../python/src/iap/portfolio/ledger.py)).
   All intents in one name at one decision time become one net order; opposite intents cross
   internally at the decision mid at no cost; the external fill is allocated back by a
   deterministic rule. Per session the identity
   `NetPnL = Cash_close + sum(q_close * V_close) - Equity_open` holds, and the waterfall
   (midpoint-reference P&L + latency drift - half-spread - impact - fees + execution residual -
   financing) must reproduce it term by term, exactly. The test that enforces this on randomised
   sessions is [`test_ledger_conservation.py`](../python/tests/portfolio/test_ledger_conservation.py).
4. **Execution and the three cost columns** ([`iap/execution/sim.py`](../python/src/iap/execution/sim.py)).
   One fill-price convention: `P_fill = M_arrival + side * (half_spread_arrival + impact)`, with
   `t_arrival = t_decision + latency` and the latency declared in a configuration file, stepped
   only as a sensitivity. The mid movement between decision and arrival is reported as latency
   drift, not charged as a cost, so the waterfall reconciles. Impact is a fixed function of order
   size over displayed depth and trailing volume, with coefficients labelled **estimated** and
   never fitted to an outcome. The three columns differ only by the impact multiplier and the
   displayed-depth haircut: base (1.0x, 100 %), conservative (1.5x, 75 %), stress (2.0x, 50 %).
   Failure cases (vanished quote, insufficient depth, halt, data gap) are deterministic, named and
   recorded. Daily families execute at the auction with a square-root impact model.
5. **Report** ([`iap/reports/e5.py`](../python/src/iap/reports/e5.py)). Every table carries units,
   the counts it rests on and an uncertainty field. A usable session with no trades enters the
   series as zero and is counted; a coverage failure is dropped and listed; an unpriced position at
   the close is valued at stress and flagged. The builder refuses the known ways of inflating a
   Sharpe ratio: an omitted session, a second denominator, a credited refused order, annualisation
   over active periods only. The economic screen is applied on the conservative column on `C`.

**A worked example.** We decide to buy 1,000 shares when the mid is 99.99. Half a second later the
order arrives; the mid is now 100.00 and the quoted spread is 2 cents, so the half-spread is 1
cent. The base impact for this size against the displayed depth comes out at 1 cent. The fill
price is `100.00 + 0.01 + 0.01 = 100.02` in the base column. The ledger records the 1 cent of
mid movement as latency drift (`-1,000 * (100.00 - 99.99) = -10` dollars, reported, not a cost),
10 dollars of half-spread and 10 dollars of impact. In the conservative column the impact
multiplier is 1.5 and only 75 % of the displayed depth is assumed available, so the impact becomes
at least 1.5 cents and the fill `100.025`; in the stress column 2.0x and 50 %, so `100.03` or
worse. With a fee of a tenth of a cent per share, the same trade costs 21, 26 and 31 dollars in
the three columns before any return. Dividing the session's net P&L by `C` gives the session value
in basis points that sections 3 and 4 test.

---

## 6. The seven evidence classes

**The idea.** A result is only as good as the conditions under which it was produced. We attach
to every performance statement one of seven words that say what those conditions were, so that a
reader never has to infer it from tone (U0 section 10):

| class | meaning |
|---|---|
| **independently confirmed** | a registered single-shot decision executed under custody separate from the developer |
| **mechanically isolated** | the same, executed by the registered runner under the developer's sole custody |
| **development-supported** | positive E5-class economics on development data; not confirmed |
| **exploratory** | reported, no claim attached (synthetic fixtures, out-of-domain cells, diagnostics) |
| **rejected** | the registered test or screen failed |
| **data-ineligible** | the data needed to test it does not exist on the route, or failed its suitability gate |
| **pending** | registered; the sealed window is accruing or the run has not happened |

**What "mechanically isolated" means.** It is the honest name for what one workstation can
deliver. The registered runner (`iap-runner`,
[governance/RUNNER_THREAT_MODEL.md](governance/RUNNER_THREAT_MODEL.md)) verifies the signed
registration, the pinned image digest and the dataset hashes before any read; asks the sealed
service for one read, which the service logs and never repeats; runs the registered command once
in a private cache; commits the result to an append-only branch; and publishes a checkpoint that
anchors the service's audit chain in the repository. Each of those controls is a file permission,
a pipe identity check or a repository setting, and the workstation's administrator could change
any of them. The administrator is the developer. So the class upgrades to *independently
confirmed* only when an approval or custody authority other than the developer exists. A
signature, for the same reason, proves integrity (the document did not change after signing) and
nothing about who ran what ([governance/TRUST_ROOTS.md](governance/TRUST_ROOTS.md)).

**Where things stand.** As of 2026-10-25 nothing on `main` is in the first two classes: no
registered run has been executed and no independent party has reproduced a result. The runner,
the services and the reproduction image exist and are tested on synthetic data
([REPRODUCTION.md](REPRODUCTION.md)). The class of every result so far is listed in
[REVIEW_PACK.md](REVIEW_PACK.md) section 0.

An honest aside about words. In v1.15 the R7 confirmation round reported two signals as
"confirmed". The result was re-issued on 2026-10-24 under the v1.18 eligibility rule as
`ineligible`: the run had used a different method bundle from the registered one and its deflated
Sharpe had been computed at the wrong trial count ([research/R7_RESULTS.md](research/R7_RESULTS.md)).
Under the classes above it is *data-ineligible*. The records keep the old word, and the audit
([DOC_AUDIT_v2.md](DOC_AUDIT_v2.md) section 6) lists every place it survives.

---

## 7. Why a negative result is a legitimate outcome

**The idea.** A platform that can only report successes is a selection mechanism. If rejected,
underpowered, ineligible and pending outcomes were quietly dropped, the survivors would be a
biased sample and section 2's accounting would be false. So we report them with the same
prominence as a pass (plan of record standard S9), and three mechanisms make that more than a
resolution: the ledger counts the negative look, so the next registration's multiplicity is right
only if it is recorded; the gate fails closed, so "refused" and "pending" are real outcomes with
reasons attached; and each family specification declares its falsifier before any fit, so a
negative report on development data is the family doing what it said it would do.

**The record so far is almost entirely negative, and we keep it that way.** The B8 two-venue
router was meant to cut execution cost; in-sample on two days it cost 5.1 basis points more
than the single-venue baseline, and out of sample on three other days it was still 1.1 to 2.2
basis points worse, with intervals too wide to say much. The study closed on that result rather
than on a retuned router (`research/execution/b8/REPORT_oos6.md`). The breadth screen passed 0 of 22
candidates net of cost. The known-null calibration failed (section 3). The order-flow and ETF
lead-lag families are data-ineligible on the free route and close pressure is ineligible on daily
data, and their specifications say so instead of being dropped from the list. Every v1.x signed
run was negative, inconclusive or underpowered ([REVIEW_PACK.md](REVIEW_PACK.md) section 0,
[release/](release/)).

---

## 8. Glossary

The terms every current document uses, defined once. British spelling throughout (artefact,
neutralisation).

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
| **family** | a hypothesis class with a specification file: H1 (Track A), D1 to D5 and H2 to H4 (Track B); also the `K` hypotheses of one registration | `research/v2/families/README.md`, R0 section 2 |
| **horizon** | the declared holding period of a forecast (`15m`, `5d`, `c2o`); never mixed | `iap/alpha/forecast.py` |
| **track** | A (H1 under R0), B (families and ML under U13 profiles), C (the combined book) | plan section 2 |
| **gate** | a fail-closed decision procedure; R0 for H1, a U13 profile for everything else | sections 3 and 4 |
| **domain** | the set of data-generating processes in which a procedure's size was validated; outside it the gate refuses | R0 section 2, CONFIRMATION_PROFILES.md section 6 |
| **profile** | a validated bundle of procedure, generators, domain and refusals for one observation unit and horizon class (PF-1, PF-2; PF-3 deferred) | U13 |
| **cost column** | base, conservative or stress: one impact multiplier and depth haircut each | `iap/execution/sim.py` |
| **E5 report** | the five-block development report with screen and decision tree; the only route from pending to development-supported | `iap/reports/e5.py` |
| **guard** | the in-process choke point every data reader calls before opening a file | `iap/core/guard.py` |
| **runner** | the registered-run workflow executed as `iap-runner`; one run per registration | RUNNER_THREAT_MODEL.md |
| **manifest** | `research/data_manifest.json`: every dataset a record may cite, with its integrity verdict | `tools/data_manifest.py` |
| **evidence class** | one of the seven words of section 6 | U0 section 10 |
| **route** | where data comes from: the free daily route (Stooq or Tiingo, Wikipedia membership proxy, EDGAR, French factors), IEX HIST, synthetic fixtures | DATA_SOURCES.md, review/DATA_COVERAGE_FREE_ROUTE.md |
