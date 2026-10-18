"""v1.18 REL-14a (gate F9): tests/harness/check_docs.py ``latency_claims``."""

from __future__ import annotations

import sys

from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "tests" / "harness"))
try:
    import check_docs
finally:
    sys.path.pop(0)

EDGE = "No latency edge; benchmarks are not colocation numbers.\n\n"


def test_unqualified_figure_fails():
    rows = check_docs.latency_findings(EDGE + "Tick-to-trade p50 1,279 ns on the golden vector.\n")
    assert len(rows) == 1 and rows[0][0] == 3
    assert "environment" in rows[0][1] and "'not colocation'" in rows[0][1]


def test_environment_without_not_colocation_fails():
    rows = check_docs.latency_findings(EDGE + "replay 27.2M events/s on a CI runner\n")
    assert rows == [(3, "'27.2M events/s' without 'not colocation'")]


def test_qualified_prose_passes_across_a_wrapped_line():
    text = EDGE + "Measured on a shared CI runner (not\ncolocation): p50 1,535 ns, p99 2,943 ns\n"
    assert check_docs.latency_findings(text) == []


def test_qualifier_in_another_paragraph_does_not_count():
    text = EDGE + "On a CI runner, not colocation.\n\nbook update 26.4 ns\n"
    assert [line for line, _ in check_docs.latency_findings(text)] == [5]


def test_table_row_stands_alone():
    text = EDGE + (
        "| what | number |\n|---|---|\n"
        "| tick-to-trade (CI runner; not colocation) | p50 1,279 ns |\n"
        "| hot path | 184.1 ns/event |\n"
    )
    assert [line for line, _ in check_docs.latency_findings(text)] == [6]


def test_figures_need_the_no_latency_edge_statement():
    rows = check_docs.latency_findings("31.7 µs in a container, not colocation\n")
    assert rows == [(0, "quotes latency figures but never states 'no latency edge'")]


def test_horizons_in_ms_and_plain_counts_are_not_latency_figures():
    text = "markouts at 100 ms/1 s/10 s; 15,805 events; IEX 350 us is a modelled delay\n"
    assert check_docs.latency_findings(text) == []


def test_repository_pages_pass():
    f = check_docs.Findings()
    check_docs.check_latency_claims(f)
    assert f.failed("latency_claims") == [], f.rows
