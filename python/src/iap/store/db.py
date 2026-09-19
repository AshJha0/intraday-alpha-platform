"""``Store`` — the platform data model over ``sqlite3`` (stdlib only).

The store is a derived, rebuildable index of the flat-file artefacts
(``docs/DATA_MODEL.md``).  Every typed write goes through
:func:`iap.contracts.validate.validate_typed` first, is an upsert by
primary key (``INSERT OR REPLACE``) and runs in one transaction per call,
so a re-run of any writer leaves the database unchanged.  Reads go through
``Contract.from_dict`` so a row that no longer satisfies its contract is
an error, not a value.

Nothing here reads a clock: every timestamp column is event or ledger time
supplied by the caller.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import (Any, Callable, Dict, Iterator, List, Mapping, Optional,
                    Sequence, Tuple, Type, TypeVar, Union)

from iap.contracts.types import (
    AlphaSignal,
    Attribution,
    ChildOrder,
    Contract,
    DecisionTrace,
    ExecutionReport,
    ExperimentResult,
    ExperimentSpec,
    LifecycleTransition,
    ParentOrder,
    PortfolioTarget,
    RiskDecision,
    TCAResult,
    VenueDecision,
    explain,
)
from iap.contracts.validate import validate_typed
from iap.contracts.versions import canonical_json
from iap.store import ddl

__all__ = ["Store", "STAGE_TABLES", "LIFECYCLE_STATES"]

C = TypeVar("C", bound=Contract)
Row = Dict[str, Any]

#: Normalized decomposition of a decision trace, rewritten together by
#: :meth:`Store.insert_trace` (delete-then-insert, keyed by ``trace_id``).
STAGE_TABLES: Tuple[str, ...] = (
    "alpha_signals", "portfolio_targets", "portfolio_legs", "risk_decisions",
    "parent_orders", "child_orders", "venue_decisions", "executions",
    "tca_results", "attribution",
)

#: The lifecycle state names accepted by ``alphas.current_state``.
LIFECYCLE_STATES: Tuple[str, ...] = (
    "RESEARCH", "CANDIDATE", "VALIDATING", "PAPER", "ACTIVE", "WATCH", "RETIRED")

_TRACE_HEADER: Tuple[str, ...] = (
    "trace_id", "session_id", "instrument_id", "event_ts", "sequence",
    "data_version", "feature_version", "model_version", "config_version")


def _j(obj: Any) -> str:
    return canonical_json(obj)


def _uj(text: str) -> Any:
    return json.loads(text)


def _flag(value: Optional[bool]) -> Optional[int]:
    return None if value is None else int(bool(value))


def _unflag(value: Optional[int]) -> Optional[bool]:
    return None if value is None else bool(value)


# --------------------------------------------------------------------------
# Row codecs: contract -> column dict (link columns added by the caller) and
# back.  Column names equal the contract field names except where a field is
# nested (JSON column, flattened sub-record) or a boolean (0/1 BIGINT).
# --------------------------------------------------------------------------

def _enc_alpha_signal(sig: AlphaSignal) -> Row:
    return sig.to_dict()


def _dec_alpha_signal(row: Row) -> AlphaSignal:
    return AlphaSignal.from_dict(_pick(row, AlphaSignal))


def _enc_portfolio_target(pt: PortfolioTarget) -> Row:
    d = pt.to_dict()
    d["targets_json"] = _j(d.pop("targets"))
    return d


def _dec_portfolio_target(row: Row) -> PortfolioTarget:
    d = _pick(row, PortfolioTarget, skip=("targets",))
    d["targets"] = _uj(row["targets_json"])
    return PortfolioTarget.from_dict(d)


def _enc_risk_decision(rd: RiskDecision) -> Row:
    return rd.to_dict()


def _dec_risk_decision(row: Row) -> RiskDecision:
    return RiskDecision.from_dict(_pick(row, RiskDecision))


def _enc_parent_order(po: ParentOrder) -> Row:
    d = po.to_dict()
    d["params_json"] = _j(d.pop("params"))
    return d


def _dec_parent_order(row: Row) -> ParentOrder:
    d = _pick(row, ParentOrder, skip=("params",))
    d["params"] = _uj(row["params_json"])
    return ParentOrder.from_dict(d)


def _enc_child_order(co: ChildOrder) -> Row:
    return co.to_dict()


def _dec_child_order(row: Row) -> ChildOrder:
    return ChildOrder.from_dict(_pick(row, ChildOrder))


def _enc_venue_decision(vd: VenueDecision) -> Row:
    d = vd.to_dict()
    d["candidates_json"] = _j(d.pop("candidates"))
    return d


def _dec_venue_decision(row: Row) -> VenueDecision:
    d = _pick(row, VenueDecision, skip=("candidates",))
    d["candidates"] = _uj(row["candidates_json"])
    return VenueDecision.from_dict(d)


def _enc_execution_report(er: ExecutionReport) -> Row:
    return er.to_dict()


def _dec_execution_report(row: Row) -> ExecutionReport:
    return ExecutionReport.from_dict(_pick(row, ExecutionReport))


def _enc_tca_result(tr: TCAResult) -> Row:
    d = tr.to_dict()
    d["venue_contribution_json"] = _j(d.pop("venue_contribution_bps"))
    lat = d.pop("latency_ns")
    for key in ("min", "mean", "max", "p50", "p99"):
        d[f"latency_{key}_ns"] = lat[key]
    return d


def _dec_tca_result(row: Row) -> TCAResult:
    d = _pick(row, TCAResult, skip=("venue_contribution_bps", "latency_ns"))
    d["venue_contribution_bps"] = _uj(row["venue_contribution_json"])
    d["latency_ns"] = {key: row[f"latency_{key}_ns"]
                       for key in ("min", "mean", "max", "p50", "p99")}
    return TCAResult.from_dict(d)


def _enc_attribution(a: Attribution) -> Row:
    return a.to_dict()


def _dec_attribution(row: Row) -> Attribution:
    return Attribution.from_dict(_pick(row, Attribution))


_PERIODS = ("train", "validation", "test")


def _enc_experiment_spec(spec: ExperimentSpec) -> Row:
    d = spec.to_dict()
    d["configuration_json"] = _j(d.pop("configuration"))
    for name in _PERIODS:
        period = d.pop(f"{name}_period")
        d[f"{name}_start_ts"] = period["start_ts"]
        d[f"{name}_end_ts"] = period["end_ts"]
    return d


def _dec_experiment_spec(row: Row) -> ExperimentSpec:
    skip = ("configuration",) + tuple(f"{n}_period" for n in _PERIODS)
    d = _pick(row, ExperimentSpec, skip=skip)
    d["configuration"] = _uj(row["configuration_json"])
    for name in _PERIODS:
        d[f"{name}_period"] = {"start_ts": row[f"{name}_start_ts"],
                               "end_ts": row[f"{name}_end_ts"]}
    return ExperimentSpec.from_dict(d)


def _enc_experiment_result(res: ExperimentResult) -> Row:
    d = res.to_dict()
    d["leakage_detail_json"] = _j(d.pop("leakage_detail"))
    d["leakage_passed"] = _flag(d["leakage_passed"])
    d["hypothesis_sign_confirmed"] = _flag(d["hypothesis_sign_confirmed"])
    return d


def _dec_experiment_result(row: Row) -> ExperimentResult:
    d = _pick(row, ExperimentResult, skip=("leakage_detail",))
    d["leakage_detail"] = _uj(row["leakage_detail_json"])
    d["leakage_passed"] = bool(row["leakage_passed"])
    d["hypothesis_sign_confirmed"] = _unflag(row["hypothesis_sign_confirmed"])
    return ExperimentResult.from_dict(d)


def _enc_lifecycle_transition(t: LifecycleTransition) -> Row:
    d = t.to_dict()
    d["gates_json"] = _j(d.pop("gates"))
    return d


def _dec_lifecycle_transition(row: Row) -> LifecycleTransition:
    d = _pick(row, LifecycleTransition, skip=("gates",))
    d["gates"] = _uj(row["gates_json"])
    return LifecycleTransition.from_dict(d)


def _enc_decision_trace(trace: DecisionTrace) -> Row:
    d = trace.to_dict()
    d["stages_json"] = _j(d.pop("stages"))
    return d


def _dec_decision_trace(row: Row) -> DecisionTrace:
    d = {name: row[name] for name in _TRACE_HEADER}
    d["stages"] = _uj(row["stages_json"])
    return DecisionTrace.from_dict(d)


def _pick(row: Row, cls: Type[Contract], skip: Sequence[str] = ()) -> Row:
    """The contract's scalar fields out of a row (nested ones in ``skip``)."""
    return {name: row[name] for name in cls.__dataclass_fields__  # type: ignore[attr-defined]
            if name not in skip}


