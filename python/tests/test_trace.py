"""``iap.trace``: builder validation, sink byte-equality, digest determinism
and sensitivity, the attribution identity, explain over a JSONL file."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from iap.contracts.examples import VENUE_NAMES, all_examples, example_trace
from iap.contracts.types import (
    AlphaSignal,
    Attribution,
    ContractError,
    DecisionTrace,
    Direction,
    ParentOrder,
    Side,
)
from iap.contracts.validate import validate_typed
from iap.contracts.versions import canonical_json
from iap.store import Store
from iap.trace import (
    JsonlTraceSink,
    MemoryTraceSink,
    MultiSink,
    StoreTraceSink,
    TraceBuilder,
    TraceDigest,
    attribute,
    attribution_report,
    explain,
    explain_jsonl,
    find_trace_jsonl,
    residual_bps,
    trace_line,
)

REPO = Path(__file__).resolve().parents[2]
GOLDEN = json.loads((REPO / "tests" / "golden" / "expected_contracts_examples.json").read_text())

#: SHA-256 of ``canonical_json(example_trace().to_dict()) + "\n"`` — the
#: replay-determinism digest of a one-trace stream (known answer for ports).
EXAMPLE_DIGEST = "bf60a300d151c9cea462e339b0dac407c595fdc5e3c59efd588d5aada8455162"
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


def _builder_for(trace: DecisionTrace) -> TraceBuilder:
    return TraceBuilder(trace.session_id, trace.instrument_id, trace.event_ts,
                        trace.sequence, trace.data_version, trace.feature_version,
                        trace.model_version, trace.config_version)


def _rebuild(trace: DecisionTrace) -> TraceBuilder:
    st = trace.stages
    b = _builder_for(trace)
    for sig in st.signal:
        b.add_signal(sig)
    b.set_portfolio(st.portfolio)
    for rd in st.risk:
        b.add_risk(rd)
    for po in st.parent_orders:
        b.add_parent_order(po)
    for co in st.child_orders:
        b.add_child_order(co)
    for vd in st.routing:
        b.add_routing(vd)
    for er in st.fills:
        b.add_fill(er)
    for tr in st.tca:
        b.add_tca(tr)
    b.set_attribution(st.attribution)
    return b


def _with_signal(trace: DecisionTrace, **changes: object) -> DecisionTrace:
    """The trace with its first signal's fields changed."""
    d = trace.to_dict()
    d["stages"]["signal"][0].update(changes)
    return DecisionTrace.from_dict(d)


def _next_decision(trace: DecisionTrace) -> DecisionTrace:
    """The same stages one sequence later (a distinct trace id)."""
    b = _rebuild(trace)
    b.sequence = trace.sequence + 1
    return b.build()


# --------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------

def test_builder_reproduces_example_and_is_repeatable() -> None:
    trace = example_trace()
    b = _rebuild(trace)
    assert b.trace_id == trace.trace_id == GOLDEN["trace_id"]["expected"]
    built = b.build()
    assert built == trace
    assert b.build() == built  # pure: building twice gives an equal trace
    assert validate_typed(built) == trace.to_dict()


def test_builder_empty_stages_render_none() -> None:
    trace = example_trace()
    empty = _builder_for(trace).build()
    assert empty.stages.signal == () and empty.stages.portfolio is None
    assert explain(empty).splitlines()[0] == f"Trace {trace.trace_id}"
    assert all(line.endswith("(none)") for line in explain(empty).splitlines()[1:])


def test_builder_rejects_wrong_types_and_invalid_traces() -> None:
    trace = example_trace()
    b = _builder_for(trace)
    with pytest.raises(TypeError):
        b.add_signal(trace.stages.parent_orders[0])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        b.add_parent_order(trace.stages.signal[0])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        b.set_portfolio(trace.stages.risk[0])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        b.set_attribution(trace.stages.tca[0])  # type: ignore[arg-type]
    bad = TraceBuilder(trace.session_id, trace.instrument_id, trace.event_ts, -1,
                       trace.data_version, trace.feature_version, trace.model_version,
                       trace.config_version)
    with pytest.raises(ValueError):  # make_trace_id rejects the sequence
        bad.build()
    bad_hash = TraceBuilder(trace.session_id, trace.instrument_id, trace.event_ts,
                            trace.sequence, "not-a-hash", trace.feature_version,
                            trace.model_version, trace.config_version)
    with pytest.raises(ContractError):
        bad_hash.build()


