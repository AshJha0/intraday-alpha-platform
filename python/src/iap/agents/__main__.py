"""``python -m iap.agents`` — the v1.17 WS-E agents (E2-E4).

::

    python -m iap.agents research --task "..." [--workspace REPO] [--data-root data/real]
        [--env-file PATH] [--model ID] [--max-usd 1] [--max-tool-calls 30] [--effort medium]
        [--runner cli|planted] [--session-id ID]
    python -m iap.agents ops [--paper-out-root data/real/paper] [--out research/ops/alerts]
        [--env-file PATH] [--model ID] [--max-usd 0.05] [--today YYYY-MM-DD]
    python -m iap.agents docs-sync --base origin/main [--head HEAD] [--out research/docs_sync]
        [--env-file PATH] [--model ID] [--max-usd 0.1]

Every command spends money on the owner's key (``--env-file`` or
``ANTHROPIC_API_KEY``; never printed) and records every call in the shared
spend ledger (``python -m iap.llm spend``).  Models default to the dated
pins in ``configs/agents/llm.json`` (research: Sonnet 5.5; ops and docs:
Haiku 5.5).  Nothing here signs, registers, applies or merges: the research
assistant writes DRAFTS, ops writes an alerts document, docs-sync writes a
patch file.  ``--dry-run`` runs the scripted client of :mod:`iap.llm.fake`
(no network, no key, no spend) so the plumbing can be exercised anywhere.
The operator commands of v1.10-v1.14 stay at ``python -m iap.agents.cli``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from iap.llm.budget import PRICES, Budget, pin_for

REPO = Path(__file__).resolve().parents[4]


def _client(a: argparse.Namespace) -> tuple[Any, str | None]:
    if a.dry_run:
        from iap.llm.fake import ScriptedClient, text

        return ScriptedClient([[text("dry run: no model was called")]]), None
    from iap.llm.agent import make_client
    from iap.llm.envfile import ApiKeyError, load_api_key

    try:
        key = load_api_key(a.env_file)
    except ApiKeyError as exc:
        raise SystemExit(f"error: {exc}") from exc
    return make_client(key), key


def _budget(a: argparse.Namespace, role: str) -> Budget:
    from iap.llm.spend import SpendLedger

    return Budget(
        max_usd=a.max_usd,
        max_tool_calls=a.max_tool_calls,
        max_tokens=a.max_tokens,
        ledger=None if a.dry_run else SpendLedger.default(),
        run_id=a.run_id,
        role=role,
    )


def _common(p: argparse.ArgumentParser, role: str, max_usd: float, tool_calls: int) -> None:
    p.add_argument("--env-file", type=Path, default=None)
    p.add_argument("--model", default=pin_for(role).model, choices=sorted(PRICES))
    p.add_argument("--max-usd", type=float, default=max_usd)
    p.add_argument("--max-tool-calls", type=int, default=tool_calls)
    p.add_argument("--max-tokens", type=int, default=300_000)
    p.add_argument("--effort", default=None, choices=("low", "medium", "high", "xhigh"))
    p.add_argument("--run-id", default="", help="recorded in the spend ledger rows")
    p.add_argument("--session-id", default=None)
    p.add_argument("--dry-run", action="store_true", help="scripted client: no key, no spend")
    p.add_argument("--json", action="store_true", help="print the summary as JSON")


def cmd_research(a: argparse.Namespace) -> int:
    from iap.agents import research_assistant as ra
    from iap.llm.runners import PLANTED_DATASET, planted_runner

    client, key = _client(a)
    ws = Path(a.workspace)
    runner: ra.InSampleRunner
    if a.runner == "planted":
        planted = planted_runner()

        def _planted(alpha: str, horizon: str, ds: Path) -> dict[str, Any]:
            return planted(alpha, horizon, PLANTED_DATASET)

        runner = _planted
    else:
        runner = ra.cli_runner(ws)
    summary, agent = ra.run(
        client,
        a.task,
        workspace=ws,
        runner=runner,
        budget=_budget(a, ra.ROLE),
        model=a.model,
        data_root=a.data_root,
        session_id=a.session_id,
        effort=a.effort or "medium",
        secret=key,
    )
    keys = (
        "session",
        "status",
        "budget",
        "runs",
        "prereg_drafts",
        "report_drafts",
        "rejected_drafts",
        "unverified_numbers",
        "finish_summary",
    )
    print(json.dumps({k: summary[k] for k in keys}, indent=2, sort_keys=True, default=str))
    print(f"artefacts: {agent.dir}")
    return 3 if summary["unverified_numbers"] else 0


def cmd_ops(a: argparse.Namespace) -> int:
    from iap.agents import ops_agent
    from iap.llm.spend import SpendLedger

    client, key = _client(a)
    spend = None if a.dry_run else SpendLedger.default().status()
    doc, md = ops_agent.run(
        client,
        out_root=a.paper_out_root,
        out_dir=a.out,
        budget=_budget(a, ops_agent.ROLE),
        model=a.model,
        spend=spend,
        today=a.today,
        session_id=a.session_id,
        effort=a.effort or "low",
        secret=key,
    )
    if a.json:
        print(json.dumps(doc, indent=2, sort_keys=True, default=str))
    else:
        print(md.read_text(encoding="utf-8"))
        print(f"wrote {md}")
    fired = doc["counts"].get("alert", 0) + doc["counts"].get("warn", 0)
    return ops_agent.EXIT_ALERTS if fired else 0


def cmd_docs_sync(a: argparse.Namespace) -> int:
    from iap.agents import docs_sync

    client, key = _client(a)
    summary, patch = docs_sync.run(
        client,
        repo=a.repo,
        base=a.base,
        head=a.head,
        out_dir=a.out,
        budget=_budget(a, docs_sync.ROLE),
        model=a.model,
        session_id=a.session_id,
        effort=a.effort or "low",
        secret=key,
    )
    keys = (
        "session",
        "status",
        "budget",
        "code_files",
        "proposals",
        "refused",
        "patch",
        "applied",
        "finish_summary",
    )
    print(json.dumps({k: summary[k] for k in keys}, indent=2, sort_keys=True, default=str))
    print("patch (NOT applied): " + (str(patch) if patch else "none; no edit proposed"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="iap.agents", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("research", help="research assistant (E2): in-sample runs, drafts")
    r.add_argument("--task", required=True)
    r.add_argument("--workspace", type=Path, default=REPO)
    r.add_argument("--data-root", type=Path, default=None, help="default WORKSPACE/data/real")
    r.add_argument("--runner", choices=("cli", "planted"), default="cli")
    _common(r, "research", 1.0, 30)
    o = sub.add_parser("ops", help="morning ops agent (E3): alerts document")
    o.add_argument("--paper-out-root", type=Path, default=REPO / "data" / "real" / "paper")
    o.add_argument("--out", type=Path, default=REPO / "research" / "ops" / "alerts")
    o.add_argument("--today", default=None)
    _common(o, "ops", 0.05, 4)
    d = sub.add_parser("docs-sync", help="docs-sync agent (E4): a patch file, never applied")
    d.add_argument("--repo", type=Path, default=REPO)
    d.add_argument("--base", required=True)
    d.add_argument("--head", default="HEAD")
    d.add_argument("--out", type=Path, default=REPO / "research" / "docs_sync")
    _common(d, "docs", 0.1, 30)
    a = ap.parse_args(argv)
    return {"research": cmd_research, "ops": cmd_ops, "docs-sync": cmd_docs_sync}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
