"""Daily forward paper-trade driver (v1.16 plan item 9, infrastructure).

``python -m iap.paper.daily (--date YYYY-MM-DD | --pending) [--dataset DIR | --itch FILE]
[--config configs/paper/paper_trade.json] [--data-root data/real]
[--out-root DATA_ROOT/paper] [--symbols A,B] [--limit-events N] [--force]``

For one session:

1. **Locate or ingest.**  ``--dataset`` names an ingested dataset; else
   the dataset under ``--data-root`` whose ``dataset.json`` lists the date
   (a ``paper_<yyyymmdd>`` directory wins); else ``--itch`` ingests the
   file (``python -m iap.marketdata ingest``) into
   ``DATA_ROOT/paper_<yyyymmdd>`` and builds its feature store
   (``python -m iap.features``).  Datasets named in the config's
   ``forbidden_datasets`` (the R7 holdout) are refused.
2. **Run the rule** through the MVP loop per symbol
   (:func:`iap.mvp.real.run_symbol`) with the prereg realism
   (``configs/prereg/v3.json``: venue latency, fee tier, exec prints,
   regulatory fees, ...) and the opt-in MVP end-of-day flatten
   (``execution.eod_flatten_lead_ns``).
3. **Live-vs-backtest TCA** (:mod:`iap.paper.tca_compare`).
4. **Monitors** (:mod:`iap.paper.monitors`): feature drift against the
   in-sample stores, realised EQ10 / EQ05 IC against the holdout ICs.
5. **Kill-switch drill** (:mod:`iap.paper.drill`), dry.

Output: ``OUT_ROOT/<date>/`` with ``mvp/<SYMBOL>/`` (the MVP artefacts and
``run_manifest.json``), ``tca_compare.json``, ``monitors.json``,
``drill.json``, ``report.json`` and ``report.md``.  ``report.json`` is
written last with ``status: complete``; a rerun of a completed date is a
no-op (exit 0) unless ``--force``, which deletes and rebuilds that one
directory.  ``--pending`` (the scheduled mode) runs every session of a
``DATA_ROOT/paper_*`` dataset that has no complete report.  Exit codes: 0 done (or already done), 10 done with alerts,
2 error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from iap.mvp.config import REPO_ROOT

DEFAULT_CONFIG = REPO_ROOT / "configs" / "paper" / "paper_trade.json"
REPORT = "report.json"
EXIT_ALERTS = 10


def _load(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n")
    tmp.replace(path)


def _repo_path(rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else REPO_ROOT / p


def is_forbidden(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _sessions(ds: Path) -> list[str]:
    try:
        return list(_load(ds / "dataset.json").get("sessions") or {})
    except (OSError, ValueError):
        return []


def locate_dataset(data_root: Path, date: str, forbidden: Sequence[str]) -> Path | None:
    """The one dataset under ``data_root`` holding ``date`` (module docstring)."""
    preferred = data_root / f"paper_{date.replace('-', '')}"
    if (preferred / "dataset.json").is_file() and date in _sessions(preferred):
        return preferred
    if not data_root.is_dir():
        return None
    hits = sorted(
        d
        for d in data_root.iterdir()
        if d.is_dir() and not is_forbidden(d.name, forbidden) and date in _sessions(d)
    )
    if len(hits) > 1:
        raise ValueError(f"several datasets hold {date}: {[h.name for h in hits]}; use --dataset")
    return hits[0] if hits else None


def ingest(itch: Path, date: str, symbols: Sequence[str], data_root: Path) -> Path:
    """Ingest ``itch`` and build its features (subprocesses; logs beside it)."""
    out = data_root / f"paper_{date.replace('-', '')}"
    env_py = [sys.executable, "-m"]
    if not (out / "dataset.json").is_file():
        cmd = [*env_py, "iap.marketdata", "ingest", "--format", "itch50", "--input", str(itch)]
        cmd += ["--date", date, "--symbols", ",".join(symbols), "--out", str(out)]
        subprocess.run(cmd, check=True)
    if not (out / "features").is_dir():
        cmd = [*env_py, "iap.features", "--data-dir", str(out / "normalized")]
        cmd += ["--out-dir", str(out / "features"), "--configs", str(out / "configs")]
        cmd += ["--registry-out", str(out / "features" / "feature_registry.json")]
        subprocess.run(cmd, check=True)
    return out


def _instrument_rows(ds: Path) -> dict[str, dict[str, Any]]:
    doc = _load(ds / "configs" / "instruments" / "instruments.json")
    return {r["symbol"]: r for r in doc.get("instruments", [])}


def _ic_history(out_root: Path, date: str, alpha: str) -> list[float]:
    """Previous dated reports' realised IC of ``alpha``, oldest first."""
    vals: list[float] = []
    if not out_root.is_dir():
        return vals
    for d in sorted(p for p in out_root.iterdir() if p.is_dir() and p.name < date):
        try:
            rep = _load(d / REPORT)
        except (OSError, ValueError):
            continue
        if rep.get("status") != "complete":
            continue
        v = (((rep.get("monitors") or {}).get("ic") or {}).get(alpha) or {}).get("realised_ic")
        if isinstance(v, (int, float)):
            vals.append(float(v))
    return vals


