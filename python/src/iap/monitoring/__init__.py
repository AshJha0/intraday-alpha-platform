"""REL-16 feedback and monitoring policy, enforced in code.

See docs/MONITORING_POLICY.md.  :mod:`.config` loads the versioned, hashed
policy; :mod:`.inputs` defines the typed inputs and their validation;
:mod:`.machine` is the deterministic ACTIVE / WATCH / REVIEW / HALT /
UNKNOWN state machine; :mod:`.report` builds the monitoring report;
:mod:`.explain` bounds what an AI explainer may say.
"""

from .config import MonitoringConfig, load_monitoring_config
from .inputs import Expectation, OrdersSnapshot, PositionsSnapshot, SessionObs, Snapshot
from .machine import (
    Acknowledgement,
    Evaluation,
    MonitorMemory,
    State,
    evaluate,
    permits_new_risk,
    run_stream,
)
from .report import build_report, render_markdown

__all__ = [
    "Acknowledgement",
    "Evaluation",
    "Expectation",
    "MonitorMemory",
    "MonitoringConfig",
    "OrdersSnapshot",
    "PositionsSnapshot",
    "SessionObs",
    "Snapshot",
    "State",
    "build_report",
    "evaluate",
    "load_monitoring_config",
    "permits_new_risk",
    "render_markdown",
    "run_stream",
]
