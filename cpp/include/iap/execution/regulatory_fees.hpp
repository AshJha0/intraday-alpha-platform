// Dated US equity regulatory fee schedule (v1.15, X-P1-1): SEC Section 31
// transaction fee + FINRA Trading Activity Fee, charged on SELL fills of
// equity venues only. Port of python/src/iap/execution/regulatory.py; the
// arithmetic is pinned and checked against
// tests/golden/expected_regulatory_fees.json:
//
//     sec   = usd_per_million * notional / 1e6
//     taf   = min(per_share * qty, max_per_trade)
//     total = sec + taf            (0.0 on a buy)
//
// A period covers fills with from_ns <= ts < until_ns (until_ns absent =
// open-ended); a fill outside every period throws (fail closed). Off by
// default: ExecConfig::regulatory_fees is empty unless a caller loads it.

#pragma once

#include <algorithm>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

#include "iap/util/json.hpp"

namespace iap {

struct RegulatoryFeePeriod {
    std::int64_t from_ns = 0;
    std::int64_t until_ns = std::numeric_limits<std::int64_t>::max();
    double rate = 0.0;           // SEC: USD per million; TAF: USD per share
    double max_per_trade = 0.0;  // TAF only
};

class RegulatoryFeeSchedule {
public:
    std::vector<RegulatoryFeePeriod> sec;
    std::vector<RegulatoryFeePeriod> taf;

    static RegulatoryFeeSchedule from_json(const Json& root) {
        RegulatoryFeeSchedule s;
        for (const auto& r : root["sec_section31"].a()) {
            RegulatoryFeePeriod p = period(r);
            p.rate = r["usd_per_million"].num();
            s.sec.push_back(p);
        }
        for (const auto& r : root["finra_taf"].a()) {
            RegulatoryFeePeriod p = period(r);
            p.rate = r["per_share"].num();
            p.max_per_trade = r["max_per_trade"].num();
            s.taf.push_back(p);
        }
        check(s.sec, "sec_section31");
        check(s.taf, "finra_taf");
        return s;
    }

    static RegulatoryFeeSchedule load(const std::string& path) {
        return from_json(read_json_file(path));
    }

    // Regulatory fee of one equity fill (side 0 = buy, 1 = sell).
    double fee(std::uint8_t side, double qty, double notional,
               std::int64_t ts) const {
        if (side != 1) return 0.0;
        const RegulatoryFeePeriod& s = find(sec, ts, "sec_section31");
        const RegulatoryFeePeriod& t = find(taf, ts, "finra_taf");
        const double sec_fee = s.rate * notional / 1e6;
        const double taf_fee = std::min(t.rate * qty, t.max_per_trade);
        return sec_fee + taf_fee;
    }

private:
    static RegulatoryFeePeriod period(const Json& r) {
        RegulatoryFeePeriod p;
        p.from_ns = r["from_ns"].i64();
        if (r.has("until_ns")) p.until_ns = r["until_ns"].i64();
        return p;
    }

    static void check(const std::vector<RegulatoryFeePeriod>& ps,
                      const char* what) {
        if (ps.empty()) {
            throw std::runtime_error(std::string("empty ") + what + " table");
        }
        for (std::size_t i = 0; i < ps.size(); ++i) {
            if (ps[i].until_ns <= ps[i].from_ns ||
                (i > 0 && ps[i].from_ns < ps[i - 1].until_ns) ||
                ps[i].rate < 0.0 || ps[i].max_per_trade < 0.0) {
                throw std::runtime_error(std::string("bad ") + what +
                                         " period " + std::to_string(i));
            }
        }
    }

    static const RegulatoryFeePeriod& find(
        const std::vector<RegulatoryFeePeriod>& ps, std::int64_t ts,
        const char* what) {
        for (const auto& p : ps) {
            if (p.from_ns <= ts && ts < p.until_ns) return p;
        }
        throw std::runtime_error(std::string("no ") + what +
                                 " period covers exchange_ts " +
                                 std::to_string(ts));
    }
};

}  // namespace iap
