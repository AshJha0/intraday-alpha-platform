"""``configs/mvp/mvp.json`` — the MVP run configuration, validated fail-fast.

Every key is checked for presence, type and domain when the document is
loaded (PLATFORM_CONVENTIONS.md §12.2): a failure is a ``ValueError`` that
names the file and the key, raised before any event is generated or read.
The loaded :class:`MvpConfig` is frozen; ``--seed`` / ``--instrument``
overrides produce a new document (:meth:`MvpConfig.with_overrides`) so the
hash of the document in force (``config_version``) always reflects what
ran.

The documents the run depends on (venues, instruments, generator, risk,
execution controls, alpha parameters, alpha registry) are named under
``reference`` as repository-relative paths and are loaded through
:meth:`MvpConfig.reference_documents`; ``config_version`` is the
``content_hash`` of all of them plus the MVP document itself.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from iap.contracts.types import Algo
from iap.contracts.versions import content_hash

__all__ = [
    "MVP_CONFIG_VERSION",
    "DEFAULT_CONFIG_PATH",
    "REPO_ROOT",
    "MvpConfig",
    "SessionSpec",
    "PortfolioSpec",
    "SolverSpec",
    "ExecutionSpec",
    "ControlsSpec",
    "UrgencyBand",
    "SorSpec",
    "load_config",
    "config_version_of",
]

#: ``x-version`` of ``configs/mvp/mvp.json``.
MVP_CONFIG_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "mvp" / "mvp.json"

_REFERENCE_KEYS = ("instruments", "venues", "generator", "risk", "execution",
                   "alpha_params", "alpha_registry")
_ALGOS = {a.value for a in Algo}
_NS_PER_S = 1_000_000_000


class _Doc:
    """Typed accessors over one JSON object; every error names file + key."""

    def __init__(self, obj: Mapping[str, Any], where: str) -> None:
        if not isinstance(obj, Mapping):
            raise ValueError(f"{where}: must be a JSON object")
        self.obj = obj
        self.where = where

    def _get(self, key: str) -> Any:
        if key not in self.obj:
            raise ValueError(f"{self.where}: missing key {key!r}")
        return self.obj[key]

    def sub(self, key: str) -> "_Doc":
        return _Doc(self._get(key), f"{self.where}.{key}")

    def integer(self, key: str, lo: int, hi: int) -> int:
        v = self._get(key)
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"{self.where}.{key}: must be an integer")
        if not lo <= v <= hi:
            raise ValueError(f"{self.where}.{key}: must be in [{lo}, {hi}], got {v}")
        return v

    def number(self, key: str, lo: float, hi: float, *, lo_open: bool = False) -> float:
        v = self._get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{self.where}.{key}: must be a finite number")
        if (v <= lo if lo_open else v < lo) or v > hi:
            bound = f"({lo}" if lo_open else f"[{lo}"
            raise ValueError(f"{self.where}.{key}: must be in {bound}, {hi}], got {v}")
        return float(v)

    def string(self, key: str) -> str:
        v = self._get(key)
        if not isinstance(v, str) or not v:
            raise ValueError(f"{self.where}.{key}: must be a non-empty string")
        return v

    def boolean(self, key: str) -> bool:
        v = self._get(key)
        if not isinstance(v, bool):
            raise ValueError(f"{self.where}.{key}: must be a boolean")
        return v

    def string_list(self, key: str, n: Optional[int] = None) -> Tuple[str, ...]:
        v = self._get(key)
        if not isinstance(v, list) or not v or not all(isinstance(s, str) and s for s in v):
            raise ValueError(f"{self.where}.{key}: must be a non-empty list of strings")
        if n is not None and len(v) != n:
            raise ValueError(f"{self.where}.{key}: expected {n} entries, got {len(v)}")
        if len(set(v)) != len(v):
            raise ValueError(f"{self.where}.{key}: entries must be unique")
        return tuple(v)

    def list_of_objects(self, key: str) -> Tuple["_Doc", ...]:
        v = self._get(key)
        if not isinstance(v, list) or not v:
            raise ValueError(f"{self.where}.{key}: must be a non-empty list")
        return tuple(_Doc(item, f"{self.where}.{key}[{i}]") for i, item in enumerate(v))


def _hms(text: str, where: str) -> Tuple[int, int, int]:
    parts = text.split(":")
    if len(parts) != 3 or not all(p.isdigit() and len(p) == 2 for p in parts):
        raise ValueError(f"{where}: time must be HH:MM:SS, got {text!r}")
    h, m, s = (int(p) for p in parts)
    if not (h <= 23 and m <= 59 and s <= 59):
        raise ValueError(f"{where}: time out of range: {text!r}")
    return h, m, s


@dataclass(frozen=True)
class SessionSpec:
    """The single MVP trading session (event time; UTC bounds via zoneinfo)."""

    trading_day: str
    timezone: str
    open: str
    close: str

    @property
    def length_ns(self) -> int:
        """Session length in nanoseconds (close - open, same day)."""
        h0, m0, s0 = _hms(self.open, "session.open")
        h1, m1, s1 = _hms(self.close, "session.close")
        return ((h1 - h0) * 3600 + (m1 - m0) * 60 + (s1 - s0)) * _NS_PER_S


@dataclass(frozen=True)
class SolverSpec:
    """PGD solver parameters (API_PORTFOLIO_TCA.md §1; Java ``SolverParams``)."""

    iters: int
    proj_passes: int
    step_decay: float
    feas_tol: float


@dataclass(frozen=True)
class PortfolioSpec:
    """Single-stock mean-variance sizing."""

    risk_aversion: float
    tc_bps: float
    max_position_qty: int
    max_notional: float
    turnover_cap: float
    vol_target_per_bar: float
    ewma_lambda: float
    min_bars: int
    conf_min: float
    solver: SolverSpec


@dataclass(frozen=True)
class UrgencyBand:
    """``urgency <= max_urgency`` selects ``algo`` (bands ascending, last = 1.0)."""

    max_urgency: float
    algo: Algo


@dataclass(frozen=True)
class ControlsSpec:
    """Declared execution controls (``configs/execution/execution.json`` defaults)."""

    max_child_qty: int
    max_participation: float
    min_slice_interval_ns: int
    latency_budget_ns: int


@dataclass(frozen=True)
class ExecutionSpec:
    """Algo selection and child scheduling parameters."""

    parent_window_ns: int
    urgency_bands: Tuple[UrgencyBand, ...]
    twap_slices: int
    is_slices: int
    is_risk_aversion: float
    pov_participation: float
    latency_decision_ns: int
    latency_risk_ns: int
    latency_wire_ns: int

    def algo_for(self, urgency: float) -> Algo:
        """The algo of the first band whose ``max_urgency`` covers ``urgency``."""
        for band in self.urgency_bands:
            if urgency <= band.max_urgency:
                return band.algo
        return self.urgency_bands[-1].algo


@dataclass(frozen=True)
class SorSpec:
    """SOR options (mirror of ``execution.json`` ``sor``)."""

    prefer_rebate: bool
    max_venue_latency_ns: int


@dataclass(frozen=True)
class MvpConfig:
    """The validated MVP configuration (see module docstring)."""

    document: Dict[str, Any]
    seed: int
    instrument: str
    strategy_id: str
    venues: Tuple[str, ...]
    alphas: Tuple[str, ...]
    horizon_ns: int
    decision_cadence_ns: int
    session: SessionSpec
    portfolio: PortfolioSpec
    execution: ExecutionSpec
    sor: SorSpec
    reference: Dict[str, str]
    notes: Tuple[str, ...]
    repo_root: Path

    # ------------------------------------------------------------ loading

    @classmethod
    def from_document(cls, doc: Mapping[str, Any], *, where: str = "mvp.json",
                      repo_root: Optional[Path] = None) -> "MvpConfig":
        """Validate a parsed ``mvp.json`` document."""
        root = Path(repo_root) if repo_root is not None else REPO_ROOT
        d = _Doc(doc, where)
        version = d.integer("x-version", MVP_CONFIG_VERSION, MVP_CONFIG_VERSION)
        seed = d.integer("seed", 0, (1 << 64) - 1)
        instrument = d.string("instrument")
        strategy_id = d.string("strategy_id")
        venues = d.string_list("venues")
        alphas = d.string_list("alphas")
        horizon_ns = d.integer("horizon_ns", 1, (1 << 62))
        cadence = d.integer("decision_cadence_ns", 1, (1 << 62))

        s = d.sub("session")
        session = SessionSpec(s.string("trading_day"), s.string("timezone"),
                              s.string("open"), s.string("close"))
        _hms(session.open, f"{where}.session.open")
        _hms(session.close, f"{where}.session.close")
        if session.length_ns <= 0:
            raise ValueError(f"{where}.session: open must be before close")

        p = d.sub("portfolio")
        sv = p.sub("solver")
        solver = SolverSpec(
            iters=sv.integer("iters", 1, 1_000_000),
            proj_passes=sv.integer("proj_passes", 1, 1_000),
            step_decay=sv.number("step_decay", 0.0, 1e6),
            feas_tol=sv.number("feas_tol", 0.0, 1.0, lo_open=True),
        )
        portfolio = PortfolioSpec(
            risk_aversion=p.number("risk_aversion", 0.0, 1e12, lo_open=True),
            tc_bps=p.number("tc_bps", 0.0, 1e6),
            max_position_qty=p.integer("max_position_qty", 1, (1 << 62)),
            max_notional=p.number("max_notional", 0.0, 1e15, lo_open=True),
            turnover_cap=p.number("turnover_cap", 0.0, 2.0, lo_open=True),
            vol_target_per_bar=p.number("vol_target_per_bar", 0.0, 1.0, lo_open=True),
            ewma_lambda=p.number("ewma_lambda", 0.0, 1.0, lo_open=True),
            min_bars=p.integer("min_bars", 2, 100_000),
            conf_min=p.number("conf_min", 0.0, 1.0),
            solver=solver,
        )
        if portfolio.ewma_lambda >= 1.0:
            raise ValueError(f"{where}.portfolio.ewma_lambda: must be < 1")

        e = d.sub("execution")
        bands = []
        prev = -1.0
        for b in e.list_of_objects("urgency_bands"):
            algo_name = b.string("algo")
            if algo_name not in _ALGOS:
                raise ValueError(f"{b.where}.algo: unknown algo {algo_name!r}")
            mx = b.number("max_urgency", 0.0, 1.0)
            if mx <= prev:
                raise ValueError(f"{b.where}.max_urgency: bands must be strictly ascending")
            prev = mx
            bands.append(UrgencyBand(mx, Algo(algo_name)))
        if bands[-1].max_urgency != 1.0:
            raise ValueError(f"{where}.execution.urgency_bands: the last band must end at 1.0")
        if any(b.algo is Algo.VWAP for b in bands):
            raise ValueError(f"{where}.execution.urgency_bands: VWAP needs a session volume "
                             "curve the MVP does not carry; use TWAP / POV / IS")
        lat = e.sub("latency")
        execution = ExecutionSpec(
            parent_window_ns=e.integer("parent_window_ns", 1, (1 << 62)),
            urgency_bands=tuple(bands),
            twap_slices=e.integer("twap_slices", 1, 10_000),
            is_slices=e.integer("is_slices", 1, 10_000),
            is_risk_aversion=e.number("is_risk_aversion", 0.0, 1e6),
            pov_participation=e.number("pov_participation", 0.0, 1.0, lo_open=True),
            latency_decision_ns=lat.integer("decision_ns", 0, (1 << 62)),
            latency_risk_ns=lat.integer("risk_ns", 0, (1 << 62)),
            latency_wire_ns=lat.integer("wire_ns", 0, (1 << 62)),
        )
        if execution.parent_window_ns > cadence:
            raise ValueError(f"{where}.execution.parent_window_ns: must not exceed "
                             "decision_cadence_ns (one live parent per decision cycle)")

        so = d.sub("sor")
        sor = SorSpec(so.boolean("prefer_rebate"),
                      so.integer("max_venue_latency_ns", 0, (1 << 62)))

        r = d.sub("reference")
        reference = {k: r.string(k) for k in _REFERENCE_KEYS}
        for key, rel in reference.items():
            if not (root / rel).is_file():
                raise ValueError(f"{where}.reference.{key}: {rel} not found under {root}")

        notes_raw = d.obj.get("notes", [])
        if not isinstance(notes_raw, list) or not all(isinstance(n, str) for n in notes_raw):
            raise ValueError(f"{where}.notes: must be a list of strings")
        if version != MVP_CONFIG_VERSION:  # pragma: no cover - guarded by integer()
            raise ValueError(f"{where}.x-version: expected {MVP_CONFIG_VERSION}")
        return cls(
            document=json.loads(json.dumps(doc)),
            seed=seed, instrument=instrument, strategy_id=strategy_id,
            venues=venues, alphas=alphas, horizon_ns=horizon_ns,
            decision_cadence_ns=cadence, session=session, portfolio=portfolio,
            execution=execution, sor=sor, reference=reference,
            notes=tuple(notes_raw), repo_root=root,
        )

    @classmethod
    def load(cls, path: Union[str, Path], *, repo_root: Optional[Path] = None) -> "MvpConfig":
        """Load + validate ``path``; errors name the file."""
        p = Path(path)
        if not p.is_file():
            raise ValueError(f"mvp config not found: {p}")
        with open(p, encoding="utf-8") as fh:
            try:
                doc = json.load(fh)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{p}: invalid JSON ({exc})") from None
        return cls.from_document(doc, where=str(p), repo_root=repo_root)

    def with_overrides(self, *, seed: Optional[int] = None,
                       instrument: Optional[str] = None) -> "MvpConfig":
        """A new config with ``seed`` / ``instrument`` replaced (re-validated)."""
        doc = json.loads(json.dumps(self.document))
        if seed is not None:
            doc["seed"] = seed
        if instrument is not None:
            doc["instrument"] = instrument
        return MvpConfig.from_document(doc, where="mvp.json (overridden)",
                                       repo_root=self.repo_root)

    # ---------------------------------------------------------- documents

    def reference_path(self, key: str) -> Path:
        """Absolute path of a ``reference`` document."""
        return self.repo_root / self.reference[key]

    def reference_documents(self) -> Dict[str, Any]:
        """Every referenced JSON document, keyed by its repository-relative path."""
        out: Dict[str, Any] = {}
        for key in _REFERENCE_KEYS:
            rel = self.reference[key]
            with open(self.repo_root / rel, encoding="utf-8") as fh:
                out[rel] = json.load(fh)
        return out

    def config_version(self) -> str:
        """``content_hash`` of every configuration document in force (the MVP
        document with its overrides applied plus all reference documents)."""
        return config_version_of(self)

    @property
    def run_id(self) -> str:
        """First 16 hex of ``content_hash(document + seed)``."""
        return content_hash({"config": self.document, "seed": self.seed})[:16]

    @property
    def session_id(self) -> str:
        """The trace session id (``mvp-<run_id>``)."""
        return f"mvp-{self.run_id}"


def config_version_of(cfg: MvpConfig) -> str:
    """See :meth:`MvpConfig.config_version`."""
    docs = cfg.reference_documents()
    docs["configs/mvp/mvp.json"] = cfg.document
    return content_hash(docs)


def load_config(path: Optional[Union[str, Path]] = None, *, seed: Optional[int] = None,
                instrument: Optional[str] = None,
                repo_root: Optional[Path] = None) -> MvpConfig:
    """Load ``configs/mvp/mvp.json`` (or ``path``) and apply CLI overrides.
    ``repo_root`` is the directory the ``reference`` paths resolve against
    (and where the default config lives); the repository by default."""
    if path is None:
        path = (DEFAULT_CONFIG_PATH if repo_root is None
                else Path(repo_root) / "configs" / "mvp" / "mvp.json")
    cfg = MvpConfig.load(path, repo_root=repo_root)
    if seed is not None or instrument is not None:
        cfg = cfg.with_overrides(seed=seed, instrument=instrument)
    return cfg
