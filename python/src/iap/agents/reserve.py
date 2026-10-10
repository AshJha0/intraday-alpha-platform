"""Reserve sessions on a hidden seed (AL03).

The evaluator holds a secret.  For each candidate and attempt it derives a
session seed as HMAC(secret, candidate hash | attempt), runs the candidate on
the session that seed generates, and returns ONLY pass/fail and the attempts
left: never the seed, the statistic or the session.  Attempts per candidate
are capped per (alpha, horizon, code hash) - since v1.10 (G2), not per
candidate dict, so a dummy field cannot reset the cap - and every one is logged on the blackboard, so the reserve cannot
be mined by trial and error.  The ``runner`` (seed, candidate) -> bool is
injected; wiring it to the synthetic generator and the research runner is
the evaluator operator's step (the secret never enters the repository or an
agent process).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping
from typing import Any

from iap.agents.blackboard import digest
from iap.agents.broker import WriteBroker

EVALUATOR = "evaluator"
MAX_ATTEMPTS = 3


class ReserveError(ValueError):
    pass


class ReserveEvaluator:
    def __init__(
        self,
        secret: bytes,
        broker: WriteBroker,
        runner: Callable[[int, Mapping[str, Any]], bool],
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        if len(secret) < 16:
            raise ReserveError("reserve secret too short")
        self._secret = secret
        self._runner = runner
        self.broker = broker
        self.max_attempts = max_attempts

    def _seed(self, candidate_id: str, attempt: int) -> int:
        mac = hmac.new(self._secret, f"{candidate_id}|{attempt}".encode("ascii"), hashlib.sha256)
        return int.from_bytes(mac.digest()[:8], "big")

    def attempts(self, candidate_id: str) -> int:
        return sum(
            1
            for e in self.broker.board.entries()
            if e["kind"] == "reserve" and e["body"]["candidate_id"] == candidate_id
        )

    def candidate_id(self, alpha_id: str, horizon: str) -> str:
        """The attempt-cap key (G2): (alpha, horizon, current code hash).

        Only those three, so extra or reordered fields in the candidate do
        not open a fresh set of attempts; changing the alpha's code does,
        but then the code no longer matches its pre-registration (which
        :meth:`evaluate` refuses) until it is re-registered - a new look."""
        code = self.broker.fingerprinter(alpha_id)
        code_hash = code.get("code_hash") if code else None
        return digest({"alpha_id": alpha_id, "horizon": horizon, "code_hash": code_hash})[:16]

    def evaluate(
        self, agent: str, candidate: Mapping[str, Any], *, auth: Mapping | None = None
    ) -> dict[str, Any]:
        """Pass/fail for one reserve attempt; the candidate must be pre-registered."""
        if agent not in self.broker.agents:
            raise ReserveError(f"unregistered agent {agent!r}")
        unknown = sorted(set(candidate) - {"alpha_id", "horizon", "expected_sign"})
        if unknown:
            raise ReserveError(f"unknown candidate fields {unknown}")
        candidate = {k: candidate.get(k) for k in ("alpha_id", "horizon", "expected_sign")}
        try:
            request = self.broker.authenticate(agent, "reserve", candidate, auth)
        except ValueError as exc:
            raise ReserveError(str(exc)) from exc
        alpha, horizon = candidate.get("alpha_id"), candidate.get("horizon")
        if not self.broker.is_preregistered(str(alpha), str(horizon)):
            raise ReserveError("candidate is not pre-registered")
        sign = candidate.get("expected_sign")
        reg = [
            p
            for p in self.broker.state()["preregs"].values()
            if p["alpha_id"] == alpha and p["horizon"] == horizon
        ]
        if not reg or reg[0]["expected_sign"] != sign:
            raise ReserveError("candidate expected_sign does not match its pre-registration")
        registered = reg[0].get("code")
        current = self.broker.fingerprinter(str(alpha))
        if registered is not None and registered != current:
            raise ReserveError("alpha code changed since its pre-registration")
        cid = self.candidate_id(str(alpha), str(horizon))
        n = self.attempts(cid)
        if n >= self.max_attempts:
            raise ReserveError("reserve attempts exhausted for this candidate")
        passed = bool(self._runner(self._seed(cid, n), candidate))
        body = {"candidate_id": cid, "requested_by": agent, "attempt": n + 1, "passed": passed}
        if request is not None:
            body["request_auth"] = request
        self.broker.write_trusted(EVALUATOR, "reserve", body)
        return {"candidate_id": cid, "passed": passed, "attempts_left": self.max_attempts - n - 1}
