"""H1 reference implementation: E0 sections 3-10 written directly, small and self-contained.

This file imports nothing from ``iap``.  It exists so that
``python/tests/equivalence/test_h1_equivalence.py`` can assert that the generic
engine (EN1 forecasts, EN2 ledgers, EN3 projection, EN4 execution, EN5
session series) reproduces, byte for byte, what a direct reading of the frozen
E0 text produces on the committed fixtures (:mod:`fixtures`): the research
ledger, the executable ledger, the orders, the fills and the R_d series.

Section by section (E0 revision 6, ``research/v119/H1_ALPHA_IMPROVEMENT_SPEC.md``):

3   signal: two-factor model (market, sector orthogonalised against the market)
    on the previous 20 sessions' 5-minute returns, shrinkage 0.3 toward (1, 0),
    minimum 15 valid sessions, condition-number bound; residual over the
    formation window standardised by the trailing F-minute residual
    deviation floored at the universe's 5th percentile; S = -z; stale quote
    or missing bar -> ineligible, never a signal.
4   forecast: one pooled reversal coefficient per horizon fitted by least
    squares on the development subset (the 20 estimation sessions), shrunk by
    the James-Stein-type weight 1 - (se / rho)^2, fail closed (interval
    includes zero, rho <= 0 or n < m_min -> the horizon trades nothing).
6   construction: eligible = valid signal and |mu_hat| > 1.5 x round-trip
    cost at the provisional ticket; 20 % tails with >= 10 names per leg;
    inverse-volatility raw legs each summing to half the cohort gross
    (C / live cohorts); the constrained least-squares repair (own SciPy call,
    E0's constraint list); shares = floor(|w| C / mid + 0.5), minimum ticket;
    the item 7 skip rules; the joint book re-check; the no-trade band with
    its feasibility clause.
7   two ledgers: cohort intents, net orders per name, internal crosses at the
    decision mid, external fill allocated expiring-first then pro rata by
    largest remainder (ties to the lower cohort id), outstanding mandatory
    remainders (`closing`), cancelled discretionary ones.
8   hurdle mu_hat > 1.5 C_RT; held to expiry; mandatory expiry exits; the
    pre-close liquidation.
9   P_fill = M_arrival + side (spread/2 + impact) at t_decision + 500 ms under
    the conservative column (impact 1.5x, 75 % depth); failure cases.
10  NetPnL_d = Cash_close + sum q V_close - Equity_open; R_d = NetPnL_d / C in
    bps; residual inventory at the close valued at the stress column with a
    cause and carried into the next session's Equity_open.

Arithmetic conventions shared with the engine (part of what "equivalent"
means): sums of products via ``numpy.dot`` over arrays in (session, bar,
name) order; standard deviations with ``ddof=1``; ``floor(x + 0.5)`` share
rounding; dictionaries iterated in sorted-name order; cash and positions
updated per fill in that order.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy.optimize import minimize

# --------------------------------------------------------------------------- data access
def _cfg(fx: dict[str, Any]) -> dict[str, Any]:
    return fx["config"]


def _ts(fx: dict[str, Any], s: int, k: int) -> int:
    c = _cfg(fx)
    return c["t0_ns"] + s * c["session_ns"] + k * c["bar_ns"]


def _col(fx: dict[str, Any], series: str) -> int:
    return fx["series"].index(series)


def _bar_returns(fx: dict[str, Any], s: int, series: str) -> np.ndarray:
    m = fx["mids"][s, :, _col(fx, series)]
    return m[1:] / m[:-1] - 1.0


def _window_returns(fx: dict[str, Any], sessions: list[int], series: str) -> np.ndarray:
    return np.concatenate([_bar_returns(fx, s, series) for s in sessions])


# --------------------------------------------------------------------------- section 3: estimation
def estimate(fx: dict[str, Any], sessions: list[int]) -> dict[str, Any]:
    """Betas, sector orthogonalisation, residual deviations and eligibility from ``sessions`` (the previous 20)."""
    cfg = _cfg(fx)
    names = fx["names"]
    m_all = _window_returns(fx, sessions, fx["market"])
    gamma: dict[str, float] = {}
    sperp_all: dict[str, np.ndarray] = {}
    for sec, etf in fx["sector_etfs"].items():
        x_s = _window_returns(fx, sessions, etf)
        g = float(np.dot(x_s, m_all) / np.dot(m_all, m_all))
        gamma[sec] = g
        sperp_all[sec] = x_s - g * m_all
    out: dict[str, Any] = {"gamma": gamma, "beta_m": {}, "beta_s": {}, "cond": {}, "valid_sessions": {}, "s_raw": {}, "eligible": {}}
    shr = cfg["beta_shrinkage"]
    w = 1.0 - shr
    for n in names:
        valid = [s for s in sessions if not np.isnan(fx["mids"][s, :, _col(fx, n)]).any()]
        out["valid_sessions"][n] = len(valid)
        if len(valid) < cfg["min_valid_sessions"]:
            out["eligible"][n] = False
            continue
        sec = fx["sectors"][n]
        r = _window_returns(fx, valid, n)
        m = _window_returns(fx, valid, fx["market"])
        sp = _window_returns(fx, valid, fx["sector_etfs"][sec]) - gamma[sec] * m
        a, b, c = float(np.dot(m, m)), float(np.dot(m, sp)), float(np.dot(sp, sp))
        p, q = float(np.dot(m, r)), float(np.dot(sp, r))
        det = a * c - b * b
        bm = (c * p - b * q) / det
        bs = (a * q - b * p) / det
        tr, disc = a + c, math.sqrt((a - c) ** 2 + 4.0 * b * b)
        lmax, lmin = (tr + disc) / 2.0, (tr - disc) / 2.0
        cond = lmax / lmin if lmin > 0 else math.inf
        out["cond"][n] = cond
        if cond > cfg["condition_bound"]:
            out["eligible"][n] = False
            continue
        out["beta_m"][n] = w * bm + shr * 1.0
        out["beta_s"][n] = w * bs
        out["eligible"][n] = True
    return out


def _f_returns(fx: dict[str, Any], s: int, series: str, fb: int) -> np.ndarray:
    m = fx["mids"][s, :, _col(fx, series)]
    return m[fb:] / m[:-fb] - 1.0


def residual_series(fx: dict[str, Any], est: dict[str, Any], s: int, n: str, fb: int) -> np.ndarray:
    """F-bar residual returns of ``n`` in session ``s`` at bars fb..B-1 (the formation residual at each bar)."""
    sec = fx["sectors"][n]
    r_i = _f_returns(fx, s, n, fb)
    r_m = _f_returns(fx, s, fx["market"], fb)
    r_s = _f_returns(fx, s, fx["sector_etfs"][sec], fb)
    return r_i - est["beta_m"][n] * r_m - est["beta_s"][n] * (r_s - est["gamma"][sec] * r_m)


def residual_scale(fx: dict[str, Any], est: dict[str, Any], sessions: list[int], fb: int) -> dict[str, float]:
    """s_i: trailing deviation of the F-minute residuals over the valid window sessions, floored at the 5th percentile."""
    cfg = _cfg(fx)
    raw: dict[str, float] = {}
    for n in fx["names"]:
        if not est["eligible"][n]:
            continue
        valid = [s for s in sessions if not np.isnan(fx["mids"][s, :, _col(fx, n)]).any()]
        e = np.concatenate([residual_series(fx, est, s, n, fb) for s in valid])
        raw[n] = float(np.std(e, ddof=1))
    floor = float(np.percentile(np.array([raw[n] for n in fx["names"] if n in raw]), cfg["vol_floor_percentile"]))
    return {n: max(v, floor) for n, v in raw.items()}


def signal_at(fx: dict[str, Any], est: dict[str, Any], scale: dict[str, float], s: int, k: int, fb: int) -> dict[str, dict[str, float]]:
    """Valid signals at bar ``k`` of session ``s``: {name: {e, z, S}}; a name with any failed input is absent."""
    cfg = _cfg(fx)
    stale = {(a, b, n) for a, b, n, _age in fx["stale"]}
    out: dict[str, dict[str, float]] = {}
    mids = fx["mids"][s]
    for n in fx["names"]:
        if not est["eligible"][n] or (s, k, n) in stale:
            continue
        sec = fx["sectors"][n]
        ci, cm, cs = _col(fx, n), _col(fx, fx["market"]), _col(fx, fx["sector_etfs"][sec])
        if np.isnan(mids[k, ci]) or np.isnan(mids[k - fb, ci]):
            continue
        r_i = mids[k, ci] / mids[k - fb, ci] - 1.0
        r_m = mids[k, cm] / mids[k - fb, cm] - 1.0
        r_s = mids[k, cs] / mids[k - fb, cs] - 1.0
        e = r_i - est["beta_m"][n] * r_m - est["beta_s"][n] * (r_s - est["gamma"][sec] * r_m)
        z = e / scale[n]
        out[n] = {"e": float(e), "z": float(z), "S": float(-z)}
    del cfg
    return out


# --------------------------------------------------------------------------- section 4: forecast
def fit_rho(fx: dict[str, Any], dev_sessions: list[int], fb: int, hb: int) -> dict[str, Any]:
    """The pooled reversal coefficient on the development subset; fail closed under the quality rule."""
    cfg = _cfg(fx)
    est = estimate(fx, dev_sessions)
    B = cfg["bars_per_session"]
    xs: list[float] = []
    ys: list[float] = []
    cm = _col(fx, fx["market"])
    for s in dev_sessions:
        mids = fx["mids"][s]
        for k in range(fb, B - hb):
            for n in fx["names"]:
                if not est["eligible"][n]:
                    continue
                sec = fx["sectors"][n]
                ci, cs = _col(fx, n), _col(fx, fx["sector_etfs"][sec])
                if np.isnan(mids[k, ci]) or np.isnan(mids[k - fb, ci]) or np.isnan(mids[k + hb, ci]):
                    continue
                r_i = mids[k, ci] / mids[k - fb, ci] - 1.0
                r_m = mids[k, cm] / mids[k - fb, cm] - 1.0
                r_s = mids[k, cs] / mids[k - fb, cs] - 1.0
                e = r_i - est["beta_m"][n] * r_m - est["beta_s"][n] * (r_s - est["gamma"][sec] * r_m)
                f_i = mids[k + hb, ci] / mids[k, ci] - 1.0
                f_m = mids[k + hb, cm] / mids[k, cm] - 1.0
                f_s = mids[k + hb, cs] / mids[k, cs] - 1.0
                y = f_i - est["beta_m"][n] * f_m - est["beta_s"][n] * (f_s - est["gamma"][sec] * f_m)
                xs.append(e * 1e4)
                ys.append(y * 1e4)
    x, y = np.array(xs), np.array(ys)
    n = int(x.size)
    xm, ym = float(np.mean(x)), float(np.mean(y))
    dx, dy = x - xm, y - ym
    sxx = float(np.dot(dx, dx))
    slope = float(np.dot(dx, dy)) / sxx
    inter = ym - slope * xm
    resid = y - inter - slope * x
    se = math.sqrt(float(np.dot(resid, resid)) / (n - 2) / sxx)
    rho_ols = -slope
    passed = rho_ols > 0 and rho_ols - 1.96 * se > 0 and n >= cfg["m_min"]
    shrink = max(0.0, 1.0 - (se / rho_ols) ** 2) if passed else 0.0
    return {"rho_ols": rho_ols, "se": se, "n": n, "passed": bool(passed), "shrink": shrink, "rho": shrink * rho_ols if passed else 0.0}


# --------------------------------------------------------------------------- section 9: quotes and costs
def _quote(mid: float, spread_bps: float, size: int) -> tuple[float, float, int]:
    s = mid * spread_bps / 1e4
    return mid - s / 2, mid + s / 2, size


def _impact(q: int, mid: float, depth: int, vol: int, mult: float, cfg: dict[str, Any]) -> float:
    if q == 0:
        return 0.0
    d, v = max(depth, 1), max(vol, 1)
    return mult * (mid * (cfg["impact_c_depth"] * (q / d) + cfg["impact_c_volume"] * math.sqrt(q / v)))


def _fee(q: int, price: float, cfg: dict[str, Any]) -> float:
    return abs(q) * cfg["fee_per_share"] + abs(q) * price * 0.0


def expected_cost_bps(fx: dict[str, Any], n: str, q: int, mid_dec: float) -> float:
    """One-way cost in bps of notional at the decision-time quote under the conservative column."""
    cfg = _cfg(fx)
    col = cfg["column"]
    bid, ask, size = _quote(mid_dec, fx["spread_bps"][n], fx["depth"][n])
    mid = (bid + ask) / 2.0
    spread = ask - bid
    depth = int(math.floor(size * col["depth_haircut"]))
    per_share = spread / 2.0 + _impact(abs(q), mid, depth, fx["volume_5m"][n], col["impact_multiplier"], cfg)
    cost = abs(q) * per_share + _fee(q, mid, cfg)
    return 1e4 * cost / (abs(q) * mid) if q else 0.0


def simulate_fill(fx: dict[str, Any], s: int, k: int, n: str, qty: int, mid_dec: float, mandatory: bool, limit: float | None) -> dict[str, Any]:
    """E0 section 9 under the conservative column; returns {fill or None, reason}."""
    cfg = _cfg(fx)
    col = cfg["column"]
    ts = _ts(fx, s, k)
    arrival = ts + cfg["latency_ns"]
    side = 1 if qty > 0 else -1
    if [s, k, n] in fx["halts"]:
        return {"fill": None, "reason": "halt"}
    if [s, k, n] in fx["gaps"]:
        return {"fill": None, "reason": "quote disappeared before arrival"}
    age_s = 0.5
    for a, b, nn, age in fx["stale"]:
        if (a, b, nn) == (s, k, n):
            age_s = age
    if age_s > cfg["quote_stale_s"]:
        return {"fill": None, "reason": "stale quote"}
    depth_displayed = fx["depth"][n]
    for a, b, nn, d in fx.get("depth_overrides", []):
        if (a, b, nn) == (s, k, n):
            depth_displayed = d
    mid_arr = mid_dec * (1.0 + float(fx["arrival_drift"][s, k, fx["names"].index(n)]))
    bid, ask, size = _quote(mid_arr, fx["spread_bps"][n], depth_displayed)
    mid = (bid + ask) / 2.0
    cap = int(math.floor(cfg["participation_cap"] * fx["volume_5m"][n]))
    submitted = qty
    reason = ""
    if abs(qty) > cap:
        if cap < cfg["min_ticket_shares"]:
            return {"fill": None, "reason": "participation breach"}
        submitted = side * cap
        reason = "participation"
    if not mandatory and abs(submitted) < cfg["min_ticket_shares"]:
        return {"fill": None, "reason": "below the minimum ticket"}
    depth = int(math.floor(size * col["depth_haircut"]))
    if depth <= 0:
        return {"fill": None, "reason": "insufficient depth"}
    filled = min(abs(submitted), depth)
    if filled < abs(submitted):
        reason = (reason + "; " if reason else "") + "insufficient depth"
    impact = _impact(filled, mid, depth, fx["volume_5m"][n], col["impact_multiplier"], cfg)
    half = (ask - bid) / 2.0
    price = mid + side * (half + impact)
    if limit is not None and not mandatory and ((side > 0 and price > limit) or (side < 0 and price < limit)):
        return {"fill": None, "reason": "price through the entry threshold"}
    fill = {
        "ts": ts, "name": n, "qty": side * filled, "price": price, "fee": _fee(filled, price, cfg),
        "half_spread": half, "impact": impact, "mid_decision": mid_dec, "mid_arrival": mid, "column": col["name"],
    }
    return {"fill": fill, "reason": reason}


def stress_valuation(fx: dict[str, Any], n: str, mid: float, qty: int, cause: str) -> float:
    cfg = _cfg(fx)
    col = cfg["stress_column"]
    depth = int(math.floor(fx["depth"][n] * col["depth_haircut"]))
    spread = mid * fx["spread_bps"][n] / 1e4
    haircut = spread / 2.0 + _impact(abs(qty), mid, depth, fx["volume_5m"][n], col["impact_multiplier"], cfg)
    side = 1 if qty > 0 else -1
    return max(mid - side * haircut, 1e-9)


# --------------------------------------------------------------------------- section 6: construction
def repair(raw: dict[str, float], betas: dict[str, float], sectors: dict[str, str], caps: dict[str, float], cfg: dict[str, Any], gross_cap: float) -> dict[str, float] | None:
    """w* = argmin ||w - w_raw||^2 under E0 item 4's constraints; None when the solve is refused."""
    names = sorted(raw)
    r = np.array([raw[n] for n in names])
    sign = np.sign(r)
    beta = np.array([betas[n] for n in names])
    secs = sorted(set(sectors[n] for n in names))
    eq_a = np.ones((1, len(names)))
    eq_b = np.zeros(1)
    rows_a = [beta, -beta]
    rows_b = [cfg["beta_tol"], cfg["beta_tol"]]
    for s in secs:
        a = np.array([1.0 if sectors[n] == s else 0.0 for n in names])
        rows_a += [a, -a]
        rows_b += [cfg["sector_cap"], cfg["sector_cap"]]
    rows_a.append(sign.copy())
    rows_b.append(gross_cap)
    le_a, le_b = np.array(rows_a), np.array(rows_b)
    bounds = []
    for k, n in enumerate(names):
        hi = min(cfg["name_cap"], caps[n])
        bounds.append((0.0, hi) if sign[k] > 0 else (-hi, 0.0))
    lo = np.array([b[0] for b in bounds])
    hi = np.array([b[1] for b in bounds])
    x0 = np.clip(r, lo, hi)
    sol = cfg["solver"]
    res = minimize(
        lambda w: float((w - r) @ (w - r)), x0, jac=lambda w: 2.0 * (w - r), method=sol["method"], bounds=bounds,
        constraints=[{"type": "eq", "fun": lambda w: eq_a @ w - eq_b, "jac": lambda w: eq_a},
                     {"type": "ineq", "fun": lambda w: le_b - le_a @ w, "jac": lambda w: -le_a}],
        options={"ftol": sol["ftol"], "maxiter": sol["maxiter"]},
    )
    w = np.clip(np.asarray(res.x, dtype=float), lo, hi)
    worst = max(float(np.max(np.abs(eq_a @ w - eq_b))), float(np.max(le_a @ w - le_b)), float(np.max(lo - w)), float(np.max(w - hi)))
    if not res.success or worst > sol["constraint_tol"] or not np.all(np.isfinite(w)):
        return None
    return {n: float(v) for n, v in zip(names, w, strict=True)}


