//! Bounded single-producer single-consumer (SPSC) ring buffer.
//!
//! Hand-rolled lock-free queue with a single-writer API: [`channel`] returns
//! exactly one [`Producer`] and one [`Consumer`]; neither is `Clone`, and
//! `push`/`pop` take `&mut self`, so at most one thread can ever write and one
//! read. FIFO order is preserved exactly (order-preservation is part of the
//! platform's determinism contract).
//!
//! Unsafe code is confined to the slot accesses and is justified inline: the
//! SPSC discipline plus acquire/release ordering on `head`/`tail` guarantee
//! each slot is owned by exactly one side at any time.

use std::cell::UnsafeCell;
use std::mem::MaybeUninit;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use marketdata::IapError;

/// Cache-line-padded atomic index so the producer's `tail` and the consumer's
/// `head` do not false-share one cache line.
#[repr(align(64))]
struct PaddedAtomicUsize(AtomicUsize);

struct Inner<T> {
    /// `capacity + 1` slots; the one permanently-empty slot distinguishes
    /// "full" from "empty" without a shared counter.
    slots: Box<[UnsafeCell<MaybeUninit<T>>]>,
    /// Next slot to read; written only by the consumer.
    head: PaddedAtomicUsize,
    /// Next slot to write; written only by the producer.
    tail: PaddedAtomicUsize,
}

// SAFETY: Inner is shared between exactly one Producer and one Consumer.
// All slot access is mediated by the head/tail protocol below, which hands
// each occupied slot from the producer to the consumer with release/acquire
// ordering, so sending T between threads only requires T: Send.
unsafe impl<T: Send> Sync for Inner<T> {}
unsafe impl<T: Send> Send for Inner<T> {}

impl<T> Inner<T> {
    fn advance(&self, idx: usize) -> usize {
        // slots.len() is capacity + 1; wrap without division-heavy modulo.
        let next = idx + 1;
        if next == self.slots.len() {
            0
        } else {
            next
        }
    }

    fn len(&self) -> usize {
        let head = self.head.0.load(Ordering::Acquire);
        let tail = self.tail.0.load(Ordering::Acquire);
        if tail >= head {
            tail - head
        } else {
            tail + self.slots.len() - head
        }
    }
}

impl<T> Drop for Inner<T> {
    fn drop(&mut self) {
        // SAFETY: `drop(&mut self)` proves no Producer/Consumer handle is
        // alive (they hold the only Arcs), so no concurrent access exists.
        // Slots in [head, tail) hold initialized values that were pushed but
        // never popped; drop exactly those.
        let mut idx = *self.head.0.get_mut();
        let tail = *self.tail.0.get_mut();
        while idx != tail {
            unsafe {
                (*self.slots[idx].get()).assume_init_drop();
            }
            idx = self.advance(idx);
        }
    }
}

/// The write half of the channel. Not `Clone`: single writer by construction.
pub struct Producer<T> {
    inner: Arc<Inner<T>>,
}

/// The read half of the channel. Not `Clone`: single reader by construction.
pub struct Consumer<T> {
    inner: Arc<Inner<T>>,
}

/// Create a bounded SPSC channel holding at most `capacity` items.
pub fn channel<T: Send>(capacity: usize) -> Result<(Producer<T>, Consumer<T>), IapError> {
    if capacity == 0 {
        return Err(IapError::InvalidArgument(
            "eventbus channel capacity must be > 0".to_string(),
        ));
    }
    let slots: Box<[UnsafeCell<MaybeUninit<T>>]> = (0..capacity + 1)
        .map(|_| UnsafeCell::new(MaybeUninit::uninit()))
        .collect();
    let inner = Arc::new(Inner {
        slots,
        head: PaddedAtomicUsize(AtomicUsize::new(0)),
        tail: PaddedAtomicUsize(AtomicUsize::new(0)),
    });
    Ok((
        Producer {
            inner: Arc::clone(&inner),
        },
        Consumer { inner },
    ))
}

impl<T> Producer<T> {
    /// Push one item; on a full buffer the item is handed back as `Err`.
    pub fn push(&mut self, value: T) -> Result<(), T> {
        let inner = &*self.inner;
        // Relaxed: only this producer writes `tail`.
        let tail = inner.tail.0.load(Ordering::Relaxed);
        let next = inner.advance(tail);
        // Acquire: pairs with the consumer's release store of `head`, so the
        // consumer's read of the slot we are about to overwrite has completed.
        if next == inner.head.0.load(Ordering::Acquire) {
            return Err(value); // full
        }
        // SAFETY: slot `tail` is outside [head, tail), i.e. currently empty
        // and owned by the producer: the consumer never touches slots at or
        // after `tail`, and the full-check above proves the consumer is done
        // with this slot from any previous lap.
        unsafe {
            (*inner.slots[tail].get()).write(value);
        }
        // Release: publishes the slot write before the consumer can observe
        // the new tail.
        inner.tail.0.store(next, Ordering::Release);
        Ok(())
    }

