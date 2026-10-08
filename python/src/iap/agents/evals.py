"""Agent evaluations (AL06): each control must fail when it is removed.

Four checks, each run twice - with the control, expecting the failure to be
caught, and with the control disabled, expecting the failure to slip through.
An eval that passes with its control removed proves nothing.  Seeds are fixed
so results are reproducible; there is no network and no model call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from iap.agents import citations

IC_CEILING = 0.30  # an intraday feature-to-label correlation above this is implausible
N_NULLS = 400
N_OBS = 200


def _leak_found(control: bool) -> bool:
    rng = np.random.default_rng(11)
    label = rng.standard_normal(2000)
    leaky = label + 0.1 * rng.standard_normal(2000)  # built from the future label
    honest = rng.standard_normal(2000)
    ceiling = IC_CEILING if control else np.inf
    ic = lambda f: abs(float(np.corrcoef(f, label)[0, 1]))  # noqa: E731
    return ic(leaky) > ceiling and not ic(honest) > ceiling


def _pnl_bug_caught(control: bool) -> bool:
    qty, entry, exit_, cost = 100, 10.00, 10.05, 1.50
    recorded = qty * (exit_ - entry)  # seeded bug: the cost was never charged
    expected = qty * (exit_ - entry) - cost
    tol = 1e-9 if control else np.inf
    return abs(recorded - expected) > tol


def _null_rejected(control: bool) -> bool:
    """Shuffled-label nulls: with multiplicity control none is promoted; without, some are."""
    rng = np.random.default_rng(5)
    y = rng.standard_normal(N_OBS)
    t = np.array(
        [
            abs(np.corrcoef(rng.standard_normal(N_OBS), y)[0, 1]) * np.sqrt(N_OBS - 2)
            for _ in range(N_NULLS)
        ]
    )
    threshold = 4.0 if control else 1.96  # ~Bonferroni for N_NULLS looks vs naive 5%
    promoted = int((t > threshold).sum())
    return promoted == 0


def _fabrication_flagged(control: bool, root: Path) -> bool:
    fake = ["experiment:deadbeefdeadbeef", "ledger:" + "0" * 64]
    bad = citations.unresolved(fake, root) if control else []
    return len(bad) == len(fake)


def run_all(root: Path) -> list[dict[str, Any]]:
    """Each row: the eval, whether the control catches the failure, whether removing it lets it through."""
    cases = [
        ("planted_leak", _leak_found),
        ("seeded_bug", _pnl_bug_caught),
        ("shuffled_label_null", _null_rejected),
        ("citation_resolution", lambda c: _fabrication_flagged(c, Path(root))),
    ]
    rows = []
    for name, fn in cases:
        on, off = bool(fn(True)), bool(fn(False))
        rows.append(
            {
                "eval": name,
                "caught_with_control": on,
                "missed_without_control": not off,
                "ok": on and not off,
            }
        )
    return rows


def main() -> int:
    rows = run_all(Path.cwd())
    for r in rows:
        print(
            f"{r['eval']:<22} control_on={r['caught_with_control']} control_off_missed={r['missed_without_control']}"
        )
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
