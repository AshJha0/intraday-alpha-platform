"""Paper order manager with a write-ahead journal (REL-18, order integrity).

Paper only (owner decision D4(a)): nothing here talks to a broker.  The
manager keeps the order and position state of a paper or recorder run so that
a crash at any instant is recoverable without a duplicate order and without a
lost or double-counted fill.

Idempotent submission
---------------------
Every order carries a caller-chosen ``cl_ord_id``.  Submitting an id that
already exists with the SAME parameters returns the existing order and sends
nothing (``SubmitResult.duplicate``); the same id with DIFFERENT parameters
raises :class:`DuplicateOrderError`.  The venue side (here
:class:`iap.paper.venue_sim.PaperVenue`) also de-duplicates by ``cl_ord_id``,
so the one re-send recovery performs is safe.

State machine (persisted)
-------------------------
::

    NEW -> ACKED | REJECTED
    ACKED | PARTIALLY_FILLED -> PARTIALLY_FILLED | FILLED          (fill)
    ACKED | PARTIALLY_FILLED -> CANCEL_PENDING                     (cancel request)
    CANCEL_PENDING -> CANCELLED                                    (cancel ack)
    CANCEL_PENDING -> back to ACKED / PARTIALLY_FILLED             (cancel reject)
    CANCEL_PENDING -> PARTIALLY_FILLED-in-cancel | FILLED          (fill wins the race)
    ACKED | PARTIALLY_FILLED | CANCEL_PENDING -> EXPIRED           (end of session)

Deterministic failure paths
---------------------------
* reject: NEW -> REJECTED, no position change.
* partial ack (venue accepts less than asked): the order's ``qty`` becomes the
  accepted quantity and ``reduced_by`` records the difference; an accepted
  quantity of 0 is a reject.
* cancel after fill: a cancel request on a terminal order is a no-op returning
  ``False``; a cancel REJECT arriving after the fill leaves the order FILLED.
* duplicate fill (same ``fill_id``): ignored, counted in ``dup_fills``.
* overfill or a fill on an unknown / terminal order: applied to NOTHING and
  recorded as an anomaly (the reconciliation reports it against the drop copy).
* end of session: :meth:`OrderManager.end_session` expires every open order and
  returns the residual (unfilled) quantities.

Write-ahead journal
-------------------
Every state change is validated, appended to ``journal.jsonl`` (one JSON line
with a running ``n`` and a CRC32 of its body, flushed and optionally fsynced),
and only then applied in memory.  :meth:`OrderManager.open` replays the journal
through the same transition code, so a restart reaches the same state byte for
byte.  A torn final line (crash mid-write) is dropped; a bad line anywhere else
raises :class:`JournalCorrupt`.
"""

from __future__ import annotations

import json
import os
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class OrdState(str, Enum):
    NEW = "NEW"
    ACKED = "ACKED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


TERMINAL = frozenset({OrdState.FILLED, OrdState.CANCELLED, OrdState.REJECTED, OrdState.EXPIRED})
LIVE = frozenset({OrdState.ACKED, OrdState.PARTIALLY_FILLED, OrdState.CANCEL_PENDING})

#: journal record kinds
OPEN, SUBMIT, ACK, REJECT, FILL, CANCEL_REQ, CANCEL_ACK, CANCEL_REJECT, EXPIRE = (
    "OPEN",
    "SUBMIT",
    "ACK",
    "REJECT",
    "FILL",
    "CANCEL_REQ",
    "CANCEL_ACK",
    "CANCEL_REJECT",
    "EXPIRE",
)


class OmsError(ValueError):
    """Base class of order-manager errors."""


class DuplicateOrderError(OmsError):
    """A ``cl_ord_id`` reused with different order parameters."""


class OrderStateError(OmsError):
    """An event invalid in the order's current state."""


class JournalCorrupt(OmsError):
    """A journal line other than the last one fails to parse or its CRC."""


