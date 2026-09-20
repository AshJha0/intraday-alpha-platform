"""``iap.store``: DDL, Store round-trips, trace decomposition, importers, CLI.

The importer tests run on the real research artefacts of the repository.
Other components append to those artefacts (the experiments ledger, the
lifecycle transitions), so counts are asserted exactly against the files
and as lower bounds against the pinned values of the Phase 0 snapshot
(65 ledger entries, 24 alphas, 33 model runs, 260 lifecycle-log rows).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from iap.contracts.examples import VENUE_NAMES, all_examples, example_trace
from iap.contracts.types import (
    Attribution,
    ChildOrder,
    ExecutionReport,
    ExperimentResult,
    ExperimentSpec,
    LatencyStats,
    LifecycleTransition,
    ParentOrder,
    PortfolioLeg,
    RiskDecision,
    TCAResult,
    VenueDecision,
)
from iap.contracts.versions import content_hash
from iap.research import LOOKS_PER_EXPERIMENT
from iap.store import (
    DDL_X_VERSION,
    STAGE_TABLES,
    Store,
    ddl_path,
    import_all,
    import_alpha_reports,
    import_baselines,
    import_experiment_documents,
    import_experiments_ledger,
    import_lifecycle_log,
    import_lifecycle_transitions,
    import_model_runs,
    import_reference,
    import_tca_orders,
    load_ddl,
    split_statements,
)
from iap.store.__main__ import main as store_main
from iap.store.ddl import apply, table_names, view_names

REPO = Path(__file__).resolve().parents[2]
CONFIGS = REPO / "configs"
RESEARCH = REPO / "research"
GOLDEN = json.loads((REPO / "tests" / "golden" / "expected_contracts_examples.json").read_text())

EXPECTED_TABLES = {
    "schema_version", "instruments", "venues", "sessions", "feature_versions", "alphas",
    "experiments", "experiment_results", "ledger_entries", "lifecycle_transitions",
    "decision_traces", "alpha_signals", "portfolio_targets", "portfolio_legs",
    "risk_decisions", "parent_orders", "child_orders", "venue_decisions", "executions",
    "tca_results", "attribution", "tca_orders", "model_runs", "drift_baselines",
}
EXPECTED_VIEWS = {"v_order_chain", "v_alpha_scorecard", "v_experiment_ledger_summary"}

#: Stage tuple lengths of the example trace -> rows in the decomposition.
EXAMPLE_DECOMPOSITION = {
    "decision_traces": 1, "alpha_signals": 1, "portfolio_targets": 1, "portfolio_legs": 1,
    "risk_decisions": 1, "parent_orders": 1, "child_orders": 3, "venue_decisions": 3,
    "executions": 3, "tca_results": 1, "attribution": 1,
}

#: Pinned snapshot minima (other agents append to the artefacts).
PINNED_MIN = {"ledger_entries": 65, "alphas": 24, "model_runs": 33,
              "lifecycle_transitions": 260, "instruments": 19, "venues": 5,
              "tca_orders": 36, "drift_baselines": 36, "experiment_results": 24}


@pytest.fixture()
def store() -> Store:
    with Store.open(":memory:") as s:
        s.init()
        yield s


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Store:
    """One store built from the real artefacts (shared by the importer tests)."""
    db = tmp_path_factory.mktemp("store") / "iap.sqlite"
    s = Store.open(db)
    s.init()
    s.reports = import_all(s, REPO)  # type: ignore[attr-defined]
    yield s
    s.close()


# --------------------------------------------------------------------------
# DDL
# --------------------------------------------------------------------------

def test_ddl_applies_on_fresh_sqlite_and_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    n1 = apply(conn)
    n2 = apply(conn)
    assert n1 == n2 > 30
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert EXPECTED_TABLES <= names
    assert views == EXPECTED_VIEWS
    assert conn.execute("SELECT x_version FROM schema_version").fetchall() == [(DDL_X_VERSION,)]


def test_ddl_names_match_expectations() -> None:
    sql = load_ddl()
    assert set(table_names(sql)) == EXPECTED_TABLES
    assert set(view_names(sql)) == EXPECTED_VIEWS
    assert ddl_path().name == "iap_v1.sql"


_ALLOWED_TYPES = {"BIGINT", "TEXT", "DOUBLE PRECISION"}
_ALLOWED_STARTS = ("CREATE TABLE IF NOT EXISTS ", "CREATE INDEX IF NOT EXISTS ",
                   "DROP VIEW IF EXISTS ", "CREATE VIEW ", "INSERT INTO schema_version ")
_FORBIDDEN = (
    "AUTOINCREMENT", "SERIAL", "IDENTITY", "BOOLEAN", "INTEGER", "VARCHAR", "DATETIME",
    "NOW(", "CURRENT_TIMESTAMP", "CURRENT_DATE", "INSERT OR REPLACE", "ON CONFLICT", "::", "ILIKE",
    "LATERAL", "DISTINCT ON", "CREATE OR REPLACE", "CREATE VIEW IF", "PRAGMA",
    "WITHOUT ROWID", "IIF(", "`", '"', "STRFTIME", "NULLS LAST", "NULLS FIRST",
    "UNSIGNED", "TINYINT", "SMALLINT", "REAL", "FLOAT", "NUMERIC", "DECIMAL", "JSON",
)
_COLUMN = re.compile(r"^\s*(\w+)\s+(BIGINT|TEXT|DOUBLE PRECISION)\b")


def test_ddl_is_portable_by_whitelist() -> None:
    """No PostgreSQL to run against: every statement form, every column
    type and every keyword is checked against the documented portable
    subset (docs/DATA_MODEL.md section 5)."""
    sql = load_ddl()
    stmts = split_statements(sql)
    assert stmts and len(stmts) == len(set(stmts))
    for stmt in stmts:
        assert stmt.startswith(_ALLOWED_STARTS), stmt[:60]
        upper = stmt.upper()
        for token in _FORBIDDEN:
            if token[0].isalpha() and token[-1].isalpha():
                hit = re.search(r"\b" + re.escape(token) + r"\b", upper) is not None
            else:
                hit = token in upper
            assert not hit, f"{token!r} in {stmt[:60]}"
        assert "--" not in stmt  # comments stripped by the splitter
        if stmt.startswith("CREATE TABLE"):
            body = stmt[stmt.index("(") + 1:stmt.rindex(")")]
            for line in body.split("\n"):
                text = line.strip().rstrip(",")
                if not text or text.startswith(("PRIMARY KEY", "CHECK", "FOREIGN KEY", "UNIQUE")):
                    continue
                if text[0] in "('":
                    continue  # continuation of a multi-line CHECK list
                m = _COLUMN.match(text)
                assert m, f"column line outside the portable grammar: {text!r}"
                assert m.group(2) in _ALLOWED_TYPES
    assert "-- " in sql  # the file is commented; comments never reach a statement


def test_split_statements_handles_quotes_and_comments() -> None:
    text = "CREATE TABLE t (a TEXT); -- c; d\nINSERT INTO t VALUES ('x;y');\n"
    assert split_statements(text) == ("CREATE TABLE t (a TEXT)", "INSERT INTO t VALUES ('x;y')")
    with pytest.raises(ValueError):
        split_statements("SELECT 1")


# --------------------------------------------------------------------------
# Store: round trips
# --------------------------------------------------------------------------

def test_init_checks_schema_version(store: Store) -> None:
    assert store.tables() == tuple(sorted(EXPECTED_TABLES))
    assert store.views() == tuple(sorted(EXPECTED_VIEWS))
    assert store.primary_key("lifecycle_transitions") == (
        "alpha_id", "policy", "event_ts", "from_state", "to_state", "source")
    with pytest.raises(KeyError):
        store.columns("no_such_table")


def test_every_storable_example_round_trips(store: Store) -> None:
    trace = example_trace()
    store.insert_trace(trace)
    tid = trace.trace_id
    ex = all_examples()
    store.insert_alpha_signal(ex["AlphaSignal"], trace_id=tid)
    store.insert_portfolio_target(ex["PortfolioTarget"], trace_id=tid)
    store.insert_risk_decision(ex["RiskDecision"], trace_id=tid)
    store.insert_parent_order(ex["ParentOrder"], trace_id=tid)
    store.insert_child_order(ex["ChildOrder"], trace_id=tid)
    store.insert_venue_decision(ex["VenueDecision"], trace_id=tid)
    store.insert_execution_report(ex["ExecutionReport"], trace_id=tid)
    store.insert_tca_result(ex["TCAResult"], trace_id=tid)
    store.insert_attribution(ex["Attribution"], trace_id=tid, parent_order_id=12345)
    store.insert_experiment_spec(ex["ExperimentSpec"])
    store.insert_experiment_result(ex["ExperimentResult"])
    store.insert_lifecycle_transition(ex["LifecycleTransition"])

    assert store.fetch(type(ex["AlphaSignal"]), trace_id=tid) == (ex["AlphaSignal"],)
    assert store.fetch(type(ex["PortfolioTarget"]), trace_id=tid) == (ex["PortfolioTarget"],)
    assert store.fetch(RiskDecision, trace_id=tid) == (ex["RiskDecision"],)
    assert store.fetch(ParentOrder, parent_order_id=12345) == (ex["ParentOrder"],)
    assert store.fetch(ChildOrder, child_order_id=ex["ChildOrder"].child_order_id) == (ex["ChildOrder"],)
    assert store.fetch(VenueDecision, child_order_id=ex["VenueDecision"].child_order_id) == (ex["VenueDecision"],)
    assert store.fetch(ExecutionReport, execution_id=ex["ExecutionReport"].execution_id) == (ex["ExecutionReport"],)
    assert store.fetch(TCAResult, parent_order_id=12345) == (ex["TCAResult"],)
    assert store.fetch(Attribution, trace_id=tid) == (ex["Attribution"],)
    assert store.fetch(ExperimentSpec, experiment_id=ex["ExperimentSpec"].experiment_id) == (ex["ExperimentSpec"],)
    assert store.fetch(ExperimentResult, alpha_id="EQ03") == (ex["ExperimentResult"],)
    assert store.fetch(LifecycleTransition, alpha_id="EQ03", source="api") == (ex["LifecycleTransition"],)
    assert store.get_trace(tid) == trace
    assert store.fetch(type(trace), trace_id=tid) == (trace,)
    # the example rows re-inserted standalone are the trace's own rows: no growth
    assert {t: store.counts()[t] for t in EXAMPLE_DECOMPOSITION} == EXAMPLE_DECOMPOSITION
    # execution parent link resolved through the trace's child orders
    row = store.query("SELECT parent_order_id FROM executions ORDER BY execution_id")[0]
    assert row["parent_order_id"] == 12345


def test_nested_types_have_no_table(store: Store) -> None:
    for cls in (PortfolioLeg, LatencyStats):
        with pytest.raises(TypeError):
            store.fetch(cls)


def test_typed_insert_validates_and_rolls_back(store: Store) -> None:
    trace = example_trace()
    store.insert_trace(trace)
    order = example_trace().stages.parent_orders[0]
    with pytest.raises(sqlite3.IntegrityError):  # unknown trace: FK enforced
        store.insert_parent_order(order, trace_id="0" * 32)
    assert store.counts()["parent_orders"] == 1
    with pytest.raises(ValueError):
        store.upsert("alphas", {"alpha_id": "X", "nope": 1})
    with pytest.raises(ValueError):
        store.insert_alpha("X", asset_class="EQUITY", family="f", horizon="1s",
                           economic_rationale="", current_state="LIVE")
    with pytest.raises(KeyError):
        store.get_trace("f" * 32)
    with pytest.raises(KeyError):
        store.explain(1)


# --------------------------------------------------------------------------
# Trace decomposition + explain
# --------------------------------------------------------------------------

def _name_example_venue_3(store: Store) -> None:
    """``configs/venues/venues.json`` names XV1/XV2 only; the pinned example
    routes to a third equity venue (``VENUE_NAMES[3] == "XV3"``), so the
    golden rendering needs that row in the venues table."""
    store.upsert("venues", {
        "venue_id": 3, "venue": VENUE_NAMES[3], "asset_class": "EQUITY",
        "taker_fee_per_share": 0.0025, "maker_rebate_per_share": 0.001,
        "commission_per_million": None, "latency_mean_ns": 300_000,
        "latency_jitter_ns": 100_000, "supports_json": "[\"MBO\"]"})


def test_insert_trace_decomposes_and_explain_matches_golden(store: Store) -> None:
    import_reference(store, CONFIGS)
    _name_example_venue_3(store)
    trace = example_trace()
    assert store.insert_trace(trace) == trace.trace_id
    counts = store.counts()
    for table, n in EXAMPLE_DECOMPOSITION.items():
        assert counts[table] == n, table
    assert store.venue_names()[1] == "XV1"
    assert store.explain(12345) == GOLDEN["explain"]["text"]
    assert {k: v for k, v in store.venue_names().items() if k in VENUE_NAMES} == dict(VENUE_NAMES)
    # an explicit venue_names mapping overrides the table
    assert "XV3" not in store.explain(12345, venue_names={3: "DARK"})
    # re-insert: identical counts (delete-then-insert, no stale rows)
    store.insert_trace(trace)
    assert store.counts() == counts
    # a smaller trace with the same id replaces every stage row
    smaller = trace.__class__.from_dict({**trace.to_dict(), "stages": {
        **trace.stages.to_dict(), "child_orders": [], "routing": [], "fills": [],
        "tca": [], "attribution": None}})
    store.insert_trace(smaller)
    after = store.counts()
    assert after["child_orders"] == after["venue_decisions"] == after["executions"] == 0
    assert after["tca_results"] == after["attribution"] == 0
    assert store.get_trace(trace.trace_id) == smaller
    assert all(t in after for t in STAGE_TABLES)
    assert store.explain(12345).splitlines()[5:] == [
        "SOR:        (none)", "Fills:      0 / 20,000 (0.0%)", "TCA:        (none)",
        "Attribution: (none)"]


def test_v_order_chain_returns_the_example_chain(store: Store) -> None:
    store.insert_trace(example_trace())
    rows = store.query("SELECT * FROM v_order_chain ORDER BY parent_order_id")
    assert len(rows) == 1
    row = rows[0]
    assert row["parent_order_id"] == 12345 and row["alpha_id"] == "EQ03"
    assert row["signal_expected_return"] == pytest.approx(4.2e-4)
    assert row["portfolio_target_qty"] == 20_000 and row["portfolio_solver_status"] == "OPTIMAL"
    assert row["risk_decision"] == 1 and row["risk_rule_id"] == ""
    assert row["n_child_orders"] == 3 and row["n_venues_routed"] == 3
    assert row["n_fills"] == 3 and row["filled_qty"] == 18_000
    assert row["fees"] == pytest.approx(0.003 * 18_000)
    assert row["implementation_shortfall_bps"] == 2.1 and row["tca_fill_rate"] == 0.9
    assert row["attribution_total_bps"] == 2.7 and row["attribution_alpha_bps"] == 6.2


def test_v_order_chain_aggregates_per_child_risk_and_counts_only_fills(store: Store) -> None:
    """Risk is decided per routed child (conventions section 11.4): the view
    aggregates the children's decisions (REJECT/KILL if any child was
    rejected/killed, else ALLOW), exposes the allowed/rejected counts and the
    first rejecting rule, and ``n_fills`` counts PARTIAL/FILLED reports only
    (review finding 2: the old view joined risk on the parent id and counted
    NEW/CANCELED reports as fills)."""
    from dataclasses import replace

    from iap.contracts.types import Decision, ExecStatus, TraceStages

    base = example_trace()
    parent = base.stages.parent_orders[0]
    # Parent id 1 and child id 1 share the id space, as in the MVP.
    parent = replace(parent, parent_order_id=1)
    c1, c2 = base.stages.child_orders[:2]
    c1 = replace(c1, child_order_id=1, parent_order_id=1)
    c2 = replace(c2, child_order_id=2, parent_order_id=1)
    r1, r2 = base.stages.routing[:2]
    r1 = replace(r1, child_order_id=1)
    r2 = replace(r2, child_order_id=2)
    t = parent.decision_ts
    risk = (
        RiskDecision(order_id=1, strategy_id=parent.strategy_id, instrument_id=parent.instrument_id,
                     timestamp_ns=t + 1, decision=Decision.ALLOW, rule_id="", rule_index=-1,
                     reason="all checks passed"),
        RiskDecision(order_id=2, strategy_id=parent.strategy_id, instrument_id=parent.instrument_id,
                     timestamp_ns=t + 2, decision=Decision.REJECT, rule_id="RATE_THROTTLE",
                     rule_index=9, reason="throttled"),
    )
    f = base.stages.fills[0]
    fills = (
        replace(f, order_id=1, execution_id=1, status=ExecStatus.NEW, filled_qty=0,
                fill_price_ticks=0, fees=0.0),
        replace(f, order_id=1, execution_id=2, status=ExecStatus.PARTIAL, filled_qty=4_000,
                fees=0.003 * 4_000),
        replace(f, order_id=1, execution_id=3, status=ExecStatus.FILLED, filled_qty=5_000,
                fees=0.003 * 5_000),
        replace(f, order_id=1, execution_id=4, status=ExecStatus.CANCELED, filled_qty=0,
                fill_price_ticks=0, fees=0.0),
    )
    tca = replace(base.stages.tca[0], parent_order_id=1, filled_qty=9_000, fill_rate=0.45,
                  n_fills=2)
    trace = replace(base, stages=TraceStages(
        signal=base.stages.signal, portfolio=base.stages.portfolio, risk=risk,
        parent_orders=(parent,), child_orders=(c1, c2), routing=(r1, r2), fills=fills,
        tca=(tca,), attribution=base.stages.attribution))
    store.insert_trace(trace)
    rows = store.query("SELECT * FROM v_order_chain ORDER BY parent_order_id")
    assert len(rows) == 1
    row = rows[0]
    assert row["parent_order_id"] == 1
    assert row["risk_decision"] == 2                      # one child REJECTed
    assert row["risk_rule_id"] == "RATE_THROTTLE" and row["risk_reason"] == "throttled"
    assert row["n_children_allowed"] == 1 and row["n_children_rejected"] == 1
    assert row["n_child_orders"] == 2 and row["n_venues_routed"] == 2
    assert row["n_fills"] == 2                            # PARTIAL + FILLED only
    assert row["filled_qty"] == 9_000
    assert row["fees"] == pytest.approx(0.003 * 9_000)
    # A parent whose every child was allowed reads ALLOW with an empty rule.
    allowed = replace(trace, stages=replace(trace.stages, risk=(
        risk[0], replace(risk[1], decision=Decision.ALLOW, rule_id="", rule_index=-1,
                         reason="all checks passed"))))
    store.insert_trace(allowed)
    row = store.query("SELECT * FROM v_order_chain")[0]
    assert row["risk_decision"] == 1 and row["risk_rule_id"] == ""
    assert row["risk_reason"] == "all checks passed"
    assert row["n_children_allowed"] == 2 and row["n_children_rejected"] == 0
    # No risk stage at all: NULLs, zero counts — nothing is fabricated.
    silent = replace(trace, stages=replace(trace.stages, risk=()))
    store.insert_trace(silent)
    row = store.query("SELECT * FROM v_order_chain")[0]
    assert row["risk_decision"] is None and row["risk_rule_id"] is None
    assert row["n_children_allowed"] == 0 and row["n_children_rejected"] == 0


def test_v_order_chain_on_the_mvp_golden_run(tmp_path: Path) -> None:
    """The bundled MVP run's traces (risk per child) read correctly through
    the view: no parent shows NULL risk when its children were checked, the
    counts match the trace file, and fills exclude NEW/CANCELED reports."""
    import subprocess
    import sys

    from iap.contracts.types import DecisionTrace, ExecStatus

    root = Path(__file__).resolve().parents[2]
    run = tmp_path / "run"
    proc = subprocess.run(
        [sys.executable, "-m", "iap.mvp", "run", "--repo-root", str(root),
         "--config", str(root / "configs" / "mvp" / "mvp_tiny.json"), "--out", str(run)],
        cwd=str(root / "python"), capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(root / "python" / "src")})
    assert proc.returncode == 0, proc.stderr[-2000:]
    traces = [DecisionTrace.from_dict(json.loads(line))
              for line in (run / "traces.jsonl").read_text().splitlines()]
    with_parent = [tr for tr in traces if tr.stages.parent_orders]
    assert with_parent, "the tiny run issued no parent order"
    with sqlite3.connect(run / "iap.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        rows = {r["parent_order_id"]: dict(r)
                for r in conn.execute("SELECT * FROM v_order_chain")}
    assert len(rows) == len(with_parent)
    for tr in with_parent:
        po = tr.stages.parent_orders[0]
        row = rows[po.parent_order_id]
        risk = tr.stages.risk
        child_ids = {c.child_order_id for c in tr.stages.child_orders}
        assert all(r.order_id in child_ids for r in risk)   # per-child decisions
        if risk:
            assert row["risk_decision"] == max(r.decision.value for r in risk)
            assert row["n_children_allowed"] == sum(r.decision.value == 1 for r in risk)
            assert row["n_children_rejected"] == sum(r.decision.value != 1 for r in risk)
        else:
            assert row["risk_decision"] is None
        assert row["n_child_orders"] == len(tr.stages.child_orders)
        assert row["n_fills"] == sum(
            f.status in (ExecStatus.PARTIAL, ExecStatus.FILLED) for f in tr.stages.fills)
        assert row["filled_qty"] == sum(f.filled_qty for f in tr.stages.fills)


def test_export_jsonl_is_byte_deterministic(store: Store, tmp_path: Path) -> None:
    store.insert_trace(example_trace())
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    assert store.export_jsonl("child_orders", a) == 3
    assert store.export_jsonl("child_orders", b) == 3
    assert a.read_bytes() == b.read_bytes()
    lines = a.read_text().splitlines()
    assert [json.loads(line)["child_order_id"] for line in lines] == [1234501, 1234502, 1234503]
    assert all(line == json.dumps(json.loads(line), sort_keys=True, separators=(",", ":"))
               for line in lines)
    # a second, independently built store exports the same bytes
    with Store.open(":memory:") as other:
        other.init()
        other.insert_trace(example_trace())
        c = tmp_path / "c.jsonl"
        other.export_jsonl("child_orders", c)
        assert c.read_bytes() == a.read_bytes()
        other.export_jsonl("v_order_chain", c)
        store.export_jsonl("v_order_chain", b)
        assert c.read_bytes() == b.read_bytes()


# --------------------------------------------------------------------------
# Importers on the real artefacts
# --------------------------------------------------------------------------

def _n_ledger_entries() -> int:
    return len(json.loads((RESEARCH / "experiments.json").read_text())["entries"])


def test_import_all_counts(built: Store) -> None:
    counts = built.counts()
    reports = built.reports  # type: ignore[attr-defined]
    for table, minimum in PINNED_MIN.items():
        assert counts[table] >= minimum, (table, counts[table])
    assert counts["alphas"] == len(list((RESEARCH / "alpha_reports").glob("*.json")))
    assert counts["ledger_entries"] == _n_ledger_entries()
    assert counts["model_runs"] == json.loads(
        (RESEARCH / "models" / "ledger.json").read_text())["experiment_count"]
    assert counts["drift_baselines"] == len(list((RESEARCH / "baselines").glob("*.json")))
    assert counts["instruments"] == 19 and counts["venues"] == 5
    assert counts["feature_versions"] == 1 and counts["schema_version"] == 1
    log_rows = sum(1 for line in (RESEARCH / "lifecycle_log.jsonl").read_text().splitlines() if line.strip())
    assert counts["lifecycle_transitions"] >= log_rows == 260
    assert counts["experiments"] == counts["experiment_results"] >= 24
    # every warning is an "absent optional artefact", never a skipped record
    for step, rep in reports.items():
        for w in rep.warnings:
            assert "absent" in w, (step, w)


def test_ledger_denominator_and_summary_view(built: Store) -> None:
    doc = json.loads((RESEARCH / "experiments.json").read_text())
    total = built.query("SELECT SUM(count) AS total, COUNT(*) AS n FROM ledger_entries")[0]
    assert total["total"] == doc["total_experiments"]
    assert total["n"] == doc["distinct_experiments"] == len(doc["entries"])
    view = built.query("SELECT * FROM v_experiment_ledger_summary ORDER BY kind")
    assert sum(r["total_count"] for r in view) == doc["total_experiments"]
    kinds = {r["kind"]: r for r in view}
    assert kinds["promotion_pipeline"]["n_alphas"] == 24
    assert kinds["promotion_pipeline"]["n_promote"] == 0


def test_alpha_scorecard_view(built: Store) -> None:
    rows = built.query("SELECT * FROM v_alpha_scorecard ORDER BY alpha_id")
    assert [r["alpha_id"] for r in rows] == sorted(r["alpha_id"] for r in rows)
    assert len(rows) == 24
    by_id = {r["alpha_id"]: r for r in rows}
    eq03 = by_id["EQ03"]
    assert eq03["family"] == "ofi_multilevel" and eq03["asset_class"] == "EQUITY"
    assert eq03["verdict"] in ("PROMOTE", "ITERATE", "REJECT")
    assert eq03["ledger_entries"] >= 1 and eq03["ledger_count"] >= 21
    assert eq03["latest_experiment_id"] is not None
    assert all(r["current_state"] in (
        "RESEARCH", "CANDIDATE", "VALIDATING", "PAPER", "ACTIVE", "WATCH", "RETIRED") for r in rows)
    rationale = built.query("SELECT economic_rationale FROM alphas WHERE alpha_id = 'EQ03'")[0]
    assert rationale["economic_rationale"].startswith("Economic rationale:")


def test_alpha_report_mapping_is_pinned(built: Store) -> None:
    """The store's alpha-report rows come from THE pinned mapping — the one
    the lifecycle registry is built from (``iap.lifecycle.bootstrap``):
    ``ic`` = ``gate_ic``, ``t_stat`` = ``nw_tstat_uncrossed``, P&L in bps of
    the 1e6 USD reference notional, ``experiment_id`` = the ledger key[:16],
    versions / commit / model hash from ``alpha_params.json``."""
    from iap.lifecycle.bootstrap import REFERENCE_NOTIONAL_USD, load_ledger_entries

    report = json.loads((RESEARCH / "alpha_reports" / "EQ03.json").read_text())
    entry = load_ledger_entries(REPO)["EQ03"]
    rows = built.query(
        "SELECT r.*, e.configuration_json, e.model_version AS spec_model_version "
        "FROM experiment_results r JOIN experiments e ON e.experiment_id = r.experiment_id "
        "WHERE r.alpha_id = 'EQ03' AND r.experiment_id = ?", (entry["key"][:16],))
    assert len(rows) == 1
    row = rows[0]
    params_doc = json.loads((REPO / "configs" / "strategies" / "alpha_params.json").read_text())
    assert row["ic"] == report["gate_ic"] and row["t_stat"] == report["nw_tstat_uncrossed"]
    assert row["hit_rate"] == report["oos_hit_rate"] and row["n_folds"] == report["n_folds_run"]
    assert row["leakage_passed"] == 1 and row["verdict"] == report["verdict"]
    x1 = report["stress"]["cost"]["x1"]
    scale = 1e4 / REFERENCE_NOTIONAL_USD
    assert row["net_return_bps"] == pytest.approx(x1["total_pnl"] * scale)
    assert row["transaction_cost_bps"] == pytest.approx(x1["total_costs"] * scale)
    assert row["gross_return_bps"] - row["transaction_cost_bps"] == pytest.approx(row["net_return_bps"])
    assert row["dataset_version"] == params_doc["data_version"]
    assert row["feature_version"] == params_doc["feature_version"]
    assert row["git_commit"] == params_doc["git_commit"]
    assert row["model_version"] == row["spec_model_version"] == content_hash(params_doc["params"]["EQ03"])
    assert row["n_experiments_in_ledger"] == entry["n"]
    cfg = json.loads(row["configuration_json"])
    assert cfg["source"] == "research/alpha_reports/EQ03.json"
    assert "1000000 USD" in cfg["pnl_basis"] and cfg["ic_source"].startswith("gate_ic")
    assert cfg["experiment_id_source"].endswith("key[:16]")
    assert "sharpe" in cfg["unrecorded"] and row["sharpe"] == 0.0
    assert row["created_ts"] == max(f["test_end"] for f in report["folds"])


def test_store_rows_equal_the_registry_evidence_for_every_alpha(built: Store) -> None:
    """Registry and store map the same report once (review finding 10): for
    all 24 alphas the store's ``experiment_results`` row is the
    ``ExperimentResult`` the registry was bootstrapped from, field for
    field, and the registry record points at that row's ``experiment_id``."""
    from iap.contracts.types import ExperimentResult
    from iap.lifecycle.bootstrap import (
        load_ledger_entries, load_params_document, load_report, research_evidence)
    from iap.lifecycle.registry import AlphaRegistry
    from iap.store.db import _dec_experiment_result

    registry = AlphaRegistry.load(RESEARCH / "alpha_registry.json")
    ledger = load_ledger_entries(REPO)
    params_doc = load_params_document(REPO)
    checked = 0
    for rec in registry.records():
        aid = rec.alpha_id
        result, missing = research_evidence(aid, load_report(REPO, aid), ledger.get(aid),
                                            params_doc)
        assert result is not None, (aid, missing)
        assert rec.experiment_id == result.experiment_id
        rows = built.query("SELECT * FROM experiment_results WHERE experiment_id = ?",
                           (result.experiment_id,))
        assert len(rows) == 1, aid
        stored: ExperimentResult = _dec_experiment_result(rows[0])
        assert stored == result, aid
        gates = rec.last_evaluation.gates
        assert gates["net_pnl_after_costs"].value == stored.net_return_bps
        assert gates["oos_ic"].value == stored.ic
        checked += 1
    assert checked == 24


