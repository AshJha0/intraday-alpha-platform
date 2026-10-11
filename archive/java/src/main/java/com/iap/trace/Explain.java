package com.iap.trace;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.contracts.PyFormat;

/**
 * Human-readable rendering of a {@link DecisionTrace}
 * ({@code iap.contracts.types.explain}), pinned byte-for-byte by
 * {@code tests/golden/expected_contracts_examples.json}:
 *
 * <pre>
 * Order 12345
 * Alpha:      EQ03  expected return = +4.2 bps  confidence = 0.81
 * Portfolio:  target = +20,000 shares
 * Risk:       ALLOW
 * Execution:  POV 15%
 * SOR:        XV1 = 45%  XV2 = 35%  XV3 = 20%
 * Fills:      18,000 / 20,000 (90.0%)
 * TCA:        IS = 2.1 bps
 * Attribution: alpha = +6.2 bps  spread = -0.8 bps  impact = -2.1 bps  fees = -0.4 bps
 * </pre>
 *
 * Labels are left-justified to 12 characters, stages that did not run
 * render {@code (none)}, the header is {@code Order <parent_order_id>} when
 * a parent order exists and {@code Trace <trace_id>} otherwise; venue ids
 * without a name render as their decimal id.
 */
public final class Explain {
    private static final int LABEL_WIDTH = 12;

    private Explain() {
    }

    private static String line(String label, String body) {
        StringBuilder sb = new StringBuilder(label).append(": ");
        while (sb.length() < LABEL_WIDTH) {
            sb.append(' ');
        }
        return sb.append(body).toString();
    }

    private static String bps(double v) {
        return PyFormat.fixed(v, 1, true) + " bps";
    }

    private static String decisionName(int code) {
        return switch (code) {
            case RiskDecisionRec.ALLOW -> "ALLOW";
            case RiskDecisionRec.REJECT -> "REJECT";
            default -> "KILL";
        };
    }

    /** Render {@code trace}; {@code venueNames} maps venue id to display name. */
    public static String render(DecisionTrace trace, Map<Integer, String> venueNames) {
        TraceStages st = trace.stages();
        List<ParentOrderRec> parents = st.parentOrders();
        Map<Integer, String> names = venueNames == null ? Map.of() : venueNames;
        List<String> lines = new ArrayList<>(9);
        lines.add(parents.isEmpty() ? "Trace " + trace.traceId()
                : "Order " + Long.toUnsignedString(parents.get(0).parentOrderId()));
        String alphaLabel = parents.isEmpty() ? null : parents.get(0).alphaId();

        if (st.signal().isEmpty()) {
            lines.add(line("Alpha", "(none)"));
        } else {
            // signal[0] is the acting signal (labelled by the order's alpha id);
            // every further signal is one of its components (its own model_version).
            List<AlphaSignalRec> signals = st.signal();
            for (int i = 0; i < signals.size(); i++) {
                AlphaSignalRec sig = signals.get(i);
                String label = (i == 0 && alphaLabel != null) ? alphaLabel : sig.modelVersion();
                lines.add(line("Alpha", label
                        + "  expected return = " + bps(sig.expectedReturn() * 1e4)
                        + "  confidence = " + PyFormat.fixed(sig.confidence(), 2)));
            }
        }

        PortfolioTargetRec pf = st.portfolio();
        if (pf == null) {
            lines.add(line("Portfolio", "(none)"));
        } else {
            PortfolioTargetRec.Leg leg = null;
            for (PortfolioTargetRec.Leg l : pf.targets()) {
                if (l.instrumentId() == trace.instrumentId()) {
                    leg = l;
                    break;
                }
            }
            if (leg == null && !pf.targets().isEmpty()) {
                leg = pf.targets().get(0);
            }
            lines.add(line("Portfolio", leg == null
                    ? pf.solverStatus() + "  no target"
                    : "target = " + PyFormat.grouped(leg.targetQty(), true) + " shares"));
        }

        if (st.risk().isEmpty()) {
            lines.add(line("Risk", "(none)"));
        } else {
            for (RiskDecisionRec rd : st.risk()) {
                String body = decisionName(rd.decision());
                if (rd.decision() != RiskDecisionRec.ALLOW) {
                    body += "  rule = " + rd.ruleId() + "  reason = " + rd.reason();
                }
                lines.add(line("Risk", body));
            }
        }

        if (parents.isEmpty()) {
            lines.add(line("Execution", "(none)"));
        } else {
            for (ParentOrderRec po : parents) {
                String body = po.algo();
                Double part = po.params().get("participation");
                if (part != null) {
                    body += " " + PyFormat.fixed(part * 100.0, 0) + "%";
                }
                lines.add(line("Execution", body));
            }
        }

        TreeMap<Long, Long> childQty = new TreeMap<>();
        for (ChildOrderRec c : st.childOrders()) {
            childQty.put(c.childOrderId(), c.qty());
        }
        TreeMap<Integer, Long> routed = new TreeMap<>();
        for (VenueDecisionRec vd : st.routing()) {
            Long q = childQty.get(vd.childOrderId());
            routed.merge(vd.venueId(), q == null ? 0L : q, Long::sum);
        }
        long totalRouted = 0;
        for (long q : routed.values()) {
            totalRouted += q;
        }
        if (totalRouted == 0) {
            lines.add(line("SOR", "(none)"));
        } else {
            StringBuilder sb = new StringBuilder();
            for (Map.Entry<Integer, Long> e : routed.entrySet()) {
                if (sb.length() > 0) {
                    sb.append("  ");
                }
                String name = names.get(e.getKey());
                sb.append(name != null ? name : Integer.toString(e.getKey()))
                        .append(" = ")
                        .append(PyFormat.fixed(100.0 * e.getValue() / totalRouted, 0))
                        .append('%');
            }
            lines.add(line("SOR", sb.toString()));
        }

        long targetQty = 0;
        for (ParentOrderRec po : parents) {
            targetQty += po.qty();
        }
        long filled = 0;
        for (ExecutionReportRec er : st.fills()) {
            filled += er.filledQty();
        }
        if (targetQty == 0) {
            lines.add(line("Fills", "(none)"));
        } else {
            lines.add(line("Fills", PyFormat.grouped(filled, false) + " / "
                    + PyFormat.grouped(targetQty, false) + " ("
                    + PyFormat.fixed(100.0 * filled / targetQty, 1) + "%)"));
        }

        if (st.tca().isEmpty()) {
            lines.add(line("TCA", "(none)"));
        } else {
            for (TCAResultRec t : st.tca()) {
                lines.add(line("TCA", "IS = "
                        + PyFormat.fixed(t.implementationShortfallBps(), 1) + " bps"));
            }
        }

        Attribution a = st.attribution();
        if (a == null) {
            lines.add(line("Attribution", "(none)"));
        } else {
            lines.add(line("Attribution", "alpha = " + bps(a.alphaBps())
                    + "  spread = " + bps(a.spreadBps())
                    + "  impact = " + bps(a.impactBps())
                    + "  fees = " + bps(a.feesBps())));
        }
        return String.join("\n", lines);
    }
}
