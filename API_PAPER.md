# API_PAPER — `iap.paper`: the forward paper trade as infrastructure (v1.16; v1.18 additions in §5)

One real session a day through the MVP loop, a live-vs-backtest TCA
comparison, drift and alpha-decay monitors and a dry kill-switch drill.
Infrastructure, not a money claim: the daily P&L is reported, never promoted
(the v1.16 in-sample study found EQ10 / EQ05 never clear taker costs). Runbook:
[docs/runbooks/RUNBOOK_paper_trade.md](docs/runbooks/RUNBOOK_paper_trade.md);
the morning ops agent reads its output ([API_AGENTS.md §2.2](API_AGENTS.md)).
Names below are checked by `tests/harness/check_docs.py`.

| what | where |
|---|---|
| Package | `python/src/iap/paper/{daily,tca_compare,monitors,drill}.py`; v1.18 (§5): `{oms,reconcile,feed,venue_sim,recorder,sim_to_real,fill_tape,supervisor}.py` |
| CLI | `python -m iap.paper.daily (--date YYYY-MM-DD \| --pending) [--dataset DIR \| --itch FILE] [--config configs/paper/paper_trade.json] [--data-root data/real] [--out-root DATA_ROOT/paper] [--symbols A,B] [--limit-events N] [--force]` |
| Config | `configs/paper/paper_trade.json` (symbols, prereg realism, `forbidden_datasets` = the holdout, TCA / drift / IC thresholds, the drill) |
| Output | `OUT_ROOT/<date>/{mvp/<SYMBOL>/, tca_compare.json, monitors.json, drill.json, report.json, report.md}` |
| Tests | `python/tests/test_paper_daily.py` (the tiny synthetic dataset of `test_mvp_real`) |

## 1. `iap.paper.daily` — the driver

```python
from iap.paper.daily import run_day, pending_dates, locate_dataset, ingest, is_forbidden, render_md, main, DEFAULT_CONFIG, REPORT, EXIT_ALERTS

code, report = run_day(date, *, config_path=DEFAULT_CONFIG, dataset=None, itch=None, data_root=None,
                       out_root=None, symbols=None, limit_events=None, force=False, log=print)
```

- `run_day` locates or ingests the dataset (`locate_dataset(data_root, date,
  forbidden)`, `ingest(itch, date, symbols, data_root)`; a dataset matching
  `forbidden_datasets` is refused by `is_forbidden(name, patterns)`), runs the
  rule per symbol through `iap.mvp.real.run_symbol` with the prereg realism and
  the opt-in EOD flatten, compares TCA, runs the monitors and the drill, and
  writes `report.json` last with `status: complete` (a rerun of a complete date
  is a no-op unless `force`). Returns `(0 | EXIT_ALERTS, report)`;
  `EXIT_ALERTS = 10` when any alert fired.
- `report.json` keys: `date`, `dataset`, `symbols`, `runs` (per symbol:
  `n_events`, `parents`, `fills`, `final_position`, `pnl_equity`, `fees`,
  `eod_flatten`, `trace_digest`), `tca` (the merged report: `aggregate` per
  component `simulated_bps` / `expected_bps` / `gap_bps`, `threshold`,
  `alerts`), `monitors` (`drift` summary, `ic` per alpha), `drill`, `alerts`.
- `pending_dates(data_root, out_root)`: the sessions of `DATA_ROOT/paper_*`
  datasets with no complete report (`--pending`). `render_md(report)` is
  `report.md`.

## 2. `iap.paper.tca_compare` — live vs backtest cost

```python
from iap.paper.tca_compare import ParentCostRow, rows_from_engine, expected_bps, compare, merge, COMPONENTS
```

