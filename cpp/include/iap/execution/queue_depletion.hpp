// Queue-depletion read of a calibration document (v1.17 F7 part 1; the
// Python reference is iap.execution.calibration.ExecCalibration.
// queue_depletion_hazard / queue_exec_depletion_hazard /
// queue_cancel_depletion_hazard over iap.tca.markout.queue_bucket).
//
// A calibration document (research/execution/calibration.json,
// `iap.exec_calibration` v1) carries, per side ("bid" / "ask") and per
// queue-position bucket, the depletions per second of a touch level:
//   hazard_per_s         executions AND cancels pooled (the v1.9 rate);
//   exec_hazard_per_s    EXECUTION depletions only (v1.17 B5, review X-4:
//                        the rate that actually fills a resting order);
//   cancel_hazard_per_s  CANCEL / MODIFY depletions (the orders ahead left).
// The split cells exist only when the document was estimated with
// depletion_split=True; a missing document, side, bucket, cell or rate is
// "unknown" (nullopt), never 0.
//
// Bucket labels (queue_bucket, pinned): edges e_0 < e_1 < ... ; a queue of
// q shares is "<e>" when e == lo (the first edge, or an edge equal to the
// running lower bound), "<lo>-<e>" for the first edge e >= q, else
// "><last edge>", with lo = previous edge + 1. Default edges (0, 500, 2000)
// give "0", "1-500", "501-2000", ">2000".
//
// The read is a dictionary lookup, not arithmetic; it is ported so the C++
// B5 queue rule and the C++ hazard fill probability (sor_v2.hpp) consume
// the same document byte for byte.

#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "iap/contracts/canonical_json.hpp"

namespace iap {

// iap.tca.markout.queue_bucket.
std::string queue_bucket(std::int64_t entry_ahead_qty,
                         const std::vector<std::int64_t>& edges);

inline const std::vector<std::int64_t>& default_queue_edges() {
    static const std::vector<std::int64_t> e{0, 500, 2000};
    return e;
}

// The `queue_depletion` block of one calibration document.
class QueueDepletionTable {
public:
    QueueDepletionTable() = default;
    // From a parsed calibration document (reads `queue_edges` and
    // `queue_depletion`; everything else is ignored).
    static QueueDepletionTable from_value(const contracts::Value& doc);

    // ExecCalibration.queue_depletion_hazard (pooled).
    std::optional<double> hazard(std::uint8_t side, std::int64_t queue_qty) const {
        return rate(side, queue_qty, "hazard_per_s");
    }
    // ExecCalibration.queue_exec_depletion_hazard (X-4: executions only).
    std::optional<double> exec_hazard(std::uint8_t side, std::int64_t queue_qty) const {
        return rate(side, queue_qty, "exec_hazard_per_s");
    }
    // ExecCalibration.queue_cancel_depletion_hazard (X-4: cancels only).
    std::optional<double> cancel_hazard(std::uint8_t side, std::int64_t queue_qty) const {
        return rate(side, queue_qty, "cancel_hazard_per_s");
    }

    const std::vector<std::int64_t>& edges() const { return edges_; }

private:
    using Cell = std::map<std::string, double>;  // rate name -> value
    std::optional<double> rate(std::uint8_t side, std::int64_t queue_qty,
                               const char* key) const;

    std::vector<std::int64_t> edges_ = default_queue_edges();
    std::map<std::string, Cell> bid_;
    std::map<std::string, Cell> ask_;
};

}  // namespace iap
