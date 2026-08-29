// JSONL + IAP1 codec unit tests (schemas/FORMAT.md; API_CORE.md section 3).

#include <gtest/gtest.h>

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

#include "iap/marketdata/codec.hpp"
#include "iap/marketdata/events.hpp"
#include "sha256.hpp"

using iap::decode_iap1;
using iap::decode_jsonl;
using iap::decode_jsonl_line;
using iap::encode_iap1;
using iap::encode_jsonl;
using iap::encode_jsonl_line;
using iap::EventType;
using iap::MarketEvent;

namespace {

MarketEvent sample() {
    return MarketEvent::of(7, 1, 1, 1787578200000000000LL,
                           1787578200000181199LL, 9,
                           static_cast<std::uint8_t>(EventType::ADD), 0, 2450,
                           300, 1234567890123456789ULL, 0);
}

std::vector<MarketEvent> sample_vec() {
    std::vector<MarketEvent> v;
    v.push_back(sample());
    MarketEvent e2 = sample();
    e2.event_id = 8;
    e2.sequence = 10;
    e2.side = 1;
    e2.price_ticks = 2451;
    v.push_back(e2);
    return v;
}

}  // namespace

// ------------------------------------------------------------------- JSONL

TEST(Jsonl, EncodeLineCanonicalForm) {
    std::string line = encode_jsonl_line(sample());
    EXPECT_EQ(line,
              "{\"event_id\":7,\"instrument_id\":1,\"venue_id\":1,"
              "\"exchange_ts\":1787578200000000000,"
              "\"receive_ts\":1787578200000181199,\"sequence\":9,"
              "\"event_type\":1,\"side\":0,\"price_ticks\":2450,\"qty\":300,"
              "\"order_id\":1234567890123456789,\"trade_id\":0}");
}

TEST(Jsonl, RoundTripLine) {
    MarketEvent ev = sample();
    EXPECT_EQ(decode_jsonl_line(encode_jsonl_line(ev)), ev);
}

TEST(Jsonl, RoundTripNegativeAndExtremeValues) {
    MarketEvent ev = sample();
    ev.exchange_ts = -42;
    ev.receive_ts = -1;
    ev.price_ticks = INT64_MIN;
    ev.qty = INT64_MAX;
    ev.order_id = UINT64_MAX;
    EXPECT_EQ(decode_jsonl_line(encode_jsonl_line(ev)), ev);
}

TEST(Jsonl, EncodeVectorHasTrailingLf) {
    std::string data = encode_jsonl(sample_vec());
    ASSERT_FALSE(data.empty());
    EXPECT_EQ(data.back(), '\n');
    EXPECT_EQ(decode_jsonl(data).size(), 2u);
}

TEST(Jsonl, DecodeSkipsBlankLines) {
    std::string data = encode_jsonl_line(sample()) + "\n\n  \n" +
                       encode_jsonl_line(sample()) + "\n";
    EXPECT_EQ(decode_jsonl(data).size(), 2u);
}

TEST(Jsonl, RejectsMisorderedKeys) {
    // instrument_id and event_id swapped.
    std::string line =
        "{\"instrument_id\":1,\"event_id\":7,\"venue_id\":1,"
        "\"exchange_ts\":0,\"receive_ts\":0,\"sequence\":9,\"event_type\":1,"
        "\"side\":0,\"price_ticks\":2450,\"qty\":300,\"order_id\":5,"
        "\"trade_id\":0}";
    EXPECT_THROW(decode_jsonl_line(line), std::invalid_argument);
}

TEST(Jsonl, RejectsMissingKey) {
    std::string line = encode_jsonl_line(sample());
    auto pos = line.find(",\"trade_id\":0");
    line.erase(pos, std::string(",\"trade_id\":0").size());
    EXPECT_THROW(decode_jsonl_line(line), std::invalid_argument);
}

TEST(Jsonl, RejectsExtraKey) {
    std::string line = encode_jsonl_line(sample());
    line.insert(line.size() - 1, ",\"extra\":1");
    EXPECT_THROW(decode_jsonl_line(line), std::invalid_argument);
}

TEST(Jsonl, RejectsNonIntegerValues) {
    std::string base = encode_jsonl_line(sample());
    {
        std::string line = base;
        line.replace(line.find("\"qty\":300"), 9, "\"qty\":3.5");
        EXPECT_THROW(decode_jsonl_line(line), std::invalid_argument);
    }
    {
        std::string line = base;
        line.replace(line.find("\"qty\":300"), 9, "\"qty\":true");
        EXPECT_THROW(decode_jsonl_line(line), std::invalid_argument);
    }
    {
        std::string line = base;
        line.replace(line.find("\"qty\":300"), 9, "\"qty\":\"3\"");
        EXPECT_THROW(decode_jsonl_line(line), std::invalid_argument);
    }
    {
        std::string line = base;
        line.replace(line.find("\"qty\":300"), 9, "\"qty\":3e2");
        EXPECT_THROW(decode_jsonl_line(line), std::invalid_argument);
    }
}

