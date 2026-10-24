"""Planted dry run before signature (v1.19 / v2.0 stage 1, R4; defect D-09).

Three signed rules of v1.18 could not produce the verdicts they were signed to
produce, and nobody noticed until real data had been read:

* REL-08 (`research/v118/rel08/screen_ds_20190130.json`): the within-day gap
  criterion C1 counted the leading gap, so every variant failed C1 and
  ``continue`` / ``stop`` were unreachable; the run returned ``underpowered``
  seven times and the session was spent.
* REL-08c (`research/auction/rel08c/DECISION.json`): the imbalance arm had
  zero trades at the registered decision times on 2019 data, so the rule
  could only ever say ``underpowered``.
* A0 of the null protocol v3 (`research/v118/f3_diagnostic/d0_recorded_out.json`):
  the exact-count acceptance line was ill-posed; a correct procedure fails it
  about a third of the time, so ``PASS`` was not a verdict the rule could be
  relied on to produce under its own null.

A **planted dry run** is the pre-signature check that catches all three: before
a rule is signed, it is run on planted inputs (synthetic or historical, never
the confirmation data) that are constructed to produce **every verdict value
the rule can return**, and the observed verdicts are recorded in a report file.
The signer binds that report (path and sha256) into the document as the
``dry_run`` block, and every sign helper refuses a document whose block is
missing, whose report file is missing or altered, or whose report fails to
demonstrate a verdict value.  A verdict the dry run could not produce is a
verdict the rule cannot be signed to produce.

The report file (JSON, repository-relative path) carries:

.. code-block:: json

    {"kind": "planted_dry_run",
     "rule": "<what was dry-run>",
     "verdict_values": ["continue", "stop", "underpowered"],
     "demonstrations": [
        {"planted": {"input": "<what was planted>", "observations": 74, "...": "..."},
         "observed": "continue"},
        {"...": "..."}],
     "statistics_planted": true,
     "confirmation_data_read": false}

Rules (:func:`require_dry_run`, all refusals name the reason):

1. the block names ``path`` and ``sha256``; the file exists and hashes to it;
2. the file is a ``planted_dry_run`` document whose ``verdict_values`` equal
   the block's and are non-empty;
3. **every** verdict value has at least one demonstration whose ``observed``
   equals it and whose planted input has at least one usable observation
   (``planted.observations`` absent or > 0): a demonstration on an empty arm
   demonstrates nothing (the REL-08c case);
4. every demonstration's ``observed`` is one of the verdict values (a verdict
   outside the declared set means the declaration is incomplete);
5. a demonstration may say which verdict its planted input was built to
   produce (``planted.expected``); when it does and the rule produced another
   verdict, that is a **mismatch** and the report is refused: a planted strong
   edge that comes back ``underpowered`` (REL-08 C1) or a planted valid null
   that comes back ``FAIL`` (A0, 32 % of 40 null matrices) is exactly the
   evidence that the rule cannot be signed as written;
6. ``confirmation_data_read`` is ``false``: a dry run that read the data the
   rule will be judged on is a look, not a dry run.

Nothing here signs, reads market data or touches the ledger.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "DRY_RUN_KIND",
    "DryRunRefused",
    "build_dry_run_block",
    "demonstrated_values",
    "load_report",
    "mismatches",
    "require_dry_run",
    "undemonstrated_values",
]

DRY_RUN_KIND = "planted_dry_run"


class DryRunRefused(ValueError):
    """The document cannot be signed: its planted dry run is missing or incomplete."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_report(root: Path | str, rel: str) -> dict[str, Any]:
    """The dry-run report at ``root / rel`` (refuses a missing or non-JSON file)."""
    p = Path(root) / rel
    if not p.is_file():
        raise DryRunRefused(f"dry run report {rel} does not exist")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DryRunRefused(f"dry run report {rel} is not readable JSON: {exc}") from exc
    if not isinstance(doc, Mapping) or doc.get("kind") != DRY_RUN_KIND:
        raise DryRunRefused(f"dry run report {rel} is not a {DRY_RUN_KIND!r} document")
    return dict(doc)


def _usable(dem: Mapping[str, Any]) -> bool:
    planted = dem.get("planted")
    if not isinstance(planted, Mapping) or not planted:
        return False
    n = planted.get("observations", 1)
    try:
        return float(n) > 0
    except (TypeError, ValueError):
        return False


def _expected(dem: Mapping[str, Any]) -> str | None:
    planted = dem.get("planted")
    exp = planted.get("expected") if isinstance(planted, Mapping) else None
    return str(exp) if isinstance(exp, str) else None


