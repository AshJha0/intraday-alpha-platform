"""Pre-registered net-of-cost screen over ALL in-sample folds (v1.17 C6).

::

    python -m iap.research.screen --dataset-dir DIR
        [--prereg research/preregs/net_of_cost_screen_prereg_DRAFT.json]
        [--methods v2] [--out research/breadth/screen] [--tag NAME]
        [--ledger PATH | --no-ledger] [--no-timestamp] [--max-rows N]

**What it is.**  Before any confirmatory look at a candidate signal, every
candidate of the DRAFT pre-registration (``alpha id x horizon x model
identity``) is screened ONCE on the in-sample folds, net of the modelled
cost at the registered size.  The screen is a filter, not evidence: a pass
says "worth a confirmatory look", a fail says "not at this size on these
days".  The C7 breadth report and the C8 pre-registration flow can be
told to refuse a confirmatory run on a candidate that failed or was never
screened (:func:`require_screened`; opt-in, default behaviour unchanged).

**The statistic** (the v1.17 gate form, :func:`iap.validation.metrics.gate_statistics`,
on the pooled test rows of every walk-forward fold of the methods bundle):

1. the model is fitted per fold on the purged, embargoed train rows and
   scored on the fold's test rows; the standardised signal ``z``
   (``expected_return / beta``, as the walk-forward gate pools it) and the
   scored label ``y`` of the candidate's horizon are pooled over ALL folds;
2. the modelled round-trip cost of trading the registered size on each row,
   as a return: :meth:`CostModel.round_trip_cost_return` (spread + fee,
   both legs) plus the impact of both legs at participation
   ``size * unit / adv`` under the cost model's impact rule;
3. ``gross``: ``gate_statistics(ts, z, y)``; ``net``: the same on the
   net-of-cost label ``y - sign(z) * cost`` - a unit position in the
   signal's direction on every scored row, charged its round trip - so the
   net IC, the net HAC t and the net day-clustered t are the gate statistic
   of the cost-adjusted label, not a new statistic;
4. ``net_return``: the mean of ``sign(z) * y - cost`` per scored row with
   the day-clustered t of that mean (one cluster per day, ``n_days - 1``
   degrees of freedom; :func:`day_cluster_mean`).

The pass / fail rules and every threshold come from the pre-registration
document; the code evaluates them and records each rule's outcome.  The
document is a DRAFT until the owner signs it: the screen says so in its
output (``prereg.signed``), and nothing here signs anything.

**R-P2-5 (the maker-filter target).**  The review found that the maker
study's filter target ``{side}_not_run_over`` conflates "not filled" with
"run over" (both are 0.0, so the filter learns to avoid fills), and that
the label orders rest for 1 s while the cells trade at the alpha's horizon
(label TTL != cell TTL).  The fix, as stated: the target is conditional on
a fill - :func:`maker_fill_target` reads the fill-conditional
``{side}_run_over`` label (:mod:`iap.labels.maker_labels`; NaN when the
order did not fill, so an unfilled row is neither a loss nor a win) - and
the label frame's TTL must equal the candidate's horizon (``attrs["ttl_ns"]``
or a ``ttl_ns`` column; a mismatch is refused, never silently used).  A
candidate with ``target: "maker_fill_conditional"`` is screened on that
target: its gross per-row return is the markout of the side the alpha
points to at the candidate's horizon, and its cost is the fee of both
legs (a maker crosses no spread) plus the modelled impact.  The maker
study driver (:mod:`iap.research.maker_real`) gains the same target and a
cell-TTL label mode, both opt-in.

**Looks.**  One ledger look per candidate, kind ``net_of_cost_screen``,
dataset-scoped, identity = the candidate, the pre-registration's sha256,
the size, the thresholds and the methods bundle at its statistics
revision.  **Holdouts.**  A dataset directory whose path contains
``holdout``, or whose ``dataset_version`` is one of the pre-registration's
``excluded_dataset_versions`` (every registered holdout), is refused
without ``--allow-holdout``; the breadth holdout (the 2025/26 Nasdaq days
for names outside AAPL / MSFT / QQQ; docs/PORTFOLIO_BREADTH.md) is never
in any candidates manifest and is listed there too.

Deterministic: the same inputs and arguments give the same JSON bytes
(``generated`` omitted under ``--no-timestamp``).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from iap.alpha.base import EPS, AlphaModel
from iap.backtest.costs import CostModel
from iap.governance.screen_gate import (  # noqa: F401  (re-exported: the historical home)
    SCHEMA,
    STATUS_FAIL,
    STATUS_NOT_SCREENED,
    STATUS_PASS,
    ScreenError,
    ScreenRefusal,
    _relative,
    base_alpha_id,
    file_sha256,
    load_screen,
    model_identity,
    require_screened,
    screen_status,
)
from iap.labels import HORIZONS_NS
from iap.labels.frames import DEFAULT_IC_ROWS, scored_labels
from iap.validation.ledger import ExperimentLedger
from iap.validation.methods import DEFAULT_METHODS, ResearchMethods, ledger_config, methods
from iap.validation.metrics import day_cluster_inference, gate_statistics
from iap.validation.splits import WalkForwardSplitter

_REPO = Path(__file__).resolve().parents[4]
NS_S = 1_000_000_000
NS_DAY = 86_400 * NS_S
PREREG_SCHEMA = "iap.prereg_draft/v1"
LEDGER_KIND = "net_of_cost_screen"
DEFAULT_PREREG = _REPO / "research" / "preregs" / "net_of_cost_screen_prereg_DRAFT.json"
DEFAULT_OUT = _REPO / "research" / "breadth" / "screen"
TARGETS = ("mid", "maker_fill_conditional")
#: The rules a pre-registration may name (``acceptance.rules``), each a
#: ``>=`` test on one statistic of the candidate document.
RULE_STATISTICS = {
    "net_ic_min": ("net", "ic"),
    "net_tstat_day_cluster_min": ("net", "tstat_day_cluster"),
    "net_tstat_hac_min": ("net", "tstat_hac"),
    "net_return_tstat_min": ("net_return", "tstat_day_cluster"),
    "net_return_mean_min": ("net_return", "mean"),
    "n_days_min": ("net", "n_days"),
    "n_pairs_min": ("net", "n_pairs"),
}


# ---------------------------------------------------------------- inputs --


@dataclass(frozen=True)
class Candidate:
    """One screened identity: ``alpha_id x horizon x model`` (+ target)."""

    alpha_id: str
    horizon: str
    model: str = "linear_z_v1"
    target: str = "mid"

    def __post_init__(self) -> None:
        if self.horizon not in HORIZONS_NS:
            raise ValueError(f"{self.alpha_id}: unknown horizon {self.horizon!r}")
        if self.target not in TARGETS:
            raise ValueError(f"{self.alpha_id}: unknown target {self.target!r}; known: {TARGETS}")

    @property
    def id(self) -> str:
        return f"{self.alpha_id}/{self.horizon}/{self.model}"

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "alpha_id": self.alpha_id,
            "horizon": self.horizon,
            "model": self.model,
            "target": self.target,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Candidate:
        return cls(
            str(d["alpha_id"]),
            str(d["horizon"]),
            str(d.get("model", "linear_z_v1")),
            str(d.get("target", "mid")),
        )


@dataclass(frozen=True)
class InstrumentCost:
    """What the cost of one row needs to know about its instrument."""

    adv: float
    asset_class: str = "EQUITY"
    lot_size: int = 1

    @property
    def unit(self) -> float:
        return float(self.lot_size) if self.asset_class == "FX" else 1.0


@dataclass(frozen=True)
class ScreenSpec:
    """The pre-registration's parameters, as the code reads them."""

    candidates: tuple[Candidate, ...]
    size_qty: float
    rules: Mapping[str, float]
    n_folds: int = 4
    embargo_ns: int = 60 * NS_S
    cost_multiplier: float = 1.0
    ic_rows: str = DEFAULT_IC_ROWS
    excluded_dataset_versions: tuple[str, ...] = ()
    prereg_sha256: str | None = None
    prereg_path: str | None = None
    prereg_status: str | None = None
    prereg_signed: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.size_qty <= 0:
            raise ValueError("size_qty must be positive")
        unknown = sorted(set(self.rules) - set(RULE_STATISTICS))
        if unknown:
            raise ValueError(
                f"unknown acceptance rules {unknown}; known: {sorted(RULE_STATISTICS)}"
            )
        if not self.rules:
            raise ValueError("a screen needs at least one acceptance rule")
        ids = [c.id for c in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate candidate ids")

    def to_dict(self) -> dict[str, Any]:
        return {
            "prereg": {
                "path": self.prereg_path,
                "sha256": self.prereg_sha256,
                "status": self.prereg_status,
                "signed": self.prereg_signed,
            },
            "size_qty": self.size_qty,
            "rules": dict(sorted(self.rules.items())),
            "n_folds": self.n_folds,
            "embargo_ns": self.embargo_ns,
            "cost_multiplier": self.cost_multiplier,
            "ic_rows": self.ic_rows,
            "excluded_dataset_versions": list(self.excluded_dataset_versions),
            "n_candidates": len(self.candidates),
        }


def load_prereg(path: str | Path) -> ScreenSpec:
    """The screen's parameters from a pre-registration document.

    The document is checked for the fields the code needs; it is not
    required to be signed (a DRAFT screens; the output says it is a draft),
    but ``signed`` must be an explicit boolean and a document claiming a
    signature without one is refused."""
    path = Path(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema") != PREREG_SCHEMA:
        raise ScreenError(f"{path}: schema {doc.get('schema')!r} != {PREREG_SCHEMA!r}")
    if doc.get("kind") != LEDGER_KIND:
        raise ScreenError(f"{path}: kind {doc.get('kind')!r} != {LEDGER_KIND!r}")
    signed = doc.get("signed")
    if not isinstance(signed, bool):
        raise ScreenError(f"{path}: 'signed' must be a boolean")
    if signed and not doc.get("signature"):
        raise ScreenError(f"{path}: signed without a signature")
    try:
        cands = tuple(Candidate.from_dict(c) for c in doc["candidates"])
        proto = doc.get("protocol", {})
        return ScreenSpec(
            candidates=cands,
            size_qty=float(doc["size"]["qty"]),
            rules={k: float(v) for k, v in doc["acceptance"]["rules"].items()},
            n_folds=int(proto.get("folds", 4)),
            embargo_ns=int(float(proto.get("embargo_s", 60)) * NS_S),
            cost_multiplier=float(proto.get("cost_multiplier", 1.0)),
            ic_rows=str(proto.get("ic_rows", DEFAULT_IC_ROWS)),
            excluded_dataset_versions=tuple(
                str(v) for v in doc.get("holdout_exclusions", {}).get("dataset_versions", [])
            ),
            prereg_sha256=file_sha256(path),
            prereg_path=_relative(path),
            prereg_status=str(doc.get("status")),
            prereg_signed=signed,
            extra={"hypothesis": doc.get("hypothesis"), "plan_item": doc.get("plan_item")},
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ScreenError(f"{path}: {exc}") from exc


# ----------------------------------------------------------- statistics --


def row_costs(
    mid: np.ndarray,
    half_spread: np.ndarray,
    cost_model: CostModel,
    instrument: InstrumentCost,
    size_qty: float,
    *,
    cross_spread: bool = True,
) -> np.ndarray:
    """Round-trip cost of ``size_qty`` units on each row, as a return.

    Spread + fee of both legs (:meth:`CostModel.round_trip_cost_return`;
    the fee alone when ``cross_spread`` is False - a maker order crosses no
    spread) plus the impact of both legs at participation
    ``size_qty * unit / adv`` under the cost model's impact rule, times the
    model's multiplier.  NaN where the row cannot be priced."""
    mid = np.asarray(mid, dtype=float)
    hs = np.asarray(half_spread, dtype=float)
    if not cross_spread:
        hs = np.where(np.isfinite(hs), 0.0, hs)
    fixed = cost_model.round_trip_cost_return(mid, hs, instrument.asset_class)
    if instrument.adv <= 0 or not np.isfinite(instrument.adv):
        return np.full(mid.shape, np.nan)
    participation = np.array([size_qty * instrument.unit / instrument.adv])
    impact = float(cost_model.impact_bps(participation)[0]) * 1e-4
    scale = cost_model.multiplier * cost_model.sensitivity_scale
    return fixed + 2.0 * scale * impact


def day_cluster_mean(ts: np.ndarray, values: np.ndarray, min_days: int = 2) -> dict[str, Any]:
    """Mean of ``values`` with one cluster per UTC day of ``ts``: the
    day-mean series, its standard error and t (``n_days - 1`` df), the
    Student-t p-value beside the normal one (report-only)."""
    ts = np.asarray(ts, dtype=np.int64)
    v = np.asarray(values, dtype=float)
    ok = np.isfinite(v)
    nan = float("nan")
    out: dict[str, Any] = {
        "mean": nan,
        "se": nan,
        "tstat_day_cluster": nan,
        "n_rows": int(ok.sum()),
        "n_days": 0,
        "day_means": [],
    }
    if not ok.any():
        return out
    days, inv = np.unique(ts[ok] // NS_DAY, return_inverse=True)
    sums = np.bincount(inv, weights=v[ok])
    counts = np.bincount(inv)
    day_means = sums / counts
    out["mean"] = float(v[ok].mean())
    out["n_days"] = int(days.size)
    out["day_means"] = [float(m) for m in day_means]
    if days.size >= max(2, min_days):
        se = float(day_means.std(ddof=1) / math.sqrt(days.size))
        out["se"] = se
        out["tstat_day_cluster"] = float(day_means.mean() / se) if se > 0 else nan
        out["inference"] = day_cluster_inference(out["tstat_day_cluster"], int(days.size))
    return out


def net_of_cost_statistics(
    ts: np.ndarray,
    z: np.ndarray,
    y: np.ndarray,
    cost: np.ndarray,
    *,
    horizon_ns: int,
    hac_bandwidth: str = "auto",
    day_separated: bool = True,
    instrument_ids: np.ndarray | None = None,
) -> dict[str, Any]:
    """The screen's three blocks on pooled rows (module docs, items 3-4).

    ``instrument_ids`` switches on the gate's per-instrument scaling
    (``standardisation="instrument"``): ``z`` and the labels (gross, net
    and the cost, like a label) are divided by each instrument's standard
    deviation before the IC blocks; the ``net_return`` block is in raw
    return units."""
    ts = np.asarray(ts, dtype=np.int64)
    z = np.asarray(z, dtype=float)
    y = np.asarray(y, dtype=float)
    c = np.asarray(cost, dtype=float)
    if not (ts.shape == z.shape == y.shape == c.shape):
        raise ValueError("ts/z/y/cost length mismatch")
    direction = np.sign(z)
    y_net = y - direction * c
    per_row = direction * y - c
    per_row = np.where(direction != 0.0, per_row, np.nan)
    zz, yg, yn = z, y, y_net
    if instrument_ids is not None:
        zz, yg, yn = _standardise_by_instrument(np.asarray(instrument_ids), z, y, y_net)
    kw = {"horizon_ns": horizon_ns, "hac_bandwidth": hac_bandwidth, "day_separated": day_separated}
    gross = gate_statistics(ts, zz, yg, **kw)
    net = gate_statistics(ts, zz, yn, **kw)
    net["inference_day_cluster"] = day_cluster_inference(
        float(net["tstat_day_cluster"]), int(net["n_days"])
    )
    finite_cost = c[np.isfinite(c) & np.isfinite(z) & np.isfinite(y)]
    return {
        "gross": gross,
        "net": net,
        "net_return": day_cluster_mean(ts, per_row),
        "cost": {
            "mean_round_trip_return": float(finite_cost.mean()) if finite_cost.size else None,
            "n_priced_rows": int(finite_cost.size),
        },
    }


def _standardise_by_instrument(ids: np.ndarray, x: np.ndarray, *labels: np.ndarray):
    """The gate's R-P2-2 scaling (``validate._standardise_by_instrument``):
    each instrument's ``x`` and labels divided by that instrument's standard
    deviation over its finite ``(x, first label)`` pairs."""
    xs = np.asarray(x, dtype=float).copy()
    ls = [np.asarray(v, dtype=float).copy() for v in labels]
    ok = np.isfinite(xs) & np.isfinite(ls[0])
    for iid in np.unique(ids):
        rows = ids == iid
        pairs = rows & ok
        if int(pairs.sum()) < 2:
            continue
        sx = float(np.std(xs[pairs]))
        sy = float(np.std(ls[0][pairs]))
        if sx > EPS:
            xs[rows] = xs[rows] / sx
        if sy > EPS:
            for v in ls:
                v[rows] = v[rows] / sy
    return (xs, *ls)


def maker_fill_target(
    z: np.ndarray, labels: pd.DataFrame, horizon: str, horizon_ns: int
) -> np.ndarray:
    """R-P2-5: the fill-conditional maker target of the side the alpha
    points to (``z > 0`` posts at the bid, ``z < 0`` at the ask), in the
    LABEL's orientation (a forward mid return, so ``sign(z) * y`` is the
    markout): ``{side}_markout_{horizon}_bps * 1e-4`` for a bid order and
    its negative for an ask order, where the label order FILLED, NaN where
    it did not (an unfilled order is not a loss).  The
    label frame's TTL (``attrs["ttl_ns"]`` or a ``ttl_ns`` column) must
    equal ``horizon_ns``: a label resting for another time than the cell
    trades is refused, never silently used."""
    ttl = labels.attrs.get("ttl_ns")
    if ttl is None and "ttl_ns" in labels.columns:
        col = labels["ttl_ns"].to_numpy()
        ttl = int(col[0]) if col.size and np.all(col == col[0]) else None
    if ttl is None:
        raise ScreenError("maker labels carry no ttl_ns (attrs or column); R-P2-5 needs it")
    if int(ttl) != int(horizon_ns):
        raise ScreenError(
            f"maker label TTL {int(ttl)} ns != cell horizon {int(horizon_ns)} ns ({horizon}); "
            "R-P2-5: the label must rest exactly as long as the cell trades"
        )
    z = np.asarray(z, dtype=float)
    out = np.full(z.shape, np.nan)
    for side, sign, mask in (("bid", 1.0, z > 0), ("ask", -1.0, z < 0)):
        col = f"{side}_markout_{horizon}_bps"
        if col not in labels.columns or f"{side}_filled" not in labels.columns:
            raise ScreenError(f"maker labels lack {col} / {side}_filled")
        filled = labels[f"{side}_filled"].to_numpy(dtype=bool)
        mk = sign * labels[col].to_numpy(dtype=float) * 1e-4
        take = mask & filled
        out[take] = mk[take]
    return out


# ------------------------------------------------------------ the screen --


def default_factories() -> dict[str, Callable[[str], AlphaModel]]:
    """Model identity -> builder of an unfitted model for an alpha id."""
    from iap.alpha import build
    from iap.alpha.linear_v2 import build_v2

    return {"linear_z_v1": build, "linear_z_v2": build_v2}


def _half_spread(df: pd.DataFrame) -> np.ndarray:
    mid = df["mid_price_v1"].to_numpy(dtype=float)
    if "spread_bps_v1" in df.columns:
        return 0.5 * mid * df["spread_bps_v1"].to_numpy(dtype=float) * 1e-4
    raise ScreenError("frames need spread_bps_v1 to price the spread")


def _apply_rules(stats: Mapping[str, Any], rules: Mapping[str, float]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, threshold in sorted(rules.items()):
        block, key = RULE_STATISTICS[name]
        value = stats[block][key]
        v = float(value) if value is not None else float("nan")
        out[name] = {
            "statistic": f"{block}.{key}",
            "value": v if np.isfinite(v) else None,
            "threshold": float(threshold),
            "passed": bool(np.isfinite(v) and v >= float(threshold)),
        }
    return out


def screen_candidate(
    candidate: Candidate,
    model_factory: Callable[[], AlphaModel],
    frames: Mapping[int, pd.DataFrame],
    instruments: Mapping[int, InstrumentCost],
    cost_model: CostModel,
    spec: ScreenSpec,
    bundle: ResearchMethods,
    *,
    maker_labels: Mapping[int, pd.DataFrame] | None = None,
) -> dict[str, Any]:
    """One candidate over ALL folds of the bundle's splitter (module docs).

    ``frames`` hold the features the model reads, the labels of the
    candidate's horizon, ``mid_price_v1`` and ``spread_bps_v1``;
    ``maker_labels`` (target ``maker_fill_conditional`` only) the maker
    label frames aligned row for row with ``frames``."""
    horizon_ns = int(HORIZONS_NS[candidate.horizon])
    probe = model_factory()
    universe = probe.universe(sorted(frames))
    uframes = {iid: frames[iid] for iid in universe}
    missing = [iid for iid in universe if iid not in instruments]
    if missing:
        raise ScreenError(f"{candidate.id}: no cost inputs for instruments {missing}")
    if not uframes:
        raise ScreenError(f"{candidate.id}: no instrument of the universe in the frames")
    splitter = WalkForwardSplitter(
        n_folds=spec.n_folds,
        embargo_ns=spec.embargo_ns,
        mode=bundle.split_mode,
        feature_window_ns=bundle.feature_window,
    )
    fold_rows: list[dict[str, Any]] = []
    ts_all: list[np.ndarray] = []
    z_all: list[np.ndarray] = []
    y_all: list[np.ndarray] = []
    c_all: list[np.ndarray] = []
    i_all: list[np.ndarray] = []
    for fold in splitter.split_folds(uframes):
        train, test = splitter.fold_frames(uframes, fold, horizon_ns)
        model = model_factory()
        model.horizon = candidate.horizon
        model.fit(train)
        del train
        scores = model.score(test)
        beta = float(model.params().get("beta", 0.0) or 0.0)
        n_fold = 0
        for iid, sc in scores.items():
            df = test[iid]
            er = sc["expected_return"].to_numpy(dtype=float).copy()
            er[sc["confidence"].to_numpy(dtype=float) <= 0.0] = np.nan
            z = er / beta if beta != 0.0 else er
            ts = df["exchange_ts"].to_numpy(dtype=np.int64)
            ins = instruments[iid]
            mid = df["mid_price_v1"].to_numpy(dtype=float)
            hs = _half_spread(df)
            if candidate.target == "mid":
                y, _ = scored_labels(df, candidate.horizon, spec.ic_rows)
                cost = row_costs(mid, hs, cost_model, ins, spec.size_qty)
            else:
                if maker_labels is None or iid not in maker_labels:
                    raise ScreenError(f"{candidate.id}: maker labels needed for {iid}")
                lab = maker_labels[iid]
                lab = lab[fold.test_mask(lab["exchange_ts"].to_numpy(dtype=np.int64))]
                lab = lab.reset_index(drop=True)
                if len(lab) != len(df):
                    raise ScreenError(f"{candidate.id}: maker labels misaligned for {iid}")
                y = maker_fill_target(z, lab, candidate.horizon, horizon_ns)
                cost = row_costs(mid, hs, cost_model, ins, spec.size_qty, cross_spread=False)
            ts_all.append(ts)
            z_all.append(z)
            y_all.append(y)
            c_all.append(cost)
            i_all.append(np.full(len(df), int(iid), dtype=np.int64))
            n_fold += int(np.sum(np.isfinite(z) & np.isfinite(y) & np.isfinite(cost)))
        fold_rows.append(
            {
                "fold": fold.index,
                "test_start": int(fold.test_start),
                "test_end": int(fold.test_end),
                "n_train": int(model.params().get("n_train", 0)),
                "n_test_pairs": n_fold,
                "beta": beta,
                "dead": bool(getattr(model, "is_dead", False)),
            }
        )
    ts = np.concatenate(ts_all)
    z = np.concatenate(z_all)
    y = np.concatenate(y_all)
    cost = np.concatenate(c_all)
    ids = np.concatenate(i_all)
    # Rows the gate scores: finite signal, label AND a priced cost.
    ok = np.isfinite(cost)
    y = np.where(ok, y, np.nan)
    stats = net_of_cost_statistics(
        ts,
        z,
        y,
        cost,
        horizon_ns=horizon_ns,
        hac_bandwidth=bundle.hac_bandwidth,
        day_separated=bundle.hac_day_separated,
        instrument_ids=ids if bundle.standardisation == "instrument" else None,
    )
    if int(stats["net"]["n_pairs"]) == 0:
        raise ScreenError(
            f"{candidate.id}: no scored (signal, label, cost) row on any fold - "
            "not screened, not failed"
        )
    rules = _apply_rules(stats, spec.rules)
    passed = all(r["passed"] for r in rules.values())
    failed = [k for k, r in rules.items() if not r["passed"]]
    return {
        **candidate.to_dict(),
        "status": STATUS_PASS if passed else STATUS_FAIL,
        "passed": passed,
        "failed_rules": failed,
        "rules": rules,
        "folds": fold_rows,
        "n_folds": len(fold_rows),
        **stats,
    }


def run_screen(
    spec: ScreenSpec,
    frames: Mapping[int, pd.DataFrame],
    instruments: Mapping[int, InstrumentCost],
    cost_model: CostModel,
    *,
    bundle: ResearchMethods | None = None,
    factories: Mapping[str, Callable[[str], AlphaModel]] | None = None,
    ledger: ExperimentLedger | None = None,
    maker_labels: Mapping[int, pd.DataFrame] | None = None,
    dataset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The screen document for every candidate of ``spec`` (module docs).

    ``ledger``, when given, is charged one look per candidate (kind
    ``net_of_cost_screen``) and saved; the document records the totals.
    A candidate whose model identity has no factory, or whose alpha the
    factory cannot build, is recorded as ``error`` and counts as not
    screened (it is still a look: the attempt was made)."""
    bundle = bundle or methods(DEFAULT_METHODS)
    factories = dict(factories or default_factories())
    cost_model = bundle.cost_model(cost_model)
    if spec.cost_multiplier != 1.0:
        cost_model = cost_model.with_multiplier(spec.cost_multiplier)
    rows: list[dict[str, Any]] = []
    identity_base = {
        "prereg_sha256": spec.prereg_sha256,
        "size_qty": spec.size_qty,
        "rules": dict(sorted(spec.rules.items())),
        "n_folds": spec.n_folds,
        "embargo_ns": spec.embargo_ns,
        "cost_multiplier": spec.cost_multiplier,
        "ic_rows": spec.ic_rows,
        "methods": bundle.name,
    }
    for cand in sorted(spec.candidates, key=lambda c: c.id):
        factory = factories.get(cand.model)
        try:
            if factory is None:
                raise ScreenError(f"{cand.id}: no builder for model {cand.model!r}")

            def make(f=factory, aid=cand.alpha_id) -> AlphaModel:
                return f(aid)

            row = screen_candidate(
                cand,
                make,
                frames,
                instruments,
                cost_model,
                spec,
                bundle,
                maker_labels=maker_labels,
            )
        except (ScreenError, ValueError, KeyError) as exc:
            row = {
                **cand.to_dict(),
                "status": STATUS_NOT_SCREENED,
                "passed": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if ledger is not None:
            config = ledger_config({**identity_base, "candidate": cand.to_dict()}, bundle)
            result = {
                "status": row["status"],
                "net_ic": _finite(row.get("net", {}).get("ic")),
                "net_tstat_day_cluster": _finite(row.get("net", {}).get("tstat_day_cluster")),
                "net_return_mean": _finite(row.get("net_return", {}).get("mean")),
            }
            ledger.record(cand.alpha_id, LEDGER_KIND, config=config, result=result)
        rows.append(row)
    if ledger is not None:
        ledger.save()
    n_pass = sum(1 for r in rows if r["status"] == STATUS_PASS)
    n_fail = sum(1 for r in rows if r["status"] == STATUS_FAIL)
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": LEDGER_KIND,
        "status": "in-sample screen; a filter before any confirmatory look, not evidence",
        "prereg_is_draft": not spec.prereg_signed,
        "spec": spec.to_dict(),
        "methods": {
            "bundle": bundle.name,
            "stats_revision": bundle.revision,
            "split_mode": bundle.split_mode,
            "hac_bandwidth": bundle.hac_bandwidth,
            "hac_day_separated": bundle.hac_day_separated,
            "standardisation": bundle.standardisation,
            "feature_window": bundle.feature_window,
            "impact_model": cost_model.impact_model,
        },
        "cost_model": {
            "impact_model": cost_model.impact_model,
            "sqrt_impact_coeff_bps": cost_model.sqrt_impact_coeff_bps,
            "impact_coeff_bps_per_pct_adv": cost_model.impact_coeff_bps_per_pct_adv,
            "equity_taker_fee_per_share": cost_model.equity_taker_fee_per_share,
            "multiplier": cost_model.multiplier,
        },
        "instruments": {
            str(i): {"adv": c.adv, "asset_class": c.asset_class, "lot_size": c.lot_size}
            for i, c in sorted(instruments.items())
        },
        "dataset": dict(dataset or {}),
        "candidates": rows,
        "summary": {
            "n_candidates": len(rows),
            "n_pass": n_pass,
            "n_fail": n_fail,
            "n_not_screened": len(rows) - n_pass - n_fail,
            "passed": sorted(r["id"] for r in rows if r["status"] == STATUS_PASS),
        },
        "ledger": (
            {
                "path": _relative(ledger.path),
                "kind": LEDGER_KIND,
                "looks_charged": len(rows),
                "total_experiments": ledger.total_experiments,
            }
            if ledger is not None
            else None
        ),
    }
    return doc


def _finite(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


# ------------------------------------------------------------- refusals --


# ------------------------------------------------------------- rendering --


def _f(v: Any, nd: int = 4) -> str:
    if v is None:
        return "n/a"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not np.isfinite(x):
        return "nan"
    return f"{x:.{nd}f}"


def _bps(v: Any) -> float | None:
    return None if v is None else float(v) * 1e4


def render_markdown(doc: Mapping[str, Any]) -> str:
    spec = doc["spec"]
    s = doc["summary"]
    lines = [
        "# Net-of-cost screen (v1.17 C6)",
        "",
        f"Status: {doc['status']}. Pre-registration: `{spec['prereg']['path']}` "
        f"(sha256 `{spec['prereg']['sha256']}`, "
        f"{'SIGNED' if not doc['prereg_is_draft'] else 'DRAFT, unsigned'}).",
        "",
        f"Methods `{doc['methods']['bundle']}` at statistics revision "
        f"`{doc['methods']['stats_revision']}`; size {spec['size_qty']:g} units per row; "
        f"{spec['n_folds']} folds, embargo {spec['embargo_ns'] / NS_S:g} s; "
        f"impact `{doc['cost_model']['impact_model']}`. "
        f"Dataset: `{doc['dataset'].get('dataset_version', 'n/a')}`.",
        "",
        f"**{s['n_pass']} pass, {s['n_fail']} fail, {s['n_not_screened']} not screened** "
        f"of {s['n_candidates']} candidates.",
        "",
        "| candidate | gross IC | net IC | net t (HAC) | net t (day) | net return / trip | "
        "cost / trip | days | pairs | result |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in doc["candidates"]:
        if r["status"] == STATUS_NOT_SCREENED:
            lines.append(f"| {r['id']} | | | | | | | | | not screened: {r.get('error')} |")
            continue
        nr = r["net_return"]
        res = "PASS" if r["passed"] else "fail (" + ", ".join(r["failed_rules"]) + ")"
        lines.append(
            f"| {r['id']} | {_f(r['gross']['ic'])} | {_f(r['net']['ic'])} | "
            f"{_f(r['net']['tstat_hac'], 2)} | {_f(r['net']['tstat_day_cluster'], 2)} | "
            f"{_f(_bps(nr['mean']), 3)} bps | "
            f"{_f(_bps(r['cost']['mean_round_trip_return']), 3)} bps | "
            f"{r['net']['n_days']} | {r['net']['n_pairs']} | {res} |"
        )
    lines += [
        "",
        "Rules (every one must hold): "
        + ", ".join(f"`{k} >= {v:g}`" for k, v in spec["rules"].items())
        + ".",
        "",
        "Reading: a pass means the candidate is worth ONE confirmatory look under its own "
        "pre-registration; a fail means no confirmatory run at this size on these days. "
        "Nothing here is evidence of an edge, nothing is signed, no holdout was read.",
    ]
    if doc.get("ledger"):
        lines += [
            "",
            f"Ledger: {doc['ledger']['looks_charged']} looks of kind `{LEDGER_KIND}` charged "
            f"to `{doc['ledger']['path']}` (total {doc['ledger']['total_experiments']}).",
        ]
    return "\n".join(lines) + "\n"


def sanitise(obj: Any) -> Any:
    """JSON-safe copy: NaN / inf -> None, numpy scalars -> Python."""
    if isinstance(obj, dict):
        return {str(k): sanitise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitise(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return f if np.isfinite(f) else None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


# ----------------------------------------------------------------- CLI --


def instruments_from_config(path: str | Path) -> dict[int, InstrumentCost]:
    """``InstrumentCost`` per instrument of an ``instruments.json`` (``adv``,
    else the mean of ``adv_by_session``)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[int, InstrumentCost] = {}
    for ins in doc["instruments"]:
        adv = ins.get("adv")
        if adv is None and ins.get("adv_by_session"):
            vals = [float(v) for v in dict(ins["adv_by_session"]).values()]
            adv = sum(vals) / len(vals) if vals else None
        out[int(ins["instrument_id"])] = InstrumentCost(
            adv=float(adv) if adv is not None else float("nan"),
            asset_class=str(ins.get("asset_class", "EQUITY")),
            lot_size=int(ins.get("lot_size", 1)),
        )
    return out


def assert_not_holdout(dataset_dir: Path | None, dataset_version: str, spec: ScreenSpec) -> None:
    if dataset_version in spec.excluded_dataset_versions:
        raise ScreenError(
            f"dataset {dataset_version[:16]}... is a registered holdout of the pre-registration"
        )
    if dataset_dir is not None:
        from iap.governance.holdout_registry import HoldoutRefused, guard

        try:  # the screen is in-sample only: no prereg id can authorise it (REL-02b)
            guard(dataset_dir, purpose="net_of_cost_screen")
        except HoldoutRefused as exc:
            raise ScreenError(str(exc)) from exc


def main(argv: Sequence[str] | None = None) -> int:
    from iap.alpha import configure_universe

    # ``_main`` points the process-global alpha universe at the dataset's
    # instruments.json; restore the pinned default so in-process callers
    # (tests, notebooks) do not inherit it.
    try:
        return _main(argv)
    finally:
        configure_universe(None)


def _main(argv: Sequence[str] | None) -> int:
    from iap.alpha import configure_universe
    from iap.alpha.data import load_features
    from iap.research.breadth_book import resolve_dataset

    ap = argparse.ArgumentParser(prog="python -m iap.research.screen", description=__doc__)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--dataset-dir", type=Path, default=None)
    src.add_argument("--features-dir", type=Path, default=None)
    ap.add_argument("--config-dir", type=Path, default=None)
    ap.add_argument("--prereg", type=Path, default=DEFAULT_PREREG)
    ap.add_argument("--methods", default=DEFAULT_METHODS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--ledger", type=Path, default=_REPO / "research" / "experiments.json")
    ap.add_argument("--no-ledger", action="store_true")
    ap.add_argument("--no-timestamp", action="store_true")
    ap.add_argument("--allow-holdout", action="store_true")
    ap.add_argument(
        "--max-rows", type=int, default=None, help="per-instrument row cap (smoke runs only)"
    )
    args = ap.parse_args(argv)

    spec = load_prereg(args.prereg)
    bundle = methods(args.methods)
    fdir, inst_path, exe_path, version = resolve_dataset(
        args.dataset_dir, args.features_dir, args.config_dir
    )
    if args.allow_holdout:
        # review finding 2: the flag only lifts the prereg's own exclusion list;
        # the holdout registry still decides (no prereg id can authorise the screen)
        assert_not_holdout(args.dataset_dir, version, replace(spec, excluded_dataset_versions=()))
    else:
        assert_not_holdout(args.dataset_dir, version, spec)
    configure_universe(inst_path)
    instruments = instruments_from_config(inst_path)
    cost_model = CostModel.load(exe_path)
    needed: set[str] = {"mid_price_v1", "spread_bps_v1", "spread_ticks_v1"}
    factories = default_factories()
    for cand in spec.candidates:
        f = factories.get(cand.model)
        if f is None:
            continue
        try:
            needed.update(f(cand.alpha_id).features)
        except (KeyError, ValueError):
            continue
        for key in ("label_mid", "label_valid", "label_reopen", "label_reason"):
            needed.add(f"{key}_{cand.horizon}")
    frames = load_features(
        fdir,
        list(instruments),
        columns=lambda cols: [
            c for c in cols if c in needed or not (c.endswith("_v1") or c.startswith("label_"))
        ],
    )
    if args.max_rows:
        frames = {i: df.head(args.max_rows).reset_index(drop=True) for i, df in frames.items()}
    ledger = None if args.no_ledger else ExperimentLedger(args.ledger, dataset_version=version)
    tag = args.tag or (args.dataset_dir.name if args.dataset_dir else "synthetic")
    doc = run_screen(
        spec,
        frames,
        instruments,
        cost_model,
        bundle=bundle,
        factories=factories,
        ledger=ledger,
        dataset={
            "tag": tag,
            "features_dir": _relative(fdir),
            "instruments": _relative(inst_path),
            "dataset_version": version,
            "max_rows": args.max_rows,
        },
    )
    if not args.no_timestamp:
        doc["generated"] = datetime.now(UTC).isoformat()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / f"screen_{tag}.json", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(sanitise(doc), indent=2, sort_keys=True, allow_nan=False) + "\n")
    with open(out_dir / f"SCREEN_{tag}.md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_markdown(doc))
    s = doc["summary"]
    print(f"wrote {out_dir / f'screen_{tag}.json'} and SCREEN_{tag}.md")
    print(
        f"candidates={s['n_candidates']} pass={s['n_pass']} fail={s['n_fail']} "
        f"not_screened={s['n_not_screened']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
