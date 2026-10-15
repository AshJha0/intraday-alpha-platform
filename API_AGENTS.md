# API_AGENTS — the agent layer (`iap.agents`): governance, the WS-E agents, evals

The package every agent acts through. Off the trading path by construction
(`python/tests/test_import_policy.py`); standard library plus `cryptography`
(Ed25519). Every `iap.*` name below is checked by
`tests/harness/check_docs.py` (`api_names`): it resolves by import or the
`docs` CI job fails. Roster, models and caps: [docs/AGENTS.md](docs/AGENTS.md);
the controls and what they stop: [GOVERNANCE.md §2a-2d](docs/governance/GOVERNANCE.md).

| what | where |
|---|---|
| Package | `python/src/iap/agents/` |
| CLIs | `python -m iap.agents {research,ops,docs-sync}` (`iap.agents.__main__`, v1.17); `python -m iap.agents.cli {keygen,issue,apply,reserve,agent-keygen,prereg,anchor,verify-board,maintainer-keygen,sign-registry}` (operators); `python -m iap.agents.eval_harness [--check\|--write-baseline\|--live]` |
| Tests | `python/tests/test_agents*.py`, `test_agents_wse.py` (v1.17), `test_llm_agent.py` |
| Committed state | `research/agents/blackboard.jsonl`, `agent_pubkeys.json` (+ `.sig.json`), `trust_roots.json`, `anchors.json`, `eval_baseline.json`; drafts in `research/preregs/drafts/` |

## 1. Governance primitives (v1.10-v1.14)

**`iap.agents.blackboard`** — `Blackboard(path)`: append-only, hash-chained
JSONL (`entries()`, `verify()`, `verify_signatures(pubkeys, legacy, board_id)`);
`iap.agents.blackboard.digest(obj)` / `canonical(obj)` are the canonical-JSON
hash every signature and citation uses; `GENESIS`, `KINDS`.

**`iap.agents.broker`** — `WriteBroker(root, agents, *, pubkeys=, trusted=,
fingerprinter=)`: the only writer. `post_task`, `claim`, `file_finding`,
`release`, `preregister(agent, alpha_id, horizon, hypothesis, expected_sign,
*, auth, ledger=, dataset_version=, supersede=, screen=)` (`screen`, v1.17 C6,
opt-in: a net-of-cost screen document or its path; the (alpha, horizon) must have
PASSED it under its model identity, else `BrokerError`; the screen's path, sha256
and status are recorded on the entry — `API_PORTFOLIO_TCA.md` section 1.10);
`request_context()` gives the
board head and id a signed request binds to (`ed25519-v2`);
`iap.agents.broker.sign(private_key, agent, op, args, **context)` builds the
`auth` document; `board_id(root, board)`. Errors: `BrokerError`.

**`iap.agents.signing`** — `generate_keypair() -> (private_hex, public_hex)`,
`public_of(private_hex)`, `sign(...)`, `verify(...)`, `verify_ed25519(public_hex,
msg, sig_hex)`, `message(...)`. **`iap.agents.trust`** — the maintainer-signed
registry: `sign_registry`, `verify_registry`, `read_roots(root, ref)`,
`load_pubkeys(root)`, `trusted_pubkeys(root, ref)`; `TrustError`.

**`iap.agents.prereg_gate`** — `require(repo_root, alpha_id, horizon, board=,
*, fingerprinter=, agents=)`: the entry a run needs, or `PreregistrationError`;
`board_path(repo_root)`, `is_closed(entry)`. **`iap.agents.fingerprint`** —
`fingerprint(alpha_id)` (scheme 2: code + feature closure hash),
`module_closure`, `closure_hash`, `check_registration_env` (CI-lock venv for
registrations), `ci_lock_deps`, `compare_deps`.

**`iap.agents.workflow`** — `Workflow(broker)`: `propose` (one look),
`draft_prereg`, `drafts()`, human-only `sign_off` / `record_outcome`
(`HUMAN_ONLY_OPS`, `require_human`, `AgentAuthorityError`);
`issue_signoff(approver, secret, draft_hash, reason)`, `hit_rate(entries)`,
`proposal_id(...)`. **`iap.agents.approvals`** — single-use signed approvals
for the human lifecycle edges: `issue`, `verify`, `apply`, `new_secret`;
`ACTIONS = ("retire", "reset")`.

