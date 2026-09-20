# The decision trace — `iap.trace` (reference), `com.iap.trace`, `rust/contracts::trace`, `iap::contracts` (C++)

One validated `DecisionTrace` per decision cycle answers *why did we trade?*
and *why was order X rejected?* from a single record: the market event that
triggered it, the four version hashes in force, and every stage's typed
output in loop order. The record is one canonical-JSON line; a session's
lines are digested into one SHA-256 that replay must reproduce; the store
indexes the lines; `explain()` renders one order's chain as text.

| what | where |
|---|---|
| Contract | `schemas/trace/decision_trace.schema.json` (x-version 1; `$ref`s the stage schemas; `$defs` `MarketEventRef`, `BookSnapshotRef`, `FeatureVectorRef`, `Attribution`, `TraceStages`); Python `iap.contracts.types.DecisionTrace` |
| Reference | `python/src/iap/trace/{builder,sinks,digest,explain,attribution}.py`; ids in `iap.contracts.ids`, canonical JSON in `iap.contracts.versions` |
| Ports | Java `java/src/main/java/com/iap/trace/` + `com/iap/contracts/` (`CanonicalJson`, `Trees`, `PyFormat`); Rust `rust/contracts/src/{canonical,sha256,trace}.rs` (+ `rust/telemetry/src/trace.rs`); C++ `cpp/include/iap/contracts/{canonical_json,trace}.hpp`, `cpp/src/contracts/`, `cpp/include/iap/util/sha256.hpp` |
| Goldens | `tests/golden/expected_canonical_json.json` (rules, 2051 float reprs, 24 string escapes, 9 documents, 5 rejects, the trace id, the trace digests; generator `python/tools/make_golden_canonical_json.py`), `tests/golden/expected_contracts_examples.json` (one instance per contract + the pinned `explain` block; `make_golden_contracts.py`), `tests/golden/expected_mvp.json` (a whole session's digest) |
| Tests | `python/tests/test_trace.py`, `test_canonical_json_golden.py`, `test_contracts*.py`; Java `TraceGoldenTest`, `CanonicalJsonGoldenTest`, `PaperTraceTest`; Rust `golden_canonical_json.rs`, `golden_trace.rs`; C++ `test_canonical_json_golden.cpp`, `test_trace_golden.cpp`, `test_replay_trace.cpp` |

## 1. The record

```
DecisionTrace
  trace_id        32 hex — make_trace_id(session_id, instrument_id, event_ts, sequence)
  session_id      id (alphabet ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$)
  instrument_id   u32
  event_ts        i64 ns — exchange_ts of the triggering market event
  sequence        u64 — that event's per-stream sequence
  data_version, feature_version, model_version, config_version   sha256 hex
  stages          TraceStages
    signal        AlphaSignal[]      signal[0] is the ACTING signal (the one the portfolio
                                     sized on); further entries are its components, each
                                     labelled by its own model_version (= alpha id)
    portfolio     PortfolioTarget | null
    risk          RiskDecision[]     ALLOW ⇔ rule_index == -1; REJECT/KILL carry rule_id, reason
    parent_orders ParentOrder[]
    child_orders  ChildOrder[]       venue_id 0 = SOR
    routing       VenueDecision[]    every candidate scored; venue_id 0 = NO_ROUTE
    fills         ExecutionReport[]  one per fill (PARTIAL / FILLED / CANCELED / …)
    tca           TCAResult[]        IS = delay + trading + opportunity (1e-9)
    attribution   Attribution | null alpha + spread + impact + fees + timing = total (1e-9)
```

An empty list or `null` means *the stage did not run* — a REJECT trace has
signal, portfolio and risk and nothing after; a C++ replay trace has the
execution stages and nothing before. Nothing is ever fabricated to fill a
stage. Every field is validated on construction and again by
`validate_typed` before any sink persists it (API_CONTRACTS.md §3).

## 2. Ids

- **Trace id** = the first 32 hex characters of
  `sha256("<session_id>|<instrument_id>|<event_ts>|<sequence>")` over ASCII
  with decimal integers. Pinned:
  `make_trace_id("golden-session-2026-09-19", 1, 1787578700000000000, 500)
  == "8b9fed6896d01463e64c4de915b0614b"` (`expected_canonical_json.json`
  `trace_id`). No wall clock, no counter: two runs of the same session
  produce the same ids, and any language reproduces them with string
  concatenation and one hash.
- **Version hashes**: `data_version` = sha256 of the IAP1 encoding of the
  event stream (the platform's dataset-version definition;
  REPRODUCIBILITY.md §2), `feature_version` = the registry hash every
  `FeatureVector` carries, `model_version` = `content_hash` of the model
  definition, `config_version` = `content_hash` of the configuration
  documents in force (`configs/…` as read, keyed by repo-relative path).
  The C++ replay, which has no feature engine or model on its path, writes
  the explicit marker `kVersionNotApplicable` (64 zeros) into
  `feature_version` / `model_version` — a documented "not applicable", never
  a fake hash.
- **Order ids** inside the stages are the platform's own (`parent_order_id`
  = the risk order id in the Java paper loop; `child_order_id` = the
  simulator order id; `execution_id` = the fill id). `Store.trace_id_for_order`
  and `v_order_chain` join on them.

