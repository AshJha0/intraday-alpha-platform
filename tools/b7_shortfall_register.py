"""Register the v1.17.1 B7 shortfall gate (signal-aware vs plain execution).

Run by the OWNER (human sign-off), never by an agent::

    python tools/b7_shortfall_register.py <repo_root> [--dry-run] [--require-ci-env]

The draft is research/execution/benchmark/prereg_DRAFT.json (the B1 driver's
grid plus the owner's decisions and the B2/B3 signal arm). The tool refuses
to do anything unless the draft is COMPLETE:

- ``status`` still says DRAFT, ``signed`` is false and the output record
  does not exist;
- ``expected_effect`` is the in-sample result (not the placeholder);
- ``signal_arm.alpha_id`` names the registered alpha and
  ``signal_arm.thresholds`` the thresholds in signal units (no field still
  marked OPEN);
- ``simulator.walk_depth_levels`` is 50 (the depth-50 comparison);
- ``fresh_test.sessions`` lists the gate days, each with its Nasdaq and IEX
  dataset names and their ``dataset_version`` manifest hashes (no empty
  value), and ``fresh_test.command`` is the driver line;
- ``fingerprints.configs/prereg/v2.json`` equals the sha256 on disk.

Then it registers the hypothesis through ``WriteBroker.preregister``
(alpha = the signal arm's alpha, horizon ``"shortfall:b7-signal-aware"``,
expected sign -1: signal-aware cheaper), which debits one ledger look, and
writes the signed record research/preregs/b7_shortfall_prereg.json (Ed25519
maintainer signature, key checked against research/agents/trust_roots.json)
carrying the draft's sha256. No market data is read; the fresh days stay
unread until the signed run. The draft file is left unchanged. Never prints
key material.
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

KEYFILE = Path("C:/Users/ashis/.iap_keys/maintainer.key")
APPROVER = "ashjha0"
DRAFT = "research/execution/benchmark/prereg_DRAFT.json"
OUT = "research/preregs/b7_shortfall_prereg.json"
LABEL = "B7-SHORTFALL-GATE"
HORIZON = "shortfall:b7-signal-aware"
WALK_DEPTH = 50
V2 = "configs/prereg/v2.json"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _open(value: object) -> bool:
    return (
        not isinstance(value, str)
        or not value.strip()
        or value.strip().upper().startswith(("OPEN", "TO BE FILLED", "TBD"))
    )


def hypothesis(doc: dict, dh: str) -> str:
    arm = doc["signal_arm"]
    days = ", ".join(s["day"] for s in doc["fresh_test"]["sessions"])
    return (
        f"{LABEL} ({DRAFT} sha256 {dh}). On the fresh days {days}, the signal-aware arms "
        f"({arm['alpha_id']} urgency / pacing / AC drift, thresholds {arm['thresholds']}) are "
        f"cheaper than the plain algo of the same name on the same windows: paired all_in "
        f"difference < 0 by at least {doc['gate_B7']['minimum_effect_bps']} bps, day-clustered, "
        "fill rate not lower by more than the tolerance, Holm over the algos, walk depth "
        f"{WALK_DEPTH}. Approver: owner AshJha0; registration run by the owner."
    )


def checks(root: Path) -> tuple[dict, str]:
    """The draft and its sha256; SystemExit naming the first incomplete field."""
    draft = root / DRAFT
    doc = json.loads(draft.read_text(encoding="utf-8"))
    problems: list[str] = []
    if doc.get("signed") or "DRAFT" not in str(doc.get("status", "")):
        problems.append("draft is already signed or its status is not DRAFT")
    if (root / OUT).exists():
        problems.append(f"{OUT} already exists")
    if _open(doc.get("expected_effect")):
        problems.append("expected_effect is not filled from the in-sample run")
    arm = doc.get("signal_arm") or {}
    if _open(arm.get("alpha_id")):
        problems.append("signal_arm.alpha_id is open (which alpha)")
    if _open(arm.get("thresholds")):
        problems.append("signal_arm.thresholds is open (thresholds in signal units)")
    for k, v in arm.items():
        if isinstance(v, str) and v.strip().upper().startswith("OPEN"):
            problems.append(f"signal_arm.{k} is still marked OPEN")
    if (doc.get("simulator") or {}).get("walk_depth_levels") != WALK_DEPTH:
        problems.append(f"simulator.walk_depth_levels is not {WALK_DEPTH}")
    fresh = doc.get("fresh_test") or {}
    sessions = fresh.get("sessions") or []
    if not sessions:
        problems.append("fresh_test.sessions is empty")
    for s in sessions:
        for k in (
            "day",
            "nasdaq_dataset",
            "nasdaq_dataset_version",
            "iex_dataset",
            "iex_dataset_version",
        ):
            if _open(s.get(k)):
                problems.append(f"fresh_test.sessions[{s.get('day', '?')}].{k} is not filled")
    if _open(fresh.get("command")):
        problems.append("fresh_test.command is not filled")
    fp = (doc.get("fingerprints") or {}).get(V2)
    v2 = root / V2
    if not v2.exists():
        problems.append(f"{V2} is missing")
    elif fp != sha(v2):
        problems.append(f"fingerprints.{V2} {fp!r} != on-disk sha256 {sha(v2)}")
    if problems:
        raise SystemExit(f"{LABEL}: draft incomplete:\n  - " + "\n  - ".join(problems))
    print(
        f"draft complete: {len(sessions)} fresh day(s), walk depth {WALK_DEPTH}, v2 fingerprint ok"
    )
    return doc, sha(draft)


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
    doc, dh = checks(root)
    alpha = doc["signal_arm"]["alpha_id"]
    if a.dry_run:
        print(f"[dry-run] draft sha256 {dh}")
        print(f"[dry-run] would call WriteBroker.preregister({alpha}/{HORIZON}, sign -1)")
        print(f"[dry-run] would write {OUT}; no data read, fresh days untouched")
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
    try:
        pid = broker.preregister(APPROVER, alpha, HORIZON, hypothesis(doc, dh), -1)
    except BrokerError as exc:
        raise SystemExit(f"{LABEL}: broker refused: {exc}") from exc
    e = broker.state()["preregs"][pid]
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    doc.update(
        {
            "status": "REGISTERED - the fresh-day run may happen once, with the command recorded",
            "signed": True,
            "on_blackboard": True,
            "registration": {
                "approver": "owner (AshJha0)",
                "approver_id": APPROVER,
                "draft_file": DRAFT,
                "draft_sha256": dh,
                "registered_utc": now,
                "path": "iap.agents.broker.WriteBroker.preregister",
                "candidate": f"{alpha}/{HORIZON}",
                "entry": {
                    "prereg_id": pid,
                    "blackboard_seq": e["seq"],
                    "ledger_total": e["ledger_total"],
                },
                "fresh_days_read": False,
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
    print(f"{LABEL}: registered {alpha}/{HORIZON} as {pid}")
    print(f"{LABEL}: wrote {OUT} (sha256 {sha(out)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
