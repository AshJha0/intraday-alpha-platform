"""iap — Intraday Alpha Platform, Python reference implementation."""

from __future__ import annotations

__version__ = "1.18.0"


def _bootstrap_guard() -> None:
    """v2.0 D1: install the lazy data-access hook.  ``iap.core`` never names
    ``iap.research``; this package root does, once, so the first real read in
    any process routes to the holdout registry (which then registers itself as
    the hook for good)."""
    from iap.core import guard

    def lazy(path, purpose):  # noqa: ANN001 - hook signature
        from iap.research import holdout_registry  # registers the real hook on import

        holdout_registry.reader_guard(path, purpose=purpose)

    if guard.registered() is None:
        guard.register(lazy)


_bootstrap_guard()