def test_import_alpha_reports_skips_nan_with_warning(tmp_path: Path) -> None:
    src = json.loads((RESEARCH / "alpha_reports" / "EQ03.json").read_text())
    reports = tmp_path / "alpha_reports"
    reports.mkdir()
    (reports / "EQ03.json").write_text(json.dumps(src))
    bad = dict(src, alpha_id="EQ04", gate_ic=float("nan"))   # the IC the mapping reads
    (reports / "EQ04.json").write_text(json.dumps(bad))
    (reports / "notes.json").write_text(json.dumps({"x": 1}))
    with Store.open(":memory:") as s:
        s.init()
        rep = import_alpha_reports(s, reports, dataset_version="a" * 64,
                                   feature_version="b" * 64)
        assert rep.inserted == {"alphas": 2, "experiment_results": 1, "experiments": 1}
        assert len(rep.warnings) == 2
        assert any("EQ04.json" in w and "non-finite" in w for w in rep.warnings)
        assert any("notes.json" in w for w in rep.warnings)
        assert s.counts()["experiment_results"] == 1
        # re-run keeps state and counts
        s.set_alpha_state("EQ03", "PAPER")
        import_alpha_reports(s, reports, dataset_version="a" * 64, feature_version="b" * 64)
        assert s.query("SELECT current_state FROM alphas WHERE alpha_id='EQ03'")[0]["current_state"] == "PAPER"


