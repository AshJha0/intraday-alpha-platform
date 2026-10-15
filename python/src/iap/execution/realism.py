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

**v1.17 (review X-2): one entry point, and mean means mean.**  A loop that
runs under a prereg calls :func:`apply` once and reads everything it needs
from the :class:`AppliedRealism` it returns - the exec config, the
calibration (with the prereg latency table on every venue, so a venue
latency a calibration would otherwise override is never silently lost),
the simulator / calibration / maker / quoting keyword arguments, the
stress and fee-tier grids - together with ``applied``, which states per
key whether it was applied, not set, or not applied and why.  Until v1.16
each key had its own applier, two had no caller at all (``latency_stress``,
``fee_tier_sensitivity``) and the MVP left the impact rule out without
saying so in one place.  The simulator draws a venue's latency as
``latency_mean_ns + U[0, latency_jitter_ns]``, so the prereg ``mean_ns``
was in fact the MINIMUM; :meth:`RealismOptIns.exec_config` and
:meth:`latency_tail` now place the prereg mean at ``mean_ns`` (the
simulator base is ``mean_ns - jitter_ns / 2``), which moves every v3 run's
latency draws down by half the jitter.

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

__all__ = [
    "FEE_TIERS_PATH",
    "V116_KEYS",
    "AppliedRealism",
    "RealismOptIns",
    "apply",
    "load_fee_tiers",
    "load_prereg",
    "simulator_latency_base_ns",
]

#: The v1.16 P0-3 keys :func:`apply` reports on (``applied``).
V116_KEYS = (
    "venue_latency",
    "latency_stress",
    "fee_tier",
    "fee_tier_sensitivity",
    "exec_prints",
    "impact_model",
    "impact_shape",
)


def simulator_latency_base_ns(mean_ns: int, jitter_ns: int) -> int:
    """The ``latency_mean_ns`` the simulator needs so that ``base + U[0,
    jitter]`` has mean ``mean_ns`` (v1.17, X-2): ``mean_ns - jitter_ns / 2``,
    never below 0."""
    return max(0, int(round(int(mean_ns) - int(jitter_ns) / 2.0)))


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
            simulator_latency_base_ns(vl["mean_ns"], vl["jitter_ns"]),
            vl["jitter_ns"],
            float(vl.get("tail_prob", 0.01)),
            float(vl.get("tail_mult", 10.0)),
        )

    def _with_fees(self, cfg, tier: str):
        assert self.fee_tiers is not None  # loaded by __post_init__ with fee_tier
        row = self.fee_tiers[tier]
        venues = {vid: v if v.is_fx else replace(v, **row) for vid, v in cfg.venues.items()}
        return replace(cfg, venues=venues)

    def _with_latency(self, cfg, mult: float = 1.0):
        vl = self.venue_latency
        venues = {}
        for vid, v in cfg.venues.items():
            # The prereg mean_ns is the MEAN of the simulator's base + U[0,
            # jitter] draw (v1.17, X-2); a config value is already a base.
            mean = (
                simulator_latency_base_ns(vl["mean_ns"], vl["jitter_ns"])
                if vl is not None
                else v.latency_mean_ns
            )
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

            assert self.impact_shape is not None  # __post_init__: required with 'parent'
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


@dataclass(frozen=True)
class AppliedRealism:
    """What :func:`apply` produced for one loop (module docs, v1.17)."""

    prereg: RealismOptIns
    exec_config: Any
    calibration: Any
    simulator_kwargs: dict[str, Any]
    calibration_kwargs: dict[str, Any]
    exec_config_kwargs: dict[str, Any]
    maker_config: Any
    quoting_config: Any
    #: ``{multiplier: ExecConfig}`` (``latency_stress``; ``{1: exec_config}`` without a grid)
    stressed: dict[float, Any]
    #: ``{tier: ExecConfig}`` (``fee_tier`` + ``fee_tier_sensitivity``; empty without a tier)
    fee_tiers: dict[str, Any]
    #: per v1.16 key: ``"applied"``, ``"not set"`` or ``"not applied: <why>"``
    applied: dict[str, str]

    def not_applied(self) -> dict[str, str]:
        """The keys that are set but were not applied, with the reason."""
        return {k: v for k, v in self.applied.items() if v.startswith("not applied")}


