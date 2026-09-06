#include "iap/marketdata/codec.hpp"

#include <array>
#include <cstring>
#include <fstream>
#include <limits>
#include <stdexcept>

namespace iap {

namespace {

// Canonical JSONL key order (== logical field order; normative).
constexpr std::array<const char*, 12> kKeys = {
    "event_id",  "instrument_id", "venue_id", "exchange_ts",
    "receive_ts", "sequence",     "event_type", "side",
    "price_ticks", "qty",         "order_id", "trade_id"};

void append_u64(std::string& out, std::uint64_t v) {
    char buf[20];
    int n = 0;
    do {
        buf[n++] = static_cast<char>('0' + v % 10);
        v /= 10;
    } while (v != 0);
    while (n > 0) out.push_back(buf[--n]);
}

void append_i64(std::string& out, std::int64_t v) {
    if (v < 0) {
        out.push_back('-');
        // Negate via unsigned to handle INT64_MIN.
        append_u64(out, ~static_cast<std::uint64_t>(v) + 1);
    } else {
        append_u64(out, static_cast<std::uint64_t>(v));
    }
}

struct Cursor {
    const char* p;
    const char* end;

    [[noreturn]] void fail(const std::string& msg) const {
        throw std::invalid_argument("malformed JSONL line: " + msg);
    }
    void skip_ws() {
        while (p != end && (*p == ' ' || *p == '\t' || *p == '\r')) ++p;
    }
    void expect(char c, const char* what) {
        skip_ws();
        if (p == end || *p != c) fail(std::string("expected '") + c + "' " + what);
        ++p;
    }
    bool done() const { return p == end; }
};

// Parsed integer token: magnitude + sign.
struct IntTok {
    std::uint64_t mag;
    bool neg;
};

IntTok parse_int(Cursor& c, const char* key) {
    c.skip_ws();
    IntTok t{0, false};
    if (c.p != c.end && *c.p == '-') {
        t.neg = true;
        ++c.p;
    }
    if (c.p == c.end || *c.p < '0' || *c.p > '9') {
        c.fail(std::string("field '") + key + "' must be an integer");
    }
    if (*c.p == '0' && c.p + 1 != c.end && c.p[1] >= '0' && c.p[1] <= '9') {
        c.fail(std::string("leading zeros in field '") + key + "'");
    }
    while (c.p != c.end && *c.p >= '0' && *c.p <= '9') {
        std::uint64_t d = static_cast<std::uint64_t>(*c.p - '0');
        if (t.mag > (std::numeric_limits<std::uint64_t>::max() - d) / 10) {
            c.fail(std::string("integer overflow in field '") + key + "'");
        }
        t.mag = t.mag * 10 + d;
        ++c.p;
    }
    if (c.p != c.end && (*c.p == '.' || *c.p == 'e' || *c.p == 'E')) {
        c.fail(std::string("field '") + key +
               "' must be an integer, got a float");
    }
    return t;
}

std::uint64_t as_unsigned(const IntTok& t, Cursor& c, const char* key,
                          std::uint64_t max) {
    if (t.neg) {  // "-0" included: a leading '-' is malformed on unsigned fields
        c.fail(std::string("field '") + key + "' must be non-negative");
    }
    if (t.mag > max) {
        c.fail(std::string("field '") + key + "' out of range");
    }
    return t.mag;
}

std::int64_t as_signed(const IntTok& t, Cursor& c, const char* key) {
    constexpr std::uint64_t kI64Max = 0x7FFFFFFFFFFFFFFFULL;
    if (t.neg) {
        if (t.mag > kI64Max + 1) {
            c.fail(std::string("field '") + key + "' out of i64 range");
        }
        return static_cast<std::int64_t>(~t.mag + 1);
    }
    if (t.mag > kI64Max) {
        c.fail(std::string("field '") + key + "' out of i64 range");
    }
    return static_cast<std::int64_t>(t.mag);
}

void store_le32(std::uint8_t* p, std::uint32_t v) {
    p[0] = static_cast<std::uint8_t>(v);
    p[1] = static_cast<std::uint8_t>(v >> 8);
    p[2] = static_cast<std::uint8_t>(v >> 16);
    p[3] = static_cast<std::uint8_t>(v >> 24);
}

void store_le64(std::uint8_t* p, std::uint64_t v) {
    for (int i = 0; i < 8; ++i) p[i] = static_cast<std::uint8_t>(v >> (8 * i));
}

struct Crc32Table {
    std::uint32_t t[256];
    Crc32Table() {
        for (std::uint32_t i = 0; i < 256; ++i) {
            std::uint32_t c = i;
            for (int k = 0; k < 8; ++k) {
                c = (c & 1u) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
            }
            t[i] = c;
        }
    }
};

const Crc32Table& crc_table() {
    static const Crc32Table table;
    return table;
}

std::uint32_t load_le32(const std::uint8_t* p) {
    return static_cast<std::uint32_t>(p[0]) |
           static_cast<std::uint32_t>(p[1]) << 8 |
           static_cast<std::uint32_t>(p[2]) << 16 |
           static_cast<std::uint32_t>(p[3]) << 24;
}

std::uint64_t load_le64(const std::uint8_t* p) {
    std::uint64_t v = 0;
    for (int i = 0; i < 8; ++i) v |= static_cast<std::uint64_t>(p[i]) << (8 * i);
    return v;
}

}  // namespace

std::uint32_t crc32(const std::uint8_t* data, std::size_t len) {
    const auto& t = crc_table().t;
    std::uint32_t c = 0xFFFFFFFFu;
    for (std::size_t i = 0; i < len; ++i) {
        c = t[(c ^ data[i]) & 0xFFu] ^ (c >> 8);
    }
    return c ^ 0xFFFFFFFFu;
}

// --------------------------------------------------------------------- JSONL

std::string encode_jsonl_line(const MarketEvent& ev) {
    std::string out;
    out.reserve(220);
    out += "{\"event_id\":";
    append_u64(out, ev.event_id);
    out += ",\"instrument_id\":";
    append_u64(out, ev.instrument_id);
    out += ",\"venue_id\":";
    append_u64(out, ev.venue_id);
    out += ",\"exchange_ts\":";
    append_i64(out, ev.exchange_ts);
    out += ",\"receive_ts\":";
    append_i64(out, ev.receive_ts);
    out += ",\"sequence\":";
    append_u64(out, ev.sequence);
    out += ",\"event_type\":";
    append_u64(out, ev.event_type);
    out += ",\"side\":";
    append_u64(out, ev.side);
    out += ",\"price_ticks\":";
    append_i64(out, ev.price_ticks);
    out += ",\"qty\":";
    append_i64(out, ev.qty);
    out += ",\"order_id\":";
    append_u64(out, ev.order_id);
    out += ",\"trade_id\":";
    append_u64(out, ev.trade_id);
    out += '}';
    return out;
}

MarketEvent decode_jsonl_line(std::string_view line) {
    Cursor c{line.data(), line.data() + line.size()};
    c.expect('{', "at start of object");

    std::array<IntTok, 12> toks{};
    for (std::size_t i = 0; i < kKeys.size(); ++i) {
        c.skip_ws();
        if (c.p == c.end || *c.p != '"') {
            c.fail(std::string("expected key \"") + kKeys[i] +
                   "\" (keys must be exact and in canonical order)");
        }
        ++c.p;
        const char* key = kKeys[i];
        std::size_t klen = std::strlen(key);
        if (static_cast<std::size_t>(c.end - c.p) < klen + 1 ||
            std::memcmp(c.p, key, klen) != 0 || c.p[klen] != '"') {
            c.fail(std::string("expected key \"") + key +
                   "\" (keys must be exact and in canonical order)");
        }
        c.p += klen + 1;
        c.expect(':', "after key");
        toks[i] = parse_int(c, key);
        if (i + 1 < kKeys.size()) {
            c.expect(',', "between fields");
        } else {
            c.expect('}', "at end of object (extra keys are rejected)");
        }
    }
    c.skip_ws();
    if (!c.done()) c.fail("trailing characters after object");

    constexpr std::uint64_t kU64Max = 0xFFFFFFFFFFFFFFFFULL;
    MarketEvent ev{};
    ev.event_id = as_unsigned(toks[0], c, "event_id", kU64Max);
    ev.instrument_id = static_cast<std::uint32_t>(
        as_unsigned(toks[1], c, "instrument_id", 0xFFFFFFFFULL));
    ev.venue_id = static_cast<std::uint16_t>(
        as_unsigned(toks[2], c, "venue_id", 0xFFFFULL));
    ev.exchange_ts = as_signed(toks[3], c, "exchange_ts");
    ev.receive_ts = as_signed(toks[4], c, "receive_ts");
    ev.sequence = as_unsigned(toks[5], c, "sequence", kU64Max);
    ev.event_type = static_cast<std::uint8_t>(
        as_unsigned(toks[6], c, "event_type", 0xFFULL));
    ev.side =
        static_cast<std::uint8_t>(as_unsigned(toks[7], c, "side", 0xFFULL));
    ev.price_ticks = as_signed(toks[8], c, "price_ticks");
    ev.qty = as_signed(toks[9], c, "qty");
    ev.order_id = as_unsigned(toks[10], c, "order_id", kU64Max);
    ev.trade_id = as_unsigned(toks[11], c, "trade_id", kU64Max);
    return ev;
}

std::string encode_jsonl(const std::vector<MarketEvent>& events) {
    std::string out;
    out.reserve(events.size() * 200);
    for (const auto& ev : events) {
        out += encode_jsonl_line(ev);
        out += '\n';
    }
    return out;
}

std::vector<MarketEvent> decode_jsonl(std::string_view data) {
    std::vector<MarketEvent> events;
    std::size_t start = 0;
    while (start <= data.size()) {
        std::size_t nl = data.find('\n', start);
        std::string_view line = (nl == std::string_view::npos)
                                    ? data.substr(start)
                                    : data.substr(start, nl - start);
        // Strip surrounding whitespace; skip blank lines (as the reference).
        std::size_t b = 0, e = line.size();
        while (b < e && (line[b] == ' ' || line[b] == '\t' || line[b] == '\r'))
            ++b;
        while (e > b &&
               (line[e - 1] == ' ' || line[e - 1] == '\t' || line[e - 1] == '\r'))
            --e;
        if (e > b) events.push_back(decode_jsonl_line(line.substr(b, e - b)));
        if (nl == std::string_view::npos) break;
        start = nl + 1;
    }
    return events;
}

std::size_t write_jsonl(const std::string& path,
                        const std::vector<MarketEvent>& events) {
    std::ofstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("cannot open for write: " + path);
    std::string data = encode_jsonl(events);
    f.write(data.data(), static_cast<std::streamsize>(data.size()));
    if (!f) throw std::runtime_error("write failed: " + path);
    return events.size();
}

std::vector<MarketEvent> read_jsonl(const std::string& path) {
    std::vector<std::uint8_t> raw = read_file_bytes(path);
    return decode_jsonl(std::string_view(
        reinterpret_cast<const char*>(raw.data()), raw.size()));
}

// ---------------------------------------------------------------------- IAP1

std::vector<std::uint8_t> encode_iap1(const std::vector<MarketEvent>& events) {
    const std::size_t body_size =
        IAP1_HEADER_SIZE + IAP1_RECORD_SIZE * events.size();
    std::vector<std::uint8_t> out(body_size + IAP1_TRAILER_SIZE);
    store_le32(out.data(), IAP1_MAGIC);
    store_le32(out.data() + 4, IAP1_VERSION);
    store_le64(out.data() + 8, events.size());
    std::size_t off = IAP1_HEADER_SIZE;
    for (const auto& ev : events) {
        // MarketEvent is layout-asserted to match the 72-byte LE record on
        // this (little-endian) target; a straight copy is byte-exact.
        std::memcpy(out.data() + off, &ev, IAP1_RECORD_SIZE);
        off += IAP1_RECORD_SIZE;
    }
    // Integrity trailer: crc32 | reserved 0 | count echo (FORMAT.md section 2).
    store_le32(out.data() + body_size, crc32(out.data(), body_size));
    store_le32(out.data() + body_size + 4, 0);
    store_le64(out.data() + body_size + 8, events.size());
    return out;
}

Iap1Decoded decode_iap1_ex(const std::uint8_t* data, std::size_t len) {
    if (len < IAP1_HEADER_SIZE) {
        throw std::invalid_argument("IAP1 file truncated: " +
                                    std::to_string(len) +
                                    " bytes < 16-byte header");
    }
    std::uint32_t magic = load_le32(data);
    std::uint32_t version = load_le32(data + 4);
    std::uint64_t count = load_le64(data + 8);
    if (magic != IAP1_MAGIC) {
        throw std::invalid_argument("bad IAP1 magic: " + std::to_string(magic) +
                                    " (expected " + std::to_string(IAP1_MAGIC) +
                                    ")");
    }
    if (version != IAP1_VERSION && version != IAP1_VERSION_LEGACY) {
        throw std::invalid_argument("unsupported IAP1 version: " +
                                    std::to_string(version));
    }
    const bool with_trailer = version == IAP1_VERSION;
    if (count > (len / IAP1_RECORD_SIZE) + 1) {
        throw std::invalid_argument("IAP1 size mismatch: header count=" +
                                    std::to_string(count) +
                                    " impossible for " + std::to_string(len) +
                                    " bytes");
    }
    const std::size_t body_size = IAP1_HEADER_SIZE + IAP1_RECORD_SIZE * count;
    const std::size_t expected =
        body_size + (with_trailer ? IAP1_TRAILER_SIZE : 0);
    if (len != expected) {
        throw std::invalid_argument(
            "IAP1 size mismatch: " + std::to_string(len) +
            " bytes, header count=" + std::to_string(count) + " (version " +
            std::to_string(version) + ") implies " + std::to_string(expected));
    }
    if (with_trailer) {
        const std::uint32_t crc = load_le32(data + body_size);
        const std::uint32_t reserved = load_le32(data + body_size + 4);
        const std::uint64_t echo = load_le64(data + body_size + 8);
        if (reserved != 0) {
            throw std::invalid_argument(
                "IAP1 trailer reserved field must be 0: " +
                std::to_string(reserved));
        }
        if (echo != count) {
            throw std::invalid_argument("IAP1 trailer count echo " +
                                        std::to_string(echo) +
                                        " != header count " +
                                        std::to_string(count));
        }
        const std::uint32_t actual = crc32(data, body_size);
        if (actual != crc) {
            throw std::invalid_argument(
                "IAP1 CRC-32 mismatch: trailer " + std::to_string(crc) +
                ", computed " + std::to_string(actual));
        }
    }
    Iap1Decoded out;
    out.version = version;
    out.integrity_checked = with_trailer;
    out.events.resize(count);
    std::size_t off = IAP1_HEADER_SIZE;
    for (std::uint64_t i = 0; i < count; ++i) {
        std::memcpy(&out.events[i], data + off, IAP1_RECORD_SIZE);
        off += IAP1_RECORD_SIZE;
    }
    return out;
}

std::vector<MarketEvent> decode_iap1(const std::uint8_t* data,
                                     std::size_t len) {
    return decode_iap1_ex(data, len).events;
}

std::vector<MarketEvent> decode_iap1(const std::vector<std::uint8_t>& data) {
    return decode_iap1(data.data(), data.size());
}

std::size_t write_iap1(const std::string& path,
                       const std::vector<MarketEvent>& events) {
    std::ofstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("cannot open for write: " + path);
    std::vector<std::uint8_t> data = encode_iap1(events);
    f.write(reinterpret_cast<const char*>(data.data()),
            static_cast<std::streamsize>(data.size()));
    if (!f) throw std::runtime_error("write failed: " + path);
    return events.size();
}

std::vector<MarketEvent> read_iap1(const std::string& path) {
    return decode_iap1(read_file_bytes(path));
}

std::vector<std::uint8_t> read_file_bytes(const std::string& path) {
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    if (!f) throw std::runtime_error("cannot open for read: " + path);
    std::streamsize size = f.tellg();
    f.seekg(0, std::ios::beg);
    std::vector<std::uint8_t> data(static_cast<std::size_t>(size));
    if (size > 0 &&
        !f.read(reinterpret_cast<char*>(data.data()), size)) {
        throw std::runtime_error("read failed: " + path);
    }
    return data;
}

}  // namespace iap
