"""Synthetic daily panel for the research loop and the ML framework (v2.0 stage 7-8 fixtures).

The panel is generated from a seed (SplitMix64 only) and hashed; ``manifest.json``
beside this file records the sha256 of the CSV bytes the generator writes, the
partition calendar and the expected report hash of the development run of each
family that runs on it.  Nothing here is market data: the panel is a fixture
with planted structure so that the pipeline can be exercised end to end and its
output pinned.  Every number it produces is labelled synthetic by the manifest's
route.

Planted structure (per name i, session t; log returns):

    mkt_t          ~ N(0, 0.9 %)                                     the market factor
    resid_{i,t}    = -phi * mean(resid_{i,t-5..t-1}) + e_{i,t}      five-session reversal (D1)
                     + kappa * filing_reaction_{i,t}                 drift after a planted filing (D4)
    ret_{i,t}      = beta_i * mkt_t + resid_{i,t}
    c2o_{i,t}      = gamma * spread_i + u_{i,t};  o2c_{i,t} = ret_{i,t} - c2o_{i,t}   (D3 asymmetry)

No residual momentum is planted: D2 runs on this panel as a negative control.
``phi``, ``kappa`` and ``gamma`` are in the manifest.

Columns: session (ISO date), name, open, close, volume (full-session shares),
auction_volume (close auction shares), open_volume (open auction shares),
sector, filing (1 when a financial filing was accepted on the session),
mkt_ret (the equal-weighted close-to-close return of the universe on the
session), mkt_c2o (the equal-weighted close-to-open return into the session).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.json"
REPO_ROOT = HERE.parents[3]
sys.path.insert(0, str(REPO_ROOT / "python" / "src"))

from iap.core.rng import SplitMix64  # noqa: E402

SECTORS = ("tech", "fin", "health", "energy", "cons", "indus")


def sessions(n: int, start: str = "2015-01-02") -> list[str]:
    """``n`` weekday dates from ``start`` (a synthetic calendar; no holidays)."""
    d = _dt.date.fromisoformat(start)
    out: list[str] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out


def make_panel(
    seed: int = 20261025,
    n_sessions: int = 560,
    n_names: int = 60,
    *,
    phi: float = 0.5,
    kappa: float = 0.004,
    gamma: float = 0.003,
    mkt_sigma: float = 0.009,
    idio_sigma: float = 0.012,
) -> pd.DataFrame:
    """The panel of the module docstring, sorted by (session, name) with a RangeIndex."""
    if n_sessions < 100 or n_names < 12:
        raise ValueError("need n_sessions >= 100 and n_names >= 12")
    rng = SplitMix64(seed)
    names = [f"SYN{i:03d}" for i in range(n_names)]
    beta = {n: 0.6 + 0.8 * rng.uniform() for n in names}
    sector = {n: SECTORS[i % len(SECTORS)] for i, n in enumerate(names)}
    spread_bias = {n: rng.normal() for n in names}  # D3: persistent overnight-intraday clientele
    base_price = {n: 20.0 + 180.0 * rng.uniform() for n in names}
    base_vol = {n: int(2e5 + 3e6 * rng.uniform()) for n in names}
    cal = sessions(n_sessions)
    resid_hist: dict[str, list[float]] = {n: [] for n in names}
    close = {n: base_price[n] for n in names}
    pending_reaction: dict[str, float] = {n: 0.0 for n in names}
    rows: list[dict[str, object]] = []
    for s in cal:
        mkt = mkt_sigma * rng.normal()
        rets: dict[str, float] = {}
        c2os: dict[str, float] = {}
        filings: dict[str, int] = {}
        for n in names:
            hist = resid_hist[n]
            rev = -phi * (sum(hist[-5:]) / 5.0) if len(hist) >= 5 else 0.0
            e = idio_sigma * rng.normal()
            drift = kappa * pending_reaction[n]
            pending_reaction[n] *= 0.85  # the planted post-filing drift decays geometrically
            filing = (
                1 if rng.uniform() < 1.0 / 21.0 else 0
            )  # about one financial filing a month (synthetic)
            resid = rev + e + drift
            if filing:
                shock = 0.02 * rng.normal()
                resid += shock
                pending_reaction[n] = math.copysign(1.0, shock)
            filings[n] = filing
            r = beta[n] * mkt + resid
            rets[n] = r
            c2o = gamma * spread_bias[n] + 0.004 * rng.normal()
            c2os[n] = c2o
            hist.append(resid)
            if len(hist) > 300:
                del hist[0]
        mkt_ret = sum(rets.values()) / len(names)
        mkt_c2o = sum(c2os.values()) / len(names)
        for n in names:
            prev = close[n]
            new_close = prev * math.exp(rets[n])
            open_ = prev * math.exp(c2os[n])
            v = int(base_vol[n] * math.exp(0.3 * rng.normal()))
            rows.append(
                {
                    "session": s,
                    "name": n,
                    "open": round(open_, 4),
                    "close": round(new_close, 4),
                    "volume": v,
                    "auction_volume": int(0.08 * v),
                    "open_volume": int(0.04 * v),
                    "sector": sector[n],
                    "filing": filings[n],
                    "mkt_ret": round(mkt_ret, 10),
                    "mkt_c2o": round(mkt_c2o, 10),
                }
            )
            close[n] = new_close
    frame = pd.DataFrame(rows)
    return frame


def panel_csv_bytes(frame: pd.DataFrame) -> bytes:
    """The canonical CSV bytes (LF, no index, fixed column order) whose sha256 the manifest records."""
    cols = [
        "session",
        "name",
        "open",
        "close",
        "volume",
        "auction_volume",
        "open_volume",
        "sector",
        "filing",
        "mkt_ret",
        "mkt_c2o",
    ]
    return frame[cols].to_csv(index=False, lineterminator="\n").encode("ascii")


def write_panel(path: Path, **kwargs) -> str:
    """Write the fixture CSV to ``path`` and return its sha256."""
    data = panel_csv_bytes(make_panel(**kwargs))
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def write_fixture(out_dir: Path) -> Path:
    """Generate the panel named by the manifest into ``out_dir`` and verify its hash."""
    m = load_manifest()
    g = m["generator"]
    path = out_dir / m["file"]
    sha = write_panel(
        path,
        seed=g["seed"],
        n_sessions=g["n_sessions"],
        n_names=g["n_names"],
        phi=g["phi"],
        kappa=g["kappa"],
        gamma=g["gamma"],
    )
    if sha != m["sha256"]:
        raise RuntimeError(f"fixture hash {sha} differs from the manifest's {m['sha256']}")
    return path


if __name__ == "__main__":  # pragma: no cover - manual regeneration
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
    m = load_manifest()
    g = m["generator"]
    print(
        write_panel(
            out / m["file"],
            seed=g["seed"],
            n_sessions=g["n_sessions"],
            n_names=g["n_names"],
            phi=g["phi"],
            kappa=g["kappa"],
            gamma=g["gamma"],
        )
    )
