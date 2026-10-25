"""Data and feature contracts (v2.0 U0 stage 2, PR U2; plan section 5 "feature contracts with lineage").

Every dataset and every feature the research loop consumes declares, in code and
before any fit, the six lineage facts the plan names: **source**, the
**availability-timestamp rule**, the **adjustment** convention, the
**missing-value policy**, the data **route** and the **leakage test** that
guards it.  The declarations are frozen dataclasses registered at import time;
a feature function without a contract cannot be registered, and a module that
defines feature functions must end with :func:`enforce_module`, which raises
:class:`MissingContractError` for any ``feature_*`` callable that carries none,
so the import itself fails (acceptance: "any feature without a contract fails
import").  The architecture test ``python/tests/leakage/test_contracts.py``
checks that every module under ``iap/data`` defining ``feature_*`` functions
makes that call.

Availability semantics (one convention for every route)
-------------------------------------------------------
Records live on a period grid: sessions for daily data, bars for intraday data.
A record of period ``p`` with availability lag ``L`` is usable for a decision at
period index ``>= p + L``.  The lag's lower bound depends on the basis:

``receive_ts``        intraday records are available at their receive timestamp
                      (``receive_ts <= t_decision``, E0 section 2); ``L`` is 0.
``period_end``        a session's close is not observable at that close;
                      ``L >= 1`` (the daily analogue of E0's "no same-day data").
``publication_date``  a reference file dated ``D`` is usable only after its
                      publication lag (E0 section 2: a holdings file dated ``D``
                      is usable from the first session after ``D`` plus one
                      business day); ``L >= 1``.

A feature's availability is the latest availability of its inputs plus the
feature's own declared lag (``basis="inputs"``, ``L >= 0``); the feature may read
rows up to and including the decision index minus that lag and never a later
row.  :func:`check_causality` tests exactly that on data: the feature computed
on the frame truncated at every probe index must equal the feature computed on
the whole frame at that index.  A feature that reads a later row (the planted
leak of the canary suite) fails that check whatever its contract says, which is
what makes the contract check a detector and not a declaration.

Nothing here reads data from disk or the network; the module is pure
declarations and checks, usable by every route (daily PIT set, intraday vendor
bars, IEX HIST, synthetic fixtures).
"""

from __future__ import annotations

import math
import re
import sys
import types
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ADJUSTMENTS",
    "AVAILABILITY_BASES",
    "FEATURE_PREFIX",
    "LEAKAGE_TESTS",
    "MISSING_POLICIES",
    "ROUTES",
    "AvailabilityRule",
    "ContractError",
    "DatasetContract",
    "FeatureContract",
    "LeakageDetected",
    "MissingContractError",
    "check_causality",
    "contract_of",
    "dataset",
    "datasets",
    "enforce_module",
    "feature",
    "features",
    "lineage",
    "reset_registry",
    "validate_feature_output",
]

#: Routes (plan section 3; one route per experiment, never mixed).
ROUTES = frozenset({"daily_pit", "intraday_vendor", "iex_hist", "order_level_vendor", "synthetic"})
#: Price-adjustment conventions a dataset or feature may declare.
ADJUSTMENTS = frozenset({"none", "split", "split_dividend", "total_return"})
#: Missing-value policies.  ``ineligible`` is E0 section 3's rule: a missing
#: input yields an ineligible name, never a value.  ``forward_fill`` carries the
#: last value for at most ``missing_max_periods`` periods, then ineligible.
#: ``zero`` is allowed only for flow-like quantities that are genuinely zero
#: when absent (volume on a halted bar), never for prices or returns.
MISSING_POLICIES = frozenset({"ineligible", "forward_fill", "zero"})
#: Availability bases (module docstring).
AVAILABILITY_BASES = frozenset({"receive_ts", "period_end", "publication_date", "inputs"})
#: Leakage tests of the canary suite (``iap.data.leakage``); a contract names
#: the one that guards it, and the suite refuses an unknown id.
LEAKAGE_TESTS = frozenset(
    {"LK-FUTURE-SHIFT", "LK-SHUFFLED-LABEL", "LK-PLANTED-LEAK", "LK-CAUSALITY"}
)
#: Feature functions are named ``feature_<id>``; the prefix is what
#: :func:`enforce_module` scans for.
FEATURE_PREFIX = "feature_"

