"""The data-read choke point (v2.0 stage 2, D1).

Every supported reader of market data in this package (:mod:`iap.io`,
:func:`iap.core.codec.read_iap1` / :func:`iap.core.codec.iter_iap1`, the
feature and dataset loaders) calls :func:`check` before it opens a file.
``check`` delegates to ONE registered hook, installed by whoever owns the
access policy (the holdout registry, :mod:`iap.research.holdout_registry`,
registers itself when it is imported; :mod:`iap` registers a lazy hook that
imports it on the first real read).  ``iap.core`` itself never names
``iap.research`` (D-11: the import contract "core independent of research"
is :func:`tests.test_import_policy.test_core_never_imports_research`).

Fail closed.  With no hook registered, :func:`check` still refuses a file
that is provably real market data: a file inside a dataset tree whose
``dataset.json`` says ``"kind": "real"``, or any file under a ``data/real``
directory.  Synthetic datasets, fixtures and temp files pass, so unit tests
that never touch real data need no registry.  There is no environment
variable that lifts a decision (the v1.18 ``IAP_HOLDOUT_AUTHORISED`` was
removed by D1): authorisation is in-process state of the registered hook.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["GuardRefused", "check", "dataset_root", "register", "registered", "reset"]

Hook = Callable[[Path, str], None]


class GuardRefused(PermissionError):
    """A read was refused at the choke point (no hook, or the hook refused)."""


_HOOK: Hook | None = None
_REAL_SEGMENTS = ("data", "real")


def register(hook: Hook | None) -> Hook | None:
    """Install ``hook`` as THE access decision (``hook(path, purpose)`` raises to
    refuse).  Returns the hook it replaced.  A hook may be installed once per
    process by its owner; re-registration is allowed so a lazy bootstrap hook
    can hand over to the real one."""
    global _HOOK
    old, _HOOK = _HOOK, hook
    return old


def registered() -> Hook | None:
    return _HOOK


def reset() -> None:
    """Tests only: forget the hook (the next :func:`check` is fail-closed again)."""
    register(None)


def dataset_root(path: Path | str) -> Path | None:
    """The dataset a path belongs to (``dataset.json`` in the path or up to three
    levels above it), or ``None``.  Symlinks are resolved first so a linked or
    copied tree is judged by what it is."""
    p = Path(path).resolve()
    for cand in (p, *list(p.parents)[:3]):
        if (cand / "dataset.json").is_file():
            return cand
    return None


def _manifest_kind(root: Path) -> Any:
    try:
        return json.loads((root / "dataset.json").read_text(encoding="utf-8")).get("kind")
    except (OSError, ValueError):
        return "unreadable"


def _under_data_real(p: Path) -> bool:
    parts = [x.lower() for x in p.resolve().parts]
    return any(parts[i : i + 2] == list(_REAL_SEGMENTS) for i in range(len(parts) - 1))


def _default(path: Path, purpose: str) -> None:
    """No hook registered: refuse anything that is provably real data."""
    p = Path(path)
    root = dataset_root(p.parent if p.suffix else p)
    kind = _manifest_kind(root) if root is not None else None
    if (root is not None and kind != "synthetic") or _under_data_real(p):
        raise GuardRefused(
            f"{p}: no data-access hook is registered and the file is (or may be) real market "
            f"data (dataset kind {kind!r}); import iap.research.holdout_registry or register a "
            f"hook with iap.core.guard.register before reading ({purpose})"
        )


def check(path: Path | str, purpose: str = "read") -> None:
    """Allow or refuse a read of ``path``; raises :class:`GuardRefused` (a
    ``PermissionError``) or whatever the registered hook raises."""
    hook = _HOOK or _default
    hook(Path(path), purpose)
