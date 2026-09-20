# RUNBOOK — Incident replay: capture → replay → reproduce → debug → fix → regression test

**Trigger**: a decision needs explaining after the fact — a fill nobody
expected, an order rejected for a rule nobody predicted, a P&L figure that
disagrees with the dashboard, a postmortem (`RUNBOOK_incident_kill_switch.md`
§6) — and the answer must be reproducible, not recollected.

**What makes it possible** (PLATFORM_CONVENTIONS.md §3, §13; docs/MVP.md §5):
everything downstream of the captured market-data stream is a pure function
of that stream plus the configuration in force. Every run keeps both, every
decision is a `DecisionTrace` line whose stream digest a replay must
reproduce, and every risk decision is an audit line. The flow is the same
for the Python MVP and the Java paper vertical; the commands differ, both
are below.

## 1. Capture — what a run already keeps

**Python MVP** (`python -m iap.mvp run`, `data/mvp/<run_id>/`, git-ignored):

| file | what |
|---|---|
| `events.jsonl`, `events.iap1`, `feed.json` | the captured stream (canonical JSONL + its IAP1 twin; `data_version` = sha256 of the IAP1 stream), the generator seed and QC totals |
| `config.json` | the `mvp.json` document as run plus the sha256 of every reference document in force (`config_version`) |
| `traces.jsonl` | one canonical-JSON `DecisionTrace` per decision, in decision order (the durable trace record) |
| `iap.sqlite` | the store: every trace decomposed into the stage tables + the MVP reference data (the index, rebuildable) |
| `risk_audit.jsonl` | every `RiskEvent` (pre-trade decisions, gaps, recoveries) |
| `report.json` / `report.md` | counts, P&L identity, risk by rule, routing, controls, TCA aggregates, realized IC, `trace.digest` |
| `paper_evidence.json` | the `PaperEvidence` document the lifecycle's PAPER gates read |

```bash
cd python && PYTHONPATH=src python3 -m iap.mvp run                 # -> ../data/mvp/<run_id>/
# mvp run 58a10f2194a3c81c: events=16578 decisions=355 parents=66 children=105 fills=55 pnl=-22.676287 USD digest=d938eeae68c85a6c... out=.../data/mvp/58a10f2194a3c81c
```

**Java paper vertical** (`java/paper.sh`, state directory `--state-dir` /
`$IAP_STATE_DIR`, `/data/state` in the deployments; `RUNBOOK_paper_trading.md`
§4–§5, PLATFORM_CONVENTIONS.md §12.3):

| file | what |
|---|---|
| `decision_traces.jsonl` | one `DecisionTrace` per pre-trade risk decision, appended and fsynced at every checkpoint (1,024 events), at session end and from the shutdown hook; never rotated |
| `risk_audit.jsonl` | every `RiskEvent`, same cadence |
| `risk_snapshot.json`, `session_state.json` (x-version 2: `trace_lines`, `audit_lines`, the event cursor) | the restart state |
| `config_audit.jsonl`, `admin_audit.jsonl` | which configuration ran; who called the kill-switch API |
| the session report (x-version 3) | `trace: {count, digest, jsonl}`, `risk.audit_sha256`, `config_sha256`; `/status` carries `trace_count`, `trace_digest` live |

The input stream of a paper session is the file the session was started on
(the golden vector or a generated `data/normalized/*.normalized.jsonl`,
identified by `data_version` in every trace header). Archive the whole
state directory with the report; `sha256sum decision_traces.jsonl
risk_audit.jsonl` must match the report before the directory is reused.

## 2. Replay — reproduce the digest from the capture, not from the generator

```bash
cd python
PYTHONPATH=src python3 -m iap.mvp replay --run ../data/mvp/<run_id>          # -> <run_id>/replay/
# replay OK: ../data/mvp/<run_id> reproduces its trace digest and report
```

