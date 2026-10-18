# REL-08c closing-auction imbalance: pre-declaration

Status: DECLARED, not run. Parameters set 2026-10-18 by Claude under the owner's
delegation to the coordinator; the prereg `research/preregs/rel08c_prereg.json` that
binds this file and `predeclaration.json` (the machine copy, which wins on any
difference) is unsigned until the coordinator signs it. No NOII message of the real
file has been read for this study.

Programme: `IAP_Release_Programme_2026-10-17.md` REL-08c and section 9; hypothesis 3 of
`IAP_Alpha_Review_2026-10-17.md` (corroboration: Bogousslavsky and Muravyev 2023).

## What one day can support

**One day is a screen only.** Inference clusters by session (one session = one
cluster). With one session the between-session variance is unknown, the MDE is
infinite and the registered verdict is `underpowered` whatever the per-trade numbers
show. The output is a screen: the sign and size of the per-trade measures, reported as
such, never as evidence of an edge. A `continue` or `reject` verdict needs at least
five sessions, which needs further approved full-market files and a new declaration.

## Data

- File: `data/vendor/01302019.NASDAQ_ITCH50.gz`, sha256
  `8c97b5b13bc451c012c2466fb7e258da134dab29aa47b67fe7b0088c78e870be` (verified
  2026-10-18; bound as the prereg's data version; the extractor re-hashes and
  refuses a mismatch before decoding a message).
- Session 2019-01-30 is `in_sample` in `research/holdout_registry.json` (all symbols);
  the runner refuses any other state. It is not a holdout and is not spent by this run.
- Universe: `research/breadth/universe.json` (100 names; bound by fingerprint). Caveat:
  ranked on the same day's displayed volume (not NOII or returns), so selection does
  not use the outcome, but it is not a pre-period selection.
- Extraction: `iap.auction.rel08c_extract`, one pass, output to
  `data/real/rel08c_2019-01-30/` (not committed; `meta.json` carries the provenance).

## Registered parameters

| parameter | value | reason |
|---|---|---|
| window | 15:30:00-15:55:00 ET continuous session | REL-08c scope; ends at the 15:55 MOC/LOC entry cutoff |
| baseline reference | 15:30:00 mid | start of the window; the baseline uses no imbalance |
| decision times | 15:51:00, 15:53:00 | the closing NOII starts at 15:50; 15:51 has six 10 s snapshots, 15:53 two minutes more, both well before the exit |
| threshold on \|level\| | 0.20 | a material imbalance (20 % of paired + imbalance shares); below it the conditioned policy is flat |
| min_persistence | 2 snapshots | the same direction for at least 10 s, filters one-print flips |
| orientation | +1 (follow) | the hypothesis is price pressure toward the imbalance into the close; fading would be a new declaration |
| size | 100 shares | one round lot, inside the touch for nearly all 100 names, so fills do not drive the result |
| exit | taker at the touch 15:54:55 (5 s before the 15:55 cutoff) | the registered execution; an MOC exit in the cross is reported, not decided on |
| cost model | half-spread from the recorded touch on both legs + $0.0030/share taker fee; cross exit $0.0010/share; no impact at 100 shares | indicative 2019 Nasdaq list prices; spread taken from the book, not a constant |
| target_bps | 2.0 bp incremental net per trade | the alpha review's bar for a taker with one trip per name per day |
| alpha, power | 0.05 two-sided, 0.80 | convention; Student-t with S - 1 df over sessions |
| MDE | (t(0.975, S-1) + t(0.80, S-1)) * sd_session / sqrt(S); infinite for S < 2 | session-clustered; `underpowered` when MDE > 2.0 bp |
| min_sessions | 5 | the alpha review's "5+ days"; fewer is `underpowered` |
| min_trades | 30 imbalance trades | a floor below which the session means are noise |
| ledger family, K | "REL-08c close imbalance (real)", K = 1 | one primary statistic (pooled over both decision times) |

## Registered design

- Features at t: the last closing NOII snapshot with time <= t; none before 15:50.
- Policies: `baseline` = direction of the 15:30 -> t mid move; `imbalance` = sign of
  level when `|level| >= 0.20` and `persistence >= 2`.
- Primary statistic: per-session mean of the incremental net taker bps (imbalance
  minus baseline).
- Reported separately, never in the verdict: prediction (gross mid move t -> exit and
  t -> cross, hit rate); fill probability (touch size vs 100 shares at entry and exit,
  limit-on-close fill at the exit mid); the MOC cross exit net of the entry leg and fee.
- Verdicts: `underpowered` (S < 5, or < 30 trades, or MDE > 2.0 bp); `continue`
  (mean >= 2.0 bp and CI low > 0); `reject` otherwise. Negatives first. No parameter is
  changed after a result; a change is a new, separately labelled declaration.

## Run

`research/auction/rel08c/run_rel08c.cmd` (detached, `IAP_RUN_ACTOR=rel08c-runner`)
calls `run_rel08c.py run`, which refuses on an unsigned prereg, an identity mismatch,
the signer as runner, a signing key in the environment, a registry state other than
`in_sample`, a running ingest, or a sha256 mismatch; then it extracts and runs the
study, writing `DECISION.json`, `REPORT.md` and `run_manifest.json` here. Expected
about 10 min, under 2 GB.

## Not claimed

No live or paper trading claim; latency zero; displayed liquidity only; one day.
