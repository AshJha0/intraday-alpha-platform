"""REL-08 v2 calibration: execution integrity (protocol: ../REL08_V2_EXECUTION_PROTOCOL.md).

Pure functions over a run manifest (``run_calibration.manifest()``) and an output directory; imports nothing
from the runner and reads no market data.  Covers: record stamping and verification, resume, completed/void
state, the OPERATIONAL-ONLY checkpoint, the machine-readable result with the preregistered gate separated from
the information-only Bonferroni bounds, and the append-only audit log.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = 1
VARIANT_IDS = ("V1", "V2", "V5", "V6", "V9", "V10", "V11")
# the preregistered gate (plan REV2 section 3), frozen as data
GATE = {
    "A_family_upper_max": 0.05,
    "A_conf": 0.95,
    "B_pooled_variant_c3_upper_max": 0.0143,
    "C_pooled_family_point_max": 0.05,
    "D_replications_per_gating_cell": 200,
}
INFO_BONFERRONI_LEVEL = (
    1 - 0.05 / 6
)  # information only: the six cell bounds at the Bonferroni level
CONTINUE = ("continue", "passed_not_expanded")
LOW_TRADING_STATEMENT = (
    "A low false-continuation rate in this synthetic screen can arise because the screen rarely permits "
    "trading or rarely passes C1-C4, not because the bootstrap inference is well calibrated; read the per-criterion "
    "pass frequencies beside the continuation rate. A PASS would not demonstrate nominal 5 % family-wise error control."
)
NOT_MEASURED = (
    "C5, C6 and C7 are not measured for variants whose outcome was settled by C1-C4/C3 (decision-neutral "
    "short-circuit); their pass frequencies are therefore not reported."
)


# --------------------------------------------------------------- hashing --


def canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=float).encode()


def rec_hash(rec: Mapping[str, Any]) -> str:
    body = {k: v for k, v in rec.items() if k != "rec_sha256"}
    return hashlib.sha256(canon(body)).hexdigest()


def stamp(rec: dict[str, Any], identity: str, unit: str) -> dict[str, Any]:
    """Bind a replication record to the frozen identity and its cell before it is stored."""
    rec["identity_sha256"] = identity
    rec["unit"] = unit
    rec["rec_sha256"] = rec_hash(rec)
    return rec


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ----------------------------------------------------------- expectations --


def expected_units(m: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """unit name -> {file, seeds, gating}; units are the null cells and the power cases."""
    out: dict[str, dict[str, Any]] = {}
    for c in m["null_cells"]:
        out[c["cell"]] = {
            "file": f"cell_{c['cell']}.json",
            "seeds": list(range(c["seed_first"], c["seed_last"] + 1)),
            "gating": c["role"] == "gating",
        }
    for p in m["power_cells"]:
        name = f"power_{p['kind']}_{p['amp']}"
        out[name] = {
            "file": "power.json",
            "seeds": list(range(p["seed_first"], p["seed_last"] + 1)),
            "gating": False,
            "kind": p["kind"],
            "amp": p["amp"],
        }
    return out


def _records(out: Path, file: str) -> list[dict[str, Any]]:
    p = out / file
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []


def _unit_of(rec: Mapping[str, Any], units: Mapping[str, Mapping[str, Any]]) -> str | None:
    u = rec.get("unit")
    return u if u in units else None


# ------------------------------------------------------------ verification --


def verify_outputs(out: Path, m: Mapping[str, Any], identity: str) -> dict[str, Any]:
    """Check every stored replication. Returns ``{"problems": [...], "counts": {unit: n_valid}}``.

    A record is VALID iff: its unit is a declared unit stored in that unit's file, its seed is in the
    unit's declared seed range, the seed is unique within the unit, its ``rec_sha256`` matches its
    content, its ``identity_sha256`` equals the frozen identity, and it has the seven variants in order.
    Any invalid, duplicated or foreign record is a PROBLEM; one problem makes the study VOID.
    """
    units = expected_units(m)
    problems: list[str] = []
    counts = {u: 0 for u in units}
    files = sorted({spec["file"] for spec in units.values()})
    for f in files:
        seen: set[tuple[str, int]] = set()
        for rec in _records(out, f):
            u = _unit_of(rec, units)
            seed = rec.get("seed")
            tag = f"{f}:{rec.get('unit')}:{seed}"
            if u is None or units[u]["file"] != f:
                problems.append(f"{tag}: unknown unit or wrong file")
                continue
            if seed not in set(units[u]["seeds"]):
                problems.append(f"{tag}: seed outside the unit's declared range")
                continue
            if (u, seed) in seen:
                problems.append(f"{tag}: duplicate seed")
                continue
            seen.add((u, seed))
            if rec.get("rec_sha256") != rec_hash(rec):
                problems.append(f"{tag}: record hash mismatch")
                continue
            if rec.get("identity_sha256") != identity:
                problems.append(f"{tag}: identity differs from the frozen identity")
                continue
            if tuple(v.get("id") for v in rec.get("variants", [])) != VARIANT_IDS:
                problems.append(f"{tag}: variants incomplete")
                continue
            counts[u] += 1
    return {"problems": problems, "counts": counts}


def state(out: Path) -> str:
    """``completed`` | ``void`` | ``open`` from the marker files."""
    if (out / "VOID.json").is_file():
        return "void"
    if (out / "COMPLETED.json").is_file():
        return "completed"
    return "open"


def mark_void(out: Path, problems: list[str], identity: str) -> None:
    doc = {"state": "void", "utc": _utc(), "identity_sha256": identity, "problems": problems[:200]}
    (out / "VOID.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    audit(out, "void", identity, {"n_problems": len(problems)})


def guard_start(out: Path, m: Mapping[str, Any], identity: str) -> dict[str, int]:
    """Called before any replication runs or resumes. Raises if the study is completed or void, or if the
    stored outputs fail verification (then the study is marked void). Returns the valid counts."""
    out.mkdir(parents=True, exist_ok=True)
    s = state(out)
    if s == "completed":
        raise SystemExit("REFUSED: the study is COMPLETED; no reruns (protocol, finality)")
    if s == "void":
        raise SystemExit(
            "REFUSED: the study is VOID; a new study needs a new freeze and a new output directory"
        )
    v = verify_outputs(out, m, identity)
    if v["problems"]:
        mark_void(out, v["problems"], identity)
        raise SystemExit(
            f"REFUSED: stored outputs fail verification ({len(v['problems'])} problems); study VOID"
        )
    return v["counts"]


# ----------------------------------------------------------------- audit --


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit(out: Path, event: str, identity: str, detail: Mapping[str, Any] | None = None) -> None:
    """Append-only JSONL audit trail."""
    line = {
        "utc": _utc(),
        "event": event,
        "identity_sha256": identity,
        "pid": os.getpid(),
        **(detail or {}),
    }
    with open(out / "audit_log.jsonl", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(line, sort_keys=True, default=float) + "\n")


# ------------------------------------------------------------ checkpoint --

CHECKPOINT_KEYS = ("kind", "utc", "identity_sha256", "progress", "integrity", "resource", "note")


def checkpoint_operational(
    out: Path, m: Mapping[str, Any], identity: str, free_ram_gb: float | None, started: float
) -> dict[str, Any]:
    """OPERATIONAL ONLY: progress counts, integrity status and resource usage. Never opens an outcome, a criterion
    or a bound: record contents beyond the verification fields are not inspected."""
    v = verify_outputs(out, m, identity)
    doc = {
        "kind": "rel08_v2_calibration_checkpoint_operational",
        "utc": _utc(),
        "identity_sha256": identity,
        "progress": v["counts"],
        "integrity": {"ok": not v["problems"], "n_problems": len(v["problems"])},
        "resource": {"free_ram_gb": free_ram_gb, "elapsed_s": round(time.time() - started, 1)},
        "note": "operational checkpoint: progress, integrity and resources only; no outcome of any replication is "
        "computed or shown here and nothing downstream reads it.",
    }
    assert tuple(doc) == CHECKPOINT_KEYS
    (out / "checkpoint_100_operational.json").write_text(
        json.dumps(doc, indent=1), encoding="utf-8"
    )
    audit(
        out,
        "checkpoint_100",
        identity,
        {"progress": v["counts"], "integrity_ok": doc["integrity"]["ok"]},
    )
    return doc


# ----------------------------------------------------------------- result --


def cp_upper(k: int, n: int, conf: float = 0.95) -> float:
    """One-sided Clopper-Pearson upper bound (bisection on the binomial CDF)."""
    if n == 0:
        return float("nan")
    if k >= n:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        cdf = sum(math.comb(n, i) * mid**i * (1 - mid) ** (n - i) for i in range(k + 1))
        lo, hi = (mid, hi) if cdf > 1 - conf else (lo, mid)
    return hi


def _cell_summary(recs: list[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(recs)
    nv = sum(len(r["variants"]) for r in recs)
    fam = sum(any(v["outcome"] in CONTINUE for v in r["variants"]) for r in recs)
    vc = sum(v["outcome"] in CONTINUE for r in recs for v in r["variants"])
    c3 = sum(bool(v["criteria"]["C3"]) for r in recs for v in r["variants"])
    freq = {
        c: sum(bool(v["criteria"][c]) for r in recs for v in r["variants"]) / nv if nv else None
        for c in ("C1", "C2", "C3", "C4", "C8")
    }
    by_reason = {"coverage_or_C1": 0, "C2": 0, "C4": 0}
    for r in recs:
        for v in r["variants"]:
            cr = v["criteria"]
            if not cr["C1"]:
                by_reason["coverage_or_C1"] += 1
            if not cr["C2"]:
                by_reason["C2"] += 1
            if not cr["C4"]:
                by_reason["C4"] += 1
    return {
        "replications": n,
        "variant_screens": nv,
        "family_events": fam,
        "family_rate": fam / n if n else None,
        "family_upper_95": cp_upper(fam, n),
        "family_upper_bonferroni_info_only": cp_upper(fam, n, INFO_BONFERRONI_LEVEL),
        "variant_continue": vc,
        "variant_continue_rate": vc / nv if nv else None,
        "variant_c3_pass": c3,
        "variant_c3_pass_upper_95": cp_upper(c3, nv),
        "criterion_pass_frequency": freq,
        "variant_screens_failing": by_reason,
        "all_four_c1_c2_c3_c4_pass_frequency": (
            sum(
                all(v["criteria"][c] for c in ("C1", "C2", "C3", "C4"))
                for r in recs
                for v in r["variants"]
            )
            / nv
            if nv
            else None
        ),
    }


def final_result(out: Path, m: Mapping[str, Any], identity: str) -> dict[str, Any]:
    """The machine-readable result. ``gate`` is the preregistered rule exactly; ``information_only`` holds the six
    Bonferroni-adjusted bounds and descriptive frequencies, which never enter the verdict."""
    units = expected_units(m)
    v = verify_outputs(out, m, identity)
    need = {u: len(s["seeds"]) for u, s in units.items()}
    complete = not v["problems"] and all(v["counts"][u] == need[u] for u in units)
    cells: dict[str, Any] = {}
    gating = [c["cell"] for c in m["null_cells"] if c["role"] == "gating"]
    for c in [c["cell"] for c in m["null_cells"]]:
        cells[c] = _cell_summary(_records(out, units[c]["file"]))
        cells[c]["role"] = "gating" if c in gating else "non-gating (stress)"
    power: dict[str, Any] = {}
    for u, s in units.items():
        if "kind" in s:
            recs = [r for r in _records(out, s["file"]) if r.get("unit") == u]
            nv = sum(len(r["variants"]) for r in recs)
            power[u] = {
                "replications": len(recs),
                "variant_c3_pass_rate": sum(
                    bool(x["criteria"]["C3"]) for r in recs for x in r["variants"]
                )
                / nv
                if nv
                else None,
                "family_continue_rate": sum(
                    any(x["outcome"] in CONTINUE for x in r["variants"]) for r in recs
                )
                / len(recs)
                if recs
                else None,
                "role": "non-gating (power-side sanity check)",
            }
    g = GATE
    a_ok = {c: cells[c]["family_upper_95"] <= g["A_family_upper_max"] for c in gating}
    b_ok = {
        c: cells[c]["variant_c3_pass_upper_95"] <= g["B_pooled_variant_c3_upper_max"]
        for c in gating
    }
    nrep = sum(cells[c]["replications"] for c in gating)
    fam_all = sum(cells[c]["family_events"] for c in gating)
    c_ok = (fam_all / nrep <= g["C_pooled_family_point_max"]) if nrep else False
    d_ok = complete and all(
        cells[c]["replications"] == g["D_replications_per_gating_cell"] for c in gating
    )
    verdict = "PASS" if (all(a_ok.values()) and all(b_ok.values()) and c_ok and d_ok) else "FAIL"
    if not complete:
        verdict = "INCOMPLETE"
    return {
        "schema_version": SCHEMA,
        "kind": "rel08_v2_calibration_result",
        "identity_sha256": identity,
        "complete": complete,
        "gate": {
            "rule": dict(g),
            "A_each_gating_cell_family_upper_le_5pct": a_ok,
            "B_each_gating_cell_pooled_variant_c3_upper_le_1_43pct": b_ok,
            "C_pooled_family_point_le_5pct": {"ok": c_ok, "events": fam_all, "replications": nrep},
            "D_200_valid_replications_per_gating_cell_no_failure": d_ok,
            "verdict": verdict,
        },
        "information_only": {
            "bonferroni_level": INFO_BONFERRONI_LEVEL,
            "note": "the six Bonferroni-adjusted cell bounds are supplied for information; they do not replace or modify the gate",
            "cells": cells,
            "power_cells_non_gating": power,
            "statements": [LOW_TRADING_STATEMENT, NOT_MEASURED],
        },
        "counts": {
            "valid_by_unit": v["counts"],
            "total_valid": sum(v["counts"].values()),
            "expected_total": sum(need.values()),
        },
    }


def finalize(out: Path, m: Mapping[str, Any], identity: str) -> dict[str, Any]:
    """Write ``result.json`` and ``COMPLETED.json`` iff every unit has all its valid replications; otherwise nothing is
    written and the study stays open (or void if verification fails)."""
    s = state(out)
    if s != "open":
        raise SystemExit(f"REFUSED: study state is {s}")
    res = final_result(out, m, identity)
    if not res["complete"]:
        v = verify_outputs(out, m, identity)
        if v["problems"]:
            mark_void(out, v["problems"], identity)
            raise SystemExit("REFUSED: verification problems; study VOID")
        raise SystemExit("not complete: not every unit has its full number of valid replications")
    (out / "result.json").write_text(json.dumps(res, indent=1, default=float), encoding="utf-8")
    files = {
        f: sha256_file(out / f) for f in sorted({u["file"] for u in expected_units(m).values()})
    }
    files["result.json"] = sha256_file(out / "result.json")
    doc = {
        "state": "completed",
        "utc": _utc(),
        "identity_sha256": identity,
        "verdict": res["gate"]["verdict"],
        "total_valid_replications": res["counts"]["total_valid"],
        "file_sha256": files,
    }
    (out / "COMPLETED.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    audit(
        out,
        "completed",
        identity,
        {"verdict": doc["verdict"], "total": doc["total_valid_replications"]},
    )
    return res
