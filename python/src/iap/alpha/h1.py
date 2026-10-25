"""H1, the first client of the v2.0 engine (E0 sections 3-10 on EN1-EN5).

This module holds what is H1-specific (E0 sections 3 and 4: the two-factor
signal and the pooled reversal forecast with its fail-closed quality rule)
and the driver that runs one H1 variant per registered horizon through the
family-agnostic engine:

- EN1 :mod:`iap.alpha.forecast` / :mod:`iap.alpha.registry`: every forecast is
  a :class:`~iap.alpha.forecast.ForecastRecord` whose registry entry is the
  frozen development fit; records are :func:`~iap.alpha.forecast.consume`\\ d at
  the decision time;
- EN3 :mod:`iap.portfolio.project`: the repair, the item 7 skip rules, the joint
  book re-check and the no-trade band;
- EN2 :mod:`iap.portfolio.ledger`: cohorts, net orders, allocation, the cash
  identity;
- EN4 :mod:`iap.execution.sim`: the one fill-price convention under the
  conservative column, the failure cases, the stress closeout valuation;
- EN5 :mod:`iap.reports.e5`: the session series R_d = NetPnL_d / C.

Input: an H1 *panel* in the format of ``research/v119/h1_reference/fixtures.py``
(mids per session, bar and series; arrival drift; per-name spread, depth and
5-minute volume; planted faults).  The real route (U-3) will produce the same
structure from data; the panel format is the H1 data contract until then.

``python/tests/equivalence/test_h1_equivalence.py`` asserts that :func:`run`
reproduces ``research/v119/h1_reference/reference.py`` byte for byte on the
committed fixtures (plan V2-C1: the test stays in CI forever).  Arithmetic
conventions shared with the reference are listed in that file's docstring.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from iap.alpha.forecast import ForecastRecord, consume
from iap.alpha.registry import ForecastRegistry, RegistryEntry
from iap.execution.sim import (
    FeeSchedule,
    ImpactCoefficients,
    Market,
    Quote,
    closeout_valuation,
    expected_cost_bps,
    simulate,
)
from iap.portfolio.ledger import CohortState, FamilyConfig, Ledger, TradeDecision, Valuation
from iap.portfolio.project import (
    ConstraintSpec,
    SolverIdentity,
    apply_band,
    check_book,
    feasibility,
    project,
)
from iap.reports.e5 import SessionRow, session_series

__all__ = ["H1Signal", "fit_rho", "run"]

FAMILY = "h1"


# --------------------------------------------------------------------------- E0 section 3
class H1Signal:
    """The frozen factor model of E0 section 3 estimated on a window of sessions of the panel."""

    def __init__(self, panel: dict[str, Any], sessions: list[int]) -> None:
        self.p = panel
        self.cfg = panel["config"]
        self.sessions = sessions
        self.col = {s: k for k, s in enumerate(panel["series"])}
        m_all = self._window(sessions, panel["market"])
        self.gamma: dict[str, float] = {}
        for sec, etf in panel["sector_etfs"].items():
            x_s = self._window(sessions, etf)
            self.gamma[sec] = float(np.dot(x_s, m_all) / np.dot(m_all, m_all))
        self.beta_m: dict[str, float] = {}
        self.beta_s: dict[str, float] = {}
        self.eligible: dict[str, bool] = {}
        self.valid_sessions: dict[str, int] = {}
        shr = self.cfg["beta_shrinkage"]
        w = 1.0 - shr
        for n in panel["names"]:
            valid = self._valid(n, sessions)
            self.valid_sessions[n] = len(valid)
            if len(valid) < self.cfg["min_valid_sessions"]:
                self.eligible[n] = False
                continue
            sec = panel["sectors"][n]
            r = self._window(valid, n)
            m = self._window(valid, panel["market"])
            sp = self._window(valid, panel["sector_etfs"][sec]) - self.gamma[sec] * m
            a, b, c = float(np.dot(m, m)), float(np.dot(m, sp)), float(np.dot(sp, sp))
            pp, q = float(np.dot(m, r)), float(np.dot(sp, r))
            det = a * c - b * b
            bm = (c * pp - b * q) / det
            bs = (a * q - b * pp) / det
            tr, disc = a + c, math.sqrt((a - c) ** 2 + 4.0 * b * b)
            lmax, lmin = (tr + disc) / 2.0, (tr - disc) / 2.0
            cond = lmax / lmin if lmin > 0 else math.inf
            if cond > self.cfg["condition_bound"]:
                self.eligible[n] = False  # ill-conditioned factor estimate: ineligible, never a signal
                continue
            self.beta_m[n] = w * bm + shr * 1.0
            self.beta_s[n] = w * bs
            self.eligible[n] = True

    def _valid(self, n: str, sessions: list[int]) -> list[int]:
        return [s for s in sessions if not np.isnan(self.p["mids"][s, :, self.col[n]]).any()]

    def _bars(self, s: int, series: str) -> np.ndarray:
        m = self.p["mids"][s, :, self.col[series]]
        return m[1:] / m[:-1] - 1.0

    def _window(self, sessions: list[int], series: str) -> np.ndarray:
        return np.concatenate([self._bars(s, series) for s in sessions])

    def _f(self, s: int, series: str, fb: int) -> np.ndarray:
        m = self.p["mids"][s, :, self.col[series]]
        return m[fb:] / m[:-fb] - 1.0

    def residuals(self, s: int, n: str, fb: int) -> np.ndarray:
        sec = self.p["sectors"][n]
        r_m = self._f(s, self.p["market"], fb)
        r_s = self._f(s, self.p["sector_etfs"][sec], fb)
        return self._f(s, n, fb) - self.beta_m[n] * r_m - self.beta_s[n] * (r_s - self.gamma[sec] * r_m)

    def scale(self, fb: int) -> dict[str, float]:
        """s_i floored at the universe's 5th percentile (E0 section 3)."""
        raw: dict[str, float] = {}
        for n in self.p["names"]:
            if not self.eligible[n]:
                continue
            e = np.concatenate([self.residuals(s, n, fb) for s in self._valid(n, self.sessions)])
            raw[n] = float(np.std(e, ddof=1))
        floor = float(np.percentile(np.array([raw[n] for n in self.p["names"] if n in raw]), self.cfg["vol_floor_percentile"]))
        return {n: max(v, floor) for n, v in raw.items()}

    def at(self, scale: dict[str, float], s: int, k: int, fb: int) -> dict[str, dict[str, float]]:
        """Valid signals {name: {e, z, S}} at bar k; invalid or missing inputs yield no entry."""
        stale = {(a, b, n) for a, b, n, _age in self.p["stale"]}
        mids = self.p["mids"][s]
        out: dict[str, dict[str, float]] = {}
        cm = self.col[self.p["market"]]
        for n in self.p["names"]:
            if not self.eligible[n] or (s, k, n) in stale:
                continue
            sec = self.p["sectors"][n]
            ci, cs = self.col[n], self.col[self.p["sector_etfs"][sec]]
            if np.isnan(mids[k, ci]) or np.isnan(mids[k - fb, ci]):
                continue
            r_i = mids[k, ci] / mids[k - fb, ci] - 1.0
            r_m = mids[k, cm] / mids[k - fb, cm] - 1.0
            r_s = mids[k, cs] / mids[k - fb, cs] - 1.0
            e = r_i - self.beta_m[n] * r_m - self.beta_s[n] * (r_s - self.gamma[sec] * r_m)
            z = e / scale[n]
            out[n] = {"e": float(e), "z": float(z), "S": float(-z)}
        return out


