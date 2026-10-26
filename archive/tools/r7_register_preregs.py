"""Register the owner-approved R7-CONFIRM-1 and AI4-DEEPLOB-1 pre-registrations.

Run by the OWNER (human sign-off), never by an agent::

    python tools/r7_register_preregs.py <repo_root> [--dry-run]

Writes (unless --dry-run): blackboard prereg entries EQ10/1s, EQ05/1s (superseding
the v1.8 entries) and AI4-deeplob/50ev via WriteBroker.preregister (writer
``ashjha0``, one look each on research/experiments.json), then the signed files
research/r7/r7_confirm_prereg.json and research/r7/ai4_deeplob_prereg.json
(Ed25519 maintainer signature, key checked against research/agents/trust_roots.json).
Never prints key material.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python" / "src"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from iap.agents import prereg_gate, signing, trust
from iap.agents.blackboard import canonical
from iap.agents.broker import WriteBroker

KEYFILE = Path("C:/Users/ashis/.iap_keys/maintainer.key")
APPROVER = "ashjha0"
SIGNED_BY = "Claude (owner-delegated in chat 2026-10-11)"
NOREAD = (
    "no holdout data (data/real/itch/r7 S1128*, S120*, S121*, S0612*) was read, decompressed "
    "or ingested before this registration; only the sha256/bytes of the compressed files "
    "recorded in the draft were computed"
)
SESSIONS = "the 7 R7 holdout sessions (2025-11-28, 2025-12-08..12, 2026-06-12; AAPL MSFT QQQ)"
PLAN = [
    (
        "research/r7/r7_confirm_prereg_DRAFT.json",
        "research/r7/r7_confirm_prereg.json",
        "R7-CONFIRM-1",
        [
            (
                a,
                "1s",
                1,
                f"H4: {a}@1s pooled uncrossed IC > 0 on {SESSIONS}; gate HAC t >= "
                "ledger threshold and deflated Sharpe > 0.95 on the per-session IC series; one "
                "look, a failure is final.",
            )
            for a in ("EQ10", "EQ05")
        ],
    ),
    (
        "research/r7/ai4_deeplob_prereg_DRAFT.json",
        "research/r7/ai4_deeplob_prereg.json",
        "AI4-DEEPLOB-1",
        [
            (
                "AI4-deeplob",
                "50ev",
                1,
                "AI4 (M-P0-1): DeepLOB classifier (arch deeplob, event clock, window 100, horizon "
                "50 events, min_confidence 0.6) earns positive mean net-of-cost taker P&L per "
                "non-overlapping trade with one-sided 95% lower bound > 0, >= 30 trades, and beats "
                f"the linear and gbm baselines, on every symbol over {SESSIONS}; costs per "
                "configs/prereg/v2.json with regulatory fees; one look.",
            ),
        ],
    ),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


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
    broker = WriteBroker(root, {APPROVER})
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    for draft_rel, out_rel, label, items in PLAN:
        draft = root / draft_rel
        dh = sha(draft)
        doc = json.loads(draft.read_text(encoding="utf-8"))
        if doc.get("signed") or (root / out_rel).exists():
            raise SystemExit(f"{label}: already signed or {out_rel} exists")
        entries = []
        for alpha, horizon, sgn, text in items:
            sup = broker.is_preregistered(alpha, horizon)
            if a.dry_run:
                print(f"[dry-run] {label}: would preregister {alpha}/{horizon} supersede={sup}")
                continue
            hyp = (
                f"{label} (owner-approved final text, {draft_rel} sha256 {dh}). {text} "
                f"Approver: owner AshJha0; signed by {SIGNED_BY}; {NOREAD}."
            )
            pid = broker.preregister(APPROVER, alpha, horizon, hyp, sgn, supersede=sup)
            e = broker.state()["preregs"][pid]
            h = next(x["hash"] for x in broker.board.entries() if x["seq"] == e["seq"])
            entries.append(
                {
                    "alpha_id": alpha,
                    "horizon": horizon,
                    "prereg_id": pid,
                    "blackboard_seq": e["seq"],
                    "blackboard_hash": h,
                    "supersedes_seq": e.get("supersedes_seq"),
                    "ledger_total": e["ledger_total"],
                    "ledger_head": e["ledger_head"],
                }
            )
        if a.dry_run:
            print(f"[dry-run] {label}: draft sha256 {dh}; would write {out_rel}")
            continue
        doc.update(
            {
                "status": "REGISTERED - owner-approved, signed off and on the blackboard; "
                "approved for ONE confirmatory look",
                "signed": True,
                "on_blackboard": True,
                "open_for_owner": [],
                "registration": {
                    "approver": "owner (AshJha0)",
                    "approver_id": APPROVER,
                    "signed_by": SIGNED_BY,
                    "approval": "owner approved the final text in chat on 2026-10-11 and delegated "
                    "sign-off/registration; registration run by the owner",
                    "draft_file": draft_rel,
                    "draft_sha256": dh,
                    "draft_commit": "dffcf45 (main)",
                    "registered_utc": now,
                    "holdout_data_read": False,
                    "holdout_note": NOREAD,
                    "path": "iap.agents.broker.WriteBroker.preregister (human writer, as the v1.8 "
                    "holdout preregs; one look debited each on research/experiments.json)",
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
        out = root / out_rel
        with out.open("w", encoding="ascii", newline="\n") as fh:
            fh.write(json.dumps(doc, indent=2, ensure_ascii=True) + "\n")
        print(
            f"{label}: wrote {out_rel} (sha256 {sha(out)}); entries "
            + ", ".join(
                f"{x['alpha_id']}/{x['horizon']} seq {x['blackboard_seq']}" for x in entries
            )
        )
    if not a.dry_run:
        for _, _, _, items in PLAN:
            for alpha, horizon, _, _ in items:
                print(
                    f"gate ok {alpha}/{horizon} seq {prereg_gate.require(root, alpha, horizon)['seq']}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
