# Review pack for the outside reviewer (v1.18)

This pack is for the independent reviewer named in the v1.18 release programme (section 9.3).
It says what to check, how to reproduce the frozen baseline and, later, the REL-04b execution
JSON on your own machine, how to verify the signed documents, and what the platform cannot
show. Access is read-only: the bound commit, the kept datasets' hashes
([research/data_manifest.json](../research/data_manifest.json)) and this repository.

The code, tests, reviews and documents were produced with Claude (AI) assistance; the owner
merges and signs. Nothing in this pack asks you to trust that text: every number below comes
from a committed artefact and a command that recomputes it.

## 1. The three deliverables (programme section 9.3)

| | deliverable | inputs in this repository | status |
|---|---|---|---|
| a | written approval or rejection of the statistical gate specification (the known-null protocol) before it becomes mandatory | [research/v118/NULL_PROTOCOL.md](../research/v118/NULL_PROTOCOL.md), [research/v118/known_null_config.json](../research/v118/known_null_config.json), runner `iap.research.known_null`, `tools/rel06_known_null.py` | pending: protocol not yet signed on main, nothing run |
| b | reproduction of the REL-04b JSON and of one alpha-track scorecard from this pack alone, with a note of what could not be reproduced | section 3 below | pending: the REL-04b JSON is not yet on main |
| c | a findings memo on the pre-registration process, the simulator rules and the holdout registry | section 2 below | pending |

If you disagree, the disagreement is recorded verbatim in the release notes; a gate does not pass
by overruling you.

## 2. What to check

**Pre-registration and analysis-identity binding (gate F1).** A signed prereg can bind an
`analysis_identity`: script fingerprints, the `python/src/iap` code-tree hash, commit, data
versions, family, metrics and thresholds. `prereg_gate.require(..., identity_actual=)` and
`tools/r7_confirm.py` refuse on any mismatch, and the runner must declare an actor that is not the
signer and hold no signing key. Code: `iap.agents.analysis_identity`, `tools/prereg_bind_identity.py`;
tests: [python/tests/test_analysis_identity.py](../python/tests/test_analysis_identity.py). Question to
answer: can a changed script, configuration or dataset reuse a prior registration?

**The holdout registry (gate F4).** [research/holdout_registry.json](../research/holdout_registry.json)
is content-hash keyed with per-session and per-symbol scope, checked by
`iap.research.holdout_registry.guard` in every supported research path; access attempts are
logged. Tests: [python/tests/test_holdout_registry.py](../python/tests/test_holdout_registry.py)
(path rename, symlink, copied directory, date-label selection). Question: is there a research path
that reads a spent or unread dataset without passing the guard?

**The statistics of the known-null protocol (gate F3).** [NULL_PROTOCOL.md](../research/v118/NULL_PROTOCOL.md):
generator A (session derangements, one permutation for all names, matched time-of-day cell),
generator B (within-session AR(1) with Cholesky cross-name innovations), per-session means,
CR1 + Student-t, Webb six-point wild-cluster bootstrap by full enumeration for G < 10, Holm over K,
pass = point estimate at most 5 % and Wilson upper at most 7 %. Question: is the size claim valid
at G of 5 sessions or fewer, and does every output say the inference rests on G clusters?

**The simulator rules and their known limits (gates F8, F10).** Rule text:
[docs/EXECUTION_QUALITY.md](EXECUTION_QUALITY.md) sections 9-10 and
[docs/SIM_TO_REAL.md](SIM_TO_REAL.md); correctness suites
[python/tests/test_property_replay.py](../python/tests/test_property_replay.py) (quantity
conservation, taker fills backed by displayed depth, maker fills within printed volume) and
[python/tests/test_rel04_child_execution.py](../python/tests/test_rel04_child_execution.py)
(adversarial events, residual never filled). Question: can the simulator fill what the tape could
not have filled, consume the same liquidity twice, or hide unfilled quantity?

## 3. Reproduce

Use the CI lock, not a hand-built environment (the reproducibility contract, programme section 9.6:
canonical JSON, stable row order, pinned dependencies; counts exact, bps to two decimals).

```bash
python -m venv .venv-ci && .venv-ci/bin/pip install --require-hashes -r python/requirements-ci.txt
```

**REL-01 baseline** ([research/v118/BASELINE_v1_18.md](../research/v118/BASELINE_v1_18.md), from
committed JSON only; no market data is read):

The baseline binds commit `8fb189b67bd6a2b6a2c868ed378d2a5a92c628f0` (branch
`feat/v118-rel01-baseline`, squash-merged to main as `ed86081`). Recompute it at that commit:

```bash
git worktree add ../iap-rel01 8fb189b67bd6a2b6a2c868ed378d2a5a92c628f0
```

```bash
cd ../iap-rel01 && PYTHONPATH=python/src python tools/v118_baseline.py --check
```