# --------------------------------------------------------------------------- E0 section 4
def fit_rho(panel: dict[str, Any], dev_sessions: list[int], fb: int, hb: int) -> dict[str, Any]:
    """The pooled reversal coefficient on the development subset, shrunk; fail closed under the quality rule."""
    cfg = panel["config"]
    sig = H1Signal(panel, dev_sessions)
    B = cfg["bars_per_session"]
    cm = sig.col[panel["market"]]
    xs: list[float] = []
    ys: list[float] = []
    for s in dev_sessions:
        mids = panel["mids"][s]
        for k in range(fb, B - hb):
            for n in panel["names"]:
                if not sig.eligible[n]:
                    continue
                sec = panel["sectors"][n]
                ci, cs = sig.col[n], sig.col[panel["sector_etfs"][sec]]
                if np.isnan(mids[k, ci]) or np.isnan(mids[k - fb, ci]) or np.isnan(mids[k + hb, ci]):
                    continue
                r_i = mids[k, ci] / mids[k - fb, ci] - 1.0
                r_m = mids[k, cm] / mids[k - fb, cm] - 1.0
                r_s = mids[k, cs] / mids[k - fb, cs] - 1.0
                e = r_i - sig.beta_m[n] * r_m - sig.beta_s[n] * (r_s - sig.gamma[sec] * r_m)
                f_i = mids[k + hb, ci] / mids[k, ci] - 1.0
                f_m = mids[k + hb, cm] / mids[k, cm] - 1.0
                f_s = mids[k + hb, cs] / mids[k, cs] - 1.0
                y = f_i - sig.beta_m[n] * f_m - sig.beta_s[n] * (f_s - sig.gamma[sec] * f_m)
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
    return {
        "rho_ols": rho_ols,
        "se": se,
        "n": n,
        "passed": bool(passed),
        "shrink": shrink,
        "rho": shrink * rho_ols if passed else 0.0,
        "quality_rule": "95 % interval excludes zero, rho > 0, n >= m_min; else no forecast, no trades, variant retained",
        "outcome_exposure": True,
    }


