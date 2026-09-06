"""Independent brute-force order-book rebuild used to VALIDATE golden states.

Deliberately written without importing ``iap.orderbook`` — plain dicts/lists,
naive scans — so it is an independent second implementation of the pinned
semantics in PLATFORM_CONVENTIONS.md section 4 (including the sequencing,
snapshot, quote, status-gating and malformed-event rules exercised by the
anomaly goldens; the reorder window is NOT implemented here, so anomaly
goldens are cross-validated for ``reorder_window == 0``). Golden expected
book states are only written when ``iap.orderbook.book.OrderBook`` and this
rebuild agree exactly.
"""

from __future__ import annotations

BID, ASK = 0, 1
ADD, MODIFY, CANCEL, EXECUTE, TRADE, QUOTE, SNAPSHOT, STATUS, HEARTBEAT = range(1, 10)
TRADING, HALT, AUCTION, CLOSE = 1, 2, 3, 4
I64_MAX = 2**63 - 1
I64_MIN = -(2**63)
RESERVED = 0xFFFF_0000_0000_0000

COUNTERS = (
    "duplicates_dropped", "gaps_detected", "dropped_while_stale",
    "unknown_order_events", "invalid_side_dropped", "invalid_payload_dropped",
    "unknown_type_dropped", "modify_price_mismatch", "snapshot_restarts",
    "sequence_resets", "late_recovered", "events_applied",
)


def synthetic(side, ordinal):
    return RESERVED | (side << 40) | ordinal


