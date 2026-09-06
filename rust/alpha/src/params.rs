//! Fitted alpha parameters (`configs/strategies/alpha_params.json`).
//!
//! Ports never fit (API_ALPHA.md §2): every number is loaded as an opaque
//! f64 from the JSON produced by the Python research layer. The honest-
//! reporting rule carries over — `hypothesis_confirmed = false` params ship
//! with the sign the fit produced; this loader never adjusts anything.

use std::collections::BTreeMap;
use std::path::Path;

use marketdata::IapError;

/// The six production alpha ids, pinned (API_ALPHA.md).
pub const GOLDEN_ALPHA_IDS: [&str; 6] = ["EQ01", "EQ03", "EQ06", "FX01", "FX05", "FX09"];

/// The pinned scoring model implemented by this port.
pub const MODEL_LINEAR_Z_V1: &str = "linear_z_v1";

/// One alpha's fitted `linear_z_v1` parameter set.
#[derive(Debug, Clone, PartialEq)]
pub struct AlphaParams {
    /// "EQ01" .. "FX12".
    pub alpha_id: String,
    /// Scoring model name (must be `linear_z_v1` for the golden six).
    pub model: String,
    /// Pinned label horizon (one of the 11).
    pub horizon: String,
    /// Pooled raw-signal mean over the training frames.
    pub mu: f64,
    /// Pooled raw-signal standard deviation.
    pub sigma: f64,
    /// Fitted (free-signed) OLS slope; `beta == beta_fit` always.
    pub beta: f64,
    /// The slope the fit produced (recorded for honesty).
    pub beta_fit: f64,
    /// Z clip bound (pinned 4.0).
    pub z_clip: f64,
    /// Confidence scale (pinned 2.0).
    pub conf_scale: f64,
    /// Registry feature names the alpha reads.
    pub features: Vec<String>,
    /// Whether the fitted sign agreed with the stated hypothesis.
    pub hypothesis_confirmed: bool,
    /// Pooled training row count.
    pub n_train: u64,
}

/// Pinned scoring constants every params file must carry (API_ALPHA.md §2).
pub const PINNED_Z_CLIP: f64 = 4.0;
/// Pinned confidence scale.
pub const PINNED_CONF_SCALE: f64 = 2.0;

impl AlphaParams {
    /// True when the fit found no usable evidence: every row scores
    /// `(0.0, 0.0)` (pinned dead-alpha rule).
    pub fn is_dead(&self) -> bool {
        !(self.sigma > 0.0) || self.beta == 0.0
    }
}

fn get_f64(obj: &serde_json::Value, aid: &str, key: &str) -> Result<f64, IapError> {
    obj[key].as_f64().ok_or_else(|| {
        IapError::InvalidArgument(format!("alpha_params {aid}: missing/non-numeric {key}"))
    })
}

fn get_str(obj: &serde_json::Value, aid: &str, key: &str) -> Result<String, IapError> {
    Ok(obj[key]
        .as_str()
        .ok_or_else(|| {
            IapError::InvalidArgument(format!("alpha_params {aid}: missing string {key}"))
        })?
        .to_string())
}