def mismatches(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Demonstrations whose planted input was built for one verdict and
    produced another (``planted.expected`` != ``observed``)."""
    out = []
    for i, dem in enumerate(report.get("demonstrations") or ()):
        if not isinstance(dem, Mapping):
            continue
        exp = _expected(dem)
        if exp is not None and exp != dem.get("observed"):
            out.append({"index": i, "expected": exp, "observed": dem.get("observed")})
    return out


def demonstrated_values(report: Mapping[str, Any]) -> dict[str, int]:
    """Verdict value -> number of usable demonstrations that observed it (a
    demonstration built for another verdict does not count)."""
    out: dict[str, int] = {}
    for dem in report.get("demonstrations") or ():
        if not isinstance(dem, Mapping) or not _usable(dem):
            continue
        observed = dem.get("observed")
        exp = _expected(dem)
        if isinstance(observed, str) and (exp is None or exp == observed):
            out[observed] = out.get(observed, 0) + 1
    return out


def undemonstrated_values(report: Mapping[str, Any]) -> list[str]:
    """The declared verdict values no usable demonstration produced."""
    values = [str(v) for v in (report.get("verdict_values") or ())]
    seen = demonstrated_values(report)
    return [v for v in values if seen.get(v, 0) < 1]


def _check_report(report: Mapping[str, Any], rel: str) -> None:
    values = report.get("verdict_values")
    if not isinstance(values, Sequence) or isinstance(values, str) or not values:
        raise DryRunRefused(f"dry run report {rel} declares no verdict_values")
    values = [str(v) for v in values]
    if len(set(values)) != len(values):
        raise DryRunRefused(f"dry run report {rel} repeats a verdict value")
    dems = report.get("demonstrations")
    if not isinstance(dems, Sequence) or isinstance(dems, str) or not dems:
        raise DryRunRefused(f"dry run report {rel} carries no demonstrations")
    for i, dem in enumerate(dems):
        if not isinstance(dem, Mapping) or not isinstance(dem.get("observed"), str):
            raise DryRunRefused(f"dry run report {rel}: demonstration {i} has no observed verdict")
        if dem["observed"] not in values:
            raise DryRunRefused(
                f"dry run report {rel}: demonstration {i} observed {dem['observed']!r}, "
                f"which is not a declared verdict value {values}; the declaration is incomplete"
            )
    if report.get("confirmation_data_read") is not False:
        raise DryRunRefused(
            f"dry run report {rel} must state confirmation_data_read: false "
            "(a dry run on the confirmation data is a look, not a dry run)"
        )
    bad = mismatches(report)
    if bad:
        by_pair: dict[tuple[str, Any], int] = {}
        for m in bad:
            by_pair[(m["expected"], m["observed"])] = (
                by_pair.get((m["expected"], m["observed"]), 0) + 1
            )
        detail = "; ".join(
            f"{n} planted for {e!r} produced {o!r}" for (e, o), n in sorted(by_pair.items())
        )
        raise DryRunRefused(
            f"dry run report {rel}: {len(bad)} of {len(dems)} planted inputs did not produce the "
            f"verdict they were built for ({detail}); the rule cannot be signed as written"
        )
    missing = undemonstrated_values(report)
    if missing:
        empty = [
            dem.get("observed")
            for dem in dems
            if isinstance(dem, Mapping) and not _usable(dem) and dem.get("observed") in missing
        ]
        hint = (
            f" (demonstrations on an empty arm do not count: {sorted(set(empty))})" if empty else ""
        )
        raise DryRunRefused(
            f"dry run report {rel} does not demonstrate verdict value(s) {missing}{hint}: "
            "a verdict the dry run cannot produce is a verdict the rule cannot be signed to produce"
        )


def build_dry_run_block(root: Path | str, rel: str) -> dict[str, Any]:
    """The ``dry_run`` block for a document: path, sha256 and the verdict
    summary of the report at ``root / rel``.  Refuses an incomplete report, so a
    block is never built for a dry run that would be refused at signing."""
    rel = str(rel).replace("\\", "/")
    report = load_report(root, rel)
    _check_report(report, rel)
    return {
        "path": rel,
        "sha256": _sha256(Path(root) / rel),
        "rule": str(report.get("rule", "")),
        "verdict_values": [str(v) for v in report["verdict_values"]],
        "demonstrated": demonstrated_values(report),
        "statistics_planted": bool(report.get("statistics_planted", False)),
    }


def require_dry_run(root: Path | str, doc: Mapping[str, Any]) -> dict[str, Any]:
    """Refuse (``DryRunRefused``) unless ``doc`` carries a complete, unaltered
    planted dry run; return the re-validated block."""
    block = doc.get("dry_run")
    if not isinstance(block, Mapping):
        raise DryRunRefused(
            "no dry_run block: bind a planted dry run (every verdict value demonstrated) "
            "before signing (R4, defect D-09)"
        )
    rel = block.get("path")
    if not isinstance(rel, str) or not rel:
        raise DryRunRefused("dry_run block names no path")
    report = load_report(root, rel)
    actual = _sha256(Path(root) / rel)
    if block.get("sha256") != actual:
        raise DryRunRefused(
            f"dry run report {rel} changed since it was bound (sha256 {actual[:12]} != "
            f"bound {str(block.get('sha256'))[:12]})"
        )
    _check_report(report, rel)
    declared = [str(v) for v in report["verdict_values"]]
    if list(block.get("verdict_values") or ()) != declared:
        raise DryRunRefused(
            f"dry_run block verdict_values {block.get('verdict_values')} differ from the "
            f"report's {declared}"
        )
    return {
        "path": rel,
        "sha256": actual,
        "rule": str(report.get("rule", "")),
        "verdict_values": declared,
        "demonstrated": demonstrated_values(report),
        "statistics_planted": bool(report.get("statistics_planted", False)),
    }
