"""The DD1 daily set as the research loop's panel (v2.0 stage 7 on the ``daily_pit`` route).

``tools/ingest/dd1_daily.py`` writes, per purchase year ``Y``,
``<dev-root>/dd1_<Y>/dataset.json`` and one directory per development-readable
partition holding ``prices.csv``, ``membership.csv``, ``filings.csv``,
``factors.csv`` and ``metadata.json``.  The research loop reads one CSV panel
with the columns of :data:`iap.research.family_features.REQUIRED_COLUMNS` and a
manifest.  :func:`build_panel` turns the first into the second:

* **what it opens**: ``dataset.json`` and the ``development`` and ``validation``
  directories only, each file checked against the sha256 the record holds before
  it is parsed; a record that is not ``complete`` (a DD1 test failed), a
  partition outside those two, or a path under a sealed root is refused
  (:class:`PanelRefused`).  Nothing sealed is ever named, let alone opened.
* **prices**: the vendor's adjusted open and close (``adj_open``, ``adj_close``;
  the raw columns where the adjusted ones are empty) and the session volume, on
  the sessions of the French calendar the DD1 calendar test checked.
* **universe**: the names with a membership interval in ``index`` (default the
  S&P 500 proxy).  ``member`` is 1 on the sessions inside an interval (start
  inclusive, end exclusive, an open end is still a member) and 0 on the others;
  the loop fits and trades member rows only, and keeps the other rows for the
  trailing windows.  Rows start ``warmup_sessions`` before the first interval.
* **market**: ``mkt_ret`` and ``mkt_c2o`` are the equal-weighted close-to-close
  and close-to-open returns of the session's members (all kept names on a
  session before the first interval).
* **filing**: 1 on the first session on or after the EDGAR filing date of a
  10-Q or 10-K (EDGAR's index carries a date, not an acceptance time).
* **estimated columns** (the manifest says so): DD1 has no auction volumes, so
  ``auction_volume`` and ``open_volume`` are the planning shares 8 % and 4 % of
  the session volume; DD1 has no sector, so each name is its own ``sector``
  bucket and the sector cap reduces to the name cap.

The panel is written deterministically, so its sha256 in the manifest pins the
run's input.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from iap.data.sources.base import is_sealed_target
from iap.research import family_features as ff

__all__ = [
    "AUCTION_SHARE",
    "FINANCIAL_FORMS",
    "OPEN_SHARE",
    "PANEL_COLUMNS",
    "PanelRefused",
    "build_panel",
    "read_record",
    "write_panel",
]

#: Planning share of the session volume printed in the close auction (estimated, labelled).
AUCTION_SHARE = 0.08
#: Planning share of the session volume printed in the open auction (estimated, labelled).
OPEN_SHARE = 0.04
#: The EDGAR forms that count as a financial filing for D4.
FINANCIAL_FORMS = frozenset({"10-Q", "10-K"})
#: The partitions a development identity may open (U0 section 2).
READABLE = ("development", "validation")
PANEL_COLUMNS = (*ff.REQUIRED_COLUMNS, "member")


class PanelRefused(PermissionError):
    """The DD1 record or its files are not what the panel may be built from."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_record(dataset_dir: str | Path, *, allow_incomplete: bool = False) -> dict[str, Any]:
    """``dataset.json`` checked: kind, status, the readable partitions and every file hash."""
    root = Path(dataset_dir)
    if is_sealed_target(root.resolve()):
        raise PanelRefused(f"{root} lies under a sealed partition root")
    rec_path = root / "dataset.json"
    if not rec_path.is_file():
        raise PanelRefused(f"no DD1 record at {rec_path}")
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    if rec.get("kind") != "daily_pit":
        raise PanelRefused(f"record kind {rec.get('kind')!r} is not daily_pit")
    # development_only (DR-18): development and validation written, test years not saved
    if rec.get("status") not in ("complete", "development_only") and not allow_incomplete:
        raise PanelRefused(
            f"record status {rec.get('status')!r}: a DD1 test failed or a service refused; "
            "the research loop runs on a complete ingestion only"
        )
    parts = rec.get("partitions") or {}
    for name in READABLE:
        p = parts.get(name)
        if not p or p.get("location") != "development-readable":
            raise PanelRefused(f"partition {name!r} is not development-readable in the record")
        pdir = root / str(p["dir"])
        if pdir.resolve().parent != root.resolve():
            raise PanelRefused(f"partition {name!r} directory escapes the dataset directory")
        for fname, digest in sorted(p["files"].items()):
            got = _sha256(pdir / fname)
            if got != digest:
                raise PanelRefused(f"{name}/{fname}: sha256 {got[:16]} != record {digest[:16]}")
    rec["_sha256"] = _sha256(rec_path)
    return rec


