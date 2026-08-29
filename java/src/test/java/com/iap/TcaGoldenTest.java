package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import org.junit.Test;

import com.iap.tca.Tca;

/**
 * Golden parity of the Perold implementation-shortfall decomposition with
 * tests/golden/expected_tca.json (1e-9), the exact IS identity, and the
 * pinned edge semantics (pure opportunity cost when unfilled).
 */
public class TcaGoldenTest {
    private static List<Map<String, Object>> cases() {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Object c : Json.array(Golden.json("expected_tca.json").get("cases"))) {
            out.add(Json.object(c));
        }
        return out;
    }

    private static Tca.Perold run(Map<String, Object> inputs) {
        List<double[]> fills = new ArrayList<>();
        for (Object f : Json.array(inputs.get("fills"))) {
            List<Object> pair = Json.array(f);
            fills.add(new double[] {Json.asDouble(pair.get(0)),
                    Json.asDouble(pair.get(1))});
        }
        return Tca.peroldDecomposition(
                (int) Json.asLong(inputs.get("side_sign")),
                Json.asLong(inputs.get("qty_target")), fills,
                Json.asDouble(inputs.get("decision_mid")),
                Json.asDouble(inputs.get("arrival_mid")),
                Json.asDouble(inputs.get("end_mid")));
    }

    @Test
    public void allGoldenCasesMatchTo1e9() {
        double tol = Json.asDouble(
                Golden.json("expected_tca.json").get("tolerance"));
        List<Map<String, Object>> cs = cases();
        assertEquals(4, cs.size());
        for (Map<String, Object> c : cs) {
            Map<String, Object> in = Json.object(c.get("inputs"));
            Map<String, Object> exp = Json.object(c.get("expected"));
            String name = (String) in.get("name");
            Tca.Perold got = run(in);
            assertEquals(name + " qty_target",
                    Json.asDouble(exp.get("qty_target")), got.qtyTarget(), tol);
            assertEquals(name + " qty_filled",
                    Json.asDouble(exp.get("qty_filled")), got.qtyFilled(), tol);
            assertEquals(name + " fill_rate",
                    Json.asDouble(exp.get("fill_rate")), got.fillRate(), tol);
            assertEquals(name + " delay_cost",
                    Json.asDouble(exp.get("delay_cost")), got.delayCost(), tol);
            assertEquals(name + " trading_cost",
                    Json.asDouble(exp.get("trading_cost")), got.tradingCost(), tol);
            assertEquals(name + " opportunity_cost",
                    Json.asDouble(exp.get("opportunity_cost")),
                    got.opportunityCost(), tol);
            assertEquals(name + " total_is",
                    Json.asDouble(exp.get("total_is")), got.totalIs(), tol);
            assertEquals(name + " delay_bps",
                    Json.asDouble(exp.get("delay_bps")), got.delayBps(), tol);
            assertEquals(name + " trading_bps",
                    Json.asDouble(exp.get("trading_bps")), got.tradingBps(), tol);
            assertEquals(name + " opportunity_bps",
                    Json.asDouble(exp.get("opportunity_bps")),
                    got.opportunityBps(), tol);
            assertEquals(name + " total_is_bps",
                    Json.asDouble(exp.get("total_is_bps")), got.totalIsBps(), tol);
        }
    }

    @Test
    public void isIdentityHoldsExactly() {
        for (Map<String, Object> c : cases()) {
            Tca.Perold p = run(Json.object(c.get("inputs")));
            assertEquals("delay + trading + opportunity == total (exact)",
                    p.delayCost() + p.tradingCost() + p.opportunityCost(),
                    p.totalIs(), 0.0);
            assertEquals("bps identity", p.delayBps() + p.tradingBps()
                    + p.opportunityBps(), p.totalIsBps(), 1e-12);
        }
    }

    @Test
    public void unfilledOrderIsPureOpportunityCost() {
        Tca.Perold p = Tca.peroldDecomposition(1, 1000, List.of(), 50.0,
                50.01, 50.2);
        assertEquals(0.0, p.delayCost(), 0.0);
        assertEquals(0.0, p.tradingCost(), 0.0);
        assertEquals(p.totalIs(), p.opportunityCost(), 0.0);
        assertEquals(0.0, p.fillRate(), 0.0);
        assertTrue(p.opportunityCost() > 0.0);
    }

    @Test
    public void invalidInputsAreRejected() {
        try {
            Tca.peroldDecomposition(0, 100, List.of(), 50.0, 50.0, 50.0);
            fail("side_sign 0");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("side_sign"));
        }
        try {
            Tca.peroldDecomposition(1, 0, List.of(), 50.0, 50.0, 50.0);
            fail("qty_target 0");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("qty_target"));
        }
        try {
            Tca.peroldDecomposition(1, 100,
                    List.of(new double[] {50.0, 200.0}), 50.0, 50.0, 50.0);
            fail("overfill");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("more than target"));
        }
        try {
            Tca.peroldDecomposition(1, 100, List.of(), 0.0, 50.0, 50.0);
            fail("decision_mid 0");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("decision_mid"));
        }
    }
}
