# REL-04: IEX capture timestamp as `receive_ts` (code ready, NOT run)

**What was missing.** `process-iex-day` used the IEX-TP *send* time for both `exchange_ts` and
`receive_ts`, although the pcap/pcapng packet header carries the capture host's timestamp
(microsecond or nanosecond, by the file). Re-ingesting the A4 days unchanged would reproduce the
same bytes.

**What is real.** The packet stamp exists in the IEX HIST captures (`iex.pcap_packets` already
decoded it; `IexReader.receive_ts` kept it). Its limits are in `iex.CAPTURE_TS_PROVENANCE`: the
capture clock's source (NIC vs kernel) and offset to IEX's send clock are unknown, so only
differences inside one capture (jitter, spikes, ordering) are interpretable; the absolute
feed latency and the skew against a Nasdaq stamp are not identified. A pcapng *simple packet
block* carries no stamp (then `receive_ts = exchange_ts`, counted in `capture_ts_absent`).

**What changed.** `process-iex-day --capture-ts` (and `ingest(..., capture_ts=True)`):

- `receive_ts` = capture stamp, raised to `exchange_ts` where the capture clock reads earlier
  (counted: `capture_ts_before_exchange` in the session `raw` block); `normalize` would clamp
  anyway.
- `dataset.json`: `source.receive_ts = "iex_capture"`, `messages.iex.feed_latency`
  (n, negative, mean, min, max, log2 histogram) and `capture_ts_provenance`. `dataset_version`
  changes (the normalized bytes differ).
- Overwrite safety: `--capture-ts` refuses `data/real/iex`, and refuses (either way) a directory
  whose finished dataset has the other mode (`ingest` refuses it too).
- Holdout registry: no change needed. Identity is content hash **or** (session, symbol) scope; the
  new dataset has an unknown hash but registered sessions, so a 2026-05-15 / 05-18 look stays
  `spent`/`unread` by scope (tested). Do not register `iex_capts` as in-sample without the
  registry's normal sealed edit.

**Re-download.** The six approved overlap days, TOPS + DEEP+ each (DEEP is not needed):
2025-11-28 4.37 + 4.31 GB; 2025-12-08 9.30 + 8.99; 2025-12-09 8.61 + 8.43; 2025-12-10 12.29 + 11.45;
2026-05-15 12.15 + 11.91; 2026-05-18 14.87 + 14.21 = 61.6 GB TOPS + 59.3 GB DEEP+ (121 GB total,
one file on disk at a time, deleted after use).

**Disk / RAM / time (from the A4 runs, same code path).** Peak working set is about the largest day
(about 15 GB gz) + dataset + partition temp + features; the disk guard needs the estimate + 30 GB
free (124 GB free at 2026-10-20). Kept output about 0.3-1.0 GB per day (about 4.5 GB for six).
RAM: partitioning is `auto` (from available RAM); the A4 days ran with the parse stage under
load; per-step peak RSS is recorded in each `process_report.json`. Wall clock per day was 2.3-5 h
(TOPS extract 0.7-2.3 h, ingest 0.3-2.8 h, plus downloads): about 17-25 h for the six, sequential.

**Run (owner/queue).** `run_rel04_capture_ts.cmd` here, detached, after the other heavy jobs.
Afterwards compare each day against `data/real/iex`: events must be identical except
`receive_ts` (same event count, `exchange_ts`, prices, sizes).
