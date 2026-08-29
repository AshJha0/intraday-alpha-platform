//! Order/execution contract structs, mirroring
//! `schemas/order_request.schema.json` and
//! `schemas/execution_report.schema.json` field-for-field.

use marketdata::IapError;
use serde::{Deserialize, Serialize};

/// Order types (u8 on the wire; schema: MARKET=1 LIMIT=2 IOC=3 FOK=4 PEG=5
/// MID=6).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum OrderType {
    /// Immediate execution at the prevailing book.
    Market = 1,
    /// Priced order; unfilled remainder rests.
    Limit = 2,
    /// Immediate-or-cancel: marketable part fills, remainder cancels.
    Ioc = 3,
    /// Fill-or-kill: fills completely or not at all.
    Fok = 4,
    /// Pegged order (simulated venue treats the peg as the touch).
    Peg = 5,
    /// Midpoint order (simulated venue crosses at the mid).
    Mid = 6,
}

impl OrderType {
    /// Wire value.
    pub const fn as_u8(self) -> u8 {
        self as u8
    }

    /// Decode a wire value; `None` for unknown codes.
    pub const fn from_u8(v: u8) -> Option<OrderType> {
        match v {
            1 => Some(OrderType::Market),
            2 => Some(OrderType::Limit),
            3 => Some(OrderType::Ioc),
            4 => Some(OrderType::Fok),
            5 => Some(OrderType::Peg),
            6 => Some(OrderType::Mid),
            _ => None,
        }
    }
}

/// Execution status (u8 on the wire; schema: NEW=1 PARTIAL=2 FILLED=3
/// CANCELED=4 REJECTED=5 EXPIRED=6).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum ExecStatus {
    /// Order accepted and acknowledged.
    New = 1,
    /// Partially filled.
    Partial = 2,
    /// Completely filled.
    Filled = 3,
    /// Canceled (IOC remainder, explicit cancel, FOK miss).
    Canceled = 4,
    /// Rejected by the venue.
    Rejected = 5,
    /// Expired.
    Expired = 6,
}

impl ExecStatus {
    /// Wire value.
    pub const fn as_u8(self) -> u8 {
        self as u8
    }

    /// Decode a wire value; `None` for unknown codes.
    pub const fn from_u8(v: u8) -> Option<ExecStatus> {
        match v {
            1 => Some(ExecStatus::New),
            2 => Some(ExecStatus::Partial),
            3 => Some(ExecStatus::Filled),
            4 => Some(ExecStatus::Canceled),
            5 => Some(ExecStatus::Rejected),
            6 => Some(ExecStatus::Expired),
            _ => None,
        }
    }
}

/// Strategy order request (`schemas/order_request.schema.json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrderRequest {
    /// Client order id (unique per session).
    pub order_id: u64,
    /// Target instrument.
    pub instrument_id: u32,
    /// BID=0 buys, ASK=1 sells.
    pub side: u8,
    /// Base units, > 0.
    pub qty: i64,
    /// Limit price in ticks; 0 for MARKET.
    pub price_ticks: i64,
    /// Order type code (see [`OrderType`]).
    pub order_type: u8,
    /// Target venue; 0 = route via SOR.
    pub venue_id: u16,
    /// Submitting strategy.
    pub strategy_id: String,
    /// 0 = fully passive, 1 = immediate.
    pub urgency: f64,
    /// Submission event time (ns).
    pub timestamp: i64,
}

/// Venue execution report (`schemas/execution_report.schema.json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExecutionReport {
    /// The order this report refers to.
    pub order_id: u64,
    /// Venue-assigned execution id (monotone per venue).
    pub execution_id: u64,
    /// Status code (see [`ExecStatus`]).
    pub status: u8,
    /// Quantity filled by THIS report (0 for pure status reports).
    pub filled_qty: i64,
    /// Fill price in ticks (0 for pure status reports).
    pub fill_price_ticks: i64,
    /// Reporting venue.
    pub venue_id: u16,
    /// Venue event time (ns).
    pub exchange_ts: i64,
    /// Client receive time (ns), >= exchange_ts.
    pub receive_ts: i64,
    /// Signed fees in currency (negative = rebate).
    pub fees: f64,
}

/// Contract-level validation of an order request (`Some(reason)` when
/// invalid). Venue- and risk-independent: only the schema's own rules.
pub fn order_validation_error(order: &OrderRequest) -> Option<String> {
    if order.side > 1 {
        return Some(format!("side must be 0 or 1: {}", order.side));
    }
    let Some(ot) = OrderType::from_u8(order.order_type) else {
        return Some(format!("unknown order_type: {}", order.order_type));
    };
    if order.qty <= 0 {
        return Some(format!("qty must be > 0: {}", order.qty));
    }
    if !(order.urgency.is_finite() && (0.0..=1.0).contains(&order.urgency)) {
        return Some(format!("urgency must be in [0, 1]: {}", order.urgency));
    }
    match ot {
        OrderType::Market => {
            if order.price_ticks != 0 {
                return Some(format!(
                    "MARKET order must carry price_ticks 0: {}",
                    order.price_ticks
                ));
            }
        }
        OrderType::Limit => {
            if order.price_ticks <= 0 {
                return Some(format!(
                    "LIMIT order needs price_ticks > 0: {}",
                    order.price_ticks
                ));
            }
        }
        // IOC/FOK may be priced (limit-style) or unpriced (market-style);
        // PEG/MID carry no price (the venue derives it).
        OrderType::Ioc | OrderType::Fok => {
            if order.price_ticks < 0 {
                return Some(format!("price_ticks must be >= 0: {}", order.price_ticks));
            }
        }
        OrderType::Peg | OrderType::Mid => {
            if order.price_ticks != 0 {
                return Some(format!(
                    "PEG/MID orders carry price_ticks 0: {}",
                    order.price_ticks
                ));
            }
        }
    }
    None
}

/// Validate an order request, `Err(IapError::Validation)` when invalid.
pub fn validate_order(order: &OrderRequest) -> Result<(), IapError> {
    match order_validation_error(order) {
        Some(reason) => Err(IapError::Validation(format!(
            "invalid OrderRequest (order_id={}): {reason}",
            order.order_id
        ))),
        None => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn valid_limit() -> OrderRequest {
        OrderRequest {
            order_id: 1,
            instrument_id: 1,
            side: 0,
            qty: 100,
            price_ticks: 2450,
            order_type: OrderType::Limit.as_u8(),
            venue_id: 1,
            strategy_id: "S1".to_string(),
            urgency: 0.5,
            timestamp: 1_000,
        }
    }

    #[test]
    fn order_contract_validation() {
        assert!(validate_order(&valid_limit()).is_ok());
        let mut o = valid_limit();
        o.qty = 0;
        assert!(validate_order(&o).is_err());
        let mut o = valid_limit();
        o.side = 2;
        assert!(validate_order(&o).is_err());
        let mut o = valid_limit();
        o.order_type = 0;
        assert!(validate_order(&o).is_err());
        let mut o = valid_limit();
        o.price_ticks = 0;
        assert!(validate_order(&o).is_err()); // LIMIT needs a price
        let mut o = valid_limit();
        o.order_type = OrderType::Market.as_u8();
        assert!(validate_order(&o).is_err()); // MARKET must be unpriced
        o.price_ticks = 0;
        assert!(validate_order(&o).is_ok());
        let mut o = valid_limit();
        o.urgency = 1.5;
        assert!(validate_order(&o).is_err());
        let mut o = valid_limit();
        o.urgency = f64::NAN;
        assert!(validate_order(&o).is_err());
    }
}
