//! Cross-thread eventbus tests: one producer thread, one consumer thread,
//! exact FIFO order preserved across the boundary.

use std::thread;

use eventbus::channel;
use marketdata::MarketEvent;

#[test]
fn cross_thread_producer_consumer_preserves_order() {
    const N: u64 = 100_000;
    let (mut tx, mut rx) = channel::<u64>(64).expect("capacity > 0");

    let producer = thread::spawn(move || {
        for i in 0..N {
            let mut v = i;
            // Spin until the bounded buffer has room.
            loop {
                match tx.push(v) {
                    Ok(()) => break,
                    Err(back) => {
                        v = back;
                        thread::yield_now();
                    }
                }
            }
        }
    });

    let mut received = 0u64;
    while received < N {
        match rx.pop() {
            Some(v) => {
                assert_eq!(v, received, "FIFO order violated");
                received += 1;
            }
            None => thread::yield_now(),
        }
    }
    assert_eq!(rx.pop(), None);
    producer.join().expect("producer thread");
}

#[test]
fn cross_thread_market_events_arrive_intact() {
    const N: u64 = 10_000;
    let (mut tx, mut rx) = channel::<MarketEvent>(128).expect("capacity > 0");

    let make = |i: u64| MarketEvent {
        event_id: i + 1,
        instrument_id: 1,
        venue_id: 1,
        exchange_ts: 1_000 + i as i64,
        receive_ts: 2_000 + i as i64,
        sequence: i + 1,
        event_type: 1,
        side: (i % 2) as u8,
        price_ticks: 2450 + (i % 7) as i64,
        qty: 100,
        order_id: i + 10,
        trade_id: 0,
    };

    let producer = thread::spawn(move || {
        for i in 0..N {
            let mut ev = make(i);
            loop {
                match tx.push(ev) {
                    Ok(()) => break,
                    Err(back) => {
                        ev = back;
                        thread::yield_now();
                    }
                }
            }
        }
    });

    let mut received = 0u64;
    while received < N {
        match rx.pop() {
            Some(ev) => {
                assert_eq!(ev, make(received), "event {received} corrupted or reordered");
                received += 1;
            }
            None => thread::yield_now(),
        }
    }
    producer.join().expect("producer thread");
}
