#!/usr/bin/env python3
"""tests/harness/check_headline_numbers.py — docs numbers vs artefacts.

`GITHUB_PAGES.md` says "a wrong number on the landing page is a documentation
bug"; PLATFORM_CONVENTIONS.md §7 requires the experiment counter in every
report. This script re-derives every headline number from the artefact that
produces it and reports which documents disagree, so nobody has to recount by
hand after a research rerun or a harness run.

Two independent sections, because they have different owners and different
failure modes:

  PLATFORM   numbers derived from artefacts this repository builds
             (feature registry, QC report, benchmark table, cookbook recipe
             count, parity table). A mismatch here is a bug and exits 1.

  RESEARCH   numbers derived from the experiment ledger
             (`research/experiments.json`): the multiple-testing denominator,
             the expected max |t| under the null and the Bonferroni threshold.
             These MOVE every time `research/adaptive_reports/run_adaptive.py`
             or the alpha reports are re-run (each run appends looks), so a
             mismatch means "the docs were written against an older ledger
             snapshot", not necessarily "someone typed a wrong number".
             It is reported as STALE and exits 2 — a signal to regenerate the
             research reports and the doc rows together, not a build break in
             the middle of a research rerun.

Exit codes: 0 all good · 1 a platform number is wrong · 2 only research
numbers are stale (pass --strict to make that a failure too).

Usage:
    python3 tests/harness/check_headline_numbers.py [--strict] [--verbose]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DOCS = {
    "README.md": ROOT / "README.md",
    "LEARN.md": ROOT / "LEARN.md",
    "COOKBOOK.md": ROOT / "COOKBOOK.md",
    "docs/index.html": ROOT / "docs" / "index.html",
    "docs/DIAGRAMS.md": ROOT / "docs" / "DIAGRAMS.md",
    "docs/BUILD_NOTES.md": ROOT / "docs" / "BUILD_NOTES.md",
    # added round 3: all three quote counts that the harness produces, and all
    # three were found carrying a stale set (ARCHITECTURE claimed the right RUN
    # DATE with the previous round's numbers).
    "docs/ARCHITECTURE.md": ROOT / "docs" / "ARCHITECTURE.md",
    "docs/SCENARIOS.md": ROOT / "docs" / "SCENARIOS.md",
}

# Documents that are not in DOCS (they are not prose) but do embed the parity
# counts and must not drift from them.
EXTRA_COUNT_SOURCES = {
    "docs/diagrams/golden_topology.mmd":
        ROOT / "docs" / "diagrams" / "golden_topology.mmd",
}

LANGS = ("python", "cpp", "rust", "java")

PLATFORM_FAILURES: list[str] = []
RESEARCH_STALE: list[str] = []
CHECKED = 0


def texts() -> dict[str, str]:
    return {name: p.read_text() for name, p in DOCS.items() if p.exists()}


def num_variants(value: int) -> list[str]:
    """A number as it may legitimately appear: 19347, 19,347, 19 347."""
    plain = str(value)
    grouped = f"{value:,}"
    return [plain, grouped]


def expect_number(label: str, value: int, docs: dict[str, str],
                  wrong: list[int], section: list[str]) -> None:
    """Every document that mentions this quantity must use `value`."""
    global CHECKED
    CHECKED += 1
    for name, text in docs.items():
        for bad in wrong:
            for form in num_variants(bad):
                # a bare-number match with word boundaries
                if re.search(rf"(?<![\d,]){re.escape(form)}(?![\d,])", text):
                    section.append(
                        f"{name}: {label} appears as {form}, artefact says "
                        f"{value:,}")
                    break


def check_platform(docs: dict[str, str]) -> None:
    print("platform numbers (derived from committed artefacts):")

    # --- cookbook recipe count -------------------------------------------
    cookbook = (ROOT / "COOKBOOK.md").read_text()
    recipes = len(re.findall(r"^## \d+\. ", cookbook, re.M))
    claims = {}
    for name, text in docs.items():
        for m in re.finditer(r"(\d+)[ -]runnable recipes|(\d+) task-oriented recipes",
                             text):
            claims[name] = int(m.group(1) or m.group(2))
    ok = all(v == recipes for v in claims.values())
    report("cookbook_recipe_count", ok,
           f"{recipes} recipes; docs say {claims or 'nothing'}",
           PLATFORM_FAILURES)

    # --- feature registry -------------------------------------------------
    reg = ROOT / "data" / "reference" / "feature_registry.json"
    if reg.exists():
        doc = json.loads(reg.read_text())
        features = len(doc.get("features", doc if isinstance(doc, list) else []))
        mentioned = {name: bool(re.search(rf"(?<![\d,]){features}(?![\d,])"
                                          r"\s+(registered|versioned)", text))
                     for name, text in docs.items()
                     if "registered, versioned features" in text
                     or "registered features" in text}
        ok = all(mentioned.values()) if mentioned else True
        report("feature_registry_count", ok,
               f"{features} features in the registry; "
               f"documents claiming a count agree: {mentioned or 'none claim one'}",
               PLATFORM_FAILURES)
    else:
        report("feature_registry_count", True,
               "data/reference/feature_registry.json absent (generated) — skipped",
               PLATFORM_FAILURES)

    # --- C++ benchmark table ---------------------------------------------
    bench = ROOT / "benchmarks" / "results_cpp.md"
    rows = {}
    for line in bench.read_text().splitlines():
        m = re.match(r"\|\s*([^|]+?)\s*\|\s*([\d.]+)\s*\|", line)
        if m:
            rows[m.group(1)] = float(m.group(2))
    decode = next((v for k, v in rows.items() if k.startswith("IAP1 decode")), None)
    book = next((v for k, v in rows.items() if k.startswith("book update (eq")), None)
    problems = []
    for name, text in docs.items():
        if decode is not None and "IAP1 decode" in text:
            for m in re.finditer(r"IAP1 decode[^\n]*?([\d.]+)\s*ns", text):
                if abs(float(m.group(1)) - decode) > 1e-9:
                    problems.append(f"{name}: IAP1 decode {m.group(1)} ns, "
                                    f"benchmark says {decode}")
        if book is not None and "book update" in text:
            for m in re.finditer(r"book update[^\n]*?([\d.]+)\s*ns", text):
                if abs(float(m.group(1)) - book) > 1e-9:
                    problems.append(f"{name}: book update {m.group(1)} ns, "
                                    f"benchmark says {book}")
    report("cpp_benchmark_numbers", not problems,
           "; ".join(problems) or f"decode {decode} ns, book {book} ns",
           PLATFORM_FAILURES)

    # --- QC report totals -------------------------------------------------
    qc = ROOT / "data" / "normalized" / "qc_report.json"
    if qc.exists():
        doc = json.loads(qc.read_text())
        total = doc.get("total_events") or doc.get("events")
        if isinstance(total, int):
            problems = [f"{name}: mentions an event total other than {total:,}"
                        for name, text in docs.items()
                        if "events" in text
                        and re.search(r"(?<![\d,])310,159(?![\d,])", text)
                        and total != 310159]
            report("qc_event_total", not problems,
                   "; ".join(problems) or f"{total:,} normalized events",
                   PLATFORM_FAILURES)
        else:
            report("qc_event_total", True, "no total in qc_report.json",
                   PLATFORM_FAILURES)
    else:
        report("qc_event_total", True,
               "data/normalized/qc_report.json absent (generated) — skipped",
               PLATFORM_FAILURES)

    # --- java golden classes ---------------------------------------------
    on_disk = sorted(p.stem for p in
                     (ROOT / "java" / "src" / "test" / "java" / "com" / "iap")
                     .glob("*GoldenTest.java"))
    run_all = (ROOT / "tests" / "harness" / "run_all.sh").read_text()
    m = re.search(r'JAVA_GOLDEN_CLASSES="([^"]*)"', run_all)
    listed = sorted(c.split(".")[-1] for c in m.group(1).split()) if m else []
    report("java_golden_classes", listed == on_disk,
           f"{len(on_disk)} classes on disk, {len(listed)} in the golden gate",
           PLATFORM_FAILURES)

    # --- mermaid sources vs the blocks embedded in the docs ---------------
    # docs/diagrams/*.mmd is what `mmdc` renders; DIAGRAMS.md / ARCHITECTURE.md
    # embed the same text inline. Round 3 found both directions of drift: an
    # updated .mmd whose embedded copy still showed the old endpoints, and an
    # updated embedded block whose .mmd was never touched. Whichever half a
    # future edit forgets, this fails.
    check_mermaid_sync()


def check_mermaid_sync() -> None:
    diagram_dir = ROOT / "docs" / "diagrams"
    if not diagram_dir.is_dir():
        report("mermaid_sources_in_sync", True,
               "docs/diagrams absent — skipped", PLATFORM_FAILURES)
        return
    sources = {p.name: p.read_text().strip() for p in
               sorted(diagram_dir.glob("*.mmd"))}
    embedded: list[tuple[str, str]] = []
    for doc in ("docs/DIAGRAMS.md", "docs/ARCHITECTURE.md"):
        path = ROOT / doc
        if not path.exists():
            continue
        for m in re.finditer(r"```mermaid\n(.*?)```", path.read_text(), re.S):
            embedded.append((doc, m.group(1).strip()))

    problems = []
    matched_sources = set()
    for doc, block in embedded:
        exact = [n for n, s in sources.items() if s == block]
        if exact:
            matched_sources.update(exact)
            continue
        # name the nearest source so the failure says WHICH diagram drifted
        import difflib
        near = max(sources.items(), key=lambda kv: difflib.SequenceMatcher(
            None, block, kv[1]).ratio(), default=("<none>", ""))
        ratio = difflib.SequenceMatcher(None, block, near[1]).ratio() \
            if sources else 0.0
        first = block.splitlines()[0][:40] if block else "<empty>"
        problems.append(
            f"{doc}: block '{first}' differs from {near[0]} "
            f"(similarity {ratio:.2f})")
    orphans = sorted(set(sources) - matched_sources)
    if orphans:
        problems.append("no doc embeds: " + ", ".join(orphans))
    report("mermaid_sources_in_sync", not problems,
           "; ".join(problems) or
           f"{len(embedded)} embedded blocks match {len(sources)} .mmd sources",
           PLATFORM_FAILURES)


# ---------------------------------------------------------------------------
# Per-language test / golden counts (added round 3).
#
# Round 3 shipped four documents quoting four different sets of parity counts.
# The checker passed throughout, because it had no notion of a test count at
# all: docs/ARCHITECTURE.md claimed "567/230/240/390 tests (61/42/44/38 golden)"
# and dated it to the same day as the run that actually produced
# 626/243/254/448 (65/45/47/85), and the golden-topology diagram carried the
# 61/42/44/38 golden counts in all three of its synchronized copies.
#
# The pinned record is the parity table in README.md, which the README itself
# labels "captured from `tests/harness/run_all.sh`". Everything else must agree
# with it, and the two counts that CAN be re-derived without running a suite
# (java @Test, rust #[test]) are checked against the tree so the table itself
# cannot silently drift either. python and cpp are not statically derivable —
# pytest parametrization and gtest macros both expand — and this says so rather
# than pretending otherwise.
# ---------------------------------------------------------------------------

def parity_table_counts() -> tuple[dict[str, int], dict[str, int]] | None:
    """(tests, golden) per language from README's captured parity table."""
    readme = DOCS["README.md"]
    if not readme.exists():
        return None
    tests: dict[str, int] = {}
    golden: dict[str, int] = {}
    for line in readme.read_text().splitlines():
        m = re.match(r"\s*(python|cpp|rust|java)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|",
                     line)
        if m:
            tests[m.group(1)] = int(m.group(2))
            golden[m.group(1)] = int(m.group(3))
    if sorted(tests) != sorted(LANGS):
        return None
    return tests, golden


