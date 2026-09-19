"""Fail-closed hard risk engine — Python reference port of ``rust/risk``.

PLATFORM_CONVENTIONS.md §11.1 is the pinned contract and ``rust/risk`` the
normative implementation whose rule text governs; this package is the
reference-equivalent Python implementation, proven by the same goldens
(``tests/golden/expected_risk_decisions.json`` exact decisions,
``expected_risk_audit.jsonl`` byte-identical audit, ``expected_risk_snapshot.json``
byte-identical snapshot + restore continuation).

Modules:

- :mod:`iap.risk.limits` — ``RiskLimits`` (strict ``configs/risk/risk.json``
  x-version 3 parser, fail-closed on any error);
- :mod:`iap.risk.refdata` — ``InstrumentRef`` and the builders from
  ``configs/instruments/instruments.json`` / ``ReferenceData`` / the golden table;
- :mod:`iap.risk.orders` — ``OrderRequest`` / ``OrderType`` / ``Fill`` contracts;
- :mod:`iap.risk.events` — ``RiskEvent``, ``Scope`` / ``Severity`` / ``Decision``,
  the pinned ``Rules`` ids and ``fmt_fixed``;
- :mod:`iap.risk.engine` — ``RiskEngine`` (the 23-check pinned order, kill
  switches, projections, loss latching, throttle, snapshot/restore) and
  ``RiskDecision``;
- :mod:`iap.risk.serialize` — serde_json-identical canonical JSON.
"""

from iap.risk.engine import SNAPSHOT_VERSION, RiskDecision, RiskEngine, RiskMetrics
from iap.risk.events import Decision, RiskEvent, Rules, Scope, Severity, fmt_fixed
from iap.risk.limits import RISK_CONFIG_VERSION, FxConversion, RiskLimits
from iap.risk.orders import Fill, OrderRequest, OrderType, order_validation_error
from iap.risk.refdata import (
    InstrumentRef,
    equity_refs,
    instrument_refs_from_config,
    instrument_refs_from_golden,
    instrument_refs_from_reference_data,
    load_instrument_refs,
)
from iap.risk.serialize import format_f64, json_escape, to_canonical_json

__all__ = [
    "SNAPSHOT_VERSION",
    "RISK_CONFIG_VERSION",
    "RiskEngine",
    "RiskDecision",
    "RiskMetrics",
    "RiskLimits",
    "FxConversion",
    "InstrumentRef",
    "equity_refs",
    "instrument_refs_from_config",
    "instrument_refs_from_golden",
    "instrument_refs_from_reference_data",
    "load_instrument_refs",
    "OrderRequest",
    "OrderType",
    "Fill",
    "order_validation_error",
    "RiskEvent",
    "Rules",
    "Scope",
    "Severity",
    "Decision",
    "fmt_fixed",
    "format_f64",
    "json_escape",
    "to_canonical_json",
]