`replay` re-runs the loop over `events.jsonl`, compares the trace digest,
`report.json` and the stream sha256 with the captured ones and exits 1 with
a line-per-difference diff otherwise. A reference document that changed
since the run (`configs/risk/risk.json`, `configs/execution/execution.json`,
`configs/strategies/alpha_params.json`, `research/alpha_registry.json`,
`configs/mvp/*`) is reported as a `config_version` mismatch **before**
anything runs: the incident must be replayed under the configuration that
produced it (`git checkout` the commit the report names, or the archived
`configs/` tree). Outside the checkout — the Python image bakes `configs/`
and `research/alpha_registry.json` under `/app` — add `--repo-root /app`.

For a Java session the equivalent is a second run over the same input and
configuration: `bash java/paper.sh` on the same vector produces the same
`decision_traces.jsonl` bytes and the same `trace.digest`
(`PaperTraceTest`: emission is a pure function of the event stream).
Re-derive the digest of an archived file with `TraceDigest.ofJsonl` (Java)
or `python -c 'from iap.trace import TraceDigest; print(TraceDigest.of_jsonl("decision_traces.jsonl").hexdigest())'`
— the Python reference reads Java's lines unchanged (same canonical JSON).

A replay that does **not** reproduce is itself the finding: either the
capture is not the input the run saw (check `data_version` in the trace
headers against `sha256sum events.iap1`), the configuration differs
(`config_version`), or a component is non-deterministic — which is a bug
with a test to write (§6).

## 3. Reproduce — find the decision

```bash
# the chain of one parent order, with venue names, from the store
PYTHONPATH=src python3 -m iap.mvp explain --run ../data/mvp/<run_id> 17
# Order 17
# Alpha:      EQ01-EQ03-EQ06  expected return = +0.0 bps  confidence = 0.12   <- acting (ensemble) signal
# Alpha:      EQ01  expected return = -0.0 bps  confidence = 0.02             <- its components
# Alpha:      EQ03  expected return = +0.0 bps  confidence = 0.34
# Alpha:      EQ06  expected return = +0.0 bps  confidence = 0.00
# Portfolio:  target = +97 shares
# Risk:       ALLOW
# Risk:       ALLOW
# Execution:  TWAP
# SOR:        XV1 = 100%
# Fills:      0 / 250 (0.0%)
# TCA:        IS = 0.0 bps
# Attribution: alpha = +0.0 bps  spread = -0.0 bps  impact = -0.0 bps  fees = -0.0 bps

# every order as one row: signal -> risk -> children -> fills -> TCA -> attribution
PYTHONPATH=src python3 -m iap.store sql --db ../data/mvp/<run_id>/iap.sqlite \
  "SELECT parent_order_id, signal_model_version, risk_decision, risk_rule_id, n_child_orders, filled_qty, implementation_shortfall_bps FROM v_order_chain ORDER BY parent_order_id"

# the rejected ones, with the deciding rule and reason
PYTHONPATH=src python3 -m iap.store sql --db ../data/mvp/<run_id>/iap.sqlite \
  "SELECT trace_id, order_id, rule_id, rule_index, reason FROM risk_decisions WHERE decision <> 1 ORDER BY timestamp_ns"
grep -c '"decision":2' ../data/mvp/<run_id>/risk_audit.jsonl              # REJECTs in the audit

# the exact trace line (the pinned renderer shows 0.1 bp; the line holds the exact values)
grep '"parent_order_id":17,' ../data/mvp/<run_id>/traces.jsonl | python3 -m json.tool | less
```

For a Java session: the line whose `stages.parent_orders[0].parent_order_id`
is the risk order id, rendered with `com.iap.trace.Explain.render(trace,
venueNames)`; or load `decision_traces.jsonl` into a store
(`iap.trace.JsonlTraceSink` lines are the same contract:
`Store.insert_trace(DecisionTrace.from_dict(json.loads(line)))`) and use the
queries above. The risk audit is byte-identical across Rust, Java and Python
engines, so `risk_audit.jsonl` can be replayed through `iap.risk` under a
debugger with the same decisions (API_TRADING.md §1.2).

