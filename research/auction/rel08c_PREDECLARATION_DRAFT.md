# REL-08c closing-auction imbalance: pre-declaration (DRAFT, NOT SIGNED)

Status: DRAFT

Written 2026-10-18 before any REL-08c number exists. No NOII message of the real
2019-01-30 file has been read for this study: reading it is a ledger look, so the
extraction (`tools/run_rel08c_extract.cmd`) runs only after this file is copied to
`rel08c_PREDECLARATION.md`, the open parameters below are set, and the owner signs it
(line `Status: SIGNED`; `iap.auction.rel08c_study.run_study` refuses real extracts
otherwise). Programme: `IAP_Release_Programme_2026-10-17.md` REL-08c and section 9;
hypothesis 3 of `IAP_Alpha_Review_2026-10-17.md` (corroboration: Bogousslavsky and
Muravyev 2023).

## What one day can support

**One day is a screen only.** Inference clusters by session (one session = one
cluster). With one session the between-session variance is unknown, the MDE is
infinite and the registered verdict is `underpowered` whatever the per-trade numbers
show. The one-day output is a screen: the sign and size of the per-trade measures,
reported as such, never as evidence of an edge. A `continue` or `reject` verdict needs
at least `min_sessions` sessions, which needs further approved full-market files.

## Data

- File: `data/vendor/01302019.NASDAQ_ITCH50.gz`, sha256 `8c97b5b1...` (full value
  recorded by the coordinator, verified 2026-10-18; the extractor re-hashes and
  refuses a mismatch with `--expected-sha256`).
- Universe: `research/breadth/universe.json` (100 names, rule in `UNIVERSE_RULE.md`).
  Caveat stated in advance: the universe was ranked on the same day's displayed
  volume (not on NOII or returns), so selection does not use the outcome, but the
  names are not a pre-period selection.
- Extraction: `iap.auction.rel08c_extract` (one pass; closing and opening NOII with
  level, change, persistence, distance to close, reference / far / near; cross
  prints; 1 s BBO grid 15:30:00-16:00:00 ET from the reconstructed book; provenance
  in `meta.json`). Output to `data/real/rel08c_2019-01-30/` (not committed).

## Registered design

- Features at decision time t: the last closing NOII snapshot with time <= t.
  None before 15:50 (Nasdaq publishes the closing NOII from 15:50), so a decision time
  before 15:50 leaves the conditioned policy flat.
- Policies: `baseline` = direction of the 15:30 -> t continuous mid move (close
  behaviour without the imbalance); `imbalance` = `orientation * sign(level)` when
  `|level| >= threshold` and `persistence >= min_persistence`.
- Primary statistic: per-session mean of the incremental net taker bps (imbalance
  minus baseline), taker entry at the touch at t, taker exit at the touch
  `exit_lead_s` before the 15:55 MOC cutoff, each leg paying the half-spread and the
  fee. Student-t interval with S - 1 df over sessions.
- Reported separately, not in the verdict: prediction (gross mid move t -> exit and
  t -> cross price, hit rate); fill probability (touch size vs qty at entry and exit;
  limit-on-close fill at the exit mid); the auction-exit alternative (MOC entered
  before the cutoff, net of the entry leg and the cross fee).
- Verdicts: `underpowered` (S < min_sessions, or imbalance trades < min_trades, or
  MDE > target_bps); `continue` (mean >= target_bps and CI low > 0); `reject`
  otherwise. Negatives are reported first. No parameter is changed after a result;
  a change is a new, separately labelled run.

## Open parameters (the coordinator sets these; the values below are code defaults)

| parameter | code default | to set |
|---|---|---|
| decision times t | 15:51:00, 15:53:00 ET | [ ] |
| threshold on `|level|` | 0.2 | [ ] |
| min_persistence (snapshots) | 2 | [ ] |
| orientation (+1 follow, -1 fade) | +1 | [ ] |
| qty (shares) | 100 | [ ] |
| exit_lead_s before 15:55:00 | 5 s | [ ] |
| taker fee per share | $0.0030 | [ ] |
| cross fee per share | $0.0010 | [ ] |
| target_bps (continuation bar) | 2.0 bp per trade | [ ] |
| alpha, power | 0.05, 0.80 | [ ] |
| min_sessions | 5 | [ ] |
| min_trades | 30 | [ ] |
| ledger family name and K (looks) | none | [ ] |
| holdout status of 2019-01-30 | not in the holdout registry | [ ] confirm |

## Not claimed

No live or paper trading claim; latency zero; displayed liquidity only for fills; the
taker exit before 15:55 is the registered execution, the cross exit is descriptive.

## Signature

Signed by: ______  Date: ______  (copy to `rel08c_PREDECLARATION.md` and set the status
line to `Status: SIGNED`)
