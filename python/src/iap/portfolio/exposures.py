"""Exposure-matrix builder for neutral books (v1.17 C1; plan row 17).

The optimizer's ``currency_matrix`` / ``currency_bounds`` pair is a general
linear-exposure constraint ``|E w| <= b`` (one halfspace pair per row, exact
projection, in both the cyclic and the Dykstra path).  This module builds
``E`` for a cross-sectional book:

- a **beta** row: ``E[r, i] = beta_i`` — the book's beta to the reference
  (the features' ``beta_w5m_v1`` where present; a missing beta is read as
  1.0 and the instrument is listed in ``missing_beta`` so the report can
  say so);
- one **sector** row per sector with at least ``min_members`` members:
  ``E[r, i] = 1`` for the members — net exposure inside the sector.  A
  sector with fewer members (a singleton would pin that name at zero) is
  merged into the ``sector_unknown`` bucket, as is every instrument the
  taxonomy does not name.  The bucket gets a row like any other sector.

Dollar neutrality is the optimizer's own ``net_cap`` and is NOT a row here
(it would duplicate the halfspace).  Bounds default to 0 (exact
neutrality); ``bound`` loosens every row by the same amount.

``load_sectors`` reads ``configs/reference/sectors.json`` (``{"sectors":
{"SYMBOL": "Sector", ...}}``); the file records its ``source`` and the
taxonomy's ``as_of`` date so a report can cite them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SECTOR_UNKNOWN = "sector_unknown"
DEFAULT_SECTORS_PATH = (
    Path(__file__).resolve().parents[4] / "configs" / "reference" / "sectors.json"
)


@dataclass
class ExposureMatrix:
    """Rows of ``E`` with their labels, bounds and provenance notes."""

    matrix: np.ndarray  # (R, N)
    bounds: np.ndarray  # (R,)
    labels: list[str]
    missing_beta: list[int] = field(default_factory=list)
    sector_of: dict[int, str] = field(default_factory=dict)

    @property
    def n_rows(self) -> int:
        return int(self.matrix.shape[0])

    def exposures(self, w: np.ndarray) -> dict[str, float]:
        """``label -> E_r . w`` for a weight vector."""
        e = self.matrix @ np.asarray(w, dtype=np.float64)
        return {lab: float(v) for lab, v in zip(self.labels, e, strict=True)}


def load_sectors(path: str | Path | None = None) -> dict:
    """The sector taxonomy document (``sectors``, ``source``, ``as_of``)."""
    p = Path(path) if path is not None else DEFAULT_SECTORS_PATH
    doc = json.loads(p.read_text(encoding="utf-8"))
    if "sectors" not in doc or not isinstance(doc["sectors"], dict):
        raise ValueError(f"{p}: expected a 'sectors' mapping")
    return doc


def build_exposure_matrix(
    instrument_ids: Sequence[int],
    betas: Mapping[int, float] | None = None,
    sectors: Mapping[int, str] | None = None,
    beta_row: bool = True,
    sector_rows: bool = True,
    bound: float = 0.0,
    min_members: int = 2,
) -> ExposureMatrix:
    """Build ``E`` for ``instrument_ids`` (column order = the given order).

    ``betas``: instrument -> beta (NaN / missing read as 1.0, listed in
    ``missing_beta``).  ``sectors``: instrument -> sector name (missing ->
    :data:`SECTOR_UNKNOWN`).  With neither row kind requested the matrix has
    zero rows (``matrix.shape == (0, N)``), which the optimizer accepts as
    "no currency constraint" only when the caller passes ``None`` instead;
    use :meth:`ExposureMatrix.n_rows` to decide.
    """
    ids = [int(i) for i in instrument_ids]
    n = len(ids)
    if len(set(ids)) != n:
        raise ValueError("instrument_ids must be distinct")
    if not (np.isfinite(bound) and bound >= 0.0):
        raise ValueError("bound must be finite and >= 0")
    if min_members < 1:
        raise ValueError("min_members must be >= 1")
    rows: list[np.ndarray] = []
    labels: list[str] = []
    missing: list[int] = []
    if beta_row:
        b = np.ones(n, dtype=np.float64)
        for j, iid in enumerate(ids):
            v = None if betas is None else betas.get(iid)
            if v is None or not np.isfinite(float(v)):
                missing.append(iid)
            else:
                b[j] = float(v)
        rows.append(b)
        labels.append("beta")
    sector_of: dict[int, str] = {}
    if sector_rows:
        raw = {iid: (sectors.get(iid) if sectors is not None else None) for iid in ids}
        counts: dict[str, int] = {}
        for s in raw.values():
            if s is not None:
                counts[s] = counts.get(s, 0) + 1
        for iid in ids:
            s = raw[iid]
            sector_of[iid] = s if (s is not None and counts[s] >= min_members) else SECTOR_UNKNOWN
        for s in sorted(set(sector_of.values())):
            r = np.array([1.0 if sector_of[iid] == s else 0.0 for iid in ids], dtype=np.float64)
            rows.append(r)
            labels.append(f"sector:{s}")
    matrix = np.vstack(rows) if rows else np.zeros((0, n), dtype=np.float64)
    bounds = np.full(len(rows), float(bound), dtype=np.float64)
    return ExposureMatrix(matrix, bounds, labels, missing, sector_of)
