"""The write broker (AL01, AL02): the only path to agent-visible shared state.

Every write is validated, attributed to a registered agent id, stamped by the
broker's clock and chained into the blackboard, so the log replays to exactly
the current state (:meth:`WriteBroker.state`).  Agents hold no verdict,
lifecycle or order authority: a finding is a claim with citations, nothing
more.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from iap.agents import citations
from iap.agents.blackboard import Blackboard, BlackboardError, digest

AGENT_ID = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
LEASE_NS = 15 * 60 * 10**9


class BrokerError(ValueError):
    pass


class WriteBroker:
    def __init__(
        self,
        root: Path,
        agents: set[str],
        *,
        clock: Callable[[], int] = time.time_ns,
        lease_ns: int = LEASE_NS,
        board: Path | None = None,
        trusted: set[str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.agents = frozenset(agents)
        #: non-agent writers (the evaluator, the approval service); never an agent id
        self.trusted = frozenset(trusted or ())
        if self.agents & self.trusted:
            raise ValueError("an agent cannot also be a trusted writer")
        self.clock = clock
        self.lease_ns = lease_ns
        self.board = Blackboard(board or self.root / "research" / "agents" / "blackboard.jsonl")

    # -- state ----------------------------------------------------------
    def state(self, now: int | None = None) -> dict[str, Any]:
        """Replay the log: tasks, live leases, findings, pre-registrations."""
        now = self.clock() if now is None else now
        tasks: dict[str, Any] = {}
        leases: dict[str, tuple[str, int]] = {}
        findings: list[dict[str, Any]] = []
        preregs: dict[str, Any] = {}
        for e in self.board.entries():
            b = e["body"]
            if e["kind"] == "task":
                tasks[b["task_id"]] = {**b, "created_by": e["agent"]}
            elif e["kind"] == "claim":
                leases[b["task_id"]] = (e["agent"], e["ts"] + b["lease_ns"])
            elif e["kind"] == "release":
                leases.pop(b["task_id"], None)
            elif e["kind"] == "finding":
                findings.append({**b, "agent": e["agent"], "seq": e["seq"]})
            elif e["kind"] == "prereg":
                preregs[b["prereg_id"]] = {**b, "agent": e["agent"], "seq": e["seq"], "ts": e["ts"]}
        live = {t: {"agent": a, "expires": x} for t, (a, x) in leases.items() if x > now}
        return {"tasks": tasks, "leases": live, "findings": findings, "preregs": preregs}

    # -- writes ---------------------------------------------------------
    def _agent(self, agent: str) -> None:
        if not AGENT_ID.match(str(agent)) or agent not in self.agents:
            raise BrokerError(f"unregistered agent {agent!r}")

    def _write(self, kind: str, agent: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.board.append(kind, agent, self.clock(), body)
        except BlackboardError as exc:
            raise BrokerError(str(exc)) from exc

    def write_trusted(self, writer: str, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        """Entry for the evaluator and approval service only (reserve / approval kinds)."""
        if writer not in self.trusted or kind not in ("reserve", "approval"):
            raise BrokerError(f"{writer!r} may not write {kind!r}")
        return self._write(kind, writer, body)

    def post_task(self, agent: str, title: str, spec: dict[str, Any]) -> str:
        """Content-hashed: the same title+spec is the same task, posted once."""
        self._agent(agent)
        task_id = digest({"title": title, "spec": spec})[:16]
        if task_id in self.state()["tasks"]:
            raise BrokerError(f"task {task_id} already posted")
        self._write("task", agent, {"task_id": task_id, "title": title, "spec": spec})
        return task_id

    def claim(self, agent: str, task_id: str) -> None:
        self._agent(agent)
        st = self.state()
        if task_id not in st["tasks"]:
            raise BrokerError(f"unknown task {task_id}")
        held = st["leases"].get(task_id)
        if held and held["agent"] != agent:
            raise BrokerError(f"task {task_id} leased to {held['agent']}")
        self._write("claim", agent, {"task_id": task_id, "lease_ns": self.lease_ns})

    def release(self, agent: str, task_id: str) -> None:
        self._agent(agent)
        held = self.state()["leases"].get(task_id)
        if not held or held["agent"] != agent:
            raise BrokerError(f"{agent} holds no lease on {task_id}")
        self._write("release", agent, {"task_id": task_id})

    def file_finding(
        self, agent: str, task_id: str, title: str, text: str, refs: list[str]
    ) -> None:
        """A finding needs a live lease and at least one citation, all resolvable."""
        self._agent(agent)
        held = self.state()["leases"].get(task_id)
        if not held or held["agent"] != agent:
            raise BrokerError(f"{agent} holds no live lease on {task_id}")
        if not refs:
            raise BrokerError("a finding must cite at least one artefact")
        bad = citations.unresolved(refs, self.root)
        if bad:
            raise BrokerError(f"unresolvable citations: {bad}")
        self._write(
            "finding",
            agent,
            {"task_id": task_id, "title": title, "text": text, "refs": sorted(refs)},
        )

    def preregister(
        self, agent: str, alpha_id: str, horizon: str, hypothesis: str, expected_sign: int
    ) -> str:
        """Commit a hypothesis before any run.  One per (alpha, horizon)."""
        self._agent(agent)
        if expected_sign not in (-1, 1):
            raise BrokerError("expected_sign must be -1 or 1")
        if not hypothesis.strip():
            raise BrokerError("empty hypothesis")
        pid = digest({"alpha": alpha_id, "horizon": horizon})[:16]
        if pid in self.state()["preregs"]:
            raise BrokerError(f"{alpha_id}/{horizon} already pre-registered")
        self._write(
            "prereg",
            agent,
            {
                "prereg_id": pid,
                "alpha_id": alpha_id,
                "horizon": horizon,
                "hypothesis": hypothesis,
                "expected_sign": expected_sign,
            },
        )
        return pid

    def is_preregistered(self, alpha_id: str, horizon: str) -> bool:
        return digest({"alpha": alpha_id, "horizon": horizon})[:16] in self.state()["preregs"]