def book_check(weights: dict[str, float], betas: dict[str, float], sectors: dict[str, str], cfg: dict[str, Any], n_live: int, tol: float) -> bool:
    net = sum(weights.values())
    beta = sum(betas[n] * w for n, w in weights.items())
    gross = sum(abs(w) for w in weights.values())
    sec: dict[str, float] = {}
    for n, w in weights.items():
        sec[sectors[n]] = sec.get(sectors[n], 0.0) + w
    largest = max((abs(w) for w in weights.values()), default=0.0)
    if abs(net) > tol:
        return False
    if abs(beta) > n_live * cfg["beta_tol"] + tol:
        return False
    if any(abs(v) > n_live * cfg["sector_cap"] + tol for v in sec.values()):
        return False
    if largest > n_live * cfg["name_cap"] + tol:
        return False
    return not gross > 1.0 + tol


# --------------------------------------------------------------------------- section 7: the two ledgers (minimal)
def _distribute(available: int, mandatory: list[tuple[Any, int]], entering: list[tuple[Any, int]]) -> list[int]:
    out_m = [0] * len(mandatory)
    for i in sorted(range(len(mandatory)), key=lambda i: mandatory[i][0]):
        take = min(available, mandatory[i][1])
        out_m[i] = take
        available -= take
    out_e = [0] * len(entering)
    total = sum(c for _, c in entering)
    if entering and total > 0 and available > 0:
        if available >= total:
            out_e = [c for _, c in entering]
        else:
            floors = [available * c // total for _, c in entering]
            rem = [(available * c) % total for _, c in entering]
            left = available - sum(floors)
            for i in sorted(range(len(entering)), key=lambda i: (-rem[i], entering[i][0])):
                if left == 0:
                    break
                if floors[i] < entering[i][1]:
                    floors[i] += 1
                    left -= 1
            out_e = floors
    return out_m + out_e


class Variant:
    """One registered variant (family h1, one horizon): its cohorts, executable book and session values."""

    def __init__(self, horizon: str, capital: float) -> None:
        self.horizon = horizon
        self.C = capital
        self.cash = capital
        self.positions: dict[str, int] = {}
        self.cohorts: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.next_id = 1
        self.fills: list[dict[str, Any]] = []
        self.orders: list[dict[str, Any]] = []
        self.sessions: list[dict[str, Any]] = []
        self.last_valuation: dict[str, float] = {}
        self.r_bps: list[tuple[str, float]] = []

    # cohorts ------------------------------------------------------------
    def key(self) -> tuple[str, str, int]:
        k = ("h1", self.horizon, self.next_id)
        self.next_id += 1
        return k

    def open_cohort(self, key, formation_ts, decision_ts, expiry_ts, intents, forecasts, decisions) -> None:
        self.cohorts[key] = {
            "key": key, "formation_ts": formation_ts, "decision_ts": decision_ts, "expiry_ts": expiry_ts,
            "intents": dict(intents), "forecasts": {n: float(forecasts[n]) for n in intents}, "decisions": decisions,
            "state": "pending", "allocated": {}, "submitted": {}, "withheld": {}, "cancelled": {}, "outstanding": {},
            "refusal_reason": "",
        }

    def refuse_cohort(self, key, decision_ts, expiry_ts, reason) -> None:
        self.cohorts[key] = {
            "key": key, "formation_ts": decision_ts, "decision_ts": decision_ts, "expiry_ts": expiry_ts, "intents": {},
            "forecasts": {}, "decisions": {}, "state": "refused", "allocated": {}, "submitted": {}, "withheld": {},
            "cancelled": {}, "outstanding": {}, "refusal_reason": reason,
        }

    def intents_at(self, ts: int, pre_close: bool) -> list[dict[str, Any]]:
        out = []
        for key in sorted(self.cohorts):
            c = self.cohorts[key]
            if c["state"] == "pending" and c["decision_ts"] == ts:
                for n, q in c["intents"].items():
                    out.append({"key": key, "name": n, "qty": q, "mandatory": False, "kind": "entry"})
            elif c["state"] in ("live", "closing"):
                expiring = c["state"] == "live" and c["expiry_ts"] <= ts
                if c["state"] == "closing" or expiring or pre_close:
                    for n, q in c["allocated"].items():
                        if q:
                            out.append({"key": key, "name": n, "qty": -q, "mandatory": True, "kind": "exit"})
        return out

    def live_book(self, mids: dict[str, float]) -> dict[str, float]:
        """Executable book in weight units from allocated cohort positions."""
        w: dict[str, float] = {}
        for c in self.cohorts.values():
            for n, q in c["allocated"].items():
                if q:
                    w[n] = w.get(n, 0.0) + q * mids[n] / self.C
        return w

    # one decision --------------------------------------------------------
    def decide_and_settle(self, ts: int, mids: dict[str, float], withhold: set[str], pre_close: bool, fill_fn) -> None:
        intents = self.intents_at(ts, pre_close)
        names = sorted({i["name"] for i in intents})
        orders = {}
        for n in names:
            own = [i for i in intents if i["name"] == n]
            o = {"name": n, "mid_decision": float(mids[n]), "intents": own, "intended": sum(i["qty"] for i in own),
                 "mandatory": sum(i["qty"] for i in own if i["mandatory"]), "crossed": 0, "filled": 0, "outstanding": 0, "cancelled": 0, "fill": None}
            if n in withhold:
                o["submitted"] = o["mandatory"]
                o["intents"] = [i for i in own if i["mandatory"]]
            else:
                o["submitted"] = o["intended"]
            o["withheld"] = o["intended"] - o["submitted"]
            orders[n] = o
        for n in names:
            o = orders[n]
            if o["submitted"] != 0:
                f = fill_fn(n, o)
                if f is not None:
                    o["fill"] = f
                    o["filled"] = f["qty"]
        for n in names:
            o = orders[n]
            m = o["mid_decision"]
            pos_side = [i for i in o["intents"] if i["qty"] > 0]
            neg_side = [i for i in o["intents"] if i["qty"] < 0]
            plus, minus = sum(i["qty"] for i in pos_side), -sum(i["qty"] for i in neg_side)
            crossed = min(plus, minus)
            filled = o["filled"]
            heavy_pos = o["submitted"] > 0
            avail_pos = crossed + (filled if heavy_pos and filled > 0 else 0)
            avail_neg = crossed + (-filled if (not heavy_pos) and filled < 0 else 0)
            alloc: dict[tuple, int] = {}
            for side_intents, avail, sign in ((pos_side, avail_pos, 1), (neg_side, avail_neg, -1)):
                if not side_intents:
                    continue
                mand = [i for i in side_intents if i["mandatory"]]
                ent = [i for i in side_intents if not i["mandatory"]]
                got = _distribute(
                    avail,
                    [((self.cohorts[i["key"]]["expiry_ts"], (i["key"][2], i["key"][0], i["key"][1])), abs(i["qty"])) for i in mand],
                    [((i["key"][2], i["key"][0], i["key"][1]), abs(i["qty"])) for i in ent],
                )
                for i, g in zip(mand + ent, got, strict=True):
                    if g:
                        alloc[i["key"]] = alloc.get(i["key"], 0) + sign * g
            for i in o["intents"]:
                c = self.cohorts[i["key"]]
                a = alloc.get(i["key"], 0)
                c["submitted"][n] = c["submitted"].get(n, 0) + i["qty"]
                if a:
                    c["allocated"][n] = c["allocated"].get(n, 0) + a
                    if c["allocated"][n] == 0:
                        del c["allocated"][n]
                rem = i["qty"] - a
                if rem:
                    if i["mandatory"]:
                        c["outstanding"][n] = c["outstanding"].get(n, 0) + rem
                        o["outstanding"] += rem
                    else:
                        c["cancelled"][n] = c["cancelled"].get(n, 0) + rem
                        o["cancelled"] += rem
            for key in sorted(self.cohorts):
                c = self.cohorts[key]
                if c["state"] == "pending" and c["decision_ts"] == ts and n in c["intents"] and not any(i["key"] == key for i in o["intents"]):
                    c["withheld"][n] = c["withheld"].get(n, 0) + c["intents"][n]
            o["crossed"] = crossed
            if o["fill"] is not None:
                f = o["fill"]
                self.cash += -f["qty"] * f["price"] - f["fee"]
                self.positions[n] = self.positions.get(n, 0) + f["qty"]
                if self.positions[n] == 0:
                    del self.positions[n]
                self.fills.append(f)
                self._wf["latency_drift"] += -f["qty"] * (f["mid_arrival"] - f["mid_decision"])
                self._wf["half_spread"] += abs(f["qty"]) * f["half_spread"]
                self._wf["impact"] += abs(f["qty"]) * f["impact"]
                self._wf["fees"] += f["fee"]
            for kq in ("intended", "mandatory", "submitted", "withheld", "crossed", "filled", "outstanding", "cancelled"):
                self._qs[kq] += o[kq]
            self.orders.append({"ts": ts, "name": n, **{kq: o[kq] for kq in ("intended", "mandatory", "submitted", "withheld", "crossed", "filled", "outstanding", "cancelled")}})
        for key, c in self.cohorts.items():
            gross = sum(abs(q) for q in c["allocated"].values())
            if c["state"] == "pending" and c["decision_ts"] == ts:
                c["state"] = "live" if gross else "unfilled"
            if c["state"] == "live" and (c["expiry_ts"] <= ts or pre_close):
                c["state"] = "closed" if not gross else "closing"
            elif c["state"] == "closing" and not gross:
                c["state"] = "closed"

    # sessions -----------------------------------------------------------
    def open_session(self, session_id: str) -> None:
        self._session = session_id
        self._wf = {"latency_drift": 0.0, "half_spread": 0.0, "impact": 0.0, "fees": 0.0}
        self._qs = {k: 0 for k in ("intended", "mandatory", "submitted", "withheld", "crossed", "filled", "outstanding", "cancelled")}
        inventory = sum(q * self.last_valuation[n] for n, q in self.positions.items() if q != 0)
        self._cash_open = self.cash
        self._equity_open = self.cash + inventory

    def close_session(self, valuations: dict[str, tuple[float, str | None]]) -> None:
        for n, (price, _cause) in valuations.items():
            self.last_valuation[n] = price
        inventory = sum(q * valuations[n][0] for n, q in self.positions.items() if q != 0)
        net = self.cash + inventory - self._equity_open
        closing = [c for c in self.cohorts.values() if c["state"] == "closing"]
        self.sessions.append({
            "session_id": self._session, "equity_open": self._equity_open, "cash_open": self._cash_open, "cash_close": self.cash,
            "marked_inventory_close": inventory, "net_pnl": net, "carried": {n: q for n, q in self.positions.items() if q != 0},
            "closeout_failures": sorted((n, c["allocated"][n]) for c in self.cohorts.values() for n in c["allocated"] if c["allocated"][n]),
            "closing_cohorts": len(closing), "closing_gross_shares": sum(sum(abs(q) for q in c["allocated"].values()) for c in closing),
            "quantity_states": dict(self._qs), "waterfall_fill_lines": dict(self._wf),
        })
        self.r_bps.append((self._session, 1e4 * net / self.C))

    def research_rows(self) -> list[dict[str, Any]]:
        rows = []
        for key in sorted(self.cohorts):
            c = self.cohorts[key]
            for n in sorted(set(c["intents"]) | set(c["allocated"])):
                td = c["decisions"].get(n)
                rows.append({
                    "family_id": key[0], "horizon": key[1], "cohort_id": key[2], "name": n, "formation_ts": c["formation_ts"],
                    "decision_ts": c["decision_ts"], "expiry_ts": c["expiry_ts"], "state": c["state"], "forecast_bps": c["forecasts"].get(n),
                    "intended": c["intents"].get(n, 0), "submitted": c["submitted"].get(n, 0), "withheld": c["withheld"].get(n, 0),
                    "position": c["allocated"].get(n, 0), "outstanding": c["outstanding"].get(n, 0), "cancelled": c["cancelled"].get(n, 0),
                    "cushion_bps": (td["mu"] - (td["entry"] + td["exit"])) if td else None,
                    "risk_status": td["risk"] if td else None, "liquidity_status": td["liq"] if td else None,
                    "refusal_reason": c["refusal_reason"],
                })
        return rows


# --------------------------------------------------------------------------- the run
def run(fx: dict[str, Any]) -> dict[str, Any]:
    """E0 sections 3-10 on the fixture; returns the ledgers, orders, fills and R_d per variant plus the frozen fits."""
    cfg = _cfg(fx)
    C = cfg["capital"]
    B = cfg["bars_per_session"]
    E = cfg["n_estimation"]
    names = fx["names"]
    sectors = fx["sectors"]
    dev = list(range(E))
    fits = {h: fit_rho(fx, dev, hc["formation_bars"], hc["holding_bars"]) for h, hc in cfg["horizons"].items()}
    variants = {h: Variant(h, C) for h in cfg["horizons"]}
    for d in range(E, E + cfg["n_trading"]):
        window = list(range(d - cfg["beta_window_sessions"], d))
        est = estimate(fx, window)
        sid = f"s{d}"
        for h, var in variants.items():
            hc = cfg["horizons"][h]
            fb, hb, n_live = hc["formation_bars"], hc["holding_bars"], hc["live_cohorts"]
            G = 1.0 / n_live
            scale = residual_scale(fx, est, window, fb)
            fit = fits[h]
            var.open_session(sid)
            for k in range(B):
                ts = _ts(fx, d, k)
                mids = {n: float(fx["mids"][d, k, _col(fx, n)]) for n in names if not np.isnan(fx["mids"][d, k, _col(fx, n)])}
                pre_close = k == B - 1
                # ---- construction (sections 3, 4, 6, 8) -------------------------------------------------
                if fit["passed"] and fb <= k < B - 1:
                    sig = signal_at(fx, est, scale, d, k, fb)
                    key = var.key()
                    expiry_ts = ts + hb * cfg["bar_ns"]
                    reason = construct(fx, var, key, ts, ts - fb * cfg["bar_ns"], expiry_ts, sig, fit, est, scale, mids, G, n_live)
                    if reason:
                        var.refuse_cohort(key, ts, expiry_ts, reason)
                # ---- band (item 6) --------------------------------------------------------------------------
                intents = var.intents_at(ts, pre_close)
                withhold: set[str] = set()
                if intents:
                    agg: dict[str, dict[str, int]] = {}
                    for i in intents:
                        a = agg.setdefault(i["name"], {"intended": 0, "mandatory": 0})
                        a["intended"] += i["qty"]
                        if i["mandatory"]:
                            a["mandatory"] += i["qty"]
                    book = var.live_book(mids)
                    for n in sorted(agg):
                        disc = agg[n]["intended"] - agg[n]["mandatory"]
                        if disc == 0 or cfg["band_bps"] == 0:
                            continue
                        bps = abs(disc) * mids[n] / C * 1e4
                        if bps >= cfg["band_bps"]:
                            continue
                        after = dict(book)
                        for m_ in agg:
                            q = agg[m_]["mandatory"] + (0 if m_ == n else agg[m_]["intended"] - agg[m_]["mandatory"])
                            if q:
                                after[m_] = after.get(m_, 0.0) + q * mids[m_] / C
                        if book_check(after, est["beta_m"], sectors, cfg, n_live, n_live * cfg["book_net_tol"]):
                            withhold.add(n)
                # ---- execution (section 9) and settlement (section 7) ------------------------------------
                outcomes: dict[str, str] = {}

                def fill_fn(n: str, o: dict[str, Any], _k=k, _d=d, _outcomes=outcomes) -> dict[str, Any] | None:
                    mandatory = o["mandatory"] != 0
                    limit = None
                    if not mandatory:
                        c = next(var.cohorts[i["key"]] for i in o["intents"] if not i["mandatory"])
                        td = c["decisions"][n]
                        slack = td["mu"] - cfg["hurdle_multiple"] * (td["entry"] + td["exit"])
                        side = 1 if o["submitted"] > 0 else -1
                        limit = o["mid_decision"] * (1.0 + side * slack / 1e4)
                    r = simulate_fill(fx, _d, _k, n, o["submitted"], o["mid_decision"], mandatory, limit)
                    _outcomes[n] = r["reason"]
                    return r["fill"]

                var.decide_and_settle(ts, mids, withhold, pre_close, fill_fn)
                if pre_close:
                    vals: dict[str, tuple[float, str | None]] = {}
                    for n, q in var.positions.items():
                        if q == 0:
                            continue
                        reason = outcomes.get(n, "")
                        cause = "halt" if "halt" in reason else ("no quote" if "quote" in reason else "depth exhausted")
                        vals[n] = (stress_valuation(fx, n, mids[n], q, cause), cause)
                    var.close_session(vals)
    return {
        "fits": fits,
        "variants": {
            h: {
                "research_ledger": v.research_rows(),
                "executable_ledger": v.sessions,
                "orders": v.orders,
                "fills": v.fills,
                "r_bps": v.r_bps,
            }
            for h, v in variants.items()
        },
    }


def construct(fx, var: Variant, key, ts, formation_ts, expiry_ts, sig, fit, est, scale, mids, G, n_live) -> str:
    """Sections 6 and 8 for one cohort; returns '' when opened, else the refusal reason."""
    cfg = _cfg(fx)
    C = cfg["capital"]
    names = fx["names"]
    sectors = fx["sectors"]
    valid = sorted(sig)
    n_leg0 = int(math.floor(cfg["tails"] * len(valid)))
    if n_leg0 < cfg["min_names_per_leg"]:
        return f"fewer than {cfg['min_names_per_leg']} names per leg ({len(valid)} valid signals)"
    w0 = G / 2.0 / n_leg0
    decisions: dict[str, dict[str, Any]] = {}
    eligible: list[str] = []
    forecasts: dict[str, float] = {}
    for n in valid:
        mu = -(fit["rho"] * (sig[n]["e"] * 1e4))
        q0 = int(math.floor(w0 * C / mids[n] + 0.5))
        ce = expected_cost_bps(fx, n, q0, mids[n])
        cx = expected_cost_bps(fx, n, q0, mids[n])
        decisions[n] = {"S": sig[n]["S"], "mu": abs(mu), "entry": ce, "exit": cx, "risk": "pass", "liq": "pass"}
        forecasts[n] = mu
        if abs(mu) > cfg["hurdle_multiple"] * (ce + cx):
            eligible.append(n)
    n_leg = int(math.floor(cfg["tails"] * len(eligible)))
    if n_leg < cfg["min_names_per_leg"]:
        return f"fewer than {cfg['min_names_per_leg']} names per leg ({len(eligible)} eligible after the hurdle)"
    ranked = sorted(eligible, key=lambda n: (-sig[n]["S"], n))
    longs, shorts = ranked[:n_leg], ranked[-n_leg:]
    raw: dict[str, float] = {}
    inv_l = sum(1.0 / scale[n] for n in longs)
    inv_s = sum(1.0 / scale[n] for n in shorts)
    for n in longs:
        raw[n] = (G / 2.0) * (1.0 / scale[n]) / inv_l
    for n in shorts:
        raw[n] = -(G / 2.0) * (1.0 / scale[n]) / inv_s
    caps = {n: cfg["participation_cap"] * fx["volume_5m"][n] * mids[n] / C for n in raw}
    w = repair(raw, est["beta_m"], sectors, caps, cfg, G)
    if w is None:
        return "repair refused: not converged or out of tolerance"
    intents: dict[str, int] = {}
    for n in sorted(w):
        q = int(math.floor(abs(w[n]) * C / mids[n] + 0.5))
        if q < cfg["min_ticket_shares"]:
            continue
        intents[n] = q if w[n] > 0 else -q
    final = {n: q * mids[n] / C for n, q in intents.items()}
    n_long = sum(1 for v in final.values() if v > 0)
    n_short = sum(1 for v in final.values() if v < 0)
    gross = sum(abs(v) for v in final.values())
    largest = max((abs(v) for v in final.values()), default=0.0)
    if n_long < cfg["min_names_per_leg"] or n_short < cfg["min_names_per_leg"]:
        return f"fewer than {cfg['min_names_per_leg']} names per leg (long {n_long}, short {n_short})"
    if gross < cfg["min_deployed_fraction"] * G:
        return f"deployed gross {gross:.6f} below {cfg['min_deployed_fraction']:.0%} of the target {G:.6f}"
    if gross > 0 and largest / gross > cfg["max_name_share_of_gross"]:
        return f"single name at {largest / gross:.4f} of cohort gross exceeds {cfg['max_name_share_of_gross']}"
    joint = var.live_book(mids)
    for n, v in final.items():
        joint[n] = joint.get(n, 0.0) + v
    if not book_check(joint, est["beta_m"], sectors, cfg, n_live, n_live * cfg["book_net_tol"]):
        return "combined book outside a hard constraint after the solve"
    var.open_cohort(key, formation_ts, ts, expiry_ts, intents, {n: forecasts[n] for n in intents}, {n: decisions[n] for n in intents})
    del names
    return ""
