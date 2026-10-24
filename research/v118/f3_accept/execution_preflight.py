"""F3 acceptance study: execution-checkout verification (reviewer's named pre-execution condition, 2026-10-24).

Run from the execution checkout root as the non-signing runner, BEFORE the first acceptance execution:

    python f3_preflight.py --root <checkout> --out <checkout>/research/v118/f3_accept/EXECUTION_EVIDENCE.json

Records five checks and exits 1 (writing the evidence with ok=false) if any fails:
 1. the exact preregistration SHA-256
 2. the maintainer signature verifies against the trust root (iap.agents.analysis_identity.verify_signed_document)
 3. the bound commit is an ancestor of the execution checkout HEAD
 4. every bound script / protocol / configuration / dependency / analysis-identity hash matches (verify_identity, as run_f3_accept.verify)
 5. the runner actor is claude-queue-runner, no signing credentials are in the environment, and no DECISION.json exists
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

EXPECTED_PREREG_SHA256 = "abe85c3ad93ebdd7381a6a907b018f840dc626638b157c01f408a72f01af2607"
EXPECTED_BOUND_COMMIT = "24eac89be177a357b6df54c4992142467c6351ff"
EXPECTED_ACTOR = "claude-queue-runner"
KEY_ENV = ("IAP_MAINTAINER_KEY", "IAP_SIGNING_KEY", "IAP_AGENT_PRIVATE_KEY", "IAP_KEYFILE")


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    root = a.root.resolve()
    sys.path.insert(0, str(root / "python" / "src"))
    spec = importlib.util.spec_from_file_location(
        "run_f3_accept", root / "research" / "v118" / "f3_accept" / "run_f3_accept.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_f3_accept"] = mod
    spec.loader.exec_module(mod)

    ev: dict = {
        "kind": "f3_accept_execution_evidence",
        "utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checkout_head": git(root, "rev-parse", "HEAD").stdout.strip(),
        "checkout_clean": git(root, "status", "--porcelain").stdout.strip() == "",
        "checks": {},
    }
    decl = mod.load_decl(mod.DECL)
    prereg = root / decl["prereg"]
    ck = ev["checks"]

    # 1. exact prereg sha256
    sha = hashlib.sha256(prereg.read_bytes()).hexdigest() if prereg.is_file() else None
    ck["1_prereg_sha256"] = {
        "expected": EXPECTED_PREREG_SHA256,
        "actual": sha,
        "ok": sha == EXPECTED_PREREG_SHA256,
    }

    # 2. signature against the trusted key
    try:
        from iap.agents.analysis_identity import verify_signed_document

        doc = json.loads(prereg.read_text(encoding="utf-8"))
        verify_signed_document(root, doc)
        ck["2_signature"] = {
            "ok": True,
            "maintainer": doc["signature"]["maintainer"],
            "public_key": doc["signature"]["public_key"],
            "trust_root": doc["signature"].get("trust_root"),
        }
    except Exception as exc:  # noqa: BLE001 (recorded as evidence)
        ck["2_signature"] = {"ok": False, "error": str(exc)}

    # 3. bound commit is an ancestor of the execution checkout
    anc = git(root, "merge-base", "--is-ancestor", EXPECTED_BOUND_COMMIT, "HEAD")
    bound = None
    try:
        bound = doc["analysis_identity"]["commit"]
    except Exception:  # noqa: BLE001
        pass
    ck["3_bound_commit_ancestor"] = {
        "expected": EXPECTED_BOUND_COMMIT,
        "prereg_commit": bound,
        "ok": anc.returncode == 0 and bound == EXPECTED_BOUND_COMMIT,
    }

    # 4. bound hashes (scripts, protocol, configs tree, deps lock, code tree, environment): run_f3_accept.verify
    try:
        rec = mod.verify(prereg, decl, root)
        ai = doc["analysis_identity"]
        ck["4_bound_hashes"] = {
            "ok": True,
            "identity_sha256": rec["identity_sha256"],
            "code_tree_sha256": ai["code_tree_sha256"],
            "configs_tree_sha256": ai["configs_tree_sha256"],
            "deps_lock_sha256": ai["deps_lock_sha256"],
            "scripts": ai.get("scripts"),
            "protocol_sha256": doc.get("protocol_sha256"),
            "market_data_consumed": rec["market_data_consumed"],
        }
    except BaseException as exc:  # noqa: BLE001 (Refused is a SystemExit)
        ck["4_bound_hashes"] = {"ok": False, "error": str(exc)}

    # 5. runner actor, credentials, no earlier final decision
    actor = os.environ.get("IAP_RUN_ACTOR")
    keys_set = [k for k in KEY_ENV if os.environ.get(k)]
    decision = (root / "research" / "v118" / "f3_accept" / "DECISION.json").exists()
    cells = sorted(p.name for p in (root / "research" / "v118" / "f3_accept").glob("cell_*.json"))
    ck["5_runner_and_state"] = {
        "actor": actor,
        "signer": doc["signature"]["maintainer"] if "doc" in dir() else None,
        "signing_env_vars_set": keys_set,
        "decision_json_exists": decision,
        "cell_files_present": cells,
        "ok": actor == EXPECTED_ACTOR and not keys_set and not decision and not cells,
        "note": "the runner process is given no key variable and no key path; the maintainer keyfile of the signer exists on the same machine under the owner's home directory and is not read by this runner",
    }

    ev["ok"] = all(c.get("ok") for c in ck.values())
    a.out.write_text(json.dumps(ev, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v.get("ok") for k, v in ck.items()} | {"ok": ev["ok"]}, indent=1))
    return 0 if ev["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
