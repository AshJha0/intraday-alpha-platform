package com.iap.risk;

import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.config.Json;
import com.iap.monitoring.MetricsRegistry;

/**
 * Audit-log emission and full-state snapshot/restore (schema x-version 1),
 * split out of {@code RiskEngine} verbatim — the Java analogue of the Rust
 * port's {@code impl RiskEngine} continued in a separate file
 * ({@code rust/risk/src/audit.rs}). {@code RiskEngine}'s public
 * {@code auditJsonl}/{@code snapshot}/{@code restore} delegate here.
 */
final class RiskAudit {
    private RiskAudit() {
    }

    /** Full audit log as JSONL (one RiskEvent per line, trailing newline). */
    static String auditJsonl(RiskEngine e) {
        StringBuilder sb = new StringBuilder(e.audit.size() * 96);
        for (RiskEvent ev : e.audit) {
            sb.append(ev.toJsonLine()).append('\n');
        }
        return sb.toString();
    }

    static void emit(RiskEngine e, RiskEvent ev) {
        e.metrics.counter("risk_events_total").inc();
        e.audit.add(ev);
    }

    private static void jsonStr(StringBuilder sb, String s) {
        sb.append('"').append(RiskEvent.esc(s)).append('"');
    }

    private static void jsonDouble(StringBuilder sb, double v) {
        if (!Double.isFinite(v)) {
            throw new IllegalStateException("non-finite value in snapshot");
        }
        sb.append(Double.toString(v));
    }