_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")


class ContractError(ValueError):
    """A contract is malformed, inconsistent with its inputs, or missing."""


class MissingContractError(ContractError, ImportError):
    """A feature function without a contract (raised at import by :func:`enforce_module`)."""


class LeakageDetected(ContractError):
    """A feature read a row later than its declared availability (planted-leak detector)."""


def _check_id(value: str, what: str) -> None:
    if not isinstance(value, str) or not _ID.match(value):
        raise ContractError(f"{what} must match {_ID.pattern!r}, got {value!r}")


def _check_in(value: str, allowed: frozenset[str], what: str) -> None:
    if value not in allowed:
        raise ContractError(f"{what} must be one of {sorted(allowed)}, got {value!r}")


@dataclass(frozen=True, slots=True)
class AvailabilityRule:
    """When a record of period ``p`` becomes usable: at decision index ``p + lag_periods``."""

    basis: str
    lag_periods: int

    def __post_init__(self) -> None:
        _check_in(self.basis, AVAILABILITY_BASES, "availability basis")
        if not isinstance(self.lag_periods, int) or isinstance(self.lag_periods, bool):
            raise ContractError("lag_periods must be an int")
        if self.lag_periods < 0:
            raise ContractError(f"lag_periods must be >= 0, got {self.lag_periods}")
        if self.basis == "receive_ts" and self.lag_periods != 0:
            raise ContractError(
                "receive_ts availability has lag 0 by definition (receive_ts <= t_decision)"
            )
        if self.basis in ("period_end", "publication_date") and self.lag_periods < 1:
            raise ContractError(
                f"{self.basis} availability needs lag_periods >= 1: a close is not observable at the "
                "close and a file dated D is not usable on D"
            )

    def available_index(self, record_index: int) -> int:
        """First decision index at which a record of ``record_index`` may be read."""
        return record_index + self.lag_periods

    def to_dict(self) -> dict[str, Any]:
        return {"basis": self.basis, "lag_periods": self.lag_periods}


@dataclass(frozen=True, slots=True)
class DatasetContract:
    """The lineage declaration of one dataset (plan section 5)."""

    dataset_id: str
    source: str
    availability: AvailabilityRule
    adjustment: str
    missing_policy: str
    route: str
    leakage_test_id: str
    fields: tuple[str, ...] = ()
    missing_max_periods: int = 0
    period: str = "session"

    def __post_init__(self) -> None:
        _check_id(self.dataset_id, "dataset_id")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ContractError("source must be a non-empty string (vendor, venue or 'synthetic')")
        if not isinstance(self.availability, AvailabilityRule):
            raise ContractError("availability must be an AvailabilityRule")
        if self.availability.basis == "inputs":
            raise ContractError(
                "a dataset's availability basis is receive_ts, period_end or publication_date"
            )
        _check_in(self.adjustment, ADJUSTMENTS, "adjustment")
        _check_in(self.missing_policy, MISSING_POLICIES, "missing_policy")
        _check_in(self.route, ROUTES, "route")
        _check_in(self.leakage_test_id, LEAKAGE_TESTS, "leakage_test_id")
        _check_missing(self.missing_policy, self.missing_max_periods)
        if not isinstance(self.fields, tuple) or not all(
            isinstance(f, str) and f for f in self.fields
        ):
            raise ContractError("fields must be a tuple of non-empty strings")
        if len(set(self.fields)) != len(self.fields):
            raise ContractError("fields must be unique")
        if self.period not in ("session", "bar"):
            raise ContractError("period must be 'session' or 'bar'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "source": self.source,
            "availability": self.availability.to_dict(),
            "adjustment": self.adjustment,
            "missing_policy": self.missing_policy,
            "missing_max_periods": self.missing_max_periods,
            "route": self.route,
            "leakage_test_id": self.leakage_test_id,
            "fields": list(self.fields),
            "period": self.period,
        }


