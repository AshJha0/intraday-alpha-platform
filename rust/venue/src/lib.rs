//! IAP venue adapter layer (spec §17 boundary): the OrderRequest /
//! ExecutionReport contracts (`schemas/*.schema.json`), the length-prefixed
//! IAPV1 wire codec, and a simulated venue endpoint that accepts, acks and
//! fills orders against the shared order book.

pub mod codec;
pub mod messages;
pub mod sim;

pub use codec::{
    decode_frame, decode_stream, encode_order, encode_report, Message, MAX_STRATEGY_LEN,
    MSG_ORDER, MSG_REPORT,
};
pub use messages::{
    order_validation_error, validate_order, ExecStatus, ExecutionReport, OrderRequest, OrderType,
};
pub use sim::{RejectReason, SimVenueConfig, SimulatedVenue};