**`iap.agents.anchor`** — `anchors(repo, board)` and `verify(repo, board,
anchors_path, ref=)` (chain, git prefixes, anchors). **`iap.agents.reserve`** —
`ReserveEvaluator(secret, broker, runner)` with `evaluate(agent, candidate)`
(pass/fail only, `MAX_ATTEMPTS = 3`); `iap.agents.reserve_runner.make_runner`.

**`iap.agents.citations`** — `resolve(ref, root)`, `unresolved(refs, root)`,
`artefact(ref, root)` (the JSON a `report:<session>.<run>` or `board:<hash>`
names), `report_path(root, ident)`. **`iap.agents.untrusted`** —
`sanitise(text, limit)`, `injection_flags(text)`, `wrap(text, source)`,
`wrap_tree(value, source, keys)`: free text from anyone is data.

**`iap.agents.mcp_server`** (read-only MCP over stdio: `Tools`, `tool_list()`,
`handle(tools, msg)`, `serve(root)`; `FREE_TEXT` names the free-text keys) and
**`iap.agents.mcp_workflow`** (`WorkflowTools`, the signed proposal workflow
over MCP; `FORBIDDEN_TOOL_NAMES`).

## 2. The WS-E agents (v1.17 E2-E4)

All three run on `iap.llm.loop.run_tool_loop` ([API_LLM.md §2](API_LLM.md)):
a fixed `ToolSpec` allowlist, argument schemas checked before dispatch,
`iap.llm.budget.Budget` caps and the spend ledger on every call, transcript +
tool log + `session.json` persisted with the key scrubbed. Each module's
`run(...)` is the programmatic entry point; the CLI is `iap.agents.__main__`.

### 2.1 `iap.agents.research_assistant` (E2, role `research`)

```python
from iap.agents.research_assistant import (ResearchAssistant, InSampleRunner, cli_runner,
    holdout_patterns, is_holdout, run, TOOLS, SYSTEM_PROMPT, DRAFTS_RELPATH, ROLE)

summary, agent = run(client, task, *, workspace, runner, budget, model,
                     data_root=None, session_id=None, effort="medium", secret=None, max_turns=40)
```

- `cli_runner(repo, *, methods="v4", timeout_s=3600) -> InSampleRunner`:
  `python -m iap.research run --no-prereg --dataset-dir D --ledger S/experiments.json
  --out-dir S/experiments` with `S` a scratch directory under the dataset; the
  flattened `result.json` comes back as the metrics. `InSampleRunner =
  Callable[[alpha_id, horizon, dataset_dir], dict]` (tests pass their own).
- `holdout_patterns(repo)`: `forbidden_datasets` of
  `configs/paper/paper_trade.json` plus `*holdout*`; `is_holdout(name, patterns)`.
- `ResearchAssistant(workspace, session_id, runner, data_root, agent=,
  holdout=)` — the tools: `list_datasets`, `list_alphas`, `list_experiments`,
  `run_in_sample(alpha_id, horizon, dataset)` (one run per key; report
  `research/agents/llm_sessions/<session>/reports/rN.json`, schema
  `iap.llm.report/1`, `confirmatory: false`, cited as `report:<session>.rN`),
  `read_artefact(ref)`, `draft_prereg(alpha_id, horizon, expected_sign,
  hypothesis, refs)` (schema `iap.prereg.draft/1`: `status "DRAFT"`, `signed
  false`, `signature null`, `registered false`; file
  `research/preregs/drafts/<session>-dN.json`), `draft_report(title, text,
  refs)` (`reports/draft_report_N.md`, DRAFT banner), `finish(summary)`;
  `call(name, args)` dispatches; `unverified_numbers(*texts)` is the post-hoc
  check of the closing text.
- Verification: `draft_prereg` (when the hypothesis carries a number, a number
  word or a citation) and `draft_report` go through
  `iap.llm.verify.verify_finding` and raise `ToolRefused` on any problem;
  the refusal is recorded in `rejected_drafts`. `summary` carries `runs`,
  `prereg_drafts`, `report_drafts`, `rejected_drafts`, `unverified_numbers`,
  `closing_verified`, the loop fields (`status`, `budget`, `pin`, `tool_calls`).

### 2.2 `iap.agents.ops_agent` (E3, role `ops`)

