//! IAP market-data core: canonical events, pinned RNG, IAP1 + JSONL codecs.
//!
//! Rust port of `python/src/iap/core` per `API_CORE.md` and
//! `schemas/FORMAT.md` (normative). All contracts are integer-exact; the
//! encoders are byte-exact across languages (verified by SHA-256 goldens).

pub mod codec;
pub mod error;
pub mod events;
pub mod rng;

pub use codec::{
    decode_iap1, decode_jsonl_line, encode_iap1, encode_jsonl, encode_jsonl_line, read_iap1,
    read_jsonl, write_iap1, write_jsonl, IAP1_HEADER_SIZE, IAP1_MAGIC, IAP1_RECORD_SIZE,
    IAP1_VERSION,
};
pub use error::IapError;
pub use events::{validate, validation_error, EventType, MarketEvent, SessionStatus, Side};
pub use rng::SplitMix64;
