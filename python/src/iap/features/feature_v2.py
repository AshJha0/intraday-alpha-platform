"""Feature spec v2 — long windows (v1.17 C4) and share-normalised flow (v1.17 A5),
as an opt-in engine extension.

The registered feature set (``iap.features.registry``, 205 features, hash
``585dd7b9...``) stops at 5-minute windows, reports volumes in shares and is
fingerprinted by the open pre-registrations; ``WINDOW_NS`` in
``iap.features.spec`` is part of that contract.  This module ADDS two
families in a separate registry with its own hash and leaves every
registered module untouched: nothing here is imported by the default
engine, the default registry or any port.  Every name ends ``_v2``.

**Family ``longwin`` (C4)** over the windows :data:`WINDOWS_V2_NS`
(``15m``, ``30m``, ``1h``):

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

**Family ``sharenorm`` (A5, review D-P1-5)** — volumes and depths divided
by a share count, so the values compare across instruments of any size.
Two normalisers: the instrument's **session ADV** (:class:`SessionAdvTable`
from the dataset's ``adv_by_session``, the AS-OF value of the session the
emission falls in; a static per-instrument map, or ``None`` = every ADV
feature invalid) and the **running session volume** (the shares printed
so far in the session-local day).  Windows :data:`WINDOWS_SHARENORM_NS`
(``1m``, ``5m``, ``15m``):

| feature | definition | valid when |
|---|---|---|
| ``session_volume_adv_v2`` | shares printed so far this session / ADV | ADV known |
| ``traded_volume_adv_w<w>_v2`` | shares printed in ``(t - w, t]`` / ADV | ADV known, window warm |
| ``signed_volume_adv_w<w>_v2`` | (buy - sell shares in ``(t - w, t]``) / ADV | ADV known, window warm |
| ``traded_volume_session_w<w>_v2`` | shares printed in ``(t - w, t]`` / shares printed so far this session | window warm, session volume > 0 |
| ``depth_l1_adv_v2`` | (merged displayed bid + ask at L1) / ADV | book two-sided, ADV known |
| ``depth_l10_adv_v2`` | (merged displayed bid + ask over 10 levels) / ADV | book two-sided, ADV known |
| ``depth_diff_l1_adv_v2`` | (merged displayed bid - ask at L1) / ADV | book two-sided, ADV known |
| ``depth_diff_l10_adv_v2`` | (merged displayed bid - ask over 10 levels) / ADV | book two-sided, ADV known |

"Shares printed" are the continuous ``TRADE`` prints the base engine
applied (same bounds: an exchange_ts regression, a duplicate, an invalid
payload or an oversized quantity contributes nothing) plus the side-less
cross prints the pipeline folds through ``on_cross_print`` (D-P2-6; in the
volumes, never in the signed volume).  Quantities and ADV are in the
instrument's ``instruments.json`` units (shares; FX: lots).

**Busts (A5, review X-11).**  A ``TRADE_BUST`` event (ITCH ``B``, IEX
trade break) names an earlier print by ``trade_id``.  From the bust's own
time forward the ``sharenorm`` volumes exclude that print: every window
it is still inside loses its shares and signed shares, and the session
volume loses them for the rest of the session; emissions before the bust
are what they were (the print was on the tape then).  A bust naming a
print this extension did not record (a cross print, a print before the
session reset, an unknown id) is counted (``busts_unmatched``) and changes
nothing.  The registered features never see a bust
(:class:`iap.features.engine.FeatureEngine` skips it) and the ``longwin``
family reads no volume, so a bust changes ``sharenorm`` only.

**Session-spanning rule (pinned).**  A window never spans a session: the
v2 state of an instrument is cleared, and its warmup anchor moved to the
current event, (a) at the first emission of a new SESSION-LOCAL day
(:class:`iap.features.context.SessionClock`: the day holding the event in
the instrument's session zone), and (b) at every stale->fresh recovery
the base engine performs (API_FEATURES §2.1, the rule the registered
windows follow).  A window is warm when ``t - anchor >= w``, so the first
``w`` of every session has no ``w``-window value — the 1 h windows exist
from 10:30 New York on a 09:30 open, and never read yesterday's close.
The running session volume is cleared at (a) only: after a recovery it
keeps what was observed before the gap (an undercount either way, so the
rule is the simpler one).  This is why one engine per day cannot
"truncate" these windows: the boundary is the session, whether the engine
is one per day or one per month.

**Opt-in.**  :class:`FeatureV2Extension` wraps a running
:class:`iap.features.engine.FeatureEngine` (or an
``ExtendedFeatureEngine``) through its ``on_vector`` hook: for every base
vector it reads the instrument's merged-book state and emits a second
:class:`FeatureVector` carrying :func:`v2_registry_hash` as its
``feature_version``.  It also wraps the engine's ``apply`` and
``on_cross_print`` (to see the prints and the busts; both are restored by
``detach``).  The base vector is unchanged byte for byte (the goldens of
§5 hold with the extension attached) and the extension is never attached
unless constructed.  ``families`` picks the families (default both; the
registry hash and the vector layout follow the choice, family order
pinned).  The feature store CLI does not write v2 columns; building a v2
frame is a programmatic step (:func:`v2_frame`) and the labels come from
:mod:`iap.labels.labels_v2`.  Python-reference only: no Rust / C++ port
(PLATFORM_CONVENTIONS §11.7, pending F7 / F8).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from math import sqrt
from typing import Any

import pandas as pd

from iap.core.events import EventType, MarketEvent
from iap.features.engine import FEATURE_MAX_QTY, FeatureEngine, FeatureVector
from iap.features.registry import registry_hash
from iap.features.rolling import RollingExtrema, RollingSum, TimeSeries
from iap.features.spec import EPS, NS_PER_SEC, FeatureSpec, mkspec
from iap.reference.session_adv import SessionAdvTable

__all__ = [
    "FAMILIES_V2",
    "FAMILY_SHARENORM",
    "FAMILY_V2",
    "FEATURE_V2_VERSION",
    "WINDOWS_SHARENORM_NS",
    "WINDOWS_V2_NS",
    "FeatureV2Extension",
    "specs_longwin",
    "specs_sharenorm",
    "specs_v2",
    "v2_feature_names",
    "v2_frame",
    "v2_registry_dicts",
    "v2_registry_hash",
]

FAMILY_V2 = "longwin"
FAMILY_SHARENORM = "sharenorm"
#: The v2 families in pinned (registry) order.
FAMILIES_V2: tuple[str, ...] = (FAMILY_V2, FAMILY_SHARENORM)
#: Contract version of this module's outputs (2: the ``sharenorm`` family).
FEATURE_V2_VERSION = 2
#: The ``longwin`` windows (event time).
WINDOWS_V2_NS: dict[str, int] = {
    "15m": 900 * NS_PER_SEC,
    "30m": 1_800 * NS_PER_SEC,
    "1h": 3_600 * NS_PER_SEC,
}
#: The ``sharenorm`` windows (event time).
WINDOWS_SHARENORM_NS: dict[str, int] = {
    "1m": 60 * NS_PER_SEC,
    "5m": 300 * NS_PER_SEC,
    "15m": 900 * NS_PER_SEC,
}
_NS_DAY = 86_400 * NS_PER_SEC
_KINDS = ("ret_log", "rvol", "trend_score", "range")
_LONGEST_SHARENORM = max(WINDOWS_SHARENORM_NS.values())


def specs_longwin() -> list[FeatureSpec]:
    """Registry entries of the ``longwin`` family (pinned order: window-major)."""
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


def specs_sharenorm() -> list[FeatureSpec]:
    """Registry entries of the ``sharenorm`` family (pinned order: the
    session total, then window-major volumes, then the depths)."""
    out: list[FeatureSpec] = [
        mkspec(
            "session_volume_adv_v2",
            FAMILY_SHARENORM,
            "Shares printed so far this session (busts removed) / session ADV.",
            version=2,
        )
    ]
    for w in WINDOWS_SHARENORM_NS:
        out.append(
            mkspec(
                f"traded_volume_adv_w{w}_v2",
                FAMILY_SHARENORM,
                f"Shares printed over {w} (busts removed) / session ADV.",
                version=2,
                window=w,
            )
        )
        out.append(
            mkspec(
                f"signed_volume_adv_w{w}_v2",
                FAMILY_SHARENORM,
                f"Buy minus sell shares over {w} (busts removed) / session ADV.",
                version=2,
                window=w,
            )
        )
        out.append(
            mkspec(
                f"traded_volume_session_w{w}_v2",
                FAMILY_SHARENORM,
                f"Shares printed over {w} / shares printed so far this session.",
                version=2,
                window=w,
            )
        )
    for lv in ("l1", "l10"):
        levels = 1 if lv == "l1" else 10
        out.append(
            mkspec(
                f"depth_{lv}_adv_v2",
                FAMILY_SHARENORM,
                f"Merged displayed bid + ask over {levels} level(s) / session ADV.",
                version=2,
                levels=levels,
            )
        )
        out.append(
            mkspec(
                f"depth_diff_{lv}_adv_v2",
                FAMILY_SHARENORM,
                f"Merged displayed bid - ask over {levels} level(s) / session ADV.",
                version=2,
                levels=levels,
            )
        )
    return out


_SPECS_OF: dict[str, Callable[[], list[FeatureSpec]]] = {
    FAMILY_V2: specs_longwin,
    FAMILY_SHARENORM: specs_sharenorm,
}


def _families(families: Sequence[str] | None) -> tuple[str, ...]:
    if families is None:
        return FAMILIES_V2
    fams = tuple(families)
    unknown = [f for f in fams if f not in _SPECS_OF]
    if unknown or len(set(fams)) != len(fams) or not fams:
        raise ValueError(f"v2 families must be a non-empty subset of {FAMILIES_V2}, got {fams}")
    return tuple(f for f in FAMILIES_V2 if f in fams)  # pinned order


def specs_v2(families: Sequence[str] | None = None) -> list[FeatureSpec]:
    """Registry entries of the chosen v2 families (default all), family
    order pinned by :data:`FAMILIES_V2`."""
    out: list[FeatureSpec] = []
    for fam in _families(families):
        out.extend(_SPECS_OF[fam]())
    return out


def v2_feature_names(families: Sequence[str] | None = None) -> list[str]:
    return [s.name for s in specs_v2(families)]


def v2_registry_dicts(families: Sequence[str] | None = None) -> list[dict]:
    """Canonical JSON entries: the base registry's hash first (the v2 set
    is defined on top of a specific registered set), then the v2 specs."""
    return [{"base_registry_hash": registry_hash()}, *[s.to_dict() for s in specs_v2(families)]]


def v2_registry_hash(families: Sequence[str] | None = None) -> str:
    """sha256 of the canonical JSON of :func:`v2_registry_dicts`."""
    import hashlib
    import json

    blob = json.dumps(v2_registry_dicts(families), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- state


class _Print:
    """One recorded print: shares, signed shares, alive until busted."""

    __slots__ = ("ts", "qty", "signed", "alive")

    def __init__(self, ts: int, qty: int, signed: int) -> None:
        self.ts = ts
        self.qty = qty
        self.signed = signed
        self.alive = True


class _PrintLedger:
    """Per-instrument prints of the session with bust removal (module docs).

    ``vol[w]`` / ``signed[w]`` are the live sums over ``(t - w, t]`` after
    ``trim(t)``; ``session_vol`` the live shares of the session.  A print
    is indexed by ``trade_id`` for the whole session; ``bust(trade_id,
    now)`` kills every live print of that id and subtracts it from the
    session total and from each window it is still inside.
    """

    __slots__ = ("windows", "vol", "signed", "session_vol", "by_id", "_queues")

    def __init__(self, windows: Mapping[str, int]) -> None:
        self.windows = dict(windows)
        self.vol: dict[str, int] = dict.fromkeys(windows, 0)
        self.signed: dict[str, int] = dict.fromkeys(windows, 0)
        self.session_vol = 0
        self.by_id: dict[int, list[_Print]] = {}
        self._queues: dict[str, deque[_Print]] = {w: deque() for w in windows}

    def reset_windows(self) -> None:
        """Clear the windows (a stale->fresh recovery); the session total
        and the id index stay."""
        for w in self.windows:
            self.vol[w] = self.signed[w] = 0
            self._queues[w].clear()

    def add(self, ts: int, qty: int, signed: int, trade_id: int) -> _Print:
        p = _Print(ts, qty, signed)
        for w in self.windows:
            self._queues[w].append(p)
            self.vol[w] += qty
            self.signed[w] += signed
        self.session_vol += qty
        if trade_id:
            self.by_id.setdefault(trade_id, []).append(p)
        self.trim(ts)
        return p

    def trim(self, now: int) -> None:
        for w, w_ns in self.windows.items():
            cutoff = now - w_ns
            q = self._queues[w]
            while q and q[0].ts <= cutoff:
                p = q.popleft()
                if p.alive:
                    self.vol[w] -= p.qty
                    self.signed[w] -= p.signed

    def remove(self, p: _Print, now: int) -> bool:
        """Kill one print as of ``now`` (returns False if already dead)."""
        if not p.alive:
            return False
        p.alive = False
        self.trim(now)
        self.session_vol -= p.qty
        for w, w_ns in self.windows.items():
            if p.ts > now - w_ns:  # still inside (t - w, t]
                self.vol[w] -= p.qty
                self.signed[w] -= p.signed
        return True

    def bust(self, trade_id: int, now: int) -> int:
        """Kill every live print of ``trade_id``; returns how many."""
        n = 0
        for p in self.by_id.get(trade_id, ()):
            if self.remove(p, now):
                n += 1
        return n


@dataclass
class _V2State:
    """Per-instrument v2 state (windows cleared per session / recovery)."""

    anchor: int | None = None
    day: int | None = None
    recoveries: int = 0
    last_logmid: float | None = None

    def __post_init__(self) -> None:
        self.hist = TimeSeries()  # ln mid at mid changes
        self.rv = {w: RollingSum(n, 1) for w, n in WINDOWS_V2_NS.items()}
        self.ext = {w: RollingExtrema(n) for w, n in WINDOWS_V2_NS.items()}
        self.ledger = _PrintLedger(WINDOWS_SHARENORM_NS)

    def reset(self, t: int, new_day: bool) -> None:
        self.anchor = t
        self.last_logmid = None
        self.hist = TimeSeries()
        self.rv = {w: RollingSum(n, 1) for w, n in WINDOWS_V2_NS.items()}
        self.ext = {w: RollingExtrema(n) for w, n in WINDOWS_V2_NS.items()}
        if new_day:
            self.ledger = _PrintLedger(WINDOWS_SHARENORM_NS)
        else:
            self.ledger.reset_windows()

    def warm(self, t: int, w_ns: int) -> bool:
        return self.anchor is not None and t - self.anchor >= w_ns


class FeatureV2Extension:
    """Attach the v2 families to a running engine (module docs, "Opt-in").

    ``on_vector(base_vector, v2_vector)`` is called for every emission;
    the engine's previous ``on_vector`` (if any) still receives the base
    vector first.  ``vectors`` keeps the emitted v2 vectors when
    ``keep=True`` (tests, small fixtures).  ``adv`` is the share
    normaliser of the ``sharenorm`` family: a :class:`SessionAdvTable`
    (per session), a ``{instrument_id: adv}`` map (static) or ``None``
    (every ``*_adv_*`` feature invalid).
    """

    def __init__(
        self,
        engine: FeatureEngine,
        on_vector: Callable[[FeatureVector, FeatureVector], None] | None = None,
        keep: bool = False,
        families: Sequence[str] | None = None,
        adv: SessionAdvTable | Mapping[int, float] | None = None,
    ) -> None:
        self.engine = engine
        self.families = _families(families)
        self.feature_names = v2_feature_names(self.families)
        self.feature_version = v2_registry_hash(self.families)
        self.states: dict[int, _V2State] = {}
        self.vectors: list[FeatureVector] = []
        self.vectors_emitted = 0
        self.session_resets = 0
        #: prints recorded / busts that removed at least one print / busts
        #: naming no recorded print
        self.prints_recorded = 0
        self.busts_applied = 0
        self.busts_unmatched = 0
        self._adv_table: SessionAdvTable | None = None
        self._adv_static: Mapping[int, float] | None = None
        if isinstance(adv, SessionAdvTable):
            self._adv_table = adv
        elif adv is not None:
            self._adv_static = {int(k): float(v) for k, v in adv.items()}
        self._keep = keep
        self._user_cb = on_vector
        self._inner_cb = engine.on_vector
        self._inner_apply = engine.apply
        self._inner_cross = engine.on_cross_print
        self._own_apply = "apply" in vars(engine)
        self._own_cross = "on_cross_print" in vars(engine)
        engine.on_vector = self._hook
        engine.apply = self._apply_hook  # type: ignore[method-assign]
        engine.on_cross_print = self._cross_hook  # type: ignore[method-assign]

    def detach(self) -> None:
        """Restore the engine's previous ``on_vector``, ``apply`` and
        ``on_cross_print``."""
        eng = self.engine
        eng.on_vector = self._inner_cb
        if self._own_apply:
            eng.apply = self._inner_apply  # type: ignore[method-assign]
        elif "apply" in vars(eng):
            del eng.apply
        if self._own_cross:
            eng.on_cross_print = self._inner_cross  # type: ignore[method-assign]
        elif "on_cross_print" in vars(eng):
            del eng.on_cross_print

    # ----------------------------------------------------------- stream hooks

    def _apply_hook(self, ev: MarketEvent, group_end: bool = True) -> FeatureVector | None:
        """See the prints and the busts the engine is handed (module docs)."""
        et = ev.event_type
        if et == EventType.TRADE_BUST:
            self._on_bust(ev)
            return self._inner_apply(ev, group_end)
        if et != EventType.TRADE:
            return self._inner_apply(ev, group_end)
        p = self._on_trade(ev)
        before = self.engine.events_dropped
        vec = self._inner_apply(ev, group_end)
        if p is not None and self.engine.events_dropped != before:
            # the book dropped / held it after all: it was never on the tape
            self._state_of(ev.instrument_id).ledger.remove(p, ev.exchange_ts)
            self.prints_recorded -= 1
        return vec

    def _on_trade(self, ev: MarketEvent) -> _Print | None:
        """Record a print BEFORE the engine folds it (the emission the
        print itself closes must see it), under the engine's own bounds:
        an exchange_ts regression, an oversized quantity, a duplicate
        sequence or an invalid payload contributes nothing."""
        if ev.qty <= 0 or ev.qty > FEATURE_MAX_QTY or ev.price_ticks <= 0 or ev.side > 1:
            return None
        st = self.engine.states.get(ev.instrument_id)
        if st is not None:
            if st.first_ts is not None and ev.exchange_ts < st.last_ts:
                return None
            book = st.cons.books.get(ev.venue_id)
            if book is not None and book.has_sequence and ev.sequence <= book.last_sequence:
                return None
        xs = self._state_of(ev.instrument_id)
        signed = ev.qty if ev.side == 0 else -ev.qty
        self.prints_recorded += 1
        return xs.ledger.add(ev.exchange_ts, ev.qty, signed, ev.trade_id)

    def _on_bust(self, ev: MarketEvent) -> None:
        xs = self.states.get(ev.instrument_id)
        st = self.engine.states.get(ev.instrument_id)
        regressed = st is not None and st.first_ts is not None and ev.exchange_ts < st.last_ts
        n = 0 if xs is None or regressed else xs.ledger.bust(ev.trade_id, ev.exchange_ts)
        if n:
            self.busts_applied += 1
        else:
            self.busts_unmatched += 1

    def _cross_hook(self, instrument_id: int, ts: int, qty: int) -> bool:
        folded = self._inner_cross(instrument_id, ts, qty)
        if folded:
            self._state_of(instrument_id).ledger.add(ts, qty, 0, 0)
            self.prints_recorded += 1
        return folded

    def _state_of(self, instrument_id: int) -> _V2State:
        xs = self.states.get(instrument_id)
        if xs is None:
            xs = self.states[instrument_id] = _V2State()
        return xs

    def _adv_of(self, instrument_id: int, t: int) -> float | None:
        if self._adv_table is not None:
            return self._adv_table.adv(instrument_id, t)
        if self._adv_static is not None:
            return self._adv_static.get(instrument_id)
        return None

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
        xs = self._state_of(instrument_id)
        day = (t + st.ctx.clock.offset_seconds(t) * NS_PER_SEC) // _NS_DAY
        if xs.anchor is None or day != xs.day or st.recoveries != xs.recoveries:
            if xs.anchor is not None:
                self.session_resets += 1
            xs.reset(t, new_day=xs.day != day)
            xs.day = day
            xs.recoveries = st.recoveries
        for win in xs.rv.values():
            win.trim(t)
        for ext in xs.ext.values():
            ext.trim(t)
        xs.hist.trim(t - WINDOWS_V2_NS["1h"] - NS_PER_SEC)
        xs.ledger.trim(t)
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
        for fam in self.families:
            if fam == FAMILY_V2:
                self._longwin(st, xs, t, ok, values, valid)
            else:
                self._sharenorm(st, xs, instrument_id, t, ok, values, valid)
        return FeatureVector(
            instrument_id=instrument_id,
            timestamp=t,
            feature_version=self.feature_version,
            values=values,
            validity=valid,
        )

    @staticmethod
    def _longwin(
        st: Any, xs: _V2State, t: int, ok: bool, values: list[float], valid: list[bool]
    ) -> None:
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

    def _sharenorm(
        self,
        st: Any,
        xs: _V2State,
        instrument_id: int,
        t: int,
        ok: bool,
        values: list[float],
        valid: list[bool],
    ) -> None:
        adv = self._adv_of(instrument_id, t)
        if adv is not None and not adv > 0.0:
            adv = None
        led = xs.ledger
        sess = led.session_vol
        _put(values, valid, sess / adv if adv is not None else None)
        for w, w_ns in WINDOWS_SHARENORM_NS.items():
            warm = xs.warm(t, w_ns)
            vol = led.vol[w]
            sgn = led.signed[w]
            _put(values, valid, vol / adv if warm and adv is not None else None)
            _put(values, valid, sgn / adv if warm and adv is not None else None)
            _put(values, valid, vol / sess if warm and sess > 0 else None)
        for d_sum, d_diff in (
            (st.bid_q + st.ask_q, st.bid_q - st.ask_q),
            (st.db10 + st.da10, st.db10 - st.da10),
        ):
            _put(values, valid, d_sum / adv if ok and adv is not None else None)
            _put(values, valid, d_diff / adv if ok and adv is not None else None)


def _put(values: list[float], valid: list[bool], v: float | None) -> None:
    if v is None or v != v:
        values.append(float("nan"))
        valid.append(False)
    else:
        values.append(float(v))
        valid.append(True)


def v2_frame(vectors: list[FeatureVector], families: Sequence[str] | None = None) -> pd.DataFrame:
    """A per-instrument v2 feature frame (``exchange_ts`` + one float64
    column per v2 feature, NaN where invalid), from one instrument's
    emitted v2 vectors in emission order (``families`` as the extension
    was built with)."""
    names = v2_feature_names(families)
    cols: dict[str, Any] = {"exchange_ts": [v.timestamp for v in vectors]}
    for j, name in enumerate(names):
        cols[name] = [v.values[j] if v.validity[j] else float("nan") for v in vectors]
    return pd.DataFrame(cols)
