//! IAP production alpha scoring — the six golden alphas of `/API_ALPHA.md`
//! (EQ01, EQ03, EQ06, FX01, FX05, FX09), model `linear_z_v1`.
//!
//! Ports never fit: parameters come from
//! `configs/strategies/alpha_params.json` and every number is treated as an
//! opaque f64 (honest-reporting rule — signs and coefficients are never
//! "fixed"). Golden parity against `tests/golden/expected_alpha.json` at
//! abs/rel 1e-9 is verified in `tests/golden_alpha.rs`, both from the
//! embedded inputs and driven end-to-end through the native feature engine
//! over the golden vectors.

pub mod fx_exposure;
pub mod labels;
pub mod params;
pub mod scoring;

pub use fx_exposure::{
    identified_pairs,
    currency_exposures, fx05_raw_signals, solve_factor_returns, GRID_STEP_NS, MAX_AGE_NS,
};
pub use labels::{
    mid_labels, mid_labels_with_age, pearson_ic, MidSeries, LABEL_MAX_AGE_FLOOR_NS,
};
pub use params::{
    document_feature_version, load_params_file, load_params_json, load_params_json_checked,
    AlphaParams, GOLDEN_ALPHA_IDS, PINNED_CONF_SCALE, PINNED_Z_CLIP,
};
pub use scoring::{raw_signal, score_linear_z, score_row, AlphaSignal, EPS};
