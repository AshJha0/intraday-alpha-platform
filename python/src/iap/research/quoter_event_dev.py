"""Grid vs event-driven quoter on one in-sample ITCH day (development only).

Exploratory, in-sample, no edge claim; ``research/round1/QUOTER_EVENT_DRIVEN_DEV.md``
holds the pre-specified parameters and the reading. Per symbol, one at a
time: load the symbol's events, run EXACTLY two configurations (``grid``,
``events``) that differ only in ``trigger_mode``, write one JSON per symbol.

    python -m iap.research.quoter_event_dev --dataset DS --date 2019-01-30 \\
        --symbols AAPL,MSFT,QQQ --out research/round1/quoter_event_dev

Common to both runs (the signed v2 ``quoting_config`` shape with the v2
execution-realism opt-ins, minus what one day cannot supply): one decision
row per second (the first event of each second in 09:31-15:59 New York),
``expected_return = 0`` and ``alpha_weight = 0`` (the no-skew control), no
calibration (no earlier session: venue latency from the dataset's venue
config, adverse-selection floor term 0), regulatory fees on, flatten at
16:00 with a 60 s lead. Sessions dated 2025 or later are refused.
"""

from __future__ import annotations

import argparse
import gc
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from iap.backtest.quoting import QuotingBacktester, QuotingConfig
from iap.research.maker_real import load_events

TZ = "America/New_York"
SEC = 1_000_000_000

#: rule-10 parameters, fixed before any real-data run (result document,
#: "Pre-specification"); the grid run ignores them
EVENT_PARAMS = {
    "ev_min_rest_ns": 100_000_000,
    "ev_max_msgs_per_s": 10,
    "ev_queue_ahead_max": 100,
    "ev_imbalance": 0.8,
}

#: the signed v2 quoting_config (research/quoting_real/prereg_v2.json) and
#: the v2 execution-realism opt-ins (configs/prereg/v2.json), read-only copy
BASE = {
    "skew_mode": "ticks",
    "h_min_ticks": 0,
    "inv_skew_ticks": 1.0,
    "qty": 100,
    "max_inventory": 300,
    "gamma": 0.1,
    "tau_s": 10.0,
    "sigma": None,
    "sigma_halflife": 20,
    "sigma_init": 0.0001,
    "min_half_spread_ticks": 0.5,
    "as_floor": True,
    "as_horizon": "1s",
    "markout_ns": SEC,
    "flatten_lead_ns": 60 * SEC,
    "instant_cancel": False,
    "feed_latency_ns": 100_000,
    "flatten_route": "sim",
    "amend_size_down": True,
    "hidden_print_fills": True,
    "alpha_weight": 0.0,
    "dev_diagnostics": True,
}
MODES = ("grid", "events")


def ns_at(date: str, hhmmss: str) -> int:
    return int(pd.Timestamp(f"{date} {hhmmss}", tz=TZ).value)


def second_rows(ts: np.ndarray, start: int, end: int) -> pd.DataFrame:
    """First event timestamp of each second in [start, end), er = 0."""
    t = ts[(ts >= start) & (ts < end)]
    sec = t // SEC
    keep = np.r_[True, sec[1:] != sec[:-1]] if t.size else np.zeros(0, dtype=bool)
    rows = t[keep]
    return pd.DataFrame({"exchange_ts": rows, "expected_return": np.zeros(rows.size)})


def config(mode: str, flatten_ts: int) -> QuotingConfig:
    extra = EVENT_PARAMS if mode == "events" else {}
    return QuotingConfig(**BASE, **extra, trigger_mode=mode, flatten_ts=flatten_ts)


