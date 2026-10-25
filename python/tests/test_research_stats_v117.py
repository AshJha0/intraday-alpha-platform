"""v1.17 statistics revision (the 2026-10-15 review: R-1, R-2, R-3).

* R-2 - one ``gate_statistics`` function behind the walk-forward gate,
  ``frozen_params_block``, the ``validity`` block and ``tools/r7_confirm.py``;
  the day-cluster t is reported beside the HAC t in every gate (R-P1-3) and
  the frozen t uses the gate's bandwidth, day separation and scaling (R-P1-2).
* R-3 - ``feature_window_ns="registry"``: the post-block embargo of every
  two-sided split is at least the longest feature window (R-P1-6, which was
  implemented in v1.13 and passed by no caller).  ``v2`` / ``v3`` / ``v4``
  carry it at revision v1.17; the v1.14.0 rules are frozen as ``*_v114``.
* R-1 - the R7 confirmatory DSR at the registered N (the repo ledger's
  distinct configurations plus the look), recomputed from the recorded
  per-session IC series without a new look.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest
from iap.validation.cpcv import CombinatorialPurgedSplitter
from iap.validation.methods import (
    METHODS,
    METHODS_V2,
    METHODS_V2_V114,
    METHODS_V3,
    METHODS_V3_V114,
    METHODS_V4,
    METHODS_V4_V114,
    STATS_REVISION,
    V113_REVISION,
    V114_REVISION,
    ledger_bundle,
    ledger_config,
    methods,
)
from iap.validation.metrics import (
    HORIZONS_NS,
    auto_hac_lags,
    day_cluster_tstat,
    gate_statistics,
    ic,
    n_ts_buckets,
    nw_lags,
    pooled_slope_hac_tstat,
)
from iap.validation.splits import WalkForwardSplitter, longest_feature_window_ns
from iap.validation.validate import frozen_params_block
from test_research_validity_v19 import _multi_day_frames, _TwoInstrumentAlpha, _validate

REPO = Path(__file__).resolve().parents[2]
NS_S = 1_000_000_000
NS_DAY = 86_400 * NS_S


def _rows(n_days: int = 4, per_day: int = 1500, rho: float = 0.2, seed: int = 3):
    rng = np.random.default_rng(seed)
    ts, xs, ys = [], [], []
    for d in range(n_days):
        x = rng.standard_normal(per_day)
        y = rho * x + np.sqrt(1 - rho * rho) * rng.standard_normal(per_day)
        ts.append(d * NS_DAY + 14 * 3600 * NS_S + np.arange(per_day, dtype=np.int64) * NS_S)
        xs.append(x)
        ys.append(y)
    return np.concatenate(ts), np.concatenate(xs), np.concatenate(ys)


# ------------------------------------------------------------------- R-2 --


def test_gate_statistics_is_the_pooled_slope_hac_t_and_the_day_cluster_t():
    ts, x, y = _rows()
    h = HORIZONS_NS["1s"]
    s = gate_statistics(ts, x, y, horizon_ns=h)
    lags = auto_hac_lags(n_ts_buckets(ts), h)
    assert s["hac_lags"] == lags and s["hac_bandwidth"] == "auto" and s["hac_day_separated"]
    assert s["ic"] == pytest.approx(ic(x, y))
    assert s["tstat_hac"] == pytest.approx(
        pooled_slope_hac_tstat(ts, x, y, lags=lags, day_ns=NS_DAY)
    )
    assert s["tstat_day_cluster"] == pytest.approx(day_cluster_tstat(ts, x, y))
    assert s["n_pairs"] == ts.size and s["n_days"] == 4
    # the v1.13 rule, by name: the horizon lags and no day separation
    old = gate_statistics(ts, x, y, horizon_ns=h, hac_bandwidth="horizon", day_separated=False)
    assert old["hac_lags"] == nw_lags(h)
    assert old["tstat_hac"] == pytest.approx(pooled_slope_hac_tstat(ts, x, y, lags=nw_lags(h)))
    # an explicit lag count wins and needs no horizon
    assert gate_statistics(ts, x, y, lags=3)["hac_lags"] == 3
    with pytest.raises(ValueError, match="horizon_ns"):
        gate_statistics(ts, x, y)
    with pytest.raises(ValueError, match="hac_bandwidth"):
        gate_statistics(ts, x, y, horizon_ns=h, hac_bandwidth="fixed")
    empty = gate_statistics(np.empty(0, np.int64), np.empty(0), np.empty(0), horizon_ns=h)
    assert empty["n_pairs"] == 0 and np.isnan(empty["tstat_hac"])


def test_validate_alpha_gate_t_is_gate_statistics_and_reports_the_day_cluster_t():
    rep = _validate(_multi_day_frames(), validity_diagnostics=True)
    g = rep["gate_statistics"]
    assert g["rows"] in ("uncrossed", "all")
    assert rep["gate_tstat"] == g["tstat_hac"]
    # the gate t is still the pooled-slope HAC t of the uncrossed rows
    assert rep["gate_tstat"] == rep["nw_tstat_pooled_uncrossed"]
    assert rep["gate_tstat_day_cluster"] == g["tstat_day_cluster"]
    assert rep["gate_n_days"] == g["n_days"] == 4  # the first day is train only
    assert g["hac_lags"] == rep["nw_lags"] and g["hac_bandwidth"] == rep["nw_lags_rule"]
    # the validity block's day-separated HAC t and day-cluster t are the
    # same numbers (one function, the gate's rows)
    v = rep["validity"]
    assert v["tstat_pooled_slope_day_separated"] == pytest.approx(g["tstat_hac"])
    assert v["tstat_day_cluster"] == pytest.approx(g["tstat_day_cluster"])


def test_frozen_t_is_computed_by_the_gate_pipeline():
    frames = _multi_day_frames()
    probe = _TwoInstrumentAlpha()
    model = _TwoInstrumentAlpha()
    model.fit(frames)
    params = model.params()
    h = HORIZONS_NS[probe.horizon]
    new = frozen_params_block(_TwoInstrumentAlpha, frames, params, probe.horizon, h)
    assert new["hac_bandwidth"] == "auto" and new["hac_day_separated"] is True
    assert new["standardisation"] == "instrument" and new["n_days"] == 5
    assert new["frozen_t_day_cluster"] is not None
    old = frozen_params_block(
        _TwoInstrumentAlpha,
        frames,
        params,
        probe.horizon,
        h,
        hac_bandwidth="horizon",
        hac_day_separated=False,
        standardisation="pooled",
    )
    # the v1.13 statistic is still reachable by name, and it differs
    assert old["hac_lags"] == nw_lags(h) and old["frozen_t"] != new["frozen_t"]
    # through validate_alpha the block inherits the run's statistics
    rep = _validate(frames, frozen_params=params)
    assert rep["frozen"]["hac_bandwidth"] == "auto" and rep["frozen"]["standardisation"] == (
        "instrument"
    )
    assert rep["frozen_t"] == rep["frozen"]["frozen_t"]


def test_r7_confirm_pooled_stats_is_gate_statistics():
    spec = importlib.util.spec_from_file_location("r7_confirm_v117", REPO / "tools/r7_confirm.py")
    rc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rc)
    ts, x, y = _rows()
    rows = {"ts": ts, "x": x, "y": y, "unc": np.ones(ts.size, dtype=bool)}
    out = rc.pooled_stats(rows, np.ones(ts.size, dtype=bool), "1s")
    s = gate_statistics(ts, x, y, horizon_ns=HORIZONS_NS["1s"])
    assert out["raw_ic"] == pytest.approx(s["ic"])
    assert out["raw_hac_t"] == pytest.approx(s["tstat_hac"])
    assert out["raw_day_cluster_t"] == pytest.approx(s["tstat_day_cluster"])
    assert out["hac_lags"] == s["hac_lags"] and out["n_days"] == 4


# ------------------------------------------------------------------- R-3 --


def test_v117_bundles_pass_the_registry_feature_window_and_v114_is_frozen():
    for new, old in (
        (METHODS_V2, METHODS_V2_V114),
        (METHODS_V3, METHODS_V3_V114),
        (METHODS_V4, METHODS_V4_V114),
    ):
        a, b = methods(new), methods(old)
        assert a.revision == STATS_REVISION == "v1.17" and b.revision == V114_REVISION
        assert a.feature_window == "registry" and b.feature_window is None
        assert a.validate_kwargs()["feature_window_ns"] == "registry"
        assert "feature_window_ns" not in b.validate_kwargs()
        keep = {f.name for f in dataclasses.fields(a)} - {"name", "feature_window", "revision"}
        assert all(getattr(a, k) == getattr(b, k) for k in keep)
    # the v1.14 rules are what they were (the v1.13 freeze is untouched)
    assert methods(METHODS_V2_V114).hac_bandwidth == "auto"
    assert methods("v2_v113").revision == V113_REVISION
    assert METHODS["legacy_v1"].feature_window is None


def test_ledger_identity_scopes_each_revision_to_its_frozen_bundle():
    base = {"n_folds": 4, "methods": "v2"}
    assert ledger_config(base, methods(METHODS_V2)) == {**base, "stats_revision": "v1.17"}
    assert ledger_config(base, methods(METHODS_V2_V114)) == {**base, "stats_revision": "v1.14"}
    assert ledger_config(base, methods("v2_v113")) == base
    assert ledger_bundle(base) == "v2_v113"
    assert ledger_bundle({**base, "stats_revision": "v1.14"}) == METHODS_V2_V114
    assert ledger_bundle({**base, "stats_revision": "v1.17"}) == METHODS_V2
    runner_entry = {"configuration": {"methods": "v4"}, "experiment_id": "x"}
    assert ledger_bundle({**runner_entry, "stats_revision": "v1.14"}) == METHODS_V4_V114
    assert ledger_bundle({**runner_entry, "stats_revision": "v1.17"}) == METHODS_V4
    assert ledger_bundle({"methods": "v3_v114"}) == METHODS_V3_V114
    with pytest.raises(ValueError, match="stats_revision"):
        ledger_bundle({**base, "stats_revision": "v9.99"})


def test_post_block_embargo_reaches_the_splitters_and_the_report():
    window = longest_feature_window_ns()
    assert window >= 300 * NS_S
    wf = WalkForwardSplitter(n_folds=4, embargo_ns=NS_S, feature_window_ns="registry")
    assert wf.post_embargo_ns == max(NS_S, window)
    cp = CombinatorialPurgedSplitter(6, 2, NS_S, feature_window_ns="registry")
    assert cp.post_embargo_ns == max(NS_S, window)
    frames = _multi_day_frames()
    rep = _validate(frames, feature_window_ns="registry", cpcv=True)
    assert rep["methods"]["feature_window"] == "registry"
    assert rep["methods"]["post_block_embargo_ns"] == max(NS_S, window)
    assert rep["cpcv"]["post_block_embargo_ns"] == max(NS_S, window)
    assert rep["cpcv"]["embargo_ns"] == NS_S
    # off by default: the report states nothing and the cpcv gap is the embargo
    base = _validate(frames, cpcv=True)
    assert "feature_window" not in base["methods"]
    assert base["cpcv"]["post_block_embargo_ns"] == NS_S
    # the walk-forward is one-sided: every gate number is unchanged
    for key in ("gate_ic", "gate_tstat", "oos_ic", "nw_tstat_pooled_uncrossed", "verdict"):
        assert rep[key] == base[key]
    # the two-sided fold rows move, and only after the test block
    for mode in ("row_mass", "day_aligned"):
        a = WalkForwardSplitter(n_folds=2, embargo_ns=NS_S, mode=mode)
        b = WalkForwardSplitter(n_folds=2, embargo_ns=NS_S, mode=mode, feature_window_ns=window)
        ts = frames[1]["exchange_ts"].to_numpy()
        for fa, fb in zip(
            a.folds_by_row_mass(ts) if mode == "row_mass" else a.folds_by_day(ts),
            b.folds_by_row_mass(ts) if mode == "row_mass" else b.folds_by_day(ts),
            strict=True,
        ):
            assert np.array_equal(
                fa.train_mask(ts, NS_S, NS_S, a.post_embargo_ns),
                fb.train_mask(ts, NS_S, NS_S, b.post_embargo_ns),
            )


# ------------------------------------------------------------------- R-1 --


def _load_r7():
    spec = importlib.util.spec_from_file_location("r7_confirm_v117b", REPO / "tools/r7_confirm.py")
    rc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rc)
    return rc


def test_dsr_recompute_uses_the_repo_ledger_count_at_the_look(tmp_path):
    from iap.validation.ledger import ExperimentLedger

    rc = _load_r7()
    root = tmp_path
    (root / "research" / "r7").mkdir(parents=True)
    ledger = ExperimentLedger(root / "research" / "experiments.json")
    for i in range(5):
        ledger.record("EQ01", "k", {"i": i})  # running totals 1..5
    ledger.record("EQ01", "k", {"i": 9}, count=3)  # total 8, 6 distinct
    ledger.save()
    assert rc.trials_at_total(ledger, 5) == 5 and rc.trials_at_total(ledger, 8) == 6
    series = {
        d: v for d, v in zip("abcdefg", [0.06, 0.07, 0.06, 0.07, 0.08, 0.10, 0.06], strict=True)
    }
    recorded = {
        "schema": "iap.r7_confirm_result/v1",
        "results": [
            {
                "alpha": "EQ10@1s",
                "experiment_id": "x",
                "result": {"ic": 0.08, "t_stat": 13.0},
                "gate_threshold": {"used": 4.4, "repo_ledger_total": 5},
                "dsr": {"n_trials": 1, "n_looks": 2, "dsr": 0.99995},
                "per_session_ic": series,
                "gate": {"passed": True},
                "verdict": "confirmed",
                "eligibility": {"gate_eligible": True},
                "sensitivity": {
                    "all_sessions": {
                        "raw_ic": 0.07,
                        "raw_hac_t": 12.0,
                        "per_session_ic": series,
                    }
                },
            },
            {"alpha": "EQ05@1s", "verdict": "no_result"},
        ],
    }
    (root / rc.RESULT_REL).write_text(json.dumps(recorded), encoding="utf-8")
    doc = rc.recompute_dsr(root)
    (r,) = doc["results"]
    b = r["by_trial_count"]
    assert b["as_recorded"]["n_trials"] == 1
    assert r["repo_ledger_distinct_at_look"] == 5 and b["registered_at_look"]["n_trials"] == 6
    assert b["repo_ledger_today"]["n_trials"] == 7
    # deflating at more trials lowers the DSR; the verdict is re-read, not copied
    dsr = [b[k]["deflated_sharpe"]["dsr"] for k in ("as_recorded", "registered_at_look")]
    assert dsr[0] > dsr[1] > 0.95
    # v1.19 R1 (defect D-01): the legacy HAC-t / DSR path yields at most ``ineligible``;
    # a historical ``confirmed`` cannot be re-derived without the canonical gate's
    # PROMOTE, so the re-read verdict differs from the recorded one and the tool says so
    assert all(v["verdict"] == "ineligible" for v in b.values())
    assert r["verdict_unchanged"] is False
    # the recorded result is untouched and the recompute names it
    assert json.loads((root / rc.RESULT_REL).read_text()) == recorded
    assert doc["source"] == rc.RESULT_REL and doc["results"][0]["recorded_dsr"] == 0.99995
    assert rc.recompute_dsr_main(root) == 3  # verdict changed -> exit 3
    assert (root / rc.RECOMPUTE_REL).is_file() and (root / rc.RECOMPUTE_MD_REL).is_file()


def test_committed_dsr_recompute_matches_the_repo_ledger():
    """The committed recompute was produced from the committed result and
    ledger; its trial counts are the ledger's (regenerated with
    ``python tools/r7_confirm.py --recompute-dsr`` when the ledger grows)."""
    rc = _load_r7()
    path = REPO / rc.RECOMPUTE_REL
    if not path.is_file() or not (REPO / rc.RESULT_REL).is_file():
        pytest.skip("R7 confirm result / recompute not in this tree")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["source_sha256"] == rc.sha256(REPO / rc.RESULT_REL)
    for r in doc["results"]:
        b = r["by_trial_count"]
        assert b["registered_at_look"]["n_trials"] == r["repo_ledger_distinct_at_look"] + 1
        assert r["verdict_unchanged"] is True
        assert b["registered_at_look"]["deflated_sharpe"]["dsr"] > rc.DSR_MIN
