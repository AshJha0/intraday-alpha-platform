"""v1.17 C6: the pre-registered net-of-cost screen (iap.research.screen) and
the R-P2-5 maker-filter target fix - synthetic folds only."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from iap.alpha.base import LinearAlpha
from iap.backtest.costs import CostModel
from iap.core.rng import SplitMix64
from iap.labels.maker_labels import maker_label_columns
from iap.research import screen as sc
from iap.research.maker_real import alpha_side_target, alpha_side_target_fill_conditional
from iap.validation.ledger import ExperimentLedger
from iap.validation.methods import methods

NS = 1_000_000_000
T0 = 1_700_000_000 * NS
HORIZON = "5s"
ROOT = Path(__file__).resolve().parents[2]
PREREG = ROOT / "research" / "preregs" / "net_of_cost_screen_prereg_DRAFT.json"


class ToyAlpha(LinearAlpha):
    """One feature, as is.

    Economic rationale: none - a test double with a planted edge.
    """

    alpha_id = "TOY"
    name = "toy"
    asset_class = "EQUITY"
    horizon = HORIZON
    features = ("f1",)

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return df["f1"]


def _normal(rng: SplitMix64, n: int) -> np.ndarray:
    u1 = np.array([max(rng.uniform(), 1e-12) for _ in range(n)])
    u2 = np.array([rng.uniform() for _ in range(n)])
    return np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * np.pi * u2)


def make_frames(edge_bps: float, seed: int = 7, n: int = 6000, step_s: int = 60):
    """Two names over ~4 UTC days (one row a minute); the label is
    ``edge_bps`` x the signal plus noise, the spread 1 bps, mid 100."""
    rng = SplitMix64(seed)
    frames = {}
    for iid in (1, 2):
        f = _normal(rng, n)
        label = 1e-4 * (edge_bps * f + 0.5 * _normal(rng, n))
        frames[iid] = pd.DataFrame(
            {
                "exchange_ts": T0 + np.arange(n, dtype=np.int64) * step_s * NS,
                "f1": f,
                "mid_price_v1": 100.0,
                "spread_bps_v1": 1.0,
                f"label_mid_{HORIZON}": label,
                f"label_valid_{HORIZON}": True,
            }
        )
    return frames


INSTRUMENTS = {1: sc.InstrumentCost(adv=5e6), 2: sc.InstrumentCost(adv=5e6)}
COST = CostModel.load(ROOT / "configs" / "execution" / "execution.json")
FACTORIES = {"toy": lambda aid: ToyAlpha()}


def _spec(**over) -> sc.ScreenSpec:
    kw = dict(
        candidates=(sc.Candidate("TOY", HORIZON, "toy"),),
        size_qty=100.0,
        rules={
            "net_ic_min": 0.0,
            "net_tstat_day_cluster_min": 2.0,
            "net_return_tstat_min": 2.0,
            "n_days_min": 3,
            "n_pairs_min": 500,
        },
        prereg_sha256="deadbeef",
        prereg_path="x.json",
        prereg_status="DRAFT",
    )
    kw.update(over)
    return sc.ScreenSpec(**kw)


# ----------------------------------------------------------- statistics --


def test_row_costs_are_spread_fee_and_two_legs_of_impact():
    mid = np.array([100.0, 100.0, np.nan])
    hs = np.array([0.005, 0.005, 0.005])
    ins = sc.InstrumentCost(adv=1e6)
    c = sc.row_costs(mid, hs, COST, ins, 100.0)
    fixed = (2 * 0.005 + 2 * COST.equity_taker_fee_per_share) / 100.0
    impact = COST.sqrt_impact_coeff_bps * np.sqrt(100.0 / 1e6) * 1e-4
    assert c[0] == pytest.approx(fixed + 2 * impact)
    assert np.isnan(c[2])
    # a maker crosses no spread: the fee and the impact remain
    m = sc.row_costs(mid, hs, COST, ins, 100.0, cross_spread=False)
    assert m[0] == pytest.approx(2 * COST.equity_taker_fee_per_share / 100.0 + 2 * impact)
    assert m[0] < c[0]


def test_net_statistics_are_the_gate_form_on_the_cost_adjusted_label():
    fr = make_frames(edge_bps=6.0)[1]
    ts = fr["exchange_ts"].to_numpy()
    z = fr["f1"].to_numpy()
    y = fr[f"label_mid_{HORIZON}"].to_numpy()
    cost = np.full(len(fr), 2e-4)
    out = sc.net_of_cost_statistics(ts, z, y, cost, horizon_ns=5 * NS)
    from iap.validation.metrics import gate_statistics

    ref = gate_statistics(ts, z, y - np.sign(z) * cost, horizon_ns=5 * NS)
    assert out["net"]["ic"] == pytest.approx(ref["ic"])
    assert out["net"]["tstat_day_cluster"] == pytest.approx(ref["tstat_day_cluster"])
    assert out["gross"]["ic"] > out["net"]["ic"]
    nr = out["net_return"]
    assert nr["mean"] == pytest.approx(float(np.mean(np.sign(z) * y - cost)))
    assert nr["n_days"] == len(nr["day_means"]) >= 4
    assert out["cost"]["mean_round_trip_return"] == pytest.approx(2e-4)
    # zero cost: the net block IS the gross block
    zero = sc.net_of_cost_statistics(ts, z, y, np.zeros(len(fr)), horizon_ns=5 * NS)
    assert zero["net"]["ic"] == pytest.approx(zero["gross"]["ic"])


def test_day_cluster_mean_degrees_of_freedom():
    ts = T0 + np.arange(10, dtype=np.int64) * 86_400 * NS
    out = sc.day_cluster_mean(ts, np.arange(10, dtype=float))
    assert out["n_days"] == 10 and out["inference"]["df"] == 9.0
    one = sc.day_cluster_mean(ts[:1], np.array([1.0]))
    assert one["n_days"] == 1 and np.isnan(one["tstat_day_cluster"])


# ------------------------------------------------------------- the screen --


def test_planted_edge_passes_and_a_weak_edge_fails_net_of_cost(tmp_path):
    bundle = methods("v2")
    strong = sc.run_screen(
        _spec(), make_frames(edge_bps=6.0), INSTRUMENTS, COST, bundle=bundle, factories=FACTORIES
    )
    weak = sc.run_screen(
        _spec(), make_frames(edge_bps=0.05), INSTRUMENTS, COST, bundle=bundle, factories=FACTORIES
    )
    s, w = strong["candidates"][0], weak["candidates"][0]
    assert s["status"] == "pass" and s["passed"] and s["failed_rules"] == []
    assert s["n_folds"] == 4 and all(f["n_test_pairs"] > 0 for f in s["folds"])
    assert s["gross"]["ic"] > s["net"]["ic"] > 0
    assert w["status"] == "fail" and not w["passed"]
    assert "net_return_tstat_min" in w["failed_rules"] or "net_ic_min" in w["failed_rules"]
    # the weak edge is positive gross and negative net: the cost did it
    assert w["net_return"]["mean"] < 0
    assert strong["summary"] == {
        "n_candidates": 1,
        "n_pass": 1,
        "n_fail": 0,
        "n_not_screened": 0,
        "passed": ["TOY/5s/toy"],
    }
    assert strong["prereg_is_draft"] is True
    assert strong["methods"]["stats_revision"] == "v1.17"
    # deterministic bytes
    again = sc.run_screen(
        _spec(), make_frames(edge_bps=6.0), INSTRUMENTS, COST, bundle=bundle, factories=FACTORIES
    )
    assert json.dumps(sc.sanitise(again), sort_keys=True) == json.dumps(
        sc.sanitise(strong), sort_keys=True
    )
    md = sc.render_markdown(strong)
    assert "1 pass, 0 fail" in md and "DRAFT, unsigned" in md
    json.dumps(sc.sanitise(weak), allow_nan=False)


def test_screen_charges_one_ledger_look_per_candidate_and_reruns_add_none(tmp_path):
    led_path = tmp_path / "experiments.json"
    led = ExperimentLedger(led_path, dataset_version="ds-test")
    cands = (sc.Candidate("TOY", HORIZON, "toy"), sc.Candidate("TOY", HORIZON, "missing"))
    doc = sc.run_screen(
        _spec(candidates=cands),
        make_frames(6.0),
        INSTRUMENTS,
        COST,
        factories=FACTORIES,
        ledger=led,
    )
    blob = json.loads(led_path.read_text())
    looks = [e for e in blob["entries"] if e["kind"] == "net_of_cost_screen"]
    assert len(looks) == 2 and blob["total_experiments"] == 2
    assert doc["ledger"]["looks_charged"] == 2
    assert {e["result"]["status"] for e in looks} == {"pass", "not_screened"}
    assert all(e["config"]["prereg_sha256"] == "deadbeef" for e in looks)
    assert all(e["config"]["stats_revision"] == "v1.17" for e in looks)
    assert doc["summary"]["n_not_screened"] == 1
    # same identity again: no new look
    sc.run_screen(
        _spec(candidates=cands),
        make_frames(6.0),
        INSTRUMENTS,
        COST,
        factories=FACTORIES,
        ledger=ExperimentLedger(led_path, dataset_version="ds-test"),
    )
    assert json.loads(led_path.read_text())["total_experiments"] == 2
    # a different size is a different look
    sc.run_screen(
        _spec(candidates=cands, size_qty=200.0),
        make_frames(6.0),
        INSTRUMENTS,
        COST,
        factories=FACTORIES,
        ledger=ExperimentLedger(led_path, dataset_version="ds-test"),
    )
    assert json.loads(led_path.read_text())["total_experiments"] == 4


# --------------------------------------------------------------- refusals --


def _screen_doc(tmp_path: Path, edge: float) -> Path:
    doc = sc.run_screen(_spec(), make_frames(edge), INSTRUMENTS, COST, factories=FACTORIES)
    p = tmp_path / f"screen_{edge}.json"
    p.write_text(json.dumps(sc.sanitise(doc), sort_keys=True), encoding="utf-8")
    return p


def test_require_screened_refuses_failed_and_unscreened_candidates(tmp_path):
    good, bad = _screen_doc(tmp_path, 6.0), _screen_doc(tmp_path, 0.05)
    block = sc.require_screened(good, "TOY", HORIZON, "toy")
    assert block["status"] == "pass" and block["candidate"] == "TOY/5s/toy"
    assert block["prereg_sha256"] == "deadbeef" and len(block["sha256"]) == 64
    with pytest.raises(sc.ScreenRefusal, match="FAILED"):
        sc.require_screened(bad, "TOY", HORIZON, "toy")
    with pytest.raises(sc.ScreenRefusal, match="NOT screened"):
        sc.require_screened(good, "EQ01", "1s")
    assert sc.screen_status(sc.load_screen(good), "TOY", HORIZON, "other") == "not_screened"
    with pytest.raises(sc.ScreenError):
        sc.load_screen(PREREG)


def test_breadth_book_and_prereg_flow_refuse_without_a_pass(tmp_path):
    from iap.agents.broker import BrokerError, WriteBroker

    class Clock:
        t = 1_000 * NS

        def __call__(self) -> int:
            self.t += NS
            return self.t

    from iap.research.breadth_book import screen_check

    good, bad = _screen_doc(tmp_path, 6.0), _screen_doc(tmp_path, 0.05)
    # C7: the book driver's check, before any data is read
    assert screen_check(None, False, None, "eq11") is None
    assert "refused" in screen_check(None, True, None, "eq11")
    assert screen_check(good, True, "TOY/5s/toy", "eq11")["required"] is True
    assert "FAILED" in screen_check(bad, True, "TOY/5s/toy", "eq11")["refused"]
    assert "NOT screened" in screen_check(good, True, None, "eq11")["refused"]
    rec = screen_check(good, False, None, "eq11")
    assert rec["status"] == "not_screened" and rec["required"] is False
    # C8: the pre-registration refuses a failed / unscreened candidate
    root = tmp_path / "ws"
    (root / "research").mkdir(parents=True)
    broker = WriteBroker(root, {"alice"}, clock=Clock())
    with pytest.raises(BrokerError, match="NOT screened"):
        broker.preregister("alice", "EQ01", "1s", "h", 1, screen=good)
    assert not broker.is_preregistered("EQ01", "1s")
    # default behaviour unchanged: no screen, no check
    broker.preregister("alice", "EQ01", "1s", "h", 1)
    assert broker.is_preregistered("EQ01", "1s")
    # the research CLI flag refuses before reading data
    from iap.research.__main__ import main as research_main

    argv = ["--json-errors", "run", "--alpha", "EQ03", "--no-prereg"]
    argv += ["--require-screen", str(bad), "--features-dir", str(tmp_path / "none")]
    assert research_main(argv) == 1


def test_model_identity_helpers():
    assert sc.model_identity("EQ05_z2") == "linear_z_v2"
    assert sc.model_identity("EQ05") == "linear_z_v1"
    assert sc.base_alpha_id("EQ05_z2") == "EQ05" and sc.base_alpha_id("EQ05") == "EQ05"


# ---------------------------------------------------------------- prereg --


def test_prereg_draft_is_unsigned_and_loads():
    doc = json.loads(PREREG.read_text(encoding="utf-8"))
    assert doc["signed"] is False and doc["signature"] is None
    assert doc["status"].startswith("DRAFT")
    assert doc["on_blackboard"] is False
    spec = sc.load_prereg(PREREG)
    assert spec.prereg_signed is False and spec.size_qty == 100.0
    assert len(spec.candidates) == 23 and spec.n_folds == 4
    assert spec.excluded_dataset_versions
    ids = [c.id for c in spec.candidates]
    assert "EQ11/15m/linear_z_v1" in ids and "EQ11/15m/linear_z_v2" not in ids
    # every candidate builds under its model identity
    for c in spec.candidates:
        m = sc.default_factories()[c.model](c.alpha_id)
        assert m.horizon == c.horizon
    with pytest.raises(sc.ScreenError):
        sc.assert_not_holdout(Path("data/real/r7_holdout"), "x", spec)
    with pytest.raises(sc.ScreenError, match="registered holdout"):
        sc.assert_not_holdout(Path("data/real/x"), spec.excluded_dataset_versions[0], spec)
    sc.assert_not_holdout(Path("data/real/multi7"), "other", spec)


def test_prereg_claiming_a_signature_without_one_is_refused(tmp_path):
    doc = json.loads(PREREG.read_text(encoding="utf-8"))
    doc["signed"] = True
    p = tmp_path / "p.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(sc.ScreenError, match="signed without a signature"):
        sc.load_prereg(p)
    with pytest.raises(ValueError, match="unknown acceptance rules"):
        _spec(rules={"made_up": 1.0})


# ---------------------------------------------------------------- R-P2-5 --


def test_fill_conditional_target_separates_not_filled_from_run_over():
    z = np.array([1.0, 1.0, 1.0, -1.0, -1.0, 0.0])
    # bid side: filled & kept, filled & run over, not filled
    bid_nro = np.array([1.0, 0.0, 0.0, 1.0, 1.0, 1.0])
    bid_ro = np.array([0.0, 1.0, np.nan, 0.0, 0.0, 0.0])
    ask_nro = np.array([1.0, 1.0, 1.0, 0.0, 0.0, 1.0])
    ask_ro = np.array([0.0, 0.0, 0.0, 1.0, np.nan, 0.0])
    old = alpha_side_target(z, bid_nro, ask_nro)
    new = alpha_side_target_fill_conditional(z, bid_ro, ask_ro)
    # the old target gives "run over" and "not filled" the same 0.0
    assert old[1] == old[2] == 0.0
    # the fixed target: not filled is NaN, run over is 0.0, kept is 1.0
    assert new[0] == 1.0 and new[1] == 0.0 and np.isnan(new[2])
    assert new[3] == 0.0 and np.isnan(new[4]) and np.isnan(new[5])
    cols = maker_label_columns(("1s",))
    assert "bid_run_over" in cols and "ask_run_over" in cols


def test_maker_fill_target_checks_the_ttl_and_conditions_on_fills():
    n = 4
    lab = pd.DataFrame(
        {
            "exchange_ts": T0 + np.arange(n) * NS,
            "bid_filled": [True, False, True, True],
            "ask_filled": [True, True, False, True],
            "bid_markout_5s_bps": [2.0, np.nan, -3.0, 1.0],
            "ask_markout_5s_bps": [1.0, 4.0, np.nan, -2.0],
        }
    )
    z = np.array([1.0, 1.0, -1.0, -1.0])
    with pytest.raises(sc.ScreenError, match="no ttl_ns"):
        sc.maker_fill_target(z, lab, "5s", 5 * NS)
    lab.attrs["ttl_ns"] = NS
    with pytest.raises(sc.ScreenError, match="label TTL"):
        sc.maker_fill_target(z, lab, "5s", 5 * NS)
    lab.attrs["ttl_ns"] = 5 * NS
    y = sc.maker_fill_target(z, lab, "5s", 5 * NS)
    assert y[0] == pytest.approx(2e-4)  # bid filled
    assert np.isnan(y[1])  # bid not filled: not a loss
    assert np.isnan(y[2])  # ask not filled
    assert y[3] == pytest.approx(2e-4)  # ask filled, run over: sign(z) * y = -2 bps


def test_screen_on_the_maker_target_uses_fee_only_costs():
    frames = make_frames(edge_bps=6.0)
    labels = {}
    for iid, fr in frames.items():
        y = fr[f"label_mid_{HORIZON}"].to_numpy() * 1e4
        lab = pd.DataFrame(
            {
                "exchange_ts": fr["exchange_ts"],
                "bid_filled": True,
                "ask_filled": True,
                "bid_markout_5s_bps": y,
                "ask_markout_5s_bps": -y,
                "ttl_ns": 5 * NS,
            }
        )
        labels[iid] = lab
    spec = _spec(candidates=(sc.Candidate("TOY", HORIZON, "toy", "maker_fill_conditional"),))
    doc = sc.run_screen(spec, frames, INSTRUMENTS, COST, factories=FACTORIES, maker_labels=labels)
    row = doc["candidates"][0]
    assert row["status"] == "pass"
    fee_only = 2 * COST.equity_taker_fee_per_share / 100.0 + 2 * (
        COST.sqrt_impact_coeff_bps * np.sqrt(100.0 / 5e6) * 1e-4
    )
    assert row["cost"]["mean_round_trip_return"] == pytest.approx(fee_only)
    # a mismatched TTL is refused, not used
    for lab in labels.values():
        lab["ttl_ns"] = NS
    doc = sc.run_screen(spec, frames, INSTRUMENTS, COST, factories=FACTORIES, maker_labels=labels)
    assert doc["candidates"][0]["status"] == "not_screened"
    assert "label TTL" in doc["candidates"][0]["error"]


def test_cli_end_to_end_on_synthetic_features(tmp_path):
    """A planted dataset directory: EQ01 under both linear models screened
    from the DRAFT prereg's thresholds, one look each, the document and the
    markdown written, the holdout path refused."""
    rng = SplitMix64(3)
    n, step = 40_000, 10 * NS
    fdir = tmp_path / "ds" / "features"
    fdir.mkdir(parents=True)
    for iid in (1, 2):
        f = _normal(rng, n)
        y = 1e-4 * (0.2 * f + _normal(rng, n))
        pd.DataFrame(
            {
                "instrument_id": iid,
                "exchange_ts": T0 + np.arange(n, dtype=np.int64) * step,
                "micro_mid_dev_bps_v1": f,
                "mid_price_v1": 100.0,
                "spread_bps_v1": 1.0,
                "label_mid_1s": y,
                "label_valid_1s": True,
            }
        ).to_parquet(fdir / f"features_{iid}.parquet", index=False)
    cdir = tmp_path / "ds" / "configs" / "instruments"
    cdir.mkdir(parents=True)
    cdir.joinpath("instruments.json").write_text(
        json.dumps(
            {
                "instruments": [
                    {"instrument_id": i, "symbol": s, "asset_class": "EQUITY", "adv": 5e6}
                    for i, s in ((1, "AAA"), (2, "BBB"))
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "ds" / "dataset.json").write_text('{"dataset_version": "ds-cli"}')
    prereg = json.loads(PREREG.read_text(encoding="utf-8"))
    prereg["candidates"] = [
        {"alpha_id": "EQ01", "horizon": "1s", "model": "linear_z_v1"},
        {"alpha_id": "EQ01", "horizon": "1s", "model": "linear_z_v2"},
    ]
    p = tmp_path / "prereg.json"
    p.write_text(json.dumps(prereg), encoding="utf-8")
    led = tmp_path / "experiments.json"
    argv = ["--prereg", str(p), "--out", str(tmp_path / "out"), "--tag", "t"]
    argv += ["--ledger", str(led), "--no-timestamp"]
    assert sc.main(["--dataset-dir", str(tmp_path / "ds"), *argv]) == 0
    # the dataset's universe must not leak into the process (it emptied FX01's
    # universe for test_legacy_methods in the same xdist worker)
    from iap.alpha import universe_ids

    assert universe_ids("fx") and universe_ids("equity") != (1, 2)
    doc = json.loads((tmp_path / "out" / "screen_t.json").read_text(encoding="utf-8"))
    assert doc["summary"]["n_candidates"] == 2
    assert doc["summary"]["n_not_screened"] == 0
    assert doc["ledger"]["looks_charged"] == 2
    assert doc["dataset"]["dataset_version"] == "ds-cli"
    assert (tmp_path / "out" / "SCREEN_t.md").is_file()
    blob = json.loads(led.read_text())
    assert blob["total_experiments"] == 2
    assert all(e["dataset_version"] == "ds-cli" for e in blob["entries"])
    assert doc["spec"]["prereg"]["sha256"] == sc.file_sha256(p)
    # a holdout-looking path is refused before anything is read
    hold = tmp_path / "r7_holdout"
    hold.mkdir()
    with pytest.raises(sc.ScreenError, match="holdout"):
        sc.main(["--dataset-dir", str(hold), *argv])
    assert json.loads(led.read_text())["total_experiments"] == 2
