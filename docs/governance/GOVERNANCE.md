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
| **Deployment / observability** | `deployment/`, `docs/runbooks/` | 1 platform reviewer; YAML must pass the structural validation used in CI (pyyaml load + `docker compose config -q`). |
| Everything else | docs, research scripts, benchmarks | 1 reviewer. |

Mandatory for every PR class: `tests/harness/run_all.sh` green (all four
languages, < 120 s each), zero compiler warnings (conventions §8), no new
dependencies without a SECURITY.md §1 entry.

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
  breach is a `RiskEvent` (schemas/risk_event.schema.json) appended to the
  JSONL audit log by the rust risk engine (`RiskEngine::audit_jsonl`,
  byte-deterministic and replayable — `rust/risk/tests/golden_risk.rs`
  proves the log replays identically). Retention: immutable, per session,
  shipped off-host daily.
- **Strategy/risk configuration changes**: any change to `configs/risk.json`,
  `configs/strategies*`, `configs/execution.json` requires a ledger entry
  (PR link, before/after diff, approver, effective time) *before* the config
  reaches production. The deployed ConfigMap is regenerated only from a
  reviewed commit (`deployment/k8s/generate_configmaps.py`), so `git log` of
  `configs/` plus the PR record is the authoritative change trail.
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
