# =============================================================================
# Dockerfile.java — IAP Java platform image (strategy/backtest/monitoring layer)
#
# OFFLINE-AGNOSTIC BY DESIGN: there is deliberately NO Maven/Gradle step.
# Maven Central is unreachable from the build environment, so the build is
# plain `javac` driven by java/build.sh with JUnit4 provided as a local jar —
# docs/BUILD_NOTES.md is the normative pom-equivalent dependency list
# (junit:junit:4.x + hamcrest-core, test scope only; nothing else permitted).
#
# Two-stage build:
#   stage 1: eclipse-temurin:21 (JDK) — java/build.sh (javac -Xlint:all -Werror)
#   stage 2: eclipse-temurin:21-jre — classes + golden vectors + configs.
#
# TEST GATE — read this before quoting GOVERNANCE promotion gate 10.
# The JUnit4 suite is NOT run in this image: eclipse-temurin:21 ships no
# /usr/share/java/{junit4,hamcrest-core}.jar and the build may not reach a
# package registry, so a `RUN test.sh` here would silently no-op (it did, for
# every build, until round 3). The gate is real but it lives in CI:
# .github/workflows/ci.yml runs tests/harness/run_all.sh (all four languages,
# including the Java golden group) and tests/harness/run_golden.sh, and the
# image job depends on it. NEVER publish this image from a tree whose CI run
# is not green.
#
# Build (from the REPO ROOT):
#   docker build -f deployment/docker/Dockerfile.java -t iap/java:1.0.0 .
#
# The entrypoint is the paper-trading vertical (com.iap.platform.PaperTrading):
# book -> features -> alphas -> portfolio -> risk -> execution over the golden
# equity vector, paced in realtime (60x) so the session is scrapeable, with
# the monitoring API (com.iap.api.MetricsServer) serving Prometheus text
# exposition on :8080 GET /metrics plus /health, /ready, /status and the
# kill-switch admin API. A session is FINITE: it exits 0 when the vector ends
# (PLATFORM_CONVENTIONS.md §12.3), so compose uses `restart: on-failure` and
# k8s a single-replica Recreate Deployment — not a restart loop.
# Digest pinning policy: see Dockerfile.python header / SECURITY.md.
# =============================================================================

# ---------------------------------------------------------------- build stage
FROM eclipse-temurin:21 AS build
# digest-pin at release: eclipse-temurin:21@sha256:<record-me>

WORKDIR /build
COPY java java
COPY tests/golden tests/golden
COPY configs configs
COPY research/baselines research/baselines

# javac -Xlint:all -Werror, exactly as CI builds it.
RUN cd java && bash build.sh

# -------------------------------------------------------------- runtime stage
FROM eclipse-temurin:21-jre
# digest-pin at release: eclipse-temurin:21-jre@sha256:<record-me>

RUN groupadd --gid 10001 iap && \
    useradd --uid 10001 --gid iap --create-home --shell /usr/sbin/nologin iap

WORKDIR /app
COPY --from=build /build/java/out/main /app/classes
COPY --from=build /build/tests/golden /golden
COPY --from=build /build/configs /app/configs
# Research signal baselines (API_ADAPTIVE.md): arm the live drift monitor
# (alpha_live_vs_backtest_drift) with the pinned PSI baselines.
COPY --from=build /build/research/baselines /app/baselines

# Durable platform state (PLATFORM_CONVENTIONS.md §12.3): risk snapshot,
# session accounting, risk/config/admin audit JSONL. MOUNT A VOLUME HERE —
# without one, a restart cannot resume and the kill-switch latch is lost.
RUN mkdir -p /data/state && chown -R iap:iap /data
VOLUME ["/data"]

USER iap

# Monitoring API port (com.iap.api.MetricsServer — GET /metrics Prometheus
# text format, /health, /ready, /status; POST /admin/* when an admin token is
# configured; port pinned to 8080 here to match the prometheus scrape config
# regardless of the mounted execution.json).
EXPOSE 8080

# Real healthcheck against the live /health endpoint. The JRE base image has
# no curl/wget, so use bash's /dev/tcp (present in eclipse-temurin/Ubuntu).
# /health is 503 when the trading loop is wedged or the session failed; a
# latched kill switch stays 200 with "trading":"halted" (§12.5).
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["bash", "-c", \
    "exec 3<>/dev/tcp/127.0.0.1/8080 && printf 'GET /health HTTP/1.0\\r\\n\\r\\n' >&3 && grep -q '\"status\":\"ok\"' <&3"]

# Low-latency-conscious JVM defaults (spec §23): predictable GC, GC pause
# visibility for the GcPauseHigh alert.
ENV JAVA_OPTS="-XX:+UseZGC -Xms256m -Xmx1g -Xlog:gc*:stdout:time,level,tags"

# Configuration directory (PLATFORM_CONVENTIONS.md §12.2): the entrypoint
# honours IAP_CONFIG_DIR, so mounting an edited configs/ (compose bind mount)
# or a ConfigMap (k8s) actually takes effect — the kill-switch runbook's
# "edit risk.json, restart the service" path depends on it. Defaults to the
# configs baked into the image.
ENV IAP_CONFIG_DIR=/app/configs
# Durable state directory (§12.3). Add --resume to the entrypoint arguments
# (or override the command) to restart INTO the checkpoint instead of flat.
ENV IAP_STATE_DIR=/data/state
# Kill-switch admin API (§12.5): unset here on purpose — with no token the
# /admin/* routes are not registered at all. Provide IAP_ADMIN_TOKEN or
# IAP_ADMIN_TOKEN_FILE from a secret to enable the runbook's manual halt.

# Paper-trading session over the golden vector, paced 60x realtime so the
# /metrics endpoint has a live session to report while Prometheus scrapes.
ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -cp /app/classes \
    com.iap.platform.PaperTrading \
    --configs \"${IAP_CONFIG_DIR:-/app/configs}\" \
    --events /golden/events_eq_mbo.jsonl \
    --baselines /app/baselines \
    --state-dir \"${IAP_STATE_DIR:-/data/state}\" \
    --mode realtime --speed 60 \
    --port 8080 \
    --report /data/paper_session_report.json"]
