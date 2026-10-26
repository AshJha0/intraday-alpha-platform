"""``iap.metrics`` — the Python ``/metrics`` process (v1.17 WS-F item F9).

Replaces the Java ``/metrics`` container retired with DECISION_N7
(``archive/docs/release/DECISION_N7_JAVA_IMAGE.md``): a stdlib-only Prometheus text
exposition (:mod:`iap.metrics.registry`, the format the retired
``com.iap.monitoring`` registry rendered), a file-backed exporter over the
configs, the session marker and the newest MVP run
(:mod:`iap.metrics.exporter`), an HTTP server with ``/metrics``, ``/health``,
``/ready`` and ``/status`` (:mod:`iap.metrics.server`) and the
``python -m iap.metrics {serve,run-session,names}`` CLI.

Metric names: every name the Java container exposed is either LIVE (a Python
producer: :data:`~iap.metrics.exporter.LIVE_METRICS`) or RETIRED (exposed as
``<name>_available 0``: :data:`~iap.metrics.exporter.RETIRED_METRICS`); the
lists are the producer contract ``tests/harness/check_deployment.py`` reads.
"""

from iap.metrics.exporter import (
    LIVE_METRICS,
    RETIRED_METRICS,
    PlatformExporter,
    read_session_marker,
    write_session_marker,
)
from iap.metrics.registry import MetricsRegistry

__all__ = [
    "LIVE_METRICS",
    "RETIRED_METRICS",
    "MetricsRegistry",
    "PlatformExporter",
    "read_session_marker",
    "write_session_marker",
]
