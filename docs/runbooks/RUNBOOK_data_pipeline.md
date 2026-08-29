# RUNBOOK — Data Pipeline

**Scope:** the synthetic/raw → normalized market-data pipeline
(`python3 -m iap.marketdata`): generator → `data/raw/*.jsonl` → normalize →
`data/normalized/{*.normalized.jsonl, *.normalized.iap1, events.parquet,
qc_report.json}`.
**Owner:** market-data. **Related alerts:** `SequenceGapRate`, `StaleFeed`,
`QueueDepthHigh`, `TargetDown`.

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

### Sequence gaps (`SequenceGapRate`) {#sequence-gaps}

1. Confirm scope: one stream or all — Grafana "Market Data & Latency" →
   *Stream QC* panel; `qc_report.json` per-stream counters for batch runs.
2. Downstream effect: books mark `stale=true` on a gap and recover on
   SNAPSHOT (conventions §4); risk fail-closes on stale books — expect
   `risk_rejected_total` to rise. That is correct behavior, not a second bug.
3. Batch pipeline: a gap rate far above `configs/generator.json`
   `anomalies.gap_prob` means a generator/normalizer regression — bisect with
   the determinism tests above; identical seed must reproduce identical
   counters.
4. Do NOT hand-edit normalized files (raw is immutable, normalized is
   regenerated); fix code/config and re-run.

### Stale feed (`StaleFeed`) {#stale-feed}

1. `kubectl -n intraday-alpha get pods` / `docker compose ps` — is the
   producer alive?
2. Check the last event age vs the 5 s budget
   (`configs/risk.json` `market_data.stale_feed_timeout_ns`).
3. If the producer is healthy but consumers see nothing: shared volume/PVC
   mounted? (`kubectl describe pod`, `Events:` section for mount errors).
4. Risk is already rejecting orders against stale books; no manual trading
   halt needed unless recovery exceeds one session — then follow
   `RUNBOOK_incident_kill_switch.md`.

### Queue depth (`QueueDepthHigh`) {#queue-depth}

Consumer slower than producer. Check consumer CPU throttling
(`kubectl top pods`), GC pauses (dashboard panel), and recent deploys. A
sustained 100k backlog means the latency SLO is already gone: prefer
restarting the slow consumer over letting it drain for hours.

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
