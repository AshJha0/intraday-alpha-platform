# Agents — the roster, the models, the caps, and what the LLM may and may not do

Roster and model pins as of v1.17 WS-E (2026-10-15), unchanged since (the
`configs/agents/llm.json` pins are still dated 2026-10-16; neither v1.18 nor the v2.2.0 round added an LLM agent).
The code holds four LLM-backed agents and a set of non-LLM agent-layer controls. When live calls are on,
every LLM call is paid from the owner's Anthropic key (`ANTHROPIC_API_KEY` or `--env-file`; never printed,
logged or persisted), charged to the shared spend ledger
([GOVERNANCE.md §2c](GOVERNANCE.md)) and refused before it is made
when it would pass the monthly cap. Tests and CI use the scripted client of
`iap.llm.fake`: no network, no key, no spend.

> **No live model call was made in the v2.2.0 round.** Since 2026-11-18 live model calls are off
> (owner decision R0-1, [ROUND0_GATES.md](../research/round0/ROUND0_GATES.md) section 1). The capped
> path below stays in the code, unused. Every eval number of the round is an offline measurement of the
> guardrails, not of a model ([AGENT_EVALS.md](AGENT_EVALS.md), [AI_STACK.md](AI_STACK.md)). The live
> results cited on this page and its neighbours are historical: the v1.11.0 run of 2026-10-10 (recorded
> in CHANGELOG.md; no result file), the 2026-10-11 run on `claude-opus-5-5`
> ([research/agents/eval_live_2026-10-11.json](../research/agents/eval_live_2026-10-11.json)) and the
> 2026-10-27 SDK 2.x run ([research/agents/eval_live_sdk2.json](../research/agents/eval_live_sdk2.json)).
> The round added no LLM agent. It fixed the tool loop (a tool crash, a client error, a response cut at
> `max_tokens` and `pause_turn` no longer end a session badly; [LLM_SAFETY.md §2.5](LLM_SAFETY.md)) and
> added tracing ([TRACING.md](TRACING.md)).

## 1. Roster

| Agent | Since | Model (dated pin, `configs/agents/llm.json`) | Entry point | Writes | Never |
|---|---|---|---|---|---|
| LLM research agent (AI1) | v1.11 | `claude-sonnet-5-5` (research) | `python -m iap.llm --task ...` | signed proposals and prereg DRAFTS on the blackboard, exploratory reports, verified findings | computes a number, signs off a prereg, runs confirmatory or real data, promotes |
| Research assistant (E2) | v1.17 | `claude-sonnet-5-5` (research) | `python -m iap.agents research --task ...` | in-sample run reports (scratch ledger), prereg DRAFTS in `research/preregs/drafts/`, report drafts | touches holdout data or the committed ledger, registers, signs, states an unverified number |
| Morning ops agent (E3) | v1.17 | `claude-haiku-5-5` (ops) | `python -m iap.agents ops` | `alerts_<date>.{json,md}` | decides an alert (code does), changes a report, a prereg or a gate |
| Docs-sync agent (E4) | v1.17 | `claude-haiku-5-5` (docs) | `python -m iap.agents docs-sync --base origin/main` | one patch file plus a JSON sidecar | applies the patch, edits a non-documentation file, invents a number or a date |
| Agent evals | v1.15 / v1.17 | stub (`iap.llm.fake`); `claude-haiku-5-5` (eval) when run live | `python -m iap.agents.eval_harness`, `python -m iap.llm.evals` | `research/agents/eval_baseline.json` (on request) | runs live in CI |

