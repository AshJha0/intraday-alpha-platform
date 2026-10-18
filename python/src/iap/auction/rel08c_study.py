"""REL-08c study: closing-auction imbalance as a 15:30-15:55 close signal.

Input: one or more :mod:`iap.auction.rel08c_extract` directories (one per
session).  Per (session, symbol) and registered decision time ``t``:

* **features at t** - the last closing (``C``) NOII snapshot with
  ``tod_ns <= t`` (level, change, persistence, distance to close,
  near-vs-reference); NaN when no snapshot yet (Nasdaq publishes the closing
  NOII from 15:50, so earlier decision times have no imbalance and the
  conditioned policy stays flat there);
* **book at t** - the 1 s grid BBO at ``t`` (the book after every message
  with ``ts <= t``).

Registered policies (one trade at most per name per session per decision
time, ``qty`` shares, taker entry at the touch):

* ``baseline`` - close behaviour without the imbalance: direction of the
  continuous-session mid return from ``grid_start`` (15:30) to ``t``
  (continuation into the close);
* ``imbalance`` - direction ``orientation * sign(level)`` when
  ``|level| >= threshold`` and ``persistence >= min_persistence``; flat
  otherwise.

Three measures are kept apart:

1. **prediction** (gross, no costs): the signed mid move from ``t`` to the
   taker exit and from ``t`` to the cross price; hit rate;
2. **fill probability**: the taker entry fill fraction ``min(1, touch size /
   qty)`` at ``t`` and at the exit; and, for the cross-exit alternative, the
   probability a limit-on-close at the exit-time mid would have crossed (the
   cross price at or through the limit);
3. **execution** (net bps per trade): taker entry at the touch, taker exit at
   the touch ``exit_lead_s`` before the 15:55 MOC cutoff, each leg paying the
   half-spread and ``fee_per_share``; the auction exit (MOC, entered before
   the cutoff) reported separately as cross-vs-entry net of the entry leg and
   ``cross_fee_per_share``.

Inference treats one session as one cluster: the per-session mean of the
incremental net bps (imbalance minus baseline over the same decisions) is
the unit.  With ``S`` sessions the minimum detectable effect is
``(t_{1-alpha/2, S-1} + t_{power, S-1}) * sd_session / sqrt(S)``
(infinite for ``S < 2``); the interval is Student-t with ``S - 1`` df.  Verdicts:

* ``underpowered`` - ``S < min_sessions`` or trades ``< min_trades`` or
  ``MDE > target_bps``: the data cannot answer; ONE day is always this
  (a screen only);
* ``continue`` - powered, mean incremental net ``>= target_bps`` and the
  lower confidence bound ``> 0``;
* ``reject`` - powered otherwise.

``run_study`` refuses to run on real extracts unless ``prereg_id`` (the
repo-relative ``research/preregs/rel08c_prereg.json``) carries a signature
that verifies under the trust root; synthetic inputs pass
``prereg_id=None, synthetic=True``.  The signed run is
``research/auction/rel08c/run_rel08c.py``.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from iap.auction.rel08c_extract import (
    CROSS_FILE,
    DEFAULT_GRID_START_NS,
    GRID_FILE,
    META_FILE,
    NOII_FILE,
    NS_S,
)
from iap.research import claims

STUDY_VERSION = 1
POLICIES = ("baseline", "imbalance")
VERDICTS = ("underpowered", "continue", "reject")
#: Nasdaq closing MOC entry cutoff (15:55 ET since 2018-10-01).
MOC_CUTOFF_TOD_NS = (15 * 3600 + 55 * 60) * NS_S
#: grid prices are in 1/10000 dollars; per-share dollars -> price units.
PRICE_UNIT = 10_000


def _hms(h: int, m: int, s: int = 0) -> int:
    return (h * 3600 + m * 60 + s) * NS_S


@dataclass(frozen=True)
class Rel08cConfig:
    """Open parameters (set and signed in the pre-declaration)."""

    decision_tod_ns: tuple[int, ...] = (_hms(15, 51), _hms(15, 53))
    threshold: float = 0.2
    min_persistence: int = 2
    orientation: int = 1
    qty: int = 100
    exit_lead_s: float = 5.0
    fee_per_share: float = 0.003
    cross_fee_per_share: float = 0.0010
    target_bps: float = 2.0
    alpha: float = 0.05
    power: float = 0.8
    min_sessions: int = 5
    min_trades: int = 30
    grid_start_tod_ns: int = DEFAULT_GRID_START_NS

    def __post_init__(self) -> None:
        if self.orientation not in (-1, 1):
            raise ValueError("orientation must be +1 or -1")
        exit_ns = MOC_CUTOFF_TOD_NS - int(self.exit_lead_s * NS_S)
        for t in self.decision_tod_ns:
            if not self.grid_start_tod_ns < t < exit_ns:
                raise ValueError("every decision time must lie in (15:30, taker exit)")

    @property
    def exit_tod_ns(self) -> int:
        return MOC_CUTOFF_TOD_NS - int(self.exit_lead_s * NS_S)


@dataclass
class Rel08cResult:
    config: dict
    trades: pd.DataFrame
    sessions: pd.DataFrame
    summary: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "version": STUDY_VERSION,
            "config": self.config,
            "summary": self.summary,
            "sessions": self.sessions.to_dict(orient="records"),
        }


def load_extract(d: str | Path) -> tuple[str, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    d = Path(d)
    meta = json.loads((d / META_FILE).read_text(encoding="utf-8"))
    text = {"symbol": str, "cross_type": str, "imbalance_direction": str, "price_variation": str}
    noii = pd.read_csv(
        d / NOII_FILE,
        dtype=text,
        keep_default_na=False,
        na_values={"change": [""], "level": [""], "distance_to_close_s": [""]},
    )
    crosses = pd.read_csv(
        d / CROSS_FILE, dtype={"symbol": str, "cross_type": str}, keep_default_na=False
    )
    grid = pd.read_csv(d / GRID_FILE, dtype={"symbol": str})
    return str(meta.get("date") or d.name), noii, crosses, grid


def features_at(noii: pd.DataFrame, symbol: str, t: int) -> dict:
    """Closing NOII features of ``symbol`` from snapshots with tod_ns <= t."""
    s = noii[(noii["symbol"] == symbol) & (noii["cross_type"] == "C") & (noii["tod_ns"] <= t)]
    if s.empty:
        return {
            "level": math.nan,
            "change": math.nan,
            "persistence": 0,
            "distance_to_close_s": math.nan,
            "near_vs_ref_bps": math.nan,
            "noii_tod_ns": None,
        }
    r = s.sort_values("tod_ns", kind="mergesort").iloc[-1]
    ref, near = float(r["ref_px4"]), float(r["near_px4"])
    nvr = 1e4 * (near - ref) / ref if ref > 0 and near > 0 else math.nan
    return {
        "level": float(r["level"]),
        "change": float(r["change"]),
        "persistence": int(r["persistence"]),
        "distance_to_close_s": float(r["distance_to_close_s"]),
        "near_vs_ref_bps": nvr,
        "noii_tod_ns": int(r["tod_ns"]),
    }


def _quote(grid: pd.DataFrame, t: int) -> dict | None:
    r = grid[grid["tod_ns"] == t]
    if r.empty:
        return None
    r = r.iloc[0]
    if pd.isna(r["bid_px4"]) or pd.isna(r["ask_px4"]) or r["ask_px4"] <= r["bid_px4"]:
        return None
    bid, ask = float(r["bid_px4"]), float(r["ask_px4"])
    return {
        "bid": bid,
        "ask": ask,
        "mid": 0.5 * (bid + ask),
        "bid_size": float(r["bid_size"]),
        "ask_size": float(r["ask_size"]),
    }


def _direction(policy: str, cfg: Rel08cConfig, feats: dict, q0: dict, q_t: dict) -> int:
    if policy == "baseline":
        move = q_t["mid"] - q0["mid"]
        return int(np.sign(move))
    lvl = feats["level"]
    if not math.isfinite(lvl) or abs(lvl) < cfg.threshold:
        return 0
    if feats["persistence"] < cfg.min_persistence:
        return 0
    return cfg.orientation * int(np.sign(lvl))


def decision_trades(
    date: str, noii: pd.DataFrame, crosses: pd.DataFrame, grid: pd.DataFrame, cfg: Rel08cConfig
) -> pd.DataFrame:
    """One row per (symbol, decision time, policy) with a non-zero direction."""
    rows: list[dict] = []
    close = crosses[crosses["cross_type"] == "C"]
    for sym, g in grid.groupby("symbol", sort=True):
        q0 = _quote(g, cfg.grid_start_tod_ns)
        qx = _quote(g, cfg.exit_tod_ns)
        cx = close[close["symbol"] == sym]
        cross_px = float(cx["price_px4"].iloc[-1]) if not cx.empty else math.nan
        if q0 is None or qx is None:
            continue
        for t in cfg.decision_tod_ns:
            qt = _quote(g, t)
            if qt is None:
                continue
            feats = features_at(noii, sym, t)
            for pol in POLICIES:
                d = _direction(pol, cfg, feats, q0, qt)
                if d == 0:
                    continue
                mid = qt["mid"]
                entry = qt["ask"] if d > 0 else qt["bid"]
                exit_ = qx["bid"] if d > 0 else qx["ask"]
                fee_bps = 1e4 * cfg.fee_per_share * PRICE_UNIT / mid
                gross_taker = 1e4 * d * (qx["mid"] - mid) / mid
                net_taker = 1e4 * d * (exit_ - entry) / mid - 2 * fee_bps
                gross_cross = 1e4 * d * (cross_px - mid) / mid
                net_cross = (
                    1e4 * d * (cross_px - entry) / mid
                    - fee_bps
                    - 1e4 * cfg.cross_fee_per_share * PRICE_UNIT / mid
                )
                touch_in = qt["ask_size"] if d > 0 else qt["bid_size"]
                touch_out = qx["bid_size"] if d > 0 else qx["ask_size"]
                # exit leg is a sell for d > 0: a limit-on-close at the exit mid fills
                # when the cross prints at or through it
                loc_fill = (cross_px >= qx["mid"]) if d > 0 else (cross_px <= qx["mid"])
                rows.append(
                    {
                        "date": date,
                        "symbol": sym,
                        "decision_tod_ns": t,
                        "policy": pol,
                        "direction": d,
                        **{f"f_{k}": v for k, v in feats.items()},
                        "gross_taker_bps": gross_taker,
                        "gross_cross_bps": gross_cross,
                        "hit": float(gross_taker > 0),
                        "fill_entry": min(1.0, touch_in / cfg.qty),
                        "fill_exit": min(1.0, touch_out / cfg.qty),
                        "loc_fill": float(loc_fill) if math.isfinite(cross_px) else math.nan,
                        "net_taker_bps": net_taker,
                        "net_cross_bps": net_cross,
                    }
                )
    return pd.DataFrame(rows)


def session_table(trades: pd.DataFrame, dates: list[str]) -> pd.DataFrame:
    """Per-session cluster means; incremental = imbalance minus baseline net
    taker bps (each policy's mean over its own trades; 0 for a flat policy)."""
    out = []
    for d in dates:
        t = trades[trades["date"] == d] if not trades.empty else trades
        row: dict = {"date": d}
        for pol in POLICIES:
            p = t[t["policy"] == pol] if not t.empty else t
            row[f"n_{pol}"] = int(len(p))
            row[f"net_{pol}_bps"] = float(p["net_taker_bps"].mean()) if len(p) else 0.0
            row[f"gross_{pol}_bps"] = float(p["gross_taker_bps"].mean()) if len(p) else 0.0
        row["incremental_bps"] = row["net_imbalance_bps"] - row["net_baseline_bps"]
        out.append(row)
    return pd.DataFrame(out)


def mde_bps(sd_session: float, n_sessions: int, alpha: float, power: float) -> float:
    if n_sessions < 2 or not math.isfinite(sd_session):
        return math.inf
    df = n_sessions - 1
    q = float(stats.t.ppf(1 - alpha / 2, df) + stats.t.ppf(power, df))
    return q * sd_session / math.sqrt(n_sessions)


def decide(
    sessions: pd.DataFrame, n_trades: int, cfg: Rel08cConfig, *, registered: bool = False
) -> dict:
    s = len(sessions)
    x = sessions["incremental_bps"].to_numpy(float) if s else np.array([])
    mean = float(x.mean()) if s else math.nan
    sd = float(x.std(ddof=1)) if s >= 2 else math.inf
    mde = mde_bps(sd, s, cfg.alpha, cfg.power)
    if s >= 2:
        half = float(stats.t.ppf(1 - cfg.alpha / 2, s - 1)) * sd / math.sqrt(s)
        lo, hi = mean - half, mean + half
    else:
        lo, hi = -math.inf, math.inf
    reasons = []
    if s < cfg.min_sessions:
        reasons.append(
            f"{s} session(s) < min_sessions {cfg.min_sessions} (one session = "
            "one cluster; one day supports a screen only)"
        )
    if n_trades < cfg.min_trades:
        reasons.append(f"{n_trades} imbalance trades < min_trades {cfg.min_trades}")
    if mde > cfg.target_bps:
        reasons.append(f"MDE {mde:.3g} bps > target {cfg.target_bps} bps")
    if reasons:
        verdict = "underpowered"
    elif mean >= cfg.target_bps and lo > 0:
        verdict = "continue"
    else:
        verdict = "reject"
        reasons.append(f"mean incremental {mean:.3g} bps, CI low {lo:.3g}")
    return {
        "verdict": verdict,
        # review finding 7: the screen's evidence status (never above
        # passes_registered_net_screen; one session caps it at exploratory_signal)
        "evidence_status": claims.from_screen_verdict(
            verdict, sessions=s, registered=registered
        ).value,
        "reasons": reasons,
        "n_sessions": s,
        "n_imbalance_trades": n_trades,
        "mean_incremental_bps": mean,
        "sd_session_bps": sd,
        "ci_low_bps": lo,
        "ci_high_bps": hi,
        "mde_bps": mde,
    }


def prereg_signed(prereg_id: str, root: Path | None = None) -> bool:
    """The repo-relative prereg JSON verifies under the trust root AND its bound
    analysis identity matches the code, configs, lock and interpreter on disk
    (review finding 1: calling :func:`run_study` directly must not bypass the
    identity check that the runner performs)."""
    import json

    from iap.agents.analysis_identity import IdentityError, environment_mismatches
    from iap.research.holdout_registry import REPO, prereg_is_signed

    if not prereg_is_signed(prereg_id, root):
        return False
    r = Path(root or REPO)
    bound = json.loads((r / prereg_id).read_text(encoding="utf-8")).get("analysis_identity")
    if not bound:
        return False
    try:
        return not environment_mismatches(r, bound)
    except IdentityError:
        return False


def run_study(
    extract_dirs: list[str | Path],
    cfg: Rel08cConfig,
    *,
    prereg_id: str | None,
    synthetic: bool = False,
    root: Path | None = None,
) -> Rel08cResult:
    if not synthetic and (prereg_id is None or not prereg_signed(prereg_id, root)):
        raise PermissionError(
            "REL-08c: refusing to run on real extracts without a SIGNED prereg whose identity matches"
        )
    dates, frames = [], []
    for d in extract_dirs:
        date, noii, crosses, grid = load_extract(d)
        dates.append(date)
        frames.append(decision_trades(date, noii, crosses, grid, cfg))
    trades = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    sessions = session_table(trades, dates)
    imb = trades[trades["policy"] == "imbalance"] if not trades.empty else trades
    summary = decide(sessions, int(len(imb)), cfg, registered=not synthetic)

    def _m(p: pd.DataFrame, c: str) -> float:
        return float(p[c].mean()) if len(p) and c in p else math.nan

    summary["measures"] = {
        pol: {
            "n": int(len(p)),
            "prediction": {
                "gross_taker_bps": _m(p, "gross_taker_bps"),
                "gross_cross_bps": _m(p, "gross_cross_bps"),
                "hit_rate": _m(p, "hit"),
            },
            "fill": {
                "entry": _m(p, "fill_entry"),
                "exit": _m(p, "fill_exit"),
                "loc_at_exit_mid": _m(p, "loc_fill"),
            },
            "execution": {
                "net_taker_bps": _m(p, "net_taker_bps"),
                "net_cross_bps": _m(p, "net_cross_bps"),
            },
        }
        for pol in POLICIES
        for p in [trades[trades["policy"] == pol] if not trades.empty else trades]
    }
    return Rel08cResult(asdict(cfg), trades, sessions, summary)
