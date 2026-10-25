"""Compare reproduced result files with the committed ones (v2.0 stage 3 P1).

    python tools/repro/compare_reproduction.py --out <report dir> \\
        --tolerance research/execution/b8v2/predeclaration.json \\
        <committed>=<reproduced> [<committed>=<reproduced> ...]

For every pair the outcome is one of

* ``byte-identical``: the files are equal byte for byte (the target);
* ``within-tolerance``: both are JSON, not byte-identical, and every
  difference is numeric and inside the result's declared tolerance
  (``reproducibility.tolerance`` of the B8 v2 predeclaration: numbers under a
  key ending in ``_bps`` within ``bps``, numbers under a key containing
  ``rate``, ``share`` or ``fraction`` within ``fraction``, integers exact,
  anything else exact); every difference is listed;
* ``differs``: anything else; the first differences are listed.

Writes ``<out>/reproduction_report.json`` and ``<out>/reproduction_report.md``
(sha256 and size of every file, the outcome per pair, the identity record of
each run when ``--record name=path`` is given) and exits 0 only when every pair
is byte-identical or within tolerance.  Reads no market data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

DEFAULT_TOL: dict[str, float] = {"bps": 0.0, "fraction": 0.0, "counts": 0}
OK = ("byte-identical", "within-tolerance")


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _tol_for(key: str, tol: dict[str, float]) -> float:
    k = key.lower()
    if k.endswith("_bps") or k == "bps":
        return float(tol.get("bps", 0.0))
    if any(w in k for w in ("rate", "share", "fraction")):
        return float(tol.get("fraction", 0.0))
    return 0.0


def json_differences(
    a: Any, b: Any, tol: dict[str, float], path: str = "$", key: str = ""
) -> list[str]:
    """Every difference between two parsed JSON documents; a numeric difference
    inside the tolerance is reported as ``within`` (listed, not a failure), one
    outside as ``OUTSIDE``."""
    out: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                side = "reproduced" if k not in a else "committed"
                out.append(f"{path}.{k}: present in {side} only")
            else:
                out += json_differences(a[k], b[k], tol, f"{path}.{k}", k)
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} != {len(b)}"]
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out += json_differences(x, y, tol, f"{path}[{i}]", key)
        return out
    numeric = (
        isinstance(a, int | float)
        and isinstance(b, int | float)
        and not isinstance(a, bool)
        and not isinstance(b, bool)
    )
    if not numeric:
        return [] if a == b else [f"{path}: {a!r} != {b!r}"]
    if isinstance(a, int) and isinstance(b, int):
        return [] if a == b else [f"{path}: count {a} != {b} (counts exact)"]
    t = _tol_for(key, tol)
    if abs(float(a) - float(b)) <= t:
        return [] if a == b else [f"{path}: {a} vs {b} within {t}"]
    return [f"{path}: {a} vs {b} OUTSIDE tolerance {t}"]


def compare(committed: Path, reproduced: Path, tol: dict[str, float]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "committed": committed.as_posix(),
        "reproduced": reproduced.as_posix(),
        "committed_sha256": sha256(committed) if committed.is_file() else None,
        "reproduced_sha256": sha256(reproduced) if reproduced.is_file() else None,
        "committed_bytes": committed.stat().st_size if committed.is_file() else None,
        "reproduced_bytes": reproduced.stat().st_size if reproduced.is_file() else None,
        "differences": [],
        "outcome": "differs",
    }
    if not reproduced.is_file() or not committed.is_file():
        row["differences"] = [
            f"{'reproduced' if not reproduced.is_file() else 'committed'} file missing"
        ]
        return row
    if row["committed_sha256"] == row["reproduced_sha256"]:
        row["outcome"] = "byte-identical"
        return row
    if committed.suffix == ".json":
        try:
            diffs = json_differences(
                json.loads(committed.read_text(encoding="utf-8")),
                json.loads(reproduced.read_text(encoding="utf-8")),
                tol,
            )
        except ValueError as exc:
            diffs = [f"not comparable as JSON: {exc}"]
        row["differences"] = diffs[:200]
        if diffs and all(" within " in d for d in diffs):
            row["outcome"] = "within-tolerance"
        return row
    a = committed.read_text(encoding="utf-8", errors="replace").splitlines()
    b = reproduced.read_text(encoding="utf-8", errors="replace").splitlines()
    diffs = [
        f"line {i + 1}: {x!r} != {y!r}"
        for i, (x, y) in enumerate(zip(a, b, strict=False))
        if x != y
    ]
    if len(a) != len(b):
        diffs.append(f"line count {len(a)} != {len(b)}")
    row["differences"] = diffs[:50]
    return row


def render_md(rep: dict[str, Any]) -> str:
    L = [
        "# Reproduction report",
        "",
        f"Status: **{rep['status']}** ({rep['pairs_ok']} of {rep['pairs']} files "
        "byte-identical or within the declared tolerance). Produced by "
        "`tools/repro/compare_reproduction.py`; the status vocabulary is in docs/REPRODUCTION.md.",
        "",
        "| committed | reproduced | outcome | committed sha256 | reproduced sha256 | bytes |",
        "|---|---|---|---|---|---|",
    ]
    for r in rep["files"]:
        L.append(
            f"| `{r['committed']}` | `{r['reproduced']}` | {r['outcome']} | "
            f"`{r['committed_sha256']}` | `{r['reproduced_sha256']}` | "
            f"{r['committed_bytes']} / {r['reproduced_bytes']} |"
        )
    for r in rep.get("informational") or []:
        L.append(
            f"| `{r['committed']}` | `{r['reproduced']}` | {r['outcome']} (informational) | "
            f"`{r['committed_sha256']}` | `{r['reproduced_sha256']}` | "
            f"{r['committed_bytes']} / {r['reproduced_bytes']} |"
        )
    for r in [*rep["files"], *(rep.get("informational") or [])]:
        if r["differences"]:
            L += ["", f"Differences, `{r['reproduced']}`:", ""]
            L += [f"* {d}" for d in r["differences"][:40]]
    if rep.get("records"):
        L += ["", "## Identity records of the runs", ""]
        for name, rec in rep["records"].items():
            bound = "bound" if rec.get("runner_bound") else "not among the bound scripts"
            L.append(
                f"* **{name}**: runner `{rec.get('runner')}` ({bound}), prereg "
                f"`{rec.get('prereg')}` sha256 `{rec.get('prereg_sha256')}`, signature "
                f"{rec.get('signature')}, interpreter `{rec.get('interpreter')}`, head "
                f"`{rec.get('head_commit')}`, bound commit `{rec.get('bound_commit')}`, image "
                f"declared `{rec.get('image_digest_declared')}`; "
                f"{len(rec.get('mismatches') or [])} identity mismatch(es)"
            )
            L += [f"  * {m}" for m in (rec.get("mismatches") or [])[:20]]
    if rep.get("image_digest"):
        L += ["", f"Image: `{rep['image_digest']}`"]
    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("pairs", nargs="+", help="<committed>=<reproduced>")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--tolerance", type=Path, help="predeclaration JSON with reproducibility.tolerance"
    )
    ap.add_argument("--record", action="append", default=[], help="name=<identity record JSON>")
    ap.add_argument(
        "--informational",
        action="append",
        default=[],
        help="<committed>=<reproduced> compared and listed but not deciding the status",
    )
    ap.add_argument("--image-digest", default=None)
    ap.add_argument("--status-ok", default="reproduced in the pinned image by CI")
    ap.add_argument("--status-bad", default="attempted, discrepancies unresolved")
    a = ap.parse_args(argv)
    tol = dict(DEFAULT_TOL)
    if a.tolerance:
        decl = json.loads(a.tolerance.read_text(encoding="utf-8"))
        tol.update((decl.get("reproducibility") or {}).get("tolerance") or {})
    rows = []
    for pair in a.pairs:
        c, r = pair.split("=", 1)
        rows.append(compare(Path(c), Path(r), tol))
    ok = sum(r["outcome"] in OK for r in rows)
    info = []
    for pair in a.informational:
        c, r = pair.split("=", 1)
        info.append({**compare(Path(c), Path(r), tol), "informational": True})
    records = {}
    for item in a.record:
        name, path = item.split("=", 1)
        if Path(path).is_file():
            records[name] = json.loads(Path(path).read_text(encoding="utf-8"))
    rep = {
        "status": a.status_ok if ok == len(rows) else a.status_bad,
        "pairs": len(rows),
        "pairs_ok": ok,
        "tolerance": tol,
        "files": rows,
        "informational": info,
        "records": records,
        "image_digest": a.image_digest,
    }
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "reproduction_report.json").write_text(
        json.dumps(rep, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (a.out / "reproduction_report.md").write_text(render_md(rep), encoding="utf-8", newline="\n")
    print(render_md(rep))
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
