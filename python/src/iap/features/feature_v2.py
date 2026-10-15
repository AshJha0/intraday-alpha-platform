"""Feature spec v2 — 15 m / 30 m / 1 h windows as an opt-in engine extension (v1.17 C4).

The registered feature set (``iap.features.registry``, 205 features, hash
``585dd7b9...``) stops at 5-minute windows and is fingerprinted by the open
pre-registrations; ``WINDOW_NS`` in ``iap.features.spec`` is part of that
contract.  This module ADDS the longer windows in a separate registry with
its own hash and leaves every registered module untouched: nothing here is
imported by the default engine, the default registry or any port.

**Families and windows.**  One family, ``longwin``, over the windows
:data:`WINDOWS_V2_NS` (``15m``, ``30m``, ``1h``); every name ends ``_v2``:

| feature | definition | valid when |
|---|---|---|
| ``ret_log_w<w>_v2`` | ``ln mid(t) - ln mid(t - w)`` (the latest mid-change sample at or before ``t - w``) | book two-sided, window warm, a sample exists at or before ``t - w`` |
| ``rvol_w<w>_v2`` | ``sqrt(sum of squared log-mid changes over (t - w, t] / w_s)`` | book two-sided, window warm |
| ``trend_score_w<w>_v2`` | ``ret_log_w / (rvol_w * sqrt(w_s) + EPS)`` | both valid and ``rvol_w > 0`` (EPS guard: an unobserved denominator is undefined) |
| ``range_w<w>_v2`` | ``max - min`` of the log mid over the emissions in ``(t - w, t]`` | book two-sided, window warm |

Samples: the log mid is recorded at every emission whose merged book is
two-sided (``range``), and at every CHANGE of the mid (``ret_log`` lookups,
``rvol`` increments) — the base engine's own sampling rule for its 10 s /
1 m / 5 m volatility and trend features, so a ``trend_score_w15m_v2`` is
the ``trend_score_w5m_v1`` formula at a longer window.  Every window is
half-open ``(t - w, t]`` in event time (``iap.features.rolling``).

**Session-spanning rule (pinned).**  A window never spans a session: the
v2 state of an instrument is cleared, and its warmup anchor moved to the
current event, (a) at the first emission of a new SESSION-LOCAL day
(:class:`iap.features.context.SessionClock`: the day holding the event in
the instrument's session zone), and (b) at every stale->fresh recovery
the base engine performs (API_FEATURES §2.1, the rule the registered
windows follow).  A window is warm when ``t - anchor >= w``, so the first
``w`` of every session has no ``w``-window value — the 1 h windows exist
from 10:30 New York on a 09:30 open, and never read yesterday's close.
This is why one engine per day cannot "truncate" these windows: the
boundary is the session, whether the engine is one per day or one per
month.

**Opt-in.**  :class:`FeatureV2Extension` wraps a running
:class:`iap.features.engine.FeatureEngine` (or an
``ExtendedFeatureEngine``) through its ``on_vector`` hook: for every base
vector it reads the instrument's merged-book state and emits a second
:class:`FeatureVector` carrying :func:`v2_registry_hash` as its
``feature_version``.  The base vector is unchanged byte for byte (the
goldens of §5 hold with the extension attached) and the extension is
never attached unless constructed.  The feature store CLI does not write
v2 columns; building a v2 frame is a programmatic step
(:func:`v2_frame`) and the labels come from
:mod:`iap.labels.labels_v2`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import sqrt
from typing import Any

import pandas as pd

from iap.features.engine import FeatureEngine, FeatureVector
from iap.features.registry import registry_hash
from iap.features.rolling import RollingExtrema, RollingSum, TimeSeries
from iap.features.spec import EPS, NS_PER_SEC, FeatureSpec, mkspec

__all__ = [
    "FAMILY_V2",
    "FEATURE_V2_VERSION",
    "WINDOWS_V2_NS",
    "FeatureV2Extension",
    "specs_v2",
    "v2_feature_names",
    "v2_frame",
    "v2_registry_dicts",
    "v2_registry_hash",
]

FAMILY_V2 = "longwin"
#: Contract version of this module's outputs.
FEATURE_V2_VERSION = 1
#: The v2 windows (event time).
WINDOWS_V2_NS: dict[str, int] = {
    "15m": 900 * NS_PER_SEC,
    "30m": 1_800 * NS_PER_SEC,
    "1h": 3_600 * NS_PER_SEC,
}
_NS_DAY = 86_400 * NS_PER_SEC
_KINDS = ("ret_log", "rvol", "trend_score", "range")


def specs_v2() -> list[FeatureSpec]:
    """Registry entries of the v2 family (pinned order: window-major)."""
    out: list[FeatureSpec] = []
    for w in WINDOWS_V2_NS:
        out.append(
            mkspec(
                f"ret_log_w{w}_v2",
                FAMILY_V2,
                f"Log mid return over {w}: ln mid(t) - ln mid(t - {w}).",
                version=2,
                window=w,
            )
        )
        out.append(
            mkspec(
                f"rvol_w{w}_v2",
                FAMILY_V2,
                f"Realized vol over {w}: sqrt(sum squared log-mid changes / w_s).",
                version=2,
                window=w,
            )
        )
        out.append(
            mkspec(
                f"trend_score_w{w}_v2",
                FAMILY_V2,
                f"Normalized drift over {w}: ret_log / (rvol * sqrt(w_s) + EPS).",
                depends_on=(f"ret_log_w{w}_v2", f"rvol_w{w}_v2"),
                version=2,
                window=w,
            )
        )
        out.append(
            mkspec(
                f"range_w{w}_v2",
                FAMILY_V2,
                f"Log-mid range over {w}: max - min over the window's emissions.",
                version=2,
                window=w,
            )
        )
    return out


def v2_feature_names() -> list[str]:
    return [s.name for s in specs_v2()]


def v2_registry_dicts() -> list[dict]:
    """Canonical JSON entries: the base registry's hash first (the v2 set
    is defined on top of a specific registered set), then the v2 specs."""
    return [{"base_registry_hash": registry_hash()}, *[s.to_dict() for s in specs_v2()]]


def v2_registry_hash() -> str:
    """sha256 of the canonical JSON of :func:`v2_registry_dicts`."""
    import hashlib
    import json

    blob = json.dumps(v2_registry_dicts(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class _V2State:
    """Per-instrument long-window state (cleared per session / recovery)."""

    anchor: int | None = None
    day: int | None = None
    recoveries: int = 0
    last_logmid: float | None = None

    def __post_init__(self) -> None:
        self.hist = TimeSeries()  # ln mid at mid changes
        self.rv = {w: RollingSum(n, 1) for w, n in WINDOWS_V2_NS.items()}
        self.ext = {w: RollingExtrema(n) for w, n in WINDOWS_V2_NS.items()}

    def reset(self, t: int) -> None:
        self.anchor = t
        self.last_logmid = None
        self.hist = TimeSeries()
        self.rv = {w: RollingSum(n, 1) for w, n in WINDOWS_V2_NS.items()}
        self.ext = {w: RollingExtrema(n) for w, n in WINDOWS_V2_NS.items()}

    def warm(self, t: int, w_ns: int) -> bool:
        return self.anchor is not None and t - self.anchor >= w_ns


class FeatureV2Extension:
    """Attach the v2 family to a running engine (module docs, "Opt-in").

    ``on_vector(base_vector, v2_vector)`` is called for every emission;
    the engine's previous ``on_vector`` (if any) still receives the base
    vector first.  ``vectors`` keeps the emitted v2 vectors when
    ``keep=True`` (tests, small fixtures).
    """

    def __init__(
        self,
        engine: FeatureEngine,
        on_vector: Callable[[FeatureVector, FeatureVector], None] | None = None,
        keep: bool = False,
    ) -> None:
        self.engine = engine
        self.feature_names = v2_feature_names()
        self.feature_version = v2_registry_hash()
        self.states: dict[int, _V2State] = {}
        self.vectors: list[FeatureVector] = []
        self.vectors_emitted = 0
        self.session_resets = 0
        self._keep = keep
        self._user_cb = on_vector
        self._inner_cb = engine.on_vector
        engine.on_vector = self._hook

    def detach(self) -> None:
        """Restore the engine's previous ``on_vector``."""
        self.engine.on_vector = self._inner_cb

    # ------------------------------------------------------------ hook

    def _hook(self, vec: FeatureVector) -> None:
        if self._inner_cb is not None:
            self._inner_cb(vec)
        v2 = self.compute(vec.instrument_id, vec.timestamp)
        self.vectors_emitted += 1
        if self._keep:
            self.vectors.append(v2)
        if self._user_cb is not None:
            self._user_cb(vec, v2)

    def compute(self, instrument_id: int, t: int) -> FeatureVector:
        """The v2 vector of ``instrument_id`` at the base emission ``t``
        (reads the engine's merged-book state; call from the hook only)."""
        st = self.engine.states[instrument_id]
        xs = self.states.get(instrument_id)
        if xs is None:
            xs = self.states[instrument_id] = _V2State()
        day = (t + st.ctx.clock.offset_seconds(t) * NS_PER_SEC) // _NS_DAY
        if xs.anchor is None or day != xs.day or st.recoveries != xs.recoveries:
            if xs.anchor is not None:
                self.session_resets += 1
            xs.reset(t)
            xs.day = day
            xs.recoveries = st.recoveries
        for win in xs.rv.values():
            win.trim(t)
        for ext in xs.ext.values():
            ext.trim(t)
        xs.hist.trim(t - WINDOWS_V2_NS["1h"] - NS_PER_SEC)
        ok = bool(st.book_ok)
        if ok:
            lm = float(st.logmid)
            for ext in xs.ext.values():
                ext.add(t, lm)
            if xs.last_logmid is None or lm != xs.last_logmid:
                if xs.last_logmid is not None:
                    d = lm - xs.last_logmid
                    for win in xs.rv.values():
                        win.add(t, (d * d,))
                xs.hist.append(t, lm)
                xs.last_logmid = lm
        values: list[float] = []
        valid: list[bool] = []
        for w, w_ns in WINDOWS_V2_NS.items():
            warm = ok and xs.warm(t, w_ns)
            w_s = w_ns / 1e9
            ret: float | None = None
            rvol: float | None = None
            trend: float | None = None
            rng: float | None = None
            if warm:
                past = xs.hist.at_or_before(t - w_ns)
                if past is not None:
                    ret = float(st.logmid) - past
                rvol = sqrt(max(xs.rv[w].sums[0], 0.0) / w_s)
                if ret is not None and rvol > 0.0:
                    trend = ret / (rvol * sqrt(w_s) + EPS)
                hi, lo = xs.ext[w].max(), xs.ext[w].min()
                if hi is not None and lo is not None:
                    rng = hi - lo
            for v in (ret, rvol, trend, rng):
                _put(values, valid, v)
        return FeatureVector(
            instrument_id=instrument_id,
            timestamp=t,
            feature_version=self.feature_version,
            values=values,
            validity=valid,
        )


def _put(values: list[float], valid: list[bool], v: float | None) -> None:
    if v is None or v != v:
        values.append(float("nan"))
        valid.append(False)
    else:
        values.append(float(v))
        valid.append(True)


def v2_frame(vectors: list[FeatureVector]) -> pd.DataFrame:
    """A per-instrument v2 feature frame (``exchange_ts`` + one float64
    column per v2 feature, NaN where invalid), from one instrument's
    emitted v2 vectors in emission order."""
    names = v2_feature_names()
    cols: dict[str, Any] = {"exchange_ts": [v.timestamp for v in vectors]}
    for j, name in enumerate(names):
        cols[name] = [v.values[j] if v.validity[j] else float("nan") for v in vectors]
    return pd.DataFrame(cols)
