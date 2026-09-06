# RUNBOOK — Data Pipeline

**Scope:** the synthetic/raw → normalized market-data pipeline
(`python3 -m iap.marketdata`): generator → `data/raw/*.jsonl` → normalize →
`data/normalized/{*.normalized.jsonl, *.normalized.iap1, events.parquet,
qc_report.json}`.
**Owner:** market-data. **Related alerts:** `SequenceGapDetected`, `BookStale`, `StaleFeed`,
`FeedWallClockStall`, `TargetDown`.

## 1. Normal operation

Local / on-demand run (deterministic — same seed ⇒ bit-identical files):

```bash
cd python
PYTHONPATH=src python3 -m iap.marketdata            # uses configs/generator.json (seed 20260829)
PYTHONPATH=src python3 -m iap.marketdata --seed 42  # explicit seed override
# custom locations:
PYTHONPATH=src python3 -m iap.marketdata --configs-dir ../configs --out ../data
```

Container / scheduled:

```bash
# compose (runs once as the data-generator service, writes the iap-data volume)
docker compose -f deployment/docker/docker-compose.yml up data-generator

# k8s: daily CronJob at 07:30 UTC (deployment/k8s/cronjob-data-pipeline.yaml)
kubectl -n intraday-alpha get cronjob data-pipeline
kubectl -n intraday-alpha create job --from=cronjob/data-pipeline data-pipeline-manual-$(date +%s)   # ad-hoc run
kubectl -n intraday-alpha logs job/<job-name>
```

## 2. Verify a run succeeded

```bash
ls data/raw data/normalized
python3 -m json.tool data/normalized/qc_report.json | head -40
```

Expect in `qc_report.json`: per-stream event counts, and gap/duplicate/
out-of-order/invalid counters consistent with `configs/generator.json`
`anomalies` rates (goldens run with anomalies disabled). The report's sha256
is the **dataset version** used by every experiment manifest
(`docs/governance/REPRODUCIBILITY.md`):

```bash
sha256sum data/normalized/qc_report.json
```

Cross-check determinism after any pipeline code change:

```bash
cd python && PYTHONPATH=src python3 -m pytest -q tests/test_generator.py tests/test_normalize.py
```

## 3. Failure modes

### Sequence gaps (`SequenceGapDetected`, `BookStale`) {#sequence-gaps}

0. **One gap is the incident.** `configs/risk.json`
   `market_data.max_sequence_gap_before_halt = 1`: a single gap marks the book
   stale and fail-closes trading in that instrument until a SNAPSHOT recovers
   it. `SequenceGapDetected` therefore fires on `increase(md_sequence_gaps_total[5m]) > 0`
   (not a rate), and the counter is exported on **every** event, so it is
   visible on the next scrape. If no snapshot arrives, `BookStale` follows
   after 2 m and `book_stale{instrument="..."}` stays 1 — that instrument is
   silently not trading until you act.
   ```bash
   curl -s http://java-platform:8080/metrics | grep -E '^(md_sequence_gaps_total|book_stale)'
   ```
1. Confirm scope: one stream or all — Grafana "Market Data & Latency" →
   *Stream QC* panel; `qc_report.json` per-stream counters for batch runs.
2. Downstream effect: books mark `stale=true` on a gap and recover on a
   complete SNAPSHOT burst (conventions §4; a gap *inside* a burst breaks
   it, the next complete burst recovers); risk fail-closes on stale books —
   expect `risk_rejected_total` to rise. That is correct behavior, not a
   second bug. Live paths with A/B feeds or retransmission should run the
   books with a `reorder_window` (≤ 4096) so late retransmissions heal the
   hole instead of waiting for the next snapshot (`late_recovered` counts
   them; the window is checkpointed).
3. **Sequence lifecycle** (API_CORE §4): the first sequence of a stream is
   accepted whatever its value (0 is legal); a SNAPSHOT burst that restarts
   *below* the last sequence is a venue **sequence reset** (daily restart,
   partition fail-over) — `sequence_resets` increments, the book is stale
   until that burst completes; the normaliser keeps day-2 data as a new
   epoch (`sequence_resets` in `qc_report.json`), never as duplicates. If a
   venue restarts *without* sending a snapshot, call `reset_sequence()` /
   `ReplayEngine.reset_sequences()` from the session-roll handler.
4. **Timestamps**: `receive_ts < exchange_ts` is clamped + counted
   (`ts_clamped`); an `exchange_ts` that goes backwards *within* a stream is
   dropped + counted (`ts_regression_dropped`) and shows up as a gap for the
   book — investigate the venue clock (PTP step, leap-second smear), do not
   re-sort by hand.
