"""v1.17 WS-B items B2 (signal-aware urgency) and B3 (signal-aware pacing).

Synthetic streams only (``test_passive_policy.stream`` style): a planted
signal on a planted trend, default-off byte identity, re-evaluation through
the parent's life, thresholds in signal units, the AC drift sign, the
provenance record, and the benchmark driver's signal-aware policies.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from iap.core.codec import read_jsonl
from iap.core.events import EventType
from iap.execution import (
    AlgoType,
    AlphaUrgencyPolicy,
    ArraySignalSource,
    ExecPolicy,
    ExecutionReplay,
    ISModel,
    OrderType,
    PacingParams,
    ParentOrder,
)
from iap.execution.optimal import ACParams, ac_drift_from_signal, ac_holdings, ac_trajectory
from iap.execution.pacing import apportion, pacing_scale, tilted_slice_qty
from iap.execution.signal import URGENCY_REGIMES, SignalReading, zscore
from iap.execution.urgency import regime_from_z
from iap.research import execution_benchmark as bench
from test_exec_algos import SEC, T0, _ev, algo_config
from test_execution_golden import golden_config, golden_parents

REPO = Path(__file__).resolve().parents[2]
INS = 7


# ------------------------------------------------------------ streams --


def trending(step_every_s: int, direction: int, hit_every_s: int = 2, seconds: int = 200):
    """Venue-1 book that ticks one level in ``direction`` every ``step_every_s``
    seconds (a planted trend), with a marketable sell of 60 at the bid every
    ``hit_every_s`` seconds (so a posted bid can be filled) and a TRADE print
    every second (so POV has volume)."""
    evs = []
    seq = 0

    def push(ts, etype, side, px, qty, oid, tid=0):
        nonlocal seq
        seq += 1
        evs.append(_ev(seq, ts, etype, side, px, qty, oid, tid))

    bid, ask = 100, 101
    # Deep ladders on both sides so the touch is always two-sided.
    for k in range(40):
        push(T0 - SEC + 2 * k, EventType.ADD, 0, bid - k, 100_000, 100 + k)
        push(T0 - SEC + 2 * k + 1, EventType.ADD, 1, ask + k, 100_000, 200 + k)
    for i in range(seconds):
        ts = T0 + i * SEC
        push(ts, EventType.TRADE, 0, bid, 40, 0, 1000 + i)
        if hit_every_s and i % hit_every_s == hit_every_s - 1:
            push(ts + SEC // 4, EventType.ADD, 1, bid, 60, 5000 + i)
        if step_every_s and i % step_every_s == step_every_s - 1:
            if direction > 0:  # cancel the ask level, add a bid level: up one tick
                push(ts + SEC // 2, EventType.CANCEL, 1, ask, 100_000, 200 + (ask - 101))
                ask += 1
                bid += 1
                push(ts + SEC // 2 + 1, EventType.ADD, 0, bid, 100_000, 300 + i)
            else:
                push(ts + SEC // 2, EventType.CANCEL, 0, bid, 100_000, 100 + (100 - bid))
                bid -= 1
                ask -= 1
                push(ts + SEC // 2 + 1, EventType.ADD, 1, ask, 100_000, 400 + i)
        push(ts + 3 * SEC // 4, EventType.HEARTBEAT, 0, 0, 0, 0)
    return evs


def parent(algo=AlgoType.TWAP, qty=400, slices=4, **kw) -> ParentOrder:
    base = {
        "parent_id": 1,
        "instrument_id": INS,
        "venue_id": 1,
        "side": 0,
        "qty": qty,
        "algo": algo,
        "start_ts": T0,
        "end_ts": T0 + 100 * SEC,
        "slices": slices,
    }
    base.update(kw)
    return ParentOrder(**base)


def run(parents, events):
    return ExecutionReplay(algo_config(), list(parents)).run(events)


def constant(z: float, er: float = 0.0):
    """A signal that reads ``z`` for the whole stream (one reading at T0 - 1 s)."""
    return ArraySignalSource({INS: [(T0 - SEC, z, er)]})


NEVER_STALE = AlphaUrgencyPolicy(max_age_ns=0)
PACE = PacingParams(max_age_ns=0)


# ----------------------------------------------------------- signal API --


def test_signal_source_is_causal_and_reports_staleness():
    src = ArraySignalSource({INS: [(10, 1.0, 1e-4), (20, -1.0, -1e-4)]}, horizon_s=5.0)
    assert src.at(INS, 9) is None
    assert src.at(INS, 10) == SignalReading(10, 1.0, 1e-4, 5.0)
    assert src.at(INS, 19).z == 1.0 and src.at(INS, 20).z == -1.0
    assert src.at(99, 20) is None
    with pytest.raises(ValueError):
        ArraySignalSource({INS: [(10, 1.0, 0.0), (10, 2.0, 0.0)]})
    z, mean, std = zscore([1.0, 3.0, float("nan")])
    assert (mean, std) == (2.0, 1.0) and z == [-1.0, 1.0, 0.0]
    assert zscore([2.0, 2.0])[0] == [0.0, 0.0]


def test_regime_thresholds_are_in_signal_units():
    # buy: a negative z (price expected to fall) agrees with waiting
    assert regime_from_z(0, -1.0, 1.0) == "agree"
    assert regime_from_z(0, -0.99, 1.0) == "neutral"
    assert regime_from_z(0, 1.0, 1.0) == "adverse"
    assert regime_from_z(1, 1.0, 1.0) == "agree"
    assert regime_from_z(1, -2.0, 1.5) == "adverse"
    assert regime_from_z(0, 0.0, 0.0) == "neutral"
    assert regime_from_z(0, -1e-9, 0.0) == "agree"
    with pytest.raises(ValueError):
        AlphaUrgencyPolicy(threshold_z=-1)
    with pytest.raises(ValueError):
        AlphaUrgencyPolicy(passive_urgency=1.5)


def test_pacing_scale_and_tilt_never_go_negative():
    prm = PacingParams(gain=0.5, min_scale=0.25, max_scale=3.0)
    assert pacing_scale(0, None, prm) == 1.0
    assert pacing_scale(0, 0.0, prm) == 1.0
    assert pacing_scale(0, 1.0, prm) == 1.5  # buy, price expected to rise: more now
    assert pacing_scale(0, -1.0, prm) == 0.5  # buy, expected fall: less now
    assert pacing_scale(1, 1.0, prm) == 0.5  # sell, expected rise: wait
    assert pacing_scale(0, 10.0, prm) == 3.0 and pacing_scale(0, -10.0, prm) == 0.25
    assert tilted_slice_qty(100, [1.0, 1.0, 1.0, 1.0], 1.0) == 25
    assert tilted_slice_qty(100, [1.0, 1.0, 1.0, 1.0], 3.0) == 50
    assert tilted_slice_qty(100, [1.0, 1.0, 1.0, 1.0], 0.0) == 0
    assert tilted_slice_qty(100, [1.0], 0.0) == 100  # the last slice takes the rest
    assert apportion(10, [1.0, 1.0, 1.0]) == [4, 3, 3]
    with pytest.raises(ValueError):
        PacingParams(min_scale=-0.1)
    with pytest.raises(ValueError):
        PacingParams(max_participation=1.0)


# ------------------------------------------------------- default-off --


def _digest(res) -> str:
    rows = [f.to_dict() for f in res.fills] + [r.to_dict() for r in res.parents.values()]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def test_default_off_every_replay_is_byte_identical(golden_dir):
    """The golden stream under every algo x policy, with and without a signal
    source attached but no opt-in: identical fills and parent rows."""
    events = read_jsonl(golden_dir / "events_eq_mbo.jsonl")
    cfg = golden_config(golden_dir.parents[1] / "configs")
    base = golden_parents(events[0].exchange_ts)
    src = ArraySignalSource({1: [(events[0].exchange_ts, 5.0, 1e-3)]})
    parents = []
    pid = 0
    for p in base:
        for algo in AlgoType:
            for pol in ExecPolicy:
                pid += 1
                parents.append(replace(p, parent_id=pid, algo=algo, policy=pol))
    plain = ExecutionReplay(cfg, parents).run(events)
    attached = ExecutionReplay(cfg, [replace(p, signal_source=src) for p in parents]).run(events)
    assert _digest(plain) == _digest(attached)
    assert all(not p.signal_aware for p in parents)
    for kids in plain.children.values():
        assert all(o.urgency_regime == "" and o.pacing_scale == 1.0 for o in kids)
    for r in plain.parents.values():
        assert r.diagnostics()["urgency_children"] == {}
        assert "urgency_children" not in r.to_dict()


def test_opt_ins_are_validated():
    src = constant(0.0)
    with pytest.raises(ValueError, match="signal_source"):
        run([parent(urgency_policy="alpha")], [])
    with pytest.raises(ValueError, match="urgency_policy"):
        run([parent(signal_source=src, urgency_policy="magic")], [])
    with pytest.raises(ValueError, match="POV"):
        run([parent(signal_source=src, pov_signal_pacing=True)], [])
    with pytest.raises(ValueError, match="TWAP"):
        run([parent(algo=AlgoType.POV, signal_source=src, slice_signal_tilt=True)], [])
    with pytest.raises(ValueError, match="ALMGREN"):
        run([parent(algo=AlgoType.IS, signal_source=src, ac_signal_drift=True)], [])


# ------------------------------------------------------------- B2 ------


def _avg(res, pid=1):
    return res.parents[pid].avg_price


def test_planted_adverse_trend_makes_the_crossing_path_cheaper():
    """Buy into a rising market: the signal says 'adverse' (z > 0 for a buy);
    the alpha-urgency parent crosses at once and pays less than the plain
    NATIVE parent whose passive slices are left behind by the trend."""
    evs = trending(step_every_s=5, direction=+1)
    plain = parent(policy=ExecPolicy.NATIVE)
    aware = replace(
        plain, signal_source=constant(+2.0, 1e-4), urgency_policy="alpha", alpha_urgency=NEVER_STALE
    )
    a, b = run([plain], evs), run([aware], evs)
    kids = b.children[1]
    assert {o.urgency_regime for o in kids} == {"adverse"}
    assert all(o.type == OrderType.MARKET and o.signal_z == 2.0 for o in kids)
    assert b.parents[1].filled_qty == 400
    assert b.parents[1].diagnostics()["urgency_qty"] == {"adverse": 400}
    # the plain parent's bids rest below the moving touch: unfilled or dearer
    assert a.parents[1].filled_qty < 400 or _avg(a) > _avg(b)
    # and the aware parent paid less per share for what it filled
    if a.parents[1].filled_qty:
        assert _avg(b) < _avg(a)


def test_planted_favourable_trend_makes_the_passive_path_cheaper():
    """Buy into a falling market: the signal says 'agree' (z < 0 for a buy);
    the alpha-urgency parent posts and is filled by the marketable sells at
    ever lower prices, paying less than the AGGRESSIVE parent that crosses
    at arrival."""
    evs = trending(step_every_s=5, direction=-1, hit_every_s=1)
    plain = parent(policy=ExecPolicy.AGGRESSIVE)
    aware = replace(
        plain,
        signal_source=constant(-2.0, -1e-4),
        urgency_policy="alpha",
        alpha_urgency=replace(NEVER_STALE, passive_urgency=0.0),
    )
    a, b = run([plain], evs), run([aware], evs)
    first = [o for o in b.children[1] if o.urgency_regime == "agree"]
    assert len(first) >= 4 and all(o.type == OrderType.LIMIT for o in first[:4])
    assert b.parents[1].filled_qty == 400 and a.parents[1].filled_qty == 400
    assert _avg(b) < _avg(a)
    assert b.parents[1].rebates > 0.0 and a.parents[1].rebates == 0.0


def test_urgency_is_re_evaluated_at_every_slice_through_the_life():
    """The signal flips sign mid-window: the first slices cross, the later
    ones post, each child recording the z it was decided on."""
    evs = trending(step_every_s=0, direction=+1)
    src = ArraySignalSource({INS: [(T0 - SEC, 2.0, 1e-4), (T0 + 49 * SEC, -2.0, -1e-4)]})
    p = parent(signal_source=src, urgency_policy="alpha", alpha_urgency=NEVER_STALE)
    res = run([p], evs)
    kids = res.children[1]
    by_slice = [((o.decision_ts - T0) // SEC, o.urgency_regime, o.signal_z) for o in kids]
    regimes = [(o.urgency_regime, o.signal_z) for o in kids if o.pacing_scale == 1.0]
    assert ("adverse", 2.0) in regimes and ("agree", -2.0) in regimes
    first_two = [o for o in kids if o.decision_ts < T0 + 49 * SEC]
    assert all(o.urgency_regime == "adverse" and o.type == OrderType.MARKET for o in first_two)
    later = [o for o in kids if o.decision_ts >= T0 + 50 * SEC and o.urgency_regime == "agree"]
    assert later and later[0].type == OrderType.LIMIT
    assert sum(res.parents[1].diagnostics()["urgency_qty"].values()) == 400
    assert by_slice  # the record is per child


def test_neutral_no_signal_and_stale_fall_back_to_the_plain_style():
    evs = trending(step_every_s=0, direction=+1)
    plain = run([parent(policy=ExecPolicy.NATIVE)], evs)
    # inside the band: neutral
    neutral = run(
        [parent(signal_source=constant(0.5), urgency_policy="alpha", alpha_urgency=NEVER_STALE)],
        evs,
    )
    assert {o.urgency_regime for o in neutral.children[1]} == {"neutral"}
    assert _digest(neutral) == _digest(plain)
    # no reading for the instrument at all
    none = run([parent(signal_source=ArraySignalSource({}), urgency_policy="alpha")], evs)
    assert {o.urgency_regime for o in none.children[1]} == {"no_signal"}
    assert _digest(none) == _digest(plain)
    # a reading older than max_age
    stale = run(
        [
            parent(
                signal_source=constant(3.0),
                urgency_policy="alpha",
                alpha_urgency=AlphaUrgencyPolicy(max_age_ns=SEC // 2),
            )
        ],
        evs,
    )
    assert {o.urgency_regime for o in stale.children[1]} == {"stale"}
    assert _digest(stale) == _digest(plain)
    assert set(URGENCY_REGIMES) >= {"agree", "adverse", "neutral", "no_signal", "stale"}


def test_latency_reads_the_signal_as_of_t_minus_latency():
    evs = trending(step_every_s=0, direction=+1)
    # z flips at exactly T0 + 50 s; the slice at 50 s sees the old value with 1 s latency
    src = ArraySignalSource({INS: [(T0 - SEC, 2.0, 0.0), (T0 + 50 * SEC, -2.0, 0.0)]})
    pol = AlphaUrgencyPolicy(max_age_ns=0, latency_ns=SEC)
    res = run([parent(signal_source=src, urgency_policy="alpha", alpha_urgency=pol)], evs)
    at50 = [o for o in res.children[1] if o.decision_ts == T0 + 50 * SEC]
    assert at50 and at50[0].urgency_regime == "adverse"
    pol0 = AlphaUrgencyPolicy(max_age_ns=0)
    res0 = run([parent(signal_source=src, urgency_policy="alpha", alpha_urgency=pol0)], evs)
    at50 = [o for o in res0.children[1] if o.decision_ts == T0 + 50 * SEC]
    assert at50 and at50[0].urgency_regime == "agree"


def test_passive_base_keeps_its_own_patience_in_the_neutral_band():
    evs = trending(step_every_s=0, direction=+1)
    base = parent(policy=ExecPolicy.PASSIVE, urgency=0.5)
    aware = replace(
        base, signal_source=constant(0.0), urgency_policy="alpha", alpha_urgency=NEVER_STALE
    )
    assert _digest(run([base], evs)) == _digest(run([aware], evs))


# ------------------------------------------------------------- B3 ------


def test_pov_pacing_participates_less_when_the_signal_favours_waiting():
    evs = trending(step_every_s=0, direction=+1)
    pov = parent(algo=AlgoType.POV, qty=5000, participation=0.5, policy=ExecPolicy.AGGRESSIVE)
    plain = run([pov], evs)
    wait = run(
        [replace(pov, signal_source=constant(-2.0), pov_signal_pacing=True, pacing=PACE)], evs
    )
    rush = run(
        [
            replace(
                pov,
                signal_source=constant(+2.0),
                pov_signal_pacing=True,
                pacing=replace(PACE, max_participation=0.9),
            )
        ],
        evs,
    )
    f = lambda r: r.parents[1].filled_qty  # noqa: E731
    assert f(wait) < f(plain) < f(rush)
    assert {o.pacing_scale for o in wait.children[1]} == {0.25}
    assert all(o.urgency_regime == "plain" for o in wait.children[1])
    # scale 1 (no signal) is the plain algo
    same = run([replace(pov, signal_source=ArraySignalSource({}), pov_signal_pacing=True)], evs)
    assert _digest(same) == _digest(plain)


def test_slice_tilt_front_loads_on_adverse_and_defers_on_favourable():
    evs = trending(step_every_s=0, direction=+1)
    p = parent(policy=ExecPolicy.AGGRESSIVE, qty=400, slices=4)
    adverse = run(
        [replace(p, signal_source=constant(+2.0), slice_signal_tilt=True, pacing=PACE)], evs
    )
    agree = run(
        [replace(p, signal_source=constant(-2.0), slice_signal_tilt=True, pacing=PACE)], evs
    )
    q_adv = [o.qty for o in adverse.children[1]]
    q_agr = [o.qty for o in agree.children[1]]
    assert sum(q_adv) == sum(q_agr) == 400
    assert q_adv[0] > 100 > q_agr[0]  # first slice: 2/(2+3)*400 = 160 vs 0.25/3.25*400 = 31
    # re-tilted at every slice over the REMAINING qty: 160 of 400, 120 of 240, 80 of 160, 40;
    # 31 of 400, 41 of 369 (0.25/2.25), 66 of 328 (0.25/1.25), the rest
    assert q_adv == [160, 120, 80, 40] and q_agr == [31, 41, 66, 262]
    assert all(q >= 0 for q in q_adv + q_agr)
    assert {o.pacing_scale for o in adverse.children[1]} == {2.0}
    # VWAP and IS weights tilt the same way
    for algo in (AlgoType.VWAP, AlgoType.IS):
        r = run(
            [
                replace(
                    p, algo=algo, signal_source=constant(+2.0), slice_signal_tilt=True, pacing=PACE
                )
            ],
            evs,
        )
        plain_q = [o.qty for o in run([replace(p, algo=algo)], evs).children[1]]
        assert [o.qty for o in r.children[1]][0] > plain_q[0]


def test_ac_drift_sign_and_the_zero_drift_limit():
    p = ACParams(sigma=0.95, eta=2.5e-6, gamma=0.0, eps=0.0, horizon=5.0, risk_aversion=1e-6)
    base = ac_holdings(1e6, p, 5)
    assert ac_holdings(1e6, p, 5, drift=0.0) == base  # byte-identical default
    up = ac_holdings(1e6, p, 5, drift=+0.01)  # waiting pays: hold more
    down = ac_holdings(1e6, p, 5, drift=-0.01)  # price running away: front-load
    assert all(u > b > d for u, b, d in zip(up[1:-1], base[1:-1], down[1:-1], strict=True))
    assert up[0] == down[0] == 1e6 and up[-1] == down[-1] == 0.0
    assert ac_trajectory(1e6, p, 5, drift=-0.01)[0] > ac_trajectory(1e6, p, 5)[0]
    # risk-neutral limit: the quadratic correction, symmetric in t
    rn = replace(p, risk_aversion=0.0)
    h = ac_holdings(1000.0, rn, 4, drift=1e-3)
    lin = ac_holdings(1000.0, rn, 4)
    corr = [a - b for a, b in zip(h, lin, strict=True)]
    assert corr[1] == pytest.approx(corr[3]) and corr[2] > corr[1] > 0
    # the signal map: a buy is hurt by a rise
    assert ac_drift_from_signal(0, 1e-4, 100.0, 0.5) == pytest.approx(-0.02)
    assert ac_drift_from_signal(1, 1e-4, 100.0, 0.5) == pytest.approx(+0.02)
    with pytest.raises(ValueError):
        ac_drift_from_signal(0, 1e-4, 100.0, 0.0)


def test_ac_drift_in_the_replay_front_loads_an_adverse_buy():
    evs = trending(step_every_s=0, direction=+1)
    acp = ACParams(sigma=0.5, eta=1e-7, horizon=1.0, risk_aversion=1e-6)
    p = parent(
        algo=AlgoType.IS,
        qty=1000,
        slices=5,
        is_model=ISModel.ALMGREN_CHRISS,
        ac_params=acp,
        policy=ExecPolicy.AGGRESSIVE,
    )
    plain = run([p], evs)
    adverse = run(
        [replace(p, signal_source=constant(+2.0, +1e-3), ac_signal_drift=True, pacing=PACE)], evs
    )
    agree = run(
        [replace(p, signal_source=constant(-2.0, -1e-3), ac_signal_drift=True, pacing=PACE)], evs
    )
    q0 = lambda r: r.children[1][0].qty  # noqa: E731
    assert q0(adverse) > q0(plain) > q0(agree)
    for r in (plain, adverse, agree):
        assert sum(o.qty for o in r.children[1]) == 1000
    # no reading: the re-solved zero-drift trajectory is the static one
    same = run([replace(p, signal_source=ArraySignalSource({}), ac_signal_drift=True)], evs)
    assert [o.qty for o in same.children[1]] == [o.qty for o in plain.children[1]]


# ---------------------------------------------------------- driver -----


@pytest.fixture(scope="module")
def dataset(tmp_path_factory, golden_dir) -> Path:
    import shutil

    root = tmp_path_factory.mktemp("bench_sig")
    norm = root / "normalized"
    norm.mkdir()
    for day in ("20190101", "20190102"):
        shutil.copy(golden_dir / "events_eq_mbo.jsonl", norm / f"eq_{day}.normalized.jsonl")
    return root


def test_driver_pairs_signal_aware_policies_with_their_plain_control(dataset, tmp_path):
    out = tmp_path / "out"
    rc = bench.main(
        [
            "--data-dir",
            str(dataset),
            "--config-dir",
            str(REPO / "configs"),
            "--out",
            str(out),
            "--sizes-pct",
            "0.001",
            "--algos",
            "TWAP,POV",
            "--window-min",
            "1.5",
            "--warmup-min",
            "0.5",
            "--no-timestamp",
            "--policies",
            "native,native+urgency,native+pacing",
            "--signal",
            "synthetic",
        ]
    )
    assert rc == 0
    doc = json.loads((out / "benchmark_bench_sig0.json").read_text())
    assert doc["signal"]["source"].startswith("synthetic")
    keys = set(doc["paired_vs_baseline"])
    assert "native+urgency-native|TWAP|0.001" in keys
    assert "native+pacing-native|POV|0.001" in keys
    cell = doc["cells"]["native+urgency|TWAP|0.001"]
    assert sum(cell["urgency_share_of_children"].values()) == pytest.approx(1.0)
    assert set(cell["urgency_share_of_children"]) <= set(URGENCY_REGIMES) | {"plain"}
    assert doc["cells"]["native|TWAP|0.001"]["urgency_share_of_children"] == {}
    md = (out / "BENCHMARK_bench_sig0.md").read_text()
    assert "signal-aware" in md.lower() and "urgency shares" in md.lower()
    # the plain cells are byte-identical to a run without any signal policy
    out2 = tmp_path / "out2"
    bench.main(
        [
            "--data-dir",
            str(dataset),
            "--config-dir",
            str(REPO / "configs"),
            "--out",
            str(out2),
            "--sizes-pct",
            "0.001",
            "--algos",
            "TWAP,POV",
            "--window-min",
            "1.5",
            "--warmup-min",
            "0.5",
            "--no-timestamp",
            "--policies",
            "native",
        ]
    )
    doc2 = json.loads((out2 / "benchmark_bench_sig0.json").read_text())
    for k in doc2["cells"]:
        a, b = doc["cells"][k], doc2["cells"][k]
        assert a["costs_bps"] == b["costs_bps"] and a["fill_rate"] == b["fill_rate"]
