# Tracing

The agent loops and the research graph write spans: one record per unit of
work, with a start and end time, a parent, a status and a few attributes.
The layout follows the OpenTelemetry GenAI semantic conventions so the
records can be read by anyone who knows those conventions, and so they can
be moved into an OpenTelemetry backend later. Nothing here adds a
dependency: the tracer, the exporter and the reader are plain Python in
`python/src/iap/llm/tracing.py` and `python/src/iap/graph/tracing.py`.

## Span model

A span has `trace_id` (32 hex), `span_id` (16 hex), `parent_id` (or null for
a root), `name`, `kind` (`INTERNAL` or `CLIENT`), `start_ns` and `end_ns`
(nanoseconds from the injected clock), `status` (`OK` or `ERROR`),
`attributes`, `events` and `seq` (the start order inside one tracer, used to
order spans that share a start time on a coarse clock).

| Where | Span name | Kind | Parent |
|---|---|---|---|
| `run_session`, `run_tool_loop` | `invoke_agent {agent}` | INTERNAL | none (root, one per session) |
| each model call | `chat {request model}` | CLIENT | the session root |
| each tool call | `execute_tool {tool name}` | INTERNAL | the session root |
| `iap.graph.execute(..., tracer=...)` | `invoke_workflow {graph name}` | INTERNAL | none (root, one per call) |
| each graph node | `graph_node {node}` | INTERNAL | the graph root |

`{agent}` is the session's agent name in `run_session` and the budget role
(`research`, `ops`, `docs`) in `run_tool_loop`.

Budget refusals are span events named `budget_refusal` with the attribute
`iap.budget.reason`: on the root when a model call is refused, on the tool
span when a tool call is refused.

## Where spans go

Tracing is on by default in both loops. With no tracer passed, the loop
writes `spans.jsonl` next to `transcript.jsonl` in the session directory, one
span per line, with the API key scrubbed by `iap.llm.envfile.redact`. A
caller (and every test) can pass its own `Tracer(exporter, clock=..., ids=...)`
with a fixed clock and `counter_ids()`, which makes the file byte-identical
across runs.

The graph is traced only when `execute` is given a tracer. The checkpoint
runner (`start`, `resume`) does not pass one, so run directories still hold
checkpoint files only.

Tracing never changes what a loop does. Every tracer method catches its own
exceptions, adds one to `Tracer.failures` and returns. A failing exporter
(disk full, say) loses spans and nothing else; the test
`test_exporter_failure_does_not_change_the_loop` compares the full loop result
with and without a broken exporter. The root span records the failure count
seen so far as `iap.tracing.failures`.

## Attributes

"Verified" means the name was read on 2026-11-18 in the GenAI conventions,
which have moved from opentelemetry.io to the
`open-telemetry/semantic-conventions-genai` repository (`docs/gen-ai/`
on `main`; status Development, so names can still change). `iap.*` names are
ours.

| Attribute | Spans | Meaning | Status |
|---|---|---|---|
| `gen_ai.operation.name` | all except graph nodes | `invoke_agent`, `chat`, `execute_tool`, `invoke_workflow` | verified (all four values are listed well-known values) |
| `gen_ai.provider.name` | session, chat | `anthropic` | verified |
| `gen_ai.agent.name` | session | agent name or budget role | verified |
| `gen_ai.conversation.id` | session, chat | session id | verified |
| `gen_ai.request.model` | session, chat | model asked for | verified |
| `gen_ai.response.model` | chat | model the API says it served | verified |
| `gen_ai.response.id` | chat | response id, when the response has one | verified |
| `gen_ai.response.finish_reasons` | chat | `[stop_reason]` | verified |
| `gen_ai.usage.input_tokens` | chat | uncached + cache read + cache write (the convention counts cached tokens here) | verified |
| `gen_ai.usage.output_tokens` | chat | output tokens | verified |
| `gen_ai.usage.cache_read.input_tokens` | chat | `cache_read_input_tokens` | verified |
| `gen_ai.usage.cache_write.input_tokens` | chat | `cache_creation_input_tokens` | verified in the new repository; the old opentelemetry.io registry named it `gen_ai.usage.cache_creation.input_tokens` |
| `gen_ai.tool.name` | tool | tool name | verified |
| `gen_ai.tool.call.id` | tool | the model's tool-use id | verified |
| `gen_ai.tool.type` | tool | `function` | verified |
| `gen_ai.workflow.name` | graph root | graph name | verified as a registry name; its use on this span is ours |
| `error.type` | any `ERROR` span | exception class name, or `client_error` on a session root | verified (stable) |
| `iap.usage.uncached_input_tokens` | chat | Anthropic `input_tokens` as reported | ours |
| `iap.cost.usd` | chat, session | cost charged by the budget (per call; session total on the root) | ours |
| `iap.turn` | chat, tool | loop turn index | ours |
| `iap.tool.outcome` | tool | `ok`, `refused` or `error` | ours |
| `iap.tool.args_bytes`, `iap.tool.result_bytes` | tool | size of the JSON arguments and of the result (or error message) | ours |
| `iap.session.status` | session | loop status (`finished`, `end_turn`, `budget_exhausted` ...) | ours |
| `iap.budget.refusals`, `iap.tracing.failures` | session | counts | ours |
| `iap.graph.*` | graph | `fingerprint`, `start_node`, `node`, `step`, `input_sha256`, `output_sha256`, `outcome`, `next`, `interrupt_reason`, `status`, `steps` | ours |

