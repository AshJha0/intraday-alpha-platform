# =============================================================================
# Dockerfile.cpp — IAP low-latency C++ replay / execution-simulator image
#
# Two-stage build (docs/BUILD_NOTES.md):
#   stage 1: g++-13-era toolchain + CMake, exactly cpp/build.sh
#            (Release, -Wall -Wextra -Werror, -j2)
#   stage 2: slim runtime carrying only the bench_all binary (decode + book +
#            deterministic replay + feature engine + alpha + execution-sim
#            replay over the golden vectors) and the golden vectors it reads.
#
# Build (from the REPO ROOT):
#   docker build -f deployment/docker/Dockerfile.cpp -t iap/cpp:1.0.0 .
#
# Offline-agnostic: everything comes from Debian packages + the repo itself —
# no external package registries at build time beyond apt.
# Digest pinning policy: see Dockerfile.python header / SECURITY.md.
# =============================================================================

# ---------------------------------------------------------------- build stage
FROM debian:bookworm-slim AS build
# digest-pin at release: debian:bookworm-slim@sha256:<record-me>

RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ cmake make libgtest-dev libeigen3-dev \
    && rm -rf /var/lib/apt/lists/*

# Debian's libgtest-dev may ship sources only; build+install the static libs
# so CMake's `find_package(GTest REQUIRED)` (cpp/CMakeLists.txt) resolves.
RUN if [ ! -e /usr/lib/x86_64-linux-gnu/libgtest.a ] && [ -d /usr/src/googletest ]; then \
        cmake -S /usr/src/googletest -B /tmp/gtest-build && \
        cmake --build /tmp/gtest-build -j2 && \
        cmake --install /tmp/gtest-build && \
        rm -rf /tmp/gtest-build; \
    fi

# Layout matters: cpp/CMakeLists.txt compiles IAP_GOLDEN_DIR as
# <source-dir>/../tests/golden, so keep the repo shape: /build/cpp + /build/tests.
# configs/ and data/reference/ are NOT optional: the golden tests resolve
# <golden>/../../configs (test_alpha_golden.cpp:32, test_replay_fills.cpp:74)
# and bench_all reads the same path AT RUNTIME (bench_all.cpp:133). Without
# them `RUN ctest` fails and the runtime container exits at startup.
# .dockerignore keeps cpp/build (host CMakeCache) out of the context.
WORKDIR /build
COPY cpp cpp
COPY tests/golden tests/golden
COPY configs configs
COPY data/reference data/reference

# Exactly cpp/build.sh (Release, -j2 — 2-CPU baseline, BUILD_NOTES.md).
RUN cd cpp && bash build.sh

# Tests run at image-build time: a production image is never published from a
# tree whose golden/parity suite fails (GOVERNANCE.md promotion gate 10).
RUN cd cpp && ctest --test-dir build --output-on-failure

# -------------------------------------------------------------- runtime stage
FROM debian:bookworm-slim
# digest-pin at release: debian:bookworm-slim@sha256:<record-me>

RUN groupadd --gid 10001 iap && \
    useradd --uid 10001 --gid iap --create-home --shell /usr/sbin/nologin iap

# bench_all resolves the golden dir from its compiled-in path (/build/tests/
# golden); keep the same absolute layout in the runtime image.
COPY --from=build /build/cpp/build/bench_all /usr/local/bin/iap-replay-sim
COPY --from=build /build/tests/golden /build/tests/golden
# bench_all reads IAP_GOLDEN_DIR/../../configs at runtime — keep the shape.
COPY --from=build /build/configs /build/configs
COPY --from=build /build/data/reference /build/data/reference

USER iap
WORKDIR /home/iap

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD test -x /usr/local/bin/iap-replay-sim || exit 1

# Runs the full deterministic replay + execution-sim benchmark pass and prints
# throughput/latency methodology output. Pass an argument (a writable path,
# e.g. /data/results_cpp.md) to also persist the results table.
ENTRYPOINT ["/usr/local/bin/iap-replay-sim"]
