# B8 follow-up: why the router lost, and the `sor_sweep` re-run (2 overlap days)

**Diagnosis: confirmed, with a refinement.** The IEX fills' −23 to −28 bps
markouts come from IEX children filling far beyond the touch they saw. A
single child rarely walks several levels: the median IEX child is 19 shares
(AAPL) against a 100-share touch. The cause is a mix of three things:

1. **Our own consumption overlay.** The simulator debits the displayed size
   our fills take. The debit stays until the level's displayed size changes.
   IEX's book updates about five times less often than Nasdaq's, so its near
   levels stay "used". When the decision was made, our own earlier fills had
   already taken the IEX touch in 57 % (AAPL) and 82 % (MSFT) of pinned-IEX
   children.
2. **Unlimited MARKET children on a sparse book.** A MARKET child skips the
   used levels and fills at the next one with size left, anywhere within 10
   displayed levels. IEX's DEEP+ book has stray resting orders far from the
   touch. Worst AAPL case: a 23-share sell filled at $175.00 against a $277.11
   mid. A real IEX order would not trade through better protected quotes
   (Reg NMS order protection), so the pinned-IEX replay overstates what IEX
   would cost.
3. **The router picks IEX on fee at an equal touch.** Of `sor`'s marketable
   IEX children, 89 % (AAPL) and 77 % (MSFT) had an IEX touch equal to
   Nasdaq's: IEX won on its lower taker fee. The rest had a better IEX touch.
   The router ranks the DISPLAYED touch. That touch was already used up by
   our own fills for 38 % (AAPL) and 53 % (MSFT) of those children.

**Re-run result: mixed, and two days is a small sample.** On the same parents,
`sor_sweep` removes the router's loss where children are marketable (POV, and
AAPL's IS). Pooled across both symbols it is still +3.8 ± 2.1 bps vs pinned
Nasdaq (p 0.32), against `sor` at +5.1 ± 1.3. MSFT's IS cells now fill more
than pinned Nasdaq and pay +38 to +90 bps for it. TWAP and VWAP children post
passively and are routed the same way under `sor` and `sor_sweep`, so the
whole fill-rate gap (−7 to −8 pp) stays. Every SE has 2 day clusters
(Student-t, 1 df).

## Provenance

