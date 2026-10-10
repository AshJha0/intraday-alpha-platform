"""tests/harness/check_bench_regression.py: the coarse bench_all latency guard."""

from __future__ import annotations

import importlib.util

import pytest
from conftest import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "check_bench_regression", REPO_ROOT / "tests" / "harness" / "check_bench_regression.py"
)
guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(guard)

COMMITTED = (REPO_ROOT / "benchmarks" / "results_cpp.md").read_text(encoding="utf-8")


def test_committed_table_parses_to_the_hot_rows_only():
    rows = guard.parse(COMMITTED)
    assert len(rows) >= 12
    assert rows["IAP1 decode (eq, 2000 ev)"] > 0
    assert any(name.startswith("canonical serialisation") for name in rows)
    # headers and the single-pass cold rows are not benchmarks of the guard
    assert not any(name.startswith("benchmark") for name in rows)
    assert not any("cold, " in name for name in rows)


def test_a_run_equal_to_the_committed_table_passes():
    base = guard.parse(COMMITTED)
    rows, problems = guard.compare(base, dict(base))
    assert problems == [] and len(rows) == len(base)
    assert all(r == pytest.approx(1.0) for *_, r in rows)


def test_slow_runner_noise_passes_but_an_algorithmic_regression_fails():
    base = guard.parse(COMMITTED)
    slow = {n: v * 2.5 for n, v in base.items()}  # a slower machine, everywhere
    assert guard.compare(base, slow)[1] == []
    name = "book update (eq MBO, 2000 ev)"
    broken = dict(base)
    broken[name] = base[name] * 50  # e.g. the order-book scan that cost 100x in the ingest
    problems = guard.compare(base, broken)[1]
    assert len(problems) == 1 and name in problems[0] and "50.0x" in problems[0]
    assert guard.compare(base, broken, factor=100)[1] == []


def test_renamed_or_missing_rows_cannot_make_the_guard_pass_silently():
    base = guard.parse(COMMITTED)
    renamed = {f"{n} v2": v for n, v in base.items()}
    problems = guard.compare(base, renamed)[1]
    assert len(problems) == 1 and "committed rows found" in problems[0]
    assert guard.compare({}, {"x": 1.0})[1] == ["the committed table has no benchmark rows"]


def test_cli_exit_codes(tmp_path, capsys):
    ok = tmp_path / "ok.md"
    ok.write_text(COMMITTED, encoding="utf-8")
    assert guard.main(["--current", str(ok)]) == 0
    assert "within 5x" in capsys.readouterr().out
    slow = tmp_path / "slow.md"
    slow.write_text(COMMITTED.replace("| 26.4 |", "| 900.0 |", 1), encoding="utf-8")
    assert guard.main(["--current", str(slow)]) == 1
    assert "REGRESSION" in capsys.readouterr().out
    assert guard.main(["--current", str(tmp_path / "missing.md")]) == 2


T2T_SAMPLE = """# C++ tick-to-trade latency (bench_tick_to_trade)

| path, workload | p50 ns | p90 ns | p99 ns | p99.9 ns | max ns | mean ns | samples |
|---|---:|---:|---:|---:|---:|---:|---:|
| **tick-to-trade (end to end)**, golden eq_mbo (2000 ev) | 1000 | 1200 | 3000 | 9000 | 50000 | 1100.0 | 100000 |

| benchmark (tick-to-trade guard) | ns |
|---|---:|
| t2t golden p50 | 1000 |
| t2t golden p99 | 3000 |
"""


def test_rows_filter_guards_only_the_tick_to_trade_percentiles():
    base = guard.parse(T2T_SAMPLE)
    rx = r"^t2t .* p(50|99)$"
    # the wide-table row (p50 in its first column) is ignored by the filter
    cur = dict(base, **{"t2t golden p99": 3000 * 7.0})
    rows, problems = guard.compare(base, cur, factor=8, rows_re=rx)
    assert len(rows) == 2 and problems == []
    cur["t2t golden p50"] = 1000 * 9.0
    assert len(guard.compare(base, cur, factor=8, rows_re=rx)[1]) == 1
    # a renamed guard table cannot pass silently
    assert guard.compare(base, {"something else": 1.0}, factor=8, rows_re=rx)[1]