    /**
     * Serialize the full mutable state as a schema-versioned JSON document
     * (same field set and semantics as the Rust {@code snapshot()}; keys
     * sorted; doubles via {@code Double.toString}, exact on round trip).
     * The audit log and metrics are not part of the snapshot.
     */
    static String snapshot(RiskEngine e) {
        StringBuilder sb = new StringBuilder(1024);
        sb.append("{\"bootstrapped\":").append(e.bootstrapped);
        sb.append(",\"buckets\":{");
        boolean first = true;
        for (Map.Entry<String, RiskEngine.Bucket> be : e.buckets.entrySet()) {
            sb.append(first ? "" : ",");
            first = false;
            jsonStr(sb, be.getKey());
            sb.append(":{\"last_ts\":").append(be.getValue().lastTs)
                    .append(",\"primed\":").append(be.getValue().primed)
                    .append(",\"tokens\":");
            jsonDouble(sb, be.getValue().tokens);
            sb.append('}');
        }
        sb.append("},\"kill_global\":").append(e.killGlobal);
        sb.append(",\"kill_instruments\":{");
        first = true;
        for (Map.Entry<Long, Boolean> ke : e.killInstruments.entrySet()) {
            sb.append(first ? "" : ",").append('"').append(ke.getKey())
                    .append("\":").append(ke.getValue());
            first = false;
        }
        sb.append("},\"kill_strategies\":{");
        first = true;
        for (Map.Entry<String, Boolean> ke : e.killStrategies.entrySet()) {
            sb.append(first ? "" : ",");
            first = false;
            jsonStr(sb, ke.getKey());
            sb.append(':').append(ke.getValue());
        }
        sb.append("},\"kill_venues\":{");
        first = true;
        for (Map.Entry<Integer, Boolean> ke : e.killVenues.entrySet()) {
            sb.append(first ? "" : ",").append('"').append(ke.getKey())
                    .append("\":").append(ke.getValue());
            first = false;
        }
        sb.append("},\"loss_override_global\":");
        if (e.lossOverrideGlobal == null) {
            sb.append("null");
        } else {
            jsonDouble(sb, e.lossOverrideGlobal);
        }
        sb.append(",\"loss_override_strategy\":{");
        first = true;
        for (Map.Entry<String, Double> le : e.lossOverrideStrategy.entrySet()) {
            sb.append(first ? "" : ",");
            first = false;
            jsonStr(sb, le.getKey());
            sb.append(':');
            jsonDouble(sb, le.getValue());
        }
        sb.append("},\"lots\":[");
        first = true;
        for (Map.Entry<String, TreeMap<Long, RiskEngine.Lot>> s : e.lots.entrySet()) {
            for (Map.Entry<Long, RiskEngine.Lot> l : s.getValue().entrySet()) {
                sb.append(first ? "" : ",");
                first = false;
                sb.append("{\"avg_price\":");
                jsonDouble(sb, l.getValue().avgPrice);
                sb.append(",\"instrument_id\":").append(l.getKey())
                        .append(",\"pos\":").append(l.getValue().pos)
                        .append(",\"strategy_id\":");
                jsonStr(sb, s.getKey());
                sb.append('}');
            }
        }
        sb.append("],\"market\":{");
        first = true;
        for (Map.Entry<Long, RiskEngine.MarketState> me : e.market.entrySet()) {
            RiskEngine.MarketState m = me.getValue();
            sb.append(first ? "" : ",").append('"').append(me.getKey())
                    .append("\":{\"ask_ticks\":").append(m.askTicks)
                    .append(",\"bid_ticks\":").append(m.bidTicks)
                    .append(",\"gaps\":").append(m.gaps)
                    .append(",\"gated\":").append(m.gated)
                    .append(",\"ts\":").append(m.ts).append('}');
            first = false;
        }
        sb.append("},\"open\":{");
        first = true;
        for (Map.Entry<Long, RiskEngine.OpenOrder> oe : e.open.entrySet()) {
            RiskEngine.OpenOrder o = oe.getValue();
            sb.append(first ? "" : ",").append('"')
                    .append(Long.toUnsignedString(oe.getKey()))
                    .append("\":{\"instrument_id\":").append(o.instrumentId)
                    .append(",\"price_ticks\":").append(o.priceTicks)
                    .append(",\"qty\":").append(o.qty)
                    .append(",\"side\":").append(o.side).append('}');
            first = false;
        }
        sb.append("},\"positions\":{");
        first = true;
        for (Map.Entry<Long, Long> pe : e.positions.entrySet()) {
            sb.append(first ? "" : ",").append('"').append(pe.getKey())
                    .append("\":").append(pe.getValue());
            first = false;
        }
        sb.append("},\"realized\":[");
        first = true;
        for (Map.Entry<String, TreeMap<String, Double>> s : e.realized.entrySet()) {
            for (Map.Entry<String, Double> c : s.getValue().entrySet()) {
                sb.append(first ? "" : ",");
                first = false;
                sb.append("{\"ccy\":");
                jsonStr(sb, c.getKey());
                sb.append(",\"pnl\":");
                jsonDouble(sb, c.getValue());
                sb.append(",\"strategy_id\":");
                jsonStr(sb, s.getKey());
                sb.append('}');
            }
        }
        sb.append("],\"seen_orders\":[");
        first = true;
        for (Map.Entry<Long, Long> se : e.seenOrders.entrySet()) {
            sb.append(first ? "" : ",").append('[')
                    .append(Long.toUnsignedString(se.getKey())).append(',')
                    .append(se.getValue()).append(']');
            first = false;
        }
        sb.append("],\"venues_down\":{");
        first = true;
        for (Map.Entry<Integer, Boolean> ve : e.venuesDown.entrySet()) {
            sb.append(first ? "" : ",").append('"').append(ve.getKey())
                    .append("\":").append(ve.getValue());
            first = false;
        }
        sb.append("},\"x-version\":").append(RiskEngine.SNAPSHOT_VERSION).append('}');
        return sb.toString();
    }

    private static IllegalArgumentException bad(String what) {
        return new IllegalArgumentException("risk snapshot: bad " + what);
    }

    private static long snapLong(Object v, String what) {
        if (!(v instanceof Long)) {
            throw bad(what);
        }
        return (Long) v;
    }

    private static boolean snapBool(Object v, String what) {
        if (!(v instanceof Boolean)) {
            throw bad(what);
        }
        return (Boolean) v;
    }

