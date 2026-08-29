"""Currency-exposure translation for FX pair portfolios (spec §15).

A weight ``w`` in pair ``BASE/QUOTE`` is long the base currency and short the
quote currency in equal measure.  The exposure matrix ``E`` (currencies x
pairs) therefore has ``+1`` in the base row and ``-1`` in the quote row of
each pair column; net currency exposure is ``E @ w``.

Currency ordering is pinned: sorted alphabetically.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

import numpy as np


def currency_exposure_matrix(
    pair_symbols: Sequence[str],
) -> Tuple[List[str], np.ndarray]:
    """Build (currencies, E) for pair symbols like ``"EUR/USD"``.

    Returns the sorted currency list and the (n_currencies, n_pairs) matrix
    with +1 for the base and -1 for the quote of each pair.
    """
    parsed = []
    for sym in pair_symbols:
        parts = sym.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"not a currency pair symbol: {sym!r}")
        if parts[0] == parts[1]:
            raise ValueError(f"degenerate pair: {sym!r}")
        parsed.append((parts[0], parts[1]))
    currencies = sorted({c for pair in parsed for c in pair})
    row = {c: i for i, c in enumerate(currencies)}
    E = np.zeros((len(currencies), len(parsed)), dtype=np.float64)
    for j, (base, quote) in enumerate(parsed):
        E[row[base], j] = 1.0
        E[row[quote], j] = -1.0
    return currencies, E
