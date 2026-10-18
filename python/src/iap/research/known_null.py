"""Known-null calibration of the confirmatory path (v1.18 REL-06).

Implements ``research/v118/NULL_PROTOCOL.md`` (programme section 9.4): run
the whole prereg path - signal to decision rows, fills and costs through the
registered simulator and cost structure, net P&L per round trip, per-session
aggregation, day-cluster inference, Holm over the family, the decision rule
with REL-02d eligibility - on labels where, by construction, there is nothing
to find, and count how often it confirms anyway.

Two null generators (protocol sections 3 and 4):

* **A** (:func:`derangements`, :func:`draw_permutations`, :func:`labels_a`):
  the label of every row of session ``s`` becomes the label of the row of
  session ``pi(s)`` with the same name and the same time-of-day cell
  (seconds since 09:30 ET floored to the 100 ms label cadence).  ``pi`` is a
  derangement, the same for every name, drawn without replacement across
  trials; a partner cell with no (valid) label leaves the row missing.
* **B** (:func:`ar1_params`, :func:`labels_b`): per name and horizon a
  zero-mean AR(1) on the cell grid with the pooled within-session lag-1
  autocorrelation and the real standard deviation, cross-name innovations
  through the Cholesky factor of the real label correlation, the real rows'
  missing mask copied.

The economics are label-independent by design: entry and exit rows, side,
fill quantity, spread, fee, impact and completion charge come from the
recorded rows and the registered simulator (``ExecutionSimulator`` rules 5
and 6/6b, via :class:`SimulatorPricer`), computed ONCE per hypothesis
(:func:`build_trips`).  Only gross P&L (side x the trip's forward mid return)
reads the label, which is the one thing a generator replaces.

Inference (:func:`cluster_inference`): one value per session (the
equal-weight mean of that session's trip nets, bps of entry notional at the
registered size), CR1 standard error, Student-t at ``G - 1`` df, and for
``G < 10`` the restricted (null-imposed) wild-cluster bootstrap with Webb
six-point weights by FULL enumeration (``6**G`` draws, :func:`webb_weights`);
one-sided at the pre-declared sign (the side already carries it, so larger
is evidence).  Holm over the K registered hypotheses
(:func:`iap.validation.multiple_testing.holm`, a missing p counted as 1).

Randomness: :class:`iap.core.rng.SplitMix64` only.  :func:`splitmix_uniforms`
is the vectorised form of the same stream (bit-identical to calling
``uniform()`` in a loop; tested), needed because generator B draws millions
of normals per trial.
"""

from __future__ import annotations

import datetime as _dt
import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from iap.core.rng import SplitMix64
from iap.research.power_stats import wilson_interval
from iap.validation.multiple_testing import holm

__all__ = [
    "CELL_NS",
    "PROTOCOL",
    "SMALL_G",
    "Hypothesis",
    "Panel",
    "SimulatorPricer",
    "TripTable",
    "ar1_params",
    "build_trips",
    "cluster_inference",
    "derangements",
    "draw_permutations",
    "evaluate",
    "labels_a",
    "labels_b",
    "labels_real",
    "run_trial",
    "small_g_statement",
    "splitmix_normals",
    "splitmix_uniforms",
    "summarise",
    "webb_weights",
]

PROTOCOL = "research/v118/NULL_PROTOCOL.md"
#: the label cadence: one time-of-day cell per 100 ms since 09:30:00 ET
CELL_NS = 100_000_000
#: 09:30 to 16:00 ET in cells
N_CELLS = int(6.5 * 3600 * 1e9) // CELL_NS
NS_S = 1_000_000_000
#: below this many clusters the wild-cluster bootstrap p is the reported p
SMALL_G = 10
WEBB = np.array(
    [-math.sqrt(1.5), -1.0, -math.sqrt(0.5), math.sqrt(0.5), 1.0, math.sqrt(1.5)], dtype=float
)
_NY = ZoneInfo("America/New_York")
_GAMMA = np.uint64(0x9E3779B97F4A7C15)
_MIX1 = np.uint64(0xBF58476D1CE4E5B9)
_MIX2 = np.uint64(0x94D049BB133111EB)


# ----------------------------------------------------------------- randomness --
def splitmix_uniforms(rng: SplitMix64, n: int) -> np.ndarray:
    """The next ``n`` ``rng.uniform()`` draws as one array; advances ``rng``.

    SplitMix64's state moves by a constant, so draw ``k`` is a pure function
    of ``state + (k + 1) * gamma`` (mod 2**64) and the stream vectorises."""
    if n <= 0:
        return np.empty(0, dtype=float)
    k = np.arange(1, n + 1, dtype=np.uint64)
    with np.errstate(over="ignore"):
        z = np.uint64(rng.state) + k * _GAMMA
        z = (z ^ (z >> np.uint64(30))) * _MIX1
        z = (z ^ (z >> np.uint64(27))) * _MIX2
        z = z ^ (z >> np.uint64(31))
    rng.state = (rng.state + n * 0x9E3779B97F4A7C15) & ((1 << 64) - 1)
    return (z >> np.uint64(11)).astype(np.float64) * 2.0**-53


