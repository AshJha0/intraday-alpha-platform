# REL-04c: single-venue execution universe (v1.18)

Status: **prepared, not run.** No byte of the three ITCH files has been read
for this item. The universe rule below was committed before the scan that
applies it; the scan is the job's `select` stage.

Code: `python/src/iap/research/rel04c_universe.py` (rule),
`python/src/iap/research/rel04c_job.py` (stages), `run_rel04c.cmd` (launcher).
Tests: `python/tests/test_v118_rel04c.py` (synthetic fixtures only).

## 1. Universe rule `rel04c_universe/v1`

Selection day 2019-10-18 (`S101819-v50.txt.gz`, the earliest of the three
full-market days in `data/real/itch/r7`), one streaming pass:

1. Per symbol: displayed regular-hours volume (ITCH `E` + printable `C`
   between system events `Q` and `M`, the breadth rule's measure), the
   closing-cross price (`Q`, cross type `C`), else the last `P` / printable
   `C` print price.
2. Eligible: common stock (issue classification `C`), Nasdaq-listed (market
   category Q, G or S), not a test security, not an ETP, price >= $5.00,
   displayed regular-hours volume > 0, **and** a Stock Directory row on
   2021-07-13 and 2021-08-13 (read from each file's prefix up to the first
   `Q` system event, so no trading message of a later day is examined).
3. Liquidity rank by displayed regular-hours volume among the eligible.
   Buckets: **L1** ranks 1-100, **L2** 101-500, **L3** 501-1500.
4. Price buckets: **P1** [$5, $20), **P2** [$20, $100), **P3** >= $100.
5. **AAPL and MSFT are anchors** (refused if either is not eligible). In each
   of the nine (L, P) cells, anchors excluded, sorted by rank, take the names
   at 0-based positions `floor(n/3)` and `floor(2n/3)`. At most 2 + 18 = 20
   names; a thin cell's shortfall is recorded in `universe.json` and never
   back-filled.

### Limitations

- One selection day and one price; buckets reflect 2019-10-18 only.
- Liquidity is **Nasdaq displayed** volume, not consolidated: a name's bucket
  mixes its liquidity with Nasdaq's market share in it.
- The presence requirement on the 2021 days conditions on survival and an
  unchanged ticker to 2021-08-13 (a look-ahead favouring survivors).
  Tolerable for an execution-cost benchmark; it would not be for an alpha
  claim. It is needed because a multi-session dataset has one universe and
  the ingest refuses a symbol with no directory row on a session date.
- Cut points (100 / 500 / 1500 ranks, $20 / $100) are round numbers fixed by
  hand, not tuned; with three price buckets per liquidity bucket, L1 P1 or
  L3 P3 may be thin.
- The selection day is also a benchmark day. All three days are `in_sample`
  in the holdout registry; the result is SECONDARY.
- Not a public, dated index list: the universe is reproducible only from the
  sha256-pinned ITCH file.

## 2. The job

```
research\execution\rel04c\run_rel04c.cmd
```

Launch detached (`tools/jobs/watchdog.py launch_detached`) from a clean
checkout of merged main; `PY` and `DATA` environment variables override the
interpreter and `data\real` paths. Log: `data\real\rel04c\run_rel04c.log`;
one JSON line per stage (seconds, `peak_rss_gb`, details) in
`data\real\rel04c\rel04c_job.jsonl`.

| stage | what | refuses when |
|---|---|---|
| `verify` | size + sha256 of the three files vs the values pinned in `rel04c_job.DAYS` (from the r7 datasets' `inputs`) | a file is missing, or size / hash differ |
| `select` | rule v1 -> `research/execution/rel04c/universe.json` | `verify` not done; anchors not eligible; a `--limit-messages` pass |
| `ingest --day D` x3 | one multi-session dataset `data\real\rel04c\r7x20_3day`, date order, `--defer-normalize` for days 1-2, day 3 normalises all; `--partition-instruments` chosen from available RAM (REL-13 `auto_partition_instruments`), `--slim` | out of order; universe file missing or bad; a dataset holding any session set other than the earlier days (it is then deleted with the ingest markers: re-launch redoes the ingest) |
| `register` | `tools/data_manifest.py generate` + `holdout_registry seed`; then the holdout guard on the dataset must say `in_sample` | sessions are not exactly the three days; guard says spent / unread |
| `volume` | Nasdaq / consolidated volume ratio per (day, symbol) -> `volume_check.json`; `consolidated_adv.json` for the benchmark | `yahoo_volume.json` has no `_source`, or a value / split factor is missing |
| `bench --bucket L1/L2/L3` | REL-03 benchmark v2 on the bucket's names, `--cover-all-cells --consolidated-adv` -> `research/execution/rel04c/benchmark/` | pre-declaration not `frozen`, an open parameter, `result_class` not `secondary` |
| `cleanup` | delete the dataset's `raw/` intermediate (normalised events, `dataset.json`, outputs kept; ITCH inputs never touched) | a bucket's benchmark not done |

Two launches by design: launch 1 runs to `volume` and stops (REFUSED) until
the owner fills `yahoo_volume.json` for the 20 names `select` chose and
freezes `predeclaration.json`; launch 2 resumes at `volume`. Commit
`universe.json`, `research/data_manifest.json` and the reseeded
`research/holdout_registry.json` (its sealed body hash is pinned in
`tests/test_holdout_registry.py`: update that pin in the same commit).

## 3. Validation hook (Yahoo Finance consolidated volume)

`volume_check.json`: per (day, symbol) Nasdaq regular-session volume
(`dataset.json` `per_symbol.volume`, crosses included) divided by Yahoo's
consolidated daily volume, Yahoo values divided by the owner-recorded
cumulative split factor (Yahoo is split-adjusted: AAPL 2019-10-18 x4).
Nasdaq is a fraction of consolidated volume (all venues, TRF, extended
hours); outside 0.05-0.60 is flagged for a look, not failed. The job reads a
file the owner fills; it downloads nothing.

## 4. Pre-declaration (draft)

`predeclaration.json`: SECONDARY, per liquidity bucket, primary metric
`cost_per_filled_share_bps`, G = 3 sessions. Open: `algos`, `policies`,
`sizes_pct_venue_adv` (the benchmark sizes parents on Nasdaq ADV),
`sizes_target_pct_consolidated_adv` (the consolidated equivalent, from the
volume stage's median ratio), `window_min`, `mde_report`. Freezing it
(`status: frozen`, `frozen_by`, `frozen_utc`) is the owner's step.

## 5. Estimates for the queue (from existing logs; unmeasured for 20 names)

| stage | wall time | peak RAM | disk |
|---|---|---|---|
| verify (hash 14.8 GB) | 3-6 min | < 0.1 GB | 0 |
| select (pure-Python pass over 4.0 GB + two prefixes) | 45-120 min (no measured pass rate; the breadth scan is the same reader) | < 1 GB (about 9,000 counters) | 0 |
| ingest 2019-10-18 | 25-40 min (3 names: parse 174 s; 100 names on 2019-01-30: parse 1,497 s) | <= 8 GB target (auto partition) | raw spool ~1-2 GB |
| ingest 2021-07-13 | 35-60 min (3 names: parse 824 s, 6.0 GB file) | <= 8 GB | raw ~2-3 GB |
| ingest 2021-08-13 + normalise all three | 50-90 min (100 names: normalise 2,923 s for 7.2 GB) | <= 8 GB (partitioned) | normalised 4-8 GB kept |
| register | < 5 min (hashes the new dataset) | < 1 GB | 0 |
| bench L1 / L2 / L3 | 2-4 h / 1-2 h / 0.5-1 h (multi7, 3 names x 7 days, 44.7 M events: about 2 h without `--cover-all-cells`) | 3-6 GB, unmeasured (sessions loaded whole) | < 50 MB outputs |
| cleanup | < 1 min | 0 | frees the raw spool |

Total: launch 1 about 2.5-5 h; launch 2 about 4-7.5 h. Peak disk below
20 GB (145 GB free on C: on 2026-10-18). The peak RSS line of every stage
replaces these figures after the first run.
