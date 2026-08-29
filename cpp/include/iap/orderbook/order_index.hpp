// Open-addressing hash map: order_id (u64) -> pool slot (u32).
//
// Linear probing with backward-shift deletion (no tombstones), power-of-two
// capacity, contiguous storage. After reserve() no allocation happens until
// the load factor threshold is crossed — this keeps the book hot path
// allocation-free after warmup (PLATFORM_CONVENTIONS.md section 8).

#pragma once

#include <cstdint>
#include <vector>

namespace iap {

class OrderIndex {
public:
    static constexpr std::uint32_t NPOS = 0xFFFFFFFFu;

    OrderIndex() { rehash(16); }

    // Ensure capacity for at least n keys without further allocation.
    void reserve(std::size_t n) {
        std::size_t cap = 16;
        while (cap * 7 < n * 10) cap <<= 1;  // keep load factor <= 0.7
        if (cap > slots_.size()) rehash(cap);
    }

    std::size_t size() const { return size_; }

    bool contains(std::uint64_t key) const { return find(key) != NPOS; }

    std::uint32_t find(std::uint64_t key) const {
        const std::size_t m = slots_.size() - 1;
        std::size_t i = mix(key) & m;
        while (slots_[i].used) {
            if (slots_[i].key == key) return slots_[i].val;
            i = (i + 1) & m;
        }
        return NPOS;
    }

    // Insert or overwrite (mirrors the reference dict assignment semantics).
    void upsert(std::uint64_t key, std::uint32_t val) {
        if ((size_ + 1) * 10 > slots_.size() * 7) rehash(slots_.size() * 2);
        const std::size_t m = slots_.size() - 1;
        std::size_t i = mix(key) & m;
        while (slots_[i].used) {
            if (slots_[i].key == key) {
                slots_[i].val = val;
                return;
            }
            i = (i + 1) & m;
        }
        slots_[i] = Slot{key, val, true};
        ++size_;
    }

    // Remove a key; returns false if absent. Backward-shift deletion keeps
    // probe chains intact without tombstones.
    bool erase(std::uint64_t key) {
        const std::size_t m = slots_.size() - 1;
        std::size_t i = mix(key) & m;
        while (slots_[i].used) {
            if (slots_[i].key == key) {
                shift_delete(i);
                --size_;
                return true;
            }
            i = (i + 1) & m;
        }
        return false;
    }

    void clear() {
        for (auto& s : slots_) s.used = false;
        size_ = 0;
    }

private:
    struct Slot {
        std::uint64_t key = 0;
        std::uint32_t val = 0;
        bool used = false;
    };

    std::vector<Slot> slots_;
    std::size_t size_ = 0;

    static std::size_t mix(std::uint64_t k) {
        k ^= k >> 33;
        k *= 0xFF51AFD7ED558CCDULL;
        k ^= k >> 33;
        k *= 0xC4CEB9FE1A85EC53ULL;
        k ^= k >> 33;
        return static_cast<std::size_t>(k);
    }

    void shift_delete(std::size_t i) {
        const std::size_t m = slots_.size() - 1;
        std::size_t j = i;
        for (;;) {
            slots_[i].used = false;
            for (;;) {
                j = (j + 1) & m;
                if (!slots_[j].used) return;
                std::size_t k = mix(slots_[j].key) & m;
                // If the home slot k lies cyclically in (i, j], slot j must
                // stay put; keep scanning. Otherwise move j back to i.
                bool stays = (i <= j) ? (i < k && k <= j) : (i < k || k <= j);
                if (!stays) break;
            }
            slots_[i] = slots_[j];
            i = j;
        }
    }

    void rehash(std::size_t cap) {
        std::vector<Slot> old = std::move(slots_);
        slots_.assign(cap, Slot{});
        size_ = 0;
        for (const auto& s : old) {
            if (s.used) upsert(s.key, s.val);
        }
    }
};

}  // namespace iap
