"""Paper-run recorder (REL-05, Level 1): one clock, versioned files, no broker.

Records what a paper-trading run saw and did so it can be replayed and audited:
market events (``receive_ts`` taken from a monotonic clock mapped to the wall
clock ONCE at start), and every child sent, acknowledged, cancelled, rejected,
expired and filled, all on the same mapped clock.  Not a broker client: the
caller feeds it events and order-lifecycle facts; nothing here touches a
network or a venue.

Files written by :meth:`PaperRecorder.write` into one directory
-----------------------------------------------------------------
* ``market.iap1``    - the project's own IAP1 v2 binary, events in arrival order,
  ``receive_ts`` = mapped monotonic arrival time (never reordered, never edited).
* ``orders.jsonl``   - one JSON object per order event, sorted by ``seq``; the
  ``seq`` is the recorder's global call order, ``mono_ns`` the raw monotonic
  reading, ``ts`` its wall-clock mapping.
* ``recording.json`` - the manifest: schema ``iap.paper_recording`` version 1,
  the clock mapping and its uncertainty, the parents with their trace ids
  (signal -> risk -> routing), counts and sha256 of the two files above.

The clock mapping is ``wall = wall0 + (mono - mono0)``; ``uncertainty_ns`` is
half the width of the bracket in which ``wall0`` was read.  It does NOT include
the offset of the host wall clock from true UTC (NTP error), which is unknown
here and typically far larger: cross-source comparisons (against exchange
timestamps) inherit that error.

Order-event state machine (per child, enforced on write AND on load)::

    SEND -> ACK -> FILL* -> (fully filled | CANCEL_ACK | EXPIRE)
    SEND -> REJECT
    ACK -> CANCEL_SEND -> (FILL* | CANCEL_ACK)       # a cancel can lose the race

Quantity is conserved: ``sum(fill qty) <= child qty`` and a child that is fully
filled takes no further event.  Violations raise :class:`RecorderError`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iap.contracts.types import ExecStatus, ExecutionReport
from iap.core.codec import decode_iap1_ex, encode_iap1
from iap.core.events import MarketEvent
from iap.execution.algos import AlgoType, ParentOrder
from iap.execution.passive import ExecPolicy

SCHEMA = "iap.paper_recording"
VERSION = 1

SEND, ACK, CANCEL_SEND, CANCEL_ACK, FILL, REJECT, EXPIRE = (
    "SEND",
    "ACK",
    "CANCEL_SEND",
    "CANCEL_ACK",
    "FILL",
    "REJECT",
    "EXPIRE",
)
KINDS = (SEND, ACK, CANCEL_SEND, CANCEL_ACK, FILL, REJECT, EXPIRE)
TRACE_KEYS = ("signal_id", "risk_decision_id", "routing_id")

#: ParentOrder fields a recorded parent spec may carry (plain algos only).
PARENT_FIELDS = (
    "parent_id",
    "instrument_id",
    "venue_id",
    "side",
    "qty",
    "start_ts",
    "end_ts",
    "slices",
    "participation",
    "risk_aversion",
    "max_child_qty",
    "urgency",
)


class RecorderError(ValueError):
    """An invalid event, sequence or file."""


@dataclass(frozen=True)
class ClockMapping:
    """``wall = wall_ns0 + (mono - mono_ns0)``, fixed at start."""

    wall_ns0: int
    mono_ns0: int
    uncertainty_ns: int

    def to_wall(self, mono_ns: int) -> int:
        return self.wall_ns0 + (mono_ns - self.mono_ns0)

    @classmethod
    def sample(
        cls,
        wall: Callable[[], int] = time.time_ns,
        mono: Callable[[], int] = time.monotonic_ns,
        tries: int = 7,
    ) -> ClockMapping:
        """Read wall/mono/wall ``tries`` times; keep the narrowest bracket."""
        best: tuple[int, int, int] | None = None
        for _ in range(max(1, tries)):
            w0 = wall()
            m = mono()
            w1 = wall()
            width = w1 - w0
            if best is None or width < best[0]:
                best = (width, (w0 + w1) // 2, m)
        assert best is not None
        width, w_mid, m = best
        return cls(wall_ns0=w_mid, mono_ns0=m, uncertainty_ns=width // 2 + 1)

    def to_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


def parent_to_spec(p: ParentOrder) -> dict[str, Any]:
    """JSON spec of a plain parent (rejects any opt-in the spec cannot carry)."""
    if p != dataclasses.replace(
        ParentOrder(), **{f: getattr(p, f) for f in PARENT_FIELDS}, algo=p.algo, policy=p.policy
    ):
        raise RecorderError("parent carries opt-ins the recording schema does not hold")
    spec: dict[str, Any] = {f: getattr(p, f) for f in PARENT_FIELDS}
    spec["algo"] = AlgoType(p.algo).name
    spec["policy"] = ExecPolicy(p.policy).name
    return spec


def spec_to_parent(spec: Mapping[str, Any]) -> ParentOrder:
    kw = {f: spec[f] for f in PARENT_FIELDS}
    return ParentOrder(**kw, algo=AlgoType[spec["algo"]], policy=ExecPolicy[spec["policy"]])


@dataclass
class _Child:
    qty: int
    parent_id: int
    state: str = "SENT"  # SENT, ACKED, CANCELING, DONE
    filled: int = 0


class OrderStateMachine:
    """Validates order events one by one (shared by recorder and loader)."""

    def __init__(self) -> None:
        self.children: dict[int, _Child] = {}
        self.fill_ids: set[int] = set()
        self.last_mono = -(1 << 62)

    def apply(self, ev: Mapping[str, Any]) -> None:
        kind, cid = ev["kind"], ev["child_id"]
        if kind not in KINDS:
            raise RecorderError(f"unknown kind {kind!r}")
        if ev["mono_ns"] < self.last_mono:
            raise RecorderError(f"seq {ev['seq']}: monotonic clock went backwards")
        self.last_mono = ev["mono_ns"]
        if kind == SEND:
            if cid in self.children:
                raise RecorderError(f"child {cid}: duplicate SEND")
            if ev["qty"] <= 0:
                raise RecorderError(f"child {cid}: qty must be > 0")
            self.children[cid] = _Child(qty=ev["qty"], parent_id=ev["parent_id"])
            return
        c = self.children.get(cid)
        if c is None:
            raise RecorderError(f"child {cid}: {kind} before SEND")
        if c.state == "DONE":
            raise RecorderError(f"child {cid}: {kind} after a terminal event")
        if kind == ACK and c.state == "SENT":
            c.state = "ACKED"
        elif kind == REJECT and c.state == "SENT":
            c.state = "DONE"
        elif kind == CANCEL_SEND and c.state == "ACKED":
            c.state = "CANCELING"
        elif kind == CANCEL_ACK and c.state in ("ACKED", "CANCELING"):
            c.state = "DONE"
        elif kind == EXPIRE and c.state in ("ACKED", "CANCELING"):
            c.state = "DONE"
        elif kind == FILL and c.state in ("ACKED", "CANCELING"):
            q = ev["qty"]
            if q <= 0 or c.filled + q > c.qty:
                raise RecorderError(f"child {cid}: fill {q} breaks quantity conservation")
            if ev["fill_id"] in self.fill_ids:
                raise RecorderError(f"child {cid}: duplicate fill_id {ev['fill_id']}")
            self.fill_ids.add(ev["fill_id"])
            c.filled += q
            if c.filled == c.qty:
                c.state = "DONE"
        else:
            raise RecorderError(f"child {cid}: {kind} invalid in state {c.state}")


class PaperRecorder:
    """Collects one paper run; see the module docstring."""

    def __init__(
        self,
        mapping: ClockMapping | None = None,
        mono: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._mono = mono
        self.mapping = mapping or ClockMapping.sample(mono=mono)
        self.events: list[MarketEvent] = []
        self.orders: list[dict[str, Any]] = []
        self.parents: dict[int, dict[str, Any]] = {}
        self._sm = OrderStateMachine()
        self._last_mono = -(1 << 62)

    # ---------------------------------------------------------------- clock
    def _now(self, mono_ns: int | None) -> int:
        m = self._mono() if mono_ns is None else int(mono_ns)
        if m < self._last_mono:
            raise RecorderError("monotonic clock went backwards")
        self._last_mono = m
        return m

    # --------------------------------------------------------------- market
    def record_market_event(self, ev: MarketEvent, mono_ns: int | None = None) -> MarketEvent:
        """Stamp ``receive_ts`` from the mapped monotonic clock and keep it."""
        m = self._now(mono_ns)
        stamped = dataclasses.replace(ev, receive_ts=self.mapping.to_wall(m))
        self.events.append(stamped)
        return stamped

    # --------------------------------------------------------------- parents
    def register_parent(self, parent: ParentOrder, trace: Mapping[str, str]) -> None:
        """A parent with its trace ids (signal -> risk -> routing); all required."""
        missing = [k for k in TRACE_KEYS if not trace.get(k)]
        if missing:
            raise RecorderError(f"parent {parent.parent_id}: trace ids missing {missing}")
        if parent.parent_id in self.parents:
            raise RecorderError(f"duplicate parent {parent.parent_id}")
        self.parents[parent.parent_id] = {
            "spec": parent_to_spec(parent),
            "trace": {k: str(trace[k]) for k in TRACE_KEYS},
        }

    # ---------------------------------------------------------------- orders
    def _emit(self, kind: str, child_id: int, mono_ns: int | None, **fields: Any) -> dict:
        m = self._now(mono_ns)
        ev: dict[str, Any] = {
            "seq": len(self.orders),
            "kind": kind,
            "child_id": int(child_id),
            "mono_ns": m,
            "ts": self.mapping.to_wall(m),
            "parent_id": 0,
            "qty": 0,
            "price_ticks": 0,
            "venue_id": 0,
            "order_type": "",
            "liquidity": "",
            "fill_id": 0,
            "fee": 0.0,
            "exchange_ts": 0,
            "decision_ts": 0,
            "side": 0,
            "instrument_id": 0,
        }
        ev.update(fields)
        self._sm.apply(ev)
        self.orders.append(ev)
        return ev

    def send(
        self,
        child_id: int,
        parent_id: int,
        *,
        instrument_id: int,
        venue_id: int,
        side: int,
        qty: int,
        order_type: str,
        price_ticks: int,
        decision_ts: int,
        mono_ns: int | None = None,
    ) -> dict:
        """``decision_ts`` is the EVENT-time (exchange clock) the algorithm decided at."""
        if parent_id not in self.parents:
            raise RecorderError(f"child {child_id}: parent {parent_id} not registered")
        return self._emit(
            SEND,
            child_id,
            mono_ns,
            parent_id=parent_id,
            instrument_id=instrument_id,
            venue_id=venue_id,
            side=side,
            qty=qty,
            order_type=order_type,
            price_ticks=price_ticks,
            decision_ts=decision_ts,
        )

    def ack(self, child_id: int, mono_ns: int | None = None) -> dict:
        return self._emit(ACK, child_id, mono_ns)

    def cancel_send(self, child_id: int, mono_ns: int | None = None) -> dict:
        return self._emit(CANCEL_SEND, child_id, mono_ns)

    def cancel_ack(self, child_id: int, mono_ns: int | None = None) -> dict:
        return self._emit(CANCEL_ACK, child_id, mono_ns)

    def reject(self, child_id: int, mono_ns: int | None = None) -> dict:
        return self._emit(REJECT, child_id, mono_ns)

    def expire(self, child_id: int, mono_ns: int | None = None) -> dict:
        return self._emit(EXPIRE, child_id, mono_ns)

    def fill(
        self,
        child_id: int,
        *,
        fill_id: int,
        qty: int,
        price_ticks: int,
        venue_id: int,
        liquidity: str,
        fee: float = 0.0,
        exchange_ts: int = 0,
        mono_ns: int | None = None,
    ) -> dict:
        if liquidity not in ("TAKER", "MAKER"):
            raise RecorderError("liquidity must be TAKER or MAKER")
        return self._emit(
            FILL,
            child_id,
            mono_ns,
            fill_id=fill_id,
            qty=qty,
            price_ticks=price_ticks,
            venue_id=venue_id,
            liquidity=liquidity,
            fee=float(fee),
            exchange_ts=exchange_ts,
        )

    # ----------------------------------------------------------------- write
    def write(self, directory: str | Path) -> Path:
        """Write the three files; returns the manifest path.  Deterministic bytes."""
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        market = encode_iap1(self.events)
        lines = "".join(json.dumps(o, sort_keys=True) + "\n" for o in self.orders)
        orders = lines.encode("utf-8")
        (d / "market.iap1").write_bytes(market)
        (d / "orders.jsonl").write_bytes(orders)
        manifest = {
            "schema": SCHEMA,
            "version": VERSION,
            "clock": {**self.mapping.to_dict(), "note": CLOCK_NOTE},
            "parents": {str(k): v for k, v in sorted(self.parents.items())},
            "counts": {"market_events": len(self.events), "order_events": len(self.orders)},
            "sha256": {
                "market.iap1": hashlib.sha256(market).hexdigest(),
                "orders.jsonl": hashlib.sha256(orders).hexdigest(),
            },
        }
        path = d / "recording.json"
        path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return path


CLOCK_NOTE = (
    "wall = wall_ns0 + (mono - mono_ns0); uncertainty_ns is half the bracket in which "
    "wall_ns0 was read and excludes the host wall clock's offset from true UTC"
)


@dataclass
class Recording:
    manifest: dict[str, Any]
    events: list[MarketEvent]
    orders: list[dict[str, Any]]
    parents: dict[int, ParentOrder] = field(default_factory=dict)

    @property
    def mapping(self) -> ClockMapping:
        c = self.manifest["clock"]
        return ClockMapping(c["wall_ns0"], c["mono_ns0"], c["uncertainty_ns"])


def load_recording(directory: str | Path) -> Recording:
    """Load and re-validate a recording (hashes, counts, state machine, order)."""
    d = Path(directory)
    man = json.loads((d / "recording.json").read_text(encoding="utf-8"))
    if man.get("schema") != SCHEMA or man.get("version") != VERSION:
        raise RecorderError(f"expected schema {SCHEMA!r} version {VERSION}")
    market = (d / "market.iap1").read_bytes()
    orders_b = (d / "orders.jsonl").read_bytes()
    for name, blob in (("market.iap1", market), ("orders.jsonl", orders_b)):
        if hashlib.sha256(blob).hexdigest() != man["sha256"][name]:
            raise RecorderError(f"{name}: sha256 mismatch")
    events = decode_iap1_ex(market).events
    orders = [json.loads(x) for x in orders_b.decode("utf-8").splitlines() if x]
    if (len(events), len(orders)) != (
        man["counts"]["market_events"],
        man["counts"]["order_events"],
    ):
        raise RecorderError("counts do not match the manifest")
    for a, b in zip(events, events[1:], strict=False):
        if b.receive_ts < a.receive_ts:
            raise RecorderError("market events: receive_ts goes backwards")
    sm = OrderStateMachine()
    for i, o in enumerate(orders):
        if o["seq"] != i:
            raise RecorderError(f"orders.jsonl: seq {o['seq']} at line {i}")
        sm.apply(o)
    parents = {int(k): spec_to_parent(v["spec"]) for k, v in man["parents"].items()}
    for o in orders:
        if o["kind"] == SEND and o["parent_id"] not in parents:
            raise RecorderError(f"child {o['child_id']}: unregistered parent")
    return Recording(manifest=man, events=events, orders=orders, parents=parents)


def reports_from_recording(rec: Recording) -> list[ExecutionReport]:
    """Order events as canonical ``ExecutionReport``s (``execution_id`` = seq + 1).

    ``exchange_ts`` is the fill's recorded event time when given and not after
    the receive time, else the mapped time itself (a paper run has no venue
    clock of its own, so this is the local mapped clock - say so when used).
    """
    out: list[ExecutionReport] = []
    state = OrderStateMachine()
    for o in rec.orders:
        state.apply(o)
        k = o["kind"]
        if k in (SEND, CANCEL_SEND):
            continue
        c = state.children[o["child_id"]]
        status = {
            ACK: ExecStatus.NEW,
            CANCEL_ACK: ExecStatus.CANCELED,
            REJECT: ExecStatus.REJECTED,
            EXPIRE: ExecStatus.EXPIRED,
        }.get(k)
        if k == FILL:
            status = ExecStatus.FILLED if c.filled == c.qty else ExecStatus.PARTIAL
        assert status is not None
        ex = o["exchange_ts"] if 0 < o["exchange_ts"] <= o["ts"] else o["ts"]
        out.append(
            ExecutionReport(
                order_id=o["child_id"],
                execution_id=o["seq"] + 1,
                status=status,
                filled_qty=o["qty"] if k == FILL else 0,
                fill_price_ticks=o["price_ticks"] if k == FILL else 0,
                venue_id=o["venue_id"] or c_venue(rec, o["child_id"]),
                exchange_ts=ex,
                receive_ts=o["ts"],
                fees=o["fee"] if k == FILL else 0.0,
            )
        )
    return out


def c_venue(rec: Recording, child_id: int) -> int:
    for o in rec.orders:
        if o["kind"] == SEND and o["child_id"] == child_id:
            return int(o["venue_id"])
    raise RecorderError(f"child {child_id}: no SEND")


def children_of(rec: Recording) -> Sequence[dict[str, Any]]:
    """SEND events in order (the recorded children)."""
    return [o for o in rec.orders if o["kind"] == SEND]
