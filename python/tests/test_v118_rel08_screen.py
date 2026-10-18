"""v1.18 REL-08: the screen driver, the signed rule loader and the decision map.

Synthetic multi-name fixtures only; no real dataset is read."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from iap.agents import signing
from iap.backtest.costs import CostModel
from iap.research import breadth_book as bb
from iap.research import rel08_rule as rr
from iap.research import rel08_screen as rs
from iap.validation.ledger import ExperimentLedger

#: runs whole books on the 7 variants; counted by the python-slow job (and its coverage floor)
pytestmark = pytest.mark.slow

NS_S = 1_000_000_000
NS_MIN = 60 * NS_S
REPO = Path(__file__).resolve().parents[2]
DAY0 = 1548858600 * NS_S  # 2019-01-30 14:30 UTC
N_EQ = 14


def _fixture(seed: int = 3, minutes: int = 180, step_s: int = 30) -> rs.ScreenData:
    """14 equities on two reference ETFs (SPY id 100, QQQ id 101), one session."""
    rng = np.random.default_rng(seed)
    n_rows = minutes * 60 // step_s
    ts = DAY0 + np.arange(n_rows, dtype=np.int64) * step_s * NS_S
    common = np.cumsum(rng.normal(0, 2e-4, n_rows))
    frames, instruments, ref_of, sector_of = {}, {}, {}, {}

    def frame(logp: np.ndarray, beta: float) -> pd.DataFrame:
        b = np.full(n_rows, beta)
        b[: 10 * 60 // step_s] = np.nan  # the window feature starts late
        return pd.DataFrame(
            {
                "exchange_ts": ts,
                "mid_price_v1": 50.0 * np.exp(logp),
                "spread_bps_v1": rng.uniform(1.0, 3.0, n_rows),
                "beta_w5m_v1": b,
                "traded_volume_w10s_v1": rng.integers(0, 2000, n_rows).astype(float),
                "ret_log_1m_v1": np.zeros(n_rows),
                "ref_ret_1m_v1": np.zeros(n_rows),
            }
        )

    for iid, sym in ((100, "SPY"), (101, "QQQ")):
        frames[iid] = frame(common + np.cumsum(rng.normal(0, 5e-5, n_rows)), 1.0)
        instruments[iid] = bb.Instrument(iid, sym, "ETF", 5e7, 100, 0.01)
    for i in range(1, N_EQ + 1):
        beta = 0.7 + 0.05 * i
        idio = np.cumsum(rng.normal(0, 3e-4, n_rows))
        frames[i] = frame(beta * common + idio, beta)
        instruments[i] = bb.Instrument(i, f"N{i}", "EQUITY", 5e6, 100, 0.01)
        ref_of[i] = 101 if i % 2 else 100
        sector_of[i] = "A" if i <= N_EQ // 2 else "B"
    return rs.ScreenData(
        frames=frames,
        instruments=instruments,
        cost_model=CostModel(2.0, 0.003, 2.5),
        ref_of=ref_of,
        sector_of=sector_of,
    )


def _test_rule(**suff) -> rr.Rule:
    """The committed draft with sufficiency sized for the 14-name fixture and a
    short bootstrap (every other number as committed)."""
    doc = copy.deepcopy(rr.load_draft().doc)
    small = {
        "names_per_rebalance_min": 8,
        "names_usable_min": 8,
        "usable_rebalances_min": {"5": 10, "15": 4, "30": 2},
    }
    doc["sufficiency"].update({**small, **suff})
    doc["statistics"]["draws"] = 199
    doc["book"]["cov_warmup_min"] = 30
    rr.validate_rule(doc)
    return rr.Rule(doc=doc, signed=True, path="test")


# ------------------------------------------------------------ rule doc --


def test_committed_draft_matches_markdown_and_registered_list():
    rule = rr.load_draft()
    assert not rule.signed and rule.K == 7 == len(rule.variants)
    assert rule.level == pytest.approx(1 - 0.05 / 7)
    ids = [v["id"] for v in rule.variants]
    assert ids == ["V1", "V2", "V5", "V6", "V9", "V10", "V11"]
    assert [v["id"] for v in rule.doc["deferred_variants"]] == ["V3", "V4", "V7", "V8"]
    th = rule.thresholds
    assert (th["C2_mde_max_bps"], th["C3_net_min_bps"], th["C4_ess_min"]) == (2.0, 1.0, 20)
    assert (th["C6_names_removed"], th["C8_ic_min"]) == (5, 0.02)
    assert rule.section("statistics")["seed"] == 20261020
    assert rule.section("statistics")["draws"] == 9999
    assert rule.section("book")["notional"] == 1_000_000.0
    assert rule.section("decision")["max_continue"] == 3
    assert rule.doc["label"] == rs.SCREEN_LABEL


def test_validate_rule_rejects_edits():
    doc = copy.deepcopy(rr.load_draft().doc)
    doc["variants"][0]["horizon_min"] = 10
    with pytest.raises(rr.RuleRefused, match="registered list"):
        rr.validate_rule(doc)
    doc = copy.deepcopy(rr.load_draft().doc)
    del doc["thresholds"]["C3_net_min_bps"]
    with pytest.raises(rr.RuleRefused, match="C3_net_min_bps"):
        rr.validate_rule(doc)
    doc = copy.deepcopy(rr.load_draft().doc)
    doc["family"]["K"] = 10
    with pytest.raises(rr.RuleRefused, match="family.K"):
        rr.validate_rule(doc)
    doc = copy.deepcopy(rr.load_draft().doc)
    doc["variants"].append(doc["deferred_variants"].pop(0))  # V3 back into the one-day family
    with pytest.raises(rr.RuleRefused, match="registered"):
        rr.validate_rule(doc)


def _tmp_root(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    for rel in (rr.RULE_MD_REL, rr.DRAFT_REL):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, root / rel)
    priv, pub = signing.generate_keypair()
    (root / "research" / "agents").mkdir(parents=True)
    (root / "research" / "agents" / "trust_roots.json").write_text(
        json.dumps({"maintainers": {"tester": pub}})
    )
    return root, priv


def _sign(root: Path, priv: str, draft: dict | None = None) -> Path:
    d = draft if draft is not None else rr.load_draft(root).doc
    ident = {"scripts": {"x": "0" * 64}, "thresholds": dict(d["thresholds"])}
    doc = rr.sign_rule(d, priv, "tester", ident, "test", "2026-10-18T00:00:00+00:00")
    out = root / rr.SIGNED_REL
    out.write_text(rr.render(doc), encoding="ascii")
    return out


def test_signed_rule_loads_and_refuses_unsigned_tampered_or_mismatched(tmp_path):
    root, priv = _tmp_root(tmp_path)
    with pytest.raises(rr.RuleRefused, match="no signed rule"):
        rr.load_signed_rule(None, root)
    with pytest.raises(rr.RuleRefused, match="not signed"):
        rr.load_signed_rule(root / rr.DRAFT_REL, root)
    out = _sign(root, priv)
    rule = rr.load_signed_rule(out, root)
    assert rule.signed and rule.message_sha256 and rule.thresholds["C3_net_min_bps"] == 1.0
    # tampered threshold: the signature no longer covers the body
    doc = json.loads(out.read_text())
    doc["thresholds"]["C3_net_min_bps"] = 0.1
    out.write_text(json.dumps(doc))
    with pytest.raises(rr.RuleRefused, match="sha256 mismatch"):
        rr.load_signed_rule(out, root)
    # a validly signed body that is not the committed draft
    other = copy.deepcopy(rr.load_draft(root).doc)
    other["thresholds"]["C3_net_min_bps"] = 0.1
    _sign(root, priv, other)
    with pytest.raises(rr.RuleRefused, match="differs from"):
        rr.load_signed_rule(out, root)
    # a key that is not the trust root's
    priv2, _ = signing.generate_keypair()
    _sign(root, priv2)
    with pytest.raises(rr.RuleRefused, match="trust-root"):
        rr.load_signed_rule(out, root)
    # the markdown edited after the draft was written
    _sign(root, priv)
    md = root / rr.RULE_MD_REL
    md.write_text(md.read_text(encoding="utf-8") + "\nedit\n", encoding="utf-8")
    with pytest.raises(rr.RuleRefused, match="disagree"):
        rr.load_signed_rule(out, root)


def test_main_refuses_unsigned_rule_before_touching_data(tmp_path, monkeypatch):
    from iap.research import holdout_registry

    def boom(*a, **k):
        raise AssertionError("the dataset guard ran before the rule check")

    monkeypatch.setattr(holdout_registry, "guard", boom)
    monkeypatch.setattr(rs, "load_screen_data", boom)
    code = rs.main(
        [
            "--dataset-dir",
            str(tmp_path / "nope"),
            "--rule",
            str(REPO / rr.DRAFT_REL),
            "--ledger",
            str(tmp_path / "l.json"),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert code == 2
    assert not (tmp_path / "l.json").exists() and not (tmp_path / "out").exists()


def test_gate_refuses_synthetic_or_unlisted_dataset(tmp_path, monkeypatch):
    from iap.research import holdout_registry

    rule = rr.Rule(doc=rr.load_draft().doc, signed=True, path="x")
    monkeypatch.setattr(rr, "load_signed_rule", lambda *a, **k: rule)
    monkeypatch.setattr(
        holdout_registry, "guard", lambda *a, **k: {"state": "synthetic", "dataset_version": "x"}
    )
    with pytest.raises(holdout_registry.HoldoutRefused, match="in_sample"):
        rs.gate(tmp_path, None, REPO)
    monkeypatch.setattr(
        holdout_registry, "guard", lambda *a, **k: {"state": "in_sample", "dataset_version": "x"}
    )
    with pytest.raises(holdout_registry.HoldoutRefused, match="not the rule's"):
        rs.gate(tmp_path, None, REPO)


# ---------------------------------------------------------- look-ahead --


def _perturb_after(data: rs.ScreenData, t_cut: int, seed: int = 11) -> rs.ScreenData:
    rng = np.random.default_rng(seed)
    frames = {}
    for iid, df in data.frames.items():
        df = df.copy()
        late = df["exchange_ts"].to_numpy() > t_cut
        for c in ("mid_price_v1", "spread_bps_v1", "beta_w5m_v1", "traded_volume_w10s_v1"):
            v = df[c].to_numpy(dtype=float).copy()
            v[late] = v[late] * rng.uniform(0.5, 1.5, int(late.sum()))
            df[c] = v
        frames[iid] = df
    return rs.ScreenData(frames, data.instruments, data.cost_model, data.ref_of, data.sector_of)


def _truncate_after(data: rs.ScreenData, t_cut: int) -> rs.ScreenData:
    frames = {
        i: df[df["exchange_ts"] <= t_cut].reset_index(drop=True) for i, df in data.frames.items()
    }
    return rs.ScreenData(frames, data.instruments, data.cost_model, data.ref_of, data.sector_of)


@pytest.mark.parametrize("score,h_min", [("vwap_deviation", 15), ("residual_reversion", 30),
                                         ("etf_catch_up", 1), ("etf_catch_up", 5)])  # fmt: skip
@pytest.mark.parametrize("probe", ["shift", "truncate"])
@pytest.mark.parametrize("alt", [False, True])
def test_scores_use_no_future_rows(score, h_min, probe, alt):
    data = _fixture()
    ids = data.universe(score)
    times = DAY0 + np.arange(20, 175, 5, dtype=np.int64) * NS_MIN
    t_cut = int(DAY0 + 100 * NS_MIN)
    h = h_min * NS_MIN
    other = _perturb_after(data, t_cut) if probe == "shift" else _truncate_after(data, t_cut)
    a, _ = rs.compute_scores(data, ids, score, h, times, 60 * NS_S, 8, alt=alt)
    b, _ = rs.compute_scores(other, ids, score, h, times, 60 * NS_S, 8, alt=alt)
    past = times <= t_cut
    np.testing.assert_array_equal(a[:, past], b[:, past])
    assert np.isfinite(a[:, past]).sum() > 0
    assert not np.array_equal(np.nan_to_num(a[:, ~past]), np.nan_to_num(b[:, ~past]))


@pytest.mark.parametrize("probe", ["shift", "truncate"])
def test_labels_depend_only_on_the_horizon_window(probe):
    data = _fixture()
    ids = data.universe("vwap_deviation")
    times = DAY0 + np.arange(20, 175, 5, dtype=np.int64) * NS_MIN
    t_cut = int(DAY0 + 100 * NS_MIN)
    h = 15 * NS_MIN
    other = _perturb_after(data, t_cut) if probe == "shift" else _truncate_after(data, t_cut)
    a = rs.compute_labels(data, ids, h, times, 60 * NS_S)
    b = rs.compute_labels(other, ids, h, times, 60 * NS_S)
    done = times + h <= t_cut
    np.testing.assert_array_equal(a[:, done], b[:, done])
    # a label whose horizon crosses the session close is incomplete
    close = max(int(df["exchange_ts"].max()) for df in data.frames.values())
    assert np.all(np.isnan(a[:, times + h > close]))


def test_beta_is_point_in_time():
    data = _fixture()
    df = data.frames[3]
    times = DAY0 + np.array([5, 9, 12, 60], dtype=np.int64) * NS_MIN
    b = rs.beta_at(df, times)
    assert np.isnan(b[0]) and np.isnan(b[1])  # no finite beta yet: none borrowed from later
    assert np.isfinite(b[2]) and np.isfinite(b[3])


# ------------------------------------------------------- decision map --


def _stats(net=2.0, lower=0.5, mde=1.0, ess=30.0, ic=0.05, ic_lower=0.01, cons=1.5, infl=1.5,
           alt=1.5, n_usable=60, floor=50, names=80, gap=10.0, vid="V1"):  # fmt: skip
    return {
        "id": vid,
        "n_usable": n_usable,
        "usable_floor": floor,
        "names_usable_80pct": names,
        "max_gap_min": gap,
        "net_bps": {"mean": net, "lower": lower, "mde": mde, "ess": ess},
        "rank_ic": {"mean": ic, "lower": ic_lower},
        "conservative_net_bps": cons,
        "influential_removed_net_bps": infl,
        "alt_normalisation_net_bps": alt,
    }


def test_decision_map_one_case_per_outcome():
    rule = rr.load_draft()
    cases = {
        "underpowered": [
            _stats(n_usable=40),  # C1: too few usable rebalances
            _stats(names=60),  # C1: too few names usable in 80 %
            _stats(gap=45.0),  # C1: a 45-minute hole
            _stats(mde=2.5),  # C2
            _stats(ess=12.0),  # C4
        ],
        "stop_c3": [_stats(net=0.8), _stats(lower=-0.1)],
        "stop_fragile": [
            _stats(cons=0.9),  # C5 retention < 50 %
            _stats(cons=-0.1),  # C5 sign
            _stats(infl=-1.0),  # C6 sign flip
            _stats(alt=0.5),  # C7 < 50 %
            _stats(ic=0.015),  # C8 floor
            _stats(ic_lower=-0.001),  # C8 lower bound
        ],
        "continue": [_stats()],
    }
    for name, rows in cases.items():
        for s in rows:
            (d,) = rs.decide([s], rule)
            want = "stop" if name.startswith("stop") else name
            assert d["outcome"] == want, (name, s, d)
            if name == "stop_c3":
                assert "C3" in d["reason"]
            if name == "stop_fragile":
                assert "fragile" in d["reason"]
    # an IC that ranks first is not a reason to continue (C8 is never sufficient)
    (d,) = rs.decide([_stats(ic=0.5, ic_lower=0.3, net=0.2)], rule)
    assert d["outcome"] == "stop"


def test_decision_map_caps_continues_by_lower_bound():
    rule = rr.load_draft()
    rows = [_stats(vid=f"V{k}", lower=lb) for k, lb in enumerate([0.2, 0.9, 0.4, 0.9, 0.1], 1)]
    out = {d["id"]: d["outcome"] for d in rs.decide(rows, rule)}
    assert out == {
        "V1": "passed_not_expanded",
        "V2": "continue",
        "V3": "continue",
        "V4": "continue",  # tie with V2 on the bound: variant order breaks it
        "V5": "passed_not_expanded",
    }


def test_thresholds_are_read_from_the_rule():
    doc = copy.deepcopy(rr.load_draft().doc)
    doc["thresholds"]["C3_net_min_bps"] = 3.0
    rule = rr.Rule(doc=doc, signed=True)
    (d,) = rs.decide([_stats(net=2.0)], rule)
    assert d["outcome"] == "stop"


# ----------------------------------------------------------- statistics --


def test_bootstrap_and_ess_arithmetic():
    rule = rr.load_draft()
    x = np.random.default_rng(0).normal(1.0, 2.0, 78)
    s1 = rs.series_stats(x, 6, rule)
    s2 = rs.series_stats(x, 6, rule)
    assert s1 == s2  # seeded
    assert s1["mean"] == pytest.approx(x.mean())
    assert s1["lower"] < s1["mean"]
    assert s1["mde"] == pytest.approx(2.49 * s1["se"])
    assert s1["ess"] <= 78
    # positively autocorrelated series: inflation > 1, ESS < n
    y = np.repeat(np.random.default_rng(1).normal(0, 1, 13), 6)
    s3 = rs.series_stats(y, 6, rule)
    assert s3["nw_inflation"] > 1.0 and s3["ess"] < 78
    assert rs.block_length(5, 30) == 6 and rs.block_length(15, 30) == 2
    assert rs.block_length(30, 30) == 1


# ------------------------------------------------------------- end to end --


def test_run_screen_ledger_look_before_result_and_deterministic(tmp_path):
    data = _fixture()
    rule = _test_rule()
    ledger_path = tmp_path / "experiments.json"
    seen: list[str] = []

    def evaluate(d, r, v):
        led = ExperimentLedger(ledger_path)
        ids = [e["alpha_id"] for e in led.entries if e["kind"] == rs.LEDGER_KIND]
        assert v["id"] in ids, "the look must be saved before the variant is evaluated"
        assert all(e.get("data_family") == "real" for e in led.entries)
        seen.append(v["id"])
        return rs.evaluate_variant(d, r, v)

    led = ExperimentLedger(ledger_path, dataset_version="dsv", data_family="real")
    rep = rs.run_screen(data, rule, led, evaluate=evaluate)
    assert seen == ["V1", "V2", "V5", "V6", "V9", "V10", "V11"]
    assert ExperimentLedger(ledger_path).total_experiments == 7
    # every variant evaluated; decisions cover all; outcomes are from the map
    assert {d["outcome"] for d in rep["decisions"]} <= set(rs.OUTCOMES)
    assert len(rep["variants"]) == 7 and rep["label"] == rs.SCREEN_LABEL
    v1 = rep["variants"][0]
    assert v1["n_usable"] > 0 and v1["net_bps"]["n"] > 0
    assert v1["book"]["max_abs_net_exposure"] < 1e-6
    v6 = rep["variants"][3]
    assert v6["n_names"] == N_EQ and v6["book"]["max_abs_sector_exposure"] < 1e-6
    assert len(v1["influential_names"]) == 5
    # deterministic bytes; a rerun adds no look
    led2 = ExperimentLedger(ledger_path, dataset_version="dsv", data_family="real")
    rep2 = rs.run_screen(data, rule, led2)
    assert rs.dumps({**rep, "ledger": None}) == rs.dumps({**rep2, "ledger": None})
    fresh = ExperimentLedger(tmp_path / "fresh.json", dataset_version="dsv", data_family="real")
    assert rs.dumps(rs.run_screen(data, rule, fresh)) == rs.dumps(rep)
    assert ExperimentLedger(ledger_path).total_experiments == 7
    # scorecard from the raw JSON alone
    raw = json.loads(rs.dumps(rep))
    card = rs.render_scorecard(raw)
    assert card.startswith("# REL-08 scorecard: hypothesis screen, one session, no edge claim")
    for head in ("Hypothesis and registration", "Data", "Signal", "Net of cost", "Robustness",
                 "Risk and neutrality", "Multiple testing", "Verdict", "does not claim"):  # fmt: skip
        assert head in card
    assert card == rs.render_scorecard(json.loads(rs.dumps(rep2)))


def test_underpowered_when_the_universe_is_thin():
    data = _fixture()
    rule = _test_rule(names_per_rebalance_min=8, names_usable_min=40)
    v = rule.variants[4]  # V9
    res = rs.evaluate_variant(data, rule, v)
    (d,) = rs.decide([res], rule)
    assert d["outcome"] == "underpowered" and not d["criteria"]["C1"]
