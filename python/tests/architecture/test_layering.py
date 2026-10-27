"""Architecture tests (v2.0 stage 11, S3): the import contracts of ``python/.importlinter``,
enforced here without the import-linter dependency, plus the recorded subpackage cycle set.

The contracts file is the single source of truth: this module parses it (INI) and checks each
``forbidden`` contract by an AST scan of ``python/src/iap`` -- every ``import`` / ``from ... import``
at any depth, relative imports resolved, imports under ``if TYPE_CHECKING:`` skipped when
``exclude_type_checking_imports = True``.  A contract without ``allow_indirect_imports`` also fails
on a chain through other packages.  CI additionally runs the real linter (``lint-imports``) in the
lint-and-secrets job; the two must agree, and a disagreement is a bug in one of them.
"""

from __future__ import annotations

import ast
import configparser
from collections import defaultdict
from pathlib import Path

import pytest
from conftest import REPO_ROOT

PY_DIR = REPO_ROOT / "python"
SRC = PY_DIR / "src"
IAP = SRC / "iap"
CONTRACTS_FILE = PY_DIR / ".importlinter"

#: The subpackage import cycles that exist after stage 11 (an AST count, direct imports,
#: TYPE_CHECKING excluded).  v1.18: 12 (plan 2.3).  After stage 2 D1 (core <-> research broken): 11.
#: After stage 11 (features <-> labels and execution <-> tca broken): 9.  v2.1 Track 3: 0 (the
#: shared governance primitives moved to iap.governance, the pure statistics to iap.stats, and three
#: single functions to the package that owns their data; docs/ARCHITECTURE.md, "Layering").  A NEW
#: cycle fails this test.
RECORDED_CYCLES: frozenset[tuple[str, str]] = frozenset()


# --------------------------------------------------------------------------- module graph


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _imports(
    tree: ast.Module, modname: str, is_package: bool, skip_type_checking: bool
) -> list[tuple[str, int]]:
    """Every imported dotted name (plus ``module.name`` for ``from module import name``) with its line."""
    out: list[tuple[str, int]] = []
    base = modname.split(".") if is_package else modname.split(".")[:-1]

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if skip_type_checking and isinstance(child, ast.If) and _is_type_checking(child.test):
                visit(
                    ast.Module(body=list(child.orelse), type_ignores=[])
                )  # the else branch is live code
                continue
            if isinstance(child, ast.Import):
                for a in child.names:
                    out.append((a.name, child.lineno))
            elif isinstance(child, ast.ImportFrom):
                if child.level:
                    pkg = base[: len(base) - (child.level - 1)] if child.level > 1 else base
                    mod = ".".join(pkg + ([child.module] if child.module else []))
                else:
                    mod = child.module or ""
                out.append((mod, child.lineno))
                for a in child.names:
                    out.append((f"{mod}.{a.name}", child.lineno))
            visit(child)

    visit(tree)
    return out


class Graph:
    def __init__(self) -> None:
        self.modules: dict[str, Path] = {}
        for p in sorted(IAP.rglob("*.py")):
            self.modules[_module_name(p)] = p
        self.edges: dict[str, dict[str, int]] = defaultdict(
            dict
        )  # module -> imported module -> line

    def build(self, skip_type_checking: bool) -> Graph:
        for name, path in self.modules.items():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for target, line in _imports(
                tree, name, path.name == "__init__.py", skip_type_checking
            ):
                resolved = self.resolve(target)
                if resolved and resolved != name:
                    self.edges[name].setdefault(resolved, line)
        return self

    def resolve(self, dotted: str) -> str | None:
        while dotted:
            if dotted in self.modules:
                return dotted
            dotted = dotted.rsplit(".", 1)[0] if "." in dotted else ""
        return None

    def members(self, package: str) -> list[str]:
        return [m for m in self.modules if m == package or m.startswith(package + ".")]

    def reach(self, start: str) -> set[str]:
        seen: set[str] = set()
        stack = [start]
        while stack:
            x = stack.pop()
            for y in self.edges.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return seen


