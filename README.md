# Intraday Alpha Platform

> **Status, v2.0.0 (2026-10-26):** engineering-complete platform. **No confirmed alpha.** Where a
> signal predicts, it is smaller than the cost of trading it, and the free daily data failed its
> survivorship test (85 % of index leavers missing against a 2 % limit). Nothing on `main` is
> above *development-supported*.

We built this to answer one question carefully: does a systematic equity signal make money after
costs, and how would we know? The platform runs from raw market data to a cash-reconciled ledger,
counts every look it takes, and confirms a hypothesis only by a pre-registered single-shot test
on data the developer cannot read. The short version is [SUMMARY.md](SUMMARY.md); the full
account, results first, is [docs/REPORT.md](docs/REPORT.md).

## Three ways in

| if you are | start with | then |
|---|---|---|
| **a quant researcher** | [docs/REPORT.md](docs/REPORT.md) §2 (results) and §3 (method: evidence classes, the look ledger, pre-registration, leakage, costs) | [docs/RESEARCH_VALIDITY.md](docs/RESEARCH_VALIDITY.md), [research/v2/families/](research/v2/families/README.md), [docs/RESEARCH_NOTES.md](docs/RESEARCH_NOTES.md) |
| **a C++ / low-latency engineer** | [docs/POLYGLOT.md](docs/POLYGLOT.md) (three languages, one semantics) and [benchmarks/results_tick_to_trade.md](benchmarks/results_tick_to_trade.md) | [cpp/](cpp/), [docs/API_REFERENCE.md](docs/API_REFERENCE.md) (order book, codec, features), [docs/BUILD_NOTES.md](docs/BUILD_NOTES.md) |
| **an engineering lead** | [docs/REPORT.md](docs/REPORT.md) §6 (where we over-invested) and §7 (the DD1 incident) | [docs/ARCHITECTURE_v2.md](docs/ARCHITECTURE_v2.md), [docs/GOVERNANCE.md](docs/GOVERNANCE.md), [docs/DECISION_TRACE.md](docs/DECISION_TRACE.md), [CHANGELOG.md](CHANGELOG.md) |

## Results

Each row carries one of seven evidence classes ([docs/REPORT.md](docs/REPORT.md) §3.1). Negatives
come first because they are most of what we found.

| result | class | source |
|---|---|---|
| Free daily route (Stooq) fails all five DD1 data tests, so daily families D1 to D4 cannot be tested on it | rejected; D1 to D4 data-ineligible | [DD1 finding](docs/release/evidence/DD1_FREE_ROUTE_FINDING_2026-10-26.md) |
| Intraday EQ10 / EQ05 predict 1-second returns on 7 holdout sessions (IC +0.0838 / +0.0409); net P&L at 1x costs is zero | data-ineligible | [docs/SHOWCASE.md](docs/SHOWCASE.md) |
| Cost-aware taker, 480 grid cells: no trade in any cell | rejected | [docs/SHOWCASE.md](docs/SHOWCASE.md) |
| Two-sided quoting: loses USD 5.0k to 6.9k per session | rejected | [docs/SHOWCASE.md](docs/SHOWCASE.md) |
| Known-null calibration of the v1.18 confirmatory method: about 4.3 % false rejections against 2.5 % | rejected | [docs/REPORT.md](docs/REPORT.md) §2 |
| Platform: pipeline, risk, execution, simulator, ledgers, evidence machinery, three-language parity | development-supported (tests on fixtures; no return claim) | [docs/release/v2.0.0_RELEASE_NOTES.md](docs/release/v2.0.0_RELEASE_NOTES.md) §7 |

Measured on a shared CI runner (AMD EPYC 7763, no core pinning, `steady_clock`; not colocation):
C++ tick-to-trade p50 1,279 ns, p99 2,559 ns, decoded frame to order intent
([benchmarks/results_tick_to_trade.md](benchmarks/results_tick_to_trade.md)). These are in-process
figures: there is no latency edge, and none is claimed.

