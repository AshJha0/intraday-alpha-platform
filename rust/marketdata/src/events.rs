//! Canonical `MarketEvent` contract and enums (PLATFORM_CONVENTIONS.md §1-§2).
//!
//! All prices are i64 ticks, quantities i64 base units, timestamps i64 ns
//! since the Unix epoch. The field order below is the canonical JSONL key
//! order and the IAP1 record field order — do not reorder.

use crate::error::IapError;

/// Order/quote/aggressor side (u8 on the wire).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Side {
    Bid = 0,
    Ask = 1,
}

impl Side {
    /// Wire value (BID=0, ASK=1).
    pub const fn as_u8(self) -> u8 {
        self as u8
    }
}

/// Canonical event types (u8 on the wire).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum EventType {
    Add = 1,
    Modify = 2,
    Cancel = 3,
    Execute = 4,
    Trade = 5,
    Quote = 6,
    Snapshot = 7,
    Status = 8,
    Heartbeat = 9,
}

impl EventType {
    /// Wire value.
    pub const fn as_u8(self) -> u8 {
        self as u8
    }

    /// Decode a wire value; `None` for unknown codes.
    pub const fn from_u8(v: u8) -> Option<EventType> {
        match v {
            1 => Some(EventType::Add),
            2 => Some(EventType::Modify),
            3 => Some(EventType::Cancel),
            4 => Some(EventType::Execute),
            5 => Some(EventType::Trade),
            6 => Some(EventType::Quote),
            7 => Some(EventType::Snapshot),
            8 => Some(EventType::Status),
            9 => Some(EventType::Heartbeat),
            _ => None,
        }
    }
}

/// STATUS event payload, carried in the `qty` field.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum SessionStatus {
    Trading = 1,
    Halt = 2,
    Auction = 3,
    Close = 4,
}

impl SessionStatus {
    /// Decode a status code; `None` for unknown codes.
    pub const fn from_i64(v: i64) -> Option<SessionStatus> {
        match v {
            1 => Some(SessionStatus::Trading),
            2 => Some(SessionStatus::Halt),
            3 => Some(SessionStatus::Auction),
            4 => Some(SessionStatus::Close),
            _ => None,
        }
    }
}

/// Canonical JSONL key order / IAP1 field order (normative).
pub const FIELDS: [&str; 12] = [
    "event_id",
    "instrument_id",
    "venue_id",
    "exchange_ts",
    "receive_ts",
    "sequence",
    "event_type",
    "side",
    "price_ticks",
    "qty",
    "order_id",
    "trade_id",
];

/// One canonical market event. Field order is normative (JSONL/IAP1).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct MarketEvent {
    pub event_id: u64,
    pub instrument_id: u32,
    pub venue_id: u16,
    pub exchange_ts: i64,
    pub receive_ts: i64,
    pub sequence: u64,
    pub event_type: u8,
    pub side: u8,
    pub price_ticks: i64,
    pub qty: i64,
    pub order_id: u64,
    pub trade_id: u64,
}

/// Return `Some(reason)` if `ev` violates the contract, else `None`.
///
/// Mirrors `iap/core/events.py::validation_error`. Integer domains beyond the
/// Rust field types cannot occur here; the remaining checks are the enum
/// domains, `receive_ts >= exchange_ts`, and the per-event-type payload rules
/// from `schemas/FORMAT.md` §4.
pub fn validation_error(ev: &MarketEvent) -> Option<String> {
    if ev.receive_ts < ev.exchange_ts {
        return Some(format!(
            "receive_ts {} < exchange_ts {}",
            ev.receive_ts, ev.exchange_ts
        ));
    }
    let Some(et) = EventType::from_u8(ev.event_type) else {
        return Some(format!("unknown event_type: {}", ev.event_type));
    };
    if ev.side > 1 {
        return Some(format!("side must be 0 (BID) or 1 (ASK): {}", ev.side));
    }
    match et {
        EventType::Add | EventType::Modify | EventType::Cancel | EventType::Execute => {
            if ev.order_id == 0 {
                return Some(format!("order_id required for event_type {}", ev.event_type));
            }
            if ev.qty <= 0 && et != EventType::Cancel {
                return Some(format!(
                    "qty must be > 0 for event_type {}: {}",
                    ev.event_type, ev.qty
                ));
            }
            if ev.price_ticks <= 0 && et != EventType::Cancel {
                return Some(format!(
                    "price_ticks must be > 0 for event_type {}: {}",
                    ev.event_type, ev.price_ticks
                ));
            }
        }
        EventType::Trade | EventType::Quote | EventType::Snapshot => {
            if ev.qty <= 0 {
                return Some(format!(
                    "qty must be > 0 for event_type {}: {}",
                    ev.event_type, ev.qty
                ));
            }
            if ev.price_ticks <= 0 {
                return Some(format!(
                    "price_ticks must be > 0 for event_type {}: {}",
                    ev.event_type, ev.price_ticks
                ));
            }
            if et == EventType::Trade && ev.trade_id == 0 {
                return Some("trade_id required for TRADE".to_string());
            }
        }
        EventType::Status => {
            if SessionStatus::from_i64(ev.qty).is_none() {
                return Some(format!("STATUS qty must be a SessionStatus code: {}", ev.qty));
            }
        }
        EventType::Heartbeat => {} // no payload constraints
    }
    None
}

