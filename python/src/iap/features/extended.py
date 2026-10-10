"""Extended (opt-in) feature set and event-time sampling (v1.12.0, M6).

The default registry (205 features, :mod:`iap.features.registry`) is pinned:
its content hash is the ``feature_version`` of every published store and the
golden vectors.  This module adds features **only** behind an explicit opt-in
(``python -m iap.features --feature-set extended``), so the default registry,
its hash and every published number are untouched.  The extended features are
Python-only (they are not among the 45 cross-language native slots) and are
appended AFTER the default registry: an extended row is the default row plus
these values, and its ``feature_version`` is the hash of the concatenated
registry (:func:`extended_registry_hash`).

Families (pinned order :data:`EXT_FAMILY_ORDER`; each name ends ``_v1``):

``xqueue`` — queue time-to-depletion at the best bid / ask
    ``qttd_{bid,ask}_w{w}_v1`` = Q_L1 / (dep_rate_w) seconds, capped at
    ``ttd_cap_s``; dep_rate_w = the summed L1 queue DECREMENTS over w (the
    engine's per-refresh L1 queue deltas: cancels, executions and the level
    vanishing) / w_seconds.  A zero depletion rate gives the cap (an
    undepleted queue is "at least the cap", not undefined).  Valid: book_ok
    and the window has elapsed.

``xsign`` — trade-sign autocorrelation
    ``sign_acf_l{k}_n{n}_v1`` = sample autocorrelation at lag k of the last n
    trade signs (+1 buyer-initiated, -1 seller-initiated, from the TRADE
    aggressor side; ITCH hidden prints carry the ingest's tick-rule
    aggressor, i.e. a Lee-Ready-style classification): with mean m,
    sum_{t>=k}(s_t-m)(s_{t-k}-m) / sum_t (s_t-m)^2.  Valid: n signs seen
    since the warmup anchor and non-zero variance.

``xhawkes`` — exponential-kernel Hawkes intensity of trade arrivals
    lambda_s(t) = mu + alpha * sum_{t_i <= t, side s} exp(-beta (t - t_i)),
    updated online (S <- S exp(-beta dt) + alpha at each trade; decayed to the
    emission time).  ``hawkes_{buy,sell,total}_b{beta}_v1`` (events/s; total =
    buy + sell, with 2*mu) and ``hawkes_imb_b{beta}_v1`` = (buy - sell) /
    (buy + sell) (valid when the total is > EPS).  Parameters are fixed per
    run (:class:`ExtendedConfig`, recorded in the registry params, so a
    different parameterisation is a different ``feature_version``);
    :func:`fit_hawkes_exp` is the offline MLE helper.  Valid once 5/beta
    seconds have elapsed since the warmup anchor (kernel memory >= 99%).

``xhidden`` — odd-lot and non-displayed liquidity (1m window)
    ``oddlot_share_w1m_v1`` = traded qty in TRADEs with qty < ``odd_lot``
    shares / total traded qty; valid only for equities / ETFs (lots have no
    meaning for FX) with traded volume > 0.
    ``hidden_share_w1m_v1`` = 1 - EXECUTE qty / TRADE qty, clipped to [0, 1]:
    an ITCH-style source reports every displayed fill as EXECUTE (+ TRADE)
    and a non-displayed fill as TRADE only.  Degrades gracefully: valid only
    once the instrument's source has reported at least one EXECUTE (sources
    without order-level executions — quote-only FX, consolidated prints —
    leave it INVALID rather than claiming 100% hidden) and traded qty > 0.

All rolling state is cleared on a stale->fresh recovery exactly like the
default engine (API_FEATURES §2.1) and only APPLIED events are folded in.
Every value flows through :func:`iap.features._famutil.put`, so NaN is never
valid.  The computation is causal: a row at time t reads only events with
exchange_ts <= t (tested by truncation).

Event-time sampling (:class:`ExtendedFeatureEngine` ``sampling=``): instead of
the clock cadence, emit a row every ``n`` applied events (``"events"``) or
every time ``n`` more shares have traded (``"volume"`` — TRADE qty, a volume
clock).  Works with either feature set.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from math import exp, log
from pathlib import Path

from iap.core.events import EventType, MarketEvent
from iap.features._famutil import put
from iap.features.engine import FEATURE_MAX_QTY, FeatureEngine, FeatureVector
from iap.features.registry import build_registry, registry_dicts
from iap.features.rolling import RollingSum
from iap.features.spec import EPS, FAMILY_ORDER, NS_PER_SEC, WINDOW_NS, FeatureSpec, mkspec

#: Pinned order of the opt-in families (appended after FAMILY_ORDER).
EXT_FAMILY_ORDER = ("xqueue", "xsign", "xhawkes", "xhidden")
FEATURE_SETS = ("default", "extended")
SAMPLING_MODES = ("clock", "events", "volume")

TTD_WINDOWS = ("1s", "10s")
HIDDEN_WINDOW = "1m"
_EQUITY_CLASSES = frozenset({"EQUITY", "ETF"})


@dataclass(frozen=True)
class ExtendedConfig:
    """Parameters of the extended families (all recorded in registry params)."""

    ttd_cap_s: float = 600.0
    sign_n: int = 100
    sign_lags: tuple[int, ...] = (1, 2, 3, 5, 10)
    hawkes_mu: float = 0.0
    hawkes_alpha: float = 1.0
    hawkes_betas: tuple[float, ...] = (1.0, 0.1)
    odd_lot: int = 100

    def __post_init__(self) -> None:
        if self.ttd_cap_s <= 0:
            raise ValueError("ttd_cap_s must be > 0")
        if self.sign_n < 2 or not self.sign_lags or max(self.sign_lags) >= self.sign_n:
            raise ValueError("need 1 <= lag < sign_n")
        if min(self.sign_lags) < 1:
            raise ValueError("lags must be >= 1")
        if self.hawkes_mu < 0 or self.hawkes_alpha < 0 or min(self.hawkes_betas) <= 0:
            raise ValueError("hawkes mu, alpha >= 0 and beta > 0 required")
        if self.odd_lot < 1:
            raise ValueError("odd_lot must be >= 1")


DEFAULT_CONFIG = ExtendedConfig()


def _beta_label(beta: float) -> str:
    return f"{beta:g}".replace(".", "p")


def specs(cfg: ExtendedConfig = DEFAULT_CONFIG) -> list[FeatureSpec]:
    """Registry entries of the extended families (pinned order)."""
    out: list[FeatureSpec] = []
    for side in ("bid", "ask"):
        for w in TTD_WINDOWS:
            out.append(
                mkspec(
                    f"qttd_{side}_w{w}_v1",
                    "xqueue",
                    f"Expected seconds to deplete the best-{side} queue: Q_L1 / (L1 queue "
                    f"decrements over {w} / {w}), capped at cap_s (cap when no depletion).",
                    window=w,
                    cap_s=cfg.ttd_cap_s,
                )
            )
    for k in cfg.sign_lags:
        out.append(
            mkspec(
                f"sign_acf_l{k}_n{cfg.sign_n}_v1",
                "xsign",
                f"Autocorrelation at lag {k} of the last {cfg.sign_n} trade signs "
                f"(+1 buyer-initiated, -1 seller-initiated; TRADE aggressor flag).",
                lag=k,
                n=cfg.sign_n,
            )
        )
    for beta in cfg.hawkes_betas:
        b = _beta_label(beta)
        prm = {"mu": cfg.hawkes_mu, "alpha": cfg.hawkes_alpha, "beta": beta}
        for kind, doc in (
            ("buy", "buyer-initiated trades"),
            ("sell", "seller-initiated trades"),
            ("total", "all trades (buy + sell)"),
        ):
            out.append(
                mkspec(
                    f"hawkes_{kind}_b{b}_v1",
                    "xhawkes",
                    f"Exponential-kernel Hawkes intensity (events/s) of {doc}: "
                    f"mu + alpha * sum exp(-beta * (t - t_i)).",
                    **prm,
                )
            )
        out.append(
            mkspec(
                f"hawkes_imb_b{b}_v1",
                "xhawkes",
                "Hawkes intensity imbalance (buy - sell) / (buy + sell).",
                depends_on=(f"hawkes_buy_b{b}_v1", f"hawkes_sell_b{b}_v1"),
                **prm,
            )
        )
    out.append(
        mkspec(
            f"oddlot_share_w{HIDDEN_WINDOW}_v1",
            "xhidden",
            f"Share of traded qty over {HIDDEN_WINDOW} in TRADEs below odd_lot shares "
            "(equities / ETFs only).",
            window=HIDDEN_WINDOW,
            odd_lot=cfg.odd_lot,
        )
    )
    out.append(
        mkspec(
            f"hidden_share_w{HIDDEN_WINDOW}_v1",
            "xhidden",
            f"Non-displayed execution share over {HIDDEN_WINDOW}: 1 - EXECUTE qty / TRADE qty "
            "(valid only when the source reports order-level executions).",
            window=HIDDEN_WINDOW,
        )
    )
    return out


def extended_registry(cfg: ExtendedConfig = DEFAULT_CONFIG) -> list[FeatureSpec]:
    """Default registry followed by the extended families (order is normative)."""
    base = build_registry()
    ext = specs(cfg)
    names = [s.name for s in base] + [s.name for s in ext]
    if len(names) != len(set(names)):
        raise ValueError("extended feature names collide with the default registry")
    return base + ext


def extended_registry_dicts(cfg: ExtendedConfig = DEFAULT_CONFIG) -> list[dict]:
    return registry_dicts() + [s.to_dict() for s in specs(cfg)]


def extended_registry_hash(cfg: ExtendedConfig = DEFAULT_CONFIG) -> str:
    """sha256 of the canonical extended-registry JSON (its feature_version)."""
    doc = extended_registry_dicts(cfg)
    canonical = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def write_extended_registry(path: str | Path, cfg: ExtendedConfig = DEFAULT_CONFIG) -> dict:
    """Write the extended registry document (same layout as write_registry)."""
    reg = extended_registry(cfg)
    fams = FAMILY_ORDER + EXT_FAMILY_ORDER
    doc = {
        "x-version": 1,
        "description": "Extended (opt-in, Python-only) feature registry: the pinned "
        "default registry followed by the v1.12 extended families. "
        "registry_hash is the feature_version of --feature-set extended stores.",
        "feature_set": "extended",
        "registry_hash": extended_registry_hash(cfg),
        "count": len(reg),
        "families": {f: sum(1 for s in reg if s.family == f) for f in fams},
        "features": extended_registry_dicts(cfg),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    return doc


# ---------------------------------------------------------------- state


class _Hawkes:
    """Online exponential-kernel excitation sum for one side."""

    __slots__ = ("beta", "alpha", "s", "t")

    def __init__(self, beta: float, alpha: float) -> None:
        self.beta = beta
        self.alpha = alpha
        self.s = 0.0  # sum alpha*exp(-beta (t_last - t_i)) at t_last
        self.t: int | None = None

    def add(self, t: int) -> None:
        if self.t is not None:
            self.s *= exp(-self.beta * (t - self.t) / NS_PER_SEC)
        self.s += self.alpha
        self.t = t

    def at(self, t: int) -> float:
        if self.t is None:
            return 0.0
        return self.s * exp(-self.beta * (t - self.t) / NS_PER_SEC)


class _ExtState:
    """Per-instrument state of the extended families."""

    def __init__(self, cfg: ExtendedConfig) -> None:
        self.cfg = cfg
        self.recoveries = 0
        self.reset()
        #: sticky: the source has reported order-level executions (EXECUTE)
        self.has_executes = False

    def reset(self) -> None:
        cfg = self.cfg
        self.signs: deque[int] = deque(maxlen=cfg.sign_n)
        self.hawkes = [
            (_Hawkes(b, cfg.hawkes_alpha), _Hawkes(b, cfg.hawkes_alpha)) for b in cfg.hawkes_betas
        ]
        # [trade_qty, oddlot_qty, execute_qty]
        self.liq = RollingSum(WINDOW_NS[HIDDEN_WINDOW], 3)

    def on_event(self, ev: MarketEvent) -> None:
        et = ev.event_type
        t = ev.exchange_ts
        if et == EventType.TRADE:
            buy = ev.side == 0
            self.signs.append(1 if buy else -1)
            for hb, hs in self.hawkes:
                (hb if buy else hs).add(t)
            odd = ev.qty if ev.qty < self.cfg.odd_lot else 0
            self.liq.add(t, (ev.qty, odd, 0))
        elif et == EventType.EXECUTE:
            self.has_executes = True
            self.liq.add(t, (0, 0, ev.qty))


def _sign_acf(signs: Sequence[int], k: int) -> float | None:
    n = len(signs)
    m = sum(signs) / n
    d = [s - m for s in signs]
    var = sum(x * x for x in d)
    if var <= EPS:
        return None
    return sum(d[i] * d[i - k] for i in range(k, n)) / var


def compute_extended(st, xs: _ExtState, values: list[float], valid: list[bool]) -> None:
    """Append the extended values for the current emission (registry order)."""
    cfg = xs.cfg
    t = st.t
    # xqueue — queue sums layout: [dep_bid, rep_bid, dep_ask, rep_ask]
    for q, di in ((st.bid_q, 0), (st.ask_q, 2)):
        for w in TTD_WINDOWS:
            ok = st.book_ok and st.warm(WINDOW_NS[w])
            v = None
            if ok:
                rate = st.queue[w].sums[di] / (WINDOW_NS[w] / NS_PER_SEC)
                v = cfg.ttd_cap_s if rate <= 0 else min(q / rate, cfg.ttd_cap_s)
            put(values, valid, v, ok)
    # xsign
    full = len(xs.signs) == cfg.sign_n
    for k in cfg.sign_lags:
        v = _sign_acf(xs.signs, k) if full else None
        put(values, valid, v, v is not None)
    # xhawkes
    for beta, (hb, hs) in zip(cfg.hawkes_betas, xs.hawkes, strict=True):
        ok = st.warm(int(5.0 / beta * NS_PER_SEC))
        lb = cfg.hawkes_mu + hb.at(t)
        ls = cfg.hawkes_mu + hs.at(t)
        tot = lb + ls
        put(values, valid, lb, ok)
        put(values, valid, ls, ok)
        put(values, valid, tot, ok)
        put(values, valid, (lb - ls) / tot if tot > EPS else None, ok and tot > EPS)
    # xhidden
    xs.liq.trim(t)
    tq, oq, eq = xs.liq.sums
    wok = st.warm(WINDOW_NS[HIDDEN_WINDOW]) and tq > 0
    ok = wok and st.ctx.asset_class in _EQUITY_CLASSES
    put(values, valid, oq / tq if ok else None, ok)
    ok = wok and xs.has_executes
    put(values, valid, min(1.0, max(0.0, 1.0 - eq / tq)) if ok else None, ok)


# ---------------------------------------------------------------- engine


class ExtendedFeatureEngine(FeatureEngine):
    """FeatureEngine with the opt-in extended families and/or event-time sampling.

    ``feature_set="default"`` keeps the default registry (values identical to
    :class:`FeatureEngine` at the same emission times); ``"extended"`` appends
    the extended values.  ``sampling="clock"`` uses ``cadence_ns`` exactly as
    the base engine; ``"events"`` / ``"volume"`` emit when ``sample_n``
    applied events / traded shares have accumulated for the instrument since
    its last row (the first row is emitted on the instrument's first event).
    """

    def __init__(
        self,
        contexts,
        cadence_ns: int = 0,
        on_vector: Callable[[FeatureVector], None] | None = None,
        profiles=None,
        feature_set: str = "extended",
        sampling: str = "clock",
        sample_n: int = 0,
        config: ExtendedConfig = DEFAULT_CONFIG,
    ) -> None:
        if feature_set not in FEATURE_SETS:
            raise ValueError(f"feature_set must be one of {FEATURE_SETS}")
        if sampling not in SAMPLING_MODES:
            raise ValueError(f"sampling must be one of {SAMPLING_MODES}")
        if sampling != "clock" and sample_n < 1:
            raise ValueError("event-time sampling needs sample_n >= 1")
        super().__init__(contexts, cadence_ns=cadence_ns, on_vector=None, profiles=profiles)
        self._user_cb = on_vector
        self.feature_set = feature_set
        self.sampling = sampling
        self.sample_n = sample_n
        self.config = config
        self.extended = feature_set == "extended"
        if self.extended:
            self.registry = extended_registry(config)
            self.feature_names = [s.name for s in self.registry]
            self.feature_version = extended_registry_hash(config)
            self._n_ext = len(specs(config))
        self.xstates: dict[int, _ExtState] = {}
        self._acc: dict[int, int] = {}  # event-time sampling accumulator
        self._cur: MarketEvent | None = None
        self._dropped_before = 0

    def apply(self, ev: MarketEvent) -> FeatureVector | None:
        self._cur = ev
        self._dropped_before = self.events_dropped
        return super().apply(ev)

    def _maybe_emit(self, st, t: int) -> FeatureVector | None:
        ev = self._cur
        iid = st.ctx.instrument_id
        applied = self.events_dropped == self._dropped_before
        xs = self.xstates.get(iid)
        if xs is None:
            xs = self.xstates[iid] = _ExtState(self.config)
        recovered = st.recoveries != xs.recoveries
        if recovered:
            xs.recoveries = st.recoveries
            xs.reset()
        usable = applied and ev is not None and ev.qty <= FEATURE_MAX_QTY
        if usable and not recovered:
            xs.on_event(ev)
        if self.sampling == "clock":
            return super()._maybe_emit(st, t)
        if usable:
            if self.sampling == "events":
                self._acc[iid] = self._acc.get(iid, 0) + 1
            elif ev.event_type == EventType.TRADE:
                self._acc[iid] = self._acc.get(iid, 0) + ev.qty
        if st.last_emit is None or self._acc.get(iid, 0) >= self.sample_n:
            self._acc[iid] = 0
            vec = self._emit(st, t)
            st.last_emit = t
            return vec
        return None

    def _emit(self, st, t: int) -> FeatureVector:
        vec = super()._emit(st, t)
        if self.extended:
            xs = self.xstates[st.ctx.instrument_id]
            before = len(vec.values)
            compute_extended(st, xs, vec.values, vec.validity)
            if len(vec.values) - before != self._n_ext:
                raise RuntimeError("extended families appended the wrong number of values")
            vec.feature_version = self.feature_version
        if self._user_cb is not None:
            self._user_cb(vec)
        return vec


# ---------------------------------------------------------------- MLE helper


def hawkes_loglik(times_s: Sequence[float], horizon_s: float, mu, alpha, beta) -> float:
    """Exact log-likelihood of a univariate exp-kernel Hawkes process on [0, T]
    with intensity mu + alpha * sum exp(-beta (t - t_i)) (Ozaki recursion)."""
    ll = 0.0
    a = 0.0  # sum exp(-beta (t_j - t_i)) for i < j
    prev = None
    for tj in times_s:
        if prev is not None:
            a = exp(-beta * (tj - prev)) * (1.0 + a)
        lam = mu + alpha * a
        if lam <= 0:
            return float("-inf")
        ll += log(lam)
        prev = tj
    comp = mu * horizon_s + (alpha / beta) * sum(
        1.0 - exp(-beta * (horizon_s - t)) for t in times_s
    )
    return ll - comp


@dataclass
class HawkesFit:
    mu: float
    alpha: float
    beta: float
    loglik: float
    branching_ratio: float = field(init=False)

    def __post_init__(self) -> None:
        self.branching_ratio = self.alpha / self.beta


def fit_hawkes_exp(times_s: Sequence[float], horizon_s: float) -> HawkesFit:
    """Offline MLE of (mu, alpha, beta) for a univariate exponential Hawkes
    process (times in seconds, sorted, within [0, horizon_s]).  Uses scipy
    when available; parameters are optimised on the log scale with the
    stationarity constraint alpha < beta enforced by a penalty.  Use the
    result to set :class:`ExtendedConfig` (never fitted inside a replay)."""
    times = sorted(float(x) for x in times_s)
    if len(times) < 10 or horizon_s <= 0:
        raise ValueError("need >= 10 events and a positive horizon")
    from scipy.optimize import minimize

    n = len(times)

    def nll(p):
        mu, alpha, beta = (exp(x) for x in p)
        if alpha >= beta:
            return 1e12
        v = hawkes_loglik(times, horizon_s, mu, alpha, beta)
        return 1e12 if v == float("-inf") else -v

    x0 = [log(0.5 * n / horizon_s), log(0.5), log(1.0)]
    res = minimize(
        nll, x0, method="Nelder-Mead", options={"xatol": 1e-6, "fatol": 1e-8, "maxiter": 4000}
    )
    mu, alpha, beta = (exp(x) for x in res.x)
    return HawkesFit(mu, alpha, beta, -float(res.fun))