def test_import_experiments_ledger(store: Store) -> None:
    rep = import_experiments_ledger(store, RESEARCH / "experiments.json")
    assert rep.inserted["ledger_entries"] == _n_ledger_entries() >= 65
    row = store.query("SELECT * FROM ledger_entries WHERE alpha_id='EQ03' "
                      "AND kind='promotion_pipeline'")[0]
    # 28, not 21: the look count now includes the time-latency grid, the
    # crossed/uncrossed split and the leakage shift IC. Asserted against the
    # constant rather than a literal so the two cannot drift apart again.
    assert row["count"] == LOOKS_PER_EXPERIMENT and row["verdict"] == "ITERATE"
    # `looks` is no longer part of the experiment identity — it is the size
    # of the recording (`count`), not what was looked at.
    config = json.loads(row["config_json"])
    assert config["horizon"] == "5s" and "looks" not in config


def test_import_lifecycle_log_gate_mapping(store: Store) -> None:
    rep = import_lifecycle_log(store, RESEARCH / "lifecycle_log.jsonl")
    assert rep.inserted["lifecycle_transitions"] == 260 and not rep.warnings
    rows = store.fetch(LifecycleTransition, alpha_id="EQ03", policy="static")
    assert rows and all(t.actor.value == "SYSTEM" for t in rows)
    first = rows[0]
    assert first.from_state.name == "ACTIVE" and first.to_state.name == "WATCH"
    gate = first.gates["watch_ic"]
    assert gate.passed is False and gate.threshold == 0.0 and gate.value < 0.0
    back = next(t for t in rows if t.to_state.name == "ACTIVE")
    assert back.gates["reactivate_ic"].passed is True
    assert back.gates["reactivate_ic"].threshold == 0.005
    assert store.query("SELECT COUNT(*) AS n FROM lifecycle_transitions WHERE eval_index IS NULL")[0]["n"] == 0
    missing = RESEARCH / "does_not_exist.jsonl"
    assert import_lifecycle_log(store, missing).warnings == (f"absent: {missing}",)


