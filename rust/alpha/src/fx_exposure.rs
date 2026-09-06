//! FX05 currency-exposure machinery (API_ALPHA.md §5, pinned).
//!
//! Currencies (sorted): AUD, CAD, CHF, EUR, GBP, JPY, NZD, USD; numeraire
//! USD. Long 1 unit of BASE/QUOTE = +1 BASE, -1 QUOTE. The factor solve
//! drops USD's column and computes the minimum-norm least-squares currency
//! factor returns `f = pinv(A_free[valid]) @ r[valid]` — implemented via a
//! Jacobi eigendecomposition of the 7x7 Gram matrix (deterministic,
//! accurate to machine precision; for >= 7 independent valid pairs this
//! equals the normal-equations solution, and for a lone pair the
//! minimum-norm solution reproduces it exactly so its residual is 0).
//!
//! FX05's raw signal for pair i is `-residual_i = -(r_i - (A_free f)_i)`
//! (NaN when the pair's own return is NaN).

use marketdata::IapError;

/// Sorted currency list (numeraire last by coincidence of the alphabet).
pub const CURRENCIES: [&str; 8] = ["AUD", "CAD", "CHF", "EUR", "GBP", "JPY", "NZD", "USD"];
/// The numeraire whose factor is pinned to 0.
pub const NUMERAIRE: &str = "USD";
/// Free (non-numeraire) currency count.
pub const N_FREE: usize = 7;
/// Shared event-time grid step (30s).
pub const GRID_STEP_NS: i64 = 30_000_000_000;
/// Maximum sample age contributed to a grid point (120s).
pub const MAX_AGE_NS: i64 = 120_000_000_000;

/// Pinned pair table: instrument_id -> (base, quote).
pub const PAIR_CURRENCIES: [(u32, &str, &str); 8] = [
    (101, "EUR", "USD"),
    (102, "GBP", "USD"),
    (103, "USD", "JPY"),
    (104, "AUD", "USD"),
    (105, "USD", "CAD"),
    (106, "USD", "CHF"),
    (107, "NZD", "USD"),
    (108, "EUR", "GBP"),
];

fn currency_index(c: &str) -> Option<usize> {
    CURRENCIES.iter().position(|&x| x == c)
}

/// Exposure row of one pair over ALL currencies (+1 base, -1 quote).
pub fn exposure_row(pair_id: u32) -> Result<[f64; 8], IapError> {
    let (_, base, quote) = PAIR_CURRENCIES
        .iter()
        .find(|&&(pid, _, _)| pid == pair_id)
        .ok_or_else(|| {
            IapError::InvalidArgument(format!("unknown FX pair instrument_id {pair_id}"))
        })?;
    let mut row = [0.0f64; 8];
    row[currency_index(base).expect("pinned currency")] += 1.0;
    row[currency_index(quote).expect("pinned currency")] -= 1.0;
    Ok(row)
}

/// Exposure matrix restricted to the non-numeraire currencies
/// (pairs x 7, pair order = input order).
pub fn free_exposure_matrix(pair_ids: &[u32]) -> Result<Vec<[f64; N_FREE]>, IapError> {
    let usd = currency_index(NUMERAIRE).expect("pinned");
    pair_ids
        .iter()
        .map(|&pid| {
            let full = exposure_row(pid)?;
            let mut free = [0.0f64; N_FREE];
            let mut j = 0;
            for (c, &v) in full.iter().enumerate() {
                if c != usd {
                    free[j] = v;
                    j += 1;
                }
            }
            Ok(free)
        })
        .collect()
}

/// Per-currency exposures `A^T p` of pair positions (spec §12): long 5
/// EUR/USD => +5 EUR, -5 USD. Returns values in [`CURRENCIES`] order.
pub fn currency_exposures(positions: &[(u32, f64)]) -> Result<[f64; 8], IapError> {
    let mut out = [0.0f64; 8];
    for &(pid, qty) in positions {
        let row = exposure_row(pid)?;
        for (o, r) in out.iter_mut().zip(row.iter()) {
            *o += qty * r;
        }
    }
    Ok(out)
}