def apply(
    prereg: RealismOptIns,
    exec_config,
    *,
    calibration=None,
    impact: bool = True,
    maker_config=None,
    quoting_config=None,
) -> AppliedRealism:
    """Apply every opt-in of ``prereg`` to one loop's inputs at once (module
    docs, v1.17 X-2).

    ``exec_config`` gets the venue latency (as a mean), the fee tier and -
    with ``impact`` and a ``calibration`` carrying a residual fit - the
    parent impact rule.  ``calibration``, when given and the prereg sets
    ``venue_latency``, gets the prereg's parametric latency table on EVERY
    venue of ``exec_config`` (the simulator reads the calibration's table
    before the venue config, so without this the prereg latency would be
    silently replaced by whatever the calibration measured).  ``applied``
    says what happened to each key; a caller that cannot apply a key passes
    ``impact=False`` and records the reason it finds there."""
    applied: dict[str, str] = {}
    cfg = exec_config
    if prereg.venue_latency is not None:
        cfg = prereg._with_latency(cfg)
        applied["venue_latency"] = "applied"
    else:
        applied["venue_latency"] = "not set"
    if prereg.fee_tier is not None:
        cfg = prereg._with_fees(cfg, prereg.fee_tier)
        applied["fee_tier"] = "applied"
    else:
        applied["fee_tier"] = "not set"
    if prereg.impact_model == "parent":
        if not impact:
            applied["impact_model"] = applied["impact_shape"] = (
                "not applied: the caller runs no execution calibration (rule 6 stays)"
            )
        elif calibration is None:
            raise ValueError("prereg impact_model 'parent' needs a calibration with impact_fit")
        else:
            from iap.execution.calibration import apply_impact_fit

            assert prereg.impact_shape is not None
            cfg = apply_impact_fit(cfg, calibration, prereg.impact_shape)
            applied["impact_model"] = applied["impact_shape"] = "applied"
    else:
        applied["impact_model"] = applied["impact_shape"] = "not set"
    cal = calibration
    if cal is not None and prereg.venue_latency is not None:
        from iap.execution.calibration import ExecCalibration, LatencyTable

        table = prereg.latency_tail()
        assert table is not None
        lat = {
            int(vid): LatencyTable(
                tuple(table["probs"]), tuple(int(q) for q in table["quantiles_ns"])
            )
            for vid in cfg.venues
        }
        doc = dict(cal.doc)
        doc["latency"] = {str(vid): dict(table) for vid in lat}
        cal = ExecCalibration(
            doc=doc, latency=lat, impact_coeff_bps_per_pct_adv=cal.impact_coeff_bps_per_pct_adv
        )
        applied["venue_latency"] = "applied (config and calibration latency table)"
    stressed = {m: prereg._with_latency(cfg, m) for m in (prereg.latency_stress or (1,))}
    applied["latency_stress"] = (
        f"applied: {len(stressed)} configs" if prereg.latency_stress else "not set"
    )
    tiers = (
        {t: prereg._with_fees(cfg, t) for t in (prereg.fee_tier, *prereg.fee_tier_sensitivity)}
        if prereg.fee_tier is not None
        else {}
    )
    applied["fee_tier_sensitivity"] = (
        f"applied: {len(tiers)} configs" if prereg.fee_tier_sensitivity else "not set"
    )
    applied["exec_prints"] = "applied" if prereg.exec_prints else "not set"
    return AppliedRealism(
        prereg=prereg,
        exec_config=cfg,
        calibration=cal,
        simulator_kwargs=prereg.simulator_kwargs(),
        calibration_kwargs=prereg.calibration_kwargs(),
        exec_config_kwargs=prereg.exec_config_kwargs(),
        maker_config=None if maker_config is None else prereg.maker_config(maker_config),
        quoting_config=None if quoting_config is None else prereg.quoting_config(quoting_config),
        stressed=stressed,
        fee_tiers=tiers,
        applied=applied,
    )
