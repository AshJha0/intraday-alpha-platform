"""F7 rule 5: the Almgren-Chriss eta path (``iap.execution.optimal``):
``ac_params_from_calibration`` (eta from the impact coefficient, both unit
forms, both impact sources resolved to a coefficient), ``ac_kappa``,
``ac_holdings`` / ``ac_trajectory`` with the B3 drift, ``ac_cost`` and
``ac_drift_from_signal``."""

from __future__ import annotations

from dataclasses import replace

from iap.execution.optimal import (
    ACParams,
    ac_cost,
    ac_drift_from_signal,
    ac_kappa,
    ac_params_from_calibration,
    ac_trajectory,
)

from . import SLICE_ADV, SLICE_PROVENANCE, SLICE_TICK, base_document
from ._slice import imbalance_er, samples

NAME = "expected_f7_almgren_chriss.json"


def params_row(p: ACParams) -> dict:
    return {
        "sigma": p.sigma,
        "eta": p.eta,
        "gamma": p.gamma,
        "eps": p.eps,
        "horizon": p.horizon,
        "risk_aversion": p.risk_aversion,
    }


def params_from_row(r: dict) -> ACParams:
    return ACParams(**r)


CALIBRATION_CASES = [
    {
        "coeff": 2.0,
        "sigma": 0.02,
        "price": 100.0,
        "adv": 1_000_000.0,
        "horizon": 1.0,
        "adv_per_day": None,
        "horizon_s": None,
        "session_s": 23_400.0,
        "permanent_fraction": 0.0,
        "eps": 0.0,
        "risk_aversion": 1e-6,
    },
    {
        "coeff": 2.0,
        "sigma": 0.02,
        "price": 100.0,
        "adv": 1_000_000.0,
        "horizon": None,
        "adv_per_day": None,
        "horizon_s": None,
        "session_s": 23_400.0,
        "permanent_fraction": 0.25,
        "eps": 0.005,
        "risk_aversion": 1e-5,
    },
    {
        "coeff": 1.37,
        "sigma": 0.0183,
        "price": 163.22,
        "adv": None,
        "horizon": None,
        "adv_per_day": 14_451_444.0,
        "horizon_s": 120.0,
        "session_s": 23_400.0,
        "permanent_fraction": 0.1,
        "eps": 0.0,
        "risk_aversion": 2e-6,
    },
    {
        "coeff": 0.8,
        "sigma": 0.5,
        "price": 1.1,
        "adv": 5e9,
        "horizon": 0.25,
        "adv_per_day": None,
        "horizon_s": None,
        "session_s": 23_400.0,
        "permanent_fraction": 0.5,
        "eps": 1e-5,
        "risk_aversion": 0.0,
    },
    {
        "coeff": 3.3,
        "sigma": 0.04,
        "price": 50.0,
        "adv": None,
        "horizon": None,
        "adv_per_day": 2_500_000.0,
        "horizon_s": 3_600.0,
        "session_s": 23_400.0,
        "permanent_fraction": 0.0,
        "eps": 0.0,
        "risk_aversion": 1e-4,
    },
    # errors
    {
        "coeff": 2.0,
        "sigma": 0.02,
        "price": 100.0,
        "adv": 1e6,
        "horizon": None,
        "adv_per_day": 1e6,
        "horizon_s": 10.0,
        "session_s": 23_400.0,
        "permanent_fraction": 0.0,
        "eps": 0.0,
        "risk_aversion": 1e-6,
    },
    {
        "coeff": 2.0,
        "sigma": 0.02,
        "price": 100.0,
        "adv": None,
        "horizon": None,
        "adv_per_day": 1e6,
        "horizon_s": None,
        "session_s": 23_400.0,
        "permanent_fraction": 0.0,
        "eps": 0.0,
        "risk_aversion": 1e-6,
    },
    {
        "coeff": 2.0,
        "sigma": 0.02,
        "price": 100.0,
        "adv": None,
        "horizon": None,
        "adv_per_day": None,
        "horizon_s": None,
        "session_s": 23_400.0,
        "permanent_fraction": 0.0,
        "eps": 0.0,
        "risk_aversion": 1e-6,
    },
    {
        "coeff": 2.0,
        "sigma": 0.02,
        "price": 0.0,
        "adv": 1e6,
        "horizon": 1.0,
        "adv_per_day": None,
        "horizon_s": None,
        "session_s": 23_400.0,
        "permanent_fraction": 0.0,
        "eps": 0.0,
        "risk_aversion": 1e-6,
    },
]