def count_sources(docs: dict[str, str]) -> dict[str, str]:
    out = dict(docs)
    for name, path in EXTRA_COUNT_SOURCES.items():
        if path.exists():
            out[name] = path.read_text()
    return out


def check_test_counts(docs: dict[str, str]) -> None:
    """Every per-language test/golden count quoted in a doc vs the harness."""
    print("per-language test / golden counts (vs README's captured "
          "parity table):")
    parsed = parity_table_counts()
    if parsed is None:
        report("parity_table_parsed", False,
               "could not parse the parity table out of README.md — the "
               "table is the pinned record every other document is checked "
               "against", PLATFORM_FAILURES)
        return
    tests, golden = parsed
    t_tuple = tuple(tests[l] for l in LANGS)
    g_tuple = tuple(golden[l] for l in LANGS)
    report("parity_table_parsed", True,
           "README table: tests %s, golden %s (python/cpp/rust/java)"
           % ("/".join(map(str, t_tuple)), "/".join(map(str, g_tuple))),
           PLATFORM_FAILURES)

    sources = count_sources(docs)
    check_parity_quadruples(sources, t_tuple, g_tuple)
    check_golden_topology_diagram(sources, golden)
    check_per_language_count_prose(sources, tests)
    check_parity_table_vs_tree(tests, golden)