def pending_dates(data_root: Path, out_root: Path) -> list[str]:
    """Session dates of ``DATA_ROOT/paper_*`` datasets without a complete report."""
    dates = set()
    if data_root.is_dir():
        for d in data_root.glob("paper_*"):
            dates.update(_sessions(d))
    out = []
    for date in sorted(dates):
        try:
            done = _load(out_root / date / REPORT).get("status") == "complete"
        except (OSError, ValueError):
            done = False
        if not done:
            out.append(date)
    return out


def run_day(
    date: str,
    *,
    config_path: Path = DEFAULT_CONFIG,
    dataset: Path | None = None,
    itch: Path | None = None,
    data_root: Path | None = None,
    out_root: Path | None = None,
    symbols: Sequence[str] | None = None,
    limit_events: int | None = None,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> tuple[int, dict[str, Any] | None]:
    """The driver (module docstring); returns (exit code, report or None if skipped)."""
    from iap.backtest.costs import CostModel
    from iap.execution.regulatory import load_default_schedule, prereg_wants_regulatory_fees
    from iap.mvp.real import prepare_symbol, run_symbol
    from iap.paper import drill, monitors, tca_compare
    from iap.validation.sessions import book_scope_for_dataset

    cfg = _load(config_path)
    data_root = data_root or REPO_ROOT / "data" / "real"
    out_root = out_root or data_root / "paper"
    out = out_root / date
    if (out / REPORT).is_file() and not force:
        try:
            done = _load(out / REPORT).get("status") == "complete"
        except ValueError:
            done = False
        if done:
            log(f"{date}: already complete in {out} (use --force to rebuild); nothing to do")
            return 0, None
    forbidden = cfg.get("forbidden_datasets", [])
    syms = list(symbols or cfg["symbols"])
    if dataset is None:
        dataset = locate_dataset(data_root, date, forbidden)
    if dataset is None:
        if itch is None:
            raise ValueError(f"no ingested dataset holds {date} under {data_root}; pass --itch")
        dataset = ingest(itch, date, syms, data_root)
    dataset = Path(dataset)
    if is_forbidden(dataset.name, forbidden):
        raise ValueError(f"{dataset.name} is a forbidden (holdout) dataset")
    if date not in _sessions(dataset):
        raise ValueError(f"{dataset}: no session {date}")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    prereg = _repo_path(cfg["prereg"])
    mvp_config = _repo_path(cfg["mvp_config"])
    eod = cfg.get("eod_flatten_lead_ns")
    rows_by_sym = _instrument_rows(dataset)
    run_syms = [s for s in syms if s in rows_by_sym]
    skipped = sorted(set(syms) - set(run_syms))
    if not run_syms:
        raise ValueError(f"{dataset}: none of {syms} in the dataset universe")

    base_model = CostModel.load(_repo_path(cfg["execution_config"]))
    model_for_row = None
    if prereg_wants_regulatory_fees(_load(prereg)):
        schedule = load_default_schedule(REPO_ROOT / "configs")

        def model_for_row(r):  # type: ignore[no-untyped-def]
            return base_model.with_dated_regulatory_fees(schedule, r.decision_ts)

    gap_alert = float(cfg["tca"]["is_gap_alert_bps"])
    runs: dict[str, Any] = {}
    tca: dict[str, Any] = {}
    for sym in run_syms:
        log(f"{date} {sym}: MVP run")
        run = run_symbol(
            dataset,
            date,
            sym,
            out / "mvp" / sym,
            prereg=prereg,
            mvp_config=mvp_config,
            limit_events=limit_events,
            eod_flatten_lead_ns=eod,
        )
        runs[sym] = run.manifest["results"] | {
            "n_events": run.manifest["stream"]["n_events"],
            "trace_digest": run.manifest["trace_digest"],
            "eod_flatten": dict(run.result.engine.eod_flatten),
        }
        row = rows_by_sym[sym]
        tca[sym] = tca_compare.compare(
            tca_compare.rows_from_engine(run.result.engine),
            base_model,
            asset_class=row["asset_class"],
            adv=float(row["adv"]),
            lot_size=int(row.get("lot_size", 1)),
            is_gap_alert_bps=gap_alert,
            model_for_row=model_for_row,
        )
        del run
    tca_doc = {"per_symbol": tca, "all": tca_compare.merge(tca, gap_alert)}
    _write_json(out / "tca_compare.json", tca_doc)

    # monitors
    mon: dict[str, Any] = {}
    feats = dataset / "features"
    if feats.is_dir():
        refs = [data_root / r for r in cfg["drift"]["reference_features"]]
        missing = [str(r) for r in refs if not r.is_dir()]
        if missing:
            mon["drift"] = {"status": "skipped", "reason": f"reference stores missing: {missing}"}
        else:
            log(f"{date}: feature drift")
            mon["drift"] = monitors.feature_drift(refs, feats, cfg["drift"].get("thresholds"))
        icc = cfg["ic"]
        single = book_scope_for_dataset(dataset) == "single_venue"
        mon["ic"] = {}
        for alpha, spec in sorted(icc["alphas"].items()):
            log(f"{date}: realised IC {alpha}")
            r = monitors.realised_ic(
                feats,
                dataset / "configs" / "instruments" / "instruments.json",
                alpha,
                icc["horizon"],
                icc["methods"],
                single,
            )
            mon["ic"][alpha] = monitors.ic_check(
                alpha,
                r["ic"],
                r["n"],
                float(spec["holdout_ic"]),
                _ic_history(out_root, date, alpha),
                min_ratio=float(icc["min_ratio"]),
                rolling_window=int(icc["rolling_window"]),
                min_rolling_days=int(icc["min_rolling_days"]),
            )
    else:
        mon["status"] = "skipped"
        mon["reason"] = f"no feature store at {feats} (build it with python -m iap.features)"
    _write_json(out / "monitors.json", mon)

    # kill-switch drill
    dcfg = cfg.get("drill") or {}
    dsym = dcfg.get("symbol") or run_syms[0]
    log(f"{date} {dsym}: kill-switch drill (dry)")
    stage = out / "drill_stage"
    prep = prepare_symbol(
        dataset,
        date,
        dsym,
        stage,
        prereg=prereg,
        mvp_config=mvp_config,
        limit_events=limit_events,
        eod_flatten_lead_ns=eod,
    )
    drill_doc = drill.kill_drill(prep, int(dcfg.get("max_events_after_kill", 5000)))
    shutil.rmtree(stage, ignore_errors=True)
    _write_json(out / "drill.json", drill_doc)

    alerts = list(tca_doc["all"]["alerts"])
    alerts += (
        (mon.get("drift") or {}).get("alerts", []) if isinstance(mon.get("drift"), dict) else []
    )
    for v in (mon.get("ic") or {}).values():
        alerts += v["alerts"]
    if drill_doc["status"] == "fail":
        alerts.append(f"kill-switch drill FAILED on {dsym}")
    report = {
        "x-version": 1,
        "kind": "paper-trade-daily",
        "status": "complete",
        "date": date,
        "dataset": dataset.name,
        "config": config_path.name,
        "prereg": cfg["prereg"],
        "eod_flatten_lead_ns": eod,
        "limit_events": limit_events,
        "symbols": run_syms,
        "symbols_not_in_dataset": skipped,
        "note": "Infrastructure, not a money claim: the rule's P&L is reported, never promoted.",
        "runs": runs,
        "tca": tca_doc["all"],
        "monitors": {
            "drift": (mon.get("drift") or {}).get("summary", mon.get("drift"))
            if "drift" in mon
            else mon.get("reason"),
            "ic": mon.get("ic"),
        },
        "drill": drill_doc,
        "alerts": alerts,
    }
    (out / "report.md").write_text(render_md(report), encoding="utf-8", newline="\n")
    _write_json(out / REPORT, report)
    log(f"{date}: complete -> {out} ({len(alerts)} alert(s))")
    return (EXIT_ALERTS if alerts else 0), report


def _f(x: Any, nd: int = 3) -> str:
    return "-" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def render_md(rep: dict[str, Any]) -> str:
    lines = [
        f"# Paper trade {rep['date']} ({rep['dataset']})",
        "",
        f"> {rep['note']}",
        "",
        "## Runs",
        "",
        "| symbol | events | parents | fills | final pos | pnl equity | fees | eod flatten |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s, r in rep["runs"].items():
        lines.append(
            f"| {s} | {r['n_events']} | {r['parents']} | {r['fills']} | {r['final_position']} "
            f"| {_f(r['pnl_equity'])} | {_f(r['fees'])} | {r['eod_flatten']} |"
        )
    lines += ["", "## Live vs backtest TCA (bps, filled-qty weighted)", ""]
    lines += ["| component | simulated | expected | gap |", "|---|---|---|---|"]
    for k, v in rep["tca"]["aggregate"].items():
        lines.append(
            f"| {k} | {_f(v['simulated_bps'])} | {_f(v['expected_bps'])} | {_f(v['gap_bps'])} |"
        )
    lines += ["", "## Alpha decay (realised IC vs holdout)", ""]
    for a, v in (rep["monitors"].get("ic") or {}).items():
        lines.append(
            f"- {a}: day {_f(v['realised_ic'], 4)} (n={v['n_rows']}) vs holdout "
            f"{v['holdout_ic']}; rolling {v['rolling']['days']}d {_f(v['rolling']['mean_ic'], 4)}"
        )
    d = rep["drill"]
    lines += ["", "## Kill-switch drill", "", f"- {d['symbol']}: **{d['status']}**"]
    if "kill_cancels" in d:
        lines.append(
            f"- working at kill {d['working_children_at_kill']}, cancels {d['kill_cancels']}, "
            f"submitted after {d['children_submitted_after_kill']}, "
            f"post-kill fills {d['post_kill_fills_alerted']}"
        )
    lines += ["", "## Alerts", ""]
    lines += [f"- {a}" for a in rep["alerts"]] or ["- none"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m iap.paper.daily", description=__doc__.split("\n")[0]
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--date", help="session date YYYY-MM-DD")
    g.add_argument(
        "--pending",
        action="store_true",
        help="every DATA_ROOT/paper_* session without a complete report (scheduled mode)",
    )
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--dataset", default=None, help="ingested dataset directory")
    p.add_argument(
        "--itch", default=None, help="ITCH 5.0 file to ingest when no dataset holds the date"
    )
    p.add_argument("--data-root", default=None, help="default <repo>/data/real")
    p.add_argument("--out-root", default=None, help="default DATA_ROOT/paper")
    p.add_argument("--symbols", default=None, help="comma-separated; default from the config")
    p.add_argument(
        "--limit-events", type=int, default=None, help="first N events per symbol (smoke)"
    )
    p.add_argument("--force", action="store_true", help="rebuild a completed date")
    a = p.parse_args(argv)
    data_root = None if a.data_root is None else Path(a.data_root)
    out_root = None if a.out_root is None else Path(a.out_root)
    if a.pending:
        dr = data_root or REPO_ROOT / "data" / "real"
        dates = pending_dates(dr, out_root or dr / "paper")
        print(f"pending sessions: {dates or 'none'}")
    else:
        dates = [a.date]
    worst = 0
    for date in dates:
        code = _main_one(a, date, data_root, out_root)
        worst = code if code == 2 or worst == 0 else worst
    return worst


def _main_one(
    a: argparse.Namespace, date: str, data_root: Path | None, out_root: Path | None
) -> int:
    try:
        code, _ = run_day(
            date,
            config_path=Path(a.config),
            dataset=None if a.dataset is None else Path(a.dataset),
            itch=None if a.itch is None else Path(a.itch),
            data_root=data_root,
            out_root=out_root,
            symbols=None
            if a.symbols is None
            else [s.strip() for s in a.symbols.split(",") if s.strip()],
            limit_events=a.limit_events,
            force=a.force,
        )
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