impl AlphaParams {
    /// Parse one alpha's parameter object (the value under `params.<id>`).
    pub fn from_json(alpha_id: &str, obj: &serde_json::Value) -> Result<AlphaParams, IapError> {
        let model = get_str(obj, alpha_id, "model")?;
        if model != MODEL_LINEAR_Z_V1 {
            return Err(IapError::InvalidArgument(format!(
                "alpha_params {alpha_id}: unsupported model {model:?}"
            )));
        }
        if obj["fitted"].as_bool() != Some(true) {
            return Err(IapError::InvalidArgument(format!(
                "alpha_params {alpha_id}: not fitted"
            )));
        }
        let features = obj["features"]
            .as_array()
            .ok_or_else(|| {
                IapError::InvalidArgument(format!("alpha_params {alpha_id}: missing features"))
            })?
            .iter()
            .map(|v| {
                v.as_str().map(str::to_string).ok_or_else(|| {
                    IapError::InvalidArgument(format!(
                        "alpha_params {alpha_id}: non-string feature name"
                    ))
                })
            })
            .collect::<Result<Vec<_>, _>>()?;
        let mu = get_f64(obj, alpha_id, "mu")?;
        let sigma = get_f64(obj, alpha_id, "sigma")?;
        let beta = get_f64(obj, alpha_id, "beta")?;
        let beta_fit = get_f64(obj, alpha_id, "beta_fit")?;
        let z_clip = get_f64(obj, alpha_id, "z_clip")?;
        let conf_scale = get_f64(obj, alpha_id, "conf_scale")?;
        // Pinned loader validation (API_ALPHA.md §2): the ports obey the
        // file's z_clip/conf_scale, so a hand-edited value would silently
        // break cross-language parity — reject it instead.
        if z_clip != PINNED_Z_CLIP || conf_scale != PINNED_CONF_SCALE {
            return Err(IapError::InvalidArgument(format!(
                "alpha_params {alpha_id}: z_clip/conf_scale must be the pinned \
                 {PINNED_Z_CLIP}/{PINNED_CONF_SCALE} (got {z_clip}/{conf_scale})"
            )));
        }
        for (name, v) in [("mu", mu), ("sigma", sigma), ("beta", beta), ("beta_fit", beta_fit)] {
            if !v.is_finite() {
                return Err(IapError::InvalidArgument(format!(
                    "alpha_params {alpha_id}: non-finite {name}"
                )));
            }
        }
        // sigma <= 0 is legal ONLY for a dead alpha (beta == 0): any other
        // file would score every row at confidence 1.0.
        if sigma <= 0.0 && beta != 0.0 {
            return Err(IapError::InvalidArgument(format!(
                "alpha_params {alpha_id}: sigma <= 0 is only legal for a dead \
                 alpha (beta == 0); got sigma={sigma}, beta={beta}"
            )));
        }
        Ok(AlphaParams {
            alpha_id: alpha_id.to_string(),
            model,
            horizon: get_str(obj, alpha_id, "horizon")?,
            mu,
            sigma,
            beta,
            beta_fit,
            z_clip,
            conf_scale,
            features,
            hypothesis_confirmed: obj["hypothesis_confirmed"].as_bool().unwrap_or(false),
            n_train: obj["n_train"].as_u64().unwrap_or(0),
        })
    }
}

/// The feature-registry hash the parameters were fitted against
/// (`feature_version` in the document header), when present.
pub fn document_feature_version(doc: &serde_json::Value) -> Option<&str> {
    doc["feature_version"].as_str()
}

/// Load the golden alphas, rejecting a `feature_version` that differs from
/// the engine's registry hash (pinned, API_ALPHA.md §2). Pass `None` only
/// for tooling that inspects a historic file.
pub fn load_params_json_checked(
    doc: &serde_json::Value,
    expected_feature_version: Option<&str>,
) -> Result<BTreeMap<String, AlphaParams>, IapError> {
    if let Some(want) = expected_feature_version {
        let got = document_feature_version(doc);
        if got != Some(want) {
            return Err(IapError::InvalidArgument(format!(
                "alpha_params.json: feature_version {got:?} does not match the \
                 engine registry hash {want:?} — the parameters were fitted \
                 against a different feature registry"
            )));
        }
    }
    load_params_json(doc)
}

