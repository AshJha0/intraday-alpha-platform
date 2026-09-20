"""``iap.trace`` — building, persisting, digesting and explaining
:class:`~iap.contracts.types.DecisionTrace` documents.

    builder = TraceBuilder(session_id, instrument_id, event_ts, sequence,
                           data_version, feature_version, model_version, config_version)
    trace = builder.add_signal(sig).add_parent_order(po).build()
    sink = MultiSink(JsonlTraceSink("traces.jsonl"), StoreTraceSink(store))
    sink.emit(trace); print(sink.sinks[0].digest.hexdigest())
"""

from iap.trace.attribution import AttributionReport, attribute, attribution_report, residual_bps
from iap.trace.builder import TraceBuilder
from iap.trace.digest import TraceDigest, trace_line
from iap.trace.explain import explain, explain_jsonl, find_trace_jsonl
from iap.trace.sinks import JsonlTraceSink, MemoryTraceSink, MultiSink, StoreTraceSink

__all__ = [
    "AttributionReport",
    "JsonlTraceSink",
    "MemoryTraceSink",
    "MultiSink",
    "StoreTraceSink",
    "TraceBuilder",
    "TraceDigest",
    "attribute",
    "attribution_report",
    "explain",
    "explain_jsonl",
    "find_trace_jsonl",
    "residual_bps",
    "trace_line",
]
