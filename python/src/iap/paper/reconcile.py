"""Fill and position reconciliation against an independent fill stream (REL-18).

Drop-copy style: the venue (here :class:`iap.paper.venue_sim.PaperVenue`)
publishes every fill on a second stream that does not pass through the order
manager.  :func:`reconcile` compares that stream with the
:class:`iap.paper.oms.OrderManager` state and returns typed exceptions:

=====================  =======================================================
``MISSING_FILL``       on the drop copy, not booked by the order manager
``UNCONFIRMED_FILL``   booked by the order manager, absent from the drop copy
``DUPLICATE_FILL``     the same ``fill_id`` twice on the drop copy
``QTY_MISMATCH``       same fill, different quantity (or a booked overfill)
``PRICE_MISMATCH``     same fill, different price
``UNKNOWN_ORDER``      a fill for a ``cl_ord_id`` the order manager never sent
``LATE_FILL``          a fill the order manager received after the order ended
``POSITION_MISMATCH``  net position per instrument differs
=====================  =======================================================

Hook for monitoring (REL-16): pass ``sinks`` (anything with
``on_recon_exceptions(report)``) or call :func:`publish`; the report's
``to_dict()`` is plain JSON.  The hook only reads: reconciliation never edits
the order manager.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol

from iap.paper.oms import OrderManager


class ReconKind(str, Enum):
    MISSING_FILL = "MISSING_FILL"
    UNCONFIRMED_FILL = "UNCONFIRMED_FILL"
    DUPLICATE_FILL = "DUPLICATE_FILL"
    QTY_MISMATCH = "QTY_MISMATCH"
    PRICE_MISMATCH = "PRICE_MISMATCH"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    LATE_FILL = "LATE_FILL"
    POSITION_MISMATCH = "POSITION_MISMATCH"


_ANOMALY_KIND = {
    "OVERFILL": ReconKind.QTY_MISMATCH,
    "FILL_UNKNOWN_ORDER": ReconKind.UNKNOWN_ORDER,
    "FILL_AFTER_TERMINAL": ReconKind.LATE_FILL,
}


@dataclass(frozen=True)
class DropCopyFill:
    cl_ord_id: str
    fill_id: int
    instrument_id: int
    side: int
    qty: int
    price_ticks: int


@dataclass(frozen=True)
class ReconException:
    kind: ReconKind
    cl_ord_id: str = ""
    fill_id: int = 0
    instrument_id: int = 0
    expected: int = 0  # drop copy (the independent source)
    observed: int = 0  # order manager
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


@dataclass(frozen=True)
class ReconReport:
    exceptions: tuple[ReconException, ...]
    positions_oms: Mapping[int, int]
    positions_drop_copy: Mapping[int, int]
    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.exceptions

    @property
    def unreconciled_positions(self) -> int:
        return sum(1 for e in self.exceptions if e.kind is ReconKind.POSITION_MISMATCH)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "unreconciled_positions": self.unreconciled_positions,
            "counts": dict(self.counts),
            "exceptions": [e.to_dict() for e in self.exceptions],
            "positions_oms": {str(k): v for k, v in sorted(self.positions_oms.items())},
            "positions_drop_copy": {str(k): v for k, v in sorted(self.positions_drop_copy.items())},
        }


class ReconSink(Protocol):
    """What monitoring (REL-16) implements to receive reconciliation results."""

    def on_recon_exceptions(self, report: ReconReport) -> None: ...


def publish(report: ReconReport, sinks: Sequence[ReconSink]) -> None:
    for s in sinks:
        s.on_recon_exceptions(report)


def reconcile(
    oms: OrderManager,
    drop_copy: Iterable[DropCopyFill],
    sinks: Sequence[ReconSink] = (),
) -> ReconReport:
    """Compare the order manager with the drop copy; deterministic order of exceptions."""
    exc: list[ReconException] = []
    booked: dict[int, tuple[str, int, int]] = {}
    for o in oms.orders.values():
        for fid, q, px in o.fills:
            booked[fid] = (o.cl_ord_id, q, px)
    anomalies = {a.fill_id: a for a in oms.anomalies}
    dc_pos: dict[int, int] = dict(oms.start_positions)
    seen: set[int] = set()
    for f in drop_copy:
        if f.fill_id in seen:
            exc.append(
                ReconException(ReconKind.DUPLICATE_FILL, f.cl_ord_id, f.fill_id, f.instrument_id)
            )
            continue
        seen.add(f.fill_id)
        dc_pos[f.instrument_id] = dc_pos.get(f.instrument_id, 0) + f.side * f.qty
        if f.fill_id in anomalies:
            a = anomalies[f.fill_id]
            exc.append(
                ReconException(
                    _ANOMALY_KIND[a.kind], f.cl_ord_id, f.fill_id, f.instrument_id, f.qty, 0, a.kind
                )
            )
            continue
        if f.cl_ord_id not in oms.orders:
            exc.append(
                ReconException(
                    ReconKind.UNKNOWN_ORDER, f.cl_ord_id, f.fill_id, f.instrument_id, f.qty, 0
                )
            )
            continue
        b = booked.get(f.fill_id)
        if b is None:
            exc.append(
                ReconException(
                    ReconKind.MISSING_FILL, f.cl_ord_id, f.fill_id, f.instrument_id, f.qty, 0
                )
            )
            continue
        if b[1] != f.qty:
            exc.append(
                ReconException(
                    ReconKind.QTY_MISMATCH, f.cl_ord_id, f.fill_id, f.instrument_id, f.qty, b[1]
                )
            )
        if b[2] != f.price_ticks:
            exc.append(
                ReconException(
                    ReconKind.PRICE_MISMATCH,
                    f.cl_ord_id,
                    f.fill_id,
                    f.instrument_id,
                    f.price_ticks,
                    b[2],
                )
            )
    for fid in sorted(set(booked) - seen):
        cl, q, _ = booked[fid]
        exc.append(
            ReconException(ReconKind.UNCONFIRMED_FILL, cl, fid, oms.orders[cl].instrument_id, 0, q)
        )
    for inst in sorted(set(dc_pos) | set(oms.positions)):
        exp, obs = dc_pos.get(inst, 0), oms.positions.get(inst, 0)
        if exp != obs:
            exc.append(
                ReconException(
                    ReconKind.POSITION_MISMATCH, instrument_id=inst, expected=exp, observed=obs
                )
            )
    report = ReconReport(
        exceptions=tuple(exc),
        positions_oms=dict(oms.positions),
        positions_drop_copy=dc_pos,
        counts=dict(Counter(e.kind.value for e in exc)),
    )
    publish(report, sinks)
    return report
