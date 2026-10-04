"""Planted effects of known size in the synthetic generator (opt-in, default
off and byte-neutral) and the power study built on them."""

from __future__ import annotations

import copy
import json
import math

import numpy as np
import pytest
from conftest import CONFIGS_DIR, REPO_ROOT
from iap.core.codec import encode_iap1, read_jsonl, sha256_bytes
from iap.core.events import EventType, Side
from iap.marketdata.generator import (
    _DEFAULT_CONFIG,
    MarketDataGenerator,
    load_generator_config,
)
from iap.research import ResearchError, power, power_stats
from iap.research.__main__ import main as cli_main
from iap.validation.methods import methods

PLANTED_CONFIG = REPO_ROOT / "research" / "power" / "generator_planted.json"

SMALL = {
    "seed": 4711,
    "sessions": 1,
    "equities": {"slots_per_stream": 120},
    "fx": {"slots_per_pair": 40},
}


def _planted(order_flow=0.0, beta=0.0, brk=None, **extra) -> dict:
    cfg = copy.deepcopy(SMALL)
    cfg["planted"] = {
        "order_flow": {"strength": order_flow, "kernel_decay": 0.9, "kernel_steps": 20},
        "lead_lag": {"beta": beta, "leader": "SYN.ETF.IDX", "lag_steps": 2},
    }
    if brk is not None:
        cfg["planted"]["break"] = brk
    cfg.update(extra)
    return cfg


def _run(refdata, tmp_path, cfg, sub):
    gen = MarketDataGenerator(refdata, cfg)
    out = tmp_path / sub
    stats = gen.generate_run(out)
    events = []
    for name in sorted(stats["files"]):
        events.extend(read_jsonl(out / name))
    return gen, events


def _digest(events) -> str:
    return sha256_bytes(encode_iap1(events))


# ---------------------------------------------------------------------------
# default off, and off is byte-neutral
# ---------------------------------------------------------------------------


def test_planted_block_defaults_to_off():
    planted = _DEFAULT_CONFIG["planted"]
    assert planted["order_flow"]["strength"] == 0.0
    assert planted["lead_lag"]["beta"] == 0.0
    assert planted["break"] == {"at_fraction": None, "post_multiplier": 1.0}
    pinned = json.loads((CONFIGS_DIR / "marketdata" / "generator.json").read_text())
    assert "planted" not in pinned  # the pinned dataset has none
    assert (
        load_generator_config(CONFIGS_DIR / "marketdata" / "generator.json")["planted"] == planted
    )


def test_planted_off_is_byte_identical_to_no_planted_block(refdata, tmp_path):
    """Absent block, explicit zeros, and a break with nothing to break all
    produce the same bytes: no draw is added or reordered when off."""
    _, base = _run(refdata, tmp_path, SMALL, "base")
    _, zeros = _run(refdata, tmp_path, _planted(0.0, 0.0), "zeros")
    _, idle_break = _run(
        refdata,
        tmp_path,
        _planted(0.0, 0.0, {"at_fraction": 0.5, "post_multiplier": -1.0}),
        "break",
    )
    assert _digest(base) == _digest(zeros) == _digest(idle_break)


def test_planted_runs_are_deterministic_and_differ_from_the_base(refdata, tmp_path):
    cfg = _planted(0.5, 0.5)
    _, a = _run(refdata, tmp_path, cfg, "a")
    _, b = _run(refdata, tmp_path, cfg, "b")
    _, base = _run(refdata, tmp_path, SMALL, "base")
    assert _digest(a) == _digest(b)
    assert _digest(a) != _digest(base)


def test_order_flow_plant_leaves_every_efficient_price_untouched(refdata, tmp_path):
    """Informed flow changes WHO trades, never the price process or the
    number of draws the price stream consumed."""
    base, _ = _run(refdata, tmp_path, SMALL, "base")
    flow, _ = _run(refdata, tmp_path, _planted(order_flow=0.6), "flow")
    assert sorted(base._eq_prices) == sorted(flow._eq_prices)
    for iid, price in base._eq_prices.items():
        assert price.path == flow._eq_prices[iid].path
        assert price.rng.next_u64() == flow._eq_prices[iid].rng.next_u64()


def test_lead_lag_plant_leaves_the_leader_untouched(refdata, tmp_path):
    base, _ = _run(refdata, tmp_path, SMALL, "base")
    lead, _ = _run(refdata, tmp_path, _planted(beta=0.8), "lead")
    leader = refdata.instrument("SYN.ETF.IDX").instrument_id
    assert base._eq_prices[leader].path == lead._eq_prices[leader].path
    follower = refdata.instrument("SYN.EQ.001").instrument_id
    assert base._eq_prices[follower].path != lead._eq_prices[follower].path
    # the follower consumed exactly as many draws as without the plant
    assert base._eq_prices[follower].rng.next_u64() == lead._eq_prices[follower].rng.next_u64()


# ---------------------------------------------------------------------------
# the effects have the size and shape the config states
# ---------------------------------------------------------------------------


#: Fraction of the (single) session the flow tests split at.  Since v1.4.0
#: the equity flow spans the whole session, so the middle of the clock is
#: also the median trade (up to v1.3.0 the slots were spent in the first
#: ~40 % of a session and the split sat at 0.2).
FLOW_SPLIT = 0.5