5. **Malformed records** never stop a replay: every class is dropped and
   counted per book (`invalid_payload_dropped`, `unknown_type_dropped`,
   `invalid_side_dropped`, `modify_price_mismatch`, `unknown_order_events`);
   a non-zero `modify_price_mismatch` means an adapter maps price changes
   to MODIFY instead of CANCEL+ADD. IAP1 v2 files are CRC-32 checked on
   load: a CRC error is corruption in transit/at rest — re-transfer, never
   partially accept.
6. Batch pipeline: a gap rate far above `configs/generator.json`
   `anomalies.gap_prob` means a generator/normalizer regression — bisect with
   the determinism tests above; identical seed must reproduce identical
   counters.
7. Do NOT hand-edit normalized files (raw is immutable, normalized is
   regenerated); fix code/config and re-run.

### Stale feed (`StaleFeed`) {#stale-feed}

0. **Which question are you answering?** Two metrics, two meanings
   (`PLATFORM_CONVENTIONS.md` §12.6):
   - `md_event_time_gap_seconds` — the gap between consecutive EVENTS. This is
     what `StaleFeed` alerts on, and it is correct whether the session is live
     or replaying an August capture.
   - `time() - md_last_event_wallclock_unixtime` — whether the PROCESS is
     still moving. That is `FeedWallClockStall`, scoped to
     `platform_mode{mode="realtime"}` so a replay never trips it.
   `md_last_event_unixtime` is the replayed event's own timestamp: never
   compare it to `time()` (the round-2 rule did, and paged on every historical
   replay the desk ran).
1. `kubectl -n intraday-alpha get pods` / `docker compose ps` — is the
   producer alive? `/ready` returns 503 on a stalled realtime feed, so k8s has
   already taken the pod out of the Service; `/health` stays 200 unless the
   loop itself is wedged for 30 s.
2. Check the last event age vs the 5 s budget
   (`configs/risk.json` `market_data.stale_feed_timeout_ns`). A silent
   venue (no gap, the line just stops) is invisible to sequence-based
   `stale`; consumers use `OrderBook.is_fresh(now_ns, max_age_ns)` and the
   consolidated view excludes only gap-stale venues (`stale_venues()`).
3. If the producer is healthy but consumers see nothing: shared volume/PVC
   mounted? (`kubectl describe pod`, `Events:` section for mount errors).
4. Risk is already rejecting orders against stale books: the risk engine's
   mark carries the market-data event time of the venues at the
   consolidated touch (`PaperTrading.RiskWiring`), so a silent line
   surfaces as `STALE_PRICE` rejects once the last quote is older than
   `stale_feed_timeout_ns`, and a gap-stale venue as `SEQUENCE_GAP` until
   its SNAPSHOT recovery (`onFeedRecovered`). The execution simulator
   produces no fill on a stale or non-TRADING venue book and the SOR never
   routes to one (conventions §11.2-11.3). No manual trading halt needed
   unless recovery exceeds one session — then follow
   `RUNBOOK_incident_kill_switch.md`.

### Event-bus queue depth — CONTRACT ONLY {#queue-depth}

**No producer exports `eventbus_queue_depth`, and there is no alert or panel
for it any more.** The round-2 `QueueDepthHigh` alert (severity `critical`)
could never fire, and the dashboard panel said "No data" forever;
`PLATFORM_CONVENTIONS.md` §12.7 forbids a rule or panel over a metric nothing
exports. The NAME and threshold remain a design contract, recorded in
`deployment/grafana/README.md` "PLACEHOLDER metrics", to be wired together
with a producer.

The procedure once one exists: consumer slower than producer — check consumer
CPU throttling (`kubectl top pods`), GC pauses (dashboard panel), and recent
deploys. A sustained 100k backlog means the latency SLO is already gone;
prefer restarting the slow consumer over letting it drain for hours.

### Pipeline job failed

```bash
kubectl -n intraday-alpha logs job/<job-name>       # ValueError/RuntimeError messages are explicit
```

Common causes: missing `pyarrow` (image build issue — the pipeline needs it
for `events.parquet`), a config edit that failed validation, PVC full
(`kubectl -n intraday-alpha get pvc iap-data`). The job is idempotent and
`concurrencyPolicy: Forbid` prevents overlap — after fixing, just re-create
the job (§1). Re-runs are safe: same seed ⇒ same bytes.

## 4. Escalation

Data integrity affects everything downstream (features, alphas, risk).
If normalized data was consumed by research runs while corrupt: identify the
affected `data_version` hashes, mark those runs invalid in
`research/models/ledger.json` (append a correcting entry — never edit
history) and re-run after the fix.
