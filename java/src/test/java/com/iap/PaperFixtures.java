package com.iap;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.Proxy;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

import com.iap.platform.PaperTrading;

/** Shared fixtures for the platform tests (sessions, config copies, HTTP). */
public final class PaperFixtures {
    private PaperFixtures() {
    }

    /** The repository's configs directory, relative to {@code java/}. */
    public static Path configs() {
        return Paths.get("..", "configs");
    }

    /** The golden EQ event vector. */
    public static Path goldenEvents() {
        return Paths.get("..", "tests", "golden", "events_eq_mbo.jsonl");
    }

    /** A short as-fast-as-possible session with an isolated state dir. */
    public static PaperTrading.Options session(long maxEvents)
            throws IOException {
        PaperTrading.Options o = new PaperTrading.Options();
        o.configsDir = configs();
        o.eventsFile = goldenEvents();
        o.maxEvents = maxEvents;
        o.stateDir = Files.createTempDirectory("iap-state");
        o.adminToken = ""; // admin API off unless a test asks for it
        return o;
    }

    /**
     * Copy {@code configs/} into a temp directory so a test can edit one
     * file (risk limits, kill switch) without touching the repository.
     */
    public static Path copyConfigs() throws IOException {
        Path src = configs();
        Path dst = Files.createTempDirectory("iap-configs");
        try (var walk = Files.walk(src)) {
            for (Path p : walk.collect(Collectors.toList())) {
                Path rel = src.relativize(p);
                Path target = dst.resolve(rel.toString());
                if (Files.isDirectory(p)) {
                    Files.createDirectories(target);
                } else {
                    Files.createDirectories(target.getParent());
                    Files.copy(p, target);
                }
            }
        }
        return dst;
    }

    /** Read a config file as text. */
    public static String readConfig(Path dir, String name) throws IOException {
        return new String(Files.readAllBytes(dir.resolve(name)),
                StandardCharsets.UTF_8);
    }

    /** Overwrite a config file. */
    public static void writeConfig(Path dir, String name, String text)
            throws IOException {
        Files.write(dir.resolve(name), text.getBytes(StandardCharsets.UTF_8));
    }

    /** One HTTP GET; returns {status, body}. */
    public static Object[] get(int port, String path) throws IOException {
        HttpURLConnection conn = open(port, path, "GET");
        int code = conn.getResponseCode();
        return new Object[] {code, body(conn)};
    }

    /** One HTTP request with an optional admin token and form body. */
    public static Object[] request(int port, String path, String method,
            String token, Map<String, String> form) throws IOException {
        HttpURLConnection conn = open(port, path, method);
        if (token != null) {
            conn.setRequestProperty("Authorization", "Bearer " + token);
        }
        if (form != null) {
            String encoded = form.entrySet().stream()
                    .map(e -> java.net.URLEncoder.encode(e.getKey(),
                            StandardCharsets.UTF_8) + "="
                            + java.net.URLEncoder.encode(e.getValue(),
                                    StandardCharsets.UTF_8))
                    .collect(Collectors.joining("&"));
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type",
                    "application/x-www-form-urlencoded");
            byte[] bytes = encoded.getBytes(StandardCharsets.UTF_8);
            conn.setFixedLengthStreamingMode(bytes.length);
            try (OutputStream os = conn.getOutputStream()) {
                os.write(bytes);
            }
        }
        int code = conn.getResponseCode();
        return new Object[] {code, body(conn), conn.getHeaderField("Cache-Control")};
    }

    private static HttpURLConnection open(int port, String path, String method)
            throws IOException {
        HttpURLConnection conn = (HttpURLConnection) URI.create(
                "http://127.0.0.1:" + port + path).toURL()
                .openConnection(Proxy.NO_PROXY);
        conn.setRequestMethod(method);
        conn.setConnectTimeout(5000);
        conn.setReadTimeout(10000);
        return conn;
    }

    private static String body(HttpURLConnection conn) throws IOException {
        InputStream in = conn.getResponseCode() >= 400
                ? conn.getErrorStream() : conn.getInputStream();
        if (in == null) {
            return "";
        }
        try (InputStream s = in) {
            return new String(s.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    /** Parse a JSON object body. */
    public static Map<String, Object> json(Object body) {
        return Json.object(Json.parse(String.valueOf(body)));
    }

    /** Non-empty lines of a state file. */
    public static List<String> lines(Path p) throws IOException {
        if (!Files.exists(p)) {
            return List.of();
        }
        return Files.readAllLines(p, StandardCharsets.UTF_8).stream()
                .filter(s -> !s.isEmpty()).collect(Collectors.toList());
    }
}
