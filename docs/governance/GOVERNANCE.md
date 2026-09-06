# Governance — Code Review, Promotion Gates, Audit Logs

Implements spec §20 (research-to-production promotion) and §26 (governance).
Binding for every change to this repository.

## 1. Code-review requirements

All changes land by pull request; no direct pushes to the default branch.
Review depth scales with blast radius:

| Change class | Paths (indicative) | Review requirement |
|---|---|---|
| **Alpha / signal logic** | `python/src/iap/alpha/`, `cpp/src/alpha/`, `rust/alpha/`, `java/.../alpha/`, `configs/strategies/` | 1 quant reviewer **+ 1 owner of each production language touched**; golden vectors regenerated only with an explicit "golden change" note explaining why (`python/tools/make_golden.py` runs are deliberate acts, never side effects). |
| **Risk engine / limits** | `rust/risk/`, `configs/risk.json`, risk contracts in `schemas/` | 2 reviewers, one of whom is the risk owner. Fail-closed semantics may never be weakened in the same PR that adds a feature. Limit changes (`configs/risk.json`) additionally require the audit-log entry of §3 *before* deploy. |
| **Execution / SOR** | `cpp/src/execution/`, `cpp/src/sor/`, `rust/execution/`, `rust/venue/`, `configs/execution.json` | 1 execution owner + 1 second reviewer; determinism proof: `tests/harness/run_golden.sh` parity table attached to the PR. |
| **Contracts & schemas** | `schemas/`, `PLATFORM_CONVENTIONS.md` §1–§2 | 2 reviewers + version bump + `MIGRATIONS.md` entry; all four languages updated in the same PR or the PR is blocked. |
| **Deployment / observability** | `deployment/`, `docs/runbooks/`, `tests/harness/`, `.github/` | 1 platform reviewer (`CODEOWNERS`); the deployment validation used in CI must pass — `python3 tests/harness/check_deployment.py`, i.e. `promtool check rules` + `check config` + `promtool test rules`, `docker compose config -q`, every Dockerfile `COPY` source resolving in a clean build context, k8s manifests parsing and dry-running, the generated ConfigMaps matching `configs/`, every rule and dashboard expression naming a metric a producer exports, and the Java golden gate covering every `*GoldenTest` class. |
| Everything else | docs, research scripts, benchmarks | 1 reviewer. |

Mandatory for every PR class: `tests/harness/run_all.sh` green (all four
languages, < 120 s each, plus the deployment column), zero compiler warnings
(conventions §8), no new dependencies without a SECURITY.md §1 entry.

**Where CI is.** `.github/workflows/ci.yml` runs exactly this on every push and
pull request: one job per language, `tests/harness/run_golden.sh` (gate 10),
`tests/harness/check_deployment.py`, and an `images` job — building the four
container images — that `needs:` all of them, so promotion gate 10's "never
publish an image from a tree whose golden suite fails" is enforced by the
dependency graph rather than by a promise in a Dockerfile header.
`CODEOWNERS` names the reviewers this table and SECURITY.md refer to.

## 2. Promotion gates (spec §20, verbatim order)

A strategy or model moves toward production only through these gates, in
order; each gate's evidence is linked from the experiment's manifest
(REPRODUCIBILITY.md):

1. Idea and economic hypothesis — written rationale in `research/alpha_reports/`.
2. Reference implementation in Python (`python/src/iap/alpha/`).
3. Feature and label validation — registry entries + leakage test green.
4. In-sample research — IC/RankIC/t-stat/hit/decay/turnover/capacity reported.
5. Walk-forward + purged/embargo validation (never random splits).
6. Realistic cost and latency simulation (event-driven backtester).
7. Capacity and parameter stress tests.
8. Cross-alpha correlation and incremental contribution.
9. Production implementation (Java/C++/Rust as appropriate).
10. **Cross-language golden-test validation** — `tests/harness/run_golden.sh`
    parity table shows every language passing its golden group; this is the
    hard gate between research code and production code.
