// Rolling event-time primitives for the native feature engine (API_FEATURES.md
// section 2; mirrors python/src/iap/features/rolling.py semantics exactly).
//
// All windows are half-open intervals (t - w, t] on exchange_ts: a sample
// stamped exactly t - w has left the window, one stamped t is inside it.
// Integer inputs keep exact integer running sums; float inputs are added and
// subtracted in the same FIFO order as the reference so incremental drift
// stays well inside the 1e-9 golden tolerance.
//
// Hot-path design: ring buffers over contiguous storage. Capacity grows
// geometrically while a window is still filling; once the session's peak
// window population has been seen, no further allocation occurs (conventions
// section 8: no allocation in feature hot loops after warmup).

#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace iap {

// Accumulator type of a rolling sum. Integer windows accumulate in __int128
// so the running sum is EXACT for any number of samples, like the Python
// reference's arbitrary-precision int: an int64 accumulator wraps once a
// window has folded in enough samples, and because trim() then subtracts
// from the wrapped total the window sum stays corrupted for the rest of the
// session (permanently poisoned ofi_* / signed_volume_*). Float windows keep
// their double accumulator (same add/evict order as the reference).
template <typename T>
struct RollingAccum {
    using type = T;
};
template <>
struct RollingAccum<std::int64_t> {
    using type = __int128;
};

// Rolling sums of an N-tuple over an event-time window. add() appends a
// sample and evicts expired ones; trim() evicts samples with ts <= now - w.
template <typename T, std::size_t N>
class RollingSum {
public:
    using Acc = typename RollingAccum<T>::type;

    explicit RollingSum(std::int64_t window_ns) : window_(window_ns) {
        sums_.fill(Acc{});
        ts_.resize(16);
        vals_.resize(16);
    }

    void add(std::int64_t ts, const std::array<T, N>& vals) {
        if (count_ == ts_.size()) grow();
        const std::size_t slot = (head_ + count_) & (ts_.size() - 1);
        ts_[slot] = ts;
        vals_[slot] = vals;
        for (std::size_t i = 0; i < N; ++i) sums_[i] += vals[i];
        ++count_;
        trim(ts);
    }

    // `now` is widened before the subtraction: exchange_ts is a signed 64-bit
    // wire field the codec accepts down to INT64_MIN, and `now - window_`
    // overflowed int64 for a timestamp within one window of the bottom of
    // the range.
    void trim(std::int64_t now) {
        const __int128 cutoff = static_cast<__int128>(now) - window_;
        while (count_ > 0 && static_cast<__int128>(ts_[head_]) <= cutoff) {
            for (std::size_t i = 0; i < N; ++i) sums_[i] -= vals_[head_][i];
            head_ = (head_ + 1) & (ts_.size() - 1);
            --count_;
        }
    }

    Acc sum(std::size_t i) const { return sums_[i]; }
    std::size_t count() const { return count_; }

private:
    void grow() {
        // Double capacity, re-laying the ring out linearly (rare: only while
        // a window's population is still reaching its session peak).
        const std::size_t cap = ts_.size() * 2;
        std::vector<std::int64_t> nts(cap);
        std::vector<std::array<T, N>> nvals(cap);
        for (std::size_t i = 0; i < count_; ++i) {
            const std::size_t slot = (head_ + i) & (ts_.size() - 1);
            nts[i] = ts_[slot];
            nvals[i] = vals_[slot];
        }
        ts_.swap(nts);
        vals_.swap(nvals);
        head_ = 0;
    }

    std::int64_t window_;
    std::vector<std::int64_t> ts_;               // power-of-two ring
    std::vector<std::array<T, N>> vals_;
    std::size_t head_ = 0;
    std::size_t count_ = 0;
    std::array<Acc, N> sums_;
};

// Append-only (ts, value) series with at-or-before lookup and trimming —
// the mid-history structure behind returns/history lookups. at_or_before(t)
// returns the latest sample with ts <= t ("history lookup" rule: no
// interpolation). Mirrors python rolling.TimeSeries (compaction at 4096).
template <typename T>
class TimeSeries {
public:
    static constexpr std::size_t kCompactAt = 4096;

    TimeSeries() {
        ts_.reserve(kCompactAt * 2);
        vals_.reserve(kCompactAt * 2);
    }

    void append(std::int64_t ts, T val) {
        ts_.push_back(ts);
        vals_.push_back(val);
    }

    // Latest value with ts <= t; found=false when no sample is that early.
    // `t` is __int128 because every caller passes `now - lookback`, which
    // overflows int64 for an exchange_ts near INT64_MIN (the codec accepts
    // the whole i64 range on the wire).
    bool at_or_before(__int128 t, T& out) const {
        const auto lo = ts_.begin() + static_cast<std::ptrdiff_t>(start_);
        auto it = std::upper_bound(
            lo, ts_.end(), t,
            [](__int128 v, std::int64_t x) { return v < static_cast<__int128>(x); });
        if (it == lo) return false;
        out = vals_[static_cast<std::size_t>(it - ts_.begin()) - 1];
        return true;
    }

    bool empty() const { return ts_.size() == start_; }
    T last() const { return vals_.back(); }

    // Forget samples with ts < min_ts, keeping the newest at-or-before one
    // (so at_or_before stays correct at the trim boundary).
    void trim(__int128 min_ts) {
        auto it = std::upper_bound(
            ts_.begin(), ts_.end(), min_ts,
            [](__int128 v, std::int64_t x) { return v < static_cast<__int128>(x); });
        const std::size_t i = static_cast<std::size_t>(it - ts_.begin());
        if (i > 0) start_ = std::max(start_, i - 1);
        if (start_ >= kCompactAt) {
            ts_.erase(ts_.begin(), ts_.begin() + static_cast<std::ptrdiff_t>(start_));
            vals_.erase(vals_.begin(),
                        vals_.begin() + static_cast<std::ptrdiff_t>(start_));
            start_ = 0;
        }
    }

private:
    std::vector<std::int64_t> ts_;
    std::vector<T> vals_;
    std::size_t start_ = 0;
};

}  // namespace iap