def _traj_case(p: ACParams, qty: float, n: int, drift: float) -> dict:
    return {"params": params_row(p), "qty": qty, "n": n, "drift": drift}


TRAJECTORY_CASES = [
    _traj_case(ACParams(sigma=0.02, eta=2e-6, horizon=1.0, risk_aversion=1e-6), 10_000.0, 10, 0.0),
    _traj_case(
        ACParams(sigma=0.02, eta=2e-6, horizon=1.0, risk_aversion=1e-6), 10_000.0, 10, 0.004
    ),
    _traj_case(
        ACParams(sigma=0.02, eta=2e-6, horizon=1.0, risk_aversion=1e-6), 10_000.0, 10, -0.004
    ),
    _traj_case(ACParams(sigma=0.02, eta=2e-6, horizon=1.0, risk_aversion=0.0), 10_000.0, 8, 0.0),
    _traj_case(ACParams(sigma=0.02, eta=2e-6, horizon=1.0, risk_aversion=0.0), 10_000.0, 8, 0.01),
    _traj_case(ACParams(sigma=0.0, eta=2e-6, horizon=1.0, risk_aversion=1e-3), 1_000.0, 4, 0.0),
    _traj_case(ACParams(sigma=0.0, eta=2e-6, horizon=1.0, risk_aversion=1e-3), 1_000.0, 4, -0.02),
    _traj_case(
        ACParams(sigma=0.03, eta=5e-7, gamma=2e-7, eps=0.002, horizon=0.5, risk_aversion=5e-5),
        25_000.0,
        12,
        0.0,
    ),
    _traj_case(
        ACParams(sigma=0.03, eta=5e-7, gamma=2e-7, eps=0.002, horizon=0.5, risk_aversion=5e-5),
        25_000.0,
        12,
        0.05,
    ),
    _traj_case(ACParams(sigma=0.5, eta=1e-9, horizon=0.25, risk_aversion=1e-2), 1e6, 6, 0.0),
    _traj_case(ACParams(sigma=0.5, eta=1e-9, horizon=0.25, risk_aversion=1e-2), 1e6, 6, -1.5),
    _traj_case(ACParams(sigma=0.02, eta=2e-6, horizon=1.0, risk_aversion=1e-6), 7.0, 1, 0.1),
]

DRIFT_CASES = [
    {"side": 0, "expected_return": 2e-4, "price": 100.0, "remaining_horizon": 0.5},
    {"side": 1, "expected_return": 2e-4, "price": 100.0, "remaining_horizon": 0.5},
    {
        "side": 0,
        "expected_return": -3.5e-5,
        "price": 163.22,
        "remaining_horizon": 0.0051282051282051,
    },
    {"side": 1, "expected_return": 0.0, "price": 163.22, "remaining_horizon": 1.0},
    {"side": 0, "expected_return": 1e-3, "price": 0.0, "remaining_horizon": 1.0},  # error
    {"side": 0, "expected_return": 1e-3, "price": 10.0, "remaining_horizon": 0.0},  # error
]


def expected_calibration() -> list:
    out = []
    for c in CALIBRATION_CASES:
        try:
            p = ac_params_from_calibration(
                sigma=c["sigma"],
                adv=c["adv"],
                price=c["price"],
                horizon=c["horizon"],
                risk_aversion=c["risk_aversion"],
                permanent_fraction=c["permanent_fraction"],
                eps=c["eps"],
                adv_per_day=c["adv_per_day"],
                horizon_s=c["horizon_s"],
                session_s=c["session_s"],
                config=_coeff_config(c["coeff"]),
            )
            out.append(params_row(p))
        except ValueError as exc:
            out.append({"error": str(exc)})
    return out


def _coeff_config(coeff: float):
    from iap.execution import ExecConfig

    return ExecConfig(impact_coeff_bps_per_pct_adv=coeff)