    private static double snapDouble(Object v, String what) {
        if (!(v instanceof Long) && !(v instanceof Double)) {
            throw bad(what);
        }
        double d = Json.asDouble(v);
        if (!Double.isFinite(d)) {
            throw bad(what);
        }
        return d;
    }

    private static String snapStr(Object v, String what) {
        if (!(v instanceof String)) {
            throw bad(what);
        }
        return (String) v;
    }

    private static Map<String, Object> snapObj(Object v, String what) {
        if (!(v instanceof Map)) {
            throw bad(what);
        }
        return Json.object(v);
    }

    private static List<Object> snapArr(Object v, String what) {
        if (!(v instanceof List)) {
            throw bad(what);
        }
        return Json.array(v);
    }

    /**
     * Rebuild an engine from limits, reference data and a parsed
     * {@code snapshot} document (strict: unknown version or a malformed
     * field throws, nothing is restored). Emits {@code STATE_RESTORED}.
     */
    static RiskEngine restore(RiskLimits limits,
            Map<Long, InstrumentRef> instruments, Map<String, Object> snap,
            long ts, MetricsRegistry metrics) {
        if (snapLong(snap.get("x-version"), "x-version") != RiskEngine.SNAPSHOT_VERSION) {
            throw bad("x-version");
        }
        RiskEngine eng = new RiskEngine(limits, instruments, metrics);
        eng.bootstrapped = snapBool(snap.get("bootstrapped"), "bootstrapped");
        eng.setKillGlobal(snapBool(snap.get("kill_global"), "kill_global"));
        for (Map.Entry<String, Object> e
                : snapObj(snap.get("kill_strategies"), "kill_strategies").entrySet()) {
            eng.killStrategies.put(e.getKey(), snapBool(e.getValue(), "kill_strategies"));
        }
        for (Map.Entry<String, Object> e
                : snapObj(snap.get("kill_instruments"), "kill_instruments").entrySet()) {
            eng.killInstruments.put(parseU32(e.getKey(), "kill_instruments"),
                    snapBool(e.getValue(), "kill_instruments"));
        }
        for (Map.Entry<String, Object> e
                : snapObj(snap.get("kill_venues"), "kill_venues").entrySet()) {
            eng.killVenues.put(parseU16(e.getKey(), "kill_venues"),
                    snapBool(e.getValue(), "kill_venues"));
        }
        for (Map.Entry<String, Object> e
                : snapObj(snap.get("venues_down"), "venues_down").entrySet()) {
            eng.venuesDown.put(parseU16(e.getKey(), "venues_down"),
                    snapBool(e.getValue(), "venues_down"));
        }
        for (Map.Entry<String, Object> e
                : snapObj(snap.get("market"), "market").entrySet()) {
            Map<String, Object> m = snapObj(e.getValue(), "market");
            RiskEngine.MarketState st = new RiskEngine.MarketState();
            st.bidTicks = snapLong(m.get("bid_ticks"), "market.bid_ticks");
            st.askTicks = snapLong(m.get("ask_ticks"), "market.ask_ticks");
            st.ts = snapLong(m.get("ts"), "market.ts");
            st.gaps = snapLong(m.get("gaps"), "market.gaps");
            st.gated = snapBool(m.get("gated"), "market.gated");
            eng.market.put(parseU32(e.getKey(), "market"), st);
        }
        for (Object pair : snapArr(snap.get("seen_orders"), "seen_orders")) {
            List<Object> p = snapArr(pair, "seen_orders");
            if (p.size() != 2) {
                throw bad("seen_orders");
            }
            eng.seenOrders.put(snapLong(p.get(0), "seen_orders"),
                    snapLong(p.get(1), "seen_orders"));
        }
        for (Map.Entry<String, Object> e
                : snapObj(snap.get("buckets"), "buckets").entrySet()) {
            Map<String, Object> b = snapObj(e.getValue(), "buckets");
            RiskEngine.Bucket bk = new RiskEngine.Bucket();
            bk.tokens = snapDouble(b.get("tokens"), "buckets.tokens");
            bk.lastTs = snapLong(b.get("last_ts"), "buckets.last_ts");
            bk.primed = snapBool(b.get("primed"), "buckets.primed");
            eng.buckets.put(e.getKey(), bk);
        }
        for (Map.Entry<String, Object> e : snapObj(snap.get("open"), "open").entrySet()) {
            Map<String, Object> o = snapObj(e.getValue(), "open");
            RiskEngine.OpenOrder r = new RiskEngine.OpenOrder();
            r.instrumentId = parseU32Value(o.get("instrument_id"), "open.instrument_id");
            long side = snapLong(o.get("side"), "open.side");
            if (side < 0 || side > 1) {
                throw bad("open.side");
            }
            r.side = (int) side;
            r.priceTicks = snapLong(o.get("price_ticks"), "open.price_ticks");
            r.qty = snapLong(o.get("qty"), "open.qty");
            long id;
            try {
                id = Long.parseUnsignedLong(e.getKey());
            } catch (NumberFormatException ex) {
                throw bad("open");
            }
            eng.open.put(id, r);
        }
        for (Map.Entry<String, Object> e
                : snapObj(snap.get("positions"), "positions").entrySet()) {
            eng.positions.put(parseU32(e.getKey(), "positions"),
                    snapLong(e.getValue(), "positions"));
        }
        for (Object lv : snapArr(snap.get("lots"), "lots")) {
            Map<String, Object> l = snapObj(lv, "lots");
            RiskEngine.Lot lot = new RiskEngine.Lot();
            lot.pos = snapLong(l.get("pos"), "lots.pos");
            lot.avgPrice = snapDouble(l.get("avg_price"), "lots.avg_price");
            eng.lots.computeIfAbsent(snapStr(l.get("strategy_id"), "lots.strategy_id"),
                    k -> new TreeMap<>())
                    .put(parseU32Value(l.get("instrument_id"), "lots.instrument_id"), lot);
        }
        for (Object rv : snapArr(snap.get("realized"), "realized")) {
            Map<String, Object> r = snapObj(rv, "realized");
            eng.realized.computeIfAbsent(snapStr(r.get("strategy_id"),
                    "realized.strategy_id"), k -> new TreeMap<>())
                    .put(snapStr(r.get("ccy"), "realized.ccy"),
                            snapDouble(r.get("pnl"), "realized.pnl"));
        }
        Object og = snap.get("loss_override_global");
        eng.lossOverrideGlobal = og == null ? null
                : snapDouble(og, "loss_override_global");
        for (Map.Entry<String, Object> e : snapObj(snap.get("loss_override_strategy"),
                "loss_override_strategy").entrySet()) {
            eng.lossOverrideStrategy.put(e.getKey(),
                    snapDouble(e.getValue(), "loss_override_strategy"));
        }
        eng.refreshPnlGauges();
        int nPos = 0;
        for (long p : eng.positions.values()) {
            if (p != 0) {
                nPos++;
            }
        }
        eng.emit(new RiskEvent(ts, Scope.GLOBAL, "", Rules.STATE_RESTORED,
                Severity.INFO.code(), Decision.ALLOW.code(),
                "restored snapshot v" + RiskEngine.SNAPSHOT_VERSION + ": " + nPos
                        + " positions, " + eng.open.size() + " open orders"));
        return eng;
    }

    private static long parseU32(String s, String what) {
        try {
            return Integer.toUnsignedLong(Integer.parseUnsignedInt(s));
        } catch (NumberFormatException e) {
            throw bad(what);
        }
    }

    private static long parseU32Value(Object v, String what) {
        long x = snapLong(v, what);
        if (x < 0 || x > 0xFFFFFFFFL) {
            throw bad(what);
        }
        return x;
    }

    private static int parseU16(String s, String what) {
        try {
            int v = Integer.parseInt(s);
            if (v < 0 || v > 0xFFFF) {
                throw bad(what);
            }
            return v;
        } catch (NumberFormatException e) {
            throw bad(what);
        }
    }
}
