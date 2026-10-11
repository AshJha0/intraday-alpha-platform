"""v1.15 R-P0-2: the execution-realism opt-ins a pre-registration turns on.

Every option below defaults OFF in code (the v1 numbers and every golden are
unchanged); ``configs/prereg/v2.json`` turns them on for the v2
pre-registered runs (``execution_realism`` block), never retroactively.

=========================  ===========================================  =========
prereg key                 where it lands                               v1 / v2
=========================  ===========================================  =========
``regulatory_fees``        ``load_exec_config(regulatory_fees=)``       off / on
``feed_latency_ns``        ``MakerConfig`` / ``QuotingConfig``          0 / >0
``exit_route``             ``MakerConfig.exit_route``                   book/sim
``flatten_route``          ``QuotingConfig.flatten_route``              book/sim
``hidden_print_fills``     ``MakerConfig`` / ``QuotingConfig``          off / on
``amend_size_down``        ``QuotingConfig.amend_size_down``            off / on
``markout_unit``           ``estimate_calibration(markout_unit=)``      message /
                                                                        touch_order
``modify_queue_credit``    always on in every engine since v1.14        on / on
=========================  ===========================================  =========

``modify_queue_credit`` (v1.14 X-P1-1, a shrinking MODIFY ahead of a resting
child credits its queue position) has no switch: it shipped on in the C++,
Rust and Python simulators with byte-identical goldens. The prereg records it
so the v2 run states every realism assumption; a value other than ``true``
is refused rather than silently ignored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from iap.execution.regulatory import prereg_wants_regulatory_fees

__all__ = ["RealismOptIns", "load_prereg"]

_ROUTES = ("book", "sim")
_UNITS = ("message", "touch_order")


@dataclass(frozen=True)
class RealismOptIns:
    """The realism switches of one pre-registration (defaults = v1, all off)."""

    regulatory_fees: bool = False
    feed_latency_ns: int = 0
    exit_route: str = "book"
    flatten_route: str = "book"
    hidden_print_fills: bool = False
    amend_size_down: bool = False
    markout_unit: str = "message"
    modify_queue_credit: bool = True

    def __post_init__(self) -> None:
        for name in ("regulatory_fees", "hidden_print_fills", "amend_size_down"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"prereg '{name}' must be a bool")
        lat = self.feed_latency_ns
        if isinstance(lat, bool) or not isinstance(lat, int) or lat < 0:
            raise ValueError("prereg 'feed_latency_ns' must be an int >= 0")
        for name in ("exit_route", "flatten_route"):
            if getattr(self, name) not in _ROUTES:
                raise ValueError(f"prereg '{name}' must be one of {_ROUTES}")
        if self.markout_unit not in _UNITS:
            raise ValueError(f"prereg 'markout_unit' must be one of {_UNITS}")
        if self.modify_queue_credit is not True:
            raise ValueError(
                "prereg 'modify_queue_credit' must be true: the MODIFY queue "
                "credit is always on since v1.14 and has no switch"
            )

    @classmethod
    def from_prereg(cls, prereg: dict[str, Any]) -> RealismOptIns:
        """Read a prereg document; absent keys keep the v1 default."""
        block = prereg.get("execution_realism", {})
        if not isinstance(block, dict):
            raise ValueError("prereg 'execution_realism' must be an object")
        known = {f.name for f in fields(cls)} - {"regulatory_fees"}
        unknown = sorted(set(block) - known - {"description"})
        if unknown:
            raise ValueError(f"prereg 'execution_realism' has unknown keys {unknown}")
        kw = {k: v for k, v in block.items() if k in known}
        return cls(regulatory_fees=prereg_wants_regulatory_fees(prereg), **kw)

    def maker_config(self, cfg):
        """``cfg`` (a ``MakerConfig``) with this prereg's options."""
        return replace(
            cfg,
            feed_latency_ns=self.feed_latency_ns,
            exit_route=self.exit_route,
            hidden_print_fills=self.hidden_print_fills,
        )

    def quoting_config(self, cfg):
        """``cfg`` (a ``QuotingConfig``) with this prereg's options."""
        return replace(
            cfg,
            feed_latency_ns=self.feed_latency_ns,
            flatten_route=self.flatten_route,
            amend_size_down=self.amend_size_down,
            hidden_print_fills=self.hidden_print_fills,
        )

    def exec_config_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for :func:`iap.execution.config.load_exec_config`."""
        return {"regulatory_fees": self.regulatory_fees}

    def simulator_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for :class:`iap.execution.ExecutionSimulator`."""
        return {"hidden_prints": self.hidden_print_fills}

    def calibration_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``estimate_calibration``."""
        return {"markout_unit": self.markout_unit}


def load_prereg(path) -> RealismOptIns:
    """The opt-ins of a prereg file (``configs/prereg/v2.json``)."""
    return RealismOptIns.from_prereg(json.loads(Path(path).read_text(encoding="utf-8")))