# --------------------------------------------------------------------------- the driver
def _quote(mid: float, spread_bps: float, size: int, ts: int) -> Quote:
    s = mid * spread_bps / 1e4
    return Quote(ts=ts, bid=mid - s / 2, ask=mid + s / 2, bid_size=size, ask_size=size)


def _market(panel: dict[str, Any], s: int, k: int, n: str, mid_dec: float, arrival: int) -> Market:
    if [s, k, n] in panel["halts"]:
        return Market(n, None, panel["volume_5m"][n], halted=True)
    if [s, k, n] in panel["gaps"]:
        return Market(n, None, panel["volume_5m"][n])
    age_s = 0.5
    for a, b, nn, age in panel["stale"]:
        if (a, b, nn) == (s, k, n):
            age_s = age
    depth = panel["depth"][n]
    for a, b, nn, d in panel.get("depth_overrides", []):
        if (a, b, nn) == (s, k, n):
            depth = d
    mid_arr = mid_dec * (1.0 + float(panel["arrival_drift"][s, k, panel["names"].index(n)]))
    return Market(n, _quote(mid_arr, panel["spread_bps"][n], depth, arrival - int(age_s * 1e9)), panel["volume_5m"][n])


def _scaled_spec(cfg: dict[str, Any], n_live: int) -> ConstraintSpec:
    return ConstraintSpec(
        beta_tol=n_live * cfg["beta_tol"], sector_cap=n_live * cfg["sector_cap"], name_cap=n_live * cfg["name_cap"], gross_cap=1.0
    )


def _live_book(led: Ledger, mids: dict[str, float], C: float) -> dict[str, float]:
    w: dict[str, float] = {}
    for c in led.cohorts.values():
        for n, q in c.allocated.items():
            if q:
                w[n] = w.get(n, 0.0) + q * mids[n] / C
    return w


