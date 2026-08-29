package com.iap;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.List;
import java.util.Map;

import com.iap.codec.JsonlCodec;
import com.iap.core.MarketEvent;
import com.iap.orderbook.BookState;

/** Package-private helpers for loading tests/golden files (cached). */
final class Golden {
    static final Path DIR = Paths.get("..", "tests", "golden");

    private static List<MarketEvent> eqEvents;
    private static List<MarketEvent> fxEvents;

    private Golden() {
    }

    static synchronized List<MarketEvent> eq() {
        if (eqEvents == null) {
            eqEvents = load("events_eq_mbo.jsonl");
        }
        return eqEvents;
    }

    static synchronized List<MarketEvent> fx() {
        if (fxEvents == null) {
            fxEvents = load("events_fx_quote.jsonl");
        }
        return fxEvents;
    }

    private static List<MarketEvent> load(String name) {
        try {
            return List.copyOf(JsonlCodec.read(DIR.resolve(name)));
        } catch (IOException e) {
            throw new IllegalStateException("cannot load golden " + name, e);
        }
    }

    static byte[] bytes(String name) {
        try {
            return Files.readAllBytes(DIR.resolve(name));
        } catch (IOException e) {
            throw new IllegalStateException("cannot load golden " + name, e);
        }
    }

    static Map<String, Object> json(String name) {
        return Json.object(Json.parse(new String(bytes(name), StandardCharsets.UTF_8)));
    }

    /** Convert one expected_book_states.json state object into a BookState. */
    static BookState bookState(Map<String, Object> st) {
        return new BookState(
                Json.asLong(st.get("best_bid_ticks")),
                Json.asLong(st.get("best_bid_size")),
                Json.asLong(st.get("best_ask_ticks")),
                Json.asLong(st.get("best_ask_size")),
                pairs(st.get("depth_bid_top5")),
                pairs(st.get("depth_ask_top5")),
                pairs(st.get("order_count_bid_top3")),
                pairs(st.get("order_count_ask_top3")),
                Json.asLong(st.get("trade_flow")),
                Json.asLong(st.get("sequence")));
    }

    static long[][] pairs(Object v) {
        List<Object> rows = Json.array(v);
        long[][] out = new long[rows.size()][];
        for (int i = 0; i < rows.size(); i++) {
            List<Object> row = Json.array(rows.get(i));
            long[] pair = new long[row.size()];
            for (int j = 0; j < row.size(); j++) {
                pair[j] = Json.asLong(row.get(j));
            }
            out[i] = pair;
        }
        return out;
    }
}
