"""Currency-exposure machinery + FX05/FX06 (spec §12).

Spec §12: "FX research must model currency exposures rather than treating
each currency pair as an independent asset."  This module owns:

- the pinned **currency exposure matrix** A (pairs x currencies): a long
  position in BASE/QUOTE is +1 unit of BASE and -1 unit of QUOTE exposure;
- translation of pair positions into per-currency exposures (A^T p);
- estimation of per-currency **factor returns** from the panel of pair
  returns: solve min_f ||A f - r||^2 with the USD factor pinned to 0
  (numeraire — the exposure matrix is otherwise rank-deficient because each
  row sums to 0).  With fewer valid pairs than free currencies the
  least-squares solution uses the pseudo-inverse (minimum-norm, still
  deterministic).

FX05 trades the residual r - A f (cross-pair relative value); FX06 trades
the smoothed factor returns mapped back through A (currency-factor
momentum).
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Sequence, Tuple

import numpy as np
import pandas as pd

from iap.alpha.cross_sectional import CrossSectionalLinearAlpha

#: pinned pair -> (base, quote) map (configs/instruments.json)
PAIR_CURRENCIES: Dict[int, Tuple[str, str]] = {
    101: ("EUR", "USD"),
    102: ("GBP", "USD"),
    103: ("USD", "JPY"),
    104: ("AUD", "USD"),
    105: ("USD", "CAD"),
    106: ("USD", "CHF"),
    107: ("NZD", "USD"),
    108: ("EUR", "GBP"),
}

#: sorted currency list; NUMERAIRE is dropped from the factor solve
CURRENCIES: Tuple[str, ...] = ("AUD", "CAD", "CHF", "EUR", "GBP", "JPY", "NZD", "USD")
NUMERAIRE = "USD"
FREE_CURRENCIES: Tuple[str, ...] = tuple(c for c in CURRENCIES if c != NUMERAIRE)


def identified_pairs(pair_ids: Sequence[int], observable: Sequence[bool]) -> np.ndarray:
    """Which observable pairs carry IDENTIFIABLE relative-value information.

    Pinned (API_ALPHA §5, round-3).  The factor solve pins USD and fits one
    free factor per remaining currency.  A currency that appears in exactly
    ONE observable pair has its factor absorb that pair's whole return, so
    the pair's residual is 0 **by construction** — a constant, not a signal.
    On this universe AUD, CAD, CHF, JPY and NZD each appear in a single
    pair, so five of the eight pairs had a constant FX05 signal (raw = -0,
    z = -mu/sigma, a small non-zero expected return on every row) and their
    "IC" was pooled into the headline number.

    A pair is identified iff every FREE currency it touches appears in at
    least two observable pairs.  Everything else scores NaN (confidence 0).
    """
    a = exposure_matrix(pair_ids)
    free = [j for j, c in enumerate(CURRENCIES) if c != NUMERAIRE]
    obs = np.asarray(observable, dtype=bool)
    counts = {j: int(np.sum(obs & (a[:, j] != 0.0))) for j in free}
    out = np.zeros(len(pair_ids), dtype=bool)
    for i in range(len(pair_ids)):
        if not obs[i]:
            continue
        out[i] = all(counts[j] >= 2 for j in free if a[i, j] != 0.0)
    return out


def exposure_matrix(pair_ids: Sequence[int]) -> np.ndarray:
    """A[i, j] = exposure of pair i to currency j (all currencies, sorted)."""
    a = np.zeros((len(pair_ids), len(CURRENCIES)))
    cidx = {c: j for j, c in enumerate(CURRENCIES)}
    for i, pid in enumerate(pair_ids):
        if pid not in PAIR_CURRENCIES:
            raise ValueError(f"unknown FX pair instrument_id {pid}")
        base, quote = PAIR_CURRENCIES[pid]
        a[i, cidx[base]] += 1.0
        a[i, cidx[quote]] -= 1.0
    return a


def free_exposure_matrix(pair_ids: Sequence[int]) -> np.ndarray:
    """Exposure matrix restricted to the non-numeraire currencies."""
    a = exposure_matrix(pair_ids)
    keep = [j for j, c in enumerate(CURRENCIES) if c != NUMERAIRE]
    return a[:, keep]


def currency_exposures(positions: Mapping[int, float]) -> Dict[str, float]:
    """Translate pair positions (base-notional units) into per-currency
    exposures: long 5 EUR/USD -> +5 EUR, -5 USD.  Exact A^T p."""
    out = {c: 0.0 for c in CURRENCIES}
    for pid, qty in positions.items():
        base, quote = PAIR_CURRENCIES[pid]
        out[base] += float(qty)
        out[quote] -= float(qty)
    return out


def solve_factor_returns(
    pair_ids: Sequence[int], returns: np.ndarray
) -> Tuple[np.ndarray, np.ndarray]:
    """Least-squares currency factor returns from one cross-section.

    ``returns`` is aligned with ``pair_ids``; NaN entries are dropped from
    the solve.  Returns ``(f_free, fitted)`` where ``f_free`` is over
    FREE_CURRENCIES (USD pinned to 0) and ``fitted`` is ``A f`` for ALL
    input pairs (NaN where the pair's own return was NaN keeps no special
    meaning — fitted is defined for every pair once any solve is possible).
    """
    r = np.asarray(returns, dtype=float)
    a_all = free_exposure_matrix(pair_ids)
    ok = np.isfinite(r)
    nfree = len(FREE_CURRENCIES)
    if ok.sum() == 0:
        return np.full(nfree, np.nan), np.full(len(r), np.nan)
    a = a_all[ok]
    f = np.linalg.pinv(a) @ r[ok]  # minimum-norm LS, deterministic
    fitted = a_all @ f
    return f, fitted


class FX05CrossPairRelativeValue(CrossSectionalLinearAlpha):
    """Cross-pair relative value via the currency exposure matrix.

    Economic rationale: triangular consistency ties the pairs together —
    every pair return is (to first order in log space) the difference of two
    currency factor moves.  A pair whose recent return deviates from what
    the cross-section-implied currency factors explain is rich or cheap
    versus its no-arbitrage-consistent value, and the residual converges as
    cross-pair arbitrageurs trade it back.  Signal: negative least-squares
    residual of the 1-minute pair log return against the currency exposure
    matrix (USD numeraire), on a shared 30-second grid, fitted linear
    scaling.  When fewer pairs than free currencies are observable the
    minimum-norm solution attributes shared moves to factors first, so a
    lone pair has residual 0 (no relative-value information — honest
    degradation).

    **Universe (pinned, round-3)**: only pairs whose every free currency
    appears in at least two observable pairs are scored
    (:func:`identified_pairs`).  A currency seen in a single pair has its
    factor absorb that pair's whole return, so the residual is 0 by
    construction — on this universe that is AUD/USD, USD/CAD, USD/CHF,
    USD/JPY and NZD/USD; only the EUR/USD-GBP/USD-EUR/GBP triangle carries
    cross-pair information.  Those five pairs score NaN (confidence 0)
    instead of a constant.
    """

    alpha_id = "FX05"
    name = "cross_pair_relative_value"
    asset_class = "FX"
    horizon = "5m"
    features = ("ret_log_1m_v1",)
    INPUT_FEATURE = "ret_log_1m_v1"
    GRID_STEP_NS = 30_000_000_000
    MAX_AGE_NS = 120_000_000_000
    _pair_ids: List[int] = []

    def signals(self, data):  # remember grid pair order for grid_signals
        self._pair_ids = sorted(
            i for i in self.universe(list(data)) if len(data[i])
        )
        return super().signals(data)

    def grid_signals(self, mat: np.ndarray) -> np.ndarray:
        sig = np.full_like(mat, np.nan)
        for t in range(mat.shape[1]):
            r = mat[:, t]
            ok = np.isfinite(r)
            if ok.sum() < 2:
                continue
            ident = identified_pairs(self._pair_ids, ok)
            if not ident.any():
                continue
            _, fitted = solve_factor_returns(self._pair_ids, r)
            resid = r - fitted
            sig[ident, t] = -resid[ident]
        return sig


class FX06CurrencyFactorMomentum(CrossSectionalLinearAlpha):
    """Currency-factor momentum.

    Economic rationale: order flow in a currency (not a pair) is persistent —
    macro rebalancing and hedging demand hit EUR or JPY across all their
    crosses at once, and that common flow continues over minutes.  Momentum
    at the currency-factor level is cleaner than per-pair momentum because
    the exposure matrix nets out the quote-currency noise.  Signal: solve
    currency factor returns from 1-minute pair returns on a 1-minute grid,
    smooth each factor with a trailing 5-point (5-minute) mean, and map the
    smoothed factor momentum back to pairs through the exposure matrix
    (buy pairs long the trending currencies).  Fitted linear scaling.
    """

    alpha_id = "FX06"
    name = "currency_factor_momentum"
    asset_class = "FX"
    horizon = "30s"
    features = ("ret_log_1m_v1",)
    INPUT_FEATURE = "ret_log_1m_v1"
    GRID_STEP_NS = 60_000_000_000
    MAX_AGE_NS = 180_000_000_000
    SMOOTH = 5  # trailing grid points in the factor-momentum mean
    _pair_ids: List[int] = []

    def signals(self, data):
        self._pair_ids = sorted(
            i for i in self.universe(list(data)) if len(data[i])
        )
        return super().signals(data)

    def grid_signals(self, mat: np.ndarray) -> np.ndarray:
        npair, ngrid = mat.shape
        nfree = len(FREE_CURRENCIES)
        factors = np.full((nfree, ngrid), np.nan)
        for t in range(ngrid):
            r = mat[:, t]
            if np.isfinite(r).sum() < 2:
                continue
            f, _ = solve_factor_returns(self._pair_ids, r)
            factors[:, t] = f
        # trailing SMOOTH-point mean of each factor (causal; needs >=2 obs)
        fdf = pd.DataFrame(factors.T)
        mom = fdf.rolling(self.SMOOTH, min_periods=2).mean().to_numpy().T
        a = free_exposure_matrix(self._pair_ids)
        sig = a @ np.where(np.isfinite(mom), mom, 0.0)
        # a pair's signal is only defined when at least one of its factor
        # legs was observed in the window
        defined = (np.abs(a) @ np.isfinite(mom).astype(float)) > 0
        sig[~defined] = np.nan
        sig[~np.isfinite(mat)] = np.nan  # pair must itself be quoted
        return sig
