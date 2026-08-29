"""Independent brute-force order-book rebuild used to VALIDATE golden states.

Deliberately written without importing ``iap.orderbook`` — plain dicts/lists,
naive scans — so it is an independent second implementation of the pinned
semantics in PLATFORM_CONVENTIONS.md section 4. Golden expected book states
are only written when ``iap.orderbook.book.OrderBook`` and this rebuild agree
exactly.
"""

from __future__ import annotations

BID, ASK = 0, 1
ADD, MODIFY, CANCEL, EXECUTE, TRADE, QUOTE, SNAPSHOT, STATUS, HEARTBEAT = range(1, 10)


class BruteForceBook:
    """Naive reference book: dict price -> FIFO list of [order_id, qty]."""

    def __init__(self) -> None:
        self.sides = ({}, {})  # index by side: {price: [[oid, qty], ...]}
        self.where = {}  # oid -> (side, price)
        self.trade_flow = 0
        self.last_seq = 0

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

    # ------------------------------------------------------------------ apply

    def apply(self, ev) -> None:
        """Apply one MarketEvent-shaped object (duck-typed attributes)."""
        self.last_seq = ev.sequence
        et = ev.event_type
        if et == ADD:
            qty = ev.qty
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
            if ev.order_id in self.where:
                side, price = self.where[ev.order_id]
                queue = self.sides[side][price]
                idx = next(i for i, e in enumerate(queue) if e[0] == ev.order_id)
                if ev.qty <= queue[idx][1]:
                    queue[idx][1] = ev.qty  # decrease keeps position
                else:
                    del queue[idx]  # increase moves to tail
                    queue.append([ev.order_id, ev.qty])
        elif et == CANCEL:
            if ev.order_id in self.where:
                self._remove(ev.order_id)
        elif et == EXECUTE:
            if ev.order_id in self.where:
                side, price = self.where[ev.order_id]
                queue = self.sides[side][price]
                idx = next(i for i, e in enumerate(queue) if e[0] == ev.order_id)
                queue[idx][1] -= ev.qty
                if queue[idx][1] <= 0:
                    self._remove(ev.order_id)
        elif et == TRADE:
            self.trade_flow += ev.qty if ev.side == BID else -ev.qty
        elif et == QUOTE:
            for oid in [o for o, (s, _) in self.where.items() if s == ev.side]:
                self._remove(oid)
            self._insert(ev.side, ev.price_ticks, ev.order_id, ev.qty)
        elif et in (STATUS, HEARTBEAT):
            pass
        else:
            raise AssertionError(f"golden vectors must not contain event_type {et}")

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