def _check_missing(policy: str, max_periods: int) -> None:
    if not isinstance(max_periods, int) or isinstance(max_periods, bool) or max_periods < 0:
        raise ContractError("missing_max_periods must be an int >= 0")
    if policy == "forward_fill" and max_periods < 1:
        raise ContractError("forward_fill needs missing_max_periods >= 1")
    if policy != "forward_fill" and max_periods != 0:
        raise ContractError("missing_max_periods applies to forward_fill only")


@dataclass(frozen=True, slots=True)
class FeatureContract:
    """The lineage declaration of one feature: inputs (datasets or features), its own lag, policies."""

    feature_id: str
    inputs: tuple[str, ...]
    availability: AvailabilityRule
    adjustment: str
    missing_policy: str
    route: str
    leakage_test_id: str
    lookback_periods: int = 0
    missing_max_periods: int = 0
    description: str = ""
    resolved: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        _check_id(self.feature_id, "feature_id")
        if not isinstance(self.inputs, tuple) or not self.inputs:
            raise ContractError("inputs must be a non-empty tuple of dataset or feature ids")
        for i in self.inputs:
            _check_id(i, "input id")
        if len(set(self.inputs)) != len(self.inputs):
            raise ContractError("inputs must be unique")
        if not isinstance(self.availability, AvailabilityRule):
            raise ContractError("availability must be an AvailabilityRule")
        if self.availability.basis != "inputs":
            raise ContractError(
                "a feature's availability basis is 'inputs' (lag relative to its inputs)"
            )
        _check_in(self.adjustment, ADJUSTMENTS, "adjustment")
        _check_in(self.missing_policy, MISSING_POLICIES, "missing_policy")
        _check_in(self.route, ROUTES, "route")
        _check_in(self.leakage_test_id, LEAKAGE_TESTS, "leakage_test_id")
        _check_missing(self.missing_policy, self.missing_max_periods)
        if not isinstance(self.lookback_periods, int) or isinstance(self.lookback_periods, bool):
            raise ContractError("lookback_periods must be an int")
        if self.lookback_periods < 0:
            raise ContractError("lookback_periods must be >= 0")

    @property
    def source(self) -> str:
        """The sources of the datasets this feature descends from (resolved at registration)."""
        return self.resolved.get("source", "")

    @property
    def total_lag_periods(self) -> int:
        """Availability lag against the raw record grid: inputs' lag plus this feature's lag."""
        return int(self.resolved.get("total_lag_periods", self.availability.lag_periods))

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "inputs": list(self.inputs),
            "availability": self.availability.to_dict(),
            "adjustment": self.adjustment,
            "missing_policy": self.missing_policy,
            "missing_max_periods": self.missing_max_periods,
            "route": self.route,
            "leakage_test_id": self.leakage_test_id,
            "lookback_periods": self.lookback_periods,
            "description": self.description,
            "source": self.source,
            "total_lag_periods": self.total_lag_periods,
        }


# --------------------------------------------------------------------------- registry
_DATASETS: dict[str, DatasetContract] = {}
_FEATURES: dict[str, FeatureContract] = {}
_FUNCTIONS: dict[str, Callable[..., Any]] = {}


def reset_registry() -> None:
    """Forget every registration (tests only)."""
    _DATASETS.clear()
    _FEATURES.clear()
    _FUNCTIONS.clear()


def datasets() -> dict[str, DatasetContract]:
    return dict(_DATASETS)


def features() -> dict[str, FeatureContract]:
    return dict(_FEATURES)


