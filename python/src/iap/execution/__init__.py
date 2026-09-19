"""Execution layer: simulator, parent algos, smart order router, replay driver.

Python reference port of the normative C++ execution stack
(``cpp/{execution,sor,replay}``; PLATFORM_CONVENTIONS.md section 11.2-11.3).
The nine pinned simulator rules live in ``iap.execution.simulator``; the
golden ``tests/golden/expected_replay_fills.json`` is reproduced exactly by
``ExecutionReplay`` (``python/tests/test_execution_golden.py``).
"""

from iap.execution.algos import (
    AlgoType,
    ParentOrder,
    slice_quantities,
    slice_times,
    slice_weights,
)
from iap.execution.config import (
    EXECUTION_FILE,
    INSTRUMENTS_FILE,
    VENUES_FILE,
    ExecConfig,
    SorOptions,
    load_exec_config,
    load_instruments,
    load_sor_options,
    load_venues,
)
from iap.execution.replay import ExecReplayResult, ExecutionReplay, ParentReport
from iap.execution.simulator import ExecutionSimulator
from iap.execution.sor import NO_ROUTE, SmartOrderRouter
from iap.execution.types import (
    CancelReason,
    ChildOrder,
    ExecCounters,
    Fill,
    InstrumentSpec,
    LatencyConfig,
    Liquidity,
    OrderState,
    OrderType,
    VenueSpec,
)

__all__ = [
    "AlgoType",
    "CancelReason",
    "ChildOrder",
    "EXECUTION_FILE",
    "ExecConfig",
    "ExecCounters",
    "ExecReplayResult",
    "ExecutionReplay",
    "ExecutionSimulator",
    "Fill",
    "INSTRUMENTS_FILE",
    "InstrumentSpec",
    "LatencyConfig",
    "Liquidity",
    "NO_ROUTE",
    "OrderState",
    "OrderType",
    "ParentOrder",
    "ParentReport",
    "SmartOrderRouter",
    "SorOptions",
    "VENUES_FILE",
    "VenueSpec",
    "load_exec_config",
    "load_instruments",
    "load_sor_options",
    "load_venues",
    "slice_quantities",
    "slice_times",
    "slice_weights",
]
