"""In-sample signal-skewed quoting study on the real ITCH sessions (v1.10 M5).

Pre-registered in ``research/quoting_real/prereg.json`` (blackboard entries
``<cell>/1s:quoting-insample`` for EQ01, EQ05, EQ10 and the QNOSKEW
control); the driver reads its parameters from that file and refuses to run
unless the blackboard carries every cell with that file's sha256.

Per unit (session, symbol), in session order and one unit at a time (the
structure of :mod:`iap.research.maker_real`):

1. load the symbol's events of the session;
2. calibration of THIS session (reused read-only from the maker study's
   ``calib/`` when it was computed there on the full session, else computed
   identically here, with the ASSUMED parametric latency) - it is used by
   the NEXT session only;
3. from session 2 on: a 1 s decision grid (09:31-15:59 New York); per
   skewed cell whose universe holds the symbol, the alpha fitted on earlier
   sessions scores the grid (``score()``, the clipped port path) and
   :class:`QuotingBacktester` runs with ``alpha_weight = 1``; the control
   runs with ``expected_return = 0`` and ``alpha_weight = 0``; both use the
   PREVIOUS session's calibration and flatten at 16:00;
4. checkpoint every cell to ``cell_units/<session>_<symbol>_<cell>.json``
   atomically (a resumed unit reuses finished cells; a heartbeat line is
   logged every 10 min inside a cell), then assemble
   ``units/<session>_<symbol>.json`` atomically (resume skips it).

``summarise`` pools units into session-clustered statistics and the
pre-registered verdicts.
"""

from __future__ import annotations

import argparse
import gc
import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from iap.research.maker_real import (
    ROOT,
    SESSIONS,
    STATE_COLS,
    SYMBOLS,
    cluster_stats,
    file_sha256,
    load_events,
    load_feature_day,
    log,
    peak_rss_gb,
    unit_name,
)

TZ = "America/New_York"
GRID_START, GRID_END, FLATTEN_AT = "09:31:00", "15:59:00", "16:00:00"
MIN_SESSIONS = 4
SEC = 1_000_000_000


HEARTBEAT_S = 600.0


class Heartbeat:
    """Logs a line every ``interval_s`` while a long cell runs (elapsed time,
    the unit's event and decision counts, peak memory). The replay itself
    lives in :class:`QuotingBacktester` and exposes no progress counter, so
    the line reports the size of the work, not the position inside it."""

    def __init__(self, label: str, interval_s: float = HEARTBEAT_S, **info: Any) -> None:
        import threading

        self.label, self.interval, self.info = label, interval_s, info
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.beats = 0

    def _run(self) -> None:
        t0 = time.time()
        while not self._stop.wait(self.interval):
            self.beats += 1
            extra = " ".join(f"{k}={v}" for k, v in self.info.items())
            rss = peak_rss_gb()
            log(
                f"  heartbeat {self.label}: {time.time() - t0:.0f}s elapsed, {extra}, "
                f"peak {rss if rss is None else round(rss, 2)} GB"
            )

    def __enter__(self) -> Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()


