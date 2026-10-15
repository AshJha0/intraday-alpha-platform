"""Spec-conformance pass of the IEX codec over a real HIST capture (v1.17, A1).

    python tools/iex_hist_conformance.py --file data/real/iex/<day>_IEXTP1_TOPS1.6.pcap.gz \
        --date 2026-10-13 --symbols AAPL,MSFT,QQQ --out research/v117/IEX_HIST_CONFORMANCE_<day>.json

Read-only: decodes every packet (``iap.marketdata.iex``), counts segments,
heartbeats, sequence gaps, message types, unknown types and symbols, checks
the invariants the specifications state (IEX-TP version 1, one message
protocol per capture, timestamps inside the session date, TOPS quotes with
bid <= ask when two-sided and non-negative sizes, the first system event
``O``, a directory row before a symbol's first quote, informational: directory
rows cover IEX-listed securities only) and writes a JSON summary.  A partial download (the first N bytes of the gzip) is fine: the
reader stops at the truncation and counts it.  No vendor bytes are copied
anywhere; the summary holds counters only.  Exit 0 = every invariant held.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python" / "src"))

from iap.marketdata.iex import (  # noqa: E402
    PROTOCOL_NAMES,
    TP_VERSION,
    IexDirectory,
    IexQuote,
    IexSystemEvent,
    IexTrade,
    PcapStats,
    UnknownMessage,
    decode_message,
    open_capture,
    pcap_packets,
    tp_segment,
)
from iap.marketdata.itch50 import session_midnight_ns  # noqa: E402

NS_DAY = 86_400 * 1_000_000_000


def run(path: Path, date: str, symbols: list[str], max_packets: int | None) -> dict:
    midnight = session_midnight_ns(date)
    stats = PcapStats()
    out: dict = {
        "file": path.name,
        "date": date,
        "symbols_checked": symbols,
        "segments": 0,
        "heartbeats": 0,
        "tp_versions": {},
        "protocols": {},
        "sessions": set(),
        "sequence_gaps": 0,
        "messages": 0,
        "by_type": {},
        "unknown_types": {},
        "symbols_seen": set(),
        "first_system_event": None,
        "system_events": [],
        "outside_session_date": 0,
        "quotes": 0,
        "quotes_crossed": 0,
        "quotes_negative_size": 0,
        "quotes_one_sided": 0,
        "quotes_before_directory": 0,
        "trades": 0,
        "capture_minus_send_ns": {"min": None, "max": None},
        "first_message_ts": None,
        "last_message_ts": None,
        "violations": [],
    }
    expected_seq: int | None = None
    protocol: int | None = None
    directory_seen: set[str] = set()
    with open_capture(path) as f:
        for n, (capture_ts, payload) in enumerate(pcap_packets(f, stats)):
            if max_packets is not None and n >= max_packets:
                break
            version = payload[0] if payload else None
            out["tp_versions"][str(version)] = out["tp_versions"].get(str(version), 0) + 1
            seg = tp_segment(payload)
            if seg is None:
                continue
            out["segments"] += 1
            out["sessions"].add(seg.session)
            out["protocols"][PROTOCOL_NAMES.get(seg.protocol, hex(seg.protocol))] = (
                out["protocols"].get(PROTOCOL_NAMES.get(seg.protocol, hex(seg.protocol)), 0) + 1
            )
            if protocol is None:
                protocol = seg.protocol
            d = capture_ts - seg.send_time
            lo, hi = out["capture_minus_send_ns"]["min"], out["capture_minus_send_ns"]["max"]
            out["capture_minus_send_ns"]["min"] = d if lo is None else min(lo, d)
            out["capture_minus_send_ns"]["max"] = d if hi is None else max(hi, d)
            if not seg.messages:
                out["heartbeats"] += 1
                continue
            if expected_seq is not None and seg.first_sequence != expected_seq:
                out["sequence_gaps"] += 1
            expected_seq = seg.first_sequence + len(seg.messages)
            for data in seg.messages:
                out["messages"] += 1
                m = decode_message(seg.protocol, data)
                code = chr(data[0]) if data and 32 <= data[0] < 127 else "?"
                out["by_type"][code] = out["by_type"].get(code, 0) + 1
                if type(m) is UnknownMessage:
                    out["unknown_types"][m.type_code] = out["unknown_types"].get(m.type_code, 0) + 1
                    continue
                ts = int(m.ts) - midnight
                if out["first_message_ts"] is None:
                    out["first_message_ts"] = ts
                out["last_message_ts"] = ts
                if not 0 <= ts < NS_DAY:
                    out["outside_session_date"] += 1
                if type(m) is IexSystemEvent:
                    out["system_events"].append({"code": m.code, "ts_ns_since_midnight": ts})
                    if out["first_system_event"] is None:
                        out["first_system_event"] = m.code
                    continue
                sym = getattr(m, "symbol", None)
                if sym is not None:
                    out["symbols_seen"].add(sym)
                if type(m) is IexDirectory:
                    directory_seen.add(m.symbol)
                elif type(m) is IexQuote:
                    out["quotes"] += 1
                    if m.bid_size < 0 or m.ask_size < 0:
                        out["quotes_negative_size"] += 1
                    if m.bid and m.ask:
                        if m.bid > m.ask:
                            out["quotes_crossed"] += 1
                    else:
                        out["quotes_one_sided"] += 1
                    if m.symbol in symbols and m.symbol not in directory_seen:
                        out["quotes_before_directory"] += 1
                elif type(m) is IexTrade:
                    out["trades"] += 1
    out["pcap"] = stats.as_dict()
    out["sessions"] = sorted(out["sessions"])
    out["symbols_seen"] = len(out["symbols_seen"])
    out["symbols_checked_seen"] = sorted(s for s in symbols if s in directory_seen)
    out["directory_rows"] = len(directory_seen)
    out["capture_clock_note"] = (
        "capture_minus_send_ns is the capture host clock minus the IEX-TP send time; "
        "about 37 s on 2026-10-13 = TAI - UTC, so the capture stamps are TAI; the reader "
        "uses the messages' own UTC timestamps"
    )
    v = out["violations"]
    if list(out["tp_versions"]) != [str(TP_VERSION)]:
        v.append(f"IEX-TP versions seen: {out['tp_versions']}")
    if len(out["protocols"]) != 1:
        v.append(f"more than one message protocol: {out['protocols']}")
    if out["unknown_types"]:
        v.append(f"unknown message types: {out['unknown_types']}")
    if out["outside_session_date"]:
        v.append(f"{out['outside_session_date']} messages outside {date}")
    if out["first_system_event"] != "O":
        v.append(f"first system event is {out['first_system_event']!r}, not 'O'")
    if out["quotes_crossed"] or out["quotes_negative_size"]:
        v.append("crossed or negative-size quotes")
    # Directory rows exist for IEX-LISTED securities only (the specifications'
    # "IEX-listed security" wording; 3 rows for 13,274 symbols on 2026-10-13),
    # so quotes before a directory row are informational: the reader
    # synthesises the row for a universe symbol and counts it.
    if out["sequence_gaps"]:
        v.append(f"{out['sequence_gaps']} sequence gaps")
    out["ok"] = not v
    out["run_utc"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", required=True, type=Path)
    ap.add_argument("--date", required=True)
    ap.add_argument("--symbols", default="AAPL,MSFT,QQQ")
    ap.add_argument("--max-packets", type=int, default=None)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    doc = run(a.file, a.date, [s for s in a.symbols.split(",") if s], a.max_packets)
    text = json.dumps(doc, indent=2, sort_keys=True)
    if a.out is not None:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {a.out}")
    else:
        print(text)
    print("OK" if doc["ok"] else "VIOLATIONS: " + "; ".join(doc["violations"]))
    return 0 if doc["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