- Code: branch `research/v117-b8-sweep` (on top of `research/v117-b8-real-run`,
  PR #214). The re-run used commit `bb6d749`. Datasets and their hashes are
  the same as in `REPORT_overlap2.md`, and so is the calibration
  (`calibration/`, in-sample).
- Diagnosis: `diagnose_depth_walk.py` re-runs the B8 benchmark (same flags,
  seed and parents) with a read-only recorder on every child. Output:
  `diagnosis/depth_walk_{AAPL,MSFT}.json`. The AAPL file also keeps the
  12 worst children per group (`--dump 12`). Runner: `run_b8_diag.cmd`.
- Re-run: `run_b8_sweep.cmd`, detached, one symbol at a time (about 7 min
  each). Log: `data/real/b8_sweep_run.log`. Outputs:
  `benchmark_sweep_{AAPL,MSFT}.json` and `BENCHMARK_sweep_*.md`.
- `pin_nasdaq`, `pin_iex` and `sor` reproduce the first run's numbers
  exactly. The replay is deterministic, and these routes did not change.
- **QQQ: not run.** One AAPL two-venue replay holds about 2.6 GB. QQQ has
  about 18 M Nasdaq events a day, about five times AAPL. During the run the
  machine had 2-4 GB free, next to the IEX ingest (2025-12-09) and the breadth
  feature build.

## The depth-walk diagnosis (marketable children only)

"Used touch" means our own earlier fills had already taken the touch level at
decision time. "Skipped" means the child filled beyond at least one level that
was displayed when it was sent. "Cost vs touch" is the qty-weighted fill price
minus the decision touch, in bps of the consolidated mid.

| symbol | policy / venue | children | mean qty | touch p50 | qty > touch | used touch | skipped | cost vs touch | of which skipped fills |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| AAPL | pin_iex / IEXG | 13,574 | 19 | 100 | 7 % | 57 % | 16 % | 27.6 | 20.5 (17,772 sh at 122 bps) |
| AAPL | sor / IEXG | 5,742 | 26 | 100 | 4 % | 38 % | 22 % | 22.1 | 9.4 (11,449 sh at 42 bps) |
| AAPL | sor / XNAS | 7,728 | 10 | 114 | 3 % | 62 % | 29 % | 0.3 | 0.3 |
| AAPL | pin_nasdaq / XNAS | 13,467 | 16 | 140 | 3 % | 76 % | 27 % | 0.5 | 0.5 |
| MSFT | pin_iex / IEXG | 12,136 | 147 | 23 | 52 % | 82 % | 24 % | 17.6 | 16.6 (3,793 sh at 177 bps) |
| MSFT | sor / IEXG | 3,132 | 14 | 38 | 33 % | 53 % | 30 % | 43.8 | 14.4 (2,563 sh at 52 bps) |
| MSFT | sor / XNAS | 7,940 | 10 | 50 | 4 % | 62 % | 28 % | 0.3 | 0.1 |
| MSFT | pin_nasdaq / XNAS | 10,951 | 11 | 55 | 3 % | 69 % | 27 % | 0.3 | 0.1 |

- Nasdaq's touch is "used" just as often, but the level refreshes quickly and
  the next level is a tick away: a skip there costs about 1 bp. On IEX the
  next level with size left is often dollars away.
- MSFT pinned-IEX children are bigger (147 shares) than its 23-share touch,
  and most get no fill at all (0.21 fill prices per child): the remainder of a
  MARKET order that runs out of displayed size is cancelled.
- The share of `sor` IEX children that saw an equal displayed touch (so won
  on fee) was 89 % for AAPL and 77 % for MSFT. The rest saw a better IEX touch.
- The skipped fills explain most of the pinned-IEX cost. Under `sor` they
  explain 43 % (AAPL) and 33 % (MSFT). The rest is bigger children walking
  IEX's sparse levels, as in the 1,000-share AAPL buys in the dump.

## The fix: `sor_sweep` (opt-in)

`sor_sweep` uses the same calibrated router as `sor`. A marketable unpinned
child goes through `CostAwareRouter.plan_sweep`, which ranks levels by all-in
price (price plus taker fee) across both books, over the walk depth (10
levels). Each level counts only its displayed size **net of our own
consumption overlay**. The child becomes one IOC LIMIT per venue: its qty is
capped at what the plan takes on that venue, and its limit is the deepest
planned level. Qty beyond both books' net displayed depth is not sent
(`two_venue.sweep_unsent_qty`: AAPL 0, MSFT 14,712 shares). Passive children
(TWAP / VWAP joins) are routed as under `sor`.

## Four policies (paired with `pin_nasdaq`; same parents and windows)

| pair | symbol | n | all-in bps | shortfall bps | fill rate diff |
|---|---|---:|---|---|---|
| sor − pin_nasdaq | AAPL | 18 | +5.22 ± 0.11 | +5.18 ± 0.12 | −0.087 ± 0.002 |
| sor − pin_nasdaq | MSFT | 18 | +5.02 ± 2.62 | +4.96 ± 2.63 | −0.073 ± 0.004 |
| sor − pin_nasdaq | pooled | 36 | **+5.12 ± 1.25** (95 % CI −10.8, 21.0; p 0.15) | +5.07 ± 1.26 | −0.080 ± 0.003 |
| sor_sweep − pin_nasdaq | AAPL | 18 | −0.77 ± 0.34 | −0.81 ± 0.35 | −0.084 ± 0.006 |
| sor_sweep − pin_nasdaq | MSFT | 18 | +8.45 ± 4.57 | +8.41 ± 4.58 | −0.057 ± 0.006 |
| sor_sweep − pin_nasdaq | pooled | 36 | **+3.84 ± 2.11** (95 % CI −23.0, 30.7; p 0.32) | +3.80 ± 2.11 | −0.070 ± 0.000 |
| pin_iex − pin_nasdaq | AAPL | 18 | +11.70 ± 4.63 | +11.65 ± 4.64 | −0.069 ± 0.007 |
| pin_iex − pin_nasdaq | MSFT | 18 | +11.40 ± 3.39 | +11.26 ± 3.38 | −0.222 ± 0.040 |
| pin_iex − pin_nasdaq | pooled | 36 | **+11.55 ± 0.62** (95 % CI 3.7, 19.4; p 0.03) | +11.46 ± 0.63 | −0.146 ± 0.016 |

Positive = more cost than pinned Nasdaq. ± is the day-clustered SE (2
clusters, Student-t with 1 df). `pin_nasdaq` is the fourth policy: it is the
baseline of every row.

**1 s markouts of our fills by venue (bps):**

| policy | AAPL | MSFT |
|---|---|---|
| pin_nasdaq | XNAS −1.13 | XNAS −1.27 |
| pin_iex | IEXG −27.85 | IEXG −23.06 |
| sor | XNAS −1.28, IEXG −13.67 | XNAS −1.18, IEXG −15.60 |
| sor_sweep | XNAS −1.42, IEXG −0.63 | XNAS −1.21, IEXG −7.33 |

**Venue split (filled qty):** `sor` AAPL 79 % XNAS / 21 % IEXG and MSFT
84 / 16. `sor_sweep` AAPL 80 / 20 and MSFT 85 / 15.

**Where the differences come from** (per-parent pairs in the JSON,
`two_venue.paired_rows`):

- **POV** (all children marketable): `sor` cost up to +10 bps per cell; `sor_sweep` is between −0.1 and 0.0 in every cell.
- **IS 0.1 % ADV:**
  - AAPL: `sor` +34 / +59 bps, `sor_sweep` −3.8 / −0.7.
  - MSFT: `sor_sweep` +90 / +38 bps, with a fill rate 10 pp ABOVE pinned
    Nasdaq. The sweep completes qty that pinned Nasdaq leaves unfilled and
    pays for it. This is likely from far IEX levels inside the 10-level plan
    (the all-in rank takes them once Nasdaq's net depth is used up). **Not
    verified per child:** the diagnostic recorder covers `sor`, not the sweep.
- **TWAP / VWAP:** identical under `sor` and `sor_sweep` (passive joins),
  including MSFT VWAP 0.1 % at +21.6 bps with −43 pp fill. The passive leg's
  fill-rate loss comes from posts routed to IEX: the X-4 hazard is about five
  times lower there.

## What this means

- The single-venue router's marketable loss is an artefact of ranking the
  DISPLAYED touch of a thin, slowly-updating book that our own fills had
  already used, compounded by MARKET children with no price limit. The sweep
  fixes the first part. It does not stop a plan from reaching stray far
  levels.
- No policy here beats pinned Nasdaq with any confidence. Two days and 2
  clusters cannot resolve a few bps either way.
- Not done here (each is a follow-up):
  - a price collar on the sweep (e.g. no level beyond the NBBO plus k bps);
  - an order-protection rule in the replay (no MARKET trade-through on the
    pinned venue);
  - net-of-overlay touches in `route_aggressive` / `route_passive`;
  - leave-one-day-out calibration;
  - QQQ, and the remaining overlap days.
