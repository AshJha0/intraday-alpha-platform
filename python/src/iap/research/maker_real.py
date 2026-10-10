"""In-sample maker study on the real ITCH sessions (v1.9 M1-M4 on real data).

Pre-registered in ``research/maker_real/prereg.json`` (blackboard entries
``<alpha>/<horizon>:maker-insample``); this driver reads its parameters from
that file and refuses to run if the blackboard does not carry them.

Per unit (session, symbol), in session order and one unit at a time:

1. load that symbol's events of the session (``normalized/events.parquet``);
2. calibrate (:func:`estimate_calibration`, the ``python -m
   iap.execution.calibration`` code path) and inject the ASSUMED parametric
   latency table (ITCH has no receive stamp);
3. build M3 maker labels on a 1 s decision grid (training rows for the
   MakerFilter of LATER sessions);
4. from session 2 on, per alpha: fit the alpha on earlier sessions, score
   this session with ``score_uncapped`` (+ ``z``), take the gate's adverse
   selection from the PREVIOUS session's calibration, fit the MakerFilter on
   earlier sessions' labels, and run ``MakerBacktester`` for each exit and
   variant (gated / gated_filter / ungated);
5. write ``units/<session>_<symbol>.json`` atomically (checkpoint: an
   existing unit file is skipped on resume).

``summarise`` pools the unit files into session-clustered statistics and
the pre-registered verdict. Outputs stay under ``--out`` (gitignored data).
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import hashlib
import json
import math
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[4]
SESSIONS = (
    "ds3_20190130",
    "ds3_20190327",
    "ds3_20190730",
    "ds3_20190830",
    "ds3_20191030",
    "ds_20191230",
    "ds3_20200130",
)
SYMBOLS = {"AAPL": 1, "MSFT": 2, "QQQ": 3}
VARIANTS = ("gated", "gated_filter", "ungated")
FILTER_FEATURES = (
    "spread_ticks_v1",
    "imbalance_l1_v1",
    "queue_depletion_rate_bid_w1s_v1",
    "queue_depletion_rate_ask_w1s_v1",
    "queue_replenish_rate_bid_w1s_v1",
    "queue_replenish_rate_ask_w1s_v1",
    "micro_mid_dev_bps_v1",
)
STATE_COLS = ("is_trading_v1", "is_auction_v1", "is_halt_v1")
HORIZON_NS = {"1s": 1_000_000_000, "5s": 5_000_000_000, "10s": 10_000_000_000}
SEC = 1_000_000_000


# ------------------------------------------------------------------ helpers


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def peak_rss_gb() -> float | None:
    """Peak working set of this process (Windows), else ru_maxrss."""
    try:
        if sys.platform == "win32":

            class PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            fn = ctypes.windll.psapi.GetProcessMemoryInfo
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
            fn(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
            return pmc.PeakWorkingSetSize / 2**30
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20
    except Exception:  # pragma: no cover - diagnostics only
        return None


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_prereg(repo_root: Path, prereg: Mapping[str, Any]) -> list[dict]:
    """The blackboard entries of every pre-registered cell (raises if missing)."""
    from iap.agents.prereg_gate import require

    return [
        require(repo_root, c["alpha"], f"{c['horizon']}:maker-insample") for c in prereg["cells"]
    ]


def unit_name(session: str, symbol: str) -> str:
    return f"{session}_{symbol}"


def alpha_side_target(z: np.ndarray, bid: np.ndarray, ask: np.ndarray) -> np.ndarray:
    """The M3 label of the side the alpha points to (z > 0 buys at the bid,
    z < 0 sells at the ask); NaN when z is 0 or not finite."""
    z = np.asarray(z, dtype=float)
    out = np.full(z.shape, np.nan)
    pos, neg = z > 0, z < 0
    out[pos] = np.asarray(bid, dtype=float)[pos]
    out[neg] = np.asarray(ask, dtype=float)[neg]
    return out


def cluster_stats(day_means: Sequence[float], n_tests: int = 1) -> dict[str, Any]:
    """Session-clustered mean, se, 95 % CI and the Bonferroni CI over ``n_tests``."""
    from scipy import stats

    x = np.asarray([v for v in day_means if v is not None and math.isfinite(v)], dtype=float)
    n = int(x.size)
    out: dict[str, Any] = {"n_sessions": n, "mean": float(x.mean()) if n else None}
    if n < 2:
        out.update(se=None, t=None, ci95=None, ci_bonf=None)
        return out
    se = float(x.std(ddof=1) / math.sqrt(n))
    out["se"] = se
    out["t"] = float(out["mean"] / se) if se > 0 else None
    for key, a in (("ci95", 0.05), ("ci_bonf", 0.05 / n_tests)):
        q = float(stats.t.ppf(1 - a / 2, n - 1))
        out[key] = [out["mean"] - q * se, out["mean"] + q * se]
    return out


def verdict(primary: Mapping[str, Any], min_sessions: int) -> str:
    """The pre-registered rule on the gated variant's clustered stats."""
    ci = primary.get("ci_bonf")
    if (
        primary.get("n_sessions", 0) >= min_sessions
        and primary.get("mean") is not None
        and primary["mean"] > 0
        and ci is not None
        and ci[0] > 0
    ):
        return "EXISTS (in-sample; worth an out-of-sample test)"
    return "NO DEMONSTRATED MAKER EDGE"


