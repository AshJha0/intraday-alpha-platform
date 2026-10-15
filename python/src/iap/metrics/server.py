"""Stdlib HTTP server for the Python ``/metrics`` producer (PLATFORM_CONVENTIONS.md
§12.5 shape): exact-path routing, ``GET`` only (405 otherwise, 404 elsewhere),
``Cache-Control: no-store`` on every response.

| endpoint   | 200                                   | 503                                   |
|------------|---------------------------------------|---------------------------------------|
| ``/metrics`` | always while the process serves      | —                                     |
| ``/health``  | the exporter can read its configs    | ``configs/risk/risk.json`` unreadable |
| ``/ready``   | a refresh succeeded with no malformed source | not refreshed yet, or a source is malformed |
| ``/status``  | always: the exporter's JSON status   | —                                     |

There is no admin API: the kill-switch runbook's manual halt is the config
path (``configs/risk/risk.json`` ``global.kill_switch_engaged`` + restart).
The listener binds ``$IAP_BIND_ADDR`` (default ``127.0.0.1``), as the Java
server did; containers set ``0.0.0.0``.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from iap.metrics.exporter import PlatformExporter

__all__ = ["BIND_ADDR_ENV", "DEFAULT_BIND_ADDR", "MetricsServer", "bind_address"]

BIND_ADDR_ENV = "IAP_BIND_ADDR"
DEFAULT_BIND_ADDR = "127.0.0.1"
CONTENT_TYPE_TEXT = "text/plain; version=0.0.4; charset=utf-8"
CONTENT_TYPE_JSON = "application/json; charset=utf-8"


def bind_address(env: dict[str, str] | None = None) -> str:
    v = (env if env is not None else os.environ).get(BIND_ADDR_ENV, "")
    return v.strip() or DEFAULT_BIND_ADDR


class _Handler(BaseHTTPRequestHandler):
    server: MetricsServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: str, ctype: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _probe(self, ok: bool, failure: str, exporter: PlatformExporter) -> None:
        body = {"status": "ok"} if ok else {"status": failure, "errors": exporter.status.errors}
        self._send(200 if ok else 503, json.dumps(body), CONTENT_TYPE_JSON)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        exporter = self.server.exporter
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            self._send(200, exporter.render(), CONTENT_TYPE_TEXT)
        elif path == "/health":
            exporter.refresh()
            self._probe(exporter.healthy(), "unhealthy", exporter)
        elif path == "/ready":
            exporter.refresh()
            self._probe(exporter.ready(), "not ready", exporter)
        elif path == "/status":
            exporter.refresh()
            self._send(200, exporter.status.json(), CONTENT_TYPE_JSON)
        else:
            self._send(404, '{"error":"not found"}', CONTENT_TYPE_JSON)

    do_HEAD = do_GET  # noqa: N815

    def do_POST(self) -> None:  # noqa: N802
        self._send(405, '{"error":"method not allowed"}', CONTENT_TYPE_JSON)

    do_PUT = do_DELETE = do_PATCH = do_POST  # noqa: N815

    def log_message(self, *args: object) -> None:  # quiet: scrapes every 15 s
        return


class MetricsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, exporter: PlatformExporter, host: str, port: int) -> None:
        self.exporter = exporter
        super().__init__((host, port), _Handler)

    @property
    def port(self) -> int:
        return int(self.server_address[1])
