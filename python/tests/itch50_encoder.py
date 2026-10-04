"""Test-only Nasdaq TotalView-ITCH 5.0 encoder and seeded session builder.

Nothing here reads or contains vendor data: every byte is synthesised from
the public message layouts, so the real-data ingestion path can be tested
(and demonstrated — COOKBOOK.md recipe 36) without a real file.

* :class:`Itch50Encoder` appends length-prefixed messages for hand-built
  scenarios; ``raw()`` writes an arbitrary type for unknown-message tests.
* :func:`build_session` simulates a small price-time-priority book per
  symbol with the platform's SplitMix64 and writes a valid day: directory,
  trading actions, pre-market orders, the opening cross, continuous trading
  (adds, executions, partial cancels, deletes, replaces, hidden trades) and
  the close.  It returns the bytes and the resting orders it expects at the
  end of the regular session, in FIFO order per level.
"""

from __future__ import annotations

import gzip
import struct
from pathlib import Path

from iap.core.rng import SplitMix64

NS = 1_000_000_000


def hms_ns(h: int, m: int = 0, s: int = 0, ns: int = 0) -> int:
    """Nanoseconds since midnight."""
    return (h * 3600 + m * 60 + s) * NS + ns


class Itch50Encoder:
    """Builds an ITCH 5.0 file image message by message."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []
        self._tracking = 0
        self.messages = 0

    # -- framing --------------------------------------------------------

    def raw(self, mtype: str, payload: bytes) -> None:
        """Append ``length | type | payload`` verbatim (any type, any length)."""
        body = mtype.encode("ascii") + payload
        self._parts.append(struct.pack(">H", len(body)) + body)
        self.messages += 1

    def _msg(self, mtype: str, locate: int, ts: int, tail: bytes) -> None:
        self._tracking = (self._tracking + 1) & 0xFFFF
        header = struct.pack(">HHHI", locate, self._tracking, ts >> 32, ts & 0xFFFFFFFF)
        self.raw(mtype, header + tail)

    def to_bytes(self) -> bytes:
        return b"".join(self._parts)

    def write(self, path: str | Path, compress: bool = False) -> Path:
        path = Path(path)
        data = self.to_bytes()
        if compress:
            # mtime=0 and no file name in the header: the same bytes every time
            with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as f:
                f.write(data)
        else:
            path.write_bytes(data)
        return path

    # -- messages -------------------------------------------------------

    @staticmethod
    def _stock(symbol: str) -> bytes:
        return symbol.encode("ascii").ljust(8, b" ")

    def system_event(self, ts: int, code: str) -> None:
        self._msg("S", 0, ts, code.encode("ascii"))

    def stock_directory(
        self,
        locate: int,
        ts: int,
        symbol: str,
        *,
        market_category: str = "Q",
        round_lot_size: int = 100,
        etp: str = "N",
    ) -> None:
        tail = struct.pack(
            ">8sccIcc2scccccIc",
            self._stock(symbol),
            market_category.encode(),
            b"N",
            round_lot_size,
            b"N",
            b"C",
            b"Z ",
            b"P",
            b"N",
            b"N",
            b"1",
            etp.encode(),
            0,
            b"N",
        )
        self._msg("R", locate, ts, tail)

    def trading_action(
        self, locate: int, ts: int, symbol: str, state: str, reason: str = ""
    ) -> None:
        tail = self._stock(symbol) + state.encode() + b" " + reason.encode().ljust(4, b" ")
        self._msg("H", locate, ts, tail)

    def reg_sho(self, locate: int, ts: int, symbol: str, action: str = "0") -> None:
        self._msg("Y", locate, ts, self._stock(symbol) + action.encode())

    def market_participant(self, locate: int, ts: int, symbol: str, mpid: str = "TEST") -> None:
        self._msg("L", locate, ts, mpid.encode().ljust(4) + self._stock(symbol) + b"YNA")

    def add(
        self,
        locate: int,
        ts: int,
        ref: int,
        side: str,
        shares: int,
        symbol: str,
        price: int,
        mpid: str | None = None,
    ) -> None:
        tail = struct.pack(">QcI8sI", ref, side.encode(), shares, self._stock(symbol), price)
        if mpid is None:
            self._msg("A", locate, ts, tail)
        else:
            self._msg("F", locate, ts, tail + mpid.encode().ljust(4))

    def executed(self, locate: int, ts: int, ref: int, shares: int, match: int) -> None:
        self._msg("E", locate, ts, struct.pack(">QIQ", ref, shares, match))

    def executed_with_price(
        self,
        locate: int,
        ts: int,
        ref: int,
        shares: int,
        match: int,
        price: int,
        printable: str = "Y",
    ) -> None:
        self._msg(
            "C", locate, ts, struct.pack(">QIQcI", ref, shares, match, printable.encode(), price)
        )

    def cancel(self, locate: int, ts: int, ref: int, shares: int) -> None:
        self._msg("X", locate, ts, struct.pack(">QI", ref, shares))

    def delete(self, locate: int, ts: int, ref: int) -> None:
        self._msg("D", locate, ts, struct.pack(">Q", ref))

    def replace(self, locate: int, ts: int, orig: int, new: int, shares: int, price: int) -> None:
        self._msg("U", locate, ts, struct.pack(">QQII", orig, new, shares, price))

    def trade(
        self, locate: int, ts: int, side: str, shares: int, symbol: str, price: int, match: int
    ) -> None:
        tail = struct.pack(">QcI8sIQ", 0, side.encode(), shares, self._stock(symbol), price, match)
        self._msg("P", locate, ts, tail)

    def cross(
        self,
        locate: int,
        ts: int,
        shares: int,
        symbol: str,
        price: int,
        match: int,
        cross_type: str,
    ) -> None:
        tail = struct.pack(
            ">Q8sIQc", shares, self._stock(symbol), price, match, cross_type.encode()
        )
        self._msg("Q", locate, ts, tail)

    def broken(self, locate: int, ts: int, match: int) -> None:
        self._msg("B", locate, ts, struct.pack(">Q", match))

    def noii(self, locate: int, ts: int, symbol: str) -> None:
        tail = struct.pack(">QQc8sIIIcc", 100, 0, b"N", self._stock(symbol), 0, 0, 0, b"O", b" ")
        self._msg("I", locate, ts, tail)


class _SimBook:
    """Price-time-priority book of one symbol (the builder's ground truth)."""

    def __init__(self, mid: int) -> None:
        self.mid = mid
        #: side -> price -> [[ref, qty], ...] FIFO
        self.levels: dict[str, dict[int, list[list[int]]]] = {"B": {}, "S": {}}
        self.orders: dict[int, tuple[str, int]] = {}
        self.refs: list[int] = []

    def best(self, side: str) -> int | None:
        prices = self.levels[side]
        if not prices:
            return None
        return max(prices) if side == "B" else min(prices)

    def passive_price(self, side: str, rng: SplitMix64) -> int:
        """A price that does not cross the opposite side (cents)."""
        opp = self.best("S" if side == "B" else "B")
        offset = 100 * rng.randint(1, 8)
        if side == "B":
            anchor = opp if opp is not None else self.mid + 100
            return max(100, anchor - offset)
        anchor = opp if opp is not None else self.mid - 100
        return anchor + offset

    def insert(self, ref: int, side: str, price: int, qty: int) -> None:
        self.levels[side].setdefault(price, []).append([ref, qty])
        self.orders[ref] = (side, price)
        self.refs.append(ref)

    def entry(self, ref: int) -> list[int]:
        side, price = self.orders[ref]
        return next(e for e in self.levels[side][price] if e[0] == ref)

    def remove(self, ref: int) -> None:
        side, price = self.orders.pop(ref)
        queue = self.levels[side][price]
        queue.remove(next(e for e in queue if e[0] == ref))
        if not queue:
            del self.levels[side][price]
        self.refs.remove(ref)

    def pick(self, rng: SplitMix64) -> int:
        return self.refs[rng.below(len(self.refs))]

    def snapshot(self) -> dict:
        return {
            side: {p: [tuple(e) for e in q] for p, q in sorted(self.levels[side].items())}
            for side in ("B", "S")
        }


def build_session(
    seed: int,
    symbols: tuple[str, ...] = ("AAPL", "MSFT"),
    n_actions: int = 2000,
    *,
    etp_symbols: tuple[str, ...] = (),
    noise_symbols: tuple[str, ...] = ("ZZZZ",),
    base_price: int = 1_500_000,
    spacing_ns: int = 5_000_000,
) -> tuple[Itch50Encoder, dict]:
    """A seeded, valid ITCH 5.0 day for ``symbols`` (+ unrelated ``noise_symbols``).

    Returns the encoder and ``{"books": {symbol: snapshot}, "volume":
    {symbol: shares}, "actions": n}`` — the builder's own book at the end of
    the regular session and the printed volume, for the tests to compare
    with what the platform reconstructs.
    """
    rng = SplitMix64(seed)
    enc = Itch50Encoder()
    everything = tuple(symbols) + tuple(noise_symbols)
    locate = {s: 10 + 3 * i for i, s in enumerate(everything)}
    books = {s: _SimBook(base_price + 250_000 * i) for i, s in enumerate(everything)}
    volume = {s: 0 for s in everything}
    next_ref = 1000
    next_match = 1

    def new_ref() -> int:
        nonlocal next_ref
        next_ref += 1 + rng.below(3)
        return next_ref

    def new_match() -> int:
        nonlocal next_match
        next_match += 1
        return next_match

    ts = hms_ns(3)
    enc.system_event(ts, "O")
    for s in everything:
        ts += 1000
        enc.stock_directory(locate[s], ts, s, etp="Y" if s in etp_symbols else "N")
    for s in everything:
        ts += 1000
        enc.trading_action(locate[s], ts, s, "T")
        enc.reg_sho(locate[s], ts, s)
    enc.system_event(hms_ns(4), "S")

    # pre-market: the book builds (adds only), then the opening cross prints
    ts = hms_ns(8)
    for s in everything:
        book = books[s]
        for _ in range(12):
            ts += spacing_ns
            side = "B" if rng.below(2) == 0 else "S"
            ref = new_ref()
            price = book.passive_price(side, rng)
            qty = 100 * rng.randint(1, 5)
            enc.add(
                locate[s], ts, ref, side, qty, s, price, mpid="MMKR" if rng.below(4) == 0 else None
            )
            book.insert(ref, side, price, qty)
        enc.noii(locate[s], ts, s)
    ts = hms_ns(9, 30)
    enc.system_event(ts, "Q")
    for s in everything:
        ts += 1000
        shares = 100 * rng.randint(5, 50)
        enc.cross(locate[s], ts, shares, s, books[s].mid, new_match(), "O")
        volume[s] += shares

    for _ in range(n_actions):
        ts += 1 + rng.below(spacing_ns)
        s = everything[rng.below(len(everything))]
        book, loc = books[s], locate[s]
        roll = rng.below(100)
        if roll < 50 or len(book.refs) < 6:
            side = "B" if rng.below(2) == 0 else "S"
            ref = new_ref()
            price = book.passive_price(side, rng)
            qty = 100 * rng.randint(1, 5)
            enc.add(loc, ts, ref, side, qty, s, price)
            book.insert(ref, side, price, qty)
        elif roll < 65:
            ref = book.pick(rng)
            enc.delete(loc, ts, ref)
            book.remove(ref)
        elif roll < 73:
            ref = book.pick(rng)
            entry = book.entry(ref)
            if entry[1] > 100:
                enc.cancel(loc, ts, ref, 100)
                entry[1] -= 100
            else:
                enc.cancel(loc, ts, ref, entry[1])
                book.remove(ref)
        elif roll < 90:
            side = "B" if rng.below(2) == 0 else "S"
            best = book.best(side)
            if best is None:
                continue
            entry = book.levels[side][best][0]
            ref = entry[0]
            qty = entry[1] if rng.below(2) == 0 else max(1, entry[1] // 2)
            if rng.below(5) == 0:
                enc.executed_with_price(loc, ts, ref, qty, new_match(), best, "Y")
            else:
                enc.executed(loc, ts, ref, qty, new_match())
            volume[s] += qty
            entry[1] -= qty
            if entry[1] == 0:
                book.remove(ref)
        elif roll < 97:
            ref = book.pick(rng)
            side, _ = book.orders[ref]
            book.remove(ref)
            new = new_ref()
            price = book.passive_price(side, rng)
            qty = 100 * rng.randint(1, 5)
            enc.replace(loc, ts, ref, new, qty, price)
            book.insert(new, side, price, qty)
        else:
            qty = 10 * rng.randint(1, 30)
            enc.trade(loc, ts, "B", qty, s, book.mid + 50, new_match())
            volume[s] += qty

    result = {
        "books": {s: books[s].snapshot() for s in symbols},
        "volume": {s: volume[s] for s in symbols},
        "actions": n_actions,
    }
    ts = max(ts + 1, hms_ns(16))
    enc.system_event(ts, "M")
    for s in everything:
        ts += 1000
        shares = 100 * rng.randint(5, 50)
        enc.cross(locate[s], ts, shares, s, books[s].mid, new_match(), "C")
        if s in volume and s in symbols:
            result["volume"][s] += shares
        # after-hours flow: mapped away by default (regular-session scope)
        enc.add(locate[s], ts + 500, new_ref(), "B", 100, s, 100)
    enc.system_event(hms_ns(20), "E")
    enc.system_event(hms_ns(20, 5), "C")
    return enc, result
