"""Feature-engine ingestion scenarios (API_FEATURES §2, round-3 RESEARCH).

Each test is a real-world event sequence with the behaviour pinned by the
contract:

- ``test_scenario_gateway_replay_after_reconnect`` — a venue reconnect
  replays messages (duplicate sequences) and interleaves traffic the book
  rejects; flow features must count the REAL events only.
- ``test_scenario_venue_disconnect_then_snapshot_recovery`` — a > 1 minute
  outage; on recovery the rolling windows reset, warmup restarts and no
  ratio blows up.
- ``test_scenario_one_sided_flicker_keeps_rvol_samples`` — an L1 cancel and
  re-add at a new price inside one millisecond still yields a vol sample.
- ``test_scenario_lp_withdraws_with_zero_price_quote`` — an FX LP sends a
  zero/negative price quote to withdraw; the engine survives, the book
  drops it, no NaN is ever valid.
- ``test_scenario_cross_venue_timestamp_regression`` — venue 2's clock is
  5 ms behind venue 1; the event is dropped + counted, never raised.
- ``test_scenario_session_timezone_and_dst`` — an equity venue across the
  DST changeover; sessions and profile buckets are session-local, and a
  session without an IANA zone fails at start-up.
"""

from __future__ import annotations

import datetime as dt
import json
import math

import pytest

from conftest import REPO_ROOT, mkev
from iap.core.events import EventType, SessionStatus
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine
from iap.features.registry import feature_index
from iap.features.rolling import SessionProfile

NS = 1_000_000_000
T0 = 1_787_578_200 * NS


@pytest.fixture(scope="module")
def contexts():
    return build_contexts(REPO_ROOT / "configs")


@pytest.fixture(scope="module")
def idx():
    return feature_index()


class _Feed:
    """Single-instrument event feed with explicit sequences per venue."""

    def __init__(self, engine, instrument_id=1, venue_id=1):
        self.engine = engine
        self.iid = instrument_id
        self.vid = venue_id
        self.seq = {}
        self.vec = None

    def send(self, event_type, ts, *, venue_id=None, seq=None, **kw):
        vid = self.vid if venue_id is None else venue_id
        if seq is None:
            seq = self.seq.get(vid, 0) + 1
        self.seq[vid] = max(self.seq.get(vid, 0), seq)
        self.vec = self.engine.apply(
            mkev(seq, event_type, instrument_id=self.iid, venue_id=vid, ts=ts,
                 **kw)
        )
        return self.vec


def _val(vec, idx, name):
    i = idx[name]
    return (vec.values[i], vec.validity[i])


def _warm_book(feed, t, oid=1):
    """A deep two-sided book so window features can warm up."""
    for k in range(5):
        feed.send(EventType.ADD, t, side=0, price=1000 - k, qty=100 + k,
                  order_id=oid + k)
        feed.send(EventType.ADD, t, side=1, price=1002 + k, qty=100 + k,
                  order_id=oid + 100 + k)
    return oid + 200


# ---------------------------------------------------------------------------
# 1. dropped events never reach rolling state
# ---------------------------------------------------------------------------