def load_or_compute(path: Path, compute) -> dict[str, Any]:
    """Cell checkpoint: return the saved JSON at ``path`` if present, else
    ``compute()`` and save it atomically (temp + rename) before returning."""
    from iap.experiment.locking import atomic_write_text

    path = Path(path)
    if path.is_file():
        log(f"  reuse cell checkpoint {path.name}")
        return json.loads(path.read_text(encoding="utf-8"))
    doc = compute()
    atomic_write_text(
        path, json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def check_prereg(repo_root: Path, prereg: Mapping[str, Any], sha: str) -> list[dict]:
    from iap.agents.prereg_gate import require

    entries = [
        require(repo_root, c["id"], f"{c['horizon']}:quoting-insample") for c in prereg["cells"]
    ]
    for e in entries:
        if sha not in e["body"]["hypothesis"]:
            raise SystemExit(f"prereg.json sha256 {sha} is not the registered one")
    return entries


def ns_at(session: str, hhmmss: str) -> int:
    date = session.split("_")[1]
    return int(pd.Timestamp(f"{date} {hhmmss}", tz=TZ).value)


def second_grid(feats: pd.DataFrame, start_ns: int, end_ns: int) -> pd.DataFrame:
    """First feature row of each second in [start_ns, end_ns)."""
    ts = feats["exchange_ts"].to_numpy(np.int64)
    f = feats[(ts >= start_ns) & (ts < end_ns)]
    sec = f["exchange_ts"].to_numpy(np.int64) // SEC
    keep = np.r_[True, sec[1:] != sec[:-1]] if len(sec) else np.zeros(0, dtype=bool)
    return f[keep].reset_index(drop=True)


def quoting_config(prereg: Mapping[str, Any], alpha_weight: float, flatten_ts: int):
    from iap.backtest.quoting import QuotingConfig

    c = {k: v for k, v in prereg["quoting_config"].items() if k != "flatten_ts"}
    return QuotingConfig(**c, alpha_weight=float(alpha_weight), flatten_ts=int(flatten_ts))


def run_summary(res) -> dict[str, Any]:
    """The JSON-safe summary of one QuotingResult plus net bps per quote fill."""
    s = res.summary()
    f = res.fills
    quotes = f[~f["flatten"]] if len(f) else f
    notional = float((quotes["px"] * quotes["qty"]).sum()) if len(quotes) else 0.0
    s["quote_notional"] = notional
    s["net_bps_per_notional"] = 1e4 * s["net"] / notional if notional else None
    flat = f[f["flatten"]] if len(f) else f
    s["flatten_qty"] = int(flat["qty"].sum()) if len(flat) else 0
    out: dict[str, Any] = {}
    for k, v in s.items():
        if isinstance(v, (np.integer,)):
            v = int(v)
        elif isinstance(v, (np.floating,)):
            v = float(v)
        out[k] = v
    return out


class Study:
    def __init__(self, args: argparse.Namespace) -> None:
        from iap.alpha import configure_universe
        from iap.execution.config import load_exec_config

        self.args = args
        self.data = Path(args.data_root)
        self.features_dir = self.data / "multi7" / "features"
        self.maker_calib = self.data / "maker_real" / "calib"
        self.out = Path(args.out)
        for sub in ("calib", "units"):
            (self.out / sub).mkdir(parents=True, exist_ok=True)
        self.prereg = json.loads(Path(args.prereg).read_text(encoding="utf-8"))
        self.prereg_sha = file_sha256(Path(args.prereg))
        configure_universe(self.data / "multi7" / "configs" / "instruments" / "instruments.json")
        self.exec_config = load_exec_config(self.data / "multi7" / "configs")
        self._alpha_cache: dict[tuple[str, int], Any] = {}

    def calib_path(self, day: int, symbol: str) -> Path | None:
        name = f"{unit_name(SESSIONS[day], symbol)}.json"
        own = self.out / "calib" / name
        if own.is_file():
            return own
        mk = self.maker_calib / name
        if mk.is_file():
            doc = json.loads(mk.read_text(encoding="utf-8"))
            if (doc.get("source") or {}).get("slice") is None and "latency" in doc:
                return mk
        return None

    def ensure_calibration(self, day: int, symbol: str, events: list) -> str:
        from iap.execution.calibration import (
            estimate_calibration,
            parametric_latency,
            write_calibration,
        )

        if self.args.slice is None and self.calib_path(day, symbol) is not None:
            return str(self.calib_path(day, symbol))
        iid = SYMBOLS[symbol]
        lat = self.prereg["latency_ASSUMED"]
        doc = estimate_calibration(
            events,
            {iid: self.exec_config.instrument(iid).tick_size},
            source={"dataset": SESSIONS[day], "symbol": symbol, "slice": self.args.slice},
        )
        doc["latency"] = {
            str(lat["venue_id"]): parametric_latency(
                lat["mean_ns"], lat["jitter_ns"], lat["tail_prob"], lat["tail_mult"]
            )
        }
        doc["latency_status"] = "ASSUMED (parametric; ITCH has no receive stamp)"
        path = self.out / "calib" / f"{unit_name(SESSIONS[day], symbol)}.json"
        tmp = path.with_name(f".{path.name}.tmp")
        write_calibration(doc, tmp)
        tmp.replace(path)  # atomic: a resumed run never reads a partial calibration
        return str(path)

    def alpha(self, alpha_id: str, horizon: str, day: int):
        from iap.alpha import build

        key = (alpha_id, day)
        if key not in self._alpha_cache:
            model = build(alpha_id)
            model.horizon = horizon
            need = [*model.features, f"label_mid_{horizon}", f"label_valid_{horizon}", *STATE_COLS]
            train = {
                iid: pd.concat(
                    [load_feature_day(self.features_dir, iid, d, need) for d in range(day)],
                    ignore_index=True,
                )
                for iid in model.universe(sorted(SYMBOLS.values()))
            }
            model.fit(train)
            self._alpha_cache[key] = model
        return self._alpha_cache[key]

    def window(self, day: int) -> tuple[int, int, int]:
        """(grid start, grid end, flatten_ts) ns; a smoke slice shortens all three."""
        s = SESSIONS[day]
        if self.args.slice:
            a, b = self.args.slice.split("-")
            end = ns_at(s, f"{b}:00")
            return ns_at(s, f"{a}:00"), end - 60 * SEC, end
        return ns_at(s, GRID_START), ns_at(s, GRID_END), ns_at(s, FLATTEN_AT)

    def backtests(self, day: int, symbol: str, events: list) -> dict[str, Any]:
        from iap.alpha import build
        from iap.execution.calibration import load_calibration

        iid = SYMBOLS[symbol]
        prev = self.calib_path(day - 1, symbol)
        if prev is None:
            raise SystemExit(f"no calibration for {SESSIONS[day - 1]} {symbol}")
        cal = load_calibration(prev)
        g0, g1, flat = self.window(day)
        need = list(STATE_COLS)
        for c in self.prereg["cells"]:
            if c["alpha"]:
                need += list(build(c["alpha"]).features)
        feats = load_feature_day(self.features_dir, iid, day, need)
        grid = second_grid(feats, g0, g1)
        del feats
        out: dict[str, Any] = {"calibration_used": str(prev), "n_decisions": len(grid)}
        cdir = self.out / "cell_units"
        cdir.mkdir(parents=True, exist_ok=True)
        for cell in self.prereg["cells"]:
            cpath = cdir / f"{unit_name(SESSIONS[day], symbol)}_{cell['id']}.json"
            label = f"{SESSIONS[day]} {symbol} {cell['id']}"
            s = load_or_compute(
                cpath,
                lambda cell=cell, label=label: self.run_cell(
                    cell, day, iid, grid, flat, cal, events, label
                ),
            )
            if s.get("not_in_universe"):
                continue
            out[cell["id"]] = s
        return out

    def run_cell(self, cell, day, iid, grid, flat, cal, events, label) -> dict[str, Any]:
        """One cell of one unit (checkpointed by the caller)."""
        from iap.backtest.quoting import QuotingBacktester

        t0 = time.time()
        if cell["alpha"]:
            model = self.alpha(cell["alpha"], cell["horizon"], day)
            if iid not in model.universe([iid]):
                return {"not_in_universe": True}
            sc = model.score({iid: grid})[iid][["exchange_ts", "expected_return"]]
            fit = {
                "mu": model.mu,
                "sigma": model.sigma,
                "beta": model.beta,
                "n_train": model.n_train,
                "dead": model.is_dead,
            }
        else:
            sc = pd.DataFrame(
                {
                    "exchange_ts": grid["exchange_ts"].to_numpy(np.int64),
                    "expected_return": np.zeros(len(grid)),
                }
            )
            fit = None
        qc = quoting_config(self.prereg, cell["alpha_weight"], flat)
        with Heartbeat(label, n_events=len(events), n_decisions=len(sc)):
            res = QuotingBacktester(self.exec_config, qc, cal).run_instrument(events, sc, iid)
        s = run_summary(res)
        s["alpha_fit"] = fit
        s["er_abs_mean_bps"] = float(1e4 * np.abs(sc["expected_return"]).mean()) if len(sc) else 0.0
        s["seconds"] = round(time.time() - t0, 1)
        log(f"  {label}: net {s['net']:+.2f} USD, {s['n_quote_fills']} fills, {s['seconds']}s")
        return s

    def run_unit(self, day: int, symbol: str) -> None:
        path = self.out / "units" / f"{unit_name(SESSIONS[day], symbol)}.json"
        if path.is_file():
            log(f"skip {path.name} (done)")
            return
        t0 = time.time()
        _, _, flat = self.window(day)
        end = flat + 60 * SEC if self.args.slice else None
        events = load_events(self.data / SESSIONS[day], SYMBOLS[symbol], end)
        t_load = time.time() - t0
        log(f"unit {SESSIONS[day]} {symbol}: {len(events)} events loaded in {t_load:.0f}s")
        cal_path = self.ensure_calibration(day, symbol, events)
        t_cal = time.time() - t0
        results = self.backtests(day, symbol, events) if day > 0 else {}
        t_all = time.time() - t0
        doc = {
            "session": SESSIONS[day],
            "day_index": day,
            "symbol": symbol,
            "instrument_id": SYMBOLS[symbol],
            "warmup_only": day == 0,
            "slice": self.args.slice,
            "n_events": len(events),
            "prereg_sha256": self.prereg_sha,
            "calibration_this_session": cal_path,
            "latency": "ASSUMED parametric",
            "results": results,
            "timing_s": {
                "load": round(t_load, 1),
                "calibration": round(t_cal - t_load, 1),
                "backtests": round(t_all - t_cal, 1),
                "total": round(t_all, 1),
            },
            "peak_rss_gb": peak_rss_gb(),
        }
        from iap.experiment.locking import atomic_write_text

        atomic_write_text(
            path, json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        log(f"done {path.name} in {doc['timing_s']['total']}s, peak {doc['peak_rss_gb']:.2f} GB")
        del events
        gc.collect()

    def run(self) -> None:
        days = [SESSIONS.index(s) for s in self.args.sessions] if self.args.sessions else range(7)
        for d in days:
            for s in self.args.symbols or list(SYMBOLS):
                self.run_unit(d, s)
        summary = summarise(self.out / "units", self.prereg)
        from iap.experiment.locking import atomic_write_text

        atomic_write_text(
            self.out / "summary.json",
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        log(f"summary -> {self.out / 'summary.json'}")


# ------------------------------------------------------------------ summary

PARTS = (
    "spread_captured",
    "markout",
    "inventory_pnl",
    "flatten_cost",
    "rebates",
    "taker_fees",
    "impact",
    "net",
)


def verdict(st: Mapping[str, Any], min_sessions: int = MIN_SESSIONS) -> str:
    ci = st.get("ci_bonf")
    if (
        st.get("n_sessions", 0) >= min_sessions
        and st.get("mean") is not None
        and st["mean"] > 0
        and ci is not None
        and ci[0] > 0
    ):
        return "EXISTS (in-sample; worth an out-of-sample test)"
    return "NO DEMONSTRATED QUOTING EDGE"


def per_session(
    units: Sequence[Mapping[str, Any]], cell: str, symbols: set[str] | None = None
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for u in units:
        if u["warmup_only"] or (symbols is not None and u["symbol"] not in symbols):
            continue
        r = u["results"].get(cell)
        if not r:
            continue
        acc = out.setdefault(
            u["session"],
            {**dict.fromkeys(PARTS, 0.0), "fills": 0, "symbols": [], "max_abs_inventory": 0},
        )
        for k in PARTS:
            acc[k] += float(r[k])
        acc["fills"] += int(r["n_quote_fills"])
        acc["max_abs_inventory"] = max(acc["max_abs_inventory"], int(r["max_abs_inventory"]))
        acc["symbols"].append(u["symbol"])
    return out


def summarise(units_dir: Path, prereg: Mapping[str, Any]) -> dict[str, Any]:
    from iap.backtest.quoting import sharpe_per_day

    units = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(units_dir.glob("*.json"))]
    n_cells = len(prereg["cells"])
    out: dict[str, Any] = {
        "status": "EXPLORATORY IN-SAMPLE",
        "units": len(units),
        "test_sessions": sorted({u["session"] for u in units if not u["warmup_only"]}),
        "cells": {},
    }
    control = next(c["id"] for c in prereg["cells"] if not c["alpha"])
    for cell in prereg["cells"]:
        per = per_session(units, cell["id"])
        nets = {d: a["net"] for d, a in per.items()}
        active = [nets[d] for d, a in per.items() if a["fills"] > 0]
        st = cluster_stats(list(nets.values()), n_cells)
        st_active = {**st, "n_sessions": len(active)}
        row: dict[str, Any] = {
            "per_session": per,
            **st,
            "sessions_with_fills": len(active),
            "sharpe_per_day": sharpe_per_day(list(nets.values())),
            "verdict": verdict(st_active),
        }
        if cell["alpha"]:
            syms = {s for a in per.values() for s in a["symbols"]}
            ctl = per_session(units, control, syms)
            diffs = [nets[d] - ctl[d]["net"] for d in nets if d in ctl]
            dst = cluster_stats(diffs, n_cells)
            row["skew_minus_control"] = {
                **dst,
                "per_session": {d: nets[d] - ctl[d]["net"] for d in nets if d in ctl},
                "skew_helps": bool(dst.get("ci_bonf") and dst["ci_bonf"][0] > 0),
            }
        out["cells"][cell["id"]] = row
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-root", required=True, help="data/real of the main checkout")
    ap.add_argument("--out", required=True)
    ap.add_argument("--prereg", default=str(ROOT / "research" / "quoting_real" / "prereg.json"))
    ap.add_argument("--sessions", nargs="*", choices=SESSIONS)
    ap.add_argument("--symbols", nargs="*", choices=list(SYMBOLS))
    ap.add_argument(
        "--slice", help="smoke slice HH:MM-HH:MM New York (grid to end-1min, flatten at end)"
    )
    ap.add_argument("--summary-only", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)
    prereg = json.loads(Path(args.prereg).read_text(encoding="utf-8"))
    sha = file_sha256(Path(args.prereg))
    entries = check_prereg(ROOT, prereg, sha)
    log(f"prereg verified ({len(entries)} cells, sha256 {sha[:12]})")
    if args.summary_only:
        print(json.dumps(summarise(Path(args.out) / "units", prereg), indent=2, sort_keys=True))
        return 0
    Study(args).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
