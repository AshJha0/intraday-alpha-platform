# Intraday Alpha Platform

A reproducible, event-driven, research-to-production trading platform spanning
market data, microstructure alpha, ML research, portfolio construction, risk,
execution, SOR, TCA, deterministic replay, and low-latency engineering — built
to the institutional specification in [docs/SPECIFICATION.md](docs/SPECIFICATION.md)
(spec §1), deliberately polyglot per the spec's responsibility matrix (§3):

- **Python** — quant research and ML environment, the *reference
  implementation* every port must match, and the executable MVP that runs
  the whole loop (`python -m iap.mvp`);
- **C++** — latency-critical HFT path (codec, book, features, alpha,
  execution simulator, SOR, replay);
- **Rust** — safety-critical infrastructure (hard risk engine, event bus,
  venue protocol simulation, telemetry, replay components).

Three languages. The Java strategy/platform layer (portfolio, TCA, risk
orchestration, backtest, paper trading, monitoring/API) is archived in
[archive/java/](archive/java/README.md), frozen at v1.14.0, not built, not a
port target (v1.15 archived it; v1.17 retired its deployment image, DECISION_N7).
Its risk engine is kept only as the byte-identical mirror of the Rust risk
engine checked by the risk goldens. The `/metrics` process is Python
(`python -m iap.metrics serve`, [deployment/](deployment/grafana/README.md)).

The connecting principle (spec §1): alpha, execution, portfolio construction
and risk are separate concerns joined by explicit, versioned contracts, and
**no strategy is production-ready because of backtest Sharpe alone**.

The engineering principle, stated once and enforced everywhere: **Python
defines the semantics, C++ and Rust implement them, golden tests prove
equivalence.** Two subsystems qualify it honestly — the rule text of the
hard risk engine is owned by Rust and of the execution simulator by C++
(PLATFORM_CONVENTIONS.md §11) — and since 2026-09-19 both also have a Python
implementation (`iap.risk`, `iap.execution`) proven by the same golden files
the Java ports are proven by, so the loop the MVP runs is the reference
loop end to end.

## Start here

New to the repository? [docs/SHOWCASE.md](docs/SHOWCASE.md) is a three-minute
read: what the platform is, what is proven and what is not (the negative
results stated plainly), the numbers a CI check re-derives, one architecture
diagram and a ten-minute quick start. The long form of everything this README
used to carry (release-by-release status, the reviews that moved the numbers,
the repository map, the real-world usage notes and the references) is in
[docs/README_HISTORY.md](docs/README_HISTORY.md).

## Docs map

