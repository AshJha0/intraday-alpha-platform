"""Many-ETF alpha universe for ``xasset_v2`` stores (v1.18 REL-07, opt-in).

:func:`iap.alpha.base.configure_universe` refuses more than one ETF row:
under the v1 reference rule the ETF-relative alphas need "the" ETF.  A store
built with ``--xasset-reference symbol_map`` (feature identity ``xasset_v2``,
:mod:`iap.features.reference_map`) names each constituent's reference ETF by
symbol, so a universe with many ETFs (the 100-name day has 29) is
well-defined there.  :func:`configure_universe_many_etfs` derives the same
four groups as ``configure_universe`` and accepts any number of ETF rows.

A separate module on purpose: ``iap.alpha.base`` is in the code fingerprint
of every alpha (the pre-registration fingerprint), so editing it would move every
alpha's ``code_hash``; this module is imported by no alpha.
"""

from __future__ import annotations

import json
from pathlib import Path

from iap.alpha.base import _UNIVERSE

__all__ = ["configure_universe_many_etfs"]


def configure_universe_many_etfs(instruments_json: str | Path) -> None:
    """``configure_universe`` for a many-ETF ``xasset_v2`` universe (module docs).

    Constituents are the EQUITY rows, ``etf`` every ETF row, FX the FX rows;
    reset with ``configure_universe(None)``.
    """
    rows = json.loads(Path(instruments_json).read_text())["instruments"]
    by_class: dict[str, list[int]] = {}
    for row in rows:
        by_class.setdefault(row["asset_class"], []).append(int(row["instrument_id"]))
    etf = tuple(sorted(by_class.get("ETF", ())))
    constituents = tuple(sorted(by_class.get("EQUITY", ())))
    _UNIVERSE.update(
        equity=tuple(sorted(constituents + etf)),
        constituents=constituents,
        etf=etf,
        fx=tuple(sorted(by_class.get("FX", ()))),
    )