_Codec = Tuple[str, Callable[[Any], Row], Callable[[Row], Any]]

#: contract type -> (table, encoder, decoder)
_CODECS: Dict[Type[Contract], _Codec] = {
    AlphaSignal: ("alpha_signals", _enc_alpha_signal, _dec_alpha_signal),
    PortfolioTarget: ("portfolio_targets", _enc_portfolio_target, _dec_portfolio_target),
    RiskDecision: ("risk_decisions", _enc_risk_decision, _dec_risk_decision),
    ParentOrder: ("parent_orders", _enc_parent_order, _dec_parent_order),
    ChildOrder: ("child_orders", _enc_child_order, _dec_child_order),
    VenueDecision: ("venue_decisions", _enc_venue_decision, _dec_venue_decision),
    ExecutionReport: ("executions", _enc_execution_report, _dec_execution_report),
    TCAResult: ("tca_results", _enc_tca_result, _dec_tca_result),
    Attribution: ("attribution", _enc_attribution, _dec_attribution),
    ExperimentSpec: ("experiments", _enc_experiment_spec, _dec_experiment_spec),
    ExperimentResult: ("experiment_results", _enc_experiment_result, _dec_experiment_result),
    LifecycleTransition: ("lifecycle_transitions", _enc_lifecycle_transition,
                          _dec_lifecycle_transition),
    DecisionTrace: ("decision_traces", _enc_decision_trace, _dec_decision_trace),
}