# ------------------------------------------------------------------ loading


def load_events(session_dir: Path, iid: int, end_ns: int | None = None) -> list:
    import pyarrow.parquet as pq

    from iap.core.events import MarketEvent

    cols = [
        "event_id",
        "instrument_id",
        "venue_id",
        "exchange_ts",
        "receive_ts",
        "sequence",
        "event_type",
        "side",
        "price_ticks",
        "qty",
        "order_id",
        "trade_id",
    ]
    filt = [("instrument_id", "=", iid)]
    if end_ns is not None:
        filt.append(("exchange_ts", "<=", end_ns))
    t = pq.read_table(session_dir / "normalized" / "events.parquet", columns=cols, filters=filt)
    arrays = [t.column(c).to_numpy().tolist() for c in cols]
    del t
    return [MarketEvent(*row) for row in zip(*arrays, strict=True)]


def load_feature_day(
    features_dir: Path, iid: int, day: int, columns: Sequence[str]
) -> pd.DataFrame:
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(features_dir / f"features_{iid}.parquet")
    names = set(pf.schema_arrow.names)
    cols = ["instrument_id", "exchange_ts", *[c for c in dict.fromkeys(columns) if c in names]]
    df = pf.read_row_group(day, columns=cols).to_pandas()
    ok = (df["is_trading_v1"] == 1) & (df["is_auction_v1"] != 1) & (df["is_halt_v1"] != 1)
    return df[ok].sort_values("exchange_ts", kind="stable").reset_index(drop=True)


# ------------------------------------------------------------------ driver