def test_scenario_gateway_replay_after_reconnect(contexts, idx):
    """Reconnect replay: duplicate TRADE, side=2 ADD, CANCEL while stale.

    Pinned (API_FEATURES §2): events the book drops contribute to NO rolling
    state.  One real 50-lot buy must show up once.
    """
    engine = FeatureEngine(contexts, cadence_ns=0)
    feed = _Feed(engine)
    t = T0
    _warm_book(feed, t)
    t += 2 * NS  # past the 1s window warmup

    feed.send(EventType.TRADE, t, side=0, price=1001, qty=50, trade_id=1)
    vec = feed.vec
    assert _val(vec, idx, "signed_volume_w1s_v1") == (50.0, True)
    assert _val(vec, idx, "trade_count_w1s_v1") == (1.0, True)

    # gateway replays the same message (duplicate sequence)
    dup_seq = feed.seq[1]
    feed.send(EventType.TRADE, t, seq=dup_seq, side=0, price=1001, qty=50,
              trade_id=1)
    vec = feed.vec
    assert engine.states[1].cons.books[1].duplicates_dropped == 1
    assert _val(vec, idx, "signed_volume_w1s_v1") == (50.0, True), (
        "a duplicate trade must not double-count signed volume")
    assert _val(vec, idx, "trade_count_w1s_v1") == (1.0, True)

    # malformed side on an ADD: book drops it, add_qty must not move
    add_qty_before = _val(vec, idx, "add_qty_w1s_v1")[0]
    feed.send(EventType.ADD, t, side=2, price=1000, qty=70, order_id=900)
    vec = feed.vec
    assert engine.states[1].cons.books[1].invalid_side_dropped == 1
    assert _val(vec, idx, "add_qty_w1s_v1") == (add_qty_before, True)

    # sequence gap -> venue stale; a CANCEL arriving while stale is dropped
    cancel_qty_before = _val(vec, idx, "cancel_qty_w1s_v1")[0]
    feed.send(EventType.ADD, t, seq=feed.seq[1] + 10, side=0, price=999,
              qty=10, order_id=901)
    feed.send(EventType.CANCEL, t, side=0, price=1000, qty=100, order_id=1)
    vec = feed.vec
    assert engine.states[1].cons.books[1].stale
    assert engine.states[1].cons.books[1].dropped_while_stale >= 1
    assert _val(vec, idx, "cancel_qty_w1s_v1") == (cancel_qty_before, True)
    # book features are invalid while the only venue is stale
    assert not vec.validity[idx["mid_price_v1"]]
    assert engine.events_dropped >= 3


# ---------------------------------------------------------------------------
# 2. stale recovery must not blow up ratios
# ---------------------------------------------------------------------------


