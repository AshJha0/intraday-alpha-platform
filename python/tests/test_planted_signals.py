"""Planted effects of known size in the synthetic generator (opt-in, default
off and byte-neutral) and the power study built on them."""

from __future__ import annotations

import copy
import json

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
from iap.research import ResearchError, power
from iap.research.__main__ import main as cli_main

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


#: Fraction of the (single) session the flow tests split at.  The equity
#: flow's slots are spent in the first ~40 % of a session (self-excitation
#: raises the event rate above the slot budget's), so the split sits at the
#: median trade, not at the middle of the clock.
FLOW_SPLIT = 0.2


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


def _fake_row(alpha_id, t, ledger_t=None):
    return {
        "alpha_id": alpha_id,
        "gate_ic": 0.01 * t,
        "t_within": t,
        "t_pooled": t + 0.5,
        "ic_vol_scaled": 0.02 * t,
        "ic_instrument_mean": 0.02 * t,
        "n_folds_survive_1x_cost": 4 if t >= 3.0 else 0,
        "net_pnl_1x_pooled": t,
        "net_pnl_ci_low": t - 3.5,
        "net_pnl_ci_high": t + 3.5,
        "sig_ledger": ledger_t is not None and t >= ledger_t,
        "pnl_ci_positive": t - 3.5 > 0.0,
        "fold_sign_consistency": 1.0 if t > 0 else 0.5,
        "hypothesis_confirmed": t > 0,
        "leakage_passed": True,
        "verdict": "ITERATE" if t >= 1.5 else "REJECT",
        "sig_within": t >= 3.0,
        "sig_pooled": t + 0.5 >= 3.0,
        "evidence": t >= 1.5,
        "promote": False,
    }


@pytest.fixture()
def fake_pipeline(monkeypatch):
    """Replace the (slow) data pipeline: the detector t-stat is a function of
    the planted config, so the grid plumbing is tested exactly."""
    built = []

    def fake_build(cfg, configs_dir, work_dir, universe=power.DEFAULT_UNIVERSE):
        built.append(
            (
                cfg["seed"],
                cfg["planted"]["order_flow"]["strength"],
                cfg["planted"]["lead_lag"]["beta"],
                cfg["planted"]["break"]["post_multiplier"],
            )
        )
        return {1: cfg}

    def fake_evaluate(frames, configs_dir, ledger_t_threshold=None, seed=0):
        cfg = frames[1]
        assert seed == cfg["seed"]
        sign = cfg["planted"]["break"]["post_multiplier"]
        flow = 10.0 * cfg["planted"]["order_flow"]["strength"] * (1.0 if sign > 0 else 0.0)
        lead = 5.0 * cfg["planted"]["lead_lag"]["beta"] * (1.0 if sign > 0 else 0.0)
        return {
            "order_flow": _fake_row("EQ04", flow, ledger_t_threshold),
            "lead_lag": _fake_row("EQ10", lead, ledger_t_threshold),
        }

    monkeypatch.setattr(power, "build_planted_frames", fake_build)
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
        scratch_dir=tmp_path / "scratch",
        progress=lines.append,
    )
    seeds = doc["seeds"]
    # stable at 3 levels + break at the 2 non-zero levels, 2 seeds each
    assert len(doc["runs"]) == (3 + 2) * 2 == len(fake_pipeline) == len(lines)
    assert [(r["scenario"], r["level"]) for r in doc["runs"][::2]] == [
        ("stable", 0.0),
        ("stable", 1.0),
        ("stable", 2.0),
        ("break", 1.0),
        ("break", 2.0),
    ]
    assert {r["seed"] for r in doc["runs"]} == set(seeds)
    cells = {(c["effect"], c["scenario"], c["level"]): c for c in doc["cells"]}
    assert len(cells) == 2 * 5
    null = cells[("order_flow", "stable", 0.0)]
    assert null["n_runs"] == 2 and null["rate_sig_within"] == 0.0 == null["rate_evidence"]
    strong = cells[("order_flow", "stable", 1.0)]  # t = 10 * 0.4 = 4
    assert strong["rate_sig_within"] == 1.0 and strong["mean_t_within"] == 4.0
    assert strong["alpha_id"] == "EQ04" and strong["rate_promote"] == 0.0
    weak = cells[("lead_lag", "stable", 1.0)]  # t = 5 * 0.4 = 2
    assert weak["rate_sig_within"] == 0.0 and weak["rate_evidence"] == 1.0
    assert weak["rate_sig_pooled"] == 0.0  # 2.5 < 3
    assert cells[("lead_lag", "stable", 2.0)]["rate_sig_pooled"] == 1.0  # 4.5
    assert cells[("order_flow", "break", 2.0)]["rate_evidence"] == 0.0
    # the study charges itself for its own looks: 10 runs x 2 detectors
    from iap.validation.ledger import ExperimentLedger

    assert doc["protocol"]["n_tests"] == 20
    assert doc["protocol"]["ledger_t_threshold"] == pytest.approx(
        ExperimentLedger.bonferroni_t_threshold_at(20), abs=1e-6
    )
    assert 3.0 < doc["protocol"]["ledger_t_threshold"] < 4.0
    assert strong["rate_sig_ledger"] == 1.0  # t = 4 clears ~3.02
    assert cells[("lead_lag", "stable", 2.0)]["rate_sig_within"] == 1.0  # t = 4
    assert weak["rate_sig_ledger"] == 0.0
    assert strong["rate_pnl_ci_positive"] == 1.0 and weak["rate_pnl_ci_positive"] == 0.0
    assert strong["mean_ic_vol_scaled"] == 0.08
    assert strong["mean_folds_survive_1x_cost"] == 4.0
    assert doc["cells"] == power.summarise(doc["runs"])
    assert doc["x-version"] == power.POWER_VERSION
    assert doc["protocol"]["stress_version"] == 2
    assert not (tmp_path / "scratch").exists() or not list((tmp_path / "scratch").iterdir())


