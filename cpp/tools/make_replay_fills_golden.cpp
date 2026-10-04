// Generator for the execution-replay goldens (run manually; the files are
// pinned — regeneration requires a schemas/MIGRATIONS.md entry).
//
//   make_replay_fills_golden <golden_dir>           expected_replay_fills.json
//   make_replay_fills_golden <golden_dir> passive   expected_replay_fills_passive.json
//
// The scenarios, the configuration and the writer live in
// replay_fills_golden.hpp (hand-traced in cpp/tests/test_replay_fills.cpp;
// the policy scenario in cpp/tests/test_replay_fills_passive.cpp, which also
// proves that this writer reproduces both committed files byte for byte).
//
// Golden v2 of the NATIVE file (round 3): children expire at end_ts
// (execution.hpp rule 7), so the VWAP slice-2 child that v1 filled 290 s
// after the window now expires unfilled (parent 1: 329 filled / 71
// unfilled).
//
// Refuses to overwrite an existing file.

#include <cstdio>
#include <fstream>
#include <string>

#include "iap/marketdata/codec.hpp"
#include "iap/replay/exec_replay.hpp"
#include "replay_fills_golden.hpp"

int main(int argc, char** argv) {
    const bool passive = argc == 3 && std::string(argv[2]) == "passive";
    if (argc != 2 && !passive) {
        std::fprintf(stderr, "usage: %s <golden_dir> [passive]\n", argv[0]);
        return 2;
    }
    const std::string golden_dir = argv[1];
    const std::string out_path =
        golden_dir + (passive ? "/expected_replay_fills_passive.json"
                              : "/expected_replay_fills.json");
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

    iap::ExecutionReplay replay(
        iap_golden::golden_exec_config(configs_dir),
        passive ? iap_golden::passive_parents(t0)
                : iap_golden::native_parents(t0));
    const auto res = replay.run(events);
    const std::string doc =
        passive ? iap_golden::render_replay_fills(
                      res, t0, 1, iap_golden::kPassiveDescription, true)
                : iap_golden::render_replay_fills(
                      res, t0, 2, iap_golden::kNativeDescription, false);

    std::FILE* f = std::fopen(out_path.c_str(), "wb");
    if (f == nullptr) {
        std::fprintf(stderr, "cannot write %s\n", out_path.c_str());
        return 1;
    }
    std::fwrite(doc.data(), 1, doc.size(), f);
    std::fclose(f);
    std::printf("wrote %s (%zu fills)\n", out_path.c_str(), res.fills.size());
    return 0;
}
