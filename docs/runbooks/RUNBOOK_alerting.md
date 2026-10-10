# RUNBOOK — alert delivery, latency SLOs and trading-state backups (v1.12.0)

Owner: platform on-call. Scope: `deployment/alertmanager/`,
`deployment/prometheus/slo.yml`, `deployment/k8s/cronjob-state-backup.yaml`.
Contracts: PLATFORM_CONVENTIONS.md §12.7 ("Alerting, SLOs and state backup").

## 1. How alerts are routed

| `severity` label | receiver | meaning | repeat |
|---|---|---|---|
| `page` | `page` (secret `page_url`) | capital at risk or trading correctness; wake someone up | 1h |
| `critical`, `warning` | `ticket` (secret `ticket_url`) | act during the session / investigate the same day | 12h |
| `none` (`Watchdog`) | `watchdog` (no-op) | always-firing heartbeat | 1m |
| anything else | `ticket` | catch-all: an unlabelled alert is never dropped | 4h |

Grouping: `alertname` + `service`. Inhibition (one page per incident, always
scoped to the same `service`):

- `KillSwitchEngaged` mutes `FillRateDrop`, `SignalRateCollapse`,
  `PreTradeRejectRatioHigh`, `LossLimitUtilizationHigh` and every latency SLO
  alert: they are consequences of the halt.
- `StaleFeed`, `FeedWallClockStall`, `BookStale`, `SequenceGapDetected` mute
  `SignalRateCollapse` and `FillRateDrop`.
- an SLO `page` mutes the `warning` of the same `slo`.

Every alert carries `severity`, `runbook` and `runbook_url`;
`tests/harness/check_deployment.py` (`alert_rule_metadata`) fails the build
otherwise.

## 2. Setting the receiver URLs (operator)

Receiver URLs embed tokens: they are **never committed**.
`alertmanager.yml` only names files (`url_file`), and
`check_deployment.py` (`alerting_secret_free`) fails on an inline URL, a
token-looking string, a non-local `.url` file or a committed `Secret` with
data under `deployment/`.

**Kubernetes** — before the first `kubectl apply` (the Alertmanager pod stays
in `ContainerCreating` until the Secret exists):

```bash
umask 077
printf '%s' 'https://pager.example/hook/<token>'  > /secure/page_url
printf '%s' 'https://tickets.example/hook/<token>' > /secure/ticket_url
kubectl -n intraday-alpha create secret generic iap-alertmanager-webhook \
  --from-file=page_url=/secure/page_url --from-file=ticket_url=/secure/ticket_url
shred -u /secure/page_url /secure/ticket_url
```

Rotation: re-create the Secret with `--dry-run=client -o yaml | kubectl apply -f -`.
Alertmanager re-reads `url_file` on every send, so no restart is needed once
the kubelet has refreshed the mounted Secret (up to about a minute).

Migrating from v1.11 (single key `url`): add the two new keys, e.g.
`kubectl -n intraday-alpha get secret iap-alertmanager-webhook -o jsonpath='{.data.url}' | base64 -d > /secure/page_url`
and create `ticket_url` as above. The pod will not start until both keys exist.

Network: `allow-alertmanager-webhook-egress` (networkpolicy.yaml) allows only
TCP 443 to non-private addresses. An internal receiver needs that ipBlock
changed to its CIDR.

**docker compose** — with nothing set, both URLs point at the in-compose
`alert-sink` echo service (`deployment/alertmanager/local-sink.url`, which
holds no secret), so `docker compose up` works out of the box:

```bash
docker logs -f iap-alert-sink     # Watchdog arrives within ~1 minute
```

To deliver for real, keep the files **outside the repo**:

```bash
export ALERT_PAGE_URL_FILE=$HOME/.iap/page_url
export ALERT_TICKET_URL_FILE=$HOME/.iap/ticket_url
docker compose -f deployment/docker/docker-compose.yml up -d alertmanager
```

`ALERT_WEBHOOK_URL_FILE` from v1.11 is still honoured as the page URL.
`*.url` files under `deployment/alertmanager/` (other than `local-sink.url`)
are git-ignored as a backstop.

**Verify delivery**: `Watchdog` is always firing. If the receiver sees no
Watchdog within 2 minutes, the pipeline is broken: check
`kubectl -n intraday-alpha logs deploy/alertmanager` for `notify` errors
(DNS, TLS, 4xx from the receiver, NetworkPolicy drops).

<a id="latency-slo"></a>
## 3. Latency SLO alerts

| SLO | objective (30 days) | source histogram |
|---|---|---|
| `order-path-latency` | 99% of orders decision-to-wire < 1,048,575 ns (about 1.05 ms) | `order_path_latency_ns` |
| `event-path-latency` | 99% of events through `onEvent` < 1,048,575 ns | `book_update_latency_ns` |