/// Return `Err(IapError::Validation)` if `ev` violates the canonical contract.
pub fn validate(ev: &MarketEvent) -> Result<(), IapError> {
    match validation_error(ev) {
        Some(reason) => Err(IapError::Validation(format!(
            "invalid MarketEvent (event_id={}): {}",
            ev.event_id, reason
        ))),
        None => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn valid_add() -> MarketEvent {
        MarketEvent {
            event_id: 1,
            instrument_id: 1,
            venue_id: 1,
            exchange_ts: 1_000,
            receive_ts: 1_500,
            sequence: 1,
            event_type: EventType::Add.as_u8(),
            side: Side::Bid.as_u8(),
            price_ticks: 2450,
            qty: 100,
            order_id: 7,
            trade_id: 0,
        }
    }

    #[test]
    fn valid_event_passes() {
        assert_eq!(validation_error(&valid_add()), None);
        assert!(validate(&valid_add()).is_ok());
    }

    #[test]
    fn receive_before_exchange_rejected() {
        let mut ev = valid_add();
        ev.receive_ts = ev.exchange_ts - 1;
        assert!(validation_error(&ev).is_some());
    }

    #[test]
    fn unknown_event_type_rejected() {
        let mut ev = valid_add();
        ev.event_type = 0;
        assert!(validation_error(&ev).is_some());
        ev.event_type = 10;
        assert!(validation_error(&ev).is_some());
    }

    #[test]
    fn bad_side_rejected() {
        let mut ev = valid_add();
        ev.side = 2;
        assert!(validation_error(&ev).is_some());
    }

    #[test]
    fn book_events_require_order_id_qty_price() {
        let mut ev = valid_add();
        ev.order_id = 0;
        assert!(validation_error(&ev).is_some());

        let mut ev = valid_add();
        ev.qty = 0;
        assert!(validation_error(&ev).is_some());

        let mut ev = valid_add();
        ev.price_ticks = 0;
        assert!(validation_error(&ev).is_some());

        // CANCEL allows qty/price 0 but still needs order_id.
        let mut ev = valid_add();
        ev.event_type = EventType::Cancel.as_u8();
        ev.qty = 0;
        ev.price_ticks = 0;
        assert_eq!(validation_error(&ev), None);
    }

    #[test]
    fn trade_requires_trade_id() {
        let mut ev = valid_add();
        ev.event_type = EventType::Trade.as_u8();
        ev.order_id = 0;
        ev.trade_id = 0;
        assert!(validation_error(&ev).is_some());
        ev.trade_id = 42;
        assert_eq!(validation_error(&ev), None);
    }

    #[test]
    fn status_qty_must_be_session_status() {
        let mut ev = valid_add();
        ev.event_type = EventType::Status.as_u8();
        ev.order_id = 0;
        ev.price_ticks = 0;
        ev.qty = 5;
        assert!(validation_error(&ev).is_some());
        ev.qty = SessionStatus::Halt as i64;
        assert_eq!(validation_error(&ev), None);
    }

    #[test]
    fn heartbeat_has_no_payload_constraints() {
        let ev = MarketEvent {
            event_type: EventType::Heartbeat.as_u8(),
            ..MarketEvent::default()
        };
        assert_eq!(validation_error(&ev), None);
    }
}
