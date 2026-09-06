// Canonical codecs: JSONL and IAP1 binary (normative layout: schemas/FORMAT.md).
//
// Both encoders are byte-exact: the same event vector must produce
// byte-identical files in every language (verified via SHA-256 golden tests).
// Malformed input -> std::invalid_argument; I/O failure -> std::runtime_error.

#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

#include "iap/marketdata/events.hpp"

namespace iap {

constexpr std::uint32_t IAP1_MAGIC = 0x49415031;  // "1PAI" on disk (LE)
// Version written by encode_iap1 (header + records + integrity trailer).
constexpr std::uint32_t IAP1_VERSION = 2;
// Legacy version (no trailer) still accepted by the decoder.
constexpr std::uint32_t IAP1_VERSION_LEGACY = 1;
constexpr std::size_t IAP1_HEADER_SIZE = 16;
constexpr std::size_t IAP1_RECORD_SIZE = 72;
constexpr std::size_t IAP1_TRAILER_SIZE = 16;

// CRC-32 (IEEE 802.3 / zlib) — the IAP1 trailer checksum.
// Known answer: crc32("123456789") == 0xCBF43926.
std::uint32_t crc32(const std::uint8_t* data, std::size_t len);

// ------------------------------------------------------------------- JSONL

// Encode one event as a canonical JSONL line (no trailing newline):
// pinned key order, compact separators, integers only.
std::string encode_jsonl_line(const MarketEvent& ev);

// Decode one canonical JSONL line. Strict: exact keys in exact order,
// integer values only (rejects floats, bools, strings, missing/extra keys).
MarketEvent decode_jsonl_line(std::string_view line);

// Encode events to canonical JSONL bytes (LF after every line, incl. last).
std::string encode_jsonl(const std::vector<MarketEvent>& events);

// Decode canonical JSONL bytes (blank lines skipped, as in the reference).
std::vector<MarketEvent> decode_jsonl(std::string_view data);

// File helpers; return/read the full event vector.
std::size_t write_jsonl(const std::string& path,
                        const std::vector<MarketEvent>& events);
std::vector<MarketEvent> read_jsonl(const std::string& path);

// -------------------------------------------------------------------- IAP1

// Encode events to IAP1 bytes (16-byte LE header + 72-byte packed records).
std::vector<std::uint8_t> encode_iap1(const std::vector<MarketEvent>& events);

// Result of decode_iap1_ex: events plus the file's format version and
// whether the CRC-32 integrity trailer was present and verified.
struct Iap1Decoded {
    std::vector<MarketEvent> events;
    std::uint32_t version = 0;
    bool integrity_checked = false;
};

// Decode IAP1 bytes (version 2 with trailer, or legacy version 1). Rejects
// bad magic / unknown version / truncation / count mismatch / bad trailer
// (reserved != 0, count echo, CRC mismatch) with std::invalid_argument.
Iap1Decoded decode_iap1_ex(const std::uint8_t* data, std::size_t len);
std::vector<MarketEvent> decode_iap1(const std::uint8_t* data, std::size_t len);
std::vector<MarketEvent> decode_iap1(const std::vector<std::uint8_t>& data);

std::size_t write_iap1(const std::string& path,
                       const std::vector<MarketEvent>& events);
std::vector<MarketEvent> read_iap1(const std::string& path);

// Read a whole file as bytes (throws std::runtime_error if unreadable).
std::vector<std::uint8_t> read_file_bytes(const std::string& path);

}  // namespace iap
