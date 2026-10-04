# Build & Test Notes

Canonical commands (PLATFORM_CONVENTIONS.md §9). Keep each language's full
test run < 120 s.

**CI** is `.github/workflows/ci.yml`, and it is the local
`tests/harness/run_all.sh` split into parallel jobs: one per language, plus
`tests/harness/run_golden.sh` (the GOVERNANCE promotion-gate-10 artefact: every
language's golden group, including ALL fifteen Java `*GoldenTest` classes),
`tests/harness/check_deployment.py` (promtool rules/config/unit tests, compose
and Dockerfile checks, k8s manifests, ConfigMap sync, dashboard metric
provenance) and an `images` job that builds the four container images and is
gated on all of them. Run the whole thing locally with
`bash tests/harness/run_all.sh`; it prints the parity table plus a
`deployment` row and reports a suite that did not run as `-`, never as 0.

## Python (reference implementation)

```bash
cd python && PYTHONPATH=src python3 -m pytest -q          # full suite (1988 tests; CI runs it as `-n auto --dist loadfile` with coverage, 149 s on a 4-vCPU runner)
cd python && PYTHONPATH=src python3 -m pytest -q -k golden # the golden group (192)
cd python && PYTHONPATH=src python3 -m iap.marketdata      # end-to-end pipeline
cd python && PYTHONPATH=src python3 -m iap.mvp run         # the traced MVP loop (under a minute)
cd python && PYTHONPATH=src python3 tools/make_golden.py   # regen goldens (deliberate only; every
                                                           # make_golden_*.py refuses to overwrite without --force)
```

- **Timing.** The suite is 1988 tests at v1.5.0 (192 in the `-k golden` group; 1573 and 166 at v1.4.0).
  Measured in CI on the `ubuntu-24.04` runner (4 vCPU), Python 3.11, on one
  tree, 2026-10-04 (pull request #23, when the suite was 1672 tests — before
  the other v1.5.0 branches were merged):

  | run | wall time |
  |---|---|
  | serial, with `--cov` (how CI ran it until v1.4.0) | 524 s |
  | `-n auto --dist loadfile`, with `--cov` (how CI runs it now) | **149 s** |
  | serial, without coverage | 110 s |

  Earlier CI runs of the 1565-test v1.3.0 suite read 319 s and 358 s serial under
  `--cov`; the 524 s above is the same job on this tree and day. Coverage
  tracing, not the tests, is most of the serial time. Where the time goes (serial,
  per-test `--durations`): `test_eq03_report_reproduces_through_the_runner` 61 s,
  the MVP golden run and its replay 40 + 37 s (module fixtures), the planted-signal
  tests about 110 s in total, `test_mvp.py` about 60 s, `test_generator.py` about 65 s;
  few other tests exceed 10 s. **The 120 s target is therefore not met by the
  instrumented CI run (149 s) and is met by the uninstrumented serial run (110 s)**;
  the harness capture on the 2-CPU container baseline
  (2026-09-20, 1362 tests) read 83 s. Nothing was removed or skipped to make
  a number fit; the parallel run collected the same 1672 tests and reported the same
  outcome for every one of them as the serial run.
- Python 3.11, src layout (`python/src/iap`), packaging via `python/pyproject.toml`
  (1.4.0; installable with `pip install -e python` if preferred over PYTHONPATH;
  console scripts `iap-marketdata`, `iap-features`, `iap-tca`, `iap-research`,
  `iap-lifecycle`, `iap-store`, `iap-mvp`).
- Dependencies (declared in `pyproject.toml`): numpy, pandas, scipy, scikit-learn,
  **pyarrow** (Parquet research dataset), **jsonschema ≥ 4.18 + referencing**
  (offline JSON-schema validation of every contract — `iap.contracts.validate`);
  extras `ml` (xgboost, lightgbm) and `dev` (pytest, pyyaml — the latter for
  `tools/github/create_issues.py` and its integration test). If missing:
  `pip install jsonschema referencing pyarrow --break-system-packages`; CI installs
  the same set in every Python job (`.github/workflows/ci.yml`).
- The pipeline writes `data/raw/*.jsonl`, `data/normalized/*.normalized.{jsonl,iap1}`,
  `data/normalized/events.parquet`, `data/normalized/qc_report.json`.
  Identical seed => bit-identical outputs; do not commit generated data.

## C++

```bash
cd cpp && bash build.sh && ctest --test-dir build --output-on-failure
```

C++17, g++13/CMake/GoogleTest/Eigen available; `-Wall -Wextra -Werror` clean; build
with `-j2` (2-CPU environment); 302 tests, 72 in the golden group. Layout: `include/iap/{marketdata,orderbook,features,
alpha,execution,sor,replay,contracts,util}` + `src/` mirrors; `contracts/` is the
canonical-JSON / decision-trace contract (`canonical_json.hpp`, `trace.hpp`),
`util/sha256.hpp` the SHA-256 the codec goldens and the trace digest share. The
golden group is `ctest --test-dir build -R Golden` (suites `*Golden`, including
`CanonicalJsonGolden`, `TraceGolden`, `ReplayTraceGolden`).

## Rust

```bash
cd rust && cargo test        # workspace (eleven crates, 358 tests, ~55 s after a full rebuild)
```

Crates: `marketdata`, `orderbook`, `replay`, `eventbus`, `telemetry`, `features`,
`alpha`, `risk`, `venue`, `contracts` (canonical JSON with Python
`json.dumps` byte parity, SHA-256, trace ids, `DecisionTrace` records, JSONL
trace sink + digest, `explain()`) and `lifecycle` (alpha promotion state
machine: gate table, 17-edge transition table, live rolling-IC rules,
byte-identical `research/alpha_registry.json`). Golden targets run by
`tests/harness/run_all.sh`: `golden_marketdata`, `golden_book`, `golden_features`,
`golden_alpha`, `golden_risk`, `golden_replay`, `golden_canonical_json`,
`golden_trace`, `golden_lifecycle`. The lifecycle golden reads
`configs/strategies/{lifecycle,strategies}.json` and
`research/alpha_registry.json` relative to the crate (the Docker image copies
them).

Rust 1.98.1 (pinned in `rust/rust-toolchain.toml`; CI and `Dockerfile.rust` name the same version and `check_deployment.py` fails if they drift; the workspace MSRV stays 1.75; the figures in the papers were measured with rustc 1.95.0), crates.io reachable; keep deps to serde/serde_json only (`rand` was
permitted historically but is NOT used — the pinned RNG is SplitMix64,
conventions §3 — and SECURITY.md §1 allows serde/serde_json alone) (+ crossbeam
where justified); zero warnings. `serde_json` is built with its
`float_roundtrip` feature (workspace `Cargo.toml`): the default parser is
best-effort on 17-digit decimals (up to 1 ulp off), which would break the
canonical-JSON byte parity the contracts / lifecycle goldens pin.

## Java — why there is NO Maven build

```bash
cd java && bash build.sh && bash test.sh    # 571 tests; the seventeen *GoldenTest classes are 124 of them
```

**Maven/Gradle are deliberately not used: Maven Central is unreachable from
this environment**, so dependency resolution would fail on a clean machine.
The Java build is plain `javac` (Java 21) driven by `build.sh`, with JUnit4
and Hamcrest provided locally as **two separate jars** at
`/usr/share/java/junit4.jar` and `/usr/share/java/hamcrest-core.jar`, both
wired into `test.sh`'s classpath. The dependency set a `pom.xml` would
otherwise declare is exactly:

| GAV (pom.xml equivalent)      | provided by                            | scope |
|-------------------------------|----------------------------------------|-------|
| `junit:junit:4.x`             | `/usr/share/java/junit4.jar`           | test  |
| `org.hamcrest:hamcrest-core`  | `/usr/share/java/hamcrest-core.jar` (separate jar, NOT bundled in junit4.jar) | test  |

No other third-party Java dependencies are permitted; if a future wave needs
one, it must be vendored into `java/lib/` and recorded here (this file is the
normative pom-equivalent list, referenced from the top-level README).

The cross-language contract ports added on 2026-09-19 — `com.iap.contracts`
(canonical JSON, Python-`repr` float layout, sha256 helpers),
`com.iap.trace` (decision-trace records, JSONL sink, stream digest,
`explain()`), `com.iap.lifecycle` (alpha promotion lifecycle + registry) —
use only the JDK (`java.security.MessageDigest`, `java.math`); the golden
gates `CanonicalJsonGoldenTest`, `TraceGoldenTest`, `LifecycleGoldenTest`
are listed in `tests/harness/run_all.sh` `JAVA_GOLDEN_CLASSES`, which
`check_deployment.py` keeps equal to the `*GoldenTest.java` set on disk.

## Benchmarks

`benchmarks/` builds per-language; methodology (2 CPUs, `-j2`, pinned compiler
versions) must be stated next to every number. `benchmarks/RESULTS.md` is the
index; the C++ `bench_all` table lives in `benchmarks/results_cpp.md`.

```bash
cd cpp && cmake --build build -j2 --target bench_all
./build/bench_all ../benchmarks/results_cpp.md                  # regenerate
./build/bench_all ../benchmarks/results_cpp.md <events.jsonl>   # other cold vector
```

`results_cpp.md` is GENERATED — never hand-edit it, including its caveats:
`bench_all` emits the methodology header and the cache-residency caveat
itself precisely so a regeneration cannot drop them. It also appends a cold
reference table (one single pass over `data/normalized/eq_20260824.
normalized.jsonl` by default), which is omitted with a printed note when
`data/` has not been generated. Regenerate only on an **idle** box —
`cat /proc/loadavg` first; the other agent's build will otherwise show up
as a hot-path regression.
