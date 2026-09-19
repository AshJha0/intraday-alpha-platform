package com.iap.trace;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

import com.iap.contracts.Trees;

/**
 * {@code decision_trace.schema.json#/$defs/TraceStages}: everything the loop
 * produced for one decision, stage by stage. Empty lists / {@code null}
 * mean the stage did not run (no orders after a REJECT, no TCA before the
 * parent ended).
 */
public record TraceStages(List<AlphaSignalRec> signal, PortfolioTargetRec portfolio,
        List<RiskDecisionRec> risk, List<ParentOrderRec> parentOrders,
        List<ChildOrderRec> childOrders, List<VenueDecisionRec> routing,
        List<ExecutionReportRec> fills, List<TCAResultRec> tca,
        Attribution attribution) {
    private static final String[] KEYS = {"signal", "portfolio", "risk",
        "parent_orders", "child_orders", "routing", "fills", "tca", "attribution"};

    public TraceStages {
        signal = List.copyOf(signal);
        risk = List.copyOf(risk);
        parentOrders = List.copyOf(parentOrders);
        childOrders = List.copyOf(childOrders);
        routing = List.copyOf(routing);
        fills = List.copyOf(fills);
        tca = List.copyOf(tca);
    }

    private static <T> List<Object> trees(List<T> items, Function<T, Map<String, Object>> f) {
        List<Object> out = new ArrayList<>(items.size());
        for (T item : items) {
            out.add(f.apply(item));
        }
        return out;
    }

    private static <T> List<T> parse(Map<String, Object> t, String key, String p,
            Function<Map<String, Object>, T> f) {
        List<T> out = new ArrayList<>();
        for (Object o : Trees.arr(t, key, p)) {
            out.add(f.apply(Trees.obj(o, p + "." + key + "[]")));
        }
        return out;
    }

    /** Schema-ordered tree. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("signal", trees(signal, AlphaSignalRec::toTree));
        t.put("portfolio", portfolio == null ? null : portfolio.toTree());
        t.put("risk", trees(risk, RiskDecisionRec::toTree));
        t.put("parent_orders", trees(parentOrders, ParentOrderRec::toTree));
        t.put("child_orders", trees(childOrders, ChildOrderRec::toTree));
        t.put("routing", trees(routing, VenueDecisionRec::toTree));
        t.put("fills", trees(fills, ExecutionReportRec::toTree));
        t.put("tca", trees(tca, TCAResultRec::toTree));
        t.put("attribution", attribution == null ? null : attribution.toTree());
        return t;
    }

    /** Strict inverse of {@link #toTree}. */
    public static TraceStages fromTree(Map<String, Object> t) {
        String p = "TraceStages";
        Trees.checkKeys(t, KEYS, p);
        Object pf = t.get("portfolio");
        Object at = t.get("attribution");
        return new TraceStages(parse(t, "signal", p, AlphaSignalRec::fromTree),
                pf == null ? null : PortfolioTargetRec.fromTree(Trees.obj(pf, p + ".portfolio")),
                parse(t, "risk", p, RiskDecisionRec::fromTree),
                parse(t, "parent_orders", p, ParentOrderRec::fromTree),
                parse(t, "child_orders", p, ChildOrderRec::fromTree),
                parse(t, "routing", p, VenueDecisionRec::fromTree),
                parse(t, "fills", p, ExecutionReportRec::fromTree),
                parse(t, "tca", p, TCAResultRec::fromTree),
                at == null ? null : Attribution.fromTree(Trees.obj(at, p + ".attribution")));
    }
}
