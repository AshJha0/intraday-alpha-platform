# Contributing

This repository holds four independent implementations of one pinned
semantics. Contributing means changing that semantics in the reference,
proving it with a golden vector, and re-matching every port — or leaving the
semantics alone and proving that too. The binding rules live in
[PLATFORM_CONVENTIONS.md](PLATFORM_CONVENTIONS.md); the review policy in
[docs/governance/GOVERNANCE.md](docs/governance/GOVERNANCE.md). This page is
the working procedure.

## 1. Branching

- `main` is protected: pull requests only, CI green
  (`.github/workflows/ci.yml`), the reviewers `CODEOWNERS` names for the
  paths touched.
- Branch names carry the area and the plan key or issue number:
  `feat/execution-X07-python-port`, `fix/orderbook-B03-reorder-window`,
  `docs/EPICS-refresh`, `research/EQ03-cost-threshold`.
- One concern per PR. A golden regeneration is its own commit with a message
  that starts `golden:` and names the tool; a schema bump is its own commit
  that starts `schema:` and names the `x-version` change.
- Rebase on `main` before requesting review; no merge commits inside a
  branch.

## 2. Build and test — the commands CI runs

From `docs/BUILD_NOTES.md` and `PLATFORM_CONVENTIONS.md` §9 (no Maven; JUnit4
is vendored at `/usr/share/java/junit4.jar`):

```bash
cd python && PYTHONPATH=src python3 -m pytest -q && cd ..
cd cpp    && bash build.sh && ctest --test-dir build --output-on-failure && cd ..
cd rust   && cargo test --workspace && cd ..
cd java   && bash build.sh && bash test.sh && cd ..
python3 -m pytest -q tests/integration tests/replay      # repo-level suites
python3 tests/harness/check_deployment.py --verbose      # deployment checks
```

Each language's full run must stay under 120 s; the repo-level suites well
under a minute.

## 3. The parity harness

```bash
bash tests/harness/run_all.sh              # every suite + the parity table; exit 0 iff all rows PASS
bash tests/harness/run_golden.sh           # = run_all.sh --golden-only: each language's golden group only
```

The table it prints is the one in `README.md` and every PR pastes it
(`.github/PULL_REQUEST_TEMPLATE.md`). Counts are truthful: `-` means "did not
run in this mode", `?` means "ran but unparseable", and both fail the run. A
test is never deleted, skipped or loosened to get green.

Tolerances are pinned once (`PLATFORM_CONVENTIONS.md` §5): IAP1 codec
parity is byte-exact (SHA-256); book states, checkpoints, risk decisions and
fills are exact integers; features, alphas, portfolio and TCA compare at
abs and rel 1e-9; adaptive PSI/KS at 1e-10 with exact refit booleans and
lifecycle state sequences; canonical-JSON lines, trace digests, the risk
audit / snapshot and the lifecycle registry are byte-identical; the 7-state
lifecycle golden is compared exactly, field by field. The 2026-09-20 table
reads python 1362 / cpp 266 / rust 298 / java 475 (golden 164/67/62/102),
`integration` 13, `replay` 4.

## 4. Golden regeneration protocol

A golden file changes only as a deliberate act, never as a side effect of a
refactor. The owning reference generates it; every other language matches
it (`docs/ARCHITECTURE.md` §6):

