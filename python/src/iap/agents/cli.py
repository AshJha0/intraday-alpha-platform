"""``python -m iap.agents.cli`` — operator commands for approvals and the reserve.

::

    keygen  --approver ID --keyfile PATH          # adds a secret to a key file OUTSIDE the repo
    issue   --approver ID --keyfile PATH --alpha EQ01 --action retire|reset
            --reason "..." [--ttl-min 30] [--out FILE]
    apply   --approval FILE --keyfile PATH [--root .]
    reserve --agent ID --alpha EQ01 --horizon 1s --expected-sign 1
            --secret-file PATH [--root .] [--sessions 2]
    agent-keygen --agent ID --keyfile PATH                     # G4 signing key
    prereg  --agent ID --alpha EQ01 --horizon 1s --hypothesis "..." --expected-sign 1
            [--keyfile PATH] [--ledger FILE] [--dataset-version V] [--supersede]
    anchor                                                     # G3: write anchors.json
    verify-board [--keyfile PATH]                              # G3/G4 verification

``issue`` is run by the person approving; ``apply`` verifies the signature,
burns the nonce on the blackboard and performs the HUMAN lifecycle edge
(``research/alpha_registry.json`` and the transition log, as
``python -m iap.lifecycle retire|reset`` does).  ``reserve`` is run by the
evaluator operator: it prints only pass/fail and attempts left.  Key and
secret files must live outside the repository; the commands refuse a path
inside ``--root``.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

from iap.agents import approvals
from iap.agents.broker import WriteBroker
from iap.agents.reserve import EVALUATOR, ReserveEvaluator

NS_MIN = 60 * 10**9


def _outside(path: Path, root: Path) -> Path:
    path, root = path.resolve(), root.resolve()
    if path == root or root in path.parents:
        raise SystemExit(f"{path}: secrets must live outside the repository ({root})")
    return path


def _keys(path: Path) -> dict[str, str]:
    return json.loads(path.read_text(encoding="ascii")) if path.is_file() else {}


def _broker(root: Path, agents: set[str] | None = None) -> WriteBroker:
    return WriteBroker(root, agents or set(), trusted={approvals.APPROVER, EVALUATOR})


def cmd_keygen(a: argparse.Namespace) -> int:
    path = _outside(a.keyfile, a.root)
    keys = _keys(path)
    if a.approver in keys:
        raise SystemExit(f"approver {a.approver!r} already has a key")
    keys[a.approver] = approvals.new_secret()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys, sort_keys=True), encoding="ascii")
    print(f"key for {a.approver!r} written to {path}")
    return 0


def cmd_issue(a: argparse.Namespace) -> int:
    keys = _keys(_outside(a.keyfile, a.root))
    if a.approver not in keys:
        raise SystemExit(f"no key for approver {a.approver!r}")
    doc = approvals.issue(
        a.approver,
        keys[a.approver],
        a.alpha,
        a.action,
        a.reason,
        time.time_ns(),
        a.ttl_min * NS_MIN,
    )
    text = json.dumps(doc, sort_keys=True)
    if a.out:
        Path(a.out).write_text(text, encoding="ascii")
    else:
        print(text)
    return 0


def cmd_apply(a: argparse.Namespace) -> int:
    from iap.lifecycle.__main__ import _load
    from iap.lifecycle.bootstrap import REGISTRY_RELPATH

    keys = _keys(_outside(a.keyfile, a.root))
    approval = json.loads(Path(a.approval).read_text(encoding="ascii"))
    _, registry, machine = _load(a.root)
    try:
        tr = approvals.apply(approval, keys, _broker(a.root), machine, time.time_ns())
    except (approvals.ApprovalError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    registry.save(a.root / REGISTRY_RELPATH)
    print(
        f"{approval['action']} {approval['alpha_id']}: {tr.from_state.name} -> {tr.to_state.name}"
    )
    return 0


def cmd_reserve(a: argparse.Namespace) -> int:
    from iap.agents.reserve_runner import make_runner

    secret = _outside(a.secret_file, a.root)
    if not secret.is_file():
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text(secrets.token_hex(32), encoding="ascii")
    broker = _broker(a.root, {a.agent})
    runner = make_runner(
        a.root / "configs", a.root / "configs" / "marketdata" / "generator.json", a.sessions
    )
    ev = ReserveEvaluator(secret.read_text(encoding="ascii").encode("ascii"), broker, runner)
    cand = {"alpha_id": a.alpha, "horizon": a.horizon, "expected_sign": a.expected_sign}
    print(json.dumps(ev.evaluate(a.agent, cand), sort_keys=True))
    return 0


def cmd_agent_keygen(a: argparse.Namespace) -> int:
    """A per-agent signing key (G4) in a key file outside the repository."""
    path = _outside(a.keyfile, a.root)
    keys = _keys(path)
    if a.agent in keys:
        raise SystemExit(f"agent {a.agent!r} already has a key")
    keys[a.agent] = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys, sort_keys=True), encoding="ascii")
    print(f"key for agent {a.agent!r} written to {path}")
    return 0


def cmd_prereg(a: argparse.Namespace) -> int:
    """Pre-register a hypothesis (debits one look, records the code fingerprint)."""
    from iap.agents.broker import BrokerError, sign

    keys = None
    auth = None
    args = {
        "alpha_id": a.alpha,
        "horizon": a.horizon,
        "hypothesis": a.hypothesis,
        "expected_sign": a.expected_sign,
    }
    if a.keyfile is not None:
        raw = _keys(_outside(a.keyfile, a.root))
        if a.agent not in raw:
            raise SystemExit(f"no key for agent {a.agent!r}")
        keys = {a.agent: bytes.fromhex(raw[a.agent])}
        auth = sign(keys[a.agent], a.agent, "preregister", args)
    broker = WriteBroker(a.root, {a.agent}, keys=keys)
    try:
        pid = broker.preregister(
            a.agent,
            a.alpha,
            a.horizon,
            a.hypothesis,
            a.expected_sign,
            auth=auth,
            ledger=a.ledger,
            dataset_version=a.dataset_version,
            supersede=a.supersede,
        )
    except BrokerError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"pre-registered {a.alpha}/{a.horizon} as {pid} (one look debited)")
    return 0


def cmd_anchor(a: argparse.Namespace) -> int:
    """Write research/agents/anchors.json: the first commit holding each entry (G3)."""
    from iap.agents import anchor, prereg_gate

    doc = anchor.anchors(a.root, prereg_gate.board_path(a.root))
    out = a.root / anchor.ANCHORS_RELPATH
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="ascii")
    print(f"{len(doc['entries'])} entries anchored in {out}")
    return 0


def cmd_verify_board(a: argparse.Namespace) -> int:
    """Chain + git-history (re-chaining) + anchors + signatures (G3, G4)."""
    from iap.agents import anchor, prereg_gate
    from iap.agents.blackboard import Blackboard

    board = prereg_gate.board_path(a.root)
    report = anchor.verify(a.root, board, a.root / anchor.ANCHORS_RELPATH)
    if a.keyfile is not None:
        raw = _keys(_outside(a.keyfile, a.root))
        sig = Blackboard(board).verify_signatures({k: bytes.fromhex(v) for k, v in raw.items()})
        report["problems"] += sig
        report["ok"] = report["ok"] and not sig
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="iap.agents.cli")
    ap.add_argument("--root", type=Path, default=Path.cwd())
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--approver", required=True)
    k.add_argument("--keyfile", type=Path, required=True)
    i = sub.add_parser("issue")
    i.add_argument("--approver", required=True)
    i.add_argument("--keyfile", type=Path, required=True)
    i.add_argument("--alpha", required=True)
    i.add_argument("--action", choices=approvals.ACTIONS, required=True)
    i.add_argument("--reason", required=True)
    i.add_argument("--ttl-min", type=int, default=30)
    i.add_argument("--out")
    p = sub.add_parser("apply")
    p.add_argument("--approval", required=True)
    p.add_argument("--keyfile", type=Path, required=True)
    r = sub.add_parser("reserve")
    r.add_argument("--agent", required=True)
    r.add_argument("--alpha", required=True)
    r.add_argument("--horizon", required=True)
    r.add_argument("--expected-sign", type=int, choices=(-1, 1), required=True)
    r.add_argument("--secret-file", type=Path, required=True)
    r.add_argument("--sessions", type=int, default=2)
    ak = sub.add_parser("agent-keygen")
    ak.add_argument("--agent", required=True)
    ak.add_argument("--keyfile", type=Path, required=True)
    pr = sub.add_parser("prereg")
    pr.add_argument("--agent", required=True)
    pr.add_argument("--alpha", required=True)
    pr.add_argument("--horizon", required=True)
    pr.add_argument("--hypothesis", required=True)
    pr.add_argument("--expected-sign", type=int, choices=(-1, 1), required=True)
    pr.add_argument("--keyfile", type=Path, default=None)
    pr.add_argument("--ledger", type=Path, default=None)
    pr.add_argument("--dataset-version", default=None)
    pr.add_argument("--supersede", action="store_true")
    sub.add_parser("anchor")
    vb = sub.add_parser("verify-board")
    vb.add_argument("--keyfile", type=Path, default=None)
    a = ap.parse_args(argv)
    return {
        "keygen": cmd_keygen,
        "issue": cmd_issue,
        "apply": cmd_apply,
        "reserve": cmd_reserve,
        "agent-keygen": cmd_agent_keygen,
        "prereg": cmd_prereg,
        "anchor": cmd_anchor,
        "verify-board": cmd_verify_board,
    }[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
