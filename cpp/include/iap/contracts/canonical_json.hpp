// Canonical JSON — the byte-level cross-language contract behind content
// hashes, config versions, trace ids and the decision-trace digest
// (python: iap.contracts.versions.canonical_json; golden:
// tests/golden/expected_canonical_json.json).
//
// A Value is a small JSON tree: null | bool | int64 | uint64 | double |
// string | array | object. The writer produces EXACTLY Python
//   json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
//              allow_nan=False)
//
// Pinned rules (the golden file's `rules` block):
//  - keys sorted by Unicode code point of the raw key, recursively. Object
//    keys live in a std::map<std::string, Value> compared bytewise on their
//    UTF-8 encoding: UTF-8 is order-preserving (a shorter prefix sorts first
//    and every continuation byte of a higher code point is >= the byte at the
//    same position of a lower one), so bytewise order of valid UTF-8 IS code
//    point order — the map order is the canonical order without a decode.
//  - separators "," and ":", no whitespace; literals null/true/false.
//  - strings: ASCII only on the wire. `"` -> \", `\` -> \\, \n \r \t \b \f
//    short escapes, every other control character (< 0x20) and every code
//    point >= 0x7f as \uXXXX (lowercase hex; code points above U+FFFF as a
//    UTF-16 surrogate pair). `/` is NOT escaped.
//  - integers: exact decimal in the i64 / u64 domain. int64 and uint64 are
//    ONE number line: a non-negative value that fits in int64 is stored as
//    Int, so equality never depends on the constructor's signedness and
//    18446744073709551615 (u64 max) survives parse -> write byte for byte.
//  - doubles: Python float.__repr__ — the shortest round-trip digits (from
//    std::to_chars, chars_format::scientific, which yields digits d.ddd and
//    a decimal exponent e), laid out positionally when -4 <= e < 16
//    (integral values keep ".0", -0.0 stays "-0.0") and as <mantissa>e<sign>
//    <at least two exponent digits> otherwise ("1e-05", "1e+16",
//    "1.2345678901234568e+17"). NaN / +-Inf throw std::invalid_argument.
//  - parse() is the strict inverse: `1` stays an integer, `1.0` stays a
//    double, integers beyond the u64 / below the i64 domain are rejected,
//    \uXXXX escapes (with surrogate pairs) decode to UTF-8, and
//    write(parse(canonical)) == canonical for every golden document.
//
// Everything here is config-plane / trace-plane code: it allocates (a tree
// is a tree). It is never called from the book / feature hot loops —
// traces are serialised AFTER a decision, outside the event loop.

#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <string_view>
#include <variant>
#include <vector>

namespace iap {
namespace contracts {

class Value;
using Array = std::vector<Value>;
using Object = std::map<std::string, Value>;

class Value {
public:
    enum class Kind { Null, Bool, Int, UInt, Double, String, Array, Object };

    Value() = default;
    Value(std::nullptr_t) {}
    Value(bool b) : v_(b) {}
    Value(int i) : v_(static_cast<std::int64_t>(i)) {}
    Value(long i) : v_(static_cast<std::int64_t>(i)) {}
    Value(long long i) : v_(static_cast<std::int64_t>(i)) {}
    Value(unsigned u) : Value(static_cast<unsigned long long>(u)) {}
    Value(unsigned long u) : Value(static_cast<unsigned long long>(u)) {}
    Value(unsigned long long u);  // Int when it fits, UInt above INT64_MAX
    Value(double d) : v_(d) {}
    Value(const char* s) : v_(std::string(s)) {}
    Value(std::string s) : v_(std::move(s)) {}
    Value(std::string_view s) : v_(std::string(s)) {}
    Value(Array a) : v_(std::move(a)) {}
    Value(Object o) : v_(std::move(o)) {}

    Kind kind() const { return static_cast<Kind>(v_.index()); }
    bool is_null() const { return kind() == Kind::Null; }
    bool is_bool() const { return kind() == Kind::Bool; }
    // Int or UInt (one number line, see header).
    bool is_integer() const {
        return kind() == Kind::Int || kind() == Kind::UInt;
    }
    bool is_double() const { return kind() == Kind::Double; }
    bool is_string() const { return kind() == Kind::String; }
    bool is_array() const { return kind() == Kind::Array; }
    bool is_object() const { return kind() == Kind::Object; }

    // Typed access; each throws std::invalid_argument on a kind mismatch
    // (message names the expected kind and the actual one).
    bool as_bool() const;
    std::int64_t as_int64() const;    // integers in the int64 domain
    std::uint64_t as_uint64() const;  // non-negative integers
    // Int, UInt or Double as a double (an integer converts exactly when it
    // fits 53 bits; this mirrors Python accepting an int for a float field).
    double as_number() const;
    const std::string& as_string() const;
    const Array& as_array() const;
    const Object& as_object() const;
    Array& as_array();
    Object& as_object();

    // Object member access; throws when not an object / key missing.
    const Value& at(const std::string& key) const;
    bool has(const std::string& key) const;
    // Array element access; throws when not an array / out of range.
    const Value& at(std::size_t index) const;

    // Structural equality (Int 5 == UInt 5; Double 5.0 != Int 5, as in
    // JSON round trips: the wire text differs).
    friend bool operator==(const Value& a, const Value& b);
    friend bool operator!=(const Value& a, const Value& b) { return !(a == b); }

private:
    std::variant<std::monostate, bool, std::int64_t, std::uint64_t, double,
                 std::string, Array, Object>
        v_;
};

// Append the canonical text of `v` to `out` (the preallocation-conscious
// entry point: callers keep one std::string, reserve() it once and clear()
// between documents). Throws std::invalid_argument on NaN / Inf.
void write_canonical(const Value& v, std::string& out);

// Canonical text of `v` (= Python canonical_json).
std::string canonical_json(const Value& v);

// Lowercase hex SHA-256 of canonical_json(v) (= Python content_hash).
std::string content_hash(const Value& v);

// Lowercase hex SHA-256 of raw bytes (re-exported from iap/util/sha256.hpp
// so contract code has one spelling).
std::string sha256_hex(std::string_view data);

// First 32 hex chars of sha256("<session>|<instrument>|<event_ts>|<seq>")
// (python: iap.contracts.ids.make_trace_id). The session id must match
// ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ (no '|'); throws otherwise.
std::string make_trace_id(std::string_view session_id,
                          std::uint32_t instrument_id, std::int64_t event_ts,
                          std::uint64_t sequence);

// True for the generic id alphabet above / a 64-char lowercase sha256 hex /
// a 32-char lowercase trace id.
bool is_generic_id(std::string_view s);
bool is_sha256_hex(std::string_view s);
bool is_trace_id(std::string_view s);

// Python float.__repr__ of a finite double (the canonical float layout);
// throws std::invalid_argument on NaN / Inf. Appends to `out`.
void write_float_repr(double d, std::string& out);
std::string float_repr(double d);

// Append the canonical (ensure_ascii) JSON string literal of a UTF-8 string,
// quotes included. Throws std::invalid_argument on malformed UTF-8.
void write_json_string(std::string_view utf8, std::string& out);

// Strict JSON parser (RFC 8259 text, any whitespace/layout) producing the
// value tree described above. Throws std::invalid_argument with the byte
// offset on malformed input, duplicate keys, integers outside the i64/u64
// domain, non-finite numbers and trailing content.
Value parse(std::string_view text);

}  // namespace contracts
}  // namespace iap
