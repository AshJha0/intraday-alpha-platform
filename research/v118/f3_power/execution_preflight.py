"""F3 power diagnostic: execution-checkout verification (reviewer's pre-execution conditions, 2026-10-24).

Run from the execution checkout as the non-signing runner, BEFORE the single execution:

    python f3_power_preflight.py --root <checkout> --out <checkout>/research/v118/f3_power/EXECUTION_EVIDENCE.json

Records the six reviewer checks and exits 1 (evidence written with ok=false) if any fails:
 1. exact preregistration SHA-256 and a valid maintainer signature against the trust root
 2. bound commit is an ancestor of the (clean) execution checkout HEAD
 3. every bound script / imported acceptance core.py / protocol / declaration / configuration / dependency lock / interpreter hash matches (the runner's verify)
 4. check_freeze.py passes all 30 checks and the prescribed 200-dataset dry run passes (no study observations kept)
 5. actor claude-queue-runner, no signing-key environment variables, no DECISION.json and no cell file
 6. the declared memory prerequisite is satisfied
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

EXPECTED_PREREG_SHA256 = "af2ffefc8920700c47972f90ae067bd16debe5e1bf49215aba42324d6fd88830"
EXPECTED_BOUND_COMMIT = "4f39f3a8a5905207ac0ac2c49c2c54b3bd6f04e8"
EXPECTED_ACTOR = "claude-queue-runner"
KEY_ENV = ("IAP_MAINTAINER_KEY", "IAP_SIGNING_KEY", "IAP_AGENT_PRIVATE_KEY", "IAP_KEYFILE")


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def run_py(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONPATH=str(root / "python" / "src"))
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, cwd=root, env=env
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    root = a.root.resolve()
    sys.path.insert(0, str(root / "python" / "src"))
    f3 = root / "research" / "v118" / "f3_power"
    spec = importlib.util.spec_from_file_location("run_f3_power", f3 / "run_f3_power.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_f3_power"] = mod
    spec.loader.exec_module(mod)

    ev: dict = {
        "kind": "f3_power_execution_evidence",
        "utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checkout_head": git(root, "rev-parse", "HEAD").stdout.strip(),
        "checkout_clean": git(root, "status", "--porcelain").stdout.strip() == "",
        "checks": {},
    }
    decl = mod.load_decl(mod.DECL)
    prereg = root / decl["prereg"]
    ck = ev["checks"]
    doc = None

    # 1. exact sha256 + signature
    sha = hashlib.sha256(prereg.read_bytes()).hexdigest() if prereg.is_file() else None
    sig: dict = {"prereg_sha256": sha, "expected": EXPECTED_PREREG_SHA256}
    try:
        from iap.agents.analysis_identity import verify_signed_document

        doc = json.loads(prereg.read_text(encoding="utf-8"))
        verify_signed_document(root, doc)
        sig.update(
            signature_ok=True,
            maintainer=doc["signature"]["maintainer"],
            public_key=doc["signature"]["public_key"],
        )
    except Exception as exc:  # noqa: BLE001 (recorded as evidence)
        sig.update(signature_ok=False, error=str(exc))
    sig["ok"] = sha == EXPECTED_PREREG_SHA256 and bool(sig.get("signature_ok"))
    ck["1_sha256_and_signature"] = sig

    # 2. bound commit ancestor of a clean checkout
    anc = git(root, "merge-base", "--is-ancestor", EXPECTED_BOUND_COMMIT, "HEAD")
    bound = doc["analysis_identity"]["commit"] if doc else None
    ck["2_bound_commit_ancestor"] = {
        "expected": EXPECTED_BOUND_COMMIT,
        "prereg_commit": bound,
        "checkout_clean": ev["checkout_clean"],
        "ok": anc.returncode == 0 and bound == EXPECTED_BOUND_COMMIT and ev["checkout_clean"],
    }

    # 3. bound hashes via the runner's own verify
    try:
        rec = mod.verify(prereg, decl, root)
        ai = doc["analysis_identity"]
        ck["3_bound_hashes"] = {
            "ok": True,
            "identity_sha256": rec["identity_sha256"],
            "code_tree_sha256": ai["code_tree_sha256"],
            "configs_tree_sha256": ai["configs_tree_sha256"],
            "deps_lock_sha256": ai["deps_lock_sha256"],
            "interpreter": ai.get("interpreter"),
            "scripts": ai.get("scripts"),
            "protocol_sha256": doc.get("protocol_sha256"),
            "market_data_consumed": rec["market_data_consumed"],
        }
    except BaseException as exc:  # noqa: BLE001 (Refused is a SystemExit)
        ck["3_bound_hashes"] = {"ok": False, "error": str(exc)}

    # 4. check_freeze 30/30 and the 200-dataset dry run
    cf = run_py(root, str(f3 / "check_freeze.py"))
    dr = run_py(root, str(f3 / "run_f3_power.py"), "dry-run")
    ck["4_freeze_check_and_dry_run"] = {
        "check_freeze_30_of_30": "30/30 checks OK" in cf.stdout and cf.returncode == 0,
        "dry_run_pass": "[dry-run] PASS" in dr.stdout and dr.returncode == 0,
        "dry_run_tail": dr.stdout.strip().splitlines()[-3:],
    }
    ck["4_freeze_check_and_dry_run"]["ok"] = (
        ck["4_freeze_check_and_dry_run"]["check_freeze_30_of_30"]
        and ck["4_freeze_check_and_dry_run"]["dry_run_pass"]
    )

    # 5. actor, credentials, no earlier decision or cell file
    actor = os.environ.get("IAP_RUN_ACTOR")
    keys_set = [k for k in KEY_ENV if os.environ.get(k)]
    decision = (f3 / "DECISION.json").exists()
    cells = sorted(p.name for p in f3.glob("cell_*.json"))
    ck["5_runner_and_state"] = {
        "actor": actor,
        "signer": doc["signature"]["maintainer"] if doc else None,
        "signing_env_vars_set": keys_set,
        "decision_json_exists": decision,
        "cell_files_present": cells,
        "ok": actor == EXPECTED_ACTOR and not keys_set and not decision and not cells,
        "note": "the runner process is given no key variable and no key path; the signer's keyfile exists on the same machine under the owner's home directory and is not read by this runner (env-var absence is not filesystem isolation)",
    }

    # 6. declared memory prerequisite
    need = float(decl["stop_rules"]["min_free_ram_gb"])
    ram = mod.available_ram_gb()
    ck["6_memory_prerequisite"] = {
        "min_free_ram_gb": need,
        "available_gb": ram,
        "ok": ram is None or ram >= need,
    }

    ev["ok"] = all(c.get("ok") for c in ck.values())
    a.out.write_text(json.dumps(ev, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v.get("ok") for k, v in ck.items()} | {"ok": ev["ok"]}, indent=1))
    return 0 if ev["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