@dataclass
class Order:
    cl_ord_id: str
    instrument_id: int
    side: int  # +1 buy, -1 sell
    qty: int
    price_ticks: int
    venue_id: int
    state: OrdState = OrdState.NEW
    filled_qty: int = 0
    notional_ticks: int = 0  # sum(fill qty * fill price)
    fills: list[tuple[int, int, int]] = field(default_factory=list)  # (fill_id, qty, price)
    venue_order_id: str = ""
    reduced_by: int = 0
    reason: str = ""
    pre_cancel_state: OrdState | None = None

    @property
    def leaves_qty(self) -> int:
        return 0 if self.state in TERMINAL else self.qty - self.filled_qty

    def params(self) -> tuple[int, int, int, int, int]:
        return (
            self.instrument_id,
            self.side,
            self.qty + self.reduced_by,
            self.price_ticks,
            self.venue_id,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        d["pre_cancel_state"] = self.pre_cancel_state.value if self.pre_cancel_state else None
        return d


@dataclass(frozen=True)
class SubmitResult:
    order: Order
    duplicate: bool  # True = existing order returned, nothing sent


@dataclass(frozen=True)
class Anomaly:
    kind: str  # OVERFILL, FILL_UNKNOWN_ORDER, FILL_AFTER_TERMINAL
    cl_ord_id: str
    fill_id: int
    qty: int
    price_ticks: int


# --------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------


def _crc(body: str) -> str:
    return f"{zlib.crc32(body.encode('utf-8')) & 0xFFFFFFFF:08x}"


class Journal:
    """Append-only JSONL with a per-line CRC; ``fsync=False`` only for tests/soak speed."""

    def __init__(self, path: str | Path, fsync: bool = True) -> None:
        self.path = Path(path)
        self.fsync = fsync
        self.n = 0
        self._fh: Any = None

    @staticmethod
    def read(path: str | Path) -> list[dict[str, Any]]:
        p = Path(path)
        if not p.exists():
            return []
        raw = p.read_bytes()
        # every append writes the line and its newline in one call: a tail
        # without its newline is torn
        lines = raw.split(b"\n")[:-1]
        out: list[dict[str, Any]] = []
        for i, line in enumerate(lines):
            rec = Journal._parse(line, i)
            if rec is None:
                raise JournalCorrupt(f"{p}: line {i + 1} unreadable")
            out.append(rec)
        return out

    @staticmethod
    def _parse(line: bytes, idx: int) -> dict[str, Any] | None:
        try:
            outer = json.loads(line.decode("utf-8"))
            body = outer["body"]
            if _crc(body) != outer["crc"] or outer["n"] != idx:
                return None
            rec = json.loads(body)
        except (ValueError, KeyError, TypeError):
            return None
        return rec if isinstance(rec, dict) else None

    def open(self, existing: int) -> None:
        self.n = existing
        p = self.path
        if p.exists():
            # drop a torn tail so the next append starts on a clean line
            raw = p.read_bytes()
            keep = raw[: raw.rfind(b"\n") + 1] if raw and not raw.endswith(b"\n") else raw
            if len(keep) != len(raw):
                with p.open("r+b") as fh:
                    fh.truncate(len(keep))
        p.parent.mkdir(parents=True, exist_ok=True)
        self._fh = p.open("ab")

    def append(self, rec: Mapping[str, Any]) -> None:
        body = json.dumps(dict(rec), sort_keys=True, separators=(",", ":"))
        line = json.dumps({"n": self.n, "crc": _crc(body), "body": body}, separators=(",", ":"))
        self._fh.write(line.encode("utf-8") + b"\n")
        self._fh.flush()
        if self.fsync:
            os.fsync(self._fh.fileno())
        self.n += 1

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# --------------------------------------------------------------------------
# order manager
# --------------------------------------------------------------------------


class OrderManager:
    """Orders and fill-derived positions of one session, journaled write-ahead.

    ``sender(order)`` is called AFTER the SUBMIT record is durable; it must be
    idempotent by ``cl_ord_id`` (the paper venue is).  Use :meth:`open` to
    construct: it replays an existing journal.
    """

    def __init__(
        self,
        journal: Journal,
        sender: Callable[[Order], None] | None = None,
    ) -> None:
        self.journal = journal
        self.sender = sender
        self.orders: dict[str, Order] = {}
        self.positions: dict[int, int] = {}
        self.start_positions: dict[int, int] = {}
        self.anomalies: list[Anomaly] = []
        self.dup_fills = 0
        self.dup_submits = 0
        self._seen_fill_ids: set[int] = set()
        self.replayed = 0

    @classmethod
    def open(
        cls,
        path: str | Path,
        sender: Callable[[Order], None] | None = None,
        start_positions: Mapping[int, int] | None = None,
        fsync: bool = True,
    ) -> OrderManager:
        """Open (replaying) or create the journal at ``path``."""
        records = Journal.read(path)
        oms = cls(Journal(path, fsync=fsync), sender)
        for rec in records:
            oms._apply(rec, commit=False)
            oms._apply(rec, commit=True)
        oms.replayed = len(records)
        oms.journal.open(len(records))
        if not records:
            oms._commit(
                {
                    "kind": OPEN,
                    "positions": {
                        str(k): int(v) for k, v in sorted((start_positions or {}).items())
                    },
                }
            )
        return oms

    def close(self) -> None:
        self.journal.close()

    # ---------------------------------------------------------------- core
    def _commit(self, rec: dict[str, Any]) -> None:
        self._apply(rec, commit=False)  # validate first: never journal an invalid record
        self.journal.append(rec)
        self._apply(rec, commit=True)

    def _get(self, cl: str) -> Order:
        o = self.orders.get(cl)
        if o is None:
            raise OrderStateError(f"unknown order {cl!r}")
        return o

    def _apply(self, rec: Mapping[str, Any], commit: bool) -> None:  # noqa: C901 - one switch
        kind = rec["kind"]
        if kind == OPEN:
            if commit:
                self.start_positions = {int(k): int(v) for k, v in rec["positions"].items()}
                self.positions = dict(self.start_positions)
            return
        cl = rec["cl_ord_id"]
        if kind == SUBMIT:
            if cl in self.orders:
                raise DuplicateOrderError(f"order {cl!r} already submitted")
            if rec["qty"] <= 0 or rec["side"] not in (1, -1):
                raise OmsError(f"order {cl!r}: qty > 0 and side +-1 required")
            if commit:
                self.orders[cl] = Order(
                    cl_ord_id=cl,
                    instrument_id=int(rec["instrument_id"]),
                    side=int(rec["side"]),
                    qty=int(rec["qty"]),
                    price_ticks=int(rec["price_ticks"]),
                    venue_id=int(rec["venue_id"]),
                )
            return
        if kind == FILL:
            self._apply_fill(rec, commit)
            return
        o = self._get(cl)
        st = o.state
        if kind == ACK:
            if st is not OrdState.NEW:
                raise OrderStateError(f"order {cl!r}: ACK in {st.value}")
            acc = int(rec["accepted_qty"])
            if not 0 < acc <= o.qty:
                raise OrderStateError(f"order {cl!r}: accepted qty {acc} outside (0, {o.qty}]")
            if commit:
                o.reduced_by = o.qty - acc
                o.qty = acc
                o.venue_order_id = str(rec.get("venue_order_id", ""))
                o.state = OrdState.ACKED
        elif kind == REJECT:
            if st is not OrdState.NEW:
                raise OrderStateError(f"order {cl!r}: REJECT in {st.value}")
            if commit:
                o.state, o.reason = OrdState.REJECTED, str(rec.get("reason", ""))
        elif kind == CANCEL_REQ:
            if st not in (OrdState.ACKED, OrdState.PARTIALLY_FILLED):
                raise OrderStateError(f"order {cl!r}: cancel request in {st.value}")
            if commit:
                o.pre_cancel_state, o.state = st, OrdState.CANCEL_PENDING
        elif kind == CANCEL_ACK:
            if st not in LIVE:  # from ACKED / PARTIALLY_FILLED = unsolicited venue cancel
                raise OrderStateError(f"order {cl!r}: CANCEL_ACK in {st.value}")
            if commit:
                o.state = OrdState.CANCELLED
        elif kind == CANCEL_REJECT:
            if st is OrdState.FILLED:  # the fill won the race: deterministic no-op
                return
            if st is not OrdState.CANCEL_PENDING:
                raise OrderStateError(f"order {cl!r}: CANCEL_REJECT in {st.value}")
            if commit:
                o.state = OrdState.PARTIALLY_FILLED if o.filled_qty else OrdState.ACKED
                o.reason = str(rec.get("reason", ""))
                o.pre_cancel_state = None
        elif kind == EXPIRE:
            if st not in LIVE:
                raise OrderStateError(f"order {cl!r}: EXPIRE in {st.value}")
            if commit:
                o.state = OrdState.EXPIRED
        else:
            raise OmsError(f"unknown journal record kind {kind!r}")

    def _apply_fill(self, rec: Mapping[str, Any], commit: bool) -> None:
        cl, fid, q, px = (
            rec["cl_ord_id"],
            int(rec["fill_id"]),
            int(rec["qty"]),
            int(rec["price_ticks"]),
        )
        if q <= 0:
            raise OmsError(f"fill {fid}: qty must be > 0")
        if not commit:
            return
        self._seen_fill_ids.add(fid)
        o = self.orders.get(cl)
        if o is None:
            self.anomalies.append(Anomaly("FILL_UNKNOWN_ORDER", cl, fid, q, px))
            return
        if o.state not in LIVE:
            self.anomalies.append(Anomaly("FILL_AFTER_TERMINAL", cl, fid, q, px))
            return
        if o.filled_qty + q > o.qty:
            self.anomalies.append(Anomaly("OVERFILL", cl, fid, q, px))
            return
        o.filled_qty += q
        o.notional_ticks += q * px
        o.fills.append((fid, q, px))
        self.positions[o.instrument_id] = self.positions.get(o.instrument_id, 0) + o.side * q
        if o.filled_qty == o.qty:
            o.state = OrdState.FILLED
        elif o.state is not OrdState.CANCEL_PENDING:
            o.state = OrdState.PARTIALLY_FILLED

    # ------------------------------------------------------------- outbound
    def submit(
        self,
        cl_ord_id: str,
        *,
        instrument_id: int,
        side: int,
        qty: int,
        price_ticks: int,
        venue_id: int = 0,
    ) -> SubmitResult:
        """Idempotent: the same id and parameters return the existing order unsent."""
        existing = self.orders.get(cl_ord_id)
        if existing is not None:
            if existing.params() != (instrument_id, side, qty, price_ticks, venue_id):
                raise DuplicateOrderError(f"order {cl_ord_id!r} reused with different parameters")
            self.dup_submits += 1
            return SubmitResult(existing, duplicate=True)
        self._commit(
            {
                "kind": SUBMIT,
                "cl_ord_id": cl_ord_id,
                "instrument_id": int(instrument_id),
                "side": int(side),
                "qty": int(qty),
                "price_ticks": int(price_ticks),
                "venue_id": int(venue_id),
            }
        )
        o = self.orders[cl_ord_id]
        if self.sender is not None:
            self.sender(o)
        return SubmitResult(o, duplicate=False)

    def request_cancel(
        self, cl_ord_id: str, canceller: Callable[[Order], None] | None = None
    ) -> bool:
        """``False`` (no-op) if the order is already terminal, NEW or cancel-pending."""
        o = self._get(cl_ord_id)
        if o.state not in (OrdState.ACKED, OrdState.PARTIALLY_FILLED):
            return False
        self._commit({"kind": CANCEL_REQ, "cl_ord_id": cl_ord_id})
        if canceller is not None:
            canceller(o)
        return True

    # -------------------------------------------------------------- inbound
    def on_ack(
        self, cl_ord_id: str, accepted_qty: int | None = None, venue_order_id: str = ""
    ) -> None:
        o = self._get(cl_ord_id)
        acc = o.qty if accepted_qty is None else int(accepted_qty)
        if acc == 0:
            self.on_reject(cl_ord_id, "accepted qty 0")
            return
        self._commit(
            {
                "kind": ACK,
                "cl_ord_id": cl_ord_id,
                "accepted_qty": acc,
                "venue_order_id": venue_order_id,
            }
        )

    def on_reject(self, cl_ord_id: str, reason: str = "") -> None:
        self._commit({"kind": REJECT, "cl_ord_id": cl_ord_id, "reason": reason})

    def on_fill(self, cl_ord_id: str, fill_id: int, qty: int, price_ticks: int) -> bool:
        """``False`` for a duplicate ``fill_id`` (ignored, not journaled)."""
        if int(fill_id) in self._seen_fill_ids:
            self.dup_fills += 1
            return False
        self._commit(
            {
                "kind": FILL,
                "cl_ord_id": cl_ord_id,
                "fill_id": int(fill_id),
                "qty": int(qty),
                "price_ticks": int(price_ticks),
            }
        )
        return True

    def on_cancel_ack(self, cl_ord_id: str) -> None:
        if self._get(cl_ord_id).state in TERMINAL:  # fill won; a stray ack is a no-op
            return
        self._commit({"kind": CANCEL_ACK, "cl_ord_id": cl_ord_id})

    def on_cancel_reject(self, cl_ord_id: str, reason: str = "") -> None:
        if self._get(cl_ord_id).state is not OrdState.CANCEL_PENDING:
            return  # fill won the race (FILLED) or stale reject: deterministic no-op
        self._commit({"kind": CANCEL_REJECT, "cl_ord_id": cl_ord_id, "reason": reason})

    def on_expire(self, cl_ord_id: str) -> None:
        if self._get(cl_ord_id).state in TERMINAL:
            return
        self._commit({"kind": EXPIRE, "cl_ord_id": cl_ord_id})

    def handle(self, ev: tuple[Any, ...]) -> None:
        """Dispatch one venue event tuple (see :mod:`iap.paper.venue_sim`)."""
        kind = ev[0]
        if kind == "ack":
            self.on_ack(ev[1], ev[2], ev[3] if len(ev) > 3 else "")
        elif kind == "reject":
            self.on_reject(ev[1], ev[2])
        elif kind == "fill":
            self.on_fill(ev[1], ev[2], ev[3], ev[4])
        elif kind == "cancel_ack":
            self.on_cancel_ack(ev[1])
        elif kind == "cancel_reject":
            self.on_cancel_reject(ev[1], ev[2])
        elif kind == "expire":
            self.on_expire(ev[1])
        else:
            raise OmsError(f"unknown venue event {kind!r}")

    # ----------------------------------------------------------- lifecycle
    def open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if o.state not in TERMINAL]

    def recover(self, venue: Any) -> dict[str, int]:
        """After a restart: re-send NEW orders (venue de-duplicates by id) and
        pull each open order's venue status, applying missed events.

        ``venue.submit(order)`` and ``venue.status(cl_ord_id)`` as in
        :class:`iap.paper.venue_sim.PaperVenue`.
        """
        resent = applied = 0
        for o in list(self.open_orders()):
            if o.state is OrdState.NEW:
                for ev in venue.submit(o):
                    self.handle(ev)
                    applied += 1
                resent += 1
            for ev in venue.status(o.cl_ord_id):
                before = self.journal.n
                st = o.state
                if ev[0] in ("ack", "reject") and st is not OrdState.NEW:
                    continue  # already known
                self.handle(ev)
                applied += self.journal.n - before
        return {"resent": resent, "applied": applied}

    def end_session(self, expire: Callable[[Order], None] | None = None) -> list[dict[str, Any]]:
        """Expire every open order; return residuals ``(cl_ord_id, qty, filled, residual)``.

        A NEW order (never acked) is rejected as ``end of session`` rather than expired.
        """
        out = []
        for o in sorted(self.open_orders(), key=lambda o: o.cl_ord_id):
            if expire is not None:
                expire(o)
            residual = o.qty - o.filled_qty
            if o.state is OrdState.NEW:
                self.on_reject(o.cl_ord_id, "end of session")
            else:
                self.on_expire(o.cl_ord_id)
            out.append(
                {
                    "cl_ord_id": o.cl_ord_id,
                    "qty": o.qty,
                    "filled": o.filled_qty,
                    "residual": residual,
                }
            )
        return out

    def snapshot(self) -> dict[str, Any]:
        """Deterministic state dump (compare before/after a restart)."""
        return {
            "orders": {k: self.orders[k].to_dict() for k in sorted(self.orders)},
            "positions": {str(k): v for k, v in sorted(self.positions.items())},
            "anomalies": [asdict(a) for a in self.anomalies],
        }


def fills_of(oms: OrderManager) -> Iterable[tuple[str, int]]:
    for o in oms.orders.values():
        for fid, _, _ in o.fills:
            yield o.cl_ord_id, fid
