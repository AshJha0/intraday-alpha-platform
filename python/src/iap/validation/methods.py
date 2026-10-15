"""The research method bundles: ``"v2"`` (the default), ``"legacy_v1"`` and
the opt-in ``"v3"`` (v1.9 research-validity rules) and ``"v4"`` (v1.12
overfitting statistics), each at the v1.17 statistics revision, plus the
frozen v1.14.0 revisions ``"v2_v114"``, ``"v3_v114"`` and ``"v4_v114"`` and
the frozen v1.13.0 revisions ``"v2_v113"``, ``"v3_v113"`` and ``"v4_v113"``.

**Statistics revision v1.17 (the 2026-10-15 review, R-2 / R-3).**  ``v2``,
``v3`` and ``v4`` now pass ``feature_window_ns="registry"`` to
``validate_alpha``: the embargo AFTER a test block on every two-sided split
(``leave_one_day_out`` folds and the ``cpcv`` block) is at least the longest
feature look-back window in the registry (5 m today), not the 60 s
``embargo_ns`` alone (R-P1-6 was implemented in v1.13 and passed by no
caller).  The expanding walk-forward is one-sided, so a ``v2`` verdict and
every published ``v2`` number are unchanged; the ``cpcv`` block of ``v4``
moves, and reports state ``methods.post_block_embargo_ns``.  In the same
revision the gate statistic is computed by one function
(:func:`iap.validation.metrics.gate_statistics`) for the walk-forward gate,
``frozen_params_block``, the ``validity`` block and ``tools/r7_confirm.py``,
with the day-cluster t beside the HAC t in every gate.  The v1.14.0 rules
are frozen as ``*_v114`` and the ledger identity carries
``stats_revision: "v1.17"``, exactly as v1.14.0 did with ``*_v113``.

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

``"v4"`` (v1.12, opt-in) is ``v3`` plus the report-only overfitting
statistics of plan item R7: the ``cpcv`` block (combinatorial purged CV,
:mod:`iap.validation.cpcv`) and the ``deflated_sharpe`` block (PSR, DSR,
minimum track record length, :mod:`iap.validation.deflated`).  No gate
reads either, so a ``v4`` verdict equals the ``v3`` verdict on the same
data; the bundle debits more looks (``looks``).  The DSR trial count is
the caller's (``deflated_sharpe_trials``; the research runner passes the
ledger's distinct configurations).

**Statistics revision v1.14.0 (the correctness release).**  ``v2``,
``v3`` and ``v4`` carry the review fixes of v1.14.0 (all defaults of
:mod:`iap.backtest.engine` and :func:`iap.validation.validate.validate_alpha`):

==============================  ==========================  ==========================
choice                          v1.14.0 (``v2``/``v3``/     v1.13.0 (``*_v113``)
                                ``v4``)
==============================  ==========================  ==========================
backtest row block (R-P0-1)     ``causal``: decision-time   ``auto``: the rows the IC
                                state only                  scores (label look-ahead)
HAC bandwidth (R-P1-3)          automatic (>= horizon rule) horizon rule (L = 2)
HAC across days (R-P1-3)        no lag product across a     adjacent across the
                                session-day boundary;       overnight gap
                                ``validity`` block always
hypothesis_confirmed (R-P2-1)   >= 70 % of folds confirm    last fold's beta
pooled scaling (R-P2-2)         per instrument              pooled
leakage (R-P2-3)                + time-shift detector       row shift only
ledger looks (R-P2-4)           one per gate decision       every report statistic
==============================  ==========================  ==========================

The bundle NAMES are unchanged, so the ``ExperimentSpec`` of an experiment
under ``v2`` keeps its ``experiment_id``: a spec is the question, the
revision is how it is answered.  A result committed before v1.14.0 under
``v2`` / ``v3`` / ``v4`` was computed at the v1.13.0 revision and is
reproduced by the matching ``*_v113`` bundle (a pre-registration written
for v1.13.0 names that bundle to be run as registered).  Every report
states the revision it was computed at (``methods.row_block``,
``hac_bandwidth``, ``hac_day_separated``, ``hypothesis_rule``,
``standardisation``, ``leakage_time_shift``) and the promotion pipeline
puts :data:`STATS_REVISION` in its ledger identity, so a v1.14.0 run of
the same alpha on the same data is a new look, never a silent rerun.

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

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from iap.backtest.costs import DEFAULT_IMPACT_MODEL, LEGACY_IMPACT_MODEL, CostModel
from iap.backtest.engine import (
    BLOCK_ROWS_AUTO,
    BLOCK_ROWS_CAUSAL,
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
    DEFAULT_HAC_BANDWIDTH,
    DEFAULT_HYPOTHESIS_RULE,
    DEFAULT_LOOKS_RULE,
    DEFAULT_SIGNIFICANCE,
    DEFAULT_STANDARDISATION,
    DEFAULT_TSTAT_THRESHOLD,
    LEGACY_CAPACITY,
    LEGACY_SIGNIFICANCE,
    LEGACY_TSTAT_THRESHOLD,
    V113_HAC_BANDWIDTH,
    V113_HYPOTHESIS_RULE,
    V113_LOOKS_RULE,
    V113_STANDARDISATION,
    looks_per_validation,
)

__all__ = [
    "DEFAULT_METHODS",
    "METHODS",
    "METHODS_LEGACY",
    "METHODS_V2",
    "METHODS_V3",
    "METHODS_V4",
    "METHODS_V2_V113",
    "METHODS_V3_V113",
    "METHODS_V4_V113",
    "METHODS_V2_V114",
    "METHODS_V3_V114",
    "METHODS_V4_V114",
    "STATS_REVISION",
    "V113_REVISION",
    "V114_REVISION",
    "ResearchMethods",
    "ledger_bundle",
    "ledger_config",
    "methods",
]

METHODS_V2 = "v2"
METHODS_LEGACY = "legacy_v1"
METHODS_V3 = "v3"
METHODS_V4 = "v4"
#: The frozen v1.13.0 revisions of ``v2`` / ``v3`` / ``v4`` (module docs).
METHODS_V2_V113 = "v2_v113"
METHODS_V3_V113 = "v3_v113"
METHODS_V4_V113 = "v4_v113"
#: The frozen v1.14.0 revisions of ``v2`` / ``v3`` / ``v4`` (module docs, v1.17).
METHODS_V2_V114 = "v2_v114"
METHODS_V3_V114 = "v3_v114"
METHODS_V4_V114 = "v4_v114"
#: The bundle a pipeline uses when it names none.
DEFAULT_METHODS = METHODS_V2
#: The statistics revision of the current ``v2`` / ``v3`` / ``v4``.
STATS_REVISION = "v1.17"
#: The revision of the ``*_v114`` bundles (v1.14.0 to v1.16.0).
V114_REVISION = "v1.14"
#: The revision of ``legacy_v1`` and the ``*_v113`` bundles.
V113_REVISION = "v1.13"


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
    cpcv: bool = False
    deflated_sharpe: bool = False
    # v1.14.0 statistics revision (module docs); the field defaults are the
    # v1.13.0 values, so a bundle that names none keeps the old rules.
    causal_row_block: bool = False
    hac_bandwidth: str = V113_HAC_BANDWIDTH
    hac_day_separated: bool = False
    hypothesis_rule: str = V113_HYPOTHESIS_RULE
    standardisation: str = V113_STANDARDISATION
    leakage_time_shift: bool = False
    looks_rule: str = V113_LOOKS_RULE
    revision: str = V113_REVISION
    # v1.17 statistics revision (module docs): the post-block embargo rule
    # of two-sided splits (``validate_alpha(feature_window_ns=)``); ``None``
    # is the v1.13-v1.14 rule (the embargo alone).
    feature_window: int | str | None = None
    # v1.17 WS-D (R-9): the ``cpcv`` path-IC rule (``validate_alpha(
    # cpcv_path_ic=)``); ``"pooled"`` is the v1.12 rule every bundle keeps.
    cpcv_path_ic: str = "pooled"

    @property
    def row_block(self) -> str:
        """The report name of the backtest row block (``methods.row_block``)."""
        if self.causal_row_block:
            return "causal"
        return "label_valid" if self.block_invalid_label_rows else "none"

    def backtest_config(self, **fields: Any) -> BacktestConfig:
        """A ``BacktestConfig`` under this bundle's backtest rules;
        ``fields`` are the protocol knobs (latency, decision age, ...)."""
        if self.causal_row_block:
            block: str | None = BLOCK_ROWS_CAUSAL
        else:
            block = BLOCK_ROWS_AUTO if self.block_invalid_label_rows else None
        return BacktestConfig(
            position_policy=self.position_policy,
            cap_fills_at_l1=self.cap_fills_at_l1,
            block_rows_column=block,
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
        if self.cpcv:
            kw["cpcv"] = True
        if self.deflated_sharpe:
            kw["deflated_sharpe"] = True
        # v1.14.0 statistics revision: named only when it differs from the
        # validate_alpha default (so the v1.14 v2 keyword set is unchanged).
        for key, default in (
            ("hac_bandwidth", DEFAULT_HAC_BANDWIDTH),
            ("hac_day_separated", True),
            ("hypothesis_rule", DEFAULT_HYPOTHESIS_RULE),
            ("standardisation", DEFAULT_STANDARDISATION),
            ("leakage_time_shift", True),
        ):
            if getattr(self, key) != default:
                kw[key] = getattr(self, key)
        # v1.17 statistics revision: named only when set (the v1.14 keyword
        # set is unchanged for the ``*_v114`` bundles).
        if self.feature_window is not None:
            kw["feature_window_ns"] = self.feature_window
        if self.cpcv_path_ic != "pooled":
            kw["cpcv_path_ic"] = self.cpcv_path_ic  # v1.17 WS-D (R-9), opt-in
        return kw

    def looks(self, n_folds: int) -> int:
        """Looks one validation plus the caller's one out-of-sample backtest
        debits (:func:`iap.validation.validate.looks_per_validation` + 1):
        2 under ``v2`` (one gate decision + the holdout, v1.14.0), 84 under
        ``v2_v113`` at four folds, 28 under ``legacy_v1``."""
        return (
            looks_per_validation(
                n_folds,
                self.fold_diagnostics,
                self.validity_diagnostics,
                cpcv=self.cpcv,
                deflated_sharpe=self.deflated_sharpe,
                looks_rule=self.looks_rule,
            )
            + 1
        )


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
#: ``v4`` (v1.12, opt-in) = ``v3`` + the report-only R7 blocks.
METHODS[METHODS_V4] = replace(METHODS[METHODS_V3], name=METHODS_V4, cpcv=True, deflated_sharpe=True)

#: The v1.13.0 revisions, frozen under their own names (module docs).
METHODS[METHODS_V2_V113] = replace(METHODS[METHODS_V2], name=METHODS_V2_V113)
METHODS[METHODS_V3_V113] = replace(METHODS[METHODS_V3], name=METHODS_V3_V113)
METHODS[METHODS_V4_V113] = replace(METHODS[METHODS_V4], name=METHODS_V4_V113)

#: The v1.14.0 statistics revision (R-P0-1, R-P1-3, R-P2-1..4).
_V114 = {
    "causal_row_block": True,
    "hac_bandwidth": DEFAULT_HAC_BANDWIDTH,
    "hac_day_separated": True,
    "hypothesis_rule": DEFAULT_HYPOTHESIS_RULE,
    "standardisation": DEFAULT_STANDARDISATION,
    "leakage_time_shift": True,
    "looks_rule": DEFAULT_LOOKS_RULE,
    "revision": V114_REVISION,
}
# ``v2`` gains the validity block (R-P1-3: required whenever the data span
# two or more session days; it debits no look under the decisions rule).
METHODS[METHODS_V2] = replace(METHODS[METHODS_V2], validity_diagnostics=True, **_V114)
METHODS[METHODS_V3] = replace(METHODS[METHODS_V3], **_V114)
METHODS[METHODS_V4] = replace(METHODS[METHODS_V4], **_V114)

#: The v1.14.0 revisions, frozen under their own names (module docs, v1.17).
METHODS[METHODS_V2_V114] = replace(METHODS[METHODS_V2], name=METHODS_V2_V114)
METHODS[METHODS_V3_V114] = replace(METHODS[METHODS_V3], name=METHODS_V3_V114)
METHODS[METHODS_V4_V114] = replace(METHODS[METHODS_V4], name=METHODS_V4_V114)

#: The v1.17 statistics revision (R-3 / R-P1-6; module docs).
_V117 = {"feature_window": "registry", "revision": STATS_REVISION}
METHODS[METHODS_V2] = replace(METHODS[METHODS_V2], **_V117)
METHODS[METHODS_V3] = replace(METHODS[METHODS_V3], **_V117)
METHODS[METHODS_V4] = replace(METHODS[METHODS_V4], **_V117)

#: Bundle name -> its v1.13.0 revision (itself when it has no other).
V113_OF = {
    METHODS_V2: METHODS_V2_V113,
    METHODS_V3: METHODS_V3_V113,
    METHODS_V4: METHODS_V4_V113,
}
#: Bundle name -> its v1.14.0 revision (v1.17).
V114_OF = {
    METHODS_V2: METHODS_V2_V114,
    METHODS_V3: METHODS_V3_V114,
    METHODS_V4: METHODS_V4_V114,
}


def methods(name: str = DEFAULT_METHODS) -> ResearchMethods:
    """The bundle called ``name`` (``ValueError`` naming the known ones)."""
    try:
        return METHODS[name]
    except KeyError:
        raise ValueError(f"unknown methods bundle {name!r}; known: {sorted(METHODS)}") from None


def ledger_config(config: Mapping[str, Any], bundle: ResearchMethods) -> dict[str, Any]:
    """``config`` (a pipeline's ledger identity) with the bundle's
    statistics revision added when it is not v1.13.0: the same bundle name
    at another revision is another look (module docs), and an identity
    recorded before v1.14.0 stays exactly what it was."""
    out = dict(config)
    if bundle.revision != V113_REVISION:
        out["stats_revision"] = bundle.revision
    return out


def ledger_bundle(config: Mapping[str, Any] | None) -> str:
    """The bundle a ledger entry's ``config`` was recorded under, at its
    revision: ``config["methods"]`` (or, for a runner entry, the spec's
    ``configuration["methods"]``; ``legacy_v1`` when neither names one),
    mapped to its frozen ``*_v113`` name when the entry carries no
    ``stats_revision`` - an entry recorded under ``v2`` before v1.14.0 was
    computed at the v1.13.0 revision (module docs) - and to its frozen
    ``*_v114`` name when it carries ``"v1.14"`` (v1.17).  An entry naming a
    revision this code does not know is an error, never a silent match."""
    cfg = dict(config or {})
    name = cfg.get("methods")
    if name is None:
        name = (cfg.get("configuration") or {}).get("methods")
    if name is None:
        return METHODS_LEGACY
    name = str(name)
    if name in V113_OF:
        revision = cfg.get("stats_revision")
        if revision is None:
            return V113_OF[name]
        if revision == V114_REVISION:
            return V114_OF[name]
        if revision != METHODS[name].revision:
            raise ValueError(
                f"ledger entry under {name!r} carries stats_revision {revision!r}; "
                f"known: None (v1.13), {V114_REVISION!r}, {METHODS[name].revision!r}"
            )
    return name
