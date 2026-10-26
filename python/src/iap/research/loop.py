"""RL1, the research loop (v2.0 stage 7; U0 section 3 "Stage 7", plan section 15.2).

One function, :func:`run_family`, takes a U3 family specification
(``research/v2/families/<family>.md``), a daily panel with its manifest and a
partition name, and:

1. **refuses** anything but the development or validation partition
   (:func:`check_partition`: the partition name, the manifest's partition
   calendar, the process identity, which may not be the runner or a sealed
   identity, and the data path, which may not lie under a sealed root), a
   specification whose gate forbids a fit (``fit_allowed: false``) or whose
   routes exclude the panel's route;
2. builds the features through the data contracts
   (:mod:`iap.research.family_features`; every feature is aligned to the decision
   session by its contract's total availability lag) and the purged labels;
3. fits the M0 forecast (pooled linear, E0 section 4's analogue) on the
   estimation window and registers it (:mod:`iap.alpha.registry`), then produces
   forecasts through the forecast interface (:mod:`iap.alpha.forecast`);
4. runs the engine on the trading window: projection (:mod:`iap.portfolio.project`),
   daily auction execution (:mod:`iap.execution.sim`), the cohort and executable
   ledgers (:mod:`iap.portfolio.ledger`);
5. builds the E5 report (:mod:`iap.reports.e5`, blocks A-E, screen, decision
   tree) and hashes it (canonical JSON, floats rounded to 9 decimals);
6. records the run as an **analysis event with its outcome exposure** in the
   selection ledger (:class:`iap.validation.ledger.ExperimentLedger`).

The M0 forecaster is one :class:`Forecaster`; the ML framework (:mod:`iap.ml`)
supplies candidates through the same protocol and reuses :func:`run_engine` so
that a candidate and its incumbent are compared on identical sessions, capital,
cost column and constraints (plan V2-C3).

Nothing here reads a sealed partition, the network or the wall clock.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

from iap.alpha.forecast import ForecastRecord, consume, validate_horizon
from iap.alpha.registry import ForecastRegistry, RegistryEntry
from iap.contracts.versions import canonical_json, content_hash
from iap.data.contracts import contract_of, lineage
from iap.data.sources.base import current_identity, is_sealed_target
from iap.execution.sim import (
    COLUMNS,
    AuctionPrint,
    DailyImpactModel,
    FeeSchedule,
    simulate_daily,
)
from iap.portfolio.ledger import FamilyConfig, Ledger, TradeDecision, Valuation
from iap.portfolio.project import (
    ConstraintSpec,
    ProjectionResult,
    SolverIdentity,
    _constraints,
    check_book,
    construction_stages,
    feasibility,
    project,
)
from iap.reports.e5 import (
    SessionRow,
    SessionSeries,
    block_a_signal,
    block_b_portfolio,
    block_c_execution,
    block_d_risk_adjusted,
    block_e_sensitivity,
    check_no_inflation,
    decision_tree,
    economic_screen,
    session_series,
)
from iap.research import family_features as ff
from iap.validation.ledger import ExperimentLedger

__all__ = [
    "ALLOWED_PARTITIONS",
    "FORBIDDEN_IDENTITIES",
    "LEDGER_KIND",
    "SOFTWARE_VERSION",
    "FamilySpec",
    "Forecaster",
    "LinearM0",
    "PartitionRefused",
    "RunResult",
    "SpecError",
    "build_features",
    "build_report",
    "check_partition",
    "fit_m0",
    "forward_label",
    "load_family_spec",
    "load_panel",
    "polish_weights",
    "record_run",
    "report_hash",
    "run_engine",
    "run_family",
    "windows",
]

#: The two partitions the development identity may run on (U0 section 2).
ALLOWED_PARTITIONS = frozenset({"development", "validation"})
#: Identities that never run the research loop (the runner and the sealed owners).
FORBIDDEN_IDENTITIES = frozenset({"iap-runner", "iap-sealed-a", "iap-sealed-b", "iap-sealed-c"})
#: The selection-ledger kind of every research-loop run.
LEDGER_KIND = "analysis_event"
SOFTWARE_VERSION = "iap v2.0 stage 7"
CAPITAL = 1_000_000.0
#: Planning share of the full-session volume printed in the close auction (labelled estimated).
AUCTION_SHARE_OF_ADV = 0.08
NS_PER_S = 1_000_000_000
OPEN_S = 9 * 3600 + 30 * 60
CLOSE_S = 16 * 3600
#: Decimals kept in the hashed report (bps and fractions of C): coarse enough that solver
#: noise across platforms (SLSQP, BLAS) does not move the hash, fine enough to pin the result.
ROUND = 6
#: Decimals for the stage-book rows of block B, whose every value descends from solver weights.
ROUND_SOLVER = 4
#: A constraint whose slack at the solver's point is within this is active for the polish (D-85).
POLISH_ACTIVE_TOL = 1e-7
#: The polished point must lie within this of the solver's (otherwise the active set was misread).
POLISH_MAX_SHIFT = 1e-5

_SPEC_SECTIONS = (
    "Hypothesis",
    "Mechanism",
    "Artefacts that could fake it",
    "Falsifier",
    "Data gate and G4 checklist outcome",
    "Label definition",
    "Eligibility rule",
    "Development diagnostics",
    "Registration",
    "Machine-readable block",
)
_SPEC_KEYS = (
    "family_id",
    "name",
    "class",
    "status",
    "fit_allowed",
    "evidence_class",
    "data_gate",
    "routes",
    "horizons",
    "primary_horizon",
    "label",
    "features",
    "eligibility",
    "live_cohorts",
    "registered_family_size",
    "profile",
    "diagnostics",
)
_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)\n```", re.S)
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?%?")


class SpecError(ValueError):
    """A family specification file is incomplete or inconsistent."""


class PartitionRefused(PermissionError):
    """The run would touch something other than the development or validation partition."""


# --------------------------------------------------------------------------- specification
@dataclass(frozen=True, slots=True)
class FamilySpec:
    path: Path
    sha256: str
    doc: dict[str, Any]

    @property
    def family_id(self) -> str:
        return str(self.doc["family_id"])

    @property
    def fit_allowed(self) -> bool:
        return bool(self.doc["fit_allowed"])

    @property
    def primary_horizon(self) -> str:
        return str(self.doc["primary_horizon"])

    @property
    def horizon_sessions(self) -> int:
        return int(self.doc["label"]["horizon_sessions"])

    @property
    def purge_sessions(self) -> int:
        return int(self.doc["label"]["purge_sessions"])

    @property
    def eligibility(self) -> dict[str, Any]:
        return dict(self.doc["eligibility"])

    @property
    def signal_feature(self) -> str:
        feats = list(self.doc["features"])
        return str(feats[0]) if feats else ""

    @property
    def exit_at_open(self) -> bool:
        return self.primary_horizon == "c2o"


def load_family_spec(path: str | Path) -> FamilySpec:
    """Read a U3 file: every section present, one JSON block with every key, consistent gate."""
    p = Path(path)
    data = p.read_bytes()
    text = data.decode("utf-8")
    for sec in _SPEC_SECTIONS:
        if not re.search(rf"^## \d+\. .*{re.escape(sec)}", text, re.M | re.I):
            raise SpecError(f"{p.name}: section {sec!r} missing")
    blocks = _JSON_BLOCK.findall(text)
    if len(blocks) != 1:
        raise SpecError(f"{p.name}: exactly one ```json block expected, found {len(blocks)}")
    try:
        doc = json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        raise SpecError(f"{p.name}: machine-readable block is not JSON: {exc}") from None
    missing = [k for k in _SPEC_KEYS if k not in doc]
    if missing:
        raise SpecError(f"{p.name}: machine-readable block lacks {missing}")
    if not re.match(r"^[a-z][a-z0-9_]*$", str(doc["family_id"])):
        raise SpecError(f"{p.name}: family_id must be a lower-case identifier")
    for h in doc["horizons"]:
        validate_horizon(h)
    validate_horizon(doc["primary_horizon"])
    if doc["primary_horizon"] not in doc["horizons"]:
        raise SpecError(f"{p.name}: primary_horizon is not one of horizons")
    gate = doc["data_gate"].get("outcome")
    if gate in ("ineligible", "gated") and doc["fit_allowed"]:
        raise SpecError(f"{p.name}: gate outcome {gate!r} with fit_allowed true")
    if doc["status"] == "data-ineligible" and (doc["fit_allowed"] or doc["routes"]):
        raise SpecError(f"{p.name}: a data-ineligible family has no fit and no route")
    if doc["fit_allowed"] and not doc["features"]:
        raise SpecError(f"{p.name}: a family that may be fitted names its signal feature")
    if doc["profile"] not in ("PF-1", "PF-2"):
        raise SpecError(f"{p.name}: profile must be PF-1 or PF-2 (U13)")
    if int(doc["registered_family_size"]) < 1 or int(doc["live_cohorts"]) < 1:
        raise SpecError(f"{p.name}: registered_family_size and live_cohorts are >= 1")
    return FamilySpec(p, hashlib.sha256(data).hexdigest(), doc)


# --------------------------------------------------------------------------- data and partitions
def load_panel(path: str | Path) -> tuple[pd.DataFrame, str]:
    """Read the CSV panel; returns the frame sorted by (session, name) and the file's sha256."""
    p = Path(path)
    data = p.read_bytes()
    frame = pd.read_csv(p)
    missing = [c for c in ff.REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise SpecError(f"panel lacks columns {missing}")
    frame = frame.sort_values(["session", "name"], kind="stable").reset_index(drop=True)
    return frame, hashlib.sha256(data).hexdigest()


def check_partition(
    partition: str,
    manifest: Mapping[str, Any],
    data_path: str | os.PathLike[str],
    *,
    identity: str | None = None,
    data_sha256: str | None = None,
) -> dict[str, Any]:
    """Fail closed (U0 section 2): identity, path, partition name and the manifest's calendar."""
    who = current_identity() if identity is None else identity
    if who in FORBIDDEN_IDENTITIES or who.startswith("iap-sealed"):
        raise PartitionRefused(
            f"the research loop runs as the development identity only; this process is {who!r}"
        )
    if is_sealed_target(data_path):
        raise PartitionRefused(
            f"data path {os.fspath(data_path)!r} lies under a sealed partition root"
        )
    if partition not in ALLOWED_PARTITIONS:
        raise PartitionRefused(
            f"partition {partition!r} refused: the loop runs on {sorted(ALLOWED_PARTITIONS)} only"
        )
    if manifest.get("sealed", False):
        raise PartitionRefused("the manifest marks this dataset sealed")
    parts = manifest.get("partitions")
    if not isinstance(parts, Mapping) or partition not in parts:
        raise PartitionRefused(
            f"the manifest has no {partition!r} partition (it has {sorted(parts or {})})"
        )
    for name in parts:
        if name not in ALLOWED_PARTITIONS:
            raise PartitionRefused(
                f"the manifest carries partition {name!r}: a file that holds a sealed partition is never opened here"
            )
    if data_sha256 is not None and manifest.get("sha256") not in (None, data_sha256):
        raise PartitionRefused(
            "the panel's sha256 differs from the manifest's: provenance unverified"
        )
    return {"identity": who, "partition": partition, "range": list(parts[partition])}


def windows(
    frame: pd.DataFrame, manifest: Mapping[str, Any], partition: str, spec: FamilySpec
) -> tuple[list[str], list[str]]:
    """(estimation sessions, trading sessions).

    Development: the first 60 % of the development partition estimates, the rest
    trades, with a purge of the label horizon between them.  Validation: the
    whole development partition estimates (purged at its end), the validation
    partition trades; the embargo between them is the manifest's calendar gap.
    """
    cal = sorted(frame["session"].unique().tolist())
    parts = manifest["partitions"]
    dev_lo, dev_hi = parts["development"]
    dev = [s for s in cal if dev_lo <= s <= dev_hi]
    h = spec.horizon_sessions
    if partition == "development":
        cut = int(math.floor(0.6 * len(dev)))
        est = dev[: max(cut - h, 0)]
        trade = dev[cut:]
    else:
        lo, hi = parts[partition]
        est = dev[: max(len(dev) - h, 0)]
        trade = [s for s in cal if lo <= s <= hi]
    if len(est) < 30 or not trade:
        raise SpecError("too few sessions in the estimation or trading window")
    return est, trade


# --------------------------------------------------------------------------- features and labels
def build_features(frame: pd.DataFrame, feature_ids: Sequence[str]) -> pd.DataFrame:
    """Feature columns through the contracts; each column is shifted by its contract's total lag per name."""
    out = frame[["session", "name"]].copy()
    fns = {
        "common.beta_60": ff.feature_common_beta_60,
        "common.sigma_60": ff.feature_common_sigma_60,
        "common.adv_20": ff.feature_common_adv_20,
        "d1.reversal_z": ff.feature_d1_reversal_z,
        "d1.reversal_z1": ff.feature_d1_reversal_z1,
        "d2.residual_momentum": ff.feature_d2_residual_momentum,
        "d3.overnight_spread": ff.feature_d3_overnight_spread,
        "d4.filing_reaction_z": ff.feature_d4_filing_reaction_z,
    }
    for fid in ("common.beta_60", "common.sigma_60", "common.adv_20", *feature_ids):
        if fid in out.columns:
            continue
        fn = fns.get(fid)
        if fn is None:
            raise SpecError(f"no contracted feature function for {fid!r}")
        c = contract_of(fn)
        lag = c.total_lag_periods
        values = fn(frame)
        out[fid] = values.groupby(frame["name"], sort=False).shift(lag)
    if "member" in frame.columns:
        # a point-in-time panel (iap.research.dd1_panel): rows outside the universe feed the
        # trailing windows but are never fitted or traded, so their signals are blanked
        outside = frame["member"].to_numpy() != 1
        for fid in out.columns[2:]:
            if not fid.startswith("common."):
                out.loc[outside, fid] = np.nan
    return out


def forward_label(frame: pd.DataFrame, horizon_sessions: int, *, exit_at_open: bool) -> pd.Series:
    """Forward factor-adjusted return in bps from the close of the row's session (U3 label definition)."""
    g = frame.groupby("name", sort=False)
    beta = ff.feature_common_beta_60(frame)
    close = frame["close"].astype(float)
    if exit_at_open:
        nxt_open = g["open"].shift(-1).astype(float)
        own = nxt_open / close - 1.0
        mkt = g["mkt_c2o"].shift(-1).astype(float)
    else:
        h = horizon_sessions
        own = g["close"].shift(-h).astype(float) / close - 1.0
        logm = np.log1p(frame["mkt_ret"].astype(float))
        csum = logm.groupby(frame["name"], sort=False).cumsum()
        ahead = csum.groupby(frame["name"], sort=False).shift(-h)
        mkt = np.expm1(ahead - csum)
    return 1e4 * (own - beta * mkt)


# --------------------------------------------------------------------------- forecasts
class Forecaster(Protocol):
    """What the engine consumes: a registered model that maps a session's feature rows to forecasts."""

    model_class: str
    registry_id: str

    def predict(self, rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """(mu_hat_bps, uncertainty_bps) aligned to ``rows``; NaN = no forecast."""


@dataclass(slots=True)
class LinearM0:
    """M0: the pooled linear forecast mu = rho * z with a fail-closed quality rule (E0 section 4 analogue)."""

    feature: str
    rho: float
    se: float
    n: int
    passed: bool
    resid_sd_bps: float
    model_class: str = "M0.pooled_linear"
    registry_id: str = "0" * 64

    def predict(self, rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        z = rows[self.feature].to_numpy(dtype=float)
        if not self.passed:
            return np.full(z.shape, np.nan), np.full(z.shape, np.nan)
        return self.rho * z, np.abs(z) * self.se + self.resid_sd_bps

    def parameters(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "rho_bps_per_z": self.rho,
            "se": self.se,
            "n": self.n,
            "passed": self.passed,
            "resid_sd_bps": self.resid_sd_bps,
        }


def fit_m0(
    feats: pd.DataFrame, label: pd.Series, est_sessions: Sequence[str], feature: str
) -> LinearM0:
    """Pooled OLS of the label on the signal over the estimation window, purged rows only; t >= 2 to pass."""
    mask = feats["session"].isin(set(est_sessions)).to_numpy()
    z = feats.loc[mask, feature].to_numpy(dtype=float)
    y = label.to_numpy(dtype=float)[mask]
    ok = np.isfinite(z) & np.isfinite(y)
    z, y = z[ok], y[ok]
    n = int(z.size)
    if n < 30 or float((z * z).sum()) <= 0:
        return LinearM0(feature, 0.0, 0.0, n, False, 0.0)  # not fitted: fails closed, no forecast
    rho = float((z * y).sum() / (z * z).sum())
    resid = y - rho * z
    s2 = float((resid * resid).sum() / max(n - 1, 1))
    se = math.sqrt(s2 / float((z * z).sum()))
    passed = bool(se > 0 and rho / se >= 2.0 and rho > 0)
    return LinearM0(feature, rho, se, n, passed, math.sqrt(s2))


def _registry_entry(
    spec: FamilySpec, model: Any, est: Sequence[str], dataset_identity: str, history: dict
) -> RegistryEntry:
    return RegistryEntry(
        spec.family_id,
        spec.primary_horizon,
        model.model_class,
        model.parameters(),
        content_hash([lineage(f) for f in (spec.signal_feature,)]),
        (est[0], est[-1]),
        dataset_identity,
        SOFTWARE_VERSION,
        selection_history=history,
    )


# --------------------------------------------------------------------------- the polish (D-85)
def _solve_consistent(g: list[list[float]], rhs: list[float], eps: float) -> list[float] | None:
    """Gauss-Jordan in plain Python floats: one solution of ``g x = rhs`` (free unknowns 0), None if inconsistent."""
    k = len(g)
    m = [row[:] + [rhs[i]] for i, row in enumerate(g)]
    pivots: list[tuple[int, int]] = []
    r = 0
    for c in range(k):
        best = max(range(r, k), key=lambda i: abs(m[i][c]), default=None)
        if best is None or abs(m[best][c]) <= eps:
            continue
        m[r], m[best] = m[best], m[r]
        pv = m[r][c]
        m[r] = [v / pv for v in m[r]]
        for i in range(k):
            if i != r and m[i][c] != 0.0:
                f = m[i][c]
                m[i] = [a - f * b for a, b in zip(m[i], m[r], strict=True)]
        pivots.append((r, c))
        r += 1
        if r == k:
            break
    for i in range(r, k):
        if abs(m[i][k]) > eps:
            return None
    x = [0.0] * k
    for row, col in pivots:
        x[col] = m[row][k]
    return x


def polish_weights(
    res: ProjectionResult,
    w_raw: Mapping[str, float],
    betas: Mapping[str, float],
    sectors: Mapping[str, str],
    *,
    spec: ConstraintSpec,
    liquidity_caps: Mapping[str, float],
    solver: SolverIdentity,
) -> dict[str, float]:
    """The repair re-solved exactly on the active set the solver found, in plain Python arithmetic.

    SLSQP's point varies by ~1e-8 with the BLAS kernel of the machine (AVX-512 runners
    against AVX2 ones), and the integer-share path amplifies that into a one-share flip
    whenever ``|w| C / close`` lands within that of a half share (D-85).  The minimiser
    of ``|w - w_raw|^2`` subject to the constraints that are active at the solver's point
    is a small linear system; solved here without numpy or BLAS it is the same floats on
    every CPU.  Falls back to the solver's weights when the active set is inconsistent or
    the polished point moves further than ``POLISH_MAX_SHIFT`` (the active set was misread).
    """
    names = tuple(res.names)
    w = [float(res.weights[n]) for n in names]
    raw = np.array([float(w_raw[n]) for n in names])
    beta = np.array([float(betas[n]) for n in names])
    caps = np.array([float(liquidity_caps.get(n, math.inf)) for n in names])
    rows, bounds = _constraints(names, np.sign(raw), beta, [sectors[n] for n in names], spec, caps)
    k = len(names)
    c_rows: list[list[float]] = []
    d: list[float] = []
    for _label, kind, a, b in rows:
        a_list = [float(v) for v in a]
        v = sum(ai * wi for ai, wi in zip(a_list, w, strict=True))
        if kind == "eq" or b - v <= POLISH_ACTIVE_TOL:
            c_rows.append(a_list)
            d.append(float(b))
    for i, (lo, hi) in enumerate(bounds):
        at = None
        if hi == lo:
            at = hi
        elif hi > 0 and hi - w[i] <= POLISH_ACTIVE_TOL:
            at = hi
        elif lo < 0 and w[i] - lo <= POLISH_ACTIVE_TOL:
            at = lo
        if at is not None:
            c_rows.append([1.0 if j == i else 0.0 for j in range(k)])
            d.append(float(at))
    raw_l = [float(v) for v in raw]
    if c_rows:
        gram = [[sum(a * b for a, b in zip(ri, rj, strict=True)) for rj in c_rows] for ri in c_rows]
        rhs = [
            di - sum(a * r for a, r in zip(ri, raw_l, strict=True))
            for ri, di in zip(c_rows, d, strict=True)
        ]
        scale = max((abs(v) for row in gram for v in row), default=1.0) or 1.0
        lam = _solve_consistent(gram, rhs, 1e-12 * scale)
        if lam is None:
            return dict(res.weights)
        pol = [
            r + sum(lm * ri[j] for lm, ri in zip(lam, c_rows, strict=True))
            for j, r in enumerate(raw_l)
        ]
    else:
        pol = raw_l
    pol = [min(max(v, lo), hi) for v, (lo, hi) in zip(pol, bounds, strict=True)]
    if max(abs(a - b) for a, b in zip(pol, w, strict=True)) > POLISH_MAX_SHIFT:
        return dict(res.weights)
    for _label, kind, a, b in rows:
        v = sum(float(ai) * wi for ai, wi in zip(a, pol, strict=True))
        if (abs(v - b) if kind == "eq" else v - b) > solver.constraint_tol:
            return dict(res.weights)
    return {n: v for n, v in zip(names, pol, strict=True)}


# --------------------------------------------------------------------------- engine
def _ts(session: str, seconds: int) -> int:
    d = _dt.date.fromisoformat(session)
    epoch = (d - _dt.date(1970, 1, 1)).days * 86_400 + seconds
    return epoch * NS_PER_S


def _daily_cost_bps(
    qty: int, price: float, sigma: float, adv: float, model: DailyImpactModel, fees: FeeSchedule
) -> float:
    col = COLUMNS["conservative"]
    q = abs(qty)
    if q == 0:
        return 0.0
    impact = col.impact_multiplier * model.c * sigma * math.sqrt(q / max(adv, 1.0)) * price
    return 1e4 * (impact * q + fees.fee(q, price)) / (q * price)


@dataclass(slots=True)
class RunResult:
    family_id: str
    horizon: str
    capital: float
    series: SessionSeries
    rows: list[SessionRow]
    waterfalls: list[dict[str, float]]
    refusals: dict[str, int]
    refused_alpha_bps: dict[str, float]
    refused_shares: dict[str, int]
    cushions_bps: list[float]
    deployed_gross: list[float]
    turnover: list[float]
    stage_books: dict[str, dict[str, float]]
    last_cohort: dict[str, Any]
    research_ledger: list[dict[str, Any]]
    fills: int
    cohorts_opened: int
    signal_rows: pd.DataFrame = field(repr=False, default_factory=pd.DataFrame)


def run_engine(
    frame: pd.DataFrame,
    feats: pd.DataFrame,
    spec: FamilySpec,
    model: Forecaster,
    registry: ForecastRegistry,
    trade_sessions: Sequence[str],
    *,
    capital: float = CAPITAL,
    eligibility_overrides: Mapping[str, Any] | None = None,
) -> RunResult:
    """Projection, execution and ledger over ``trade_sessions`` with ``model``'s forecasts (one cost column).

    ``eligibility_overrides`` are the fixture-size parameters a synthetic manifest
    declares (a 60-name panel cannot meet a 500-name universe's names-per-leg
    rule); they are recorded in the report's identity and never apply on a real route.
    """
    el = {**spec.eligibility, **dict(eligibility_overrides or {})}
    h = spec.horizon_sessions
    live = int(spec.doc["live_cohorts"])
    gross_target = 1.0 / live
    rebalance_every = int(el.get("rebalance_every", 1))
    name_cap = float(el.get("name_cap", 0.05))
    sector_cap = float(el.get("sector_cap", 0.10))
    cspec = ConstraintSpec(
        beta_tol=0.05,
        sector_cap=sector_cap / live,
        name_cap=name_cap / live,
        gross_cap=gross_target,
        min_names_per_leg=int(el.get("min_names_per_leg", 10)),
        min_deployed_fraction=0.5,
        max_name_share_of_gross=float(el.get("max_name_share_of_gross", 0.15)),
    )
    book_spec = ConstraintSpec(
        0.05 * live,
        sector_cap,
        name_cap,
        1.0,
        cspec.min_names_per_leg,
        0.5,
        cspec.max_name_share_of_gross,
    )
    solver = SolverIdentity()
    model_daily = DailyImpactModel()
    fees = FeeSchedule.default()  # the dated engine_default schedule (D-07)
    participation_cap = 0.05
    hurdle = float(el.get("hurdle_multiple", 1.5))
    min_abs_z = float(el.get("min_abs_z", 0.0))
    tails = float(el.get("tails", 0.2))
    fam = spec.family_id
    horizon = spec.primary_horizon
    sig = spec.signal_feature

    led = Ledger()
    led.register_family(FamilyConfig(fam, flat_at_close=False))
    led.deposit(capital)
    cal = sorted(frame["session"].unique().tolist())
    idx = {s: i for i, s in enumerate(cal)}
    by_session = {s: g for s, g in frame.groupby("session", sort=True)}
    feat_by_session = {s: g for s, g in feats.groupby("session", sort=True)}
    sectors_all = dict(zip(frame["name"], frame["sector"], strict=True))

    rows: list[SessionRow] = []
    waterfalls: list[dict[str, float]] = []
    refusals: dict[str, int] = {}
    refused_alpha: dict[str, float] = {}
    refused_shares: dict[str, int] = {}
    cushions: list[float] = []
    deployed: list[float] = []
    turnover: list[float] = []
    stage_books: dict[str, dict[str, float]] = {}
    last_cohort: dict[str, Any] = {}
    fills = 0
    opened = 0
    cohort_id = 1
    signal_rows: list[pd.DataFrame] = []

    def refuse(reason: str, key: str, shares: int = 0, alpha: float = 0.0) -> None:
        key = _NUMBER.sub("#", key).strip()  # one key per cause, whatever the numbers
        refusals[key] = refusals.get(key, 0) + 1
        refused_shares[key] = refused_shares.get(key, 0) + shares
        refused_alpha[key] = refused_alpha.get(key, 0.0) + alpha

    def execute(ts: int, prices: Mapping[str, float], volumes: Mapping[str, int], f: pd.DataFrame):
        nonlocal fills
        decision = led.decide(ts, prices)
        traded_notional = 0.0
        # one lookup per session, not a scan of the frame per order (D3 fills most names daily)
        sig_of = dict(zip(f["name"], f["sigma"].astype(float), strict=True))
        adv_of = dict(zip(f["name"], f["adv"].astype(float), strict=True))
        for n in sorted(decision.orders):
            o = decision.orders[n]
            if o.submitted == 0:
                continue
            sigma = sig_of[n] if np.isfinite(sig_of[n]) else 0.02
            adv = adv_of[n] if np.isfinite(adv_of[n]) else 1.0
            out = simulate_daily(
                ts_decision=ts,
                name=n,
                qty=int(o.submitted),
                mid_decision=float(prices[n]),
                auction=AuctionPrint(ts, float(prices[n]), int(volumes[n]), sigma, int(adv)),
                model=model_daily,
                fees=fees,
                participation_cap=participation_cap,
                mandatory=o.mandatory != 0,
            )
            if out.fill is not None:
                led.fill(out.fill)
                fills += 1
                traded_notional += abs(out.fill.qty) * out.fill.price
            if out.refused and not out.fill:
                refuse(out.reason, "execution: " + out.reason.split(":")[0], abs(out.refused))
        led.settle()
        return traded_notional

    for s in trade_sessions:
        i = idx[s]
        panel = by_session[s]
        f = feat_by_session[s].merge(
            panel[["name", "close", "open", "auction_volume", "open_volume", "sector"]], on="name"
        )
        f = f.assign(sigma=f["common.sigma_60"], beta=f["common.beta_60"], adv=f["common.adv_20"])
        f = f.sort_values("name").reset_index(drop=True)
        closes = dict(zip(f["name"], f["close"].astype(float), strict=True))
        opens = dict(zip(f["name"], f["open"].astype(float), strict=True))
        led.open_session(s)
        traded = 0.0
        # the open auction: exits of cohorts expiring at the open (c2o families)
        ts_open = _ts(s, OPEN_S)
        if any(
            c.expiry_ts <= ts_open and c.state.value in ("live", "closing")
            for c in led.cohorts.values()
        ):
            traded += execute(
                ts_open, opens, dict(zip(f["name"], f["open_volume"].astype(int), strict=True)), f
            )
        # the close auction: entries (and close-to-close exits)
        ts_close = _ts(s, CLOSE_S)
        decide_today = (i % rebalance_every) == 0 and sig in f.columns
        if decide_today:
            mu, unc = model.predict(f)
            f = f.assign(mu=mu, unc=unc)
            keep = (
                np.isfinite(f["mu"])
                & np.isfinite(f["sigma"])
                & np.isfinite(f["beta"])
                & np.isfinite(f["adv"])
                & (f["sigma"] > 0)
                & (f[sig].abs() >= min_abs_z)
            )
            cand = f.loc[keep].copy()
            signal_rows.append(f.assign(session=s))
            key = (fam, horizon, cohort_id)
            cohort_id += 1
            if spec.exit_at_open:
                expiry = _ts(cal[i + 1], OPEN_S) if i + 1 < len(cal) else ts_close + 1
            else:
                expiry = _ts(cal[i + h], CLOSE_S) if i + h < len(cal) else ts_close + 1
            reason = ""
            records = [
                ForecastRecord(
                    fam,
                    str(r.name_),
                    ts_close,
                    horizon,
                    float(r.mu),
                    float(r.unc),
                    model.registry_id,
                )
                for r in cand.rename(columns={"name": "name_"}).itertuples()
            ]
            forecasts = {
                r.name: r.mu_hat_bps
                for r in consume(records, t_decision=ts_close, registry=registry)
            }
            w0 = gross_target / 2.0 / max(int(math.floor(tails * len(cand))), 1)
            decisions: dict[str, TradeDecision] = {}
            eligible: list[str] = []
            costs_bps: dict[str, float] = {}
            for r in cand.itertuples():
                n = r.name
                q0 = int(math.floor(w0 * capital / closes[n] + 0.5))
                ce = _daily_cost_bps(q0, closes[n], float(r.sigma), float(r.adv), model_daily, fees)
                costs_bps[n] = ce
                decisions[n] = TradeDecision(
                    float(cand.loc[r.Index, sig]), abs(forecasts[n]), ce, ce
                )
                if abs(forecasts[n]) > hurdle * 2.0 * ce:
                    eligible.append(n)
                else:
                    refuse("hurdle", "hurdle", q0, abs(forecasts[n]) * w0)
            n_leg = int(math.floor(tails * len(eligible)))
            if n_leg < cspec.min_names_per_leg:
                reason = f"fewer than {cspec.min_names_per_leg} names per leg ({len(eligible)} eligible after the hurdle)"
            else:
                ranked = sorted(eligible, key=lambda n: (-forecasts[n], n))
                longs = [n for n in ranked[:n_leg] if forecasts[n] > 0]
                shorts = [n for n in ranked[-n_leg:] if forecasts[n] < 0]
                if len(longs) < cspec.min_names_per_leg or len(shorts) < cspec.min_names_per_leg:
                    reason = (
                        f"fewer than {cspec.min_names_per_leg} names per leg after the sign rule"
                    )
            if not reason:
                sig_of = dict(zip(cand["name"], cand["sigma"].astype(float), strict=True))
                raw: dict[str, float] = {}
                inv_l = sum(1.0 / sig_of[n] for n in longs)
                inv_s = sum(1.0 / sig_of[n] for n in shorts)
                for n in longs:
                    raw[n] = (gross_target / 2.0) * (1.0 / sig_of[n]) / inv_l
                for n in shorts:
                    raw[n] = -(gross_target / 2.0) * (1.0 / sig_of[n]) / inv_s
                betas = dict(zip(cand["name"], cand["beta"].astype(float), strict=True))
                advs = dict(zip(cand["name"], cand["adv"].astype(float), strict=True))
                # liquidity cap: the participation cap of the expected auction volume (auction share of ADV)
                caps = {
                    n: participation_cap * AUCTION_SHARE_OF_ADV * advs[n] * closes[n] / capital
                    for n in raw
                }
                secs = {n: sectors_all[n] for n in raw}
                res = project(raw, betas, secs, spec=cspec, liquidity_caps=caps, solver=solver)
                if res.refused:
                    reason = "repair refused: " + ", ".join(res.active)
                else:
                    weights = polish_weights(
                        res, raw, betas, secs, spec=cspec, liquidity_caps=caps, solver=solver
                    )
                    intents: dict[str, int] = {}
                    for n in sorted(weights):
                        q = int(math.floor(abs(weights[n]) * capital / closes[n] + 0.5))
                        if q >= 1:
                            intents[n] = q if weights[n] > 0 else -q
                    final = {n: q * closes[n] / capital for n, q in intents.items()}
                    feas = feasibility(final, gross_target=gross_target, spec=cspec)
                    if not feas.feasible:
                        reason = feas.reason
                    else:
                        joint: dict[str, float] = {}
                        for c in led.cohorts.values():
                            for n, q in c.allocated.items():
                                if q and n in closes:
                                    joint[n] = joint.get(n, 0.0) + q * closes[n] / capital
                        for n, v in final.items():
                            joint[n] = joint.get(n, 0.0) + v
                        chk = check_book(
                            joint,
                            {**betas, **{n: 0.0 for n in joint if n not in betas}},
                            {n: sectors_all[n] for n in joint},
                            spec=book_spec,
                            tol=live * 2e-3,
                        )
                        if not chk.ok:
                            reason = "combined book outside a hard constraint after the solve"
                        else:
                            led.open_cohort(
                                key,
                                formation_ts=ts_close - 1,
                                decision_ts=ts_close,
                                expiry_ts=expiry,
                                intents=intents,
                                forecasts={n: forecasts[n] for n in intents},
                                decisions={n: decisions[n] for n in intents},
                            )
                            opened += 1
                            deployed.append(sum(abs(v) for v in final.values()) * capital)
                            cushions.extend(
                                abs(forecasts[n]) - hurdle * 2.0 * costs_bps[n] for n in intents
                            )
                            stage_books = {
                                k: dict(v.weights)
                                for k, v in construction_stages(
                                    raw, betas, secs, spec=cspec, liquidity_caps=caps, solver=solver
                                ).items()
                            }
                            stage_books["executable"] = final
                            last_cohort = {
                                "forecasts_bps": {n: forecasts[n] for n in raw},
                                "cost_bps": {n: 2.0 * costs_bps[n] for n in raw},
                                "betas": {n: betas[n] for n in raw},
                                "sectors": secs,
                            }
            if reason:
                led.refuse_cohort(key, decision_ts=ts_close, expiry_ts=expiry, reason=reason)
                refuse(reason, "construction: " + reason.split("(")[0].strip())
        traded += execute(
            ts_close, closes, dict(zip(f["name"], f["auction_volume"].astype(int), strict=True)), f
        )
        turnover.append(traded / capital)
        vals = {n: Valuation(closes[n]) for n, q in led.positions.items() if q != 0}
        for c in led.cohorts.values():
            for n, q in c.allocated.items():
                if q and n not in vals:
                    vals[n] = Valuation(closes[n])
        res_s = led.close_session(vals)
        waterfalls.append(dict(res_s.waterfall))
        kind = "traded" if traded > 0 else "usable_no_trades"
        rows.append(
            SessionRow(
                s,
                kind,
                res_s.net_pnl if kind == "traded" else 0.0,
                res_s.waterfall["midpoint_reference_pnl"] if kind == "traded" else 0.0,
            )
        )
        if kind != "traded" and abs(res_s.net_pnl) > 1e-6:
            # carried inventory with no auction fill today still marks: count the session as traded
            rows[-1] = SessionRow(
                s, "traded", res_s.net_pnl, res_s.waterfall["midpoint_reference_pnl"]
            )
    series = session_series(fam, horizon, rows, capital)
    check_no_inflation(series, all_session_ids=list(trade_sessions), denominators=[capital])
    return RunResult(
        fam,
        horizon,
        capital,
        series,
        rows,
        waterfalls,
        refusals,
        refused_alpha,
        refused_shares,
        cushions,
        deployed,
        turnover,
        stage_books,
        last_cohort,
        led.research_rows(),
        fills,
        opened,
        pd.concat(signal_rows, ignore_index=True) if signal_rows else pd.DataFrame(),
    )


# --------------------------------------------------------------------------- report
def _clean(obj: Any, nd: int = ROUND) -> Any:
    """Floats rounded to ``nd`` decimals, NaN and infinities to None, numpy scalars to Python."""
    if isinstance(obj, Mapping):
        return {str(k): _clean(v, nd) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_clean(v, nd) for v in obj]
    if isinstance(obj, np.ndarray):
        return [_clean(v, nd) for v in obj.tolist()]
    if isinstance(obj, bool | np.bool_):
        return bool(obj)
    if isinstance(obj, int | np.integer):
        return int(obj)
    if isinstance(obj, float | np.floating):
        f = float(obj)
        if not math.isfinite(f):
            return None
        r = round(f, nd)
        return 0.0 if r == 0 else r
    return obj


def build_report(
    spec: FamilySpec,
    run: RunResult,
    feats: pd.DataFrame,
    labels: Mapping[str, pd.Series],
    model: Any,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    """The E5-class report: blocks A-E, screen, decision tree, identities and selection history."""
    sig = spec.signal_feature
    srows = run.signal_rows
    if len(srows):
        key = srows[["session", "name"]]
        merged = key.merge(
            feats[["session", "name", sig]].assign(_i=np.arange(len(feats))), on=["session", "name"]
        )
        z = merged[sig].to_numpy(dtype=float)
        ok = np.isfinite(z)
        fwd = {h: lab.to_numpy(dtype=float)[merged["_i"].to_numpy()] for h, lab in labels.items()}
        ok &= np.all([np.isfinite(v) for v in fwd.values()], axis=0) if fwd else ok
        z = z[ok]
        fwd = {h: v[ok] for h, v in fwd.items()}
        mu = srows["mu"].to_numpy(dtype=float)[ok] if "mu" in srows else None
        block_a = block_a_signal(
            -z,  # the builder works on S = -z; the signal is already signed, so pass -signal
            fwd,
            mu_hat_bps={spec.primary_horizon: mu}
            if mu is not None and np.all(np.isfinite(mu))
            else None,
            coverage=float(ok.mean()) if ok.size else None,
        )
        ic_primary = next(
            (r["rank_ic"] for r in block_a.rows if r.get("horizon") == spec.primary_horizon),
            float("nan"),
        )
        n_obs = int(z.size)
    else:
        block_a = block_a_signal([], {})
        ic_primary, n_obs = float("nan"), 0
    series = run.series
    lc = run.last_cohort
    block_b = block_b_portfolio(
        run.stage_books,
        lc.get("forecasts_bps", {}),
        lc.get("cost_bps", {}),
        lc.get("betas", {}),
        lc.get("sectors", {}),
        series=series,
        deployed_gross=run.deployed_gross,
        turnover_per_rebalance=run.turnover,
        refusals=run.refusals,
    )
    block_c = block_c_execution(
        run.waterfalls,
        capital=run.capital,
        refusals={k: (run.refused_shares[k], run.refused_alpha_bps[k]) for k in run.refusals},
        cushions_bps=run.cushions_bps,
        columns_pnl={"conservative": series.net_total},
    )
    block_d = block_d_risk_adjusted(series, block_length=5, bootstrap=400)
    block_e = block_e_sensitivity(run.waterfalls, capital=run.capital, series=series)
    screen = economic_screen(series)
    band = 3.0 / math.sqrt(n_obs) if n_obs else math.inf
    predictability = bool(math.isfinite(ic_primary) and ic_primary > band)
    tree = decision_tree(
        e3_red=False,
        predictability=predictability,
        screen_passed=bool(screen["passed"]),
        powered=False,
        x0_eligible=None,
        attribution="blocks B and C of this report",
    )
    report = {
        "title": f"E5-class development report: {spec.doc['name']} ({spec.family_id}, {spec.primary_horizon})",
        "evidence_class": "development diagnostics on a synthetic fixture: no claim about markets"
        if identity.get("route") == "synthetic"
        else "development-stage (pending classification)",
        "identity": dict(identity),
        "model": {
            "class": model.model_class,
            "parameters": model.parameters(),
            "registry_id": model.registry_id,
        },
        "sessions": {
            "trading": series.n + len(series.dropped),
            "counts": series.counts,
            "cohorts_opened": run.cohorts_opened,
            "fills": run.fills,
        },
        "blocks": {
            "A": block_a.as_dict(),
            "B": _clean(block_b.as_dict(), ROUND_SOLVER),
            "C": block_c.as_dict(),
            "D": block_d.as_dict(),
            "E": block_e.as_dict(),
        },
        "economic_screen": screen,
        "predictability": {"rank_ic_primary": ic_primary, "chance_band": band, "n": n_obs},
        "decision_tree": tree,
        "r_d_bps": list(series.r_bps),
        "session_ids": list(series.session_ids),
        "selection_history": {
            "kind": LEDGER_KIND,
            "outcome_exposure": True,
            "what": "M0 fit on the estimation window and the E5 tables on the trading window",
        },
    }
    return _clean(report)


def report_hash(report: Mapping[str, Any]) -> str:
    return content_hash(_clean(report))


# --------------------------------------------------------------------------- the ledger
def record_run(
    ledger_path: str | Path,
    spec: FamilySpec,
    identity: Mapping[str, Any],
    report: Mapping[str, Any],
    sha: str,
) -> dict[str, Any]:
    """One analysis event with its outcome exposure in the selection ledger; returns the entry's identity."""
    led = ExperimentLedger(
        ledger_path,
        dataset_version=str(identity["dataset_sha256"]),
        data_family="synthetic" if identity.get("route") == "synthetic" else "real",
    )
    config = {
        "family_id": spec.family_id,
        "spec_sha256": spec.sha256,
        "partition": identity["partition"],
        "horizon": spec.primary_horizon,
        "model_class": report["model"]["class"],
        "registry_id": report["model"][
            "registry_id"
        ],  # a different fitted model is a different look
        "route": identity.get("route"),
        "software_version": SOFTWARE_VERSION,
    }
    result = {
        "outcome_exposure": True,
        "report_sha256": sha,
        "r_d_mean_bps": report["economic_screen"]["mean_bps"],
        "screen_passed": report["economic_screen"]["passed"],
        "decision_branch": report["decision_tree"]["branch"],
        "verdict": report["decision_tree"]["verdict"],
    }
    total = led.record(spec.family_id, LEDGER_KIND, config, result)
    led.save()
    return {
        "key": led.experiment_key(spec.family_id, LEDGER_KIND, config, led.dataset_version),
        "total_experiments": total,
        "chain_head": led.chain_head,
        "path": str(ledger_path),
    }


# --------------------------------------------------------------------------- the loop
def run_family(
    spec_path: str | Path,
    data_path: str | Path,
    manifest_path: str | Path,
    *,
    partition: str = "development",
    out_dir: str | Path | None = None,
    ledger_path: str | Path | None = None,
    identity: str | None = None,
    capital: float = CAPITAL,
    model: Forecaster | None = None,
    extra_features: Sequence[str] = (),
    registry_entry: RegistryEntry | None = None,
) -> dict[str, Any]:
    """The loop (module docstring).  Returns ``{"report", "sha256", "ledger", "paths"}``.

    ``model`` is a candidate :class:`Forecaster` (the ML framework's) run in place
    of M0 on the same sessions; ``extra_features`` are its feature group's
    contracted features, built alongside the family's signal.
    """
    spec = load_family_spec(spec_path)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    frame, data_sha = load_panel(data_path)
    who = check_partition(partition, manifest, data_path, identity=identity, data_sha256=data_sha)
    if not spec.fit_allowed:
        raise PartitionRefused(
            f"{spec.family_id}: the specification forbids a fit (status {spec.doc['status']!r}, "
            f"gate {spec.doc['data_gate'].get('outcome')!r}); no development run"
        )
    route = manifest.get("route")
    if route not in spec.doc["routes"]:
        raise PartitionRefused(
            f"{spec.family_id}: route {route!r} is not one of the specification's routes {spec.doc['routes']}"
        )
    est, trade = windows(frame, manifest, partition, spec)
    feats = build_features(frame, [*spec.doc["features"], *extra_features])
    labels = {
        h: forward_label(
            frame,
            int(h[:-1]) if h != "c2o" else 1,
            exit_at_open=(h == "c2o"),
        )
        for h in spec.doc["horizons"]
        if h == "c2o" or h.endswith("d")
    }
    primary = labels[spec.primary_horizon]
    # purge: a label at session d is usable for a fit only if d + h lies inside the estimation window
    cal = sorted(frame["session"].unique().tolist())
    est_end = cal.index(est[-1])
    purge_ok = feats["session"].map(lambda s: cal.index(s) + spec.horizon_sessions <= est_end)
    primary_purged = primary.where(purge_ok.to_numpy())
    dataset_identity = f"{manifest.get('dataset_id', 'panel')}:{data_sha[:16]}"
    registry = ForecastRegistry()
    if model is None:
        m0 = fit_m0(feats, primary_purged, est, spec.signal_feature)
        m0.registry_id = registry.register(
            _registry_entry(
                spec,
                m0,
                est,
                dataset_identity,
                {
                    "kind": "outcome exposure",
                    "what": "M0 pooled linear fit on the estimation window",
                },
            )
        )
        model = m0
    else:
        entry = registry_entry or _registry_entry(
            spec,
            model,
            est,
            dataset_identity,
            {"kind": "candidate configuration", "what": model.model_class},
        )
        model.registry_id = registry.register(
            entry
        )  # the records name the entry that produced them
    overrides = dict(manifest.get("eligibility_overrides") or {}) if route == "synthetic" else {}
    run = run_engine(
        frame, feats, spec, model, registry, trade, capital=capital, eligibility_overrides=overrides
    )
    ident = {
        **who,
        "eligibility_overrides": overrides,
        "dataset_id": manifest.get("dataset_id"),
        "dataset_sha256": data_sha,
        "route": route,
        "spec_path": Path(spec_path).name,
        "spec_sha256": spec.sha256,
        "estimation_window": [est[0], est[-1]],
        "trading_window": [trade[0], trade[-1]],
        "capital": capital,
        "cost_column": "conservative",
        "software_version": SOFTWARE_VERSION,
        "registry_digest": registry.digest,
    }
    ident.pop("identity", None)  # the OS user name is not part of the report's identity
    report = build_report(spec, run, feats, labels, model, ident)
    sha = report_hash(report)
    paths: dict[str, str] = {}
    if out_dir is not None:
        od = Path(out_dir)
        od.mkdir(parents=True, exist_ok=True)
        rp = od / f"{spec.family_id}_{partition}_report.json"
        rp.write_text(canonical_json(report) + "\n", encoding="ascii")
        (od / f"{spec.family_id}_{partition}_research_ledger.json").write_text(
            canonical_json(_clean(run.research_ledger)) + "\n", encoding="ascii"
        )
        registry.save(od / f"{spec.family_id}_{partition}_registry.json")
        paths = {"report": str(rp)}
    ledger = record_run(ledger_path, spec, ident, report, sha) if ledger_path else None
    return {
        "report": report,
        "sha256": sha,
        "ledger": ledger,
        "paths": paths,
        "run": run,
        "model": model,
    }