| if you want to… | read |
|---|---|
| understand the platform | [LEARN.md](LEARN.md), [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/DIAGRAMS.md](docs/DIAGRAMS.md) |
| run something | [COOKBOOK.md](COOKBOOK.md), [Quick start](#quick-start), [docs/MVP.md](docs/MVP.md), [docs/BUILD_NOTES.md](docs/BUILD_NOTES.md) |
| use real data | [docs/REAL_DATA.md](docs/REAL_DATA.md), [docs/R7_DATA_PLAN.md](docs/R7_DATA_PLAN.md), [docs/research/](docs/research/) |
| check the research methods | [docs/RESEARCH_VALIDITY.md](docs/RESEARCH_VALIDITY.md), [docs/LIFECYCLE.md](docs/LIFECYCLE.md), [research/alpha_reports/REPORT.md](research/alpha_reports/REPORT.md) |
| port or call an API | [API_CORE.md](API_CORE.md), [API_FEATURES.md](API_FEATURES.md), [API_ALPHA.md](API_ALPHA.md), [API_PORTFOLIO_TCA.md](API_PORTFOLIO_TCA.md), [API_ADAPTIVE.md](API_ADAPTIVE.md), [API_CONTRACTS.md](API_CONTRACTS.md), [API_TRADING.md](API_TRADING.md), [API_AGENTS.md](API_AGENTS.md), [API_LLM.md](API_LLM.md), [API_PAPER.md](API_PAPER.md), [python/src/iap/README.md](python/src/iap/README.md) |
| follow the rules | [PLATFORM_CONVENTIONS.md](PLATFORM_CONVENTIONS.md), [CONTRIBUTING.md](CONTRIBUTING.md), [docs/POLYGLOT.md](docs/POLYGLOT.md), [docs/governance/](docs/governance/), [docs/SPECIFICATION.md](docs/SPECIFICATION.md) |
| operate it | [docs/runbooks/](docs/runbooks/), [docs/SCENARIOS.md](docs/SCENARIOS.md), [docs/DECISION_TRACE.md](docs/DECISION_TRACE.md), [docs/DATA_MODEL.md](docs/DATA_MODEL.md) |
| see the plan and the history | [docs/ROADMAP.md](docs/ROADMAP.md), [docs/EPICS.md](docs/EPICS.md), [CHANGELOG.md](CHANGELOG.md) |

The full list with descriptions is the [Documentation index](#documentation-index).

## Current status (v1.18.0, tag v1.18.0, dated 2026-10-23; the previous release v1.17.1 is dated 2026-10-17)

**v1.18.0: a measurement instrument and recorded negative results. No signal is promoted and no edge is claimed**
([CHANGELOG.md](CHANGELOG.md) v1.18.0, [docs/release/v1.18.0_RELEASE_NOTES.md](docs/release/v1.18.0_RELEASE_NOTES.md),
[the REL item table and result index](docs/release/RESULTS_INDEX.md)):

- **The known-null calibration FAILED** (null v3, run 2026-10-23): conditional rejection 4.3 % against a 2.5 % target in two cells.
  The outside reviewer rejected the method as the F3 gate; no real-data error-rate control is claimed
  ([research/v118/known_null_v3_RESULT.md](research/v118/known_null_v3_RESULT.md), [reviewer close-out](docs/release/REVIEWER_CLOSEOUT_2026-10-23.md)).
- **Every signed run is negative or underpowered:** B8 v2 (6 comparisons, none better than pinned Nasdaq; G = 3 sessions), REL-05 (AAPL, MSFT
  inconclusive, QQQ supports), REL-08 (one-session screen, 7 variants, all underpowered, with defects found afterwards), REL-09 (underpowered),
  REL-10 (one taker cell rejected: `hid_imb` 1 min AAPL loses money; the rest underpowered). REL-08c was dropped
  ([research/v118/rel08c/DROPPED.md](research/v118/rel08c/DROPPED.md)); REL-08b and REL-11 were not run.
- **The fill model is validated at Level 1 (deterministic replay) only, not at Level 2** ([docs/SIM_TO_REAL.md](docs/SIM_TO_REAL.md)); there is no latency edge.
  The REL-04 verification run found an identity pass on the six IEX overlap days; absolute IEX latency and skew against Nasdaq are not claimed.
  The replay's feed delay is a declared assumption, never zero by default and never a measurement (`configs/reference/venue_latency.json`;
  [API_TRADING.md](API_TRADING.md) section 2.20), and every execution number can be reported at the plan's 0.5 s to 5 s latency grid.
- **Built:** signed pre-declarations that bind script, commit, data and thresholds (`python tools/verify_signatures.py`, [docs/governance/TRUST_ROOT.md](docs/governance/TRUST_ROOT.md));
  a fail-closed holdout registry (`research/holdout_registry.json`); one real-data ledger family (real data: 256 configurations; see the
  headline table); the execution benchmark v2 metrics; a venue-aware capped child simulator with an invariant suite; a known-null
  protocol (REL-06, failed); monitoring with an explicit UNKNOWN state ([docs/MONITORING_POLICY.md](docs/MONITORING_POLICY.md)); a factor risk
  model, portfolio limits and a kill switch ([docs/FACTOR_RISK.md](docs/FACTOR_RISK.md), [docs/PORTFOLIO_LIMITS.md](docs/PORTFOLIO_LIMITS.md));
  a paper-path order manager with a write-ahead journal and reconciliation (60-minute and 6-hour fault-injection soaks PASS on a synthetic
  feed, not a live venue); a data manifest (`tools/data_manifest.py`).
- **Open gates, accepted open for this tag:** F3, F7 (no independent rerun of the B8 v2 JSON), the reviewer's full findings memo; F5 was partly met at the tag and is met by the 2026-10-24 R7 re-issue
  ([docs/release/FOUNDATION_COMPLETE_v1.18.md](docs/release/FOUNDATION_COMPLETE_v1.18.md)).
- **F3 diagnostic review (2026-10-24, exploratory; [findings](research/v118/F3_DIAGNOSTIC_REVIEW.md), plan [F3_DIAGNOSTIC_REVIEW_PLAN.md](research/v118/F3_DIAGNOSTIC_REVIEW_PLAN.md)): the A0 acceptance criterion was ill-posed -- a correctly sized test fails a count <= 55 of 1,854 line about a third of the time on one dataset; the estimator is not shown to be mis-sized; no v4 is recommended; F3 stays open and no real-data error-rate control is claimed. **The signed F3 acceptance study has been run (2026-10-24): all 27 mandatory evaluations PASS under the declared rule on synthetic regimes only (`research/v118/f3_accept/REPORT.md`); this does not close F3 and supports no real-data claim; the reviewer accepted the PASS on 2026-10-24 ([post-execution decision](docs/release/REVIEWER_F3_POSTEXEC_DECISION_2026-10-24.md)): P1 (Student t(G-1) with Holm at operating alpha 0.020) is calibrated below 2.5 % under the specified synthetic nulls at G = 5, 7, 10 only; no real-market-data FWER control, no serial-dependence robustness, no power claim and no alpha claim; the broader F3 gate stays OPEN pending a power-diagnostic specification and an operating-domain assessment. Audit limitations kept visible: the first pre-flight evidence file was overwritten, and the signer's key file sat on the same machine as the runner.** The outside reviewer accepted the findings and **retired the A0 count criterion as a pass/fail gate (kept as a diagnostic)**; CR1 + Webb at G = 7 is not approved for strict nominal-control claims, a bounded CR2/CR3 comparison is required before any replacement, drafting v4 is not authorised, and REL-08 v2 signing awaits a separate review of its draft ([reviewer decision, 2026-10-24](docs/release/REVIEWER_F3_DECISION_2026-10-24.md)).** **Specification review (2026-10-24, [reviewer decision](docs/release/REVIEWER_F3_SPEC_DECISION_2026-10-24.md)):** the unsigned F3 specification ([draft](research/v118/F3_SPEC_DRAFT.md), pilot [results](research/v118/f3_spec/F3_PILOT_RESULTS.md); pilot verdict NO ELIGIBLE PROCEDURE) is approved with named changes; P1 (Student t(G-1)) is the candidate for a new preregistered acceptance study, not a validated procedure; no acceptance run is authorised, F3 stays open. **Freeze review (2026-10-24, [reviewer decision](docs/release/REVIEWER_F3_FREEZE_DECISION_2026-10-24.md)):** the unsigned acceptance-study freeze package (`research/v118/f3_accept/`) is approved with named conditions for signature preparation, not for execution; four verification checks (freeze consistency, checkpoint integrity, no-data enforcement, acceptance interpretation) are met in the repo and await the reviewer; F3 stays open, a statistical pass would not automatically close it. **Power and operating domain (2026-10-24, unsigned drafts, nothing run):** a separate non-gating power-diagnostic specification ([F3_POWER_DIAGNOSTIC_SPEC.md](research/v118/F3_POWER_DIAGNOSTIC_SPEC.md)) and an operating-domain assessment of the diagnostics that would be needed before P1 could be used on real data, all NOT RUN ([F3_OPERATING_DOMAIN_ASSESSMENT.md](research/v118/F3_OPERATING_DOMAIN_ASSESSMENT.md)), await the reviewer; neither changes the acceptance result, closes F3 or supports a real-data claim. **Reviewer decision on both (2026-10-24, [decision](docs/release/REVIEWER_F3_POWER_DOMAIN_DECISION_2026-10-24.md)):** the power specification is approved with named changes for freeze/signature preparation (revision 2 incorporates them), the domain assessment is a planning document only, real-data diagnostics are NOT authorised, nothing is executed; a metadata-only inventory finds only 10 sparse in-sample sessions ([inventory](research/v118/F3_INSAMPLE_HISTORY_INVENTORY.md)), so the serial-dependence diagnostic would be uninformative and P1 is not usable for real-data inference claims on that history. **Power-study freeze package (unsigned draft, nothing run, non-gating):** [`research/v118/f3_power/`](research/v118/f3_power/PROTOCOL.md) (22/22 freeze checks, 200-dataset dry run PASS); it awaits the reviewer's confirmation for signature and then the owner's signature; it cannot change the acceptance result. **Reviewer decision on the power freeze package (2026-10-24, [decision](docs/release/REVIEWER_F3_POWER_FREEZE_DECISION_2026-10-24.md)):** approved with two documentation checks for signature preparation (mu = d*sigma_RMS/sqrt(G) used consistently and the integrity band's `excess` fixed in the declaration; the imported acceptance `core.py` bound by its exact hash), now satisfied and tested; power-study execution is NOT authorised, F3 stays open.
- **REL-08 v2 draft review (2026-10-24, [reviewer decision](docs/release/REVIEWER_REL08_V2_DECISION_2026-10-24.md), PR #342):** approved with named changes for development, **not approved for signature**. The within-session C1 gap is approved in principle; book parameters 20 / 100 are provisional synthetic settings only; the moving-block bootstrap is not approved for v2 inference without a prospective synthetic-null calibration of the full decision (within-session resampling, K = 7, C1-C4, six-session structure); a minimum of two usable rebalances per qualifying session is required. v2 real-data execution and alpha promotion are not authorised, F3 stays open, and the v1 rule and its recorded result are unchanged.
- **REL-08 v2 calibration-plan amendment (2026-10-24, [reviewer decision](docs/release/REVIEWER_REL08_V2_AMENDMENT_DECISION_2026-10-24.md)):** R = 200 per gating cell remains the confirmatory design (Option A only as a labelled preliminary stage; N3 stays); the short-circuit is accepted with equivalence evidence; calibration execution, signature and real-data execution are not authorised. [Revision 2 of the plan](research/v118/rel08/REL08_V2_CALIBRATION_PLAN_REV2.md) (staged fixed-sample run with a non-decisional checkpoint at 100, [manifest](research/v118/rel08/REL08_V2_CALIBRATION_MANIFEST.json)) awaits the reviewer; nothing has been run.
- **REL-08 v2 revision 4 (2026-10-24, [reviewer decision](docs/release/REVIEWER_REL08_V2_REV4_DECISION_2026-10-24.md)): the reviewer conditionally authorised one synthetic-only calibration against the frozen execution identity; the guarded launch passed and started, then the owner decided to skip the run and it was stopped without reading any result. REL-08 v2 is PARKED as an unsigned draft; the operating-domain assessment is parked as inconclusive; F3 stays OPEN.**
- **REL-08 v2 calibration, revision 3 (2026-10-24, [reviewer decision](docs/release/REVIEWER_REL08_V2_REV3_DECISION_2026-10-24.md)):** the statistical design (fixed R = 200, six mandatory cells, operational-only checkpoint at 100) is approved and freeze preparation is authorised; launch awaits final freeze verification, and signature and real-data execution are not authorised. The [execution-integrity protocol](research/v118/rel08/REL08_V2_EXECUTION_PROTOCOL.md) and a content-only frozen [execution manifest](research/v118/rel08/calibration/execution_manifest.json) answer the six conditions; nothing has been run.

**v1.17.1 (released 2026-10-17):**

**v1.17.1 is a patch release after v1.17.0 (tag f9e351a): negative results first, then the fixed release workflow**
([CHANGELOG.md](CHANGELOG.md) v1.17.1 and [docs/release/v1.17.1_RELEASE_NOTES.md](docs/release/v1.17.1_RELEASE_NOTES.md)):

- **B7 (signal-aware execution) closed on its negative in-sample result** (2026-10-16): no cell cheaper by the 2 bp minimum, urgency dearer on TWAP / VWAP at 1-2 % ADV; nothing signed or registered ([research/preregs/B7_CLOSED_v117.md](research/preregs/B7_CLOSED_v117.md)).
- **B8 (two-venue router on real Nasdaq + IEX books) is negative on the first two overlap days:** `sor` +5.1 ± 1.3 bps all-in and 8 pp lower fill than pinned Nasdaq; the opt-in `sor_sweep` route +3.8 ± 2.1 bps. 2 day clusters, provisional ([REPORT_overlap2](research/execution/b8/REPORT_overlap2.md), [REPORT_sweep](research/execution/b8/REPORT_sweep.md)). **B8 six-day out-of-sample result (pre-declared in [PREDECLARATION_oos6.md](research/execution/b8/PREDECLARATION_oos6.md)): also negative.** Calibrated on 3 days, evaluated on 3 others (AAPL, MSFT): `sor` +2.2 / +1.1 bps, `sor_sweep` +0.6 / +1.2 bps vs pinned Nasdaq, no 95 % interval excludes zero ([REPORT_oos6](research/execution/b8/REPORT_oos6.md)).
- **IEX overlap days:** 6 of 9 recorded (2025-11-28, 12-08, 12-09, 12-10, 2026-05-15, 2026-05-18), TOPS oracle 0 mismatches on AAPL / MSFT / QQQ ([docs/research/IEX_OVERLAP_SET.md](docs/research/IEX_OVERLAP_SET.md)).
- **Release workflow fixed (#211):** v1.17.0 shipped without `release-manifest.json`, the Python lock-file SBOMs and digest-pinned k8s manifests; v1.17.1 is the first release to carry them.

**v1.17.0 (released 2026-10-16), execution quality and breadth; its headline results stand:**

- **Breadth: the net-of-cost screen passes 0 of 22.** The pre-registered C6 screen on multi7
  (7 in-sample sessions): 0 pass, 22 fail, 1 not screened (EQ11 at 15 m had no scored row).
  Gross ICs reach 0.13, but the modelled round trip (about 1.90-2.02 bps) is larger than every
  edge, and every screened candidate's net IC is negative
  ([research/breadth/screen/SCREEN_multi7.md](research/breadth/screen/SCREEN_multi7.md)).
- **The breadth book makes no confirmatory look.** The owner registered C8 under option (a):
  the screen is a hard gate, it refused every candidate, and the holdout stays unread
  (`research/preregs/breadth_book_prereg.json`, signed).
- **Execution quality: the depth cap, then the book.** The implementation-shortfall benchmark
  (B1) re-run with a 50-level walk on multi7: at 0.5 % ADV the old 10-level walk cap, not the
  book, caused the "depth exhausted" readings (60-67 % of parents to 0 %, 11 % for VWAP); at
  2 % ADV the displayed book itself runs out (67-89 % still exhausted); the 10-level runs
  understated cost by 2-5 bps at 1-2 % ADV. In-sample only
  ([research/execution/benchmark/DEPTH_COMPARISON_multi7.md](research/execution/benchmark/DEPTH_COMPARISON_multi7.md)).
  Signal-aware urgency and pacing (B2 / B3), quote fading (B4), queue-aware cancels (B5) and
  markout decomposition (B6) ship opt-in; the dark venue (B10) is **modelled, not measured**
  (no free dark-pool data exists).
- **IEX, a free second venue.** The IEX HIST codec (A1) rebuilds the DEEP+ book and checks it
  against TOPS. Day 1 of the nine Nasdaq / IEX overlap days, 2025-11-28: 148,115,121 messages,
  1,969,356 events, TOPS oracle 0 mismatches on AAPL / MSFT / QQQ (435,973 / 72,883 / 998,642
  comparisons) ([docs/research/IEX_OVERLAP_SET.md](docs/research/IEX_OVERLAP_SET.md)).
- **Agents within a budget.** A spend ledger with a monthly cap of USD 30 (E1), a research
  assistant on `claude-sonnet-5-5` (E2), a morning ops agent and a docs-sync agent on
  `claude-haiku-5-5` (E3 / E4); the LLM never computes a number, signs, registers or merges
  ([docs/AGENTS.md](docs/AGENTS.md)).
- **Engineering.** C++ tick-to-trade on a real ITCH slice (AAPL, 2019-01-30), measured on a
  shared GitHub-hosted CI runner (AMD EPYC 7763, no pinning, `steady_clock`, in-process; not
  colocation): p50 1,535 ns, p99 2,943 ns
  ([benchmarks/results_tick_to_trade.md](benchmarks/results_tick_to_trade.md));
  hand-written C++ ports of the new execution rules with byte-for-byte goldens (F7), the
  dark-first pre-trade controls in Rust (F8), Java retired from the release (F9).

**Standing results.** Bundled synthetic data: 24 alphas, 0 PROMOTE, 23 held at CANDIDATE; the
executable loop loses 82.84 USD on its golden session (the table below). Real data
(owner-supplied, not bundled): R7-CONFIRM-1 confirmed EQ10@1s and EQ05@1s on 7 unseen sessions
in v1.15 under the rules of that release (predictive, not tradable at 1x cost; re-issued as `ineligible` under the v1.18 verdict rule, see R7_RESULTS.md;
[docs/research/R7_RESULTS.md](docs/research/R7_RESULTS.md)); v1.16 found no taker, capacity,
quoting or DeepLOB edge; v1.17 adds the screen and breadth results above;
v1.18 adds only negative or underpowered results (the status block at the top)
([docs/REAL_DATA.md](docs/REAL_DATA.md) §3, [LEARN.md](LEARN.md) §31-§44).

**Next (after v1.18.0, not part of it):** the post-release documentation pass (REL-14c, done), a diagnostic review of the failed
known-null method (F3, done 2026-10-24: see the findings; not an automatic v4), a REL-08 rule v2 (an unsigned draft) with a multi-session screen, the R7 re-issue (F5, done 2026-10-24), and v1.19 planning. C++ is parity-only since decision D5(a) (2026-10-18); the C++ ports still listed as pending in
PLATFORM_CONVENTIONS.md §11.3 are the B2-regime arm of dark eligibility, the exchange-local (IANA zone) hazard
clock, and the B8 two-venue merge and router hook (the B10 proxy sampler and fit are Python by design). Plan:
[docs/ROADMAP.md](docs/ROADMAP.md) §3.14.

What each release from v1.9 to v1.16 added, and the 2026-10-11 quality
review, are recorded in full in
[docs/README_HISTORY.md](docs/README_HISTORY.md) §1 and
[CHANGELOG.md](CHANGELOG.md).

## What this is, and what it is not

It is a research platform that tries hard not to fool its owner: one
seeded generator, integer contracts, byte-identical goldens across Python,
C++ and Rust (and Java until its archive at v1.14.0), a look ledger that prices every statistic computed, and
pre-registration that is checked by code. It runs the full loop from
market data to TCA on one instrument and paper-trades a universe.

**Execution simulator: Level 1 only, Level 2 not validated.** Fills are
modelled on replayed books; no real orders or real fills exist, so no fill
rate, shortfall, impact or capacity figure is empirically validated
([docs/review/SIMULATOR_CORRECTNESS.md](docs/review/SIMULATOR_CORRECTNESS.md)).

It is not a profitable strategy. Nothing has been promoted, and the
documents say so at the top rather than in a footnote. It is not connected
to a live venue: there is no order gateway, so the 15c3-5 checks guard a
simulator. The real data is seven (plus two holdout) Nasdaq sessions on
three symbols, which is enough to find statistically strong signals and
not enough to say much about their size (since then: six Nasdaq / IEX
overlap days recorded by v1.17.1, and in v1.18 the REL-04c single-venue
universe of about 20 Nasdaq names, all in-sample; none of it is bundled). The Java tree is an archived
copy frozen at v1.14.0 ([archive/java/](archive/java/README.md)), not a
production service.

## AI assistance and who checked what

The code, tests, reviews and documents in this repository were produced with Claude (AI)
assistance. Checks are human or mechanical, never the assistant's own word: the owner merges
every pull request and signs every pre-registration and continuation rule; CI enforces the
goldens, parity counts and documentation checks; an outside reviewer delivered a written close-out
on 2026-10-23 ([docs/release/REVIEWER_CLOSEOUT_2026-10-23.md](docs/release/REVIEWER_CLOSEOUT_2026-10-23.md)):
the known-null method is rejected as the F3 gate, the simulator conclusions stay limited to deterministic
replay, and the reviewer inspected but did not rerun the B8 v2 result (F7 open). What the reviewer is
asked to check, and how to reproduce the headline results without trusting this text, is in
[docs/REVIEW_PACK.md](docs/REVIEW_PACK.md).

## Architecture in one line

Spec §4, realized end to end in this repository:

```
venues → feed gateways → normalization + sequence validation → canonical event bus
→ order-book reconstruction → feature engine → alpha ensemble → regime/confidence
→ portfolio construction → hard risk → execution optimizer → SOR/venue adapters
→ executions → TCA + attribution → decision trace (JSONL + SQLite, explain)
→ research feedback: ExperimentRunner → ledger → 7-state promotion lifecycle
```

Every hop is a typed, versioned contract (`schemas/`, `iap.contracts`); every
decision cycle is one `DecisionTrace`; every alpha's position in RESEARCH →
CANDIDATE → VALIDATING → PAPER → ACTIVE → WATCH → RETIRED is a gated,
ledgered transition. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for
the full design, data flow, and diagrams.

## Headline numbers (all verified against repo artifacts)

| what | number | artifact |
|---|---|---|
| Registered features | **205** (10 families; 40-feature native core set ported to C++/Rust/Java) | `data/reference/feature_registry.json` |
| Flagship alphas | **24** (EQ01–EQ12, FX01–FX12), each with an enforced `Economic rationale:` docstring | `python/src/iap/alpha/`, `research/alpha_reports/` |
| Promotion verdicts | **0 PROMOTE / 12 ITERATE / 12 REJECT** (gated on the pooled *uncrossed* IC and its HAC t against the ledger threshold, 4.426 at the run's look count; the v1.14.0 methods: causal row block, automatic day-separated HAC, time-shift leakage; FX11 fails leakage) | `research/alpha_reports/REPORT.md` |
| Lifecycle registry | **23 alphas at CANDIDATE, 0 beyond** (FX11 held in RESEARCH by the v1.14 time-shift leakage gate) — every candidate fails `net_pnl_after_costs` at 1× costs, its bootstrap bound `net_pnl_bootstrap_ci` and `capacity`; the cross-alpha correlation gate passes vacuously (7 states, 17 pinned edges, 20 gates) | `research/alpha_registry.json`, `research/lifecycle_transitions.jsonl`, `tests/golden/expected_lifecycle.json` |
| Experiments ledger (as of the v1.18 REL-02 migration) | every entry tagged `data_family`: **real data: 256 configurations** (holding 1,132 of the looks: the 24 multi7 looks, 14 pre-registration debits, 12 entries merged from the per-dataset real ledgers holdout2026 / multi7 / r7_holdout, and 206 retroactive B7/B8 execution-study comparisons); synthetic data: 384 configurations holding 5,694 of the looks (1,068 + 4,626, recorded but excluded from the real-data N); total 6,826 recorded looks over **640 distinct configurations** (de-duplicated by alpha × kind × config × dataset × statistics revision); expected max \|t\| 4.202 under the global null, Bonferroni per-test \|t\| ≥ 4.484 (both over all looks); a hash chain since v1.14 (`research/v118/REL02_LEDGER_MIGRATION.md`). Not a complete historical denominator: a lower bound, with an unmerged multi7 per-dataset ledger and other known gaps listed in `research/v118/LEDGER_INVENTORY.md`. | `research/experiments.json`, `research/v118/baseline_metrics.json` |
| Contracts | **17** JSON Schemas (all `x-version` 1 except `risk_event`, 2 since v1.14) mirrored by **22** typed Python contracts and **18** runtime-checkable Protocols; one pinned instance each | `schemas/`, `python/src/iap/contracts/`, `tests/golden/expected_contracts_examples.json` |
| Python reference ports proven by the ports' own goldens | risk: `expected_risk_decisions.json` exact, audit JSONL + snapshot **byte-identical**; execution: `expected_replay_fills.json` **bit-identical** | `python/tests/test_risk_golden.py`, `python/tests/test_execution_golden.py` |
| MVP golden run (`python -m iap.mvp run`, seed 12345) | **15,805** events · **800** decisions · **231** parent orders · **163** fills · P&L **−82.84 USD** (cost-negative: +0.0245 bps alpha vs −0.4367 bps execution cost) · trace digest `a7f1ee60…` reproduced by run-twice and replay-from-capture | `tests/golden/expected_mvp.json` |
| Adaptive deployment study | 4 refit policies × 10 alphas; 88 drift-triggered refits; FX01 retired under every policy; 19 of 40 deployments make no trade, none ends above zero | `research/adaptive_reports/ADAPTIVE_REPORT.md` |
| Bundled dataset | 2 synthetic sessions, 19 instruments, 308,975 normalized events (`data_version` `116b7787…`; equity flow runs to the close since v1.4.0) | `data/normalized/qc_report.json` |
| Feature emission | 213,021 vectors at 100 ms cadence | `data/features/features_summary.json` |
| Rust feature engine from Python (v1.11, CI runner ubuntu-24.04, wall clock; not colocation) | about 5,250 events/s (Python reference, full 205-feature engine) against about 790,000 events/s (Rust, the 45 native slots): **150x** on the golden vectors, 123x on a seeded equity day at 100 ms; not a pipeline speed-up, the other 160 features still run in Python | `API_FEATURES.md` §7.1, `rust-pyo3` CI job summary |
| LLM agent behaviour evals (v1.11) | 4 of 4 pass mocked in CI, each control-bearing eval also failing with its control removed; live 2026-10-10 on `claude-haiku-5-5`: 4 of 4, estimated $0.0096 (the model behaved well, so the live run did not stress the controls) | `python -m iap.llm.evals`, GOVERNANCE.md §2b |
| C++ tick-to-trade (v1.12, shared CI runner AMD EPYC 7763, no pinning, `steady_clock`; not colocation) | golden `eq_mbo`: p50 **1,279 ns**, p99 **2,559 ns**, p99.9 11,263 ns, max 33,112 ns; generated 100,000-event day: p50 1,407, p99 1,727, p99.9 12,287 ns; stage p50s decode 271, book 83, features 735, alpha 135, decision 30 ns. In-process, decoded frame to order intent: no network, no kernel bypass, no C++ risk engine | `benchmarks/results_tick_to_trade.md`, `benchmarks/RESULTS.md` |
| Deflated Sharpe (v1.12, opt-in `v4`) | PSR / DSR / minimum track record with N = the ledger's distinct configurations (`dsr_at_looks` at the raw looks beside it); CPCV paths and PBO; the published worked example reproduced, DSR **0.9004** (SR0 0.1132) | `iap.validation.deflated`, `iap.validation.cpcv`, COOKBOOK 56 |
| C++ hot path (2-CPU container, Intel Xeon 2.10 GHz, no pinning, `steady_clock`, cache-resident mean; not colocation) | IAP1 decode 184.1 ns/event (CRC-32 verified); book update 26.4 ns; replay 27.2M events/s; one 5.6 KB decision trace serialised in 31.7 µs off the event loop | `benchmarks/results_cpp.md` |

**No latency edge; benchmarks are not colocation numbers.** Every latency or speed figure above
was measured on a shared CI runner or a small container with `steady_clock` and no CPU pinning,
in-process, with no network, NIC, kernel bypass or venue round trip
([benchmarks/RESULTS.md](benchmarks/RESULTS.md)). They measure the code's cost, not a competitive
position; nothing here claims a speed advantage at any venue.

**What the signals are.** The alphas are **execution inputs, not strategies**: B7 found no
cell where signal-aware execution saved the 2 bp minimum
([research/preregs/B7_CLOSED_v117.md](research/preregs/B7_CLOSED_v117.md)), the net-of-cost screen
passes 0 of 22, and the v1.16 monetisation study found no taker, capacity, quoting or DeepLOB edge
([research/v116/MONETISATION_P0-6.md](research/v116/MONETISATION_P0-6.md)). They are studied as
inputs to child-order timing and routing, and measured as such.

The honesty is the point (spec §32): of 24 alphas on the bundled synthetic
data, **none** survives every promotion gate, and none makes money net of
modelled costs at 1x. Every review since 2026-09-20 moved numbers toward a
harsher reading, never a kinder one; what each one changed is in
[docs/README_HISTORY.md](docs/README_HISTORY.md) §2. **All bundled market
data is synthetic** (seeded generator,
`python/src/iap/marketdata/generator.py`); real files you obtain yourself
(Nasdaq TotalView-ITCH 5.0, LOBSTER) go through the same pipeline
([docs/REAL_DATA.md](docs/REAL_DATA.md)). The repository map is in
[docs/README_HISTORY.md](docs/README_HISTORY.md) §3.

## Quick start

Prerequisites are already the environment baseline: Python 3.11 (+ pyarrow),
g++ 13 / CMake / GoogleTest, Rust (the toolchain is pinned to 1.99.0 in
`rust/rust-toolchain.toml`). Java 21 is needed only for the archived Java
engine (`archive/java/`, no Maven; see [docs/BUILD_NOTES.md](docs/BUILD_NOTES.md)).

```bash
# 1. Generate the seeded synthetic dataset (raw → normalized → Parquet + QC)
cd python && PYTHONPATH=src python3 -m iap.marketdata && cd ..

# 2. Run each language's test suite
cd python && PYTHONPATH=src python3 -m pytest -q && cd ..
cd cpp    && bash build.sh && ctest --test-dir build --output-on-failure && cd ..
cd rust   && cargo test && cd ..

# 3. Or all three + the repo-level integration/replay suites + the parity table
bash tests/harness/run_all.sh              # add --golden-only for the fast parity check
python3 -m pytest -q tests/integration tests/replay   # the two repo-level suites alone

# 4. Run the whole platform loop on one instrument, fully traced (docs/MVP.md; under a minute)
cd python && PYTHONPATH=src python3 -m iap.mvp run                    # -> ../data/mvp/<run_id>/
PYTHONPATH=src python3 -m iap.mvp verify                              # run twice, identical bytes
PYTHONPATH=src python3 -m iap.mvp replay --run ../data/mvp/<run_id>   # same digest from the capture
PYTHONPATH=src python3 -m iap.mvp explain --run ../data/mvp/<run_id> 5   # one order's chain
cd ..

# 5. Research → ledger → lifecycle → store
cd python && PYTHONPATH=src python3 -m iap.research run --alpha EQ03 --horizon 1s   # one ledgered experiment
PYTHONPATH=src python3 -m iap.lifecycle bootstrap --dry-run           # 24 alphas -> CANDIDATE, 0 beyond
PYTHONPATH=src python3 -m iap.lifecycle status                        # the registry table
PYTHONPATH=src python3 -m iap.store build                             # SQLite index of every artefact
cd ..

# 6. Run the research pipelines (features → alphas → ML → TCA)
cd python && PYTHONPATH=src python3 -m iap.features && cd ..     # ~1 min
PYTHONPATH=python/src python3 research/alpha_reports/run_all.py  # 24-alpha promotion report (default methods, v2)
PYTHONPATH=python/src python3 research/alpha_reports/run_all.py --methods legacy_v1 --out-dir /tmp/legacy_v1   # the v1.4.0 report, reproduced
PYTHONPATH=python/src python3 research/ml_reports/run_ml.py      # gated ML + meta-labeling
PYTHONPATH=python/src python3 research/adaptive_reports/run_adaptive.py  # adaptive policy study
cd python && PYTHONPATH=src python3 -m iap.tca && cd ..          # TCA report

# 7. Paper trading: the Java platform vertical (archive/java/paper.sh) is archived,
#    frozen at v1.14.0 and retired from the deployment in v1.17 (archive/java/README.md);
#    the MVP (step 4) is the maintained loop and `python -m iap.metrics serve` the
#    /metrics process the deployment scrapes.

# 8. Validate the deployment the way CI does, and the build plan
python3 tests/harness/check_deployment.py
python3 tests/harness/check_headline_numbers.py   # every number in this README vs its artefact
python3 tools/github/create_issues.py --dry-run   # the epics/issues plan (docs/EPICS.md)

# 9. Verify every signed pre-declaration against the pinned trust root (v1.18; docs/governance/TRUST_ROOT.md)
PYTHONPATH=python/src python3 tools/verify_signatures.py
```

## Cross-language parity (the `tests/harness/run_all.sh` table, counts from CI)

Since v1.15 the table has three language rows: the Java engine is archived
under [archive/java/](archive/java/README.md), frozen at v1.14.0 (575 tests,
124 golden at that tag), and no longer built or tested in CI. The counts below
are from main CI run 40015853764 (2026-10-23, v1.18.0 at 23b69fb2); CI derives
them from the jobs' own artefacts (`tests/harness/parity_counts.py`): python is
the fast job plus the `python-slow` job (3,666 + 396 passed; 21 + 3 skipped, 1
xfailed), cpp the ctest total, rust the passes of `cargo test --workspace`
(and `check_headline_numbers.py` re-counts 376 `#[test]` functions with 75
golden over 12 targets in the source tree).

```
===================== cross-language parity table =====================
language | tests passed | golden passed  | time   | status
---------+--------------+----------------+--------+-------
python   | 4062         | 217            |    -s | PASS
cpp      | 333          | 74             |    -s | PASS
rust     | 376          | 75             |    -s | PASS
integration | 35           | -              |    -s | PASS
replay   | 6            | -              |    -s | PASS
deployment | -            | -              |    -s | PASS
numbers  | -            | -              |    -s | PASS
=======================================================================
deployment checks: 29 passed, 0 failed, 0 skipped
headline numbers: all headline numbers match their artefacts
(a '-' count means the suite did not run in this mode, or has no golden
 group (integration/replay); '?' means it ran but its count could not be
 parsed — a '?' or FAIL anywhere fails the run.)
>> PARITY OK — all languages passed (full suites).
```

The policy of [docs/POLYGLOT.md](docs/POLYGLOT.md) (v1.11.0) froze 20 of the
duplicated language copies and retired none, so these counts are unchanged by it.
What the counts contain (which goldens, which suites, why the time column is
blank) is explained in [docs/README_HISTORY.md](docs/README_HISTORY.md) §4;
the six-level testing strategy with exact commands is in
[tests/README.md](tests/README.md). The `numbers` row is
`tests/harness/check_headline_numbers.py`, which re-derives every headline
figure in this README from the artefact that produces it.

## Documentation index

| document | what it covers |
|---|---|
| [LEARN.md](LEARN.md) | textbook walkthrough: microstructure, generator, book, features, honest alpha research, ML/meta-labeling, portfolio, risk, execution, TCA, parity, latency economics, adaptability, contracts & Protocols, the Python risk/execution reference, the 7-state lifecycle, the decision trace, the data model, the MVP walkthrough with its honest numbers, the v1.3.0 review as six case-study chapters (fail-closed risk bugs, simulator realism, statistical power, gate gaming, crash consistency, supply-chain hygiene), pitfalls, interview Q&A, combining signals, adverse selection, five v1.9-v1.10 case studies (why taker-only research cannot pay at 1 s, the FOMC sampling flaw and day-clustered inference, the 2026 holdout as pre-registration, what the synthetic maker/quoting results show, governance keys and code hashes), and four from v1.11 (parity proves agreement, not correctness: the snapshot-burst bug; why an LLM must never compute a number; freezing copies instead of deleting them; model registries as evidence), and four from v1.12 (why a Sharpe must be deflated; what tick-to-trade does and does not measure; an underpowered result is not a negative result, AUC01; feedback loops in passive execution), and one from v1.13 (what an independent review found, and how it was triaged) |
| [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) | how the quant, algo and AI sides work — a guided explanation for a newcomer: the pipeline on one page, the research statistics and gates, the execution algorithms and simulator rules, the fail-closed risk engine, the ML layer with its negative results, the LLM/agent boundary (what exists, what is backlog, what would be theatre on this data), determinism and replay; since v1.10 also the real-data path and the `v3` validity bundle, the maker path (calibrate, post or quote, decompose), auctions, optimal execution and the governance chain; since v1.11 the LLM research agent and its controls, the Rust feature engine from Python, the model registry and the polyglot policy; every section ends with where to look and a command that runs |
| [COOKBOOK.md](COOKBOOK.md) | 200 task-oriented recipes with runnable commands (39-61: maker backtest, quoting, optimal execution, auctions, parallel features, the `v3` validity block, calibrating and backtesting a real session, signed pre-registration, the Almgren-Chriss frontier, the Rust feature engine from Python and building its wheel with maturin, the model registry, shadow mode and week-by-week monitoring, the LLM research agent, its evals and a guarded session with the scripted client, the `POLYGLOT-OVERRIDE` workflow, combinatorial purged CV, PBO and the deflated Sharpe ratio, tick-to-trade percentiles, extended features and event clocks, the multi-venue router, alert routing and SLOs, the fill hazard with post-only and markout feedback; 62-65: deflating a study's Sharpe, a maker run combining extended features and the hazard, the feedback loop on a toxic regime, reading the SLO burn rates; 66: planning the R7 data load; 67-68: regulatory fees on a round trip, the governance checks run locally; 69-74 (v1.14): verifying the risk audit hash chain, the 15c3-5 pre-trade controls, a v1.13 feature store next to a v1.14 one, the `*_v113` method bundles, per-touch-order adverse selection, maintainer trust roots; 75-100 (v1.15-v1.16): parent-order impact and the residual impact fit, POV exact participation, v3 prereg realism, router opt-ins, multiple-testing and beta-stability diagnostics, ledger and dataset merges, MVP end-of-day flatten, release bundles, fork boards, the quoting v2 and monetisation studies, conformal sizing, drift, the real-session MVP, DeepLOB, cost sensitivity, time-shift triage, covariance shrinkage, board verification, model cards; 101-140 (v1.17): the shortfall benchmark with signal-aware urgency and pacing, then one recipe per v1.17 opt-in: IEX HIST ingest, process-and-delete, breadth book, fading, queue cancels, markout decomposition, linear_z_v2, regime, GBM, meta-label split, conformal sizing, zoo gates, the agents and spend ledger, docs checks, reproducible build, latency guards, sharenorm, per-session ADV, TRADE_BUST) |
| [docs/REAL_DATA.md](docs/REAL_DATA.md) | real historical data: what `python -m iap.marketdata ingest` reads (Nasdaq TotalView-ITCH 5.0, LOBSTER), how to obtain files yourself (nothing is bundled), the commands from a downloaded file to an alpha report, the mapping table to canonical events, the point-in-time security master and corporate-actions table, known limitations, and what a first real-data study can and cannot conclude |
| [docs/RESEARCH_VALIDITY.md](docs/RESEARCH_VALIDITY.md) + [research/power/POWER_REPORT.md](research/power/POWER_REPORT.md) | the corrected research methods (the defaults since v1.5.0, each with its named legacy rule), the research store under parallel writers, gate eligibility; the planted-signal power study of the validation chain |
| [CHANGELOG.md](CHANGELOG.md) | release notes, newest first (v1.18.0: the measurement instrument and recorded negative results; v1.17.1: the B8 out-of-sample negative and the release-workflow fix; v1.17.0: execution quality and breadth; v1.16.0: monetisation; v1.15.0: R7 confirmed, the MVP on a real ITCH day, Java archived; v1.14.0: the correctness release; v1.13.0: review-driven hardening; v1.12.0: R7 statistics, tick-to-trade, the multi-venue router; v1.11.0: the Rust feature engine via pyo3, the polyglot policy, the model registry, the LLM research agent and its evals; v1.10.0: skewed quoting, auctions, optimal execution, governance G1-G4; v1.9.0: research validity options, parallel features, maker economics; v1.8.0: the 2026 holdout and the ingest memory fix; v1.6.0-v1.7.2: real data, the agent layer, signal combination, real-data power; v1.5.0: the corrected research methods become the defaults, every old rule keeps a legacy name, every dataset-derived artefact regenerated; v1.4.0: the generator's equity flow calibration fixed so flow reaches the close, and every dataset-derived artefact regenerated; v1.3.0: fail-closed risk, simulator fill rules, paper-platform safety, governance and deployment hardening, research validity) |
| [docs/MVP.md](docs/MVP.md) | the executable MVP (`python -m iap.mvp run / replay / verify / explain`): one deterministic, fully traced trading loop on a synthetic equity — the loop module by module, the §11.4 wiring rules with code references, the determinism contract, the incident replay flow, the honest golden-run results (cost-negative) with the realized-IC audit, and the success-criteria table |
| [docs/LIFECYCLE.md](docs/LIFECYCLE.md) | the 7-state promotion lifecycle: states, the 17-edge transition table, the 20 gates with config keys and defaults, evidence documents, registry and transition-log formats, the bootstrap result (23 CANDIDATE / 0 beyond), the golden, the Java/Rust ports, the RETIRED-is-observational caveat |
| [docs/DECISION_TRACE.md](docs/DECISION_TRACE.md) | the decision trace: the record, ids, canonical JSON rules, the stream digest with its known answers, sinks, the pinned `explain()` block, store views, emission points in Python / Java / C++ / Rust, incident replay |
| [API_CONTRACTS.md](API_CONTRACTS.md) | the contract layer: 22 typed contracts field by field, ids and canonical JSON, validation, the 18 Protocols and what satisfies them, versioning, the 17-schema index |
| [docs/AGENTS.md](docs/AGENTS.md) | the agent roster (v1.17 WS-E): models, caps, what the LLM may and may not do, the CLIs |
| [API_AGENTS.md](API_AGENTS.md) / [API_LLM.md](API_LLM.md) / [API_PAPER.md](API_PAPER.md) | `iap.agents` (governance, the three WS-E agents, evals), `iap.llm` (loop, budget, spend, verify) and `iap.paper` (forward paper trade), checked by `tests/harness/check_docs.py` |
| [API_TRADING.md](API_TRADING.md) | the Python reference ports of the hard risk engine (`iap.risk`) and the execution stack (`iap.execution`): public APIs, golden parity statements, what is pinned about each port |
| [docs/ROADMAP.md](docs/ROADMAP.md) | the six-week plan (Phase 0 → Week 6) and Phase 2/3 mapped to what exists with evidence, what is backlog, the MVP success criteria; one section per release, §3.11 is v1.16, §3.12 v1.17.0, §3.13 v1.17.1 and §3.14 v1.18.0 (delivered, not run or dropped, moved to v1.19) |
| [docs/research/](docs/research/) | research write-ups: [R7_RESULTS.md](docs/research/R7_RESULTS.md) (the confirmatory result), [R7_POWER.md](docs/research/R7_POWER.md), [MVP_REAL_20191018.md](docs/research/MVP_REAL_20191018.md) (the MVP on a real day), [FX11_FINDING.md](docs/research/FX11_FINDING.md), [AGENT_EVAL_LIVE.md](docs/research/AGENT_EVAL_LIVE.md), [MEMORY_REVIEW_v116.md](docs/research/MEMORY_REVIEW_v116.md); the v1.16 study in [research/v116/MONETISATION_P0-6.md](research/v116/MONETISATION_P0-6.md) |
| [docs/R7_DATA_PLAN.md](docs/R7_DATA_PLAN.md) | the R7 data plan: unseen sessions, tiers, download and ingest |
| [docs/POLYGLOT.md](docs/POLYGLOT.md) | which language copy of each duplicated component is canonical and which is frozen (v1.11.0, plan E3): inventory, golden pins, consumers, the `POLYGLOT-OVERRIDE:` rule enforced by `tests/harness/check_polyglot_policy.py` (COOKBOOK 53) |
| [docs/governance/GOVERNANCE.md](docs/governance/GOVERNANCE.md) §2b | the LLM research agent's controls (v1.11.0, AI1-AI2): tool allowlist, a signing key the model never sees, pre-registration caps, the numbers rule, budgets, persisted transcripts, the evals and what they do not cover |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | system design, per-language responsibilities, contracts, the research → trading → execution → adaptive loop with lifecycle and trace, determinism, golden topology, hot-path notes, observability, the MVP vertical, deployment, the AI / agent boundary (what is pinned, what exists, what is backlog), failure modes and fail-closed design, the research-store concurrency model, and the distance to a production system |
| [docs/DIAGRAMS.md](docs/DIAGRAMS.md) | all twenty-nine architecture diagrams on one page (pipeline, golden topology, paper trading, responsibility matrix, risk decision flow, queue-position model, data model, lifecycle state machine, decision-trace chain, MVP loop; and, since v1.3.0, the fail-closed risk branches, the simulator fill/queue flow, the paper-platform checkpoint commit point and resume, the admin kill latch, the research run with ledger lock and eligibility, the power study, the CI/release pipeline, the deployment topology, the agent layer as planned at v1.3.0; and, since v1.9-v1.10, the simulator calibration flow, the maker and quoting P&L decomposition, the auction pipeline, the governance (prereg, anchoring, signing) sequence, the v3 validity flow and the v1.9-v1.11 roadmap; and, since v1.11, the LLM agent loop with its controls, the model registry and shadow flow, the polyglot canonical/frozen map and the Rust/Python feature backends) |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | the relational data model (`schemas/sql/iap_v2.sql`, SQLite + PostgreSQL): every table, the dataset- and bundle-scoped views, portability rules, how the store indexes the flat-file artefacts, query cookbook |
| [docs/index.html](docs/index.html) + [docs/GITHUB_PAGES.md](docs/GITHUB_PAGES.md) | the GitHub Pages landing site and how to publish it (Settings → Pages → main branch, /docs folder) |
| [docs/SPECIFICATION.md](docs/SPECIFICATION.md) | the governing institutional specification (verbatim) |
| [PLATFORM_CONVENTIONS.md](PLATFORM_CONVENTIONS.md) | binding conventions: types, serialization, determinism, book semantics, golden rules, trading contracts (§11: risk engine, execution simulator, SOR/algos, paper wiring, currency), platform/deployment (§12), contracts / lifecycle / trace / data model (§13) |
| [docs/SCENARIOS.md](docs/SCENARIOS.md) | real-life scenarios (feed gaps, halts, clock regressions, loss latches, re-arms, FX notionals, lifecycle demotions, trace replay mismatches, NO_ROUTE, …) → pinned behaviour → contract clause → the tests in each language |
| [API_CORE.md](API_CORE.md) / [API_FEATURES.md](API_FEATURES.md) / [API_ALPHA.md](API_ALPHA.md) / [API_PORTFOLIO_TCA.md](API_PORTFOLIO_TCA.md) / [API_ADAPTIVE.md](API_ADAPTIVE.md) | the five original port contracts (API_CONTRACTS.md and API_TRADING.md above make seven) |
| [docs/BUILD_NOTES.md](docs/BUILD_NOTES.md) | per-language build/test commands; the no-Maven rationale and pom-equivalent table |
| [docs/papers/INDEX.md](docs/papers/INDEX.md) | six flagship research papers/case studies (spec §28) with dated errata |
| [research/alpha_reports/REPORT.md](research/alpha_reports/REPORT.md) | the honest 24-alpha promotion report |
| [research/ml_reports/ML_REPORT.md](research/ml_reports/ML_REPORT.md) | gated model comparison + meta-labeling (incl. the crossed-book artifact story) |
| [research/adaptive_reports/ADAPTIVE_REPORT.md](research/adaptive_reports/ADAPTIVE_REPORT.md) | the honest adaptive-deployment study: static vs scheduled vs drift-triggered refits, lifecycle retirements, and what two sessions cannot prove |
| [research/tca/TCA_REPORT.md](research/tca/TCA_REPORT.md) | simulated parent-order TCA |
| [research/experiments/README.md](research/experiments/README.md) | the ExperimentRunner's spec / result documents and the fifteen committed experiments (five under the default methods, five under the rules up to v1.4.0, five kept from the v1.3.0 dataset) |
| [benchmarks/RESULTS.md](benchmarks/RESULTS.md) | benchmark index; C++ table in [results_cpp.md](benchmarks/results_cpp.md) + methodology |
| [docs/runbooks/](docs/runbooks/) | data pipeline, backtest, paper trading, kill-switch incident, incident replay, alerting, job watchdog and Nasdaq-watch runbooks |
| [docs/governance/](docs/governance/) | governance, reproducibility, security, maintainer trust roots, repository settings (branch protection, required checks) |
| [docs/EPICS.md](docs/EPICS.md) + [CONTRIBUTING.md](CONTRIBUTING.md) | the build plan as epics/issues with honest done / in-progress / backlog status (generated from `tools/github/issues.yaml`; E36 is v1.16); how to contribute |
| [archive/java/README.md](archive/java/README.md) | the archived Java engine: state at v1.14.0, what archiving means, how to restore it |
| [schemas/README.md](schemas/README.md) / [schemas/FORMAT.md](schemas/FORMAT.md) / [schemas/MIGRATIONS.md](schemas/MIGRATIONS.md) | the 17-schema index + the SQL DDL; normative wire layout (JSONL + IAP1 binary); every versioned change |
| [tests/README.md](tests/README.md) | the six-level testing strategy with exact commands and the golden inventory |
| [deployment/grafana/README.md](deployment/grafana/README.md) | dashboards and observability stack (incl. the trace metrics) |
| [python/src/iap/README.md](python/src/iap/README.md) | the Python package map, module by module |
| [docs/REPRODUCTION.md](docs/REPRODUCTION.md) | v2.0 stage 3 P1: the pinned reproduction image (`Dockerfile.repro`, digest in `deployment/docker/IMAGE_DIGEST`), the in-image reproduction of B8 v2 and the REL-08 scorecard with its exact commands, the status vocabulary ("reproduced in the pinned image by CI" is not "reproduced independently") and the P2 hand-over |
| [docs/SHOWCASE.md](docs/SHOWCASE.md) | the three-minute read for a newcomer: what is proven, what is not, the re-derived numbers, one diagram, a ten-minute quick start |
| [docs/EXECUTION_QUALITY.md](docs/EXECUTION_QUALITY.md) | v1.17 execution quality: the implementation-shortfall benchmark and its depth-cap finding, signal-aware urgency and pacing, the realism layer |
| [docs/PORTFOLIO_BREADTH.md](docs/PORTFOLIO_BREADTH.md) | v1.17 breadth: the universe rule, the cross-sectional book through the optimiser, the breadth report and the breadth holdout rule |
| [docs/SIGNAL_RESEARCH.md](docs/SIGNAL_RESEARCH.md) | v1.17 signal research: the statistics revision, `linear_z_v2`, long horizons, regime model and gate, GBM, meta-labelling split, conformal sizing, zoo gates |
| [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) | free Nasdaq sample days and IEX HIST (TOPS / DEEP), process-and-delete, the download approval rule |
| [docs/release/v1.18.0_RELEASE_NOTES.md](docs/release/v1.18.0_RELEASE_NOTES.md) | v1.18.0 release notes: negative and inconclusive results first, the REL item status table, the consolidated minimum detectable effects, what shipped, what is not in the release, the open gates (F3, F7, reviewer memo (c); F5 partly met); with [RESULTS_INDEX.md](docs/release/RESULTS_INDEX.md) (result artefacts with prereg, signing commit and sha256), [FOUNDATION_COMPLETE_v1.18.md](docs/release/FOUNDATION_COMPLETE_v1.18.md), [REVIEWER_CLOSEOUT_2026-10-23.md](docs/release/REVIEWER_CLOSEOUT_2026-10-23.md) and [F7_REPRODUCTION_GUIDE.md](docs/release/F7_REPRODUCTION_GUIDE.md) (a desk-check draft; nothing in it was run) |
| [docs/MONITORING_POLICY.md](docs/MONITORING_POLICY.md) / [docs/FACTOR_RISK.md](docs/FACTOR_RISK.md) / [docs/PORTFOLIO_LIMITS.md](docs/PORTFOLIO_LIMITS.md) / [docs/SIM_TO_REAL.md](docs/SIM_TO_REAL.md) | v1.18 paper-path layers: the monitoring policy with its UNKNOWN state (REL-16), the factor risk model, the enforced portfolio limits and kill switch (REL-17), and the paper recording / replay comparison / fill tape test with the Level 1 / Level 2 statement (REL-05) |
| [docs/governance/TRUST_ROOT.md](docs/governance/TRUST_ROOT.md) / [docs/governance/ANALYSIS_IDENTITY.md](docs/governance/ANALYSIS_IDENTITY.md) / [docs/governance/DATA_ACCESS_PATHS.md](docs/governance/DATA_ACCESS_PATHS.md) | v1.18 research authorisation: the pinned trust root and `tools/verify_signatures.py`, the analysis identity a signed pre-declaration binds, and the data-access inventory behind the fail-closed holdout registry |
| [docs/governance/SEALED_DATA.md](docs/governance/SEALED_DATA.md) / [docs/governance/RUNNER_THREAT_MODEL.md](docs/governance/RUNNER_THREAT_MODEL.md) / [docs/governance/RUNNER_SETUP.md](docs/governance/RUNNER_SETUP.md) | v2.0 stage 2: the sealed identities and partitions, the OS-authenticated sealed-data service and its registration ledger, the registered runner's six properties and the custody label "mechanically isolated", and the owner's runner install steps |
| [docs/governance/DD1_INGESTION_RUNBOOK.md](docs/governance/DD1_INGESTION_RUNBOOK.md) | v2.0 stage 2 DD1: the owner's sequence for ingesting the free daily route as `iap-runner` into the four calendar partitions (`tools/ingest/dd1_daily.py`, the sealed service's `ingest` endpoint, `tools/admin/install_sealed_services.ps1`), the five DD1 tests on the development partition, what "complete" means |
| [docs/README_HISTORY.md](docs/README_HISTORY.md) | the sections moved out of this README: release-by-release status, the reviews that moved the numbers, the repository map, what the parity counts contain, [real-world usage notes](docs/README_HISTORY.md#real-world-usage-notes) (scope, units and out-of-scope items for a live deployment) and the [references](docs/README_HISTORY.md#references) (the literature and standards the platform implements) |

## License

MIT License, Copyright (c) 2026 Ashish Jha — see [LICENSE](LICENSE). That
covers this repository's own code. The toolchain, the test libraries and the
container images pull in third-party software under its own terms (JUnit EPL-1.0,
Eigen MPL-2.0, GoogleTest BSD-3, OpenJDK GPL-2.0-with-classpath-exception,
Prometheus Apache-2.0, Grafana AGPL-3.0) — [NOTICE](NOTICE) records what,
where, and under which licence.

## Disclaimer

This platform is for **education and research engineering practice only**. All
market data is synthetically generated; no result here is a claim about real
markets, and nothing in this repository is investment advice or a solicitation
to trade. The honest-reporting standard (spec §32) exists precisely because
research truth — including negative results — is the product.
