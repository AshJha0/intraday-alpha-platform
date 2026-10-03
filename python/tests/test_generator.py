"""Synthetic generator tests: determinism, structure, anomaly injection."""

import pytest
from iap.core.codec import encode_iap1, read_jsonl, sha256_bytes
from iap.core.events import EventType, SessionStatus, validation_error
from iap.marketdata.generator import (
    MarketDataGenerator,
    excitation_time_factor,
    generate_golden_eq,
    generate_golden_fx,
    load_generator_config,
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
    assert all(a.exchange_ts <= b.exchange_ts for a, b in zip(eq, eq[1:], strict=False))
    types = {e.event_type for e in eq}
    assert {
        EventType.ADD,
        EventType.MODIFY,
        EventType.CANCEL,
        EventType.EXECUTE,
        EventType.TRADE,
        EventType.STATUS,
    } <= types
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
        "gap_prob": 0.004,
        "gap_max_events": 3,
        "dup_prob": 0.01,
        "ooo_prob": 0.006,
        "invalid_prob": 0.004,
        "ts_violation_prob": 0.004,
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
        1 for e in events if e.receive_ts >= e.exchange_ts and validation_error(e) is not None
    )
    assert n_invalid == inj["invalid"]
    n_tsv = sum(1 for e in events if e.receive_ts < e.exchange_ts)
    assert n_tsv == inj["ts_violations"]


def test_run_contains_halt_and_auctions(refdata, tmp_path):
    _, stats, events = _run(refdata, tmp_path)
    halted = [e for e in events if e.event_type == EventType.STATUS and e.qty == SessionStatus.HALT]
    assert len(halted) == 2  # SYN.EQ.007 on both venues, session 0
    assert all(e.instrument_id == 7 for e in halted)
    stat = [e for e in events if e.event_type == EventType.STATUS]
    assert any(e.qty == SessionStatus.AUCTION for e in stat)
    assert any(e.qty == SessionStatus.CLOSE for e in stat)
    assert any(e.qty == SessionStatus.TRADING for e in stat)


def test_gap_injection_comes_with_snapshot_recovery(refdata, tmp_path):
    cfg = dict(SMALL_CFG)
    cfg["anomalies"] = {
        "gap_prob": 0.01,
        "gap_max_events": 2,
        "dup_prob": 0.0,
        "ooo_prob": 0.0,
        "invalid_prob": 0.0,
        "ts_violation_prob": 0.0,
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
        "seed": 5,
        "sessions": 2,
        "equities": {"slots_per_stream": 60},
        "fx": {"slots_per_pair": 50},
        "anomalies": {
            "gap_prob": 0,
            "gap_max_events": 1,
            "dup_prob": 0,
            "ooo_prob": 0,
            "invalid_prob": 0,
            "ts_violation_prob": 0,
        },
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
        "seed": 424242,
        "sessions": 1,
        "equities": {"slots_per_stream": 1800},
        "fx": {"slots_per_pair": 10},
        "anomalies": {
            "gap_prob": 0,
            "gap_max_events": 1,
            "dup_prob": 0,
            "ooo_prob": 0,
            "invalid_prob": 0,
            "ts_violation_prob": 0,
        },
    }
    gen = MarketDataGenerator(refdata, cfg)
    out = tmp_path / "run"
    stats = gen.generate_run(out)
    eq_file = next(n for n in sorted(stats["files"]) if n.startswith("eq_"))
    books = {}
    total = crossed = 0
    for ev in read_jsonl(out / eq_file):
        cons = books.setdefault(ev.instrument_id, ConsolidatedBook(ev.instrument_id))
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
    close_ns = max(
        e.exchange_ts
        for e in events
        if e.event_type == EventType.STATUS and e.qty == SessionStatus.CLOSE
    )
    # close-auction trade prints per (instrument, venue)
    prints = {}
    for e in events:
        if e.event_type == EventType.TRADE and e.exchange_ts > close_ns - 60_000_000_000:
            prints.setdefault((e.instrument_id, e.venue_id), []).append(e.price_ticks)
    by_inst = {}
    for (iid, vid), prices in prints.items():
        by_inst.setdefault(iid, {})[vid] = sorted(set(prices))
    assert by_inst
    for iid, venues in by_inst.items():
        assert len(venues) == 2
        a, b = venues.values()
        assert a == b, f"instrument {iid} close prints differ across venues"


def _last_continuous_fractions(refdata, events, date):
    """Per equity stream: how far into the session its last pre-close event is."""
    open_ns, close_ns = refdata.session_bounds_ns("EQUITY", date)
    last = {}
    for e in events:
        if e.exchange_ts < close_ns:
            key = (e.instrument_id, e.venue_id)
            last[key] = max(last.get(key, open_ns), e.exchange_ts)
    return [(t - open_ns) / (close_ns - open_ns) for t in last.values()]


_QUIET = {
    "gap_prob": 0,
    "gap_max_events": 1,
    "dup_prob": 0,
    "ooo_prob": 0,
    "invalid_prob": 0,
    "ts_violation_prob": 0,
}


def _eq_run(refdata, tmp_path, tag, equities):
    """One quiet single-session run; returns (equity events, raw bytes)."""
    cfg = {
        "seed": 31,
        "sessions": 1,
        "anomalies": _QUIET,
        "equities": equities,
        "fx": {"slots_per_pair": 10},
    }
    date = refdata.trading_days[0]
    name = f"eq_{date.replace('-', '')}.jsonl"
    MarketDataGenerator(refdata, cfg).generate_run(tmp_path / tag)
    path = tmp_path / tag / name
    return read_jsonl(path), path.read_bytes()