def dataset(contract: DatasetContract) -> DatasetContract:
    """Register a dataset contract; re-registering an identical contract is a no-op."""
    if not isinstance(contract, DatasetContract):
        raise ContractError("dataset() takes a DatasetContract")
    prior = _DATASETS.get(contract.dataset_id)
    if prior is not None and prior != contract:
        raise ContractError(
            f"dataset {contract.dataset_id!r} already registered with a different contract"
        )
    if contract.dataset_id in _FEATURES:
        raise ContractError(f"{contract.dataset_id!r} is already a feature id")
    _DATASETS[contract.dataset_id] = contract
    return contract


def _resolve(contract: FeatureContract) -> FeatureContract:
    sources: list[str] = []
    lag = contract.availability.lag_periods
    max_input_lag = 0
    for i in contract.inputs:
        if i in _DATASETS:
            d = _DATASETS[i]
            if d.route != contract.route:
                raise ContractError(
                    f"feature {contract.feature_id!r} declares route {contract.route!r} but input dataset "
                    f"{i!r} is on route {d.route!r} (one route per experiment)"
                )
            if (
                d.leakage_test_id not in LEAKAGE_TESTS
            ):  # pragma: no cover - guarded by DatasetContract
                raise ContractError("input dataset has no leakage test")
            sources.append(d.source)
            max_input_lag = max(max_input_lag, d.availability.lag_periods)
        elif i in _FEATURES:
            f = _FEATURES[i]
            if f.route != contract.route:
                raise ContractError(
                    f"feature {contract.feature_id!r} declares route {contract.route!r} but input feature "
                    f"{i!r} is on route {f.route!r}"
                )
            sources.extend(s for s in f.source.split("+") if s)
            max_input_lag = max(max_input_lag, f.total_lag_periods)
        else:
            raise ContractError(
                f"feature {contract.feature_id!r} names input {i!r}, which is neither a registered "
                "dataset nor a registered feature (register the dataset contract first)"
            )
    contract.resolved.clear()
    contract.resolved["source"] = "+".join(sorted(set(sources)))
    contract.resolved["total_lag_periods"] = max_input_lag + lag
    return contract


def feature(contract: FeatureContract) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: register ``contract`` for the decorated ``feature_<id>`` function.

    The function name must be ``feature_`` + the contract's ``feature_id`` with
    dots replaced by underscores, so a reader can find the contract from the
    name and :func:`enforce_module` can find the function from the prefix.
    """
    if not isinstance(contract, FeatureContract):
        raise ContractError("feature() takes a FeatureContract")

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        expected = FEATURE_PREFIX + contract.feature_id.replace(".", "_")
        if fn.__name__ != expected:
            raise ContractError(f"feature function must be named {expected!r}, got {fn.__name__!r}")
        if contract.feature_id in _DATASETS:
            raise ContractError(f"{contract.feature_id!r} is already a dataset id")
        prior = _FEATURES.get(contract.feature_id)
        if prior is not None and prior != contract:
            raise ContractError(
                f"feature {contract.feature_id!r} already registered with a different contract"
            )
        _resolve(contract)
        _FEATURES[contract.feature_id] = contract
        _FUNCTIONS[contract.feature_id] = fn
        fn.__iap_contract__ = contract  # type: ignore[attr-defined]
        return fn

    return deco


def contract_of(obj: Callable[..., Any] | str) -> FeatureContract | DatasetContract:
    """The contract behind a feature function, feature id or dataset id; raises if none."""
    if isinstance(obj, str):
        if obj in _FEATURES:
            return _FEATURES[obj]
        if obj in _DATASETS:
            return _DATASETS[obj]
        raise MissingContractError(f"no contract registered for {obj!r}")
    c = getattr(obj, "__iap_contract__", None)
    if c is None:
        raise MissingContractError(f"{getattr(obj, '__name__', obj)!r} has no contract")
    return c


def lineage(feature_id: str) -> dict[str, Any]:
    """The full lineage document of a feature: its contract and, recursively, its inputs'."""
    c = _FEATURES.get(feature_id)
    if c is None:
        raise MissingContractError(f"no contract registered for feature {feature_id!r}")
    doc: dict[str, Any] = {"feature": c.to_dict(), "inputs": {}}
    for i in c.inputs:
        doc["inputs"][i] = lineage(i) if i in _FEATURES else _DATASETS[i].to_dict()
    return doc