```python
from iap.agents.ops_agent import (Alert, DEFAULT_RULES, evaluate, latest_report, build_document,
    narrative_fallback, unverified_numbers, render_md, run, EXIT_ALERTS)

alerts = evaluate(report, monitors, tca, spend, *, rules=None, today=None)   # list[Alert]
doc, md_path = run(client, *, out_root, out_dir, budget, model, spend=None, rules=None,
                   today=None, session_id=None, effort="low", secret=None)
```

- `latest_report(out_root) -> (date, dir) | None`: the newest
  `<date>/report.json` with `status: complete` of `python -m iap.paper.daily`.
- `evaluate(...)` — the rules, in code, each an `Alert(rule, severity, message,
  value, threshold)`: `paper.report_missing`, `paper.report_stale`
  (`max_report_age_days`), `cost.is_gap` (the report's own
  `is_gap_alert_bps`), `cost.{spread,fee,impact}_gap` (`cost_gap_alert_bps`),
  `cost.fees_share`, `paper.open_position`, `drift.feature` (the monitor's
  alerts), `drift.skipped` / `monitors.skipped`, `decay.day_ic`,
  `decay.rolling_ic` (`ic_min_ratio`), `decay.no_ic`, `drill.failed`,
  `spend.monthly` (`spend_alert_fraction`), `spend.corrupt_rows`. `DEFAULT_RULES`
  holds the thresholds; `rules=` overrides.
- `run(...)`: builds the document (schema `iap.agents.ops_alerts/1`), lets the
  model `read_alerts` and `write_narrative(text)` (at most 6 turns); a narrative
  with a number not in the document is refused (`unverified_numbers(text, doc)`)
  and the document falls back to `narrative_fallback(doc)` with
  `narrative_by: "code (fallback)"`. Writes `alerts_<date>.json` and
  `alerts_<date>.md` (`render_md`). CLI exit `EXIT_ALERTS` (10) when an alert
  or a warning fired.

### 2.3 `iap.agents.docs_sync` (E4, role `docs`)

```python
from iap.agents.docs_sync import DocsSync, gather, docs_findings, run, TOOLS, SYSTEM_PROMPT

inputs = gather(repo, base, head="HEAD")       # diff of code paths, modules, docs ranked by mentions
summary, patch_path = run(client, *, repo, base, head, out_dir, budget, model, findings=None,
                          session_id=None, effort="low", secret=None, max_turns=30)
```

- `gather(...)`: `git diff --name-only base...head`, the code diff (capped at
  60 k chars), the `iap.*` modules changed, the tracked docs (`*.md`,
  `docs/index.html`) ordered by how often they mention the changed modules.
  `docs_findings(repo)` runs `tests/harness/check_docs.py --json`.
- `DocsSync(repo, inputs, findings)` — tools `read_diff`, `read_findings`,
  `list_docs`, `read_doc(path)` (tracked docs only), `propose_edit(path, old,
  new, reason)` (`old` must occur exactly once; a code file, an empty or
  identical edit, or an overlap with an earlier proposal is refused),
  `finish(summary)`; `patch()` renders every accepted proposal as one unified
  diff.
- `run(...)` writes `docs_sync_<head>.patch` (only when something was
  proposed) and `docs_sync_<head>.json` (`proposals`, `refused`, `patch`,
  `applied: false`). Nothing is applied: `git apply <patch>` is the human's step.

### 2.4 `iap.agents.wse_evals` and the harness

`WSE_TASKS` (`wse_research_assistant`, `wse_ops_agent`, `wse_docs_sync`) are
`Callable[[Path], dict[str, bool]]`; `iap.agents.eval_harness.run_wse_task(name,
base)` scores one and `run_all` includes them in the suite compared with
`research/agents/eval_baseline.json` (`regressions(current, baseline)`).
Fixture helpers: `fake_dataset(root, name)`, `fake_paper_report(root, date,
*, is_gap, ratio)`, `fake_repo(base)`. The v1.15 research-agent tasks
(`TASKS`, `run_task`, `run_live`) are unchanged.

## 3. Evals (v1.10, v1.11)

`iap.agents.evals.run_all(root)` — each non-LLM control must fail when
removed (leak probe, P&L bug, null rejection, fabrication flag).
`iap.llm.evals.run_all` / `run_scenario` — the LLM research agent's four
scenarios (`SCENARIOS`), mocked in CI, `--live` by the owner
([API_LLM.md §5](API_LLM.md)).