def test_import_lifecycle_transitions_tolerates_absence_and_bad_lines(store: Store, tmp_path: Path) -> None:
    missing = tmp_path / "lifecycle_transitions.jsonl"
    rep = import_lifecycle_transitions(store, missing)
    assert rep.inserted == {} and rep.warnings == (f"absent: {missing}",)
    ex = all_examples()["LifecycleTransition"]
    store.insert_alpha("EQ03", asset_class="EQUITY", family="ofi_multilevel", horizon="5s",
                       economic_rationale="")
    lines = [json.dumps(ex.to_dict()), "not json", json.dumps({"alpha_id": "EQ03"}),
             json.dumps({**ex.to_dict(), "alpha_id": "EQ99"})]
    missing.write_text("\n".join(lines) + "\n")
    rep = import_lifecycle_transitions(store, missing)
    assert rep.inserted == {"alphas.current_state": 1, "lifecycle_transitions": 2}
    assert len(rep.warnings) == 3
    assert store.query("SELECT current_state FROM alphas WHERE alpha_id='EQ03'")[0]["current_state"] == "ACTIVE"


def test_import_experiment_documents_roundtrip(store: Store, tmp_path: Path) -> None:
    ex = all_examples()
    spec, result = ex["ExperimentSpec"], ex["ExperimentResult"]
    run = tmp_path / spec.experiment_id
    run.mkdir()
    (run / "spec.json").write_text(json.dumps(spec.to_dict()))
    (run / "result.json").write_text(json.dumps(result.to_dict()))
    (tmp_path / "wrongid").mkdir()
    (tmp_path / "wrongid" / "spec.json").write_text(json.dumps(spec.to_dict()))
    rep = import_experiment_documents(store, tmp_path)
    assert rep.inserted == {"experiment_results": 1, "experiments": 1}
    assert len(rep.warnings) == 1 and "wrongid" in rep.warnings[0]
    assert store.fetch(ExperimentSpec) == (spec,)
    assert store.fetch(ExperimentResult) == (result,)


