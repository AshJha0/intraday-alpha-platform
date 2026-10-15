"""v1.17 F9: the Python ``/metrics`` process (iap.metrics) — exposition
format, the file-backed exporter, the session marker, the HTTP server and
the ``run-session`` wrapper. No network beyond loopback, no repo writes."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from iap.metrics import __main__ as cli
from iap.metrics.exporter import (
    LIVE_METRICS,
    RETIRED_METRICS,
    PlatformExporter,
    newest_run,
    read_session_marker,
    write_session_marker,
)
from iap.metrics.registry import Histogram, MetricsRegistry, escape_label, series_key
from iap.metrics.server import MetricsServer, bind_address

CONFIGS = REPO_ROOT / "configs"


# ------------------------------------------------------------- registry ---


def test_series_key_sorts_and_escapes_labels():
    assert series_key("m", None) == "m"
    assert series_key("m", {"b": "y", "a": 'x"\\\n'}) == 'm{a="x\\"\\\\\\n",b="y"}'
    assert escape_label("plain") == "plain"


def test_histogram_log2_buckets_match_java_contract():
    assert [Histogram.bucket_of(v) for v in (0, 1, 2, 3, 4, 7, 8, 1023, 1024)] == [
        0,
        1,
        2,
        2,
        3,
        3,
        4,
        10,
        11,
    ]
    assert [Histogram.bucket_upper(i) for i in range(5)] == [0, 1, 3, 7, 15]
    assert Histogram.bucket_upper(20) == 1048575  # the SLO bound slo.yml uses
    h = Histogram()
    for v in (0, 5, 5, 300000):
        h.record(v)
    assert h.quantile(0.5) == 7 and h.quantile(0.99) == 524287 and h.quantile(1.0) == 524287
    with pytest.raises(ValueError):
        h.record(-1)


def test_render_known_answer():
    reg = MetricsRegistry()
    reg.counter("b_total").set(3)
    reg.counter("a_total", k="v").inc(2)
    reg.gauge("g", mode="asap").set(1)
    reg.gauge("g", mode="realtime").set(0)
    reg.gauge("f").set(2.5)
    h = reg.histogram("lat_ns", alpha="EQ01")
    for v in (0, 5, 5):
        h.record(v)
    assert reg.render() == (
        "# TYPE a_total counter\n"
        'a_total{k="v"} 2\n'
        "# TYPE b_total counter\n"
        "b_total 3\n"
        "# TYPE f gauge\n"
        "f 2.5\n"
        "# TYPE g gauge\n"
        'g{mode="asap"} 1\n'
        'g{mode="realtime"} 0\n'
        "# TYPE lat_ns histogram\n"
        'lat_ns_bucket{alpha="EQ01",le="0"} 1\n'
        'lat_ns_bucket{alpha="EQ01",le="7"} 3\n'
        'lat_ns_bucket{alpha="EQ01",le="+Inf"} 3\n'
        'lat_ns_sum{alpha="EQ01"} 10\n'
        'lat_ns_count{alpha="EQ01"} 3\n'
    )
    assert reg.names() == {"a_total", "b_total", "f", "g", "lat_ns"}
    assert MetricsRegistry().render() == ""


# --------------------------------------------------------------- marker ---


def test_session_marker_roundtrip_and_errors(tmp_path):
    assert read_session_marker(tmp_path) is None
    p = write_session_marker(tmp_path, "FINISHED", 42, unix_time=1700000000)
    assert p.name == "session_exit.json"
    assert json.loads(p.read_text()) == {
        "code": 2,
        "event_cursor": 42,
        "state": "FINISHED",
        "unix_time": 1700000000,
    }
    assert read_session_marker(tmp_path)["code"] == 2
    with pytest.raises(ValueError):
        write_session_marker(tmp_path, "BOGUS")
    p.write_text("not json")
    with pytest.raises(ValueError):
        read_session_marker(tmp_path)
    p.write_text('{"code": "2", "event_cursor": 0, "state": "FINISHED", "unix_time": 1}')
    with pytest.raises(ValueError):
        read_session_marker(tmp_path)


# ------------------------------------------------------------- exporter ---


def _report(**over) -> dict:
    rep = {
        "run": {"last_ts": 1_571_428_800_000_054_457},
        "counts": {
            "n_events": 1000,
            "n_child_orders_submitted": 40,
            "n_fills": 30,
            "counters": {"sequence_gaps": 2, "risk_rejected": 3, "parents_without_tca": 1},
            "qc_totals": {"duplicates": 4},
        },
        "alpha": {
            "ensemble": {"n_signals": 500},
            "per_alpha": {"EQ01": {"realized_ic": 0.05}, "EQ03": {"realized_ic": -0.01}},
        },
        "portfolio": {"solves": 499},
        "pnl": {
            "final_position": -10,
            "final_mark": 236.28,
            "max_drawdown": 12.5,
            "realized": -5.0,
            "unrealized": 1.25,
            "risk_daily": -3.75,
        },
        "risk": {"audit_events": 43, "allowed": 40, "rejected": 3, "kill_switch_engaged": True},
        "trace": {"n_traces": 501},
    }
    rep.update(over)
    return rep


def _run_dir(root: Path, name: str, report: dict | None, config: bool = True) -> Path:
    d = root / name
    d.mkdir(parents=True)
    if config:
        (d / "config.json").write_text("{}")
    if report is not None:
        (d / "report.json").write_text(json.dumps(report, sort_keys=True))
        parents = [
            {"decision_ts": 100, "arrival_ts": 100 + 300_000},
            {"decision_ts": 5, "arrival_ts": 12},
        ]
        lines = [json.dumps({"stages": {"parent_orders": [pp]}}) for pp in parents]
        lines.append(json.dumps({"stages": {"parent_orders": []}}))
        (d / "traces.jsonl").write_text(chr(10).join(lines) + chr(10))
    return d


def _series(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        key, _, value = line.rpartition(" ")
        out[key] = value
    return out


def test_exporter_reads_configs_marker_and_newest_run(tmp_path):
    runs = tmp_path / "mvp"
    _run_dir(runs, "older", _report(counts={"n_events": 1}), config=True)
    newer = _run_dir(runs, "newer", _report())
    import os

    os.utime(newer / "report.json", (2_000_000_000, 2_000_000_000))
    os.utime(runs / "older" / "report.json", (1_000_000_000, 1_000_000_000))
    state = tmp_path / "state"
    write_session_marker(state, "STOPPED", 777, unix_time=1234)
    ex = PlatformExporter(CONFIGS, state, runs)
    s = _series(ex.render())
    risk = json.loads((CONFIGS / "risk" / "risk.json").read_text())
    assert float(s['risk_limit{limit="max_daily_loss"}']) == risk["global"]["max_daily_loss"]
    assert (
        float(s['risk_limit{limit="max_strategy_daily_loss"}'])
        == risk["per_strategy"]["max_daily_loss"]
    )
    assert s["platform_persisted_session_state"] == "4"
    assert s["platform_persisted_session_event_cursor"] == "777"
    assert s["platform_persisted_session_unixtime"] == "1234"
    assert s["platform_session_state"] == "4"  # the marker wins over "report exists"
    assert s["md_events_total"] == "1000" and s["md_sequence_gaps_total"] == "2"
    assert s["md_duplicates_total"] == "4"
    assert s["exec_orders_submitted_total"] == "40" and s["exec_fills_total"] == "30"
    assert s["exec_child_orders_rejected_total"] == "3"
    assert s["risk_decisions_total"] == "43" and s["risk_rejected_total"] == "3"
    assert s["risk_kill_switch_engaged"] == "1"
    assert float(s["portfolio_gross_notional"]) == pytest.approx(2362.8)
    assert float(s["portfolio_net_notional"]) == pytest.approx(-2362.8)
    assert s['alpha_rolling_ic{alpha="EQ01"}'] == "0.05"
    assert s['platform_mode{mode="asap"}'] == "1" and s['platform_mode{mode="realtime"}'] == "0"
    assert float(s["md_last_event_unixtime"]) == pytest.approx(1571428800.0000544)
    assert s["md_last_event_wallclock_unixtime"] == "2000000000"
    assert s["trace_records_total"] == "501" and s["trace_tca_skipped_total"] == "1"
    # order_path_latency_ns from traces.jsonl: 300000 ns -> bucket 2^19-1, 7 ns -> 7
    assert s['order_path_latency_ns_bucket{le="7"}'] == "1"
    assert s['order_path_latency_ns_bucket{le="524287"}'] == "2"
    assert s['order_path_latency_ns_bucket{le="+Inf"}'] == "2"
    assert s["order_path_latency_ns_count"] == "2" and s["order_path_latency_ns_sum"] == "300007"
    first = ex._traces_cache
    ex.refresh()
    assert ex._traces_cache is first  # unchanged file: histogram not rebuilt
    for name in RETIRED_METRICS:
        assert s[f"{name}_available"] == "0"
    assert ex.status.run_dir == str(newer) and ex.status.errors == []
    assert ex.ready() and ex.healthy()


def test_exporter_every_contract_name_is_exposed(tmp_path):
    _run_dir(tmp_path / "mvp", "r", _report())
    write_session_marker(tmp_path / "state", "FINISHED", 1)
    names = PlatformExporter(CONFIGS, tmp_path / "state", tmp_path / "mvp").refresh().names()
    assert set(LIVE_METRICS) <= names, sorted(set(LIVE_METRICS) - names)
    assert {f"{n}_available" for n in RETIRED_METRICS} <= names
    assert not (set(LIVE_METRICS) & set(RETIRED_METRICS))


def test_exporter_session_state_without_marker(tmp_path):
    runs = tmp_path / "mvp"
    ex = PlatformExporter(CONFIGS, tmp_path / "state", runs)
    s = _series(ex.render())
    assert s["platform_session_state"] == "0" and s["platform_persisted_session_state"] == "-1"
    assert "md_events_total" not in s and ex.ready()
    d = _run_dir(runs, "live", None)  # config.json only: a session in flight
    s = _series(ex.render())
    assert s["platform_session_state"] == "1" and ex.status.run_dir == str(d)
    (d / "report.json").write_text(json.dumps(_report()))
    assert _series(ex.render())["platform_session_state"] == "2"
    assert newest_run(d) == d  # a run dir itself is accepted as the root


def test_exporter_degrades_not_crashes_on_malformed_sources(tmp_path):
    runs = tmp_path / "mvp"
    d = _run_dir(runs, "r", None)
    (d / "report.json").write_text("{bad json")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "session_exit.json").write_text("[]")
    ex = PlatformExporter(CONFIGS, tmp_path / "state", runs)
    s = _series(ex.render())
    assert s["platform_persisted_session_state"] == "-2"
    assert any("report.json" in e for e in ex.status.errors)
    assert ex.healthy() and not ex.ready()
    bad = PlatformExporter(tmp_path / "no-configs", tmp_path / "state", runs)
    bad.refresh()
    assert not bad.healthy()
    assert 'risk_limit{limit="max_daily_loss"}' not in _series(bad.render())


def test_exporter_tolerates_partial_report(tmp_path):
    _run_dir(tmp_path / "mvp", "r", {"counts": {"n_events": 7}})
    ex = PlatformExporter(CONFIGS, tmp_path / "state", tmp_path / "mvp")
    s = _series(ex.render())
    assert s["md_events_total"] == "7" and s["risk_decisions_total"] == "0"
    assert any("pnl" in e for e in ex.status.errors)


# --------------------------------------------------------------- server ---


def _get(url: str) -> tuple[int, dict[str, str], str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 (loopback)
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def test_server_endpoints(tmp_path):
    _run_dir(tmp_path / "mvp", "r", _report())
    write_session_marker(tmp_path / "state", "FINISHED", 1000)
    ex = PlatformExporter(CONFIGS, tmp_path / "state", tmp_path / "mvp")
    srv = MetricsServer(ex, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        code, headers, body = _get(base + "/metrics")
        assert code == 200 and headers["Content-Type"].startswith("text/plain; version=0.0.4")
        assert headers["Cache-Control"] == "no-store"
        assert "# TYPE md_events_total counter\nmd_events_total 1000\n" in body
        assert _get(base + "/health")[0] == 200
        assert _get(base + "/ready")[0] == 200
        code, _, status = _get(base + "/status")
        assert code == 200 and json.loads(status)["component"] == "platform-metrics"
        assert _get(base + "/nope")[0] == 404
        req = urllib.request.Request(base + "/admin/kill", data=b"x", method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)  # noqa: S310
        assert ei.value.code == 405
        # a malformed marker makes /ready 503 while /metrics and /health stay 200
        (tmp_path / "state" / "session_exit.json").write_text("{")
        assert _get(base + "/ready")[0] == 503
        assert _get(base + "/health")[0] == 200
        assert _get(base + "/metrics")[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_bind_address_defaults_to_loopback():
    assert bind_address({}) == "127.0.0.1"
    assert bind_address({"IAP_BIND_ADDR": " 0.0.0.0 "}) == "0.0.0.0"


# ------------------------------------------------------------------ CLI ---


def test_cli_names_and_serve_once(capsys, tmp_path):
    assert cli.main(["names"]) == 0
    out = capsys.readouterr().out
    assert "live     md_events_total" in out and "retired  jvm_gc_pause_ns" in out
    _run_dir(tmp_path / "mvp", "r", _report())
    rc = cli.main(
        [
            "serve",
            "--once",
            "--configs-dir",
            str(CONFIGS),
            "--state-dir",
            str(tmp_path / "state"),
            "--runs-root",
            str(tmp_path / "mvp"),
        ]
    )
    assert rc == 0
    assert "risk_daily_pnl -3.75" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("snippet", "rc", "state"),
    [
        ("import sys; sys.exit(0)", 0, "FINISHED"),
        ("import sys; sys.exit(3)", 3, "FAILED"),
        ("import sys; sys.exit(143)", 143, "STOPPED"),
    ],
)
def test_run_session_marks_start_and_end(tmp_path, snippet, rc, state):
    runs = tmp_path / "mvp"
    _run_dir(runs, "r", _report())
    got = cli.main(
        [
            "run-session",
            "--state-dir",
            str(tmp_path / "state"),
            "--runs-root",
            str(runs),
            "--",
            sys.executable,
            "-c",
            snippet,
        ]
    )
    assert got == rc
    marker = read_session_marker(tmp_path / "state")
    assert marker["state"] == state and marker["event_cursor"] == 1000


def test_run_session_requires_a_command(tmp_path, capsys):
    assert (
        cli.main(["run-session", "--state-dir", str(tmp_path), "--runs-root", str(tmp_path)]) == 2
    )
    assert "no command" in capsys.readouterr().err


def test_module_runs_as_main(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "iap.metrics", "names"],
        cwd=REPO_ROOT / "python",
        env={**__import__("os").environ, "PYTHONPATH": str(REPO_ROOT / "python" / "src")},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0 and "md_events_total" in proc.stdout
