# REL-05 real-tape fill test: DRAFT pre-declaration (unsigned, not run)

Date: 2026-10-20. Status: **proposed by a code session; unsigned; no data has been read.** The machine
copy is `predeclaration.json` (same directory); where the two differ, the JSON governs. Both are
fingerprinted by `research/preregs/rel05_prereg.json`, which does not exist yet: `run_rel05.py bind`
writes it unsigned, and only the coordinator signs it, after the owner decisions below are settled.

## What is tested

`iap.paper.fill_tape.tape_test` (REL-05, `docs/SIM_TO_REAL.md`): the fill hazard
(`FillHazardModel`) is fitted on survival samples of the FIT days and predicts `P(fill within 1 s)` for
a hypothetical 100-share resting order at every decision of the EVAL days. The outcome is what the tape
shows the order would have done under the simulator's own queue rules (`maker_labels`). One model per
symbol (AAPL, MSFT, QQQ), no calibration (ExecConfig defaults; ITCH has no receive stamp).

Decision grid: every 5 s from 09:45 to 15:45 ET (4,320 decisions per day-symbol). The first and last
15 minutes are left out to avoid the auctions and their book rebuild; the 5 s stride keeps the label
pass cheap and reduces (it does not remove) serial dependence.

## Proposed fit / score split (the open item)

| role | days (all `in_sample` in `research/holdout_registry.json`) |
|---|---|
| fit | 2019-01-30, 2019-03-27, 2019-07-30, 2019-08-30 |
| score | 2019-10-30, 2019-12-30, 2020-01-30 |

Reasons:
1. **Registered in-sample only.** These are the seven `multi7` ds3 sessions already on disk. No spent,
   unread or reserved session (every 2025-26 day) is read, so the holdout is not touched and no prereg
   id needs to be listed in the registry. `guard_or_exit` runs on all seven directories; the runner
   also refuses any split containing a non-`in_sample` session.
2. **Chronological.** Every score day is after every fit day, so the fit never sees the future of a
   scored day (regime drift counts against the model, as it would live). A random split would let
   same-quarter days sit on both sides.
3. **Four vs three.** The hazard has per-time-bucket parameters; four days give it 17,280 decisions per
   symbol at the declared grid (before survival expansion), and three score days are the minimum that lets "positive on every day"
   mean something. Eight-plus days are not available in-sample without ingesting more (out of scope).
4. **Spread of conditions.** Scoring covers Q4 2019 and the Jan 2020 pre-COVID day, so a stale fit shows.

Limits: only seven in-sample ITCH days exist on disk; they were already used by the in-sample maker
study, so this is not a fresh-data test (they are in-sample, not spent holdout). The 2021 in-sample
sessions (`r7_20210713`, `r7_20210813`) are full-market ITCH and are not in `data/real` as ds3 sets;
adding them is a data job and an owner decision, not part of this draft.

## Statistics, K, stop rules

- Metrics: Brier of the model, Brier of the naive baseline (fit-set pooled fill rate), skill
  `1 - Brier / Brier_naive`, reliability bins, per-day skill, queue-tercile and venue cells.
- **K = 3** (one verdict per symbol). The headline needs all three to pass, so no multiplicity
  correction is applied. No p-value and no interval: three score days make a day-clustered interval
  meaningless, and within-day samples are serially dependent. Verdicts are descriptive.
- Per symbol: `supports` = pooled skill >= 0.02 AND skill > 0 on each score day AND the largest
  |observed - predicted| over reliability bins with n >= 100 is <= 0.10; `contradicts` = pooled skill
  < 0; otherwise `inconclusive`; `not_estimable` if a statistic is undefined.
- Refuse (exit 2): unsigned prereg, identity mismatch, signer as runner, signing key in the
  environment, bad split, guard refusal, `dataset_version` mismatch, free RAM under 6 GB, existing
  `tape_*.json` (one run only).
- A parameter change (stride, ttl, split) is a new declaration, never a retry.

## What the result can and cannot claim

Can: whether, on seven in-sample 2019-2020 days, a hazard fitted on four predicts the simulator-implied
resting-order fill better than a constant, out of sample in time, per symbol.

Cannot: **Level 2** (no real order or fill exists, decision D4a; the label is the simulator's own queue
rule), agreement of the queue rules with real fills, any 2025-26 or holdout result, any profit or alpha,
statistical significance. A "supports" is a check that the hazard matches its own label generator on
later days; a "contradicts" is a real defect finding in the hazard.

## Owner decisions

1. Approve the split (4 fit / 3 score, chronological) or name another from the in-sample days.
2. Approve the label setup: TTL 1 s, 100 shares, 5 s grid 09:45-15:45, no calibration.
3. Approve thresholds `skill_min` 0.02 and `reliability_gap_max` 0.10 (chosen without seeing data;
   they are arbitrary and should be signed before any result exists).
4. Whether to widen the test with the 2021 in-sample sessions (needs a ds3-style ingest of two
   full-market days first; would change the split and re-bind).
5. Whether 1 s TTL only is enough, or add 5 s and 10 s horizons as extra declared cells (K would grow
   to 9).
6. Bind, review, sign (coordinator), then launch `run_rel05.cmd` detached on an otherwise idle machine
   (about 21 day-symbol label passes; chain B and other heavy jobs should not run alongside).