def test_import_tca_orders_and_perold_identity(store: Store) -> None:
    rep = import_tca_orders(store, RESEARCH / "tca" / "tca_orders.json")
    assert rep.inserted["tca_orders"] == 36 and not rep.warnings
    rows = store.query("SELECT * FROM tca_orders ORDER BY order_id")
    for r in rows:
        assert r["total_is_bps"] == pytest.approx(
            r["delay_bps"] + r["trading_bps"] + r["opportunity_bps"], abs=1e-9)
    assert rows[18]["order_id"] == 19 and rows[18]["vwap_slippage_bps"] is None


def test_import_model_runs_and_baselines(store: Store) -> None:
    rep = import_model_runs(store, RESEARCH / "models")
    assert rep.inserted["model_runs"] == 33 and not rep.warnings
    ols = store.query("SELECT * FROM model_runs WHERE run_id='run_0001_ols'")[0]
    assert ols["model_version"] == "ols_v1" and ols["mean_ic"] is not None
    assert json.loads(ols["manifest_json"])["experiment_id"] == "run_0001_ols"
    rep = import_baselines(store, RESEARCH / "baselines")
    assert rep.inserted["drift_baselines"] == 36 and not rep.warnings
    kinds = {r["kind"]: r["n"] for r in store.query(
        "SELECT kind, COUNT(*) AS n FROM drift_baselines GROUP BY kind ORDER BY kind")}
    expected: dict = {}
    for path in (RESEARCH / "baselines").glob("*.json"):
        kind = json.loads(path.read_text())["kind"]
        expected[kind] = expected.get(kind, 0) + 1
    assert kinds == expected and kinds["ic"] == 9
    ic = store.query("SELECT * FROM drift_baselines WHERE name='run_eq03_ic'")[0]
    assert ic["horizon"] == "5s" and ic["edges_json"] is None and ic["ic_mean"] > 0


