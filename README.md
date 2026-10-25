# Intraday Alpha Platform

We built this platform to answer one question carefully: does a systematic equity signal make
money after costs, and how would we know? It takes point-in-time daily and intraday data through
forecasts, portfolio construction, simulated execution and a cash-reconciled ledger, and it
confirms a hypothesis only by a pre-registered, single-shot test on data the developer cannot
read. Every result can be rebuilt from a hash, and every claim carries a stated grade of
evidence. As of 2026-10-25 no signal is confirmed: the machinery is built and tested on synthetic
data, and the real data has not yet been ingested.

**Start here:** [docs/LEARN.md](docs/LEARN.md) (the method and the vocabulary),
[docs/COOKBOOK.md](docs/COOKBOOK.md) (commands that run today), [docs/ARCHITECTURE_v2.md](docs/ARCHITECTURE_v2.md)
(where things are), [docs/governance/](docs/governance/) (who may do what). The v2.0 programme's
plan of record and unified specification are the owner's documents, kept outside the repository;
the documents inside cite them by section.

## The three evidence tracks and the seven evidence classes

| track | what it tests | its gate | sealed data it never reads during development |
|---|---|---|---|
| **A** | H1, one frozen residual-reversal hypothesis ([research/v119/H1_ALPHA_IMPROVEMENT_SPEC.md](research/v119/H1_ALPHA_IMPROVEMENT_SPEC.md)) | R0, the frozen confirmatory specification ([python/tests/spec/CONFIRMATORY_TEST_SPEC.md](python/tests/spec/CONFIRMATORY_TEST_SPEC.md)) | the H1 prospective window (`iap-sealed-a`) |
| **B** | daily families D1 to D5, intraday families H2 to H4, and machine-learning candidates M0 to M4 | a validated confirmation profile per registration (U13) | the Track B test year (`iap-sealed-b`) |
| **C** | the combined multi-horizon book | a profile for the combined statistic (PF-3, deferred) | the Track C confirmation window (`iap-sealed-c`) |

Every performance statement in this repository carries exactly one of these classes
(U0 section 10; [docs/LEARN.md](docs/LEARN.md) section 6 explains each):

| class | meaning |
|---|---|
| independently confirmed | a registered single-shot decision executed under custody separate from the developer |
| mechanically isolated | the same, executed by the registered runner under the developer's sole custody |
| development-supported | positive economics on development data; not confirmed |
| exploratory | reported, no claim attached |
| rejected | the registered test or screen failed |
| data-ineligible | the data to test it does not exist on the route, or failed its suitability gate |
| pending | registered; the window is accruing or the run has not happened |

## Current status (2026-10-25)

**Nothing is confirmed.** No registered run has been executed and no independent party has
reproduced a result, so nothing on `main` is in the first two classes; the class of every result
so far is listed in [docs/REVIEW_PACK.md](docs/REVIEW_PACK.md) section 0. Every v1.x signed run
was negative, inconclusive or underpowered, and the v1.18 known-null calibration study failed and
was rejected by the outside reviewer as a gate.

**Built and tested on `main`** (synthetic fixtures in CI; nothing below has touched real data):

- the frozen R0 gate and its fail-closed implementation (`iap.validation.confirmatory`, the only
  constructor of a PROMOTE verdict), with its adversarial Monte Carlo results committed;
- data and feature contracts, the point-in-time universe and the leakage canaries (`iap.data`),
  and the five free-route source adapters;
- the shared engine: forecast interface and content-addressed registry (`iap.alpha`), projection
  and the two reconciling ledgers (`iap.portfolio`), the fill simulator with three cost columns
  (`iap.execution.sim`), the E5 report builders (`iap.reports`); the H1 reference implementation
  and the byte-for-byte equivalence test;
- the data-read choke point (`iap.core.guard`), the sealed-data service and the operating-system
  identities, the registered runner and its threat model, the pinned reproduction image, the
  ingestion job for the free daily route, and the data manifest with integrity verdicts;
- the v1.x intraday engines (order book, feature factory, execution simulator, risk engine,
  replay) in Python with C++ and Rust ports proven equal by golden vectors.

**Pending** (dated 2026-10-25): the owner's ingestion of the free daily route and the repository
settings for the sealed environment ([docs/governance/DD1_INGESTION_RUNBOOK.md](docs/governance/DD1_INGESTION_RUNBOOK.md),
[docs/governance/REPO_SETTINGS.md](docs/governance/REPO_SETTINGS.md) section 7); the first
registered run; the H1 prospective window; the combined-book profile PF-3. One pull request
was open on 2026-10-25 and is named where the documents depend on it: #389 (family
specifications, the research loop, the ML protocol). The H3 and H4 intraday families are data-ineligible on the free route and
D5 is ineligible on daily data; their specifications say so.

## Five-minute quick start

```bash
git clone https://github.com/AshJha0/intraday-alpha-platform.git
cd intraday-alpha-platform
python -m venv .venv-ci
.venv-ci/bin/pip install --require-hashes -r python/requirements-ci.txt      # Windows: .venv-ci\Scripts\pip
cd python && PYTHONPATH=src ../.venv-ci/bin/python -m pytest -q tests/leakage tests/portfolio tests/execution/test_sim_waterfall.py tests/reports tests/equivalence
```

