"""Synthetic generator tests: determinism, structure, anomaly injection."""

import pytest

from iap.core.codec import encode_iap1, read_jsonl, sha256_bytes
from iap.core.events import EventType, SessionStatus, Side, validation_error
from iap.marketdata.generator import (
    MarketDataGenerator,
    generate_golden_eq,
    generate_golden_fx,
)

SMALL_CFG = {
    "seed": 777,
    "sessions": 1,
    "equities": {"slots_per_stream": 120},
    "fx": {"slots_per_pair": 100},
}


def _run(refdata, tmp_path, cfg=None, sub="run"):
    gen = MarketDataGenerator(refdata, cfg or SMALL_CFG)
    out = tmp_path / sub
    stats = gen.generate_run(out)
    events = []
    for name in sorted(stats["files"]):
        events.extend(read_jsonl(out / name))
    return gen, stats, events


def test_same_seed_identical_sha256(refdata, tmp_path):
    _, s1, e1 = _run(refdata, tmp_path, sub="a")
    _, s2, e2 = _run(refdata, tmp_path, sub="b")
    assert s1["files"] == s2["files"]
    assert sha256_bytes(encode_iap1(e1)) == sha256_bytes(encode_iap1(e2))


def test_different_seed_differs(refdata, tmp_path):
    cfg2 = dict(SMALL_CFG, seed=778)
    _, _, e1 = _run(refdata, tmp_path, sub="a")
    _, _, e2 = _run(refdata, tmp_path, cfg2, sub="b")
    assert sha256_bytes(encode_iap1(e1)) != sha256_bytes(encode_iap1(e2))


def test_golden_eq_vector_properties(refdata):
    eq = generate_golden_eq(refdata)
    assert len(eq) == 2000
    assert [e.event_id for e in eq] == list(range(1, 2001))
    assert [e.sequence for e in eq] == list(range(1, 2001))  # single clean stream
    assert all(e.instrument_id == 1 and e.venue_id == 1 for e in eq)
    assert all(validation_error(e) is None for e in eq)
    assert all(e.receive_ts >= e.exchange_ts for e in eq)
    # exchange-time ordered
    assert all(a.exchange_ts <= b.exchange_ts for a, b in zip(eq, eq[1:]))
    types = {e.event_type for e in eq}
    assert {EventType.ADD, EventType.MODIFY, EventType.CANCEL,
            EventType.EXECUTE, EventType.TRADE, EventType.STATUS} <= types
    assert EventType.SNAPSHOT not in types  # golden vector is anomaly-free


def test_golden_eq_deterministic(refdata):
    a = generate_golden_eq(refdata)
    b = generate_golden_eq(refdata)
    assert a == b


def test_golden_fx_vector_properties(refdata):
    fx = generate_golden_fx(refdata)
    assert len(fx) == 800
    assert [e.event_id for e in fx] == list(range(1, 801))
    assert all(e.instrument_id == 101 for e in fx)
    assert {e.venue_id for e in fx} == {10, 11, 12}
    assert all(validation_error(e) is None for e in fx)
    assert {e.event_type for e in fx} <= {EventType.QUOTE, EventType.TRADE}
    # per-venue sequences contiguous from 1
    for vid in (10, 11, 12):
        seqs = [e.sequence for e in fx if e.venue_id == vid]
        assert seqs == list(range(1, len(seqs) + 1))


def test_fx_venue_specific_latency(refdata):
    fx = generate_golden_fx(refdata)
    lat = {}
    for vid in (10, 11, 12):
        ls = [e.receive_ts - e.exchange_ts for e in fx if e.venue_id == vid]
        lat[vid] = sum(ls) / len(ls)
        mean = refdata.venue(vid).latency_mean_ns
        jitter = refdata.venue(vid).latency_jitter_ns
        assert all(mean <= l <= mean + jitter + 1000 for l in ls)
    assert lat[11] > lat[10] > lat[12]  # LP2 slowest, PRI fastest


def test_event_id_monotone_per_file(refdata, tmp_path):
    gen = MarketDataGenerator(refdata, SMALL_CFG)
    out = tmp_path / "run"
    stats = gen.generate_run(out)
    for name in stats["files"]:
        ids = [e.event_id for e in read_jsonl(out / name)]
        assert ids == list(range(1, len(ids) + 1))


def test_run_injects_all_anomaly_classes(refdata, tmp_path):
    cfg = dict(SMALL_CFG)
    cfg["anomalies"] = {
        "gap_prob": 0.004, "gap_max_events": 3, "dup_prob": 0.01,
        "ooo_prob": 0.006, "invalid_prob": 0.004, "ts_violation_prob": 0.004,
    }
    _, stats, events = _run(refdata, tmp_path, cfg)
    inj = stats["injected_anomalies"]
    assert inj["gaps"] > 0
    assert inj["duplicates"] > 0
    assert inj["out_of_order"] > 0
    assert inj["invalid"] > 0
    assert inj["ts_violations"] > 0
    # invalid events really are invalid; everything else validates or is a
    # ts violation (receive < exchange).
    n_invalid = sum(
        1 for e in events
        if e.receive_ts >= e.exchange_ts and validation_error(e) is not None
    )
    assert n_invalid == inj["invalid"]
    n_tsv = sum(1 for e in events if e.receive_ts < e.exchange_ts)
    assert n_tsv == inj["ts_violations"]


def test_run_contains_halt_and_auctions(refdata, tmp_path):
    _, stats, events = _run(refdata, tmp_path)
    halted = [e for e in events
              if e.event_type == EventType.STATUS and e.qty == SessionStatus.HALT]
    assert len(halted) == 2  # SYN.EQ.007 on both venues, session 0
    assert all(e.instrument_id == 7 for e in halted)
    stat = [e for e in events if e.event_type == EventType.STATUS]
    assert any(e.qty == SessionStatus.AUCTION for e in stat)
    assert any(e.qty == SessionStatus.CLOSE for e in stat)
    assert any(e.qty == SessionStatus.TRADING for e in stat)


