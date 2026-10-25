"""R2: re-emit the REL-09 and REL-10 family MDE columns from the committed JSON (v1.19 stage 1).

The signed REL-09 / REL-10 result JSONs (``research/v118/rel09/DECISION.json``,
``research/v118/rel10/DECISION.json``) carry a ``mde_family_usd`` computed by the
two-quantile Student rule ``(t_{1-alpha/(2K)} + t_{power}) * sd / sqrt(G)``, which
understates the MDE at G = 4 (defect D-03).  This tool reads the committed numbers
(``G``, ``sd_usd``, ``family_k``, ``alpha``, ``power``), recomputes the family MDE
with the one exact noncentral-t inversion (:func:`iap.validation.confirmatory.mde`,
one-sided ``alpha / (2 K)``, G - 1 df) and appends a dated correction section to
each ``REPORT.md``.  The signed JSONs and the bound ``run_rel09.py`` /
``run_rel10.py`` are untouched; no data is read; verdicts do not change (every cell
stayed ``underpowered`` or ``reject``, and a larger MDE can only confirm that).

    PYTHONPATH=python/src python research/v119/reemit_mde.py [--check]

``--check`` recomputes and compares with the sections already in the reports
(exit 1 on any difference) instead of writing.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.validation.confirmatory import mde  # noqa: E402

CORRECTION_DATE = "2026-10-25"
MARK = "## MDE correction (v1.19 R2, "
NOTE = (
    "The `family MDE` column above was computed by the two-quantile Student rule "
    "`(t_{1-alpha/(2K)} + t_{power}) sd / sqrt(G)` (defect D-03). The column below is the exact "
    "noncentral-t MDE at 80 % power, G - 1 df, one-sided alpha / (2K) "
    "(`iap.validation.confirmatory.mde`, the one MDE implementation since R2), recomputed from the "
    "committed `DECISION.json` (`G`, `sd_usd`) by `research/v119/reemit_mde.py`. The signed JSON and "
    "the bound run script are untouched; the verdicts do not change (a larger MDE can only confirm "
    "`underpowered`)."
)


def _rows_rel09(
    doc: dict[str, Any], k: int, alpha: float, power: float
) -> list[tuple[str, int, float, float, float]]:
    out = []
    for sym, s in sorted(doc["per_symbol"].items()):
        g, sd = int(s["G"]), float(s["sd_usd"])
        out.append((sym, g, float(s["mde_family_usd"]), sd * mde(g, alpha / (2 * k), power), sd))
    return out


def _rows_rel10(
    doc: dict[str, Any], k: int, alpha: float, power: float
) -> list[tuple[str, int, float, float, float]]:
    out = []
    for use in sorted(doc["uses"]):
        for key, c in sorted(doc["uses"][use]["cells"].items()):
            g = int(c["G"])
            sd = c.get("sd_usd")
            if sd is None or not math.isfinite(float(sd)) or float(sd) <= 0 or g < 2:
                out.append((f"{use} {key}", g, float("nan"), float("nan"), float("nan")))
                continue
            sd = float(sd)
            out.append(
                (
                    f"{use} {key}",
                    g,
                    float(c["mde_family_usd"]),
                    sd * mde(g, alpha / (2 * k), power),
                    sd,
                )
            )
    return out


def section(
    name: str, rows: list[tuple[str, int, float, float, float]], k: int, alpha: float, power: float
) -> str:
    L = [
        f"{MARK}{CORRECTION_DATE})",
        "",
        NOTE,
        "",
        f"Family K = {k}, two-sided alpha = {alpha:g} (one-sided {alpha / (2 * k):.6g} per hypothesis), power = {power:g}.",
        "",
    ]
    L.append(
        "| cell | G | sd USD | family MDE as reported (two-quantile) | family MDE, noncentral t (R2) | ratio |"
    )
    L.append("|---|---|---|---|---|---|")
    for cell, g, old, new, sd in rows:
        if math.isnan(new):
            L.append(f"| {cell} | {g} | not estimable | - | - | - |")
        else:
            L.append(f"| {cell} | {g} | {sd:.2f} | {old:.2f} | {new:.2f} | {new / old:.3f} |")
    L.append("")
    return "\n".join(L)


def build() -> dict[str, str]:
    out = {}
    for name, loader in (("rel09", _rows_rel09), ("rel10", _rows_rel10)):
        d = ROOT / "research" / "v118" / name
        doc = json.loads((d / "DECISION.json").read_text(encoding="utf-8"))
        decl = json.loads((d / "predeclaration.json").read_text(encoding="utf-8"))["statistics"]
        k, alpha, power = int(decl["family_k"]), float(decl["alpha"]), float(decl["power"])
        assert k == int(doc["family_k"])
        out[name] = section(name, loader(doc, k, alpha, power), k, alpha, power)
    return out


def apply(check: bool) -> int:
    rc = 0
    for name, text in build().items():
        report = ROOT / "research" / "v118" / name / "REPORT.md"
        current = report.read_text(encoding="utf-8")
        head = current.split(MARK, 1)[0].rstrip("\n") + "\n\n"
        want = head + text
        if check:
            if current != want:
                print(f"{report}: differs from the recomputed correction section")
                rc = 1
            else:
                print(f"{report}: up to date")
        else:
            report.write_text(want, encoding="utf-8", newline="\n")
            print(f"wrote {report}")
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    return apply(a.check)


if __name__ == "__main__":
    raise SystemExit(main())
