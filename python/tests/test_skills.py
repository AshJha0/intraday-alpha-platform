"""The Claude Code skills under .claude/skills/: frontmatter, guardrails, and that
every command a skill names points at a module or file that exists."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / ".claude" / "skills"
SRC = REPO / "python" / "src"
EXPECTED = {"prior-tests-check", "research-flow", "triage-report", "itch-day-bars", "agent-evals"}
GUARDRAIL_TERMS = ("sign", "regist", "holdout", "number")

PY_MODULE = re.compile(r"python -m ([A-Za-z_][\w.]*)")
PY_FILE = re.compile(r"python ((?:tools|python)/[\w./-]+\.py)")
CARGO_PKG = re.compile(r"cargo build -p ([\w-]+)")
REPO_PATH = re.compile(r"`((?:research|docs|python|tools|rust)/[\w./-]+?\.(?:json|md|py))`")


def frontmatter(text: str) -> dict[str, str]:
    """The ``key: value`` lines between the leading ``---`` fences (flat YAML)."""
    lines = text.splitlines()
    assert lines and lines[0] == "---", "SKILL.md must start with a --- frontmatter fence"
    end = lines.index("---", 1)
    out = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        assert sep and key.strip() and not key.startswith(" "), f"not a flat key: {line!r}"
        out[key.strip()] = value.strip()
    return out


def module_exists(name: str) -> bool:
    path = SRC.joinpath(*name.split("."))
    return path.with_suffix(".py").is_file() or (path / "__main__.py").is_file()


def _skill_files() -> list[Path]:
    return sorted(SKILLS.glob("*/SKILL.md"))


def test_all_expected_skills_exist():
    assert {p.parent.name for p in _skill_files()} >= EXPECTED


def test_skills_path_is_not_git_ignored():
    for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines():
        rule = line.strip()
        assert rule not in (".claude", ".claude/", "/.claude", "/.claude/", ".claude/*"), rule


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.parent.name)
def test_frontmatter_and_guardrails(path):
    text = path.read_text(encoding="utf-8")
    fm = frontmatter(text)
    assert fm.get("name") == path.parent.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", fm["name"]) and len(fm["name"]) <= 64
    desc = fm.get("description", "")
    assert 80 <= len(desc) <= 1024
    assert "Use when" in desc, "the description must say when to use the skill"
    body = text.split("---", 2)[2]
    assert "## Guardrails" in body
    guard = body.split("## Guardrails", 1)[1].lower()
    for term in GUARDRAIL_TERMS:
        assert term in guard, f"{path.parent.name}: guardrails do not mention {term!r}"


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.parent.name)
def test_every_named_command_exists(path):
    body = path.read_text(encoding="utf-8")
    modules = PY_MODULE.findall(body)
    files = PY_FILE.findall(body)
    assert modules or files, "a skill must name at least one command"
    missing = [m for m in modules if not module_exists(m)]
    missing += [f for f in files if not (REPO / f).is_file()]
    missing += [
        p for p in CARGO_PKG.findall(body) if not (REPO / "rust" / p / "Cargo.toml").is_file()
    ]
    missing += [p for p in REPO_PATH.findall(body) if "<" not in p and not (REPO / p).exists()]
    assert not missing, f"{path.parent.name} names commands or files that do not exist: {missing}"


def test_checker_catches_a_missing_module():
    assert module_exists("iap.memory") and module_exists("iap.llm.verifier_eval")
    assert not module_exists("iap.memory.nope")