11. Paper/shadow deployment — monitored against the live-vs-backtest drift
    metric (`alpha_live_vs_backtest_drift`, deployment/grafana/README.md).
    Note the lifecycle gauge (`alpha_lifecycle_state`) is IC-gated and
    **observational** in the live loop: RETIRED pages a human
    (`AlphaLifecycleRetired`), it does not halt the allocation by itself
    (API_ADAPTIVE.md §6, PLATFORM_CONVENTIONS.md §12.6).
12. Limited-capital promotion subject to **written risk approval** (risk owner
    sign-off recorded in the audit log).
13. Continuous monitoring and pre-agreed retirement criteria (drift, decay,
    fill-quality thresholds recorded at promotion time).

No gate may be skipped; "backtest Sharpe alone is never sufficient" (spec §1).
Honest reporting is a hard requirement (conventions §7): costs and OOS
degradation are always shown, and the experiment ledger
(`research/models/ledger.json` `experiment_count`) makes the multiple-testing
denominator public.

## 3. Audit-log policy

Auditable events and where they are recorded:

- **Risk decisions and kill events (runtime)**: every pre-trade decision and
  breach is a `RiskEvent` (schemas/risk_event.schema.json), byte-deterministic
  and replayable (`rust/risk/tests/golden_risk.rs` proves the log replays
  identically). **Where the deployed platform writes it**: the Java vertical
  streams it to `<state-dir>/risk_audit.jsonl` — appended and fsynced at every
  checkpoint (1,024 events), at session end and from a shutdown hook — and the
  session report carries the file's path and its sha256
  (`report.risk.audit_jsonl` / `audit_sha256`), so a postmortem can prove which
  bytes it read (`PLATFORM_CONVENTIONS.md` §12.3, tested by
  `PaperStateRecoveryTest.auditLogIsPersistedAndMatchesTheEngine`). In the
  deployments `<state-dir>` is `$IAP_STATE_DIR` on the shared volume/PVC
  (`/data/state`). Retention: immutable, per session, shipped off-host daily.
- **Kill-switch operations (runtime)**: every call to the operator API
  (`POST /admin/{kill,clear,override,roll}`) — accepted, refused or rejected —
  appends a line to `<state-dir>/admin_audit.jsonl` with the action, the scope,
  the approval `reason` verbatim, the HTTP status and the **sha256 of the
  actor's token** (never the token). The corresponding `RiskEvent` is written
  by the trading thread at the current event time, so the two logs line up
  (`PLATFORM_CONVENTIONS.md` §12.5).
- **Strategy/risk configuration changes**: any change to `configs/risk.json`,
  `configs/strategies*`, `configs/execution.json` requires a ledger entry
  (PR link, before/after diff, approver, effective time) *before* the config
  reaches production. The deployed ConfigMap is regenerated only from a
  reviewed commit (`deployment/k8s/generate_configmaps.py`) and CI fails if the
  committed ConfigMaps drift from `configs/`, so `git log` of `configs/` plus
  the PR record is the authoritative change trail. At runtime the platform
  writes `<state-dir>/config_audit.jsonl` (one `config_loaded` record with the
  sha256 of every pinned file) at startup and reports a single `config_sha256`
  over all of them on `/status` and in the session report — so a session can
  always be tied to the exact configuration it ran with.
- **Golden vector changes**: regeneration commits must carry the "golden
  change" note (§1) — the goldens define cross-language truth, so their
  history is part of the audit trail.
- **Model/experiment lineage**: every training run writes
  `research/models/<run_id>/manifest.json` (REPRODUCIBILITY.md); the ledger
  (`ledger.json`) is append-only.
- **Deploys**: each release records image digests, git commit and config hash
  in the release manifest; rollbacks reference the prior manifest rather than
  rebuilding.

Audit logs are never edited in place. Corrections are new entries referencing
the entry they correct.