# --------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------

def test_jsonl_sink_bytes_are_canonical_lines(tmp_path: Path) -> None:
    trace = example_trace()
    other = _with_signal(trace, confidence=0.5)
    path = tmp_path / "traces.jsonl"
    with JsonlTraceSink(path) as sink:
        sink.emit(trace)
        sink.emit(other)
        assert sink.digest.count == 2
    expected = (canonical_json(trace.to_dict()) + "\n" + canonical_json(other.to_dict()) + "\n")
    assert path.read_bytes() == expected.encode("ascii")
    with pytest.raises(RuntimeError):
        sink.emit(trace)
    # a second run truncates and reproduces the same bytes; append continues
    with JsonlTraceSink(path) as sink:
        sink.emit(trace)
        sink.emit(other)
    assert path.read_bytes() == expected.encode("ascii")
    with JsonlTraceSink(path, append=True) as sink:
        sink.emit(trace)
    assert path.read_text().count("\n") == 3


def test_all_sinks_agree_and_multisink_fans_out(tmp_path: Path) -> None:
    trace = example_trace()
    other = _next_decision(trace)
    assert other.trace_id != trace.trace_id
    path = tmp_path / "t.jsonl"
    memory = MemoryTraceSink()
    with Store.open(":memory:") as store:
        store.init()
        with MultiSink(JsonlTraceSink(path), memory, StoreTraceSink(store)) as multi:
            multi.emit(trace)
            multi.emit(other)
        assert memory.traces == (trace, other) and len(memory) == 2
        assert store.counts()["decision_traces"] == 2
        assert store.get_trace(other.trace_id) == other
        digests = {s.digest.hexdigest() for s in multi.sinks}  # type: ignore[attr-defined]
        assert digests == {TraceDigest.of_jsonl(path).hexdigest()}
    with pytest.raises(TypeError):
        MultiSink(object())  # type: ignore[arg-type]


def test_sinks_validate_before_persisting(tmp_path: Path) -> None:
    trace = example_trace()
    broken = object.__new__(DecisionTrace)
    for name in DecisionTrace.__dataclass_fields__:  # type: ignore[attr-defined]
        object.__setattr__(broken, name, getattr(trace, name))
    object.__setattr__(broken, "trace_id", "not-hex")
    memory = MemoryTraceSink()
    with pytest.raises(ValueError):
        memory.emit(broken)
    assert len(memory) == 0
    path = tmp_path / "t.jsonl"
    with JsonlTraceSink(path) as sink:
        with pytest.raises(ValueError):
            sink.emit(broken)
    assert path.read_bytes() == b""


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------

def test_digest_known_answer_and_determinism() -> None:
    trace = example_trace()
    line = trace_line(trace)
    assert line == canonical_json(trace.to_dict())
    assert hashlib.sha256((line + "\n").encode("ascii")).hexdigest() == EXAMPLE_DIGEST
    assert TraceDigest().update(trace).hexdigest() == EXAMPLE_DIGEST
    assert TraceDigest().hexdigest() == EMPTY_DIGEST
    # streaming == one-shot over the concatenation
    a = TraceDigest().update(trace).update(_with_signal(trace, confidence=0.5))
    b = TraceDigest().update(_rebuild(trace).build()).update(_with_signal(trace, confidence=0.5))
    assert a.hexdigest() == b.hexdigest() and a.count == 2
    assert a.hexdigest() == hashlib.sha256(
        (line + "\n" + trace_line(_with_signal(trace, confidence=0.5)) + "\n").encode("ascii")
    ).hexdigest()


def test_digest_is_sensitive_to_one_field_and_to_order() -> None:
    trace = example_trace()
    base = TraceDigest().update(trace).hexdigest()
    changed = _with_signal(trace, expected_return=4.3e-4)
    assert TraceDigest().update(changed).hexdigest() != base
    header_changed = DecisionTrace.from_dict({**trace.to_dict(), "sequence": trace.sequence + 1,
                                              "trace_id": trace.trace_id})
    assert TraceDigest().update(header_changed).hexdigest() != base
    ab = TraceDigest().update(trace).update(changed).hexdigest()
    ba = TraceDigest().update(changed).update(trace).hexdigest()
    assert ab != ba


