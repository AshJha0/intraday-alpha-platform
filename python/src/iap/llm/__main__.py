"""``python -m iap.llm`` — run one LLM research session (AI1).

::

    python -m iap.llm --workspace WS --agent llm-researcher --keyfile KEYS/agent.key \\
        --env-file C:/Work/Claude/AgenticTrader/.env --task "Test whether ..." \\
        [--model claude-opus-5-5] [--effort medium] [--max-usd 2] [--max-tool-calls 40]
        [--max-tokens 400000] [--max-preregs 3] [--runner planted|power]
        [--dataset synthetic:planted-v1 ...]

``WS`` holds the blackboard, the ledger, the public-key registry and the
session artefacts (``research/agents/llm_sessions/<id>/``).  Use a scratch
workspace unless the pre-registrations are meant for the committed board.
The key file holds the agent's Ed25519 private key and must live outside both
the workspace and the repository (``python -m iap.agents.cli --root WS
agent-keygen --agent ID --keyfile PATH``).  The API key comes from
``--env-file`` or ``ANTHROPIC_API_KEY`` and is never printed or persisted.
This command spends money: the session stops at ``--max-usd`` (estimated
from usage).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from iap.llm.budget import DEFAULT_MODEL, PRICES, Budget
from iap.llm.runners import PLANTED_DATASET

REPO = Path(__file__).resolve().parents[4]


def _outside(path: Path, *roots: Path) -> Path:
    path = path.resolve()
    for root in roots:
        root = root.resolve()
        if path == root or root in path.parents:
            raise SystemExit(f"{path}: the agent key must live outside {root}")
    return path


def main(argv: list[str] | None = None) -> int:
    from iap.llm import agent as ag
    from iap.llm import runners
    from iap.llm.envfile import ApiKeyError, load_api_key

    ap = argparse.ArgumentParser(prog="iap.llm", description=__doc__.split("\n")[0])
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("--agent", required=True)
    ap.add_argument("--keyfile", type=Path, required=True)
    ap.add_argument("--env-file", type=Path, default=None)
    ap.add_argument("--task", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(PRICES))
    ap.add_argument("--effort", default="medium", choices=("low", "medium", "high", "xhigh"))
    ap.add_argument("--max-usd", type=float, default=2.0)
    ap.add_argument("--max-tool-calls", type=int, default=40)
    ap.add_argument("--max-tokens", type=int, default=400_000)
    ap.add_argument("--max-preregs", type=int, default=3)
    ap.add_argument("--runner", choices=("planted", "power"), default="planted")
    ap.add_argument("--dataset", action="append", default=None)
    a = ap.parse_args(argv)

    keyfile = _outside(a.keyfile, a.workspace, REPO)
    keys = json.loads(keyfile.read_text(encoding="ascii"))
    if a.agent not in keys:
        raise SystemExit(f"no key for agent {a.agent!r} in {keyfile}")
    if a.runner == "planted":
        runner = runners.planted_runner()
        datasets = frozenset(a.dataset or [PLANTED_DATASET])
    else:
        runner = runners.power_runner(
            REPO / "configs", REPO / "configs" / "marketdata" / "generator.json"
        )
        datasets = frozenset(a.dataset or ["synthetic:seed=7"])
    try:
        api_key = load_api_key(a.env_file)
    except ApiKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    budget = Budget(
        max_tool_calls=a.max_tool_calls,
        max_tokens=a.max_tokens,
        max_usd=a.max_usd,
        max_preregs=a.max_preregs,
    )
    session = ag.open_session(
        a.workspace, a.agent, keys[a.agent], runner, budget, datasets=datasets, repo=REPO
    )
    out = ag.run_session(
        ag.make_client(api_key), session, a.task, model=a.model, effort=a.effort, secret=api_key
    )
    print(
        json.dumps(
            {k: out[k] for k in ("session", "status", "budget", "findings", "rejected_findings")},
            indent=2,
            sort_keys=True,
        )
    )
    print(f"artefacts: {session.dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