def test_scenario_venue_disconnect_then_snapshot_recovery(contexts, idx):
    """LSE-style disconnect at t, snapshot recovery 120 s later 1 % higher.

    Pinned: rolling windows reset at the recovery, warmup restarts
    (``warmup_after_recovery``) and the vol-adjusted return / trend score /
    vol regime ratio are INVALID rather than 1e9-scaled.
    """
    engine = FeatureEngine(contexts, cadence_ns=0)
    feed = _Feed(engine)
    t = T0
    feed.send(EventType.ADD, t, side=0, price=1000, qty=100, order_id=1)
    feed.send(EventType.ADD, t, side=1, price=1002, qty=100, order_id=2)
    # 400 s of mid changes so every window is warm and rvol > 0
    oid = 10
    bid, ask = 1000, 1002
    for k in range(200):
        t += 2 * NS
        step = 1 if k % 2 == 0 else -1
        feed.send(EventType.ADD, t, side=0, price=bid + step, qty=100,
                  order_id=oid)
        feed.send(EventType.CANCEL, t, side=0, price=bid, qty=0,
                  order_id=oid - 1 if k else 1)
        bid += step
        oid += 1
    vec = feed.vec
    assert vec.validity[idx["rvol_w1m_v1"]] and vec.values[idx["rvol_w1m_v1"]] > 0
    assert vec.validity[idx["ret_vol_adj_10s_v1"]]

    # gap: 50 sequence numbers lost, then 120 s of silence
    t += 1 * NS
    feed.send(EventType.ADD, t, seq=feed.seq[1] + 50, side=0, price=1000,
              qty=10, order_id=oid + 50)
    assert engine.states[1].cons.books[1].stale
    assert not feed.vec.validity[idx["mid_price_v1"]]

    # recovery: complete SNAPSHOT burst 120 s later, 1 % higher
    t += 120 * NS
    seq = feed.seq[1] + 1
    new_bid, new_ask = 1010, 1012
    feed.send(EventType.SNAPSHOT, t, seq=seq, side=0, price=new_bid, qty=100,
              order_id=0, trade_id=3)
    feed.send(EventType.SNAPSHOT, t, seq=seq + 1, side=1, price=new_ask,
              qty=100, order_id=0, trade_id=2)
    feed.send(EventType.SNAPSHOT, t, seq=seq + 2, side=0, price=new_bid - 1,
              qty=90, order_id=0, trade_id=1)
    feed.send(EventType.SNAPSHOT, t, seq=seq + 3, side=1, price=new_ask + 1,
              qty=90, order_id=0, trade_id=0)
    vec = feed.vec
    st = engine.states[1]
    assert not st.cons.books[1].stale
    assert st.recoveries == 1 and st.recovered_ts == t and st.warm_ts == t
    assert st.book_ok
    # every window restarts its warmup: no rvol, no returns, no ratios
    for name in ("rvol_w10s_v1", "rvol_w1m_v1", "rvol_w5m_v1",
                 "ret_log_10s_v1", "ret_log_1m_v1", "ret_vol_adj_10s_v1",
                 "trend_score_w1m_v1", "vol_regime_ratio_v1",
                 "ofi_l1_w1s_v1", "signed_volume_w1m_v1"):
        assert not vec.validity[idx[name]], f"{name} valid right after recovery"
    # instantaneous book features are valid again immediately
    assert vec.validity[idx["mid_price_v1"]]
    assert vec.values[idx["mid_price_v1"]] == pytest.approx(
        (new_bid + new_ask) * 0.01 / 2.0)

    # 10 s later with one mid change: the 10s window is warm again and the
    # ratio is finite and small (it cannot see across the outage)
    t += 11 * NS
    feed.send(EventType.ADD, t, side=0, price=new_bid + 1, qty=50,
              order_id=oid + 999)
    vec = feed.vec
    assert vec.validity[idx["rvol_w10s_v1"]]
    rva = _val(vec, idx, "ret_vol_adj_10s_v1")
    assert (not rva[1]) or abs(rva[0]) < 1e3
    for x, ok in zip(vec.values, vec.validity):
        assert (not ok) or math.isfinite(x)


def test_stale_recovery_ratio_features_are_invalid_not_huge(contexts, idx):
    """EPS guard: rvol == 0 makes ret_vol_adj / trend_score INVALID."""
    engine = FeatureEngine(contexts, cadence_ns=0)
    feed = _Feed(engine)
    t = T0
    _warm_book(feed, t)
    # 70 s with NO mid change at all -> rvol windows warm but exactly zero
    for k in range(70):
        t += 1 * NS
        feed.send(EventType.ADD, t, side=0, price=990, qty=5, order_id=500 + k)
    vec = feed.vec
    assert _val(vec, idx, "rvol_w1m_v1") == (0.0, True)
    for name in ("ret_vol_adj_10s_v1", "ret_vol_adj_1m_v1",
                 "trend_score_w1m_v1", "vol_regime_ratio_v1",
                 "vol_ratio_w10s_w1m_v1", "alpha_decay_proxy_v1"):
        assert not vec.validity[idx[name]], f"{name} valid with rvol == 0"


def test_resiliency_halflife_invalid_without_replenishment(contexts, idx):
    """No observed L1 replenishment => the half-life is undefined, not 3e13."""
    engine = FeatureEngine(contexts, cadence_ns=0)
    feed = _Feed(engine)
    t = T0
    _warm_book(feed, t)
    for k in range(12):
        t += 1 * NS
        feed.send(EventType.MODIFY, t, side=0, price=0, qty=100, order_id=5)
    vec = feed.vec
    assert not vec.validity[idx["resiliency_halflife_v1"]]


# ---------------------------------------------------------------------------
# 3. one-sided flicker
# ---------------------------------------------------------------------------


