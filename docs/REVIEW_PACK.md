# Review pack for the outside reviewer (v1.18)

> Status at the v1.18.0 tag (2026-10-24): the reviewer's written close-out of 2026-10-23 is in
> [release/REVIEWER_CLOSEOUT_2026-10-23.md](release/REVIEWER_CLOSEOUT_2026-10-23.md). Gate F3 (known-null) failed and stays open,
> F7 (independent rerun of the B8 v2 JSON) is open, and the findings memo (c) was written from inspection, not from running the
> suites. The status column and the placeholders below are updated to that state; see
> [release/v1.18.0_RELEASE_NOTES.md](release/v1.18.0_RELEASE_NOTES.md).

This pack is for the independent reviewer named in the v1.18 release programme (section 9.3).
It says what to check, how to reproduce the frozen baseline and the REL-04b execution
JSON on your own machine, how to verify the signed documents, and what the platform cannot
show. Access is read-only: the bound commit, the kept datasets' hashes
([research/data_manifest.json](../research/data_manifest.json)) and this repository.

The code, tests, reviews and documents were produced with Claude (AI) assistance; the owner
merges and signs. Nothing in this pack asks you to trust that text: every number below comes
from a committed artefact and a command that recomputes it.

## 1. The three deliverables (programme section 9.3)