Non-LLM agent-layer components: the hash-chained blackboard and
its single writer (`WriteBroker`), Ed25519 signing and the maintainer-signed key
registry, the pre-registration gate and fingerprint, the human sign-off
workflow, the reserve evaluator, the read-only MCP servers. They live in
`iap.governance` (moved there in v2.1). The v2.2.0 round removed the nine re-export shims
that `iap.agents` kept for them; `iap.agents.signing`, `iap.agents.trust`,
`iap.agents.fingerprint` and `iap.agents.analysis_identity` remain as aliases only because
scripts bound by signed pre-registrations import them by that path. What `iap.agents`
itself holds: the WS-E agents, the CLIs, the eval harness, the anchors, the reserve runner and
the MCP proposal workflow. These are the governance the LLM agents act through;
[docs/API_REFERENCE.md](API_REFERENCE.md) lists them. v1.18 added the analysis-identity binding
of signed pre-declarations (`iap.governance.analysis_identity`,
[governance/ANALYSIS_IDENTITY.md](GOVERNANCE.md)): a runner
declares an actor and holds no signing key, which is tamper evidence, not independence
([governance/TRUST_ROOTS.md](GOVERNANCE.md), "What a signature proves"). REL-16 monitoring
(`iap.monitoring`, [MONITORING_POLICY.md](MONITORING_POLICY.md)) lets an explainer
use only code-computed facts and rejects text that names a different state; no LLM decides a
monitoring state. No agent promoted a signal in v1.18.

## 2. Models and caps

| Role | Model | Pinned | Per-run budget | Tool calls | Typical cost |
|---|---|---|---|---|---|
| research (AI1, E2) | `claude-sonnet-5-5` | 2026-10-16 | USD 1 (`--max-usd`) | 30-40 | ~USD 0.1-0.2 per session |
| ops (E3) | `claude-haiku-5-5` | 2026-10-16 | USD 0.05 | 4 | ~USD 0.01 per morning |
| docs (E4) | `claude-haiku-5-5` | 2026-10-16 | USD 0.1 | 30 | ~USD 0.02 per PR |
| eval | `claude-haiku-5-5` | 2026-10-16 | per task cap | - | owner-run only |

Shared: a **monthly cap of USD 30** on the ledger outside the repository, an
80 % alert once per month, prices from `configs/reference/llm_prices.json`
(unknown model refused). `python -m iap.llm spend` prints the month-to-date.
Model ids are explicit, never a floating alias; changing a pin is its own PR.

## 3. What the LLM may do

- **Look**: list alphas, features, datasets, experiments; read a cited
  artefact (free text comes back wrapped and flagged as untrusted data).
- **Ask code to run something exploratory**: a gated synthetic study (AI1), an
  in-sample research run on an ingested real dataset (E2). Code computes every
  metric and the verdict and writes the report; the model gets a citation.
- **Draft**: a hypothesis, a pre-registration DRAFT (unsigned, `status: DRAFT`,
  registers nothing), a report draft, a narrative paragraph, a documentation
  edit. Every draft is for a human to read.
- **Cite**: every number it writes must be copied verbatim from a tool result
  and the artefact holding it cited.

## 4. What the LLM may not do, and what stops it

| It may not | Stopped by | Test |
|---|---|---|
| compute, estimate, round, combine or invent a number | `iap.llm.verify.verify_finding`: every number in a finding, a prereg draft, a report draft or the closing text must appear in the evidence of a cited artefact (exact-decimal match; labelled statistics must match their field; number words refused). The ops narrative is checked against the alerts document; a failing narrative is replaced by the code-written fallback | `test_numbers_identifiers_and_rounding`, `test_refusals` (`python/tests/test_llm_agent.py`); `test_drafts_verified_and_unsigned`, `test_ops_narrative_verified_or_fallback` (`python/tests/test_agents_wse.py`); the golden and held-out sets of [AGENT_EVALS.md](AGENT_EVALS.md) |
| sign, register, approve or promote anything | no tool does it: prereg drafts are unsigned files; a human runs `python -m iap.agents.cli prereg` (one look, signed), `sign-registry`, `python -m iap.lifecycle` | `test_drafts_verified_and_unsigned`; the `human_signoff` task of `python -m iap.agents.eval_harness` (scripted client); off-allowlist calls such as `sign` and `promote_alpha` in the injection suite (`test_structural_control_executes_no_off_allowlist_call`, `python/tests/test_offline_evals.py`) |
| run holdout or confirmatory data | AI1 sessions accept only `synthetic:` datasets; E2 refuses every dataset matching `forbidden_datasets` of `configs/paper/paper_trade.json` (the R7 holdout, `holdout2026`) and `*holdout*`; every report says `confirmatory: false` | `test_refusals` (a `real:` dataset is refused); `test_holdout_patterns_and_refusal` |
| touch the committed research ledger | E2 runs the research CLI with `--ledger` and `--out-dir` under a scratch directory; AI1 proposals cost one look each through the broker, capped per session | AI1: `test_full_study_is_signed_debited_and_cited`. E2: **no test**; `research_assistant.cli_runner`, which passes the scratch paths, is not exercised ([COVERAGE_BASELINE_2026-11-18.md](review/COVERAGE_BASELINE_2026-11-18.md)) |
| edit a file, run code, apply a patch, merge | no such tool; the docs-sync output is a `.patch` a human applies; the ops output is a document | `test_docs_sync_gather_and_patch` (the patch is written, the file is unchanged) |
| follow instructions found in data | tool results are wrapped by `iap.governance.untrusted`; the system prompts say so; the structural controls above hold regardless | `test_untrusted_wrapping` (`python/tests/test_agents.py`), `test_mcp_server_wraps_injected_board_text`; the injection suite |
| spend past the caps | `iap.llm.budget.Budget` + `iap.llm.spend.SpendLedger`: refused before the call | `test_usd_cap_stops_the_session`, `test_tool_call_cap`; `test_budget_obeys_ledger_cap_across_two_instances` (`python/tests/test_llm_spend.py`) |
| act unaudited | transcript, tool log and summary persisted per session; the API key scrubbed from anything written | `test_transcript_persisted_cached_prompt_and_no_secret` |

