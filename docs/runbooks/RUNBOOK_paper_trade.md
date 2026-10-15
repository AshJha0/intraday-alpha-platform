# Runbook: forward paper trade (daily, real sessions)

`python -m iap.paper.daily` (v1.16 plan item 9) runs the **current rule** on each
new real Nasdaq session through the MVP loop and writes a dated report.

> **Infrastructure, not a money claim.** The v1.16 in-sample study (#138) found
> EQ10 / EQ05 never clear taker costs (0 trades in 480 cells). The daily P&L is
> reported, never promoted. The point is the plumbing: realism, TCA against the
> research cost model, drift / alpha-decay monitors and a kill-switch drill
> every day, so a future rule that does clear costs lands on a tested path.
>
> Not to be confused with RUNBOOK_shadow_deployment.md (the Java shadow stack on the
> golden replay vector).

## What one run does

For session `D` (config `configs/paper/paper_trade.json`):

1. **Locate / ingest.** `--dataset DIR`, else the `data\real` dataset whose
   `dataset.json` lists `D` (`data\real\paper_<yyyymmdd>` wins), else `--itch FILE`
   is ingested into `data\real\paper_<yyyymmdd>` (`python -m iap.marketdata ingest`)
   and its feature store built (`python -m iap.features`). Datasets matching
   `forbidden_datasets` (`r7_holdout`, `r7_2025*`, `r7_20260612`, `holdout2026`)
   are refused: the R7 holdout is never paper-traded.
2. **MVP run** per symbol (`iap.mvp.real.run_symbol`) with `configs/prereg/v3.json`
   realism (venue latency, fee tier `mid`, ITCH exec prints, hidden prints,
   regulatory fees, feed latency) and the opt-in end-of-day flatten
   `execution.eod_flatten_lead_ns` (config: 300 s before the close).
3. **Live-vs-backtest TCA** (`iap.paper.tca_compare`): per parent, the MVP's
   simulated implementation shortfall / spread / fee / impact (bps, `iap.tca`)
   against the research `CostModel` (`configs/execution/execution.json` + dated
   regulatory fees) for the same filled quantity at the decision mid and half
   spread; per algo and aggregated (filled-qty weighted). The MVP impact is the
   linear rule 6, the research model square-root: an impact gap is expected.
4. **Monitors** (`iap.paper.monitors`), when the dataset has `features/`:
   feature drift (PSI / KS / mean shift / std ratio, `iap.research.drift`) against
   the in-sample stores in `drift.reference_features`; realised EQ10 / EQ05 IC at
   1s (research definitions of `tools/r7_confirm.py`) against the holdout ICs
   0.0838 / 0.0409, for the day and as a rolling mean over earlier reports.
5. **Kill-switch drill** (`iap.paper.drill`), dry: a separate engine replays the
   day until a child rests at a venue, engages the strategy kill, and checks every
   working child is cancelled and nothing is submitted after it (v1.15 #115).

Output `data\real\paper\<D>\`: `mvp\<SYMBOL>\` (MVP artefacts + `run_manifest.json`),
`tca_compare.json`, `monitors.json`, `drill.json`, `report.md`, and `report.json`
(written last, `status: complete`).

**Idempotent.** A completed date is a no-op; `--force` deletes and rebuilds that
one date directory. **Exit codes:** `0` done / nothing to do, `10` done with
alerts (read `report.md` → Alerts), `2` error (no dataset, forbidden dataset,
ingest failure).

Thresholds (`configs/paper/paper_trade.json`): `tca.is_gap_alert_bps` (2.0),
`drift.thresholds` (the `DriftThresholds` defaults), `ic.min_ratio` (0.25 of the
holdout IC), `ic.rolling_window` (20) / `ic.min_rolling_days` (5),
`drill.max_events_after_kill` (5000). Alerts are descriptive: they never change a
pre-registration, a gate or a result.

## Disk

A normalized real day is 10-25 GB plus features. Check free space before an
ingest (`--itch`); remove `data\real\paper_<yyyymmdd>\raw` after a good run if
space is short. The report directories are small.

## Run by hand

```
cd C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform
set PYTHONUTF8=1
set PYTHONPATH=python\src
python -m iap.paper.daily --date 2026-10-15 --itch data\real\itch\10152026.NASDAQ_ITCH50.gz
python -m iap.paper.daily --pending
python -m iap.paper.daily --date 2026-10-15 --force
```

`--limit-events N` truncates each symbol's stream (smoke runs only).

## New sessions: from the Nasdaq watch to a report

1. The Nasdaq watch (RUNBOOK_r7_nasdaq_watch.md) reports a usable new day; it never
   downloads. Download it as usual (owner approval, `tools/r7_download.py`).
2. Run `python -m iap.paper.daily --date D --itch <file>` once by hand (ingest +
   features + report), or ingest into `data\real\paper_<yyyymmdd>` yourself.
3. The scheduled task (below) then picks up any `paper_*` session still without a
   complete report (`--pending`); it does not ingest.

## Daily schedule (not registered; the owner registers it)

`tools\run_paper_daily.cmd` runs `--pending` and appends to
`data\real\paper\paper_daily.log` (`START` / `EXIT <rc>` markers).

```
schtasks /Create /TN "IAP paper trade daily" /SC DAILY /ST 10:00 /TR "C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\tools\run_paper_daily.cmd" /F
```

This works in PowerShell and in cmd: the path has no spaces, so it needs no inner quotes (and the cmd-style escape for inner quotes does not work in PowerShell). 10:00 is after the 09:00 Nasdaq watch. Remove with
`schtasks /Delete /TN "IAP paper trade daily" /F`; run once now with
`schtasks /Run /TN "IAP paper trade daily"`.

## Job watchdog entry

Add to `data\real\jobs.json` (RUNBOOK_job_watchdog.md; paths relative to that file):

```json
{
  "name": "paper_daily",
  "wrapper": "..\\..\\tools\\run_paper_daily.cmd",
  "log": "paper\\paper_daily.log",
  "process_pattern": "iap\\.paper\\.dail[y]",
  "done_regex": "^EXIT (0|10)\\s*$",
  "restart_safe": true,
  "max_restarts": 1,
  "stall_minutes": 240
}
```

`restart_safe` is true: a rerun skips completed dates and rebuilds an incomplete
one from scratch.

## When it alerts

- **TCA gap:** compare `tca_compare.json` → `per_algo`; a persistent impact gap is
  the linear-vs-sqrt model difference, a spread / fee gap points at the fee tier or
  exec-print fills.
- **Feature drift:** `monitors.json` → `drift.groups`; descriptive, check the
  ingest QC (`dataset.json` → `qc_totals`) first.
- **IC below 0.25 x holdout / sign flip:** one day is noise; the rolling alert
  (≥ 5 days) is the alpha-decay signal. Report it; do not retune.
- **Drill fail:** a working child survived the kill or a child was submitted after
  it. Treat as a kill-switch regression: RUNBOOK_incident_kill_switch.md.
