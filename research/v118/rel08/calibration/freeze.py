"""REL-08 v2 calibration: execution freeze and pre-launch verification (protocol: ../REL08_V2_EXECUTION_PROTOCOL.md).

    python freeze.py create --root <checkout> --out execution_manifest.json
    python freeze.py verify --root <execution checkout> --manifest execution_manifest.json
                            --evidence <file> [--launch --out-dir <results dir>]

``create`` records the frozen identity: the SHA-256 of every frozen file (plan, protocol, manifest, runner, integrity module,
this script, the dry-run generator, the rule draft body), the python/src/iap code tree, configs and reference trees, the
dependency lock, the installed versions of every locked package, the interpreter, the calibration manifest (cells, replication counts,
seeds), the declared null definitions and the preregistered gate.  The identity is content-only (no commit hash, time or machine),
so it survives merging; the commit is recorded for information.

``verify`` recomputes the identity in the execution checkout and refuses (exit 1, evidence written with ok=false) when anything
differs.  With ``--launch`` it also checks the actor, that no signing key variable is set, the memory prerequisite and that the
output directory is fresh (or open with verified content).  It reads no market data.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = 1
EXPECTED_ACTOR = "claude-queue-runner"
KEY_ENV = ("IAP_MAINTAINER_KEY", "IAP_SIGNING_KEY", "IAP_AGENT_PRIVATE_KEY", "IAP_KEYFILE")
MIN_FREE_RAM_GB = 4.0
CAL = "research/v118/rel08/calibration"
FROZEN_FILES = (
    "research/v118/rel08/REL08_V2_CALIBRATION_PLAN_REV2.md",
    "research/v118/rel08/REL08_V2_EXECUTION_PROTOCOL.md",
    "research/v118/rel08/REL08_V2_CALIBRATION_MANIFEST.json",
    f"{CAL}/run_calibration.py",
    f"{CAL}/integrity.py",
    f"{CAL}/freeze.py",
    "research/v118/rel08/dryrun/rel08_v2_dryrun.py",
    "research/v118/rel08_rule_DRAFT.json",
)
NULL_DEFINITIONS = {
    "N1": "martingale prices, common factor, i.i.d. idiosyncratic noise (dry-run null mode)",
    "N2": "N1 plus log-volatility AR(1) clustering (phi 0.95, sd 0.3) and i.i.d. microstructure noise sd 1e-4 on log mid",
    "N3": "N1 plus uninformative persistent scores: score times a slowly varying random sign (AR(1) sign process, half-life 20 columns)",
    "N4": "N1 with per-session log-normal volatility and spread multipliers (sd 0.4) and a mean-zero daily common shock",
}
# every other seed stream in use, as (name, first, last): none may overlap a calibration cell or power case
OTHER_STREAMS = (
    ("timing", 20269000, 20269003),
    ("equivalence_check", 20269300, 20269330),
    ("equivalence_tests", 20269401, 20269403),
    ("equivalence_tests_unit", 20269201, 20269202),
    ("dry_run_small", 1, 99999),  # the dry run used small integer seeds (5000+, 9000+, 9100+)
)
ORIGINAL_NULL_SEED_BASE = 20261100  # the original plan's null-cell seeds: base + 1000 * cell_index


def _norm_sha(p: Path) -> str:
    text = p.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def available_ram_gb() -> float | None:
    try:
        if sys.platform == "win32":

            class MS(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            ms = MS()
            ms.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))  # type: ignore[attr-defined]
            return ms.ullAvailPhys / 2**30
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 2**20
    except Exception:  # noqa: BLE001
        return None
    return None


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def calibration_manifest(root: Path) -> dict[str, Any]:
    """The runner's own manifest, loaded from the checkout under verification."""
    sys.path.insert(0, str(root / "python" / "src"))
    spec = importlib.util.spec_from_file_location(
        "run_calibration", root / CAL / "run_calibration.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_calibration"] = mod
    spec.loader.exec_module(mod)
    return json.loads(json.dumps(mod.manifest()))


def seed_report(m: dict[str, Any]) -> dict[str, Any]:
    """Disjointness of every seed stream, and the original null seeds unchanged."""
    spans = [(c["cell"], c["seed_first"], c["seed_last"]) for c in m["null_cells"]]
    spans += [
        (f"power_{p['kind']}_{p['amp']}", p["seed_first"], p["seed_last"]) for p in m["power_cells"]
    ]
    all_spans = sorted(spans + list(OTHER_STREAMS), key=lambda t: t[1])
    overlaps = []
    for i, (n1, a1, b1) in enumerate(all_spans):  # all pairs: no stream may overlap any other
        for n2, a2, b2 in all_spans[i + 1 :]:
            if not (b1 < a2 or b2 < a1):
                overlaps.append([n1, n2])
    unchanged = all(
        c["seed_first"] == ORIGINAL_NULL_SEED_BASE + 1000 * i for i, c in enumerate(m["null_cells"])
    )
    power_first = min(p["seed_first"] for p in m["power_cells"])
    return {
        "streams": [{"name": n, "first": a, "last": b} for n, a, b in all_spans],
        "overlaps": overlaps,
        "disjoint": not overlaps,
        "original_null_seeds_unchanged": unchanged,
        "power_seeds_start_at": power_first,
        "stopped_launch_note": "the stopped first launch consumed null-cell seeds 20261100 and 20261101 (N1S1, r = 0, 1); they are the "
        "frozen seeds and are re-run from scratch; the discarded records cannot enter the new results (stamped identity + new output directory)",
    }