## 5. Running them

```bash
# E2: in-sample exploration; artefacts under research/agents/llm_sessions/<id>/
python -m iap.agents research --env-file ~/.config/iap/anthropic.env \
  --task "Explore EQ02 at 1s on ds3_20190130 in-sample and draft a pre-registration" --max-usd 0.5
```

```bash
# E3: the morning alerts document (exit 10 when an alert or warning fired)
python -m iap.agents ops --env-file ~/.config/iap/anthropic.env --paper-out-root data/real/paper
```

```bash
# E4: a patch file for this branch's doc drift; never applied by code
python -m iap.agents docs-sync --env-file ~/.config/iap/anthropic.env --base origin/main
```

`--dry-run` on any of them runs the scripted client (no key, no spend) to
exercise the plumbing. The research assistant exits 3 when its closing text
carried a number no report of the session holds; the drafts it wrote stand,
the text does not.

## 6. Evals and CI

`agent-evals` (a required check) runs `python -m iap.agents.eval_harness
--check`: the v1.15 research-agent task set plus, since v1.17, one eval per
WS-E agent (`iap.agents.wse_evals`): the research assistant refuses a holdout
run and a fabricated number and writes an unsigned DRAFT; the ops agent decides
the alerts in code and refuses a narrative with a number not in the document;
the docs-sync agent refuses a code-file edit and an ambiguous excerpt, writes a
patch that applies cleanly and leaves the tree untouched. Every eval asserts
that the (temporary) spend ledger holds one row per model call. All of it runs on the
scripted client, so it measures the plumbing and the controls, not a model. The
offline guardrail evals of [AGENT_EVALS.md](AGENT_EVALS.md) (`python -m iap.llm.verifier_eval`,
`python -m iap.llm.injection_eval`, `python -m iap.memory eval`) are not CI jobs of their own;
`python/tests/test_offline_evals.py` and `python/tests/test_research_memory.py` re-run them
against the committed result files inside the test suite. The `docs` job
(advisory) runs `tests/harness/check_docs.py` and lychee.

## 7. Read-only MCP server (official SDK)

`python -m iap.memory.mcp_server --root .` serves the read tools of
`iap.governance.mcp_sdk_server` plus the two research-memory tools of `iap.memory.mcp_tools`,
the resources and two prompts over stdio, built on the official `mcp` SDK (an
optional extra; `.mcp.json` at the root launches it). The base server never imports
`iap.memory`. Details, versions and
the tests behind each security property: [MCP_SERVER.md](MCP_SERVER.md).

Related: [GOVERNANCE.md §2a-2d](GOVERNANCE.md),
[docs/RUNBOOKS.md](RUNBOOKS.md),
[DIAGRAMS.md §26](DIAGRAMS.md) (the AI1 loop and its controls).