/// Load the six golden alphas' parameters from an `alpha_params.json`
/// document (the `params` map may carry more alphas; only the golden six
/// are required and returned).
pub fn load_params_json(doc: &serde_json::Value) -> Result<BTreeMap<String, AlphaParams>, IapError> {
    let params = doc["params"].as_object().ok_or_else(|| {
        IapError::InvalidArgument("alpha_params.json: missing params map".to_string())
    })?;
    let mut out = BTreeMap::new();
    for aid in GOLDEN_ALPHA_IDS {
        let obj = params.get(aid).ok_or_else(|| {
            IapError::InvalidArgument(format!("alpha_params.json: missing alpha {aid}"))
        })?;
        out.insert(aid.to_string(), AlphaParams::from_json(aid, obj)?);
    }
    Ok(out)
}

/// Load golden-alpha parameters from a file path.
pub fn load_params_file<P: AsRef<Path>>(path: P) -> Result<BTreeMap<String, AlphaParams>, IapError> {
    let text = std::fs::read_to_string(path.as_ref())?;
    let doc: serde_json::Value = serde_json::from_str(&text)
        .map_err(|e| IapError::Codec(format!("alpha_params.json: {e}")))?;
    load_params_json(&doc)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_unknown_model_and_unfitted() {
        let bad: serde_json::Value = serde_json::from_str(
            r#"{"model":"other","fitted":true,"horizon":"1s","mu":0,"sigma":1,
                "beta":0,"beta_fit":0,"z_clip":4.0,"conf_scale":2.0,"features":[]}"#,
        )
        .unwrap();
        assert!(AlphaParams::from_json("EQ01", &bad).is_err());
        let unfitted: serde_json::Value = serde_json::from_str(
            r#"{"model":"linear_z_v1","fitted":false,"horizon":"1s","mu":0,"sigma":1,
                "beta":0,"beta_fit":0,"z_clip":4.0,"conf_scale":2.0,"features":[]}"#,
        )
        .unwrap();
        assert!(AlphaParams::from_json("EQ01", &unfitted).is_err());
    }

    fn params_json(extra: &str) -> serde_json::Value {
        serde_json::from_str(&format!(
            r#"{{"model":"linear_z_v1","fitted":true,"horizon":"1s","mu":0.0,
                "beta_fit":1.0,"features":["f"],{extra}}}"#
        ))
        .unwrap()
    }

    #[test]
    fn rejects_edited_z_clip_or_conf_scale() {
        assert!(AlphaParams::from_json(
            "EQ01",
            &params_json(r#""sigma":1.0,"beta":1.0,"z_clip":3.0,"conf_scale":2.0"#)
        )
        .is_err());
        assert!(AlphaParams::from_json(
            "EQ01",
            &params_json(r#""sigma":1.0,"beta":1.0,"z_clip":4.0,"conf_scale":1.0"#)
        )
        .is_err());
        assert!(AlphaParams::from_json(
            "EQ01",
            &params_json(r#""sigma":1.0,"beta":1.0,"z_clip":4.0,"conf_scale":2.0"#)
        )
        .is_ok());
    }

    #[test]
    fn rejects_zero_sigma_unless_the_alpha_is_dead() {
        // sigma = 0 with a live beta would score every row at confidence 1
        assert!(AlphaParams::from_json(
            "EQ01",
            &params_json(r#""sigma":0.0,"beta":1.0,"z_clip":4.0,"conf_scale":2.0"#)
        )
        .is_err());
        // the dead-alpha shape is legal and scores (0, 0) on every row
        let dead = AlphaParams::from_json(
            "EQ01",
            &params_json(r#""sigma":0.0,"beta":0.0,"z_clip":4.0,"conf_scale":2.0"#),
        )
        .expect("dead params load");
        assert!(dead.is_dead());
        assert_eq!(crate::score_linear_z(&dead, Some(3.0)), (0.0, 0.0));
    }

    #[test]
    fn rejects_a_foreign_feature_version() {
        let doc: serde_json::Value = serde_json::from_str(
            r#"{"feature_version":"aaaa","params":{}}"#,
        )
        .unwrap();
        assert_eq!(document_feature_version(&doc), Some("aaaa"));
        assert!(load_params_json_checked(&doc, Some("bbbb")).is_err());
    }
}