def identity_parts(root: Path) -> dict[str, Any]:
    sys.path.insert(0, str(root / "python" / "src"))
    from iap.agents import analysis_identity as ai

    root = root.resolve()
    gate_mod_spec = importlib.util.spec_from_file_location("integrity", root / CAL / "integrity.py")
    gate_mod = importlib.util.module_from_spec(gate_mod_spec)
    gate_mod_spec.loader.exec_module(gate_mod)
    files = {rel: _norm_sha(root / rel) for rel in FROZEN_FILES}
    return {
        "x-version": SCHEMA,
        "files": files,
        "environment_block": ai._environment_block(root),
        "calibration_manifest": calibration_manifest(root),
        "null_definitions": NULL_DEFINITIONS,
        "gate": dict(gate_mod.GATE),
        "short_circuit": True,
    }


def identity_sha256(parts: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def create(root: Path, out: Path) -> dict[str, Any]:
    parts = identity_parts(root)
    m = parts["calibration_manifest"]
    sr = seed_report(m)
    if not sr["disjoint"] or not sr["original_null_seeds_unchanged"]:
        raise SystemExit(f"REFUSED: seed streams overlap or original null seeds changed: {sr}")
    doc = {
        "kind": "rel08_v2_calibration_execution_manifest",
        "schema_version": SCHEMA,
        "status": "FROZEN FOR REVIEW (not yet authorised to launch)",
        "identity_sha256": identity_sha256(parts),
        "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "frozen_commit_info_only": git(root, "rev-parse", "HEAD").stdout.strip(),
        "identity_parts": parts,
        "calibration_manifest_sha256": hashlib.sha256(
            json.dumps(m, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "seed_disjointness": sr,
    }
    out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return doc


def identity_matches(root: Path, manifest_path: Path) -> tuple[bool, str, str]:
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    now = identity_sha256(identity_parts(root))
    return now == doc["identity_sha256"], now, doc["identity_sha256"]


def verify(
    root: Path, manifest_path: Path, evidence: Path, launch: bool, out_dir: Path | None
) -> int:
    root = root.resolve()
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    parts = identity_parts(root)
    now = identity_sha256(parts)
    ev: dict[str, Any] = {
        "kind": "rel08_v2_calibration_execution_evidence",
        "utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checkout_head": git(root, "rev-parse", "HEAD").stdout.strip(),
        "checks": {},
    }
    ck = ev["checks"]
    diffs = sorted(
        k
        for k in set(parts["files"]) | set(doc["identity_parts"]["files"])
        if parts["files"].get(k) != doc["identity_parts"]["files"].get(k)
    )
    block_diffs = sorted(
        k
        for k in parts["environment_block"]
        if parts["environment_block"][k] != doc["identity_parts"]["environment_block"].get(k)
    )
    ck["1_identity"] = {
        "frozen": doc["identity_sha256"],
        "recomputed": now,
        "file_differences": diffs,
        "environment_block_differences": block_diffs,
        "ok": now == doc["identity_sha256"],
    }
    clean = git(root, "status", "--porcelain").stdout.strip() == ""
    anc = git(root, "merge-base", "--is-ancestor", doc["frozen_commit_info_only"], "HEAD")
    ck["2_checkout"] = {
        "clean": clean,
        "frozen_commit_is_ancestor_info_only": anc.returncode == 0,
        "ok": clean,
    }
    sr = seed_report(parts["calibration_manifest"])
    ck["3_seeds"] = {
        "disjoint": sr["disjoint"],
        "original_null_seeds_unchanged": sr["original_null_seeds_unchanged"],
        "ok": sr["disjoint"] and sr["original_null_seeds_unchanged"],
    }
    if launch:
        actor = os.environ.get("IAP_RUN_ACTOR")
        keys_set = [k for k in KEY_ENV if os.environ.get(k)]
        ram = available_ram_gb()
        state_files = [p.name for p in (out_dir.glob("*") if out_dir and out_dir.exists() else [])]
        ck["4_runner_and_state"] = {
            "actor": actor,
            "signing_env_vars_set": keys_set,
            "output_dir": str(out_dir),
            "output_dir_files": state_files,
            "ok": actor == EXPECTED_ACTOR
            and not keys_set
            and "COMPLETED.json" not in state_files
            and "VOID.json" not in state_files,
        }
        ck["5_memory"] = {
            "min_free_ram_gb": MIN_FREE_RAM_GB,
            "available_gb": ram,
            "ok": ram is None or ram >= MIN_FREE_RAM_GB,
        }
    ev["ok"] = all(c.get("ok") for c in ck.values())
    evidence.write_text(json.dumps(ev, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v.get("ok") for k, v in ck.items()} | {"ok": ev["ok"]}, indent=1))
    return 0 if ev["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("cmd", choices=("create", "verify"))
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None, help="create: manifest path")
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--evidence", type=Path, default=None)
    ap.add_argument("--launch", action="store_true")
    ap.add_argument("--out-dir", type=Path, default=None)
    a = ap.parse_args(argv)
    if a.cmd == "create":
        doc = create(a.root, a.out)
        print(doc["identity_sha256"])
        return 0
    return verify(a.root, a.manifest, a.evidence, a.launch, a.out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