Span names and kinds follow the conventions where they define them:
`{gen_ai.operation.name} {gen_ai.request.model}` with kind CLIENT for model
calls, and `execute_tool {gen_ai.tool.name}` with kind INTERNAL for tool
calls (both verified). The `invoke_agent {agent}`, `invoke_workflow {graph}`
and `graph_node {node}` names and their INTERNAL kind were **not** checked
against the specification; they follow the same pattern.

## Worked example

A scripted session (`ScriptedClient`, usage 1000 in / 100 out, a tick clock of
1 ms and `counter_ids()`): the model calls `probe`, then `probe` with an
argument the governance refuses, then `finish`.

The first model call, as written to `spans.jsonl`:

```json
{"attributes": {"gen_ai.conversation.id": "s1", "gen_ai.operation.name": "chat",
 "gen_ai.provider.name": "anthropic", "gen_ai.request.model": "claude-haiku-5-5",
 "gen_ai.response.finish_reasons": ["tool_use"],
 "gen_ai.response.model": "claude-haiku-5-5-served",
 "gen_ai.usage.cache_read.input_tokens": 0, "gen_ai.usage.cache_write.input_tokens": 1500,
 "gen_ai.usage.input_tokens": 2500, "gen_ai.usage.output_tokens": 100,
 "iap.cost.usd": 0.0003375, "iap.turn": 0, "iap.usage.uncached_input_tokens": 1000},
 "end_ns": 3000000, "events": [], "kind": "CLIENT", "name": "chat claude-haiku-5-5",
 "parent_id": "0000000000000002", "schema": "iap.llm.tracing/1", "seq": 2,
 "span_id": "0000000000000003", "start_ns": 2000000, "status": "OK",
 "trace_id": "00000000000000000000000000000001"}
```

`python -m iap.llm.tracing summarize <session_dir>` on that directory:

```text
spans: 7  traces: 1  wall: 13.0ms
model calls: 3  input tokens: 7500  output tokens: 300  cache read: 3000  cache write: 1500  cost: $0.000668
tool calls: 3
  finish: ok x1
  probe: ok x1
  probe: refused x1
budget refusals: 0
status (invoke_agent research): finished
latency by operation (p50 / max):
  chat: n=3 p50=1.0ms max=1.0ms
  execute_tool: n=3 p50=1.0ms max=1.0ms
  invoke_agent: n=1 p50=13.0ms max=13.0ms
waterfall:
  |########################################| +    0.0ms    13.0ms invoke_agent research
  |   ###                                  | +    1.0ms     1.0ms   chat claude-haiku-5-5
  |         ###                            | +    3.0ms     1.0ms   execute_tool probe
  |               ###                      | +    5.0ms     1.0ms   chat claude-haiku-5-5
  |                     ###                | +    7.0ms     1.0ms   execute_tool probe !
  |                           ###          | +    9.0ms     1.0ms   chat claude-haiku-5-5
  |                                 ###    | +   11.0ms     1.0ms   execute_tool finish
```

`!` marks a span with status `ERROR`. Latency is grouped by
`gen_ai.operation.name`.

## What is deliberately not recorded

* Prompt, system prompt, completion text, tool arguments and tool results.
  The conventions' opt-in attributes for these (`gen_ai.input.messages`,
  `gen_ai.output.messages`, `gen_ai.system_instructions`,
  `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`) are never set.
  Spans carry sizes in bytes instead. The full text is already in
  `transcript.jsonl` and `tool_log.jsonl`, which have their own access rules.
* Error messages. A failed tool span carries the exception class in
  `error.type`, not the message, because messages can quote arguments.
* Graph state. Graph spans carry the state hashes the engine already
  computes, not the state.
* The API key. The exporter scrubs it, and no attribute is built from it.
  The tests check the file for the key and for sentinel strings planted in
  the prompt, the completion, the tool arguments and the finish summary.

## OpenTelemetry

`iap.llm.tracing.to_otel(spans, otel_tracer=None)` replays finished spans
(objects or `spans.jsonl` records) through the OpenTelemetry API, keeping
the parent links, times, events and status. It imports `opentelemetry`
inside the function and raises a `RuntimeError` naming the missing
packages if it is not installed. The project does not install it, and this
path has no test against a real SDK. OpenTelemetry assigns its own trace and
span ids.
