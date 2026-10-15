"""``python -m iap.metrics`` — the Python ``/metrics`` process (v1.17 F9).

    python -m iap.metrics serve [--port 8080] [--configs-dir DIR] [--state-dir DIR]
                                [--runs-root DIR]
    python -m iap.metrics run-session [--state-dir DIR] [--runs-root DIR] -- CMD [ARG ...]
    python -m iap.metrics names

``serve`` exposes ``GET /metrics`` (Prometheus text), ``/health``, ``/ready``
and ``/status`` on ``$IAP_BIND_ADDR`` (default ``127.0.0.1``; containers set
``0.0.0.0``) from the configs, the session marker in ``--state-dir`` and the
newest MVP run under ``--runs-root`` (:mod:`iap.metrics.exporter`).  Defaults
read ``$IAP_CONFIG_DIR`` (else ``configs``), ``$IAP_STATE_DIR`` (else
``data/state``) and ``$IAP_RUNS_ROOT`` (else ``data/mvp``).

``run-session`` runs ``CMD`` (typically ``python -m iap.mvp run --out
<runs-root>/<id>``) and maintains the session marker around it: RUNNING at
start, FINISHED on exit 0, FAILED on a non-zero exit, STOPPED when the child
was interrupted (SIGINT / SIGTERM); the event cursor is ``counts.n_events``
of the run's ``report.json`` when one exists.  It exits with the child's
code.

``names`` prints the live and retired metric families (what the deployment
check reads as the producer contract).  Exit codes: 0 ok, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from iap.metrics.exporter import (
    LIVE_METRICS,
    RETIRED_METRICS,
    PlatformExporter,
    newest_run,
    write_session_marker,
)
from iap.metrics.server import MetricsServer, bind_address

DEFAULT_PORT = 8080


def _env_path(var: str, default: str) -> Path:
    v = os.environ.get(var, "").strip()
    return Path(v or default)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m iap.metrics", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_dirs(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--configs-dir", type=Path, default=None, help="configs/ (default $IAP_CONFIG_DIR)"
        )
        sp.add_argument(
            "--state-dir", type=Path, default=None, help="session marker dir ($IAP_STATE_DIR)"
        )
        sp.add_argument(
            "--runs-root", type=Path, default=None, help="MVP runs root ($IAP_RUNS_ROOT)"
        )

    s = sub.add_parser("serve", help="serve /metrics, /health, /ready, /status")
    add_dirs(s)
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument(
        "--bind", default=None, help="listen address (default $IAP_BIND_ADDR or 127.0.0.1)"
    )
    s.add_argument("--once", action="store_true", help="print one exposition and exit (smoke test)")

    r = sub.add_parser("run-session", help="run a command with the session marker maintained")
    add_dirs(r)
    r.add_argument("command", nargs=argparse.REMAINDER, help="-- CMD [ARG ...]")

    sub.add_parser("names", help="print the live and retired metric families")
    return p


def _dirs(a: argparse.Namespace) -> tuple[Path, Path, Path]:
    configs = a.configs_dir or _env_path("IAP_CONFIG_DIR", "configs")
    state = a.state_dir or _env_path("IAP_STATE_DIR", "data/state")
    runs = a.runs_root or _env_path("IAP_RUNS_ROOT", "data/mvp")
    return configs, state, runs


def serve(a: argparse.Namespace) -> int:
    configs, state, runs = _dirs(a)
    exporter = PlatformExporter(configs, state, runs)
    if a.once:
        sys.stdout.write(exporter.render())
        return 0
    server = MetricsServer(exporter, a.bind or bind_address(), a.port)
    print(
        f"platform metrics: http://{server.server_address[0]}:{server.port}/metrics "
        f"configs={configs} state={state} runs={runs}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def run_session(a: argparse.Namespace) -> int:
    configs, state, runs = _dirs(a)
    del configs
    cmd = list(a.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("run-session: no command given (use -- CMD [ARG ...])", file=sys.stderr)
        return 2
    write_session_marker(state, "RUNNING", 0)
    interrupted = {"flag": False}

    def _on_signal(signum: int, _frame: object) -> None:
        interrupted["flag"] = True
        if proc.poll() is None:
            proc.send_signal(signum)

    proc = subprocess.Popen(cmd)  # noqa: S603 (the operator's own command line)
    handlers = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            handlers.append((sig, signal.signal(sig, _on_signal)))
        except (ValueError, OSError):  # not the main thread / unsupported
            pass
    try:
        rc = proc.wait()
    finally:
        for sig, old in handlers:
            signal.signal(sig, old)
    cursor = 0
    run_dir = newest_run(runs)
    if run_dir is not None and (run_dir / "report.json").is_file():
        try:
            rep = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
            cursor = int(rep.get("counts", {}).get("n_events", 0))
        except (OSError, ValueError):
            cursor = 0
    if interrupted["flag"] or rc < 0 or rc in (130, 143):
        final = "STOPPED"
    elif rc == 0:
        final = "FINISHED"
    else:
        final = "FAILED"
    write_session_marker(state, final, cursor)
    print(f"run-session: {final} (exit {rc}, event_cursor {cursor})", flush=True)
    return 0 if final == "STOPPED" and rc < 0 else rc


def names(_a: argparse.Namespace) -> int:
    for name, src in sorted(LIVE_METRICS.items()):
        print(f"live     {name:48s} {src}")
    for name, src in sorted(RETIRED_METRICS.items()):
        print(f"retired  {name:48s} {src} (exposed as {name}_available 0)")
    return 0


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    if a.cmd == "serve":
        return serve(a)
    if a.cmd == "run-session":
        return run_session(a)
    return names(a)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