    /// Number of items currently buffered (racy but monotone-consistent).
    pub fn len(&self) -> usize {
        self.inner.len()
    }

    /// Whether the buffer is currently empty.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Maximum number of buffered items.
    pub fn capacity(&self) -> usize {
        self.inner.slots.len() - 1
    }
}

impl<T> Consumer<T> {
    /// Pop the oldest item, or `None` when the buffer is empty.
    pub fn pop(&mut self) -> Option<T> {
        let inner = &*self.inner;
        // Relaxed: only this consumer writes `head`.
        let head = inner.head.0.load(Ordering::Relaxed);
        // Acquire: pairs with the producer's release store of `tail`, making
        // the producer's slot write visible before we read it.
        if head == inner.tail.0.load(Ordering::Acquire) {
            return None; // empty
        }
        // SAFETY: head != tail, so slot `head` is inside [head, tail): it was
        // fully written by the producer (visible via the acquire load above)
        // and the producer will not touch it again until we advance `head`.
        let value = unsafe { (*inner.slots[head].get()).assume_init_read() };
        // Release: hands the now-empty slot back to the producer.
        inner.head.0.store(inner.advance(head), Ordering::Release);
        Some(value)
    }

    /// Number of items currently buffered (racy but monotone-consistent).
    pub fn len(&self) -> usize {
        self.inner.len()
    }

    /// Whether the buffer is currently empty.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Maximum number of buffered items.
    pub fn capacity(&self) -> usize {
        self.inner.slots.len() - 1
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zero_capacity_is_an_error() {
        assert!(channel::<u64>(0).is_err());
    }

    #[test]
    fn push_pop_preserves_fifo_order() {
        let (mut tx, mut rx) = channel::<u64>(8).expect("capacity > 0");
        for i in 0..8 {
            tx.push(i).expect("not full");
        }
        for i in 0..8 {
            assert_eq!(rx.pop(), Some(i));
        }
        assert_eq!(rx.pop(), None);
    }

    #[test]
    fn capacity_is_enforced_and_value_returned_on_full() {
        let (mut tx, mut rx) = channel::<u64>(3).expect("capacity > 0");
        assert_eq!(tx.capacity(), 3);
        for i in 0..3 {
            tx.push(i).expect("not full");
        }
        assert_eq!(tx.push(99), Err(99));
        assert_eq!(tx.len(), 3);
        assert_eq!(rx.pop(), Some(0));
        tx.push(3).expect("one slot freed");
        assert_eq!(tx.push(100), Err(100));
    }

    #[test]
    fn wraparound_keeps_order() {
        let (mut tx, mut rx) = channel::<u64>(4).expect("capacity > 0");
        let mut next_in = 0u64;
        let mut next_out = 0u64;
        // Push/pop far past capacity so indices wrap many times.
        for _ in 0..1000 {
            while tx.push(next_in).is_ok() {
                next_in += 1;
            }
            for _ in 0..3 {
                assert_eq!(rx.pop(), Some(next_out));
                next_out += 1;
            }
        }
        while let Some(v) = rx.pop() {
            assert_eq!(v, next_out);
            next_out += 1;
        }
        assert_eq!(next_in, next_out);
    }

    #[test]
    fn len_and_is_empty_track_contents() {
        let (mut tx, mut rx) = channel::<u64>(4).expect("capacity > 0");
        assert!(tx.is_empty() && rx.is_empty());
        tx.push(1).expect("not full");
        tx.push(2).expect("not full");
        assert_eq!(tx.len(), 2);
        assert_eq!(rx.len(), 2);
        rx.pop();
        assert_eq!(rx.len(), 1);
        rx.pop();
        assert!(rx.is_empty());
    }

    #[test]
    fn unread_items_are_dropped_exactly_once() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static DROPS: AtomicUsize = AtomicUsize::new(0);
        struct Tracked;
        impl Drop for Tracked {
            fn drop(&mut self) {
                DROPS.fetch_add(1, Ordering::SeqCst);
            }
        }
        {
            let (mut tx, mut rx) = channel::<Tracked>(4).expect("capacity > 0");
            for _ in 0..4 {
                let _ = tx.push(Tracked);
            }
            drop(rx.pop()); // one dropped by the consumer
        } // remaining three dropped by the channel itself
        assert_eq!(DROPS.load(Ordering::SeqCst), 4);
    }
}