def test_scenario_one_sided_flicker_keeps_rvol_samples(contexts, idx):
    """Cancelling the lone L1 bid and re-adding one tick lower inside a ms
    must still record the mid change in the vol windows."""
    engine = FeatureEngine(contexts, cadence_ns=0)
    feed = _Feed(engine)
    t = T0
    feed.send(EventType.ADD, t, side=0, price=1000, qty=100, order_id=1)
    feed.send(EventType.ADD, t, side=1, price=1002, qty=100, order_id=2)
    t += 11 * NS  # warm the 10s window
    feed.send(EventType.CANCEL, t, side=0, price=1000, qty=100, order_id=1)
    assert not feed.vec.validity[idx["mid_price_v1"]]  # one-sided
    t += 1_000  # 1 microsecond later
    feed.send(EventType.ADD, t, side=0, price=999, qty=100, order_id=3)
    vec = feed.vec
    st = engine.states[1]
    assert st.rv["10s"].count == 1, "the flicker mid change must be sampled"
    assert vec.validity[idx["rvol_w10s_v1"]]
    assert vec.values[idx["rvol_w10s_v1"]] > 0.0


def test_flicker_back_to_the_same_mid_records_nothing(contexts, idx):
    engine = FeatureEngine(contexts, cadence_ns=0)
    feed = _Feed(engine)
    t = T0
    feed.send(EventType.ADD, t, side=0, price=1000, qty=100, order_id=1)
    feed.send(EventType.ADD, t, side=1, price=1002, qty=100, order_id=2)
    t += 1
    feed.send(EventType.CANCEL, t, side=0, price=1000, qty=100, order_id=1)
    t += 1
    feed.send(EventType.ADD, t, side=0, price=1000, qty=80, order_id=3)
    st = engine.states[1]
    assert st.rv["10s"].count == 0
    assert len(st.hist2) == 1


# ---------------------------------------------------------------------------
# 4. malformed prices
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("price", [0, -5])
def test_scenario_lp_withdraws_with_zero_price_quote(contexts, idx, price):
    """An FX LP withdraws by quoting price 0 (or a negative price).

    The book drops the payload; the engine must neither raise nor produce a
    NaN with valid=True.
    """
    engine = FeatureEngine(contexts, cadence_ns=0)
    feed = _Feed(engine, instrument_id=101, venue_id=10)
    t = T0
    feed.send(EventType.QUOTE, t, side=0, price=110000, qty=1000, order_id=0)
    feed.send(EventType.QUOTE, t, side=1, price=110002, qty=1000, order_id=0)
    assert feed.vec.validity[idx["mid_price_v1"]]
    t += NS
    feed.send(EventType.QUOTE, t, side=0, price=price, qty=1000, order_id=0)
    vec = feed.vec
    book = engine.states[101].cons.books[10]
    assert book.invalid_payload_dropped == 1
    assert engine.events_dropped == 1
    # the withdrawal was rejected: the previous quote still prevails
    assert vec.validity[idx["mid_price_v1"]]
    for x, ok in zip(vec.values, vec.validity):
        assert (not ok) or math.isfinite(x)


def test_zero_qty_quote_is_dropped_not_applied(contexts, idx):
    engine = FeatureEngine(contexts, cadence_ns=0)
    feed = _Feed(engine, instrument_id=101, venue_id=10)
    t = T0
    feed.send(EventType.QUOTE, t, side=0, price=110000, qty=1000, order_id=0)
    feed.send(EventType.QUOTE, t, side=1, price=110002, qty=1000, order_id=0)
    t += NS
    feed.send(EventType.QUOTE, t, side=1, price=110004, qty=0, order_id=0)
    assert engine.states[101].cons.books[10].invalid_payload_dropped == 1
    assert feed.vec.values[idx["depth_ask_l1_v1"]] == 1000.0


# ---------------------------------------------------------------------------
# 5. cross-venue timestamp regression
# ---------------------------------------------------------------------------


