"""Versioned, hashed monitoring policy (``configs/monitoring/policy.json``).

Loading is fail-fast: a wrong ``x-version``, a missing or unknown key, or a
value of the wrong type or range raises ``ValueError`` naming the key.  The
policy's identity is :attr:`MonitoringConfig.sha256`, the SHA-256 of the
document's canonical JSON (sorted keys, no whitespace), stamped on every
evaluation and report so a state can always be traced to the exact policy
that produced it.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "CHANNELS",
    "DEFAULT_POLICY_PATH",
    "MONITORING_CONFIG_VERSION",
    "MonitoringConfig",
    "canonical_sha256",
    "load_monitoring_config",
]

MONITORING_CONFIG_VERSION = 1

#: Input channels with a declared freshness (seconds).
CHANNELS = ("sessions", "positions", "orders", "heartbeat")

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[4] / "configs" / "monitoring" / "policy.json"

_TOP_KEYS = (
    "x-version",
    "policy_id",
    "description",
    "freshness_s",
    "window_sessions",
    "min_sessions",
    "data_quality",
    "cusum",
    "band",
    "turnover",
    "drawdown",
    "exposure",
    "fill",
    "reconciliation",
    "persistence",
    "clear_after",
    "unknown_escalate_after",
    "declared",
)


def canonical_sha256(doc: Mapping[str, Any]) -> str:
    """SHA-256 of ``doc`` as canonical JSON (sorted keys, compact)."""
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


def _keys(block: Any, expected: tuple[str, ...], where: str) -> Mapping[str, Any]:
    if not isinstance(block, Mapping):
        raise ValueError(f"{where}: expected an object")
    missing = [k for k in expected if k not in block]
    extra = sorted(set(block) - set(expected))
    if missing or extra:
        raise ValueError(f"{where}: missing keys {missing}, unknown keys {extra}")
    return block


def _num(block: Mapping[str, Any], key: str, where: str, lo: float = 0.0) -> float:
    v = block[key]
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        raise ValueError(f"{where}.{key}: expected a finite number")
    if v < lo:
        raise ValueError(f"{where}.{key}: must be >= {lo}")
    return float(v)


def _int(block: Mapping[str, Any], key: str, where: str, lo: int = 1) -> int:
    v = block[key]
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise ValueError(f"{where}.{key}: expected an integer >= {lo}")
    return int(v)


def _pair(block: Mapping[str, Any], key: str, where: str) -> tuple[float, float]:
    v = block[key]
    if (
        not isinstance(v, list)
        or len(v) != 2
        or not all(isinstance(x, int | float) and not isinstance(x, bool) for x in v)
        or not 0.0 <= v[0] < v[1]
    ):
        raise ValueError(f"{where}.{key}: expected [lo, hi] with 0 <= lo < hi")
    return float(v[0]), float(v[1])


@dataclass(frozen=True)
class MonitoringConfig:
    """The validated policy.  ``doc`` keeps the raw document; ``sha256`` its hash."""

    policy_id: str
    sha256: str
    freshness_s: dict[str, float]
    window_sessions: int
    min_sessions: int
    invalid_below_completeness: float
    watch_below_completeness: float
    cusum_k: float
    cusum_h_watch: float
    cusum_h_review: float
    band_watch_z: float
    band_review_z: float
    upside_watch_z: float
    turnover_watch: tuple[float, float]
    turnover_review: tuple[float, float]
    drawdown_watch_frac: float
    drawdown_review_frac: float
    exposure_watch_utilisation: float
    fill_watch_shortfall: float
    fill_review_shortfall: float
    recon_halt_after: int
    persistence: dict[str, int]
    clear_after: int
    unknown_escalate_after: int
    declared: dict[str, float]
    doc: Mapping[str, Any]

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any], where: str = "monitoring policy") -> MonitoringConfig:
        _keys(doc, _TOP_KEYS, where)
        if doc["x-version"] != MONITORING_CONFIG_VERSION:
            raise ValueError(
                f"{where}: x-version {doc['x-version']!r} != {MONITORING_CONFIG_VERSION}"
            )
        if not isinstance(doc["policy_id"], str) or not doc["policy_id"]:
            raise ValueError(f"{where}.policy_id: expected a non-empty string")
        fr = _keys(doc["freshness_s"], CHANNELS, f"{where}.freshness_s")
        dq = _keys(
            doc["data_quality"],
            ("invalid_below_completeness", "watch_below_completeness"),
            f"{where}.data_quality",
        )
        cu = _keys(doc["cusum"], ("k", "h_watch", "h_review"), f"{where}.cusum")
        bd = _keys(doc["band"], ("watch_z", "review_z", "upside_watch_z"), f"{where}.band")
        to = _keys(doc["turnover"], ("watch_ratio", "review_ratio"), f"{where}.turnover")
        dd = _keys(doc["drawdown"], ("watch_frac", "review_frac"), f"{where}.drawdown")
        ex = _keys(doc["exposure"], ("watch_utilisation",), f"{where}.exposure")
        fl = _keys(doc["fill"], ("watch_shortfall", "review_shortfall"), f"{where}.fill")
        rc = _keys(doc["reconciliation"], ("halt_after_evaluations",), f"{where}.reconciliation")
        pe = _keys(doc["persistence"], ("WATCH", "REVIEW", "HALT"), f"{where}.persistence")
        de = _keys(
            doc["declared"],
            (
                "horizon_sessions",
                "false_alarm_rate_review",
                "planted_decay_sd",
                "max_detection_delay_sessions",
                "detection_quantile",
            ),
            f"{where}.declared",
        )
        cfg = cls(
            policy_id=doc["policy_id"],
            sha256=canonical_sha256(doc),
            freshness_s={c: _num(fr, c, f"{where}.freshness_s", 1.0) for c in CHANNELS},
            window_sessions=_int(doc, "window_sessions", where, 2),
            min_sessions=_int(doc, "min_sessions", where, 3),
            invalid_below_completeness=_num(dq, "invalid_below_completeness", where),
            watch_below_completeness=_num(dq, "watch_below_completeness", where),
            cusum_k=_num(cu, "k", f"{where}.cusum"),
            cusum_h_watch=_num(cu, "h_watch", f"{where}.cusum"),
            cusum_h_review=_num(cu, "h_review", f"{where}.cusum"),
            band_watch_z=_num(bd, "watch_z", f"{where}.band"),
            band_review_z=_num(bd, "review_z", f"{where}.band"),
            upside_watch_z=_num(bd, "upside_watch_z", f"{where}.band"),
            turnover_watch=_pair(to, "watch_ratio", f"{where}.turnover"),
            turnover_review=_pair(to, "review_ratio", f"{where}.turnover"),
            drawdown_watch_frac=_num(dd, "watch_frac", f"{where}.drawdown"),
            drawdown_review_frac=_num(dd, "review_frac", f"{where}.drawdown"),
            exposure_watch_utilisation=_num(ex, "watch_utilisation", f"{where}.exposure"),
            fill_watch_shortfall=_num(fl, "watch_shortfall", f"{where}.fill"),
            fill_review_shortfall=_num(fl, "review_shortfall", f"{where}.fill"),
            recon_halt_after=_int(rc, "halt_after_evaluations", f"{where}.reconciliation"),
            persistence={
                k: _int(pe, k, f"{where}.persistence") for k in ("WATCH", "REVIEW", "HALT")
            },
            clear_after=_int(doc, "clear_after", where),
            unknown_escalate_after=_int(doc, "unknown_escalate_after", where),
            declared={k: _num(de, k, f"{where}.declared") for k in de},
            doc=doc,
        )
        if cfg.min_sessions > cfg.window_sessions:
            raise ValueError(f"{where}: min_sessions must be <= window_sessions")
        if not cfg.cusum_h_watch < cfg.cusum_h_review:
            raise ValueError(f"{where}.cusum: need h_watch < h_review")
        if not cfg.band_watch_z < cfg.band_review_z:
            raise ValueError(f"{where}.band: need watch_z < review_z")
        if not cfg.drawdown_watch_frac < cfg.drawdown_review_frac < 1.0:
            raise ValueError(f"{where}.drawdown: need watch_frac < review_frac < 1")
        if not cfg.fill_watch_shortfall < cfg.fill_review_shortfall:
            raise ValueError(f"{where}.fill: need watch_shortfall < review_shortfall")
        if not cfg.invalid_below_completeness < cfg.watch_below_completeness <= 1.0:
            raise ValueError(f"{where}.data_quality: need invalid < watch <= 1")
        lo_w, hi_w = cfg.turnover_watch
        lo_r, hi_r = cfg.turnover_review
        if not (lo_r < lo_w and hi_w < hi_r):
            raise ValueError(f"{where}.turnover: review band must contain the watch band")
        return cfg


def load_monitoring_config(path: Path | None = None) -> MonitoringConfig:
    """Load and validate the policy (default: ``configs/monitoring/policy.json``)."""
    p = Path(path) if path is not None else DEFAULT_POLICY_PATH
    if not p.is_file():
        raise ValueError(f"{p}: monitoring policy not found")
    with open(p, encoding="utf-8") as fh:
        doc = json.load(fh)
    return MonitoringConfig.from_doc(doc, str(p))
