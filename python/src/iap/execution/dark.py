"""Dark / midpoint venue model (v1.17 WS-B item B10, Python reference, opt-in).

A venue with ``VenueSpec.venue_type`` ``"dark"`` or ``"midpoint"`` has no
displayed book in the replayed stream, so nothing about a fill there can be
replayed: every dark fill is **modelled, not measured**, and every document
and report this module produces carries that label. No free dark-pool data
exists; the proxy is the Nasdaq non-displayed print (ITCH ``P``, the
``hidden_print`` of simulator rule 4) that printed INSIDE the displayed
spread.

**Fill model** (:class:`DarkFillModel`). The probability that a midpoint
order of ``qty`` shares, posted when the displayed spread is
``spread_ticks`` and with ``queue_ahead`` of our own earlier dark shares
ahead of it, fills within ``horizon_ns`` once ``lit_volume`` shares have
traded on the displayed book::

    p = sigmoid(b0 + b1 * log1p(lit_volume / 100) + b2 * spread_ticks
                   + b3 * log1p((qty + queue_ahead) / 100))

fitted by plain Newton logistic regression (:func:`fit_logistic`) on the
proxy samples of :func:`hidden_print_samples`: at every ``step_ns`` of a
session, for each probe size ``q``, the label is "the inside-spread hidden
volume of the next ``horizon_ns`` reached ``q``", the lit-volume feature is
the displayed EXECUTE volume of the PREVIOUS ``horizon_ns`` (causal), the
spread is the displayed spread at the probe time. Only the fitted
coefficients leave the fit; the document (:meth:`DarkFillModel.to_dict`)
names the proxy, the sessions, the sample counts and the label.

**Replay rule** (:mod:`iap.execution.replay`, ``ParentOrder.dark_first``).
A low-urgency parent posts each step dark first: a B2 ``urgency_policy=
"alpha"`` parent when the regime of the decision is in
``DarkFirstParams.regimes`` (default: ``agree`` — the signal favours
waiting), a plain parent when ``ParentOrder.urgency <= max_urgency``. The
dark child draws one uniform ``u`` (its own SplitMix64 stream, seeded from
``ExecConfig.seed`` so nothing lit moves) and fills at the FIRST event
inside its horizon where ``p_fill(lit volume since the post, spread at the
post, qty + own dark queue) >= u`` — the inverse-CDF realisation of the
fitted horizon probability, driven by the lit tape. The fill price is the
consolidated displayed midpoint at that event, rounded AGAINST us on a
half tick (buy up, sell down); the fee is the dark venue's
``taker_fee_per_share`` on both sides; ``Fill.provenance`` is
``"dark_midpoint"`` and ``Liquidity.MAKER``. A child whose horizon expires
unfilled is cancelled and its remainder goes to the lit book in the
parent's base child style at once.

**Pre-trade controls** (:class:`DarkControls`, :func:`dark_pretrade_check`).
Three controls gate every dark post, in this order, first failure wins:
``venue_type`` (the venue's type must be in ``allowed_venue_types``),
``participation`` (``qty <= max_participation * trailing displayed volume
over participation_window_ns``; ``0`` = off), ``notional`` (the parent's
dark-filled notional plus this child's at the current mid must not exceed
``max_dark_notional``; ``0`` = off). A rejected post goes to the lit path
at once and is counted per code. Per plan row F8 these controls are
Rust-owned: this module is the Python mirror of the risk-engine interface
and the golden vectors of :data:`DARK_CONTROL_GOLDENS` are the ones the
Rust engine must reproduce (``docs/research/B10_F8_HANDOVER.md``).

Every function here is plain scalar arithmetic and loops (the C++ port,
F7, keeps the shape); numpy is not used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from iap.core.events import EventType, MarketEvent

#: ``VenueSpec.venue_type`` values. ``lit`` (the default) is a displayed
#: book replayed from the stream; ``dark`` and ``midpoint`` have no
#: displayed book and fill only through :class:`DarkFillModel`.
VENUE_TYPES: tuple[str, ...] = ("lit", "dark", "midpoint")
#: the venue types the dark-first rule may post to
DARK_VENUE_TYPES: tuple[str, ...] = ("dark", "midpoint")
#: the label every dark-fill document and report carries
MODELLED_LABEL = "modelled, not measured"
#: the proxy definition, verbatim in every document
PROXY_DEFINITION = (
    "Nasdaq ITCH non-displayed prints ('P' TRADE messages that are not the print of the "
    "EXECUTE immediately before them on the same venue book at the same exchange_ts) whose "
    "price lies strictly inside the displayed best bid / ask at the time of the print; the "
    "label of a probe (t, q) is 'inside-spread hidden volume in [t, t + horizon) >= q'; the "
    "lit-volume feature is the displayed EXECUTE volume in [t - horizon, t); no dark-pool "
    "data was used (none is free)"
)
#: ``dark_pretrade_check`` results (``""`` = accepted); the order is the
#: evaluation order (first failure wins).
DARK_CONTROL_CODES: tuple[str, ...] = ("venue_type", "participation", "notional")
#: ``ChildOrder.dark_outcome`` values of a dark child.
DARK_OUTCOMES: tuple[str, ...] = ("filled", "expired")
#: order and fill ids of dark children live above every simulator id
DARK_ID_BASE = 1 << 40
#: the model's feature names, in coefficient order
FEATURES: tuple[str, ...] = ("intercept", "log1p_lit_volume_100", "spread_ticks", "log1p_qty_100")


def sigmoid(x: float) -> float:
    """Numerically safe logistic function."""
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def dark_features(
    lit_volume: int, spread_ticks: int, qty: int, queue_ahead: int = 0
) -> list[float]:
    """The model's regressors (module docstring), intercept first."""
    if lit_volume < 0 or spread_ticks < 0 or qty <= 0 or queue_ahead < 0:
        raise ValueError("dark_features: lit_volume / spread / queue >= 0 and qty > 0")
    return [
        1.0,
        math.log1p(lit_volume / 100.0),
        float(spread_ticks),
        math.log1p((qty + queue_ahead) / 100.0),
    ]