def test_scenario_cross_venue_timestamp_regression(contexts, idx):
    """Venue 2's gateway clock runs 5 ms behind venue 1's.

    Pinned: the late event is dropped + counted before the book sees it; the
    engine never raises and the at-or-before history is unchanged.
    """
    engine = FeatureEngine(contexts, cadence_ns=0)
    feed = _Feed(engine)
    t = T0
    feed.send(EventType.ADD, t, venue_id=1, side=0, price=1000, qty=100,
              order_id=1)
    feed.send(EventType.ADD, t, venue_id=1, side=1, price=1002, qty=100,
              order_id=2)
    hist_before = len(engine.states[1].hist2)
    mid_before = feed.vec.values[idx["mid_price_v1"]]

    vec = feed.send(EventType.ADD, t - 5_000_000, venue_id=2, side=0,
                    price=1001, qty=100, order_id=3)
    assert vec is None, "a ts regression emits no vector"
    st = engine.states[1]
    assert st.ts_regressions_dropped == 1 and engine.ts_regressions_dropped == 1
    assert 2 not in st.cons.books, "the book must never see the regressed event"
    assert len(st.hist2) == hist_before

    # the stream continues normally afterwards
    t += NS
    feed.send(EventType.ADD, t, venue_id=2, side=0, price=1001, qty=100,
              order_id=4)
    vec = feed.vec
    assert vec.validity[idx["mid_price_v1"]]
    assert vec.values[idx["mid_price_v1"]] > mid_before


def test_ts_regression_counter_is_reported_in_run_summary(contexts):
    engine = FeatureEngine(contexts, cadence_ns=0)
    events = [
        mkev(1, EventType.ADD, side=0, price=1000, qty=100, order_id=1,
             ts=T0 + NS),
        mkev(2, EventType.ADD, side=1, price=1002, qty=100, order_id=2,
             ts=T0 + NS),
        mkev(3, EventType.ADD, side=0, price=1001, qty=10, order_id=3, ts=T0),
    ]
    summary = engine.run(events)
    assert summary["ts_regressions_dropped"] == 1
    assert summary["events_dropped"] == 1
    assert summary["events_processed"] == 3


# ---------------------------------------------------------------------------
# halt / auction flags survive the ingestion rules
# ---------------------------------------------------------------------------


def test_scenario_halt_then_reopen_auction_flags(contexts, idx):
    engine = FeatureEngine(contexts, cadence_ns=0)
    feed = _Feed(engine)
    t = T0
    _warm_book(feed, t)
    t += NS
    feed.send(EventType.STATUS, t, qty=int(SessionStatus.HALT))
    assert feed.vec.values[idx["is_halt_v1"]] == 1.0
    t += 300 * NS
    feed.send(EventType.STATUS, t, qty=int(SessionStatus.AUCTION))
    assert feed.vec.values[idx["is_auction_v1"]] == 1.0
    t += 30 * NS
    feed.send(EventType.STATUS, t, qty=int(SessionStatus.TRADING))
    assert feed.vec.values[idx["is_trading_v1"]] == 1.0


# ---------------------------------------------------------------------------
# 12. session time zones and DST (API_FEATURES §3)
# ---------------------------------------------------------------------------


def _tz_config(tmp_path, tzname="America/New_York", open_="09:30:00",
               close="16:00:00"):
    """A minimal instruments.json with one equity session in ``tzname``."""
    cfg = {
        "sessions": {
            "EQUITY": {"timezone": tzname, "open": open_, "close": close},
        },
        "instruments": [
            {"instrument_id": 1, "symbol": "SYN.EQ.001",
             "asset_class": "EQUITY", "tick_size": 0.01},
        ],
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "instruments.json").write_text(json.dumps(cfg))
    return tmp_path


def _utc_ns(y, mo, d, h, mi):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc)
               .timestamp()) * NS


