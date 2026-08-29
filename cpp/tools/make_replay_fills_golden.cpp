// Generator for tests/golden/expected_replay_fills.json (run manually once;
// the file is pinned — regeneration requires a schemas/MIGRATIONS.md entry).
//
// Pinned scenario (documented in the file itself and hand-traced in
// cpp/tests/test_replay_fills.cpp): the EQ golden vector
// (events_eq_mbo.jsonl, instrument 1, venue XV1, tick 0.01) with two parent
// orders:
//
//   parent 1: VWAP BUY 400 over [t0+60s, t0+660s), 4 slices, passive LIMIT
//             children joining the best bid at each slice decision;
//   parent 2: IS   SELL 600 over [t0+120s, t0+720s), 3 slices,
//             risk_aversion 1.0, aggressive MARKET children.
//
// Config: seed 20260829 (configs/execution.json), latency decision/risk/wire
// 50/50/100 us + XV1 venue latency (mean 150 us, jitter uniform [0, 50 us],
// SplitMix64), impact_coeff_bps_per_pct_adv 2.0, instrument 1 adv 38,000,000
// lot 1.0 (fees per share). Fill list is exact (ticks/qty/ts); fee and
// impact_cost are doubles printed with %.17g (round-trip exact).
//
// Usage: make_replay_fills_golden <golden_dir>   (refuses to overwrite)

#include <cinttypes>
#include <cstdio>
#include <fstream>
#include <string>

#include "iap/marketdata/codec.hpp"
#include "iap/replay/exec_replay.hpp"

namespace {

iap::ExecConfig golden_exec_config(const std::string& configs_dir) {
    iap::ExecConfig cfg;
    cfg.seed = 20260829;
    cfg.impact_coeff_bps_per_pct_adv = 2.0;
    cfg.venues = iap::load_venues(configs_dir + "/venues.json");
    iap::InstrumentSpec ins;
    ins.instrument_id = 1;
    ins.tick_size = 0.01;
    ins.lot_size = 1.0;
    ins.adv = 38000000.0;
    cfg.instruments[1] = ins;
    return cfg;
}

std::vector<iap::ParentOrder> golden_parents(std::int64_t t0) {
    constexpr std::int64_t kSec = 1'000'000'000;
    iap::ParentOrder vwap;
    vwap.parent_id = 1;
    vwap.instrument_id = 1;
    vwap.venue_id = 1;
    vwap.side = 0;  // buy
    vwap.qty = 400;
    vwap.algo = iap::AlgoType::VWAP;
    vwap.start_ts = t0 + 60 * kSec;
    vwap.end_ts = t0 + 660 * kSec;
    vwap.slices = 4;
    iap::ParentOrder is;
    is.parent_id = 2;
    is.instrument_id = 1;
    is.venue_id = 1;
    is.side = 1;  // sell
    is.qty = 600;
    is.algo = iap::AlgoType::IS;
    is.start_ts = t0 + 120 * kSec;
    is.end_ts = t0 + 720 * kSec;
    is.slices = 3;
    is.risk_aversion = 1.0;
    return {vwap, is};
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: %s <golden_dir>\n", argv[0]);
        return 2;
    }
    const std::string golden_dir = argv[1];
    const std::string out_path = golden_dir + "/expected_replay_fills.json";
    {
        std::ifstream probe(out_path);
        if (probe) {
            std::fprintf(stderr, "%s already exists — golden files are "
                                 "pinned; refusing to overwrite\n",
                         out_path.c_str());
            return 1;
        }
    }
    const std::string configs_dir = golden_dir + "/../../configs";
    const auto events = iap::read_jsonl(golden_dir + "/events_eq_mbo.jsonl");
    const std::int64_t t0 = events.front().exchange_ts;

    iap::ExecutionReplay replay(golden_exec_config(configs_dir),
                                golden_parents(t0));
    const auto res = replay.run(events);

    std::FILE* f = std::fopen(out_path.c_str(), "wb");
    if (f == nullptr) {
        std::fprintf(stderr, "cannot write %s\n", out_path.c_str());
        return 1;
    }
    std::fprintf(f, "{\n  \"x-version\": 1,\n");
    std::fprintf(
        f,
        "  \"description\": \"Execution-replay golden fills: "
        "events_eq_mbo.jsonl (instrument 1, venue XV1, tick 0.01) worked by "
        "parent 1 = VWAP BUY 400 over [t0+60s, t0+660s) in 4 passive LIMIT "
        "slices joining the best bid, and parent 2 = IS SELL 600 over "
        "[t0+120s, t0+720s) in 3 front-loaded MARKET slices "
        "(risk_aversion 1.0). Pinned rules: cpp/include/iap/execution/"
        "execution.hpp and algos.hpp (C++ port is the reference). Latency "
        "decision/risk/wire 50000/50000/100000 ns + venue mean 150000 ns + "
        "SplitMix64(seed).below(jitter+1) per submission. ticks/qty/ts "
        "exact; fee/impact_cost at 1e-9.\",\n");
    std::fprintf(f, "  \"seed\": 20260829,\n");
    std::fprintf(f, "  \"t0\": %" PRId64 ",\n", t0);
    std::fprintf(f, "  \"events_processed\": %llu,\n",
                 static_cast<unsigned long long>(res.events_processed));
    std::fprintf(f, "  \"fills\": [\n");
    for (std::size_t i = 0; i < res.fills.size(); ++i) {
        const auto& x = res.fills[i];
        std::fprintf(
            f,
            "    {\"fill_id\": %llu, \"order_id\": %llu, \"parent_id\": "
            "%llu, \"instrument_id\": %u, \"venue_id\": %u, \"side\": %u, "
            "\"price_ticks\": %" PRId64 ", \"qty\": %" PRId64
            ", \"ts\": %" PRId64 ", \"liquidity\": \"%s\", \"fee\": %.17g, "
            "\"impact_cost\": %.17g}%s\n",
            static_cast<unsigned long long>(x.fill_id),
            static_cast<unsigned long long>(x.order_id),
            static_cast<unsigned long long>(x.parent_id), x.instrument_id,
            x.venue_id, x.side, x.price_ticks, x.qty, x.ts,
            x.liquidity == iap::Liquidity::MAKER ? "MAKER" : "TAKER", x.fee,
            x.impact_cost, i + 1 < res.fills.size() ? "," : "");
    }
    std::fprintf(f, "  ],\n  \"parents\": {\n");
    std::size_t np = 0;
    for (const auto& [pid, r] : res.parents) {
        std::fprintf(
            f,
            "    \"%llu\": {\"filled_qty\": %" PRId64 ", \"unfilled_qty\": "
            "%" PRId64 ", \"children\": %" PRId64
            ", \"notional\": %.17g, \"avg_price\": %.17g, \"fees\": %.17g, "
            "\"rebates\": %.17g, \"impact\": %.17g, \"total_cost\": "
            "%.17g}%s\n",
            static_cast<unsigned long long>(pid), r.filled_qty,
            r.unfilled_qty, r.children, r.notional, r.avg_price, r.fees,
            r.rebates, r.impact, r.total_cost,
            ++np < res.parents.size() ? "," : "");
    }
    std::fprintf(f, "  }\n}\n");
    std::fclose(f);
    std::printf("wrote %s (%zu fills)\n", out_path.c_str(),
                res.fills.size());
    return 0;
}