@dataclass(frozen=True, slots=True)
class DarkFillModel:
    """Logistic midpoint fill probability over one horizon (module docstring).

    ``coef`` follows :data:`FEATURES`. ``fitted`` is False for the
    documented prior (``prior()``), which exists so a replay can run
    without a calibration file; a report must name which was used.
    """

    coef: tuple[float, float, float, float]
    horizon_ns: int
    fitted: bool = False
    n_samples: int = 0
    n_positive: int = 0
    sessions: tuple[str, ...] = ()
    instruments: tuple[int, ...] = ()
    probe_sizes: tuple[int, ...] = ()
    step_ns: int = 0
    log_likelihood: float = 0.0
    n_hidden_prints: int = 0
    n_hidden_inside_spread: int = 0
    source: str = ""

    def __post_init__(self) -> None:
        if len(self.coef) != 4:
            raise ValueError("DarkFillModel.coef needs 4 coefficients")
        if self.horizon_ns <= 0:
            raise ValueError("DarkFillModel.horizon_ns must be > 0")

    @property
    def label(self) -> str:
        return MODELLED_LABEL

    def p_fill(self, lit_volume: int, spread_ticks: int, qty: int, queue_ahead: int = 0) -> float:
        """``P(fill within horizon | lit_volume, spread, qty + queue_ahead)``."""
        x = dark_features(lit_volume, spread_ticks, qty, queue_ahead)
        z = 0.0
        for i in range(4):
            z += self.coef[i] * x[i]
        return sigmoid(z)

    @classmethod
    def prior(cls, horizon_ns: int = 30_000_000_000) -> DarkFillModel:
        """The documented prior, NOT fitted: p = 0.5 at 1,000 lit shares, a
        1-tick spread and 100 shares; more lit volume helps, a wider spread
        and a larger order hurt. For smoke runs and tests only."""
        return cls(
            coef=(-1.42, 0.8, -0.15, -0.5),
            horizon_ns=horizon_ns,
            fitted=False,
            source="prior (not fitted)",
        )

    def to_dict(self) -> dict[str, object]:
        """The calibration document; ``label`` is always the modelled label."""
        return {
            "label": MODELLED_LABEL,
            "model": "logistic midpoint fill probability within horizon (iap.execution.dark)",
            "proxy": PROXY_DEFINITION,
            "features": list(FEATURES),
            "coef": [float(c) for c in self.coef],
            "horizon_ns": self.horizon_ns,
            "fitted": self.fitted,
            "source": self.source,
            "n_samples": self.n_samples,
            "n_positive": self.n_positive,
            "sessions": list(self.sessions),
            "instruments": list(self.instruments),
            "probe_sizes": list(self.probe_sizes),
            "step_ns": self.step_ns,
            "log_likelihood": self.log_likelihood,
            "n_hidden_prints": self.n_hidden_prints,
            "n_hidden_inside_spread": self.n_hidden_inside_spread,
        }

    @classmethod
    def from_dict(cls, doc: dict) -> DarkFillModel:
        if doc.get("label") != MODELLED_LABEL:
            raise ValueError(f"dark fill model document must carry label {MODELLED_LABEL!r}")
        coef = doc["coef"]
        return cls(
            coef=(float(coef[0]), float(coef[1]), float(coef[2]), float(coef[3])),
            horizon_ns=int(doc["horizon_ns"]),
            fitted=bool(doc.get("fitted", False)),
            n_samples=int(doc.get("n_samples", 0)),
            n_positive=int(doc.get("n_positive", 0)),
            sessions=tuple(str(s) for s in doc.get("sessions", ())),
            instruments=tuple(int(i) for i in doc.get("instruments", ())),
            probe_sizes=tuple(int(q) for q in doc.get("probe_sizes", ())),
            step_ns=int(doc.get("step_ns", 0)),
            log_likelihood=float(doc.get("log_likelihood", 0.0)),
            n_hidden_prints=int(doc.get("n_hidden_prints", 0)),
            n_hidden_inside_spread=int(doc.get("n_hidden_inside_spread", 0)),
            source=str(doc.get("source", "")),
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> DarkFillModel:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ----------------------------------------------------------------- fit --


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (small dense systems)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = c
        for r in range(c + 1, n):
            if abs(m[r][c]) > abs(m[piv][c]):
                piv = r
        m[c], m[piv] = m[piv], m[c]
        if abs(m[c][c]) < 1e-300:
            raise ValueError("singular normal equations")
        for r in range(n):
            if r == c:
                continue
            f = m[r][c] / m[c][c]
            if f == 0.0:
                continue
            for k in range(c, n + 1):
                m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit_logistic(
    xs: Sequence[Sequence[float]],
    ys: Sequence[int],
    *,
    ridge: float = 1e-6,
    max_iter: int = 50,
    tol: float = 1e-9,
) -> tuple[list[float], float, int]:
    """Newton-Raphson logistic regression; ``(coef, log_likelihood, iterations)``.

    ``ridge`` is a tiny L2 penalty (not on the intercept) that keeps the
    normal equations invertible on separable fixtures. Plain loops.
    """
    n = len(xs)
    if n == 0 or n != len(ys):
        raise ValueError("fit_logistic needs non-empty xs / ys of equal length")
    k = len(xs[0])
    coef = [0.0] * k
    ll_prev = -math.inf
    ll = 0.0
    it = 0
    while it < max_iter:
        it += 1
        grad = [0.0] * k
        hess = [[0.0] * k for _ in range(k)]
        ll = 0.0
        for x, y in zip(xs, ys, strict=True):
            z = 0.0
            for j in range(k):
                z += coef[j] * x[j]
            p = sigmoid(z)
            ll += math.log(p) if y else math.log(1.0 - p) if p < 1.0 else -745.0
            r = (1.0 if y else 0.0) - p
            w = p * (1.0 - p)
            for j in range(k):
                grad[j] += r * x[j]
                xjw = x[j] * w
                for m in range(k):
                    hess[j][m] += xjw * x[m]
        for j in range(1, k):
            grad[j] -= ridge * coef[j]
            hess[j][j] += ridge
        step = _solve(hess, grad)
        for j in range(k):
            coef[j] += step[j]
        if abs(ll - ll_prev) < tol * max(1.0, abs(ll)):
            break
        ll_prev = ll
    return coef, ll, it


# ------------------------------------------------------------- sampler --


@dataclass(slots=True)
class _Pending:
    t0: int
    spread_ticks: int
    lit_prior: int
    hidden_qty: int = 0


@dataclass(slots=True)
class _InstState:
    bid: int = 0
    ask: int = 0
    next_probe: int = 0
    execs: list[tuple[int, int]] | None = None  #: (ts, qty) of EXECUTEs, trailing
    exec_head: int = 0
    exec_sum: int = 0
    pending: list[_Pending] | None = None
    last_kind: dict[int, tuple[int, int, int]] | None = None


@dataclass(slots=True)
class HiddenPrintSamples:
    """Output of :func:`hidden_print_samples`."""

    xs: list[list[float]]
    ys: list[int]
    n_probes: int = 0
    n_hidden_prints: int = 0
    n_hidden_inside_spread: int = 0
    instruments: list[int] | None = None


def hidden_print_samples(
    events: Iterable[MarketEvent],
    *,
    horizon_ns: int,
    step_ns: int,
    probe_sizes: Sequence[int],
    instrument_ids: Sequence[int] | None = None,
    reorder_window: int = 0,
) -> HiddenPrintSamples:
    """Proxy samples from one session's stream (module docstring).

    Streams the events once (a book per instrument, O(1) state per probe),
    so a whole session fits in a few MB whatever its size. Probes are taken
    every ``step_ns`` while the consolidated book is two-sided and not
    crossed, from the first two-sided state; a probe's window must end
    before the stream does to be labelled.
    """
    from iap.orderbook.book import ConsolidatedBook

    if horizon_ns <= 0 or step_ns <= 0 or not probe_sizes:
        raise ValueError("hidden_print_samples: horizon_ns, step_ns > 0 and probe_sizes non-empty")
    want = None if instrument_ids is None else set(instrument_ids)
    books: dict[int, ConsolidatedBook] = {}
    st: dict[int, _InstState] = {}
    out = HiddenPrintSamples([], [], instruments=[])
    sizes = sorted(int(q) for q in probe_sizes)

    def finalize(s: _InstState, upto: int) -> None:
        keep: list[_Pending] = []
        pend = s.pending
        assert pend is not None
        for pe in pend:
            if pe.t0 + horizon_ns <= upto:
                for q in sizes:
                    out.xs.append(dark_features(pe.lit_prior, pe.spread_ticks, q))
                    out.ys.append(1 if pe.hidden_qty >= q else 0)
                out.n_probes += 1
            else:
                keep.append(pe)
        s.pending = keep

    for ev in events:
        iid = ev.instrument_id
        if want is not None and iid not in want:
            continue
        s = st.get(iid)
        if s is None:
            s = _InstState(execs=[], pending=[], last_kind={})
            st[iid] = s
            books[iid] = ConsolidatedBook(iid, reorder_window)
            assert out.instruments is not None
            out.instruments.append(iid)
        t = ev.exchange_ts
        et = ev.event_type
        assert s.last_kind is not None and s.execs is not None and s.pending is not None
        # --- hidden print test on the PRE-event state (simulator rule 4, X-P2-7)
        hidden = False
        if et == EventType.TRADE:
            prev = s.last_kind.get(ev.venue_id)
            is_print_of_prev = (
                prev is not None
                and prev[0] == t
                and prev[1] == int(EventType.EXECUTE)
                and (prev[2] == 0 or ev.trade_id == 0 or prev[2] == ev.trade_id)
            )
            hidden = not is_print_of_prev
        if et in (EventType.TRADE, EventType.EXECUTE):
            s.last_kind[ev.venue_id] = (t, int(et), ev.trade_id)
        if hidden:
            out.n_hidden_prints += 1
            if s.bid > 0 and s.ask > 0 and s.bid < ev.price_ticks < s.ask and ev.qty > 0:
                out.n_hidden_inside_spread += 1
                for pe in s.pending:
                    if pe.t0 <= t < pe.t0 + horizon_ns:
                        pe.hidden_qty += ev.qty
        # --- displayed volume
        if et == EventType.EXECUTE and ev.qty > 0:
            s.execs.append((t, ev.qty))
            s.exec_sum += ev.qty
        execs = s.execs
        while s.exec_head < len(execs) and execs[s.exec_head][0] < t - horizon_ns:
            s.exec_sum -= execs[s.exec_head][1]
            s.exec_head += 1
        if s.exec_head > 4096 and s.exec_head * 2 > len(execs):
            del execs[: s.exec_head]
            s.exec_head = 0
        # --- apply, then refresh the touch
        books[iid].apply(ev)
        bb = books[iid].best_bid()
        ba = books[iid].best_ask()
        s.bid = 0 if bb is None else bb[0]
        s.ask = 0 if ba is None else ba[0]
        # --- probes
        finalize(s, t)
        if s.bid > 0 and s.ask > s.bid:
            if s.next_probe == 0:
                s.next_probe = t
            while s.next_probe <= t:
                s.pending.append(_Pending(s.next_probe, s.ask - s.bid, s.exec_sum))
                s.next_probe += step_ns
    return out


def calibrate_dark_model(
    sessions: Sequence[tuple[str, Iterable[MarketEvent]]],
    *,
    horizon_ns: int,
    step_ns: int,
    probe_sizes: Sequence[int],
    instrument_ids: Sequence[int] | None = None,
    source: str = "",
) -> DarkFillModel:
    """Fit a :class:`DarkFillModel` on the hidden-print proxy of ``sessions``
    (``(label, events)`` pairs, streamed one at a time)."""
    xs: list[list[float]] = []
    ys: list[int] = []
    labels: list[str] = []
    inst: set[int] = set()
    n_hidden = 0
    n_inside = 0
    for label, events in sessions:
        smp = hidden_print_samples(
            events,
            horizon_ns=horizon_ns,
            step_ns=step_ns,
            probe_sizes=probe_sizes,
            instrument_ids=instrument_ids,
        )
        xs.extend(smp.xs)
        ys.extend(smp.ys)
        labels.append(label)
        inst.update(smp.instruments or ())
        n_hidden += smp.n_hidden_prints
        n_inside += smp.n_hidden_inside_spread
    if not xs:
        raise ValueError("calibrate_dark_model: no proxy samples (no two-sided book?)")
    pos = 0
    for y in ys:
        pos += y
    if pos == 0 or pos == len(ys):
        raise ValueError("calibrate_dark_model: labels are all equal; the fit is undefined")
    coef, ll, _ = fit_logistic(xs, ys)
    return DarkFillModel(
        coef=(coef[0], coef[1], coef[2], coef[3]),
        horizon_ns=horizon_ns,
        fitted=True,
        n_samples=len(ys),
        n_positive=pos,
        sessions=tuple(labels),
        instruments=tuple(sorted(inst)),
        probe_sizes=tuple(sorted(int(q) for q in probe_sizes)),
        step_ns=step_ns,
        log_likelihood=ll,
        n_hidden_prints=n_hidden,
        n_hidden_inside_spread=n_inside,
        source=source,
    )


# ------------------------------------------------------------ controls --


@dataclass(frozen=True, slots=True)
class DarkControls:
    """Pre-trade controls of the dark-first rule (module docstring; Rust-owned
    per F8, mirrored here)."""

    #: ``qty <= max_participation * trailing displayed volume``; ``0`` = off
    max_participation: float = 0.0
    #: the trailing window of the participation cap
    participation_window_ns: int = 30_000_000_000
    #: venue types a dark post may go to
    allowed_venue_types: tuple[str, ...] = DARK_VENUE_TYPES
    #: cap on a parent's cumulative dark-filled notional incl. this child; ``0`` = off
    max_dark_notional: float = 0.0

    def __post_init__(self) -> None:
        if self.max_participation < 0.0 or self.max_dark_notional < 0.0:
            raise ValueError("DarkControls: max_participation / max_dark_notional must be >= 0")
        if self.participation_window_ns <= 0:
            raise ValueError("DarkControls.participation_window_ns must be > 0")
        for vt in self.allowed_venue_types:
            if vt not in VENUE_TYPES:
                raise ValueError(f"DarkControls.allowed_venue_types: unknown type {vt!r}")

    def to_dict(self) -> dict[str, object]:
        return {
            "max_participation": self.max_participation,
            "participation_window_ns": self.participation_window_ns,
            "allowed_venue_types": list(self.allowed_venue_types),
            "max_dark_notional": self.max_dark_notional,
        }


def dark_pretrade_check(
    controls: DarkControls,
    venue_type: str,
    qty: int,
    trailing_lit_volume: int,
    dark_notional_so_far: float,
    child_notional: float,
) -> str:
    """``""`` when the post may go, else the first failing
    :data:`DARK_CONTROL_CODES` entry. Pure; the Rust engine reproduces it.

    - ``venue_type``: ``venue_type not in allowed_venue_types``;
    - ``participation``: with ``max_participation > 0``,
      ``qty > max_participation * trailing_lit_volume`` (float compare, so a
      zero trailing volume rejects every positive qty);
    - ``notional``: with ``max_dark_notional > 0``,
      ``dark_notional_so_far + child_notional > max_dark_notional``.
    """
    if qty <= 0:
        raise ValueError("dark_pretrade_check: qty must be > 0")
    if venue_type not in controls.allowed_venue_types:
        return "venue_type"
    if controls.max_participation > 0.0:
        if float(qty) > controls.max_participation * float(trailing_lit_volume):
            return "participation"
    if controls.max_dark_notional > 0.0:
        if dark_notional_so_far + child_notional > controls.max_dark_notional:
            return "notional"
    return ""


#: Golden vectors the Rust risk engine (F8) must reproduce:
#: ``(controls kwargs, venue_type, qty, trailing_lit_volume, dark_notional_so_far,
#: child_notional) -> expected``. Pinned by ``test_v117_dark_venue.py``.
DARK_CONTROL_GOLDENS: tuple[tuple[dict, str, int, int, float, float, str], ...] = (
    ({}, "dark", 100, 0, 0.0, 0.0, ""),
    ({}, "lit", 100, 0, 0.0, 0.0, "venue_type"),
    ({"allowed_venue_types": ("midpoint",)}, "dark", 100, 0, 0.0, 0.0, "venue_type"),
    ({"max_participation": 0.1}, "dark", 100, 1000, 0.0, 0.0, ""),
    ({"max_participation": 0.1}, "dark", 101, 1000, 0.0, 0.0, "participation"),
    ({"max_participation": 0.1}, "midpoint", 1, 0, 0.0, 0.0, "participation"),
    ({"max_dark_notional": 10_000.0}, "dark", 100, 0, 9_000.0, 1_000.0, ""),
    ({"max_dark_notional": 10_000.0}, "dark", 100, 0, 9_000.0, 1_000.01, "notional"),
    (
        {"max_participation": 0.1, "max_dark_notional": 10_000.0},
        "dark",
        500,
        1000,
        9_999.0,
        5.0,
        "participation",
    ),
    (
        {"max_participation": 0.5, "max_dark_notional": 10_000.0},
        "lit",
        500,
        1000,
        9_999.0,
        5.0,
        "venue_type",
    ),
)


@dataclass(frozen=True, slots=True)
class DarkFirstParams:
    """``ParentOrder.dark_first``: post each step dark first (module docstring)."""

    model: DarkFillModel
    #: the dark venue; ``0`` = the first non-lit venue of the config
    venue_id: int = 0
    controls: DarkControls = DarkControls()
    #: a plain parent is low-urgency when ``ParentOrder.urgency <= max_urgency``
    max_urgency: float = 0.5
    #: a B2 alpha-urgency parent posts dark when its decision regime is one of these
    regimes: tuple[str, ...] = ("agree",)
    #: rest horizon of a dark child; ``0`` = the model's horizon
    horizon_ns: int = 0

    def __post_init__(self) -> None:
        if not (0.0 <= self.max_urgency <= 1.0):
            raise ValueError("DarkFirstParams.max_urgency must be in [0, 1]")
        if self.horizon_ns < 0 or self.venue_id < 0:
            raise ValueError("DarkFirstParams.horizon_ns / venue_id must be >= 0")

    @property
    def rest_ns(self) -> int:
        return self.horizon_ns if self.horizon_ns > 0 else self.model.horizon_ns


def midpoint_ticks(bid_ticks: int, ask_ticks: int, side: int) -> int:
    """The displayed midpoint in ticks, a half tick rounded AGAINST us
    (buy up, sell down)."""
    s = bid_ticks + ask_ticks
    if s % 2 == 0:
        return s // 2
    return s // 2 + 1 if side == 0 else s // 2


# ----------------------------------------------------------------- CLI --


def _fingerprint(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:16]


def main(argv: list[str] | None = None) -> int:
    """``python -m iap.execution.dark --data-dir data/real/multi7 --out <json>``:
    fit the hidden-print proxy model on every ``eq_*.normalized.iap1`` of a
    dataset (streamed), write the document and print its headline."""
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--data-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--sessions", default="", help="comma list of session labels (YYYYMMDD)")
    ap.add_argument("--instruments", default="", help="comma list of instrument ids")
    ap.add_argument("--horizon-s", type=float, default=30.0)
    ap.add_argument("--step-s", type=float, default=5.0)
    ap.add_argument("--probe-sizes", default="100,300,1000,3000")
    ap.add_argument("--max-events", type=int, default=0, help="per session (smoke runs)")
    args = ap.parse_args(argv)

    from iap.core.codec import iter_iap1, read_jsonl

    sub = args.data_dir / "normalized"
    if not sub.is_dir():
        sub = args.data_dir
    files = sorted(sub.glob("eq_*.normalized.iap1")) or sorted(sub.glob("eq_*.normalized.jsonl"))
    if not files:
        raise SystemExit(f"no eq_*.normalized.{{iap1,jsonl}} under {args.data_dir}")
    want = [s for s in args.sessions.split(",") if s]
    files = [p for p in files if not want or any(s in p.name for s in want)]
    if not files:
        raise SystemExit(f"no session matches --sessions {args.sessions}")
    inst = [int(x) for x in args.instruments.split(",") if x]

    def events_of(path: Path) -> Iterable[MarketEvent]:
        it: Iterable[MarketEvent]
        it = iter_iap1(path) if path.suffix == ".iap1" else read_jsonl(path)
        if args.max_events > 0:
            n = args.max_events

            def capped() -> Iterable[MarketEvent]:
                for i, ev in enumerate(it):
                    if i >= n:
                        return
                    yield ev

            return capped()
        return it

    sessions = [(p.name.split(".")[0].removeprefix("eq_"), events_of(p)) for p in files]
    model = calibrate_dark_model(
        sessions,
        horizon_ns=int(args.horizon_s * 1e9),
        step_ns=int(args.step_s * 1e9),
        probe_sizes=[int(q) for q in args.probe_sizes.split(",") if q],
        instrument_ids=inst or None,
        source=f"{args.data_dir.name}: {', '.join(p.name for p in files)}"
        + (f" (first {args.max_events} events per session)" if args.max_events else ""),
    )
    doc = model.to_dict()
    doc["fingerprint"] = _fingerprint(doc)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(
        f"{MODELLED_LABEL}: coef={[round(c, 4) for c in model.coef]} n={model.n_samples} "
        f"positive={model.n_positive} hidden_prints={model.n_hidden_prints} "
        f"inside_spread={model.n_hidden_inside_spread} -> {args.out}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
