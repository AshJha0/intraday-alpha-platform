//! `linear_z_v1` scoring and the pinned raw-signal formulas
//! (API_ALPHA.md §§2 and 4).
//!
//! ```text
//! z    = clip((raw - mu) / (sigma + EPS), -z_clip, +z_clip)
//! er   = beta * z
//! conf = min(1, |z| / conf_scale)
//! invalid raw (NaN / missing input)  =>  er = 0.0, conf = 0.0
//! dead alpha (sigma <= 0 or beta == 0)  =>  er = 0.0, conf = 0.0
//! ```
//!
//! The dead-alpha rule is pinned (round-3): with `sigma == 0` the z
//! denominator collapses to EPS, so every row clipped to +-z_clip and
//! reported **confidence 1.0** with expected return 0 — maximum conviction
//! in nothing.

use crate::params::AlphaParams;

/// Pinned epsilon in the z denominator.
pub const EPS: f64 = 1e-12;

/// One scored row (schemas/alpha_signal.schema.json).
#[derive(Debug, Clone, PartialEq)]
pub struct AlphaSignal {
    /// "EQ01" .. "FX12".
    pub alpha_id: String,
    /// Scored instrument.
    pub instrument_id: u32,
    /// `exchange_ts` of the scored feature row.
    pub timestamp: i64,
    /// Expected mid-to-mid return over `horizon` (dimensionless).
    pub expected_return: f64,
    /// Confidence in [0, 1]; exactly 0 whenever the signal is invalid.
    pub confidence: f64,
    /// Pinned label horizon.
    pub horizon: String,
}

/// Score one raw-signal observation (`None`/non-finite = invalid row).
/// Returns `(expected_return, confidence)` — finite always, `(0.0, 0.0)`
/// exactly for invalid rows.
pub fn score_linear_z(params: &AlphaParams, raw: Option<f64>) -> (f64, f64) {
    if params.is_dead() {
        return (0.0, 0.0);
    }
    match raw {
        Some(x) if x.is_finite() => {
            let z = ((x - params.mu) / (params.sigma + EPS))
                .clamp(-params.z_clip, params.z_clip);
            (params.beta * z, (z.abs() / params.conf_scale).min(1.0))
        }
        _ => (0.0, 0.0),
    }
}

/// Build a full [`AlphaSignal`] for one row.
pub fn score_row(
    params: &AlphaParams,
    instrument_id: u32,
    timestamp: i64,
    raw: Option<f64>,
) -> AlphaSignal {
    let (expected_return, confidence) = score_linear_z(params, raw);
    AlphaSignal {
        alpha_id: params.alpha_id.clone(),
        instrument_id,
        timestamp,
        expected_return,
        confidence,
        horizon: params.horizon.clone(),
    }
}

/// Raw signals of the five single-frame golden alphas (API_ALPHA.md §4).
/// Inputs are registry feature values, `None` = invalid; any invalid input
/// makes the raw signal invalid. FX05's raw signal comes from
/// [`crate::fx_exposure`] instead.
pub fn raw_signal(
    alpha_id: &str,
    get: &dyn Fn(&str) -> Option<f64>,
) -> Result<Option<f64>, marketdata::IapError> {
    let raw = match alpha_id {
        "EQ01" | "FX01" => get("micro_mid_dev_bps_v1"),
        "EQ03" => match (
            get("ofi_norm_l1_w1s_v1"),
            get("ofi_norm_l5_w1s_v1"),
            get("ofi_norm_l5_w5s_v1"),
        ) {
            (Some(a), Some(b), Some(c)) => Some(0.5 * a + 0.3 * b + 0.2 * c),
            _ => None,
        },
        "EQ06" => get("ret_vol_adj_10s_v1"),
        "FX09" => match (get("ret_vol_adj_10s_v1"), get("vol_regime_ratio_v1")) {
            (Some(r), Some(v)) => Some(-r * v),
            _ => None,
        },
        other => {
            return Err(marketdata::IapError::InvalidArgument(format!(
                "no single-frame raw-signal formula for alpha {other:?}"
            )))
        }
    };
    Ok(raw.filter(|v| v.is_finite()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn params() -> AlphaParams {
        AlphaParams {
            alpha_id: "EQ01".to_string(),
            model: "linear_z_v1".to_string(),
            horizon: "1s".to_string(),
            mu: 1.0,
            sigma: 2.0,
            beta: -0.5,
            beta_fit: -0.5,
            z_clip: 4.0,
            conf_scale: 2.0,
            features: vec!["micro_mid_dev_bps_v1".to_string()],
            hypothesis_confirmed: false,
            n_train: 10,
        }
    }

    #[test]
    fn linear_z_basic_math() {
        let p = params();
        // raw 5 -> z = (5-1)/(2+eps) = 2 -> er = -1, conf = 1
        let (er, conf) = score_linear_z(&p, Some(5.0));
        assert!((er - -1.0).abs() < 1e-12);
        assert!((conf - 1.0).abs() < 1e-12);
    }

    #[test]
    fn z_is_clipped_and_conf_capped() {
        let p = params();
        // raw 1e9 -> z clipped to 4 -> er = -2, conf capped at 1
        let (er, conf) = score_linear_z(&p, Some(1e9));
        assert_eq!(er, -0.5 * 4.0);
        assert_eq!(conf, 1.0);
        let (er_neg, _) = score_linear_z(&p, Some(-1e9));
        assert_eq!(er_neg, 0.5 * 4.0);
    }

    #[test]
    fn invalid_raw_scores_exact_zero() {
        let p = params();
        assert_eq!(score_linear_z(&p, None), (0.0, 0.0));
        assert_eq!(score_linear_z(&p, Some(f64::NAN)), (0.0, 0.0));
        assert_eq!(score_linear_z(&p, Some(f64::INFINITY)), (0.0, 0.0));
    }

    #[test]
    fn missing_input_invalidates_composite_raw() {
        let get = |name: &str| -> Option<f64> {
            if name == "ofi_norm_l1_w1s_v1" {
                Some(1.0)
            } else {
                None
            }
        };
        assert_eq!(raw_signal("EQ03", &get).unwrap(), None);
        assert!(raw_signal("ZZ99", &get).is_err());
    }

    #[test]
    fn eq03_weights_are_pinned() {
        let get = |name: &str| -> Option<f64> {
            match name {
                "ofi_norm_l1_w1s_v1" => Some(1.0),
                "ofi_norm_l5_w1s_v1" => Some(10.0),
                "ofi_norm_l5_w5s_v1" => Some(100.0),
                _ => None,
            }
        };
        let raw = raw_signal("EQ03", &get).unwrap().unwrap();
        assert!((raw - (0.5 + 3.0 + 20.0)).abs() < 1e-12);
    }
}
