"""Register the owner-approved M5 quoting v2 pre-registration (v1.16).

Run by the OWNER (human sign-off), never by an agent::

    python tools/quoting_v2_register.py <repo_root> [--dry-run]

Writes (unless --dry-run): one blackboard prereg entry per cell (EQ01, EQ05,
EQ10, QNOSKEW at ``1s:quoting-v2-insample``) on MAIN's board via
WriteBroker.preregister (writer ``ashjha0``, one look each on
research/experiments.json), then the signed file
research/quoting_real/prereg_v2.json (Ed25519 maintainer signature, key
checked against research/agents/trust_roots.json). The draft
research/quoting_real/prereg_v2_DRAFT.json is left unchanged; its sha256 is
in every hypothesis. Never prints key material.

If it fails part-way, do NOT re-run blindly (a re-run spends more looks):
inspect the board first.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python" / "src"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from iap.agents import prereg_gate, signing, trust  # noqa: E402
from iap.agents.blackboard import canonical  # noqa: E402
from iap.agents.broker import WriteBroker  # noqa: E402

KEYFILE = Path("C:/Users/ashis/.iap_keys/maintainer.key")
APPROVER = "ashjha0"
DRAFT = "research/quoting_real/prereg_v2_DRAFT.json"
OUT = "research/quoting_real/prereg_v2.json"
LABEL = "M5-QUOTING-V2"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def hypothesis(cell: dict, dh: str) -> str:
    if cell["alpha"]:
        what = (
            f"{cell['id']}@{cell['horizon']} skewed two-sided tick quoter (skew_mode ticks, "
            f"alpha_weight {cell['alpha_weight']}, inv_skew_ticks 1/qty) earns positive "
            "session-clustered mean net USD on the 6 in-sample test sessions with a Bonferroni CI "
            "(4 cells) excluding 0 and >= 4 sessions with quote fills, and beats the QNOSKEW control "
            "on the same symbols; only if the cell passes the 0.10 skew validity gate"
        )
    else:
        what = (
            "QNOSKEW control: the same tick quoter with expected_return = 0 on AAPL, MSFT, QQQ; "
            "verdict as for the skewed cells"
        )
    return (
        f"{LABEL} (owner-approved values 2026-10-14, {DRAFT} sha256 {dh}). EXPLORATORY IN-SAMPLE "
        f"(the 7 multi7 sessions were already seen; session 1 warm-up). {what}. Approver: owner "
        "AshJha0; registration run by the owner."
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="checks only; no writes, no signing")
    ap.add_argument(
        "--require-ci-env",
        action="store_true",
        help="refuse unless numpy/pandas/scipy match python/requirements-ci.txt",
    )
    a = ap.parse_args()
    from iap.agents.fingerprint import check_registration_env

    check_registration_env(require_ci_env=a.require_ci_env)
    root = a.root.resolve()
    key = json.loads(KEYFILE.read_text(encoding="ascii"))[APPROVER]
    pub = trust.read_roots(root)["maintainers"][APPROVER]
    if signing.public_of(key) != pub:
        raise SystemExit("maintainer key does not match research/agents/trust_roots.json")
    print(f"maintainer key matches trust root {APPROVER}")
    draft = root / DRAFT
    dh = sha(draft)
    doc = json.loads(draft.read_text(encoding="utf-8"))
    if doc.get("signed") or (root / OUT).exists():
        raise SystemExit(f"{LABEL}: already signed or {OUT} exists")
    if any(str(v).startswith("TO_CONFIRM") for v in doc["quoting_config"].values()):
        raise SystemExit(f"{LABEL}: draft still has TO_CONFIRM values")
    tag = doc["horizon_tag"]
    broker = WriteBroker(root, {APPROVER})
    entries = []
    for cell in doc["cells"]:
        horizon = f"{cell['horizon']}:{tag}"
        sup = broker.is_preregistered(cell["id"], horizon)
        if a.dry_run:
            print(f"[dry-run] would preregister {cell['id']}/{horizon} supersede={sup}")
            continue
        pid = broker.preregister(
            APPROVER, cell["id"], horizon, hypothesis(cell, dh), 1, supersede=sup
        )
        e = broker.state()["preregs"][pid]
        h = next(x["hash"] for x in broker.board.entries() if x["seq"] == e["seq"])
        entries.append(
            {
                "alpha_id": cell["id"],
                "horizon": horizon,
                "prereg_id": pid,
                "blackboard_seq": e["seq"],
                "blackboard_hash": h,
                "ledger_total": e["ledger_total"],
            }
        )
    if a.dry_run:
        print(f"[dry-run] draft sha256 {dh}; would write {OUT}")
        return 0
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    doc.update(
        {
            "status": "REGISTERED - owner-approved, signed off and on the blackboard; approved for "
            "ONE exploratory in-sample run",
            "signed": True,
            "on_blackboard": True,
            "open_for_owner": [],
            "registration": {
                "approver": "owner (AshJha0)",
                "approver_id": APPROVER,
                "draft_file": DRAFT,
                "draft_sha256": dh,
                "registered_utc": now,
                "path": "iap.agents.broker.WriteBroker.preregister (human writer; one look each)",
                "blackboard": "research/agents/blackboard.jsonl",
                "entries": entries,
            },
        }
    )
    doc.pop("signature", None)
    msg = canonical(doc).encode("ascii")
    sig = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key)).sign(msg).hex()
    assert signing.verify_ed25519(pub, msg, sig)
    doc["signature"] = {
        "scheme": "ed25519",
        "maintainer": APPROVER,
        "public_key": pub,
        "trust_root": "research/agents/trust_roots.json",
        "message": "iap.agents.blackboard.canonical(this document without 'signature')",
        "message_sha256": hashlib.sha256(msg).hexdigest(),
        "sig": sig,
    }
    out = root / OUT
    with out.open("w", encoding="ascii", newline="\n") as fh:
        fh.write(json.dumps(doc, indent=2, ensure_ascii=True) + "\n")
    print(f"{LABEL}: wrote {OUT} (sha256 {sha(out)})")
    for cell in doc["cells"]:
        horizon = f"{cell['horizon']}:{tag}"
        print(
            f"gate ok {cell['id']}/{horizon} seq {prereg_gate.require(root, cell['id'], horizon)['seq']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