## Quick start

The one-command demo runs the whole loop on one synthetic instrument, explains one order and runs
the leakage canaries, in about a minute (64 s on the owner's Windows laptop, 2026-10-26):

```bash
python tools/demo.py              # needs the .venv-ci environment below; --replay adds the replay check
```

The engine test suite:

```bash
git clone https://github.com/AshJha0/intraday-alpha-platform.git
cd intraday-alpha-platform
python -m venv .venv-ci
.venv-ci/bin/pip install --require-hashes -r python/requirements-ci.txt      # Windows: .venv-ci\Scripts\pip
cd python && PYTHONPATH=src ../.venv-ci/bin/python -m pytest -q tests/leakage tests/portfolio tests/execution/test_sim_waterfall.py tests/reports tests/equivalence
```

That runs the v2.0 engine suite on synthetic fixtures: the leakage canaries, the ledger
conservation invariants, the cost waterfall against the cash identity, the E5 report builders and
the H1 equivalence test (about twenty seconds; CPython 3.11 or 3.12). It is a correctness check
and carries no evidence class. The full loop on one instrument is `python -m iap.mvp run`
([docs/MVP.md](docs/MVP.md)); every runnable command is in [docs/COOKBOOK.md](docs/COOKBOOK.md) (v2.0) and [COOKBOOK.md](COOKBOOK.md) (v1.x).

## Documentation

| for | read |
|---|---|
| the method and its vocabulary | [docs/LEARN.md](docs/LEARN.md) (v2.0); the v1.x intraday version is [LEARN.md](LEARN.md) |
| where things are | [docs/ARCHITECTURE_v2.md](docs/ARCHITECTURE_v2.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (v1.x engines), [docs/DIAGRAMS.md](docs/DIAGRAMS.md), [python/src/iap/README.md](python/src/iap/README.md) |
| interfaces | [docs/API_REFERENCE.md](docs/API_REFERENCE.md), [PLATFORM_CONVENTIONS.md](PLATFORM_CONVENTIONS.md) |
| operating it | [docs/RUNBOOK_v2.md](docs/RUNBOOK_v2.md), [docs/RUNBOOKS.md](docs/RUNBOOKS.md) |
| governance, security, reproduction | [docs/GOVERNANCE.md](docs/GOVERNANCE.md), [docs/REPRODUCTION.md](docs/REPRODUCTION.md) |
| data and licences | [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md), [docs/REAL_DATA.md](docs/REAL_DATA.md) |
| the record | [docs/release/](docs/release/v2.0.0_RELEASE_NOTES.md), [CHANGELOG.md](CHANGELOG.md); older plans, reviews and release notes are in [archive/docs/](archive/docs/) |
| contributing | [CONTRIBUTING.md](CONTRIBUTING.md) |

## How it is built, and who checked what

Python is the reference implementation of every semantics; the C++ and Rust copies implement the
same rules and golden tests prove the three equal. The Java layer of v1.x is archived at the git
tag `archive/java-v1.14.0`. The code, tests and documents were produced with Claude (AI)
assistance; the owner merges and signs, and an outside reviewer wrote decisions on the
statistical protocols. Every number in a document names the artefact it comes from, and CI
re-derives the headline figures and fails on a dead link or a stale status phrase.

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
blank) is explained in [archive/docs/README_HISTORY.md](archive/docs/README_HISTORY.md) §4;
the six-level testing strategy with exact commands is in
[tests/README.md](tests/README.md). The `numbers` row is
`tests/harness/check_headline_numbers.py`, which re-derives every headline
figure in this README from the artefact that produces it.

## License and disclaimer

MIT, see [LICENSE](LICENSE) and [NOTICE](NOTICE). This repository is research software. It
places no orders, holds no capital and gives no investment advice; every result it reports is
labelled with its evidence class and, as of the date above, none is confirmed.