def measure(res, horizons: Sequence[int]) -> dict[str, Any]:
    """Summary plus the markout decomposition at each horizon and the
    inventory distribution (identical for both quoters)."""
    s = res.summary()
    f = res.fills
    qf = f[~f["flatten"]] if len(f) else f
    out: dict[str, Any] = {}
    for k, v in s.items():
        if isinstance(v, np.integer):
            v = int(v)
        elif isinstance(v, np.floating):
            v = float(v)
        out[k] = v
    notional = float((qf["px"] * qf["qty"]).sum()) if len(qf) else 0.0
    out["quote_notional"] = notional
    out["quote_qty"] = int(qf["qty"].sum()) if len(qf) else 0
    for h in horizons:
        col = f"mid_{h // SEC}s"
        mk = float((qf["d"] * (qf[col] - qf["mid"])).sum()) if len(qf) else 0.0
        out[f"markout_{h // SEC}s"] = mk
        out[f"markout_{h // SEC}s_bps"] = 1e4 * mk / notional if notional else None
    out["spread_captured_bps"] = 1e4 * out["spread_captured"] / notional if notional else None
    out["net_bps"] = 1e4 * out["net"] / notional if notional else None
    inv = res.inventory
    if len(inv) > 1:
        q = inv["q"].to_numpy(dtype=float)[:-1]
        dt = np.diff(inv["ts"].to_numpy(dtype=np.int64)).astype(float)
        a = np.abs(q)
        order = np.argsort(a, kind="stable")
        cw = np.cumsum(dt[order]) / dt.sum() if dt.sum() else np.zeros(0)
        for p in (50, 90, 99):
            j = int(np.searchsorted(cw, p / 100.0)) if cw.size else 0
            out[f"abs_inventory_p{p}"] = float(a[order][min(j, a.size - 1)]) if a.size else 0.0
    return out


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", required=True)
    p.add_argument("--date", required=True, help="YYYY-MM-DD (before 2025)")
    p.add_argument("--symbols", default="AAPL,MSFT,QQQ")
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    if int(a.date[:4]) >= 2025:
        raise SystemExit("sessions dated 2025 or later are a sealed holdout")
    from iap.execution.config import load_exec_config

    ds = Path(a.dataset)
    syms = a.symbols.split(",")
    manifest = json.loads((ds / "dataset.json").read_text(encoding="utf-8"))
    iids = {u["symbol"]: int(u["instrument_id"]) for u in manifest["universe"]}
    from dataclasses import replace

    from iap.execution.regulatory import load_default_schedule
    from iap.research.maker_real import ROOT

    # the dated Section 31 + TAF schedule lives in the repository's configs
    exec_config = replace(
        load_exec_config(ds / "configs"),
        regulatory_fees=load_default_schedule(ROOT / "configs"),
    )
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    g0, g1 = ns_at(a.date, "09:31:00"), ns_at(a.date, "15:59:00")
    flat = ns_at(a.date, "16:00:00")
    print(
        f"configurations: {len(MODES)} per symbol x {len(syms)} symbols = {len(MODES) * len(syms)}"
    )
    for sym in syms:
        path = out / f"{a.date}_{sym}.json"
        if path.is_file():
            print(f"{sym}: done already ({path})")
            continue
        t0 = time.time()
        evs = load_events(ds, iids[sym], flat + 120 * SEC)
        ts = np.fromiter((e.exchange_ts for e in evs), dtype=np.int64, count=len(evs))
        sc = second_rows(ts, g0, g1)
        print(f"{sym}: {len(evs)} events, {len(sc)} decision rows, loaded {time.time() - t0:.0f}s")
        doc: dict[str, Any] = {"symbol": sym, "date": a.date, "n_events": len(evs)}
        doc["n_decision_rows"] = len(sc)
        for mode in MODES:
            t1 = time.time()
            cfg = config(mode, flat)
            res = QuotingBacktester(exec_config, cfg).run_instrument(evs, sc, iids[sym])
            doc[mode] = measure(res, cfg.dev_markout_ns)
            doc[mode]["seconds"] = round(time.time() - t1, 1)
            print(
                f"  {sym} {mode}: net {doc[mode]['net']:+.2f} USD, "
                f"{doc[mode]['n_quote_fills']} fills, {doc[mode]['seconds']}s",
                flush=True,
            )
            del res
            gc.collect()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        del evs
        gc.collect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
