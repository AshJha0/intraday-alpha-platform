# tests/integration — cross-component end-to-end runs

The integration level of the testing strategy (`tests/README.md`): several
components wired together the way the platform wires them, driven by a real
input (a golden vector or the generated dataset), asserting the observable
contract at the end of the chain — not the internals of any one stage.

What belongs here:

- decode → order book → feature engine over a golden vector, asserting a
  `FeatureVector` is emitted with the registry hash as `feature_version`
  (`test_pipeline_smoke.py`);
- longer verticals as they become cheap enough to run in CI: features →
  alpha → risk → execution simulator, or the Python pipeline entry points
  (`python3 -m iap.marketdata`, `python3 -m iap.features`) against a temp
  directory.

What does not: per-component unit tests (the per-language suites),
exact-value golden comparisons (`tests/golden/`), and byte-determinism
(`tests/replay/`).

Run from the repository root (no `PYTHONPATH` needed — `tests/conftest.py`
adds `python/src`):

    python3 -m pytest -q tests/integration

`tests/harness/run_all.sh` runs this directory as the `integration` row of
the parity table. Keep every test here well under a minute: this level runs
on every CI push.
