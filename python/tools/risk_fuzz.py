#!/usr/bin/env python3
"""Differential fuzzing of the three hard-risk engines (backlog epic E31).

The risk engine exists three times — Rust ``rust/risk`` (normative), Java
``com.iap.risk`` and Python ``iap.risk`` — and the three must produce
byte-identical decisions and audit output. This module is the shared core of
that proof:

- :class:`Generator` — a deterministic, state-aware step-sequence generator.
  It is seeded with the project's SplitMix64 and nothing else (no ``random``,
  no wall clock, no hash-ordered iteration), and it drives a live Python
  engine while it generates so that it can aim at the state the engine is
  actually in: quantities at and around every remaining headroom, prices at
  the band edge, timestamps at the staleness boundary, a clear for a kill that
  is engaged, a fill for an order that is open.
- :class:`Runner` — the Python replay driver. The Python engine is the oracle:
  in ``record`` mode it writes each order step's expectation into the script;
  in ``check`` mode it verifies one (the mode the Rust and Java replays
  implement).
- :func:`build_script` / :func:`expected_outputs` / :func:`verify_script` —
  script assembly, the expected audit JSONL + final snapshot, and the full
  check including the snapshot -> restore -> continue check at each cut point.
- :func:`shrink` — a delta-debugging (ddmin) minimiser that drops steps while
  a divergence persists, evaluated in batches so the two external engines are
  started once per round.

Step-script format (``x-version`` 1). A script is one JSON object::

    {"x-version": 1, "name": ..., "profile": ..., "seed": ...,
     "config": "configs/risk/risk.json",
     "config_set": [[section, key, value], ...],      # optional
     "config_remove": [section, key] | [section],     # optional
     "instruments": {"<id>": {tick_size, qty_unit, quote_ccy}, ...},
     "require_bootstrap": bool,
     "cuts": [step index, ...],
     "steps": [...]}

``steps`` use the step types of ``tests/golden/expected_risk_decisions.json``
(market, fill, cancel, gap, recover, venue_down, venue_up, kill, unkill,
override_loss, roll_session, order) and of the edge golden (bad_kill,
bad_unkill, bootstrap, restore), plus ``bad_override`` (an
``override_loss_limit`` call that must fail and change nothing). An order
step's ``expect`` carries ``decision``, ``rule_id``, ``severity``, ``scope``,
``scope_id`` and ``reason``.

Next to ``<name>.json`` live ``<name>.audit.jsonl`` (the audit JSONL of the
whole run; across a ``restore`` step: the old engine's log, then the restored
engine's) and ``<name>.snapshot.json`` (the final ``snapshot()`` in the
compact ``serde_json`` layout plus a newline, empty for a fail-closed
configuration, which cannot be restored). ``cuts`` are step indices: after
each one a replay snapshots the engine, restores it (``ts`` 0) and must
produce exactly the unbroken run's remaining audit lines and final snapshot.

NaN and Infinity cannot be carried in JSON, so no script contains them; those
inputs are covered by per-language unit tests instead.
"""

from __future__ import annotations

import copy
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "python" / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.core.rng import SplitMix64  # noqa: E402
from iap.risk import (  # noqa: E402
    Fill,
    OrderRequest,
    RiskEngine,
    RiskLimits,
    Scope,
    instrument_refs_from_golden,
)

SCRIPT_VERSION = 1
CONFIG_PATH = "configs/risk/risk.json"
T0 = 1_700_000_000_000_000_000
MS = 1_000_000
SEC = 1_000_000_000
I64_MAX = (1 << 63) - 1
I64_MIN = -(1 << 63)
U64_MAX = (1 << 64) - 1
U32_MAX = (1 << 32) - 1

#: Step types whose engine call must fail (and the type each is recorded from).
_BAD_OF = {"kill": "bad_kill", "unkill": "bad_unkill", "override_loss": "bad_override"}
_GOOD_OF = {v: k for k, v in _BAD_OF.items()}

STEP_TYPES = (
    "order",
    "market",
    "fill",
    "cancel",
    "gap",
    "recover",
    "venue_down",
    "venue_up",
    "kill",
    "unkill",
    "bad_kill",
    "bad_unkill",
    "override_loss",
    "bad_override",
    "roll_session",
    "bootstrap",
    "restore",
)


class Divergence(AssertionError):
    """A replay did not reproduce the recorded expectation."""


# --------------------------------------------------------------------- driver