def test_scenario_session_timezone_and_dst(tmp_path):
    """NYSE across the 2026-11-01 DST end: the session moves with the venue.

    Pinned (API_FEATURES §3): ``open``/``close`` are wall-clock times in the
    session's IANA zone, and every time-of-day feature is keyed in
    SESSION-LOCAL time.  Round 2 read them as UTC minutes, so for the five
    winter months `is_open_phase_v1` fired an hour early and every
    `norm_*_m5` profile compared 09:30 ET against the bucket that had held
    10:30 ET volume.
    """
    ctxs = build_contexts(_tz_config(tmp_path))
    ctx = ctxs[1]
    assert ctx.session_timezone == "America/New_York"
    # 09:30 local is minute 570 of the SESSION-LOCAL day, in both DST states
    assert ctx.session_open_min == 9 * 60 + 30
    assert ctx.session_close_min == 16 * 60

    # 2026-10-30 is EDT (UTC-4): the open is 13:30 UTC
    edt_open = _utc_ns(2026, 10, 30, 13, 30)
    # 2026-11-02 is EST (UTC-5): the SAME local open is 14:30 UTC
    est_open = _utc_ns(2026, 11, 2, 14, 30)
    assert ctx.clock.offset_seconds(edt_open) == -4 * 3600
    assert ctx.clock.offset_seconds(est_open) == -5 * 3600
    # ... and both land on the same session-local minute
    assert ctx.clock.local_second_of_day(edt_open) == 9 * 3600 + 30 * 60
    assert ctx.clock.local_second_of_day(est_open) == 9 * 3600 + 30 * 60

    # the 5-minute-of-day profile bucket is therefore identical across the
    # changeover: 09:30 ET volume is always compared against 09:30 ET volume
    b_edt = SessionProfile.bucket_of(edt_open, ctx.clock.offset_seconds(edt_open))
    b_est = SessionProfile.bucket_of(est_open, ctx.clock.offset_seconds(est_open))
    assert b_edt == b_est == (9 * 3600 + 30 * 60) // 300

    # the naive-UTC reading is what round 2 did, and it is an hour out in EST
    assert SessionProfile.bucket_of(est_open) != b_est


def test_session_timezone_features_are_session_local(tmp_path, idx):
    """`minute_of_day` / `session_frac` / `is_open_phase` on both DST sides."""
    ctxs = build_contexts(_tz_config(tmp_path))
    for ts in (_utc_ns(2026, 10, 30, 13, 30), _utc_ns(2026, 11, 2, 14, 30)):
        engine = FeatureEngine(ctxs, cadence_ns=0)
        feed = _Feed(engine, instrument_id=1)
        _warm_book(feed, ts)
        vec = feed.vec
        assert _val(vec, idx, "minute_of_day_v1")[0] == pytest.approx(570.0, abs=1.0)
        assert _val(vec, idx, "is_open_phase_v1") == (1.0, True)
        assert _val(vec, idx, "is_close_phase_v1") == (0.0, True)
        assert _val(vec, idx, "session_frac_v1")[0] == pytest.approx(0.0, abs=0.01)


def test_missing_or_unknown_session_timezone_fails_fast(tmp_path):
    """A session without an IANA zone is a start-up error, never UTC."""
    d = _tz_config(tmp_path / "a")
    (tmp_path / "a" / "instruments.json").write_text(json.dumps({
        "sessions": {"EQUITY": {"open": "09:30:00", "close": "16:00:00"}},
        "instruments": [{"instrument_id": 1, "symbol": "S", "asset_class":
                         "EQUITY", "tick_size": 0.01}],
    }))
    with pytest.raises(ValueError, match="timezone"):
        build_contexts(d)

    d2 = _tz_config(tmp_path / "b", tzname="Mars/Olympus_Mons")
    with pytest.raises(ValueError, match="unknown session timezone"):
        build_contexts(d2)
