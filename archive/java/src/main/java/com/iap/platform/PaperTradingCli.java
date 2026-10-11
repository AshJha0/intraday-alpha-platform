package com.iap.platform;

import java.io.IOException;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Locale;

import com.iap.config.ConfigService;
import com.iap.platform.PaperTrading.Options;
import com.iap.platform.PaperTrading.Result;

/**
 * Command-line entry point of {@link PaperTrading} (see java/paper.sh).
 * Package-private: split out of PaperTrading (v1.13 E4) without behaviour change.
 */
final class PaperTradingCli {
    private PaperTradingCli() {
    }

    /** CLI: see java/paper.sh for the flags. */
    static void main(String[] args) throws IOException {
        Options opts = new Options();
        Path explicitConfigs = null;
        opts.reportPath = Paths.get("out", "paper_session_report.json");
        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            switch (a) {
                case "--configs" -> explicitConfigs = Paths.get(need(args, ++i, a));
                case "--events" -> opts.eventsFile = Paths.get(need(args, ++i, a));
                case "--instrument" ->
                        opts.instrumentId = parseLong(need(args, ++i, a), a);
                case "--alpha" -> opts.alphaId = need(args, ++i, a);
                case "--mode" -> opts.realtime = switch (need(args, ++i, a)) {
                    case "realtime" -> true;
                    case "asap" -> false;
                    default -> throw new IllegalArgumentException(
                            "--mode must be asap|realtime");
                };
                case "--speed" -> opts.speed = parseDouble(need(args, ++i, a), a);
                case "--max-events" ->
                        opts.maxEvents = parseLong(need(args, ++i, a), a);
                case "--port" -> {
                    String v = need(args, ++i, a);
                    opts.port = "config".equals(v) ? -2 : (int) parseLong(v, a);
                }
                case "--report" -> opts.reportPath = Paths.get(need(args, ++i, a));
                case "--baselines" ->
                        opts.baselinesDir = Paths.get(need(args, ++i, a));
                case "--state-dir" -> opts.stateDir = Paths.get(need(args, ++i, a));
                case "--resume" -> opts.resume = true;
                case "--checkpoint-every" -> opts.checkpointEveryEvents =
                        parseLong(need(args, ++i, a), a);
                default -> throw new IllegalArgumentException(
                        "unknown argument " + a);
            }
        }
        // Config directory: --configs, else $IAP_CONFIG_DIR, else the default
        // (PLATFORM_CONVENTIONS.md §12.2).
        opts.configsDir = ConfigService.resolveDir(explicitConfigs,
                System.getenv(), opts.configsDir);
        if (opts.stateDir == null) {
            String env = System.getenv("IAP_STATE_DIR");
            if (env != null && !env.isBlank()) {
                opts.stateDir = Paths.get(env.trim());
            }
        }
        if (opts.port == -2) { // sentinel: from config
            opts.port = new ConfigService(opts.configsDir).monitoringPort();
        }
        PaperTrading.validate(opts);
        Result res = PaperTrading.run(opts);
        System.out.println(String.format(Locale.ROOT,
                "paper session: events=%d orders=%d fills=%d pnl=%.6f "
                        + "risk[allowed=%d rejected=%d] status=%s "
                        + "port=%d state=%s report=%s",
                res.eventsProcessed, res.ordersSubmitted, res.fillCount,
                res.totalPnl, res.riskAllowed, res.riskRejected,
                res.state.label().toUpperCase(Locale.ROOT), res.httpPort,
                res.stateDir, opts.reportPath));
    }

    static String need(String[] args, int i, String flag) {
        if (i >= args.length) {
            throw new IllegalArgumentException(flag + " needs a value");
        }
        return args[i];
    }

    static long parseLong(String v, String flag) {
        try {
            return Long.parseLong(v);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                    flag + " must be an integer, got " + v);
        }
    }

    static double parseDouble(String v, String flag) {
        try {
            return Double.parseDouble(v);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                    flag + " must be a number, got " + v);
        }
    }
}