def check_parity_quadruples(sources: dict[str, str],
                            t_tuple: tuple[int, ...],
                            g_tuple: tuple[int, ...]) -> None:
    """`626/243/254/448` and `65/45/47/85` wherever they are quoted.

    Only quadruples whose surrounding text is talking about tests are
    considered, so version strings and ratios elsewhere are left alone.
    """
    problems = []
    seen = 0
    for name, text in sources.items():
        for m in re.finditer(r"(?<![\d,./])(\d{2,4})/(\d{2,4})/(\d{2,4})"
                             r"/(\d{2,4})(?![\d,./])", text):
            window = text[max(0, m.start() - 120):m.end() + 120].lower()
            if not any(w in window for w in ("test", "golden", "parity")):
                continue
            seen += 1
            quad = tuple(int(g) for g in m.groups())
            if quad in (t_tuple, g_tuple):
                continue
            problems.append(
                f"{name}: {m.group(0)} — the harness table says "
                f"{'/'.join(map(str, t_tuple))} tests and "
                f"{'/'.join(map(str, g_tuple))} golden")
    report("parity_quadruples_in_docs", not problems,
           "; ".join(problems) or
           f"{seen} python/cpp/rust/java count quadruples, all matching",
           PLATFORM_FAILURES)


def check_golden_topology_diagram(sources: dict[str, str],
                                  golden: dict[str, int]) -> None:
    """The golden-topology diagram's four leaf nodes carry golden counts.

    They live in three synchronized copies (the .mmd source plus the blocks
    embedded in DIAGRAMS.md and ARCHITECTURE.md); mermaid_sources_in_sync
    keeps the copies equal, this keeps them true.
    """
    node = {
        "python": r"python: pytest -k golden<br/>(\d+) tests",
        "cpp": r"cpp: ctest -R Golden<br/>(\d+) tests",
        "rust": r"rust: 6 golden test targets<br/>(\d+) tests",
        "java": r"java: [^\"]*GoldenTest \(JUnitCore\)<br/>(\d+) "
                r"golden-group tests",
    }
    problems = []
    found = 0
    for name, text in sources.items():
        for lang, pattern in node.items():
            for m in re.finditer(pattern, text):
                found += 1
                if int(m.group(1)) != golden[lang]:
                    problems.append(
                        f"{name}: golden-topology {lang} node says "
                        f"{m.group(1)}, harness says {golden[lang]}")
    if not found:
        report("golden_topology_diagram_counts", True,
               "golden-topology diagram not found — skipped",
               PLATFORM_FAILURES)
        return
    report("golden_topology_diagram_counts", not problems,
           "; ".join(problems) or
           f"{found} diagram leaf nodes across the .mmd source and its "
           f"embedded copies, all matching",
           PLATFORM_FAILURES)


