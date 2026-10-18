#!/usr/bin/env python3
"""REL-18 soak: the paper loop under injected faults for a fixed wall time.

Synthetic sequenced feed -> :class:`iap.paper.feed.FeedGuard` (RESYNC policy,
reorder window, real clock check, stale detection) -> a toy strategy that
submits orders -> :class:`iap.paper.oms.OrderManager` (fsynced journal) ->
:class:`iap.paper.venue_sim.PaperVenue` (rejects, partial acks, random fills,
cancel races, drop copy).  Injected: duplicate, swapped and dropped feed
messages, silent periods, and process "kills" (the order manager is discarded
with a random tail of venue events unprocessed, then reopened from its journal
and recovered).  Sessions roll every ``--session-minutes``: end-of-session
expiry, reconciliation, a fresh journal carrying the positions.

Every kill and every session end is reconciled against the drop copy; the
summary JSON records the exception counts, memory (working set) per minute and
the feed statistics.  Exit code 1 if any reconciliation exception occurred.

    python tools/rel18_soak.py --minutes 60 --out research/v118/rel18_soak
"""

from __future__ import annotations

import argparse
import gc
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python" / "src"))

from iap.paper.feed import ClockGuard, FeedGuard, GapPolicy  # noqa: E402
from iap.paper.oms import OrderManager  # noqa: E402
from iap.paper.reconcile import reconcile  # noqa: E402
from iap.paper.recorder import ClockMapping  # noqa: E402
from iap.paper.venue_sim import PaperVenue  # noqa: E402


