#!/usr/bin/env python3
"""tests/harness/check_docs.py — structural documentation checks (v1.17 E4).

Four named checks over the tracked Markdown (and ``docs/index.html``):

  internal_links      every relative Markdown link ``[..](path)`` names a file
                      or directory in the tree (external links are lychee's
                      job: the advisory step of the ``docs`` CI job)
  diagram_index       every ``docs/diagrams/*.mmd`` is embedded in
                      docs/DIAGRAMS.md or docs/ARCHITECTURE.md, the numbered
                      sections of DIAGRAMS.md are consecutive, and every
                      embedded block names a Mermaid diagram type
  api_names           every ``iap.<module>[.<name>]`` dotted name quoted in
                      API_*.md (repository root and docs/) resolves by import
                      (``python/src`` on the path), so an API reference never
                      names a symbol that is gone
  status_freshness    every "in progress" / "pending PR" / "not yet released"
                      style phrase whose line (git blame) is OLDER than the
                      last release tag is STALE: it was written before a
                      release shipped and never updated

Exit codes, as check_headline_numbers.py: 0 all good · 1 a structural
check failed (links, diagrams, API names) · 2 only status text is stale
(``--strict`` makes that 1 too).  ``--json`` prints the findings as one
JSON document (what the docs-sync agent reads).

Usage:
    python tests/harness/check_docs.py [--strict] [--json] [--verbose]
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

STALE_PHRASES = re.compile(
    r"\b(in progress|pending PR|pending pull request|not (?:yet )?released|work in progress|"
    r"coming soon|to be (?:done|written|added)|not yet (?:merged|implemented|shipped))\b",
    re.I,
)
#: a dated statement ("as of 2026-10-15") is deliberate; it is never stale
DATED = re.compile(r"\b(as of|dated|on) \d{4}-\d{2}-\d{2}\b", re.I)
_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_API_NAME = re.compile(r"(?<![\w./-])iap\.[a-z_][a-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_MERMAID_TYPES = (
    "flowchart",
    "graph",
    "sequenceDiagram",
    "stateDiagram",
    "stateDiagram-v2",
    "classDiagram",
    "erDiagram",
    "gantt",
    "pie",
    "journey",
    "gitGraph",
    "mindmap",
    "timeline",
    "quadrantChart",
    "xychart-beta",
    "block-beta",
    "sankey-beta",
    "requirementDiagram",
    "C4Context",
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout


def tracked_docs() -> list[Path]:
    out = _git("ls-files", "-z", "*.md", "docs/index.html")
    return [ROOT / p for p in out.split("\0") if p]


class Findings:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, check: str, status: str, where: str, detail: str) -> None:
        self.rows.append({"check": check, "status": status, "where": where, "detail": detail})

    def failed(self, check: str) -> list[dict]:
        return [r for r in self.rows if r["check"] == check and r["status"] != "PASS"]


# ---------------------------------------------------------------------------
def check_internal_links(f: Findings) -> None:
    n = 0
    for doc in tracked_docs():
        if doc.suffix != ".md":
            continue
        text = doc.read_text(encoding="utf-8", errors="replace")
        # fenced code is not prose: links inside it are examples
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        for m in _LINK.finditer(text):
            target = m.group(1)
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue
            path = target.split("#", 1)[0]
            if not path:
                continue
            path = path.replace("%20", " ")
            resolved = (doc.parent / path) if not path.startswith("/") else ROOT / path.lstrip("/")
            n += 1
            if not resolved.exists():
                line = text[: m.start()].count("\n") + 1
                f.add(
                    "internal_links",
                    "FAIL",
                    f"{doc.relative_to(ROOT).as_posix()}:{line}",
                    f"{target} does not exist",
                )
    if not f.failed("internal_links"):
        f.add("internal_links", "PASS", "", f"{n} relative links resolve")


def check_diagram_index(f: Findings) -> None:
    ddir = ROOT / "docs" / "diagrams"
    sources = {p.name: p.read_text(encoding="utf-8").strip() for p in sorted(ddir.glob("*.mmd"))}
    embedded: list[tuple[str, int, str]] = []
    for name in ("docs/DIAGRAMS.md", "docs/ARCHITECTURE.md"):
        path = ROOT / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"```mermaid\n(.*?)```", text, re.S):
            embedded.append((name, text[: m.start()].count("\n") + 1, m.group(1).strip()))
    blocks = {b for _, _, b in embedded}
    for name, src in sources.items():
        if src not in blocks:
            f.add(
                "diagram_index",
                "FAIL",
                f"docs/diagrams/{name}",
                "not embedded in DIAGRAMS.md or ARCHITECTURE.md",
            )
    for doc, line, block in embedded:
        first = block.splitlines()[0].strip().split()[0] if block.strip() else ""
        first = first.rstrip(":")
        if first not in _MERMAID_TYPES:
            f.add(
                "diagram_index",
                "FAIL",
                f"{doc}:{line}",
                f"block does not start with a Mermaid diagram type ({first!r})",
            )
    text = (ROOT / "docs" / "DIAGRAMS.md").read_text(encoding="utf-8")
    nums = [int(m.group(1)) for m in re.finditer(r"^## (\d+)\.", text, re.M)]
    if nums != list(range(1, len(nums) + 1)):
        f.add(
            "diagram_index",
            "FAIL",
            "docs/DIAGRAMS.md",
            f"section numbers are not consecutive: {nums}",
        )
    if not f.failed("diagram_index"):
        f.add(
            "diagram_index",
            "PASS",
            "",
            f"{len(sources)} sources embedded, {len(nums)} numbered sections",
        )


def _resolves(name: str) -> bool:
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        mod = ".".join(parts[:i])
        try:
            obj = importlib.import_module(mod)
        except ImportError:
            continue
        except Exception:  # a module that imports but fails at import time still exists
            return True
        try:
            for attr in parts[i:]:
                obj = getattr(obj, attr)
        except AttributeError:
            return False
        return True
    return False


def api_docs() -> list[Path]:
    return sorted(ROOT.glob("API_*.md")) + sorted((ROOT / "docs").glob("API_*.md"))


def check_api_names(f: Findings) -> None:
    seen: dict[str, bool] = {}
    n = 0
    for doc in api_docs():
        text = doc.read_text(encoding="utf-8", errors="replace")
        for m in _API_NAME.finditer(text):
            name = m.group(0).rstrip(".")
            # a schema id (`schema "iap.exec_calibration"`, `iap.fill_hazard/1`) is not a name
            before = text[max(0, m.start() - 16) : m.start()].lower()
            after = text[m.end() : m.end() + 14]
            if re.match(r"`?\s*(v\d|/\d|document|schema)", after) or "schema" in before:
                continue
            n += 1
            if name not in seen:
                seen[name] = _resolves(name)
            if not seen[name]:
                line = text[: m.start()].count("\n") + 1
                f.add(
                    "api_names",
                    "FAIL",
                    f"{doc.relative_to(ROOT).as_posix()}:{line}",
                    f"{name} does not resolve by import",
                )
    if not f.failed("api_names"):
        f.add("api_names", "PASS", "", f"{len(seen)} distinct names over {n} mentions resolve")


def last_tag_time() -> tuple[str, int] | None:
    tag = _git("describe", "--tags", "--abbrev=0").strip()
    if not tag:
        return None
    ts = _git("log", "-1", "--format=%ct", tag).strip()
    return (tag, int(ts)) if ts.isdigit() else None


def _blame_time(path: Path, line: int) -> int | None:
    out = _git("blame", "-L", f"{line},{line}", "--porcelain", "--", str(path.relative_to(ROOT)))
    m = re.search(r"^committer-time (\d+)$", out, re.M)
    return int(m.group(1)) if m else None


def check_status_freshness(f: Findings) -> None:
    tag = last_tag_time()
    if tag is None:
        f.add(
            "status_freshness", "PASS", "", "no release tag reachable; nothing to compare against"
        )
        return
    tag_name, tag_ts = tag
    checked = 0
    for doc in tracked_docs():
        if "CHANGELOG" in doc.name or "README_HISTORY" in doc.name or doc.parent.name == "release":
            continue  # history is meant to say what was pending at the time
        lines = doc.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines, 1):
            if not STALE_PHRASES.search(line) or DATED.search(line):
                continue
            checked += 1
            ts = _blame_time(doc, i)
            if ts is not None and ts < tag_ts:
                phrase = STALE_PHRASES.search(line).group(0)  # type: ignore[union-attr]
                f.add(
                    "status_freshness",
                    "STALE",
                    f"{doc.relative_to(ROOT).as_posix()}:{i}",
                    f"'{phrase}' written before {tag_name}: {line.strip()[:100]}",
                )
    if not f.failed("status_freshness"):
        f.add(
            "status_freshness", "PASS", "", f"{checked} status phrases, none older than {tag_name}"
        )


# ---------------------------------------------------------------------------
def run_all() -> Findings:
    f = Findings()
    check_internal_links(f)
    check_diagram_index(f)
    check_api_names(f)
    check_status_freshness(f)
    return f


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--strict", action="store_true", help="stale status text fails too")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args(argv)
    f = run_all()
    fails = [r for r in f.rows if r["status"] == "FAIL"]
    stale = [r for r in f.rows if r["status"] == "STALE"]
    if a.json:
        print(
            json.dumps(
                {"rows": f.rows, "fail": len(fails), "stale": len(stale)}, indent=2, sort_keys=True
            )
        )
    else:
        for r in f.rows:
            if r["status"] == "PASS" and not a.verbose:
                print(f"PASS  {r['check']:<18} {r['detail']}")
            else:
                print(f"{r['status']:<5} {r['check']:<18} {r['where']} — {r['detail']}")
        print(f"\n{len(fails)} failure(s), {len(stale)} stale status row(s)")
    if fails:
        return 1
    if stale:
        return 1 if a.strict else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