| | deliverable | inputs in this repository | status |
|---|---|---|---|
| a | written approval or rejection of the statistical gate specification (the known-null protocol) before it becomes mandatory | [research/v118/NULL_PROTOCOL.md](../research/v118/NULL_PROTOCOL.md), [research/v118/known_null_config.json](../research/v118/known_null_config.json), runner `iap.research.known_null`, `tools/rel06_known_null.py`; the signed v3 protocol is [research/v118/NULL_PROTOCOL_v3.md](../research/v118/NULL_PROTOCOL_v3.md) with its result [known_null_v3_RESULT.md](../research/v118/known_null_v3_RESULT.md) | the v3 study was run on 2026-10-23 and FAILED (A0 conditional rejection 79 and 80 of 1,854, about 4.3 %, against 2.5 %); the reviewer rejected the method as the mandatory F3 gate in its current form; F3 stays open and no real-data FWER control is claimed |
| b | reproduction of the REL-04b JSON and of one alpha-track scorecard from this pack alone, with a note of what could not be reproduced | section 3 below | not done: the reviewer inspected the committed B8 v2 report and JSON and the REL-08 scorecard but did not rerun them (the market data and the bound checkout were not available to them); gate F7 is open |
| c | a findings memo on the pre-registration process, the simulator rules and the holdout registry | section 2 below | received 2026-10-23, written from inspection; the reviewer has not run the full test suites or an end-to-end signature verification |

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
``` One headline to reproduce, as frozen at the baseline commit: the repo ledger held 5,732 recorded looks, of
which 24 were real-data looks (`ledger.total_looks`, `ledger.real_data_looks_per_document_summary`). The REL-02
migration (`research/v118/REL02_LEDGER_MIGRATION.md`) then moved the chain to 640 entries and 6,826 looks, with real-data N
(distinct configurations) 24 before and 256 after; the baseline tool's `--check` is for the frozen commit only.

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

**REL-04b execution JSON (B8 v2, run and on main).** The result is `research/execution/b8v2/` (report
`REPORT.md`, the eight `benchmark_v2_oos3*.json` files, `DECISION.json`, `run_manifest.json`).

- bound commit: `4980fc11bd7542ab238af4577554058279505382` (`run_manifest.json`, `commit_bound`)
- signed pre-declaration: `research/preregs/b8v2_prereg.json` (sha256 `1c0191b5bef462f398aa6903feb3291ab0b868ed10ce9816af0dd8c21966df56`)
- command: `research/execution/b8v2/run_b8v2.cmd` (runs `run_b8v2.py` from a clean checkout of the signed commit;
  venue file `research/execution/b8/venues_xnas_iexg_v2.json`)
- inputs: the declared sessions (needs the ITCH + IEX data; not reproducible from the clone alone); dataset hashes are in
  `run_manifest.json` and `research/data_manifest.json`
- run window: 2026-10-19 09:36 to 2026-10-20 01:00 UTC
- comparison: byte-identical where the goldens already achieve it, otherwise within the declared tolerance (bps to two
  decimals, counts exact) with every difference listed. No independent rerun exists (F7 open).

**REL-08 alpha scorecard (one-session screen, run).** Signed rule `research/v118/rel08_rule_signed.json`; scorecard
`research/v118/rel08/SCORECARD_ds_20190130.md` with `screen_ds_20190130.json`. All seven variants are underpowered, and
defects were found afterwards (`docs/release/ANOMALY_REL08_INVESTIGATION_2026-10-20.md`). The v2 rule in
`research/v118/rel08/` is an unsigned draft. Not independently rerun.

## 4. Signed documents and how to verify them

Signatures are Ed25519 over the canonical JSON of the document without its `signature` block,
under the maintainer key in [research/agents/trust_roots.json](../research/agents/trust_roots.json)
([docs/governance/TRUST_ROOTS.md](governance/TRUST_ROOTS.md)). Signed on main at the tag:
`research/preregs/breadth_book_prereg.json`, `research/quoting_real/prereg_v2.json`,
`research/r7/ai4_deeplob_prereg.json`, `research/r7/r7_confirm_prereg.json`,
`research/preregs/b8v2_prereg.json`, `research/preregs/rel04_capts_verify_prereg.json`,
`research/preregs/rel05_prereg.json`, `research/preregs/rel08c_prereg.json`,
`research/preregs/rel09_prereg.json`, `research/preregs/rel10_prereg.json`,
`research/v118/null_protocol_prereg.json` (and `_v2`, `_v3`), `research/v118/rel08_rule_signed.json`
(the tool lists the current set). `rel08_rule_signed.json` carries a stale `status` text ("DRAFT, unsigned") although it
is signed and verifies; it is left unedited because editing it would void the signature. Verify every signed document (trust root against its pinned version, every signed JSON document,
the agent key registry, the main and archived blackboards). It prints `OK` or `FAIL <reason>` per
item and exits 1 on any failure. A blackboard anchor whose commit is not in the clone (a shallow fetch) is reported as
`UNVERIFIABLE-HERE`, never `OK`; `--strict` (reviewer and release, on a full-history clone) makes that
fatal too. Signature, chain and trust-root problems always fail. Run it from a full clone:

```bash
PYTHONPATH=python/src python tools/verify_signatures.py --trust-ref origin/main --strict
```

The maintainer key fingerprint, the pinned trust-root version, and the rotation and revocation
procedure are in [docs/governance/TRUST_ROOT.md](governance/TRUST_ROOT.md).

The agent key registry and the blackboard chain verify with:

```bash
PYTHONPATH=python/src python -m iap.agents.cli verify-board --trust-ref origin/main
```

The known-null protocol, the REL-08 continuation rule and the B8 v2 prereg are signed, and each
binds its runner scripts through an `analysis_identity` block.

## 5. Known limitations

- **Level 2 fill validation is not reachable.** The fill model passes Level 1 (deterministic replay
  validation) only. Level 2 would compare it with real orders and real fills on held-out days; no
  real fills exist (decision D4(a)), so nothing here is empirical validation of the live fill model
  ([docs/SIM_TO_REAL.md](SIM_TO_REAL.md)).
- **Three liquid names.** The real data is Nasdaq ITCH sessions on AAPL, MSFT and QQQ, plus IEX HIST
  overlap days; nothing generalises to a broad or illiquid universe.
- **Small G.** Real-data inference rests on few sessions (seven in-sample multi7 sessions plus two
  holdout sessions; one session for the REL-08 screen; three evaluation sessions for B8 v2). The known-null protocol reports size, not
  power, at G of 5 or fewer, and it failed its calibration (F3 open).
- **No latency edge.** Every latency figure is an in-process measurement on a shared CI runner or a
  small container; benchmarks are not colocation numbers.
- **Ledger coverage.** At the frozen baseline 24 of the 5,732 recorded looks were real-data looks; after the REL-02
  migration the repo ledger is one family with 6,826 looks over 640 configurations, 256 of them real-data configurations
  ([REL02_LEDGER_MIGRATION.md](../research/v118/REL02_LEDGER_MIGRATION.md)). The real in-sample looks on multi7 and the
  holdouts stay in per-dataset ledgers under `data/real`, outside git ([BASELINE_v1_18.md](../research/v118/BASELINE_v1_18.md)).
