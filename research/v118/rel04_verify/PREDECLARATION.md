# REL-04 capture-ts verification: DRAFT pre-declaration (unsigned, not run)

Date: 2026-10-20. Status: **proposed by a code session; unsigned; no spent-session data has been
read.** The machine copy is `predeclaration.json` (same directory); where the two differ, the JSON
governs. Both are fingerprinted by `research/preregs/rel04_capts_verify_prereg.json`, which does not
exist yet: `run_rel04_verify.py bind` writes it unsigned and only the coordinator signs it.

## Why

The REL-04 re-ingest (`research/v118/rel04/`) wrote six IEX overlap days with the capture host's
packet stamp as `receive_ts` into `data/real/iex_capts`. Its last step, `features`, is refused by the
holdout guard because all six sessions are SPENT (a read needs a SIGNED prereg id listed in
`spent_by`). The ingest outputs stay. This declaration is the authorisation and the analysis in one:

- **A. Identity.** For each day, `iex_capts/<day>/normalized/events.parquet` is compared with
  `iex/<day>/normalized/events.parquet` (the `receive_ts == exchange_ts` ingest of the same capture).
  Everything but `receive_ts` must be identical, row for row.
- **C. Feed-latency summary.** Per day, the distribution already recorded in `dataset.json`
  (`messages.iex.feed_latency`: n, negative, mean, min, max, log2 histogram) plus the
  `capture_ts_before_exchange` / `capture_ts_absent` counters. No event is read for this step.
- **B. Features.** Only if A passes on all six days: the `features` step of `process-iex-day` on each
  capts dataset (`python -m iap.features`, rust engine, 1 worker, defaults otherwise), so the
  downstream execution studies can use them.

## Pass / fail (identity)

A day **passes** only if all hold: same schema; same row count; zero mismatching rows in every column
except `receive_ts` (a null equals a null; `event_id` and file order included); zero capts rows with
`receive_ts < exchange_ts`; at least one capts row whose `receive_ts` differs from the reference
(otherwise the capture stamp had no effect); `dataset.json` `source.receive_ts == "iex_capture"`.
The identity check **passes** only if all six days pass. No tolerance, no p-value: it is a
deterministic equality test and every mismatch is reported with up to five example rows per column.
On a fail: features are not built, the findings are reported as found, and the capts datasets are not
used downstream until the cause is understood (a fix is a new declaration).

## What the result can and cannot claim

Can: that the capture-timestamp ingest changed `receive_ts` and nothing else in `events.parquet` on
six days (or exactly which columns and rows differ); the recorded within-capture latency summary per
day as a descriptive count; that features for the six capts datasets exist, built by the declared
command.

Cannot: the absolute IEX feed latency, or the skew against a Nasdaq stamp. The capture clock's
source (NIC or kernel, NTP or PTP) and its offset to IEX's send clock are unknown
(`iex.CAPTURE_TS_PROVENANCE`), so `receive - send` contains an unidentified constant: only differences
within one capture (jitter, spikes, ordering) are interpretable. Negative latencies are evidence of
that offset or of reordering, not proof of either. No signal, edge, fill, profit, holdout or
significance claim. No statistic with a sampling distribution is computed (K = 0).

## Stop rules

Refuse (exit 2): unsigned prereg, identity mismatch, signer as runner, signing key in the
environment; a capts `dataset_version` not pinned or different on disk; any of the twelve datasets
missing, not registered as `spent`, or refused by `guard_or_exit` for this prereg id; results of this
run already present (one run only). Abort: RAM under 8 GB or disk under 20 GB free at start;
`iap.features` exits non-zero on a day (log kept, no retry). A parameter change is a new
declaration, never a retry. Exit 0 means the analysis completed; the verdict is in `DECISION.json`.

## Signing flow (coordinator)

1. REL-04 re-ingest finished (six `dataset.json` present).
2. `run_rel04_verify.py pin` (reads only the six `dataset.json`), commit `predeclaration.json`.
3. `run_rel04_verify.py registry-patch --out <file>`: sealed registry with this prereg id added to
   `spent_by` of the six sessions and the six capts datasets registered as `spent` (never
   `in_sample`). Review the diff, replace `research/holdout_registry.json` by the normal sealed edit
   (`holdout_registry.seal`, as PR #304 did for REL-09 / REL-10), merge.
4. `run_rel04_verify.py bind` from a clean checkout, review, sign, commit the signed prereg.
5. Launch `run_rel04_verify.cmd` detached on an otherwise idle machine, as a non-signing actor.
6. The `keep` / `delete` steps of `process-iex-day` are not part of this run; `normalized/` stays.

## Owner decisions

1. Approve the pass rule, notably that the identity check includes `event_id` and file order (the
   normaliser's sort key does not use `receive_ts`, so both are expected to be equal).
2. Whether a failed day should block features for all six days (as drafted) or only for that day.
3. Features engine/workers: drafted as the `process-iex-day` defaults (rust, 1 worker). Features of
   the two ingests are not compared byte for byte (receive_ts-dependent features may legitimately
   differ); say if a features parity check is wanted as a second declaration.
4. Whether the `keep` / `delete` steps should be run afterwards (and by whom) to finish the
   `process-iex-day` chain for the six capts days.
5. Confirm the resource floors (8 GB RAM, 20 GB disk) and that registering the capts datasets as
   `spent` (not `in_sample`) is intended.
