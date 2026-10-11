"""Config / reference-data I/O of the MVP engine: the pinned horizon names,
the declared execution controls and the feature context (split out of
:mod:`iap.mvp.engine`, v1.13 E4; re-exported there).
"""

from __future__ import annotations

import json

from iap.features.context import InstrumentContext, SessionClock
from iap.labels.labels import HORIZONS_NS
from iap.mvp.config import ControlsSpec, MvpConfig
from iap.reference.refdata import ReferenceData


def horizon_name(horizon_ns: int) -> str:
    """The pinned label-horizon name of ``horizon_ns`` (conventions §7);
    raises when the duration is not one of the eleven pinned horizons."""
    for name, ns in HORIZONS_NS.items():
        if ns == horizon_ns:
            return name
    raise ValueError(
        f"horizon_ns {horizon_ns} is not a pinned label horizon ({', '.join(HORIZONS_NS)})"
    )


def load_controls(cfg: MvpConfig) -> ControlsSpec:
    """The declared controls from ``execution.json`` ``defaults`` (fail-fast)."""
    path = cfg.reference_path("execution")
    with open(path, encoding="utf-8") as fh:
        root = json.load(fh)
    defaults = root.get("defaults")
    if not isinstance(defaults, dict):
        raise ValueError(f"{path}: missing defaults block")

    def integer(key: str, lo: int) -> int:
        v = defaults.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or v < lo:
            raise ValueError(f"{path}: defaults.{key} must be an integer >= {lo}")
        return v

    part = defaults.get("max_participation")
    if isinstance(part, bool) or not isinstance(part, (int, float)) or not 0.0 < part <= 1.0:
        raise ValueError(f"{path}: defaults.max_participation must be in (0, 1]")
    return ControlsSpec(
        max_child_qty=integer("max_child_qty", 1),
        max_participation=float(part),
        min_slice_interval_ns=integer("min_slice_interval_ns", 0),
        latency_budget_ns=integer("latency_budget_ns", 1),
    )


def feature_context(cfg: MvpConfig, ref: ReferenceData) -> InstrumentContext:
    """The feature engine's :class:`InstrumentContext` of the configured
    instrument (session bounds from ``mvp.json``, tick / class from the
    MVP reference data) — the one context both the engine and the research
    frame builder use."""
    inst = ref.instrument(cfg.instrument)
    h0, m0, _ = (int(x) for x in cfg.session.open.split(":"))
    h1, m1, s1 = (int(x) for x in cfg.session.close.split(":"))
    return InstrumentContext(
        instrument_id=inst.instrument_id,
        symbol=inst.symbol,
        asset_class=inst.asset_class,
        tick_size=float(inst.tick_size),
        session_open_min=h0 * 60 + m0,
        session_close_min=h1 * 60 + m1 + (1 if s1 >= 30 else 0),
        ref_instrument_id=inst.instrument_id,
        session_timezone=cfg.session.timezone,
        clock=SessionClock(cfg.session.timezone),
    )