class Study:
    def __init__(self, args: argparse.Namespace) -> None:
        from iap.alpha import configure_universe
        from iap.execution.config import load_exec_config

        self.args = args
        self.sessions = tuple(getattr(args, "session_list", None) or SESSIONS)
        self.symbols = dict(getattr(args, "symbol_map", None) or SYMBOLS)
        self.data = Path(args.data_root)
        self.features_dir = self.data / "multi7" / "features"
        self.out = Path(args.out)
        for sub in ("calib", "labels", "units"):
            (self.out / sub).mkdir(parents=True, exist_ok=True)
        self.prereg_path = Path(args.prereg)
        self.prereg = json.loads(self.prereg_path.read_text(encoding="utf-8"))
        self.prereg_sha = file_sha256(self.prereg_path)
        configure_universe(self.data / "multi7" / "configs" / "instruments" / "instruments.json")
        self.exec_config = load_exec_config(self.data / "multi7" / "configs")
        self._alpha_cache: dict[tuple[str, int], Any] = {}

    # -- per-unit pieces -------------------------------------------------

    def _window(self, day: int) -> tuple[int | None, int | None]:
        """(score start, event/score end) ns of the smoke slice, else (None, None)."""
        if not self.args.slice:
            return None, None
        a, b = self.args.slice.split("-")
        date = self.sessions[day].split("_")[1]
        tz = "America/New_York"
        start = pd.Timestamp(f"{date} {a}", tz=tz).value
        end = pd.Timestamp(f"{date} {b}", tz=tz).value
        return start, end

    def calibration(self, day: int, symbol: str, events: list) -> dict:
        from iap.execution.calibration import (
            estimate_calibration,
            parametric_latency,
            write_calibration,
        )

        path = self.out / "calib" / f"{unit_name(self.sessions[day], symbol)}.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        iid = self.symbols[symbol]
        tick = self.exec_config.instrument(iid).tick_size
        lat = self.prereg["latency_ASSUMED"]
        doc = estimate_calibration(
            events,
            {iid: tick},
            source={"dataset": self.sessions[day], "symbol": symbol, "slice": self.args.slice},
        )
        doc["latency"] = {
            str(lat["venue_id"]): parametric_latency(
                lat["mean_ns"], lat["jitter_ns"], lat["tail_prob"], lat["tail_mult"]
            )
        }
        doc["latency_status"] = "ASSUMED (parametric; ITCH has no receive stamp)"
        write_calibration(doc, path)
        return doc

    def prev_calibration(self, day: int, symbol: str):
        from iap.execution.calibration import load_calibration

        path = self.out / "calib" / f"{unit_name(self.sessions[day - 1], symbol)}.json"
        return load_calibration(path) if path.is_file() else None

    def labels(self, day: int, symbol: str, events: list, cal_doc: dict, feats: pd.DataFrame):
        from iap.execution.calibration import ExecCalibration
        from iap.labels.maker_labels import maker_labels

        path = self.out / "labels" / f"{unit_name(self.sessions[day], symbol)}.parquet"
        if path.is_file():
            return
        if feats.empty:
            pd.DataFrame().to_parquet(path)
            return
        sec = feats["exchange_ts"].to_numpy() // SEC
        grid = feats.loc[np.r_[True, sec[1:] != sec[:-1]]].reset_index(drop=True)
        lab = maker_labels(
            events,
            grid["exchange_ts"].to_numpy(dtype=np.int64),
            instrument_id=self.symbols[symbol],
            exec_config=self.exec_config,
            ttl_ns=SEC,
            qty=100,
            calibration=ExecCalibration.from_dict(cal_doc),
        )
        keep = ["bid_not_run_over", "ask_not_run_over", "bid_filled", "ask_filled"]
        out = pd.concat([grid.reset_index(drop=True), lab[keep].reset_index(drop=True)], axis=1)
        out.to_parquet(path)

    def alpha(self, alpha_id: str, horizon: str, day: int):
        """The alpha fitted on sessions strictly before ``day`` (all symbols)."""
        from iap.alpha import build

        key = (alpha_id, day)
        if key in self._alpha_cache:
            return self._alpha_cache[key]
        model = build(alpha_id)
        model.horizon = horizon
        need = [*model.features, f"label_mid_{horizon}", f"label_valid_{horizon}", *STATE_COLS]
        train: dict[int, pd.DataFrame] = {}
        for iid in model.universe(sorted(self.symbols.values())):
            frames = [load_feature_day(self.features_dir, iid, d, need) for d in range(day)]
            train[iid] = pd.concat(frames, ignore_index=True)
        model.fit(train)
        self._alpha_cache[key] = model
        return model

    @staticmethod
    def z_of(model, df: pd.DataFrame, iid: int) -> np.ndarray:
        sig = model.signals({iid: df})[iid].to_numpy(dtype=float)
        if model.is_dead:
            return np.zeros(len(sig))
        z = (sig - model.mu) / (model.sigma + 1e-12)
        return np.where(np.isfinite(z), z, 0.0)

    def maker_filter(self, model, day: int):
        from iap.backtest.maker import MakerFilter

        xs, ys = [], []
        for d in range(day):
            for sym, iid in self.symbols.items():
                if iid not in model.universe([iid]):
                    continue
                p = self.out / "labels" / f"{unit_name(self.sessions[d], sym)}.parquet"
                if not p.is_file():
                    continue
                lab = pd.read_parquet(p)
                if lab.empty:
                    continue
                z = self.z_of(model, lab, iid)
                y = alpha_side_target(z, lab["bid_not_run_over"], lab["ask_not_run_over"])
                xs.append(self.filter_X(lab, z))
                ys.append(y)
        if not xs:
            return None, "no earlier labels"
        X, y = np.vstack(xs), np.concatenate(ys)
        try:
            return MakerFilter("meta_gbm", 0.5).fit(
                X, y
            ), f"fit on {int(np.isfinite(y).sum())} rows"
        except ValueError as exc:
            return None, str(exc)

    @staticmethod
    def filter_X(df: pd.DataFrame, z: np.ndarray) -> np.ndarray:
        cols = [np.asarray(z, dtype=float)]
        for c in FILTER_FEATURES:
            cols.append(df[c].to_numpy(dtype=float) if c in df else np.full(len(df), np.nan))
        return np.column_stack(cols)

    def unit_features(self, day: int, symbol: str) -> pd.DataFrame:
        from iap.alpha import build

        need = list(FILTER_FEATURES) + list(STATE_COLS)
        for c in self.prereg["cells"]:
            need += list(build(c["alpha"]).features)
        feats = load_feature_day(self.features_dir, self.symbols[symbol], day, need)
        _, end = self._window(day)
        if end is not None:
            feats = feats[feats["exchange_ts"] <= end].reset_index(drop=True)
        return feats

    def unit_events(self, day: int, symbol: str) -> list:
        _, end = self._window(day)
        return load_events(self.data / self.sessions[day], self.symbols[symbol], end)

    def unit_cells(self, symbol: str) -> list[dict]:
        """The pre-registered cells whose alpha trades ``symbol``."""
        from iap.alpha import build

        iid = self.symbols[symbol]
        return [c for c in self.prereg["cells"] if iid in build(c["alpha"]).universe([iid])]

    def alpha_path(self, day: int, symbol: str, alpha_id: str) -> Path:
        name = f"{unit_name(self.sessions[day], symbol)}_{alpha_id}.json"
        return self.out / "alpha_units" / name

    def run_alpha(
        self, day: int, symbol: str, cell: Mapping[str, Any], events: list, feats: pd.DataFrame
    ) -> dict[str, Any]:
        """Every exit and variant of one alpha on one unit (deterministic)."""
        from iap.backtest.maker import MakerBacktester, MakerConfig

        iid = self.symbols[symbol]
        cal = self.prev_calibration(day, symbol)
        start, _ = self._window(day)
        aid, hz = cell["alpha"], cell["horizon"]
        model = self.alpha(aid, hz, day)
        t0 = time.time()
        sc = model.score_uncapped({iid: feats}, z_cap=None)[iid]
        sc["z"] = self.z_of(model, feats, iid)
        mask = np.ones(len(sc), dtype=bool)
        if start is not None:
            mask = sc["exchange_ts"].to_numpy() >= start
        sc = sc[mask].reset_index(drop=True)
        fx = feats[mask].reset_index(drop=True)
        filt, filt_note = self.maker_filter(model, day)
        allow_f = filt.allow(self.filter_X(fx, sc["z"].to_numpy())) if filt else None
        h_ns = HORIZON_NS[hz]
        cell_out: dict[str, Any] = {
            "alpha_fit": {
                "mu": model.mu,
                "sigma": model.sigma,
                "beta": model.beta,
                "n_train": model.n_train,
                "dead": model.is_dead,
            },
            "n_scores": len(sc),
            "filter": filt_note,
            "filter_allow_share": None if allow_f is None else float(allow_f.mean()),
        }
        for exit_mode in self.prereg["exit_modes"]:
            base: dict[str, Any] = dict(
                qty=100, ttl_ns=h_ns, horizon_ns=h_ns, as_horizon=cell["as_horizon"]
            )
            if exit_mode == "passive":
                base.update(exit="passive", exit_timeout_ns=h_ns, exit_reprices=1)
            for variant in VARIANTS:
                if variant == "gated_filter" and allow_f is None:
                    cell_out[f"{exit_mode}/{variant}"] = {"skipped": filt_note}
                    continue
                mc = MakerConfig(**base, margin_bps=-1e9 if variant == "ungated" else 0.0)
                res = MakerBacktester(self.exec_config, mc, cal).run_instrument(
                    events,
                    sc,
                    iid,
                    allow=allow_f if variant == "gated_filter" else None,
                )
                s = res.summary()
                t = res.trips
                s["sum_net_bps"] = float(t["net_bps"].sum()) if len(t) else 0.0
                s["sumsq_net_bps"] = float((t["net_bps"] ** 2).sum()) if len(t) else 0.0
                cell_out[f"{exit_mode}/{variant}"] = s
        cell_out["seconds"] = round(time.time() - t0, 1)
        return cell_out

    def write_alpha(self, day: int, symbol: str, alpha_id: str, cell_out: Mapping) -> Path:
        from iap.experiment.locking import atomic_write_text

        path = self.alpha_path(day, symbol, alpha_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path, json.dumps(cell_out, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path

    def plan_workers(self, n_alphas: int, n_events: int) -> tuple[int, float, float | None]:
        """(workers, per-worker GB estimate, available GB) for one unit."""
        per = max(float(self.args.worker_mem_gb), 0.6 + n_events * 7e-7)
        avail = available_ram_gb()
        w = min(int(self.args.workers or 4), n_alphas)
        if avail is not None:
            w = min(w, max(1, int((avail - 1.0) // per)))
        return max(1, w), per, avail

    # -- loop ------------------------------------------------------------

    def run_unit(self, day: int, symbol: str) -> None:
        path = self.out / "units" / f"{unit_name(self.sessions[day], symbol)}.json"
        if path.is_file():
            log(f"skip {path.name} (done)")
            return
        t0 = time.time()
        iid = self.symbols[symbol]
        events = self.unit_events(day, symbol)
        n_events = len(events)
        t_load = time.time() - t0
        log(f"unit {self.sessions[day]} {symbol}: {n_events} events loaded in {t_load:.0f}s")
        cal_doc = self.calibration(day, symbol, events)
        feats = self.unit_features(day, symbol)
        t_cal = time.time() - t0
        self.labels(day, symbol, events, cal_doc, feats)
        t_lab = time.time() - t0
        results: dict[str, Any] = {}
        workers = None
        if day > 0:
            cells = self.unit_cells(symbol)
            todo = [c for c in cells if not self.alpha_path(day, symbol, c["alpha"]).is_file()]
            for c in cells:
                if c not in todo:
                    log(f"  {self.sessions[day]} {symbol} {c['alpha']}: checkpoint found")
            if todo:
                workers, per, avail = self.plan_workers(len(todo), n_events)
                shown = None if avail is None else round(avail, 2)
                log(
                    f"  {len(todo)} alphas to run, workers={workers} "
                    f"(per-worker est {per:.2f} GB, available {shown} GB)"
                )
                if workers <= 1:
                    for c in todo:
                        out = self.run_alpha(day, symbol, c, events, feats)
                        out["peak_rss_gb"] = peak_rss_gb()
                        self.write_alpha(day, symbol, c["alpha"], out)
                        log(f"  {self.sessions[day]} {symbol} {c['alpha']}: {out['seconds']}s")
                else:
                    events = []  # free the parent's copy; each worker loads its own
                    gc.collect()
                    self._pool(day, symbol, todo, workers)
            for c in cells:
                results[c["alpha"]] = json.loads(
                    self.alpha_path(day, symbol, c["alpha"]).read_text(encoding="utf-8")
                )
        t_all = time.time() - t0
        fr = cal_doc.get("fill_rates", {}).get("all", {})
        doc = {
            "session": self.sessions[day],
            "day_index": day,
            "symbol": symbol,
            "instrument_id": iid,
            "warmup_only": day == 0,
            "slice": self.args.slice,
            "n_events": n_events,
            "prereg_sha256": self.prereg_sha,
            "calibration_same_day": {
                "p_any_fill": fr.get("p_any_fill"),
                "markouts_bps": {
                    h: (cal_doc.get("markouts", {}).get("all", {}).get(h) or {}).get("mean_bps")
                    for h in ("100ms", "1s", "10s")
                },
                "latency": "ASSUMED parametric",
            },
            "results": results,
            "workers": workers,
            "timing_s": {
                "load": round(t_load, 1),
                "calibration": round(t_cal - t_load, 1),
                "labels": round(t_lab - t_cal, 1),
                "backtests": round(t_all - t_lab, 1),
                "total": round(t_all, 1),
            },
            "peak_rss_gb": peak_rss_gb(),
        }
        from iap.experiment.locking import atomic_write_text

        atomic_write_text(path, json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        log(f"done {path.name} in {doc['timing_s']['total']}s, peak {doc['peak_rss_gb']:.2f} GB")
        del events, feats
        gc.collect()

    def _pool(self, day: int, symbol: str, todo: list[dict], workers: int) -> None:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor, as_completed

        spec = worker_spec(self.args)
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
            futs = {ex.submit(alpha_worker, spec, day, symbol, c["alpha"]): c for c in todo}
            for f in as_completed(futs):
                out = f.result()
                log(
                    f"  {self.sessions[day]} {symbol} {futs[f]['alpha']}: {out['seconds']}s, "
                    f"worker peak {out.get('peak_rss_gb') or 0:.2f} GB"
                )

    def run(self) -> None:
        names = self.args.sessions or list(self.sessions)
        days = [self.sessions.index(s) for s in names]
        syms = self.args.symbols or list(self.symbols)
        for d in days:
            for s in syms:
                self.run_unit(d, s)
        summary = summarise(self.out / "units", self.prereg)
        (self.out / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        log(f"summary -> {self.out / 'summary.json'}")


# ------------------------------------------------------------------ workers


def worker_spec(args: argparse.Namespace) -> dict[str, Any]:
    """A picklable copy of the CLI namespace for a spawned worker."""
    return dict(vars(args))


def alpha_worker(spec: Mapping[str, Any], day: int, symbol: str, alpha_id: str) -> dict:
    """Process-pool entry point (importable by module name under spawn): one
    alpha of one unit, from the unit's cached calibration and labels; writes
    its (unit, alpha) checkpoint atomically and returns it."""
    study = Study(argparse.Namespace(**spec))
    cell = next(c for c in study.prereg["cells"] if c["alpha"] == alpha_id)
    events = study.unit_events(day, symbol)
    feats = study.unit_features(day, symbol)
    out = study.run_alpha(day, symbol, cell, events, feats)
    out["peak_rss_gb"] = peak_rss_gb()
    study.write_alpha(day, symbol, alpha_id, out)
    return out


def available_ram_gb() -> float | None:
    """Available physical memory (Windows GlobalMemoryStatusEx, else /proc)."""
    try:
        if sys.platform == "win32":

            class MS(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            ms = MS()
            ms.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return ms.ullAvailPhys / 2**30
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 2**20
    except Exception:  # pragma: no cover - diagnostics only
        return None
    return None


# ------------------------------------------------------------------ summary


def summarise(units_dir: Path, prereg: Mapping[str, Any]) -> dict[str, Any]:
    """Session-clustered stats per (alpha, exit, variant) from the unit files."""
    units = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(units_dir.glob("*.json"))]
    n_primary = len(prereg["cells"]) * len(prereg["exit_modes"])
    test_sessions = sorted({u["session"] for u in units if not u["warmup_only"]})
    out: dict[str, Any] = {
        "status": "EXPLORATORY IN-SAMPLE",
        "units": len(units),
        "test_sessions": test_sessions,
        "cells": {},
    }
    for cell in prereg["cells"]:
        aid = cell["alpha"]
        for exit_mode in prereg["exit_modes"]:
            for variant in VARIANTS:
                key = f"{exit_mode}/{variant}"
                per_day: dict[str, dict[str, float]] = {}
                for u in units:
                    r = (u["results"].get(aid) or {}).get(key)
                    if not r or "skipped" in r:
                        continue
                    acc = per_day.setdefault(
                        u["session"], {"n": 0, "sum": 0.0, "net_usd": 0.0, "posted": 0}
                    )
                    acc["n"] += r["n_trips"]
                    acc["sum"] += r["sum_net_bps"]
                    acc["net_usd"] += r["total_net"]
                    acc["posted"] += r["posted"]
                means = {d: (a["sum"] / a["n"] if a["n"] else None) for d, a in per_day.items()}
                st = cluster_stats(list(means.values()), n_primary)
                row = {"per_session": per_day, "session_mean_net_bps": means, **st}
                if variant == "gated":
                    row["verdict"] = verdict(st, min_sessions=4)
                out["cells"][f"{aid}@{cell['horizon']} {key}"] = row
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-root", required=True, help="data/real of the main checkout")
    ap.add_argument("--out", required=True)
    ap.add_argument("--prereg", default=str(ROOT / "research" / "maker_real" / "prereg.json"))
    ap.add_argument("--sessions", nargs="*", choices=SESSIONS)
    ap.add_argument("--symbols", nargs="*", choices=list(SYMBOLS))
    ap.add_argument("--slice", help="smoke slice HH:MM-HH:MM New York time (events to its end)")
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--workers", type=int, default=4, help="max alpha workers per unit")
    ap.add_argument("--worker-mem-gb", type=float, default=2.5, help="per-worker RAM budget")
    args = ap.parse_args(list(argv) if argv is not None else None)
    prereg = json.loads(Path(args.prereg).read_text(encoding="utf-8"))
    entries = check_prereg(ROOT, prereg)
    sha = file_sha256(Path(args.prereg))
    for e in entries:
        if sha not in e["body"]["hypothesis"]:
            raise SystemExit(f"prereg.json sha256 {sha} is not the registered one")
    log(f"prereg verified ({len(entries)} cells, sha256 {sha[:12]})")
    log(f"resume (parallel): workers<={args.workers}, worker-mem-gb {args.worker_mem_gb}")
    if args.summary_only:
        s = summarise(Path(args.out) / "units", prereg)
        print(json.dumps(s, indent=2, sort_keys=True))
        return 0
    Study(args).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