class Store:
    """The platform store over one ``sqlite3`` connection.

    Use :meth:`open` (``":memory:"`` or a file path) then :meth:`init`
    to apply the DDL.  All writes are upserts by primary key inside one
    transaction per call; ``PRAGMA foreign_keys`` is on, so a stage row
    can never outlive its trace.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._columns_cache: Dict[str, Tuple[str, ...]] = {}
        self._pk_cache: Dict[str, Tuple[str, ...]] = {}

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def open(cls, path: Union[str, Path] = ":memory:") -> "Store":
        """Open (creating if needed) the database at ``path``."""
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        return cls(conn)

    def init(self) -> None:
        """Apply the DDL (idempotent) and check the data-model version."""
        ddl.apply(self._conn)
        rows = self.query("SELECT x_version FROM schema_version ORDER BY x_version")
        versions = [r["x_version"] for r in rows]
        if versions != [ddl.DDL_X_VERSION]:
            raise RuntimeError(
                f"store: schema_version rows {versions} != [{ddl.DDL_X_VERSION}]")
        self._columns_cache.clear()
        self._pk_cache.clear()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        nested = self._conn.in_transaction
        try:
            if not nested:
                cur.execute("BEGIN")
            yield cur
            if not nested:
                cur.execute("COMMIT")
        except Exception:
            if not nested:
                cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    # -- catalogue ----------------------------------------------------------

    def tables(self) -> Tuple[str, ...]:
        """All table names, sorted."""
        rows = self.query("SELECT name FROM sqlite_master WHERE type = 'table' "
                          "AND name NOT LIKE 'sqlite_%' ORDER BY name")
        return tuple(r["name"] for r in rows)

    def views(self) -> Tuple[str, ...]:
        """All view names, sorted."""
        rows = self.query("SELECT name FROM sqlite_master WHERE type = 'view' "
                          "ORDER BY name")
        return tuple(r["name"] for r in rows)

    def columns(self, table: str) -> Tuple[str, ...]:
        """Column names of ``table`` (or view) in definition order."""
        cols = self._columns_cache.get(table)
        if cols is None:
            info = self.query(f"PRAGMA table_info({self._ident(table)})")
            if not info:
                raise KeyError(f"store: unknown table {table!r}")
            cols = tuple(r["name"] for r in info)
            self._columns_cache[table] = cols
            self._pk_cache[table] = tuple(
                r["name"] for r in sorted((r for r in info if r["pk"]),
                                          key=lambda r: r["pk"]))
        return cols

    def primary_key(self, table: str) -> Tuple[str, ...]:
        """Primary-key columns of ``table`` in key order (empty for views)."""
        self.columns(table)
        return self._pk_cache[table]

    @staticmethod
    def _ident(name: str) -> str:
        if not name.isidentifier():
            raise ValueError(f"store: invalid identifier {name!r}")
        return name

    # -- generic access -----------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Row]:
        """Run ``sql`` and return every row as a dict (column order of the
        SELECT).  Row order is whatever the statement's ``ORDER BY`` says —
        pass one for deterministic output."""
        cur = self._conn.execute(sql, tuple(params))
        try:
            names = [d[0] for d in cur.description] if cur.description else []
            return [dict(zip(names, row)) for row in cur.fetchall()]
        finally:
            cur.close()

    def upsert(self, table: str, row: Mapping[str, Any]) -> None:
        """``INSERT OR REPLACE`` one row; unknown columns are an error."""
        with self._tx() as cur:
            self._upsert(cur, table, row)

    def _upsert(self, cur: sqlite3.Cursor, table: str, row: Mapping[str, Any]) -> None:
        known = set(self.columns(table))
        unknown = sorted(set(row) - known)
        if unknown:
            raise ValueError(f"store: {table} has no columns {unknown}")
        cols = sorted(row)
        sql = (f"INSERT OR REPLACE INTO {self._ident(table)} "
               f"({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})")
        cur.execute(sql, tuple(row[c] for c in cols))

    def counts(self) -> Dict[str, int]:
        """``{table: row count}`` for every table, sorted by name."""
        return {t: self.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
                for t in self.tables()}

    def export_jsonl(self, table: str, path: Union[str, Path]) -> int:
        """Write ``table`` (or view) as canonical JSON lines ordered by its
        primary key (all columns for a view); returns the row count.
        Byte-deterministic for equal contents."""
        cols = self.columns(table)
        order = self.primary_key(table) or cols
        rows = self.query(f"SELECT {', '.join(cols)} FROM {self._ident(table)} "
                          f"ORDER BY {', '.join(order)}")
        with open(path, "w", encoding="ascii", newline="\n") as fh:
            for row in rows:
                fh.write(canonical_json(row))
                fh.write("\n")
        return len(rows)

    def fetch(self, cls: Type[C], **where: Any) -> Tuple[C, ...]:
        """Every stored ``cls`` matching the equality filters, decoded
        through ``cls.from_dict`` and ordered by primary key."""
        table, _, dec = self._codec(cls)
        cols = self.columns(table)
        unknown = sorted(set(where) - set(cols))
        if unknown:
            raise ValueError(f"store: {table} has no columns {unknown}")
        keys = sorted(where)
        clause = (" WHERE " + " AND ".join(f"{k} = ?" for k in keys)) if keys else ""
        rows = self.query(f"SELECT {', '.join(cols)} FROM {table}{clause} "
                          f"ORDER BY {', '.join(self.primary_key(table))}",
                          [where[k] for k in keys])
        return tuple(dec(r) for r in rows)

    @staticmethod
    def _codec(cls: Type[Contract]) -> _Codec:
        try:
            return _CODECS[cls]
        except KeyError:
            raise TypeError(f"store: {cls.__name__} has no table "
                            "(nested record types are embedded)") from None

    # -- typed writes: trace stages (linked by trace_id) ---------------------

    def insert_alpha_signal(self, signal: AlphaSignal, *, trace_id: str,
                            signal_index: int = 0) -> None:
        validate_typed(signal)
        with self._tx() as cur:
            self._write_alpha_signal(cur, signal, trace_id, signal_index)

    def _write_alpha_signal(self, cur: sqlite3.Cursor, signal: AlphaSignal,
                            trace_id: str, signal_index: int) -> None:
        row = _enc_alpha_signal(signal)
        row.update(trace_id=trace_id, signal_index=signal_index)
        self._upsert(cur, "alpha_signals", row)

    def insert_portfolio_target(self, target: PortfolioTarget, *, trace_id: str) -> None:
        validate_typed(target)
        with self._tx() as cur:
            self._write_portfolio_target(cur, target, trace_id)

    def _write_portfolio_target(self, cur: sqlite3.Cursor, target: PortfolioTarget,
                                trace_id: str) -> None:
        row = _enc_portfolio_target(target)
        row["trace_id"] = trace_id
        self._upsert(cur, "portfolio_targets", row)
        cur.execute("DELETE FROM portfolio_legs WHERE trace_id = ?", (trace_id,))
        for leg in target.targets:
            leg_row = leg.to_dict()
            leg_row["trace_id"] = trace_id
            self._upsert(cur, "portfolio_legs", leg_row)

    def insert_risk_decision(self, decision: RiskDecision, *, trace_id: str,
                             risk_index: int = 0) -> None:
        validate_typed(decision)
        with self._tx() as cur:
            self._write_risk_decision(cur, decision, trace_id, risk_index)

    def _write_risk_decision(self, cur: sqlite3.Cursor, decision: RiskDecision,
                             trace_id: str, risk_index: int) -> None:
        row = _enc_risk_decision(decision)
        row.update(trace_id=trace_id, risk_index=risk_index)
        self._upsert(cur, "risk_decisions", row)

    def insert_parent_order(self, order: ParentOrder, *, trace_id: str) -> None:
        validate_typed(order)
        with self._tx() as cur:
            self._write_linked(cur, order, trace_id)

    def insert_child_order(self, order: ChildOrder, *, trace_id: str) -> None:
        validate_typed(order)
        with self._tx() as cur:
            self._write_linked(cur, order, trace_id)

    def insert_venue_decision(self, decision: VenueDecision, *, trace_id: str) -> None:
        validate_typed(decision)
        with self._tx() as cur:
            self._write_linked(cur, decision, trace_id)

    def insert_tca_result(self, result: TCAResult, *, trace_id: str) -> None:
        validate_typed(result)
        with self._tx() as cur:
            self._write_linked(cur, result, trace_id)

    def _write_linked(self, cur: sqlite3.Cursor, inst: Contract, trace_id: str) -> None:
        table, enc, _ = self._codec(type(inst))
        row = enc(inst)
        row["trace_id"] = trace_id
        self._upsert(cur, table, row)

    def insert_execution_report(self, report: ExecutionReport, *, trace_id: str) -> None:
        """The parent link is resolved through the trace's child orders
        already stored (``NULL`` when the child order is unknown)."""
        validate_typed(report)
        with self._tx() as cur:
            rows = self.query("SELECT parent_order_id FROM child_orders "
                              "WHERE trace_id = ? AND child_order_id = ?",
                              (trace_id, report.order_id))
            parent = rows[0]["parent_order_id"] if rows else None
            self._write_execution(cur, report, trace_id, parent)

    def _write_execution(self, cur: sqlite3.Cursor, report: ExecutionReport,
                         trace_id: str, parent_order_id: Optional[int]) -> None:
        row = _enc_execution_report(report)
        row.update(trace_id=trace_id, parent_order_id=parent_order_id)
        self._upsert(cur, "executions", row)

    def insert_attribution(self, attribution: Attribution, *, trace_id: str,
                           parent_order_id: Optional[int] = None) -> None:
        validate_typed(attribution)
        with self._tx() as cur:
            self._write_attribution(cur, attribution, trace_id, parent_order_id)

    def _write_attribution(self, cur: sqlite3.Cursor, attribution: Attribution,
                           trace_id: str, parent_order_id: Optional[int]) -> None:
        row = _enc_attribution(attribution)
        row.update(trace_id=trace_id, parent_order_id=parent_order_id)
        self._upsert(cur, "attribution", row)

    # -- typed writes: research / lifecycle ---------------------------------

    def insert_experiment_spec(self, spec: ExperimentSpec) -> None:
        validate_typed(spec)
        with self._tx() as cur:
            self._upsert(cur, "experiments", _enc_experiment_spec(spec))

    def insert_experiment_result(self, result: ExperimentResult) -> None:
        validate_typed(result)
        with self._tx() as cur:
            self._upsert(cur, "experiment_results", _enc_experiment_result(result))

    def insert_lifecycle_transition(self, transition: LifecycleTransition, *,
                                    source: str = "api",
                                    eval_index: Optional[int] = None) -> None:
        """``source`` names the artefact (``lifecycle_log``,
        ``lifecycle_transitions``) or ``api`` for a live write."""
        validate_typed(transition)
        with self._tx() as cur:
            self._write_lifecycle_transition(cur, transition, source, eval_index)

    def _write_lifecycle_transition(self, cur: sqlite3.Cursor,
                                    transition: LifecycleTransition, source: str,
                                    eval_index: Optional[int]) -> None:
        row = _enc_lifecycle_transition(transition)
        row.update(source=source, eval_index=eval_index)
        self._upsert(cur, "lifecycle_transitions", row)

    # -- reference rows (no contract type) ----------------------------------

    def insert_session(self, session_id: str, *, data_version: str,
                       config_version: str, seed: int, start_ts: int,
                       end_ts: int, n_events: int) -> None:
        """One replay / paper / backtest session (event-time bounds)."""
        self.upsert("sessions", {
            "session_id": session_id, "data_version": data_version,
            "config_version": config_version, "seed": seed,
            "start_ts": start_ts, "end_ts": end_ts, "n_events": n_events})

    def insert_alpha(self, alpha_id: str, *, asset_class: str, family: str,
                     horizon: str, economic_rationale: str,
                     current_state: str = "RESEARCH") -> None:
        if current_state not in LIFECYCLE_STATES:
            raise ValueError(f"store: unknown lifecycle state {current_state!r}")
        self.upsert("alphas", {
            "alpha_id": alpha_id, "asset_class": asset_class, "family": family,
            "horizon": horizon, "economic_rationale": economic_rationale,
            "current_state": current_state})

    def set_alpha_state(self, alpha_id: str, state: str) -> bool:
        """Update ``alphas.current_state``; False if the alpha is unknown."""
        if state not in LIFECYCLE_STATES:
            raise ValueError(f"store: unknown lifecycle state {state!r}")
        with self._tx() as cur:
            cur.execute("UPDATE alphas SET current_state = ? WHERE alpha_id = ?",
                        (state, alpha_id))
            return cur.rowcount > 0

    # -- decision traces ----------------------------------------------------

    def insert_trace(self, trace: DecisionTrace) -> str:
        """Store the validated trace document AND its normalized
        decomposition (:data:`STAGE_TABLES`), all in one transaction.

        Re-inserting a trace id replaces the document and every stage row
        that carried that trace id, so the decomposition never holds stale
        rows.  Returns the trace id.
        """
        validate_typed(trace)
        st = trace.stages
        tid = trace.trace_id
        parents = st.parent_orders
        child_parent = {c.child_order_id: c.parent_order_id for c in st.child_orders}
        with self._tx() as cur:
            for table in STAGE_TABLES:
                cur.execute(f"DELETE FROM {table} WHERE trace_id = ?", (tid,))
            self._upsert(cur, "decision_traces", _enc_decision_trace(trace))
            for i, sig in enumerate(st.signal):
                self._write_alpha_signal(cur, sig, tid, i)
            if st.portfolio is not None:
                self._write_portfolio_target(cur, st.portfolio, tid)
            for i, rd in enumerate(st.risk):
                self._write_risk_decision(cur, rd, tid, i)
            for po in parents:
                self._write_linked(cur, po, tid)
            for co in st.child_orders:
                self._write_linked(cur, co, tid)
            for vd in st.routing:
                self._write_linked(cur, vd, tid)
            for er in st.fills:
                self._write_execution(cur, er, tid, child_parent.get(er.order_id))
            for tr in st.tca:
                self._write_linked(cur, tr, tid)
            if st.attribution is not None:
                self._write_attribution(
                    cur, st.attribution, tid,
                    parents[0].parent_order_id if parents else None)
        return tid

    def get_trace(self, trace_id: str) -> DecisionTrace:
        """The stored trace, re-validated through ``from_dict``;
        ``KeyError`` when absent."""
        rows = self.query(
            f"SELECT {', '.join(_TRACE_HEADER)}, stages_json FROM decision_traces "
            "WHERE trace_id = ?", (trace_id,))
        if not rows:
            raise KeyError(f"store: no trace {trace_id!r}")
        return _dec_decision_trace(rows[0])

    def trace_id_for_order(self, parent_order_id: int) -> str:
        """The trace that carries ``parent_order_id``; ``KeyError`` if none."""
        rows = self.query("SELECT trace_id FROM parent_orders WHERE parent_order_id = ?",
                          (parent_order_id,))
        if not rows:
            raise KeyError(f"store: no parent order {parent_order_id}")
        return rows[0]["trace_id"]

    def venue_names(self) -> Dict[int, str]:
        """``{venue_id: display name}`` from the venues table (sorted)."""
        rows = self.query("SELECT venue_id, venue FROM venues ORDER BY venue_id")
        return {r["venue_id"]: r["venue"] for r in rows}

    def explain(self, parent_order_id: int,
                venue_names: Optional[Mapping[int, str]] = None) -> str:
        """:func:`iap.contracts.types.explain` of the order's trace, with
        venue display names from the venues table (``venue_names`` entries
        override them)."""
        trace = self.get_trace(self.trace_id_for_order(parent_order_id))
        names = self.venue_names()
        names.update(venue_names or {})
        return explain(trace, names)
