// Canonical JSON writer / strict parser (rules pinned in canonical_json.hpp).

#include "iap/contracts/canonical_json.hpp"

#include <charconv>
#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <system_error>

#include "iap/util/sha256.hpp"

namespace iap {
namespace contracts {

namespace {

const char* kind_name(Value::Kind k) {
    switch (k) {
        case Value::Kind::Null: return "null";
        case Value::Kind::Bool: return "bool";
        case Value::Kind::Int: return "integer";
        case Value::Kind::UInt: return "integer";
        case Value::Kind::Double: return "double";
        case Value::Kind::String: return "string";
        case Value::Kind::Array: return "array";
        case Value::Kind::Object: return "object";
    }
    return "?";
}

[[noreturn]] void kind_mismatch(const char* wanted, Value::Kind got) {
    throw std::invalid_argument(std::string("canonical_json: expected ") +
                                wanted + ", got " + kind_name(got));
}

constexpr char kHex[] = "0123456789abcdef";

void append_u16_escape(std::uint32_t cu, std::string& out) {
    out.push_back('\\');
    out.push_back('u');
    out.push_back(kHex[(cu >> 12) & 0xF]);
    out.push_back(kHex[(cu >> 8) & 0xF]);
    out.push_back(kHex[(cu >> 4) & 0xF]);
    out.push_back(kHex[cu & 0xF]);
}

void append_u64(std::uint64_t u, std::string& out) {
    char buf[24];
    auto r = std::to_chars(buf, buf + sizeof(buf), u);
    out.append(buf, static_cast<std::size_t>(r.ptr - buf));
}

void append_i64(std::int64_t i, std::string& out) {
    char buf[24];
    auto r = std::to_chars(buf, buf + sizeof(buf), i);
    out.append(buf, static_cast<std::size_t>(r.ptr - buf));
}

// Decode one UTF-8 sequence starting at text[i]; advances i. Surrogate code
// points encoded as three bytes are accepted (they re-emit as a lone
// \uD8xx, exactly what Python does for a str holding a lone surrogate).
std::uint32_t decode_utf8(std::string_view text, std::size_t& i) {
    const auto b0 = static_cast<unsigned char>(text[i]);
    std::size_t n = 0;
    std::uint32_t cp = 0;
    if (b0 < 0x80) {
        ++i;
        return b0;
    } else if ((b0 & 0xE0) == 0xC0) {
        n = 1;
        cp = b0 & 0x1F;
    } else if ((b0 & 0xF0) == 0xE0) {
        n = 2;
        cp = b0 & 0x0F;
    } else if ((b0 & 0xF8) == 0xF0) {
        n = 3;
        cp = b0 & 0x07;
    } else {
        throw std::invalid_argument(
            "canonical_json: malformed UTF-8 lead byte at offset " +
            std::to_string(i));
    }
    for (std::size_t k = 1; k <= n; ++k) {
        if (i + k >= text.size()) {
            throw std::invalid_argument(
                "canonical_json: truncated UTF-8 sequence at offset " +
                std::to_string(i));
        }
        const auto b = static_cast<unsigned char>(text[i + k]);
        if ((b & 0xC0) != 0x80) {
            throw std::invalid_argument(
                "canonical_json: malformed UTF-8 continuation at offset " +
                std::to_string(i + k));
        }
        cp = (cp << 6) | (b & 0x3F);
    }
    static constexpr std::uint32_t kMin[4] = {0, 0x80, 0x800, 0x10000};
    if (cp < kMin[n] || cp > 0x10FFFF) {
        throw std::invalid_argument(
            "canonical_json: overlong or out-of-range UTF-8 at offset " +
            std::to_string(i));
    }
    i += n + 1;
    return cp;
}

void append_utf8(std::uint32_t cp, std::string& out) {
    if (cp < 0x80) {
        out.push_back(static_cast<char>(cp));
    } else if (cp < 0x800) {
        out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp < 0x10000) {
        out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else {
        out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
}

void write_value(const Value& v, std::string& out) {
    switch (v.kind()) {
        case Value::Kind::Null: out.append("null"); return;
        case Value::Kind::Bool: out.append(v.as_bool() ? "true" : "false"); return;
        case Value::Kind::Int: append_i64(v.as_int64(), out); return;
        case Value::Kind::UInt: append_u64(v.as_uint64(), out); return;
        case Value::Kind::Double: write_float_repr(v.as_number(), out); return;
        case Value::Kind::String: write_json_string(v.as_string(), out); return;
        case Value::Kind::Array: {
            out.push_back('[');
            bool first = true;
            for (const auto& e : v.as_array()) {
                if (!first) out.push_back(',');
                first = false;
                write_value(e, out);
            }
            out.push_back(']');
            return;
        }
        case Value::Kind::Object: {
            out.push_back('{');
            bool first = true;
            for (const auto& [k, e] : v.as_object()) {
                if (!first) out.push_back(',');
                first = false;
                write_json_string(k, out);
                out.push_back(':');
                write_value(e, out);
            }
            out.push_back('}');
            return;
        }
    }
}

// ----------------------------------------------------------------- parser
class Parser {
public:
    explicit Parser(std::string_view text) : text_(text) {}

    Value run() {
        Value v = value(0);
        skip_ws();
        if (pos_ != text_.size()) fail("trailing content");
        return v;
    }

private:
    static constexpr int kMaxDepth = 512;

    std::string_view text_;
    std::size_t pos_ = 0;

    [[noreturn]] void fail(const std::string& msg) const {
        throw std::invalid_argument("canonical_json: parse error at offset " +
                                    std::to_string(pos_) + ": " + msg);
    }
    char peek() const {
        if (pos_ >= text_.size()) fail("unexpected end of input");
        return text_[pos_];
    }
    char take() {
        char c = peek();
        ++pos_;
        return c;
    }
    void skip_ws() {
        while (pos_ < text_.size() &&
               (text_[pos_] == ' ' || text_[pos_] == '\t' ||
                text_[pos_] == '\n' || text_[pos_] == '\r')) {
            ++pos_;
        }
    }
    void expect(char c) {
        if (take() != c) {
            --pos_;
            fail(std::string("expected '") + c + "'");
        }
    }
    bool consume(std::string_view lit) {
        if (text_.substr(pos_, lit.size()) == lit) {
            pos_ += lit.size();
            return true;
        }
        return false;
    }

    Value value(int depth) {
        if (depth > kMaxDepth) fail("nesting too deep");
        skip_ws();
        switch (peek()) {
            case '{': return object(depth);
            case '[': return array(depth);
            case '"': return Value(string());
            case 't':
                if (consume("true")) return Value(true);
                fail("bad literal");
            case 'f':
                if (consume("false")) return Value(false);
                fail("bad literal");
            case 'n':
                if (consume("null")) return Value(nullptr);
                fail("bad literal");
            default: return number();
        }
    }

    Value object(int depth) {
        Object o;
        expect('{');
        skip_ws();
        if (peek() == '}') {
            ++pos_;
            return Value(std::move(o));
        }
        for (;;) {
            skip_ws();
            if (peek() != '"') fail("object key must be a string");
            std::string key = string();
            skip_ws();
            expect(':');
            Value v = value(depth + 1);
            if (!o.emplace(std::move(key), std::move(v)).second) {
                fail("duplicate object key");
            }
            skip_ws();
            char c = take();
            if (c == '}') break;
            if (c != ',') {
                --pos_;
                fail("expected ',' or '}'");
            }
        }
        return Value(std::move(o));
    }

    Value array(int depth) {
        Array a;
        expect('[');
        skip_ws();
        if (peek() == ']') {
            ++pos_;
            return Value(std::move(a));
        }
        for (;;) {
            a.push_back(value(depth + 1));
            skip_ws();
            char c = take();
            if (c == ']') break;
            if (c != ',') {
                --pos_;
                fail("expected ',' or ']'");
            }
        }
        return Value(std::move(a));
    }

    std::uint32_t hex4() {
        std::uint32_t code = 0;
        for (int i = 0; i < 4; ++i) {
            char h = take();
            code <<= 4;
            if (h >= '0' && h <= '9') code |= static_cast<std::uint32_t>(h - '0');
            else if (h >= 'a' && h <= 'f') code |= static_cast<std::uint32_t>(h - 'a' + 10);
            else if (h >= 'A' && h <= 'F') code |= static_cast<std::uint32_t>(h - 'A' + 10);
            else fail("bad \\u escape");
        }
        return code;
    }

    std::string string() {
        expect('"');
        std::string out;
        for (;;) {
            char c = take();
            if (c == '"') break;
            const auto uc = static_cast<unsigned char>(c);
            if (uc < 0x20) fail("unescaped control character in string");
            if (c != '\\') {
                out.push_back(c);
                continue;
            }
            char e = take();
            switch (e) {
                case '"': out.push_back('"'); break;
                case '\\': out.push_back('\\'); break;
                case '/': out.push_back('/'); break;
                case 'b': out.push_back('\b'); break;
                case 'f': out.push_back('\f'); break;
                case 'n': out.push_back('\n'); break;
                case 'r': out.push_back('\r'); break;
                case 't': out.push_back('\t'); break;
                case 'u': {
                    std::uint32_t cp = hex4();
                    if (cp >= 0xD800 && cp <= 0xDBFF) {
                        // High surrogate: pair with a following \uDCxx.
                        const std::size_t save = pos_;
                        if (consume("\\u")) {
                            const std::uint32_t lo = hex4();
                            if (lo >= 0xDC00 && lo <= 0xDFFF) {
                                cp = 0x10000 + ((cp - 0xD800) << 10) +
                                     (lo - 0xDC00);
                            } else {
                                pos_ = save;  // lone high surrogate
                            }
                        }
                    }
                    append_utf8(cp, out);
                    break;
                }
                default: fail("bad escape");
            }
        }
        // Validate the raw bytes as UTF-8 so the writer never sees garbage.
        std::size_t i = 0;
        while (i < out.size()) decode_utf8(out, i);
        return out;
    }

    Value number() {
        const std::size_t start = pos_;
        bool neg = false;
        if (peek() == '-') {
            neg = true;
            ++pos_;
        }
        if (pos_ >= text_.size() || text_[pos_] < '0' || text_[pos_] > '9') {
            fail("bad number");
        }
        if (text_[pos_] == '0' && pos_ + 1 < text_.size() &&
            text_[pos_ + 1] >= '0' && text_[pos_ + 1] <= '9') {
            fail("leading zero");
        }
        std::uint64_t mag = 0;
        bool overflow = false;
        while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') {
            const auto d = static_cast<std::uint64_t>(text_[pos_] - '0');
            if (mag > (std::numeric_limits<std::uint64_t>::max() - d) / 10) {
                overflow = true;
            }
            mag = mag * 10 + d;
            ++pos_;
        }
        bool integral = true;
        if (pos_ < text_.size() && text_[pos_] == '.') {
            integral = false;
            ++pos_;
            if (pos_ >= text_.size() || text_[pos_] < '0' || text_[pos_] > '9') {
                fail("bad fraction");
            }
            while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') {
                ++pos_;
            }
        }
        if (pos_ < text_.size() && (text_[pos_] == 'e' || text_[pos_] == 'E')) {
            integral = false;
            ++pos_;
            if (pos_ < text_.size() && (text_[pos_] == '+' || text_[pos_] == '-')) {
                ++pos_;
            }
            if (pos_ >= text_.size() || text_[pos_] < '0' || text_[pos_] > '9') {
                fail("bad exponent");
            }
            while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') {
                ++pos_;
            }
        }
        if (integral) {
            if (overflow) fail("integer outside the u64 domain");
            if (neg) {
                if (mag > (static_cast<std::uint64_t>(1) << 63)) {
                    fail("integer below the i64 domain");
                }
                // 0 - mag wraps to the two's complement value (INT64_MIN ok).
                return Value(static_cast<std::int64_t>(std::uint64_t{0} - mag));
            }
            return Value(static_cast<unsigned long long>(mag));
        }
        double d = 0.0;
        const auto r = std::from_chars(text_.data() + start, text_.data() + pos_, d);
        if (r.ec != std::errc() || r.ptr != text_.data() + pos_) {
            if (r.ec == std::errc::result_out_of_range) {
                fail("floating-point literal out of range");
            }
            fail("bad number");
        }
        if (!std::isfinite(d)) fail("non-finite number");
        return Value(d);
    }
};

}  // namespace

// ------------------------------------------------------------------ Value
Value::Value(unsigned long long u) {
    if (u <= static_cast<unsigned long long>(
                 std::numeric_limits<std::int64_t>::max())) {
        v_ = static_cast<std::int64_t>(u);
    } else {
        v_ = static_cast<std::uint64_t>(u);
    }
}

bool Value::as_bool() const {
    if (kind() != Kind::Bool) kind_mismatch("bool", kind());
    return std::get<bool>(v_);
}

std::int64_t Value::as_int64() const {
    if (kind() == Kind::Int) return std::get<std::int64_t>(v_);
    if (kind() == Kind::UInt) {
        throw std::invalid_argument(
            "canonical_json: integer " +
            std::to_string(std::get<std::uint64_t>(v_)) +
            " exceeds the int64 domain");
    }
    kind_mismatch("integer", kind());
}

std::uint64_t Value::as_uint64() const {
    if (kind() == Kind::UInt) return std::get<std::uint64_t>(v_);
    if (kind() == Kind::Int) {
        const auto i = std::get<std::int64_t>(v_);
        if (i < 0) {
            throw std::invalid_argument("canonical_json: integer " +
                                        std::to_string(i) +
                                        " is negative (uint64 wanted)");
        }
        return static_cast<std::uint64_t>(i);
    }
    kind_mismatch("integer", kind());
}

double Value::as_number() const {
    switch (kind()) {
        case Kind::Double: return std::get<double>(v_);
        case Kind::Int: return static_cast<double>(std::get<std::int64_t>(v_));
        case Kind::UInt: return static_cast<double>(std::get<std::uint64_t>(v_));
        default: kind_mismatch("number", kind());
    }
}

const std::string& Value::as_string() const {
    if (kind() != Kind::String) kind_mismatch("string", kind());
    return std::get<std::string>(v_);
}

const Array& Value::as_array() const {
    if (kind() != Kind::Array) kind_mismatch("array", kind());
    return std::get<Array>(v_);
}

Array& Value::as_array() {
    if (kind() != Kind::Array) kind_mismatch("array", kind());
    return std::get<Array>(v_);
}

const Object& Value::as_object() const {
    if (kind() != Kind::Object) kind_mismatch("object", kind());
    return std::get<Object>(v_);
}

Object& Value::as_object() {
    if (kind() != Kind::Object) kind_mismatch("object", kind());
    return std::get<Object>(v_);
}

const Value& Value::at(const std::string& key) const {
    const auto& o = as_object();
    auto it = o.find(key);
    if (it == o.end()) {
        throw std::invalid_argument("canonical_json: missing key '" + key + "'");
    }
    return it->second;
}

bool Value::has(const std::string& key) const {
    return is_object() && as_object().count(key) != 0;
}

const Value& Value::at(std::size_t index) const {
    const auto& a = as_array();
    if (index >= a.size()) {
        throw std::invalid_argument("canonical_json: array index " +
                                    std::to_string(index) + " out of range");
    }
    return a[index];
}

bool operator==(const Value& a, const Value& b) {
    // Int/UInt are normalised by construction (UInt only above INT64_MAX),
    // so identical numbers always share a kind; a plain variant compare is
    // the structural comparison.
    return a.v_ == b.v_;
}

// ----------------------------------------------------------------- writer
void write_float_repr(double d, std::string& out) {
    if (!std::isfinite(d)) {
        throw std::invalid_argument(
            "canonical_json: non-finite float (NaN / Inf) is not allowed");
    }
    // Shortest round-trip digits in scientific form: [-]d[.ddd]e[+-]XX.
    char buf[40];
    const auto r =
        std::to_chars(buf, buf + sizeof(buf), d, std::chars_format::scientific);
    if (r.ec != std::errc()) {
        throw std::invalid_argument("canonical_json: float formatting failed");
    }
    const std::string_view sci(buf, static_cast<std::size_t>(r.ptr - buf));
    std::size_t p = 0;
    if (sci[p] == '-') {
        out.push_back('-');
        ++p;
    }
    char digits[24];
    std::size_t n = 0;
    while (p < sci.size() && sci[p] != 'e') {
        if (sci[p] != '.') digits[n++] = sci[p];
        ++p;
    }
    ++p;  // 'e'
    bool exp_neg = false;
    if (p < sci.size() && (sci[p] == '+' || sci[p] == '-')) {
        exp_neg = sci[p] == '-';
        ++p;
    }
    int exp10 = 0;
    const auto er = std::from_chars(sci.data() + p, sci.data() + sci.size(), exp10);
    if (er.ec != std::errc() || er.ptr != sci.data() + sci.size()) {
        throw std::invalid_argument("canonical_json: float exponent parse failed");
    }
    if (exp_neg) exp10 = -exp10;
    // Trim trailing zeros of the mantissa (to_chars never emits them for
    // shortest output, but "0e+00" style zero has a single digit anyway).
    while (n > 1 && digits[n - 1] == '0') --n;

    if (exp10 < -4 || exp10 >= 16) {
        // Exponent form: d[.ddd]e<sign><at least two digits>.
        out.push_back(digits[0]);
        if (n > 1) {
            out.push_back('.');
            out.append(digits + 1, n - 1);
        }
        out.push_back('e');
        out.push_back(exp10 < 0 ? '-' : '+');
        const int mag = exp10 < 0 ? -exp10 : exp10;
        if (mag < 10) out.push_back('0');
        append_i64(mag, out);
        return;
    }
    if (exp10 >= 0) {
        // Positional, point after exp10 + 1 digits.
        const auto int_digits = static_cast<std::size_t>(exp10) + 1;
        if (n <= int_digits) {
            out.append(digits, n);
            out.append(int_digits - n, '0');
            out.append(".0");
        } else {
            out.append(digits, int_digits);
            out.push_back('.');
            out.append(digits + int_digits, n - int_digits);
        }
        return;
    }
    // -4 <= exp10 < 0: 0.000ddd
    out.append("0.");
    out.append(static_cast<std::size_t>(-exp10 - 1), '0');
    out.append(digits, n);
}

std::string float_repr(double d) {
    std::string out;
    write_float_repr(d, out);
    return out;
}

void write_json_string(std::string_view utf8, std::string& out) {
    out.push_back('"');
    std::size_t i = 0;
    while (i < utf8.size()) {
        const auto b = static_cast<unsigned char>(utf8[i]);
        if (b < 0x80) {
            ++i;
            switch (b) {
                case '"': out.append("\\\""); continue;
                case '\\': out.append("\\\\"); continue;
                case '\n': out.append("\\n"); continue;
                case '\r': out.append("\\r"); continue;
                case '\t': out.append("\\t"); continue;
                case '\b': out.append("\\b"); continue;
                case '\f': out.append("\\f"); continue;
                default: break;
            }
            if (b < 0x20 || b == 0x7F) {
                append_u16_escape(b, out);
            } else {
                out.push_back(static_cast<char>(b));
            }
            continue;
        }
        const std::uint32_t cp = decode_utf8(utf8, i);
        if (cp >= 0x10000) {
            const std::uint32_t v = cp - 0x10000;
            append_u16_escape(0xD800 + (v >> 10), out);
            append_u16_escape(0xDC00 + (v & 0x3FF), out);
        } else {
            append_u16_escape(cp, out);
        }
    }
    out.push_back('"');
}

void write_canonical(const Value& v, std::string& out) { write_value(v, out); }

std::string canonical_json(const Value& v) {
    std::string out;
    out.reserve(256);
    write_value(v, out);
    return out;
}

std::string content_hash(const Value& v) { return sha256_hex(canonical_json(v)); }

std::string sha256_hex(std::string_view data) { return iap::sha256_hex(data); }

bool is_generic_id(std::string_view s) {
    if (s.empty() || s.size() > 128) return false;
    auto alnum = [](char c) {
        return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
               (c >= '0' && c <= '9');
    };
    if (!alnum(s[0])) return false;
    for (std::size_t i = 1; i < s.size(); ++i) {
        const char c = s[i];
        if (!alnum(c) && c != '.' && c != '_' && c != ':' && c != '-') {
            return false;
        }
    }
    return true;
}

namespace {
bool is_lower_hex(std::string_view s, std::size_t n) {
    if (s.size() != n) return false;
    for (char c : s) {
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    }
    return true;
}
}  // namespace

bool is_sha256_hex(std::string_view s) { return is_lower_hex(s, 64); }
bool is_trace_id(std::string_view s) { return is_lower_hex(s, 32); }

std::string make_trace_id(std::string_view session_id,
                          std::uint32_t instrument_id, std::int64_t event_ts,
                          std::uint64_t sequence) {
    if (!is_generic_id(session_id)) {
        throw std::invalid_argument(
            "make_trace_id: session_id '" + std::string(session_id) +
            "' is not in the id alphabet [A-Za-z0-9][A-Za-z0-9._:-]{0,127}");
    }
    std::string key;
    key.reserve(session_id.size() + 64);
    key.append(session_id);
    key.push_back('|');
    append_u64(instrument_id, key);
    key.push_back('|');
    append_i64(event_ts, key);
    key.push_back('|');
    append_u64(sequence, key);
    return iap::sha256_hex(key).substr(0, 32);
}

Value parse(std::string_view text) { return Parser(text).run(); }

}  // namespace contracts
}  // namespace iap
