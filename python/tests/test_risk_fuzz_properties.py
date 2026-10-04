"""Property tests of the hard risk engine over generated step sequences, and
tests of the fuzz tooling itself (``python/tools/risk_fuzz.py``).

These need no other engine: after ANY generated sequence the invariants below
hold. The sequences are fresh (seeds disjoint from the committed corpus), come
from every generator profile, and reach the deep states the generator aims
for — positions built up, limits at their boundaries, kills engaged and
cleared, sessions rolled, engines restored mid-run.

- a kill at a scope rejects every order in that scope until it is cleared,
  with the pinned precedence GLOBAL > STRATEGY > INSTRUMENT > VENUE, and
  nothing is ever ALLOWed while the GLOBAL kill is engaged;
- position accounting equals the sum of the fills the engine applied;
- decisions are a deterministic function of the step history;
- the audit log is append-only canonical JSON, one line per event;
- a snapshot restores to an engine with the identical state.

The kill model is derived from the AUDIT LOG alone (the engine's own record of
what it engaged and cleared), not from the engine's private state.

NaN and Infinity cannot be carried in a JSON step script, so the differential
corpus never contains them; ``test_non_finite_inputs_*`` pins the Python
engine's answers for those inputs (the Rust and Java replays carry the same
test).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest
from iap.risk import (
    Decision,
    Fill,
    OrderRequest,
    RiskEngine,
    RiskEvent,
    RiskLimits,
    Rules,
    Scope,
    instrument_refs_from_golden,
    to_canonical_json,
)
from iap.risk import engine as engine_mod

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import risk_fuzz as rf  # noqa: E402

#: Master seed of the property sequences (the committed corpus uses 31).
PROPERTY_SEED = 20261004
N_SCRIPTS = 3 * len(rf.PROFILES)
AUDIT_KEYS = ["decision", "reason", "rule_id", "scope", "scope_id", "severity", "timestamp"]


@pytest.fixture(scope="module")
def base() -> dict[str, Any]:
    return rf.load_base_config()


@pytest.fixture(scope="module")
def scripts(base) -> list[dict[str, Any]]:
    return [
        rf.build_script(profile, seed, base, f"p{k:03d}_{profile}")
        for k, (profile, seed) in enumerate(rf.seeds_from(PROPERTY_SEED, N_SCRIPTS))
    ]


class RecordingEngine(RiskEngine):
    """The real engine, remembering every fill it accepted."""

    applied: ClassVar[list[Fill]] = []

    def on_fill(self, fill: Fill) -> bool:
        ok = super().on_fill(fill)
        if ok:
            type(self).applied.append(fill)
        return ok


class KillModel:
    """Kill-switch state reconstructed from audit records only."""

    def __init__(self, engaged_at_start: bool) -> None:
        self.global_ = engaged_at_start
        self.strategies: set[str] = set()
        self.instruments: set[int] = set()
        self.venues: set[int] = set()

    def _set(self, ev: RiskEvent, on: bool) -> None:
        if ev.scope == Scope.GLOBAL:
            self.global_ = on
            return
        if ev.scope == Scope.STRATEGY:
            target: set[Any] = self.strategies
            key: Any = ev.scope_id
        else:
            target = self.instruments if ev.scope == Scope.INSTRUMENT else self.venues
            key = int(ev.scope_id.lstrip("+"))
        if on:
            target.add(key)
        else:
            target.discard(key)

    def observe(self, ev: RiskEvent) -> None:
        if ev.rule_id == Rules.KILL_SWITCH_ENGAGED:
            self._set(ev, True)
        elif ev.rule_id == Rules.KILL_SWITCH_CLEARED:
            self._set(ev, False)
        elif ev.decision == Decision.KILL and ev.rule_id == Rules.MALFORMED_KILL:
            self.global_ = True  # an unparseable kill escalates to GLOBAL
        elif ev.decision == Decision.KILL and ev.rule_id == Rules.STRATEGY_LOSS:
            self.strategies.add(ev.scope_id)
        elif ev.decision == Decision.KILL and ev.rule_id == Rules.DAILY_LOSS:
            self.global_ = True

    def expected_rule(self, order: OrderRequest) -> str | None:
        """The kill rule that must decide ``order`` (``None``: no kill applies)."""
        if self.global_:
            return Rules.KILL_GLOBAL
        if order.strategy_id in self.strategies:
            return Rules.KILL_STRATEGY
        if order.instrument_id in self.instruments:
            return Rules.KILL_INSTRUMENT
        if (order.venue_id != 0 and order.venue_id in self.venues) or (
            order.venue_id == 0 and self.venues
        ):
            return Rules.KILL_VENUE
        return None


_KILL_RULES = (Rules.KILL_GLOBAL, Rules.KILL_STRATEGY, Rules.KILL_INSTRUMENT, Rules.KILL_VENUE)
_BEFORE_KILLS = (Rules.CONFIG_MISSING, Rules.NOT_BOOTSTRAPPED)


def test_generated_sequences_reach_deep_states(scripts, base):
    """The property sequences are not shallow: every step type occurs, most
    orders get past the kill switches, and most rule ids are exercised."""
    corpus = [(s, rf.expected_outputs(s, base)[0], "") for s in scripts]
    cov = rf.coverage(corpus)
    assert all(n > 0 for n in cov["step_types"].values()), cov["step_types"]
    assert cov["rules"].get("ALLOW", 0) >= 200
    for rule in (
        Rules.POSITION_LIMIT,
        Rules.GROSS_NOTIONAL,
        Rules.NET_NOTIONAL,
        Rules.INSTRUMENT_NOTIONAL,
        Rules.DAILY_LOSS,
        Rules.STRATEGY_LOSS,
        Rules.SESSION_ROLLED,
        Rules.STATE_RESTORED,
        Rules.SELF_MATCH,
        Rules.RATE_THROTTLE,
    ):
        assert cov["rules"].get(rule, 0) > 0, rule


def test_kill_at_a_scope_rejects_every_order_in_it_until_cleared(scripts, base):
    """Both directions, from the audit log alone: while a kill applies to an
    order the decision is exactly that kill rule (never ALLOW, never a later
    check); while none applies no kill rule decides."""
    killed_orders = allowed = 0
    for script in scripts:
        runner = rf.Runner(script, base)
        limits_ok = runner.eng._limits is not None
        model = KillModel(limits_ok and runner.eng._limits.kill_switch_engaged)
        seen = 0
        for i, step in enumerate(script["steps"]):
            order = rf.parse_order(step["order"]) if step["type"] == "order" else None
            expected = model.expected_rule(order) if order is not None else None
            runner.apply(i, step, "check")
            lines = rf.audit_lines(runner.audit())
            new = [RiskEvent.from_json_line(line) for line in lines[seen:]]
            seen = len(lines)
            if order is not None:
                assert len(new) == 1, f"{script['name']} step {i}: one record per decision"
                ev = new[0]
                where = f"{script['name']} step {i}: {ev}"
                if ev.rule_id in _BEFORE_KILLS:
                    assert ev.decision == Decision.REJECT, where
                elif expected is not None:
                    assert ev.rule_id == expected and ev.decision == Decision.REJECT, where
                    killed_orders += 1
                else:
                    assert ev.rule_id not in _KILL_RULES, where
                if model.global_:
                    assert ev.decision != Decision.ALLOW, where
                allowed += ev.decision == Decision.ALLOW
            for ev in new:
                model.observe(ev)
        snap = runner.eng.snapshot()
        assert snap["kill_global"] == model.global_, script["name"]
        assert {k for k, v in snap["kill_strategies"].items() if v} == model.strategies
        assert {int(k) for k, v in snap["kill_instruments"].items() if v} == model.instruments
        assert {int(k) for k, v in snap["kill_venues"].items() if v} == model.venues
    assert killed_orders >= 100 and allowed >= 200


def test_positions_equal_the_sum_of_applied_fills(scripts, base):
    """After every step, across restores: the aggregate position of each
    instrument and each (strategy, instrument) lot is the signed sum of the
    fills ``on_fill`` returned True for — no fill is half-applied, a rejected
    fill changes nothing."""
    applied_total = 0
    for script in scripts:
        RecordingEngine.applied = []
        runner = rf.Runner(script, base, RecordingEngine)
        for i, step in enumerate(script["steps"]):
            runner.apply(i, step, "check")
            positions: dict[int, int] = {}
            lots: dict[tuple[str, int], int] = {}
            for f in RecordingEngine.applied:
                signed = f.qty if f.side == 0 else -f.qty
                positions[f.instrument_id] = positions.get(f.instrument_id, 0) + signed
                key = (f.strategy_id, f.instrument_id)
                lots[key] = lots.get(key, 0) + signed
            snap = runner.eng.snapshot()
            assert {int(k): v for k, v in snap["positions"].items()} == positions, (
                f"{script['name']} step {i}"
            )
            got = {(lot["strategy_id"], lot["instrument_id"]): lot["pos"] for lot in snap["lots"]}
            assert got == lots, f"{script['name']} step {i}"
            for iid, pos in positions.items():
                assert runner.eng.position(iid) == pos
                assert sum(v for (_, i2), v in lots.items() if i2 == iid) == pos
        applied_total += len(RecordingEngine.applied)
    assert applied_total >= 300


def test_decisions_are_a_deterministic_function_of_the_step_history(scripts, base):
    """Same steps, same bytes — and the output after the first k steps does
    not depend on what follows them."""
    for script in scripts:
        audit, _, snapshot = rf.run_script(script, base)
        again, _, snapshot2 = rf.run_script(script, base)
        assert (audit, snapshot) == (again, snapshot2), script["name"]
        n = len(script["steps"])
        for k in (n // 3, 2 * n // 3):
            prefix = dict(script, steps=script["steps"][:k], cuts=[])
            head, _, _ = rf.run_script(prefix, base)
            assert audit.startswith(head), f"{script['name']} prefix {k}"
        regenerated = rf.build_script(script["profile"], script["seed"], base, script["name"])
        assert rf.dump_script(regenerated) == rf.dump_script(script)


def test_audit_lines_are_canonical_json_in_emission_order(scripts, base):
    """Every line is one canonical JSON object with exactly the schema keys,
    round-trips through ``RiskEvent``, and the log only ever grows: the lines
    a step emits are appended after everything emitted before it, and an
    order's decision record carries the order's own timestamp."""
    lines_total = 0
    for script in scripts:
        runner = rf.Runner(script, base)
        before = ""
        for i, step in enumerate(script["steps"]):
            runner.apply(i, step, "check")
            now = runner.audit()
            assert now.startswith(before), f"{script['name']} step {i}: audit is append-only"
            new = rf.audit_lines(now[len(before) :])
            if step["type"] == "order":
                assert json.loads(new[-1])["timestamp"] == step["order"]["timestamp"]
            before = now
        assert before == "" or before.endswith("\n")
        for line in rf.audit_lines(before):
            doc = json.loads(line)
            assert list(doc) == AUDIT_KEYS
            assert to_canonical_json(doc) == line
            assert RiskEvent.from_json_line(line).to_json_line() == line
            assert 1 <= doc["decision"] <= 3 and 1 <= doc["severity"] <= 3
            lines_total += 1
    assert lines_total >= 1500


def test_a_snapshot_restores_to_the_identical_state(scripts, base):
    """At every step: restore(snapshot()) has the same snapshot, through the
    JSON text, and emits exactly one STATE_RESTORED record."""
    checked = 0
    for script in scripts:
        runner = rf.Runner(script, base)
        if runner.eng._limits is None:
            continue
        limits = RiskLimits.from_json(runner.doc)
        refs = instrument_refs_from_golden(script["instruments"])
        for i, step in enumerate(script["steps"]):
            runner.apply(i, step, "check")
            if i % 5:
                continue
            text = runner.eng.snapshot_json(pretty=False)
            restored = RiskEngine.restore(limits, refs, json.loads(text), 0)
            assert restored.snapshot_json(pretty=False) == text, f"{script['name']} step {i}"
            assert [e.rule_id for e in restored.audit()] == [Rules.STATE_RESTORED]
            checked += 1
    assert checked >= 400


def test_allowed_orders_are_tracked_open_and_rejected_ones_are_not(scripts, base):
    for script in scripts:
        runner = rf.Runner(script, base)
        for i, step in enumerate(script["steps"]):
            before = dict(runner.eng.snapshot()["open"])
            runner.apply(i, step, "check")
            if step["type"] != "order":
                continue
            after = runner.eng.snapshot()["open"]
            oid = str(step["order"]["order_id"])
            if step["expect"]["decision"] == Decision.ALLOW:
                assert after[oid]["qty"] == step["order"]["qty"]
                assert {k: v for k, v in after.items() if k != oid} == {
                    k: v for k, v in before.items() if k != oid
                }
            else:
                assert after == before, f"{script['name']} step {i}: a reject changes nothing open"


# --------------------------------------------------------------- the tooling
# Mutation tests: re-plant the classes of bug the v1.3.0 review found (and a
# few a port typically gets wrong) into the Python engine and require the
# committed corpus to notice each one. This is what "the corpus has teeth"
# means; a mutant that survives is a branch the corpus does not pin.


def _unchecked(a: int, b: int) -> int:
    return a - b


def _lenient_uint(text: str, hi: int) -> int | None:
    try:
        v = int(text)  # accepts "-0", " 1", fullwidth and Arabic-Indic digits
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= hi else None


def _truncating_fmt(v: float, decimals: int) -> str:
    scale = 10**decimals
    units = min(int(abs(v) * scale), (1 << 63) - 1) if math.isfinite(v) else 0
    sign = "-" if v < 0 and units > 0 else ""
    return f"{sign}{units // scale}.{units % scale:0{decimals}d}" if decimals else f"{sign}{units}"


def _skipping_daily_pnl(self: RiskEngine, sid: str | None) -> float | None:
    """The v1.3.0 fail-open: an unmarked held lot counts as zero P&L."""
    total = 0.0
    for (s, ccy), pnl in sorted(self._realized.items()):
        if sid is not None and s != sid:
            continue
        rate = self._fx_rate(ccy)
        if rate is None:
            return None
        total += pnl * rate[0]
    for (s, iid), lot in sorted(self._lots.items()):
        if (sid is not None and s != sid) or lot.pos == 0:
            continue
        mark = self._mark_price(iid)
        if mark is None:
            continue
        ins = self._instruments[iid]
        rate = self._fx_rate(ins.quote_ccy)
        if rate is None:
            return None
        total += float(lot.pos) * (mark - lot.avg_price) * ins.qty_unit * rate[0]
    return total


def _utf16_key(s: str) -> bytes:
    return s.encode("utf-16-be", "surrogatepass")


class Utf16Engine(RiskEngine):
    """A port that orders strategy ids by UTF-16 code unit (a Java
    ``TreeMap<String, _>``) instead of by code point (Rust ``BTreeMap``)."""

    def snapshot(self) -> dict[str, Any]:
        snap = super().snapshot()
        snap["lots"] = sorted(
            snap["lots"], key=lambda lot: (_utf16_key(lot["strategy_id"]), lot["instrument_id"])
        )
        return snap


MUTANTS: dict[str, tuple[Any, str, Any]] = {
    "position domain not symmetric (i64::MIN accepted)": (
        engine_mod,
        "_pos_add",
        lambda a, b: a + b if -(1 << 63) <= a + b < (1 << 63) else None,
    ),
    "timestamp differences unchecked": (engine_mod, "_ts_sub", _unchecked),
    "bid + ask unchecked": (engine_mod, "_sum_ticks", lambda b, a: b + a),
    "kill scope ids parsed leniently": (engine_mod, "_parse_uint", _lenient_uint),
    "money formatted by truncation": (engine_mod, "fmt_fixed", _truncating_fmt),
    "future-stamped marks trusted (clock = the order's own time)": (
        RiskEngine,
        "_event_clock",
        lambda self, order_ts: order_ts,
    ),
    "unmarked lots count as zero P&L": (RiskEngine, "_daily_pnl", _skipping_daily_pnl),
    "PEG orders tracked unpriced": (
        RiskEngine,
        "_tracked_price",
        lambda self, order: max(order.price_ticks, 0),
    ),
    "strategy kill not consulted": (RiskEngine, "_strategy_killed", lambda self, sid: False),
}


def _killers(corpus, base, engine_cls=RiskEngine) -> list[str]:
    out = []
    for script, audit, snapshot in corpus:
        try:
            rf.verify_script(script, base, audit, snapshot, engine_cls)
        except (rf.Divergence, ValueError, OverflowError, KeyError):
            out.append(script["name"])
    return out


@pytest.fixture(scope="module")
def corpus(golden_dir: Path) -> list:
    return rf.load_corpus(golden_dir / "risk_fuzz")


@pytest.mark.parametrize("mutant", sorted(MUTANTS))
def test_the_committed_corpus_detects_a_planted_bug(mutant, corpus, base, monkeypatch):
    owner, attr, replacement = MUTANTS[mutant]
    monkeypatch.setattr(owner, attr, replacement)
    killers = _killers(corpus, base)
    assert killers, f"no corpus script notices: {mutant}"


def test_the_committed_corpus_detects_a_utf16_ordered_port(corpus, base):
    """Strategy ids above U+FFFF sort differently in UTF-16 code units and in
    code points; a port that iterates in the wrong order is caught."""
    assert _killers(corpus, base, Utf16Engine)


def test_the_shrinker_minimises_a_divergence(corpus, base, monkeypatch):
    """ddmin: a planted bug's first diverging script (a hundred-odd steps)
    reduces to a handful that still diverge under the bug and still replay
    cleanly on the real engine."""
    owner, attr, replacement = MUTANTS["PEG orders tracked unpriced"]

    def diverges(cands: list[dict[str, Any]]) -> list[bool]:
        expected = [rf.expected_outputs(c, base) for c in cands]  # the real engine
        verdicts = []
        with monkeypatch.context() as m:
            m.setattr(owner, attr, replacement)
            for cand, (audit, snapshot) in zip(cands, expected, strict=True):
                try:
                    rf.verify_script(cand, base, audit, snapshot)
                    verdicts.append(False)
                except (rf.Divergence, ValueError, OverflowError):
                    verdicts.append(True)
        return verdicts

    target = None
    for script, _, _ in corpus:
        if diverges([script])[0]:
            target = script
            break
    assert target is not None
    rounds = []

    def counted(cands):
        rounds.append(len(cands))
        return diverges(cands)

    small = rf.shrink(target, base, counted)
    assert len(small["steps"]) <= 8 < len(target["steps"]), len(small["steps"])
    assert diverges([small]) == [True]
    audit, snapshot = rf.expected_outputs(small, base)
    rf.verify_script(small, base, audit, snapshot)
    assert small["name"].endswith("_min") and len(rounds) < 200


def test_the_report_protocol_round_trips(corpus, base, tmp_path, monkeypatch):
    """``replay_report`` (the Python side of the nightly job's engine
    protocol): every script OK on the real engine, the planted bug reported
    per script with a one-line reason."""
    src = tmp_path / "corpus"
    subset = corpus[:6]
    files: dict[str, bytes] = {}
    for script, _, _ in subset:
        files.update(rf.script_files(script, base))
    rf.write_files(src, files)
    rows = rf.replay_report(src, tmp_path / "out", "python")
    assert [why for _, why in rows] == [""] * len(subset)
    report = rf.read_report(tmp_path / "out" / "python.tsv")
    assert report == {s["name"]: "" for s, _, _ in subset}
    monkeypatch.setattr(RiskEngine, "_strategy_killed", lambda self, sid: False)
    bad = rf.replay_report(src, tmp_path / "bad", "python")
    report = rf.read_report(tmp_path / "bad" / "python.tsv")
    assert any(report.values()) and all("\n" not in why and "\t" not in why for _, why in bad)


def test_non_finite_inputs_are_rejected_like_the_other_ports(base):
    """NaN / Infinity never reach the JSON corpus; pin them here. The same
    assertions live in the Rust and Java fuzz replays."""
    refs = instrument_refs_from_golden(rf.INSTRUMENTS)
    eng = RiskEngine.from_config(base, refs)
    eng.on_market(1, 2450, 2452, rf.T0)
    want = {float("nan"): "NaN", float("inf"): "inf", float("-inf"): "-inf"}
    for k, (urgency, text) in enumerate(want.items()):
        d = eng.check_order(OrderRequest(k + 1, 1, 0, 10, 2450, 2, 1, "S1", urgency, rf.T0))
        assert (d.rule_id, d.reason) == (
            Rules.MALFORMED_ORDER,
            f"urgency must be in [0, 1]: {text}",
        )
    for scope, scope_id in ((Scope.GLOBAL, ""), (Scope.STRATEGY, "S1")):
        for bad in want:
            with pytest.raises(ValueError):
                eng.override_loss_limit(scope, scope_id, bad, rf.T0, "ops")
    assert eng.audit_len() == 3, "a refused override leaves no audit record"
    d = eng.check_order(OrderRequest(9, 1, 0, 10, 2450, 2, 1, "S1", 0.5, rf.T0))
    assert d.rule_id == Rules.ALLOW