`--check` exits 1 if `research/v118/baseline_metrics.json` differs from what the tool recomputes
(ledger hash chain and counts, R7 verdict fields, alpha registry states, the B8 out-of-sample
headline with its MDE). On a later main commit `--check` reports the file stale, because the
`commit` and toolchain fields (`ci.yml` and lock-file hashes) move with every CI change; the
research inputs do not. On main, the frozen research inputs are checked by:

```bash
PYTHONPATH=python/src python -m pytest -q python/tests/test_v118_baseline.py
``` One headline to reproduce: the repo ledger holds 5,732 recorded looks, of
which 24 are real-data looks (`ledger.total_looks`, `ledger.real_data_looks_per_document_summary`).

**Clean-environment reproduction (review finding 6).** One command clones the repository at a
commit into a temporary folder, builds the venv from the CI lock with `--require-hashes`, records
the interpreter and every package version, runs the baseline tool and its `--check`, compares the
regenerated JSON with the committed one under section 9.6 and writes a record (commit, environment,
commands, exit codes, output tails, discrepancies). Stdlib only; run it with any Python 3.12:

```bash
python tools/reproduce_baseline.py --repo https://github.com/AshJha0/intraday-alpha-platform.git --commit 8fb189b67bd6a2b6a2c868ed378d2a5a92c628f0 --out repro/baseline.json
```

Exit 0 = reproduced under the contract. The maintainer's own run is committed as
[review/REPRODUCTION_BASELINE_2026-10-18.md](review/REPRODUCTION_BASELINE_2026-10-18.md).

**REL-04b execution JSON (placeholder, to be filled when the B8 v2 run finishes).** The REL-04b
benchmark is not on main yet. When it lands its PR fills in:

- bound commit: `TBD` (the commit named in the signed pre-declaration)
- command: `TBD` (exact `iap.research.execution_benchmark` invocation, venue file
  `research/execution/b8/venues_xnas_iexg_v2.json`)
- inputs: the six declared sessions (needs the ITCH + IEX data; not reproducible from the clone alone)
- output path and sha256: `TBD`
- comparison: byte-identical where the goldens already achieve it, otherwise within the declared
  tolerance (bps to two decimals, counts exact) with every difference listed.

**REL-08 alpha scorecard (placeholder, to be filled when the REL-08 screen run finishes).**

- bound commit and signed continuation rule: `TBD` (`research/v118/rel08_rule_signed.json`)
- command: `TBD`
- inputs: `TBD` (data sessions; holdout registry state)
- output path and sha256: `TBD`
- comparison: as above; every verdict field (including INCONCLUSIVE) exact.

## 4. Signed documents and how to verify them

Signatures are Ed25519 over the canonical JSON of the document without its `signature` block,
under the maintainer key in [research/agents/trust_roots.json](../research/agents/trust_roots.json)
([docs/governance/TRUST_ROOTS.md](governance/TRUST_ROOTS.md)). Signed on main today:
`research/preregs/breadth_book_prereg.json`, `research/quoting_real/prereg_v2.json`,
`research/r7/ai4_deeplob_prereg.json`, `research/r7/r7_confirm_prereg.json`. Verify all signed
JSON documents under `research/` (prints OK or the reason per file):

```bash
PYTHONPATH=python/src python -c "import json,subprocess;from pathlib import Path;from iap.agents.analysis_identity import verify_signed_document as v;[print(p, v(Path('.'), d) and 'OK') for p in subprocess.run(['git','ls-files','research/*.json'],capture_output=True,text=True).stdout.split() for d in [json.load(open(p,encoding='utf-8'))] if isinstance(d,dict) and d.get('signed') is True]"
```

The agent key registry and the blackboard chain verify with:

```bash
PYTHONPATH=python/src python -m iap.agents.cli verify-board --trust-ref origin/main
```

The known-null protocol and the REL-08 continuation rule are written but not signed on main;
their signatures will bind the runner scripts.

## 5. Known limitations

- **Level 2 fill validation is not reachable.** The fill model passes Level 1 (deterministic replay
  validation) only. Level 2 would compare it with real orders and real fills on held-out days; no
  real fills exist (decision D4(a)), so nothing here is empirical validation of the live fill model
  ([docs/SIM_TO_REAL.md](SIM_TO_REAL.md)).
- **Three liquid names.** The real data is Nasdaq ITCH sessions on AAPL, MSFT and QQQ, plus IEX HIST
  overlap days; nothing generalises to a broad or illiquid universe.
- **Small G.** Real-data inference rests on few sessions (seven in-sample multi7 sessions plus two
  holdout sessions; one session for the REL-08 screen). The known-null protocol reports size, not
  power, at G of 5 or fewer.
- **No latency edge.** Every latency figure is an in-process measurement on a shared CI runner or a
  small container; benchmarks are not colocation numbers.
- **Ledger coverage.** 24 of the 5,732 recorded looks in the repo ledger are real-data looks; the
  real in-sample looks on multi7 and the holdouts are in per-dataset ledgers under `data/real`,
  outside git and outside this count ([BASELINE_v1_18.md](../research/v118/BASELINE_v1_18.md)).