def test_gap_injection_comes_with_snapshot_recovery(refdata, tmp_path):
    cfg = dict(SMALL_CFG)
    cfg["anomalies"] = {
        "gap_prob": 0.01, "gap_max_events": 2, "dup_prob": 0.0,
        "ooo_prob": 0.0, "invalid_prob": 0.0, "ts_violation_prob": 0.0,
    }
    _, stats, events = _run(refdata, tmp_path, cfg)
    assert stats["injected_anomalies"]["gaps"] > 0
    snaps = [e for e in events if e.event_type == EventType.SNAPSHOT]
    assert snaps, "gaps must be followed by SNAPSHOT recovery bursts"
    assert all(validation_error(e) is None for e in snaps)
    # every burst terminates (trade_id == 0 marks the last record)
    assert any(e.trade_id == 0 for e in snaps)


def test_multi_session_sequences_continue(refdata, tmp_path):
    cfg = {
        "seed": 5, "sessions": 2,
        "equities": {"slots_per_stream": 60}, "fx": {"slots_per_pair": 50},
        "anomalies": {"gap_prob": 0, "gap_max_events": 1, "dup_prob": 0,
                      "ooo_prob": 0, "invalid_prob": 0, "ts_violation_prob": 0},
    }
    gen = MarketDataGenerator(refdata, cfg)
    out = tmp_path / "run"
    stats = gen.generate_run(out)
    names = sorted(stats["files"])
    day1 = read_jsonl(out / "eq_20260824.jsonl")
    day2 = read_jsonl(out / "eq_20260825.jsonl")
    key = (1, 1)
    s1 = [e.sequence for e in day1 if (e.instrument_id, e.venue_id) == key]
    s2 = [e.sequence for e in day2 if (e.instrument_id, e.venue_id) == key]
    assert s1 == list(range(1, len(s1) + 1))
    assert s2 == list(range(s1[-1] + 1, s1[-1] + 1 + len(s2)))
    assert len(names) == 4


def test_calendar_exhaustion_raises(refdata, tmp_path):
    cfg = dict(SMALL_CFG, sessions=99)
    gen = MarketDataGenerator(refdata, cfg)
    with pytest.raises(ValueError, match="trading days"):
        gen.generate_run(tmp_path / "x")


def test_equity_receive_after_exchange_everywhere(refdata):
    eq = generate_golden_eq(refdata)
    v = refdata.venue("XV1")
    for e in eq:
        assert e.receive_ts - e.exchange_ts >= 0
        # latency modelled: bounded by mean+jitter plus monotonicity nudges
        assert e.receive_ts - e.exchange_ts <= v.latency_mean_ns + v.latency_jitter_ns + 10_000


def test_consolidated_equity_book_rarely_crossed(refdata, tmp_path):
    """CRITICAL research-validity property: venues share one efficient price
    per instrument, so the consolidated equity book must be crossed for well
    under 2% of event states (an earlier per-venue-mid design sat near 95%)."""
    from iap.orderbook.book import ConsolidatedBook

    cfg = {
        "seed": 424242, "sessions": 1,
        "equities": {"slots_per_stream": 1800}, "fx": {"slots_per_pair": 10},
        "anomalies": {"gap_prob": 0, "gap_max_events": 1, "dup_prob": 0,
                      "ooo_prob": 0, "invalid_prob": 0,
                      "ts_violation_prob": 0},
    }
    gen = MarketDataGenerator(refdata, cfg)
    out = tmp_path / "run"
    stats = gen.generate_run(out)
    eq_file = next(n for n in sorted(stats["files"]) if n.startswith("eq_"))
    books = {}
    total = crossed = 0
    for ev in read_jsonl(out / eq_file):
        cons = books.setdefault(ev.instrument_id,
                                ConsolidatedBook(ev.instrument_id))
        cons.apply(ev)
        bb, ba = cons.best_bid(), cons.best_ask()
        if bb is None or ba is None:
            continue
        total += 1
        if bb[0] > ba[0]:
            crossed += 1
    assert total > 30_000
    assert crossed / total < 0.02, (
        f"consolidated equity book crossed {crossed}/{total} "
        f"({crossed / total:.2%}) of event states"
    )


def test_equity_venues_share_one_efficient_price(refdata, tmp_path):
    """Both venues print IDENTICAL close-auction prices per instrument (they
    quote around the same shared efficient price path)."""
    gen = MarketDataGenerator(refdata, SMALL_CFG)
    out = tmp_path / "run"
    stats = gen.generate_run(out)
    eq_file = next(n for n in sorted(stats["files"]) if n.startswith("eq_"))
    events = read_jsonl(out / eq_file)
    close_ns = max(e.exchange_ts for e in events
                   if e.event_type == EventType.STATUS
                   and e.qty == SessionStatus.CLOSE)
    # close-auction trade prints per (instrument, venue)
    prints = {}
    for e in events:
        if (e.event_type == EventType.TRADE
                and e.exchange_ts > close_ns - 60_000_000_000):
            prints.setdefault((e.instrument_id, e.venue_id),
                              []).append(e.price_ticks)
    by_inst = {}
    for (iid, vid), prices in prints.items():
        by_inst.setdefault(iid, {})[vid] = sorted(set(prices))
    assert by_inst
    for iid, venues in by_inst.items():
        assert len(venues) == 2
        a, b = venues.values()
        assert a == b, f"instrument {iid} close prints differ across venues"