def splitmix_normals(rng: SplitMix64, n: int) -> np.ndarray:
    """``n`` standard normals, each exactly ``rng.normal()`` (Box-Muller on two
    uniforms, cosine branch).  ``rng.normal`` redraws ``u1 == 0`` (probability
    2**-53 per draw); the vector form raises instead of diverging silently."""
    u = splitmix_uniforms(rng, 2 * n)
    u1, u2 = u[0::2], u[1::2]
    if np.any(u1 == 0.0):
        raise ArithmeticError("SplitMix64 drew u1 == 0; the vector stream would diverge")
    return np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * math.pi * u2)


@lru_cache(maxsize=4)
def derangements(g: int) -> tuple[tuple[int, ...], ...]:
    """Every permutation of ``range(g)`` with no fixed point, lexicographic."""
    return tuple(p for p in itertools.permutations(range(g)) if all(p[i] != i for i in range(g)))


def draw_permutations(g: int, n_trials: int, base_seed: int) -> list[dict[str, Any]]:
    """Generator A's permutations: trial ``i`` draws, with
    ``SplitMix64(base_seed + i).below(len(remaining))``, one derangement from
    those not yet used (without replacement; protocol section 3)."""
    remaining = list(derangements(g))
    if n_trials > len(remaining):
        raise ValueError(
            f"{n_trials} trials need more than the {len(remaining)} derangements of {g}"
        )
    out = []
    for i in range(n_trials):
        seed = base_seed + i
        perm = remaining.pop(SplitMix64(seed).below(len(remaining)))
        out.append({"trial": i, "seed": seed, "permutation": list(perm)})
    return out


# ---------------------------------------------------------------------- data --
@dataclass
class Panel:
    """Rows of every name, concatenated name-major (``offsets[n]`` is the first
    global row of name ``n``), each name's rows sorted by time."""

    names: list[str]
    instrument_ids: list[int]
    sessions: list[str]
    offsets: np.ndarray
    ts: np.ndarray
    sess: np.ndarray
    cell: np.ndarray
    name: np.ndarray
    cols: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def n_rows(self) -> int:
        return int(self.ts.size)

    def name_slice(self, n: int) -> slice:
        return slice(int(self.offsets[n]), int(self.offsets[n + 1]))


def session_open_ns(iso: str) -> int:
    d = _dt.date.fromisoformat(iso)
    return int(_dt.datetime(d.year, d.month, d.day, 9, 30, tzinfo=_NY).timestamp()) * NS_S


def cells_of(ts: np.ndarray, iso_of_row: np.ndarray, sessions: Sequence[str]) -> np.ndarray:
    """Time-of-day cell of every row, ``-1`` outside 09:30-16:00 ET."""
    opens = np.array([session_open_ns(s) for s in sessions], dtype=np.int64)
    c = (ts - opens[iso_of_row]) // CELL_NS
    return np.where((c >= 0) & (c < N_CELLS), c, -1).astype(np.int64)