def load_base_config() -> dict[str, Any]:
    """The committed ``configs/risk/risk.json`` document."""
    with open(ROOT / CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def effective_config(script: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """``base`` with the script's ``config_remove`` / ``config_set`` applied."""
    doc = copy.deepcopy(base)
    rm = script.get("config_remove")
    if rm:
        if len(rm) == 1:
            del doc[rm[0]]
        else:
            del doc[rm[0]][rm[1]]
    for section, key, value in script.get("config_set", ()):
        doc[section][key] = copy.deepcopy(value)
    return doc


def parse_order(v: dict[str, Any]) -> OrderRequest:
    return OrderRequest(
        order_id=v["order_id"],
        instrument_id=v["instrument_id"],
        side=v["side"],
        qty=v["qty"],
        price_ticks=v["price_ticks"],
        order_type=v["order_type"],
        venue_id=v["venue_id"],
        strategy_id=v["strategy_id"],
        urgency=v["urgency"],
        timestamp=v["timestamp"],
    )


def parse_fill(v: dict[str, Any]) -> Fill:
    return Fill(
        ts=v["ts"],
        strategy_id=v["strategy_id"],
        instrument_id=v["instrument_id"],
        order_id=v["order_id"],
        side=v["side"],
        qty=v["qty"],
        price_ticks=v["price_ticks"],
    )


class Runner:
    """One replay of a script through the Python engine.

    ``engine_cls`` is the engine under test (default: the real
    :class:`RiskEngine`); the mutation tests pass a deliberately broken one.
    """

    def __init__(
        self,
        script: dict[str, Any],
        base: dict[str, Any],
        engine_cls: type[RiskEngine] = RiskEngine,
    ) -> None:
        self.engine_cls = engine_cls
        self.doc = effective_config(script, base)
        self.refs = instrument_refs_from_golden(script["instruments"])
        self.eng = engine_cls.from_config(self.doc, self.refs)
        if script.get("require_bootstrap", False):
            self.eng.require_bootstrap()
        self._done: list[str] = []
        self._done_lines = 0

    # ------------------------------------------------------------- outputs

    def line_count(self) -> int:
        """Audit lines emitted so far, across restores."""
        return self._done_lines + self.eng.audit_len()

    def audit(self) -> str:
        """Audit JSONL of the whole run so far, across restores."""
        return "".join(self._done) + self.eng.audit_jsonl()

    def snapshot_text(self) -> str:
        """Final snapshot, compact ``serde_json`` layout plus a newline;
        empty for a fail-closed configuration (it cannot be restored)."""
        if self.eng._limits is None:
            return ""
        return self.eng.snapshot_json(pretty=False) + "\n"

    def restore(self, ts: int) -> None:
        """Snapshot through the JSON text and continue on the restored engine."""
        self._done.append(self.eng.audit_jsonl())
        self._done_lines += self.eng.audit_len()
        snap = json.loads(self.eng.snapshot_json(pretty=False))
        self.eng = self.engine_cls.restore(RiskLimits.from_json(self.doc), self.refs, snap, ts)

    # --------------------------------------------------------------- steps

    def _call(self, kind: str, step: dict[str, Any]) -> None:
        eng = self.eng
        scope = Scope.parse(step["scope"])
        if kind == "kill":
            eng.engage_kill(scope, step["scope_id"], step["ts"], step.get("reason", ""))
        elif kind == "unkill":
            eng.clear_kill(scope, step["scope_id"], step["ts"], step.get("reason", ""))
        else:
            eng.override_loss_limit(
                scope, step["scope_id"], step["new_limit"], step["ts"], step.get("approver", "")
            )

    def apply(self, i: int, step: dict[str, Any], mode: str = "check") -> None:
        """Apply step ``i``. ``mode``: ``record`` writes the expectation into
        the step, ``check`` raises :class:`Divergence` on a mismatch, ``run``
        only executes."""
        kind = step["type"]
        eng = self.eng
        if kind == "order":
            order = parse_order(step["order"])
            d = eng.check_order(order)
            ev = eng.audit()[-1]
            got = {
                "decision": int(d.decision),
                "rule_id": d.rule_id,
                "severity": int(d.severity),
                "scope": ev.scope.value,
                "scope_id": ev.scope_id,
                "reason": d.reason,
            }
            if mode == "record":
                step["expect"] = got
            elif mode == "check" and got != step["expect"]:
                raise Divergence(
                    f"step {i} order {order.order_id}: got {got}, want {step['expect']}"
                )
        elif kind == "market":
            eng.on_market(step["instrument_id"], step["bid_ticks"], step["ask_ticks"], step["ts"])
        elif kind == "fill":
            eng.on_fill(parse_fill(step))
        elif kind == "cancel":
            eng.on_order_done(step["order_id"])
        elif kind == "gap":
            eng.on_sequence_gap(step["instrument_id"], step["ts"])
        elif kind == "recover":
            eng.on_feed_recovered(step["instrument_id"], step["ts"])
        elif kind == "venue_down":
            eng.on_venue_disconnect(step["venue_id"], step["ts"])
        elif kind == "venue_up":
            eng.on_venue_reconnect(step["venue_id"], step["ts"])
        elif kind in _BAD_OF or kind in _GOOD_OF:
            base_kind = _GOOD_OF.get(kind, kind)
            try:
                self._call(base_kind, step)
                failed = False
            except ValueError:
                failed = True
            if mode == "record":
                step["type"] = _BAD_OF[base_kind] if failed else base_kind
            elif mode == "check" and failed != (kind in _GOOD_OF):
                raise Divergence(f"step {i} {kind}: the call {'failed' if failed else 'succeeded'}")
        elif kind == "roll_session":
            eng.roll_session(step["ts"], step.get("reason", ""))
        elif kind == "bootstrap":
            eng.bootstrap_positions([parse_fill(f) for f in step["fills"]], step["ts"])
        elif kind == "restore":
            self.restore(step["ts"])
        else:
            raise ValueError(f"unknown step type {kind!r}")


def strip_expectations(script: dict[str, Any]) -> dict[str, Any]:
    """A copy with every recorded expectation removed (``bad_*`` step types
    back to the plain call, no ``expect``), ready to be re-recorded."""
    out = copy.deepcopy(script)
    for step in out["steps"]:
        step.pop("expect", None)
        step["type"] = _GOOD_OF.get(step["type"], step["type"])
    return out


def record(script: dict[str, Any], base: dict[str, Any]) -> Runner:
    """Run ``script`` through the oracle, writing every expectation in place."""
    runner = Runner(script, base)
    for i, step in enumerate(script["steps"]):
        runner.apply(i, step, "record")
    return runner


def run_script(
    script: dict[str, Any],
    base: dict[str, Any],
    cut: int | None = None,
    mode: str = "check",
    engine_cls: type[RiskEngine] = RiskEngine,
) -> tuple[str, int, str]:
    """Replay ``script`` once. With ``cut`` the engine is snapshotted and
    restored (``ts`` 0) after that step. Returns ``(audit, mark, snapshot)``
    where ``mark`` is the number of audit lines emitted up to the cut."""
    runner = Runner(script, base, engine_cls)
    mark = -1
    for i, step in enumerate(script["steps"]):
        runner.apply(i, step, mode)
        if cut is not None and i == cut:
            mark = runner.line_count()
            runner.restore(0)
    return runner.audit(), mark, runner.snapshot_text()


def expected_outputs(script: dict[str, Any], base: dict[str, Any]) -> tuple[str, str]:
    """``(audit JSONL, snapshot text)`` the oracle produces for ``script``."""
    audit, _, snapshot = run_script(script, base, mode="run")
    return audit, snapshot


def verify_script(
    script: dict[str, Any],
    base: dict[str, Any],
    want_audit: str,
    want_snapshot: str,
    engine_cls: type[RiskEngine] = RiskEngine,
) -> None:
    """The full replay check every language implements: decisions, audit bytes,
    final snapshot bytes, and the restore check at each cut point. Raises
    :class:`Divergence`."""
    name = script.get("name", "?")
    audit, _, snapshot = run_script(script, base, engine_cls=engine_cls)
    if audit != want_audit:
        raise Divergence(f"{name}: audit differs: {_first_diff(audit, want_audit)}")
    if snapshot != want_snapshot:
        raise Divergence(f"{name}: final snapshot differs")
    full = audit_lines(audit)
    for cut in script.get("cuts", ()):
        got, mark, snap = run_script(script, base, cut=cut, engine_cls=engine_cls)
        lines = audit_lines(got)
        if lines[mark].find('"rule_id":"STATE_RESTORED"') < 0:
            raise Divergence(f"{name}: cut {cut}: no STATE_RESTORED record")
        if lines[:mark] != full[:mark] or lines[mark + 1 :] != full[mark:]:
            raise Divergence(f"{name}: cut {cut}: the restored engine's audit tail differs")
        if snap != want_snapshot:
            raise Divergence(f"{name}: cut {cut}: the restored engine's final state differs")


def audit_lines(text: str) -> list[str]:
    """The lines of an audit JSONL. Split on LF only: ``str.splitlines`` also
    breaks on U+2028 and friends, which a reason may legitimately contain."""
    return text.split("\n")[:-1] if text else []


def _first_diff(got: str, want: str) -> str:
    g, w = audit_lines(got), audit_lines(want)
    for i, (a, b) in enumerate(zip(g, w, strict=False)):
        if a != b:
            return f"line {i}: got {a} want {b}"
    return f"{len(g)} lines, want {len(w)}"


# ------------------------------------------------------------ script file io


def dump_script(script: dict[str, Any]) -> str:
    """Canonical text of a script file: compact JSON, one step per line, LF,
    ASCII only (non-ASCII ids are ``\\u`` escaped)."""
    head = {k: v for k, v in script.items() if k != "steps"}
    lines = [json.dumps(head, separators=(",", ":"), ensure_ascii=True)[:-1] + ',"steps":[']
    steps = script["steps"]
    for i, step in enumerate(steps):
        tail = "," if i + 1 < len(steps) else ""
        lines.append(json.dumps(step, separators=(",", ":"), ensure_ascii=True) + tail)
    lines.append("]}")
    return "\n".join(lines) + "\n"


def script_files(script: dict[str, Any], base: dict[str, Any]) -> dict[str, bytes]:
    """The three files of one script, by file name, as exact bytes."""
    audit, snapshot = expected_outputs(script, base)
    name = script["name"]
    return {
        f"{name}.json": dump_script(script).encode("utf-8"),
        f"{name}.audit.jsonl": audit.encode("utf-8"),
        f"{name}.snapshot.json": snapshot.encode("utf-8"),
    }


def write_files(out_dir: Path, files: dict[str, bytes]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in sorted(files):
        (out_dir / name).write_bytes(files[name])


def load_corpus(corpus_dir: Path) -> list[tuple[dict[str, Any], str, str]]:
    """Every ``(script, audit, snapshot)`` of a corpus directory, by name."""
    out = []
    for path in sorted(corpus_dir.glob("*.json")):
        if path.name.endswith(".snapshot.json"):
            continue
        script = json.loads(path.read_text(encoding="utf-8"))
        stem = path.name[: -len(".json")]
        audit = (corpus_dir / f"{stem}.audit.jsonl").read_bytes().decode("utf-8")
        snapshot = (corpus_dir / f"{stem}.snapshot.json").read_bytes().decode("utf-8")
        out.append((script, audit, snapshot))
    return out


# ------------------------------------------------------------------ generator


_I64_FIELDS = ("ts", "timestamp", "qty", "price_ticks", "bid_ticks", "ask_ticks")


def _clamp_i64(step: dict[str, Any]) -> None:
    """Keep every i64 field of a generated step inside the wire domain (a
    boundary value plus an offset must saturate, not leave i64)."""
    for obj in (step, step.get("order", {}), *step.get("fills", ())):
        for key in _I64_FIELDS:
            if key in obj:
                obj[key] = max(I64_MIN, min(I64_MAX, obj[key]))


class Rng:
    """The few draws the generator needs, all from one SplitMix64 stream."""

    def __init__(self, seed: int) -> None:
        self._r = SplitMix64(seed)

    def below(self, n: int) -> int:
        return self._r.below(n)

    def pct(self, p: int) -> bool:
        return self._r.below(100) < p

    def between(self, lo: int, hi: int) -> int:
        return self._r.randint(lo, hi)

    def pick(self, seq: Sequence[Any]) -> Any:
        return seq[self._r.below(len(seq))]

    def weighted(self, table: Sequence[tuple[int, Any]]) -> Any:
        x = self._r.below(sum(w for w, _ in table))
        for w, item in table:
            if x < w:
                return item
            x -= w
        raise AssertionError("unreachable")


#: Reference data shared by every script (ids 101 / 103 are the EURUSD /
#: USDJPY pairs ``configs/risk/risk.json`` converts EUR and JPY through; id 5
#: quotes a currency with no conversion entry).
INSTRUMENTS: dict[str, dict[str, Any]] = {
    "1": {"tick_size": 0.01, "qty_unit": 1.0, "quote_ccy": "USD"},
    "2": {"tick_size": 0.01, "qty_unit": 1.0, "quote_ccy": "USD"},
    "3": {"tick_size": 0.05, "qty_unit": 1.0, "quote_ccy": "EUR"},
    "4": {"tick_size": 1.0, "qty_unit": 100.0, "quote_ccy": "JPY"},
    "5": {"tick_size": 0.0001, "qty_unit": 1.0, "quote_ccy": "XXX"},
    "101": {"tick_size": 0.00001, "qty_unit": 100000.0, "quote_ccy": "USD"},
    "103": {"tick_size": 0.001, "qty_unit": 100000.0, "quote_ccy": "JPY"},
}
#: Starting mid (ticks) of each instrument.
BASE_MID = {1: 2451, 2: 500, 3: 400, 4: 1500, 5: 10_000, 101: 108_500, 103: 150_000}
#: The pair whose mid converts each non-reporting quote currency.
FX_PAIR = {"EUR": 101, "JPY": 103}

#: Strategy ids outside the comfortable ASCII range. Built with ``chr`` so
#: this file stays ASCII: U+FFEE and U+FFFD sort AFTER the astral U+1F600 /
#: U+10000 in UTF-16 code units and BEFORE them in code points (and UTF-8
#: bytes), which is exactly the ordering a BTreeMap<String, _> port must get
#: right; the rest are JSON-escaping cases (quote, backslash, control
#: characters, DEL, U+2028) and an oversized id.
EXOTIC_STRATEGIES = (
    "",
    "S 1",
    chr(0xE9),
    chr(0x7B56) + chr(0x7565),
    chr(0xFFEE),
    chr(0x1F600),
    chr(0x10000) + "z",
    chr(0xFFFD),
    'q"uo' + chr(92) + "te",
    "tab" + chr(9) + "here",
    "nl" + chr(10) + "x",
    chr(1) + "ctl",
    chr(0x7F) + "del",
    chr(0x2028) + "ls",
    "x" * 300,
)
REASONS = (
    "ops halt",
    "",
    "desk request #42",
    'said "stop"',
    "back" + chr(92) + "slash",
    "na" + chr(0xEF) + "ve caf" + chr(0xE9),
    chr(0x505C) + chr(0x6B62),
    chr(0x1F6D1) + " halt",
    "line1" + chr(10) + "line2",
    chr(0) + "nul",
    "cr" + chr(13) + "lf" + chr(10) + chr(0x1F) + chr(0x7F) + chr(0x85) + chr(0x2029),
    "r" * 400,
)
#: Scope ids ``str::parse::<u32>()`` / ``<u16>`` rejects (letters, empty,
#: signs, whitespace, a fullwidth and an Arabic-Indic digit, a fraction, hex,
#: an exponent, out of range, a trailing newline).
BAD_SCOPE_IDS = (
    "AAPL",
    "",
    "-0",
    "-1",
    "+",
    "++1",
    "1 ",
    " 1",
    chr(0xFF11),
    chr(0x663),
    "1.0",
    "0x10",
    "1e3",
    "4294967296",
    "99999999999999999999999999",
    "1" + chr(10),
)
BIG_ORDER_IDS = (1 << 63, (1 << 63) + 1, U64_MAX, U64_MAX - 1, (1 << 63) - 1, 1 << 62)
LOSS_LIMITS = (1.0, 100.0, 2500.5, 50000.0, 250000.0, 1e7, 0.01, 123456.789, 1e15, 3, 0.005)


@dataclass(frozen=True)
class Profile:
    """One generation regime: what the script is biased towards."""

    name: str
    steps: int
    weights: tuple[tuple[int, str], ...]
    config_set: tuple[tuple[str, str, Any], ...] = ()
    config_remove: tuple[str, ...] = ()
    require_bootstrap: bool = False
    strategies: tuple[str, ...] = ("S1", "S2", "S3")
    instruments: tuple[int, ...] = (1, 2)
    #: Percent of event timestamps replaced by a boundary / corrupt one.
    chaos_ts: int = 1
    #: Percent of ids / strings replaced by an exotic one.
    exotic: int = 3
    cuts: int = 3


_FLOW = (
    (22, "order"),
    (8, "order_boundary"),
    (3, "order_malformed"),
    (4, "order_cross"),
    (2, "order_dup"),
    (2, "order_burst"),
    (1, "order_unknown"),
    (14, "market"),
    (3, "market_jump"),
    (1, "market_weird"),
    (12, "fill_open"),
    (6, "fill_ext"),
    (2, "fill_bad"),
    (6, "cancel"),
    (2, "kill"),
    (2, "unkill"),
    (1, "kill_bad"),
    (1, "override"),
    (1, "override_bad"),
    (1, "roll"),
    (2, "venue"),
    (1, "gap"),
    (1, "restore"),
    (2, "recipe"),
)


def _weights(**overrides: int) -> tuple[tuple[int, str], ...]:
    table = dict((name, w) for w, name in _FLOW)
    table.update(overrides)
    return tuple((w, name) for name, w in table.items() if w > 0)


_TIGHT = (
    ("per_order", "max_order_qty", 500),
    ("per_order", "max_order_notional", 9000.0),
    ("per_instrument", "max_position_qty", 1200),
    ("per_instrument", "max_instrument_notional", 20000.0),
    ("global", "max_gross_notional", 30000.0),
    ("global", "max_net_notional", 18000.0),
    ("global", "max_daily_loss", 900.0),
    ("per_strategy", "max_daily_loss", 300.0),
)

PROFILES: dict[str, Profile] = {
    p.name: p
    for p in (
        Profile("baseline", 110, _FLOW),
        Profile("tight", 120, _weights(order_boundary=14, market_jump=6), config_set=_TIGHT),
        Profile(
            "kills",
            100,
            _weights(kill=10, unkill=8, kill_bad=5, venue=8, gap=4, order=26, restore=2),
            exotic=8,
        ),
        Profile(
            "clock",
            110,
            _weights(market_weird=5, order_dup=6, order_burst=5, order_boundary=10),
            config_set=(
                ("per_order", "duplicate_order_window_ns", 2 * SEC),
                ("global", "max_order_rate_per_sec", 40),
                ("global", "order_rate_burst", 3),
            ),
            chaos_ts=12,
        ),
        Profile(
            "unstale",
            90,
            _weights(market_weird=5, order_boundary=10),
            config_set=(
                ("per_order", "stale_book_reject", False),
                ("market_data", "max_sequence_gap_before_halt", 3),
                ("market_data", "stale_feed_timeout_ns", 250 * MS),
            ),
            chaos_ts=10,
        ),
        Profile(
            "overflow",
            100,
            _weights(fill_huge=6, order_malformed=10, order_unknown=4, fill_bad=6, cancel=8),
            chaos_ts=5,
            exotic=15,
        ),
        Profile(
            "fx",
            120,
            _weights(market=20, market_weird=3, fill_ext=10, market_jump=5, recipe=10),
            instruments=(1, 3, 4, 5, 101, 103),
        ),
        Profile(
            "unicode",
            100,
            _weights(kill=5, unkill=4, override=4, fill_ext=10, roll=2, market_jump=5),
            strategies=("S1", chr(0xFFEE), chr(0x1F600), chr(0xE9), chr(0x7B56) + chr(0x7565), ""),
            exotic=30,
        ),
        Profile(
            "loss",
            120,
            _weights(market_jump=10, fill_ext=10, override=5, roll=3, unkill=6, recipe=8),
            config_set=(
                ("global", "max_daily_loss", 6000.0),
                ("per_strategy", "max_daily_loss", 2500.0),
            ),
        ),
        Profile(
            "bootstrap",
            90,
            _weights(bootstrap=4, restore=6, fill_ext=8),
            require_bootstrap=True,
        ),
        Profile(
            "halted",
            60,
            _weights(unkill=6, kill=4),
            config_set=(("global", "kill_switch_engaged", True),),
        ),
        Profile("recipes", 110, _weights(recipe=30), instruments=(1, 2, 3)),
        Profile("config", 16, _weights(recipe=0), cuts=1),
    )
}

#: Single mutations of ``configs/risk/risk.json``; a ``config`` script applies
#: the one its seed selects (``seed % len``), so a run of consecutive seeds
#: covers them all, and sometimes a second one on top. Most make the document
#: INVALID: the engine must land fail-closed and the ``CONFIG_MISSING`` reason
#: (the first offending key, in the reference's parse order) is audit output
#: like any other. The rest are valid but unusual limit sets.
BAD_CONFIG_SETS: tuple[tuple[str, str, Any], ...] = (
    ("global", "max_gross_notional", 0),
    ("global", "max_net_notional", -5.5),
    ("global", "max_daily_loss", -0.0),
    ("global", "max_order_rate_per_sec", "500"),
    ("global", "order_rate_burst", -1e-7),
    ("global", "kill_switch_engaged", 0),
    ("per_order", "max_order_qty", 0),
    ("per_order", "max_order_qty", 50000.0),
    ("per_order", "max_order_notional", None),
    ("per_order", "price_band_bps", -1e21),
    ("per_order", "stale_book_reject", "true"),
    ("per_order", "duplicate_order_window_ns", -1),
    ("per_order", "duplicate_order_window_ns", 1.5),
    ("per_instrument", "max_position_qty", -100000),
    ("per_instrument", "max_instrument_notional", 0.0),
    ("per_strategy", "max_daily_loss", -250000.125),
    ("market_data", "max_sequence_gap_before_halt", -1),
    ("market_data", "stale_feed_timeout_ns", 0),
    ("currency", "reporting_ccy", ""),
    ("currency", "conversion", []),
    ("currency", "conversion", {"EUR": {"instrument_id": 0, "invert": False}}),
    ("currency", "conversion", {"EUR": {"instrument_id": 4294967296, "invert": False}}),
    ("currency", "conversion", {"EUR": {"instrument_id": -3, "invert": False}}),
    ("currency", "conversion", {"EUR": {"instrument_id": 101}}),
    ("currency", "conversion", {"EUR": 101}),
    ("currency", "conversion", {"ZZZ": {"invert": True}, "AAA": {"instrument_id": 101}}),
)
BAD_CONFIG_REMOVES: tuple[tuple[str, ...], ...] = (
    ("global", "max_gross_notional"),
    ("per_order", "duplicate_order_window_ns"),
    ("per_order", "stale_book_reject"),
    ("market_data", "max_sequence_gap_before_halt"),
    ("currency", "reporting_ccy"),
    ("currency", "conversion"),
    ("global",),
    ("per_order",),
    ("per_instrument",),
    ("per_strategy",),
    ("market_data",),
    ("currency",),
)
ODD_CONFIG_SETS: tuple[tuple[str, str, Any], ...] = (
    ("global", "max_gross_notional", 5000000),
    ("global", "order_rate_burst", 0.5),
    ("global", "max_order_rate_per_sec", 1e-9),
    ("per_order", "max_order_qty", I64_MAX),
    ("per_order", "price_band_bps", 1e-9),
    ("per_order", "duplicate_order_window_ns", I64_MAX),
    ("market_data", "max_sequence_gap_before_halt", 0),
    ("market_data", "stale_feed_timeout_ns", I64_MAX),
    ("currency", "reporting_ccy", "EUR"),
    ("currency", "conversion", {}),
    ("currency", "conversion", {"JPY": {"instrument_id": 103, "invert": False}}),
    ("currency", "conversion", {"EUR": {"instrument_id": 102, "invert": False}}),
)
#: Every single mutation, in the order a ``config`` script's seed indexes.
CONFIG_MUTATIONS: tuple[tuple[str, tuple[Any, ...]], ...] = (
    tuple(("set", m) for m in BAD_CONFIG_SETS)
    + tuple(("remove", m) for m in BAD_CONFIG_REMOVES)
    + tuple(("set", m) for m in ODD_CONFIG_SETS)
)


@dataclass
class Generator:
    """State-aware step generator (see the module docstring)."""

    seed: int
    profile: Profile
    base: dict[str, Any]
    name: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.rng = Rng(self.seed)
        p = self.profile
        self.script: dict[str, Any] = {
            "x-version": SCRIPT_VERSION,
            "name": self.name or f"{p.name}_{self.seed}",
            "profile": p.name,
            "seed": self.seed,
            "config": CONFIG_PATH,
        }
        config_set = [list(t) for t in p.config_set]
        config_remove: list[str] = list(p.config_remove)
        if p.name == "config":
            kind, mutation = CONFIG_MUTATIONS[self.seed % len(CONFIG_MUTATIONS)]
            if kind == "remove":
                config_remove = list(mutation)
            else:
                config_set.append(list(mutation))
            if self.rng.pct(30):
                extra = list(self.rng.pick(BAD_CONFIG_SETS))
                if extra[0] != mutation[0]:
                    config_set.append(extra)
        if config_set:
            self.script["config_set"] = config_set
        if config_remove:
            self.script["config_remove"] = config_remove
        self.script["instruments"] = copy.deepcopy(INSTRUMENTS)
        self.script["require_bootstrap"] = p.require_bootstrap
        self.script["cuts"] = []
        self.script["steps"] = self.steps
        self.runner = Runner(self.script, self.base)
        self.now = T0
        self.next_oid = 1
        self.big_oid = 0
        self.owner: dict[int, str] = {}

    # ----------------------------------------------------------- plumbing

    @property
    def eng(self) -> RiskEngine:
        return self.runner.eng

    @property
    def limits(self) -> RiskLimits | None:
        return self.eng._limits

    def emit(self, step: dict[str, Any]) -> dict[str, Any]:
        _clamp_i64(step)
        self.runner.apply(len(self.steps), step, "record")
        self.steps.append(step)
        return step

    def timeout(self) -> int:
        return self.limits.stale_feed_timeout_ns if self.limits else 5 * SEC

    def tick(self) -> None:
        r = self.rng.below(100)
        if r < 50:
            self.now += self.rng.between(1, 60) * MS
        elif r < 72:
            return
        elif r < 88:
            self.now += self.rng.between(1, 900) * 1000
        elif r < 97:
            self.now += self.rng.between(1, 3) * SEC
        else:
            self.now += min(self.timeout(), 10 * SEC) + self.rng.between(-1, 1)

    def weird_ts(self) -> int:
        t = self.timeout()
        return self.rng.pick(
            (
                self.now - 1,
                self.now - t,
                self.now - t - 1,
                self.now + t,
                self.now + t + 1,
                self.now + 3600 * SEC,
                self.now - 3600 * SEC,
                0,
                -1,
                I64_MAX,
                I64_MAX - 1,
                I64_MIN,
                I64_MIN + 1,
            )
        )

    def ts(self) -> int:
        return self.weird_ts() if self.rng.pct(self.profile.chaos_ts) else self.now

    def strategy(self) -> str:
        if self.rng.pct(self.profile.exotic):
            return self.rng.pick(EXOTIC_STRATEGIES)
        return self.rng.pick(self.profile.strategies)

    def text(self) -> str:
        if self.rng.pct(max(self.profile.exotic, 10)):
            return self.rng.pick(REASONS)
        return self.rng.pick(REASONS[:3])

    def instrument(self) -> int:
        return self.rng.pick(self.profile.instruments)

    def venue(self) -> int:
        return self.rng.weighted(((60, 1), (18, 2), (15, 0), (5, 3), (2, 65535)))

    def new_oid(self) -> int:
        if self.rng.pct(self.profile.exotic):
            self.big_oid += 1
            return self.rng.pick(((1 << 63) + self.big_oid, U64_MAX - self.big_oid))
        self.next_oid += 1
        return self.next_oid

    # ------------------------------------------------------- engine reads

    def mid(self, iid: int) -> int | None:
        md = self.eng._market.get(iid)
        if md is None or md.bid_ticks <= 0 or md.ask_ticks <= 0:
            return None
        s = md.bid_ticks + md.ask_ticks
        return s // 2 if s <= I64_MAX else None

    def ref_mid(self, iid: int) -> int:
        m = self.mid(iid)
        if m is not None and 0 < m < 1 << 40:
            return m
        return BASE_MID.get(iid, 1000)

    def fresh(self, iid: int) -> bool:
        md = self.eng._market.get(iid)
        if md is None or self.mid(iid) is None:
            return False
        return 0 <= self.now - md.ts <= self.timeout() // 2

    def market_step(self, iid: int, mid: int, ts: int) -> dict[str, Any]:
        half = self.rng.between(1, 2)
        return {
            "type": "market",
            "instrument_id": iid,
            "bid_ticks": mid - half,
            "ask_ticks": mid + half,
            "ts": ts,
        }

    def ensure_mark(self, iid: int) -> None:
        """Keep the instrument (and its conversion pair) freshly marked."""
        need = [iid]
        ccy = INSTRUMENTS.get(str(iid), {}).get("quote_ccy")
        if ccy in FX_PAIR and FX_PAIR[ccy] != iid:
            need.append(FX_PAIR[ccy])
        for i in need:
            if str(i) in INSTRUMENTS and not self.fresh(i) and self.rng.pct(85):
                self.emit(self.market_step(i, self.ref_mid(i), self.now))

    def unit_value(self, iid: int, price_ticks: int) -> float:
        """Reporting-currency value of one qty unit at ``price_ticks``."""
        ref = self.runner.refs.get(iid)
        if ref is None:
            return 1.0
        rate = self.eng._fx_rate(ref.quote_ccy)
        fx = rate[0] if rate is not None else 1.0
        return max(float(price_ticks) * ref.tick_size * ref.qty_unit * fx, 1e-12)

    def exposure(self) -> tuple[float, float]:
        """(gross, net) of positions and open orders, as check 19/20 sums them."""
        gross = net = 0.0
        for iid, p in self.eng._positions.items():
            v = float(p) * self.unit_value(iid, self.ref_mid(iid))
            gross += abs(v)
            net += v
        for r in self.eng._open.values():
            price = r.price_ticks if r.price_ticks > 0 else self.ref_mid(r.instrument_id)
            v = float(r.qty) * self.unit_value(r.instrument_id, price)
            gross += v
            net += v if r.side == 0 else -v
        return gross, net

    def projected(self, iid: int, side: int) -> int:
        pos = self.eng._positions.get(iid, 0)
        same = sum(
            r.qty for r in self.eng._open.values() if r.instrument_id == iid and r.side == side
        )
        return pos + same if side == 0 else pos - same

    # -------------------------------------------------------------- orders

    def order_step(
        self,
        iid: int,
        side: int,
        qty: int,
        price: int,
        otype: int,
        strategy: str,
        ts: int,
        venue: int | None = None,
        oid: int | None = None,
        urgency: Any = 0.5,
    ) -> dict[str, Any]:
        if oid is None:
            oid = self.new_oid()
        return {
            "type": "order",
            "order": {
                "order_id": oid,
                "instrument_id": iid,
                "side": side,
                "qty": qty,
                "price_ticks": price,
                "order_type": otype,
                "venue_id": self.venue() if venue is None else venue,
                "strategy_id": strategy,
                "urgency": urgency,
                "timestamp": ts,
            },
        }

    def send(self, step: dict[str, Any]) -> dict[str, Any]:
        self.emit(step)
        if step["expect"]["decision"] == 1:
            self.owner[step["order"]["order_id"]] = step["order"]["strategy_id"]
        return step

    def passive_price(self, iid: int, side: int) -> int:
        mid = self.ref_mid(iid)
        band = self.limits.price_band_bps if self.limits else 200.0
        width = max(2, int(mid * band / 1e4 * 0.8))
        off = self.rng.between(1, width)
        if self.rng.pct(8):
            off = -off  # aggressive: may cross an own resting order
        return max(1, mid - off if side == 0 else mid + off)

    def small_qty(self) -> int:
        cap = self.limits.max_order_qty if self.limits else 1000
        if self.rng.pct(70):
            return self.rng.between(1, max(1, cap // 20))
        return self.rng.between(max(1, cap // 4), cap)

    def do_order(self) -> None:
        iid = self.instrument()
        self.ensure_mark(iid)
        side = self.rng.below(2)
        otype = self.rng.weighted(((70, 2), (8, 1), (7, 3), (5, 4), (5, 5), (5, 6)))
        price = self.passive_price(iid, side) if otype == 2 else 0
        if otype in (3, 4) and self.rng.pct(50):
            price = self.passive_price(iid, side)
        self.send(
            self.order_step(iid, side, self.small_qty(), price, otype, self.strategy(), self.ts())
        )

    def boundary_qty(self, iid: int, side: int, price: int) -> int:
        lim = self.limits
        if lim is None:
            return self.rng.between(1, 100)
        value = self.unit_value(iid, price)
        mid_value = self.unit_value(iid, self.ref_mid(iid))
        proj = self.projected(iid, side)
        signed = proj if side == 0 else -proj
        gross, net = self.exposure()
        signed_net = net if side == 0 else -net
        target = self.rng.pick(
            (
                lim.max_order_qty,
                int(lim.max_order_notional / value),
                lim.max_position_qty - signed,
                int(lim.max_instrument_notional / mid_value) - signed,
                int((lim.max_gross_notional - gross) / value),
                int((lim.max_net_notional - signed_net) / value),
            )
        )
        qty = target + self.rng.between(-1, 1)
        return qty if qty > 0 else self.rng.between(1, 3)

    def do_order_boundary(self) -> None:
        iid = self.instrument()
        self.ensure_mark(iid)
        side = self.rng.below(2)
        lim = self.limits
        mid = self.ref_mid(iid)
        md = self.eng._market.get(iid)
        ts = self.now
        price = self.passive_price(iid, side)
        qty = self.small_qty()
        which = self.rng.below(10)
        if which < 6 or lim is None:
            qty = self.boundary_qty(iid, side, price)
        elif which < 8:
            edge = mid * lim.price_band_bps / 1e4
            sign = -1 if self.rng.pct(50) else 1
            price = max(1, int(mid + sign * edge) + self.rng.between(-1, 1))
        elif md is not None:
            ts = md.ts + self.timeout() + self.rng.between(-1, 1)
        self.send(self.order_step(iid, side, qty, price, 2, self.strategy(), ts))

    def do_order_malformed(self) -> None:
        iid = self.instrument()
        self.ensure_mark(iid)
        side, qty, otype, urgency = self.rng.below(2), self.rng.between(1, 50), 2, 0.5
        price = self.passive_price(iid, side)
        which = self.rng.below(12)
        if which == 0:
            side = self.rng.pick((2, 7, 255))
        elif which == 1:
            otype = self.rng.pick((0, 7, 255))
        elif which == 2:
            qty = self.rng.pick((0, -1, I64_MIN, I64_MIN + 1))
        elif which == 3:
            qty = self.rng.pick((I64_MAX, I64_MAX - 1, 1 << 62))
        elif which == 4:
            urgency = self.rng.pick(
                (-0.5, 1.5, 2, 1e21, -1e-7, 1.0000000000000002, -5e-324, 1e300, 123456789.125)
            )
        elif which == 5:
            urgency = self.rng.pick((0, 1, 0.0, 1.0, -0.0, 5e-324, 0.1, 1e-300))
        elif which == 6:
            otype, price = 1, self.rng.pick((price, -1, I64_MAX))
        elif which == 7:
            price = self.rng.pick((0, -3, I64_MIN))
        elif which == 8:
            otype, price = self.rng.pick((3, 4)), self.rng.pick((-1, I64_MIN))
        elif which == 9:
            otype, price = self.rng.pick((5, 6)), self.rng.pick((price, -7))
        elif which == 10:
            price = self.rng.pick((I64_MAX, I64_MAX - 1, 1 << 62, 1 << 53))
        else:
            otype, price, qty = self.rng.pick((3, 4)), I64_MAX, I64_MAX
        self.send(
            self.order_step(
                iid, side, qty, price, otype, self.strategy(), self.ts(), None, None, urgency
            )
        )

    def do_order_cross(self) -> None:
        opens = sorted(self.eng._open)
        if not opens:
            return self.do_order()
        oid = self.rng.pick(opens)
        r = self.eng._open[oid]
        self.ensure_mark(r.instrument_id)
        side = 1 - r.side if r.side in (0, 1) else 0
        if r.price_ticks > 0 and self.rng.pct(75):
            price, otype = max(1, r.price_ticks + self.rng.between(-1, 1)), 2
        else:
            price, otype = 0, self.rng.pick((1, 3, 5, 6))
        self.send(
            self.order_step(
                r.instrument_id,
                side,
                self.rng.between(1, 50),
                price,
                otype,
                self.strategy(),
                self.now,
            )
        )

    def do_order_dup(self) -> None:
        seen = sorted(self.eng._seen_orders)
        if not seen:
            return self.do_order()
        oid = self.rng.pick(seen)
        iid = self.instrument()
        self.ensure_mark(iid)
        side = self.rng.below(2)
        ts = self.now
        window = self.limits.duplicate_order_window_ns if self.limits else 0
        if window and self.rng.pct(60):
            ts = self.eng._seen_orders[oid] + window + self.rng.between(-1, 1)
            if not (I64_MIN <= ts <= I64_MAX):
                ts = self.now
        self.send(
            self.order_step(
                iid,
                side,
                self.rng.between(1, 50),
                self.passive_price(iid, side),
                2,
                self.strategy(),
                ts,
                None,
                oid,
            )
        )

    def do_order_burst(self) -> None:
        iid = self.instrument()
        self.ensure_mark(iid)
        sid = self.strategy()
        side = self.rng.below(2)
        for _ in range(self.rng.between(3, 7)):
            mid = self.ref_mid(iid)
            price = max(1, mid - 3 if side == 0 else mid + 3)
            self.send(self.order_step(iid, side, self.rng.between(1, 5), price, 2, sid, self.now))

    def do_order_unknown(self) -> None:
        iid = self.rng.pick((99, 0, U32_MAX, 6, 102))
        side = self.rng.below(2)
        self.send(
            self.order_step(
                iid,
                side,
                self.rng.between(1, 50),
                self.rng.between(1, 5000),
                2,
                self.rng.pick(EXOTIC_STRATEGIES + ("S9",)),
                self.ts(),
                self.rng.pick((0, 9, 65535, 1)),
            )
        )

    # -------------------------------------------------------------- market

    def marked(self) -> list[int]:
        out = list(self.profile.instruments)
        for iid in self.profile.instruments:
            pair = FX_PAIR.get(INSTRUMENTS[str(iid)]["quote_ccy"])
            if pair is not None and pair not in out:
                out.append(pair)
        return out

    def do_market(self) -> None:
        iid = self.rng.pick(self.marked())
        mid = self.ref_mid(iid)
        step = max(1, mid // 400)
        self.emit(self.market_step(iid, max(3, mid + self.rng.between(-step, step)), self.now))

    def do_market_jump(self) -> None:
        held = sorted(i for i, p in self.eng._positions.items() if p != 0 and str(i) in INSTRUMENTS)
        iid = self.rng.pick(held) if held else self.instrument()
        mid = self.ref_mid(iid)
        pos = self.eng._positions.get(iid, 0)
        move = max(1, mid * self.rng.between(1, 12) // 200)
        against = -1 if pos > 0 else 1
        if self.rng.pct(20):
            against = -against
        self.emit(self.market_step(iid, max(3, mid + against * move), self.now))

    def do_market_weird(self) -> None:
        pool = self.marked()
        iid = pool[-1] if self.rng.pct(60) else self.rng.pick(pool)
        mid = self.ref_mid(iid)
        md = self.eng._market.get(iid)
        t = self.timeout()
        bid, ask, ts = mid - 1, mid + 1, self.now
        which = self.rng.below(12)
        if which == 0:
            bid = self.rng.pick((0, -1, I64_MIN))
        elif which == 1:
            ask = self.rng.pick((0, -1, I64_MIN))
        elif which == 2:
            bid = ask = 0
        elif which == 3:
            bid, ask = mid + 5, mid - 5
        elif which == 4:
            bid = ask = I64_MAX
        elif which == 5:
            bid, ask = I64_MAX, 1
        elif which == 6:
            ts = (md.ts if md is not None else self.now) - self.rng.pick((1, t, 3600 * SEC))
        elif which == 7:
            ts = self.now + t + self.rng.between(-1, 1)
        elif which == 8:
            ts = self.rng.pick((self.now + 3600 * SEC, I64_MAX, I64_MAX - 1))
        elif which == 9:
            ts = self.rng.pick((I64_MIN, 0, -1))
        elif which == 10:
            iid = self.rng.pick((99, 0, U32_MAX))
        else:
            bid, ask = 1, I64_MAX - 1
        if not (I64_MIN <= ts <= I64_MAX):
            ts = self.now
        self.emit(
            {"type": "market", "instrument_id": iid, "bid_ticks": bid, "ask_ticks": ask, "ts": ts}
        )

    # --------------------------------------------------------------- fills

    def fill_step(
        self, sid: str, iid: int, side: int, qty: int, price: int, ts: int, oid: int = 0
    ) -> dict[str, Any]:
        return {
            "type": "fill",
            "strategy_id": sid,
            "instrument_id": iid,
            "order_id": oid,
            "side": side,
            "qty": qty,
            "price_ticks": price,
            "ts": ts,
        }

    def do_fill_open(self) -> None:
        opens = sorted(self.eng._open)
        if not opens:
            return self.do_fill_ext()
        oid = self.rng.pick(opens)
        r = self.eng._open[oid]
        qty = self.rng.pick(
            (
                r.qty,
                r.qty,
                max(1, r.qty - 1),
                r.qty + 1,
                self.rng.between(1, max(1, min(r.qty, 1 << 40))),
            )
        )
        if qty > I64_MAX:
            qty = r.qty
        price = r.price_ticks if r.price_ticks > 0 else self.ref_mid(r.instrument_id)
        side = r.side if self.rng.pct(92) else 1 - r.side
        sid = self.owner.get(oid, "S1") if self.rng.pct(92) else self.strategy()
        self.emit(self.fill_step(sid, r.instrument_id, side, qty, price, self.ts(), oid))

    def ext_fill(self) -> dict[str, Any]:
        iid = self.instrument()
        mid = self.ref_mid(iid)
        cap = self.limits.max_position_qty if self.limits else 1000
        qty = self.rng.between(1, max(1, cap // self.rng.pick((2, 5, 20, 100))))
        price = max(1, mid + self.rng.between(-max(1, mid // 100), max(1, mid // 100)))
        return self.fill_step(self.strategy(), iid, self.rng.below(2), qty, price, self.ts())

    def do_fill_ext(self) -> None:
        self.emit(self.ext_fill())

    def bad_fill(self) -> dict[str, Any]:
        f = self.ext_fill()
        which = self.rng.below(6)
        if which == 0:
            f["qty"] = self.rng.pick((0, -5, I64_MIN))
        elif which == 1:
            f["side"] = self.rng.pick((2, 255))
        elif which == 2:
            f["price_ticks"] = self.rng.pick((0, -1, I64_MIN))
        elif which == 3:
            f["instrument_id"] = self.rng.pick((99, 0, U32_MAX))
        elif which == 4:
            f["order_id"] = self.rng.pick(BIG_ORDER_IDS + (777_777,))
        else:
            f["strategy_id"] = self.rng.pick(EXOTIC_STRATEGIES)
            f["order_id"] = self.rng.pick(BIG_ORDER_IDS)
            f["qty"] = 0
        return f

    def do_fill_bad(self) -> None:
        self.emit(self.bad_fill())

    def do_fill_huge(self) -> None:
        f = self.ext_fill()
        which = self.rng.below(5)
        if which == 0:
            f["qty"] = self.rng.pick((I64_MAX, I64_MAX - 1))
        elif which == 1:
            f["qty"] = 1 << 62
        elif which == 2:
            f["price_ticks"] = self.rng.pick((I64_MAX, 1 << 62, 1 << 53))
        elif which == 3:
            # the exact amount that would take the aggregate position to +/- i64::MAX (+1)
            pos = self.eng._positions.get(f["instrument_id"], 0)
            room = I64_MAX - abs(pos) if (pos >= 0) == (f["side"] == 0) else I64_MAX
            f["qty"] = max(1, min(I64_MAX, room + self.rng.between(-1, 1)))
        else:
            f["qty"], f["order_id"] = I64_MAX, self.rng.pick(BIG_ORDER_IDS)
        self.emit(f)

    def do_cancel(self) -> None:
        opens = sorted(self.eng._open)
        if opens and self.rng.pct(85):
            oid = self.rng.pick(opens)
        else:
            oid = self.rng.pick(BIG_ORDER_IDS + (0, 424242))
        self.emit({"type": "cancel", "order_id": oid})

    # ------------------------------------------------------ kills / limits

    def kill_target(self) -> tuple[str, str]:
        scope = self.rng.weighted(((2, "GLOBAL"), (4, "STRATEGY"), (3, "INSTRUMENT"), (3, "VENUE")))
        if scope == "GLOBAL":
            return scope, "" if self.rng.pct(90) else "ignored"
        if scope == "STRATEGY":
            return scope, self.strategy()
        if scope == "INSTRUMENT":
            pool = [str(i) for i in self.profile.instruments] + ["99", "+1", "0002", "4294967295"]
            return scope, self.rng.pick(pool)
        return scope, self.rng.pick(("1", "2", "3", "0", "65535", "+2", "001"))

    def kill_step(self, kind: str, scope: str, scope_id: str) -> dict[str, Any]:
        return {
            "type": kind,
            "scope": scope,
            "scope_id": scope_id,
            "ts": self.ts(),
            "reason": self.text(),
        }

    def do_kill(self) -> None:
        self.emit(self.kill_step("kill", *self.kill_target()))

    def engaged(self) -> list[tuple[str, str]]:
        e = self.eng
        out: list[tuple[str, str]] = [("GLOBAL", "")] if e._kill_global else []
        out += [("STRATEGY", s) for s in sorted(e._kill_strategies) if e._kill_strategies[s]]
        out += [
            ("INSTRUMENT", str(i)) for i in sorted(e._kill_instruments) if e._kill_instruments[i]
        ]
        out += [("VENUE", str(v)) for v in sorted(e._kill_venues) if e._kill_venues[v]]
        return out

    def do_unkill(self) -> None:
        engaged = self.engaged()
        if engaged and self.rng.pct(75):
            scope, scope_id = self.rng.pick(engaged)
        else:
            scope, scope_id = self.kill_target()
        self.emit(self.kill_step("unkill", scope, scope_id))

    def do_kill_bad(self) -> None:
        scope = self.rng.pick(("INSTRUMENT", "VENUE"))
        scope_id = self.rng.pick(BAD_SCOPE_IDS + ("65536",))
        self.emit(self.kill_step(self.rng.pick(("kill", "unkill")), scope, scope_id))

    def override_step(self, scope: str, scope_id: str, new_limit: Any) -> dict[str, Any]:
        return {
            "type": "override_loss",
            "scope": scope,
            "scope_id": scope_id,
            "new_limit": new_limit,
            "ts": self.ts(),
            "approver": self.text(),
        }

    def do_override(self) -> None:
        if self.rng.pct(40):
            scope, scope_id = "GLOBAL", ""
        else:
            scope, scope_id = "STRATEGY", self.strategy()
        self.emit(self.override_step(scope, scope_id, self.rng.pick(LOSS_LIMITS)))

    def do_override_bad(self) -> None:
        if self.rng.pct(50):
            scope, scope_id = self.rng.pick((("GLOBAL", ""), ("STRATEGY", "S1")))
            limit: Any = self.rng.pick((0, 0.0, -0.0, -1.0, -1e-300))
        else:
            scope, scope_id = self.rng.pick((("INSTRUMENT", "1"), ("VENUE", "1"), ("VENUE", "x")))
            limit = self.rng.pick(LOSS_LIMITS)
        self.emit(self.override_step(scope, scope_id, limit))

    def do_roll(self) -> None:
        self.emit({"type": "roll_session", "ts": self.ts(), "reason": self.text()})

    def do_venue(self) -> None:
        kind = self.rng.pick(("venue_down", "venue_down", "venue_up"))
        venue = self.rng.weighted(((5, 1), (4, 2), (2, 3), (1, 0), (1, 65535)))
        self.emit({"type": kind, "venue_id": venue, "ts": self.ts()})

    def do_gap(self) -> None:
        iid = self.instrument() if self.rng.pct(85) else self.rng.pick((99, 0, U32_MAX))
        kind = "gap" if self.rng.pct(75) else "recover"
        self.emit({"type": kind, "instrument_id": iid, "ts": self.ts()})

    def do_bootstrap(self) -> None:
        fills = []
        for _ in range(self.rng.between(0, 5)):
            f = self.bad_fill() if self.rng.pct(25) else self.ext_fill()
            del f["type"]
            fills.append(f)
        self.emit({"type": "bootstrap", "ts": self.ts(), "fills": fills})

    def do_restore(self) -> None:
        if self.limits is None:
            return self.do_market()
        self.emit({"type": "restore", "ts": self.ts()})

    # -------------------------------------------------------------- recipes

    def do_recipe(self) -> None:
        """A short targeted sequence for a branch random flow rarely reaches."""
        self.rng.pick(
            (
                self.recipe_loss_latch,
                self.recipe_flat_unvaluable,
                self.recipe_open_unvaluable,
                self.recipe_sor,
                self.recipe_future_mark,
                self.recipe_position_cap,
                self.recipe_ts_overflow,
                self.recipe_position_overflow,
                self.recipe_open_unconvertible,
                self.recipe_global_loss,
                self.recipe_rate_age,
            )
        )()

    def recipe_ts_overflow(self) -> None:
        """An order stamped where ``ts - mark_ts`` leaves i64 (and one tick
        inside the domain)."""
        iid = self.instrument()
        self.ensure_mark(iid)
        md = self.eng._market.get(iid)
        if md is None or md.ts <= 0:
            return self.do_market()
        sid = self.rng.pick(self.profile.strategies)
        self.clear_path(sid, iid)
        for d in (self.rng.pick((-1, -1, I64_MIN)), 0):
            ts = I64_MIN + md.ts + d
            self.send(self.order_step(iid, 0, 1, self.passive_price(iid, 0), 2, sid, ts, 1))

    def recipe_position_overflow(self) -> None:
        """A position at the edge of the symmetric i64 domain: the buy
        projection overflows, the fill that would leave the domain latches
        GLOBAL, and the position is then flattened at the same price."""
        iid = self.instrument()
        self.ensure_mark(iid)
        if self.mid(iid) is None:
            return self.do_market()
        self.flatten(iid)
        if any(lot.pos != 0 for (_, i), lot in self.eng._lots.items() if i == iid):
            return
        sid = self.rng.pick(self.profile.strategies)
        self.clear_path(sid, iid)
        side = self.rng.below(2)
        mid = self.ref_mid(iid)
        room = self.rng.between(0, 3)
        self.emit(self.fill_step(sid, iid, side, I64_MAX - room, mid, self.now))
        for qty in (room + 1, room):
            if qty > 0:
                self.send(
                    self.order_step(
                        iid, side, qty, self.passive_price(iid, side), 2, sid, self.now, 1
                    )
                )
        other = 1 - side
        self.send(
            self.order_step(iid, other, 1, self.passive_price(iid, other), 2, sid, self.now, 1)
        )
        if self.rng.pct(50):
            self.emit(self.fill_step(sid, iid, side, room + 1, mid, self.now, self.new_oid()))
            self.emit(self.kill_step("unkill", "GLOBAL", ""))
        self.emit(self.fill_step(sid, iid, other, I64_MAX - room, mid, self.now))

    def recipe_rate_age(self) -> None:
        """A fresh instrument mark with a conversion rate at the staleness
        boundary, or stamped at the future-stamp boundary."""
        fx = [
            i
            for i in self.profile.instruments
            if INSTRUMENTS[str(i)]["quote_ccy"] in FX_PAIR
            and FX_PAIR[INSTRUMENTS[str(i)]["quote_ccy"]] != i
        ]
        if not fx:
            return self.do_market()
        iid = self.rng.pick(fx)
        pair = FX_PAIR[INSTRUMENTS[str(iid)]["quote_ccy"]]
        t = self.timeout()
        if t > 3600 * SEC:
            return self.do_market()
        d = self.rng.between(-1, 1)
        sid = self.rng.pick(self.profile.strategies)
        self.clear_path(sid, iid)
        md = self.eng._market.get(pair)
        if md is not None and self.mid(pair) is not None and self.rng.pct(60):
            if md.ts + t + d < self.now:
                return self.do_market()
            self.now = md.ts + t + d
        else:
            ts = self.eng._event_clock(self.now) + t + max(d, 0)
            if md is not None and md.ts > ts:
                return self.do_market()
            self.emit(self.market_step(pair, self.ref_mid(pair), ts))
        self.emit(self.market_step(iid, self.ref_mid(iid), self.now))
        self.send(self.order_step(iid, 0, 1, self.passive_price(iid, 0), 2, sid, self.now, 1))

    def recipe_open_unconvertible(self) -> None:
        """A priced order rests in a non-reporting-currency instrument whose
        position is flat, then the conversion pair loses its mark."""
        fx = [
            i
            for i in self.profile.instruments
            if INSTRUMENTS[str(i)]["quote_ccy"] in FX_PAIR
            and FX_PAIR[INSTRUMENTS[str(i)]["quote_ccy"]] != i
        ]
        usd = [i for i in self.profile.instruments if INSTRUMENTS[str(i)]["quote_ccy"] == "USD"]
        if not fx or not usd:
            return self.do_market()
        iid, other = self.rng.pick(fx), usd[0]
        self.flatten(iid)
        if self.eng._positions.get(iid, 0) != 0:
            return
        pair = FX_PAIR[INSTRUMENTS[str(iid)]["quote_ccy"]]
        self.ensure_mark(iid)
        self.ensure_mark(other)
        sid = self.rng.pick(self.profile.strategies)
        self.clear_path(sid, iid)
        self.clear_path(sid, other)
        side = self.rng.below(2)
        step = self.send(
            self.order_step(iid, side, 1, self.passive_price(iid, side), 2, sid, self.now, 1)
        )
        pair_mid = self.ref_mid(pair)
        self.emit(
            {
                "type": "market",
                "instrument_id": pair,
                "bid_ticks": 0,
                "ask_ticks": pair_mid + 1,
                "ts": self.now,
            }
        )
        self.send(self.order_step(other, 0, 1, self.passive_price(other, 0), 2, sid, self.now, 1))
        self.emit({"type": "cancel", "order_id": step["order"]["order_id"]})
        self.emit(self.market_step(pair, pair_mid, self.now))

    def recipe_global_loss(self) -> None:
        """One strategy loses the firm-wide limit; GLOBAL is cleared without
        an override and another strategy's order meets check 21."""
        lim = self.limits
        if lim is None or len(self.profile.strategies) < 2:
            return self.do_market()
        iid = self.profile.instruments[0]
        self.ensure_mark(iid)
        loser, other = self.profile.strategies[0], self.profile.strategies[1]
        mid = self.ref_mid(iid)
        limit = self.eng._effective_global_loss(lim)
        pnl = self.eng.global_daily_pnl()
        if pnl is None or self.mid(iid) is None:
            return self.do_market()
        drop = max(1, mid // 10)
        qty = int((pnl + limit) / (drop * self.unit_value(iid, 1))) + self.rng.between(0, 2)
        if not (0 < qty < 1 << 40):
            return self.do_market()
        self.emit(self.fill_step(loser, iid, 0, qty, mid, self.now))
        self.now += MS
        self.emit(self.market_step(iid, mid - drop - self.rng.between(0, 1), self.now))
        self.emit(self.kill_step("unkill", "GLOBAL", ""))
        self.emit(self.kill_step("unkill", "STRATEGY", other))
        self.send(self.order_step(iid, 1, 1, self.passive_price(iid, 1), 2, other, self.now, 1))

    def clear_path(self, sid: str, iid: int) -> None:
        """Clear whatever kill, gap or disconnect would stop an order of
        ``sid`` in ``iid`` on venue 1 before the check a recipe aims at."""
        e = self.eng
        if e._kill_global:
            self.emit(self.kill_step("unkill", "GLOBAL", ""))
        if e._kill_strategies.get(sid, False):
            self.emit(self.kill_step("unkill", "STRATEGY", sid))
        if e._kill_instruments.get(iid, False):
            self.emit(self.kill_step("unkill", "INSTRUMENT", str(iid)))
        if e._kill_venues.get(1, False):
            self.emit(self.kill_step("unkill", "VENUE", "1"))
        if e._venues_down.get(1, False):
            self.emit({"type": "venue_up", "venue_id": 1, "ts": self.now})
        md = e._market.get(iid)
        if md is not None and md.gated:
            self.emit({"type": "recover", "instrument_id": iid, "ts": self.now})

    def flatten(self, iid: int) -> None:
        """Close every lot of ``iid`` at the current mid."""
        mid = self.ref_mid(iid)
        for (sid, i), lot in sorted(self.eng._lots.items()):
            if i == iid and lot.pos != 0 and abs(lot.pos) <= I64_MAX:
                side = 1 if lot.pos > 0 else 0
                self.emit(self.fill_step(sid, iid, side, abs(lot.pos), mid, self.now))

    def recipe_loss_latch(self) -> None:
        """Build a position, then move the mark to the loss limit +/- a tick."""
        lim = self.limits
        if lim is None:
            return self.do_market()
        iid = self.instrument()
        self.ensure_mark(iid)
        sid = self.rng.pick(self.profile.strategies)
        mid = self.ref_mid(iid)
        side = self.rng.below(2)
        qty = self.rng.between(
            max(1, lim.max_position_qty // 10), max(1, lim.max_position_qty // 2)
        )
        self.emit(self.fill_step(sid, iid, side, qty, mid, self.now))
        pnl = self.eng.strategy_daily_pnl(sid)
        lot = self.eng._lots.get((sid, iid))
        if pnl is None or lot is None or lot.pos == 0:
            return
        limit = self.eng._effective_strategy_loss(lim, sid)
        per_tick = float(abs(lot.pos)) * self.unit_value(iid, 1)
        ticks = int((pnl + limit) / per_tick) + self.rng.between(-1, 1)
        new_mid = self.ref_mid(iid) + (-ticks if lot.pos > 0 else ticks)
        if 3 < new_mid < 1 << 40:
            self.now += MS
            self.emit(
                {
                    "type": "market",
                    "instrument_id": iid,
                    "bid_ticks": new_mid - 1,
                    "ask_ticks": new_mid + 1,
                    "ts": self.now,
                }
            )
        self.send(self.order_step(iid, side, 1, self.passive_price(iid, side), 2, sid, self.now))
        if self.rng.pct(60):
            self.emit(self.kill_step("unkill", "STRATEGY", sid))
            self.send(
                self.order_step(iid, side, 1, self.passive_price(iid, side), 2, sid, self.now)
            )

    def recipe_flat_unvaluable(self) -> None:
        """Two strategies flat in aggregate, then the mark goes one-sided:
        gross/net see nothing, daily P&L is undeterminable (check 21)."""
        if len(self.profile.instruments) < 2 or len(self.profile.strategies) < 2:
            return self.do_market()
        iid, other = self.profile.instruments[0], self.profile.instruments[1]
        net = self.eng._positions.get(iid, 0)
        a, b = self.profile.strategies[0], self.profile.strategies[1]
        qty = self.rng.between(1, 50)
        mid = self.ref_mid(iid)
        self.emit(self.fill_step(a, iid, 0, qty + max(0, -net), mid, self.now))
        self.emit(self.fill_step(b, iid, 1, qty + max(0, net), mid, self.now))
        self.emit(
            {
                "type": "market",
                "instrument_id": iid,
                "bid_ticks": mid - 1,
                "ask_ticks": 0,
                "ts": self.now,
            }
        )
        self.ensure_mark(other)
        self.send(self.order_step(other, 0, 1, self.passive_price(other, 0), 2, a, self.now, 1))
        self.emit(self.market_step(iid, mid, self.now))

    def recipe_open_unvaluable(self) -> None:
        """An unpriced order rests, then its instrument loses its mark."""
        if len(self.profile.instruments) < 2:
            return self.do_market()
        iid, other = self.profile.instruments[-1], self.profile.instruments[0]
        self.ensure_mark(iid)
        self.ensure_mark(other)
        sid = self.rng.pick(self.profile.strategies)
        self.clear_path(sid, iid)
        self.clear_path(sid, other)
        side = self.rng.below(2)
        step = self.send(self.order_step(iid, side, 1, 0, self.rng.pick((1, 6)), sid, self.now, 1))
        mid = self.ref_mid(iid)
        self.emit(
            {
                "type": "market",
                "instrument_id": iid,
                "bid_ticks": 0,
                "ask_ticks": mid + 1,
                "ts": self.now,
            }
        )
        self.send(self.order_step(other, 0, 1, self.passive_price(other, 0), 2, sid, self.now, 1))
        self.emit({"type": "cancel", "order_id": step["order"]["order_id"]})
        self.emit(self.market_step(iid, mid, self.now))

    def recipe_sor(self) -> None:
        """Venue-0 (SOR) orders around venue kills and disconnects."""
        iid = self.instrument()
        self.ensure_mark(iid)
        sid = self.rng.pick(self.profile.strategies)
        if self.rng.pct(50):
            v = self.rng.pick((1, 2, 3))
            self.emit(self.kill_step("kill", "VENUE", str(v)))
            self.send(self.order_step(iid, 0, 1, self.passive_price(iid, 0), 2, sid, self.now, 0))
            self.emit(self.kill_step("unkill", "VENUE", str(v)))
        else:
            known = sorted(self.eng._venues_down) or [1]
            for v in known:
                self.emit({"type": "venue_down", "venue_id": v, "ts": self.now})
            self.send(self.order_step(iid, 0, 1, self.passive_price(iid, 0), 2, sid, self.now, 0))
            self.emit({"type": "venue_up", "venue_id": self.rng.pick(known), "ts": self.now})
        self.send(self.order_step(iid, 0, 1, self.passive_price(iid, 0), 2, sid, self.now, 0))

    def recipe_future_mark(self) -> None:
        """A mark stamped at the future-stamp boundary of the event clock."""
        iid = self.profile.instruments[-1]
        clock = self.eng._event_clock(self.now)
        ts = clock + self.timeout() + self.rng.between(0, 1)
        md = self.eng._market.get(iid)
        if not (I64_MIN <= ts <= I64_MAX) or (md is not None and md.ts > ts):
            return self.do_market()
        self.emit(self.market_step(iid, self.ref_mid(iid), ts))
        sid = self.rng.pick(self.profile.strategies)
        self.send(self.order_step(iid, 0, 1, self.passive_price(iid, 0), 2, sid, self.now, 1))

    def recipe_position_cap(self) -> None:
        """Walk the cheapest instrument's position to the cap with fills."""
        lim = self.limits
        if lim is None:
            return self.do_market()
        iid = min(self.profile.instruments, key=lambda i: self.unit_value(i, self.ref_mid(i)))
        self.ensure_mark(iid)
        sid = self.rng.pick(self.profile.strategies)
        pos = self.eng._positions.get(iid, 0)
        side = 0 if pos >= 0 else 1
        room = lim.max_position_qty - abs(pos) - self.rng.between(0, 3)
        if 0 < room < 1 << 40:
            self.emit(self.fill_step(sid, iid, side, room, self.ref_mid(iid), self.now))
        for d in (2, 3, 4, 5):
            self.send(
                self.order_step(
                    iid, side, d - 1, self.passive_price(iid, side), 2, sid, self.now, 1
                )
            )
            self.now += 3 * MS

    # ------------------------------------------------------------ the loop

    def maintain(self) -> None:
        """State-aware housekeeping so a script recovers from a rejection
        state instead of bouncing off it for the rest of its length."""
        e, lim = self.eng, self.limits
        if not e.is_bootstrapped() and self.rng.pct(7):
            self.do_bootstrap()
        if e._kill_global and self.rng.pct(40):
            pnl = e.global_daily_pnl()
            if lim is not None and pnl is not None and pnl <= -e._effective_global_loss(lim):
                how = self.rng.below(3)
                if how == 0:
                    self.emit(self.override_step("GLOBAL", "", round(-pnl * 2.0, 2) + 1000.0))
                elif how == 1:
                    self.do_roll()
            self.emit(self.kill_step("unkill", "GLOBAL", ""))
        for sid in self.profile.strategies:
            if e._kill_strategies.get(sid, False) and self.rng.pct(22):
                pnl = e.strategy_daily_pnl(sid)
                if (
                    lim is not None
                    and pnl is not None
                    and pnl <= -e._effective_strategy_loss(lim, sid)
                ):
                    how = self.rng.below(3)
                    if how == 0:
                        self.emit(self.override_step("STRATEGY", sid, round(-pnl * 2.0, 2) + 500.0))
                    elif how == 1:
                        self.do_roll()
                self.emit(self.kill_step("unkill", "STRATEGY", sid))
        for scope, scope_id in self.engaged():
            if scope in ("INSTRUMENT", "VENUE") and self.rng.pct(20):
                self.emit(self.kill_step("unkill", scope, scope_id))
        for v in sorted(e._venues_down):
            if e._venues_down[v] and self.rng.pct(25):
                self.emit({"type": "venue_up", "venue_id": v, "ts": self.now})
        for iid in sorted(e._market):
            md = e._market[iid]
            if md.gated and self.rng.pct(12):
                self.emit({"type": "recover", "instrument_id": iid, "ts": self.now})
            if iid in self.profile.instruments and 0 < md.ts - self.now < 86_400 * SEC:
                if self.rng.pct(25):
                    self.now = md.ts + MS

    def generate(self) -> dict[str, Any]:
        p = self.profile
        while len(self.steps) < p.steps:
            self.maintain()
            getattr(self, "do_" + self.rng.weighted(p.weights))()
            self.tick()
        n = len(self.steps)
        if p.cuts and self.limits is not None and n > 2:
            cuts: list[int] = []
            while len(cuts) < min(p.cuts, n - 1):
                c = self.rng.below(n - 1)
                if c not in cuts:
                    cuts.append(c)
            self.script["cuts"] = sorted(cuts)
        return self.script


def build_script(profile: str, seed: int, base: dict[str, Any], name: str = "") -> dict[str, Any]:
    """Generate one script (expectations recorded by the Python oracle)."""
    return Generator(seed, PROFILES[profile], base, name).generate()


def seeds_from(master: int, count: int) -> list[tuple[str, int]]:
    """``count`` (profile, seed) pairs derived from one master seed: profiles
    in rotation, seeds from a SplitMix64 stream (53-bit, exact in JSON)."""
    rng = SplitMix64(master)
    names = list(PROFILES)
    return [(names[i % len(names)], rng.next_u64() >> 11) for i in range(count)]


# ------------------------------------------------------------------- coverage

#: Every reason-string branch of the engines: ``(rule_id, fragment, ...)`` — a
#: record of that rule whose reason contains every fragment.
#: ``coverage`` reports which of them a corpus reaches.
BRANCHES: tuple[tuple[str, ...], ...] = (
    ("ALLOW", ""),
    ("CONFIG_MISSING", "fail-closed: invalid argument: risk.json:"),
    ("NOT_BOOTSTRAPPED", "positions not bootstrapped (fail-closed)"),
    ("KILL_GLOBAL", "global kill switch engaged"),
    ("KILL_STRATEGY", "kill switch engaged"),
    ("KILL_INSTRUMENT", "kill switch engaged"),
    ("KILL_VENUE", "kill switch engaged"),
    ("KILL_VENUE", "venue 0 (SOR) order rejected: venue"),
    ("MALFORMED_ORDER", "side must be 0 or 1:"),
    ("MALFORMED_ORDER", "unknown order_type:"),
    ("MALFORMED_ORDER", "qty must be > 0:"),
    ("MALFORMED_ORDER", "urgency must be in [0, 1]:"),
    ("MALFORMED_ORDER", "MARKET order must carry price_ticks 0:"),
    ("MALFORMED_ORDER", "LIMIT order needs price_ticks > 0:"),
    ("MALFORMED_ORDER", "price_ticks must be >= 0:"),
    ("MALFORMED_ORDER", "PEG/MID orders carry price_ticks 0:"),
    ("MALFORMED_ORDER", "timestamp arithmetic overflows i64 (fail-closed)"),
    ("MALFORMED_ORDER", "projected position overflows i64 (fail-closed)"),
    ("UNKNOWN_INSTRUMENT", "no reference data for instrument"),
    ("DUPLICATE_ORDER_ID", "already used at ts"),
    ("VENUE_DISCONNECTED", "is disconnected"),
    ("VENUE_DISCONNECTED", "venue 0 (SOR) order rejected: every known venue is disconnected"),
    ("SEQUENCE_GAP", "feed has an unrecovered gap"),
    ("STALE_PRICE", "no reference price for instrument"),
    ("STALE_PRICE", "reference price age"),
    ("STALE_PRICE", "ahead of the latest order event time"),
    ("FAT_FINGER_QTY", "exceeds max_order_qty"),
    ("FX_RATE_MISSING", "no conversion rate for"),
    ("FX_RATE_MISSING", "ns exceeds"),
    ("FX_RATE_MISSING", "ahead of the latest order event time"),
    ("FX_RATE_MISSING", "global daily pnl undeterminable"),
    ("FX_RATE_MISSING", "strategy daily pnl undeterminable"),
    ("FAT_FINGER_NOTIONAL", "exceeds max_order_notional"),
    ("PRICE_BAND", "bps from mid, band"),
    ("RATE_THROTTLE", "orders/s (burst"),
    ("SELF_MATCH", "would cross own open order"),
    ("POSITION_LIMIT", "exceeds max_position_qty"),
    ("INSTRUMENT_NOTIONAL", "exceeds max_instrument_notional"),
    ("GROSS_NOTIONAL", "position in instrument", "has no mark price (fail-closed)"),
    ("GROSS_NOTIONAL", "position in instrument", "conversion rate (fail-closed)"),
    ("GROSS_NOTIONAL", "open order", "has no mark price (fail-closed)"),
    ("GROSS_NOTIONAL", "open order in instrument", "conversion rate (fail-closed)"),
    ("GROSS_NOTIONAL", "exceeds max_gross_notional"),
    ("NET_NOTIONAL", "exceeds max_net_notional"),
    ("DAILY_LOSS", "at daily loss limit"),
    ("DAILY_LOSS", "breaches daily loss limit"),
    ("STRATEGY_LOSS", "at loss limit"),
    ("STRATEGY_LOSS", "breaches loss limit"),
    ("KILL_SWITCH_ENGAGED", ""),
    ("KILL_SWITCH_ENGAGED", "overflows i64 position accounting (fail-closed)"),
    ("KILL_SWITCH_CLEARED", ""),
    ("MALFORMED_KILL", "escalated to GLOBAL (fail-closed)"),
    ("MALFORMED_KILL", "nothing cleared (fail-closed)"),
    ("MALFORMED_FILL", "qty must be > 0:"),
    ("MALFORMED_FILL", "side must be 0 or 1:"),
    ("MALFORMED_FILL", "price_ticks must be > 0:"),
    ("MALFORMED_FILL", "no reference data for instrument"),
    ("VENUE_DISCONNECT", "disconnected"),
    ("VENUE_RECONNECT", "reconnected"),
    ("LOSS_LIMIT_OVERRIDE", "daily loss limit"),
    ("SESSION_ROLLED", ""),
    ("BOOTSTRAP_COMPLETE", "drop-copy fills"),
    ("STATE_RESTORED", "restored snapshot v1"),
)
#: Branches no input can reach, with the reason (reported, never required).
UNREACHABLE: dict[tuple[str, ...], str] = {
    ("FX_RATE_MISSING", "strategy daily pnl undeterminable"): (
        "check 21 rejects first: a strategy's lots and buckets are a subset of the firm's"
    ),
}


def _branch_hit(branch: tuple[str, ...], events: Sequence[dict[str, Any]]) -> bool:
    rule, parts = branch[0], branch[1:]
    return any(e["rule_id"] == rule and all(p in e["reason"] for p in parts) for e in events)


def coverage(corpus: Sequence[tuple[dict[str, Any], str, str]]) -> dict[str, Any]:
    """Step-type counts, rule-id counts and reason-branch coverage of a corpus."""
    step_types = dict.fromkeys(STEP_TYPES, 0)
    rules: dict[str, int] = {}
    events: list[dict[str, Any]] = []
    for script, audit, _ in corpus:
        for step in script["steps"]:
            step_types[step["type"]] += 1
        for line in audit_lines(audit):
            ev = json.loads(line)
            events.append(ev)
            rules[ev["rule_id"]] = rules.get(ev["rule_id"], 0) + 1
    reached = [b for b in BRANCHES if _branch_hit(b, events)]
    missed = [b for b in BRANCHES if b not in reached]
    return {
        "scripts": len(corpus),
        "steps": sum(step_types.values()),
        "audit_lines": len(events),
        "step_types": step_types,
        "rules": {k: rules[k] for k in sorted(rules)},
        "branches_reached": reached,
        "branches_missed": missed,
    }


def branch_label(branch: tuple[str, ...]) -> str:
    return f"{branch[0]}: {' ... '.join(branch[1:])}".rstrip(": ")


# -------------------------------------------------------------------- shrinker


def subset_script(
    script: dict[str, Any], steps: list[dict[str, Any]], base: dict[str, Any]
) -> dict[str, Any] | None:
    """``script`` reduced to ``steps`` and re-recorded by the oracle, or
    ``None`` when the reduced script is not replayable (a ``restore`` of a
    state the oracle refuses, for instance)."""
    cand = {k: v for k, v in script.items() if k != "steps"}
    cand["steps"] = copy.deepcopy(steps)
    cand = strip_expectations(cand)
    n = len(steps)
    if script.get("cuts"):
        cand["cuts"] = list(range(n - 1)) if n <= 40 else sorted({n // 4, n // 2, 3 * n // 4})
    try:
        record(cand, base)
    except (ValueError, OverflowError):
        return None
    return cand


def shrink(
    script: dict[str, Any],
    base: dict[str, Any],
    diverges: Callable[[list[dict[str, Any]]], list[bool]],
    max_rounds: int = 400,
) -> dict[str, Any]:
    """Delta debugging (ddmin) over the step list: repeatedly drop a chunk of
    steps while ``diverges`` still reports the divergence, halving the chunk
    size when no chunk can go. ``diverges`` judges a whole batch of candidate
    scripts at once (one start of each external engine per round)."""
    steps = list(script["steps"])
    n = 2
    for _ in range(max_rounds):
        if len(steps) < 2:
            break
        chunk = -(-len(steps) // n)
        cands: list[dict[str, Any]] = []
        for start in range(0, len(steps), chunk):
            cand = subset_script(script, steps[:start] + steps[start + chunk :], base)
            if cand is not None:
                cand["name"] = f"{script['name']}_c{len(cands):03d}"
                cands.append(cand)
        verdicts = diverges(cands) if cands else []
        hit = next((c for c, bad in zip(cands, verdicts, strict=True) if bad), None)
        if hit is not None:
            steps = strip_expectations(hit)["steps"]
            n = max(n - 1, 2)
        elif chunk == 1:
            break
        else:
            n = min(len(steps), n * 2)
    out = subset_script(script, steps, base)
    assert out is not None
    out["name"] = f"{script['name']}_min"
    return out


# ------------------------------------------------- external-engine protocol
#
# The nightly job runs each engine over a directory of scripts and reads a
# report back. All three replays speak the same two environment variables:
#
#   IAP_RISK_FUZZ_DIR   directory of scripts to replay instead of the
#                       committed corpus
#   IAP_RISK_FUZZ_OUT   directory to write ``<lang>.tsv`` into — one line per
#                       script, ``name<TAB>OK`` or ``name<TAB>DIVERGED<TAB>why``
#                       — plus ``<name>.<lang>.audit.jsonl`` and
#                       ``<name>.<lang>.snapshot.json`` (the audit and final
#                       state the engine actually produced). With it set a
#                       divergence is reported, not raised.


def one_line(text: str) -> str:
    return " ".join(str(text).split())[:2000]


def replay_report(
    corpus_dir: Path, out_dir: Path, lang: str, engine_cls: type[RiskEngine] = RiskEngine
) -> list[tuple[str, str]]:
    """Replay every script of ``corpus_dir`` and write ``<lang>.tsv`` (the
    Python side of the protocol; ``engine_cls`` lets a test plant a bug)."""
    base = load_base_config()
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[str, str]] = []
    for script, audit, snapshot in load_corpus(corpus_dir):
        name = script["name"]
        try:
            verify_script(script, base, audit, snapshot, engine_cls)
            why = ""
        except (Divergence, ValueError, OverflowError, KeyError, IndexError) as e:
            why = one_line(f"{type(e).__name__}: {e}") or "diverged"
        try:
            got, _, state = run_script(script, base, mode="run", engine_cls=engine_cls)
        except (ValueError, OverflowError, KeyError, IndexError):
            got = state = ""
        (out_dir / f"{name}.{lang}.audit.jsonl").write_bytes(got.encode("utf-8"))
        (out_dir / f"{name}.{lang}.snapshot.json").write_bytes(state.encode("utf-8"))
        rows.append((name, why))
    text = "".join(f"{n}\tDIVERGED\t{w}\n" if w else f"{n}\tOK\n" for n, w in rows)
    (out_dir / f"{lang}.tsv").write_bytes(text.encode("utf-8"))
    return rows


def read_report(path: Path) -> dict[str, str]:
    """``{script name: "" | reason}`` from a ``<lang>.tsv`` report."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        out[parts[0]] = "" if parts[1] == "OK" else (parts[2] if len(parts) > 2 else "diverged")
    return out
