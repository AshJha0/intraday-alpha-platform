//! IAP native feature engine: the 40 pinned features of `/API_FEATURES.md`
//! (plus 5 auxiliary alpha inputs), event-driven with preallocated rolling
//! state, golden-parity-tested against `tests/golden/expected_features.json`.

pub mod engine;
pub mod names;
pub mod rolling;

pub use engine::{FeatureEngine, FeatureVector, EPS};
pub use names::{feature_index, FEATURE_COUNT, FEATURE_NAMES, NATIVE_COUNT};
