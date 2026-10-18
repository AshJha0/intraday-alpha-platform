#!/usr/bin/env python
"""Outside review finding 6: reproduce the REL-01 baseline in a clean environment.

From a fresh clone at a given commit this script

1. creates a virtual environment and installs ``python/requirements-ci.txt`` with
   ``--require-hashes`` (the CI lock file),
2. records the interpreter, platform and installed package versions,
3. runs ``tools/v118_baseline.py --check`` against the committed file, then
   ``tools/v118_baseline.py`` (which overwrites ``research/v118/baseline_metrics.json`` in the clone),
4. compares the regenerated JSON with the committed one under the reproducibility contract
   (programme section 9.6): byte-identical first; otherwise every difference is listed, counts and
   strings must be exact, ``*_bps``-style floats agree to two decimals, other floats to 1e-12
   relative; the provenance fields the tool itself excludes in ``--check`` (``commit``,
   ``tree_is_clean_at_inputs``, ``toolchain.python_used_for_this_baseline``) are listed as
   expected, not as discrepancies,
5. writes a reproduction record (JSON + Markdown): commit, environment, every command with its
   exit code and output tail, and the discrepancies.

Stdlib only; reads only committed files.  Exit 0 = reproduced under the contract, 1 = not.

    python tools/reproduce_baseline.py --repo https://github.com/AshJha0/intraday-alpha-platform \\
        --commit <sha> --out docs/review/reproduction_baseline_<date>.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASELINE = "research/v118/baseline_metrics.json"
PROVENANCE = {"commit", "tree_is_clean_at_inputs", "toolchain.python_used_for_this_baseline"}
TAIL = 4000


def _run(cmd: list[str], cwd: Path, env: dict | None = None) -> dict:
    p = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return {
        "cmd": " ".join(cmd),
        "cwd": str(cwd),
        "exit_code": p.returncode,
        "stdout_tail": p.stdout[-TAIL:],
        "stderr_tail": p.stderr[-TAIL:],
    }


def _bps_key(path: str) -> bool:
    leaf = path.rsplit(".", 1)[-1]
    return "bps" in path or leaf in {"mean", "se", "ci_low", "ci_high", "mde_at_t_crit"}


def compare(a, b, path: str = "") -> list[dict]:
    """Differences between committed ``a`` and regenerated ``b`` under the 9.6 contract."""
    out: list[dict] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            p = f"{path}.{k}" if path else k
            if k not in a or k not in b:
                out.append(
                    {"path": p, "committed": a.get(k), "reproduced": b.get(k), "kind": "key"}
                )
            else:
                out += compare(a[k], b[k], p)
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [{"path": path, "committed": len(a), "reproduced": len(b), "kind": "length"}]
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out += compare(x, y, f"{path}[{i}]")
        return out
    num = (int, float)
    if isinstance(a, float) and isinstance(b, num) or isinstance(b, float) and isinstance(a, num):
        if a == b:
            return []
        if _bps_key(path):
            ok = round(float(a), 2) == round(float(b), 2)
        else:
            ok = math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=0.0)
        return [
            {
                "path": path,
                "committed": a,
                "reproduced": b,
                "kind": "float_within_tolerance" if ok else "float",
            }
        ]
    if a != b or type(a) is not type(b):
        return [{"path": path, "committed": a, "reproduced": b, "kind": "exact"}]
    return []


def _markdown(rec: dict) -> str:
    env = rec["environment"]
    lines = [
        f"# Baseline reproduction record ({rec['date_utc'][:10]})",
        "",
        f"**Result: {rec['verdict']}.** {rec['verdict_detail']}",
        "",
        f"- Repository: `{rec['repo']}`",
        f"- Commit: `{rec['commit']}`",
        f"- Clean clone: `{rec['clone_dir']}` (temporary, deleted after the run unless --keep)",
        f"- Interpreter: `{env['python_version']}` ({env['implementation']}), `{env['platform']}`",
        f"- Lock file: `python/requirements-ci.txt` sha256 `{env['requirements_ci_sha256']}`, "
        "installed with `--require-hashes`",
        f"- Packages installed: {len(env['packages'])} (full list in the JSON record)",
        "",
        "## Commands",
        "",
        "| # | command | exit |",
        "|---|---|---|",
    ]
    for i, c in enumerate(rec["commands"], 1):
        lines.append(f"| {i} | `{c['cmd']}` | {c['exit_code']} |")
    lines += [
        "",
        "## Comparison with the committed baseline_metrics.json (programme 9.6)",
        "",
        f"- Byte-identical: **{rec['comparison']['byte_identical']}**",
        f"- Committed sha256: `{rec['comparison']['committed_sha256']}`",
        f"- Reproduced sha256: `{rec['comparison']['reproduced_sha256']}`",
        f"- Expected provenance differences: {len(rec['comparison']['provenance'])}",
        f"- Within-tolerance differences: {len(rec['comparison']['within_tolerance'])}",
        f"- Discrepancies: **{len(rec['comparison']['discrepancies'])}**",
        "",
    ]
    for title, key in (
        ("Expected provenance differences", "provenance"),
        ("Within tolerance", "within_tolerance"),
        ("Discrepancies", "discrepancies"),
    ):
        rows = rec["comparison"][key]
        if rows:
            lines += [f"### {title}", "", "| path | committed | reproduced |", "|---|---|---|"]
            lines += [f"| `{d['path']}` | `{d['committed']}` | `{d['reproduced']}` |" for d in rows]
            lines.append("")
    return "\n".join(lines) + "\n"


def _sha256(p: Path) -> str:
    import hashlib

    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, help="clone URL or local path")
    ap.add_argument("--commit", required=True)
    ap.add_argument(
        "--out", required=True, type=Path, help="record path (.json; .md written beside)"
    )
    ap.add_argument("--workdir", type=Path, default=None, help="parent of the temporary clone")
    ap.add_argument("--keep", action="store_true", help="keep the clone and venv")
    args = ap.parse_args()

    base = Path(tempfile.mkdtemp(prefix="iap-repro-", dir=args.workdir))
    clone = base / "repo"
    cmds: list[dict] = []

    def step(cmd: list[str], cwd: Path, env: dict | None = None) -> dict:
        r = _run(cmd, cwd, env)
        cmds.append(r)
        return r

    ok = step(["git", "clone", "--quiet", "--no-hardlinks", args.repo, str(clone)], base)
    if ok["exit_code"] == 0:
        ok = step(
            ["git", "-c", "advice.detachedHead=false", "checkout", "--quiet", args.commit], clone
        )
    if ok["exit_code"] == 0:
        commit = step(["git", "rev-parse", "HEAD"], clone)["stdout_tail"].strip()
    else:
        commit = args.commit
    venv = base / "venv"
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if ok["exit_code"] == 0:
        ok = step([sys.executable, "-m", "venv", str(venv)], base)
    if ok["exit_code"] == 0:
        ok = step(
            [
                str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                "--no-input", "--require-hashes", "--only-binary", ":all:",
                "-r", "python/requirements-ci.txt",
            ],
            clone,
        )  # fmt: skip
    env_rec: dict = {"packages": {}, "requirements_ci_sha256": "", "python_version": "",
                     "implementation": "", "platform": platform.platform()}  # fmt: skip
    if ok["exit_code"] == 0:
        info = step(
            [str(py), "-c", "import json,sys,platform,importlib.metadata as m;"
             "print(json.dumps({'python_version':sys.version.split()[0],"
             "'implementation':platform.python_implementation(),'executable':sys.executable,"
             "'packages':{d.metadata['Name']:d.version for d in m.distributions()}}))"],
            clone,
        )  # fmt: skip
        if info["exit_code"] == 0:
            env_rec.update(json.loads(info["stdout_tail"]))
            info["stdout_tail"] = "(recorded under environment)"
        env_rec["requirements_ci_sha256"] = _sha256(clone / "python/requirements-ci.txt")
    env_run = {**os.environ, "PYTHONPATH": str(clone / "python" / "src"), "PYTHONHASHSEED": "0"}
    committed = clone / BASELINE
    committed_bytes = committed.read_bytes() if committed.exists() else b""
    gen = None
    if ok["exit_code"] == 0:
        step([str(py), "tools/v118_baseline.py", "--check"], clone, env_run)
        gen = step([str(py), "tools/v118_baseline.py"], clone, env_run)
    status = step(["git", "status", "--porcelain"], clone) if clone.exists() else None

    comparison: dict = {"byte_identical": False, "committed_sha256": "", "reproduced_sha256": "",
                  "provenance": [], "within_tolerance": [], "discrepancies": []}  # fmt: skip
    if gen is not None and gen["exit_code"] == 0 and committed_bytes:
        import hashlib

        rep_bytes = committed.read_bytes()
        comparison["committed_sha256"] = hashlib.sha256(committed_bytes).hexdigest()
        comparison["reproduced_sha256"] = hashlib.sha256(rep_bytes).hexdigest()
        comparison["byte_identical"] = committed_bytes == rep_bytes
        for d in compare(json.loads(committed_bytes), json.loads(rep_bytes)):
            if d["path"] in PROVENANCE:
                comparison["provenance"].append(d)
            elif d["kind"] == "float_within_tolerance":
                comparison["within_tolerance"].append(d)
            else:
                comparison["discrepancies"].append(d)
    else:
        comparison["discrepancies"].append(
            {
                "path": "<run>",
                "committed": "",
                "reproduced": "baseline tool did not run",
                "kind": "run",
            }
        )

    failed = [c for c in cmds if c["exit_code"] != 0]
    reproduced = not failed and not comparison["discrepancies"]
    if reproduced and comparison["byte_identical"]:
        verdict, detail = (
            "REPRODUCED (byte-identical)",
            "The regenerated file equals the committed one.",
        )
    elif reproduced:
        verdict = "REPRODUCED (within the declared tolerance)"
        detail = "Every non-provenance value matches; differences are listed below."
    else:
        verdict = "NOT REPRODUCED"
        detail = (
            f"{len(failed)} command(s) failed; {len(comparison['discrepancies'])} discrepancies."
        )
    rec = {
        "schema": "iap.reproduction_record/1",
        "date_utc": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "repo": args.repo,
        "requested_commit": args.commit,
        "commit": commit,
        "clone_dir": str(clone),
        "host_python": sys.version.split()[0],
        "environment": env_rec,
        "contract": "IAP_Release_Programme_2026-10-17 section 9.6: byte-identical, else counts and "
        "strings exact, bps floats to 2 decimals, other floats rel 1e-12; provenance fields "
        f"{sorted(PROVENANCE)} expected to differ",
        "commands": cmds,
        "clone_dirty_after_run": status["stdout_tail"].splitlines() if status else None,
        "comparison": comparison,
        "verdict": verdict,
        "verdict_detail": detail,
        "exit_code": 0 if reproduced else 1,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.out.with_suffix(".md").write_text(_markdown(rec), encoding="utf-8")
    print(f"{verdict}: record {args.out}")
    if not args.keep:
        shutil.rmtree(base, ignore_errors=True)
    return rec["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
