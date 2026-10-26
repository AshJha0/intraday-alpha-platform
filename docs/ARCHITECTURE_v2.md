# Architecture, v2.0: the package map, the import rules, identities and partitions, the runner flow

One page for a contributor or reviewer who needs to know where things are and what may import
what. The v1.x engine architecture (order book, feature factory, execution simulator, risk engine,
replay, the C++ and Rust ports and their goldens) is unchanged and documented in
[ARCHITECTURE.md](ARCHITECTURE.md) and [DIAGRAMS.md](DIAGRAMS.md); this page covers what v2.0
added on top of it and how the pieces are kept apart. Method and vocabulary: [LEARN.md](../archive/docs/LEARN.md).
Runnable commands: [COOKBOOK.md](../archive/docs/COOKBOOK.md).

## 1. Package map

All Python code lives under [`python/src/iap/`](../python/src/iap/) (the full module list, one line
per file, is [`python/src/iap/README.md`](../python/src/iap/README.md)). The v2.0 packages and the
ones they rest on:

| package | role in v2.0 | key modules | tests |
|---|---|---|---|
| `iap.core` | the substrate: events, the only RNG (SplitMix64), codecs, **the data-read choke point** | `guard.py` (every reader calls `check` before opening a file; fail closed with no hook), `rng.py`, `codec.py`, `events.py` | `python/tests/test_guard_hook.py`, `test_codec.py` |
| `iap.contracts` | versioned, validated contracts and canonical JSON (`content_hash` is the identity of every v2.0 artefact) | `versions.py`, `types.py`, `validate.py` | `python/tests/test_contracts.py` |
| `iap.data` | **data and feature contracts**, the point-in-time universe, the leakage canaries, the free-route source adapters, the synthetic fixtures | `contracts.py`, `pit.py`, `leakage.py`, `synthetic.py`, `sources/{stooq,tiingo,wiki_membership,edgar,french}.py` | `python/tests/leakage/`, `python/tests/data_sources/` |
| `iap.alpha` | the **forecast interface** and the content-addressed **forecast registry**; H1's client; the v1.x alphas | `forecast.py`, `registry.py`, `h1.py`, `base.py` | `python/tests/test_forecast_interface_v2.py`, `python/tests/equivalence/` |
| `iap.portfolio` | **projection and risk** (one constrained least-squares repair), the **cohort and executable ledgers** with the cash identity, the v1.x optimiser and factor risk | `project.py`, `ledger.py`, `factor_risk.py`, `optimizer.py` | `python/tests/portfolio/` |
| `iap.execution` | the **v2.0 fill simulator** (one fill-price convention, three cost columns, failure cases, auction execution) and the v1.x simulator, router and algos | `sim.py`, `latency.py`, `fees.py`, `simulator.py`, `sor.py` | `python/tests/execution/`, `python/tests/test_execution_golden.py` |
| `iap.reports` | the **E5 report builders** (blocks A to E, session series, screen, decision tree, inflation checks) | `e5.py` | `python/tests/reports/` |
| `iap.validation` | the **confirmatory gate** (the only PROMOTE constructor for H1), the **confirmation profiles** (the only other one), the selection ledger, the v1.x splits, metrics and stress grids | `confirmatory.py`, `profiles.py`, `ledger.py`, `splits.py` | `python/tests/test_confirmatory*.py`, `python/tests/spec/`, `python/tests/validation/` |
| `iap.research` | the **research loop** and the family feature builders; the v1.x studies, screens and the holdout registry that owns the guard hook | `loop.py`, `family_features.py`, `holdout_registry.py`, `claims.py` | `python/tests/research/` |
| `iap.ml` | the ML framework under the frozen ML protocol: splits, trials, admission, the M1 pipeline | `protocol.py`, `splits.py`, `trials.py`, `admission.py`, `pipeline.py` | `python/tests/ml/` |
| `tools/` | operator entry points, outside the package: the sealed service, the registered runner, the ingestion job, the data manifest, signature verification, the research-loop CLI | `sealed_service.py`, `runner/run.py`, `ingest/dd1_daily.py`, `data_manifest.py`, `verify_signatures.py`, `research/run_family.py`, `repro/` | `python/tests/runner/`, `python/tests/isolation/`, `python/tests/ingest/` |

