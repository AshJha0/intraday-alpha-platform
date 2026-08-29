package com.iap.replay;

import java.io.IOException;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

import com.iap.codec.Iap1Codec;
import com.iap.codec.JsonlCodec;
import com.iap.codec.Sha256;
import com.iap.core.MarketEvent;
import com.iap.core.Side;
import com.iap.orderbook.ConsolidatedBook;
import com.iap.orderbook.OrderBook;

/**
 * Demo: replays the golden vectors, prints per-venue book summaries, codec
 * SHA-256 digests, and a replay throughput figure. Usage:
 * {@code java com.iap.replay.Demo [goldenDir]} (default ../tests/golden).
 */
public final class Demo {
    private Demo() {
    }

    public static void main(String[] args) throws IOException {
        Path golden = Paths.get(args.length > 0 ? args[0] : "../tests/golden");
        List<MarketEvent> eq = JsonlCodec.read(golden.resolve("events_eq_mbo.jsonl"));
        List<MarketEvent> fx = JsonlCodec.read(golden.resolve("events_fx_quote.jsonl"));
        System.out.println("Loaded golden vectors: events_eq_mbo=" + eq.size()
                + " events, events_fx_quote=" + fx.size() + " events");
        System.out.println("IAP1 sha256(events_eq_mbo)  = " + Sha256.hex(Iap1Codec.encode(eq)));
        System.out.println("IAP1 sha256(events_fx_quote) = " + Sha256.hex(Iap1Codec.encode(fx)));

        ReplayEngine engine = new ReplayEngine(0, 500, 4);
        ReplayEngine.Summary s1 = engine.run(eq);
        ReplayEngine.Summary summary = engine.run(fx);
        System.out.println();
        System.out.println("Replay: events=" + summary.eventsProcessed()
                + " instruments=" + summary.instruments()
                + " snapshots=" + summary.snapshots()
                + " time_regressions=" + summary.timeRegressions()
                + " (eq pass: " + s1.eventsProcessed() + " events)");

        for (Map.Entry<Long, ConsolidatedBook> e : engine.instruments().entrySet()) {
            ConsolidatedBook cons = e.getValue();
            System.out.println();
            System.out.println("instrument " + e.getKey() + " (venues="
                    + cons.venues().keySet() + ")");
            System.out.println("  consolidated best_bid=" + fmt(cons.bestBid())
                    + " best_ask=" + fmt(cons.bestAsk())
                    + " trade_flow=" + cons.tradeFlow());
            System.out.println("  consolidated depth bid top5: "
                    + Arrays.deepToString(cons.depth(Side.BID, 5)));
            System.out.println("  consolidated depth ask top5: "
                    + Arrays.deepToString(cons.depth(Side.ASK, 5)));
            for (Map.Entry<Integer, OrderBook> v : cons.venues().entrySet()) {
                OrderBook b = v.getValue();
                System.out.println("  venue " + v.getKey()
                        + ": best_bid=" + fmt(b.bestBid()) + " best_ask=" + fmt(b.bestAsk())
                        + " orders=" + b.orderCountTotal()
                        + " seq=" + b.lastSequence()
                        + " applied=" + b.eventsApplied()
                        + " dups=" + b.duplicatesDropped()
                        + " gaps=" + b.gapsDetected()
                        + " stale_drops=" + b.droppedWhileStale()
                        + " unknown=" + b.unknownOrderEvents());
            }
        }

        // Throughput: repeat full replays on fresh engines (warm-up + timed).
        int warmupReps = 5;
        int reps = 25;
        for (int i = 0; i < warmupReps; i++) {
            replayOnce(eq, fx);
        }
        long t0 = System.nanoTime();
        long total = 0;
        for (int i = 0; i < reps; i++) {
            total += replayOnce(eq, fx);
        }
        double secs = (System.nanoTime() - t0) / 1e9;
        System.out.println();
        System.out.printf("Throughput: %,d events replayed in %.3fs => %,.0f events/sec%n",
                total, secs, total / secs);
    }

    private static long replayOnce(List<MarketEvent> eq, List<MarketEvent> fx) {
        ReplayEngine engine = new ReplayEngine();
        engine.run(eq);
        engine.run(fx);
        return engine.eventsProcessed();
    }

    private static String fmt(long[] level) {
        return level == null ? "none" : level[0] + "x" + level[1];
    }
}