def check_per_language_count_prose(sources: dict[str, str],
                                   tests: dict[str, int]) -> None:
    """Prose of the form "cpp 243, rust 254, java 448 tests passed"."""
    problems = []
    seen = 0
    for name, text in sources.items():
        for m in re.finditer(r"(python|cpp|rust|java)[ ·]+(\d{2,4})\b",
                             text):
            window = text[max(0, m.start() - 200):m.end() + 200].lower()
            if "tests passed" not in window and "tests;" not in window \
                    and "tests)" not in window and "tests," not in window:
                continue
            seen += 1
            lang, claimed = m.group(1), int(m.group(2))
            if claimed != tests[lang]:
                problems.append(
                    f"{name}: '{m.group(0)}' — the harness table says "
                    f"{lang} {tests[lang]}")
    report("per_language_test_counts_in_prose", not problems,
           "; ".join(problems) or
           f"{seen} '<language> <count>' claims, all matching",
           PLATFORM_FAILURES)


def check_parity_table_vs_tree(tests: dict[str, int],
                               golden: dict[str, int]) -> None:
    """Re-derive the two counts that do not need a suite run.

    java: `@Test` annotations under java/src/test (JUnit4, and the harness
    asserts zero `@Ignore`); the golden column is the same count restricted to
    the ten `*GoldenTest` classes.
    rust: `#[test]` under rust/; the golden column is the same restricted to
    the six golden integration-test targets run_all.sh names.
    python and cpp are deliberately NOT derived: pytest parametrization and the
    gtest TEST_P/TEST_F macros both expand at collection time, so a static
    count would be wrong in a way that trains people to ignore this check.
    """
    problems = []
    detail = []

    java_test_dir = ROOT / "java" / "src" / "test"
    if java_test_dir.is_dir():
        java_files = sorted(java_test_dir.rglob("*.java"))
        total = sum(len(re.findall(r"@Test\b", p.read_text()))
                    for p in java_files)
        gold = sum(len(re.findall(r"@Test\b", p.read_text()))
                   for p in java_files if p.stem.endswith("GoldenTest"))
        detail.append(f"java @Test {total} (golden {gold})")
        if total != tests["java"]:
            problems.append(f"java: {total} @Test in the tree, table says "
                            f"{tests['java']}")
        if gold != golden["java"]:
            problems.append(f"java golden: {gold} @Test in the *GoldenTest "
                            f"classes, table says {golden['java']}")

    rust_dir = ROOT / "rust"
    if rust_dir.is_dir():
        rs = [p for p in rust_dir.rglob("*.rs") if "target" not in p.parts]
        total = sum(len(re.findall(r"#\[test\]", p.read_text(errors="ignore")))
                    for p in rs)
        run_all = (ROOT / "tests" / "harness" / "run_all.sh").read_text()
        m = re.search(r'RUST_GOLDEN_TARGETS="([^"]*)"', run_all)
        targets = [pair.split(":")[-1] for pair in m.group(1).split()] if m \
            else []
        gold = sum(len(re.findall(r"#\[test\]", p.read_text(errors="ignore")))
                   for p in rs if p.stem in targets)
        detail.append(f"rust #[test] {total} (golden {gold} over "
                      f"{len(targets)} targets)")
        if total != tests["rust"]:
            problems.append(f"rust: {total} #[test] in the tree, table says "
                            f"{tests['rust']}")
        if gold != golden["rust"]:
            problems.append(f"rust golden: {gold} #[test] in the golden "
                            f"targets, table says {golden['rust']}")

    report("parity_table_vs_source_tree", not problems,
           "; ".join(problems) or
           "; ".join(detail) + " (python/cpp not statically derivable)",
           PLATFORM_FAILURES)