| golden | owner / tool | consumed by |
|---|---|---|
| codec SHA-256, book states, anomaly states, checkpoint, features, alpha, portfolio, TCA, backtest, adaptive | Python — `python/tools/make_golden.py`, `make_golden_features.py`, `make_golden_alpha.py`, `make_golden_anomalies.py`, `make_golden_tca.py`, `make_golden_adaptive.py` | C++, Rust, Java (each its own subset) |
| replay fills (`expected_replay_fills.json`) | C++ — `cpp/tools/make_replay_fills_golden.cpp` (refuses to overwrite) | Java `ReplayFillsGoldenTest`, Python `test_execution_golden.py` (`iap.execution`) |
| risk decisions, snapshot, audit (`expected_risk_*.json`, `expected_risk_audit.jsonl`) | Rust — `rust/risk/src/bin/make_risk_golden.rs` | Java `RiskGoldenTest`, Python `test_risk_golden.py` (`iap.risk`) |
| contract examples + pinned `explain` block (`expected_contracts_examples.json`) | Python — `python/tools/make_golden_contracts.py` (`--force`) | Java `TraceGoldenTest`, Rust `golden_trace.rs`, C++ `TraceGolden` |
| canonical JSON rules, float reprs, escapes, documents, trace id, trace digests (`expected_canonical_json.json`) | Python — `python/tools/make_golden_canonical_json.py` (`--force`) | Java `CanonicalJsonGoldenTest`, Rust `golden_canonical_json.rs`, C++ `CanonicalJsonGolden` |
| 7-state lifecycle scenarios + transition table (`expected_lifecycle.json`) | Python — `python/tools/make_golden_lifecycle.py` (`--force`) | Java `LifecycleGoldenTest`, Rust `golden_lifecycle.rs` |
| experiment golden frame (`expected_experiment_golden_frame.json`) | Python — `python/tools/make_golden_research.py` (`--force`) | Python only (research documents; no port) |
| MVP session (`expected_mvp.json`) | Python — `python/tools/make_golden_mvp.py` (`--force`) | Python (`test_mvp_golden.py`, from scratch and from the capture); the pin for a future port of the loop |

Steps, in order:

1. State in the PR *why* the semantics change and which contract clause it
   serves. If no clause changes, the golden does not change.
2. Change the reference implementation and its brute-force counterpart
   (`python/tests/bruteforce_book.py`, `bruteforce_features.py`, or the port's
   brute test); the generator tool cross-validates against it before writing.
3. Run the owning tool once. Commit the golden alone (`golden: …`).
4. Add a `schemas/MIGRATIONS.md` entry: which file, what changed, why, and
   the tool that produced it. If a schema field changed, bump its
   `x-version` in the same entry.
5. Re-match every port in the same PR; paste the parity table. A PR that
   leaves one language failing its golden group is blocked, not merged with
   a follow-up.
6. If a headline number in `README.md` moves, update it and re-run
   `python3 tests/harness/check_headline_numbers.py` — it re-derives the
   parity counts, the ledger denominator, the schema / contract / Protocol
   counts, the lifecycle registry and transition table, the MVP golden
   numbers and the benchmark table from their artefacts and fails the
   harness's `numbers` row otherwise.
7. Byte-parity goldens (canonical JSON, trace digests, the lifecycle
   registry, the risk audit and snapshot) are compared **exactly**: a port
   that is 1 ulp off in float parsing or prints `4.9E-324` for `5e-324` is
   wrong, not "within tolerance" (`PLATFORM_CONVENTIONS.md` §13.1).

## 5. Determinism rules for any change

`PLATFORM_CONVENTIONS.md` §3, checked in review and by the goldens:

- one RNG, SplitMix64, seeded from `configs/`; never `random`, `rand`,
  `std::mt19937` or `java.util.Random` on a shared path;
- no wall clock on a deterministic path — replay, features, fills, risk,
  reports; wall time appears only in throughput printouts;
- no iteration over unordered maps where the order reaches an output; sort
  keys explicitly;
- `int64 price_ticks` / `qty` / ns timestamps on every contract; floats only
  for genuinely real-valued research quantities at 1e-9;
- fail closed: unknown or degraded state produces no signal and no order.

## 6. Research changes and the promotion-gate rule

Research truth is the product (spec §32). Two rules are mechanical:

1. **Every look is ledgered.** An experiment is registered in
   `research/experiments.json` (via the ExperimentRunner — `python -m
   iap.research run`, which assigns `experiment_id` as the first 16 hex of
   the SHA-256 of the canonical spec and writes
   `research/experiments/<id>/{spec,result}.json`) *before* its result is
   read, and every report prints the denominator and the expected max |t|
   under the global null (865 looks / 70 configurations, max |t| ≈ 3.68 as
   of 2026-09-20). Runner entries are never de-duplicated against the
   report pipeline's entries even when the computation coincides: the
   denominator only grows.
