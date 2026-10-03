# Changelog

Release notes for tagged versions, newest first. Notes for v1.1.1 and v1.2.0
are copied from their GitHub releases; v1.1.0 has a git tag but no GitHub
release, so its entry comes from the annotated tag message and the changes
recorded in the repository for that tag.

## Unreleased

- Repository governance hardening: CI actions pinned to commit SHAs, runners
  pinned, `--locked` on every cargo invocation, least-privilege `permissions`,
  non-blocking clippy / ruff / `pip-audit` / `cargo audit` / ASan+UBSan jobs,
  CodeQL and Dependabot configuration, image build on pull requests that touch
  image inputs.
- Release workflow (`.github/workflows/release.yml`): on a `v*` tag builds the
  four images, pushes them to `ghcr.io/ashjha0/intraday-alpha-platform-<lang>`,
  attests build provenance and attaches `release-manifest.json` with the image
  digests to the GitHub release. Untested until the first tag.
- Python dependency bounds in `python/pyproject.toml` and exact CI versions in
  `python/requirements-ci.txt`; Rust toolchain pinned in
  `rust/rust-toolchain.toml` and `overflow-checks = true` in the release
  profile; Docker base images pinned by digest.
- Deployment: Alertmanager (compose and Kubernetes) with a Watchdog heartbeat,
  default-deny egress and an operator ingress rule, separate state PVC for the
  Java platform, read-only root filesystem, no service-account token mounts,
  Grafana PVC, loopback-only published ports in compose.
- `CODEOWNERS` now names `@AshJha0` (the `@iap/*` teams never existed).
- Governance documents state which controls are repository/ops settings not
  yet configured; `docs/governance/REPO_SETTINGS.md` has the commands.
- Backlog: seven new epics (real exchange data, latency engineering, simulator
  calibration, book-level risk, research throughput, agent layer, differential
  fuzzing and risk golden coverage).

## v1.2.0 - Risk engine split into submodules (2026-09-27)

Follow-up to the code review that produced v1.1.1 — completes the last
deferred P1 item: splitting the safety-critical, byte-identical-port risk
engine into cohesive submodules.

**rust/risk/src/**
- `engine.rs` — core `RiskEngine` state machine (constructors, market/fill
  state-in, P&L, `check_order`)
- `limits_eval.rs` — the pinned rule-0..22 limit evaluation
- `killswitch.rs` — kill/unkill/override_loss_limit/roll_session
- `audit.rs` — audit-log emission and snapshot/restore
- wired via `mod` declarations in `lib.rs`; crate's public surface unchanged

**java/src/main/java/com/iap/risk/**
- `RiskEngine.java` — same shape as the Rust core, public methods now delegate
  to the new classes
- `LimitsEvaluator.java`, `KillSwitch.java`, `RiskAudit.java` — same-package
  classes operating on the `RiskEngine` instance
- Every public method signature on `RiskEngine` unchanged

Both splits were done as pure mechanical, line-for-line moves — no logic
changed, only visibility and cross-file glue. Verified behavior-identical by
CI, including `tests/golden`'s cross-language risk-parity fixtures
(`expected_risk_decisions.json`, `expected_risk_audit.jsonl`,
`expected_risk_snapshot.json`) — all checks passed on the first CI run.

See [PR #6](https://github.com/AshJha0/intraday-alpha-platform/pull/6).

## v1.1.1 - Supply-chain and error-handling hardening (2026-09-27)

Follow-up to a full-repo code review (PR #5).

**CI / supply chain**
- `promtool` and `kubeconform` downloads now verified against their published
  SHA256 checksums before install.
- Added report-only coverage instrumentation: `pytest-cov` (python),
  `cargo-llvm-cov` for the `risk` crate (rust), gcov/lcov (cpp). Java coverage
  intentionally deferred — no Maven/Gradle or vendored JaCoCo jar yet.
- The `images` job (4 Docker builds) now runs only on push to `main`, not on
  every PR, while remaining the required merge-to-main promotion gate.

**Error handling**
- `rust/alpha/src/fx_exposure.rs`: currency-lookup `.expect()` panics replaced
  with `Result<_, IapError>` propagation.
- `rust/risk/src/bin/make_risk_golden.rs`: ~30 `.unwrap()` calls on JSON field
  access replaced with helpers that return actionable errors naming the
  offending step and field, instead of bare panics on malformed golden-fixture
  input.

**Documentation / logging**
- `python/src/iap/experiment/tracker.py`: documented the pickle trust boundary
  on `save_model`/`load_model` (deserialization is an RCE vector — only ever
  load a `model.pkl` this platform produced locally).
- Swallowed `OSError`/`SubprocessError` in the git-metadata lookup is now
  logged at debug level instead of silently passed.

**Not included** (from the same review, intentionally deferred): splitting
`rust/risk/src/engine.rs` / `java/.../risk/RiskEngine.java` into submodules —
judged too risky without a local toolchain to verify zero behavioral change
against the golden risk fixtures; a `java/lib/` checksum manifest — there are
no vendored jars in the repo today.

## v1.1.0 - Round-3 hardening (2026-09-06)

Annotated tag message: "Round-3 hardening: real-world scenario correctness,
1,571 tests across four languages". No GitHub release was created for this tag.
Changes recorded in the repository for this round: real-world scenario
correctness across all four languages (`docs/SCENARIOS.md`), the CI workflow
(`.github/workflows/ci.yml`), the deployment validation harness
(`tests/harness/check_deployment.py`, `check_docker_build.py`), and the Java
paper-trading vertical's health, readiness and kill-switch admin API
(`PLATFORM_CONVENTIONS.md` section 12).
