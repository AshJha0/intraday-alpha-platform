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
#   stage 1: eclipse-temurin:21 (JDK) — java/build.sh (javac -Xlint:all
#            -Werror) + java/test.sh (JUnit4 suite, incl. golden group)
#   stage 2: eclipse-temurin:21-jre — classes + golden vectors only.
#
# Build (from the REPO ROOT):
#   docker build -f deployment/docker/Dockerfile.java -t iap/java:1.0.0 .
#
# The entrypoint is the paper-trading vertical (com.iap.platform.PaperTrading):
# book -> features -> alphas -> portfolio -> risk -> execution over the golden
# equity vector, paced in realtime (60x) so the session is scrapeable, with
# the monitoring API (com.iap.api.MetricsServer) serving Prometheus text
# exposition on :8080 GET /metrics plus /health and /status. The healthcheck
# hits /health for real (bash /dev/tcp — the JRE image ships neither curl nor
# wget, and the build must stay offline-agnostic).
# Digest pinning policy: see Dockerfile.python header / SECURITY.md.
# =============================================================================

# ---------------------------------------------------------------- build stage
FROM eclipse-temurin:21 AS build
# digest-pin at release: eclipse-temurin:21@sha256:<record-me>

WORKDIR /build
COPY java java
COPY tests/golden tests/golden
COPY configs configs

# JUnit4 + Hamcrest must be LOCAL jars (no network) — note they are two
# SEPARATE jars: junit4.jar and hamcrest-core.jar (docs/BUILD_NOTES.md).
# Provide them in the build context or bake them from the CI base image;
# test.sh pins both at /usr/share/java. The image build compiles main sources
# exactly as CI does (build.sh); the JUnit test pass runs when both jars are
# present, and is otherwise executed by tests/harness/run_all.sh on the CI
# host (which has /usr/share/java/{junit4,hamcrest-core}.jar).
RUN cd java && ./build.sh
RUN if [ -f /usr/share/java/junit4.jar ] && [ -f /usr/share/java/hamcrest-core.jar ]; then \
        cd java && ./test.sh; \
    else \
        echo "junit4/hamcrest jars not present in build context; test pass deferred to CI harness"; \
    fi

# -------------------------------------------------------------- runtime stage
FROM eclipse-temurin:21-jre
# digest-pin at release: eclipse-temurin:21-jre@sha256:<record-me>

RUN groupadd --gid 10001 iap && \
    useradd --uid 10001 --gid iap --create-home --shell /usr/sbin/nologin iap

WORKDIR /app
COPY --from=build /build/java/out/main /app/classes
COPY --from=build /build/tests/golden /golden
COPY --from=build /build/configs /app/configs

USER iap

# Monitoring API port (com.iap.api.MetricsServer — GET /metrics Prometheus
# text format, /health, /status; port pinned to 8080 here to match the
# prometheus scrape config regardless of the mounted execution.json).
EXPOSE 8080

# Real healthcheck against the live /health endpoint. The JRE base image has
# no curl/wget, so use bash's /dev/tcp (present in eclipse-temurin/Ubuntu).
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["bash", "-c", \
    "exec 3<>/dev/tcp/127.0.0.1/8080 && printf 'GET /health HTTP/1.0\\r\\n\\r\\n' >&3 && grep -q '\"status\":\"ok\"' <&3"]

# Low-latency-conscious JVM defaults (spec §23): predictable GC, GC pause
# visibility for the GcPauseHigh alert.
ENV JAVA_OPTS="-XX:+UseZGC -Xms256m -Xmx1g -Xlog:gc*:stdout:time,level,tags"

# Paper-trading session over the golden vector, paced 60x realtime so the
# /metrics endpoint has a live session to report while Prometheus scrapes.
ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -cp /app/classes \
    com.iap.platform.PaperTrading \
    --configs /app/configs \
    --events /golden/events_eq_mbo.jsonl \
    --mode realtime --speed 60 \
    --port 8080 \
    --report /tmp/paper_session_report.json"]