## 3. Canonical JSON (the `rules` block of `expected_canonical_json.json`)

| rule | pinned |
|---|---|
| keys | sorted by Unicode code point of the raw key, recursively |
| separators | `,` and `:` — no whitespace |
| ascii | non-ASCII escaped as `\uXXXX` (UTF-16 surrogate pairs above U+FFFF); `/` not escaped |
| floats | shortest round-trip digits; exponent form iff the decimal exponent is `< -4` or `>= 16`; exponent written `e-05` / `e+16` (sign, at least two digits); integral values keep `.0` — i.e. Python `float.__repr__` |
| ints | exact decimal, i64/u64 domain (`18446744073709551615` round-trips) |
| literals | `null`, `true`, `false`; NaN / ±Inf and non-string keys are rejected, never serialised |
| line hash | sha256 over `ascii(line) + "\n"` per trace, concatenated (§4) |

Python: `json.dumps(obj, sort_keys=True, separators=(",", ":"),
ensure_ascii=True, allow_nan=False)` (`iap.contracts.versions.canonical_json`).
Java: `CanonicalJson.serialize` with `floatRepr` (Java's `Double.toString`
prints two significant digits where Python prints one — `4.9E-324` vs
`5e-324` — so the port tries the one-digit roundings and keeps the one that
parses back). Rust: an own writer over `serde_json::Value`, `format_float`
re-laying `{:e}` out under Python's rule; **serde_json is built with the
`float_roundtrip` feature** because its default parser can be 1 ulp off on
17-digit decimals, which broke the registry byte parity until it was
enabled — a port in any language must use a correctly rounded parser. C++:
`std::to_chars` shortest scientific + the same layout rule; `std::map`
keys compared bytewise on UTF-8, which is code-point order. All 2051 float
bit patterns, 24 escapes and 9 documents of the golden reproduce byte for
byte in the four languages.

`content_hash(obj)` = sha256 hex of that text. It is what `config_version`,
`portfolio_version`, `experiment_id` (first 16 hex) and
`BookSnapshotRef.state_hash` are made of.

## 4. The stream digest (`TraceDigest`)

For each emitted trace, in emission order: `line = canonical_json(trace)`;
hash the ASCII bytes of `line`, then one byte `0x0A`; `hexdigest()` is the
lowercase SHA-256 over everything hashed so far. Known answers
(`expected_canonical_json.json` `trace_digest`):

| stream | sha256 |
|---|---|
| the golden `DecisionTrace` example, one line (5627 bytes, line sha256 `8ecadebd…`) | `bf60a300d151c9cea462e339b0dac407c595fdc5e3c59efd588d5aada8455162` |
| the same trace twice | `e6f6ea54e5d5ff314dc11d235efc4caa4065e3604756101dbdce215502e053ca` |
| empty stream | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| the MVP golden session (355 traces, seed 12345) | `16cd29aa4c28ffb221b84b8f97b30c70eee09394a5160b5608233a07b536a187` (`expected_mvp.json`) |