def test_power_study_document_is_deterministic_and_renders(fake_pipeline, tmp_path):
    kwargs = dict(levels=(0.0, 1.0), n_seeds=2)
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
    assert "| order_flow | EQ04 | stable | 1 | 2 | 1.00 | 1.00 | 1.00 |" in md
    assert "| lead_lag | EQ10 | break | 1 | 2 | 0.00 |" in md
    assert "## Statistics behind the rates" in md and "IC (vol-scaled)" in md
    power.write_reports(a, tmp_path / "out")  # idempotent rewrite
    assert paths["json"].read_bytes() == raw


def test_power_study_counts_an_unvalidatable_run_as_a_miss():
    runs = [
        {
            "scenario": "stable",
            "level": 1.0,
            "seed": 1,
            "detectors": {"order_flow": _fake_row("EQ04", 4.0, 3.5)},
        },
        {
            "scenario": "stable",
            "level": 1.0,
            "seed": 2,
            "detectors": {"order_flow": {"alpha_id": "EQ04", "error": "too few rows"}},
        },
    ]
    (cell,) = power.summarise(runs)
    assert cell["n_runs"] == 2 and cell["n_failed"] == 1
    assert cell["rate_sig_within"] == 0.5 and cell["mean_t_within"] == 4.0
    assert cell["rate_sig_ledger"] == 0.5 and cell["rate_pnl_ci_positive"] == 0.5


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
    assert fake_pipeline == []


def test_power_cli_writes_the_reports(fake_pipeline, tmp_path, capsys):
    out = tmp_path / "power"
    assert cli_main(["power", "--levels", "0,1", "--seeds", "1", "--power-out-dir", str(out)]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("# Planted-signal power study")
    assert (out / "POWER_REPORT.md").is_file() and (out / "POWER_REPORT.json").is_file()
    assert cli_main(["--json-errors", "power", "--levels", "a,b", "--power-out-dir", str(out)]) == 1
    err = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert err["error"]["code"] == "power_study_error"


def test_planted_pipeline_end_to_end_on_a_tiny_universe(tmp_path):
    """The real pipeline (generator -> QC -> features + labels -> the two
    detectors' validation) on a deliberately tiny planted dataset."""
    base = load_generator_config(PLANTED_CONFIG)
    base["equities"]["slots_per_stream"] = 400
    cfg = power.cell_config(base, 1.0, "stable", 31337)
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
    rows = power.evaluate_run(frames, CONFIGS_DIR, ledger_t_threshold=3.3, seed=31337)
    assert sorted(rows) == ["lead_lag", "order_flow"]
    for effect, row in rows.items():
        assert row["alpha_id"] == power.DETECTORS[effect]
        assert "error" in row or row["verdict"] in ("REJECT", "ITERATE", "PROMOTE")
        if "error" not in row:
            assert {
                "ic_vol_scaled",
                "sig_ledger",
                "pnl_ci_positive",
                "n_folds_survive_1x_cost",
                "net_pnl_ci_low",
            } <= set(row)
            assert not (row["sig_ledger"] and not row["sig_within"])  # never looser
    assert rows == power.evaluate_run(
        frames, CONFIGS_DIR, ledger_t_threshold=3.3, seed=31337
    )  # seeded
    json.dumps(rows, allow_nan=False)
    # nothing leaked outside the work directory's own tree
    assert sorted(p.name for p in (tmp_path / "work").iterdir()) == [
        "configs",
        "feature_registry.json",
        "features",
        "normalized",
        "raw",
    ]


def test_committed_power_report_matches_its_json():
    """If the study has been run and committed, the markdown is exactly the
    rendering of the JSON (no hand edits) and the document is well-formed."""
    json_path = REPO_ROOT / "research" / "power" / "POWER_REPORT.json"
    md_path = REPO_ROOT / "research" / "power" / "POWER_REPORT.md"
    if not json_path.is_file():
        pytest.skip("research/power/POWER_REPORT.json not generated")
    doc = json.loads(json_path.read_text(encoding="ascii"))
    assert doc["x-version"] == power.POWER_VERSION
    assert doc["generator_config"] == PLANTED_CONFIG.name
    assert doc["seeds"] == power.study_seeds(doc["base_seed"], len(doc["seeds"]))
    assert doc["cells"] == power.summarise(doc["runs"])
    assert md_path.read_text(encoding="utf-8").replace("\r\n", "\n") == power.render_markdown(doc)
