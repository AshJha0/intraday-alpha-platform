// SHA-256 (FIPS 180-4), header-only, no dependencies.
//
// Production use: content hashes of canonical JSON (iap::contracts), the
// decision-trace digest, trace ids and the data_version of a replayed event
// stream. The same implementation backs the codec golden tests
// (tests/golden/expected_codec_sha256.json). Streaming API (`update` any
// number of times, `hexdigest` finalizes a copy so the object can keep
// accumulating) plus one-shot helpers. Constants and the round primitives
// are constexpr; the compression itself is plain integer arithmetic on a
// 64-byte block — no allocation anywhere.

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>
#include <string_view>
#include <vector>

namespace iap {

class Sha256 {
public:
    Sha256() { reset(); }

    void reset() {
        state_ = kInit;
        total_ = 0;
        buflen_ = 0;
    }

    void update(const void* data, std::size_t len) {
        const auto* p = static_cast<const std::uint8_t*>(data);
        total_ += len;
        while (len > 0) {
            std::size_t take = 64 - buflen_;
            if (take > len) take = len;
            std::memcpy(buf_ + buflen_, p, take);
            buflen_ += take;
            p += take;
            len -= take;
            if (buflen_ == 64) {
                compress(buf_);
                buflen_ = 0;
            }
        }
    }

    void update(std::string_view s) { update(s.data(), s.size()); }

    // Hex digest (finalizes a copy; the object itself is left untouched).
    std::string hexdigest() const {
        Sha256 copy = *this;
        const std::uint64_t bits = copy.total_ * 8;
        const std::uint8_t pad = 0x80;
        copy.update(&pad, 1);
        const std::uint8_t zero = 0;
        while (copy.buflen_ != 56) copy.update(&zero, 1);
        std::uint8_t lenb[8];
        for (int i = 0; i < 8; ++i) {
            lenb[i] = static_cast<std::uint8_t>(bits >> (56 - 8 * i));
        }
        // Bypass total_ accounting for the length block:
        std::memcpy(copy.buf_ + copy.buflen_, lenb, 8);
        copy.compress(copy.buf_);

        constexpr char hex[] = "0123456789abcdef";
        std::string out;
        out.reserve(64);
        for (std::uint32_t w : copy.state_) {
            for (int shift = 28; shift >= 0; shift -= 4) {
                out.push_back(hex[(w >> shift) & 0xF]);
            }
        }
        return out;
    }

    static std::string hash(const void* data, std::size_t len) {
        Sha256 h;
        h.update(data, len);
        return h.hexdigest();
    }
    static std::string hash(const std::vector<std::uint8_t>& data) {
        return hash(data.data(), data.size());
    }
    static std::string hash(std::string_view data) {
        return hash(data.data(), data.size());
    }

private:
    static constexpr std::uint32_t rotr(std::uint32_t x, int n) {
        return (x >> n) | (x << (32 - n));
    }

    static constexpr std::array<std::uint32_t, 8> kInit = {
        0x6A09E667u, 0xBB67AE85u, 0x3C6EF372u, 0xA54FF53Au,
        0x510E527Fu, 0x9B05688Cu, 0x1F83D9ABu, 0x5BE0CD19u};

    static constexpr std::array<std::uint32_t, 64> kRound = {
        0x428A2F98u, 0x71374491u, 0xB5C0FBCFu, 0xE9B5DBA5u, 0x3956C25Bu,
        0x59F111F1u, 0x923F82A4u, 0xAB1C5ED5u, 0xD807AA98u, 0x12835B01u,
        0x243185BEu, 0x550C7DC3u, 0x72BE5D74u, 0x80DEB1FEu, 0x9BDC06A7u,
        0xC19BF174u, 0xE49B69C1u, 0xEFBE4786u, 0x0FC19DC6u, 0x240CA1CCu,
        0x2DE92C6Fu, 0x4A7484AAu, 0x5CB0A9DCu, 0x76F988DAu, 0x983E5152u,
        0xA831C66Du, 0xB00327C8u, 0xBF597FC7u, 0xC6E00BF3u, 0xD5A79147u,
        0x06CA6351u, 0x14292967u, 0x27B70A85u, 0x2E1B2138u, 0x4D2C6DFCu,
        0x53380D13u, 0x650A7354u, 0x766A0ABBu, 0x81C2C92Eu, 0x92722C85u,
        0xA2BFE8A1u, 0xA81A664Bu, 0xC24B8B70u, 0xC76C51A3u, 0xD192E819u,
        0xD6990624u, 0xF40E3585u, 0x106AA070u, 0x19A4C116u, 0x1E376C08u,
        0x2748774Cu, 0x34B0BCB5u, 0x391C0CB3u, 0x4ED8AA4Au, 0x5B9CCA4Fu,
        0x682E6FF3u, 0x748F82EEu, 0x78A5636Fu, 0x84C87814u, 0x8CC70208u,
        0x90BEFFFAu, 0xA4506CEBu, 0xBEF9A3F7u, 0xC67178F2u};

    void compress(const std::uint8_t* block) {
        std::uint32_t w[64];
        for (int i = 0; i < 16; ++i) {
            w[i] = static_cast<std::uint32_t>(block[4 * i]) << 24 |
                   static_cast<std::uint32_t>(block[4 * i + 1]) << 16 |
                   static_cast<std::uint32_t>(block[4 * i + 2]) << 8 |
                   static_cast<std::uint32_t>(block[4 * i + 3]);
        }
        for (int i = 16; i < 64; ++i) {
            const std::uint32_t s0 =
                rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
            const std::uint32_t s1 =
                rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16] + s0 + w[i - 7] + s1;
        }
        std::uint32_t a = state_[0], b = state_[1], c = state_[2],
                      d = state_[3], e = state_[4], f = state_[5],
                      g = state_[6], h = state_[7];
        for (int i = 0; i < 64; ++i) {
            const std::uint32_t s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
            const std::uint32_t ch = (e & f) ^ (~e & g);
            const std::uint32_t t1 = h + s1 + ch + kRound[i] + w[i];
            const std::uint32_t s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
            const std::uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
            const std::uint32_t t2 = s0 + maj;
            h = g; g = f; f = e; e = d + t1;
            d = c; c = b; b = a; a = t1 + t2;
        }
        state_[0] += a; state_[1] += b; state_[2] += c; state_[3] += d;
        state_[4] += e; state_[5] += f; state_[6] += g; state_[7] += h;
    }

    std::array<std::uint32_t, 8> state_;
    std::uint64_t total_;
    std::uint8_t buf_[64];
    std::size_t buflen_;
};

// Lowercase hex SHA-256 of a byte string.
inline std::string sha256_hex(std::string_view data) {
    return Sha256::hash(data);
}

}  // namespace iap
