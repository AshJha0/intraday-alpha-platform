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

v1.16 P0-3 adds the keys a NET-OF-COST prereg must fix before any look
(all absent in v2, so v2 runs are unchanged; ``configs/prereg/v3.json``):

=========================  ===========================================  =========
prereg key                 where it lands                               default
=========================  ===========================================  =========
``venue_latency``          :meth:`RealismOptIns.exec_config` (every     None
                           venue's ``latency_mean_ns``/``jitter_ns``)
                           + :meth:`latency_tail` (calibration
                           ``latency_tail``, ``parametric_latency``)
``latency_stress``         :meth:`stressed_exec_configs`: venue leg     ()
                           scaled by each multiplier ({0, 1, 5})
``fee_tier``               :meth:`exec_config`: equity venues' taker    None
                           fee / maker rebate from the fee-tier table
``fee_tier_sensitivity``   :meth:`fee_tier_configs`: the same run at    ()
                           each named lower tier (reported only)
``exec_prints``            ``ExecutionSimulator(exec_print_price=)``    off
                           and ``Itch50Mapper(exec_prints=)`` (#115)
``impact_model``           :meth:`exec_config` (rule 6 / 6b)            "child"
``impact_shape``           :func:`~iap.execution.calibration.           None
                           apply_impact_fit` shape (parent model)
=========================  ===========================================  =========

The fee-tier table (``configs/reference/fee_tiers.json``) holds ASSUMED
per-share schedules for sensitivity, not a quoted exchange price list.

``modify_queue_credit`` (v1.14 X-P1-1, a shrinking MODIFY ahead of a resting
child credits its queue position) has no switch: it shipped on in the C++,
Rust and Python simulators with byte-identical goldens. The prereg records it
so the v2 run states every realism assumption; a value other than ``true``
is refused rather than silently ignored.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from iap.execution.regulatory import prereg_wants_regulatory_fees

__all__ = ["FEE_TIERS_PATH", "RealismOptIns", "load_fee_tiers", "load_prereg"]

_ROUTES = ("book", "sim")
_UNITS = ("message", "touch_order")
_IMPACT_MODELS = ("child", "parent")
_IMPACT_SHAPES = ("linear", "sqrt", "power")
_VENUE_LATENCY_KEYS = {"mean_ns", "jitter_ns", "tail_prob", "tail_mult"}

#: The assumed fee-tier table ``fee_tier`` names refer to.
FEE_TIERS_PATH = Path(__file__).resolve().parents[4] / "configs" / "reference" / "fee_tiers.json"


def load_fee_tiers(path=None) -> dict[str, dict[str, float]]:
    """``{tier: {"taker_fee_per_share", "maker_rebate_per_share"}}``."""
    doc = json.loads(Path(path or FEE_TIERS_PATH).read_text(encoding="utf-8"))
    tiers = doc.get("tiers")
    if not isinstance(tiers, dict) or not tiers:
        raise ValueError("fee-tier table must have a non-empty 'tiers' object")
    out: dict[str, dict[str, float]] = {}
    for name, row in tiers.items():
        taker, rebate = row.get("taker_fee_per_share"), row.get("maker_rebate_per_share")
        if not all(isinstance(v, (int, float)) and v >= 0 for v in (taker, rebate)):
            raise ValueError(f"fee tier {name!r}: fees must be numbers >= 0")
        out[name] = {"taker_fee_per_share": float(taker), "maker_rebate_per_share": float(rebate)}
    return out


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
    # ---- v1.16 P0-3 (absent = unchanged) ----
    venue_latency: Mapping[str, Any] | None = None
    latency_stress: tuple[float, ...] = ()
    fee_tier: str | None = None
    fee_tier_sensitivity: tuple[str, ...] = ()
    exec_prints: bool = False
    impact_model: str = "child"
    impact_shape: str | None = None
    fee_tiers: Mapping[str, Mapping[str, float]] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "latency_stress", tuple(self.latency_stress))
        object.__setattr__(self, "fee_tier_sensitivity", tuple(self.fee_tier_sensitivity))
        self._check_v116()
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

    def _check_v116(self) -> None:
        vl = self.venue_latency
        if vl is not None:
            if not isinstance(vl, Mapping) or not {"mean_ns", "jitter_ns"} <= set(vl):
                raise ValueError("prereg 'venue_latency' needs mean_ns and jitter_ns")
            extra = sorted(set(vl) - _VENUE_LATENCY_KEYS)
            if extra:
                raise ValueError(f"prereg 'venue_latency' has unknown keys {extra}")
            for k in ("mean_ns", "jitter_ns"):
                v = vl[k]
                if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                    raise ValueError(f"prereg 'venue_latency.{k}' must be an int >= 0")
            self.latency_tail()  # validates tail_prob / tail_mult
        for m in self.latency_stress:
            if isinstance(m, bool) or not isinstance(m, (int, float)) or m < 0:
                raise ValueError("prereg 'latency_stress' multipliers must be numbers >= 0")
        if self.latency_stress and 1 not in self.latency_stress:
            raise ValueError("prereg 'latency_stress' must include the 1x base case")
        if not isinstance(self.exec_prints, bool):
            raise ValueError("prereg 'exec_prints' must be a bool")
        if self.impact_model not in _IMPACT_MODELS:
            raise ValueError(f"prereg 'impact_model' must be one of {_IMPACT_MODELS}")
        if self.impact_shape is not None and self.impact_shape not in _IMPACT_SHAPES:
            raise ValueError(f"prereg 'impact_shape' must be one of {_IMPACT_SHAPES}")
        if (self.impact_shape is None) != (self.impact_model == "child"):
            raise ValueError("prereg 'impact_shape' is required with, and only with, 'parent'")
        names = [self.fee_tier, *self.fee_tier_sensitivity]
        if self.fee_tier_sensitivity and self.fee_tier is None:
            raise ValueError("prereg 'fee_tier_sensitivity' needs a 'fee_tier'")
        if self.fee_tier is not None:
            tiers = self.fee_tiers if self.fee_tiers is not None else load_fee_tiers()
            object.__setattr__(self, "fee_tiers", dict(tiers))
            missing = [n for n in names if n not in tiers]
            if missing:
                raise ValueError(f"prereg fee tiers {missing} are not in the fee-tier table")

    # ---- v1.16 P0-3 appliers ----

    def latency_tail(self) -> dict | None:
        """The ``parametric_latency`` table of ``venue_latency`` (or None)."""
        if self.venue_latency is None:
            return None
        from iap.execution.calibration import parametric_latency

        vl = self.venue_latency
        return parametric_latency(
            vl["mean_ns"],
            vl["jitter_ns"],
            float(vl.get("tail_prob", 0.01)),
            float(vl.get("tail_mult", 10.0)),
        )

    def _with_fees(self, cfg, tier: str):
        row = self.fee_tiers[tier]
        venues = {vid: v if v.is_fx else replace(v, **row) for vid, v in cfg.venues.items()}
        return replace(cfg, venues=venues)

    def _with_latency(self, cfg, mult: float = 1.0):
        vl = self.venue_latency
        venues = {}
        for vid, v in cfg.venues.items():
            mean = vl["mean_ns"] if vl is not None else v.latency_mean_ns
            jit = vl["jitter_ns"] if vl is not None else v.latency_jitter_ns
            venues[vid] = replace(
                v, latency_mean_ns=int(round(mean * mult)), latency_jitter_ns=int(round(jit * mult))
            )
        return replace(cfg, venues=venues)

    def exec_config(self, cfg, calibration=None, *, impact: bool = True):
        """``cfg`` (an ``ExecConfig``) with this prereg's venue latency, fee
        tier and impact rule. A ``parent`` impact model needs the residual
        impact fit of ``calibration`` (``estimate_calibration(impact_fit=True)``);
        ``impact=False`` leaves the impact rule alone (a loop with no
        calibration, e.g. the MVP, records it as not applied)."""
        if self.venue_latency is not None:
            cfg = self._with_latency(cfg)
        if self.fee_tier is not None:
            cfg = self._with_fees(cfg, self.fee_tier)
        if impact and self.impact_model == "parent":
            if calibration is None:
                raise ValueError("prereg impact_model 'parent' needs a calibration with impact_fit")
            from iap.execution.calibration import apply_impact_fit

            cfg = apply_impact_fit(cfg, calibration, self.impact_shape)
        return cfg

    def stressed_exec_configs(self, cfg, calibration=None) -> dict[float, Any]:
        """``{multiplier: config}`` with the venue leg scaled (the latency
        stress grid); ``{1: exec_config(cfg)}`` when no grid is set."""
        base = self.exec_config(cfg, calibration)
        return {m: self._with_latency(base, m) for m in (self.latency_stress or (1,))}

    def fee_tier_configs(self, cfg, calibration=None) -> dict[str, Any]:
        """``{tier: config}`` for the prereg tier and each sensitivity tier."""
        base = self.exec_config(cfg, calibration)
        if self.fee_tier is None:
            return {}
        return {t: self._with_fees(base, t) for t in (self.fee_tier, *self.fee_tier_sensitivity)}

    @classmethod
    def from_prereg(cls, prereg: dict[str, Any]) -> RealismOptIns:
        """Read a prereg document; absent keys keep the v1 default."""
        block = prereg.get("execution_realism", {})
        if not isinstance(block, dict):
            raise ValueError("prereg 'execution_realism' must be an object")
        known = {f.name for f in fields(cls)} - {"regulatory_fees", "fee_tiers"}
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
        kw: dict[str, Any] = {"hidden_prints": self.hidden_print_fills}
        if self.exec_prints:
            kw["exec_print_price"] = True
        return kw

    def calibration_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``estimate_calibration``."""
        kw: dict[str, Any] = {"markout_unit": self.markout_unit}
        if self.impact_model == "parent":
            kw["impact_fit"] = True
        return kw


def load_prereg(path) -> RealismOptIns:
    """The opt-ins of a prereg file (``configs/prereg/v2.json``)."""
    return RealismOptIns.from_prereg(json.loads(Path(path).read_text(encoding="utf-8")))