def _read(root: Path, rec: dict[str, Any], fname: str, **kw: Any) -> pd.DataFrame:
    frames = [pd.read_csv(root / str(rec["partitions"][n]["dir"]) / fname, **kw) for n in READABLE]
    return pd.concat(frames, ignore_index=True)


def build_panel(
    dataset_dir: str | Path,
    *,
    index: str = "sp500",
    warmup_sessions: int = 300,
    allow_incomplete: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """(panel, manifest without ``sha256``) from a DD1 dataset directory (module docstring)."""
    root = Path(dataset_dir)
    rec = read_record(root, allow_incomplete=allow_incomplete)
    windows = rec["windows"]

    prices = _read(
        root,
        rec,
        "prices.csv",
        usecols=["symbol", "date", "open", "close", "volume", "adj_open", "adj_close"],
        dtype={"symbol": str, "date": str},
    )
    prices["open"] = prices["adj_open"].fillna(prices["open"]).astype(float)
    prices["close"] = prices["adj_close"].fillna(prices["close"]).astype(float)
    prices = prices.drop(columns=["adj_open", "adj_close"])
    ok = (
        np.isfinite(prices["open"])
        & np.isfinite(prices["close"])
        & (prices["open"] > 0)
        & (prices["close"] > 0)
        & np.isfinite(prices["volume"])
        & (prices["volume"] >= 0)
    )
    prices = prices.loc[ok]

    factors = _read(root, rec, "factors.csv", usecols=["date"], dtype={"date": str})
    calendar = sorted(set(factors["date"]) & set(prices["date"]))
    if not calendar:
        raise PanelRefused("no session is both a calendar day and a priced day")
    prices = prices.loc[prices["date"].isin(set(calendar))]

    mem = _read(
        root,
        rec,
        "membership.csv",
        usecols=["index", "symbol", "start", "end"],
        dtype={"index": str, "symbol": str, "start": str, "end": str},
    )
    mem = mem.loc[mem["index"] == index, ["symbol", "start", "end"]].drop_duplicates()
    if mem.empty:
        raise PanelRefused(f"no membership interval for index {index!r}")
    universe = set(mem["symbol"])
    first_start = str(mem["start"].min())
    i0 = int(np.searchsorted(np.array(calendar), first_start))
    lo = calendar[max(i0 - warmup_sessions, 0)]
    panel = prices.loc[prices["symbol"].isin(universe) & (prices["date"] >= lo)].copy()
    panel = panel.rename(columns={"date": "session", "symbol": "name"})
    panel = panel.sort_values(["name", "session"], kind="stable").reset_index(drop=True)

    # member: inside an interval (start inclusive, end exclusive, open end = member)
    pairs = (
        panel[["name", "session"]]
        .reset_index()
        .merge(mem.rename(columns={"symbol": "name"}), on="name")
    )
    inside = (pairs["session"] >= pairs["start"]) & (
        pairs["end"].isna() | (pairs["session"] < pairs["end"].fillna(""))
    )
    panel["member"] = 0
    panel.loc[pairs.loc[inside, "index"].unique(), "member"] = 1

    # market returns: equal-weighted over the session's members
    prev = panel.groupby("name", sort=False)["close"].shift(1)
    ret = panel["close"] / prev - 1.0
    c2o = panel["open"] / prev - 1.0
    tmp = pd.DataFrame({"session": panel["session"], "m": panel["member"], "r": ret, "c": c2o})
    tmp = tmp.loc[np.isfinite(tmp["r"])]
    by_mem = tmp.loc[tmp["m"] == 1].groupby("session")[["r", "c"]].mean()
    by_all = tmp.groupby("session")[["r", "c"]].mean()
    mkt = by_mem.reindex(by_all.index).fillna(by_all)
    panel["mkt_ret"] = panel["session"].map(mkt["r"]).fillna(0.0)
    panel["mkt_c2o"] = panel["session"].map(mkt["c"]).fillna(0.0)

    # filings: the first session on or after the filing date
    fil = _read(
        root,
        rec,
        "filings.csv",
        usecols=["symbol", "form", "filing_date"],
        dtype={"symbol": str, "form": str, "filing_date": str},
    )
    fil = fil.loc[fil["form"].isin(FINANCIAL_FORMS) & fil["symbol"].isin(universe)]
    cal = np.array(calendar)
    pos = np.searchsorted(cal, fil["filing_date"].to_numpy(dtype=str))
    fil = fil.assign(session=[cal[p] if p < len(cal) else "" for p in pos])
    filed = set(zip(fil["symbol"], fil["session"], strict=True))
    panel["filing"] = [
        1 if (n, s) in filed else 0 for n, s in zip(panel["name"], panel["session"], strict=True)
    ]

    vol = panel["volume"].astype(float)
    panel["volume"] = vol.round().astype(np.int64)
    panel["auction_volume"] = np.floor(AUCTION_SHARE * vol).astype(np.int64)
    panel["open_volume"] = np.floor(OPEN_SHARE * vol).astype(np.int64)
    panel["sector"] = "name:" + panel["name"]
    panel = panel.sort_values(["session", "name"], kind="stable").reset_index(drop=True)
    panel = panel[list(PANEL_COLUMNS)]

    sessions = sorted(panel["session"].unique().tolist())
    part_ranges: dict[str, list[str]] = {}
    for name in READABLE:
        wlo, whi = windows[name]
        inside_w = [s for s in sessions if (wlo is None or s >= wlo) and (whi is None or s <= whi)]
        if not inside_w:
            raise PanelRefused(f"no panel session inside the {name} window {windows[name]}")
        part_ranges[name] = [inside_w[0], inside_w[-1]]

    manifest: dict[str, Any] = {
        "dataset_id": f"dd1_{rec['purchase_year']}.{index}",
        "route": "daily_pit",
        "sealed": False,
        "file": "panel.csv",
        "partitions": part_ranges,
        "source": {
            "record_sha256": rec["_sha256"],
            "purchase_year": rec["purchase_year"],
            "status": rec["status"],
            "sources": rec.get("sources", {}),
            "dataset_hash_ids": {n: rec["partitions"][n]["dataset_hash_id"] for n in READABLE},
        },
        "universe": {
            "index": index,
            "names": len(universe),
            "sessions": len(sessions),
            "warmup_sessions": warmup_sessions,
            "member_rows": int(panel["member"].sum()),
            "rows": len(panel),
        },
        "estimated_columns": {
            "auction_volume": f"{AUCTION_SHARE} x session volume (DD1 has no auction volume)",
            "open_volume": f"{OPEN_SHARE} x session volume (DD1 has no auction volume)",
            "sector": "one bucket per name (DD1 has no sector): the sector cap reduces to the name cap",
        },
        "conventions": {
            "prices": "vendor-adjusted open and close (adj_open, adj_close; raw where empty)",
            "member": "1 inside a membership interval (start inclusive, end exclusive); the loop fits and trades member rows only",
            "market": "equal-weighted returns of the session's members",
            "filing": "first session on or after the EDGAR filing date of a 10-Q or 10-K",
        },
    }
    return panel, manifest


def _fmt(x: float) -> str:
    return "" if not math.isfinite(x) else repr(float(x))


def write_panel(
    dataset_dir: str | Path,
    out_dir: str | Path,
    *,
    index: str = "sp500",
    warmup_sessions: int = 300,
    allow_incomplete: bool = False,
) -> tuple[Path, Path]:
    """Write ``panel.csv`` and ``manifest.json`` (with the panel's sha256) under ``out_dir``."""
    out = Path(out_dir)
    if is_sealed_target(out.resolve()):
        raise PanelRefused(f"{out} lies under a sealed partition root")
    panel, manifest = build_panel(
        dataset_dir,
        index=index,
        warmup_sessions=warmup_sessions,
        allow_incomplete=allow_incomplete,
    )
    out.mkdir(parents=True, exist_ok=True)
    data = panel_csv_bytes(panel)
    p = out / "panel.csv"
    p.write_bytes(data)
    manifest["sha256"] = hashlib.sha256(data).hexdigest()
    m = out / "manifest.json"
    m.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return p, m


def panel_csv_bytes(panel: pd.DataFrame, columns: Sequence[str] = PANEL_COLUMNS) -> bytes:
    """Deterministic CSV bytes (floats by ``repr``, LF line ends)."""
    floats = {"open", "close", "mkt_ret", "mkt_c2o"}
    cols = [panel[c].map(_fmt) if c in floats else panel[c].astype(str) for c in columns]
    lines = [",".join(columns)]
    lines.extend(",".join(row) for row in zip(*cols, strict=True))
    return ("\n".join(lines) + "\n").encode("utf-8")
