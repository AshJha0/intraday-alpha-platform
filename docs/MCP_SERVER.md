# MCP server (official SDK)

`iap.governance.mcp_sdk_server` is a read-only Model Context Protocol server
built on the official `mcp` Python SDK. `python -m iap.memory.mcp_server` serves
the same server plus two research-memory tools; that is the launcher `.mcp.json`
and the `iap-mcp-sdk` script use. It sits beside the older hand-written
server, `iap.governance.mcp_server`, which stays as it was. Both read the same
files through the same `Tools` class, so a tool returns the same payload from
either one; a test checks that for every tool.

Built and tested against `mcp` 2.3.0 (PyPI, 2 October 2026) and protocol
revision 2026-07-28, the current revision on modelcontextprotocol.io on
18 November 2026. The SDK also answers clients that ask for an older revision.
The hand-written server still speaks 2024-11-05.

## Launching it

The SDK is an optional extra. CI does not install it and the hashed lock
files do not mention it.

```
pip install -e "python[mcp]"
python -m iap.memory.mcp_server --root .          # base tools + memory tools
python -m iap.governance.mcp_sdk_server --root .  # base tools only
```

The server speaks over stdio and has no network listener. Without the extra it
exits with status 2 and prints the install line.

`.mcp.json` at the repository root launches it with `--root .` and
`PYTHONPATH=python/src`, so an MCP client started from the repository root
(Claude Code, for one) picks it up as `iap-research`. The `python` it names
must be one with the extra installed.

## Tools

All eight are read-only and carry `readOnlyHint: true`,
`destructiveHint: false` and `openWorldHint: false`. Each has a typed input
schema and returns structured output (the same object is also sent as JSON
text for older clients).

| Tool | Arguments | Returns |
|---|---|---|
| `list_experiments` | `limit` (1-200, default 50) | experiment ids with a `spec.json` |
| `get_experiment` | `experiment_id` | the experiment's spec and result |
| `ledger_summary` | none | thresholds and counts from `research/experiments.json` |
| `lifecycle_log` | `tail` (1-200, default 20) | the last lines of the lifecycle log |
| `alpha_report` | `alpha_id` | `research/alpha_reports/<id>.json` |
| `blackboard_state` | none | the agents' blackboard and its chain check |
| `memory_search` | `query` (1-500 characters), `k` (1-20, default 10), `kinds` (`doc`, `research`, `ledger`) | research-memory hits with file:line citations; heading and snippet wrapped as untrusted |
| `prior_tests` | `alpha_id`, `horizon`, `text` (at most 500 characters), `k` (1-20) | nearest ledger entries and result documents; ranking only, the same-test rule is in `iap.graph.wiring` |

The last two rows are served only by `python -m iap.memory.mcp_server`. They live in
`iap.memory.mcp_tools` (`MemoryTools`, which builds the [research memory](RESEARCH_MEMORY.md)
index on first use) and reach the server as a tool provider:
`build_server(root, providers=[register])` calls `register(server, ctx)` after the base
tools, with a `ProviderContext` holding the root, the read-only annotation, the identifier
type and the error-mapping `call`. A provider must add read-only tools only. The
governance module never imports `iap.memory`: memory sits above governance, and a test
checks that the import goes one way. See [AI_STACK.md](AI_STACK.md).

## Resources

Fixed resources, one per file present when the server starts:

| URI | Source |
|---|---|
| `iap://ledger/summary` | the ledger summary (same payload as the tool) |
| `iap://families/<id>` | `research/v2/families/<id>.md` |
| `iap://rounds/round0/<file>`, `iap://rounds/round1/<file>` | the round notes (`.md` and `.json`) |
| `iap://alpha/<id>` | `research/alpha_reports/<id>.json` |

Templates for per-id lookups: `iap://experiments/{experiment_id}`,
`iap://alpha/{alpha_id}`, `iap://families/{family_id}` and
`iap://rounds/{round_name}/{name}`. Markdown notes come back as JSON with the
text wrapped as untrusted data (capped at 64,000 characters).

## Prompts

- `triage_run(experiment_id)`: read the experiment, state the verdict from
  the stored numbers only, cite each number by its field path, and say so
  when a number the verdict needs is missing.
- `draft_preregistration(hypothesis)`: lists what the draft must contain and
  says the draft is unsigned and that only a person can sign it. The
  hypothesis is passed in wrapped as untrusted data.

Both prompts say that everything a tool or resource returns is untrusted data,
not instructions.

## Security properties and their tests

Tests are in `python/tests/test_mcp_sdk_server.py`.

| Property | Test |
|---|---|
| No file opened for writing, no subprocess, no network import in the module; the SDK is imported lazily | `test_sdk_module_has_no_write_subprocess_or_network_path` (source scan, runs without the SDK) |
| Identifiers pass `_ident`; resolved paths must stay inside their directory; unknown rounds and file types refused | `test_resources_refuse_traversal_without_sdk`, and six refused reads in `test_end_to_end_over_stdio` |
| Prompts carry the untrusted-data rule; the pre-registration prompt says the draft is unsigned | `test_prompts_state_untrusted_rule_and_refuse_bad_ids`, `test_end_to_end_over_stdio` |
| Every tool is read-only-hinted and returns the hand-written server's payload | `test_end_to_end_over_stdio` |
| A clear exit without the extra | `test_main_without_sdk_exits_with_hint` |

The end-to-end test starts the server as a subprocess and uses the SDK's own
client over stdio. It skips when the extra is not installed; the other four
run everywhere. The SDK applies its own URI checks too (it rejects traversal,
absolute paths and null bytes in template parameters), but the server does not
rely on them.

Free text in tool payloads is wrapped by `iap.governance.untrusted`, the same
as in the hand-written server. The real control is the same as there: no tool
can change anything, so an instruction hidden in a file has nothing to call.