def _construct(panel, led: Ledger, key, ts, formation_ts, expiry_ts, sig, fit, est: H1Signal, scale, mids, G, n_live, registry, registry_id) -> str:
    """E0 sections 6 and 8 for one cohort on the engine; '' when opened, else the refusal reason."""
    cfg = panel["config"]
    C = cfg["capital"]
    sectors = panel["sectors"]
    valid = sorted(sig)
    n_leg0 = int(math.floor(cfg["tails"] * len(valid)))
    if n_leg0 < cfg["min_names_per_leg"]:
        return f"fewer than {cfg['min_names_per_leg']} names per leg ({len(valid)} valid signals)"
    w0 = G / 2.0 / n_leg0
    fees = FeeSchedule(per_share=cfg["fee_per_share"])
    coeffs = ImpactCoefficients(c_depth=cfg["impact_c_depth"], c_volume=cfg["impact_c_volume"])
    records = [
        ForecastRecord(FAMILY, n, ts, key[1], -(fit["rho"] * (sig[n]["e"] * 1e4)), fit["se"] * abs(sig[n]["e"] * 1e4), registry_id)
        for n in valid
    ]
    forecasts = {r.name: r.mu_hat_bps for r in consume(records, t_decision=ts, registry=registry)}  # EN1: validated, available, registered
    decisions: dict[str, TradeDecision] = {}
    eligible: list[str] = []
    for n in valid:
        mu = forecasts[n]
        q0 = int(math.floor(w0 * C / mids[n] + 0.5))
        quote = _quote(mids[n], panel["spread_bps"][n], panel["depth"][n], ts)
        ce = expected_cost_bps(q0, quote, panel["volume_5m"][n], coeffs=coeffs, fees=fees)
        cx = expected_cost_bps(q0, quote, panel["volume_5m"][n], coeffs=coeffs, fees=fees)
        decisions[n] = TradeDecision(sig[n]["S"], abs(mu), ce, cx)
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
    caps = {n: cfg["participation_cap"] * panel["volume_5m"][n] * mids[n] / C for n in raw}
    spec = ConstraintSpec(cfg["beta_tol"], cfg["sector_cap"], cfg["name_cap"], G, cfg["min_names_per_leg"], cfg["min_deployed_fraction"], cfg["max_name_share_of_gross"])
    sol = cfg["solver"]
    res = project(raw, est.beta_m, sectors, spec=spec, liquidity_caps=caps, solver=SolverIdentity(sol["method"], sol["constraint_tol"], sol["ftol"], sol["maxiter"]))
    if res.refused:
        return f"repair refused: not converged or out of tolerance (active: {', '.join(res.active)})"
    intents: dict[str, int] = {}
    for n in sorted(res.weights):
        q = int(math.floor(abs(res.weights[n]) * C / mids[n] + 0.5))
        if q < cfg["min_ticket_shares"]:
            continue
        intents[n] = q if res.weights[n] > 0 else -q
    final = {n: q * mids[n] / C for n, q in intents.items()}
    feas = feasibility(final, gross_target=G, spec=spec)
    if not feas.feasible:
        return feas.reason
    joint = _live_book(led, mids, C)
    for n, v in final.items():
        joint[n] = joint.get(n, 0.0) + v
    chk = check_book(joint, est.beta_m, sectors, spec=_scaled_spec(cfg, n_live), tol=n_live * cfg["book_net_tol"])
    if not chk.ok:
        return "combined book outside a hard constraint after the solve"
    led.open_cohort(key, formation_ts=formation_ts, decision_ts=ts, expiry_ts=expiry_ts, intents=intents,
                    forecasts={n: forecasts[n] for n in intents}, decisions={n: decisions[n] for n in intents})
    return ""


def _collect(led: Ledger, ts: int, pre_close: bool) -> dict[str, dict[str, int]]:
    """Aggregate intended and mandatory quantity per name at ``ts`` (what the ledger's decide will net)."""
    agg: dict[str, dict[str, int]] = {}
    for key in sorted(led.cohorts):
        c = led.cohorts[key]
        if c.state is CohortState.PENDING and c.decision_ts == ts:
            for n, q in c.intents.items():
                a = agg.setdefault(n, {"intended": 0, "mandatory": 0})
                a["intended"] += q
        elif c.state in (CohortState.LIVE, CohortState.CLOSING):
            expiring = c.state is CohortState.LIVE and c.expiry_ts <= ts
            if c.state is CohortState.CLOSING or expiring or pre_close:
                for n, q in c.allocated.items():
                    if q:
                        a = agg.setdefault(n, {"intended": 0, "mandatory": 0})
                        a["intended"] -= q
                        a["mandatory"] -= q
    return agg


