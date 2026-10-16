"""Register the v1.17 C8 breadth-book pre-registration under option (a).

Run by the OWNER (human sign-off), never by an agent::

    python tools/breadth_book_register.py <repo_root> [--dry-run] [--require-ci-env]

Option (a) registers the book with the C6 net-of-cost screen as a HARD gate.
The registration goes through ``WriteBroker.preregister(screen=...)`` for the
book's screen candidate (EQ11 at ``15m:breadth-book-c8``). The screen
``research/breadth/screen/screen_multi7.json`` did not screen EQ11/15m, so
the broker's gate refuses before any blackboard write or ledger look: that
refusal IS the registered outcome ("no confirmatory look"). The tool then
writes the signed record research/preregs/breadth_book_prereg.json (Ed25519
maintainer signature, key checked against research/agents/trust_roots.json)
carrying the refusal text. No data is read; no holdout dataset is built.

Refuses unless the draft records ``decision_required.chosen.option == "a"``
and the screen file's sha256 equals the one pinned in the draft. The draft
research/preregs/breadth_book_prereg_DRAFT.json is left unchanged; its
sha256 is in the record. Never prints key material.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python" / "src"))

from iap.agents.broker import BrokerError, WriteBroker  # noqa: E402
from iap.research.screen import ScreenRefusal, require_screened  # noqa: E402

KEYFILE = Path("C:/Users/ashis/.iap_keys/maintainer.key")
APPROVER = "ashjha0"
DRAFT = "research/preregs/breadth_book_prereg_DRAFT.json"
OUT = "research/preregs/breadth_book_prereg.json"
LABEL = "C8-BREADTH-BOOK"
ALPHA, HORIZON_BASE, MODEL = "EQ11", "15m", "linear_z_v1"
HORIZON = f"{HORIZON_BASE}:breadth-book-c8"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def hypothesis(dh: str) -> str:
    return (
        f"{LABEL} (owner option (a), {DRAFT} sha256 {dh}). The eq11 cross-sectional book on the "
        "breadth holdout earns a positive day-clustered net P&L; registered with the C6 screen "
        "as a hard gate. Approver: owner AshJha0; registration run by the owner."
    )


def checks(root: Path) -> tuple[dict, str, Path]:
    """The draft, its sha256 and the pinned screen path; SystemExit on any mismatch."""
    draft = root / DRAFT
    doc = json.loads(draft.read_text(encoding="utf-8"))
    chosen = (doc.get("decision_required") or {}).get("chosen") or {}
    if chosen.get("option") != "a":
        raise SystemExit(f"{LABEL}: draft decision is {chosen.get('option')!r}, not option (a)")
    if doc.get("signed") or (root / OUT).exists():
        raise SystemExit(f"{LABEL}: already signed or {OUT} exists")
    dep = doc["screen_dependency"]
    screen = root / dep["path"]
    got = sha(screen)
    if got != dep["sha256"]:
        raise SystemExit(f"{LABEL}: {dep['path']} sha256 {got} != pinned {dep['sha256']}")
    print(f"draft option (a) ok; screen {dep['path']} sha256 matches the pin")
    return doc, sha(draft), screen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="checks only; no writes, no signing")
    ap.add_argument(
        "--require-ci-env",
        action="store_true",
        help="refuse unless numpy/pandas/scipy match python/requirements-ci.txt",
    )
    a = ap.parse_args(argv)
    from iap.agents.fingerprint import check_registration_env

    check_registration_env(require_ci_env=a.require_ci_env)
    root = a.root.resolve()
    doc, dh, screen = checks(root)
    try:
        require_screened(screen, ALPHA, HORIZON_BASE, MODEL)
        gate = None
    except ScreenRefusal as exc:
        gate = str(exc)
    if a.dry_run:
        print(f"[dry-run] draft sha256 {dh}")
        print(f"[dry-run] screen gate for {ALPHA}/{HORIZON_BASE}/{MODEL}: {gate or 'PASS'}")
        print(
            f"[dry-run] would call WriteBroker.preregister({ALPHA}/{HORIZON}, screen=...) "
            f"-> {'refused (expected under option a)' if gate else 'registered'}; would write {OUT}"
        )
        return 0

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from iap.agents import signing, trust
    from iap.agents.blackboard import canonical

    key = json.loads(KEYFILE.read_text(encoding="ascii"))[APPROVER]
    pub = trust.read_roots(root)["maintainers"][APPROVER]
    if signing.public_of(key) != pub:
        raise SystemExit("maintainer key does not match research/agents/trust_roots.json")
    print(f"maintainer key matches trust root {APPROVER}")
    broker = WriteBroker(root, {APPROVER})
    entry: dict | None = None
    refusal: str | None = None
    try:
        pid = broker.preregister(APPROVER, ALPHA, HORIZON, hypothesis(dh), 1, screen=screen)
        e = broker.state()["preregs"][pid]
        entry = {"prereg_id": pid, "blackboard_seq": e["seq"], "ledger_total": e["ledger_total"]}
    except BrokerError as exc:
        refusal = str(exc)
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    doc.update(
        {
            "status": (
                "REGISTERED under option (a) - the C6 screen gate refused the candidate at "
                "registration; outcome: no confirmatory look (documented negative result)"
                if refusal
                else "REGISTERED under option (a) - candidate passed the screen gate"
            ),
            "signed": True,
            "on_blackboard": entry is not None,
            "registration": {
                "approver": "owner (AshJha0)",
                "approver_id": APPROVER,
                "draft_file": DRAFT,
                "draft_sha256": dh,
                "registered_utc": now,
                "path": "iap.agents.broker.WriteBroker.preregister(screen=...)",
                "candidate": f"{ALPHA}/{HORIZON}",
                "screen_refusal": refusal,
                "entry": entry,
                "holdout_read": False,
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
    print(f"{LABEL}: screen gate {'REFUSED: ' + refusal if refusal else 'passed'}")
    print(f"{LABEL}: wrote {OUT} (sha256 {sha(out)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
