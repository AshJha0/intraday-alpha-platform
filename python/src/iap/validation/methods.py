"""The research method bundles: ``"v2"`` (the default), ``"legacy_v1"`` and
the opt-in ``"v3"`` (v1.9 research-validity rules).

Every method choice of the validation chain is an argument somewhere — a
``BacktestConfig`` field, a ``CostModel`` field, a ``validate_alpha`` keyword
— and each has a default and a named legacy value.  A pipeline has to make
all of them together and say which set it made, or two reports that both
claim "the default protocol" can differ in one flag.  This module is that
set, written down once:

==============================  ==========================  ==========================
choice                          ``v2`` (default, v1.5.0)    ``legacy_v1`` (to v1.4.0)
==============================  ==========================  ==========================
row-latency stress grid         version 2 (base config      version 1 (four-field
                                carried)                    rebuild)
backtest position policy        ``cost_aware``              ``sign``
fills                           capped at displayed L1      any size at the touch
rows every IC scores            valid + BLACKOUT rows at    valid labels only
                                the reopen return
rows traded                     the rows the IC scores      every row
impact                          square root                 linear
t-statistic the gate reads      pooled-slope HAC t          within-bucket NW t
PROMOTE t threshold             ledger Bonferroni, >= 3.0   fixed 3.0
capacity                        edge breakeven, capped      participation proxy
per-fold diagnostics/bootstrap  reported                    not computed
recompute leakage probe         run when events exist       not run
==============================  ==========================  ==========================

``"v3"`` (v1.9, opt-in; NOT the default, so no published number moves) is
``v2`` plus the research-validity rules of
:mod:`iap.validation.validate` (R1-R3, R6): the gate reads the
equal-weight per-instrument IC (``gate_ic_source="instrument_mean"``), the
folds are day-aligned (``split_mode="day_aligned"``) and every report
carries the ``validity`` block (day-clustered / day-block-bootstrap t, the
day-separated HAC t, results without FOMC and holiday-thin days).  The
book scope (R4) is a property of the DATA, not of the bundle: pass
``book_scope`` from :func:`iap.validation.sessions.book_scope_for_dataset`.

``ExperimentSpec.configuration["methods"]`` carries the bundle name, so it
is part of an experiment's identity; the report pipelines put it in their
ledger configuration for the same reason — a statistic computed under the
other bundle is another look.  A function-level caller can still mix
choices by hand; a pipeline that feeds the ledger or the lifecycle uses a
bundle.

The drift monitor's z, the retirement rule and the meta-label imputation
are not part of the bundle: they are configured where they live
(``configs/strategies/strategies.json`` ``adaptive``;
``iap.models.metalabel``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from iap.backtest.costs import DEFAULT_IMPACT_MODEL, LEGACY_IMPACT_MODEL, CostModel
from iap.backtest.engine import (
    BLOCK_ROWS_AUTO,
    DEFAULT_POSITION_POLICY,
    LEGACY_POSITION_POLICY,
    BacktestConfig,
)
from iap.labels.frames import DEFAULT_IC_ROWS, LEGACY_IC_ROWS
from iap.validation.splits import DEFAULT_SPLIT_MODE
from iap.validation.stress import STRESS_VERSION_CARRY, STRESS_VERSION_LEGACY
from iap.validation.validate import (
    DEFAULT_CAPACITY,
    DEFAULT_GATE_IC_SOURCE,
    DEFAULT_SIGNIFICANCE,
    DEFAULT_TSTAT_THRESHOLD,
    LEGACY_CAPACITY,
    LEGACY_SIGNIFICANCE,
    LEGACY_TSTAT_THRESHOLD,
    looks_per_validation,
)

__all__ = [
    "DEFAULT_METHODS",
    "METHODS",
    "METHODS_LEGACY",
    "METHODS_V2",
    "METHODS_V3",
    "ResearchMethods",
    "methods",
]

METHODS_V2 = "v2"
METHODS_LEGACY = "legacy_v1"
METHODS_V3 = "v3"
#: The bundle a pipeline uses when it names none.
DEFAULT_METHODS = METHODS_V2


@dataclass(frozen=True)
class ResearchMethods:
    """One row of the table in the module docs."""

    name: str
    ic_rows: str
    stress_version: int
    position_policy: str
    cap_fills_at_l1: bool
    block_invalid_label_rows: bool
    impact_model: str
    significance: str
    tstat_threshold: str
    capacity: str
    fold_diagnostics: bool
    recompute_probe: bool
    split_mode: str = DEFAULT_SPLIT_MODE
    gate_ic_source: str = DEFAULT_GATE_IC_SOURCE
    validity_diagnostics: bool = False

    def backtest_config(self, **fields: Any) -> BacktestConfig:
        """A ``BacktestConfig`` under this bundle's backtest rules;
        ``fields`` are the protocol knobs (latency, decision age, ...)."""
        return BacktestConfig(
            position_policy=self.position_policy,
            cap_fills_at_l1=self.cap_fills_at_l1,
            block_rows_column=BLOCK_ROWS_AUTO if self.block_invalid_label_rows else None,
            **fields,
        )

    def cost_model(self, cost_model: CostModel) -> CostModel:
        """``cost_model`` under this bundle's impact rule (coefficients and
        multiplier unchanged)."""
        if cost_model.impact_model == self.impact_model:
            return cost_model
        return replace(cost_model, impact_model=self.impact_model)

    def validate_kwargs(self) -> dict[str, Any]:
        """The method keywords of :func:`iap.validation.validate.validate_alpha`
        (the ledger threshold, the seed and the recompute source are the
        caller's)."""
        kw: dict[str, Any] = {
            "ic_rows": self.ic_rows,
            "tstat_threshold": self.tstat_threshold,
            "stress_version": self.stress_version,
            "significance": self.significance,
            "capacity": self.capacity,
            "fold_diagnostics": self.fold_diagnostics,
        }
        # v1.9 options only when they differ from the default, so the v2 and
        # legacy keyword sets are exactly what they were.
        if self.split_mode != DEFAULT_SPLIT_MODE:
            kw["split_mode"] = self.split_mode
        if self.gate_ic_source != DEFAULT_GATE_IC_SOURCE:
            kw["gate_ic_source"] = self.gate_ic_source
        if self.validity_diagnostics:
            kw["validity_diagnostics"] = True
        return kw

    def looks(self, n_folds: int) -> int:
        """Looks one validation plus the caller's one out-of-sample backtest
        debits (:func:`iap.validation.validate.looks_per_validation` + 1):
        84 under ``v2`` at four folds, 28 under ``legacy_v1``."""
        return looks_per_validation(n_folds, self.fold_diagnostics, self.validity_diagnostics) + 1


METHODS: dict[str, ResearchMethods] = {
    METHODS_V2: ResearchMethods(
        name=METHODS_V2,
        ic_rows=DEFAULT_IC_ROWS,
        stress_version=STRESS_VERSION_CARRY,
        position_policy=DEFAULT_POSITION_POLICY,
        cap_fills_at_l1=True,
        block_invalid_label_rows=True,
        impact_model=DEFAULT_IMPACT_MODEL,
        significance=DEFAULT_SIGNIFICANCE,
        tstat_threshold=DEFAULT_TSTAT_THRESHOLD,
        capacity=DEFAULT_CAPACITY,
        fold_diagnostics=True,
        recompute_probe=True,
    ),
    METHODS_LEGACY: ResearchMethods(
        name=METHODS_LEGACY,
        ic_rows=LEGACY_IC_ROWS,
        stress_version=STRESS_VERSION_LEGACY,
        position_policy=LEGACY_POSITION_POLICY,
        cap_fills_at_l1=False,
        block_invalid_label_rows=False,
        impact_model=LEGACY_IMPACT_MODEL,
        significance=LEGACY_SIGNIFICANCE,
        tstat_threshold=LEGACY_TSTAT_THRESHOLD,
        capacity=LEGACY_CAPACITY,
        fold_diagnostics=False,
        recompute_probe=False,
    ),
    METHODS_V3: ResearchMethods(
        name=METHODS_V3,
        ic_rows=DEFAULT_IC_ROWS,
        stress_version=STRESS_VERSION_CARRY,
        position_policy=DEFAULT_POSITION_POLICY,
        cap_fills_at_l1=True,
        block_invalid_label_rows=True,
        impact_model=DEFAULT_IMPACT_MODEL,
        significance=DEFAULT_SIGNIFICANCE,
        tstat_threshold=DEFAULT_TSTAT_THRESHOLD,
        capacity=DEFAULT_CAPACITY,
        fold_diagnostics=True,
        recompute_probe=True,
        split_mode="day_aligned",
        gate_ic_source="instrument_mean",
        validity_diagnostics=True,
    ),
}


def methods(name: str = DEFAULT_METHODS) -> ResearchMethods:
    """The bundle called ``name`` (``ValueError`` naming the known ones)."""
    try:
        return METHODS[name]
    except KeyError:
        raise ValueError(f"unknown methods bundle {name!r}; known: {sorted(METHODS)}") from None
