"""Test-only LOBSTER fixture builder (message file + matching orderbook file).

Synthesised with the platform's SplitMix64 from the public file layout; no
vendor data.  The simulated book holds **pre-open orders** the message file
never shows the submission of, on more levels than the orderbook file
displays, so the fixtures exercise seeding (row 0), messages on unseen
orders and the back-fill of depth that only becomes visible later.
"""

from __future__ import annotations

from pathlib import Path

from iap.core.rng import SplitMix64

EMPTY_ASK = 9_999_999_999
EMPTY_BID = -9_999_999_999


class LobsterSim:
    """Price-time-priority book that writes LOBSTER rows."""

    def __init__(self, levels: int, mid: int = 1_500_000) -> None:
        self.levels = levels
        self.mid = mid
        #: direction (1 buy / -1 sell) -> price -> [[order_id, qty], ...]
        self.book: dict[int, dict[int, list[list[int]]]] = {1: {}, -1: {}}
        self.where: dict[int, tuple[int, int]] = {}
        self.ids: list[int] = []
        self.messages: list[str] = []
        self.rows: list[str] = []
        self.ns = 34_200 * 10**9

    def best(self, direction: int) -> int | None:
        prices = self.book[direction]
        if not prices:
            return None
        return max(prices) if direction == 1 else min(prices)

    def insert(self, oid: int, direction: int, price: int, qty: int) -> None:
        self.book[direction].setdefault(price, []).append([oid, qty])
        self.where[oid] = (direction, price)
        self.ids.append(oid)

    def entry(self, oid: int) -> list[int]:
        direction, price = self.where[oid]
        return next(e for e in self.book[direction][price] if e[0] == oid)

    def remove(self, oid: int) -> None:
        direction, price = self.where.pop(oid)
        queue = self.book[direction][price]
        queue.remove(next(e for e in queue if e[0] == oid))
        if not queue:
            del self.book[direction][price]
        self.ids.remove(oid)

    def row(self) -> str:
        asks = sorted(self.book[-1])[: self.levels]
        bids = sorted(self.book[1], reverse=True)[: self.levels]
        out: list[int] = []
        for k in range(self.levels):
            if k < len(asks):
                out += [asks[k], sum(q for _, q in self.book[-1][asks[k]])]
            else:
                out += [EMPTY_ASK, 0]
            if k < len(bids):
                out += [bids[k], sum(q for _, q in self.book[1][bids[k]])]
            else:
                out += [EMPTY_BID, 0]
        return ",".join(str(v) for v in out)

    def emit(
        self, step_ns: int, mtype: int, oid: int, size: int, price: int, direction: int
    ) -> None:
        self.ns += step_ns
        stamp = f"{self.ns // 10**9}.{self.ns % 10**9:09d}"
        self.messages.append(f"{stamp},{mtype},{oid},{size},{price},{direction}")
        self.rows.append(self.row())


def build_lobster(
    seed: int,
    n_messages: int = 600,
    levels: int = 5,
    preopen_levels: int = 9,
    with_halt: bool = True,
) -> tuple[str, str, dict]:
    """``(message_csv, orderbook_csv, info)`` for one symbol-day."""
    rng = SplitMix64(seed)
    sim = LobsterSim(levels)
    next_id = 5_000_000
    for k in range(1, preopen_levels + 1):
        for direction in (1, -1):
            for _ in range(rng.randint(1, 2)):
                next_id += 7
                sim.insert(
                    next_id, direction, sim.mid - direction * 100 * k, 100 * rng.randint(1, 4)
                )
    counts = {str(t): 0 for t in range(1, 8)}
    halted = not with_halt
    while len(sim.messages) < n_messages:
        step = 1 + rng.below(40_000_000)
        roll = rng.below(100)
        thin = min(len(sim.book[1]), len(sim.book[-1])) < 3
        if roll < 38 or thin or len(sim.ids) < 8:
            direction = 1 if rng.below(2) == 0 else -1
            opp = sim.best(-direction)
            anchor = opp if opp is not None else sim.mid
            price = max(100, anchor - direction * 100 * rng.randint(1, 12))
            next_id += 1 + rng.below(5)
            qty = 100 * rng.randint(1, 4)
            sim.insert(next_id, direction, price, qty)
            sim.emit(step, 1, next_id, qty, price, direction)
            counts["1"] += 1
        elif roll < 48:
            oid = sim.ids[rng.below(len(sim.ids))]
            entry = sim.entry(oid)
            direction, price = sim.where[oid]
            if entry[1] > 100:
                entry[1] -= 100
                sim.emit(step, 2, oid, 100, price, direction)
                counts["2"] += 1
            else:
                size = entry[1]
                sim.remove(oid)
                sim.emit(step, 3, oid, size, price, direction)
                counts["3"] += 1
        elif roll < 62:
            oid = sim.ids[rng.below(len(sim.ids))]
            direction, price = sim.where[oid]
            size = sim.entry(oid)[1]
            sim.remove(oid)
            sim.emit(step, 3, oid, size, price, direction)
            counts["3"] += 1
        elif roll < 92:
            direction = 1 if rng.below(2) == 0 else -1
            best = sim.best(direction)
            entry = sim.book[direction][best][0]
            oid = entry[0]
            size = entry[1] if rng.below(3) else max(1, entry[1] // 2)
            entry[1] -= size
            if entry[1] == 0:
                sim.remove(oid)
            sim.emit(step, 4, oid, size, best, direction)
            counts["4"] += 1
        elif roll < 97:
            direction = 1 if rng.below(2) == 0 else -1
            sim.emit(step, 5, 0, 10 * rng.randint(1, 20), sim.mid + 50, direction)
            counts["5"] += 1
        elif roll < 99:
            sim.emit(step, 6, 0, 100 * rng.randint(1, 9), sim.mid, -1)
            counts["6"] += 1
        elif not halted:
            halted = True
            for code in (-1, 0, 1):
                sim.emit(step, 7, 0, 0, code, -1)
                counts["7"] += 1
    info = {
        "message_counts": counts,
        "final": {
            "ask": [(p, sum(q for _, q in sim.book[-1][p])) for p in sorted(sim.book[-1])],
            "bid": [
                (p, sum(q for _, q in sim.book[1][p])) for p in sorted(sim.book[1], reverse=True)
            ],
        },
        "messages": len(sim.messages),
    }
    return "\n".join(sim.messages) + "\n", "\n".join(sim.rows) + "\n", info


def write_lobster(
    directory: Path,
    symbol: str,
    date: str,
    seed: int,
    levels: int = 5,
    **kw,
) -> tuple[Path, Path, dict]:
    """Write the pair under LOBSTER's file names; returns the two paths + info."""
    messages, rows, info = build_lobster(seed, levels=levels, **kw)
    stem = f"{symbol}_{date}_34200000_57600000"
    msg_path = directory / f"{stem}_message_{levels}.csv"
    book_path = directory / f"{stem}_orderbook_{levels}.csv"
    msg_path.write_text(messages, encoding="ascii", newline="\n")
    book_path.write_text(rows, encoding="ascii", newline="\n")
    return msg_path, book_path, info