def _flow_alignment(gen, events, refdata, horizon_steps=10):
    """mean(sign(aggressor) * sign(efficient move over the next steps)) over
    the continuous-session trades of a single-session run, before and after
    ``FLOW_SPLIT`` of the session; plus the two trade counts."""
    first, second = [], []
    for ev in events:
        if ev.event_type != EventType.TRADE or ev.instrument_id not in gen._eq_prices:
            continue
        price = gen._eq_prices[ev.instrument_id]
        k = int((ev.exchange_ts - price.open_ns) // price.STEP_NS)
        if k + horizon_steps >= len(price.path) or k < 60:
            continue  # auction prints / the close
        move = price.path[k + horizon_steps] - price.path[k]
        if move == 0.0:
            continue
        sign = 1.0 if ev.side == Side.BID else -1.0
        (first if k < FLOW_SPLIT * len(price.path) else second).append(sign * np.sign(move))
    return float(np.mean(first)), float(np.mean(second)), min(len(first), len(second))


def test_order_flow_sign_leads_the_efficient_price(refdata, tmp_path):
    cfg = _planted(order_flow=0.6, sessions=1, equities={"slots_per_stream": 1500})
    off = copy.deepcopy(cfg)
    off["planted"]["order_flow"]["strength"] = 0.0
    gen, events = _run(refdata, tmp_path, cfg, "on")
    gen0, events0 = _run(refdata, tmp_path, off, "off")
    a, b, n = _flow_alignment(gen, events, refdata)
    a0, b0, n0 = _flow_alignment(gen0, events0, refdata)
    assert n > 1000 and n0 > 1000  # each side of the split
    assert a > 0.2 and b > 0.2  # planted: flow leads the price
    assert abs(a0) < 0.08 and abs(b0) < 0.08  # off: unrelated


def test_break_reverses_the_planted_flow_mid_sample(refdata, tmp_path):
    cfg = _planted(
        order_flow=0.6,
        sessions=1,
        brk={"at_fraction": FLOW_SPLIT, "post_multiplier": -1.0},
        equities={"slots_per_stream": 1500},
    )
    gen, events = _run(refdata, tmp_path, cfg, "brk")
    first, second, n = _flow_alignment(gen, events, refdata)
    assert n > 1000
    assert first > 0.2 and second < -0.2


def _lagged_corr(gen, refdata, lag, lo=0.0, hi=1.0):
    leader = gen._eq_prices[refdata.instrument("SYN.ETF.IDX").instrument_id].step_moves()
    out = []
    for symbol in ("SYN.EQ.001", "SYN.EQ.005", "SYN.EQ.010"):
        moves = gen._eq_prices[refdata.instrument(symbol).instrument_id].step_moves()
        n = len(moves)
        a, b = int(lo * n), int(hi * n)
        x = np.asarray(leader[a : b - lag])
        y = np.asarray(moves[a + lag : b])
        out.append(float(np.corrcoef(x, y)[0, 1]))
    return float(np.mean(out))


def test_lead_lag_plants_the_stated_lagged_correlation(refdata, tmp_path):
    """Lagged correlation of efficient moves is ~ beta / sqrt(1 + beta^2) at
    the configured lag and ~0 at any other lag or with the plant off."""
    beta = 0.75
    cfg = _planted(
        beta=beta,
        sessions=1,
        equities={"vol_regimes": {"sigma_ticks_per_s": [0.2, 0.2], "switch_prob_per_s": 0.0}},
    )
    gen, _ = _run(refdata, tmp_path, cfg, "on")
    want = beta / np.sqrt(1.0 + beta * beta)  # 0.6
    assert _lagged_corr(gen, refdata, lag=2) == pytest.approx(want, abs=0.02)
    assert abs(_lagged_corr(gen, refdata, lag=1)) < 0.03
    assert abs(_lagged_corr(gen, refdata, lag=3)) < 0.03
    off = copy.deepcopy(cfg)
    off["planted"]["lead_lag"]["beta"] = 0.0
    gen0, _ = _run(refdata, tmp_path, off, "off")
    assert abs(_lagged_corr(gen0, refdata, lag=2)) < 0.03


def test_break_reverses_the_lead_lag_mid_sample(refdata, tmp_path):
    cfg = _planted(
        beta=0.75,
        sessions=1,
        brk={"at_fraction": 0.5, "post_multiplier": -1.0},
        equities={"vol_regimes": {"sigma_ticks_per_s": [0.2, 0.2], "switch_prob_per_s": 0.0}},
    )
    gen, _ = _run(refdata, tmp_path, cfg, "brk")
    assert _lagged_corr(gen, refdata, 2, 0.0, 0.5) == pytest.approx(0.6, abs=0.03)
    assert _lagged_corr(gen, refdata, 2, 0.5, 1.0) == pytest.approx(-0.6, abs=0.03)


def test_break_fraction_spans_sessions(refdata):
    """``at_fraction`` is a fraction of the whole RUN: with two sessions the
    study's 0.5 reverses exactly the second one."""
    gen = MarketDataGenerator(
        refdata, _planted(0.5, 0.0, {"at_fraction": 0.5, "post_multiplier": 0.0})
    )
    assert gen._planted_multipliers(0, 2, 10) == [1.0] * 10  # first session
    assert gen._planted_multipliers(1, 2, 10) == [0.0] * 10  # second session
    quarter = MarketDataGenerator(
        refdata, _planted(0.5, 0.0, {"at_fraction": 0.25, "post_multiplier": 0.0})
    )
    assert quarter._planted_multipliers(0, 2, 10) == [1.0] * 5 + [0.0] * 5
    stable = MarketDataGenerator(refdata, _planted(0.5, 0.0))
    assert stable._planted_multipliers(1, 2, 4) == [1.0] * 4


@pytest.mark.parametrize(
    "planted, message",
    [
        ({"order_flow": {"strength": 1.0}}, "strength must be in"),
        ({"order_flow": {"strength": -0.1}}, "strength must be in"),
        ({"order_flow": {"kernel_decay": 0.0}}, "kernel_decay"),
        ({"order_flow": {"kernel_steps": 0}}, "kernel_steps"),
        ({"lead_lag": {"lag_steps": 0, "beta": 0.5}}, "lag_steps must be >= 1"),
        ({"lead_lag": {"beta": 0.5, "leader": "SYN.NOPE"}}, "not an equity instrument"),
        ({"lead_lag": {"beta": float("inf")}}, "beta must be finite"),
        ({"break": {"at_fraction": 1.0, "post_multiplier": 1.0}}, "at_fraction"),
        (
            {
                "order_flow": {"strength": 0.6},
                "break": {"at_fraction": 0.5, "post_multiplier": 2.0},
            },
            "post_multiplier",
        ),
    ],
)
def test_planted_config_is_validated(refdata, planted, message):
    with pytest.raises(ValueError, match=message):
        MarketDataGenerator(refdata, {"planted": planted})


def test_reference_power_config_is_planted_and_outside_configs():
    cfg = load_generator_config(PLANTED_CONFIG)
    assert cfg["planted"]["order_flow"]["strength"] > 0.0
    assert cfg["planted"]["lead_lag"]["beta"] > 0.0
    assert cfg["planted"]["break"]["at_fraction"] is None
    # configs/ is the deployable tree (every JSON in it is shipped to the
    # pods): the planted config must not be part of it
    assert not list((CONFIGS_DIR / "marketdata").glob("*planted*"))


# ---------------------------------------------------------------------------
# power study
# ---------------------------------------------------------------------------


def test_study_seeds_are_deterministic_distinct_and_json_exact():
    seeds = power.study_seeds(20261003, 4)
    assert seeds == power.study_seeds(20261003, 4)
    assert seeds[:2] == power.study_seeds(20261003, 2)
    assert len(set(seeds)) == 4 and all(0 <= s < 2**31 for s in seeds)
    assert power.study_seeds(1, 3) != power.study_seeds(2, 3)
    with pytest.raises(ResearchError):
        power.study_seeds(1, 0)


def test_cell_config_scales_both_reference_effects():
    base = load_generator_config(PLANTED_CONFIG)
    ref = copy.deepcopy(base["planted"])
    cell = power.cell_config(base, 2.0, "break", 99)
    assert cell["seed"] == 99
    assert cell["planted"]["order_flow"]["strength"] == 2.0 * ref["order_flow"]["strength"]
    assert cell["planted"]["lead_lag"]["beta"] == 2.0 * ref["lead_lag"]["beta"]
    assert cell["planted"]["break"] == {"at_fraction": 0.5, "post_multiplier": -1.0}
    null = power.cell_config(base, 0.0, "stable", 1)
    assert null["planted"]["order_flow"]["strength"] == 0.0
    assert null["planted"]["lead_lag"]["beta"] == 0.0
    assert null["planted"]["break"]["at_fraction"] is None
    assert base["planted"] == ref  # the base is not mutated
    with pytest.raises(ResearchError, match="unknown scenario"):
        power.cell_config(base, 1.0, "wobble", 1)
    with pytest.raises(ResearchError, match="level"):
        power.cell_config(base, -1.0, "stable", 1)


DETECTOR_IDS = (
    "lead_lag:EQ10@1s",
    "lead_lag:EQ10@5s",
    "order_flow:EQ04@5s",
    "order_flow:EQ04@10s",
)
THRESHOLDS = {"fixed": 3.0, "gate": 4.365155, "study": 3.9}


def _fake_row(det, t, break_z=0.0, sessions=1):
    """A detector row whose pooled-slope t is ``t`` (within-bucket t one
    half less, direct t one tenth more)."""
    _, _, rest = det.partition(":")
    alpha_id, horizon = rest.split("@")
    return {
        "alpha_id": alpha_id,
        "horizon": horizon,
        "gate_ic": 0.01 * t,
        "ic_vol_scaled": 0.02 * t,
        "t_pooled": t,
        "t_within": t - 0.5,
        "fold_sign_consistency": 1.0 if t > 0 else 0.5,
        "hypothesis_confirmed": t > 0,
        "leakage_passed": True,
        "recompute_ok": True,
        "trade_count_1x": 12 if t >= 3.0 else 0,
        "n_folds_survive_1x_cost": 4 if t >= 3.0 else 0,
        "pnl_ci_positive": t >= 3.5,
        "verdict": "ITERATE" if t >= 1.5 else "REJECT",
        "ic_direct": 0.01 * t,
        "t_direct": t + 0.1,
        "n_pairs": 1000 * sessions,
        "break_z": break_z,
        "signal_coverage": 0.5,
        "label_scored_frac": 1.0,
        "label_zero_frac": 0.8,
    }


@pytest.fixture()
def fake_pipeline(monkeypatch):
    """Replace the (slow) data pipeline: the detector t-stat is a function of
    the planted config and the session count, so the grid plumbing is tested
    exactly.  t = 5 * strength * sqrt(sessions) for the order-flow detectors
    and half of it for the lead-lag ones; a break run has t = 0 and a break
    z of 2.5 * strength * sqrt(sessions)."""
    built = []

    def fake_build(cfg, configs_dir, work_dir, universe=power.DEFAULT_UNIVERSE):
        built.append(
            (
                cfg["seed"],
                cfg["sessions"],
                cfg["planted"]["order_flow"]["strength"],
                cfg["planted"]["lead_lag"]["beta"],
                cfg["planted"]["break"]["post_multiplier"],
            )
        )
        return {1: dict(cfg, work_dir=work_dir)}

    def fake_first(frames, n):
        return {1: dict(frames[1], n=n)}

    def fake_evaluate(
        frames,
        configs_dir,
        promote_t_threshold,
        detectors,
        seed=0,
        normalized_dir=None,
        engine_configs_dir=None,
    ):
        cfg = frames[1]
        assert seed == cfg["seed"]
        assert [d["id"] for d in detectors] == list(DETECTOR_IDS)
        # the recompute probe is pointed at the run's OWN events and at the
        # reduced configs tree its features were built with — on the full
        # sample of the first seed of a cell, and nowhere else
        if normalized_dir is not None:
            assert cfg["n"] == cfg["sessions"]
            assert normalized_dir == cfg["work_dir"] / "normalized"
            assert engine_configs_dir == cfg["work_dir"] / "configs"
        stable = cfg["planted"]["break"]["post_multiplier"] > 0
        size = cfg["planted"]["order_flow"]["strength"] * math.sqrt(cfg["n"])
        rows = {}
        for det in detectors:
            scale = 5.0 if det["effect"] == "order_flow" else 2.5
            rows[det["id"]] = _fake_row(
                det["id"],
                scale * size if stable else 0.0,
                break_z=0.0 if stable else 2.5 * size,
                sessions=cfg["n"],
            )
            rows[det["id"]]["recompute_ok"] = True if normalized_dir is not None else None
        return rows

    monkeypatch.setattr(power, "build_planted_frames", fake_build)
    monkeypatch.setattr(power, "first_sessions", fake_first)
    monkeypatch.setattr(power, "evaluate_run", fake_evaluate)
    monkeypatch.setattr(power, "_frame_rows", lambda frames: {"1": 0})
    return built


def test_power_study_grid_layout_and_rates(fake_pipeline, tmp_path):
    lines = []
    doc = power.run_power_study(
        PLANTED_CONFIG,
        CONFIGS_DIR,
        levels=(0.0, 1.0, 2.0),
        n_seeds=2,
        sessions=[1, 4],
        break_levels=(1.0, 2.0),
        gate_looks=3936,
        scratch_dir=tmp_path / "scratch",
        progress=lines.append,
    )
    seeds = doc["seeds"]
    # stable at 3 levels + break at 2 levels, 2 seeds each, generated ONCE at
    # the largest session count
    assert len(doc["runs"]) == (3 + 2) * 2 == len(fake_pipeline) == len(lines)
    assert {b[1] for b in fake_pipeline} == {4}
    assert [(r["scenario"], r["level"]) for r in doc["runs"][::2]] == [
        ("stable", 0.0),
        ("stable", 1.0),
        ("stable", 2.0),
        ("break", 1.0),
        ("break", 2.0),
    ]
    assert {r["seed"] for r in doc["runs"]} == set(seeds)
    # stable runs are evaluated on every session prefix, break runs on the full run
    assert [e["sessions"] for e in doc["runs"][0]["evaluations"]] == [1, 4]
    assert [e["sessions"] for e in doc["runs"][-1]["evaluations"]] == [4]
    # the recompute probe: first seed of a cell, full session count
    first, second = doc["runs"][2], doc["runs"][3]
    det = "order_flow:EQ04@5s"
    assert first["evaluations"][1]["detectors"][det]["recompute_ok"] is True
    assert first["evaluations"][0]["detectors"][det]["recompute_ok"] is None
    assert second["evaluations"][1]["detectors"][det]["recompute_ok"] is None

    cells = {(c["detector"], c["scenario"], c["level"], c["sessions"]): c for c in doc["cells"]}
    assert len(cells) == 4 * (3 * 2 + 2)
    null = cells[(det, "stable", 0.0, 4)]
    assert null["n_runs"] == 2 and null["sig_fixed"]["k"] == 0 == null["evidence"]["k"]
    assert (null["sig_fixed"]["lo"], round(null["sig_fixed"]["hi"], 3)) == (0.0, 0.658)
    one = cells[(det, "stable", 1.0, 1)]  # t = 5 * 0.4 = 2
    assert one["mean_t_pooled"] == 2.0 and one["sig_fixed"]["k"] == 0
    assert one["evidence"]["rate"] == 1.0 and one["promote"]["k"] == 0
    four = cells[(det, "stable", 1.0, 4)]  # t = 4: clears 3.0 and the study's, not the gate's
    assert four["mean_t_pooled"] == 4.0 and four["sd_t_pooled"] == 0.0
    assert four["sig_fixed"]["k"] == 2 and four["sig_gate"]["k"] == 0
    assert four["sig_study"]["k"] == 2
    assert four["sig_fixed"]["lo"] == pytest.approx(0.342, abs=1e-3)
    assert four["sig_fixed"]["hi"] == 1.0
    strong = cells[(det, "stable", 2.0, 4)]  # t = 8
    assert strong["sig_gate"]["rate"] == 1.0 and strong["pnl_ci_positive"]["k"] == 2
    assert strong["mean_trades_1x"] == 12.0 and strong["mean_folds_survive_1x_cost"] == 4.0
    assert strong["mean_ic_vol_scaled"] == 0.16 and strong["mean_t_within"] == 7.5
    assert strong["alpha_id"] == "EQ04" and strong["horizon"] == "5s"
    assert strong["effect"] == "order_flow" and strong["n_recompute_failed"] == 0
    assert strong["mean_n_pairs"] == 4000.0 and strong["mean_signal_coverage"] == 0.5
    weak = cells[("lead_lag:EQ10@1s", "stable", 2.0, 4)]  # t = 2.5 * 0.8 * 2 = 4
    assert weak["sig_fixed"]["k"] == 2 and weak["sig_gate"]["k"] == 0
    # the direct t (4.1) is counted against the gate threshold too
    assert weak["sig_direct_gate"]["k"] == 0 and strong["sig_direct_gate"]["k"] == 2
    brk = cells[(det, "break", 2.0, 4)]  # break z = 2.5 * 0.8 * 2 = 4
    assert brk["evidence"]["k"] == 0 and brk["mean_break_z"] == 4.0
    assert brk["break_fixed"]["k"] == 2 and brk["break_gate"]["k"] == 0
    assert cells[(det, "break", 1.0, 4)]["break_fixed"]["k"] == 0  # z = 2
    assert strong["break_fixed"]["k"] == 0  # no false alarm on a stable run

    # the study charges itself for its own looks: every chain t and every
    # break z — 4 detectors x 2 seeds x (3 stable levels x 2 prefixes + 2 break)
    from iap.validation.ledger import ExperimentLedger

    protocol = doc["protocol"]
    assert protocol["n_tests"] == 2 * 4 * 2 * (3 * 2 + 2) == 128
    thr = protocol["thresholds"]
    assert thr["fixed"] == 3.0
    assert thr["study"] == pytest.approx(ExperimentLedger.bonferroni_t_threshold_at(128), abs=1e-6)
    assert thr["gate"] == pytest.approx(ExperimentLedger.bonferroni_t_threshold_at(3936), abs=1e-6)
    assert 3.0 < thr["study"] < thr["gate"] and protocol["gate_looks"] == 3936
    # verdicts are judged at the strictest of the three: never looser than the platform
    assert protocol["promote_t_threshold"] == thr["gate"]
    assert doc["cells"] == power.summarise(doc["runs"], thr)
    assert doc["x-version"] == power.POWER_VERSION == 3
    assert doc["sessions"] == [1, 4] and doc["break_levels"] == [1.0, 2.0]
    assert [d["id"] for d in doc["detectors"]] == list(DETECTOR_IDS)
    # the protocol names the method bundle; the bundle pins the stress grid
    assert protocol["methods"] == "v2"
    assert methods(protocol["methods"]).stress_version == 2
    assert not (tmp_path / "scratch").exists() or not list((tmp_path / "scratch").iterdir())


def test_power_study_defaults_follow_the_planted_config(fake_pipeline):
    doc = power.run_power_study(PLANTED_CONFIG, CONFIGS_DIR, levels=(1.0,), n_seeds=1)
    sessions = load_generator_config(PLANTED_CONFIG)["sessions"]
    assert doc["sessions"] == power.default_session_grid(sessions)
    assert {b[1] for b in fake_pipeline} == {sessions}
    assert doc["break_levels"] == list(power.DEFAULT_BREAK_LEVELS)
    # the gate threshold is read from the committed promotion reports
    reports = REPO_ROOT / "research" / "alpha_reports"
    assert doc["protocol"]["gate_looks"] == power.gate_looks_in_force(reports)
    assert power.DEFAULT_SEEDS >= 20


def test_power_model_and_diagnosis_from_the_fake_grid(fake_pipeline):
    doc = power.run_power_study(
        PLANTED_CONFIG,
        CONFIGS_DIR,
        levels=(0.0, 0.5, 1.0, 2.0),
        n_seeds=2,
        sessions=[1, 4],
        break_levels=(1.0, 2.0),
        gate_looks=3936,
    )
    models = {m["detector"]: m for m in doc["power_model"]}
    flow = models["order_flow:EQ04@5s"]
    # t = 5 * 0.4 * level * sqrt(sessions) exactly: kappa 2, no null offset, no residual
    assert flow["chain"]["kappa"] == pytest.approx(2.0) and flow["chain"]["sd"] == 0.0
    assert flow["chain"]["kappa0"] == pytest.approx(0.0, abs=1e-6)
    assert flow["chain"]["n"] == 4 * 2 * 2  # the null rows are fitted too
    assert flow["chain"]["by_threshold"]["gate"]["sessions_needed_at_reference"] is None  # sd 0
    assert flow["null"] == {"mean_t": 0.0, "sd_t": 0.0, "n": 4}
    assert flow["break"]["kappa"] == pytest.approx(1.0) and flow["break"]["n"] == 4
    assert flow["break"]["kappa0"] == 0.0  # the break model has no intercept
    assert models["lead_lag:EQ10@5s"]["chain"]["kappa"] == pytest.approx(1.0)

    diag = {(d["detector"], d["sessions"]): d for d in doc["diagnosis"]}
    assert sorted(diag) == sorted((d, n) for d in DETECTOR_IDS for n in (1, 4))
    row = diag[("order_flow:EQ04@5s", 4)]
    ref = doc["reference_planted"]["order_flow"]
    ideal = power_stats.ideal_ic_order_flow(
        ref["strength"], ref["kernel_decay"], ref["kernel_steps"], 10, 5, (1.0, 0.4 / 0.15)
    )
    assert row["ideal_ic"] == pytest.approx(ideal, abs=1e-6)
    assert row["ideal_r2"] == pytest.approx(ideal**2, abs=1e-6)
    assert row["measured_ic"] == 0.04
    assert row["attenuation"] == pytest.approx(0.04 / ideal, abs=1e-5)
    assert row["n_pairs"] == 4000.0
    # the chain of expected t: all scored rows -> rows with a signal ->
    # observed IC -> measured direct t -> walk-forward OOS share -> chain
    assert row["t_frictionless"] == pytest.approx(ideal * math.sqrt(8000.0), abs=1e-4)
    assert row["t_signal_rows"] == pytest.approx(ideal * math.sqrt(4000.0), abs=1e-4)
    assert row["t_observed_iid"] == pytest.approx(0.04 * math.sqrt(4000.0), abs=1e-5)
    assert row["t_direct"] == 4.1 and row["t_chain"] == 4.0
    assert row["t_oos_expected"] == pytest.approx(4.1 * math.sqrt(0.8), abs=1e-5)
    assert row["n_eff"] == pytest.approx((4.1 / 0.04) ** 2, rel=1e-5)
    assert row["design_effect"] == pytest.approx(4000.0 / (4.1 / 0.04) ** 2, rel=1e-4)
    # the declared lead-lag detector: a 1 s label cannot see a 2 s lag
    blind = diag[("lead_lag:EQ10@1s", 4)]
    assert blind["ideal_ic"] == 0.0 and blind["attenuation"] is None
    assert blind["t_frictionless"] == 0.0
    assert diag[("lead_lag:EQ10@5s", 4)]["ideal_ic"] == pytest.approx(
        power_stats.ideal_ic_lead_lag(0.4, 2, 5), abs=1e-6
    )


def test_power_study_document_is_deterministic_and_renders(fake_pipeline, tmp_path):
    kwargs = dict(levels=(0.0, 1.0), n_seeds=2, sessions=[1, 4], gate_looks=3936)
    a = power.run_power_study(PLANTED_CONFIG, CONFIGS_DIR, **kwargs)
    b = power.run_power_study(PLANTED_CONFIG, CONFIGS_DIR, **kwargs)
    assert a == b
    paths = power.write_reports(a, tmp_path / "out")
    raw = paths["json"].read_bytes()
    assert b"\r" not in raw and b"NaN" not in raw
    assert json.loads(raw) == a
    md = paths["md"].read_text(encoding="utf-8")
    assert md == power.render_markdown(a)
    assert md.startswith("# Planted-signal power study\n")
    for heading in (
        "## Detection of the reference effect, by number of sessions",
        "## False positives on the null",
        "## Detection by effect size (4 sessions)",
        "## The mid-sample break",
        "## Where the power goes",
        "## Minimum detectable effect and sessions needed",
        "## What the platform can and cannot detect",
    ):
        assert heading in md
    # 4 sessions: t = 4 clears 3.0 in both runs and the gate threshold in neither
    assert (
        "| order_flow:EQ04@5s | 4 | 0/2 [0.00, 0.66] | 2/2 [0.34, 1.00] | 2/2 [0.34, 1.00] "
        "| +4.00 | 0.00 |" in md
    )
    assert "| order_flow:EQ04@5s | break | 1 | 0/2 [0.00, 0.66] |" in md
    assert "`gate` 4.365, the PROMOTE threshold in force" in md and "3,936 looks" in md
    assert "**not established**" in md
    power.write_reports(a, tmp_path / "out")  # idempotent rewrite
    assert paths["json"].read_bytes() == raw


def test_power_study_counts_an_unvalidatable_run_as_a_miss():
    det = "order_flow:EQ04@5s"

    def run(seed, row):
        return {
            "scenario": "stable",
            "level": 1.0,
            "seed": seed,
            "evaluations": [{"sessions": 2, "detectors": {det: row}}],
        }

    runs = [
        run(1, _fake_row(det, 5.0)),
        run(2, {"alpha_id": "EQ04", "horizon": "5s", "error": "too few rows"}),
    ]
    (cell,) = power.summarise(runs, THRESHOLDS)
    assert cell["n_runs"] == 2 and cell["n_failed"] == 1
    assert cell["sig_gate"]["rate"] == 0.5 and cell["mean_t_pooled"] == 5.0
    assert cell["evidence"]["k"] == 1 and cell["pnl_ci_positive"]["rate"] == 0.5
    assert cell["sd_t_pooled"] is None  # one usable run has no spread


def test_detection_reads_the_pooled_slope_t_and_a_positive_gate_ic():
    """A detection is the POOLED-slope t at or above the threshold with a
    positive gate IC; the within-bucket t is reported, never counted."""
    det = "order_flow:EQ04@5s"

    def cell_of(row):
        runs = [
            {
                "scenario": "stable",
                "level": 1.0,
                "seed": 1,
                "evaluations": [{"sessions": 2, "detectors": {det: row}}],
            }
        ]
        return power.summarise(runs, THRESHOLDS)[0]

    row = _fake_row(det, 3.2)
    row["t_within"] = 6.0
    cell = cell_of(row)
    assert (cell["sig_fixed"]["k"], cell["sig_study"]["k"], cell["sig_gate"]["k"]) == (1, 0, 0)
    row = _fake_row(det, 4.0)
    cell = cell_of(row)
    assert (cell["sig_fixed"]["k"], cell["sig_study"]["k"], cell["sig_gate"]["k"]) == (1, 1, 0)
    row = _fake_row(det, 4.4)
    assert cell_of(row)["sig_gate"]["k"] == 1
    row["gate_ic"] = -0.02  # a significant NEGATIVE relation is not a detection
    cell = cell_of(row)
    assert cell["sig_gate"]["k"] == 0 == cell["sig_fixed"]["k"]
    # the break test is two-sided
    row = _fake_row(det, 0.0, break_z=-4.5)
    cell = cell_of(row)
    assert cell["break_gate"]["k"] == 1 == cell["break_fixed"]["k"]


def test_run_row_reads_the_uncrossed_statistics():
    report = {
        "gate_ic": 0.02,
        "nw_tstat": 1.0,
        "nw_tstat_uncrossed": 2.0,
        "nw_tstat_pooled": 3.0,
        "nw_tstat_pooled_uncrossed": 4.0,
        "net_pnl_bootstrap": {"ci_low": 0.5, "ci_high": 2.0},
        "oos_ic_vol_scaled": 0.03,
        "trade_count_1x_cost": 7,
        "leakage": {"recompute_ok": True, "passed": True},
        "n_folds_survive_1x_cost": 2,
        "fold_sign_consistency": 0.75,
        "hypothesis_confirmed": True,
        "verdict": "ITERATE",
    }
    row = power._run_row("EQ04", "10s", report)
    assert (row["alpha_id"], row["horizon"]) == ("EQ04", "10s")
    assert (row["t_within"], row["t_pooled"]) == (2.0, 4.0)
    assert (row["trade_count_1x"], row["recompute_ok"], row["pnl_ci_positive"]) == (7, True, True)
    report["nw_tstat_uncrossed"] = report["nw_tstat_pooled_uncrossed"] = None
    report["net_pnl_bootstrap"]["ci_low"] = None
    row = power._run_row("EQ04", "10s", report)
    assert (row["t_within"], row["t_pooled"], row["pnl_ci_positive"]) == (1.0, 3.0, False)


def test_power_study_rejects_bad_inputs(tmp_path, fake_pipeline):
    with pytest.raises(ResearchError, match="not found") as err:
        power.run_power_study(tmp_path / "nope.json", CONFIGS_DIR)
    assert err.value.code == "power_study_error"
    with pytest.raises(ResearchError, match="all zero"):
        power.run_power_study(CONFIGS_DIR / "marketdata" / "generator.json", CONFIGS_DIR)
    with pytest.raises(ResearchError, match="strictly increasing"):
        power.run_power_study(PLANTED_CONFIG, CONFIGS_DIR, levels=(1.0, 0.5))
    with pytest.raises(ResearchError, match="unknown scenario"):
        power.run_power_study(PLANTED_CONFIG, CONFIGS_DIR, scenarios=("wobble",))
    with pytest.raises(ResearchError, match="sessions"):
        power.run_power_study(PLANTED_CONFIG, CONFIGS_DIR, sessions=[4, 2])
    with pytest.raises(ResearchError, match="sessions"):
        power.run_power_study(PLANTED_CONFIG, CONFIGS_DIR, sessions=[0, 2])
    with pytest.raises(ResearchError, match="break levels"):
        power.run_power_study(PLANTED_CONFIG, CONFIGS_DIR, break_levels=(0.0, 1.0))
    with pytest.raises(ResearchError, match="jobs"):
        power.run_power_study(PLANTED_CONFIG, CONFIGS_DIR, jobs=0)
    with pytest.raises(ResearchError, match="gate_looks"):
        power.run_power_study(PLANTED_CONFIG, CONFIGS_DIR, gate_looks=0)
    with pytest.raises(ResearchError, match="grid is empty"):
        power.run_power_study(PLANTED_CONFIG, CONFIGS_DIR, scenarios=("break",), break_levels=())
    assert fake_pipeline == []


def test_power_cli_writes_the_reports(fake_pipeline, tmp_path, capsys):
    out = tmp_path / "power"
    argv = ["power", "--levels", "0,1", "--seeds", "1", "--sessions", "1,2", "--jobs", "1"]
    assert cli_main([*argv, "--break-levels", "1,2", "--power-out-dir", str(out)]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("# Planted-signal power study")
    assert (out / "POWER_REPORT.md").is_file()
    doc = json.loads((out / "POWER_REPORT.json").read_text(encoding="ascii"))
    assert doc["sessions"] == [1, 2] and doc["break_levels"] == [1.0, 2.0]
    assert {b[1] for b in fake_pipeline} == {2}
    assert cli_main([*argv, "--gate-looks", "50", "--power-out-dir", str(out)]) == 0
    capsys.readouterr()
    doc = json.loads((out / "POWER_REPORT.json").read_text(encoding="ascii"))
    assert doc["protocol"]["gate_looks"] == 50
    for flag, value in (("--levels", "a,b"), ("--sessions", "1,x"), ("--break-levels", "?")):
        bad = ["--json-errors", "power", flag, value, "--jobs", "1", "--power-out-dir", str(out)]
        assert cli_main(bad) == 1
        err = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        assert err["error"]["code"] == "power_study_error"


def test_planted_pipeline_end_to_end_on_a_tiny_universe(tmp_path):
    """The real pipeline (generator -> QC -> features + labels -> every
    detector's validation) on a deliberately tiny planted dataset."""
    base = load_generator_config(PLANTED_CONFIG)
    base["equities"]["slots_per_stream"] = 400
    cfg = power.cell_config(base, 1.0, "stable", 31337, sessions=2)
    frames = power.build_planted_frames(cfg, CONFIGS_DIR, tmp_path / "work")
    assert sorted(frames) == [1, 2, 11]  # the reduced universe
    for df in frames.values():
        assert len(df) > 200
        assert {
            "exchange_ts",
            "trade_imbalance_w10s_v1",
            "ref_ret_1s_v1",
            "label_mid_5s",
            "label_valid_1s",
        } <= set(df.columns)
    detectors = power.detectors_for(base["planted"])
    rows = power.evaluate_run(frames, CONFIGS_DIR, 4.4, detectors, seed=31337)
    assert sorted(rows) == sorted(DETECTOR_IDS)
    for det in detectors:
        row = rows[det["id"]]
        assert (row["alpha_id"], row["horizon"]) == (det["alpha_id"], det["horizon"])
        assert "error" in row or row["verdict"] in ("REJECT", "ITERATE", "PROMOTE")
        if "error" not in row:
            assert {
                "ic_vol_scaled",
                "t_pooled",
                "t_direct",
                "break_z",
                "pnl_ci_positive",
                "n_folds_survive_1x_cost",
                "signal_coverage",
            } <= set(row)
            assert row["verdict"] != "PROMOTE" or row["t_pooled"] >= 4.4
    assert rows == power.evaluate_run(frames, CONFIGS_DIR, 4.4, detectors, seed=31337)  # seeded
    json.dumps(power._rounded(rows), allow_nan=False)
    # the recompute leakage probe runs on the run's OWN events (as the study
    # calls it) and passes; without them it does not run, and nothing else
    # in the row depends on it
    probed = power.evaluate_run(
        frames,
        CONFIGS_DIR,
        4.4,
        detectors[:1] + detectors[2:3],
        seed=31337,
        normalized_dir=tmp_path / "work" / "normalized",
        engine_configs_dir=tmp_path / "work" / "configs",
    )
    assert sorted(probed) == ["lead_lag:EQ10@1s", "order_flow:EQ04@5s"]
    for det_id, row in probed.items():
        if "error" in rows[det_id]:
            assert "error" in row
            continue
        assert rows[det_id]["recompute_ok"] is None and row["recompute_ok"] is True
        assert {**row, "recompute_ok": None} == rows[det_id]
    # nothing leaked outside the work directory's own tree
    assert sorted(p.name for p in (tmp_path / "work").iterdir()) == [
        "configs",
        "feature_registry.json",
        "features",
        "normalized",
        "raw",
    ]


def _assert_same(committed, recomputed, path="cells"):
    """Equal, with floats to one unit of the sixth decimal the report rounds
    to: the committed means were summed by Python 3.11 (the CI runner) and
    ``sum`` of floats is compensated from 3.12 on, which can move the last
    rounded digit of a mean.  Counts, intervals' counts and strings are
    compared exactly."""
    if isinstance(committed, dict):
        assert committed.keys() == recomputed.keys(), path
        for key in committed:
            _assert_same(committed[key], recomputed[key], f"{path}.{key}")
    elif isinstance(committed, list):
        assert len(committed) == len(recomputed), path
        for i, (a, b) in enumerate(zip(committed, recomputed, strict=True)):
            _assert_same(a, b, f"{path}[{i}]")
    elif isinstance(committed, float) and isinstance(recomputed, float):
        assert committed == pytest.approx(recomputed, abs=1.5e-6), path
    else:
        assert committed == recomputed, path


def test_assert_same_is_exact_except_for_the_last_rounded_digit():
    _assert_same([{"k": 3, "mean": 1.234567, "id": "a"}], [{"k": 3, "mean": 1.234568, "id": "a"}])
    for other in ({"k": 4, "mean": 1.234567}, {"k": 3, "mean": 1.234570}, {"k": 3}):
        with pytest.raises(AssertionError):
            _assert_same({"k": 3, "mean": 1.234567}, other)


@pytest.mark.parametrize("sub", ["", "extended"])
def test_committed_power_report_matches_its_json(sub):
    """If the study has been run and committed, the markdown is exactly the
    rendering of the JSON (no hand edits) and the document is well-formed —
    the default grid and the extended one alike."""
    out_dir = REPO_ROOT / "research" / "power" / sub
    json_path = out_dir / "POWER_REPORT.json"
    md_path = out_dir / "POWER_REPORT.md"
    if not json_path.is_file():
        pytest.skip(f"{json_path} not generated")
    doc = json.loads(json_path.read_text(encoding="ascii"))
    assert doc["x-version"] == power.POWER_VERSION
    assert doc["generator_config"] == PLANTED_CONFIG.name
    assert doc["seeds"] == power.study_seeds(doc["base_seed"], len(doc["seeds"]))
    assert len(doc["seeds"]) >= 20  # a rate needs enough seeds to mean something
    reference = load_generator_config(PLANTED_CONFIG)
    assert doc["reference_planted"]["order_flow"] == reference["planted"]["order_flow"]
    assert doc["reference_planted"]["lead_lag"] == reference["planted"]["lead_lag"]
    if sub == "":
        assert doc["sessions"] == power.default_session_grid(reference["sessions"])
        assert doc["levels"] == list(power.DEFAULT_LEVELS)
    else:
        assert doc["sessions"][-1] > reference["sessions"]
    thresholds = doc["protocol"]["thresholds"]
    assert doc["protocol"]["promote_t_threshold"] == max(thresholds.values())
    _assert_same(doc["cells"], power.summarise(doc["runs"], thresholds))
    assert doc["power_model"] == power._rounded(
        power.power_model(doc["runs"], doc["detectors"], thresholds, doc["sessions"])
    )
    assert md_path.read_text(encoding="utf-8").replace("\r\n", "\n") == power.render_markdown(doc)
