"""Code fingerprint of an alpha (G1): what a pre-registration commits to.

``fingerprint(alpha_id)`` returns ``{"scheme", "code_hash", "modules",
"features", "feature_hash", "deps"}``:

- ``code_hash`` - sha256 over the source FILES of the module(s) defining the
  alpha class and its ``iap`` base classes, closed transitively over every
  ``iap.*`` module they import (:func:`module_closure`: an AST walk of
  ``import`` / ``from ... import`` statements anywhere in the file, including
  function-local and relative imports; ``from pkg import name`` counts
  ``pkg.name`` when that is a module).  So a module-level helper the alpha
  calls is covered wherever it lives in ``iap``.  Granularity is the module:
  editing ANY code in a covered module (e.g. another alpha in
  ``iap/alpha/equity.py``) changes the hash - a sound over-approximation.
- ``modules`` - the sorted covered module names (for the audit trail).
- ``features`` - the registry features the alpha declares, closed over
  ``depends_on``; ``feature_hash`` - their registry entries plus the
  closure hash of the feature family modules that compute them.
- ``deps`` - installed versions of numpy, pandas and scipy: third-party code
  is pinned by version, not hashed.

Stability: files are read as bytes, decoded as UTF-8 and their line endings
normalised to LF, so a Windows (CRLF) and a Linux checkout hash alike; no
``inspect``, bytecode or interpreter version enters the hash, so it is the
same on every supported Python minor version.

NOT covered: parent-package ``__init__`` modules that are only executed
implicitly (not named by an import), dynamic imports (``importlib`` with a
computed name), data/config files the code reads at run time (the fitted
params, ``configs/``), the Python interpreter and third-party libraries
other than the pinned versions above.

An id that is not a flagship alpha (a combination id, a maker-study id)
has no fingerprint (``None``): its pre-registration carries none and the
gate cannot check one.
"""

from __future__ import annotations

import ast
import hashlib
from importlib import metadata
from pathlib import Path
from typing import Any

from iap.agents.blackboard import canonical

SCHEME = 2
ROOT_PACKAGE = "iap"
PINNED_DISTRIBUTIONS = ("numpy", "pandas", "scipy")
SRC_ROOT = Path(__file__).resolve().parents[2]  # .../python/src


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def module_file(name: str, src_root: Path = SRC_ROOT) -> Path | None:
    """The source file of an ``iap`` module under ``src_root`` (``None`` if absent)."""
    base = Path(src_root).joinpath(*name.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def normalised_source(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _imports(name: str, path: Path, src_root: Path) -> set[str]:
    tree = ast.parse(normalised_source(path), filename=str(path))
    is_pkg = path.name == "__init__.py"
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = name.split(".")
                keep = len(parts) - node.level + (1 if is_pkg else 0)
                base = ".".join(parts[:keep] + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            out.add(base)
            out.update(f"{base}.{a.name}" for a in node.names)
    return {
        m
        for m in out
        if (m == ROOT_PACKAGE or m.startswith(ROOT_PACKAGE + ".")) and module_file(m, src_root)
    }


def module_closure(roots: list[str], src_root: Path = SRC_ROOT) -> list[str]:
    """Sorted ``iap`` modules reachable from ``roots`` through explicit imports."""
    seen: set[str] = set()
    todo = sorted(set(roots))
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        path = module_file(name, src_root)
        if path is None:
            continue
        seen.add(name)
        todo.extend(sorted(_imports(name, path, src_root) - seen))
    return sorted(seen)


def closure_hash(modules: list[str], src_root: Path = SRC_ROOT) -> str:
    parts = []
    for m in sorted(modules):
        path = module_file(m, src_root)
        parts.append(f"# module {m}\n{normalised_source(path) if path else ''}")
    return _sha("\n".join(parts))


def _deps() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for dist in PINNED_DISTRIBUTIONS:
        try:
            out[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            out[dist] = None
    return out


def fingerprint(alpha_id: str, src_root: Path = SRC_ROOT) -> dict[str, Any] | None:
    from iap import alpha
    from iap.features import registry

    if alpha_id not in alpha.ALPHA_CLASSES:
        return None
    cls = alpha.ALPHA_CLASSES[alpha_id]
    roots = sorted({c.__module__ for c in cls.__mro__ if c.__module__.startswith("iap.")})
    modules = module_closure(roots, src_root)
    specs = {s.name: s for s in registry.build_registry()}
    todo, used = list(cls.features), set()
    while todo:
        name = todo.pop()
        if name in used or name not in specs:
            continue
        used.add(name)
        todo.extend(specs[name].depends_on)
    names = sorted(used)
    fam_roots = sorted({registry.family_module(specs[n].family).__name__ for n in names})
    feat = canonical(
        {
            "specs": [specs[n].to_dict() for n in names],
            "families": closure_hash(module_closure(fam_roots, src_root), src_root),
        }
    )
    return {
        "scheme": SCHEME,
        "code_hash": closure_hash(modules, src_root),
        "modules": modules,
        "features": names,
        "feature_hash": _sha(feat),
        "deps": _deps(),
    }
