package com.iap.tca;

import java.util.List;

import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.core.Side;
import com.iap.orderbook.ConsolidatedBook;

/**
 * Production TCA service (spec §19): builds the market timeline from the
 * normalized event stream (the same consolidated-book semantics as the
 * backtester) and analyzes parent orders assembled from the backtest
 * engine's {@link com.iap.execution.Fill} records.
 */
public final class TcaService {
    private TcaService() {
    }

    /**
     * Build a per-instrument {@link MarketTimeline} by replaying events
     * through a consolidated book: after every event of the instrument with
     * a two-sided book, the BBO state is appended at {@code exchange_ts};
     * TRADE events are recorded as market prints.
     */
    public static MarketTimeline timelineFromEvents(List<MarketEvent> events,
            long instrumentId, double tickSize) {
        if (tickSize <= 0.0) {
            throw new IllegalArgumentException("tick_size must be > 0");
        }
        ConsolidatedBook book = new ConsolidatedBook(instrumentId);
        MarketTimeline timeline = new MarketTimeline();
        for (MarketEvent ev : events) {
            if (ev.instrumentId != instrumentId) {
                continue;
            }
            book.apply(ev);
            if (ev.eventType == EventType.TRADE) {
                timeline.addTrade(ev.exchangeTs,
                        (double) ev.priceTicks * tickSize, ev.qty);
            }
            long[] bb = book.bestBid();
            long[] ba = book.bestAsk();
            if (bb != null && ba != null) {
                timeline.append(ev.exchangeTs, (double) bb[0] * tickSize,
                        (double) ba[0] * tickSize, bb[1], ba[1]);
            }
        }
        return timeline;
    }

    /**
     * Assemble a {@link TcaParentOrder} from execution-simulator fills:
     * each fill is stamped with the prevailing mid / half-spread at its
     * timestamp and the displayed contra depth (fail if a fill precedes the
     * first market state — TCA never guesses reference prices).
     */
    public static TcaParentOrder parentFromFills(long orderId,
            long instrumentId, int side, long qtyTarget, long decisionTs,
            long arrivalTs, long endTs, List<com.iap.execution.Fill> fills,
            double tickSize, MarketTimeline timeline) {
        TcaParentOrder parent = new TcaParentOrder(orderId, instrumentId, side,
                qtyTarget, decisionTs, arrivalTs, endTs);
        for (com.iap.execution.Fill f : fills) {
            int i = timeline.prevailing(f.ts());
            if (i < 0) {
                throw new IllegalArgumentException(
                        "fill at " + f.ts() + " precedes the first market state");
            }
            long oppDepth = f.side() == Side.BID
                    ? timeline.askSize(i) : timeline.bidSize(i);
            parent.fills.add(new TcaFill(f.ts(),
                    (double) f.priceTicks() * tickSize, f.qty(),
                    timeline.mid(i), timeline.halfSpread(i), oppDepth));
        }
        return parent;
    }

    /** Analyze one parent order (full spec §19 record). */
    public static Tca.OrderTca analyze(TcaParentOrder order,
            MarketTimeline timeline) {
        return Tca.orderTca(order, timeline);
    }
}