/// Jacobi eigendecomposition of a symmetric matrix (values, vectors).
/// Deterministic cyclic sweeps; converges to machine precision.
#[allow(clippy::needless_range_loop)] // index math mirrors the textbook rotation
fn jacobi_eigen(mut g: [[f64; N_FREE]; N_FREE]) -> ([f64; N_FREE], [[f64; N_FREE]; N_FREE]) {
    let n = N_FREE;
    let mut v = [[0.0f64; N_FREE]; N_FREE];
    for (i, row) in v.iter_mut().enumerate() {
        row[i] = 1.0;
    }
    for _sweep in 0..64 {
        let mut off = 0.0f64;
        for p in 0..n {
            for q in (p + 1)..n {
                off += g[p][q] * g[p][q];
            }
        }
        if off <= 1e-30 {
            break;
        }
        for p in 0..n {
            for q in (p + 1)..n {
                if g[p][q].abs() <= 1e-300 {
                    continue;
                }
                let theta = (g[q][q] - g[p][p]) / (2.0 * g[p][q]);
                let t = theta.signum() / (theta.abs() + (theta * theta + 1.0).sqrt());
                let c = 1.0 / (t * t + 1.0).sqrt();
                let s = t * c;
                for k in 0..n {
                    let (gkp, gkq) = (g[k][p], g[k][q]);
                    g[k][p] = c * gkp - s * gkq;
                    g[k][q] = s * gkp + c * gkq;
                }
                for k in 0..n {
                    let (gpk, gqk) = (g[p][k], g[q][k]);
                    g[p][k] = c * gpk - s * gqk;
                    g[q][k] = s * gpk + c * gqk;
                }
                for row in v.iter_mut() {
                    let (vkp, vkq) = (row[p], row[q]);
                    row[p] = c * vkp - s * vkq;
                    row[q] = s * vkp + c * vkq;
                }
            }
        }
    }
    let mut vals = [0.0f64; N_FREE];
    for (i, val) in vals.iter_mut().enumerate() {
        *val = g[i][i];
    }
    (vals, v)
}

/// Minimum-norm least-squares currency factor returns from one
/// cross-section.
///
/// `returns` is aligned with `pair_ids`; NaN entries are dropped from the
/// solve. Returns `(f_free, fitted)` where `f_free` is over the free
/// currencies (USD pinned to 0) and `fitted` is `A_free f` for ALL input
/// pairs. With zero valid pairs both outputs are all-NaN.
#[allow(clippy::needless_range_loop)] // small fixed-dimension linear algebra
pub fn solve_factor_returns(
    pair_ids: &[u32],
    returns: &[f64],
) -> Result<([f64; N_FREE], Vec<f64>), IapError> {
    if pair_ids.len() != returns.len() {
        return Err(IapError::InvalidArgument(
            "solve_factor_returns: pair/return length mismatch".to_string(),
        ));
    }
    let a_all = free_exposure_matrix(pair_ids)?;
    let valid: Vec<usize> = (0..returns.len())
        .filter(|&i| returns[i].is_finite())
        .collect();
    if valid.is_empty() {
        return Ok(([f64::NAN; N_FREE], vec![f64::NAN; returns.len()]));
    }
    // Gram matrix G = A^T A and right-hand side b = A^T r over valid rows.
    let mut g = [[0.0f64; N_FREE]; N_FREE];
    let mut b = [0.0f64; N_FREE];
    for &i in &valid {
        let row = &a_all[i];
        for p in 0..N_FREE {
            b[p] += row[p] * returns[i];
            for q in 0..N_FREE {
                g[p][q] += row[p] * row[q];
            }
        }
    }
    // Min-norm LS: f = V diag(1/lambda_i, lambda_i > tol) V^T b.
    let (vals, vecs) = jacobi_eigen(g);
    let lmax = vals.iter().cloned().fold(0.0f64, f64::max);
    let tol = lmax * 1e-12;
    let mut f = [0.0f64; N_FREE];
    for i in 0..N_FREE {
        if vals[i] > tol {
            let mut proj = 0.0;
            for k in 0..N_FREE {
                proj += vecs[k][i] * b[k];
            }
            let scale = proj / vals[i];
            for k in 0..N_FREE {
                f[k] += vecs[k][i] * scale;
            }
        }
    }
    let fitted = a_all
        .iter()
        .map(|row| row.iter().zip(f.iter()).map(|(a, x)| a * x).sum())
        .collect();
    Ok((f, fitted))
}

/// Which observable pairs carry IDENTIFIABLE relative-value information
/// (pinned, API_ALPHA.md §5): a pair is identified iff every FREE currency
/// it touches appears in at least two observable pairs.  A currency seen in
/// a single observable pair has its factor absorb that pair's whole return,
/// so the residual is 0 by construction — a constant, not a signal.
pub fn identified_pairs(pair_ids: &[u32], observable: &[bool]) -> Result<Vec<bool>, IapError> {
    let rows = free_exposure_matrix(pair_ids)?;
    let mut counts = [0usize; N_FREE];
    for (i, row) in rows.iter().enumerate() {
        if !observable[i] {
            continue;
        }
        for (j, &a) in row.iter().enumerate() {
            if a != 0.0 {
                counts[j] += 1;
            }
        }
    }
    Ok(rows
        .iter()
        .enumerate()
        .map(|(i, row)| {
            observable[i]
                && row
                    .iter()
                    .enumerate()
                    .all(|(j, &a)| a == 0.0 || counts[j] >= 2)
        })
        .collect())
}

