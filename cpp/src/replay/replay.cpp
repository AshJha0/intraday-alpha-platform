#include "iap/replay/replay.hpp"

#include <stdexcept>

namespace iap {

ReplayEngine::ReplayEngine(std::uint64_t checkpoint_every,
                           std::uint64_t snapshot_every,
                           std::size_t keep_checkpoints)
    : checkpoint_every_(checkpoint_every),
      snapshot_every_(snapshot_every),
      keep_checkpoints_(keep_checkpoints) {}

ConsolidatedBook& ReplayEngine::instrument_book(std::uint32_t instrument_id) {
    auto it = books_.find(instrument_id);
    if (it == books_.end()) {
        it = books_.emplace(instrument_id, ConsolidatedBook(instrument_id))
                 .first;
    }
    return it->second;
}

void ReplayEngine::apply(const MarketEvent& ev) {
    if (ev.exchange_ts < last_exchange_ts_) ++time_regressions_;
    last_exchange_ts_ = ev.exchange_ts;
    instrument_book(ev.instrument_id).apply(ev);
    ++events_processed_;
}

ReplayRunSummary ReplayEngine::run(const std::vector<MarketEvent>& events,
                                   const SnapshotCallback& on_snapshot) {
    for (const auto& ev : events) {
        apply(ev);
        if (snapshot_every_ != 0 &&
            events_processed_ % snapshot_every_ == 0) {
            ReplaySnapshot snap;
            snap.index = events_processed_;
            snap.states = book_states();
            snapshots_.push_back(snap);
            if (on_snapshot) on_snapshot(events_processed_, snapshots_.back());
        }
        if (checkpoint_every_ != 0 &&
            events_processed_ % checkpoint_every_ == 0) {
            checkpoints_.push_back(checkpoint());
            if (checkpoints_.size() > keep_checkpoints_) {
                checkpoints_.erase(checkpoints_.begin());
            }
        }
    }
    ReplayRunSummary summary;
    summary.events_processed = events_processed_;
    summary.instruments = books_.size();
    summary.time_regressions = time_regressions_;
    summary.snapshots = snapshots_.size();
    return summary;
}

BookStates ReplayEngine::book_states() const {
    BookStates out;
    for (const auto& [iid, cons] : books_) {
        auto& venues = out[iid];
        for (const auto& [vid, book] : cons.books()) {
            venues.emplace(vid, book.state_summary());
        }
    }
    return out;
}

ReplayCheckpoint ReplayEngine::checkpoint() const {
    ReplayCheckpoint cp;
    cp.events_processed = events_processed_;
    cp.last_exchange_ts = last_exchange_ts_;
    cp.time_regressions = time_regressions_;
    cp.checkpoint_every = checkpoint_every_;
    cp.snapshot_every = snapshot_every_;
    for (const auto& [iid, cons] : books_) {
        cp.books.emplace(iid, cons.checkpoint());
    }
    return cp;
}

ReplayEngine ReplayEngine::restore(const ReplayCheckpoint& cp) {
    ReplayEngine engine(cp.checkpoint_every, cp.snapshot_every);
    engine.events_processed_ = cp.events_processed;
    engine.last_exchange_ts_ = cp.last_exchange_ts;
    engine.time_regressions_ = cp.time_regressions;
    for (const auto& [iid, bcp] : cp.books) {
        engine.books_.emplace(iid, ConsolidatedBook::restore(bcp));
    }
    return engine;
}

}  // namespace iap