def test_import_all_is_idempotent(built: Store) -> None:
    before = built.counts()
    again = import_all(built, REPO)
    assert built.counts() == before
    for step, rep in again.items():
        assert rep.inserted == built.reports[step].inserted, step  # type: ignore[attr-defined]


def test_built_store_exports_deterministically(built: Store, tmp_path: Path) -> None:
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    assert built.export_jsonl("ledger_entries", a) == built.export_jsonl("ledger_entries", b)
    assert a.read_bytes() == b.read_bytes()
    keys = [json.loads(line)["ledger_key"] for line in a.read_text().splitlines()]
    assert keys == sorted(keys)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_build_explain_sql(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "store" / "iap.sqlite"
    assert store_main(["build", "--db", str(db), "--repo-root", str(REPO)]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].split() == ["table", "rows"]
    table = {line.split()[0]: int(line.split()[1]) for line in lines[1:]}
    assert table["alphas"] == 24 and table["model_runs"] == 33
    assert list(table) == sorted(table)

    with Store.open(db) as s:
        s.insert_trace(example_trace())
        _name_example_venue_3(s)
    assert store_main(["explain", "--db", str(db), "12345"]) == 0
    assert capsys.readouterr().out.rstrip("\n") == GOLDEN["explain"]["text"]
    assert store_main(["explain", "--db", str(db), "1"]) == 1
    assert "no parent order 1" in capsys.readouterr().err

    assert store_main(["sql", "--db", str(db),
                       "SELECT alpha_id, COUNT(*) AS n FROM ledger_entries "
                       "WHERE alpha_id = 'EQ03' GROUP BY alpha_id"]) == 0
    assert json.loads(capsys.readouterr().out.strip())["alpha_id"] == "EQ03"
    assert store_main(["sql", "--db", str(tmp_path / "none.sqlite"), "SELECT 1"]) == 2
    # sql is read-only: a writing statement fails and changes nothing.
    assert store_main(["sql", "--db", str(db), "DROP TABLE alphas"]) == 1
    assert "read-only" in capsys.readouterr().err
    assert store_main(["sql", "--db", str(db), "INSERT INTO venues (venue_id, venue, "
                       "asset_class, latency_mean_ns, latency_jitter_ns, supports_json) "
                       "VALUES (99, 'X', 'EQUITY', 1, 0, '[]')"]) == 1
    capsys.readouterr()
    with Store.open(db) as s:
        assert s.counts()["alphas"] == 24
        assert not s.query("SELECT 1 FROM venues WHERE venue_id = 99")
    with pytest.raises(sqlite3.OperationalError):
        Store.open(tmp_path / "missing.sqlite", read_only=True)
