"""A deterministic paper venue for resilience tests and the REL-18 soak.

Not a market model: it exists to exercise the order manager's failure paths.
It de-duplicates submissions by ``cl_ord_id`` (counting the duplicates it saw),
acks (sometimes for less than asked), rejects, fills in random clips, loses the
cancel race, expires at end of session, and publishes every fill on an
independent drop-copy list.  Events are tuples consumed by
:meth:`iap.paper.oms.OrderManager.handle`::

    ("ack", cl, accepted_qty, venue_order_id)   ("reject", cl, reason)
    ("fill", cl, fill_id, qty, price_ticks)     ("cancel_ack", cl)
    ("cancel_reject", cl, reason)               ("expire", cl)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from iap.paper.reconcile import DropCopyFill


@dataclass
class _VOrder:
    cl_ord_id: str
    instrument_id: int
    side: int
    qty: int
    price_ticks: int
    state: str = "OPEN"  # OPEN, REJECTED, FILLED, CANCELLED, EXPIRED
    filled: int = 0
    events: list[tuple[Any, ...]] = field(default_factory=list)


class PaperVenue:
    def __init__(
        self,
        seed: int = 0,
        reject_p: float = 0.0,
        partial_ack_p: float = 0.0,
        fill_p: float = 0.3,
    ) -> None:
        self.rng = random.Random(seed)
        self.reject_p = reject_p
        self.partial_ack_p = partial_ack_p
        self.fill_p = fill_p
        self.orders: dict[str, _VOrder] = {}
        self.drop_copy: list[DropCopyFill] = []
        self.dup_submits = 0
        self._next_fill = 1

    def _emit(self, v: _VOrder, ev: tuple[Any, ...]) -> tuple[Any, ...]:
        v.events.append(ev)
        return ev

    def submit(self, order: Any) -> list[tuple[Any, ...]]:
        cl = order.cl_ord_id
        if cl in self.orders:
            self.dup_submits += 1
            return []  # idempotent: the original order stands, nothing new
        v = _VOrder(
            cl, order.instrument_id, order.side, order.qty + order.reduced_by, order.price_ticks
        )
        self.orders[cl] = v
        if self.rng.random() < self.reject_p:
            v.state = "REJECTED"
            return [self._emit(v, ("reject", cl, "venue reject"))]
        acc = v.qty
        if v.qty > 1 and self.rng.random() < self.partial_ack_p:
            acc = self.rng.randint(1, v.qty - 1)
        v.qty = acc
        return [self._emit(v, ("ack", cl, acc, f"V{cl}"))]

    def cancel(self, order: Any) -> list[tuple[Any, ...]]:
        v = self.orders.get(order.cl_ord_id)
        if v is None or v.state != "OPEN":
            return [("cancel_reject", order.cl_ord_id, "too late")]
        v.state = "CANCELLED"
        return [self._emit(v, ("cancel_ack", v.cl_ord_id))]

    def fill(self, cl: str, qty: int | None = None) -> list[tuple[Any, ...]]:
        """Force a fill of ``qty`` (default: all leaves) on an open order."""
        v = self.orders[cl]
        if v.state != "OPEN":
            return []
        q = v.qty - v.filled if qty is None else min(qty, v.qty - v.filled)
        fid = self._next_fill
        self._next_fill += 1
        v.filled += q
        if v.filled == v.qty:
            v.state = "FILLED"
        self.drop_copy.append(DropCopyFill(cl, fid, v.instrument_id, v.side, q, v.price_ticks))
        return [self._emit(v, ("fill", cl, fid, q, v.price_ticks))]

    def step(self) -> list[tuple[Any, ...]]:
        """Random fills on open orders (deterministic per seed)."""
        out: list[tuple[Any, ...]] = []
        for cl, v in list(self.orders.items()):
            if v.state == "OPEN" and self.rng.random() < self.fill_p:
                out += self.fill(cl, self.rng.randint(1, v.qty - v.filled))
        return out

    def end_session(self) -> list[tuple[Any, ...]]:
        out = []
        for v in self.orders.values():
            if v.state == "OPEN":
                v.state = "EXPIRED"
                out.append(self._emit(v, ("expire", v.cl_ord_id)))
        return out

    def status(self, cl: str) -> list[tuple[Any, ...]]:
        """Every event the venue ever sent for ``cl`` (the status/replay query)."""
        v = self.orders.get(cl)
        return list(v.events) if v else []

    def prune(self, keep: set[str]) -> None:
        """Forget finished orders not in ``keep`` (soak memory bound)."""
        for cl in [c for c, v in self.orders.items() if v.state != "OPEN" and c not in keep]:
            del self.orders[cl]