def check_research(docs: dict[str, str]) -> None:
    print("research ledger numbers (move on every research rerun):")
    ledger = ROOT / "research" / "experiments.json"
    if not ledger.exists():
        report("experiment_ledger", True,
               "research/experiments.json absent — skipped", RESEARCH_STALE)
        return
    doc = json.loads(ledger.read_text())
    total = doc["total_experiments"]
    max_t = doc["expected_max_null_t"]
    bonf = doc["bonferroni_t_threshold"]

    stale = []
    for name, text in docs.items():
        for m in re.finditer(r"(?<![\d,])([\d]{1,3}(?:,[\d]{3})+|\d{4,6})"
                             r"[- ]?look", text):
            claimed = int(m.group(1).replace(",", ""))
            if claimed != total:
                stale.append(f"{name}: {m.group(1)}-look ledger, "
                             f"experiments.json says {total:,}")
        for m in re.finditer(r"expected max \|?t\|?[^\n\d]{0,20}([\d.]+)", text):
            if abs(float(m.group(1)) - max_t) > 0.005:
                stale.append(f"{name}: expected max |t| {m.group(1)}, "
                             f"ledger says {max_t:.3f}")
        # only a Bonferroni *t threshold* (a small number next to |t|), never
        # a citation year or a p-value in the same sentence
        for m in re.finditer(r"Bonferroni[^\n\d]{0,40}\|t\|[^\n\d]{0,10}([\d.]+)",
                             text):
            if abs(float(m.group(1)) - bonf) > 0.005:
                stale.append(f"{name}: Bonferroni {m.group(1)}, "
                             f"ledger says {bonf:.3f}")
    report("experiment_ledger",
           not stale,
           "; ".join(stale) or
           f"total_experiments {total:,}, expected max |t| {max_t:.3f}, "
           f"Bonferroni |t| >= {bonf:.3f}",
           RESEARCH_STALE)
    if stale:
        print("     ^ regenerate the research reports "
              "(research/*/run_*.py) and the doc rows together, and state which "
              "ledger snapshot each document quotes "
              "(PLATFORM_CONVENTIONS.md §7).")


def report(case: str, ok: bool, detail: str, sink: list[str]) -> None:
    print(f"  [{'ok  ' if ok else 'STALE' if sink is RESEARCH_STALE else 'FAIL'}]"
          f" {case}: {detail}")
    if not ok:
        sink.append(case)


def main() -> int:
    strict = "--strict" in sys.argv
    docs = texts()
    print("headline numbers vs artefacts "
          "(GITHUB_PAGES.md: a wrong number on the landing page is a bug)")
    check_platform(docs)
    check_test_counts(docs)
    check_research(docs)
    print()
    if PLATFORM_FAILURES:
        print("FAILED (platform): " + ", ".join(PLATFORM_FAILURES))
        return 1
    if RESEARCH_STALE:
        print("STALE (research ledger): " + ", ".join(RESEARCH_STALE))
        return 1 if strict else 2
    print("all headline numbers match their artefacts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
