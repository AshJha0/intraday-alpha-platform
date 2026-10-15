"""Test-only IEX HIST encoder: DEEP+ / DEEP / TOPS messages inside IEX-TP v1
segments inside UDP/IPv4/Ethernet frames inside a classic pcap file.

Nothing here reads or contains vendor data: every byte is synthesised from
the public message layouts (IEX-TP v1, DEEP+ v1.06, DEEP v1.08, TOPS
v1.66), so the IEX ingestion path can be tested without a 10 GB capture.
"""

from __future__ import annotations

import gzip
import struct
from pathlib import Path

from iap.marketdata.iex import PROTOCOL_DEEP, PROTOCOL_DEEP_PLUS, PROTOCOL_TOPS

NS = 1_000_000_000


def sym8(symbol: str) -> bytes:
    return symbol.encode("ascii").ljust(8, b" ")


# ------------------------------------------------------------ messages


def system_event(code: str, ts: int) -> bytes:
    return b"S" + code.encode() + struct.pack("<q", ts)


def directory(
    symbol: str, ts: int, round_lot: int = 100, flags: int = 0, poc: int = 0, luld: int = 1
) -> bytes:
    return (
        b"D"
        + bytes([flags])
        + struct.pack("<q", ts)
        + sym8(symbol)
        + struct.pack("<Iq", round_lot, poc)
        + bytes([luld])
    )


def trading_status(symbol: str, ts: int, status: str, reason: str = "    ") -> bytes:
    return (
        b"H"
        + status.encode()
        + struct.pack("<q", ts)
        + sym8(symbol)
        + reason.encode().ljust(4, b" ")
    )


def operational_halt(symbol: str, ts: int, status: str) -> bytes:
    return b"O" + status.encode() + struct.pack("<q", ts) + sym8(symbol)


def security_event(symbol: str, ts: int, code: str = "O") -> bytes:
    return b"E" + code.encode() + struct.pack("<q", ts) + sym8(symbol)


def price_level(
    side: str, symbol: str, ts: int, size: int, price: int, complete: bool = True
) -> bytes:
    t = b"8" if side == "B" else b"5"
    return (
        t
        + bytes([1 if complete else 0])
        + struct.pack("<q", ts)
        + sym8(symbol)
        + struct.pack("<Iq", size, price)
    )


def quote(
    symbol: str, ts: int, bid_size: int, bid: int, ask: int, ask_size: int, flags: int = 0
) -> bytes:
    return (
        b"Q"
        + bytes([flags])
        + struct.pack("<q", ts)
        + sym8(symbol)
        + struct.pack("<IqqI", bid_size, bid, ask, ask_size)
    )


def trade(symbol: str, ts: int, size: int, price: int, trade_id: int, flags: int = 0) -> bytes:
    return (
        b"T"
        + bytes([flags])
        + struct.pack("<q", ts)
        + sym8(symbol)
        + struct.pack("<Iqq", size, price, trade_id)
    )


def trade_break(
    symbol: str, ts: int, size: int, price: int, trade_id: int, flags: int = 0
) -> bytes:
    return (
        b"B"
        + bytes([flags])
        + struct.pack("<q", ts)
        + sym8(symbol)
        + struct.pack("<Iqq", size, price, trade_id)
    )


def official_price(symbol: str, ts: int, kind: str, price: int) -> bytes:
    return b"X" + kind.encode() + struct.pack("<q", ts) + sym8(symbol) + struct.pack("<q", price)


def add_order(symbol: str, ts: int, side: str, order_id: int, size: int, price: int) -> bytes:
    return (
        b"a"
        + (b"8" if side == "B" else b"5")
        + struct.pack("<q", ts)
        + sym8(symbol)
        + struct.pack("<qIq", order_id, size, price)
    )


def modify_order(
    symbol: str, ts: int, order_id: int, size: int, price: int, maintain_priority: bool = True
) -> bytes:
    return (
        b"M"
        + bytes([1 if maintain_priority else 0])
        + struct.pack("<q", ts)
        + sym8(symbol)
        + struct.pack("<qIq", order_id, size, price)
    )


def delete_order(symbol: str, ts: int, order_id: int) -> bytes:
    return b"R\x00" + struct.pack("<q", ts) + sym8(symbol) + struct.pack("<q", order_id)