def run(panel: dict[str, Any]) -> dict[str, Any]:
    """Run every registered H1 horizon through the engine on ``panel``; the same output shape as the reference."""
    cfg = panel["config"]
    C = cfg["capital"]
    B = cfg["bars_per_session"]
    E = cfg["n_estimation"]
    names = panel["names"]
    sectors = panel["sectors"]
    bar = cfg["bar_ns"]
    col = {s: k for k, s in enumerate(panel["series"])}
    dev = list(range(E))
    registry = ForecastRegistry()
    fits: dict[str, dict[str, Any]] = {}
    registry_ids: dict[str, str] = {}
    for h, hc in cfg["horizons"].items():
        fit = fit_rho(panel, dev, hc["formation_bars"], hc["holding_bars"])
        fits[h] = fit
        registry_ids[h] = registry.register(RegistryEntry(
            FAMILY, h, "pooled_linear_reversal",
            {"rho": fit["rho"], "rho_ols": fit["rho_ols"], "se": fit["se"], "n": fit["n"], "shrink": fit["shrink"], "passed": fit["passed"], "formation_bars": hc["formation_bars"]},
            "0" * 64, (f"s{dev[0]}", f"s{dev[-1]}"), f"h1_panel:{panel['scenario']}:{panel['seed']}", "iap v2.0 stage 5",
            selection_history={"kind": "outcome exposure", "what": "rho_H fit and quality rule on the development subset"},
        ))
    ledgers = {h: Ledger() for h in cfg["horizons"]}
    for led in ledgers.values():
        led.register_family(FamilyConfig(FAMILY, flat_at_close=True))
        led.deposit(C)
    next_id = {h: 1 for h in cfg["horizons"]}
    orders: dict[str, list[dict[str, Any]]] = {h: [] for h in cfg["horizons"]}
    fees = FeeSchedule(per_share=cfg["fee_per_share"])
    coeffs = ImpactCoefficients(c_depth=cfg["impact_c_depth"], c_volume=cfg["impact_c_volume"])
    session_rows: dict[str, list[SessionRow]] = {h: [] for h in cfg["horizons"]}
    for d in range(E, E + cfg["n_trading"]):
        window = list(range(d - cfg["beta_window_sessions"], d))
        est = H1Signal(panel, window)
        sid = f"s{d}"
        for h, led in ledgers.items():
            hc = cfg["horizons"][h]
            fb, hb, n_live = hc["formation_bars"], hc["holding_bars"], hc["live_cohorts"]
            G = 1.0 / n_live
            scale = est.scale(fb)
            fit = fits[h]
            led.open_session(sid)
            traded = False
            for k in range(B):
                ts = cfg["t0_ns"] + d * cfg["session_ns"] + k * bar
                mids = {n: float(panel["mids"][d, k, col[n]]) for n in names if not np.isnan(panel["mids"][d, k, col[n]])}
                pre_close = k == B - 1
                if fit["passed"] and fb <= k < B - 1:
                    sig = est.at(scale, d, k, fb)
                    key = (FAMILY, h, next_id[h])
                    next_id[h] += 1
                    expiry_ts = ts + hb * bar
                    reason = _construct(panel, led, key, ts, ts - fb * bar, expiry_ts, sig, fit, est, scale, mids, G, n_live, registry, registry_ids[h])
                    if reason:
                        led.refuse_cohort(key, decision_ts=ts, expiry_ts=expiry_ts, reason=reason)
                # the no-trade band (E0 section 6 item 6) on the discretionary part of each aggregate order
                agg = _collect(led, ts, pre_close)
                withhold: frozenset[str] = frozenset()
                if agg and cfg["band_bps"] > 0:
                    book = _live_book(led, mids, C)
                    disc_bps = {n: (a["intended"] - a["mandatory"]) * mids[n] / C * 1e4 for n, a in agg.items()}
                    after: dict[str, dict[str, float]] = {}
                    for n in agg:
                        b = dict(book)
                        for m_, a in agg.items():
                            q = a["mandatory"] + (0 if m_ == n else a["intended"] - a["mandatory"])
                            if q:
                                b[m_] = b.get(m_, 0.0) + q * mids[m_] / C
                        after[n] = b
                    withhold, _sent = apply_band(disc_bps, {n: False for n in agg}, cfg["band_bps"], book_after_withholding=after,
                                                 betas=est.beta_m, sectors=sectors, spec=_scaled_spec(cfg, n_live), tol=n_live * cfg["book_net_tol"])
                decision = led.decide(ts, mids, withhold=withhold, pre_close=pre_close)
                outcomes: dict[str, str] = {}
                for n in sorted(decision.orders):
                    o = decision.orders[n]
                    if o.submitted == 0:
                        continue
                    mandatory = o.mandatory != 0
                    limit = None
                    if not mandatory:
                        c = next(led.cohorts[i.key] for i in o.intents if not i.mandatory)
                        td = c.decisions[n]
                        slack = td.mu_hat_bps - cfg["hurdle_multiple"] * (td.expected_entry_cost_bps + td.expected_exit_cost_bps)
                        side = 1 if o.submitted > 0 else -1
                        limit = o.mid_decision * (1.0 + side * slack / 1e4)
                    out = simulate(ts_decision=ts, name=n, qty=o.submitted, mid_decision=o.mid_decision,
                                   market=_market(panel, d, k, n, o.mid_decision, ts + cfg["latency_ns"]), column=cfg["column"]["name"],
                                   coeffs=coeffs, fees=fees, participation_cap=cfg["participation_cap"], min_ticket=cfg["min_ticket_shares"],
                                   limit_price=limit, mandatory=mandatory, latency_ns=cfg["latency_ns"])
                    outcomes[n] = out.reason
                    if out.fill is not None:
                        led.fill(out.fill)
                        traded = True
                led.settle()
                for n in sorted(decision.orders):
                    o = decision.orders[n]
                    orders[h].append({"ts": ts, "name": n, **{kq: getattr(o, kq) for kq in ("intended", "mandatory", "submitted", "withheld", "crossed", "filled", "outstanding", "cancelled")}})
                if pre_close:
                    vals: dict[str, Valuation] = {}
                    for n, q in led.positions.items():
                        if q == 0:
                            continue
                        reason = outcomes.get(n, "")
                        cause = "halt" if "halt" in reason else ("no quote" if "quote" in reason else "depth exhausted")
                        vals[n] = closeout_valuation(mids[n], q, panel["volume_5m"][n], panel["depth"][n], cause, coeffs,
                                                     reference_spread=mids[n] * panel["spread_bps"][n] / 1e4)
                    res = led.close_session(vals)
                    kind = "unpriced_at_close" if res.closeout_failures else ("traded" if traded else "usable_no_trades")
                    flags = tuple(sorted({f["cause"] for f in res.closeout_failures}))
                    session_rows[h].append(SessionRow(sid, kind, res.net_pnl, res.waterfall["midpoint_reference_pnl"], flags))
    out: dict[str, Any] = {"fits": fits, "registry": registry.to_document(), "variants": {}}
    for h, led in ledgers.items():
        series = session_series(FAMILY, h, session_rows[h], C)
        out["variants"][h] = {
            "research_ledger": led.research_rows(),
            "executable_ledger": [
                {
                    "session_id": r.session_id, "equity_open": r.equity_open, "cash_open": r.cash_open, "cash_close": r.cash_close,
                    "marked_inventory_close": r.marked_inventory_close, "net_pnl": r.net_pnl, "carried": r.carried,
                    "closeout_failures": sorted((f["name"], f["qty"]) for f in r.closeout_failures), "closing_cohorts": r.closing_cohorts,
                    "closing_gross_shares": r.closing_gross_shares, "quantity_states": r.quantity_states,
                    "waterfall_fill_lines": {k: r.waterfall[k] for k in ("latency_drift", "half_spread", "impact", "fees")},
                }
                for r in led.sessions
            ],
            "orders": orders[h],
            "fills": [
                {"ts": f.ts, "name": f.name, "qty": f.qty, "price": f.price, "fee": f.fee, "half_spread": f.half_spread, "impact": f.impact,
                 "mid_decision": f.mid_decision, "mid_arrival": f.mid_arrival, "column": f.column}
                for f in led.fills
            ],
            "r_bps": list(zip(series.session_ids, series.r_bps, strict=True)),
            "session_counts": series.counts,
            "waterfalls": [r.waterfall for r in led.sessions],
        }
    return out
