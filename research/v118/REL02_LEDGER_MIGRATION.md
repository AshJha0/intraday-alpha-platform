# REL-02 ledger migration (2026-10-18)

Owner-approved 2026-10-18 from the `tools/ledger_family_report.py` dry run (PR #245).
Applied by `tools/migrate_ledger_rel02.py` (idempotent; pinned by
`python/tests/test_ledger_family.py::test_committed_ledger_is_migrated_rel02`).

## Chain

| | entries | looks (`total_experiments`) | `chain_head` |
|---|---:|---:|---|
| before | 422 | 5,732 | `1f95affdee760e380149f400b4db86f0b3e87ae0a17181890ede3301116e489d` |
| after | 640 | 6,826 | `5dc957df3bf2674307ebe8da14a3cd037a54c3fc013b8b049a353817a752e669` |

The `data_family` tag is not part of the chained identity (`_CHAINED`), so tagging
moved no key and no link. The merged and retroactive entries are APPENDED: the old
chain is a prefix of the new one (`chain_heads(entries)[421]` is the old head), so
`tests/harness/check_ledger_monotone.py` passes unchanged. The head changed once.

## Family accounting

| family | entries (distinct configurations) | looks |
|---|---:|---:|
| real | 256 | 1,132 |
| synthetic | 384 | 5,694 |
| unknown | 0 | 0 |

Real-data N (distinct configurations on real data): **24 before, 256 after**.

- 24 multi7 entries: real by registry dataset version (unchanged).
- 14 `prereg` entries (no dataset version): tagged real. Each debits the look of a
  pre-registered test on real ITCH sessions; conservative (only raises real N).
- 384 seeded-generator entries (dataset versions `203c8f54...`, `116b7787...`),
  including the 25 `experiment_runner` entries whose dataset version is inside their
  config (5 on `203c8f54`, 20 on `116b7787`): synthetic, per the document-level
  `datasets` summary. They stay recorded and are excluded from the real-data N.
- 12 entries merged from the git-ignored per-dataset real ledgers (REL-02c, as
  recorded entries): `holdout2026` 6 (EQ01/02/04/05/07/10 runner, 84 looks each),
  `multi7` 4 (COMB_EQ combination, 95 looks each), `r7_holdout` 2 (EQ05/EQ10, 2 each).
- 206 retroactive execution-study looks (below).

## Deviation: retroactive B7 and B8 looks

The B7 multi7 benchmark (`research/execution/benchmark/benchmark_multi7_signal.json`)
and the B8 two-venue benchmarks (`research/execution/b8/benchmark_*.json`) ran on real
sessions and were published without ledger entries. They are recorded now, from the
committed result files, as `kind` `execution_study`, one entry per paired
policy-vs-baseline comparison (policy, algo, size): B7 44 (pooled over AAPL/MSFT/QQQ,
7 sessions); B8 162 (per symbol AAPL/MSFT: `sweep` 39 x 2 on 2025-11-28/12-08, of which
`overlap2`'s 26 x 2 are the same comparisons on the same sessions and counted once;
`oos3` 42 x 2 on 2025-12-10, 2026-05-15, 2026-05-18). Each carries a deviation note and
no `gate_looks`: the recording does not re-judge any published verdict. No look was
added for a run that did not happen. Not recorded here: the B8 fill-model calibration
runs (`research/execution/b8/calibration*/`), a follow-up.

## What did not change

The PROMOTE gate and Bonferroni threshold still use `total_experiments` (all looks);
switching the gates to the real-data N is a separate change. A recorded verdict keeps
its `gate_looks` (the threshold is not retroactive).