def enforce_module(module: str | types.ModuleType) -> int:
    """Fail the import of ``module`` if any ``feature_*`` callable in it lacks a contract.

    Call it as the last statement of every module that defines feature
    functions: ``enforce_module(__name__)``.  Returns the number of contracted
    feature functions found (so a module can assert it is not empty).
    """
    mod = sys.modules[module] if isinstance(module, str) else module
    missing: list[str] = []
    found = 0
    for name, obj in vars(mod).items():
        if not name.startswith(FEATURE_PREFIX) or not callable(obj):
            continue
        if getattr(obj, "__module__", None) != mod.__name__:
            continue  # re-exported from another module, which is enforced there
        if getattr(obj, "__iap_contract__", None) is None:
            missing.append(name)
        else:
            found += 1
    if missing:
        raise MissingContractError(
            f"module {mod.__name__!r} defines feature functions without a contract: {sorted(missing)}; "
            "decorate each with @feature(FeatureContract(...))"
        )
    return found


# --------------------------------------------------------------------------- data checks
def validate_feature_output(fn: Callable[..., Any], frame: Any, out: Any) -> None:
    """The output of a feature function must be a Series aligned to the frame's index."""
    import pandas as pd

    if not isinstance(out, pd.Series):
        raise ContractError(f"{fn.__name__} must return a pandas Series, got {type(out).__name__}")
    if len(out) != len(frame) or not out.index.equals(frame.index):
        raise ContractError(
            f"{fn.__name__} must return a Series aligned to the input frame's index"
        )


def _same(a: float, b: float) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    try:
        return bool(a == b) or (
            isinstance(a, int | float)
            and isinstance(b, int | float)
            and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
        )
    except Exception:  # pragma: no cover - exotic dtypes
        return False


def check_causality(
    fn: Callable[..., Any], frame: Any, probes: Sequence[int] | None = None
) -> dict[str, Any]:
    """The planted-leak detector: a feature may not depend on rows after its availability.

    For every probe index ``t`` the feature is recomputed on ``frame`` truncated
    to rows ``<= t`` (a panel sorted by period then name hides every later
    period and the rest of the current period) and its value at ``t`` must
    equal the value computed on the whole frame.  A difference means the
    feature read the future; :class:`LeakageDetected` names the first probe.
    The declared lag is applied by the caller when aligning to decisions; the
    check here is about *reading* later rows, which no lag can repair.
    """
    import pandas as pd

    contract = contract_of(fn)
    if not isinstance(frame, pd.DataFrame):
        raise ContractError("check_causality needs a pandas DataFrame")
    full = fn(frame)
    validate_feature_output(fn, frame, full)
    n = len(frame)
    if probes is None:
        step = max(1, n // 8)
        probes = list(range(min(n - 1, contract.lookback_periods + 1), n, step))
        if n - 1 not in probes:
            probes.append(n - 1)
    checked = 0
    for t in probes:
        if t < 0 or t >= n:
            raise ContractError(f"probe {t} outside the frame (n = {n})")
        cut = frame.iloc[: t + 1]
        partial = fn(cut)
        validate_feature_output(fn, cut, partial)
        a, b = partial.iloc[-1], full.iloc[t]
        if not _same(a, b):
            raise LeakageDetected(
                f"feature {contract.feature_id!r} at row {t} is {b!r} on the full frame but {a!r} when "
                f"later rows are hidden: it reads the future"
            )
        checked += 1
    return {"feature_id": contract.feature_id, "probes": checked, "rows": n, "leak": False}
