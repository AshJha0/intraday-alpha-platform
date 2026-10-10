"""AUC01 auction-imbalance study on real ITCH NOII streams (plan item A1).

Pre-registered in ``research/auction_real/prereg.json`` (blackboard entries
``AUC01/C-300s``, ``AUC01/O-300s``, ``AUC01/C-300s:holdout2026``); every
subcommand that touches results checks the blackboard first.

* ``mids`` - real mid and half-spread at each NOII snapshot of a stream
  directory, from one streaming pass over the session's normalized JSONL
  (one :class:`ConsolidatedBook` per symbol). NaN when the book is not
  two-sided or is locked/crossed. Writes the ``--mids`` CSV of
  ``python -m iap.auction backtest`` (``symbol,ts,mid,half_spread``).
* ``insample`` - the registered walk-forward per cell (n_folds = 6) with
  session-clustered statistics and the pre-registered verdict.
* ``holdout`` - the frozen C-300s fit on all in-sample sessions applied
  once to the holdout streams (refuses unless the in-sample summary exists).
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from iap.research.maker_real import ROOT, cluster_stats, file_sha256, log

N_FOLDS = 6
MIN_SESSIONS = 4


# ------------------------------------------------------------------ mids


def query_times(noii: pd.DataFrame) -> dict[str, np.ndarray]:
    """Sorted unique snapshot times per symbol, plus 1 ns before each
    scheduled cross of the window (so ``mid_pre_cross`` sees the last book)."""
    from iap.auction.features import SCHEDULED_CROSS_TOD_NS

    out: dict[str, np.ndarray] = {}
    for sym, g in noii.groupby("symbol"):
        ts = g["ts"].to_numpy(np.int64)
        midnight = ts - g["tod_ns"].to_numpy(np.int64)
        extra = [
            m + SCHEDULED_CROSS_TOD_NS[ct] - 1
            for m, ct in zip(midnight, g["cross_type"], strict=True)
            if ct in SCHEDULED_CROSS_TOD_NS
        ]
        out[str(sym)] = np.unique(np.concatenate([ts, np.asarray(extra, dtype=np.int64)]))
    return out


def book_mids(
    events: Iterable,
    queries: Mapping[int, np.ndarray],
    tick: Mapping[int, float],
) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """(mid, half_spread) in dollars prevailing at each query time per
    instrument: the book after every event with ``exchange_ts <= t``."""
    from iap.orderbook.book import ConsolidatedBook

    books = {iid: ConsolidatedBook(iid) for iid in queries}
    pos = dict.fromkeys(queries, 0)
    res = {iid: (np.full(len(q), np.nan), np.full(len(q), np.nan)) for iid, q in queries.items()}

    def state(iid: int) -> tuple[float, float]:
        b = books[iid]
        bb, ba = b.best_bid(), b.best_ask()
        if bb is None or ba is None or bb[0] >= ba[0]:
            return math.nan, math.nan
        t = tick[iid]
        return 0.5 * (bb[0] + ba[0]) * t, 0.5 * (ba[0] - bb[0]) * t

    def flush(iid: int, upto: int | None) -> None:
        q = queries[iid]
        i = pos[iid]
        if i >= len(q) or (upto is not None and q[i] >= upto):
            return
        m, h = state(iid)
        while i < len(q) and (upto is None or q[i] < upto):
            res[iid][0][i], res[iid][1][i] = m, h
            i += 1
        pos[iid] = i

    for ev in events:
        iid = ev.instrument_id
        if iid not in books:
            continue
        flush(iid, ev.exchange_ts)
        books[iid].apply(ev)
    for iid in books:
        flush(iid, None)
    return res


def mids_for_session(stream_dir: Path, session_dir: Path) -> pd.DataFrame:
    from iap.auction.stream import read_auction_stream
    from iap.core.codec import iter_jsonl

    s = read_auction_stream(stream_dir)
    ds = json.loads((session_dir / "dataset.json").read_text(encoding="utf-8"))
    uni = {u["symbol"]: u for u in ds["universe"]}
    date = str(s.noii["date"].iloc[0]).replace("-", "")
    files = sorted((session_dir / "normalized").glob(f"*{date}*.normalized.jsonl"))
    if len(files) != 1:
        raise SystemExit(f"expected one normalized JSONL for {date} in {session_dir}: {files}")
    qs = query_times(s.noii)
    q_iid = {uni[sym]["instrument_id"]: q for sym, q in qs.items() if sym in uni}
    tick = {uni[sym]["instrument_id"]: float(uni[sym]["tick_size"]) for sym in qs if sym in uni}
    res = book_mids(iter_jsonl(files[0]), q_iid, tick)
    sym_of = {u["instrument_id"]: sym for sym, u in uni.items()}
    frames = [
        pd.DataFrame({"symbol": sym_of[iid], "ts": q_iid[iid], "mid": m, "half_spread": h})
        for iid, (m, h) in res.items()
    ]
    return pd.concat(frames, ignore_index=True)


# ------------------------------------------------------------------ study


def load_labels(streams: Sequence[str], mids: Sequence[str], cross_type: str) -> pd.DataFrame:
    from iap.auction.features import auction_features
    from iap.auction.stream import AuctionStream, read_auction_stream
    from iap.auction.targets import auction_targets

    stream = AuctionStream.concat(read_auction_stream(d) for d in streams)
    m = pd.concat([pd.read_csv(p, dtype={"symbol": str}) for p in mids], ignore_index=True)
    return auction_targets(auction_features(stream.noii, cross_type), stream.crosses, m)


def cell_config(cell: Mapping[str, Any], prereg: Mapping[str, Any]):
    from iap.auction.strategy import AuctionStrategyConfig

    c = prereg["config"]
    return AuctionStrategyConfig(
        cross_type=cell["cross_type"],
        decision_s=float(cell["decision_s"]),
        exit=cell["exit"],
        qty=int(c["qty"]),
        adv_shares=float(c["adv_shares"]),
        default_half_spread_bps=float(c["default_half_spread_bps"]),
        cross_fee_per_share=float(c["cross_fee_per_share"]),
    )


def session_means(trades: pd.DataFrame) -> dict[str, dict[str, float]]:
    if not len(trades):
        return {}
    g = trades.groupby("date")["net_bps"]
    return {
        str(d): {"n": int(n), "mean_net_bps": float(m)}
        for d, n, m in zip(g.size().index, g.size(), g.mean(), strict=True)
    }


def verdict(st: Mapping[str, Any], min_sessions: int = MIN_SESSIONS) -> str:
    ci = st.get("ci_bonf")
    if (
        st.get("n_sessions", 0) >= min_sessions
        and st.get("mean") is not None
        and st["mean"] > 0
        and ci is not None
        and ci[0] > 0
    ):
        return "EXISTS (in-sample, exploratory; worth the holdout)"
    return "NO DEMONSTRATED EDGE"


def holdout_verdict(fitted: bool, net_bps: np.ndarray) -> dict[str, Any]:
    n = int(net_bps.size)
    mean = float(net_bps.mean()) if n else None
    sd = float(net_bps.std(ddof=1)) if n > 1 else None
    t = float(mean / (sd / math.sqrt(n))) if n > 1 and sd and sd > 0 else None
    ok = fitted and n >= 3 and mean is not None and mean > 0 and t is not None and t >= 1.645
    return {
        "n_trades": n,
        "mean_net_bps": mean,
        "t_one_sided": t,
        "verdict": "CONFIRMED (consistent; underpowered)" if ok else "NOT CONFIRMED",
    }


def _require(horizon: str) -> dict:
    from iap.agents.prereg_gate import require

    return require(ROOT, "AUC01", horizon)


def _check_sha(entry: Mapping[str, Any], sha: str) -> None:
    if sha not in entry["body"]["hypothesis"]:
        raise SystemExit(f"prereg.json sha256 {sha} is not the registered one")


def run_insample(a: argparse.Namespace) -> dict[str, Any]:
    from iap.auction.strategy import walk_forward
    from iap.backtest.costs import CostModel

    prereg = json.loads(Path(a.prereg).read_text(encoding="utf-8"))
    sha = file_sha256(Path(a.prereg))
    cm = CostModel.load(str(ROOT / "configs" / "execution" / "execution.json"))
    out: dict[str, Any] = {
        "status": "EXPLORATORY IN-SAMPLE",
        "prereg_sha256": sha,
        "streams": list(a.streams),
        "cells": {},
    }
    n_cells = len(prereg["cells"])
    for cell in prereg["cells"]:
        _check_sha(_require(cell["horizon"]), sha)
        labels = load_labels(a.streams, a.mids, cell["cross_type"])
        cfg = cell_config(cell, prereg)
        wf = walk_forward(labels, cfg, cm, n_folds=N_FOLDS)
        test_days = sorted({str(d) for d in labels["date"].unique()})[1:]
        per = session_means(wf.trades)
        st = cluster_stats([v["mean_net_bps"] for v in per.values()], n_cells)
        out["cells"][cell["horizon"]] = {
            "role": cell["role"],
            "mid_source": str(labels["mid_source"].iloc[0]) if len(labels) else None,
            "decisions_with_mid": int(labels.groupby(["date", "symbol"]).ngroups),
            "test_sessions": test_days,
            "per_session": per,
            **st,
            "verdict": verdict(st),
            "walk_forward": wf.to_dict(),
        }
        log(f"{cell['horizon']}: {wf.summary()} -> {out['cells'][cell['horizon']]['verdict']}")
    out["final"] = True
    return out


def run_holdout(a: argparse.Namespace) -> dict[str, Any]:
    from dataclasses import asdict

    from iap.auction.strategy import DEFAULT_THRESHOLDS, _apply, _fit, _trade_frame, decision_rows
    from iap.backtest.costs import CostModel

    ins = json.loads(Path(a.insample_summary).read_text(encoding="utf-8"))
    if not ins.get("final"):
        raise SystemExit("in-sample summary is not final; the holdout stays sealed")
    prereg = json.loads(Path(a.prereg).read_text(encoding="utf-8"))
    sha = file_sha256(Path(a.prereg))
    _check_sha(_require("C-300s:holdout2026"), sha)
    cell = next(c for c in prereg["cells"] if c["horizon"] == "C-300s")
    cfg = cell_config(cell, prereg)
    cm = CostModel.load(str(ROOT / "configs" / "execution" / "execution.json"))
    tr = load_labels(a.streams, a.mids, "C")
    frame, valid = _trade_frame(decision_rows(tr, cfg), cfg, cm)
    fitted = _fit(frame, valid, cfg, DEFAULT_THRESHOLDS, 5)
    ho = load_labels(a.holdout_streams, a.holdout_mids, "C")
    hframe, hvalid = _trade_frame(decision_rows(ho, cfg), cfg, cm)
    trades = _apply(hframe, hvalid, fitted) if fitted is not None else hframe[:0]
    net = trades["net_bps"].to_numpy(float) if len(trades) else np.zeros(0)
    return {
        "status": "HOLDOUT (AUC01 C-300s, frozen fit)",
        "prereg_sha256": sha,
        "frozen_fit": None if fitted is None else asdict(fitted),
        "holdout_decisions": int(len(hframe)),
        "trades": trades.to_dict(orient="records") if len(trades) else [],
        **holdout_verdict(fitted is not None, net),
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mids")
    m.add_argument("--stream", required=True)
    m.add_argument("--session", required=True, help="dataset dir with dataset.json + normalized/")
    m.add_argument("--out", required=True)
    for name in ("insample", "holdout"):
        p = sub.add_parser(name)
        p.add_argument("--prereg", default=str(ROOT / "research" / "auction_real" / "prereg.json"))
        p.add_argument("--streams", nargs="+", required=True)
        p.add_argument("--mids", nargs="+", required=True)
        p.add_argument("--out", required=True)
    h = sub.choices["holdout"]
    h.add_argument("--insample-summary", required=True)
    h.add_argument("--holdout-streams", nargs="+", required=True)
    h.add_argument("--holdout-mids", nargs="+", required=True)
    a = ap.parse_args(list(argv) if argv is not None else None)
    t0 = time.time()
    if a.cmd == "mids":
        df = mids_for_session(Path(a.stream), Path(a.session))
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(a.out, index=False)
        ok = int(np.isfinite(df["mid"]).sum())
        log(f"{a.out}: {len(df)} rows ({ok} with a two-sided book) in {time.time() - t0:.0f}s")
        return 0
    doc = run_insample(a) if a.cmd == "insample" else run_holdout(a)
    text = json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n"
    Path(a.out).write_text(text, encoding="utf-8")
    log(f"{a.cmd} -> {a.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