## 4. Debug — step the engine

The engine is a plain Python object; drive it over the captured stream with
an in-memory sink and inspect the books, the risk engine, the simulator and
the TCA timeline at every event:

```python
from iap.mvp.config import load_config
from iap.mvp.feed import load_feed
from iap.mvp.engine import MvpEngine
from iap.trace import MemoryTraceSink
from pathlib import Path
cfg = load_config("../configs/mvp/mvp.json", repo_root=Path(".."))   # the configuration that produced the run
feed = load_feed("../data/mvp/<run_id>")                               # the captured stream (identity re-derived from bytes)
sink = MemoryTraceSink()
eng = MvpEngine(cfg, feed, sink)                                       # MvpEngine(cfg, feed, sink, *, ref=None)
for ev in feed.events:
    if ev.exchange_ts >= T_INCIDENT:                                   # stop right before the decision
        break
    eng.on_event(ev)
eng.risk_engine.position(12), eng.risk_engine.open_order_count()       # iap.risk.RiskEngine
eng.sim.simulator.orders, eng.sim.simulator.fills                      # iap.execution.ExecutionSimulator
sink.traces                                                            # every trace emitted so far
```

`python/tests/test_mvp.py::test_engaged_kill_switch_rejects_every_child_and_nothing_fills`
is the pattern (a scripted engine, a changed risk state, assertions on what
the trace shows). The truncation probe
(`test_shift_by_one_and_truncation_leakage_probes`) is the model for "does
the state at event k depend on anything after k?": run the engine on
`events[:k]` and compare every earlier signal and target bit for bit.

## 5. Fix — change the component, let `replay` show the diff

After the change, `python -m iap.mvp replay --run <run_id>` no longer
reproduces the captured report: its diff (digest, counts, P&L, per-rule
decisions) is the evidence of what the fix changed, and it belongs in the
PR next to the reason. A fix that changes a pinned semantics also changes a
golden (CONTRIBUTING.md §4): regenerate with the owning tool, one `golden:`
commit, a `schemas/MIGRATIONS.md` entry, and every port re-matched in the
same PR. A fix in `iap.risk` or `iap.execution` is a fix in the
reference-equivalent port only; the normative text is `rust/risk` /
`cpp/execution`, and the golden the three languages share decides
(API_TRADING.md).

## 6. Regression test — pin it

A captured stream + its expected digest is a test, exactly like
`tests/golden/expected_mvp.json`:

```bash
cd python && PYTHONPATH=src python3 tools/make_golden_mvp.py --force        # deliberate, versioned change only
PYTHONPATH=src python3 -m pytest -q tests/test_mvp_golden.py               # reproduces it from scratch AND from the capture
python3 -m pytest -q tests/replay/test_mvp_replay_determinism.py           # repo root: run twice, replay, identical bytes
```

For a smaller pin, `configs/mvp/mvp_tiny.json` + `generator_tiny.json` are
the fast fixtures the tests use; a scenario-shaped incident (a kill switch
engaged mid-session, a venue halted, a NO_ROUTE child) becomes a scripted
test in `python/tests/test_mvp.py` in the style of the kill-switch case,
and its row in `docs/SCENARIOS.md`. For a Java incident, the equivalent pin
is a `PaperTraceTest`-style case over the same vector asserting the
`trace.digest`.

## 7. Escalation and records

- A replay that reproduces the incident bit for bit is attached to the
  postmortem with the run id / state directory, its `data_version`,
  `config_version` and trace digest (GOVERNANCE.md §3: audit logs are never
  edited; corrections are new entries).
- A kill switch engaged during the incident follows
  `RUNBOOK_incident_kill_switch.md` — a replay is never a re-arm path.
- A fix to a risk rule needs the risk owner (CODEOWNERS `rust/risk/`,
  `python/src/iap/risk/`); a fix to a fill rule the execution owner
  (`cpp/include/iap/execution/`, `python/src/iap/execution/`).
