"""Per-instrument static context for the feature engine.

Built from ``configs/instruments.json`` (tick size, asset class, session
bounds) — independent of any live data.  The cross-asset reference instrument
is pinned per asset class:

- EQUITY  -> the ETF (asset_class == "ETF"; SYN.ETF.IDX, instrument_id 11)
- ETF     -> itself
- FX      -> EUR/USD (instrument_id 101); EUR/USD -> itself

For the reference instrument itself, cross-asset features degenerate
naturally (beta = 1, residual = 0, correlation = 1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Union

_FX_REF_SYMBOL = "EUR/USD"


@dataclass(frozen=True)
class InstrumentContext:
    """Static per-instrument facts the feature engine needs."""

    instrument_id: int
    symbol: str
    asset_class: str
    tick_size: float
    session_open_min: int  # minute of UTC day the session opens
    session_close_min: int  # minute of UTC day the session closes
    ref_instrument_id: int  # cross-asset reference instrument


def _minute_of_day(hhmmss: str) -> int:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return h * 60 + m + (1 if s >= 30 else 0)


def build_contexts(config_dir: Union[str, Path]) -> Dict[int, InstrumentContext]:
    """Build {instrument_id: InstrumentContext} from configs/instruments.json."""
    config_dir = Path(config_dir)
    with open(config_dir / "instruments.json") as f:
        cfg = json.load(f)

    sessions = cfg["sessions"]
    rows = cfg["instruments"]

    etf_id = None
    fx_ref_id = None
    for row in rows:
        if row["asset_class"] == "ETF" and etf_id is None:
            etf_id = row["instrument_id"]
        if row["symbol"] == _FX_REF_SYMBOL:
            fx_ref_id = row["instrument_id"]

    out: Dict[int, InstrumentContext] = {}
    for row in rows:
        ac = row["asset_class"]
        sess = sessions.get(ac)
        if sess is None:
            raise ValueError(f"no session definition for asset class {ac!r}")
        if ac == "FX":
            ref = fx_ref_id if fx_ref_id is not None else row["instrument_id"]
        elif ac == "ETF":
            ref = row["instrument_id"]
        else:  # EQUITY
            ref = etf_id if etf_id is not None else row["instrument_id"]
        out[row["instrument_id"]] = InstrumentContext(
            instrument_id=row["instrument_id"],
            symbol=row["symbol"],
            asset_class=ac,
            tick_size=float(row["tick_size"]),
            session_open_min=_minute_of_day(sess["open"]),
            session_close_min=_minute_of_day(sess["close"]),
            ref_instrument_id=ref,
        )
    return out