- `rows_from_engine(engine) -> list[ParentCostRow]` (one per parent order,
  from the simulator's fills); `expected_bps(row, model, asset_class, adv,
  lot_size)` is the research cost model's expectation for that parent.
- `compare(rows, model, *, asset_class, adv, lot_size, is_gap_alert_bps,
  model_for_row=None)`: per-parent and filled-qty-weighted `simulated_bps`,
  `expected_bps`, `gap_bps` for `COMPONENTS = ("is", "spread", "fee",
  "impact")`, `threshold`, `alerts` (an |IS gap| above the threshold).
  `merge(reports, is_gap_alert_bps)` aggregates the per-symbol reports.

## 3. `iap.paper.monitors` — drift and alpha decay

```python
from iap.paper.monitors import feature_drift, drift_alerts, realised_ic, ic_check
```

- `feature_drift(reference_dirs, target_dir, thresholds=None)`: the day's
  feature store against the in-sample reference stores
  (`iap.research.drift.compute_drift`: PSI, KS, mean shift, std ratio) with
  `alerts` from `drift_alerts(rep, top=5)` (one compact line per group).
- `realised_ic(features_dir, instruments_json, alpha, horizon, methods_name,
  single_venue)` → `{"ic", "n"}` with the research definitions;
  `ic_check(alpha, realised, n, holdout_ic, history, *, min_ratio,
  rolling_window, min_rolling_days)` → the day and rolling ratios to the
  registered holdout IC and their `alerts` (sign flip, day ratio below
  `min_ratio`, rolling mean below it after `min_rolling_days`). Pure functions:
  alerts never change a pre-registration, a gate or a result.

## 4. `iap.paper.drill` — the dry kill-switch drill

`iap.paper.drill.kill_drill(prep, max_events_after_kill) -> dict`: replays a
prepared symbol (`iap.mvp.real.prepare_symbol`) into a null trace sink, latches
the kill switch mid-session and reports `status` (`pass` / `fail`),
`working_children_at_kill`, `kill_cancels`, `children_submitted_after_kill`,
`post_kill_fills_alerted`. Nothing is sent anywhere.

## 5. v1.18 paper-path modules (REL-05, REL-16..18; Python only, no broker)

Paper only (decision D4(a)): nothing here talks to a broker or a venue, and none of it validates the live fill model
(Level 1 only; [docs/SIM_TO_REAL.md](docs/SIM_TO_REAL.md)). Names are taken from the modules' definitions.

```python
from iap.paper.oms import OrderManager, Order, OrdState, Journal, SubmitResult, OmsError, DuplicateOrderError, OrderStateError, JournalCorrupt
from iap.paper.reconcile import reconcile, ReconKind, ReconReport, ReconException, DropCopyFill, ReconSink, publish
from iap.paper.feed import FeedGuard, GuardedRecorder, GapPolicy, ClockGuard, FeedStats, HealthSink, FeedError, FeedGapError, ClockDriftError
from iap.paper.venue_sim import PaperVenue
from iap.paper.recorder import PaperRecorder, Recording, ClockMapping, load_recording, reports_from_recording
from iap.paper.sim_to_real import compare
from iap.paper.fill_tape import day_samples, reliability, tape_test
from iap.paper.supervisor import PaperSupervisor, MonitoringRefused
```

- `OrderManager` (REL-18): idempotent submission by caller-chosen `cl_ord_id` (the same id with the same parameters
  returns the existing order, `SubmitResult.duplicate`; different parameters raise), a write-ahead `Journal`, and the
  event methods `submit`, `request_cancel`, `on_ack`, `on_reject`, `on_fill`, `on_cancel_ack`, `on_cancel_reject`,
  `on_expire`, `handle(event_tuple)`, `recover(venue)`, `end_session()` and `snapshot()`. An optional `pre_trade` hook
  is called before a new order is journaled.
- `reconcile(...)` compares an independent drop-copy fill stream (`PaperVenue` publishes one) with the order manager and
  returns typed exceptions (`MISSING_FILL`, `UNCONFIRMED_FILL`, `DUPLICATE_FILL`, `QTY_MISMATCH`, ...; see `ReconKind`).
  `FeedGuard` drops duplicates, reorders within a window and applies a declared `GapPolicy` on a gap.
- `PaperRecorder` writes the IAP1 market file, `orders.jsonl` and `recording.json` on one monotonic clock mapped to
  wall once; `compare` replays the same parents on the recorded events and compares per child; `tape_test` fits the fill
  hazard on days A and scores it on separate days B (tape-implied label, not a real fill).
- `PaperSupervisor` wires REL-16 monitoring (`iap.monitoring`), REL-17 limits (`iap.risk.portfolio_limits`) and the REL-18
  order path: a limit breach or a latched kill switch raises `LimitRefused`, and while monitoring does not permit new
  risk an order that increases an absolute position raises `MonitoringRefused`.

Related v1.18 Python APIs, documented in their own documents: `iap.monitoring`
([docs/MONITORING_POLICY.md](docs/MONITORING_POLICY.md)), `iap.portfolio.factor_risk`
([docs/FACTOR_RISK.md](docs/FACTOR_RISK.md)), `iap.risk.portfolio_limits` and `iap.portfolio.enforced`
([docs/PORTFOLIO_LIMITS.md](docs/PORTFOLIO_LIMITS.md)).
