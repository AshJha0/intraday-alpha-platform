# tests/replay — determinism (same seed ⇒ identical bytes)

The replay level of the testing strategy (`tests/README.md`) pins the
platform's reproducibility promise (PLATFORM_CONVENTIONS.md §3,
docs/governance/REPRODUCIBILITY.md): **a seeded run, repeated, produces the
same bytes** — not merely the same numbers within tolerance.

What belongs here:

- generator determinism — the same seed produces byte-identical event vectors
  (`test_generator_determinism.py`: the golden EQ vector generated twice in
  one process, compared as canonical JSONL bytes, IAP1 bytes and SHA-256, and
  against the pinned `tests/golden/expected_codec_sha256.json`);
- replay determinism — a pipeline stage re-run over the same input emits the
  same output bytes (books, feature vectors, fills, risk audit JSONL);
- the MVP loop run twice from scratch and replayed from its captured stream
  (`test_mvp_replay_determinism.py`: `python -m iap.mvp verify` / `replay` on
  `configs/mvp/mvp_tiny.json` — identical event-stream sha256, trace digest,
  `traces.jsonl`, `report.json` and `risk_audit.jsonl` bytes; docs/MVP.md §5);
- cross-run comparisons of any artefact `docs/governance/REPRODUCIBILITY.md`
  says is byte-stable.

What does not: value-level golden comparisons (those are `tests/golden/` and
the per-language golden groups) and single-component behaviour (unit suites).

Run from the repository root (no `PYTHONPATH` needed — `tests/conftest.py`
adds `python/src`):

    python3 -m pytest -q tests/replay

`tests/harness/run_all.sh` runs this directory as the `replay` row of the
parity table.