/// FX05 raw signals for one grid cross-section: `-(r_i - fitted_i)` per
/// IDENTIFIED pair, NaN where the pair's own return is NaN, fewer than 2
/// pairs are valid, or the pair carries no identifiable relative value
/// (API_ALPHA.md §5).
pub fn fx05_raw_signals(pair_ids: &[u32], returns: &[f64]) -> Result<Vec<f64>, IapError> {
    let observable: Vec<bool> = returns.iter().map(|v| v.is_finite()).collect();
    let n_valid = observable.iter().filter(|&&v| v).count();
    if n_valid < 2 {
        return Ok(vec![f64::NAN; returns.len()]);
    }
    let ident = identified_pairs(pair_ids, &observable)?;
    let (_, fitted) = solve_factor_returns(pair_ids, returns)?;
    Ok(returns
        .iter()
        .zip(fitted.iter())
        .zip(ident.iter())
        .map(|((&r, &fit), &id)| if id { -(r - fit) } else { f64::NAN })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    const ALL_PAIRS: [u32; 8] = [101, 102, 103, 104, 105, 106, 107, 108];

    #[test]
    fn exposure_rows_are_pinned() {
        // EUR/USD: +EUR (index 3), -USD (index 7)
        let r = exposure_row(101).unwrap();
        assert_eq!(r, [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, -1.0]);
        // USD/JPY: +USD, -JPY (index 5)
        let r = exposure_row(103).unwrap();
        assert_eq!(r, [0.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 1.0]);
        // EUR/GBP: +EUR, -GBP (index 4)
        let r = exposure_row(108).unwrap();
        assert_eq!(r, [0.0, 0.0, 0.0, 1.0, -1.0, 0.0, 0.0, 0.0]);
        assert!(exposure_row(999).is_err());
    }

    #[test]
    fn currency_exposures_translate_positions() {
        // long 5 EUR/USD, short 2 USD/JPY: +5 EUR, -5-2 USD... careful:
        // short 2 USD/JPY = -2 USD, +2 JPY.
        let out = currency_exposures(&[(101, 5.0), (103, -2.0)]).unwrap();
        assert_eq!(out[3], 5.0); // EUR
        assert_eq!(out[5], 2.0); // JPY
        assert_eq!(out[7], -7.0); // USD
    }

    #[test]
    fn exact_factor_structure_has_zero_residuals() {
        // Build returns exactly from a chosen factor vector: residuals ~ 0.
        let f_true = [0.5, -0.25, 0.125, 1.0, -1.5, 0.75, 0.3125];
        let a = free_exposure_matrix(&ALL_PAIRS).unwrap();
        let returns: Vec<f64> = a
            .iter()
            .map(|row| row.iter().zip(f_true.iter()).map(|(x, y)| x * y).sum())
            .collect();
        let (f, fitted) = solve_factor_returns(&ALL_PAIRS, &returns).unwrap();
        for i in 0..N_FREE {
            assert!((f[i] - f_true[i]).abs() < 1e-12, "factor {i}");
        }
        for (r, fit) in returns.iter().zip(fitted.iter()) {
            assert!((r - fit).abs() < 1e-12);
        }
    }

    #[test]
    fn lone_pair_has_zero_residual() {
        // A single valid pair: the minimum-norm solution attributes the
        // whole move to factors, so the residual (and FX05 signal) is 0 —
        // honest degradation, pinned by the reference docstring. But with
        // < 2 valid pairs fx05_raw_signals reports no signal at all.
        let mut returns = vec![f64::NAN; 8];
        returns[2] = 0.004; // USD/JPY
        let (_, fitted) = solve_factor_returns(&ALL_PAIRS, &returns).unwrap();
        assert!((fitted[2] - 0.004).abs() < 1e-15);
        let sig = fx05_raw_signals(&ALL_PAIRS, &returns).unwrap();
        assert!(sig.iter().all(|v| v.is_nan()));
    }

    #[test]
    fn nan_pairs_keep_nan_signals() {
        let mut returns = vec![0.001; 8];
        returns[4] = f64::NAN;
        let sig = fx05_raw_signals(&ALL_PAIRS, &returns).unwrap();
        assert!(sig[4].is_nan());
        // only the identified triangle (EUR/USD, GBP/USD, EUR/GBP) scores
        for (i, v) in sig.iter().enumerate() {
            let identified = i == 0 || i == 1 || i == 7;
            assert_eq!(v.is_finite(), identified, "pair index {i}");
        }
    }

    #[test]
    fn only_pairs_with_a_shared_currency_are_identified() {
        // AUD, CAD, CHF, JPY and NZD each appear in ONE pair: their factor
        // absorbs the whole return, so the residual is 0 by construction.
        let obs = [true; 8];
        let ident = identified_pairs(&ALL_PAIRS, &obs).unwrap();
        assert_eq!(
            ident,
            vec![true, true, false, false, false, false, false, true]
        );
        // the EUR/USD-GBP/USD-EUR/GBP triangle alone is identified
        let tri = [101u32, 102, 108];
        assert_eq!(
            identified_pairs(&tri, &[true; 3]).unwrap(),
            vec![true, true, true]
        );
        // two unrelated pairs carry no cross-pair information
        assert_eq!(
            identified_pairs(&[101u32, 103], &[true; 2]).unwrap(),
            vec![false, false]
        );
        // a degenerate signal is NaN, never a constant
        let returns = vec![0.001; 8];
        let sig = fx05_raw_signals(&ALL_PAIRS, &returns).unwrap();
        assert!(sig[2].is_nan() && sig[3].is_nan());
    }
}