The other v1.x packages (`marketdata`, `orderbook`, `features`, `labels`, `replay`, `risk`, `tca`,
`mvp`, `paper`, `agents`, `llm`, `mlops`, `adaptive`, `lifecycle`, `monitoring`, `metrics`,
`store`, `trace`, `experiment`, `models`, `combine`, `auction`, `backtest`, `reference`, `io`) are
listed with their status in [`python/src/iap/README.md`](../python/src/iap/README.md); the C++ and
Rust trees are frozen copies under the policy of [POLYGLOT.md](POLYGLOT.md).

## 2. Import rules

Two mechanisms enforce the layering, and they agree by construction:

- [`python/.importlinter`](../python/.importlinter) (ten contracts; the `lint-and-secrets` CI job
  runs `lint-imports`) and [`python/tests/architecture/test_layering.py`](../python/tests/architecture/test_layering.py),
  an AST scan that parses the same file so the fast suite fails on the same violation;
- [`python/tests/test_import_policy.py`](../python/tests/test_import_policy.py): no module of a
  guarded package (`risk`, `execution`, `orderbook`, `portfolio`, `mvp`, `core`, `marketdata`,
  `features`, `alpha`, `replay`, `trace`, `monitoring`, `data`) imports a network client, a server
  or an LLM client, anywhere in the file.

The contracts, in words:

1. `iap.core` and `iap.contracts` import nothing from `iap.research`, `iap.agents` or `iap.llm`.
   The guard hook is *registrable*: `iap.core.guard` holds one hook, the holdout registry in
   `iap.research` installs it, and the package root installs a lazy hook that imports the registry
   on the first real read. The policy lives above the choke point; the choke point never names the
   policy.
2. `iap.marketdata`, `iap.features` and `iap.execution` do not import `iap.research`, `iap.agents`
   or `iap.llm` directly.
3. `iap.alpha`, `iap.portfolio` and `iap.ml` do not import `iap.research`: the engine is
   family-agnostic and the research loop is its client, never the other way round.
4. `iap.labels` does not import `iap.features` at run time, and `iap.execution` does not import
   `iap.tca`: the two cycles stage 11 broke stay broken.
5. `iap.data` imports no network module; every source adapter takes an injected transport, so CI
   makes no request and the vendor SDKs stay in `tools/`.

Contracts marked `allow_indirect_imports` bind direct imports only; the transitive reach through
`mlops -> agents`, `models -> research` and `validation` is recorded in the file's header and in
[ARCHITECTURE.md](ARCHITECTURE.md) section 16.1 rather than hidden.

## 3. Where identities and partitions sit

