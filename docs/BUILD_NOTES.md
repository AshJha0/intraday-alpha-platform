# Build & Test Notes

Canonical commands (PLATFORM_CONVENTIONS.md §9; CI entry point is
`tests/harness/run_all.sh`). Keep each language's full test run < 120s.

## Python (reference implementation)

```bash
cd python && PYTHONPATH=src python3 -m pytest -q          # full suite (~20-30s)
cd python && PYTHONPATH=src python3 -m iap.marketdata      # end-to-end pipeline
cd python && PYTHONPATH=src python3 tools/make_golden.py   # regen goldens (deliberate only)
```

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
cd cpp && ./build.sh && ctest --test-dir build --output-on-failure
```

C++17, g++13/CMake/GoogleTest/Eigen available; `-Wall -Wextra` clean; build with
`-j2` (2-CPU environment).

## Rust

```bash
cd rust && cargo test        # workspace
```

Rust 1.95, crates.io reachable; keep deps to rand/serde/serde_json (+ crossbeam
where justified); zero warnings.

## Java — why there is NO Maven build

```bash
cd java && ./build.sh && ./test.sh
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