def _in(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def _subpackage(module: str) -> str | None:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else None


# --------------------------------------------------------------------------- contracts


def load_contracts() -> tuple[dict[str, str], list[dict]]:
    cp = configparser.ConfigParser()
    cp.read(CONTRACTS_FILE, encoding="utf-8")
    assert cp.has_section("importlinter"), CONTRACTS_FILE
    options = dict(cp["importlinter"])
    contracts = []
    for section in cp.sections():
        if not section.startswith("importlinter:contract:"):
            continue
        s = cp[section]
        contracts.append(
            {
                "id": section.split(":", 2)[2],
                "name": s.get("name", section),
                "type": s.get("type"),
                "source_modules": s.get("source_modules", "").split(),
                "forbidden_modules": s.get("forbidden_modules", "").split(),
                "allow_indirect": s.getboolean("allow_indirect_imports", fallback=False),
            }
        )
    return options, contracts


OPTIONS, CONTRACTS = load_contracts()


@pytest.fixture(scope="module")
def graph() -> Graph:
    skip = OPTIONS.get("exclude_type_checking_imports", "False") == "True"
    return Graph().build(skip_type_checking=skip)


def test_contracts_file_is_the_expected_shape():
    assert OPTIONS["root_package"] == "iap"
    ids = [c["id"] for c in CONTRACTS]
    assert len(ids) == len(set(ids))
    # the five carried contracts plus the stage 11 ones
    for required in (
        "core-independent",
        "contracts-independent",
        "marketdata-independent",
        "features-independent",
        "execution-independent",
        "alpha-no-research",
        "portfolio-no-research",
        "labels-below-features",
        "execution-below-tca",
    ):
        assert required in ids, required
    # iap.ml (stage 7-8) gets its contract the moment the package is in the tree
    # iap.ml sits beside iap.research, not below it: its pipeline runs every candidate and the
    # incumbent through loop.run_engine on identical sessions, so the ml-no-research contract stage 11
    # anticipated was withdrawn when iap.ml landed (D-80); the engine packages above stay bound.
    assert (IAP / "ml").is_dir() and "ml-no-research" not in ids
    for c in CONTRACTS:
        assert c["type"] == "forbidden", c["id"]
        assert c["source_modules"] and c["forbidden_modules"], c["id"]


@pytest.mark.parametrize("contract", CONTRACTS, ids=[c["id"] for c in CONTRACTS])
def test_forbidden_contract(graph: Graph, contract: dict):
    violations: list[str] = []
    for source in contract["source_modules"]:
        members = graph.members(source)
        if not members:
            pytest.fail(f"{contract['id']}: source package {source} has no modules")
        for m in members:
            direct = graph.edges.get(m, {})
            for target, line in direct.items():
                for forbidden in contract["forbidden_modules"]:
                    if _in(target, forbidden):
                        where = graph.modules[m].relative_to(PY_DIR)
                        violations.append(f"{where}:{line}: {m} -> {target}")
            if not contract["allow_indirect"]:
                for target in graph.reach(m):
                    for forbidden in contract["forbidden_modules"]:
                        if _in(target, forbidden) and not any(_in(t, forbidden) for t in direct):
                            violations.append(f"{m} reaches {target} indirectly")
    assert violations == [], f"{contract['name']}:\n  " + "\n  ".join(sorted(set(violations)))


def test_recorded_subpackage_cycles(graph: Graph):
    pkg_edges: dict[str, set[str]] = defaultdict(set)
    for m, targets in graph.edges.items():
        a = _subpackage(m)
        if a is None:
            continue
        for t in targets:
            b = _subpackage(t)
            if b is not None and b != a:
                pkg_edges[a].add(b)
    cycles = {
        tuple(sorted((a, b))) for a in pkg_edges for b in pkg_edges[a] if a in pkg_edges.get(b, ())
    }
    new = sorted(cycles - RECORDED_CYCLES)
    gone = sorted(RECORDED_CYCLES - cycles)
    assert new == [], f"new subpackage import cycle(s): {new}"
    assert gone == [], f"cycle(s) no longer present; delete them from RECORDED_CYCLES: {gone}"


def test_scanner_skips_type_checking_blocks_and_resolves_relative_imports():
    src = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "from . import sibling\n"
        "from ..other import thing\n"
        "if TYPE_CHECKING:\n"
        "    from iap.features.context import InstrumentContext\n"
        "else:\n"
        "    import iap.core\n"
        "def f():\n"
        "    import iap.orderbook.book\n"
    )
    tree = ast.parse(src)
    names = {n for n, _ in _imports(tree, "iap.labels.grid_v2", False, True)}
    assert "iap.labels.sibling" in names
    assert "iap.other.thing" in names
    assert "iap.core" in names and "iap.orderbook.book" in names
    assert not any(n.startswith("iap.features") for n in names)
    with_tc = {n for n, _ in _imports(tree, "iap.labels.grid_v2", False, False)}
    assert "iap.features.context.InstrumentContext" in with_tc
