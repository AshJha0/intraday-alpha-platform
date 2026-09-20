"""Unit tests of the MVP loop (``iap.mvp``) on the tiny configuration.

Covers: fail-fast config validation; protocol conformance of every MVP
component against ``iap.contracts.protocols``; the streaming alpha adapter
against the batch scorer and the alpha golden; the §12.1 P&L identity; a
REJECTed child is never submitted and never fills; filled <= submitted per
child; every fill inside its parent window; every trace validates against
the schema; store row counts equal trace stage counts; run-twice
determinism.  The golden session itself is ``test_mvp_golden.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from iap.alpha import load_params_file
from iap.contracts.protocols import (
    Alpha,
    ExecutionAlgorithm,
    ExecutionSimulatorLike,
    FeatureEngineLike,
    MarketDataSource,
    PortfolioConstructor,
    RiskEngineLike,
    SmartOrderRouterLike,
    TCAEngine,
    TraceSink,
)
from iap.contracts.types import Decision, DecisionTrace, ExecStatus, Side
from iap.contracts.validate import validate_typed
from iap.features.engine import FeatureEngine, FeatureVector
from iap.features.registry import build_registry, registry_hash
from iap.mvp.adapters import (
    AlgoScheduler,
    RiskEngineAdapter,
    SimulatorAdapter,
    SorAdapter,
    TcaAdapter,
    rule_index_of,
)
from iap.mvp.alpha import AlphaEnsemble, LinearZAlpha
from iap.mvp.config import REPO_ROOT, MvpConfig, load_config
from iap.mvp.engine import MvpEngine, pearson
from iap.mvp.feed import JsonlMarketDataSource, generate_feed, load_feed
from iap.mvp.portfolio import SingleStockPortfolio, round_half_away
from iap.mvp.session import (
    PAPER_EVIDENCE_FILE,
    REPORT_JSON,
    STORE_FILE,
    TRACES_FILE,
    RunResult,
    compare_runs,
    run_session,
)
from iap.risk.events import Rules, Scope
from iap.store.db import Store
from iap.trace.digest import TraceDigest
from iap.trace.sinks import MemoryTraceSink

TINY_CONFIG = REPO_ROOT / "configs" / "mvp" / "mvp_tiny.json"
GOLDEN_DIR = REPO_ROOT / "tests" / "golden"


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def tiny_cfg() -> MvpConfig:
    return load_config(TINY_CONFIG)


@pytest.fixture(scope="module")
def tiny_run(tiny_cfg: MvpConfig, tmp_path_factory: pytest.TempPathFactory) -> RunResult:
    out = tmp_path_factory.mktemp("mvp-tiny") / "run"
    feed = generate_feed(tiny_cfg, out)
    return run_session(tiny_cfg, feed, out)


def _traces(run: RunResult) -> List[DecisionTrace]:
    with open(run.out_dir / TRACES_FILE, encoding="ascii") as fh:
        return [DecisionTrace.from_dict(json.loads(line)) for line in fh if line.strip()]


# ------------------------------------------------------------- config


def _doc() -> Dict[str, Any]:
    return json.loads(TINY_CONFIG.read_text())


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d.__setitem__("x-version", 2), "x-version"),
    (lambda d: d.pop("seed"), "seed"),
    (lambda d: d.__setitem__("seed", -1), "seed"),
    (lambda d: d.__setitem__("venues", ["XV1", "XV1"]), "venues"),
    (lambda d: d["session"].__setitem__("close", "13:30:00"), "session"),
    (lambda d: d["session"].__setitem__("open", "1:30:00"), "session.open"),
    (lambda d: d["portfolio"].__setitem__("ewma_lambda", 1.0), "ewma_lambda"),
    (lambda d: d["portfolio"].__setitem__("conf_min", 1.5), "conf_min"),
    (lambda d: d["portfolio"]["solver"].__setitem__("iters", 0), "iters"),
    (lambda d: d["execution"].__setitem__("urgency_bands", [{"max_urgency": 0.5, "algo": "TWAP"}]),
     "urgency_bands"),
    (lambda d: d["execution"].__setitem__("urgency_bands", [
        {"max_urgency": 0.5, "algo": "TWAP"}, {"max_urgency": 1.0, "algo": "VWAP"}]), "VWAP"),
    (lambda d: d["execution"].__setitem__("urgency_bands", [
        {"max_urgency": 0.5, "algo": "TWAP"}, {"max_urgency": 0.4, "algo": "IS"},
        {"max_urgency": 1.0, "algo": "POV"}]), "ascending"),
    (lambda d: d["execution"].__setitem__("parent_window_ns", 2_000_000_000), "parent_window_ns"),
    (lambda d: d["sor"].__setitem__("prefer_rebate", "yes"), "prefer_rebate"),
    (lambda d: d["reference"].__setitem__("risk", "configs/risk/nope.json"), "reference.risk"),
    (lambda d: d.__setitem__("notes", "not a list"), "notes"),
])
def test_config_validation_names_the_key(mutate, needle: str) -> None:
    doc = _doc()
    mutate(doc)
    with pytest.raises(ValueError) as exc:
        MvpConfig.from_document(doc, where="mvp.json")
    assert needle in str(exc.value)


def test_config_loads_and_hashes(tiny_cfg: MvpConfig) -> None:
    assert tiny_cfg.instrument == "SYN.EQ.AAPL"
    assert tiny_cfg.alphas == ("EQ01", "EQ03", "EQ06")
    assert len(tiny_cfg.run_id) == 16 and len(tiny_cfg.config_version()) == 64
    assert tiny_cfg.session_id == f"mvp-{tiny_cfg.run_id}"
    over = tiny_cfg.with_overrides(seed=7)
    assert over.seed == 7 and over.run_id != tiny_cfg.run_id
    assert over.config_version() != tiny_cfg.config_version()
    assert tiny_cfg.execution.algo_for(0.0).value == "TWAP"
    assert tiny_cfg.execution.algo_for(1.0).value == "IS"
    with pytest.raises(ValueError):
        MvpConfig.load(REPO_ROOT / "configs" / "mvp" / "missing.json")


def test_sor_block_must_match_execution_json(tiny_cfg: MvpConfig, tmp_path: Path) -> None:
    doc = _doc()
    doc["sor"]["max_venue_latency_ns"] = 999
    cfg = MvpConfig.from_document(doc, where="mvp.json")
    feed = generate_feed(tiny_cfg, tmp_path / "feed")
    with pytest.raises(ValueError, match="sor block must equal"):
        MvpEngine(cfg, feed, MemoryTraceSink())


def test_unknown_instrument_fails_at_feed(tiny_cfg: MvpConfig, tmp_path: Path) -> None:
    bad = tiny_cfg.with_overrides(instrument="SYN.EQ.NOPE")
    with pytest.raises(ValueError, match="not defined"):
        generate_feed(bad, tmp_path)


# ---------------------------------------------------------- protocols


def test_components_satisfy_the_contract_protocols(tiny_run: RunResult) -> None:
    eng = tiny_run.engine
    assert isinstance(JsonlMarketDataSource(tiny_run.out_dir / "events.jsonl"), MarketDataSource)
    assert isinstance(eng.features, FeatureEngineLike)
    for alpha in eng.alphas:
        assert isinstance(alpha, Alpha)
    assert isinstance(eng.portfolio, PortfolioConstructor)
    assert isinstance(eng.risk, RiskEngineLike) and isinstance(eng.risk, RiskEngineAdapter)
    assert isinstance(eng.scheduler, ExecutionAlgorithm)
    assert isinstance(eng.scheduler, AlgoScheduler)
    assert isinstance(eng.sor, SmartOrderRouterLike) and isinstance(eng.sor, SorAdapter)
    assert isinstance(eng.sim, ExecutionSimulatorLike) and isinstance(eng.sim, SimulatorAdapter)
    assert isinstance(eng.tca, TCAEngine) and isinstance(eng.tca, TcaAdapter)
    assert isinstance(eng.sink, TraceSink)


def test_market_data_source_filters_by_event_time(tiny_run: RunResult) -> None:
    src = JsonlMarketDataSource(tiny_run.out_dir / "events.jsonl")
    events = tiny_run.feed.events
    lo, hi = events[10].exchange_ts, events[50].exchange_ts
    window = list(src.events(lo, hi))
    assert window and all(lo <= ev.exchange_ts < hi for ev in window)
    assert len(window) == sum(1 for ev in events if lo <= ev.exchange_ts < hi)
    with pytest.raises(ValueError):
        list(src.events(hi, lo))


# -------------------------------------------------------------- alphas


def test_streaming_alpha_matches_batch_scorer_and_alpha_golden() -> None:
    names = [s.name for s in build_registry()]
    models = load_params_file(REPO_ROOT / "configs" / "strategies" / "alpha_params.json",
                              expected_feature_version=registry_hash())
    golden = json.loads((GOLDEN_DIR / "expected_alpha.json").read_text())
    for aid in ("EQ01", "EQ03", "EQ06"):
        adapter = LinearZAlpha(models[aid], names, 1_000_000_000)
        assert adapter.alpha_id == aid and len(adapter.version) == 64
        for case in golden["alphas"][aid]["cases"]:
            values = [0.0] * len(names)
            validity = [False] * len(names)
            for fname, val in case["inputs"].items():
                i = names.index(fname)
                values[i] = float(val)
                validity[i] = True
            vec = FeatureVector(instrument_id=12, timestamp=case["exchange_ts"],
                                feature_version=registry_hash(), values=values, validity=validity)
            sig = adapter.generate(vec)
            assert sig.expected_return == pytest.approx(case["expected_return"], abs=1e-9, rel=1e-9)
            assert sig.confidence == pytest.approx(case["confidence"], abs=1e-9, rel=1e-9)
            assert sig.model_version == aid and sig.instrument_id == 12
        # an invalid input scores (0, 0), never NaN
        vec = FeatureVector(instrument_id=12, timestamp=1, feature_version=registry_hash(),
                            values=[0.0] * len(names), validity=[False] * len(names))
        sig = adapter.generate(vec)
        assert sig.expected_return == 0.0 and sig.confidence == 0.0
    members = [LinearZAlpha(models[a], names, 10**9) for a in ("EQ06", "EQ01", "EQ03")]
    ens = AlphaEnsemble(members, 10**9)
    assert ens.alpha_id == "EQ01-EQ03-EQ06" and len(ens.version) == 64


# ---------------------------------------------------------- invariants


def test_pnl_identity_holds(tiny_run: RunResult) -> None:
    eng = tiny_run.engine
    eng.assert_pnl_identity()
    p = tiny_run.report["pnl"]
    assert p["identity_abs_diff"] <= 1e-9 * max(1.0, abs(p["risk_daily"]))
    expected_total = (p["gross"] - p["spread_cost"]) - p["fees_net"] - p["impact"]
    assert p["total"] == pytest.approx(expected_total, abs=1e-9)
    assert p["final_position"] == eng.risk_engine.position(eng.iid)
    assert tiny_run.report["counts"]["n_parent_orders"] > 0
    assert tiny_run.report["counts"]["n_fills"] > 0


def test_fills_are_bounded_and_inside_their_parent_window(tiny_run: RunResult) -> None:
    traces = _traces(tiny_run)
    n_children = 0
    for tr in traces:
        st = tr.stages
        for po in st.parent_orders:
            children = [c for c in st.child_orders if c.parent_order_id == po.parent_order_id]
            submitted = {c.child_order_id: c for c in children
                         if any(r.order_id == c.child_order_id and r.status is ExecStatus.NEW
                                for r in st.fills)}
            n_children += len(submitted)
            for cid, child in submitted.items():
                filled = sum(r.filled_qty for r in st.fills if r.order_id == cid)
                assert 0 <= filled <= child.qty
                for r in st.fills:
                    if r.order_id == cid and r.filled_qty:
                        assert po.arrival_ts <= r.exchange_ts <= po.end_ts
                        assert r.receive_ts >= r.exchange_ts
            # every report belongs to a submitted child of this parent
            for r in st.fills:
                assert r.order_id in submitted
            filled_total = sum(r.filled_qty for r in st.fills)
            assert filled_total <= po.qty
            for t in st.tca:
                assert t.filled_qty == filled_total and t.qty == po.qty
    assert n_children == tiny_run.report["counts"]["n_child_orders_submitted"]


def test_every_trace_validates_and_digest_matches_file(tiny_run: RunResult) -> None:
    traces = _traces(tiny_run)
    assert len(traces) == tiny_run.report["counts"]["n_decisions"]
    ids = [t.trace_id for t in traces]
    assert len(set(ids)) == len(ids)
    for tr in traces:
        validate_typed(tr)
        assert tr.session_id == tiny_run.config.session_id
        assert tr.data_version == tiny_run.feed.data_version
    assert TraceDigest.of_jsonl(tiny_run.out_dir / TRACES_FILE).hexdigest() == tiny_run.trace_digest
    assert tiny_run.report["trace"]["digest"] == tiny_run.trace_digest
    # decision order == event-time order
    keys = [(t.event_ts, t.sequence) for t in traces]
    assert keys == sorted(keys)


def test_store_row_counts_equal_trace_stage_counts(tiny_run: RunResult) -> None:
    traces = _traces(tiny_run)
    expected = {
        "decision_traces": len(traces),
        "alpha_signals": sum(len(t.stages.signal) for t in traces),
        "portfolio_targets": sum(1 for t in traces if t.stages.portfolio is not None),
        "portfolio_legs": sum(len(t.stages.portfolio.targets) for t in traces
                              if t.stages.portfolio is not None),
        "risk_decisions": sum(len(t.stages.risk) for t in traces),
        "parent_orders": sum(len(t.stages.parent_orders) for t in traces),
        "child_orders": sum(len(t.stages.child_orders) for t in traces),
        "venue_decisions": sum(len(t.stages.routing) for t in traces),
        "executions": sum(len(t.stages.fills) for t in traces),
        "tca_results": sum(len(t.stages.tca) for t in traces),
        "attribution": sum(1 for t in traces if t.stages.attribution is not None),
        "instruments": 1, "venues": 3, "alphas": 3, "sessions": 1, "feature_versions": 1,
    }
    with Store.open(tiny_run.out_dir / STORE_FILE) as store:
        counts = store.counts()
        assert {k: counts[k] for k in expected} == expected
        first = next(t for t in traces if t.stages.parent_orders)
        pid = first.stages.parent_orders[0].parent_order_id
        text = store.explain(pid)
        assert text.startswith(f"Order {pid}") and "XV" in text
        assert store.venue_names() == {1: "XV1", 2: "XV2", 3: "XV3"}


def test_risk_decisions_reference_children_and_rejects_never_submit(tiny_run: RunResult) -> None:
    traces = _traces(tiny_run)
    n_allow = n_reject = 0
    for tr in traces:
        st = tr.stages
        child_ids = {c.child_order_id for c in st.child_orders}
        submitted = {r.order_id for r in st.fills if r.status is ExecStatus.NEW}
        for rd in st.risk:
            assert rd.order_id in child_ids
            if rd.decision is Decision.ALLOW:
                n_allow += 1
                assert rd.rule_index == -1 and rd.order_id in submitted
            else:
                n_reject += 1
                assert rd.rule_index >= 0 and rd.order_id not in submitted
                assert not any(r.order_id == rd.order_id for r in st.fills)
        # a child without an ALLOW decision was never submitted
        allowed = {rd.order_id for rd in st.risk if rd.decision is Decision.ALLOW}
        assert submitted <= allowed
    assert n_allow == tiny_run.report["risk"]["allowed"]
    assert n_reject == tiny_run.report["risk"]["rejected"]
    assert rule_index_of(Rules.ALLOW) == -1 and rule_index_of(Rules.KILL_GLOBAL) == 1
    assert rule_index_of(Rules.STRATEGY_LOSS) == 22
    with pytest.raises(ValueError):
        rule_index_of("NOT_A_RULE")


def test_engaged_kill_switch_rejects_every_child_and_nothing_fills(
        tiny_cfg: MvpConfig, tiny_run: RunResult) -> None:
    """The wiring under a REJECT: once the global kill switch is latched,
    every generated child is checked, rejected with KILL_GLOBAL, never
    submitted and never filled, while the loop keeps tracing decisions."""
    feed = load_feed(tiny_run.out_dir)
    sink = MemoryTraceSink()
    eng = MvpEngine(tiny_cfg, feed, sink)
    events = list(feed.events)
    cut = len(events) // 2
    for ev in events[:cut]:
        eng.on_event(ev)
    eng.risk_engine.engage_kill(Scope.GLOBAL, "", events[cut - 1].exchange_ts, "test kill")
    submitted_before = eng.counters.child_orders_submitted
    fills_before = eng.counters.fills
    for ev in events[cut:]:
        eng.on_event(ev)
    eng.finish()
    assert eng.counters.child_orders_generated > 0
    assert eng.counters.risk_rejected > 0
    assert eng.counters.child_orders_submitted == submitted_before
    rejected = [rd for tr in sink.traces for rd in tr.stages.risk
                if rd.decision is not Decision.ALLOW]
    assert rejected
    assert all(rd.rule_id == Rules.KILL_GLOBAL and rd.rule_index == 1 for rd in rejected)
    for tr in sink.traces:
        if tr.event_ts >= events[cut].exchange_ts:
            assert not any(r.status is ExecStatus.NEW for r in tr.stages.fills)
    # fills after the cut can only come from children submitted before it
    assert eng.counters.fills >= fills_before
    assert eng.risk_engine.open_order_count() == 0


def test_paper_evidence_and_report_are_consistent(tiny_run: RunResult) -> None:
    ev = json.loads((tiny_run.out_dir / PAPER_EVIDENCE_FILE).read_text())
    assert ev["x-version"] == 2 and set(ev["alphas"]) == {"EQ01", "EQ03", "EQ06"}
    for aid, row in ev["alphas"].items():
        assert row["state_at_run"] == "CANDIDATE"
        paper = row["paper"]
        assert paper["n_sessions"] == 1 and paper["net_pnl"] == tiny_run.report["pnl"]["total"]
        assert paper["n_kill_events"] == tiny_run.report["risk"]["kill_events"]
        assert row["research_ic_defined"] and paper["research_ic"] == \
            tiny_run.report["alpha"]["per_alpha"][aid]["research_ic"]
    report = json.loads((tiny_run.out_dir / REPORT_JSON).read_text())
    assert compare_runs(report, tiny_run.report) == []
    assert report["run"]["run_id"] == tiny_run.config.run_id
    assert (tiny_run.out_dir / "report.md").read_text().startswith("# MVP run")


def test_run_twice_is_bit_identical(tiny_cfg: MvpConfig, tiny_run: RunResult,
                                    tmp_path: Path) -> None:
    feed = generate_feed(tiny_cfg, tmp_path / "second")
    assert feed.events_sha256 == tiny_run.feed.events_sha256
    assert feed.data_version == tiny_run.feed.data_version
    second = run_session(tiny_cfg, feed, tmp_path / "second")
    assert second.trace_digest == tiny_run.trace_digest
    assert compare_runs(tiny_run.report, second.report) == []
    assert (tmp_path / "second" / TRACES_FILE).read_bytes() == \
        (tiny_run.out_dir / TRACES_FILE).read_bytes()
    assert (tmp_path / "second" / REPORT_JSON).read_bytes() == \
        (tiny_run.out_dir / REPORT_JSON).read_bytes()


def test_a_different_seed_changes_the_stream(tiny_cfg: MvpConfig, tiny_run: RunResult,
                                             tmp_path: Path) -> None:
    other = tiny_cfg.with_overrides(seed=tiny_cfg.seed + 1)
    feed = generate_feed(other, tmp_path)
    assert feed.data_version != tiny_run.feed.data_version
    assert feed.events_sha256 != tiny_run.feed.events_sha256


# ------------------------------------------------------------- IC audit


def _research_frame(cfg: MvpConfig, run: RunResult) -> Any:
    """The research feature + label frame of the captured stream, built the
    way ``iap.features.__main__`` / ``iap.alpha.goldenframes`` build it
    (a fresh FeatureEngine at the decision cadence, one mid sample per book
    refresh, ``compute_labels`` at the emission timestamps)."""
    import numpy as np
    import pandas as pd
    from iap.labels.labels import HORIZON_ORDER, MidSeries, compute_labels, max_sample_age
    from iap.mvp.engine import feature_context
    from iap.mvp.feed import build_reference_data

    ref = build_reference_data(cfg)
    iid = ref.instrument(cfg.instrument).instrument_id
    eng = FeatureEngine({iid: feature_context(cfg, ref)}, cadence_ns=cfg.decision_cadence_ns)
    series = MidSeries()
    last_seq = 0
    ts_list: List[int] = []
    rows = []
    last_event_ts = 0
    for ev in run.feed.events:
        vec = eng.apply(ev)
        last_event_ts = ev.exchange_ts
        st = eng.states[iid]
        if st.refresh_seq != last_seq:
            last_seq = st.refresh_seq
            if st.label_tradable:
                series.append(ev.exchange_ts, st.mid, st.spread_ticks * st.tick / 2.0, True)
            else:
                series.append(ev.exchange_ts, float("nan"), float("nan"), False)
        if vec is not None:
            vals = np.asarray(vec.values, dtype=float).copy()
            vals[~np.asarray(vec.validity, dtype=bool)] = np.nan
            ts_list.append(vec.timestamp)
            rows.append(vals)
    frame = pd.DataFrame(np.vstack(rows), columns=list(eng.feature_names))
    frame.insert(0, "exchange_ts", np.asarray(ts_list, dtype=np.int64))
    labels = compute_labels(ts_list, series, last_event_ts, max_age_ns=max_sample_age(series))
    for h in HORIZON_ORDER:
        frame[f"label_mid_{h}"] = np.asarray(labels[h].mid, dtype=float)
        frame[f"label_cost_{h}"] = np.asarray(labels[h].cost, dtype=float)
        frame[f"label_valid_{h}"] = np.asarray(labels[h].valid, dtype=bool)
    return frame


def test_realized_ic_is_pinned_to_the_research_label_definition(
        tiny_cfg: MvpConfig, tiny_run: RunResult) -> None:
    """The MVP's per-decision realized returns ARE the research labels
    (``iap.labels.compute_labels`` on an independently built frame): same
    anchors, same valid set, same mid / cost label to 1e-9, same IC as
    ``iap.validation.metrics.ic`` on the research scoring path (raw signal
    -> z -> expected_return), at the MVP horizon and at each alpha's fitted
    horizon."""
    import math

    import numpy as np
    from iap.validation.metrics import ic as research_ic

    engine = tiny_run.engine
    frame = _research_frame(tiny_cfg, tiny_run)
    assert frame["exchange_ts"].tolist() == engine.decision_ts
    for alpha in engine.alphas:
        m = alpha.model
        raw = m.raw_signal(frame).to_numpy(dtype=float)
        ok = np.isfinite(raw)
        z = np.zeros(len(raw))
        z[ok] = np.clip((raw[ok] - m.mu) / (m.sigma + 1e-12), -m.z_clip, m.z_clip)
        er = m.beta * z
        conf = np.minimum(1.0, np.abs(z) / m.conf_scale)
        conf[~ok] = 0.0
        # the streaming signals are the research scoring path, row for row
        streamed = {i: e for i, e in engine.ic_samples[alpha.alpha_id]}
        assert sorted(streamed) == [i for i in range(len(er)) if conf[i] > 0.0]
        for i, e in streamed.items():
            assert e == pytest.approx(er[i], abs=1e-12)
        for h in {engine.horizon, m.horizon}:
            pairs = engine.realized_pairs(alpha.alpha_id, h)
            valid = frame[f"label_valid_{h}"].to_numpy(dtype=bool)
            assert [i for i, _, _, _ in pairs] == \
                [i for i in range(len(er)) if conf[i] > 0.0 and valid[i]]
            for i, _, mid, cost in pairs:
                assert mid == pytest.approx(frame[f"label_mid_{h}"].iloc[i], abs=1e-9)
                assert cost == pytest.approx(frame[f"label_cost_{h}"].iloc[i], abs=1e-9)
            got = engine.realized_ic(alpha.alpha_id, h)
            lab = frame[f"label_mid_{h}"].to_numpy(dtype=float).copy()
            lab[~valid] = np.nan
            er_conf = er.copy()
            er_conf[conf <= 0.0] = np.nan
            want = research_ic(er_conf, lab, min_obs=3)
            assert got.n == len(pairs)
            if math.isnan(want):
                assert got.ic is None
            else:
                assert got.ic == pytest.approx(want, abs=1e-12)
    # the report carries exactly these numbers
    for aid, row in tiny_run.report["alpha"]["per_alpha"].items():
        assert row["realized_ic"] == engine.realized_ic(aid).ic
        assert row["at_research_horizon"]["realized_ic"] == \
            engine.realized_ic(aid, row["research_horizon"]).ic


def test_realized_returns_agree_with_the_tca_timeline(tiny_run: RunResult) -> None:
    """Where both are defined, the label's mid-to-mid return equals the
    return read off the TCA timeline (the simulator's consolidated book):
    the label series and the execution path see the same market."""
    engine = tiny_run.engine
    tl = engine.timeline
    h_ns = tiny_run.config.horizon_ns
    checked = 0
    for aid in list(tiny_run.config.alphas) + [engine.ensemble.alpha_id]:
        for index, _, mid, _ in engine.realized_pairs(aid, engine.horizon):
            t = engine.decision_ts[index]
            if not tl.mid_defined_at(t + h_ns, after_ts=t) or tl.prevailing(t) is None:
                continue
            assert tl.mid_at(t + h_ns) / tl.mid_at(t) - 1.0 == pytest.approx(mid, abs=1e-9)
            checked += 1
    assert checked > 0


def test_shift_by_one_and_truncation_leakage_probes(tiny_cfg: MvpConfig,
                                                    tiny_run: RunResult) -> None:
    """Two leakage probes (conventions §7, ``iap.validation.leakage``):

    * shift-by-one — the report carries ``realized_ic_shifted`` (the signal
      of the previous confidence > 0 decision against this decision's
      label) for every alpha and the ensemble, and it is computed from the
      same pairs;
    * truncation — the engine is a single-pass stream: re-running it on the
      stream cut at several points reproduces every signal decided before
      the cut bit for bit (a scoring path that peeked at later events
      could not)."""
    engine = tiny_run.engine
    for aid in list(tiny_cfg.alphas) + [engine.ensemble.alpha_id]:
        res = engine.realized_ic(aid)
        pairs = engine.realized_pairs(aid, engine.horizon)
        samples = engine.ic_samples[aid]
        pos = {i: k for k, (i, _) in enumerate(samples)}
        xs = [samples[pos[i] - 1][1] for i, _, _, _ in pairs if pos[i] > 0]
        ys = [m for i, _, m, _ in pairs if pos[i] > 0]
        assert res.ic_shifted == pearson(xs, ys)
    block = tiny_run.report["alpha"]
    assert block["ensemble"]["realized_ic_shifted"] == \
        engine.realized_ic(engine.ensemble.alpha_id).ic_shifted

    full = _traces(tiny_run)
    events = tiny_run.feed.events
    for frac in (0.35, 0.7):
        cut = int(len(events) * frac)
        cut_ts = events[cut - 1].exchange_ts
        sink = MemoryTraceSink()
        part = MvpEngine(tiny_cfg, tiny_run.feed, sink)
        for ev in events[:cut]:
            part.on_event(ev)
        part.finish()
        before = [t for t in full if t.event_ts <= cut_ts]
        assert len(sink.traces) >= len(before) > 0
        for want, got in zip(before, sink.traces[:len(before)]):
            assert got.trace_id == want.trace_id
            assert got.stages.signal == want.stages.signal
            assert got.stages.portfolio == want.stages.portfolio


def test_signal_stage_is_ensemble_first_then_components(tiny_run: RunResult) -> None:
    """``signal[0]`` is the acting (ensemble) signal — what ``v_order_chain``
    and ``explain`` attribute to the order — followed by the members in
    ensemble order, each labelled by its alpha id."""
    engine = tiny_run.engine
    members = [a.alpha_id for a in engine.ensemble.members]
    for trace in _traces(tiny_run):
        labels = [s.model_version for s in trace.stages.signal]
        assert labels == [engine.ensemble.alpha_id] + members
        for po in trace.stages.parent_orders:
            assert po.alpha_id == trace.stages.signal[0].model_version
    with Store.open(tiny_run.out_dir / STORE_FILE) as store:
        rows = store.query("SELECT parent_order_id, signal_expected_return, "
                           "signal_model_version FROM v_order_chain ORDER BY parent_order_id")
        assert rows and all(r["signal_model_version"] == engine.ensemble.alpha_id
                            for r in rows)
        text = store.explain(rows[0]["parent_order_id"])
    lines = text.splitlines()
    assert lines[1].startswith(f"Alpha:      {engine.ensemble.alpha_id}  ")
    assert [ln.split()[1] for ln in lines[2:2 + len(members)]] == members


# ------------------------------------------------------------- helpers


def test_helpers() -> None:
    assert round_half_away(2.5) == 3 and round_half_away(-2.5) == -3 and round_half_away(0.4) == 0
    assert pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)
    assert pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert pearson([1.0, 2.0], [1.0, 2.0]) is None
    assert Side.BID.value == 0
    assert isinstance(SingleStockPortfolio("MVP", "x", "a" * 64, "b" * 64), PortfolioConstructor)