def expected_trajectories(cases) -> list:
    out = []
    for c in cases:
        p = params_from_row(c["params"])
        n = c["n"]
        try:
            kappa = ac_kappa(p, n)
            tr = ac_trajectory(c["qty"], p, n, c["drift"])
            e, v = ac_cost(c["qty"], p, n, tr)
            e0, v0 = ac_cost(c["qty"], p, n)
            out.append({"kappa": kappa, "trades": tr, "cost": [e, v], "cost_optimal": [e0, v0]})
        except ValueError as exc:
            out.append({"error": str(exc)})
    return out


def expected_drift() -> list:
    out = []
    for c in DRIFT_CASES:
        try:
            out.append(
                ac_drift_from_signal(
                    c["side"], c["expected_return"], c["price"], c["remaining_horizon"]
                )
            )
        except ValueError as exc:
            out.append({"error": str(exc)})
    return out


SLICE_SIGMA = 0.0183
SLICE_COEFF = 2.0
SLICE_HORIZON_S = 120.0
SLICE_N = 6
SLICE_RISK = 2e-6


def slice_cases() -> tuple[dict, list]:
    """Per sample: price = the consolidated mid in currency, eta from the
    slice ADV in the explicit (adv_per_day, horizon_s) form, the trajectory
    re-solved over the whole horizon with the drift of the planted
    imbalance signal for a BUY, and the plain trajectory."""
    inputs = dict(
        SLICE_PROVENANCE,
        sigma=SLICE_SIGMA,
        coeff=SLICE_COEFF,
        horizon_s=SLICE_HORIZON_S,
        session_s=23_400.0,
        n=SLICE_N,
        risk_aversion=SLICE_RISK,
        rule=(
            "per sample: price = 0.5 * (best_bid + best_ask) * tick; params = "
            "ac_params_from_calibration(sigma, price, adv_per_day=adv, horizon_s, "
            "risk_aversion, coeff); expected_return = (bid_sz - ask_sz) / (bid_sz + ask_sz) "
            "* 1e-4; drift = ac_drift_from_signal(0, expected_return, price, params.horizon); "
            "trades = ac_trajectory(10000, params, n, drift); plain = ac_trajectory(10000, "
            "params, n)"
        ),
    )
    rows = []
    for s in samples():
        price = 0.5 * (s["bid"] + s["ask"]) * SLICE_TICK
        p = ac_params_from_calibration(
            sigma=SLICE_SIGMA,
            price=price,
            adv_per_day=SLICE_ADV,
            horizon_s=SLICE_HORIZON_S,
            risk_aversion=SLICE_RISK,
            config=_coeff_config(SLICE_COEFF),
        )
        er = imbalance_er(s["bid_sz"], s["ask_sz"])
        drift = ac_drift_from_signal(0, er, price, p.horizon)
        tr = ac_trajectory(10_000.0, p, SLICE_N, drift)
        plain = ac_trajectory(10_000.0, p, SLICE_N)
        rows.append(
            {
                "k": s["k"],
                "price": price,
                "params": params_row(p),
                "kappa": ac_kappa(p, SLICE_N),
                "drift": drift,
                "trades": tr,
                "plain": plain,
                "cost": list(ac_cost(10_000.0, replace(p), SLICE_N, tr)),
            }
        )
    return inputs, rows


def document() -> dict:
    doc = base_document(
        "F7 rule 5 golden: the Almgren-Chriss eta path (iap.execution.optimal; API_TRADING.md "
        "sections 2.11 X-P2-15, 2.13 P0-5, 2.14 B3). calibration cases: "
        "ac_params_from_calibration with `coeff` already resolved (the slope or residual "
        "coefficient; the resolution from a calibration document is a dict read, not "
        "arithmetic) -> ACParams or {error}; trajectory cases: ac_kappa, "
        "ac_trajectory(qty, params, n, drift), ac_cost of those trades and of the "
        "optimal trades; drift cases: ac_drift_from_signal.",
        "PYTHONPATH=src python tools/make_golden_f7.py --only r05_almgren_chriss --force",
    )
    sl_inputs, sl_rows = slice_cases()
    doc["inputs"] = {
        "calibration": CALIBRATION_CASES,
        "trajectories": TRAJECTORY_CASES,
        "drift": DRIFT_CASES,
        "slice": sl_inputs,
    }
    doc["expected"] = {
        "calibration": expected_calibration(),
        "trajectories": expected_trajectories(TRAJECTORY_CASES),
        "drift": expected_drift(),
        "slice": sl_rows,
    }
    return doc
