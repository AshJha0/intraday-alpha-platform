# Build & Test Notes

Canonical commands (PLATFORM_CONVENTIONS.md §9). Keep each language's full
test run < 120 s.

**CI** is `.github/workflows/ci.yml`, and it is the local
`tests/harness/run_all.sh` split into parallel jobs: one per language, plus
`tests/harness/run_golden.sh` (the GOVERNANCE promotion-gate-10 artefact: every
language's golden group, including ALL ten Java `*GoldenTest` classes),
`tests/harness/check_deployment.py` (promtool rules/config/unit tests, compose
and Dockerfile checks, k8s manifests, ConfigMap sync, dashboard metric
provenance) and an `images` job that builds the four container images and is
gated on all of them. Run the whole thing locally with
`bash tests/harness/run_all.sh`; it prints the parity table plus a
`deployment` row and reports a suite that did not run as `-`, never as 0.

## Python (reference implementation)

```bash
cd python && PYTHONPATH=src python3 -m pytest -q          # full suite (~75s on the 2-CPU baseline)
cd python && PYTHONPATH=src python3 -m iap.marketdata      # end-to-end pipeline
cd python && PYTHONPATH=src python3 tools/make_golden.py   # regen goldens (deliberate only)
```

- **Timing.** ~75 s is what the harness measures: the `python` row of the README
  parity table (captured from `tests/harness/run_all.sh`) reads 75 s, and that row
  is this full suite plus the much faster `-k golden` re-run, so the pytest command
  above is very nearly all of it. This file used to say "~30 s", which was stale by
  a wide margin and contradicted the parity table — the only other statement of the
  same quantity in the repo. As everywhere else here, the number is for the 2-CPU
  container baseline and will move with the machine.
- Python 3.11, src layout (`python/src/iap`), packaging via `python/pyproject.toml`
  (installable with `pip install -e python` if preferred over PYTHONPATH).
- Extra dependency: **pyarrow** (Parquet research dataset). If missing:
  `pip install pyarrow --break-system-packages`. numpy/pandas/scipy/sklearn/
  pytest are preinstalled; polars/duckdb are pip-installable the same way.
- The pipeline writes `data/raw/*.jsonl`, `data/normalized/*.normalized.{jsonl,iap1}`,
  `data/normalized/events.parquet`, `data/normalized/qc_report.json`.
  Identical seed => bit-identical outputs; do not commit generated data.

## C++

```bash
cd cpp && bash build.sh && ctest --test-dir build --output-on-failure
```

C++17, g++13/CMake/GoogleTest/Eigen available; `-Wall -Wextra` clean; build with
`-j2` (2-CPU environment).

## Rust

```bash
cd rust && cargo test        # workspace
```

Rust 1.95, crates.io reachable; keep deps to serde/serde_json only (`rand` was
permitted historically but is NOT used — the pinned RNG is SplitMix64,
conventions §3 — and SECURITY.md §1 allows serde/serde_json alone) (+ crossbeam
where justified); zero warnings.

## Java — why there is NO Maven build

```bash
cd java && bash build.sh && bash test.sh
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
