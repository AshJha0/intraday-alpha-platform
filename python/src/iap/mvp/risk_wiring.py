"""Risk wiring of the MVP engine (steps 1, 3 and 4 of :mod:`iap.mvp.engine`):
execution reports -> account + risk engine, per-venue stale transitions and
the reference mark, and the TCA market timeline.  Split out of
:class:`~iap.mvp.engine.MvpEngine` as a mixin (v1.13 E4); no behaviour change.
"""

from __future__ import annotations

from collections.abc import Sequence

from iap.contracts.types import (
    ExecStatus,
    ExecutionReport,
)
from iap.core.events import EventType, MarketEvent, SessionStatus
from iap.mvp.orders import _LiveParent
from iap.risk.orders import Fill as RiskFill


class RiskWiringMixin:
    """Mixin of :class:`~iap.mvp.engine.MvpEngine` (module docstring); it
    reads and writes the engine's own attributes."""

    # -------------------------------------------------------------- reports

    def _process_reports(self, reports: Sequence[ExecutionReport]) -> None:
        filled = False
        for rep in reports:
            live = self._owner_of(rep.order_id)
            if live is not None:
                live.reports.append(rep)
                live.decision.builder.add_fill(rep)
            if rep.status in (ExecStatus.PARTIAL, ExecStatus.FILLED):
                fill = self.sim.fills_by_execution[rep.execution_id]
                price = float(rep.fill_price_ticks) * self.tick
                self.account.book_fill(
                    int(fill.side), rep.filled_qty, price, rep.fees, fill.impact_cost
                )
                self.counters.fills += 1
                filled = True
                if live is not None:
                    live.fill_fees += rep.fees
                    live.fill_impact += fill.impact_cost
                applied = self.risk_engine.on_fill(
                    RiskFill(
                        ts=rep.exchange_ts,
                        strategy_id=self.strategy_id,
                        instrument_id=self.iid,
                        order_id=rep.order_id,
                        side=int(fill.side),
                        qty=rep.filled_qty,
                        price_ticks=rep.fill_price_ticks,
                    )
                )
                if not applied:
                    # The account booked a fill the risk engine refused as
                    # malformed: the two positions have diverged and the
                    # §12.1 identity is gone — fail closed, never continue.
                    raise RuntimeError(
                        f"risk engine rejected fill execution "
                        f"{rep.execution_id} of child {rep.order_id} as malformed"
                    )
            if rep.status in (
                ExecStatus.FILLED,
                ExecStatus.CANCELED,
                ExecStatus.EXPIRED,
                ExecStatus.REJECTED,
            ):
                self.risk_engine.on_order_done(rep.order_id)
        if filled:
            # §12.1 (PaperUnitsTest): after EVERY fill the risk position equals
            # the account position and the risk daily P&L equals gross - spread.
            self.assert_pnl_identity()

    def _owner_of(self, child_order_id: int) -> _LiveParent | None:
        if self._live is not None and child_order_id in self._live.children:
            return self._live
        for live in self._closing:
            if child_order_id in live.children:
                return live
        return None

    # ---------------------------------------------------------- risk wiring

    def _on_market(self, ev: MarketEvent) -> None:
        """PaperTrading.RiskWiring.onMarket, verbatim semantics."""
        if ev.event_type != EventType.HEARTBEAT:
            self._last_data_ts[ev.venue_id] = ev.exchange_ts
        best_bid: int | None = None
        best_ask: int | None = None
        books = self.book.books
        # The gap gate is per INSTRUMENT but staleness is per venue: one
        # venue's recovery must not reopen the gate while another venue of
        # the same instrument is still stale, so the recovery is reported
        # only once no venue is stale.
        venue_recovered = False
        for vid in sorted(books):
            vb = books[vid]
            stale = vb.stale
            was = self._venue_stale.get(vid, False)
            self._venue_stale[vid] = stale
            if stale and not was:
                self.counters.sequence_gaps += 1
                self.risk_engine.on_sequence_gap(self.iid, ev.exchange_ts)
            elif was and not stale:
                venue_recovered = True
        if venue_recovered and not any(books[vid].stale for vid in books):
            self.counters.feed_recoveries += 1
            self.risk_engine.on_feed_recovered(self.iid, ev.exchange_ts)
        for vid in sorted(books):
            vb = books[vid]
            if vb.stale:
                continue
            bb = vb.best_bid()
            ba = vb.best_ask()
            if bb is not None:
                best_bid = bb[0] if best_bid is None else max(best_bid, bb[0])
            if ba is not None:
                best_ask = ba[0] if best_ask is None else min(best_ask, ba[0])
        if best_bid is None or best_ask is None:
            return  # no fresh two-sided venue: the previous mark ages
        mark_ts: int | None = None
        for vid in sorted(books):
            vb = books[vid]
            if vb.stale:
                continue
            bb = vb.best_bid()
            ba = vb.best_ask()
            at_touch = (bb is not None and bb[0] == best_bid) or (
                ba is not None and ba[0] == best_ask
            )
            data_ts = self._last_data_ts.get(vid)
            if at_touch and data_ts is not None:
                mark_ts = data_ts if mark_ts is None else min(mark_ts, data_ts)
        if mark_ts is None:
            return
        self.risk_engine.on_market(self.iid, best_bid, best_ask, mark_ts)
        # The account marks at exactly the reference the risk engine keeps:
        # an update older than the stored mark is dropped there (and
        # counted, ``risk_market_regressions_dropped_total``), so it is
        # dropped here too — the §12.1 identity then holds at every event.
        if self._mark_ts is not None and mark_ts < self._mark_ts:
            return
        self._mark_ts = mark_ts
        self.account.mark_to(float(best_bid + best_ask) * self.tick / 2.0)

    # ------------------------------------------------------------- timeline

    def _record_timeline(self, ev: MarketEvent) -> None:
        tl = self.timeline
        if ev.event_type == EventType.TRADE:
            tl.add_trade(ev.exchange_ts, ev.price_ticks * self.tick, ev.qty)
        if ev.event_type == EventType.STATUS and ev.qty == int(SessionStatus.HALT):
            tl.add_halt(ev.exchange_ts)
        bb, ba = self.book.best_bid(), self.book.best_ask()
        if bb is None or ba is None:
            return
        if not tl.append_state_pinned(
            ev.exchange_ts, bb[0] * self.tick, ba[0] * self.tick, bb[1], ba[1]
        ):
            self.counters.timeline_crossed_skipped += 1