class BruteForceBook:
    """Naive reference book: dict price -> FIFO list of [order_id, qty]."""

    def __init__(self) -> None:
        self.sides = ({}, {})  # index by side: {price: [[oid, qty], ...]}
        self.where = {}  # oid -> (side, price)
        self.trade_flow = 0
        self.last_seq = 0
        self.started = False
        self.epoch = 0
        self.status = TRADING
        self.stale = False
        self.burst = False
        self.broken = False
        self.countdown = 0
        self.synth = [0, 0]
        self.c = {k: 0 for k in COUNTERS}

    # ------------------------------------------------------------- internals

    def _best_price(self, side):
        prices = list(self.sides[side].keys())
        if not prices:
            return None
        return max(prices) if side == BID else min(prices)

    def _remove(self, oid):
        side, price = self.where.pop(oid)
        queue = self.sides[side][price]
        for i, entry in enumerate(queue):
            if entry[0] == oid:
                del queue[i]
                break
        if not queue:
            del self.sides[side][price]

    def _insert(self, side, price, oid, qty):
        self.sides[side].setdefault(price, []).append([oid, qty])
        self.where[oid] = (side, price)

    def _total(self, side, price):
        return sum(q for _, q in self.sides[side].get(price, []))

    # ------------------------------------------------------------------ apply

    def apply(self, ev) -> None:
        """Apply one MarketEvent-shaped object (duck-typed attributes)."""
        et = ev.event_type
        seq = ev.sequence
        if self.started:
            if seq <= self.last_seq:
                if et == SNAPSHOT and not self.burst and seq < self.last_seq:
                    self.epoch += 1
                    self.c["sequence_resets"] += 1
                    self.stale = True
                else:
                    self.c["duplicates_dropped"] += 1
                    return
            elif seq > self.last_seq + 1:
                self.c["gaps_detected"] += 1
                self.stale = True
                if self.burst:
                    self.broken = True
        self.started = True
        self.last_seq = seq

        if et < 1 or et > 9:
            self.c["unknown_type_dropped"] += 1
            return
        if ev.side > 1 and et in (ADD, QUOTE, SNAPSHOT, TRADE):
            self.c["invalid_side_dropped"] += 1
            return
        bad = False
        if et in (ADD, MODIFY, CANCEL, EXECUTE) and ev.order_id == 0:
            bad = True
        elif et == ADD and (ev.qty <= 0 or ev.price_ticks <= 0 or ev.order_id >= RESERVED):
            bad = True
        elif et == EXECUTE and ev.qty <= 0:
            bad = True
        elif et in (QUOTE, SNAPSHOT) and (ev.qty <= 0 or ev.price_ticks <= 0
                                          or ev.order_id >= RESERVED):
            bad = True
        elif et == TRADE and (ev.qty <= 0 or ev.price_ticks <= 0):
            bad = True
        elif et == STATUS and ev.qty not in (TRADING, HALT, AUCTION, CLOSE):
            bad = True
        if bad:
            self.c["invalid_payload_dropped"] += 1
            return
        if self.stale and et not in (SNAPSHOT, STATUS, TRADE, HEARTBEAT):
            self.c["dropped_while_stale"] += 1
            return

        if et == ADD:
            if ev.order_id in self.where:
                self.c["unknown_order_events"] += 1
                return
            if self._total(ev.side, ev.price_ticks) + ev.qty > I64_MAX:
                self.c["invalid_payload_dropped"] += 1
                return
            qty = ev.qty
            if self.status == TRADING:
                opp = ASK if ev.side == BID else BID
                while qty > 0:
                    best = self._best_price(opp)
                    if best is None:
                        break
                    if ev.side == BID and not ev.price_ticks >= best:
                        break
                    if ev.side == ASK and not ev.price_ticks <= best:
                        break
                    head = self.sides[opp][best][0]
                    fill = min(qty, head[1])
                    qty -= fill
                    head[1] -= fill
                    if head[1] == 0:
                        self._remove(head[0])
            if qty > 0:
                self._insert(ev.side, ev.price_ticks, ev.order_id, qty)
        elif et == MODIFY:
            if ev.order_id not in self.where:
                self.c["unknown_order_events"] += 1
                return
            side, price = self.where[ev.order_id]
            if ev.price_ticks != 0 and ev.price_ticks != price:
                self.c["modify_price_mismatch"] += 1
                return
            queue = self.sides[side][price]
            idx = next(i for i, e in enumerate(queue) if e[0] == ev.order_id)
            if ev.qty <= 0:
                self._remove(ev.order_id)
            elif ev.qty <= queue[idx][1]:
                queue[idx][1] = ev.qty  # decrease keeps position
            else:
                if self._total(side, price) + ev.qty - queue[idx][1] > I64_MAX:
                    self.c["invalid_payload_dropped"] += 1
                    return
                del queue[idx]  # increase moves to tail
                queue.append([ev.order_id, ev.qty])
        elif et == CANCEL:
            if ev.order_id not in self.where:
                self.c["unknown_order_events"] += 1
                return
            self._remove(ev.order_id)
        elif et == EXECUTE:
            if ev.order_id not in self.where:
                self.c["unknown_order_events"] += 1
                return
            side, price = self.where[ev.order_id]
            queue = self.sides[side][price]
            idx = next(i for i, e in enumerate(queue) if e[0] == ev.order_id)
            queue[idx][1] -= ev.qty
            if queue[idx][1] <= 0:
                self._remove(ev.order_id)
        elif et == TRADE:
            flow = self.trade_flow + (ev.qty if ev.side == BID else -ev.qty)
            if flow > I64_MAX or flow < I64_MIN:
                self.c["invalid_payload_dropped"] += 1
                return
            self.trade_flow = flow
        elif et == QUOTE:
            oid = ev.order_id if ev.order_id else synthetic(ev.side, 0)
            if oid in self.where and self.where[oid][0] != ev.side:
                self.c["unknown_order_events"] += 1
                return
            for o in [o for o, (s, _) in self.where.items() if s == ev.side]:
                self._remove(o)
            self._insert(ev.side, ev.price_ticks, oid, ev.qty)
        elif et == SNAPSHOT:
            if self.burst:
                if ev.trade_id >= self.countdown:
                    self.burst = False
                    self.c["snapshot_restarts"] += 1
                elif ev.trade_id != self.countdown - 1:
                    self.broken = True
            if not self.burst:
                self.sides = ({}, {})
                self.where = {}
                self.burst = True
                self.broken = False
                self.synth = [0, 0]
            self.countdown = ev.trade_id
            if ev.order_id:
                oid = ev.order_id
            else:
                oid = synthetic(ev.side, self.synth[ev.side])
                self.synth[ev.side] += 1
            ok = True
            if oid in self.where:
                self.c["unknown_order_events"] += 1
                ok = False
            elif self._total(ev.side, ev.price_ticks) + ev.qty > I64_MAX:
                self.c["invalid_payload_dropped"] += 1
                ok = False
            else:
                self._insert(ev.side, ev.price_ticks, oid, ev.qty)
            if ev.trade_id == 0:
                self.burst = False
                if not self.broken:
                    self.stale = False
                self.broken = False
            if not ok:
                return
        elif et == STATUS:
            self.status = ev.qty
        # HEARTBEAT: nothing
        self.c["events_applied"] += 1

    # ---------------------------------------------------------------- summary

    def _levels(self, side, n):
        prices = sorted(self.sides[side], reverse=(side == BID))[:n]
        return [
            [p, sum(q for _, q in self.sides[side][p])] for p in prices
        ]

    def _counts(self, side, n):
        prices = sorted(self.sides[side], reverse=(side == BID))[:n]
        return [[p, len(self.sides[side][p])] for p in prices]

    def state_summary(self) -> dict:
        """Same shape as OrderBook.state_summary() (exact integers)."""
        bid = self._levels(BID, 1)
        ask = self._levels(ASK, 1)
        return {
            "best_bid_ticks": bid[0][0] if bid else 0,
            "best_bid_size": bid[0][1] if bid else 0,
            "best_ask_ticks": ask[0][0] if ask else 0,
            "best_ask_size": ask[0][1] if ask else 0,
            "depth_bid_top5": self._levels(BID, 5),
            "depth_ask_top5": self._levels(ASK, 5),
            "order_count_bid_top3": self._counts(BID, 3),
            "order_count_ask_top3": self._counts(ASK, 3),
            "trade_flow": self.trade_flow,
            "sequence": self.last_seq,
        }

    def counters(self) -> dict:
        return dict(self.c)
