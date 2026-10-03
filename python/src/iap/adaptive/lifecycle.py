"""Alpha lifecycle state machine (spec §20 step 13: continuous monitoring
and retirement criteria).

States and pinned transition rules (evaluated once per adaptive block, on
the rolling OOS IC of the *deployed* scores over matured rows only):

- **ACTIVE**  — allocated.  ``rolling_ic < watch_ic_gate`` -> WATCH.
- **WATCH**   — allocated, on probation.
  - persistent breach -> RETIRED, by the retirement rule below (the
    default CUSUM, or the legacy count of ``retire_breach_evals``
    CONSECUTIVE breaches, the entering one included).
  - ``rolling_ic >= reactivate_ic_gate`` for ``reactivate_evals``
    consecutive evaluations -> ACTIVE (re-activation rule).
  - ``watch_ic_gate <= rolling_ic < reactivate_ic_gate``: neutral zone —
    BOTH counters reset (hysteresis; neither breach nor recovery).
- **RETIRED** — allocation halted (the adaptive backtester forces flat).
  ``rolling_ic >= reactivate_ic_gate`` for ``reactivate_evals``
  consecutive evaluations -> WATCH (a retired alpha must re-earn ACTIVE
  through probation, never jump straight back).
- ``rolling_ic is None`` (too little matured data): no transition, all
  counters unchanged — silence is not evidence.
- ``informative=False`` — the evaluation re-read an UNCHANGED matured set
  (no new matured rows since the last counted evaluation): treated exactly
  like ``rolling_ic is None``.  Re-reading the same 2-hour IC window six
  times after a feed goes quiet is ONE reading, not six consecutive
  breaches; without this rule an alpha was retired on one bad window plus
  silence, and stayed retired through the next session's open.

**Retirement rule** (``breach_rule``; the default changed in v1.5.0).

*``"cusum"`` — the default.*  The consecutive-count rule treats
``retire_breach_evals`` breaches in a row as that many independent pieces
of evidence.  They are not: each evaluation reads a rolling window (2 h)
that advances by one block (15 min), so successive readings share most of
their rows, and N consecutive breaches can be ONE bad stretch seen N times.
The CUSUM rule accumulates evidence in proportion to the NEW information of
each reading instead:

    s = S + new_fraction * (watch_ic_gate - rolling_ic - cusum_k)
    S <- s if s > 0 else 0

- ``new_fraction`` in (0, 1] is the share of the evaluation's window that is
  new since the last counted evaluation (the caller passes it; 1.0 = a
  disjoint window, ``block / window`` for the rolling replay);
- ``cusum_k`` (>= 0) is the slack: a reading has to be more than ``k`` below
  the watch gate to add evidence, and anything above ``gate - k`` drains it;
- ``S`` accumulates in ACTIVE and WATCH alike (a slow bleed is evidence
  before the first reading under the gate).  In WATCH, a reading that is
  itself a BREACH and leaves ``S >= cusum_h`` retires the alpha.  A
  retirement therefore always happens on a reading under the watch gate —
  the ``rolling_ic`` gate result recorded with it is a genuine failure —
  and never on the reading that entered WATCH: probation lasts at least one
  further reading.  (The v1.3.0 opt-in form retired on ANY WATCH reading
  with ``S >= cusum_h``, including one above the gate; that was tightened
  when the rule became the default and was ported.)
- Entering WATCH is the first reading under the gate, as before; ``S`` is
  kept across that transition and reset by every other one.  Recovery and
  re-activation are unchanged: ``reactivate_evals`` consecutive readings
  ``>= reactivate_ic_gate``; a reading that is not a recovery resets the
  recovery count.  ``breach_count`` is not used and stays 0.
- uninformative / missing readings move nothing, as before.

Pinned parameters (``adaptive.lifecycle`` in
``configs/strategies/strategies.json``): ``cusum_k = 0.0025`` — half the
re-activation gate, the usual half-the-shift-to-detect slack — and
``cusum_h = 0.01`` — the PROMOTE IC gate: an alpha is retired when its
shortfall below the watch gate, beyond the slack and weighted by new
information, adds up to the IC it needed to be promoted.  Both were fixed
before any result was computed with them; they are conventions, not
calibrations to this dataset.

The three implementations (this module, Java ``LifecycleGauge``, Rust
``lifecycle::tracker``) compute ``S`` with exactly the two expressions
above — one multiplication, left-to-right subtraction, an explicit
comparison instead of ``max`` — so the statistic is bit-identical across
languages.

*``"consecutive"`` — the legacy rule*, the default up to v1.4.0: in WATCH,
``retire_breach_evals`` consecutive breaches (the entering one counts)
retire the alpha, and the neutral zone
``watch_ic_gate <= rolling_ic < reactivate_ic_gate`` resets both counters.
It stays selectable by name in all three implementations.

Gates (``watch_ic_gate``, ``reactivate_ic_gate``,
``retire_breach_evals``, ``reactivate_evals``) and the rule are pinned in
``configs/strategies/strategies.json`` ``adaptive.lifecycle`` with
``reactivate_ic_gate >= watch_ic_gate`` enforced; the block must NAME its
``breach_rule`` (a block written for v1.4.0 is rejected, not read under the
other rule).  Comparisons are strict ``<`` for breach and inclusive ``>=``
for recovery (pinned).

Every transition is appended to ``research/lifecycle_log.jsonl`` — one
sorted-key JSON object per line:

```
{"alpha_id": str, "policy": str, "event_ts": i64, "from": str, "to": str,
 "reason": str, "rolling_ic": float|null, "eval_index": int}
```

Deterministic and wall-clock-free: ``event_ts`` is the block boundary in
event time; identical rerun => identical log bytes (the report runner
truncates the log at the start of each run).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ACTIVE = "ACTIVE"
WATCH = "WATCH"
RETIRED = "RETIRED"
STATES = (ACTIVE, WATCH, RETIRED)
#: Retirement rules (module docs): the default, then the legacy rule.
BREACH_RULES = ("cusum", "consecutive")
DEFAULT_BREACH_RULE = "cusum"
LEGACY_BREACH_RULE = "consecutive"
#: Pinned CUSUM parameters (module docs).
DEFAULT_CUSUM_K = 0.0025
DEFAULT_CUSUM_H = 0.01


@dataclass(frozen=True)
class LifecycleConfig:
    watch_ic_gate: float
    reactivate_ic_gate: float
    retire_breach_evals: int
    reactivate_evals: int
    #: "cusum" (default) or "consecutive" (legacy) — module docs
    breach_rule: str = DEFAULT_BREACH_RULE
    #: CUSUM slack below the watch gate (>= 0; "cusum" only)
    cusum_k: float = DEFAULT_CUSUM_K
    #: CUSUM decision threshold (> 0; "cusum" only)
    cusum_h: float = DEFAULT_CUSUM_H

    def __post_init__(self) -> None:
        if self.retire_breach_evals < 1 or self.reactivate_evals < 1:
            raise ValueError("lifecycle eval counts must be >= 1")
        if self.reactivate_ic_gate < self.watch_ic_gate:
            raise ValueError("reactivate_ic_gate must be >= watch_ic_gate")
        if self.breach_rule not in BREACH_RULES:
            raise ValueError(f"unknown breach_rule {self.breach_rule!r}; known: {BREACH_RULES}")
        if self.cusum_k < 0.0:
            raise ValueError("cusum_k must be >= 0")
        if self.breach_rule == "cusum" and not self.cusum_h > 0.0:
            raise ValueError("breach_rule 'cusum' needs cusum_h > 0")

    @staticmethod
    def from_config(block: dict) -> LifecycleConfig:
        """From an ``adaptive.lifecycle`` block.  The block must name its
        ``breach_rule``; ``"cusum"`` must also give ``cusum_k`` and
        ``cusum_h`` (a ``"consecutive"`` block may omit them)."""
        if "breach_rule" not in block:
            raise ValueError(
                "adaptive.lifecycle names no 'breach_rule'. Since v1.5.0 the default is "
                f"{DEFAULT_BREACH_RULE!r} (with 'cusum_k' and 'cusum_h'); a block written "
                f"for v1.4.0 must say {LEGACY_BREACH_RULE!r} to keep the rule it was "
                "written for"
            )
        rule = str(block["breach_rule"])
        if rule == "cusum":
            for key in ("cusum_k", "cusum_h"):
                if key not in block:
                    raise ValueError(f"adaptive.lifecycle: breach_rule 'cusum' needs {key!r}")
        return LifecycleConfig(
            watch_ic_gate=float(block["watch_ic_gate"]),
            reactivate_ic_gate=float(block["reactivate_ic_gate"]),
            retire_breach_evals=int(block["retire_breach_evals"]),
            reactivate_evals=int(block["reactivate_evals"]),
            breach_rule=rule,
            cusum_k=float(block.get("cusum_k", DEFAULT_CUSUM_K)),
            cusum_h=float(block.get("cusum_h", DEFAULT_CUSUM_H)),
        )

    @staticmethod
    def legacy(
        watch_ic_gate: float,
        reactivate_ic_gate: float,
        retire_breach_evals: int,
        reactivate_evals: int,
    ) -> LifecycleConfig:
        """The gates under the LEGACY consecutive-breach rule, named."""
        return LifecycleConfig(
            watch_ic_gate,
            reactivate_ic_gate,
            retire_breach_evals,
            reactivate_evals,
            breach_rule=LEGACY_BREACH_RULE,
        )

    def to_dict(self) -> dict:
        """The block :meth:`from_config` reads (every key, in pinned order)."""
        return {
            "watch_ic_gate": self.watch_ic_gate,
            "reactivate_ic_gate": self.reactivate_ic_gate,
            "retire_breach_evals": self.retire_breach_evals,
            "reactivate_evals": self.reactivate_evals,
            "breach_rule": self.breach_rule,
            "cusum_k": self.cusum_k,
            "cusum_h": self.cusum_h,
        }


@dataclass(frozen=True)
class Transition:
    alpha_id: str
    policy: str
    event_ts: int
    from_state: str
    to_state: str
    reason: str
    rolling_ic: float | None
    eval_index: int

    def to_dict(self) -> dict:
        return {
            "alpha_id": self.alpha_id,
            "policy": self.policy,
            "event_ts": self.event_ts,
            "from": self.from_state,
            "to": self.to_state,
            "reason": self.reason,
            "rolling_ic": self.rolling_ic,
            "eval_index": self.eval_index,
        }


class LifecycleLog:
    """Append-only jsonl transition log (research/lifecycle_log.jsonl)."""

    def __init__(self, path, truncate: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if truncate or not self.path.exists():
            self.path.write_text("")

    def append(self, transition: Transition) -> None:
        with open(self.path, "a") as f:
            f.write(json.dumps(transition.to_dict(), sort_keys=True) + "\n")

    def read_all(self) -> list[dict]:
        rows = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows


@dataclass
class LifecycleTracker:
    """Per-alpha lifecycle state machine (rules pinned in module docstring)."""

    alpha_id: str
    config: LifecycleConfig
    policy: str = ""
    log: LifecycleLog | None = None
    state: str = ACTIVE
    breach_count: int = 0
    recovery_count: int = 0
    eval_index: int = 0
    transitions: list[Transition] = field(default_factory=list)
    #: CUSUM statistic ``S`` (stays 0.0 under the "consecutive" rule)
    cusum: float = 0.0

    def _transition(self, to_state: str, ts: int, reason: str, rolling_ic: float | None) -> None:
        tr = Transition(
            alpha_id=self.alpha_id,
            policy=self.policy,
            event_ts=int(ts),
            from_state=self.state,
            to_state=to_state,
            reason=reason,
            rolling_ic=None if rolling_ic is None else float(rolling_ic),
            eval_index=self.eval_index,
        )
        self.transitions.append(tr)
        if self.log is not None:
            self.log.append(tr)
        self.state = to_state
        self.breach_count = 0
        self.recovery_count = 0
        if to_state != WATCH or tr.from_state == RETIRED:
            self.cusum = 0.0  # a verdict was reached; evidence starts over

    def update(
        self,
        ts: int,
        rolling_ic: float | None,
        informative: bool = True,
        new_fraction: float = 1.0,
    ) -> str:
        """One evaluation at event time ts; returns the (possibly new) state.

        ``informative`` is False when the evaluation's matured set gained no
        new rows since the last counted evaluation (pinned, API_ADAPTIVE §6):
        the reading carries no new evidence and moves nothing.
        ``new_fraction`` is the share of this reading's window that is new
        since the last counted one; the CUSUM rule weights the reading by
        it, the consecutive rule does not read it.
        """
        self.eval_index += 1
        cfg = self.config
        if rolling_ic is None or not informative:
            return self.state  # no evidence, no movement (pinned)
        breach = rolling_ic < cfg.watch_ic_gate
        recover = rolling_ic >= cfg.reactivate_ic_gate
        if cfg.breach_rule == "cusum":
            return self._update_cusum(ts, rolling_ic, breach, recover, new_fraction)

        if self.state == ACTIVE:
            if breach:
                self._transition(
                    WATCH,
                    ts,
                    f"rolling_ic {rolling_ic:.6f} < watch gate {cfg.watch_ic_gate}",
                    rolling_ic,
                )
                self.breach_count = 1  # the entering breach counts (pinned)
            return self.state

        if self.state == WATCH:
            if breach:
                self.breach_count += 1
                self.recovery_count = 0
                if self.breach_count >= cfg.retire_breach_evals:
                    self._transition(
                        RETIRED,
                        ts,
                        f"persistent breach: {cfg.retire_breach_evals} consecutive "
                        f"evals below watch gate {cfg.watch_ic_gate}",
                        rolling_ic,
                    )
            elif recover:
                self.recovery_count += 1
                self.breach_count = 0
                if self.recovery_count >= cfg.reactivate_evals:
                    self._transition(
                        ACTIVE,
                        ts,
                        f"re-activation: {cfg.reactivate_evals} consecutive evals "
                        f">= reactivate gate {cfg.reactivate_ic_gate}",
                        rolling_ic,
                    )
            else:
                self.breach_count = 0
                self.recovery_count = 0
            return self.state

        # RETIRED
        if recover:
            self.recovery_count += 1
            if self.recovery_count >= cfg.reactivate_evals:
                self._transition(
                    WATCH,
                    ts,
                    f"recovery from retirement: {cfg.reactivate_evals} consecutive "
                    f"evals >= reactivate gate {cfg.reactivate_ic_gate}; "
                    "probation before ACTIVE",
                    rolling_ic,
                )
        else:
            self.recovery_count = 0
        return self.state

    def _update_cusum(
        self, ts: int, rolling_ic: float, breach: bool, recover: bool, new_fraction: float
    ) -> str:
        """The CUSUM retirement rule (module docs)."""
        cfg = self.config
        if not 0.0 < new_fraction <= 1.0:
            raise ValueError("new_fraction must be in (0, 1]")
        if self.state != RETIRED:
            # Exactly these two expressions in every port (module docs).
            s = self.cusum + new_fraction * (cfg.watch_ic_gate - rolling_ic - cfg.cusum_k)
            self.cusum = s if s > 0.0 else 0.0

        if self.state == ACTIVE:
            if breach:
                self._transition(
                    WATCH,
                    ts,
                    f"rolling_ic {rolling_ic:.6f} < watch gate {cfg.watch_ic_gate}",
                    rolling_ic,
                )
            return self.state

        if self.state == WATCH:
            if breach and self.cusum >= cfg.cusum_h:
                stat = self.cusum
                self._transition(
                    RETIRED,
                    ts,
                    f"persistent breach: CUSUM {stat:.6f} >= {cfg.cusum_h} "
                    f"(slack {cfg.cusum_k}) below watch gate {cfg.watch_ic_gate}",
                    rolling_ic,
                )
            elif recover:
                self.recovery_count += 1
                if self.recovery_count >= cfg.reactivate_evals:
                    self._transition(
                        ACTIVE,
                        ts,
                        f"re-activation: {cfg.reactivate_evals} consecutive evals "
                        f">= reactivate gate {cfg.reactivate_ic_gate}",
                        rolling_ic,
                    )
            else:
                self.recovery_count = 0
            return self.state

        # RETIRED
        if recover:
            self.recovery_count += 1
            if self.recovery_count >= cfg.reactivate_evals:
                self._transition(
                    WATCH,
                    ts,
                    f"recovery from retirement: {cfg.reactivate_evals} consecutive "
                    f"evals >= reactivate gate {cfg.reactivate_ic_gate}; "
                    "probation before ACTIVE",
                    rolling_ic,
                )
        else:
            self.recovery_count = 0
        return self.state

    @property
    def allocatable(self) -> bool:
        """Retirement halts allocation; ACTIVE and WATCH trade."""
        return self.state != RETIRED
