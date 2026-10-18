//! cargo-fuzz target (v1.18 REL-12, UNVERIFIED): any byte string must decode
//! to Ok or a typed `IapError`, never panic, and a successful decode must
//! re-encode to bytes that decode to the same events.
#![no_main]

use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    if let Ok(events) = marketdata::codec::decode_iap1(data) {
        let again = marketdata::codec::encode_iap1(&events);
        assert_eq!(marketdata::codec::decode_iap1(&again).ok(), Some(events));
    }
});