def executed(
    symbol: str, ts: int, order_id: int, size: int, price: int, trade_id: int, flags: int = 0
) -> bytes:
    return (
        b"L"
        + bytes([flags])
        + struct.pack("<q", ts)
        + sym8(symbol)
        + struct.pack("<qIqq", order_id, size, price, trade_id)
    )


def clear_book(symbol: str, ts: int) -> bytes:
    return b"C\x00" + struct.pack("<q", ts) + sym8(symbol)


# ------------------------------------------------------------- framing


def tp_segment(
    protocol: int,
    messages: list[bytes],
    *,
    first_seq: int,
    send_time: int,
    session: int = 1,
    channel: int = 1,
    offset: int = 0,
) -> bytes:
    payload = b"".join(struct.pack("<H", len(m)) + m for m in messages)
    header = struct.pack(
        "<BBHIIHHqqq",
        1,
        0,
        protocol,
        channel,
        session,
        len(payload),
        len(messages),
        offset,
        first_seq,
        send_time,
    )
    return header + payload


def udp_frame(payload: bytes, *, vlan: bool = False) -> bytes:
    udp = struct.pack(">HHHH", 10000, 10378, 8 + len(payload), 0) + payload
    total = 20 + len(udp)
    ip = (
        struct.pack(
            ">BBHHHBBH4s4s",
            0x45,
            0,
            total,
            0,
            0,
            64,
            17,
            0,
            b"\xc0\xa8\x00\x01",
            b"\xe9\x01\x01\x01",
        )
        + udp
    )
    eth = b"\x01\x00\x5e\x01\x01\x01" + b"\x00\x11\x22\x33\x44\x55"
    if vlan:
        return eth + b"\x81\x00\x00\x64" + b"\x08\x00" + ip
    return eth + b"\x08\x00" + ip


class IexPcapWriter:
    """Accumulates segments as pcap records; ``write`` emits the file."""

    def __init__(self, protocol: int, *, nanosecond: bool = False, linktype: int = 1) -> None:
        self.protocol = protocol
        self.nanosecond = nanosecond
        self.linktype = linktype
        self.records: list[bytes] = []
        self.seq = 1
        self.offset = 0
        self.session = 7

    def segment(
        self,
        messages: list[bytes],
        *,
        send_time: int,
        capture_ts: int | None = None,
        vlan: bool = False,
        protocol: int | None = None,
    ) -> None:
        proto = self.protocol if protocol is None else protocol
        # every protocol has its own sequence; a foreign segment leaves ours alone
        seq = self.seq if proto == self.protocol else 1
        seg = tp_segment(
            proto,
            messages,
            first_seq=seq,
            send_time=send_time,
            session=self.session,
            offset=self.offset,
        )
        if proto == self.protocol:
            self.seq += len(messages)
            self.offset += len(seg) - 40
        self.packet(
            udp_frame(seg, vlan=vlan) if self.linktype == 1 else seg[0:0] + udp_frame(seg)[14:],
            capture_ts if capture_ts is not None else send_time,
        )

    def heartbeat(self, send_time: int) -> None:
        self.segment([], send_time=send_time)

    def packet(self, frame: bytes, capture_ts: int) -> None:
        sec, rem = divmod(capture_ts, NS)
        frac = rem if self.nanosecond else rem // 1000
        self.records.append(struct.pack("<IIII", sec, frac, len(frame), len(frame)) + frame)

    def bytes(self) -> bytes:
        magic = b"\x4d\x3c\xb2\xa1" if self.nanosecond else b"\xd4\xc3\xb2\xa1"
        head = magic + struct.pack("<HHiIII", 2, 4, 0, 0, 65535, self.linktype)
        return head + b"".join(self.records)

    def write(self, path: str | Path, compress: bool = False) -> Path:
        p = Path(path)
        data = self.bytes()
        if compress:
            with gzip.open(p, "wb") as f:
                f.write(data)
        else:
            p.write_bytes(data)
        return p


__all__ = [
    "PROTOCOL_DEEP",
    "PROTOCOL_DEEP_PLUS",
    "PROTOCOL_TOPS",
    "IexPcapWriter",
    "add_order",
    "clear_book",
    "delete_order",
    "directory",
    "executed",
    "modify_order",
    "official_price",
    "operational_halt",
    "price_level",
    "quote",
    "security_event",
    "system_event",
    "tp_segment",
    "trade",
    "trade_break",
    "trading_status",
    "udp_frame",
]
