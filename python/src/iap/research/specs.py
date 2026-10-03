"""Building :class:`~iap.contracts.types.ExperimentSpec` documents.

An experiment is fully specified by *what* is tested (``alpha_id``,
``horizon``, ``model_version``), *on which data* (``dataset_version``,
``feature_version``, the three periods) and *under which protocol*
(``configuration``, ``seed``).  :func:`build_spec` fills every field from
its pinned source, normalises the configuration so that two equivalent
requests hash identically, derives the periods from the dataset's session
calendar when the caller gives none, and computes the experiment id.

**Experiment id (pinned, ``research/experiments/README.md``)**::

    experiment_id = content_hash(spec_without_experiment_id)[:16]

where ``content_hash`` is the SHA-256 of the canonical JSON
(:mod:`iap.contracts.versions`).  Any change to any field — one more fold,
a different horizon, another dataset — is a different experiment with its
own directory; an identical request lands in the same one.

**Configuration (pinned keys, all others rejected)** — a key the runner
would ignore must not exist, because it would change the experiment id
without changing the computation:

============================ ======= =====================================
key                          default meaning
============================ ======= =====================================
``n_folds``                  4       expanding walk-forward folds
``embargo_ns``               60 s    embargo after the label horizon
``cost_multiplier``          1.0     cost scale of the holdout backtest
``latency_ns``               1 s     event-time decision-to-fill latency
``max_decision_age_ns``      60 s    a decision older than this never fills
``flatten_at_session_end``   true    no overnight carry in the backtest
============================ ======= =====================================

The defaults are exactly the pinned research execution model of
``research/alpha_reports/run_all.py`` (4 folds, 60 s embargo, 1 s latency,
60 s decision age, session flattening, 1x costs), so a spec with an empty
configuration reproduces the flagship promotion report's protocol.

**Period derivation (pinned)** — see :func:`derive_periods`.

**Gate eligibility (pinned)** — see :func:`gate_eligibility`.  Any
well-formed configuration and any ordered period set can be RUN and is
recorded and ledgered: a half-cost backtest, a zero-latency fill or a
hand-picked holdout are legitimate questions.  They are not legitimate
PROMOTION evidence, because each of them is a knob an automated caller can
turn until the gates pass.  A result is *gate-eligible* only when

* every protocol knob is at least as conservative as
  :data:`DEFAULT_CONFIGURATION` (:data:`GATE_ELIGIBILITY_BOUNDS`:
  ``cost_multiplier >= 1.0``, ``latency_ns >= 1 s``, ``embargo_ns >= 60 s``,
  ``n_folds >= 4``, ``max_decision_age_ns <= 60 s``,
  ``flatten_at_session_end`` true), and
* its three periods are exactly the ones :func:`derive_periods` derives from
  the dataset it ran on — i.e. the caller did not choose the holdout.

Eligibility is not part of the spec and never changes an experiment id; the
runner writes it beside the result (``eligibility.json``) and
``iap.lifecycle.gates`` refuses research evidence flagged not eligible.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from iap.alpha import ALPHA_CLASSES, build
from iap.alpha.base import VALID_HORIZONS
from iap.alpha.data import session_days
from iap.contracts.ids import is_sha256_hex
from iap.contracts.types import ExperimentSpec, Period
from iap.contracts.validate import validate_typed
from iap.contracts.versions import content_hash
from iap.experiment import tracker
from iap.research.errors import ResearchError
from iap.validation.metrics import HORIZONS_NS

__all__ = [
    "DEFAULT_CONFIGURATION",
    "DEFAULT_SEED",
    "ELIGIBILITY_VERSION",
    "GATE_ELIGIBILITY_BOUNDS",
    "GateEligibility",
    "NS_DAY",
    "build_spec",
    "derive_periods",
    "experiment_id_of",
    "gate_eligibility",
    "model_definition_hash",
    "normalise_configuration",
    "pinned_horizon",
    "verify_experiment_id",
]

NS_DAY = 86_400_000_000_000

#: Pinned protocol defaults (= ``run_all.py``'s research execution model).
DEFAULT_CONFIGURATION: dict[str, Any] = {
    "n_folds": 4,
    "embargo_ns": 60_000_000_000,
    "cost_multiplier": 1.0,
    "latency_ns": 1_000_000_000,
    "max_decision_age_ns": 60_000_000_000,
    "flatten_at_session_end": True,
}

#: Seed recorded when the caller gives none (the contract example's value).
DEFAULT_SEED = 20_260_919


def _invalid(message: str) -> ResearchError:
    """A spec that cannot be built or does not verify (code ``invalid_spec``)."""
    return ResearchError(message, code="invalid_spec")


def _corrupt(message: str) -> ResearchError:
    """A persisted document that is damaged (code ``experiment_corrupt``)."""
    return ResearchError(message, code="experiment_corrupt")


def _require_int(cfg: Mapping[str, Any], key: str, minimum: int) -> int:
    value = cfg[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"configuration.{key}: expected an integer, got {value!r}")
    if value < minimum:
        raise _invalid(f"configuration.{key}: must be >= {minimum}, got {value}")
    return int(value)


def _require_float(cfg: Mapping[str, Any], key: str, exclusive_min: float) -> float:
    value = cfg[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(f"configuration.{key}: expected a number, got {value!r}")
    out = float(value)
    if not math.isfinite(out) or out <= exclusive_min:
        raise _invalid(
            f"configuration.{key}: must be a finite number > {exclusive_min}, got {value!r}"
        )
    return out


def _require_bool(cfg: Mapping[str, Any], key: str) -> bool:
    value = cfg[key]
    if not isinstance(value, bool):
        raise _invalid(f"configuration.{key}: expected a boolean, got {value!r}")
    return value


def normalise_configuration(configuration: Mapping[str, Any] | None) -> dict[str, Any]:
    """Fill the pinned defaults, reject unknown keys, canonicalise types.

    ``cost_multiplier=1`` and ``cost_multiplier=1.0`` are the same protocol
    and must hash identically, so numbers are coerced to their pinned type
    (``1`` -> ``1.0``); a boolean where an integer is expected is an error
    (``True`` is not a fold count).  Keys come back in pinned order.
    """
    given = dict(configuration or {})
    unknown = sorted(set(given) - set(DEFAULT_CONFIGURATION))
    if unknown:
        raise _invalid(
            f"unknown configuration keys {unknown}; pinned keys are {sorted(DEFAULT_CONFIGURATION)}"
        )
    merged = {**DEFAULT_CONFIGURATION, **given}
    return {
        "n_folds": _require_int(merged, "n_folds", 1),
        "embargo_ns": _require_int(merged, "embargo_ns", 0),
        "cost_multiplier": _require_float(merged, "cost_multiplier", 0.0),
        "latency_ns": _require_int(merged, "latency_ns", 0),
        "max_decision_age_ns": _require_int(merged, "max_decision_age_ns", 1),
        "flatten_at_session_end": _require_bool(merged, "flatten_at_session_end"),
    }


def pinned_horizon(alpha_id: str) -> str:
    """The alpha's own label horizon (the default when a spec names none)."""
    if alpha_id not in ALPHA_CLASSES:
        raise _invalid(f"unknown alpha_id {alpha_id!r}; known: {sorted(ALPHA_CLASSES)}")
    return str(ALPHA_CLASSES[alpha_id].horizon)


