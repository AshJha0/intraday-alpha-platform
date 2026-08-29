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
        Ok(AlphaParams {
            alpha_id: alpha_id.to_string(),
            model,
            horizon: get_str(obj, alpha_id, "horizon")?,
            mu: get_f64(obj, alpha_id, "mu")?,
            sigma: get_f64(obj, alpha_id, "sigma")?,
            beta: get_f64(obj, alpha_id, "beta")?,
            beta_fit: get_f64(obj, alpha_id, "beta_fit")?,
            z_clip: get_f64(obj, alpha_id, "z_clip")?,
            conf_scale: get_f64(obj, alpha_id, "conf_scale")?,
            features,
            hypothesis_confirmed: obj["hypothesis_confirmed"].as_bool().unwrap_or(false),
            n_train: obj["n_train"].as_u64().unwrap_or(0),
        })
    }
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
}
