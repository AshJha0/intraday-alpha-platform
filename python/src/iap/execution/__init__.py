"""Execution layer: simulator, parent algos, smart order router, replay driver.

Python reference port of the normative C++ execution stack
(``cpp/{execution,sor,replay}``; PLATFORM_CONVENTIONS.md section 11.2-11.3).
The nine pinned simulator rules live in ``iap.execution.simulator``; the
golden ``tests/golden/expected_replay_fills.json`` is reproduced exactly by
``ExecutionReplay`` (``python/tests/test_execution_golden.py``).
"""

from iap.execution.algos import (
    AlgoType,
    ISModel,
    ParentOrder,
    slice_quantities,
    slice_times,
    slice_weights,
)
from iap.execution.child_exec import ChildExecParams
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
from iap.execution.dark import (
    DARK_CONTROL_CODES,
    DARK_CONTROL_GOLDENS,
    VENUE_TYPES,
    DarkControls,
    DarkFillModel,
    DarkFirstParams,
    dark_pretrade_check,
)
from iap.execution.fading import (
    FadeParams,
    PullRecord,
    QueueCancelParams,
    pull_outcomes,
    pull_rows,
    pull_summary,
)
from iap.execution.fill_hazard import FillHazardModel
from iap.execution.markout_feedback import FeedbackConfig, MarkoutFeedback
from iap.execution.pacing import PacingParams
from iap.execution.passive import (
    POLICY_NAMES,
    ExecPolicy,
    PassiveParams,
    PassiveStats,
    max_behind_qty,
    patience_ns,
    post_price,
)
from iap.execution.replay import ExecReplayResult, ExecutionReplay, ParentReport
from iap.execution.signal import ArraySignalSource, SignalReading
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
from iap.execution.urgency import AlphaUrgencyPolicy

__all__ = [
    "AlgoType",
    "AlphaUrgencyPolicy",
    "ArraySignalSource",
    "PacingParams",
    "SignalReading",
    "CancelReason",
    "ChildOrder",
    "EXECUTION_FILE",
    "ExecConfig",
    "ExecCounters",
    "FeedbackConfig",
    "FillHazardModel",
    "MarkoutFeedback",
    "ExecPolicy",
    "ExecReplayResult",
    "ExecutionReplay",
    "ExecutionSimulator",
    "FadeParams",
    "DARK_CONTROL_CODES",
    "DARK_CONTROL_GOLDENS",
    "VENUE_TYPES",
    "DarkControls",
    "DarkFillModel",
    "ChildExecParams",
    "DarkFirstParams",
    "dark_pretrade_check",
    "Fill",
    "PullRecord",
    "QueueCancelParams",
    "pull_outcomes",
    "pull_rows",
    "pull_summary",
    "INSTRUMENTS_FILE",
    "InstrumentSpec",
    "LatencyConfig",
    "Liquidity",
    "NO_ROUTE",
    "OrderState",
    "OrderType",
    "ParentOrder",
    "ParentReport",
    "PassiveParams",
    "PassiveStats",
    "POLICY_NAMES",
    "SmartOrderRouter",
    "SorOptions",
    "VENUES_FILE",
    "VenueSpec",
    "load_exec_config",
    "load_instruments",
    "load_sor_options",
    "load_venues",
    "max_behind_qty",
    "patience_ns",
    "post_price",
    "slice_quantities",
    "slice_times",
    "slice_weights",
    "ISModel",
]