| what | where | who may read | authority |
|---|---|---|---|
| the development identity (`iap-dev` by role: the owner's normal user, every agent session, every research script) | the repository checkout, `data/` (in-sample and development partitions) | itself | [governance/SEALED_DATA.md](GOVERNANCE.md) section 1 |
| development and validation partitions of the daily route | `C:\iap\daily\dd1_<Y>\` on the workstation (written by the ingestion job as `iap-runner`, readable by the development identity) | `iap-dev`, `iap-runner` | [governance/DD1_INGESTION_RUNBOOK.md](GOVERNANCE.md) |
| Track A sealed window (H1) | `C:\iap-sealed\a` | `iap-sealed-a`, and `iap-runner` through the service only | docs/GOVERNANCE.md |
| Track B test year | `C:\iap-sealed\b` | `iap-sealed-b`, and `iap-runner` through the service only | docs/GOVERNANCE.md |
| Track C confirmation slice and forward window | `C:\iap-sealed\c` | `iap-sealed-c`, and `iap-runner` through the service only | docs/GOVERNANCE.md |
| the runner's private cache | `C:\iap-sealed\runner` | `iap-runner` | [governance/RUNNER_THREAT_MODEL.md](GOVERNANCE.md) property 1 |
| the sealed service (one process per partition, a Windows named pipe, OS-authenticated) | `tools/sealed_service.py`, installed by `tools/admin/install_sealed_services.ps1` | answers `iap-runner` only | docs/GOVERNANCE.md section 2 |
| the audit anchor | `research/audit/CHECKPOINTS.jsonl` on the `results` branch | anyone (`sealed_service.py verify-checkpoints`) | docs/GOVERNANCE.md section 3 |
| what a record may cite | `research/data_manifest.json` (every dataset with its integrity verdict; DD1 partitions by hash id) | anyone (`tools/data_manifest.py check` in CI) | `tools/data_manifest.py` |
| the in-process access decision for every read | `iap.core.guard.check(path, purpose)` and the registered hook (`iap.research.holdout_registry`) | n/a | [governance/DATA_ACCESS_PATHS.md](GOVERNANCE.md) |

The partition calendar (development to the end of year `Y-4`, validation `Y-3`, Track B `Y-2`,
Track C `Y-1` onward, `Y` the ingestion year) is cut by the ingestion job before any fit; the
research loop refuses any partition but development and validation, any identity but the
development one, and any data path under a sealed root. The whole arrangement runs on one
workstation whose administrator is the developer, which is why its evidence class is *mechanically
isolated* (LEARN.md section 6).

## 4. The registered-run flow

The procedure of [governance/RUNNER_THREAT_MODEL.md](GOVERNANCE.md), as the
code executes it ([`tools/runner/run.py`](../tools/runner/run.py),
[`.github/workflows/registered-run.yml`](../.github/workflows/registered-run.yml)); each step
refuses before the next:

1. A signed pre-registration with a `registered_run` block (partition, dataset hash id, pinned
   image digest, command) is merged to `main`; `tools/verify_signatures.py --strict` passes and the
   dataset id is in the manifest.
2. The owner starts the `registered-run` workflow naming the prereg and the bound commit; GitHub
   holds the job until the owner approves the `sealed` environment (the required reviewer).
3. The self-hosted runner, a Windows service running as `iap-runner`, checks out the bound commit.
   The process refuses to continue as any other account (exit 3).
4. The runner verifies the signature, the `registered_run` block, the image digest against
   `deployment/docker/IMAGE_DIGEST`, the analysis identity (script and code-tree hashes, lock,
   environment, interpreter, bound commit an ancestor of HEAD) and that the dataset id is bound.
5. The runner sends one `read` request to the partition's sealed service. The service checks the
   caller's account and process image, that no checkpoint is pending, the signature under its
   pinned trust-root copy, that the registration hash and id have never been served, and that the
   dataset files still match their recorded hashes; it writes the ledger entry, the audit record
   and the pending checkpoint, then serves the bytes into the runner's private cache.
6. The runner recomputes the served file hashes against the bound `dataset_files`.
7. The registered command runs once with the dataset directory and an output directory and must
   write `result.json`; its stdout and stderr are never copied into the log.
8. `hashes.json` (result hashes, prereg hash, dataset id, sequence, chain head, image digest,
   commit, evidence class) is written; `store_result` records the result hash with the service.
9. The result is committed to the protected, append-only `results` branch from the `iap-runner`
   git identity, with the checkpoint line appended to `research/audit/CHECKPOINTS.jsonl`.
10. `publish` hands the commit to the service, which clears the pending checkpoint; until then
    every further read is refused.
11. The redacted log is the only uploaded artefact; the private cache is removed.
12. Anyone verifies afterwards: `tools/runner/run.py verify-result`, `tools/sealed_service.py
    verify-checkpoints`, and the words `mechanically isolated` in `hashes.json`.

The ingestion job ([`tools/ingest/dd1_daily.py`](../tools/ingest/dd1_daily.py)) runs through the
same workflow in `dd1` mode with the same identity and approval steps; it writes the two sealed
partitions through the service's `ingest` endpoint and never reads one.

## 5. Where a result's class is decided

| artefact | written by | class it can carry |
|---|---|---|
| an E5 report on the development or validation partition (`<family>_<partition>_report.json` with its sha256, a selection-ledger entry) | the research loop as the development identity | exploratory (synthetic route) or development-supported (real route); never higher |
| a confirmation verdict for H1 | `iap.validation.confirmatory.decide` | PROMOTE or `refused: <reason>`; the class of the surrounding run is the runner's |
| a confirmation verdict for any other registration | `iap.validation.profiles.decide` (U13) | the same |
| `results/<prereg>/<seq>/hashes.json` | the registered runner | mechanically isolated (until custody separate from the developer exists) |
| the release notes and `archive/docs/REVIEW_PACK.md` section 0 | the maintainer | one class per result, in the words of U0 section 10 |