def rss_bytes() -> int:
    """Working set (Windows) or max RSS (POSIX)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return int(pmc.WorkingSetSize)
    import resource

    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


def _opened(
    jpath: Path, positions: dict[int, int], venue: PaperVenue, pending: list[Any]
) -> OrderManager:
    m = OrderManager.open(jpath, start_positions=positions, fsync=True)
    m.sender = lambda o: pending.extend(venue.submit(o))
    return m


class Health:
    def __init__(self) -> None:
        self.transitions: list[tuple[str, str]] = []

    def set_state(self, component: str, state: str, reason: str) -> None:
        self.transitions.append((state, reason))


def log(fh: Any, **kw: Any) -> None:
    fh.write(json.dumps(kw, sort_keys=True) + "\n")
    fh.flush()


def main(argv: list[str] | None = None) -> int:  # noqa: C901 - one loop
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--session-minutes", type=float, default=5.0)
    ap.add_argument("--rate", type=int, default=1000, help="feed messages per second")
    ap.add_argument("--seed", type=int, default=18)
    ap.add_argument("--out", required=True)
    ap.add_argument("--clock-tolerance-ms", type=float, default=1000.0)
    a = ap.parse_args(argv)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    work = out / "work"
    work.mkdir(exist_ok=True)
    rng = random.Random(a.seed)
    fh = (out / "soak_log.jsonl").open("w", encoding="utf-8")
    t0 = time.monotonic()
    end = t0 + a.minutes * 60
    health = Health()
    mapping = ClockMapping.sample()
    clock = ClockGuard(mapping, tolerance_ns=int(a.clock_tolerance_ms * 1e6))
    tot: dict[str, int] = {
        "messages_sent": 0,
        "orders": 0,
        "kills": 0,
        "recon_runs": 0,
        "recon_exceptions": 0,
        "unreconciled_positions": 0,
        "venue_dup_submits": 0,
        "resyncs": 0,
        "silences": 0,
        "residual_orders": 0,
    }
    sessions: list[dict[str, Any]] = []
    mem: list[dict[str, Any]] = []
    positions: dict[int, int] = {}
    feed_stats_total: dict[str, int] = {}
    seq = 0
    session = 0
    next_mem = t0

    def check(oms: OrderManager, venue: PaperVenue, where: str) -> None:
        rep = reconcile(oms, venue.drop_copy)
        tot["recon_runs"] += 1
        tot["recon_exceptions"] += len(rep.exceptions)
        tot["unreconciled_positions"] += rep.unreconciled_positions
        if not rep.ok:
            log(fh, kind="recon_fail", where=where, report=rep.to_dict())

    while time.monotonic() < end:
        s_end = min(end, time.monotonic() + a.session_minutes * 60)
        venue = PaperVenue(
            seed=a.seed * 1000 + session, reject_p=0.03, partial_ack_p=0.1, fill_p=0.05
        )
        jpath = work / f"journal_{session:04d}.jsonl"
        pending: list[tuple[Any, ...]] = []

        oms = _opened(jpath, positions, venue, pending)
        held: list[tuple[int, int]] = []

        def ask_snapshot(expected: int, got: int) -> None:
            tot["resyncs"] += 1

        guard = FeedGuard(
            policy=GapPolicy.RESYNC,
            reorder_window=8,
            stale_after_ns=200_000_000,
            clock=clock,
            health=health,
            on_gap=ask_snapshot,
        )
        n_order = 0
        while time.monotonic() < s_end:
            tick = time.monotonic()
            for _ in range(max(1, a.rate // 100)):  # one 10 ms batch
                seq += 1
                msg = [seq]
                r = rng.random()
                if r < 0.01:
                    msg = [seq, seq]  # duplicate
                elif r < 0.02:
                    held.append((seq, 2))  # deliver late (out of order)
                    msg = []
                elif r < 0.0205:
                    msg = []  # lost: a gap
                msg += [q for q, k in held if k <= 0]
                held = [(q, k - 1) for q, k in held if k > 0]
                for s in msg:
                    tot["messages_sent"] += 1
                    for p in guard.on_message(s, s, time.monotonic_ns()):
                        if p % 40 == 0:  # toy strategy: one order per 40 delivered
                            n_order += 1
                            oms.submit(
                                f"S{session}-{n_order}",
                                instrument_id=p % 5,
                                side=rng.choice((1, -1)),
                                qty=rng.randint(1, 500),
                                price_ticks=10_000 + p % 17,
                            )
                if guard.resyncing and rng.random() < 0.2:
                    guard.resync(seq + 1)  # snapshot received
            pending.extend(venue.step())
            for o in oms.open_orders():
                if rng.random() < 0.01 and oms.request_cancel(o.cl_ord_id):
                    pending.extend(venue.cancel(o))
            if rng.random() < 0.002:  # kill -9 mid-order
                del pending[rng.randint(0, len(pending)) :]
                for ev in pending:
                    oms.handle(ev)
                pending.clear()
                oms.close()
                del oms
                tot["kills"] += 1
                oms = _opened(jpath, positions, venue, pending)
                oms.recover(venue)
                for ev in pending:
                    oms.handle(ev)
                pending.clear()
                check(oms, venue, f"kill#{tot['kills']}")
            else:
                for ev in pending:
                    oms.handle(ev)
                pending.clear()
            if rng.random() < 0.0005:  # silent feed
                tot["silences"] += 1
                time.sleep(0.3)
                guard.check_stale(time.monotonic_ns())
            guard.check_stale(time.monotonic_ns())
            now = time.monotonic()
            if now >= next_mem:
                gc.collect()
                mem.append({"t_s": round(now - t0, 1), "rss_mb": round(rss_bytes() / 2**20, 2)})
                log(fh, kind="mem", **mem[-1], seq=seq, orders=tot["orders"] + n_order)
                next_mem = now + 60
            time.sleep(max(0.0, 0.01 - (time.monotonic() - tick)))
        # ---- session end
        if guard.resyncing:
            guard.resync(seq + 1)
        guard.flush() if not guard.resyncing else None
        for ev in venue.end_session():
            oms.handle(ev)
        oms.recover(venue)
        residuals = oms.end_session()
        rep = reconcile(oms, venue.drop_copy)
        tot["recon_runs"] += 1
        tot["recon_exceptions"] += len(rep.exceptions)
        tot["unreconciled_positions"] += rep.unreconciled_positions
        tot["orders"] += n_order
        tot["venue_dup_submits"] += venue.dup_submits
        tot["residual_orders"] += len(residuals)
        for k, v in guard.stats.to_dict().items():
            if isinstance(v, int):
                feed_stats_total[k] = feed_stats_total.get(k, 0) + v
        venue_orders = len(venue.orders)
        sessions.append(
            {
                "session": session,
                "orders": n_order,
                "venue_orders": venue_orders,
                "residual_orders": len(residuals),
                "recon_ok": rep.ok,
                "exceptions": rep.to_dict()["counts"],
                "positions": rep.to_dict()["positions_oms"],
                "journal_bytes": jpath.stat().st_size,
            }
        )
        log(fh, kind="session", **sessions[-1])
        if venue_orders != n_order:
            log(fh, kind="order_count_mismatch", oms=n_order, venue=venue_orders)
            tot["recon_exceptions"] += 1
        positions = dict(oms.positions)
        oms.close()
        jpath.unlink()  # bounded disk; the session record above keeps the result
        del oms, venue
        session += 1

    rss = [m["rss_mb"] for m in mem]
    half = rss[len(rss) // 2 :] or rss
    summary = {
        "schema": "iap.rel18_soak",
        "version": 1,
        "args": vars(a),
        "elapsed_minutes": round((time.monotonic() - t0) / 60, 2),
        "totals": tot,
        "feed": feed_stats_total,
        "health_transitions": len(health.transitions),
        "unknown_episodes": sum(1 for s, _ in health.transitions if s == "UNKNOWN"),
        "clock_max_abs_drift_ms": round(clock.max_abs_drift_ns / 1e6, 3),
        "memory_mb": {
            "first": rss[0] if rss else None,
            "max": max(rss) if rss else None,
            "last": rss[-1] if rss else None,
            "second_half_range": round(max(half) - min(half), 2) if half else None,
        },
        "sessions": sessions,
        "verdict": "PASS"
        if tot["recon_exceptions"] == 0 and tot["unreconciled_positions"] == 0
        else "FAIL",
    }
    (out / "soak_summary.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    log(fh, kind="done", verdict=summary["verdict"])
    fh.close()
    print(json.dumps({k: summary[k] for k in ("verdict", "totals", "memory_mb")}))
    return 0 if summary["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