TEST(Jsonl, RejectsMalformedLines) {
    EXPECT_THROW(decode_jsonl_line(""), std::invalid_argument);
    EXPECT_THROW(decode_jsonl_line("not json"), std::invalid_argument);
    EXPECT_THROW(decode_jsonl_line("{"), std::invalid_argument);
    std::string line = encode_jsonl_line(sample());
    EXPECT_THROW(decode_jsonl_line(line + "x"), std::invalid_argument);
}

TEST(Jsonl, RejectsUnsignedFieldOutOfRange) {
    std::string line = encode_jsonl_line(sample());
    line.replace(line.find("\"venue_id\":1"), 12, "\"venue_id\":70000");
    EXPECT_THROW(decode_jsonl_line(line), std::invalid_argument);
    std::string line2 = encode_jsonl_line(sample());
    line2.replace(line2.find("\"sequence\":9"), 12, "\"sequence\":-1");
    EXPECT_THROW(decode_jsonl_line(line2), std::invalid_argument);
}

// -------------------------------------------------------------------- IAP1

TEST(Iap1, HeaderAndRecordBytes) {
    auto data = encode_iap1(sample_vec());
    ASSERT_EQ(data.size(), 16u + 2u * 72u);
    // Magic 0x49415031 little-endian on disk: '1' 'P' 'A' 'I'.
    EXPECT_EQ(data[0], '1');
    EXPECT_EQ(data[1], 'P');
    EXPECT_EQ(data[2], 'A');
    EXPECT_EQ(data[3], 'I');
    EXPECT_EQ(data[4], 1u);  // version LE
    EXPECT_EQ(data[5], 0u);
    EXPECT_EQ(data[8], 2u);  // count LE
    // First record starts with event_id=7 LE.
    EXPECT_EQ(data[16], 7u);
    EXPECT_EQ(data[17], 0u);
}

TEST(Iap1, RoundTrip) {
    auto vec = sample_vec();
    auto decoded = decode_iap1(encode_iap1(vec));
    ASSERT_EQ(decoded.size(), vec.size());
    for (std::size_t i = 0; i < vec.size(); ++i) {
        EXPECT_EQ(decoded[i], vec[i]);
    }
}

TEST(Iap1, EmptyVectorRoundTrip) {
    auto data = encode_iap1({});
    EXPECT_EQ(data.size(), 16u);
    EXPECT_TRUE(decode_iap1(data).empty());
}

TEST(Iap1, RejectsBadMagic) {
    auto data = encode_iap1(sample_vec());
    data[0] = 'X';
    EXPECT_THROW(decode_iap1(data), std::invalid_argument);
}

TEST(Iap1, RejectsBadVersion) {
    auto data = encode_iap1(sample_vec());
    data[4] = 2;
    EXPECT_THROW(decode_iap1(data), std::invalid_argument);
}

TEST(Iap1, RejectsTruncatedHeader) {
    std::vector<std::uint8_t> data(10, 0);
    EXPECT_THROW(decode_iap1(data), std::invalid_argument);
}

TEST(Iap1, RejectsSizeCountMismatch) {
    auto data = encode_iap1(sample_vec());
    data.pop_back();  // truncate a record
    EXPECT_THROW(decode_iap1(data), std::invalid_argument);
    auto data2 = encode_iap1(sample_vec());
    data2[8] = 3;  // count says 3, payload has 2
    EXPECT_THROW(decode_iap1(data2), std::invalid_argument);
}

TEST(Iap1, FileRoundTrip) {
    std::string path = ::testing::TempDir() + "iap_codec_roundtrip.iap1";
    auto vec = sample_vec();
    EXPECT_EQ(iap::write_iap1(path, vec), vec.size());
    auto back = iap::read_iap1(path);
    ASSERT_EQ(back.size(), vec.size());
    EXPECT_EQ(back[0], vec[0]);
    EXPECT_EQ(back[1], vec[1]);
}

TEST(Iap1, ReadMissingFileThrows) {
    EXPECT_THROW(iap::read_iap1("/nonexistent/path/x.iap1"),
                 std::runtime_error);
}

// ----------------------------------------------------------------- SHA-256

TEST(Sha256Impl, KnownAnswers) {
    EXPECT_EQ(iap_test::Sha256::hash(std::string("")),
              "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    EXPECT_EQ(iap_test::Sha256::hash(std::string("abc")),
              "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    // Multi-block message (> 64 bytes).
    EXPECT_EQ(iap_test::Sha256::hash(std::string(
                  "abcdefghbcdefghicdefghijdefghijkefghijklfghijklmghijklmn"
                  "hijklmnoijklmnopjklmnopqklmnopqrlmnopqrsmnopqrstnopqrstu")),
              "cf5b16a778af8380036ce59e7b0492370b249b11e8f07a51afac45037afee9d1");
}
