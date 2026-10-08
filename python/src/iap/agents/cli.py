"""``python -m iap.agents.cli`` — operator commands for approvals and the reserve.

::

    keygen  --approver ID --keyfile PATH          # adds a secret to a key file OUTSIDE the repo
    issue   --approver ID --keyfile PATH --alpha EQ01 --action retire|reset
            --reason "..." [--ttl-min 30] [--out FILE]
    apply   --approval FILE --keyfile PATH [--root .]
    reserve --agent ID --alpha EQ01 --horizon 1s --expected-sign 1
            --secret-file PATH [--root .] [--sessions 2]

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
    a = ap.parse_args(argv)
    return {"keygen": cmd_keygen, "issue": cmd_issue, "apply": cmd_apply, "reserve": cmd_reserve}[
        a.cmd
    ](a)


if __name__ == "__main__":
    raise SystemExit(main())
