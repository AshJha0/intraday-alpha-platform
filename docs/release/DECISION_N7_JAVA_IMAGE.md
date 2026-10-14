# Decision N-7: the Java deployment image (closed in v1.16)

**Status: PROPOSED for owner acceptance (v1.16 plan item 16).** This closes the
open decision N-7 ("retire the Java deployment image: compose, k8s, network
policy, state backup"), raised in the v1.15 plan and deferred on 2026-10-11
("kept for v1.15; `release.yml` keeps building it from `archive/java`").

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
