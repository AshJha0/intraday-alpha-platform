// Golden canonical-JSON contract (tests/golden/expected_canonical_json.json):
// every case of the file — float layout by IEEE bit pattern, ensure_ascii
// string escaping from code points, whole documents (canonical text +
// sha256 + parse -> re-serialise byte identity), the pinned trace id and the
// reject cases — must match the Python reference byte for byte.

#include <gtest/gtest.h>

#include <cmath>
#include <cstring>
#include <limits>
#include <string>

#include "golden_util.hpp"
#include "iap/contracts/canonical_json.hpp"

namespace {

namespace cj = iap::contracts;

cj::Value load_canonical_golden() {
    return cj::parse(iap_test::read_text_file(
        iap_test::golden_path("expected_canonical_json.json")));
}

double double_from_bits_hex(const std::string& hex) {
    if (hex.size() != 16) throw std::runtime_error("bits_hex must be 16 chars");
    const std::uint64_t bits = std::stoull(hex, nullptr, 16);
    double d;
    std::memcpy(&d, &bits, sizeof d);
    return d;
}

std::string utf8_from_codepoints(const cj::Value& cps) {
    std::string s;
    for (const auto& cp : cps.as_array()) {
        const auto c = static_cast<std::uint32_t>(cp.as_uint64());
        if (c < 0x80) {
            s.push_back(static_cast<char>(c));
        } else if (c < 0x800) {
            s.push_back(static_cast<char>(0xC0 | (c >> 6)));
            s.push_back(static_cast<char>(0x80 | (c & 0x3F)));
        } else if (c < 0x10000) {
            s.push_back(static_cast<char>(0xE0 | (c >> 12)));
            s.push_back(static_cast<char>(0x80 | ((c >> 6) & 0x3F)));
            s.push_back(static_cast<char>(0x80 | (c & 0x3F)));
        } else {
            s.push_back(static_cast<char>(0xF0 | (c >> 18)));
            s.push_back(static_cast<char>(0x80 | ((c >> 12) & 0x3F)));
            s.push_back(static_cast<char>(0x80 | ((c >> 6) & 0x3F)));
            s.push_back(static_cast<char>(0x80 | (c & 0x3F)));
        }
    }
    return s;
}

TEST(CanonicalJsonGolden, FileVersionAndRules) {
    const auto g = load_canonical_golden();
    EXPECT_EQ(g.at("x-version").as_int64(), 1);
    const auto& seps = g.at("rules").at("separators").as_array();
    ASSERT_EQ(seps.size(), 2u);
    EXPECT_EQ(seps[0].as_string(), ",");
    EXPECT_EQ(seps[1].as_string(), ":");
}

TEST(CanonicalJsonGolden, FloatReprEveryCase) {
    const auto g = load_canonical_golden();
    const auto& cases = g.at("float_repr").as_array();
    ASSERT_GT(cases.size(), 2000u);
    std::size_t checked = 0;
    for (const auto& c : cases) {
        const double d = double_from_bits_hex(c.at("bits_hex").as_string());
        const std::string want = c.at("repr").as_string();
        EXPECT_EQ(cj::float_repr(d), want) << "bits " << c.at("bits_hex").as_string();
        // The repr must also round-trip through the parser to the same bits.
        const auto back = cj::parse(want);
        ASSERT_TRUE(back.is_double()) << want;
        std::uint64_t b1, b2;
        const double p = back.as_number();
        std::memcpy(&b1, &d, sizeof b1);
        std::memcpy(&b2, &p, sizeof b2);
        EXPECT_EQ(b1, b2) << want;
        ++checked;
    }
    EXPECT_EQ(checked, cases.size());
}

TEST(CanonicalJsonGolden, StringEscapeEveryCase) {
    const auto g = load_canonical_golden();
    const auto& cases = g.at("string_escape").as_array();
    ASSERT_GE(cases.size(), 24u);
    for (const auto& c : cases) {
        const std::string input = utf8_from_codepoints(c.at("input_codepoints"));
        const std::string want = c.at("json").as_string();
        EXPECT_EQ(cj::canonical_json(cj::Value(input)), want);
        // parse(json) gives the raw string back
        EXPECT_EQ(cj::parse(want).as_string(), input);
    }
}

TEST(CanonicalJsonGolden, DocumentsCanonicalHashAndRoundTrip) {
    const auto g = load_canonical_golden();
    const auto& docs = g.at("documents").as_array();
    ASSERT_GE(docs.size(), 9u);
    for (const auto& d : docs) {
        const std::string canonical = d.at("canonical").as_string();
        const cj::Value v = cj::parse(canonical);
        EXPECT_EQ(cj::canonical_json(v), canonical);
        EXPECT_EQ(cj::content_hash(v), d.at("sha256").as_string());
        EXPECT_EQ(cj::sha256_hex(canonical), d.at("sha256").as_string());
        // Structural equality survives a second round trip.
        EXPECT_EQ(cj::parse(cj::canonical_json(v)), v);
    }
}

TEST(CanonicalJsonGolden, IntegersStayExactAcrossTheWholeDomain) {
    const cj::Value v = cj::parse(
        "{\"i64_max\":9223372036854775807,\"i64_min\":-9223372036854775808,"
        "\"two53_plus_one\":9007199254740993,\"u64_max\":18446744073709551615}");
    EXPECT_EQ(v.at("i64_max").as_int64(), std::numeric_limits<std::int64_t>::max());
    EXPECT_EQ(v.at("i64_min").as_int64(), std::numeric_limits<std::int64_t>::min());
    EXPECT_EQ(v.at("two53_plus_one").as_int64(), 9007199254740993LL);
    EXPECT_EQ(v.at("u64_max").as_uint64(), std::numeric_limits<std::uint64_t>::max());
    EXPECT_THROW(v.at("u64_max").as_int64(), std::invalid_argument);
    EXPECT_THROW(v.at("i64_min").as_uint64(), std::invalid_argument);
    EXPECT_THROW(cj::parse("18446744073709551616"), std::invalid_argument);
    EXPECT_THROW(cj::parse("-9223372036854775809"), std::invalid_argument);
    // 1 is an integer, 1.0 a double — and they are not equal.
    EXPECT_TRUE(cj::parse("1").is_integer());
    EXPECT_TRUE(cj::parse("1.0").is_double());
    EXPECT_NE(cj::parse("1"), cj::parse("1.0"));
    EXPECT_EQ(cj::canonical_json(cj::parse("1.0")), "1.0");
    EXPECT_EQ(cj::canonical_json(cj::parse("1")), "1");
    // Int and UInt are one number line.
    EXPECT_EQ(cj::Value(5), cj::Value(5ull));
    EXPECT_EQ(cj::Value(std::uint64_t{7}).kind(), cj::Value::Kind::Int);
}

TEST(CanonicalJsonGolden, RejectsNonFiniteAndNonStringKeys) {
    const auto g = load_canonical_golden();
    const auto& rejects = g.at("rejects").as_array();
    ASSERT_EQ(rejects.size(), 5u);
    for (const auto& r : rejects) {
        const std::string c = r.at("case").as_string();
        if (c == "nan") {
            EXPECT_THROW(cj::canonical_json(cj::Value(std::nan(""))),
                         std::invalid_argument);
        } else if (c == "inf") {
            EXPECT_THROW(
                cj::canonical_json(cj::Value(std::numeric_limits<double>::infinity())),
                std::invalid_argument);
        } else if (c == "-inf") {
            EXPECT_THROW(
                cj::canonical_json(cj::Value(-std::numeric_limits<double>::infinity())),
                std::invalid_argument);
        } else if (c == "nested nan") {
            cj::Object o;
            o["a"] = cj::Array{cj::Value(1), cj::Value(std::nan(""))};
            EXPECT_THROW(cj::canonical_json(cj::Value(o)), std::invalid_argument);
            EXPECT_THROW(cj::content_hash(cj::Value(o)), std::invalid_argument);
        } else if (c == "integer key") {
            // The typed tree cannot express a non-string key; the parser
            // rejects one on the wire instead.
            EXPECT_THROW(cj::parse("{1:2}"), std::invalid_argument);
        } else {
            FAIL() << "unknown reject case " << c;
        }
    }
    EXPECT_THROW(cj::parse("NaN"), std::invalid_argument);
    EXPECT_THROW(cj::parse("Infinity"), std::invalid_argument);
    EXPECT_THROW(cj::parse("[1,]"), std::invalid_argument);
    EXPECT_THROW(cj::parse("{\"a\":1,\"a\":2}"), std::invalid_argument);
    EXPECT_THROW(cj::parse("01"), std::invalid_argument);
    EXPECT_THROW(cj::parse("1 2"), std::invalid_argument);
}

TEST(CanonicalJsonGolden, TraceIdPinned) {
    const auto g = load_canonical_golden();
    const auto& t = g.at("trace_id");
    const auto& in = t.at("inputs");
    const std::string got = cj::make_trace_id(
        in.at("session_id").as_string(),
        static_cast<std::uint32_t>(in.at("instrument_id").as_uint64()),
        in.at("event_ts").as_int64(), in.at("sequence").as_uint64());
    EXPECT_EQ(got, t.at("expected").as_string());
    EXPECT_EQ(cj::sha256_hex(t.at("preimage").as_string()).substr(0, 32),
              t.at("expected").as_string());
    EXPECT_TRUE(cj::is_trace_id(got));
    EXPECT_THROW(cj::make_trace_id("bad|session", 1, 0, 0), std::invalid_argument);
    EXPECT_THROW(cj::make_trace_id("", 1, 0, 0), std::invalid_argument);
}

TEST(CanonicalJsonGolden, ContractsExampleKnownAnswer) {
    // expected_contracts_examples.json carries a second canonical_json known
    // answer (input object -> text -> sha256).
    const auto ex = cj::parse(iap_test::read_text_file(
        iap_test::golden_path("expected_contracts_examples.json")));
    const auto& kv = ex.at("canonical_json");
    EXPECT_EQ(cj::canonical_json(kv.at("input")), kv.at("text").as_string());
    EXPECT_EQ(cj::content_hash(kv.at("input")), kv.at("sha256").as_string());
}

TEST(CanonicalJsonGolden, KeyOrderIsCodePointOrder) {
    cj::Object o;
    o["\xc3\xa9"] = cj::Value(8);   // U+00E9
    o["z"] = cj::Value(5);
    o["A"] = cj::Value(3);
    o["a0"] = cj::Value(10);
    o["a"] = cj::Value(2);
    o["_"] = cj::Value(6);
    o["0"] = cj::Value(7);
    o["\xe2\x82\xac"] = cj::Value(11);  // U+20AC sorts after U+00E9
    EXPECT_EQ(cj::canonical_json(cj::Value(o)),
              "{\"0\":7,\"A\":3,\"_\":6,\"a\":2,\"a0\":10,\"z\":5,"
              "\"\\u00e9\":8,\"\\u20ac\":11}");
}

}  // namespace