The threshold is a log2 bucket bound (`le="1048575"`): the Java histograms
only have 2^i - 1 ns bounds, so the SLO is exact at the bound, with no
interpolation. Error ratios are recorded per window as
`job:{order,event}_path_latency_slo_errors:ratio_rate{5m,30m,1h,2h,6h,1d,3d}`.

| alert | severity | condition (budget 1%) | budget spent |
|---|---|---|---|
| `*LatencySLOFastBurn` | page | 14.4x over 1h and 5m, **or** 6x over 6h and 30m | 2% in 1h / 5% in 6h |
| `*LatencySLOSlowBurn` | warning | 3x over 1d and 2h, **or** 1x over 3d and 6h | 10% in 1d / 10% in 3d |

When it fires:

1. Is it real load or one bad stretch? Compare `job:order_path_latency_ns:p99`
   and `job:book_update_latency_ns:p99` (Grafana "Market Data & Latency") with
   the cold reference in `benchmarks/RESULTS.md`.
2. GC: `job:jvm_gc_pause_ns:p99`, and `GcPauseHigh`, which usually fires with it.
3. Host contention: CPU throttling on the pod (`limits.cpu`), a noisy
   neighbour, or a node that is too small.
4. A burn with **no traffic** cannot fire (the ratio is NaN); a finished
   paper session is not an SLO breach.
5. Do **not** raise the threshold to silence it: the objective is a contract.
   Change it with a reviewed PR to `slo.yml` and its promtool tests.

Limits, stated plainly: the SLO measures in-process latency as the JVM sees
it, not venue round-trip. A JVM restart resets the cumulative histograms;
`rate()` handles the reset, but samples in flight at the crash are lost.

## 4. Trading-state HA, backups and restore

What this deployment provides is **restart-and-resume, plus backups**. It is
not active-active and it has no hot standby:

- `java-platform` is a singleton (replicas 1, `Recreate`, `ReadWriteOnce`
  claim `iap-java-state`). The RWO attach plus `Recreate` is the fence. No
  Lease-based leader election exists (it would be a Java change). A second
  trading loop would duplicate orders once a venue adapter is wired.
- On pod or node loss Kubernetes reschedules the single pod, which re-attaches
  the claim with its checkpoint intact (§12.3). Continuing the interrupted
  session needs `--resume` in the container arguments (Dockerfile.java;
  RUNBOOK_paper_trading.md §5). `/ready` keeps the pod out of the Service until
  it is trading. Expect a gap of minutes on node loss (volume detach timeout),
  not seconds.
- There is no PodDisruptionBudget, on purpose: with one replica it would block
  every node drain.
- The `state-backup` CronJob archives `/data/state` every 30 minutes to the
  `iap-java-state-backup` claim and keeps 96 archives (2 days). It mounts the
  state claim **read-only** and is pinned by a required pod affinity to the
  node running `java-platform` (RWO is per node). When the trading pod is not
  running, the job stays Pending until `activeDeadlineSeconds` reaps it, and
  no backup is taken. `check_deployment.py` (`k8s_state_backup`) pins this
  shape.

**Check backups**

```bash
kubectl -n intraday-alpha get jobs -l app.kubernetes.io/name=state-backup
kubectl -n intraday-alpha logs job/<latest state-backup job>   # "backup ok: state-<ts>.tgz"
```

**Restore** (the state volume is lost or corrupt):

1. Engage the kill switch if the platform is up, then scale to zero:
   `kubectl -n intraday-alpha scale deploy/java-platform --replicas=0`.
2. Recreate `iap-java-state` if it is gone (`kubectl apply -f deployment/k8s/pvc.yaml`).
3. Run a one-off pod that mounts `iap-java-state-backup` read-only and
   `iap-java-state` read-write, and extract the newest archive:
   `tar -xzf /backup/state-<ts>.tgz -C /data` (this restores `/data/state`).
4. Scale back to 1, with `--resume` in the arguments (RUNBOOK_paper_trading.md §5).
5. If resume **refuses** (audit or trace line count, or the risk snapshot's
   sha256, does not match the checkpoint), the archive was taken while a
   checkpoint was being written. The refusal is the
   fail-closed guard working, not a bug. Try the previous archive; archives
   taken while the session was quiescent always match. Never edit the audit
   to make the counts agree. If no archive resumes, start a fresh session with
   the kill switch re-engaged and file an incident
   (RUNBOOK_incident_kill_switch.md).

Off-cluster copies (object storage) are the operator's responsibility and are
not in this repo. In-cluster backups do not survive loss of the cluster's
storage.
