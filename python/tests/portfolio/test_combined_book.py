"""Track C (v2.0 U0 stage 9): C0-C2 combined book, U11 netting and the U12 report on synthetic multi-family fixtures.

Synthetic only: three families on a seeded random panel (two at 10m that
may be combined, one at 20m that may not), no real or sealed data.  The tests
check conservation (cash identity, no fill created or lost, charges split
exactly), refusal on missing or inconsistent fields, the V2-C8 horizon rules,
byte-identical reruns and the incremental-net-value comparison against C0.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest
from iap.alpha.combined_book import (
    C2_FAMILY,
    BookPanel,
    C2Protocol,
    CombinedBookError,
    ExecutionSpec,
    FamilySpec,
    combine_within_horizon,
    run_configuration,
    solve_c2_bucket,
)
from iap.alpha.forecast import ForecastError, ForecastRecord
from iap.alpha.registry import ForecastRegistry, RegistryEntry
from iap.contracts.versions import canonical_json
from iap.core.rng import SplitMix64
from iap.execution.sim import FeeSchedule, ImpactCoefficients, Market, Quote
from iap.portfolio.covariance import ewma_covariance
from iap.portfolio.netting import ConservationError, attribute, check_conservation
from iap.portfolio.project import ConstraintSpec, SolverIdentity
from iap.reports.u12 import (
    U12Error,
    build_report,
    freeze_configuration,
    incremental_net_value,
    report_json,
)

T0 = 1_700_000_000_000_000_000
STEP = 5 * 60 * 10**9
N_NAMES, N_SESSIONS, STEPS = 24, 5, 10
CAPITAL = 10_000_000.0
FAMILY_SPEC = ConstraintSpec(
    beta_tol=0.05,
    sector_cap=0.05,
    name_cap=0.03,
    gross_cap=1.0,
    min_names_per_leg=3,
    min_deployed_fraction=0.5,
    max_name_share_of_gross=0.25,
)
BOOK_SPEC = ConstraintSpec(beta_tol=0.2, sector_cap=0.2, name_cap=0.08, gross_cap=1.2)
FAMILIES = (
    FamilySpec("fa_rev", "10m", 2, 0.3, FAMILY_SPEC),
    FamilySpec("fb_mom", "10m", 2, 0.3, FAMILY_SPEC),
    FamilySpec("fc_slow", "20m", 4, 0.4, FAMILY_SPEC),
)


def _uniform(rng: SplitMix64) -> float:
    return (rng.next_u64() >> 11) / float(1 << 53)


def _normal(rng: SplitMix64) -> float:
    return sum(_uniform(rng) for _ in range(12)) - 6.0


def build_world(seed: int = 7, *, drop_family: str | None = None):
    rng = SplitMix64(seed)
    names = tuple(f"N{i:02d}" for i in range(N_NAMES))
    sectors = {n: f"S{i % 4}" for i, n in enumerate(names)}
    betas = {n: 0.8 + 0.4 * _uniform(rng) for n in names}
    T = N_SESSIONS * STEPS
    fast = np.array([[_normal(rng) * 4e-4 for _ in names] for _ in range(T + 4)])
    slow = np.array([[_normal(rng) * 2e-4 for _ in names] for _ in range(T + 4)])
    noise = np.array([[_normal(rng) * 6e-4 for _ in names] for _ in range(T + 4)])
    rets = np.zeros((T, N_NAMES))
    for t in range(1, T):
        rets[t] = fast[t - 1] + 0.5 * slow[max(t - 2, 0)] + noise[t]
    px = 50.0 * np.exp(np.cumsum(rets, axis=0)) * np.array([1.0 + 0.02 * i for i in range(N_NAMES)])
    reg = ForecastRegistry()
    ids = {
        f.family_id: reg.register(
            RegistryEntry(
                f.family_id,
                f.horizon,
                "synthetic",
                {"seed": seed},
                "0" * 64,
                ("s0", "s0"),
                f"trackc_fixture:{seed}",
                "test",
            )
        )
        for f in FAMILIES
    }
    fnoise = {f.family_id: SplitMix64(seed * 31 + i) for i, f in enumerate(FAMILIES)}

    def mids(s, k):
        return {n: float(px[s * STEPS + k, j]) for j, n in enumerate(names)}

    def forecasts(s, k):
        t = s * STEPS + k
        out = []
        for f in FAMILIES:
            if f.family_id == drop_family:
                continue
            g = fnoise[f.family_id]
            for j, n in enumerate(names):
                if f.family_id == "fc_slow":
                    mu = 1e4 * slow[t, j] + 3.0 * _normal(g)
                elif f.family_id == "fa_rev":
                    mu = 1e4 * fast[t, j] + 3.0 * _normal(g)
                else:
                    mu = 0.5 * 1e4 * fast[t, j] - 1e4 * slow[t, j] + 4.0 * _normal(g)
                out.append(
                    ForecastRecord(
                        f.family_id, n, panel.ts(s, k), f.horizon, mu, 2.0, ids[f.family_id]
                    )
                )
        return out

    def market(s, k, n, arrival):
        m = mids(s, k)[n]
        hs = m * 1e-4
        return Market(n, Quote(arrival, m - hs, m + hs, 40_000, 40_000), 400_000)

    panel = BookPanel(
        capital=CAPITAL,
        names=names,
        betas=betas,
        sectors=sectors,
        n_sessions=N_SESSIONS,
        steps_per_session=STEPS,
        t0_ns=T0,
        session_ns=86_400 * 10**9,
        step_ns=STEP,
        mids=mids,
        forecasts=forecasts,
        market=market,
        cost_bps=lambda s, k, n: 1.5,
        liquidity_cap=lambda s, k, n: 0.05,
        registry=reg,
        book_spec=BOOK_SPEC,
        book_tol=1e-3,
        execution=ExecutionSpec(
            "conservative",
            ImpactCoefficients(),
            FeeSchedule(per_share=0.0005, dated="2026-01-01"),
            0.1,
            1,
            500_000_000,
        ),
    )
    cov = ewma_covariance(rets[:STEPS], shrinkage="ledoit_wolf", shrink_intensity=0.1)
    protocol = C2Protocol(
        risk_aversion=5000.0,
        turnover_penalty=0.1,
        combination_weights={"fa_rev": 0.5, "fb_mom": 0.5, "fc_slow": 1.0},
        covariance={"names": names, "cov": cov},
        estimator="ewma lam=0.94, constant-correlation shrinkage 0.1 (synthetic fixture)",
        window=("s0", "s0"),
        bucket_specs={
            "10m": dataclasses.replace(FAMILY_SPEC, name_cap=0.02),
            "20m": dataclasses.replace(FAMILY_SPEC, name_cap=0.01),
        },
        solver=SolverIdentity(),
    )
    return panel, protocol


@pytest.fixture(scope="module")
def world():
    return build_world()


@pytest.fixture(scope="module")
def runs(world):
    panel, protocol = world
    return {c: run_configuration(panel, FAMILIES, c, protocol=protocol) for c in ("C0", "C1", "C2")}


# --------------------------------------------------------------------------- conservation
@pytest.mark.parametrize("config", ["C0", "C1", "C2"])
def test_every_ledger_conserves_cash_fills_and_charges(runs, config):
    run = runs[config]
    assert run.session_rows and len(run.session_rows) == N_SESSIONS
    for led in run.ledgers.values():
        att = check_conservation(led)
        assert att.fills == len(led.fills)
        for r in led.sessions:
            assert abs(r.identity_gap) <= 1e-9 * max(1.0, r.equity_open)
            assert r.carried == {}  # flat-at-close families


def test_c1_nets_across_families_and_horizons_without_fictional_fills(runs):
    led = runs["C1"].ledgers["book"]
    att = attribute(led)
    assert att.crossed_shares > 0, "fixture should produce opposite intents to cross"
    assert att.fills == len(led.fills)  # a cross creates no Fill
    for row in att.crosses:
        assert row["buy_cohorts"] and row["sell_cohorts"]
    families = {k[0] for k in led.cohorts}
    assert {"fa_rev", "fb_mom", "fc_slow"} <= families
    # one external charge: the families' split equals the book's waterfall
    total = sum(
        r.waterfall["half_spread"] + r.waterfall["impact"] + r.waterfall["fees"]
        for r in led.sessions
    )
    split = sum(v["half_spread"] + v["impact"] + v["fees"] for v in att.by_family.values())
    assert split == pytest.approx(total, abs=1e-9)
    assert sum(v["net"] for v in att.by_family.values()) == pytest.approx(
        sum(r.net_pnl for r in led.sessions), abs=1e-6
    )


def test_c1_pays_less_external_charge_than_independent_sleeves(runs):
    c0 = sum(
        r.waterfall["half_spread"] + r.waterfall["impact"] + r.waterfall["fees"]
        for led in runs["C0"].ledgers.values()
        for r in led.sessions
    )
    c1 = sum(
        r.waterfall["half_spread"] + r.waterfall["impact"] + r.waterfall["fees"]
        for r in runs["C1"].ledgers["book"].sessions
    )
    assert c1 < c0


def test_tampered_event_log_is_caught(world):
    panel, _ = world
    run = run_configuration(panel, FAMILIES, "C1")
    led = run.ledgers["book"]
    fill = next(e for e in led.events if e["event"] == "fill")
    led.events.append(dict(fill, ts=fill["ts"] + 1))  # a fill created from nothing
    with pytest.raises(ConservationError):
        check_conservation(led)


def test_lost_fill_is_caught(world):
    panel, _ = world
    led = run_configuration(panel, FAMILIES, "C1").ledgers["book"]
    led.fills.pop()
    with pytest.raises(ConservationError):
        check_conservation(led)


# --------------------------------------------------------------------------- horizons (V2-C8)
def test_c2_cohorts_are_per_horizon_bucket_and_obligations_are_kept(runs):
    led = runs["C2"].ledgers["book"]
    keys = sorted(led.cohorts)
    assert keys and all(k[0] == C2_FAMILY for k in keys)
    assert {k[1] for k in keys} <= {"10m", "20m"}
    # an obligation ends at its expiry or at the declared pre-close exit, never earlier
    for c in led.cohorts.values():
        if c.attribution_closed_ts is not None:
            assert (
                c.attribution_closed_ts >= c.expiry_ts
                or c.early_exit_reason == "pre-close liquidation"
            )
    assert runs["C2"].joint_checks > 0


def test_forecasts_are_never_combined_across_horizons():
    rid = "a" * 64
    a = {"x": ForecastRecord("fa", "x", 0, "10m", 1.0, 1.0, rid)}
    b = {"x": ForecastRecord("fb", "x", 0, "20m", 1.0, 1.0, rid)}
    with pytest.raises(CombinedBookError, match="V2-C8"):
        combine_within_horizon({"fa": a, "fb": b}, {"fa": 1.0, "fb": 1.0}, "10m")
    same = {
        "x": ForecastRecord("fb", "x", 0, "10m", 3.0, 1.0, rid),
        "y": ForecastRecord("fb", "y", 0, "10m", 3.0, 1.0, rid),
    }
    assert combine_within_horizon({"fa": a, "fb": same}, {"fa": 0.5, "fb": 0.5}, "10m") == {
        "x": 2.0
    }


def test_c2_solve_treats_live_exposure_as_fixed():
    names = ("a", "b", "c", "d")
    proto = C2Protocol(
        1.0,
        0.0,
        {"f": 1.0},
        {"names": names, "cov": np.eye(4) * 1e-6},
        "identity",
        ("s0", "s0"),
        {"1m": ConstraintSpec(min_names_per_leg=1)},
        SolverIdentity(),
    )
    spec = ConstraintSpec(beta_tol=1.0, sector_cap=1.0, name_cap=0.05, gross_cap=1.0)
    kw = dict(
        protocol=proto,
        gross=0.2,
        book_spec=spec,
        betas=dict.fromkeys(names, 1.0),
        sectors=dict.fromkeys(names, "s"),
        liquidity_caps=dict.fromkeys(names, 1.0),
    )
    mu = {"a": 10.0, "b": 10.0, "c": -10.0, "d": -10.0}
    w_free, reason, _ = solve_c2_bucket(mu, dict.fromkeys(names, 0.0), {}, **kw)
    assert not reason and w_free["a"] == pytest.approx(0.05, abs=1e-6)
    # a live cohort already at the name cap in 'a': the new decision cannot add to it
    w_fixed, reason, _ = solve_c2_bucket(
        mu, dict.fromkeys(names, 0.0), {"a": 0.05, "c": -0.05}, **kw
    )
    assert not reason and abs(w_fixed.get("a", 0.0)) <= 1e-8
    assert abs(w_fixed.get("c", 0.0)) <= 1e-8


# --------------------------------------------------------------------------- refusals
def test_forecast_with_missing_field_is_refused():
    doc = ForecastRecord("fa", "x", 0, "10m", 1.0, 1.0, "a" * 64).to_dict()
    del doc["uncertainty_bps"]
    with pytest.raises(ForecastError, match="missing"):
        ForecastRecord.from_dict(doc)


def test_forecast_from_unregistered_family_or_wrong_horizon_is_refused(world):
    panel, _ = world
    bad = dataclasses.replace(
        panel,
        forecasts=lambda s, k: [
            dataclasses.replace(r, horizon="20m") if r.family_id == "fa_rev" else r
            for r in panel.forecasts(s, k)
        ],
    )
    # fa_rev is registered at 10m: a 20m forecast is refused before the book sees it
    with pytest.raises(ForecastError):
        run_configuration(bad, FAMILIES, "C1")
    with pytest.raises(ForecastError, match="unregistered family"):
        run_configuration(panel, FAMILIES[:2], "C1")


def test_c2_without_a_complete_protocol_is_refused(world):
    panel, protocol = world
    with pytest.raises(CombinedBookError, match="protocol"):
        run_configuration(panel, FAMILIES, "C2")
    partial = dataclasses.replace(protocol, combination_weights={"fa_rev": 1.0})
    with pytest.raises(CombinedBookError, match="combination weight"):
        run_configuration(panel, FAMILIES, "C2", protocol=partial)
    with pytest.raises(CombinedBookError):
        dataclasses.replace(protocol, risk_aversion=float("nan"))
    with pytest.raises(CombinedBookError, match="estimator"):
        dataclasses.replace(protocol, estimator="")


def test_bad_family_declarations_are_refused():
    with pytest.raises(CombinedBookError):
        FamilySpec("x", "10m", 0, 0.5, FAMILY_SPEC)
    with pytest.raises(CombinedBookError, match="budgets"):
        run_configuration(
            None,
            (
                FamilySpec("a", "10m", 2, 0.7, FAMILY_SPEC),
                FamilySpec("b", "10m", 2, 0.7, FAMILY_SPEC),
            ),
            "C1",
        )
    with pytest.raises(CombinedBookError, match="holding steps"):
        run_configuration(
            None,
            (
                FamilySpec("a", "10m", 2, 0.3, FAMILY_SPEC),
                FamilySpec("b", "10m", 3, 0.3, FAMILY_SPEC),
            ),
            "C1",
        )


# --------------------------------------------------------------------------- U12 report
def test_u12_report_compares_c1_c2_with_c0(runs):
    rep = build_report(runs, capital=CAPITAL, panel_identity="trackc_fixture:7")
    assert rep["selection_event"]["kind"] == "model selection"
    assert rep["selection_event"]["partition"] == "development"
    inc = rep["incremental_net_value_vs_C0"]
    assert set(inc) == {"C1", "C2"}
    c0 = dict(map(tuple, rep["configurations"]["C0"]["r_bps"]))
    c1 = dict(map(tuple, rep["configurations"]["C1"]["r_bps"]))
    assert inc["C1"]["total_bps"] == pytest.approx(sum(c1[s] - c0[s] for s in c0), abs=1e-9)
    assert inc["C1"]["n_sessions"] == N_SESSIONS
    for blk in rep["configurations"].values():
        assert blk["conservation"] == "passed"
    assert rep["configurations"]["C2"]["protocol"]["risk_aversion"] == 5000.0
    frozen = freeze_configuration(rep, "C1", "synthetic fixture: exercise the record only")
    assert len(frozen["report_hash"]) == 64
    with pytest.raises(U12Error):
        freeze_configuration(rep, "C1", " ")


def test_u12_refuses_without_baseline_or_on_sealed_partition(runs):
    with pytest.raises(U12Error, match="C0"):
        build_report({"C1": runs["C1"]}, capital=CAPITAL, panel_identity="x")
    with pytest.raises(U12Error, match="development"):
        build_report(runs, capital=CAPITAL, panel_identity="x", partition="sealed")
    with pytest.raises(U12Error, match="different sessions"):
        incremental_net_value([("s0", 1.0), ("s1", 2.0)], [("s0", 1.0)])


def test_reruns_are_byte_identical(world, runs):
    panel, protocol = world
    first = report_json(build_report(runs, capital=CAPITAL, panel_identity="trackc_fixture:7"))
    panel2, protocol2 = build_world()
    again = {
        c: run_configuration(panel2, FAMILIES, c, protocol=protocol2) for c in ("C0", "C1", "C2")
    }
    second = report_json(build_report(again, capital=CAPITAL, panel_identity="trackc_fixture:7"))
    assert first == second
    assert canonical_json(runs["C1"].ledgers["book"].events) == canonical_json(
        again["C1"].ledgers["book"].events
    )
