# B8: two-venue SOR on real books (Nasdaq + IEX) — run book

Status (2026-10-16): **code and synthetic tests only; no real result yet.** No
Nasdaq / IEX overlap day has been ingested with its normalized events kept, so
nothing under this directory is a measurement. The reports land here when the
commands below run.

## Prerequisite: both venues' NORMALIZED events

The replay needs `normalized/eq_YYYYMMDD.normalized.iap1` of BOTH datasets for
each day: the Nasdaq one (`data/real/r7_YYYYMMDD`, kept) and the IEX one
(`data/real/iex/iex_YYYYMMDD`). The A4 process-and-delete chain
(`process-iex-day`) deletes the IEX normalized events after the features step,
unless it runs with `--keep-normalized` (decided 2026-10-16: the A4 chain keeps
`normalized/*.iap1` and Parquet for every IEX overlap day; vendor files, raw and
JSONL are still deleted). B8 reads `data/real/iex/iex_YYYYMMDD/normalized/*.iap1`
beside `data/real/r7_YYYYMMDD/normalized/*.iap1` (the `.iap1` codec is preferred
where both exist).
`python -m iap.research.two_venue` refuses a dataset without them and says why.

## The commands (run once >= 2 overlap days are ingested)

From the repository root, with `PYTHONPATH=python/src`; one `--nasdaq-dir` /
`--iex-dir` pair per day for the calibration, one `--pair-dir` per extra day
for the benchmark (shown for 2025-12-08 and 2025-12-09):

```
python -m iap.research.two_venue calibrate \
  --nasdaq-dir data/real/r7_20251208 --iex-dir data/real/iex/iex_20251208 \
  --nasdaq-dir data/real/r7_20251209 --iex-dir data/real/iex/iex_20251209 \
  --venues research/execution/b8/venues_xnas_iexg.json \
  --out research/execution/b8/calibration

python -m iap.research.execution_benchmark \
  --data-dir data/real/r7_20251208 --second-data-dir data/real/iex/iex_20251208 \
  --pair-dir data/real/r7_20251209 data/real/iex/iex_20251209 \
  --venues research/execution/b8/venues_xnas_iexg.json \
  --venue-calibration research/execution/b8/calibration \
  --policies pin_nasdaq,pin_iex,sor --baseline-policy pin_nasdaq \
  --out research/execution/b8 --tag overlap
```

Outputs: `calibration/venue_calibration_{XNAS,IEXG}.json` (schema
`iap.venue_calibration/v1`) and `benchmark_overlap.json` /
`BENCHMARK_overlap.md` (the B1 report plus the "Two venues: router vs pinned"
section: venue split and per-venue markouts by horizon, day-clustered; the
shortfall / fill-rate cells and the paired `sor - pin_nasdaq` and
`pin_iex - pin_nasdaq` differences are the B1 tables).

Passive variants: add `passive@pin_nasdaq,passive@pin_iex,passive@sor` with
`--baseline-policy passive@pin_nasdaq` in a second run (the pairing is always
against one baseline). The B10 dark arm composes: `sor+dark` with a venue
table that also names the dark venue.

## Resources

A real Nasdaq overlap day is about 23 M events (QQQ about 18 M of them) and the
IEX day adds its own; the replay holds one merged session in memory. On the
16 GB machine run one symbol at a time (`--symbols AAPL`, then MSFT, then QQQ,
each with its own `--tag`), detached (memory note "Long jobs: run detached"):
the two-venue mode drops other symbols before the merge. The calibration
command takes `--symbols` too.

## Things to decide before reading the result as evidence

1. **In-sample calibration.** The command calibrates the router on the same
   days it evaluates. Use leave-one-day-out (calibrate on the other days) or
   calibrate on days kept out of the evaluation before quoting a router gain.
2. **Fees.** `venues_xnas_iexg.json` carries indicative list prices (XNAS
   0.003 / -0.002, IEXG 0.0009 / 0); a tiered schedule changes the router.
3. **Latency.** Both feeds carry exchange timestamps only, so venue latency is
   zero and the alignment is the clock rule alone (no skew estimate).
