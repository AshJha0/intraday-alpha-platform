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
# Digest pinning policy: see Dockerfile.python header / SECURITY.md (base images are
# pinned by digest below; Dependabot's docker ecosystem proposes bumps).
# =============================================================================

# ---------------------------------------------------------------- build stage
FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587 AS build

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
# <golden>/../../configs (test_alpha_golden.cpp, test_replay_fills.cpp,
# test_replay_trace.cpp) and bench_all reads the same path AT RUNTIME
# (bench_exec_config in bench_all.cpp). Without them `RUN ctest` fails and
# the runtime container exits at startup. The canonical-JSON / decision-trace
# goldens (test_canonical_json_golden.cpp, test_trace_golden.cpp) read only
# tests/golden.
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
FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587

RUN groupadd --gid 10001 iap && \
    useradd --uid 10001 --gid iap --create-home --shell /usr/sbin/nologin iap

# bench_all resolves the golden dir from $IAP_GOLDEN_DIR, falling back to the
# path compiled in at build time (iap/util/data_paths.hpp). Setting it below
# states the image's layout explicitly instead of inheriting the build tree's,
# and every path derived from it (configs/, data/) is collapsed lexically, so
# the binary never needs an intermediate build directory that this stage does
# not copy. Keeping the same absolute layout as the build stage is therefore
# belt-and-braces rather than load-bearing.
COPY --from=build /build/cpp/build/bench_all /usr/local/bin/iap-replay-sim
COPY --from=build /build/tests/golden /build/tests/golden
# bench_all reads IAP_GOLDEN_DIR/../../configs at runtime — keep the shape.
COPY --from=build /build/configs /build/configs
COPY --from=build /build/data/reference /build/data/reference

# The image's data root, read by iap::golden_dir() (iap/util/data_paths.hpp).
# check_docker_build.py asserts this is set and that the directory it names is
# one this stage actually copies.
ENV IAP_GOLDEN_DIR=/build/tests/golden

USER iap
WORKDIR /home/iap

# No HEALTHCHECK, deliberately. This is a finite batch replay that exits when
# the pass is done, and the binary has no cheap self-test mode: its only
# argument is the path of a results file to write, so `--version`/`--help`
# would create a file of that name instead of probing.
# The previous `test -x <binary>` could never fail, so it reported health it
# did not measure. Liveness for a batch job is its exit code (compose
# service_completed_successfully, k8s Job status).

# Runs the full deterministic replay + execution-sim benchmark pass and prints
# throughput/latency methodology output. Pass an argument (a writable path,
# e.g. /data/results_cpp.md) to also persist the results table.
ENTRYPOINT ["/usr/local/bin/iap-replay-sim"]