`TraceDigest.of_jsonl(path)` re-canonicalises a file line by line and must
equal the digest the emitting sink reported; same seed ⇒ same digest; any
changed field or changed order changes it. The Java `TraceDigest` is
resumable (`--resume` rebuilds it with `ofJsonl` before continuing).

## 5. Sinks

| sink | Python | Java | Rust | C++ |
|---|---|---|---|---|
| JSONL (the durable record) | `JsonlTraceSink(path, append=False)` — one line per trace, flushed | `JsonlTraceSink` — buffered, `flush()` appends complete lines + fsync at the caller's commit points, `(path, append, digest)` resumes | `JsonlTraceSink<W: Write>` — validate → write → flush → digest | `JsonlTraceSink(std::ostream&)` |
| store (the index) | `StoreTraceSink(store)` → `Store.insert_trace` (document + decomposition into `alpha_signals`, `portfolio_targets`, `portfolio_legs`, `risk_decisions`, `parent_orders`, `child_orders`, `venue_decisions`, `executions`, `tca_results`, `attribution`, one transaction) | — | — | — |
| memory | `MemoryTraceSink` | — | — | `MemoryTraceSink` |
| fan-out | `MultiSink(*sinks)` (first failure propagates) | — | — | — |

Every sink validates before persisting; nothing is written for an invalid
trace. All satisfy `iap.contracts.protocols.TraceSink` (`emit(trace)`).
`Store.insert_trace` deletes and rewrites every stage row of that trace id,
so a partial re-emit never leaves stale rows (DATA_MODEL.md §3.3).

## 6. `explain()` — the pinned block

```
Order 12345
Alpha:      EQ03  expected return = +4.2 bps  confidence = 0.81
Portfolio:  target = +20,000 shares
Risk:       ALLOW
Execution:  POV 15%
SOR:        XV1 = 45%  XV2 = 35%  XV3 = 20%
Fills:      18,000 / 20,000 (90.0%)
TCA:        IS = 2.1 bps
Attribution: alpha = +6.2 bps  spread = -0.8 bps  impact = -2.1 bps  fees = -0.4 bps
```

Labels left-justified to 12 characters, one line per stage item, `(none)`
for a stage that did not run; header `Order <parent_order_id>` (or `Trace
<trace_id>` when no order exists); `signal[0]` is labelled with the order's
`alpha_id`, every further signal with its own `model_version` (the MVP's
`EQ01-EQ03-EQ06` ensemble followed by EQ01 / EQ03 / EQ06); REJECT/KILL
render `REJECT  rule = <rule_id>  reason = <reason>`; POV shows
`participation`; SOR percentages are routed child qty per venue over total
routed qty; Fills = Σ `filled_qty` / Σ parent `qty`; unnamed venues render
as their decimal id. The text above is pinned in
`expected_contracts_examples.json` and reproduced byte for byte by Python
(`iap.contracts.types.explain`), Java (`com.iap.trace.Explain`), Rust
(`contracts::trace::explain`) and C++ (`iap::contracts::explain`); the
multi-signal rule has one test in each suite (schemas/MIGRATIONS.md
2026-09-20). Expected returns render at 0.1 bp — the MVP's fitted alphas
show `+0.0 bps`; the exact values are in the trace.

Entry points: `python -m iap.store explain [--db …] <parent_order_id>`,
`python -m iap.mvp explain --run <dir> <parent_order_id>`,
`iap.trace.explain_jsonl(path, parent_order_id)`; in Java
`Explain.render(trace, venueNames)` (no CLI yet). The SQL view
`v_order_chain` is the same chain as one row per parent order
(DATA_MODEL.md §4, §7).

## 7. Emission points per language

