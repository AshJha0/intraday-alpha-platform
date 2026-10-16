"""v1.17 B8: two-venue SOR on (synthetic) two-venue books — the merged
stream and its clock alignment, per-venue calibration, the paired
router-vs-pinned benchmark policies, determinism. Synthetic data only: the
golden MBO stream is replayed once as the "Nasdaq" book (XNAS 101) and once,
one tick higher and re-keyed, as the "IEX" book (IEXG 102)."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from iap.core.codec import read_jsonl, write_jsonl
from iap.core.events import EventType, MarketEvent
from iap.execution import ExecutionReplay
from iap.execution.fill_hazard import IEX_TAI_OFFSET_S
from iap.execution.types import VenueSpec
from iap.research import execution_benchmark as bench
from iap.research import two_venue as tv
from test_execution_golden import golden_config, golden_parents

REPO = Path(__file__).resolve().parents[2]
SEC = 1_000_000_000
NAS_IID = 1
IEX_IID = 7  # the IEX dataset numbers the same symbol differently
SYMBOL = "AAA"


def _ev(ev: MarketEvent, **kw) -> MarketEvent:
    d = ev.to_dict()
    d.update(kw)
    return MarketEvent(**d)


def nasdaq_stream(golden_dir: Path) -> list[MarketEvent]:
    return [_ev(e, venue_id=101) for e in read_jsonl(golden_dir / "events_eq_mbo.jsonl")]


def iex_stream(golden_dir: Path, shift_ns: int = 0) -> list[MarketEvent]:
    out = []
    for e in read_jsonl(golden_dir / "events_eq_mbo.jsonl"):
        px = e.price_ticks + 1 if e.price_ticks > 0 else 0
        out.append(
            _ev(
                e,
                venue_id=102,
                instrument_id=IEX_IID,
                price_ticks=px,
                exchange_ts=e.exchange_ts + 3_000 + shift_ns,
                receive_ts=e.receive_ts + 3_000 + shift_ns,
            )
        )
    return out


def _dataset(root: Path, events, iid: int, venue: tuple[str, int, float, float]) -> Path:
    shutil.copytree(REPO / "configs", root / "configs")
    inst = json.loads((REPO / "configs/instruments/instruments.json").read_text())
    row = dict(next(r for r in inst["instruments"] if r["instrument_id"] == 1))
    row.update({"instrument_id": iid, "symbol": SYMBOL})
    inst["instruments"] = [row]
    (root / "configs/instruments/instruments.json").write_text(json.dumps(inst))
    name, vid, taker, rebate = venue
    ven = {
        "x-version": 1,
        "description": "B8 test venue",
        "venues": [
            {
                "venue": name,
                "venue_id": vid,
                "asset_class": "EQUITY",
                "taker_fee_per_share": taker,
                "maker_rebate_per_share": rebate,
                "latency": {"mean_ns": 0, "jitter_ns": 0},
                "supports": ["MBO", "AUCTION", "HALT"],
            }
        ],
    }
    (root / "configs/venues/venues.json").write_text(json.dumps(ven))
    (root / "normalized").mkdir(parents=True)
    write_jsonl(root / "normalized" / "eq_20251208.normalized.jsonl", events)
    return root


@pytest.fixture(scope="module")
def pair(tmp_path_factory, golden_dir) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("b8")
    nas = _dataset(
        root / "r7_20251208", nasdaq_stream(golden_dir), NAS_IID, ("XNAS", 101, 0.003, 0.002)
    )
    iex = _dataset(
        root / "iex_20251208", iex_stream(golden_dir), IEX_IID, ("IEXG", 102, 0.0009, 0.0)
    )
    return nas, iex


# ------------------------------------------------------------- alignment --


def test_merge_orders_by_exchange_ts_then_venue_and_keeps_each_stream(golden_dir):
    a = nasdaq_stream(golden_dir)
    b = iex_stream(golden_dir)
    cnt: dict[str, int] = {}
    m = tv.merge_events(a, b, id_map={IEX_IID: NAS_IID}, counters=cnt)
    assert cnt == {"primary": len(a), "second": len(b), "second_unmapped": 0, "dropped_by_keep": 0}
    assert len(m) == len(a) + len(b)
    assert [e.event_id for e in m] == list(range(1, len(m) + 1))
    keys = [(e.exchange_ts, e.venue_id) for e in m]
    assert keys == sorted(keys)
    assert {e.instrument_id for e in m} == {NAS_IID}
    # each venue's own order (sequence numbers) is untouched
    for vid, src in ((101, a), (102, b)):
        assert [e.sequence for e in m if e.venue_id == vid] == [e.sequence for e in src]


def test_tie_breaks_to_the_lower_venue_id():
    def ev(vid, seq):
        return MarketEvent(seq, 1, vid, 1000, 1000, seq, int(EventType.HEARTBEAT), 0, 0, 0, 0, 0)

    m = tv.merge_events([ev(102, 1), ev(102, 2)], [ev(101, 1)])
    assert [(e.venue_id, e.sequence) for e in m] == [(101, 1), (102, 1), (102, 2)]


def test_capture_clock_is_shifted_by_the_tai_offset(golden_dir):
    assert IEX_TAI_OFFSET_S == 37
    assert tv.clock_shift_ns("exchange") == 0
    assert tv.clock_shift_ns("capture") == -37 * SEC
    with pytest.raises(ValueError):
        tv.clock_shift_ns("tai")
    ref = tv.merge_events(
        nasdaq_stream(golden_dir), iex_stream(golden_dir), id_map={IEX_IID: NAS_IID}
    )
    tai = tv.merge_events(
        nasdaq_stream(golden_dir),
        iex_stream(golden_dir, shift_ns=37 * SEC),
        id_map={IEX_IID: NAS_IID},
        iex_clock="capture",
    )
    assert [e.to_dict() for e in tai] == [e.to_dict() for e in ref]
    # without the shift the IEX book would trail Nasdaq by 37 s
    raw = tv.merge_events(
        nasdaq_stream(golden_dir),
        iex_stream(golden_dir, shift_ns=37 * SEC),
        id_map={IEX_IID: NAS_IID},
    )
    assert [e.to_dict() for e in raw] != [e.to_dict() for e in ref]


def test_unmapped_second_stream_symbols_are_dropped_and_counted(golden_dir):
    cnt: dict[str, int] = {}
    m = tv.merge_events(nasdaq_stream(golden_dir), iex_stream(golden_dir), id_map={}, counters=cnt)
    assert cnt["second"] == 0 and cnt["second_unmapped"] == 2000
    assert {e.venue_id for e in m} == {101}


def test_instrument_map_is_by_symbol():
    assert tv.instrument_map({"AAPL": 1, "MSFT": 2}, {"MSFT": 9, "QQQ": 3, "AAPL": 4}) == {
        4: 1,
        9: 2,
    }


def test_load_merged_session_reads_both_datasets(pair):
    nas, iex = pair
    m = tv.load_merged_session(nas, iex, "20251208")
    assert {e.venue_id for e in m} == {101, 102} and {e.instrument_id for e in m} == {NAS_IID}


def test_missing_normalized_events_say_b8_needs_them(tmp_path):
    with pytest.raises(SystemExit, match="BOTH venues"):
        tv.session_file(tmp_path)


# ------------------------------------------------------- the router hook --


def test_router_none_leaves_the_golden_replay_unchanged(golden_dir):
    events = read_jsonl(golden_dir / "events_eq_mbo.jsonl")
    cfg = golden_config(golden_dir.parents[1] / "configs")
    parents = golden_parents(events[0].exchange_ts)
    a = ExecutionReplay(cfg, parents).run(events)
    b = ExecutionReplay(cfg, parents, router=None, router_fill_prob=None).run(events)
    assert [f.to_dict() for f in a.fills] == [f.to_dict() for f in b.fills]


def test_sor_v2_router_routes_unpinned_children_to_the_cheaper_book(golden_dir):
    """Buys: XNAS asks are one tick lower than IEXG's, so an aggressive
    child routed by sor_v2 goes to XNAS; a pinned parent stays pinned."""
    cfg = golden_config(golden_dir.parents[1] / "configs")
    venues = {
        101: VenueSpec(101, "XNAS", taker_fee_per_share=0.003, maker_rebate_per_share=0.002),
        102: VenueSpec(102, "IEXG", taker_fee_per_share=0.0009),
    }
    cfg = replace(cfg, venues=venues)
    events = tv.merge_events(
        nasdaq_stream(golden_dir), iex_stream(golden_dir), id_map={IEX_IID: NAS_IID}
    )
    router, _ = tv.router_from_calibrations(venues, None)
    parents = [
        replace(p, venue_id=0)
        for p in golden_parents(events[0].exchange_ts)
        if p.side == 0 and p.instrument_id == 1
    ]
    assert parents
    res = ExecutionReplay(cfg, parents, router=router).run(events)
    assert res.fills and {f.venue_id for f in res.fills} <= {101, 102}
    pinned = [replace(p, venue_id=102) for p in parents]
    res2 = ExecutionReplay(cfg, pinned, router=router).run(events)
    assert {f.venue_id for f in res2.fills} == {102}


# ------------------------------------------------------------ calibration --


def test_per_venue_calibration_documents(pair, tmp_path):
    nas, iex = pair
    rc = tv.main(
        [
            "calibrate",
            "--nasdaq-dir",
            str(nas),
            "--iex-dir",
            str(iex),
            "--out",
            str(tmp_path),
        ]
    )
    assert rc == 0
    docs = tv.load_calibrations(tmp_path)
    assert sorted(docs) == [101, 102]
    for vid, doc in docs.items():
        assert doc["schema"] == tv.SCHEMA and doc["venue"] == {101: "XNAS", 102: "IEXG"}[vid]
        fp = doc["fill_probability"]
        assert "X-4" in fp["model"]
        cells = [c for s in ("bid", "ask") for c in fp["queue_depletion"][s].values()]
        assert cells and all(
            c["exec_depletions"] + c["cancel_depletions"] <= c["depletions"] for c in cells
        )
        assert fp["p_fill_touch"] is None or 0.0 <= fp["p_fill_touch"] <= 1.0
        tox = doc["toxicity"]
        assert tox["n_fills"] > 0
        for h in tv.DEFAULT_TOX_HORIZONS:
            st = tox["markout_bps"][h]
            assert st["n_clusters"] == 1 and st["se"] is None  # one day: no SE
            if st["mean"] is not None:
                assert tox["toxicity_bps"][h] == pytest.approx(-st["mean"])
    router, fp = tv.router_from_calibrations(
        {
            d["venue_id"]: VenueSpec(d["venue_id"], d["venue"], taker_fee_per_share=0.003)
            for d in docs.values()
        },
        docs,
    )
    assert sorted(router.models) == [101, 102] and fp is not None
    p = fp(101, 0, 100)
    assert p is None or 0.0 <= p <= 1.0


def test_toxicity_is_day_clustered_across_days(golden_dir):
    evs = tv.merge_events(
        nasdaq_stream(golden_dir), iex_stream(golden_dir), id_map={IEX_IID: NAS_IID}
    )
    ticks = {NAS_IID: 0.01}
    days = [tv.calibrate_day(evs, ticks, label=lab) for lab in ("d1", "d2")]
    docs = tv.combine_days(days, {})
    for doc in docs.values():
        st = doc["toxicity"]["markout_bps"]["1s"]
        if st["mean"] is not None:
            assert st["n_clusters"] == 2
        one = tv.combine_days(days[:1], {})[doc["venue_id"]]
        # pooled exposure doubles, the rate is unchanged
        assert doc["fill_probability"]["exec_hazard_per_s"] == pytest.approx(
            one["fill_probability"]["exec_hazard_per_s"]
        )


# ------------------------------------------------------- paired benchmark --


ARGS = [
    "--sizes-pct",
    "0.0005,0.002",
    "--algos",
    "TWAP,POV",
    "--window-min",
    "1.5",
    "--warmup-min",
    "0.5",
    "--no-timestamp",
    "--policies",
    "pin_nasdaq,pin_iex,sor",
    "--baseline-policy",
    "pin_nasdaq",
]


def _run(pair, out: Path, extra=()) -> dict:
    nas, iex = pair
    rc = bench.main(
        ["--data-dir", str(nas), "--second-data-dir", str(iex), "--out", str(out), "--tag", "b8"]
        + ARGS
        + list(extra)
    )
    assert rc == 0
    return json.loads((out / "benchmark_b8.json").read_text())


def test_paired_router_vs_pinned(pair, tmp_path):
    doc = _run(pair, tmp_path)
    t = doc["two_venue"]
    assert t["routes"] == {"pin_iex": "IEXG", "pin_nasdaq": "XNAS", "sor": None}
    assert t["iex_clock"] == "exchange" and t["unmatched"] == []
    assert t["merge_counts"]["20251208"]["second"] == 2000
    sp = t["venue_split"]
    assert set(sp["pin_nasdaq"]["venue_qty"]) <= {"XNAS"}
    assert set(sp["pin_iex"]["venue_qty"]) <= {"IEXG"}
    assert set(sp["sor"]["venue_qty"]) == {"XNAS", "IEXG"}  # buys XNAS, sells IEXG
    # the same parents and windows under every route
    n = doc["n_parents"]
    assert n["sor"] == n["pin_nasdaq"] == n["pin_iex"] > 0
    assert any(k.startswith("sor-pin_nasdaq|") for k in doc["paired_vs_baseline"])
    assert any(k.startswith("pin_iex-pin_nasdaq|") for k in doc["paired_vs_baseline"])
    md = (tmp_path / "BENCHMARK_b8.md").read_text(encoding="utf-8")
    assert "Two venues: router vs pinned (v1.17 B8)" in md


def test_sor_sweep_pairs_with_pinned(pair, tmp_path):
    """v1.17.1: ``sor_sweep`` (plan_sweep across both books) runs on the
    same parents as the pinned and single-venue routes."""
    doc = _run(pair, tmp_path, ["--policies", "pin_nasdaq,sor,sor_sweep"])
    t = doc["two_venue"]
    assert t["routes"] == {"pin_nasdaq": "XNAS", "sor": None, "sor_sweep": None}
    n = doc["n_parents"]
    assert n["sor_sweep"] == n["sor"] == n["pin_nasdaq"] > 0
    assert any(k.startswith("sor_sweep-pin_nasdaq|") for k in doc["paired_vs_baseline"])
    assert set(t["venue_split"]["sor_sweep"]["venue_qty"]) <= {"XNAS", "IEXG"}


def test_paired_benchmark_is_deterministic(pair, tmp_path):
    a = _run(pair, tmp_path / "a")
    b = _run(pair, tmp_path / "b")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_calibrated_router_is_recorded(pair, tmp_path):
    nas, iex = pair
    cal = tmp_path / "cal"
    tv.main(["calibrate", "--nasdaq-dir", str(nas), "--iex-dir", str(iex), "--out", str(cal)])
    doc = _run(pair, tmp_path / "o", ["--venue-calibration", str(cal)])
    rm = doc["two_venue"]["router_models"]
    assert sorted(rm) == ["101", "102"]
    assert doc["two_venue"]["calibration"] == str(cal)


def test_pinning_to_an_absent_venue_is_refused(pair, tmp_path):
    nas, _ = pair
    with pytest.raises(SystemExit, match="IEXG"):
        bench.run_benchmark(nas, policies=("pin_nasdaq", "pin_iex"), baseline_policy="pin_nasdaq")


def test_route_policy_names():
    assert bench.split_route("sor") == ("native", "sor")
    assert bench.split_route("passive@pin_iex+dark") == ("passive+dark", "pin_iex")
    assert bench.split_route("native+urgency") == ("native+urgency", None)
    assert bench.parse_policy("passive@sor") == ("passive", None)
    assert bench.control_policy("sor+dark") == "sor"
    assert bench.control_policy("passive@sor+dark") == "passive@sor"
    with pytest.raises(SystemExit):
        bench.split_route("passive@nowhere")


def test_two_day_pairs_give_day_clustered_ses(pair, tmp_path, golden_dir):
    """A second overlap day (its own dataset pair, different instrument
    ids) is re-keyed by symbol and adds a cluster: SEs exist."""
    nas, iex = pair
    n2 = _dataset(
        tmp_path / "r7_20251209", nasdaq_stream(golden_dir), 3, ("XNAS", 101, 0.003, 0.002)
    )
    x2 = _dataset(tmp_path / "iex_20251209", iex_stream(golden_dir), 9, ("IEXG", 102, 0.0009, 0.0))
    for d in (n2, x2):
        src = d / "normalized" / "eq_20251208.normalized.jsonl"
        evs = read_jsonl(src)
        src.unlink()
        iid = 3 if d is n2 else 9
        write_jsonl(
            d / "normalized" / "eq_20251209.normalized.jsonl",
            [_ev(e, instrument_id=iid) for e in evs],
        )
    doc = _run(pair, tmp_path / "o", ["--pair-dir", str(n2), str(x2)])
    assert doc["sessions"] == ["20251208", "20251209"]
    assert doc["two_venue"]["merge_counts"]["20251209"]["second"] == 2000
    cell = next(v for k, v in doc["cells"].items() if k.startswith("sor|"))
    assert cell["n_sessions"] == 2 and cell["costs_bps"]["all_in"]["se"] is not None
    pv = next(v for k, v in doc["paired_vs_baseline"].items() if k.startswith("sor-pin_nasdaq|"))
    assert pv["n_pairs"] > 0


def test_keep_drops_other_instruments_of_both_streams(golden_dir):
    cnt: dict[str, int] = {}
    m = tv.merge_events(
        nasdaq_stream(golden_dir),
        iex_stream(golden_dir),
        id_map={IEX_IID: NAS_IID},
        keep={99},
        counters=cnt,
    )
    assert m == [] and cnt["dropped_by_keep"] == 4000


def test_symbols_stream_path_gives_the_same_report(pair, tmp_path):
    """--symbols in two-venue mode streams the files with the filter; the
    report equals the unfiltered one when the filter keeps everything."""
    a = _run(pair, tmp_path / "a")
    b = _run(pair, tmp_path / "b", ["--symbols", SYMBOL])
    for k in ("cells", "paired_vs_baseline", "n_events"):
        assert a[k] == b[k]
    c = tv.read_events(tv.session_file(pair[0]), {99})
    assert c == []


def test_pooled_paired_summary_and_rows(pair, tmp_path):
    doc = _run(pair, tmp_path)
    t = doc["two_venue"]
    assert set(t["paired_pooled"]) == {"pin_iex-pin_nasdaq", "sor-pin_nasdaq"}
    n = doc["n_parents"]["sor"]
    assert t["paired_pooled"]["sor-pin_nasdaq"]["n_pairs"] == n
    rows = t["paired_rows"]["sor-pin_nasdaq"]
    assert len(rows) == n and {"all_in", "shortfall"} <= set(rows[0]["costs_bps_diff"])