def _check_horizon(horizon: str) -> str:
    if horizon not in VALID_HORIZONS:
        raise _invalid(f"unknown horizon {horizon!r}; pinned horizons: {list(VALID_HORIZONS)}")
    return horizon


def model_definition_hash(alpha_id: str, horizon: str) -> str:
    """``model_version`` of an experiment: the hash of the model DEFINITION.

    The flagship alphas are fitted inside the experiment (once per
    walk-forward fold and once for the holdout), so the fitted parameters
    are outputs, not identity.  What identifies the model before any fit is
    its class, scoring formula, feature list, horizon and the pinned scoring
    constants — exactly the structural entries of ``AlphaModel.params()``.
    """
    model = build(alpha_id)
    params = model.params()
    return content_hash(
        {
            "alpha_id": alpha_id,
            "class": type(model).__name__,
            "model": params["model"],
            "horizon": _check_horizon(horizon),
            "features": list(params["features"]),
            "z_clip": params["z_clip"],
            "conf_scale": params["conf_scale"],
        }
    )


def _version(explicit: str | None, source, what: str, repo_root: Path | None) -> str:
    value = explicit if explicit is not None else source(repo_root)
    if not is_sha256_hex(value):
        raise _invalid(
            f"{what} {value!r} is not a sha256 hex digest; the tracker could not "
            "fingerprint the bundled data (is data/ present?) — pass it explicitly"
        )
    return value