| language | where | one trace per | stages filled | when |
|---|---|---|---|---|
| Python | `iap.mvp.engine.MvpEngine` (`iap.trace.TraceBuilder`) | decision cycle (feature vector emitted) | all nine: `signal` = [ensemble, EQ01, EQ03, EQ06], portfolio (null before the first sizing solve), risk per routed child, parent, children, routing with every candidate scored, fills, TCA, attribution | emitted in decision order once the parent is finalised (window closed, children terminal, timeline covers `end_ts`); sinks JSONL + store (`data/mvp/<run_id>/{traces.jsonl,iap.sqlite}`) + digest — both sinks' digests must agree |
| Java | `com.iap.platform.PaperTraces` inside `PaperTrading` (`RiskWiring` hook) | pre-trade risk decision | a REJECT at once (signal, portfolio, risk); an ALLOW when its single MARKET child is terminal **and** the TCA timeline has reached the terminal event; `routing[0]` scores every configured venue; fills as `ExecutionReport`s; TCA over the paper `MarketTimeline`; attribution by the pinned identity | `<state-dir>/decision_traces.jsonl` (fsynced at every checkpoint, session end, shutdown hook); `session_state.json` x-version 2 records `trace_lines`; the report (x-version 3) carries `trace: {count, digest, jsonl}`; `/status` reports `trace_count`, `trace_digest`; metrics `trace_records_total`, `trace_tca_skipped_total` (a parent the timeline never covered — 1 in the golden session). 1200-event golden session: 273 traces. |
| C++ | `iap::ExecutionReplay::set_trace_sink(sink, TraceOptions)` | parent order | `parent_orders`, `child_orders`, `routing` (the SOR's candidate table per child, rank 1 = the routed venue), `fills`; `signal` / `risk` / `tca` empty, `portfolio` / `attribution` null — the owners of those stages produce them | emitted **after** `run()` (after `cancel_all`), never inside the event loop; ~32 µs per 5.6 KB trace to serialise + ~30 µs to hash (`benchmarks/results_cpp.md`, trace-path table) |
| Rust | `contracts::trace::JsonlTraceSink` / `TraceDigest`, re-exported by `telemetry::trace` with `emit_counted` and the `trace_records_total` counter | caller's choice (no Rust loop emits traces today) | whatever the caller builds; `DecisionTrace::validate()` runs before any serialisation (a NaN would otherwise become `null`) | — |

The Java `PaperTraceTest` proves emission is a pure function of the event
stream (two identical runs ⇒ identical lines and digest) and that `--resume`
refuses a torn trace file (line count ≠ `trace_lines`, both named).

## 8. Incident replay with the trace

Capture → replay → reproduce → debug → fix → regression test, with exact
commands, is `docs/runbooks/RUNBOOK_incident_replay.md` (MVP: docs/MVP.md
§6; Java: the state directory of `RUNBOOK_paper_trading.md` §4–§5). The
short form: the JSONL file and its digest are the evidence; `replay` must
reproduce the digest from the captured stream; `explain` and
`v_order_chain` locate the decision; the engine is a plain object you can
step; the fix shows up as a digest diff; the captured stream + expected
digest become a golden.

## 9. Pinned in

| topic | document |
|---|---|
| the type inventory, Protocols, validation | [../API_CONTRACTS.md](../API_CONTRACTS.md) |
| canonical JSON, trace id, digest, no-LLM-on-the-path rule | [../PLATFORM_CONVENTIONS.md](../PLATFORM_CONVENTIONS.md) §13 |
| the store tables and views | [DATA_MODEL.md](DATA_MODEL.md) |
| the diagram | [DIAGRAMS.md](DIAGRAMS.md) §9, `diagrams/decision_trace_chain.mmd` |
| Java state directory, retention, archival | `RUNBOOK_paper_trading.md` §4–§5; PLATFORM_CONVENTIONS.md §12.3 |
| the observability story (metrics + audit + trace) | [ARCHITECTURE.md](ARCHITECTURE.md) §8 |