That runs the v2.0 engine suite on its synthetic fixtures: the leakage canaries, the ledger
conservation invariants, the cost waterfall reconciling to the cash identity, the E5 report
builders and the H1 equivalence test (62 tests, about twenty seconds; CPython 3.11 or 3.12).
Everything it produces is a correctness check on fixtures and carries no evidence class. The
research loop on a family specification, once pull request #389 lands, is
[docs/COOKBOOK.md](docs/COOKBOOK.md) recipe 1. The full four-language parity table is
`bash tests/harness/run_all.sh` (Linux or WSL; needs the C++ and Rust toolchains of
[docs/BUILD_NOTES.md](docs/BUILD_NOTES.md)).

## Where to go next

| if you want to | read |
|---|---|
| understand the method: point-in-time data, selection accounting, the gate, the economic chain, evidence classes | [docs/LEARN.md](docs/LEARN.md) |
| run something that exists today | [docs/COOKBOOK.md](docs/COOKBOOK.md) |
| find a package, an import rule, an identity or the runner flow | [docs/ARCHITECTURE_v2.md](docs/ARCHITECTURE_v2.md), [python/src/iap/README.md](python/src/iap/README.md) |
| operate it day to day | [docs/RUNBOOK_v2.md](docs/RUNBOOK_v2.md), [docs/runbooks/](docs/runbooks/) |
| check what is enforced and by whom | [docs/governance/GOVERNANCE.md](docs/governance/GOVERNANCE.md), [docs/governance/TRUST_ROOTS.md](docs/governance/TRUST_ROOTS.md), [docs/governance/SEALED_DATA.md](docs/governance/SEALED_DATA.md), [docs/governance/RUNNER_THREAT_MODEL.md](docs/governance/RUNNER_THREAT_MODEL.md) |
| reproduce a committed result | [docs/REPRODUCTION.md](docs/REPRODUCTION.md) |
| know what data is used and under which licence | [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md), [docs/review/DATA_COVERAGE_FREE_ROUTE.md](docs/review/DATA_COVERAGE_FREE_ROUTE.md) |
| read the record: reviews, releases, defects, every result and its class | [docs/REVIEW_PACK.md](docs/REVIEW_PACK.md), [docs/release/](docs/release/), [docs/review/](docs/review/), [CHANGELOG.md](CHANGELOG.md) |
| see what every document is for and whether it is current | [docs/DOC_AUDIT_v2.md](docs/DOC_AUDIT_v2.md) |
| the v1.x engines and their contracts | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/DIAGRAMS.md](docs/DIAGRAMS.md), [API_CORE.md](API_CORE.md), [API_FEATURES.md](API_FEATURES.md), [API_TRADING.md](API_TRADING.md), [PLATFORM_CONVENTIONS.md](PLATFORM_CONVENTIONS.md), the frozen v1.x [LEARN.md](LEARN.md) and [COOKBOOK.md](COOKBOOK.md) |
| contribute | [CONTRIBUTING.md](CONTRIBUTING.md), [docs/POLYGLOT.md](docs/POLYGLOT.md) (which language copies are frozen), the pull-request template |
| the history: how the numbers moved, release by release | [docs/README_HISTORY.md](docs/README_HISTORY.md), [docs/SHOWCASE.md](docs/SHOWCASE.md) |

## How it is built, and who checked what

Python is the reference implementation of every semantics; the C++ and Rust copies implement the
same rules and golden tests prove the three equal. The Java layer of v1.x is archived at the git
tag `archive/java-v1.14.0` and is not built. The code, tests and documents were produced with
Claude (AI) assistance; the owner merges and signs, and an outside reviewer has written decisions
on file for the statistical protocols ([docs/release/](docs/release/)). Nothing here asks a reader
to trust that text: every number in a document names the artefact it comes from, and the CI jobs
`docs` and `deployment` re-derive the headline figures and fail on a dead link, a vanished symbol
or a stale status phrase.

## Cross-language parity (the `tests/harness/run_all.sh` table, counts from CI)

The test counts of the three live languages, kept here because `tests/harness/check_headline_numbers.py`
treats this table as the pinned record every other document is checked against. They are counts of
tests, not results.
Since v1.15 the table has three language rows: the Java engine is archived
under the git tag `archive/java-v1.14.0`, frozen at v1.14.0 (575 tests,
124 golden at that tag), and no longer built or tested in CI. The counts below
are from main CI run 40015853764 (2026-10-23, v1.18.0 at 23b69fb2); CI derives
them from the jobs' own artefacts (`tests/harness/parity_counts.py`): python is
the fast job plus the `python-slow` job (3,666 + 396 passed; 21 + 3 skipped, 1
xfailed), cpp the ctest total, rust the passes of `cargo test --workspace`
(and `check_headline_numbers.py` re-counts 376 `#[test]` functions with 75
golden over 12 targets in the source tree). Since v2.0 stage 11 (S2) the per-PR
`rust` job tests the `risk` and `features` crates only and the workspace runs
weekly (`weekly-structural.yml`), so a PR run's derived rust count is lower than
the 376 of this table (reported as drift, defect D-79); every crate's golden
target still runs per PR in the `golden` job.

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

## License and disclaimer

MIT, see [LICENSE](LICENSE) and [NOTICE](NOTICE). This repository is research software. It
places no orders, holds no capital and gives no investment advice; every result it reports is
labelled with its evidence class and, as of the date above, none is confirmed.