def derive_periods(
    frames: Mapping[int, pd.DataFrame],
    horizon: str,
    embargo_ns: int,
) -> tuple[Period, Period, Period]:
    """Walk-forward periods from the dataset's session calendar (pinned).

    Sessions are UTC days of ``exchange_ts`` (``iap.alpha.data.session_days``)
    across every frame; at least two are required.  With ``t_first`` /
    ``t_last`` the first / last row timestamp of all frames and
    ``test_start`` the first row timestamp of the LAST session:

    * ``test_period       = [test_start, t_last + 1)`` — the final session,
      the holdout; half-open, so the last row is inside it;
    * ``validation_period = [test_start - horizon_ns - embargo_ns, test_start)``
      — the purged + embargoed tail: exactly the rows the pinned splitter
      (``iap.validation.splits.Fold.train_mask``) refuses to train on
      because their label window, plus the embargo, reaches the holdout.
      On the bundled dataset it falls inside the overnight gap and holds
      no rows; on contiguous data it holds the rows a naive split would
      leak;
    * ``train_period      = [t_first, validation_period.start_ts)`` — every
      earlier session, minus that tail.

    Two sessions give the ``run_all.py`` day-1 / day-2 split; with more,
    all earlier sessions train and the last one is the holdout.  The result
    always satisfies the contract invariant (ordered, non-overlapping).
    """
    if not frames:
        raise _invalid("cannot derive periods from an empty frame set")
    nonempty = [df for df in frames.values() if len(df)]
    if not nonempty:
        raise _invalid("cannot derive periods: every frame is empty")
    days = session_days(frames)
    if len(days) < 2:
        raise _invalid(
            f"period derivation needs >= 2 sessions (UTC days), found {len(days)}; "
            "pass train/validation/test periods explicitly"
        )
    last_day = days[-1]
    t_first = min(int(df["exchange_ts"].iloc[0]) for df in nonempty)
    t_last = max(int(df["exchange_ts"].iloc[-1]) for df in nonempty)
    test_start = min(
        int(df["exchange_ts"][df["exchange_ts"] // NS_DAY >= last_day].iloc[0])
        for df in nonempty
        if bool((df["exchange_ts"] // NS_DAY >= last_day).any())
    )
    purge_start = test_start - HORIZONS_NS[_check_horizon(horizon)] - int(embargo_ns)
    if purge_start <= t_first:
        raise _invalid(
            "period derivation: the purge + embargo zone before the last session "
            "swallows every earlier row — nothing is left to train on"
        )
    return (
        Period(start_ts=t_first, end_ts=purge_start),
        Period(start_ts=purge_start, end_ts=test_start),
        Period(start_ts=test_start, end_ts=t_last + 1),
    )


#: ``x-version`` of the ``eligibility.json`` sidecar document.
ELIGIBILITY_VERSION = 1

#: Configuration bounds of a gate-eligible result: ``(kind, bound)`` per key,
#: ``min`` = the value must be >= bound, ``max`` = <= bound, ``is`` = equal.
#: Every bound is the :data:`DEFAULT_CONFIGURATION` value, so the pinned
#: protocol is eligible and any knob turned in the flattering direction
#: (cheaper, faster, less embargo, fewer folds, staler fills, overnight
#: carry) is not.
GATE_ELIGIBILITY_BOUNDS: dict[str, tuple[str, Any]] = {
    "n_folds": ("min", DEFAULT_CONFIGURATION["n_folds"]),
    "embargo_ns": ("min", DEFAULT_CONFIGURATION["embargo_ns"]),
    "cost_multiplier": ("min", DEFAULT_CONFIGURATION["cost_multiplier"]),
    "latency_ns": ("min", DEFAULT_CONFIGURATION["latency_ns"]),
    "max_decision_age_ns": ("max", DEFAULT_CONFIGURATION["max_decision_age_ns"]),
    "flatten_at_session_end": ("is", DEFAULT_CONFIGURATION["flatten_at_session_end"]),
}


@dataclass(frozen=True)
class GateEligibility:
    """Whether a result may be used as lifecycle-gate evidence, and why not.

    ``periods_verified`` says the period check was actually made against a
    dataset; ``False`` means only the configuration bounds were checked
    (a run directory written before the sidecar existed)."""

    eligible: bool
    reasons: tuple[str, ...]
    periods_verified: bool

    def to_dict(self, experiment_id: str) -> dict[str, Any]:
        """The ``eligibility.json`` document for ``experiment_id``."""
        return {
            "x-version": ELIGIBILITY_VERSION,
            "experiment_id": experiment_id,
            "gate_eligible": self.eligible,
            "periods_verified": self.periods_verified,
            "reasons": list(self.reasons),
        }

    @staticmethod
    def from_dict(doc: Mapping[str, Any], experiment_id: str) -> GateEligibility:
        """Strict inverse of :meth:`to_dict` for ``experiment_id``."""
        want = {"x-version", "experiment_id", "gate_eligible", "periods_verified", "reasons"}
        if not isinstance(doc, Mapping) or set(doc) != want:
            raise _corrupt(
                f"eligibility document for {experiment_id}: expected keys {sorted(want)}"
            )
        if doc["x-version"] != ELIGIBILITY_VERSION:
            raise _corrupt(
                f"eligibility document for {experiment_id}: x-version "
                f"{doc['x-version']!r}, this build reads {ELIGIBILITY_VERSION}"
            )
        if doc["experiment_id"] != experiment_id:
            raise _corrupt(
                f"eligibility document belongs to {doc['experiment_id']!r}, not {experiment_id!r}"
            )
        reasons = doc["reasons"]
        if (
            not isinstance(doc["gate_eligible"], bool)
            or not isinstance(doc["periods_verified"], bool)
            or not isinstance(reasons, list)
            or not all(isinstance(r, str) for r in reasons)
        ):
            raise _corrupt(f"eligibility document for {experiment_id}: malformed fields")
        if doc["gate_eligible"] and reasons:
            raise _corrupt(f"eligibility document for {experiment_id}: eligible with reasons")
        return GateEligibility(
            eligible=doc["gate_eligible"],
            reasons=tuple(reasons),
            periods_verified=doc["periods_verified"],
        )


def _configuration_violations(configuration: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for key, (kind, bound) in GATE_ELIGIBILITY_BOUNDS.items():
        value = configuration[key]
        if kind == "min" and value < bound:
            out.append(
                f"configuration.{key}={value!r} is below the gate-eligible minimum {bound!r}"
            )
        elif kind == "max" and value > bound:
            out.append(
                f"configuration.{key}={value!r} is above the gate-eligible maximum {bound!r}"
            )
        elif kind == "is" and value is not bound:
            out.append(
                f"configuration.{key}={value!r} must be {bound!r} for a gate-eligible result"
            )
    return out


def gate_eligibility(
    spec: ExperimentSpec,
    frames: Mapping[int, pd.DataFrame] | None = None,
) -> GateEligibility:
    """Is ``spec``'s result admissible as promotion evidence (module docs)?

    ``frames`` is the dataset the spec runs on (the WHOLE feature store, as
    :func:`build_spec` receives it).  With it, the spec's periods must equal
    :func:`derive_periods` of that dataset; a dataset the periods cannot be
    derived from (fewer than two sessions) makes every period set
    caller-chosen.  Without ``frames`` only the configuration bounds are
    checked and ``periods_verified`` is ``False``.
    """
    reasons = _configuration_violations(spec.configuration)
    verified = frames is not None
    if frames is not None:
        try:
            derived = derive_periods(frames, spec.horizon, int(spec.configuration["embargo_ns"]))
        except ResearchError as exc:
            reasons.append(f"periods are caller-chosen: none can be derived ({exc})")
        else:
            given = (spec.train_period, spec.validation_period, spec.test_period)
            if tuple(p.to_dict() for p in given) != tuple(p.to_dict() for p in derived):
                reasons.append(
                    "periods are caller-chosen: they differ from the periods "
                    "derived from the dataset's session calendar"
                )
    return GateEligibility(eligible=not reasons, reasons=tuple(reasons), periods_verified=verified)


def experiment_id_of(body: Mapping[str, Any]) -> str:
    """The pinned id of a spec body (a spec dict WITHOUT ``experiment_id``)."""
    if "experiment_id" in body:
        raise _invalid("experiment_id_of: body must not carry experiment_id")
    return content_hash(dict(body))[:16]


def verify_experiment_id(spec: ExperimentSpec) -> None:
    """Raise unless ``spec.experiment_id`` is the hash of its own body."""
    body = spec.to_dict()
    del body["experiment_id"]
    want = experiment_id_of(body)
    if spec.experiment_id != want:
        raise _invalid(
            f"experiment_id {spec.experiment_id!r} does not match the spec body "
            f"(expected {want!r}): the document was edited or built by hand"
        )


def build_spec(
    alpha_id: str,
    horizon: str | None = None,
    configuration: Mapping[str, Any] | None = None,
    *,
    dataset_version: str | None = None,
    feature_version: str | None = None,
    model_version: str | None = None,
    seed: int = DEFAULT_SEED,
    train_period: Period | None = None,
    validation_period: Period | None = None,
    test_period: Period | None = None,
    frames: Mapping[int, pd.DataFrame] | None = None,
    repo_root: Path | None = None,
) -> ExperimentSpec:
    """A validated, id-stamped :class:`ExperimentSpec`.

    * ``horizon`` defaults to the alpha's pinned horizon; any pinned label
      horizon is accepted (a horizon scan is a legitimate experiment).
    * ``configuration`` is normalised by :func:`normalise_configuration`.
    * ``dataset_version`` / ``feature_version`` default to
      :func:`iap.experiment.tracker.data_version` /
      :func:`~iap.experiment.tracker.feature_version` for ``repo_root``
      (the checkout when ``None``); both must be sha256 digests.
    * ``model_version`` defaults to :func:`model_definition_hash`.
    * the three periods are given together or not at all; when absent they
      are derived from ``frames`` by :func:`derive_periods`.
    * ``seed`` is recorded and hashed into the id.  The pinned evidence
      chain is closed-form event-time arithmetic with no random element,
      so today no component consumes it; it exists so that a stochastic
      component added later (a bootstrap, a resampled stress) is pinned by
      the spec rather than by chance.
    """
    pinned = pinned_horizon(alpha_id)
    horizon = _check_horizon(horizon if horizon is not None else pinned)
    config = normalise_configuration(configuration)
    given = (train_period, validation_period, test_period)
    if any(p is None for p in given) and not all(p is None for p in given):
        raise _invalid("train/validation/test periods must be given together")
    if all(p is None for p in given):
        if frames is None:
            raise _invalid("no periods given and no frames to derive them from")
        train_period, validation_period, test_period = derive_periods(
            frames, horizon, config["embargo_ns"]
        )
    body: dict[str, Any] = {
        "alpha_id": alpha_id,
        "dataset_version": _version(
            dataset_version, tracker.data_version, "dataset_version", repo_root
        ),
        "feature_version": _version(
            feature_version, tracker.feature_version, "feature_version", repo_root
        ),
        "model_version": (
            model_version if model_version is not None else model_definition_hash(alpha_id, horizon)
        ),
        "configuration": config,
        "train_period": train_period.to_dict(),
        "validation_period": validation_period.to_dict(),
        "test_period": test_period.to_dict(),
        "seed": int(seed),
        "horizon": horizon,
    }
    try:
        spec = ExperimentSpec.from_dict({"experiment_id": experiment_id_of(body), **body})
        validate_typed(spec)
    except ValueError as exc:  # ContractError / ContractValidationError
        raise _invalid(f"invalid experiment spec: {exc}") from exc
    return spec
