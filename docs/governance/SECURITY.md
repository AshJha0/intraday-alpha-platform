# Security Policy — Intraday Alpha Platform

Implements spec §26 (security, governance, reproducibility). Scope: everything
in this repository plus the images built from `deployment/docker/`.

## 1. Dependency pinning

The dependency surface is deliberately tiny and every entry is pinned and
auditable:

| Layer | Dependency set | Pin mechanism |
|---|---|---|
| Python | `python/pyproject.toml` 1.1.0: numpy, pandas, scipy, scikit-learn, `pyarrow` (Parquet), `jsonschema` + `referencing` (offline contract validation), each with lower and upper bounds; extras `ml` (xgboost, lightgbm), `dev` (pytest, pytest-cov, pyyaml) | Bounds in `python/pyproject.toml`; exact versions (direct and transitive, no hashes) in `python/requirements-ci.txt`, which CI installs and `Dockerfile.python` passes as a pip constraints file (`-c`). `Dockerfile.python` installs the package with its declared dependencies and not the `dev` extra. No network or LLM client anywhere on the trading path (`PLATFORM_CONVENTIONS.md` §13.7). |
| C++ | g++/CMake toolchain, GoogleTest, Eigen (system packages) | Debian package versions recorded at image build; no FetchContent/network downloads in CMake (`cpp/CMakeLists.txt` resolves system packages only). |
| Rust | `serde`, `serde_json` (+ `crossbeam` only where justified) — PLATFORM_CONVENTIONS.md §10 | `rust/Cargo.toml` workspace dependencies; **`Cargo.lock` is the pin** — commit it, and never publish a release image built without a lockfile. Every cargo invocation in CI, `run_all.sh` and `Dockerfile.rust` passes `--locked`, and the toolchain is pinned in `rust/rust-toolchain.toml` (both enforced by `check_deployment.py`). The release profile keeps `overflow-checks = true`. |
| Java | **none at runtime**; JUnit4 + hamcrest, test scope only | Local jar at `/usr/share/java/junit4.jar` (or vendored `java/lib/`). Maven/Gradle are deliberately absent (Maven Central unreachable — `docs/BUILD_NOTES.md` is the normative pom-equivalent list). Any new Java dependency must be vendored into `java/lib/` and recorded there. The `iap/java` image build does NOT run the JUnit suite (its base image has no JUnit jars and the build may not reach a registry): the gate is `.github/workflows/ci.yml`, and the `images` job `needs:` it. |
| Images | base images in `deployment/docker/*`, `docker-compose.yml`, `deployment/k8s/*` | Third-party and base images are pinned by tag **and** sha256 digest in the repo (enforced by `check_deployment.py`; Dependabot's docker ecosystem proposes bumps). The platform's own images are named `ghcr.io/ashjha0/intraday-alpha-platform-<lang>:<tag>`; **digest-pinning them is a manual release step** from `release-manifest.json` (`REPO_SETTINGS.md` section 5). |
| GitHub Actions | `.github/workflows/*.yml` | Every `uses:` is a full commit SHA with the version as a trailing comment, runners are `ubuntu-24.04`, workflows declare least-privilege `permissions:` (enforced by `check_deployment.py`; Dependabot updates the SHAs). |

Adding a dependency in any language is a reviewed change (see GOVERNANCE.md)
and requires: why it is needed, its license, its pin, and its removal plan if
it is a stopgap.

## 2. Vulnerability scanning

What runs today. The two dependency audits are **non-blocking** (a finding
is reported in the CI log and does not fail the run, because none has been
triaged yet); the lint and sanitizer steps are **blocking**:

- Python: `pip-audit` against `python/requirements-ci.txt` (the `advisory` job
  in `ci.yml`, non-blocking), plus `ruff check` (blocking; the rule set is
  correctness-only — `ruff.toml`).
- Rust: `cargo audit` against `Cargo.lock` (RustSec advisory DB, `advisory`
  job, non-blocking) and `cargo clippy -D warnings` (a blocking step of the
  `rust` job).
- C++: an ASan+UBSan build of the full ctest suite (`cpp-sanitizers` job,
  blocking).
- Source code: CodeQL for python, java-kotlin, c-cpp and the workflows
  (`codeql.yml`).

What is required but **not implemented**: `trivy image` (or equivalent) on
every candidate image before it is digest-pinned, and refreshing/re-digesting
base images monthly (Dependabot opens the PRs; the cadence is not enforced).
Dependabot alerts are a repository setting that is not yet configured
(`REPO_SETTINGS.md` section 3). Policy: a release is blocked on any
Critical/High finding without a written, time-boxed waiver from the platform
owner; no automation enforces that blocking.

## 3. Secrets and configuration separation

Hard rule: **this repository contains no secrets, and configs are not
secrets.**

- `configs/<domain>/*.json` (instruments, venues, generator, risk limits, execution,
  strategies) are *behavioral configuration*: reviewed, versioned, deployed
  via the `iap-configs` ConfigMap / baked read-only into images. They are
  world-readable by design; nothing in them may be secret.
- Credentials (venue/API keys, DB passwords, Grafana admin) enter only
  through the environment or an orchestrator secret store:
  - docker-compose: `GRAFANA_ADMIN_PASSWORD` must come from the host
    environment or a git-ignored `.env` file — compose fails fast if unset
    (`${GRAFANA_ADMIN_PASSWORD:?...}`).
  - Kubernetes: `Secret` objects created out-of-band (e.g.
    `iap-grafana-admin`), referenced by name only from the manifests
    (`deployment/k8s/grafana.yaml`); never `--from-literal` values committed
    in any script.
- Environment variables carrying secrets are never logged; the structured
  loggers (rust `JsonlLogger`, python logging) log explicit fields only —
  never a dump of `os.environ`/`std::env`.
- Any secret that touches a commit (even briefly) is considered burned:
  rotate it, then purge history.

## 4. Runtime hardening

- All containers run as a dedicated non-root user (uid 10001 for iap images);
  Kubernetes enforces `runAsNonRoot`, `seccompProfile: RuntimeDefault`,
  dropped capabilities, no service-account token mounts
  (`automountServiceAccountToken: false`), a read-only root filesystem on the
  java platform (writable `/tmp` emptyDir and its own state PVC only), and the
  namespace carries `pod-security.kubernetes.io/enforce: restricted`.
- Ingress and egress are default-deny; only the flows in
  `deployment/k8s/networkpolicy.yaml` are open (prometheus→java:8080,
  prometheus→alertmanager:9093, grafana→prometheus:9090, ingress→grafana:3000,
  `iap.role=operator` pods→java:8080 for the admin API, DNS for every pod,
  alertmanager→tcp/443 for the webhook). NetworkPolicies take effect only on a
  CNI that implements them.
- docker-compose publishes 8080, 9090 and 9093 on `127.0.0.1` only; the java
  server's bind address is `IAP_BIND_ADDR` (0.0.0.0 inside the containers).
- Raw market data is immutable (spec §1): pipeline outputs are written once
  per dated run; nothing rewrites `data/raw/` in place.
- **Monitoring API exposure** (`com.iap.api.MetricsServer`, port 8080):
  `/metrics`, `/health`, `/ready` and `/status` are **unauthenticated** and
  must stay behind the NetworkPolicy (only `prometheus → java-platform:8080`
  is open) — they expose position, P&L and limit data. The write surface is
  authenticated: `POST /admin/{kill,clear,override,roll}` requires a bearer
  token from `$IAP_ADMIN_TOKEN` or `$IAP_ADMIN_TOKEN_FILE` (a Kubernetes
  Secret), compared in constant time; **with no token configured the routes
  are not registered at all** (404) — the platform never exposes an
  unauthenticated kill endpoint. The token is never logged: the audit records
  its sha256 (`PLATFORM_CONVENTIONS.md` §12.5). Handlers run on a bounded pool
  with `maxReqTime`/`maxRspTime`, so a client that connects and stalls cannot
  occupy the server, and — because the metric registry is lock-free (§12.4) —
  cannot stall the trading thread either.
- **Durable state** (`$IAP_STATE_DIR`) holds positions, P&L and the audit
  logs. It is written by the container's non-root user on a mounted
  volume/PVC and must never be baked into an image or committed;
  `.gitignore` excludes it.

## 5. Enforcement status

Several controls named in this policy and in `GOVERNANCE.md` are repository or
operations settings that are **not enforced by any file in this repository and
are not yet configured**: branch protection and required status checks on
`main`, the review counts (two reviewers for risk and contracts changes need a
second maintainer), signed release tags (a practice; nothing verifies it),
Dependabot alerts and security updates, off-host shipping of the audit logs,
image vulnerability scanning before digest-pinning, and delivery of alerts to a
real receiver (the Alertmanager webhook URL is a secret the operator supplies).
`docs/governance/REPO_SETTINGS.md` lists each with the exact `gh api` commands.
Until they are applied, treat the corresponding statements as requirements.

## 6. Reporting

Suspected vulnerabilities or leaked credentials go to the platform owners
(CODEOWNERS for `deployment/` and `docs/governance/`: today `@AshJha0`) immediately; kill-switch
and incident procedure in `docs/runbooks/RUNBOOK_incident_kill_switch.md`
applies if production trading could be affected.
