//! Wire-codec round-trip and malformed-input rejection tests.

use venue::{
    decode_frame, decode_stream, encode_order, encode_report, ExecStatus, ExecutionReport,
    Message, OrderRequest, OrderType,
};

fn sample_order() -> OrderRequest {
    OrderRequest {
        order_id: 0xDEAD_BEEF_CAFE_F00D,
        instrument_id: 101,
        side: 1,
        qty: 250_000,
        price_ticks: 116_570,
        order_type: OrderType::Limit.as_u8(),
        venue_id: 12,
        strategy_id: "FX05/prod".to_string(),
        urgency: 0.734375, // exactly representable
        timestamp: 1_787_578_200_123_456_789,
    }
}

fn sample_report() -> ExecutionReport {
    ExecutionReport {
        order_id: 42,
        execution_id: 7,
        status: ExecStatus::Partial.as_u8(),
        filled_qty: 100,
        fill_price_ticks: -3, // negative prices survive (rates products)
        venue_id: 1,
        exchange_ts: 1_787_578_200_000_000_000,
        receive_ts: 1_787_578_200_000_150_000,
        fees: -0.25,
    }
}

#[test]
fn order_round_trips_bit_exactly() {
    let frame = encode_order(&sample_order()).unwrap();
    let (msg, used) = decode_frame(&frame).unwrap();
    assert_eq!(used, frame.len());
    match msg {
        Message::Order(o) => {
            assert_eq!(o, sample_order());
            assert_eq!(o.urgency.to_bits(), sample_order().urgency.to_bits());
        }
        other => panic!("wrong message type: {other:?}"),
    }
}

#[test]
fn report_round_trips_bit_exactly() {
    let frame = encode_report(&sample_report());
    let (msg, used) = decode_frame(&frame).unwrap();
    assert_eq!(used, frame.len());
    match msg {
        Message::Report(r) => {
            assert_eq!(r, sample_report());
            assert_eq!(r.fees.to_bits(), sample_report().fees.to_bits());
        }
        other => panic!("wrong message type: {other:?}"),
    }
}

#[test]
fn empty_strategy_and_unicode_round_trip() {
    let mut o = sample_order();
    o.strategy_id = String::new();
    let frame = encode_order(&o).unwrap();
    assert!(matches!(decode_frame(&frame).unwrap().0, Message::Order(b) if b == o));
    o.strategy_id = "αβγ-стратегия".to_string();
    let frame = encode_order(&o).unwrap();
    assert!(matches!(decode_frame(&frame).unwrap().0, Message::Order(b) if b == o));
}

#[test]
fn stream_of_mixed_frames_decodes_in_order() {
    let mut buf = Vec::new();
    buf.extend(encode_order(&sample_order()).unwrap());
    buf.extend(encode_report(&sample_report()));
    buf.extend(encode_order(&sample_order()).unwrap());
    let msgs = decode_stream(&buf).unwrap();
    assert_eq!(msgs.len(), 3);
    assert!(matches!(msgs[0], Message::Order(_)));
    assert!(matches!(msgs[1], Message::Report(_)));
    assert!(matches!(msgs[2], Message::Order(_)));
}

#[test]
fn truncated_frames_are_rejected() {
    let frame = encode_order(&sample_order()).unwrap();
    for cut in [0, 1, 3, 4, 5, frame.len() / 2, frame.len() - 1] {
        assert!(
            decode_frame(&frame[..cut]).is_err(),
            "cut at {cut} must be rejected"
        );
    }
}

#[test]
fn corrupt_frames_are_rejected() {
    // unknown message type
    let mut frame = encode_order(&sample_order()).unwrap();
    frame[4] = 99;
    assert!(decode_frame(&frame).is_err());
    // empty payload
    let empty = 0u32.to_le_bytes().to_vec();
    assert!(decode_frame(&empty).is_err());
    // length prefix longer than the buffer
    let mut frame = encode_order(&sample_order()).unwrap();
    let bogus = (frame.len() as u32) * 2;
    frame[..4].copy_from_slice(&bogus.to_le_bytes());
    assert!(decode_frame(&frame).is_err());
    // trailing bytes inside the declared payload
    let mut frame = encode_order(&sample_order()).unwrap();
    frame.push(0);
    let longer = (frame.len() - 4) as u32;
    frame[..4].copy_from_slice(&longer.to_le_bytes());
    assert!(decode_frame(&frame).is_err());
    // strategy length pointing past the payload
    let mut frame = encode_order(&sample_order()).unwrap();
    let slen_off = 4 + 1 + 49; // prefix + msg_type + fixed body up to strategy_len
    frame[slen_off] = 0xFF;
    frame[slen_off + 1] = 0x00;
    assert!(decode_frame(&frame).is_err());
    // invalid UTF-8 strategy id
    let mut frame = encode_order(&sample_order()).unwrap();
    let last = frame.len() - 1;
    frame[last] = 0xFF;
    assert!(decode_frame(&frame).is_err());
}

#[test]
fn enum_domain_violations_are_rejected() {
    // side out of domain
    let mut frame = encode_order(&sample_order()).unwrap();
    frame[4 + 1 + 8 + 4 + 2] = 5; // side byte
    assert!(decode_frame(&frame).is_err());
    // order_type out of domain
    let mut frame = encode_order(&sample_order()).unwrap();
    frame[4 + 1 + 8 + 4 + 2 + 1] = 0; // order_type byte
    assert!(decode_frame(&frame).is_err());
    // status out of domain
    let mut frame = encode_report(&sample_report());
    frame[4 + 1 + 8 + 8 + 2] = 9; // status byte
    assert!(decode_frame(&frame).is_err());
}

#[test]
fn oversized_strategy_is_rejected_at_encode() {
    let mut o = sample_order();
    o.strategy_id = "x".repeat(venue::MAX_STRATEGY_LEN + 1);
    assert!(encode_order(&o).is_err());
}
