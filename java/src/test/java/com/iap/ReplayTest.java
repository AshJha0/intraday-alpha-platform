package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.util.ArrayList;
import java.util.List;

import org.junit.Test;

import com.iap.core.MarketEvent;
import com.iap.replay.ReplayEngine;

/** Deterministic replay + checkpoint/restart (API_CORE.md section 5). */
public class ReplayTest {

    private static List<MarketEvent> both() {
        List<MarketEvent> all = new ArrayList<>(Golden.eq());
        all.addAll(Golden.fx());
        return all;
    }

    @Test
    public void replayIsDeterministic() {
        ReplayEngine a = new ReplayEngine();
        ReplayEngine b = new ReplayEngine();
        a.run(both());
        b.run(both());
        assertEquals(a.checkpoint(), b.checkpoint());
        assertEquals(a.bookStates(), b.bookStates());
    }

    @Test
    public void runSummaryCountsInstrumentsAndRegressions() {
        ReplayEngine engine = new ReplayEngine();
        ReplayEngine.Summary summary = engine.run(both());
        assertEquals(2800, summary.eventsProcessed());
        assertEquals(2, summary.instruments());
        // The fx vector starts earlier in event time than the eq vector ends.
        assertEquals(1, summary.timeRegressions());
        assertEquals(0, summary.snapshots());
    }

    @Test
    public void snapshotCadenceAndIndices() {
        ReplayEngine engine = new ReplayEngine(0, 250, 4);
        engine.run(Golden.eq());
        assertEquals(8, engine.snapshots().size());
        for (int i = 0; i < 8; i++) {
            assertEquals(250L * (i + 1), engine.snapshots().get(i).index);
        }
        // Snapshot content equals the golden shape source: instrument 1, venue 1.
        ReplayEngine.Snapshot last = engine.snapshots().get(7);
        assertEquals(engine.instruments().get(1L).venues().get(1).stateSummary(),
                last.instruments.get(1L).get(1));
    }

    @Test
    public void snapshotCallbackObservesEveryEmission() {
        ReplayEngine engine = new ReplayEngine(0, 500, 4);
        List<Long> seen = new ArrayList<>();
        engine.run(Golden.eq(), (index, snap) -> seen.add(index));
        assertEquals(List.of(500L, 1000L, 1500L, 2000L), seen);
    }

    @Test
    public void rollingCheckpointsKeepLatestOnly() {
        ReplayEngine engine = new ReplayEngine(200, 0, 4);
        engine.run(Golden.eq()); // 10 checkpoints taken, keep last 4
        assertEquals(4, engine.checkpoints().size());
        assertEquals(1400, engine.checkpoints().get(0).eventsProcessed);
        assertEquals(2000, engine.checkpoints().get(3).eventsProcessed);
    }

    @Test
    public void checkpointRestartEqualsSinglePass() {
        List<MarketEvent> all = both();
        int split = 1777;

        ReplayEngine full = new ReplayEngine(0, 500, 4);
        full.run(all);

        ReplayEngine prefix = new ReplayEngine(0, 500, 4);
        prefix.run(all.subList(0, split));
        ReplayEngine resumed = ReplayEngine.restore(prefix.checkpoint());
        resumed.run(all.subList(split, all.size()));

        assertEquals(full.checkpoint(), resumed.checkpoint());
        assertEquals(full.bookStates(), resumed.bookStates());
        assertEquals(full.eventsProcessed(), resumed.eventsProcessed());
        assertEquals(full.timeRegressions(), resumed.timeRegressions());
    }

    @Test
    public void checkpointRestartAtEveryQuarterMatches() {
        List<MarketEvent> all = both();
        ReplayEngine full = new ReplayEngine();
        full.run(all);
        for (int split : new int[] {700, 1400, 2100}) {
            ReplayEngine prefix = new ReplayEngine();
            prefix.run(all.subList(0, split));
            ReplayEngine resumed = ReplayEngine.restore(prefix.checkpoint());
            resumed.run(all.subList(split, all.size()));
            assertEquals("split at " + split, full.checkpoint(), resumed.checkpoint());
        }
    }

    @Test
    public void restorePreservesSettingsAndCounters() {
        ReplayEngine engine = new ReplayEngine(100, 50, 4);
        engine.run(Golden.eq().subList(0, 300));
        ReplayEngine restored = ReplayEngine.restore(engine.checkpoint());
        assertEquals(100, restored.checkpointEvery);
        assertEquals(50, restored.snapshotEvery);
        assertEquals(300, restored.eventsProcessed());
        assertEquals(engine.lastExchangeTs(), restored.lastExchangeTs());
    }

    @Test(expected = IllegalArgumentException.class)
    public void negativeCadencesRejected() {
        new ReplayEngine(-1, 0, 4);
    }

    @Test
    public void bookStatesUsesSortedKeys() {
        ReplayEngine engine = new ReplayEngine();
        engine.run(both());
        var states = engine.bookStates();
        assertEquals(List.of(1L, 101L), List.copyOf(states.keySet()));
        assertEquals(List.of(1), List.copyOf(states.get(1L).keySet()));
        assertEquals(List.of(10, 11, 12), List.copyOf(states.get(101L).keySet()));
        assertTrue(states.get(101L).get(11).sequence > 0);
    }
}