def make_panel(
    frames: Sequence[tuple[str, int, Mapping[str, np.ndarray]]],
) -> Panel:
    """``frames`` = ``(symbol, instrument_id, columns)``; ``columns`` holds
    ``exchange_ts`` and every other column the study reads.  Sessions are the
    ET calendar days the rows fall on."""
    names, iids, parts = [], [], []
    for sym, iid, cols in frames:
        ts = np.asarray(cols["exchange_ts"], dtype=np.int64)
        order = np.argsort(ts, kind="mergesort")
        names.append(sym)
        iids.append(int(iid))
        parts.append({k: np.asarray(v)[order] for k, v in cols.items()})
    ts_all = np.concatenate([p["exchange_ts"] for p in parts]).astype(np.int64)
    # the ET calendar day of every row, converted once per distinct UTC hour
    uh, inv = np.unique(ts_all // (3600 * NS_S), return_inverse=True)
    iso_h = [_dt.datetime.fromtimestamp(int(h) * 3600, _NY).date().isoformat() for h in uh]
    sessions = sorted(set(iso_h))
    sidx = {s: k for k, s in enumerate(sessions)}
    sess = np.array([sidx[s] for s in iso_h], dtype=np.int64)[inv.reshape(-1)]
    lens = [p["exchange_ts"].size for p in parts]
    offsets = np.concatenate([[0], np.cumsum(lens)]).astype(np.int64)
    keys = sorted(set().union(*[set(p) for p in parts]) - {"exchange_ts"})
    cols = {k: np.concatenate([np.asarray(p[k]) for p in parts]) for k in keys}
    return Panel(
        names=names,
        instrument_ids=iids,
        sessions=sessions,
        offsets=offsets,
        ts=ts_all,
        sess=sess,
        cell=cells_of(ts_all, sess, sessions),
        name=np.repeat(np.arange(len(parts)), lens),
        cols=cols,
    )


def labels_real(panel: Panel, horizon: str) -> np.ndarray:
    """The recorded mid label of every row, NaN where it is not valid."""
    lab = np.asarray(panel.cols[f"label_mid_{horizon}"], dtype=float)
    ok = np.asarray(panel.cols[f"label_valid_{horizon}"], dtype=bool) & np.isfinite(lab)
    return np.where(ok, lab, np.nan)


def _cell_grid(panel: Panel, values: np.ndarray) -> np.ndarray:
    """``(names, sessions, N_CELLS)`` grid of ``values`` (NaN = no row); a cell
    holding several rows keeps the last one."""
    grid = np.full((len(panel.names), len(panel.sessions), N_CELLS), np.nan)
    on = panel.cell >= 0
    grid[panel.name[on], panel.sess[on], panel.cell[on]] = values[on]
    return grid


def labels_a(
    panel: Panel, real: np.ndarray, permutation: Sequence[int], grid: np.ndarray | None = None
) -> np.ndarray:
    """Generator A: row (name, s, cell) takes the real label of (name, pi(s), cell).
    ``grid`` is ``_cell_grid(panel, real)`` when the caller caches it."""
    grid = _cell_grid(panel, real) if grid is None else grid
    pi = np.asarray(permutation, dtype=np.int64)
    out = np.full(panel.n_rows, np.nan)
    on = panel.cell >= 0
    out[on] = grid[panel.name[on], pi[panel.sess[on]], panel.cell[on]]
    return out


def ar1_params(panel: Panel, real: np.ndarray) -> dict[str, Any]:
    """Generator B's parameters from the real labels of one horizon: per name the
    pooled within-session lag-1 autocorrelation on the cell grid (pairs of
    adjacent cells both present, never across sessions) and the standard
    deviation; across names the correlation of labels in cells where all names
    are present, and its Cholesky factor."""
    grid = _cell_grid(panel, real)
    phis, sds = [], []
    for n in range(len(panel.names)):
        a, b = grid[n, :, :-1], grid[n, :, 1:]
        ok = np.isfinite(a) & np.isfinite(b)
        x, y = a[ok], b[ok]
        x, y = x - x.mean(), y - y.mean()
        den = math.sqrt(float((x * x).sum()) * float((y * y).sum()))
        phis.append(float((x * y).sum() / den) if den > 0 else 0.0)
        v = grid[n][np.isfinite(grid[n])]
        sds.append(float(v.std()) if v.size > 1 else 0.0)
    flat = grid.reshape(len(panel.names), -1)
    ok = np.all(np.isfinite(flat), axis=0)
    corr = np.corrcoef(flat[:, ok]) if ok.sum() > 2 else np.eye(len(panel.names))
    corr = np.atleast_2d(corr)
    return {
        "phi": phis,
        "sd": sds,
        "corr": corr.tolist(),
        "cholesky": np.linalg.cholesky(corr).tolist(),
        "n_cells_joint": int(ok.sum()),
    }


def labels_b(
    panel: Panel, real: np.ndarray, params: Mapping[str, Any], rng: SplitMix64 | int
) -> np.ndarray:
    """Generator B: per session, for every cell ``k`` in order, draws one normal
    per name (from ``rng``, or ``SplitMix64(rng)`` for an int; session-major,
    cell, then name), correlates
    them through the Cholesky factor, and runs the AR(1)
    ``y_k = phi y_(k-1) + sd sqrt(1 - phi**2) e_k`` started at stationarity
    ``y_0 = sd e_0``.  Rows take their cell's value where the real label is
    valid (the missing mask is copied)."""
    from scipy.signal import lfilter

    rng = SplitMix64(rng) if isinstance(rng, int) else rng
    n_names, g = len(panel.names), len(panel.sessions)
    chol = np.asarray(params["cholesky"], dtype=float)
    phi = np.asarray(params["phi"], dtype=float)
    sd = np.asarray(params["sd"], dtype=float)
    grid = np.empty((n_names, g, N_CELLS))
    for s in range(g):
        e = splitmix_normals(rng, N_CELLS * n_names).reshape(N_CELLS, n_names) @ chol.T
        for n in range(n_names):
            scale = sd[n] * math.sqrt(max(0.0, 1.0 - phi[n] ** 2))
            x = e[:, n] * scale
            x[0] = e[0, n] * sd[n]
            grid[n, s] = lfilter([1.0], [1.0, -phi[n]], x)
    out = np.full(panel.n_rows, np.nan)
    on = (panel.cell >= 0) & np.isfinite(real)
    out[on] = grid[panel.name[on], panel.sess[on], panel.cell[on]]
    return out


# --------------------------------------------------------------- economics --
@dataclass(frozen=True)
class Hypothesis:
    """One registered (signal x horizon x universe) cell with its sign: trade
    ``sign * sign(signal)`` when ``|signal| > threshold`` and flat."""

    id: str
    signal: str
    horizon: str
    symbols: tuple[str, ...]
    sign: int
    threshold: float

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Hypothesis:
        sign = int(d["sign"])
        if sign not in (-1, 1):
            raise ValueError(f"hypothesis {d.get('id')}: sign must be +1 or -1")
        return cls(
            id=str(d["id"]),
            signal=str(d["signal"]),
            horizon=str(d["horizon"]),
            symbols=tuple(d["symbols"]),
            sign=sign,
            threshold=float(d["threshold"]),
        )


HORIZON_NS = {"1s": NS_S, "5s": 5 * NS_S, "10s": 10 * NS_S, "30s": 30 * NS_S, "1m": 60 * NS_S}


class SimulatorPricer:
    """Fees and impact of one taker fill through the registered
    ``ExecutionSimulator`` (rule 5 fees incl. the dated regulatory schedule
    when configured; rule 6 linear or 6b parent impact), so the null runs
    through exactly the cost code a real run uses (protocol 10.5).  Each leg
    is its own parent: impact never accumulates across trips."""

    def __init__(self, config: Any, venue_id: int) -> None:
        from iap.execution.simulator import ExecutionSimulator

        self._sim = ExecutionSimulator(config)
        self._config = config
        self.venue_id = int(venue_id)
        self._pid = 0

    def leg(
        self, instrument_id: int, side: int, qty: int, price: float, ts: int
    ) -> tuple[float, float]:
        """``(fee, impact)`` in currency for a taker fill of ``qty`` at ``price``."""
        from iap.execution.types import ChildOrder, Liquidity, OrderType

        if qty <= 0:
            return 0.0, 0.0
        tick = self._config.instrument(instrument_id).tick_size
        self._pid += 1
        o = ChildOrder(
            parent_id=self._pid,
            instrument_id=instrument_id,
            venue_id=self.venue_id,
            side=side,
            type=OrderType.MARKET,
            qty=qty,
            decision_ts=ts,
        )
        o.order_id = self._pid
        self._sim._emit_fill(o, int(round(price / tick)), qty, ts, Liquidity.TAKER)  # noqa: SLF001
        f = self._sim.fills[-1]
        return float(f.fee), float(f.impact_cost)


@dataclass
class TripTable:
    """Label-independent part of every round trip of one hypothesis."""

    entry_row: np.ndarray  # global panel row whose label is the trip's gross
    sess: np.ndarray
    side: np.ndarray  # +1 long, -1 short
    fill_frac: np.ndarray  # entry filled qty / registered size
    fixed_bps: np.ndarray  # everything but gross, bps of registered entry notional
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return int(self.entry_row.size)


def _first_at_or_after(ts: np.ndarray, t: int) -> int:
    return int(np.searchsorted(ts, t, side="left"))


def build_trips(
    panel: Panel,
    hyp: Hypothesis,
    *,
    size: int,
    latency_ns: int,
    max_decision_age_ns: int,
    cost_multiplier: float,
    pricer: SimulatorPricer | None,
) -> TripTable:
    """Decision rows to round trips (protocol section 9), once per hypothesis.

    Per name: while flat, the first row with ``|signal| > threshold`` is a
    decision; the entry is the first row at or after ``decision + latency``
    (refused when older than ``max_decision_age``); the trip is a taker entry
    of ``size`` shares filled up to the far-side L1 depth (partial fills: the
    trip is the filled quantity) and a taker exit at the first row at or after
    ``entry + horizon``; the next decision comes after the exit (no overlap,
    so FIFO matching is trivial).  Costs, in bps of the registered entry
    notional ``size x mid``: the recorded spread crossed at both ends
    (``label_mid - label_cost`` of the entry row, i.e. ``(hs(t) + hs(t+h)) / m(t)``),
    fees and impact of both legs from the simulator, and the REL-03 completion
    charge (half spread at the horizon end plus impact) on entry residual and
    on exit quantity beyond the exit row's L1 depth.  A trip whose recorded
    cost label is not valid cannot be priced and is not a trip.
    """
    h_ns = HORIZON_NS[hyp.horizon]
    c = panel.cols
    sig_all = np.asarray(c[hyp.signal], dtype=float)
    mid_all = np.asarray(c["mid_price_v1"], dtype=float)
    hs_bps_all = np.asarray(c["half_spread_cost_bps_v1"], dtype=float)
    lab_mid = np.asarray(c[f"label_mid_{hyp.horizon}"], dtype=float)
    lab_cost = np.asarray(c[f"label_cost_{hyp.horizon}"], dtype=float)
    lab_ok = np.asarray(c[f"label_valid_{hyp.horizon}"], dtype=bool)
    d_bid = np.asarray(c["depth_bid_l1_v1"], dtype=float)
    d_ask = np.asarray(c["depth_ask_l1_v1"], dtype=float)
    rows: dict[str, list] = {k: [] for k in ("entry", "sess", "side", "f", "fixed")}
    diag = {
        "decisions": 0,
        "too_old": 0,
        "unpriceable": 0,
        "unfilled_entries": 0,
        "exit_residual": 0,
    }
    for n, sym in enumerate(panel.names):
        if sym not in hyp.symbols:
            continue
        sl = panel.name_slice(n)
        base = sl.start
        ts, sess = panel.ts[sl], panel.sess[sl]
        sig = sig_all[sl]
        cand = np.flatnonzero(np.isfinite(sig) & (np.abs(sig) > hyp.threshold))
        iid = panel.instrument_ids[n]
        t_free = np.iinfo(np.int64).min
        ts_c, sess_c = ts[cand], sess[cand]
        k = 0
        while k < cand.size:
            r = int(cand[k])
            if ts[r] < t_free:
                k = int(np.searchsorted(ts_c, t_free, side="left"))
                continue
            diag["decisions"] += 1
            e = _first_at_or_after(ts, int(ts[r]) + latency_ns)
            if e >= ts.size or sess[e] != sess[r]:
                # no entry row left in this session: go to the next session
                k = int(np.searchsorted(sess_c, sess[r], side="right"))
                continue
            if ts[e] - ts[r] > max_decision_age_ns:
                diag["too_old"] += 1
                k += 1
                continue
            t_exit = int(ts[e]) + h_ns
            t_free = t_exit
            x = _first_at_or_after(ts, t_exit)
            ge = base + e
            if not lab_ok[ge] or x >= ts.size or sess[x] != sess[e]:
                diag["unpriceable"] += 1
                k += 1
                continue
            side = hyp.sign * (1 if sig[r] > 0 else -1)
            gx = base + x
            mid = mid_all[ge]
            far = d_ask[ge] if side > 0 else d_bid[ge]
            filled = int(min(size, max(0.0, far if np.isfinite(far) else 0.0)))
            exit_depth = d_bid[gx] if side > 0 else d_ask[gx]
            exit_fill = int(min(filled, max(0.0, exit_depth if np.isfinite(exit_depth) else 0.0)))
            notional = size * mid
            spread_bps = 1e4 * (lab_mid[ge] - lab_cost[ge])
            fee = imp = 0.0
            comp_imp = 0.0
            if pricer is not None:
                hs_px = mid * hs_bps_all[ge] * 1e-4
                f1, i1 = pricer.leg(
                    iid, 0 if side > 0 else 1, filled, mid + side * hs_px, int(ts[e])
                )
                exit_mid = mid_all[gx]
                f2, i2 = pricer.leg(iid, 1 if side > 0 else 0, filled, exit_mid, int(ts[x]))
                fee, imp = f1 + f2, i1 + i2
                resid = (size - filled) + (filled - exit_fill)
                if resid > 0:
                    _, comp_imp = pricer.leg(iid, 0 if side > 0 else 1, resid, mid, int(ts[x]))
            comp_bps = (
                float(hs_bps_all[gx]) if np.isfinite(hs_bps_all[gx]) else float(hs_bps_all[ge])
            )
            f = filled / size
            fixed = (
                f * spread_bps
                + 1e4 * (fee + imp + comp_imp) / notional
                + ((size - filled) + (filled - exit_fill)) / size * comp_bps
            )
            diag["unfilled_entries"] += int(filled < size)
            diag["exit_residual"] += int(exit_fill < filled)
            rows["entry"].append(ge)
            rows["sess"].append(int(sess[e]))
            rows["side"].append(side)
            rows["f"].append(f)
            rows["fixed"].append(cost_multiplier * fixed)
            k += 1
    return TripTable(
        entry_row=np.asarray(rows["entry"], dtype=np.int64),
        sess=np.asarray(rows["sess"], dtype=np.int64),
        side=np.asarray(rows["side"], dtype=float),
        fill_frac=np.asarray(rows["f"], dtype=float),
        fixed_bps=np.asarray(rows["fixed"], dtype=float),
        diagnostics=diag,
    )


def session_means(
    trips: TripTable, labels: np.ndarray, n_sessions: int
) -> tuple[np.ndarray, np.ndarray]:
    """Equal-weight mean net bps per session (sessions with no trip dropped) and
    the trip counts behind them."""
    lab = labels[trips.entry_row]
    ok = np.isfinite(lab)
    net = trips.fill_frac * trips.side * 1e4 * np.where(ok, lab, 0.0) - trips.fixed_bps
    cnt = np.bincount(trips.sess[ok], minlength=n_sessions)
    tot = np.bincount(trips.sess[ok], weights=net[ok], minlength=n_sessions)
    have = cnt > 0
    return tot[have] / cnt[have], cnt[have]


# ---------------------------------------------------------------- inference --
@lru_cache(maxsize=4)
def webb_weights(g: int) -> tuple[np.ndarray, np.ndarray]:
    """All ``6**g`` Webb weight vectors (rows) and their squares."""
    w = np.array(list(itertools.product(WEBB, repeat=g)), dtype=float).reshape(-1, g)
    return w, w * w


def _cr1(x: np.ndarray) -> tuple[float, float]:
    """Mean and CR1 SE with one observation per cluster:
    ``se**2 = G / (G - 1) * sum (x_g - mean)**2 / G**2``
    (``iap.research.execution_benchmark.cluster_mean_stats`` at one row per
    cluster; tested equal)."""
    g = x.size
    m = float(x.mean())
    return m, math.sqrt(g / (g - 1) * float(((x - m) ** 2).sum()) / (g * g))


def cluster_inference(x: Sequence[float] | np.ndarray, alpha: float = 0.05) -> dict[str, Any]:
    """One-sided (greater) inference on the mean of per-session values ``x``."""
    from scipy import stats

    x = np.asarray(x, dtype=float)
    g = int(x.size)
    out: dict[str, Any] = {
        "G": g,
        "mean": float(x.mean()) if g else None,
        "se": None,
        "t": None,
        "p_student_t": None,
        "p_wild": None,
        "p": 1.0,
        "p_method": "none",
        "mde": None,
    }
    if g < 2:
        return out
    m, se = _cr1(x)
    out["se"] = se
    if se <= 0:
        out["p_method"] = "degenerate"
        return out
    t = m / se
    out["t"] = t
    out["p_student_t"] = float(stats.t.sf(t, g - 1))
    out["mde"] = float((stats.t.isf(alpha, g - 1) + stats.t.isf(0.2, g - 1)) * se)
    if g < SMALL_G:
        w, w2 = webb_weights(g)
        s1 = w @ x  # sum of x*_g
        s2 = w2 @ (x * x)  # sum of x*_g ** 2
        mean_b = s1 / g
        var_b = (s2 - g * mean_b * mean_b) * g / (g - 1) / (g * g)
        with np.errstate(divide="ignore", invalid="ignore"):
            tb = np.where(var_b > 0, mean_b / np.sqrt(np.maximum(var_b, 0.0)), -np.inf)
        # ties counted as at least as extreme (conservative); 1e-12 absorbs rounding
        out["p_wild"] = float(np.count_nonzero(tb >= t - 1e-12 * max(1.0, abs(t))) / tb.size)
        out["p"], out["p_method"] = out["p_wild"], f"wild_webb_full_enumeration_6^{g}"
    else:
        out["p"], out["p_method"] = out["p_student_t"], "student_t"
    return out


# -------------------------------------------------------------------- trials --
def run_trial(
    trips: Mapping[str, TripTable],
    labels_by_h: Mapping[str, np.ndarray],
    family: Sequence[Hypothesis],
    n_sessions: int,
    *,
    alpha: float,
    eligible: bool,
) -> dict[str, Any]:
    """One trial of one family: per hypothesis inference, Holm over K (a
    hypothesis with no p counts as p = 1), the decision rule."""
    infs = []
    for h in family:
        x, cnt = session_means(trips[h.id], labels_by_h[h.horizon], n_sessions)
        inf = cluster_inference(x, alpha)
        inf["trips"] = int(cnt.sum())
        infs.append(inf)
    adj = holm([inf["p"] if inf["p"] is not None else 1.0 for inf in infs])
    confirmed = [
        h.id
        for h, inf, p in zip(family, infs, adj, strict=True)
        if p is not None and p <= alpha and inf["mean"] is not None and inf["mean"] > 0 and eligible
    ]
    return {
        "confirmed": confirmed,
        "p": [inf["p"] for inf in infs],
        "p_holm": adj,
        "mean_bps": [inf["mean"] for inf in infs],
        "se_bps": [inf["se"] for inf in infs],
        "mde_bps": [inf["mde"] for inf in infs],
        "G": [inf["G"] for inf in infs],
        "trips": [inf["trips"] for inf in infs],
        "p_method": sorted({inf["p_method"] for inf in infs}),
    }


def _rate(k: int, n: int) -> dict[str, Any]:
    lo, hi = wilson_interval(k, n)
    return {
        "count": k,
        "n": n,
        "rate": round(k / n, 4) if n else None,
        "wilson95": [round(lo, 4), round(hi, 4)],
    }


def evaluate(
    k: int, n: int, *, max_point: float, max_upper: float, flag_upper_below: float
) -> dict[str, Any]:
    """Rate, Wilson interval and the protocol section 7 verdict for one cell."""
    r = _rate(k, n)
    lo, hi = wilson_interval(k, n)
    r["pass"] = bool(n > 0 and k / n <= max_point + 1e-12 and hi <= max_upper + 1e-12)
    r["over_conservative"] = bool(n > 0 and hi < flag_upper_below)
    del lo
    return r


def summarise(
    trials: Sequence[Mapping[str, Any]],
    family: Sequence[Hypothesis],
    rule: Mapping[str, float],
) -> dict[str, Any]:
    """Per hypothesis and family-level false-confirmation counts and verdicts."""
    n = len(trials)
    per = {}
    for h in family:
        k = sum(1 for t in trials if h.id in t["confirmed"])
        per[h.id] = evaluate(k, n, **rule)
    fam = evaluate(sum(1 for t in trials if t["confirmed"]), n, **rule)
    pooled = _rate(sum(len(t["confirmed"]) for t in trials), n * len(family))
    return {
        "per_hypothesis": per,
        "per_hypothesis_pooled": pooled,
        "family": fam,
        "pass": bool(fam["pass"] and all(v["pass"] for v in per.values())),
        "over_conservative": bool(fam["over_conservative"]),
    }


def small_g_statement(g: int) -> str:
    """Protocol section 8, printed on every output that uses the method."""
    return (
        f"This inference rests on G = {g} session clusters. For G <= 5 the known-null protocol "
        f"reports size, not power. Intervals are Student-t at G-1 = {g - 1} degrees of freedom; "
        "the minimum detectable effect and the effective sample size are printed beside every "
        "interval."
    )


def round_floats(obj: Any, digits: int = 6) -> Any:
    """Recursively round floats (canonical output, programme section 9.6)."""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return None
        return round(obj, digits)
    if isinstance(obj, Mapping):
        return {str(k): round_floats(v, digits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [round_floats(v, digits) for v in obj]
    if isinstance(obj, (np.floating,)):
        return round_floats(float(obj), digits)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    return obj


def config_hash(path: Path) -> str:
    import hashlib

    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: what the study binds (REL-02a): the scripts whose bytes decide the result
BOUND_SCRIPTS = (
    "python/src/iap/research/known_null.py",
    "research/v118/known_null_config.json",
    "tools/rel06_known_null.py",
)
METRICS = (
    "cr1_se_student_t",
    "false_confirmation_rate_family",
    "false_confirmation_rate_per_hypothesis",
    "net_bps_per_round_trip_session_mean",
    "wild_cluster_webb_full_enumeration_p",
    "wilson95",
)


def identity_args(config: Mapping[str, Any]) -> dict[str, Any]:
    """The ``analysis_identity`` arguments of the study (everything but the
    file hashes and the commit): the binder and the runner both call this, so
    what is signed and what is checked cannot drift apart."""
    import hashlib
    import json

    variants = config["variants"]
    fam_digest = hashlib.sha256(
        json.dumps(variants, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    family = (
        "REL-06 known-null: "
        + ", ".join(f"{k} (K={len(v)})" for k, v in sorted(variants.items()))
        + f"; hypotheses sha256 {fam_digest}"
    )
    thresholds = {
        "alpha": config["alpha"],
        "pass_rule": dict(config["pass_rule"]),
        "n_trials": config["n_trials"],
        "base_seeds": {g: v["base_seed"] for g, v in sorted(config["generators"].items())},
        "registered_size_shares": config["registered_size_shares"],
        "configuration": dict(config["configuration"]),
        "execution": dict(config["execution"]),
        "compute_budget_hours": config["compute_budget_hours"],
    }
    return {
        "scripts": list(BOUND_SCRIPTS),
        "data_versions": [config["dataset"]["dataset_version"]],
        "family": family,
        "metrics": list(METRICS),
        "thresholds": thresholds,
    }


# --------------------------------------------------------------------- study --
def eligibility(configuration: Mapping[str, Any]) -> dict[str, Any]:
    """REL-02d gate eligibility of the registered configuration (the same
    bounds :func:`iap.research.specs.gate_eligibility` applies)."""
    from iap.research.specs import _configuration_violations

    reasons = _configuration_violations(configuration)
    return {"gate_eligible": not reasons, "reasons": reasons}


@dataclass
class Study:
    """Everything a trial needs, built once (:func:`prepare`)."""

    panel: Panel
    config: Mapping[str, Any]
    variants: dict[str, list[Hypothesis]]
    trips: dict[str, TripTable]
    real: dict[str, np.ndarray]
    grids: dict[str, np.ndarray]
    ar1: dict[str, dict[str, Any]]
    eligible: dict[str, Any]

    @property
    def horizons(self) -> list[str]:
        return sorted(self.real)


def prepare(
    panel: Panel,
    config: Mapping[str, Any],
    *,
    pricer: SimulatorPricer | None,
    variants: Sequence[str] | None = None,
) -> Study:
    conf = config["configuration"]
    chosen = {
        k: [Hypothesis.from_dict(d) for d in v]
        for k, v in sorted(config["variants"].items())
        if variants is None or k in variants
    }
    uniq: dict[str, Hypothesis] = {}
    for fam in chosen.values():
        for h in fam:
            if uniq.setdefault(h.id, h) != h:
                raise ValueError(f"hypothesis id {h.id} is registered twice with different fields")
    trips = {
        hid: build_trips(
            panel,
            h,
            size=int(config["registered_size_shares"]),
            latency_ns=int(conf["latency_ns"]),
            max_decision_age_ns=int(conf["max_decision_age_ns"]),
            cost_multiplier=float(conf["cost_multiplier"]),
            pricer=pricer,
        )
        for hid, h in sorted(uniq.items())
    }
    horizons = sorted({h.horizon for h in uniq.values()})
    real = {h: labels_real(panel, h) for h in horizons}
    return Study(
        panel=panel,
        config=config,
        variants=chosen,
        trips=trips,
        real=real,
        grids={h: _cell_grid(panel, real[h]) for h in horizons},
        ar1={h: ar1_params(panel, real[h]) for h in horizons},
        eligible=eligibility(conf),
    )


def trial_labels(
    study: Study, generator: str, trial: int, perm: Sequence[int] | None
) -> dict[str, np.ndarray]:
    if generator == "A":
        assert perm is not None
        return {
            h: labels_a(study.panel, study.real[h], perm, study.grids[h]) for h in study.horizons
        }
    rng = SplitMix64(int(study.config["generators"]["B"]["base_seed"]) + trial)
    return {h: labels_b(study.panel, study.real[h], study.ar1[h], rng) for h in study.horizons}


def run_trials(
    study: Study, generator: str, trials: Sequence[int], perms: Mapping[int, Sequence[int]]
) -> dict[int, dict[str, Any]]:
    """Trials ``trials`` of one generator, every variant: ``{trial: {variant: record}}``."""
    alpha = float(study.config["alpha"])
    ok = bool(study.eligible["gate_eligible"])
    g = len(study.panel.sessions)
    out = {}
    for i in trials:
        labs = trial_labels(study, generator, i, perms.get(i))
        out[i] = {
            k: run_trial(study.trips, labs, fam, g, alpha=alpha, eligible=ok)
            for k, fam in study.variants.items()
        }
    return out


def study_document(
    study: Study,
    results: Mapping[str, Mapping[int, Mapping[str, Any]]],
    perms: Sequence[Mapping[str, Any]],
    *,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """The canonical output (programme section 9.6): counts exact, rates to four
    decimals, every seed, permutation and AR parameter recorded."""
    cfg = study.config
    rule = {k: float(v) for k, v in cfg["pass_rule"].items()}
    g = len(study.panel.sessions)
    gens: dict[str, Any] = {}
    verdicts = []
    for gen, res in sorted(results.items()):
        idx = sorted(res)
        block: dict[str, Any] = {
            "base_seed": int(cfg["generators"][gen]["base_seed"]),
            "n_trials": len(idx),
            "variants": {},
        }
        if gen == "A":
            block["permutations"] = [dict(p) for p in perms if p["trial"] in res]
            block["distinct_nulls_available"] = len(derangements(g))
        else:
            block["seeds"] = [block["base_seed"] + i for i in idx]
            block["ar1"] = study.ar1
        for k, fam in study.variants.items():
            recs = [res[i][k] for i in idx]
            summ = summarise(recs, fam, rule)
            verdicts.append(summ["pass"])
            block["variants"][k] = {
                "K": len(fam),
                "hypotheses": [h.id for h in fam],
                "summary": summ,
                "trials": [{"trial": i, **res[i][k]} for i in idx],
            }
        gens[gen] = block
    trips = {
        hid: {
            "trips": t.n,
            "mean_fixed_cost_bps": float(t.fixed_bps.mean()) if t.n else None,
            "fill_rate": float(t.fill_frac.mean()) if t.n else None,
            "trips_per_session": np.bincount(t.sess, minlength=g).tolist(),
            **t.diagnostics,
        }
        for hid, t in sorted(study.trips.items())
    }
    complete = (
        set(results) == set(cfg["generators"])
        and all(len(r) == int(cfg["n_trials"]) for r in results.values())
        and set(study.variants) == set(cfg["variants"])
    )
    passed = bool(verdicts) and all(verdicts)
    return round_floats(
        {
            "x-version": 1,
            "protocol": PROTOCOL,
            "kind": "rel06_known_null",
            "small_g_statement": small_g_statement(g),
            "G": g,
            "sessions": study.panel.sessions,
            "names": study.panel.names,
            "eligibility": study.eligible,
            "alpha": float(cfg["alpha"]),
            "pass_rule": rule,
            "registered_size_shares": int(cfg["registered_size_shares"]),
            "configuration": dict(cfg["configuration"]),
            "execution": dict(cfg["execution"]),
            "trip_tables": trips,
            "generators": gens,
            "complete": complete,
            "verdict": ("PASS" if passed else "FAIL")
            if complete
            else "INCOMPLETE (not a protocol verdict)",
            **dict(provenance),
        }
    )