def test_digest_of_jsonl_recanonicalises(tmp_path: Path) -> None:
    trace = example_trace()
    pretty = tmp_path / "pretty.jsonl"
    pretty.write_text(json.dumps(trace.to_dict(), indent=2).replace("\n", " ") + "\n\n")
    assert TraceDigest.of_jsonl(pretty).hexdigest() == EXAMPLE_DIGEST
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"trace_id": "x"}\n')
    with pytest.raises(ValueError, match="bad.jsonl:1"):
        TraceDigest.of_jsonl(bad)


# --------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------

def test_attribution_identity_and_signs() -> None:
    ex = all_examples()
    parent, signal, tca = ex["ParentOrder"], ex["AlphaSignal"], ex["TCAResult"]
    a = attribute(parent, signal, tca, realized_bps=2.0)
    assert isinstance(a, Attribution)
    assert a.alpha_bps == pytest.approx(4.2)          # +4.2e-4 * 1e4, BID
    assert a.spread_bps == -tca.spread_cost_bps == -0.8
    assert a.impact_bps == -tca.impact_bps == -0.5
    assert a.fees_bps == -tca.fees_bps == -0.4
    assert a.timing_bps == -tca.timing_cost_bps == -0.2
    assert a.total_bps == pytest.approx(a.alpha_bps + a.spread_bps + a.impact_bps
                                        + a.fees_bps + a.timing_bps)
    assert a.total_bps == pytest.approx(2.3)
    assert validate_typed(a)
    # residual is reported, never folded in
    report = attribution_report(parent, signal, tca, realized_bps=2.0)
    assert report.attribution == a and report.realized_bps == 2.0
    assert report.residual_bps == pytest.approx(2.0 - a.total_bps)
    assert residual_bps(a, 2.0) == report.residual_bps
    # selling on a negative forecast is positive alpha
    sell = ParentOrder.from_dict({**parent.to_dict(), "side": Side.ASK.value})
    down = AlphaSignal.from_dict({**signal.to_dict(), "expected_return": -3.0e-4,
                                  "direction": Direction.DOWN.value})
    assert attribute(sell, down, tca, 0.0).alpha_bps == pytest.approx(3.0)
    assert attribute(sell, signal, tca, 0.0).alpha_bps == pytest.approx(-4.2)


def test_attribution_rejects_mismatches_and_non_finite() -> None:
    ex = all_examples()
    parent, signal, tca = ex["ParentOrder"], ex["AlphaSignal"], ex["TCAResult"]
    with pytest.raises(ValueError):
        attribute(parent, signal, tca, float("nan"))
    with pytest.raises(ValueError):
        attribute(parent, signal, tca, float("inf"))
    other_instrument = AlphaSignal.from_dict({**signal.to_dict(), "instrument_id": 2})
    with pytest.raises(ValueError):
        attribute(parent, other_instrument, tca, 0.0)
    other_order = ParentOrder.from_dict({**parent.to_dict(), "parent_order_id": 1})
    with pytest.raises(ValueError):
        attribute(other_order, signal, tca, 0.0)


# --------------------------------------------------------------------------
# Explain over JSONL
# --------------------------------------------------------------------------

def test_explain_jsonl_matches_golden(tmp_path: Path) -> None:
    trace = example_trace()
    other = _with_signal(trace, confidence=0.5)
    path = tmp_path / "traces.jsonl"
    with JsonlTraceSink(path) as sink:
        sink.emit(other)
        sink.emit(trace)
    assert find_trace_jsonl(path, 12345) == other  # first match wins
    text = explain_jsonl(path, 12345, VENUE_NAMES)
    assert text.splitlines()[1].endswith("confidence = 0.50")
    with JsonlTraceSink(path) as sink:
        sink.emit(trace)
    assert explain_jsonl(path, 12345, VENUE_NAMES) == GOLDEN["explain"]["text"]
    assert explain_jsonl(path, 12345) == GOLDEN["explain"]["text"].replace(
        "XV1", "1").replace("XV2", "2").replace("XV3", "3")
    with pytest.raises(KeyError):
        explain_jsonl(path, 99)
