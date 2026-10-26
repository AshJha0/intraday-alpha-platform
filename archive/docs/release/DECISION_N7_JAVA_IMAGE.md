# Decision N-7: the Java deployment image (CLOSED 2026-10-15 — image retired)

**Status: CLOSED 2026-10-15 by owner decision (v1.17 WS-F item F9, widening
F2).** The retirement trigger below was met by the same change set: the
Python process `python -m iap.metrics serve` (`python/src/iap/metrics/`)
serves `/metrics`, `/health`, `/ready` and `/status`; Prometheus, the alert
rules, the promtool tests and the dashboards were moved to it; and the
kill-switch runbook points at the config path (there is no Python admin API,
see "What was done"). The `java` image is gone from `release.yml`, the CI
`images` job and the release manifest (three images: python, cpp, rust);
`Dockerfile.java` lives, unbuilt, under `archive/java/`; the compose services
`java-platform` and `java-platform-state`, the k8s `java-platform.yaml`, the
`iap-java-state*` claims and the operator network-policy rule are replaced by
`platform-metrics` / `mvp-session`, `deployment/k8s/platform-metrics.yaml`,
`iap-platform-state*` and a Prometheus-only ingress rule. `archive/java`
stays read-only (frozen at v1.14.0, not built, not a port target); its risk
engine is the byte-identical mirror of the Rust risk engine checked by
`tests/golden/expected_risk_*.json`, the one edit the archive accepts.

## What was done (v1.17, F9)

- **Metric names.** Every name the Java container exposed is accounted for
  in `iap.metrics.exporter`: 30 LIVE families with a Python producer (the
  MVP run's `report.json` / `traces.jsonl`, the session marker, the risk
  limits in `configs/risk/risk.json`) and 16 RETIRED Java-only families
  exposed as `<name>_available 0` (`python -m iap.metrics names`;
  `deployment/grafana/README.md`). The alerts, recording rules, SLO and
  dashboard panels that read only retired names were removed and are listed
  in the header of `deployment/prometheus/alerts.yml`.
- **Admin API.** Not replaced. The Java `POST /admin/*` kill path had no
  Python counterpart to point the runbook at, so the manual halt is the
  config path (`configs/risk/risk.json` `global.kill_switch_engaged`, then
  regenerate the ConfigMap and restart), and `KillPendingNotRecorded`,
  `AdminAuthRateLimited`, `AdminAuditSuppressed` are retired.
- **Digest pinning (F2).** `python tools/release.py publish` now rewrites the
  Kubernetes manifests to `<name>:vX.Y.Z@sha256:<digest>` from the release's
  `release-manifest.json` (`pin-digests` alone, dry-run by default); compose
  keeps the tag because a `build:` service cannot carry a digest.

The text below is the v1.16 decision as it stood, kept for the record.

---

This closed the open decision N-7 ("retire the Java deployment image:
compose, k8s, network policy, state backup"), raised in the v1.15 plan and
deferred on 2026-10-11 ("kept for v1.15; `release.yml` keeps building it from
`archive/java`").

## Decision

N-7 is closed as an open question: the Java image is **kept frozen**. It is not
removed in v1.16, and nothing more is invested in it. It is removed only when
the retirement trigger below is met. That needs no new decision, just a PR.

## What is kept

- `deployment/docker/Dockerfile.java`. It builds the `java` platform image from
  the frozen copy in `archive/java` (source state of tag `v1.14.0`).
- The `java` image in `release.yml` (build, push to ghcr, attestation) and in
  the `supply-chain` CI job (build plus Trivy scan), so the published image
  stays scanned and attested like the other three.
- `docker-compose.yml` services `java-platform` and `java-platform-state`.
- `deployment/k8s/java-platform.yaml`, the `iap-java-state` claim in
  `pvc.yaml`, `cronjob-state-backup.yaml`, and the java rules in
  `networkpolicy.yaml` (Prometheus scrape and operator admin access).
- The Prometheus scrape config, alert rules, promtool tests and Grafana
  dashboards that read the Java platform's metrics.

## What is dropped

- N-7 as an open owner decision. It is no longer carried in the plan.
- Any further work on the Java engine or image: no new features, no Java CI
  tests (already removed when Java was archived in v1.15), and no leader
  election for the trading singleton (the backup CronJob comment records it
  as not implemented; it stays not implemented).
- Any claim that the image matches current goldens. It runs v1.14.0 code,
  and `archive/java/README.md` already says the goldens may move on without it.

## Why

1. **It is the only metrics source.** `deployment/prometheus/prometheus.yml`
   scrapes one target: the Java platform (`/metrics` on :8080). The alert
   rules, the promtool SLO/alert tests and the Grafana dashboards are built on
   its `exec_*` and risk metrics, and the kill-switch admin API used in
   `RUNBOOK_incident_kill_switch.md` lives in it. Removing the image now would
   leave the monitoring and alerting stack with nothing to monitor, or force
   a rewrite of the dashboards, alerts and runbooks in a release whose focus is
   monetisation.
2. **The cost is small and bounded.** One extra `docker build` plus a Trivy
   scan in `supply-chain` and in the release. That image build is the only
   place the Java code is still compiled; no CI job runs its tests.
3. **It is honest as it stands.** The image is labelled frozen (archive README,
   POLYGLOT.md). Keeping it does not imply it is maintained.

## Retirement trigger

Remove the Java image (Dockerfile, release and supply-chain matrix entries,
compose services, k8s Deployment, claim, backup CronJob and network-policy
rules) in one PR once **both** hold:

- a Python process (e.g. the v1.16 forward paper-trade runner) exposes
  `/metrics`, and Prometheus, the alert rules, the promtool tests and the
  dashboards have been moved to it; and
- the kill-switch runbook points at the Python process's admin path.

Also remove it earlier if Trivy reports a HIGH/CRITICAL finding in the Java
image with no fix that a base-image bump can deliver, instead of patching
frozen code.
