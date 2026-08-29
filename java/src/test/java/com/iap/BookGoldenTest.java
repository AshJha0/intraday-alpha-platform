package com.iap;

import static org.junit.Assert.assertEquals;

import java.util.List;
import java.util.Map;

import org.junit.Test;

import com.iap.core.MarketEvent;
import com.iap.orderbook.BookState;
import com.iap.orderbook.OrderBook;

/**
 * Golden book states: applying the first N events of events_eq_mbo.jsonl must
 * reproduce expected_book_states.json exactly (integer equality, no epsilons).
 */
public class BookGoldenTest {

    private static BookState stateAfter(int n) {
        Map<String, Object> g = Golden.json("expected_book_states.json");
        OrderBook book = new OrderBook(Json.asLong(g.get("instrument_id")),
                (int) Json.asLong(g.get("venue_id")));
        List<MarketEvent> events = Golden.eq();
        for (int i = 0; i < n; i++) {
            book.apply(events.get(i));
        }
        return book.stateSummary();
    }

    private static void check(int n) {
        Map<String, Object> g = Golden.json("expected_book_states.json");
        Map<String, Object> states = Json.object(g.get("states"));
        BookState expected = Golden.bookState(Json.object(states.get(Integer.toString(n))));
        assertEquals("book state after " + n + " events", expected, stateAfter(n));
    }

    @Test
    public void stateAfter100EventsExact() {
        check(100);
    }

    @Test
    public void stateAfter500EventsExact() {
        check(500);
    }

    @Test
    public void stateAfter1000EventsExact() {
        check(1000);
    }

    @Test
    public void stateAfter1500EventsExact() {
        check(1500);
    }

    @Test
    public void stateAfter2000EventsExact() {
        check(2000);
    }

    @Test
    public void allPinnedIndicesCoveredByGoldenFile() {
        Map<String, Object> g = Golden.json("expected_book_states.json");
        Map<String, Object> states = Json.object(g.get("states"));
        assertEquals(List.of("100", "500", "1000", "1500", "2000"),
                List.copyOf(states.keySet()));
    }

    @Test
    public void fxVectorFinalStateMatchesPythonReference() {
        // Values computed with the validated Python reference implementation
        // (python/src/iap) replaying events_fx_quote.jsonl.
        com.iap.orderbook.ConsolidatedBook cons =
                new com.iap.orderbook.ConsolidatedBook(101);
        for (MarketEvent ev : Golden.fx()) {
            cons.apply(ev);
        }
        assertEquals(-9L, cons.tradeFlow());
        org.junit.Assert.assertArrayEquals(new long[] {108678, 11}, cons.bestBid());
        org.junit.Assert.assertArrayEquals(new long[] {108679, 14}, cons.bestAsk());
        assertEquals(List.of(10, 11, 12), List.copyOf(cons.venues().keySet()));
        assertEquals(252L, cons.venues().get(10).lastSequence());
        assertEquals(303L, cons.venues().get(11).lastSequence());
        assertEquals(245L, cons.venues().get(12).lastSequence());
        for (OrderBook b : cons.venues().values()) {
            assertEquals(2, b.orderCountTotal()); // QUOTE books: one order per side
        }
    }
}
