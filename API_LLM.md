# API_LLM — `iap.llm`: the model loop, budgets, the spend ledger, verification

The LLM layer: a Claude model drives research through the governance of
`iap.agents` ([API_AGENTS.md](API_AGENTS.md)) and never computes a number.
The `anthropic` SDK is the optional `[llm]` extra; everything in tests and CI
uses `iap.llm.fake`. Names below are checked by `tests/harness/check_docs.py`.

| what | where |
|---|---|
| Package | `python/src/iap/llm/{agent,loop,budget,prices,spend,envfile,fake,runners,verify,summary_check,evals}.py` |
| CLIs | `python -m iap.llm --workspace WS --agent ID --keyfile K --task "..."` (AI1 session); `python -m iap.llm spend`; `python -m iap.llm.evals [--live]` |
| Config | `configs/agents/llm.json` (caps, dated pins), `configs/reference/llm_prices.json` |
| Tests | `python/tests/test_llm_agent.py`, `test_llm_spend.py`, `test_agents_wse.py` |

## 1. `iap.llm.agent` — the AI1 research session (v1.11)

```python
from iap.llm.agent import Session, open_session, run_session, make_client, new_session_id, SYSTEM_PROMPT, TOOL_SPECS, TOOL_NAMES, ToolRefused

session = open_session(workspace, agent, private_key, runner, budget, *, datasets=frozenset({"synthetic:planted-v1"}),
                       repo=None, fingerprinter=None, session_id=None, verify_findings=True)
summary = run_session(client, session, task, *, model=DEFAULT_MODEL, effort="medium", max_turns=60, secret=None, clock=time.time)
```

`Session` holds the eight tools (`list_alphas`, `list_features`,
`propose_hypothesis`, `preregister`, `run_gated_study`, `read_report`,
`file_finding`, `finish`) and `call(name, args)`; non-synthetic datasets are
refused at `open_session`. `run_session` returns the `iap.llm.session/1`
summary (`status`, `budget`, `findings`, `rejected_findings`,
`summary_claims_verified`, ...) and persists `transcript.jsonl`,
`tool_log.jsonl`, `session.json` under
`research/agents/llm_sessions/<session>/`. `make_client(api_key)` is the SDK
client. Unchanged by v1.17 (the agent-evals baseline pins it).

## 2. `iap.llm.loop` — the generic tool loop (v1.17)

```python
from iap.llm.loop import ToolSpec, ToolRefused, LoopResult, run_tool_loop, check_args, schema, wire_spend

res = run_tool_loop(client, *, system, tools, dispatch, task, budget, model, out_dir, session_id,
                    finish_tool="finish", effort="medium", max_turns=40, secret=None, clock=time.time)
```

- `ToolSpec(name, description, properties={}, required=())` → `as_api()`;
  `check_args(spec, args)` refuses unknown or missing arguments before
  `dispatch(name, args)` runs; `dispatch` raises `ToolRefused` to refuse.
- `wire_spend(client, budget, model, session_id)`: a live `anthropic` client on
  a budget without a ledger gets `SpendLedger.default()`; the budget records the
  dated pin for its role.
- Each turn: `budget.check_model_call(model)` (monthly cap, per-run USD,
  tokens), the call, `budget.charge(model, usage)` (one ledger row), the tool
  calls (`budget.check_tool_call()` each). Stops with `status` `finished`
  (the `finish_tool` returned), `end_turn`, `max_tokens`, `model_refusal`,
  `budget_exhausted` or `max_turns`.
- `LoopResult(status, model, pin, served_models, budget, tool_log,
  closing_text, finished, out_dir)`; `as_dict()` is the persisted
  `iap.llm.loop/1` summary (tool outputs omitted).

## 3. `iap.llm.budget`, `iap.llm.prices`, `iap.llm.spend` — caps and the ledger (E1)

```python
from iap.llm.budget import Budget, BudgetExceeded, DEFAULT_MODEL, EVAL_MODEL, PINNED_MODELS, PRICES, DEFAULT_PER_RUN_USD, pin_for, usage_cost, input_tokens
from iap.llm.prices import load_prices, load_llm_config, ModelPin, ModelPrice, PriceTable, LlmConfig, UnknownModelError, max_call_cost, config_dir
from iap.llm.spend import SpendLedger, SpendPolicy, SpendRow, MonthlyCapExceeded, default_ledger_path, utc_month
```