def test_default_flow_calibration_spans_the_session(refdata, tmp_path):
    """The default (``flow.calibration = "session"``) spreads
    ``slots_per_stream`` expected slots over the WHOLE session: every equity
    stream's continuous flow starts at the open and reaches the close, and the
    event count stays that of the slot budget (no 2.5x blow-up)."""
    date = refdata.trading_days[0]
    events, raw = _eq_run(refdata, tmp_path, "default", {"slots_per_stream": 400})
    explicit, raw_explicit = _eq_run(
        refdata,
        tmp_path,
        "explicit",
        {"slots_per_stream": 400, "flow": {"calibration": "session"}},
    )
    assert raw == raw_explicit  # "session" IS the default
    legacy, _ = _eq_run(
        refdata,
        tmp_path,
        "legacy",
        {"slots_per_stream": 400, "flow": {"calibration": "legacy_budget"}},
    )

    last = _last_continuous_fractions(refdata, events, date)
    assert len(last) == 22  # 11 instruments x 2 venues
    assert min(last) > 0.95, min(last)  # flow reaches the close on every stream
    assert sum(last) / len(last) > 0.98

    # ... and it is spread over the session, not packed into its start: each
    # quarter of the clock holds a comparable share of the continuous events.
    open_ns, close_ns = refdata.session_bounds_ns("EQUITY", date)
    quarters = [0, 0, 0, 0]
    for e in events:
        if open_ns < e.exchange_ts < close_ns and e.event_type != EventType.STATUS:
            quarters[min(3, int(4 * (e.exchange_ts - open_ns) / (close_ns - open_ns)))] += 1
    assert min(quarters) > 0.15 * sum(quarters), quarters

    # the slot budget keeps its meaning: the event count is the legacy one
    # to within the dispersion of a self-exciting process, not a multiple.
    assert 0.8 * len(legacy) < len(events) < 1.25 * len(legacy), (len(events), len(legacy))
    assert all(validation_error(e) is None for e in events)


def test_legacy_budget_keeps_the_v1_3_0_flow_rule(refdata, tmp_path):
    """``flow.calibration = "legacy_budget"`` is the v1.3.0 generator: the
    slot budget is spent well before the close (rate margin 1.30, excitation
    uncalibrated), and its ``fill_session`` opt-in still removes the budget at
    that rate. tests/replay pins the full v1.3.0 dataset hashes."""
    date = refdata.trading_days[0]
    legacy = {"slots_per_stream": 400, "flow": {"calibration": "legacy_budget"}}
    budgeted, raw_budgeted = _eq_run(refdata, tmp_path, "off", legacy)
    _, raw_false = _eq_run(refdata, tmp_path, "false", dict(legacy, fill_session=False))
    filled, _ = _eq_run(refdata, tmp_path, "on", dict(legacy, fill_session=True))
    assert raw_budgeted == raw_false

    early = _last_continuous_fractions(refdata, budgeted, date)
    late = _last_continuous_fractions(refdata, filled, date)
    assert len(early) == len(late) == 22
    assert max(early) < 0.6, max(early)  # the v1.3.0 limitation
    assert min(late) > 0.98, min(late)
    assert len(filled) > 2 * len(budgeted)  # same rate, no budget: far more events


def test_flow_calibration_is_validated(refdata):
    with pytest.raises(ValueError, match="flow.calibration must be one of"):
        MarketDataGenerator(refdata, {"equities": {"flow": {"calibration": "budget"}}})
    # fill_session belongs to the legacy rule; under "session" it would be a
    # silent no-op on a config written for v1.3.0 semantics
    with pytest.raises(ValueError, match="fill_session"):
        MarketDataGenerator(refdata, {"equities": {"fill_session": True}})
    with pytest.raises(ValueError, match="excitation_decay"):
        MarketDataGenerator(refdata, {"equities": {"flow": {"excitation_decay": 1.0}}})


def test_excitation_time_factor_is_pinned_and_explains_the_legacy_stop():
    """E[1 / (1 + excitation)] for the pinned flow config. The v1.3.0 rule
    ran the base rate 1.30x too fast on top of it, so its budget was spent
    factor / 1.30 = 40 % of the way through the session — the observed stop."""
    factor = excitation_time_factor(1.4, 0.82, 8.0, 0.16)
    assert factor == pytest.approx(0.522, abs=0.003)
    assert factor / 1.30 == pytest.approx(0.405, abs=0.01)
    assert factor == excitation_time_factor(1.4, 0.82, 8.0, 0.16)  # deterministic
    assert excitation_time_factor(0.0, 0.82, 8.0, 0.16) == 1.0  # no excitation, no speed-up
    assert excitation_time_factor(0.5, 0.82, 8.0, 0.16) > factor  # weaker kick, slower flow


def test_generator_config_x_version_gate(tmp_path):
    """An x-version 1 document predates the calibration key: it must name
    the calibration it wants instead of silently getting the new default."""
    import json

    def write(doc):
        path = tmp_path / "generator.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    with pytest.raises(ValueError, match="x-version 1 generator config"):
        load_generator_config(write({"x-version": 1, "seed": 5}))
    legacy = load_generator_config(
        write({"x-version": 1, "equities": {"flow": {"calibration": "legacy_budget"}}})
    )
    assert legacy["equities"]["flow"]["calibration"] == "legacy_budget"
    assert legacy["equities"]["flow"]["excitation_kick"] == 1.4  # merged over the defaults
    current = load_generator_config(write({"x-version": 2, "seed": 5}))
    assert current["equities"]["flow"]["calibration"] == "session"
    with pytest.raises(ValueError, match="unsupported generator config x-version"):
        load_generator_config(write({"x-version": 3}))
