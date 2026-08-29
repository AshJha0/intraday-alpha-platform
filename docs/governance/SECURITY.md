# Security Policy — Intraday Alpha Platform

Implements spec §26 (security, governance, reproducibility). Scope: everything
in this repository plus the images built from `deployment/docker/`.

## 1. Dependency pinning

The dependency surface is deliberately tiny and every entry is pinned and
auditable:

| Layer | Dependency set | Pin mechanism |
|---|---|---|
| Python | stdlib + `pyarrow` (pipeline); numpy/pandas/scipy/sklearn preinstalled research stack | `python/pyproject.toml`; production image installs only the pipeline deps. Release images must add a `pip freeze`-generated constraints file recorded in the release manifest. |
| C++ | g++/CMake toolchain, GoogleTest, Eigen (system packages) | Debian package versions recorded at image build; no FetchContent/network downloads in CMake (`cpp/CMakeLists.txt` resolves system packages only). |
| Rust | `serde`, `serde_json` (+ `crossbeam` only where justified) — PLATFORM_CONVENTIONS.md §10 | `rust/Cargo.toml` workspace dependencies; **`Cargo.lock` is the pin** — commit it, and never publish a release image built without a lockfile. |
| Java | **none at runtime**; JUnit4 + hamcrest, test scope only | Local jar at `/usr/share/java/junit4.jar` (or vendored `java/lib/`). Maven/Gradle are deliberately absent (Maven Central unreachable — `docs/BUILD_NOTES.md` is the normative pom-equivalent list). Any new Java dependency must be vendored into `java/lib/` and recorded there. |
| Images | base images in `deployment/docker/*`, `docker-compose.yml`, `deployment/k8s/*` | Tag-pinned in repo; **digest-pinned at release** (`image@sha256:...` recorded in the release manifest — symbolic in-repo because this environment builds offline). |

Adding a dependency in any language is a reviewed change (see GOVERNANCE.md)
and requires: why it is needed, its license, its pin, and its removal plan if
it is a stopgap.

## 2. Vulnerability scanning

- Python: `pip-audit` against the frozen constraints file on every release
  branch build.
- Rust: `cargo audit` against `Cargo.lock` (RustSec advisory DB) on every
  release branch build.
- Images: `trivy image` (or equivalent) on every candidate image before it is
  digest-pinned; base images are refreshed and re-digested at least monthly
  and immediately on a critical CVE affecting glibc/openssl/JVM.
- Java/C++: dependency surface is system packages + vendored jars only; the
  image scan covers them.
- Scans run in CI where the network allows; in the offline build environment
  the scan happens at the release gate on the machine that resolves digests.
  A release is blocked on any Critical/High finding without a written,
  time-boxed waiver from the platform owner.

## 3. Secrets and configuration separation

Hard rule: **this repository contains no secrets, and configs are not
secrets.**

- `configs/*.json` (instruments, venues, generator, risk limits, execution,
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
  dropped capabilities, and the namespace carries
  `pod-security.kubernetes.io/enforce: restricted`.
- Ingress is default-deny; only the flows in
  `deployment/k8s/networkpolicy.yaml` (prometheus→java:8080,
  grafana→prometheus:9090, ingress→grafana:3000) are open.
- Raw market data is immutable (spec §1): pipeline outputs are written once
  per dated run; nothing rewrites `data/raw/` in place.

## 5. Reporting

Suspected vulnerabilities or leaked credentials go to the platform owners
(CODEOWNERS for `deployment/` and `docs/governance/`) immediately; kill-switch
and incident procedure in `docs/runbooks/RUNBOOK_incident_kill_switch.md`
applies if production trading could be affected.