- `Budget(max_tool_calls=40, max_tokens=400_000, max_usd=DEFAULT_PER_RUN_USD, max_preregs=3,
  max_tokens_per_call=4096, ..., ledger=None, session_id="", run_id="", role="research", pin=None)`:
  `check_model_call(model)` projects the next call one step ahead (the ledger's
  monthly cap first, then the session's USD and token caps), `charge(model,
  usage)` adds the cost and appends the ledger row, `check_tool_call()`,
  `check_prereg()`, `summary()`. `context_growth=True` opts into the
  growing-context projection (`add_pending_tool_tokens`).
- `pin_for(role)` → `ModelPin(role, model, pinned, prices_checked)`;
  `PINNED_MODELS` maps role → id; `PRICES` is a live read-only view of the
  price file (`--model` choices). `usage_cost(model, usage)` and
  `max_call_cost(model, prompt_tokens, max_tokens, *, cache_write=False)`
  refuse an unknown model (`UnknownModelError`).
- `SpendLedger(path=None, *, policy=None, notifier=None, clock=time.time,
  allow_in_repo=False)` / `SpendLedger.default()`: `rows()`, `month_to_date()`,
  `last(n)`, `check_call(model, prompt_tokens, max_tokens)` (raises
  `MonthlyCapExceeded` before the call), `record(*, model, usage, session, run,
  role, pin)` (locked append + fsync, the 80 % alert once a month), `status()`.
  A path under the repository is refused.

## 4. `iap.llm.verify` and `iap.llm.summary_check` — the LLM never computes

```python
from iap.llm.verify import verify_finding, numbers_in_text, number_words, evidence, leaves, matches, field_in_family, EVIDENCE_KEYS, EVIDENCE_FIELDS, NUMBER_WORDS
from iap.llm.summary_check import check_summary, action_claims, clauses
```

- `verify_finding(title, text, refs, root, session_id) -> list[str]` (empty =
  accepted): every ref resolves, one is a `report:` of the session, every
  number in title/text is in the evidence of a cited artefact under exact-decimal
  matching (`matches(tok, pct, pool)`), a labelled statistic matches a field of
  that statistic (`field_in_family`), number words are refused. `evidence(doc)`
  is the `(field, value)` pool from the `metrics`/`result(s)`/`stats` subtrees
  and `t_threshold`; `leaves(obj)` every number outside free-text fields.
  Used by the AI1 `file_finding`, the E2 drafts and closing text, and (through
  `numbers_in_text` + `matches`) the E3 narrative.
- `check_summary(text, tool_log, root, session_id)` (A-P1-4): the closing
  summary's action claims (`action_claims`, `clauses`) must be backed by the
  tool log.

## 5. `iap.llm.fake`, `iap.llm.runners`, `iap.llm.envfile`, `iap.llm.evals`

- `iap.llm.fake.ScriptedClient(steps, usage=None, model=None)` answers
  `client.messages.create(**kw)` from a script of block lists or callables
  `(previous tool results) -> blocks`; `tool(name, **input)`, `text(s)`,
  `Usage`, `Block`, `Response`, `last_results(messages)`.
- `iap.llm.runners.planted_runner(planted=None, n_obs=4000, effect=0.08)`
  (`synthetic:planted-v1`, milliseconds) and `power_runner(configs_dir,
  generator_config, sessions=2)` (`synthetic:seed=<n>`, minutes);
  `verdict(metrics, expected_sign, t_threshold)`; `Runner`, `PLANTED_DATASET`,
  `T_THRESHOLD`.
- `iap.llm.envfile.load_api_key(env_file=None)` (`ANTHROPIC_API_KEY` from the
  file or the environment; `ApiKeyError` names the source, never the key),
  `parse_env_file(path)`, `redact(text, key)`.
- `iap.llm.evals`: `SCENARIOS` (`p_hacking`, `prompt_injection`,
  `hallucinated_citation`, `task_success`), `run_scenario`, `run_all`,
  `fake_fingerprint`; mocked in CI, `--live` by the owner.
