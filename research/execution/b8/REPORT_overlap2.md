# B8 on real books: two overlap days (2025-11-28, 2025-12-08) — router vs pinned

**Result: negative.** On these two days the cost-aware router (`sor`) did NOT beat
working every child on Nasdaq alone. It cost about 5 bps more all-in and filled
about 8 pp less. Pinning to IEX was worse still: about 11.5 bps more, with a fill
rate 15 pp lower. **Two days is a small sample.** Every SE below has 2 clusters
(Student-t, 1 df), so the 95 % CIs are wide and only the IEX-pinned gap clears
zero. 2025-11-28 is a half day, so each symbol has only 18 parents per policy.

## Provenance

- Code: branch `research/v117-b8-real-run`. Benchmark commit `d5c33cf`, which is
  B8 (#208) plus streamed symbol-filtered reads and the pooled paired summary.
  Calibration ran at `af86019`, whose two_venue code is identical to `d5c33cf`.
- Datasets (`dataset_version`, first 16 hex):
  - Nasdaq `r7_20251128`: `535f4868b0a5f6d1`
  - IEX `iex_20251128`: `f977bb3a39d16557`
  - Nasdaq `r7_20251208`: `284629a29df4e641`
  - IEX `iex_20251208`: `6c94942ab59df29f`
  - Both IEX days passed the TOPS oracle with 0 mismatches. The full hashes are
    in each dataset's `dataset.json`.
- Run: detached, one process at a time, via `data/real/run_b8_real.cmd`.
  Log: `data/real/b8_real_run.log`. Calibration took 3 min; the benchmark took
  about 4 min (AAPL) and 2.5 min (MSFT).
- QQQ was **not run**: about 18 M Nasdaq events a day, with about 3-4 GB of RAM
  free next to the IEX ingest and the breadth build.
- Commands, with `PYTHONPATH=python/src` and `D=data/real`:

```
python -m iap.research.two_venue calibrate \
  --nasdaq-dir $D/r7_20251128 --iex-dir $D/iex/iex_20251128 \
  --nasdaq-dir $D/r7_20251208 --iex-dir $D/iex/iex_20251208 \
  --symbols AAPL,MSFT --venues research/execution/b8/venues_xnas_iexg.json \
  --out research/execution/b8/calibration
python -m iap.research.execution_benchmark \
  --data-dir $D/r7_20251128 --second-data-dir $D/iex/iex_20251128 \
  --pair-dir $D/r7_20251208 $D/iex/iex_20251208 \
  --venues research/execution/b8/venues_xnas_iexg.json \
  --venue-calibration research/execution/b8/calibration \
  --policies pin_nasdaq,pin_iex,sor --baseline-policy pin_nasdaq \
  --out research/execution/b8 --symbols AAPL --tag overlap2_AAPL   # then MSFT
```

- Grid: the B1 defaults. TWAP / VWAP / POV / IS; 0.1 / 0.5 / 1 / 2 % ADV;
  30-min windows; NATIVE child style; linear child impact adder; walk depth
  10; seed 20260829.
- Every cost is replay + adder: our fills never move the replayed books.

## Per-venue calibration (AAPL + MSFT, both days)

| venue | exec depletion rate /s | P(fill ≤ 1 s) at touch | displayed executions | maker markout 1 s | 5 s | 30 s |
|---|---:|---:|---:|---|---|---|
| XNAS | 0.284 | 0.248 | 255,907 | −0.09 ± 0.05 | −0.14 ± 0.07 | −0.13 ± 0.01 |
| IEXG | 0.056 | 0.054 | 15,051 | −0.09 ± 0.03 | −0.15 ± 0.07 | −0.17 ± 0.08 |

Markouts are in bps (± day-clustered SE, 2 clusters). Resting orders fill about
five times faster on Nasdaq. Maker toxicity is small, and alike on the two venues.

## Router vs pinned (paired, same parents and windows; `other − pin_nasdaq`)

| pair | symbol | n | all-in bps | shortfall bps | fill rate diff |
|---|---|---:|---|---|---|
| sor − pin_nasdaq | AAPL | 18 | +5.22 ± 0.11 | +5.18 ± 0.12 | −0.087 ± 0.002 |
| sor − pin_nasdaq | MSFT | 18 | +5.02 ± 2.62 | +4.96 ± 2.63 | −0.073 ± 0.004 |
| sor − pin_nasdaq | pooled | 36 | **+5.12 ± 1.25** (95 % CI −10.8, 21.0; p 0.15) | +5.07 ± 1.26 | **−0.080 ± 0.003** |
| pin_iex − pin_nasdaq | AAPL | 18 | +11.70 ± 4.63 | +11.65 ± 4.64 | −0.069 ± 0.007 |
| pin_iex − pin_nasdaq | MSFT | 18 | +11.40 ± 3.39 | +11.26 ± 3.38 | −0.222 ± 0.040 |
| pin_iex − pin_nasdaq | pooled | 36 | **+11.55 ± 0.62** (95 % CI 3.7, 19.4; p 0.03) | +11.46 ± 0.63 | **−0.146 ± 0.016** |

Positive = more cost than pinned Nasdaq. Net fees barely move (0.00 bps).
The pooled rows come from the per-parent pair rows of both files
(`two_venue.paired_rows`), clustered by day.

- **Fill rates** (filled / parent qty):
  - AAPL: pin_nasdaq 0.546, pin_iex 0.544, sor 0.514.
  - MSFT: 0.499, 0.316, 0.437.
- **Venue split of `sor`:** AAPL 79 % XNAS / 21 % IEXG; MSFT 84 % / 16 %.

## Our fills' markouts by venue (1 s, bps; negative = against us)

| policy | AAPL | MSFT |
|---|---|---|
| pin_nasdaq | XNAS −1.13 | XNAS −1.27 |
| pin_iex | IEXG −27.85 | IEXG −23.06 |
| sor | XNAS −1.28, IEXG −13.67 | XNAS −1.18, IEXG −15.60 |

- The IEX fills' price impact is about 0. The −20 to −28 bps sits in the
  effective spread: the fill prices are far from the consolidated mid, which
  points to walking IEX's thin displayed depth, not to adverse selection.
- **Likely cause of the router's loss, not yet verified.** In the replay
  `sor` routes each child to ONE venue (`route_aggressive`: touch price + taker
  fee). IEX's lower taker fee wins when the touch prices tie, and the child
  then walks IEX's thin book. The depth-aware `plan_sweep` (all-in sweep across
  both books) is not wired into the replay. The next step is a check of the IEX
  touch sizes at the routed children and a `plan_sweep` arm.

## Caveats

- Two days, two symbols, 36 paired parents per comparison; SEs rest on 2
  clusters. Treat every number here as provisional.
- The calibration is in-sample: the router was calibrated on the days it was
  evaluated on. That would flatter the router, and it still lost.
- Fees are the indicative list prices in `venues_xnas_iexg.json`. Venue latency
  is zero (exchange timestamps only).
- Files: `benchmark_overlap2_{AAPL,MSFT}.json` / `BENCHMARK_overlap2_*.md`
  (full B1 tables) and `calibration/venue_calibration_{XNAS,IEXG}.json`.
