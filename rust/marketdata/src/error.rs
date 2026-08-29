//! Platform error type (conventions §8: `Result<_, IapError>`, no panics on
//! malformed input).

use std::fmt;

/// Error type shared by every IAP crate.
#[derive(Debug)]
pub enum IapError {
    /// A `MarketEvent` violates the canonical contract.
    Validation(String),
    /// Malformed JSONL / IAP1 input (bad magic, truncation, bad keys, ...).
    Codec(String),
    /// Order-book routing / application error (wrong book, unknown type, ...).
    Book(String),
    /// Checkpoint serialization / restore error.
    Checkpoint(String),
    /// Invalid argument to an API call (bad capacity, bad rate, ...).
    InvalidArgument(String),
    /// Underlying I/O failure (file read/write).
    Io(String),
}

impl fmt::Display for IapError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            IapError::Validation(msg) => write!(f, "validation error: {msg}"),
            IapError::Codec(msg) => write!(f, "codec error: {msg}"),
            IapError::Book(msg) => write!(f, "order book error: {msg}"),
            IapError::Checkpoint(msg) => write!(f, "checkpoint error: {msg}"),
            IapError::InvalidArgument(msg) => write!(f, "invalid argument: {msg}"),
            IapError::Io(msg) => write!(f, "io error: {msg}"),
        }
    }
}

impl std::error::Error for IapError {}

impl From<std::io::Error> for IapError {
    fn from(err: std::io::Error) -> Self {
        IapError::Io(err.to_string())
    }
}
