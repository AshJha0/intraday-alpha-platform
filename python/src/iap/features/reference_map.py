"""Cross-asset reference ETF by symbol — feature identity ``xasset_v2`` (v1.18 REL-07).

The v1 rule (:func:`iap.features.context.build_contexts`) gives every EQUITY
row the FIRST ETF row of ``instruments.json`` as its cross-asset reference.
On the 100-name 2019-01-30 store that is AMLP (instrument_id 7), so every
``xasset`` feature there (``ref_ret_*``, ``beta_w5m_v1``, ``corr_*``,
``leadlag_*``, ``resid_vol_w1m_v1``) is measured against an MLP fund.

This module is the opt-in alternative: a committed, versioned map
(``configs/reference/reference_etf_map_v1.json``) names each EQUITY symbol's
reference ETF (QQQ for Nasdaq-100 names as of the map's ``as_of`` date, SPY
otherwise) and its sector.  :func:`apply_reference_map` rewrites only the
``ref_instrument_id`` of EQUITY contexts; ETF rows keep referencing
themselves and FX rows are untouched, exactly as in v1.

Identity: the feature NAMES and formulas are those of v1 (the registry hash
does not move); what changes is the reference series they are computed
against.  A store built this way records
``{"feature_identity": "xasset_v2", "map_version", "map_sha256", "as_of"}``
under ``xasset_reference`` in ``features_summary.json`` (key absent on the
default path), and a consumer must read that key before mixing stores.
``context.py`` itself is not edited, so nothing fingerprinted moves.

Fail-closed: an EQUITY symbol absent from the map, or a reference ETF absent
from the dataset (or not an ETF row there), is a start-up error, never a
silent fallback to the v1 rule.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

from iap.features.context import InstrumentContext, build_contexts

__all__ = [
    "DEFAULT_MAP",
    "FEATURE_IDENTITY",
    "XASSET_MODES",
    "ReferenceMap",
    "apply_reference_map",
    "build_contexts_v2",
    "load_reference_map",
]

_REPO = Path(__file__).resolve().parents[4]
#: The committed map (versioned; a new membership date is a new file).
DEFAULT_MAP = _REPO / "configs" / "reference" / "reference_etf_map_v1.json"
#: Identity recorded by every store built with the map.
FEATURE_IDENTITY = "xasset_v2"
#: ``--xasset-reference`` choices: the v1 rule (default) or the map.
XASSET_MODES = ("first_etf", "symbol_map")


@dataclass(frozen=True)
class ReferenceMap:
    """A loaded reference map (module docs)."""

    map_version: str
    as_of: str
    sha256: str  # of the file bytes as committed
    references: dict[str, str]  # EQUITY symbol -> reference ETF symbol
    sectors: dict[str, str]

    def identity(self) -> dict[str, str]:
        """The ``xasset_reference`` block written into a store summary."""
        return {
            "feature_identity": FEATURE_IDENTITY,
            "map_version": self.map_version,
            "map_sha256": self.sha256,
            "as_of": self.as_of,
        }


def load_reference_map(path: str | Path = DEFAULT_MAP) -> ReferenceMap:
    """Read and validate a reference map file."""
    raw = Path(path).read_bytes()
    doc = json.loads(raw)
    for key in ("map_version", "as_of", "source", "limitations", "reference_etfs", "symbols"):
        if key not in doc:
            raise ValueError(f"{path}: reference map has no {key!r}")
    if doc.get("feature_identity", FEATURE_IDENTITY) != FEATURE_IDENTITY:
        raise ValueError(f"{path}: feature_identity must be {FEATURE_IDENTITY!r}")
    allowed = set(doc["reference_etfs"])
    refs: dict[str, str] = {}
    sectors: dict[str, str] = {}
    for sym, entry in doc["symbols"].items():
        ref = entry.get("reference_etf")
        if ref not in allowed:
            raise ValueError(f"{path}: {sym}: reference_etf {ref!r} not in {sorted(allowed)}")
        refs[sym] = ref
        sectors[sym] = str(entry.get("sector", "unknown"))
    return ReferenceMap(
        map_version=str(doc["map_version"]),
        as_of=str(doc["as_of"]),
        sha256=hashlib.sha256(raw).hexdigest(),
        references=refs,
        sectors=sectors,
    )


def apply_reference_map(
    contexts: dict[int, InstrumentContext], rmap: ReferenceMap
) -> dict[int, InstrumentContext]:
    """A copy of ``contexts`` with each EQUITY row's reference taken from ``rmap``."""
    by_symbol = {c.symbol: c for c in contexts.values()}
    out: dict[int, InstrumentContext] = {}
    for iid, ctx in contexts.items():
        if ctx.asset_class != "EQUITY":
            out[iid] = ctx
            continue
        ref_sym = rmap.references.get(ctx.symbol)
        if ref_sym is None:
            raise ValueError(
                f"{ctx.symbol} (instrument {iid}) is not in reference map {rmap.map_version}"
            )
        ref = by_symbol.get(ref_sym)
        if ref is None or ref.asset_class != "ETF":
            raise ValueError(
                f"{ctx.symbol}: reference ETF {ref_sym} is not an ETF row of this dataset"
            )
        out[iid] = replace(ctx, ref_instrument_id=ref.instrument_id)
    return out


def build_contexts_v2(
    config_dir: str | Path, map_path: str | Path = DEFAULT_MAP
) -> dict[int, InstrumentContext]:
    """:func:`build_contexts` with the ``xasset_v2`` references applied."""
    return apply_reference_map(build_contexts(config_dir), load_reference_map(map_path))
