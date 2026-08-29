#!/usr/bin/env python3
"""(Re)generate tests/golden/expected_features.json.

Run from python/ with PYTHONPATH=src:

    PYTHONPATH=src python3 tools/make_golden_features.py

Runs the FeatureEngine (cadence = every event) over the pinned golden
vectors and captures a representative subset of feature values (covering
every family) at the pinned checkpoints:

    events_eq_mbo.jsonl   -> after events 500 / 1000 / 1500 / 2000
    events_fx_quote.jsonl -> after events 400 / 800

BEFORE writing, several features are re-derived by independent brute-force
pandas computations straight from the event stream (reference OrderBook for
book state, pandas rolling/asof for windows) and compared at 1e-9 abs/rel —
the file is only written when every cross-check agrees.

Golden values are pinned: regenerate ONLY on a deliberate, versioned change
(schemas/MIGRATIONS.md); every other language must match these numbers.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python" / "src"))

from iap.core.codec import read_jsonl  # noqa: E402
from iap.core.events import EventType  # noqa: E402
from iap.features.context import build_contexts  # noqa: E402
from iap.features.engine import FeatureEngine  # noqa: E402
from iap.features.registry import build_registry, registry_hash  # noqa: E402
from iap.orderbook.book import OrderBook  # noqa: E402

GOLDEN_DIR = REPO / "tests" / "golden"
EQ_CHECKPOINTS = (500, 1000, 1500, 2000)  # 1-based event indices
FX_CHECKPOINTS = (400, 800)
TOL = 1e-9

#: Representative features — at least one per family, weighted toward the
#: cross-language core set in API_FEATURES.md.
REPRESENTATIVE = [
    # price
    "ret_simple_5s_v1", "ret_log_1m_v1", "ret_accel_10s_v1",
    "mid_change_ticks_1s_v1", "ret_vol_adj_30s_v1",
    # micro
    "mid_price_v1", "microprice_v1", "micro_mid_dev_bps_v1", "spread_ticks_v1",
    "spread_bps_v1", "imbalance_l1_v1", "imbalance_l5_v1", "depth_bid_l1_v1",
    "depth_ask_l10_v1", "depth_bid_l5_avg_w10s_v1",
    "queue_depletion_rate_bid_w10s_v1",
    # flow
    "ofi_l1_w1s_v1", "ofi_l5_w5s_v1", "ofi_l10_w30s_v1",
    "signed_volume_w1m_v1", "trade_imbalance_w10s_v1",
    "cancel_intensity_w10s_v1", "add_intensity_w1s_v1",
    # liquidity
    "quoted_depth_total_v1", "effective_spread_bps_w1m_v1",
    "participation_w1m_v1", "resiliency_halflife_v1",
    # vol
    "rvol_w1m_v1", "range_bps_w1m_v1", "vol_ratio_w10s_w1m_v1",
    # tod
    "minute_of_day_v1", "session_frac_v1", "norm_spread_m5_v1",
    # xasset
    "ref_ret_1s_v1", "ref_ret_1m_v1", "corr_contemp_w5m_v1",
    # venue
    "venue_count_active_v1", "venue_depth_hhi_v1",
    "venue_imbalance_divergence_v1", "venue_update_share_top_w10s_v1",
    # regime
    "trend_score_w1m_v1", "meanrev_score_w1m_v1", "vol_regime_ratio_v1",
    # exec
    "half_spread_cost_bps_v1", "fill_prob_bid_h1s_v1",
    "alpha_decay_proxy_v1", "urgency_score_v1",
]


def close(a: float, b: float) -> bool:
    return abs(a - b) <= TOL + TOL * abs(b)


def run_engine(events, checkpoints):
    """Run the engine (emit every event); return {index: FeatureVector}."""
    engine = FeatureEngine(build_contexts(REPO / "configs"), cadence_ns=0)
    out = {}
    for i, ev in enumerate(events, start=1):
        vec = engine.apply(ev)
        if i in checkpoints:
            out[i] = vec
    return engine, out


def brute_force_frames(events, tick: float):
    """Independent per-event L1/trade/mid-sample frames from a reference
    OrderBook.

    The mid-sample frame mirrors the PINNED sampling semantics: a sample is
    recorded at every two-sided refresh where the mid changed (or the book
    just became two-sided again), and the log-mid return ``dlm`` of a sample
    is only defined when the PREVIOUS book-touching refresh was two-sided —
    the return chain breaks across one-sided periods (no dlm bridges them).
    """
    book = OrderBook(events[0].instrument_id, 0)
    rows, trows, srows = [], [], []
    prev = None
    hist_mid2 = None      # last recorded mid-sample value
    last_ok_mid2 = None   # mid2 at the last two-sided refresh
    prev_touch_ok = False  # was the previous book-touching refresh two-sided?
    for n, ev in enumerate(events, start=1):
        book.apply(ev)
        et = ev.event_type
        if et == EventType.TRADE:
            trows.append({"ts": ev.exchange_ts, "n": n,
                          "signed": ev.qty if ev.side == 0 else -ev.qty})
        touch = et in (EventType.ADD, EventType.MODIFY, EventType.CANCEL,
                       EventType.EXECUTE, EventType.QUOTE) or (
            et == EventType.SNAPSHOT and ev.trade_id == 0)
        if not touch:
            continue
        bb, ba = book.best_bid(), book.best_ask()
        row = {"ts": ev.exchange_ts, "n": n,
               "bp": bb[0] if bb else None, "bq": bb[1] if bb else None,
               "ap": ba[0] if ba else None, "aq": ba[1] if ba else None}
        ok = bb is not None and ba is not None
        if ok:
            mid2 = row["bp"] + row["ap"]
            if not prev_touch_ok or mid2 != last_ok_mid2:
                dlm = (math.log(mid2) - math.log(hist_mid2)
                       if prev_touch_ok and hist_mid2 is not None else None)
                srows.append({"ts": ev.exchange_ts, "n": n,
                              "mid2": mid2, "dlm": dlm})
                hist_mid2 = mid2
            last_ok_mid2 = mid2
        prev_touch_ok = ok
        # L1 OFI contribution (pinned formula, level union of prev/curr best)
        e = 0
        if prev is not None:
            for (pp, pq, cp, cq, sign) in (
                (prev["bp"], prev["bq"], row["bp"], row["bq"], 1),
                (prev["ap"], prev["aq"], row["ap"], row["aq"], -1),
            ):
                pmap = {pp: pq} if pp is not None else {}
                cmap = {cp: cq} if cp is not None else {}
                d = 0
                for p in set(pmap) | set(cmap):
                    d += cmap.get(p, 0) - pmap.get(p, 0)
                e += sign * d
        row["ofi1"] = e if prev is not None else 0
        row["has_ofi"] = prev is not None
        rows.append(row)
        prev = row
    return pd.DataFrame(rows), pd.DataFrame(trows), pd.DataFrame(srows)


def validate_eq(events, vecs, names_idx, tick):
    """Brute-force cross-checks on the EQ golden vector (abort on mismatch)."""
    df, trades, chg = brute_force_frames(events, tick)
    both = df.dropna(subset=["bp", "ap"]).copy()
    both["mid2"] = both.bp + both.ap
    both["mid"] = both.mid2 * tick / 2
    first_ts = events[0].exchange_ts

    failures = []
    for idx, vec in vecs.items():
        t = vec.timestamp

        def engine_val(name):
            return vec.values[names_idx[name]], vec.validity[names_idx[name]]

        # spread_bps / imbalance_l1 / microprice from the raw book state
        # (n-filters drop later events sharing the checkpoint's exchange_ts)
        state = both[(both.ts <= t) & (both.n <= idx)].iloc[-1]
        mid = state.mid
        checks = {
            "spread_bps_v1": (state.ap - state.bp) * tick / mid * 1e4,
            "imbalance_l1_v1": (state.bq - state.aq) / (state.bq + state.aq),
            "microprice_v1": (state.bp * state.aq + state.ap * state.bq)
            / (state.bq + state.aq) * tick,
            "mid_price_v1": mid,
        }
        # ret_log_1m (at-or-before lookup on the mid-change series)
        past = chg[(chg.ts <= t - 60_000_000_000) & (chg.n <= idx)]
        if not past.empty:
            checks["ret_log_1m_v1"] = (
                math.log(chg[(chg.ts <= t) & (chg.n <= idx)].iloc[-1].mid2)
                - math.log(past.iloc[-1].mid2)
            )
        # ofi_l1_w1s: sum of contributions in (t-1s, t]
        w = df[(df.ts > t - 1_000_000_000) & (df.ts <= t) & (df.n <= idx)
               & df.has_ofi]
        if t - first_ts >= 1_000_000_000:
            checks["ofi_l1_w1s_v1"] = float(w.ofi1.sum())
        # signed_volume_w1m
        if t - first_ts >= 60_000_000_000:
            tw = trades[(trades.ts > t - 60_000_000_000) & (trades.ts <= t)
                        & (trades.n <= idx)]
            checks["signed_volume_w1m_v1"] = float(tw.signed.sum())
        # rvol_w1m from mid-change returns
        if t - first_ts >= 60_000_000_000:
            rw = chg[(chg.ts > t - 60_000_000_000) & (chg.ts <= t)
                     & (chg.n <= idx)].dlm.dropna()
            checks["rvol_w1m_v1"] = math.sqrt((rw ** 2).sum() / 60.0)
        # minute_of_day
        checks["minute_of_day_v1"] = ((t // 1_000_000_000) % 86_400) / 60.0 + (
            (t % 1_000_000_000) / 6e10)

        for name, expected in checks.items():
            got, ok = engine_val(name)
            if not ok:
                failures.append(f"event {idx}: {name} invalid, expected {expected}")
            elif not close(got, expected):
                failures.append(
                    f"event {idx}: {name} engine={got!r} brute={expected!r}")
    if failures:
        raise SystemExit("VALIDATION FAILED:\n" + "\n".join(failures))
    print(f"  eq brute-force cross-checks passed at {list(vecs)}")


def main() -> int:
    registry = build_registry()
    names_idx = {s.name: i for i, s in enumerate(registry)}
    missing = [n for n in REPRESENTATIVE if n not in names_idx]
    if missing:
        raise SystemExit(f"unregistered representative features: {missing}")

    eq = read_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl")
    fx = read_jsonl(GOLDEN_DIR / "events_fx_quote.jsonl")
    _, eq_vecs = run_engine(eq, EQ_CHECKPOINTS)
    _, fx_vecs = run_engine(fx, FX_CHECKPOINTS)

    validate_eq(eq, eq_vecs, names_idx, tick=0.01)

    def capture(vecs):
        out = {}
        for idx, vec in sorted(vecs.items()):
            entry = {}
            for name in REPRESENTATIVE:
                i = names_idx[name]
                entry[name] = {
                    "value": vec.values[i] if vec.validity[i] else None,
                    "valid": vec.validity[i],
                }
            out[str(idx)] = {"timestamp": vec.timestamp, "features": entry}
        return out

    doc = {
        "description": "Feature-engine golden checkpoints: representative "
                       "feature values (every family) after applying the "
                       "first N events of each golden vector with emission "
                       "cadence = every event. value is null when valid is "
                       "false. Tolerance: abs 1e-9 / rel 1e-9. Validated "
                       "against independent brute-force pandas recomputation "
                       "before writing.",
        "registry_hash": registry_hash(),
        "registered_count": len(registry),
        "tolerance": {"abs": TOL, "rel": TOL},
        "eq": {"vector": "events_eq_mbo.jsonl", "instrument_id": 1,
               "checkpoints": capture(eq_vecs)},
        "fx": {"vector": "events_fx_quote.jsonl", "instrument_id": 101,
               "checkpoints": capture(fx_vecs)},
    }
    out_path = GOLDEN_DIR / "expected_features.json"
    with open(out_path, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    n_valid = sum(
        1 for side in ("eq", "fx")
        for cp in doc[side]["checkpoints"].values()
        for v in cp["features"].values() if v["valid"]
    )
    print(f"wrote {out_path}")
    print(f"  registry_hash {doc['registry_hash'][:16]}...  "
          f"count {doc['registered_count']}")
    print(f"  {len(REPRESENTATIVE)} representative features; "
          f"{n_valid} valid (feature, checkpoint) pairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
