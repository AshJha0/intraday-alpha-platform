# RUNBOOK — Backtest & Deterministic Replay

**Scope:** the Python research backtester (`iap.backtest`), the
production-grade replay/execution simulators (C++/Rust/Java) and the
cross-language parity harness.
**Owner:** research + per-language owners. **Related alerts:** `GcPauseHigh`.

## 1. Prerequisites

Fresh market data (backtests read `data/normalized/`):

```bash
cd python && PYTHONPATH=src python3 -m iap.marketdata     # see RUNBOOK_data_pipeline.md
```

## 2. Python research backtester

The fast engine (decision at t, execution at t+latency, spread/fees/impact
costs, exact accounting identity — `python/src/iap/backtest/engine.py`):

```bash
cd python
PYTHONPATH=src python3 -m pytest -q tests/test_backtester.py          # engine invariants
PYTHONPATH=src python3 -m pytest -q tests/test_model_economics.py     # cost-adjusted economics
```

Research runs go through the experiment tracker so every result carries a
manifest (git commit, data/feature/model versions, hardware —
`docs/governance/REPRODUCIBILITY.md`). Check the ledger after a run:

```bash
python3 -m json.tool research/models/ledger.json | head
python3 -m json.tool research/models/<run_id>/manifest.json
```

## 3. Production-grade simulators (per language)

```bash
# C++ — replay + execution sim + fills golden
cd cpp && bash build.sh && ctest --test-dir build --output-on-failure -R 'Replay|Exec|Golden'
./build/bench_all                       # full decode/book/replay/exec throughput+latency pass
./build/bench_all ../benchmarks/results_cpp.md   # ... persisting the methodology table

# Rust — deterministic replay demo over the golden vectors
cd rust && cargo run --release --bin demo

# Java — replay demo (golden vectors, codec SHA-256s, throughput)
cd java && bash build.sh && java -cp out/main com.iap.replay.Demo ../tests/golden
```

Containerized equivalents: `docker compose -f
deployment/docker/docker-compose.yml up cpp-replay rust-replay java-platform`.

## 4. Cross-language parity (the gate that matters)

```bash
bash tests/harness/run_all.sh        # full builds + tests, parity table
bash tests/harness/run_golden.sh     # golden groups only, parity table
```

Both must show every language passing. Promotion gate 10 (GOVERNANCE.md)
requires the golden parity table attached to the PR.

## 5. Failure modes

### A golden test fails in ONE language

That language deviates from the pinned semantics — fix the language, never
the golden. Diff the failing checkpoint (expected files in `tests/golden/`),
then look for: unordered-map iteration, wall-clock reads, float on an
integer path (conventions §3), or a missed pinned rule (e.g. MODIFY
increase→tail, marketable ADD executes — conventions §4).

### Golden tests fail in ALL languages

The vectors changed. `git log tests/golden/` — if a regeneration commit
lacks the mandatory "golden change" note, treat as an incident
(GOVERNANCE.md §3): revert, then review. Regeneration is only ever done
deliberately via `cd python && PYTHONPATH=src python3 tools/make_golden.py`.

### Backtest results differ between runs

They must not (identical seed ⇒ identical outputs). Bisect for wall-clock or
map-ordering leaks; the determinism tests
(`pytest -k determinism`, `ctest -R Determinism`, `cargo test deterministic`)
localize the layer.

### JVM latency issues {#jvm}

`GcPauseHigh` (p99 pause > 10 ms) during Java replay/backtest: check heap
sizing in `JAVA_OPTS` (compose/k8s set ZGC + 256m–1g), then allocation on hot
paths (conventions §8: primitive arrays, no boxing). GC logs go to stdout
(`-Xlog:gc*`); correlate pause timestamps with the order-path latency panel.

### Runtime budget exceeded

Each language's suite must stay < 120 s (conventions §9). If a suite creeps
past, profile it in the PR that caused it — CI treats budget breaks as
failures, not warnings.