2. **A PR cannot flip a verdict without the ledger entry.** A change to a
   verdict in `research/alpha_reports/*.json` (PROMOTE / ITERATE / REJECT),
   to a lifecycle state in `research/alpha_registry.json` /
   `research/lifecycle_transitions.jsonl`, or to an alpha's position in the
   RESEARCH → CANDIDATE → VALIDATING → PAPER → ACTIVE → WATCH → RETIRED
   machine (`docs/LIFECYCLE.md`) must cite the ledger entry id of the
   experiment that supports it, in the PR and in the `LifecycleTransition`
   document's reason. Reports are regenerated by their `run_*.py`, the
   registry by `python -m iap.lifecycle bootstrap`, never edited by hand;
   a manual `retire` / `reset` is a HUMAN edge with a non-empty reason.
   Reviewers reject a verdict change with no id; a CI check for it is
   tracked in the plan (issue `L05`).

Promotion requests use the **Alpha promotion request** issue form, one
transition per request, with evidence per gate
(`docs/governance/GOVERNANCE.md` §2). An alpha whose fitted sign contradicts
its `Economic rationale:` docstring can at best be ITERATE.

## 7. Epics and issues

`tools/github/issues.yaml` is the single source of truth for labels,
milestones, epics and issues; `docs/EPICS.md` is generated from it and
`tests/integration/test_github_issue_plan.py` fails when the two disagree.

- To add or change an issue: edit the YAML, then
  `python3 tools/github/create_issues.py --render-md docs/EPICS.md`, and
  commit both. Validate with `python3 tools/github/create_issues.py`
  (dry run, default).
- Status is a statement about the repository, not the plan: `done` cites
  the files and tests that prove it (the integration test checks the paths
  exist); `in-progress` lists planned paths (legitimately none between
  releases — 0 as of 2026-09-20); `backlog` says what would prove it done.
  Do not mark something done because its epic is.
- Push to GitHub with `python3 tools/github/create_issues.py --apply`
  (idempotent by exact title; `tools/github/README.md`).
- Filing by hand (the **Epic** / **Feature** / **Bug** / **Research
  experiment** / **Alpha promotion request** forms) is for work not yet in
  the plan; add it to the YAML in the same PR.
- Every PR names its issue (`Closes #N` / `Part of #N`) and, when it closes
  one, updates that issue's status and evidence in the YAML.

## 8. Code style (`PLATFORM_CONVENTIONS.md` §8)

| language | rules |
|---|---|
| Python 3.11 | `ValueError` / `RuntimeError` with messages; type hints and docstrings everywhere; the reference must be readable before it is fast |
| C++17 | `std::invalid_argument` / `std::runtime_error`; `-Wall -Wextra -Werror` clean; no allocation in book/feature hot loops after warmup (reserve), enforced by tests |
| Rust (edition 2021) | `Result<_, IapError>`, no panics on input, zero warnings; workspace dependencies are `serde` and `serde_json` only (`docs/governance/SECURITY.md` §1) |
| Java 21 | `IllegalArgumentException` / `IllegalStateException`; `-Xlint:all` clean; hot paths allocation-conscious (primitive arrays, no boxing) |
| all | no dead code, no TODOs, no new dependency without a SECURITY.md entry; names follow the repo (`price_ticks`, `exchange_ts`, `feature_version`, `x-version`, `EQ01..FX12`) |

## 9. Documentation that moves with the code

A behaviour change updates its contract in the same PR: `API_CORE.md`,
`API_FEATURES.md`, `API_ALPHA.md`, `API_PORTFOLIO_TCA.md`, `API_ADAPTIVE.md`,
`API_CONTRACTS.md`, `API_TRADING.md`, `PLATFORM_CONVENTIONS.md` §11–§13,
`docs/LIFECYCLE.md` / `docs/DECISION_TRACE.md` / `docs/DATA_MODEL.md` /
`docs/MVP.md` for their subsystems, `docs/SCENARIOS.md` for a new pinned
behaviour, the runbooks for anything an operator sees, `docs/EPICS.md`
(via the YAML) for the plan, and `README.md` for any headline number. A
diagram change edits the `.mmd` source and its embedded copy together
(`check_headline_numbers.py` `mermaid_sources_in_sync`).
