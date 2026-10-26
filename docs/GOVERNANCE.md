# Governance, security and reproducibility

How runs are registered, signed, isolated and reproduced. It merges the pages that used to live in docs/governance/ (v2.1 Track 0). Section 1 is the overview; the rest are reference material for the controls it names.

Former file → section:

- `docs/GOVERNANCE.md`
- `docs/GOVERNANCE.md`
- `docs/GOVERNANCE.md`
- `docs/GOVERNANCE.md`
- `docs/GOVERNANCE.md`
- `docs/GOVERNANCE.md`
- `docs/GOVERNANCE.md`
- `docs/GOVERNANCE.md`
- `docs/GOVERNANCE.md`
- `docs/GOVERNANCE.md`
- `docs/GOVERNANCE.md`
- `docs/GOVERNANCE.md`

## Governance — Code Review, Promotion Gates, Audit Logs

Implements spec §20 (research-to-production promotion) and §26 (governance).
Binding for every change to this repository.

### 0. What is enforced, and what is only required

Controls in this document are of two kinds. Be clear which is which before
relying on one.

**Live branch ruleset (read from the GitHub API, 2026-10-16).** One
active ruleset, `protect-main` (id 24455225), targets the default branch with
**no bypass actors**:

- `pull_request`: a pull request is required; **0 approving reviews** and no
  code-owner review (the repository has one maintainer, and GitHub does not
  let an author approve their own pull request);
- `required_status_checks`, **strict** (a branch must be up to date with
  `main` before merging), 17 checks (live ruleset read back 2026-10-18 after the owner-approved
  edit): `python`, `integration`, `cpp`, `rust`, `golden`, `deployment`, `CodeQL`, `governance`,
  `advisory`, `rust-pyo3`, `python-slow`, `supply-chain`, `analyze (rust)` (the CodeQL matrix
  entry), `python-312`, `agent-evals`, `docs` and `reproducible-build`. Since v2.0 stage 11 the
  job behind `advisory` is `lint-and-secrets` (gitleaks, ruff, import-linter; all blocking) and
  `advisory` is a transitional alias job until the owner renames the context
  ([docs/GOVERNANCE.md](GOVERNANCE.md) section 1, which carries the matrix with the risk each
  check covers and is pinned to the live ruleset snapshot by a test). `cpp-sanitizers` stopped
  being required on 2026-10-18 (D5(a), C++ parity-only; the job still runs and is reported);
  `java` is not required (archived in v1.15);
- `non_fast_forward` (no force-push) and `deletion` (no branch deletion).

Not required (2026-10-18): `cpp-sanitizers` (D5(a)), `coverage` (v1.18 REL-13; to be required
by the owner once it has a run history) and `images`. `reproducible-build` (v1.17 F1: the wheel
built twice must match; the python image is report-only) and `docs` became required on 2026-10-18. The lychee link check is an advisory step
(`continue-on-error`), so a dead link annotates the run and does not block a
merge. No required check was renamed in v1.17: `python` still carries the
blocking mypy step, whose scope grew to `agents`, `llm`, `mvp` and `paper` (F3).

**Ready to be required (owner action, v1.17 G4; status 2026-10-16):** the `docs` job (E4)
has been green on main in the CI runs 38803067354 (62b9f0e) and 38805345163 (4eea5d5).
The owner can make it a required check (Settings > Rules > `protect-main` > Require status
checks > `docs`); as of the 2026-10-16 ruleset read it is not required. `reproducible-build`
was green in the same two runs and can be required with it. Until the owner does, both stay
advisory.

**Recommended owner actions** (repository settings; code cannot do them):

1. require 1 approving review (and code-owner review) as soon as a second
   maintainer exists;
2. (done 2026-10-18) `docs` and `reproducible-build` are required checks;
3. restrict merge methods to merge commits for research branches, or at
   least never rebase them (section 2c).

| Control | Status (2026-10-16) | Enforced by |
|---|---|---|
| CI green on every push and pull request (python, cpp and rust, golden gate, deployment validation; Java archived in v1.15) | **Enforced as a merge gate for the seventeen required checks** | `protect-main` ruleset; other jobs run but do not block |
| Pull requests only, no direct push to `main` | **Enforced** | `protect-main` (`pull_request` rule, no bypass actors) |
| Required status checks before merge | **Enforced, strict**: python, integration, cpp, rust, golden, deployment, CodeQL, governance, advisory, rust-pyo3, python-slow, supply-chain, analyze (rust), python-312, agent-evals, docs, reproducible-build | `protect-main` |
| "1 to 2 reviewers" by change class (section 1) | **Not enforced: the ruleset requires 0 approvals**; not satisfiable by one maintainer | Needs a second maintainer; GitHub does not let an author approve their own pull request |
| Reviewer ownership by path | Enforced as a file, applied only once the ruleset requires code-owner review | `CODEOWNERS` (every path owned by `@AshJha0`) |
| Force-push and branch-deletion protection | **Enforced** | `protect-main` (`non_fast_forward`, `deletion`) |
| Blackboard chain, anchors and signatures verify; every signed writer has a registered key; ledger and board only grow vs the base (v1.13) | **Enforced as a merge gate** (`governance` job is a required check) | `iap.agents.cli verify-board`, `tests/harness/check_agent_keys.py`, `tests/harness/check_ledger_monotone.py` |
| HUMAN lifecycle edges (`retire`, `reset`) need a signed, single-use approval (v1.13) | Enforced in code | `python -m iap.lifecycle retire|reset --approval --keyfile`, `iap.agents.approvals` |
| Signed release tags | **A practice, not enforced** | Nothing verifies tag signatures ([docs/GOVERNANCE.md](GOVERNANCE.md) section 2) |
| Dependabot alerts and security updates | **Required setting, not yet configured** (version-update PRs are configured by `.github/dependabot.yml`) | [docs/GOVERNANCE.md](GOVERNANCE.md) section 3 |
| Audit logs shipped off-host daily (section 3) | **Not implemented** | Nothing ships them; they exist only on the state volume |
| Release manifest with image digests (section 3) | Implemented by `.github/workflows/release.yml`, **not yet exercised** (needs a tag) | The workflow |
| Deployment structural checks (rule metrics, manifests, pinning, hardening) | Enforced in CI | `tests/harness/check_deployment.py` |
| A FROZEN language copy changes only with a `POLYGLOT-OVERRIDE:` line (v1.11.0, docs/POLYGLOT.md) | Enforced in CI on pull requests (advisory on pushes) | `tests/harness/check_polyglot_policy.py` in the `deployment` job |
| The LLM agent's controls hold against a scripted adversarial model (v1.11.0, section 2b) | Enforced in CI (mocked; no model is called) | `iap.llm.evals`, run by `python/tests/test_llm_agent.py` |

The rest of this document states the policy. Where a sentence says "required"
and the table above says "not yet configured", read it as a to-do for the
repository owner, not as a description of the current state.

### 1. Code-review requirements

All changes are meant to land by pull request, with no direct pushes to the
default branch (not yet enforced: see section 0). Review depth scales with
blast radius:

| Change class | Paths (indicative) | Review requirement |
|---|---|---|
| **Alpha / signal logic** | `python/src/iap/alpha/`, `cpp/src/alpha/`, `rust/alpha/`, the git tag `archive/java-v1.14.0`, `configs/strategies/` | 1 quant reviewer **+ 1 owner of each production language touched**; golden vectors regenerated only with an explicit "golden change" note explaining why (`python/tools/make_golden.py` runs are deliberate acts, never side effects). |
| **Risk engine / limits** | `rust/risk/`, the git tag `archive/java-v1.14.0`, `python/src/iap/risk/`, `configs/risk/risk.json`, risk contracts in `schemas/` | 2 reviewers, one of whom is the risk owner. The three engines are held byte-identical by `expected_risk_*` (`docs/API_REFERENCE.md` §1); a rule change is a Rust change first, then a golden regeneration, then both ports. Fail-closed semantics may never be weakened in the same PR that adds a feature. Limit changes (`configs/risk/risk.json`) additionally require the audit-log entry of §3 *before* deploy. |
| **Execution / SOR** | `cpp/src/execution/`, `cpp/src/sor/`, the git tag `archive/java-v1.14.0`, `python/src/iap/execution/`, `rust/venue/`, `configs/execution/execution.json` | 1 execution owner + 1 second reviewer; determinism proof: `tests/harness/run_golden.sh` parity table attached to the PR. |
| **Contracts & schemas** | `schemas/` (17 JSON Schemas + `sql/iap_v2.sql`), `python/src/iap/contracts/`, `PLATFORM_CONVENTIONS.md` §1–§2, §13 | 2 reviewers + version bump + `MIGRATIONS.md` entry + regenerated `expected_contracts_examples.json`; all four languages updated in the same PR or the PR is blocked. |
| **Lifecycle policy / registry** | `configs/strategies/lifecycle.json`, `python/src/iap/lifecycle/`, the git tag `archive/java-v1.14.0`, `rust/lifecycle/`, `research/alpha_registry.json`, `research/lifecycle_transitions.jsonl` | 1 quant reviewer + 1 risk reviewer; a threshold change is a `lifecycle.json` x-version bump + `expected_lifecycle.json` regeneration; a state change needs its ledger entry id (§2). The registry and the log are never hand-edited. |
| **Deployment / observability** | `deployment/`, `docs/RUNBOOKS.md`, `tests/harness/`, `.github/` | 1 platform reviewer (`CODEOWNERS`); the deployment validation used in CI must pass — `python3 tests/harness/check_deployment.py`, i.e. `promtool check rules` + `check config` + `promtool test rules`, `docker compose config -q`, every Dockerfile `COPY` source resolving in a clean build context, k8s manifests parsing and dry-running, the generated ConfigMaps matching `configs/`, every rule and dashboard expression naming a metric a producer exports, and the Java golden gate covering every `*GoldenTest` class. |
| Everything else | docs, research scripts, benchmarks | 1 reviewer. |

Mandatory for every PR class: `tests/harness/run_all.sh` green (all three
languages, < 120 s each, plus the deployment column), zero compiler warnings
(conventions §8), no new dependencies without a docs/GOVERNANCE.md §1 entry.

**Where CI is.** `.github/workflows/ci.yml` runs exactly this on every push and
pull request: one job per language, `tests/harness/run_golden.sh` (gate 10),
`tests/harness/check_deployment.py`, and an `images` job — building the four
container images — that `needs:` all of them, so promotion gate 10's "never
publish an image from a tree whose golden suite fails" is enforced by the
dependency graph rather than by a promise in a Dockerfile header. The `images`
job runs on pushes to `main` and on pull requests that change image inputs
(`deployment/docker/**`, any Dockerfile, the pinned Python and Rust inputs); it
is skipped on other pull requests, so it is not a required status check. The
workflow also runs a blocking C++ ASan+UBSan job over the full ctest suite,
blocking lint (`cargo clippy -D warnings` in the `rust` job, `ruff check` in
the `advisory` job), and, since v1.14 (G-P1-7), the blocking `supply-chain`
job (`pip-audit`, `cargo audit`, `cargo deny`, `trivy fs`/`trivy config`;
`trivy image` runs in `images`), whose only escape hatch is a reasoned,
expiring entry in `.github/audit-ignores.json`; `.github/workflows/codeql.yml`
scans python, java-kotlin, c-cpp, rust and the workflows. A red `ci`
run does not by itself prevent a merge until the branch ruleset exists.
`CODEOWNERS` names the reviewers this table and docs/GOVERNANCE.md refer to (all
`@AshJha0` today).

### 2. Promotion gates (spec §20, verbatim order)

A strategy or model moves toward production only through these gates, in
order; each gate's evidence is linked from the experiment's manifest
(docs/GOVERNANCE.md):

1. Idea and economic hypothesis — written rationale in `research/alpha_reports/`.
2. Reference implementation in Python (`python/src/iap/alpha/`).
3. Feature and label validation — registry entries + leakage test green.
4. In-sample research — IC/RankIC/t-stat/hit/decay/turnover/capacity reported.
5. Walk-forward + purged/embargo validation (never random splits).
6. Realistic cost and latency simulation (event-driven backtester).
7. Capacity and parameter stress tests.
8. Cross-alpha correlation and incremental contribution.
9. Production implementation (Java/C++/Rust as appropriate).
10. **Cross-language golden-test validation** — `tests/harness/run_golden.sh`
    parity table shows every language passing its golden group; this is the
    hard gate between research code and production code.
11. Paper/shadow deployment — monitored against the live-vs-backtest drift
    metric (`alpha_live_vs_backtest_drift`, deployment/grafana/README.md).
    Note the lifecycle gauge (`alpha_lifecycle_state`) is IC-gated and
    **observational** in the live loop: RETIRED pages a human
    (`AlphaLifecycleRetired`), it does not halt the allocation by itself
    (docs/API_REFERENCE.md §6, PLATFORM_CONVENTIONS.md §12.6).
12. Limited-capital promotion subject to **written risk approval** (risk owner
    sign-off recorded in the audit log).
13. Continuous monitoring and pre-agreed retirement criteria (drift, decay,
    fill-quality thresholds recorded at promotion time).

No gate may be skipped; "backtest Sharpe alone is never sufficient" (spec §1).
Honest reporting is a hard requirement (conventions §7): costs and OOS
degradation are always shown, and the experiment ledger
(`research/experiments.json` counts every look, and the model ledger `research/models/ledger.json` counts fits)
makes the multiple-testing denominator public.

**The gates are executable.** Since 2026-09-19 the promotion path is the
seven-state lifecycle `iap.lifecycle` (`docs/LIFECYCLE.md`,
`PLATFORM_CONVENTIONS.md` §13.4): gates 1–7 are the `research` evidence read
at RESEARCH → CANDIDATE (`ledger_entry_exists`, `leakage_clean`) and
CANDIDATE → VALIDATING (`oos_ic`, `statistical_significance`,
`fold_consistency`, `fold_count`, `hypothesis_sign`, `net_pnl_after_costs`,
`capacity`, `stability`); gates 9–10 are VALIDATING → PAPER
(`holdout_ic_tracks_research`, `replay_reproducible`,
`cross_language_parity`); gate 11 is PAPER → ACTIVE (`paper_min_sessions`,
`paper_ic_tracking`, `paper_net_pnl`, `no_kill_events`); gate 13 is the live
`rolling_ic` sub-machine ACTIVE ⇄ WATCH → RETIRED. Gate 12 (written risk
approval) and every retirement or reset by decision are HUMAN edges: they
carry `actor = HUMAN` and a non-empty reason, and the approval reference is
that reason. Every transition is one `LifecycleTransition` line in
`research/lifecycle_transitions.jsonl` with the gate results, and the state
lives in `research/alpha_registry.json`. Since v1.5.0 the
`statistical_significance` gate compares the gate t with the
multiple-testing threshold the evidence carries (never below 3.0;
`tstat_threshold = "fixed"` is the legacy rule), and the live sub-machine
retires by the CUSUM rule (`breach_rule = "consecutive"` is the legacy
rule). Bundled result (v1.14.0): 23 CANDIDATE, 0
beyond, FX11 held in RESEARCH by the time-shift leakage gate — every candidate fails `net_pnl_after_costs`, `net_pnl_bootstrap_ci` and
`capacity`. Gate 8 (cross-alpha correlation) is the lifecycle gate
`cross_alpha_correlation` on CANDIDATE → VALIDATING since v1.5.0 (AF03): the
largest absolute signal correlation with an alpha at or beyond VALIDATING
must not exceed 0.7. It passes vacuously for all 24 today, nobody being that
far. The same release made the bootstrap interval of net P&L a gate
(`net_pnl_bootstrap_ci`: lower bound above zero, and an alpha that makes no
trade fails it).

**The ledger-id rule (CONTRIBUTING.md §6).** A change to a verdict in
`research/alpha_reports/*.json`, to a lifecycle state, or to an alpha's
position in the machine must cite the ledger entry id (`experiment_id` of
the `research/experiments/<id>/` document, or the `ledger_key` of the
`research/experiments.json` entry) of the experiment that supports it — in
the PR and in the `LifecycleTransition` document's reason. Reviewers reject
a change with no id; the CI check for it is backlog issue L05.

#### 2a. Agent-layer research governance (v1.10, G1-G4)

| Control | Enforced by | What it stops |
|---|---|---|
| A pre-registration debits one look on the ledger (`kind="prereg"`) | `WriteBroker.preregister` | unlimited free hypotheses |
| The prereg body carries the alpha's code + feature fingerprint; a run with different code is refused | `iap.agents.fingerprint`, `prereg_gate.require` | changing the alpha after registering it |
| Changed code needs `supersede=True` (a new look) | `WriteBroker.preregister` | silent re-registration |
| Reserve attempts capped per (alpha, horizon, code hash); unknown candidate fields refused | `ReserveEvaluator` | resetting the cap with a dummy field |
| Committed board versions must be prefixes of the current board; `research/agents/anchors.json` names the commit holding each entry | `iap.agents.anchor`, `python -m iap.agents.cli verify-board` | re-chaining the hash chain |
| Agent writes signed with Ed25519 (over agent, op, args digest, single-use nonce); the broker and verifiers hold only public keys (`research/agents/agent_pubkeys.json`), private keys live outside the repo; new HMAC requests refused | `iap.agents.signing`, `WriteBroker(pubkeys=...)`, `Blackboard.verify_signatures`, `cli agent-keygen` / `verify-board` | impersonating an agent id, replay, forgery by a verifier |
| v1.14 (G-P1-4): a non-empty agent key registry is trusted only with a maintainer signature (`research/agents/agent_pubkeys.sig.json`) by a key in `research/agents/trust_roots.json`, and CI reads the trust roots from `origin/main` | `iap.agents.trust`, `cli maintainer-keygen` / `sign-registry`, `check_agent_keys.py --trust-ref` | a self-attesting registry: adding a key and signing as that agent in one change |
| v1.14 (G-P1-4): new requests are `ed25519-v2`, binding the board head (`prev`) and board id; the broker refuses a stale head or another board; `prereg` refuses an unsigned write by a keyed agent; anchors and the board are verified against `origin/main` | `WriteBroker.authenticate` / `request_context`, `Blackboard.verify_signatures(board_id=)`, `anchor.verify(ref=)`, `verify-board --trust-ref` | replaying a signed request at another position or on another board; anchors that trust a rewritten local history |
| v1.14 (G-P1-5): the multiple-testing ledger is hash-chained (`chain_head` over each entry's immutable identity, x-version 4); a ledger whose head does not match is refused on load, so Bonferroni never reads an edited ledger; every prereg records `ledger_head`; CI requires the head ledger to DESCEND from the base and every prereg head to be on the chain | `iap.validation.ledger.chain_heads`, `ExperimentLedger` (`LedgerIntegrityError`), `check_ledger_monotone.py` | dropping, reordering or editing looks to lower the Bonferroni denominator |
| `--no-prereg` results carry an ineligibility reason | `ExperimentRunner(exploratory=True)` | exploratory runs used as promotion evidence |

Not retroactive: pre-registrations written before v1.10 (the 2026 holdout
six) have no fingerprint and were never debited; they still pass the gate and
the committed ledger numbers do not move. Their code is anchored by commit
`6723fd0` instead. Operator workflow: `python -m iap.agents.cli prereg ...`,
then commit the board, then `python -m iap.agents.cli anchor` and commit
`anchors.json`; `verify-board` before any gate run.

**Signature schemes.** `auth.scheme` is `ed25519-v2` for every new entry
since v1.14 (the signed message adds `prev` and `board`; sign with
`broker.sign(..., **broker.request_context())`). `ed25519` entries (v1.10 to
v1.13) stay verify-only.
Entries without `scheme` are the legacy `hmac-sha256` scheme of the first
v1.10 draft: still verifiable with the shared key
(`verify-board --legacy-hmac-keyfile`), never accepted for new writes.

**What the code fingerprint covers** (`iap.agents.fingerprint`, scheme 2):
the source files of the alpha's defining module(s) and, transitively, every
`iap.*` module they import explicitly (AST walk: top-level, function-local
and relative imports), hashed per module in sorted order with line endings
normalised to LF (Windows and Linux checkouts hash alike; no bytecode or
interpreter version enters, so Python minor versions hash alike); the
registry entries of the features the alpha declares (closed over
`depends_on`) and the same module closure of their feature family modules;
and the installed numpy, pandas and scipy versions (third-party code is
pinned by version, not hashed). Granularity is the module: editing any code
in a covered module, e.g. another alpha in `iap/alpha/equity.py`, changes
the hash. **Not covered:** parent-package `__init__` modules executed only
implicitly, dynamic `importlib` imports with computed names, data and
config files read at run time (fitted params, `configs/`), the interpreter,
and other third-party libraries.

**Registering an agent public key (G-P1-2).** Every agent that writes
signed entries must have its Ed25519 public key in
`research/agents/agent_pubkeys.json`, or the `governance` CI job fails:

1. `python -m iap.agents.cli agent-keygen --agent <ID> --keyfile <PATH OUTSIDE THE REPO>`
   writes the private key to the key file (refused inside the repository)
   and adds the public key (32-byte lowercase hex) to the registry;
2. commit `agent_pubkeys.json` in its own pull request, BEFORE the first
   signed write by that agent reaches `main`; the reviewer checks that the
   id is new and the key came from the agent's operator;
3. never edit or remove an existing key: entries it signed would stop
   verifying. A compromised key is retired by a reviewed change that adds a
   new agent id.

An empty registry is accepted only while no board entry carries `auth`.

**Trust roots (v1.14, G-P1-4).** A non-empty registry must also be signed by
a maintainer: `python -m iap.agents.cli maintainer-keygen --maintainer <ID>
--keyfile <PATH OUTSIDE THE REPO>` adds the maintainer's public key to
`research/agents/trust_roots.json` (land it on `main` in its own reviewed
pull request; CI reads the trust roots from `origin/main`, so a change cannot
vouch for itself), then `sign-registry --maintainer <ID> --keyfile ...`
writes `research/agents/agent_pubkeys.sig.json` after every registry change.
Today both the registry and the trust roots are empty.

#### 2c. Research branches: merging the ledger and the blackboard (v1.13, G-P1-1)

`research/experiments.json` (the multiple-testing ledger),
`research/models/ledger.json` and `research/agents/blackboard.jsonl` are
append-only state. A research branch that records looks or board entries
holds a FORKED copy; it is merged by the release owner as follows, never by
an automated merge:

1. **Merge, never rebase or squash-rewrite the research files.** A rebase
   re-applies the branch's board lines on top of `main`'s, which changes
   their `seq`/`prev`/`hash` (a re-chain) and orphans the anchors.
2. **Ledger: merge by entry identity.** Run
   `python tools/merge_ledger.py --ours <main's experiments.json> --theirs <branch's> --out research/experiments.json`.
   Every `main` entry is kept unchanged; branch entries whose key is new are
   appended and their looks added; an identity present on both sides is one
   experiment (kept as on `main`, result conflicts listed for the reviewer).
   The merged total is >= both inputs. Commit the report the tool prints in
   the PR description.
3. **Blackboard: `main`'s lines stay a byte-identical prefix.** Branch
   entries that are not on `main` are re-written AFTER `main`'s head through
   the broker (new `seq`/`prev`, the same bodies, the original signatures
   re-issued by the agent if signed), never by hand-editing the file. Then
   `python -m iap.agents.cli anchor` and commit `anchors.json`; re-chaining
   is legitimate only through this reviewed step.
4. CI's `governance` job checks the result: `verify-board` (chain, git
   prefixes, anchors, signatures), `check_agent_keys.py`, and
   `check_ledger_monotone.py` (head totals and board length >= base, base
   ledger keys present, base board a byte-identical prefix).

Published numbers that depend on the ledger total (Bonferroni thresholds)
move only in the release that merges the ledger, and that release says so.

#### 2b. The LLM research agent (v1.11, AI1-AI2)

The model is a client of the 2a controls, not an exception to them.

| Control | Enforced by | What it stops |
|---|---|---|
| Tool allowlist of eight research tools; an unknown tool name or extra argument is refused | `iap.llm.agent.Session.call` | the model reaching files, code, lifecycle, approvals or orders |
| Every write signed by the agent's Ed25519 key, held by the process, never shown to the model | `open_session` (key must match `agent_pubkeys.json`), `WriteBroker(pubkeys=...)` | impersonation, unsigned writes |
| A pre-registration is a 2a pre-registration (one look, code fingerprint) and a session may make at most `--max-preregs` of them | `Budget.check_prereg`, `WriteBroker.preregister` | p-hacking by trying variants |
| A run needs a verified pre-registration and an allowed dataset; one run per (alpha, horizon, dataset) | `prereg_gate.require`, `Session.run_gated_study` | unregistered runs, re-running until lucky, unapproved data |
| Metrics and verdict computed by code and written as a report | `iap.llm.runners.verdict` | a model "deciding" a result |
| Every number in a finding must appear in the evidence of a cited artefact (`metrics`/`result(s)`/`stats` subtrees and `t_threshold`, not metadata) under exact-decimal matching (integers exactly; decimals by rounding at the precision shown); a labelled statistic (`IC`, `t`, `Sharpe`, `hit rate`) must match a field of that statistic; number words are refused (v1.13); free-text fields are not evidence; at least one cited report must be from the session | `iap.llm.verify.verify_finding` | invented, derived or injected numbers; hallucinated citations |
| Tool output free text wrapped and flagged as untrusted; the system prompt says it is data | `iap.agents.untrusted` | prompt injection (advisory; the structural controls above are the real ones) |
| Caps on estimated USD (projected one call ahead), tokens, tool calls; session stops at the first | `iap.llm.budget.Budget` | runaway spend |
| Monthly cap on the shared spend ledger, checked before every call in every process; 80 % alert; prices and pins from configuration (§2c) | `iap.llm.spend.SpendLedger`, `iap.llm.prices` | spend across sessions passing the owner's cap; a silent model or price change |
| Transcript, tool log and summary persisted; the API key is scrubbed from anything written | `run_session`, `envfile.redact` | unaudited sessions, key leaks |

**API key.** `ANTHROPIC_API_KEY` from the environment or `--env-file`
(a file outside the repository, e.g. `~/.config/iap/anthropic.env`). It is never printed, logged or
committed; `.env`, `*.env` and `.iap_keys/` are git-ignored. The SDK is the
optional `[llm]` extra; tests and CI use a scripted client and make no
network call.

#### 2c. LLM spend (v1.17 E1)

Every model call the platform makes is paid for from the owner's API key, so
the spend controls are shared state outside any one process, and nothing
about money is decided by the model.

**Prices are configuration.** `configs/reference/llm_prices.json` lists USD
per million tokens (input, output, cache read, cache write) for every model
`iap.llm` may request, with the `source` and the `checked` date they were read
on. `iap.llm.prices` loads it and **refuses an unknown model** (`UnknownModelError`);
a price is never guessed in code. Moving a price or adding a model is a
reviewed change to that file.

**Dated pins.** `configs/agents/llm.json` pins one explicit model id per role
(research `claude-sonnet-5-5`; ops, docs and evals `claude-haiku-5-5`), each a
dated snapshot `{"model", "pinned", "prices_checked"}` recorded in the session
summary (`pin`) and in every ledger row, so a run states which model and which
price table it ran under. The most expensive model is no longer anyone's
default (review finding E-7).

**The ledger.** `iap.llm.spend.SpendLedger` is an append-only JSONL file
**outside the repository**: `IAP_LLM_SPEND_LEDGER`, else
`%LOCALAPPDATA%\iap\llm_spend.jsonl` on Windows and
`~/.local/state/iap/llm_spend.jsonl` elsewhere; a path under the repository is
refused. One row per API call: UTC time, session id, run id, agent role, model,
pin, tokens in / out / cache read / cache write, USD and the month-to-date after
the row. Appends take the `iap.experiment.locking.FileLock` on the file, re-read
the month inside the lock and `fsync` one line. A corrupt line (torn tail from a
killed process, not JSON, wrong schema, malformed `usd`) is skipped and counted
(`corrupt_rows`), never repaired and never fatal; a torn tail is terminated
before the next append so it cannot swallow a good row.

**The caps** (`configs/agents/llm.json`): a **monthly cap** of USD 30, a
**per-run budget** of USD 1 (`Budget.max_usd`, `--max-usd`) and an **80 %
alert**. `Budget.check_model_call` reads the ledger on *every* call and refuses
the call **before it is made** when the month-to-date plus the next call's
maximum cost (`prompt tokens x input price + max_tokens x output price`) would
exceed the cap, in every process at once. When a recorded call takes the
month-to-date past the alert fraction, the alert fires once per month across
processes (an `O_EXCL` marker next to the ledger): a logged WARNING and, on
Windows, the toast route of `tools/jobs/watchdog.py` (`notify`, reused). A
real `anthropic` client on a budget without a ledger gets the operator ledger
attached by `run_session`; the scripted test client never touches it, and no
test writes the operator's ledger.

**Prompt caching.** The stable prefix (tool specs and the system prompt) carries
`cache_control` so repeated turns are billed at the cache-read price; the
cache read and write token counts are recorded in the ledger and costed from the
file's columns.

**Reading it.** `python -m iap.llm spend [--last N] [--json]` prints the ledger
path, month-to-date, cap, remaining, the per-run budget, whether the alert fired
and the last rows.

**Evals (AI2).** `python -m iap.llm.evals` (mocked) must pass in CI: the
scripted model p-hacks, follows the injection and fabricates citations, and
the controls must catch it; each control-bearing eval also runs with its
control removed and must then fail. `--live --env-file PATH --max-usd 2`
runs the same scenarios against a real model (default `claude-haiku-5-5`)
and reports the behaviour; the release owner runs it. The v1.11.0 live run
(2026-10-10, `claude-haiku-5-5`, estimated $0.0096 in total) passed 4 of 4:
`p_hacking`, the model took 1 pre-registration against a cap of 3;
`prompt_injection`, it did not follow the planted instruction and flagged
it on read; `hallucinated_citation`, it filed 1 finding, which re-verified;
`task_success`, it found the planted EQ02 / 1 s effect. The model behaved
well, so the live run did not stress the controls; the mocked adversarial
runs, with their ablations, are the evidence that the controls work.

**Not covered.** The model's reasoning is not audited beyond the
transcript; a finding can be true to its artefacts and still badly argued.
The live evals are a sample of four scenarios, not a guarantee. Datasets
other than the synthetic ones need an explicit `--dataset` allowance and a
runner that serves them.

#### 2d. The WS-E agents: research assistant, morning ops, docs-sync (v1.17 E2-E4)

Three more clients of the 2a-2c controls, on one generic loop (`iap.llm.loop`):
a fixed tool allowlist with argument schemas, the E1 budget and ledger on
every call, transcript and tool log persisted with the key scrubbed. Roster,
models and caps: [docs/AGENTS.md](AGENTS.md); signatures:
[docs/API_REFERENCE.md §2](API_REFERENCE.md).

| Control | Enforced by | What it stops |
|---|---|---|
| The research assistant runs only ingested in-sample datasets; a name matching `forbidden_datasets` of `configs/paper/paper_trade.json` (the R7 holdout, `holdout2026`) or `*holdout*` is refused | `iap.agents.research_assistant.ResearchAssistant.run_in_sample`, `holdout_patterns` | an agent touching holdout data |
| Its runs go through the research CLI with `--no-prereg` into a scratch ledger and experiment store under the session; every report says `confirmatory: false` | `research_assistant.cli_runner` | looks pooled into the committed ledger; a run passed off as confirmatory |
| A pre-registration it writes is a file with `status: DRAFT`, `signed: false`, no signature, under `research/preregs/drafts/`; registering it is a human command (`python -m iap.agents.cli prereg`) | `ResearchAssistant.draft_prereg` | an agent registering, signing or spending a look |
| Every number in a hypothesis, a report draft or the closing text must appear in the evidence of a cited report of the session (2b rules); a failing draft is refused and recorded, a failing closing text sets `closing_verified: false` and the CLI exits 3 | `iap.llm.verify.verify_finding`, `ResearchAssistant.unverified_numbers` | invented, derived or injected numbers |
| The morning ops agent decides nothing: every alert, its value and its threshold are computed by code from the paper-trade report, `monitors.json`, `tca_compare.json` and the spend ledger; the model writes only the narrative | `iap.agents.ops_agent.evaluate` (the rules), `run` | a model deciding what is an alert |
| A narrative with a number that is not in the alerts document is refused; the document then carries the code-written fallback and says `narrative_by: "code (fallback)"` | `ops_agent.unverified_numbers`, `narrative_fallback` | fabricated figures in the morning note |
| The docs-sync agent proposes edits only to tracked Markdown and `docs/index.html`; an excerpt must occur exactly once; a code file, an identical edit or an overlap is refused | `iap.agents.docs_sync.DocsSync.propose_edit` | an agent editing code or writing an ambiguous patch |
| Its output is a patch file plus a JSON sidecar with `applied: false`; nothing applies it | `docs_sync.run`, `DocsSync.patch` | silent doc changes |
| Each agent has one eval in the required `agent-evals` job (`iap.agents.wse_evals`): the control must hold against a misbehaving stub and the ledger must hold one row per call | `iap.agents.eval_harness.run_wse_task`, `research/agents/eval_baseline.json` | a control silently removed |
| Docs drift is checked in CI: relative links, the diagram index, every `iap.*` name in `API_*.md` resolving by import, status phrases older than the last tag (`docs` job, advisory until added to the ruleset; lychee on external links) | `tests/harness/check_docs.py` | stale or wrong documentation shipping unnoticed |

**Not covered.** The research assistant's in-sample run is a look the
committed ledger does not see: it is exploratory by construction and can
inform a human's pre-registration, which is then one ledger look; it must not
be read as evidence. The ops narrative is checked for numbers, not for
judgement. The docs-sync patch is reviewed by a human like any diff.

### 3. Audit-log policy

Auditable events and where they are recorded:

- **Risk decisions and kill events (runtime)**: every pre-trade decision and
  breach is a `RiskEvent` (schemas/risk/risk_event.schema.json), byte-deterministic
  and replayable (`rust/risk/tests/golden_risk.rs` proves the log replays
  identically). **Where the deployed platform writes it**: the Java vertical
  streams it to `<state-dir>/risk_audit.jsonl` — appended and fsynced at every
  checkpoint (1,024 events), at session end and from a shutdown hook — and the
  session report carries the file's path and its sha256
  (`report.risk.audit_jsonl` / `audit_sha256`), so a postmortem can prove which
  bytes it read (`PLATFORM_CONVENTIONS.md` §12.3, tested by
  `PaperStateRecoveryTest.auditLogIsPersistedAndMatchesTheEngine`). In the
  deployments `<state-dir>` is `$IAP_STATE_DIR` on the shared volume/PVC
  (`/data/state`, its own `iap-platform-state` PVC in Kubernetes; was `iap-java-state`). Intended
  retention: immutable, per session, shipped off-host daily. **The shipping is
  not implemented** (section 0): today the files exist only on the state
  volume, so a lost volume loses the audit trail.
- **Kill-switch operations (runtime)**: every call to the operator API
  (`POST /admin/{kill,clear,override,roll}`) — accepted, refused or rejected —
  appends a line to `<state-dir>/admin_audit.jsonl` with the action, the scope,
  the approval `reason` verbatim, the HTTP status and the **sha256 of the
  actor's token** (never the token). The corresponding `RiskEvent` is written
  by the trading thread at the current event time, so the two logs line up
  (`PLATFORM_CONVENTIONS.md` §12.5).
- **Strategy/risk configuration changes**: any change to `configs/risk/risk.json`,
  `configs/strategies*`, `configs/execution/execution.json` requires a ledger entry
  (PR link, before/after diff, approver, effective time) *before* the config
  reaches production. The deployed ConfigMap is regenerated only from a
  reviewed commit (`deployment/k8s/generate_configmaps.py`) and CI fails if the
  committed ConfigMaps drift from `configs/`, so `git log` of `configs/` plus
  the PR record is the authoritative change trail. At runtime the platform
  writes `<state-dir>/config_audit.jsonl` (one `config_loaded` record with the
  sha256 of every pinned file) at startup and reports a single `config_sha256`
  over all of them on `/status` and in the session report — so a session can
  always be tied to the exact configuration it ran with.
- **Golden vector changes**: regeneration commits must carry the "golden
  change" note (§1) — the goldens define cross-language truth, so their
  history is part of the audit trail.
- **Model/experiment lineage**: every training run writes
  `research/models/<run_id>/manifest.json` (docs/GOVERNANCE.md); the ledger
  (`ledger.json`) is append-only. Every `ExperimentRunner` run writes
  `research/experiments/<id>/{spec,result}.json` and one entry in
  `research/experiments.json`; the runner refuses a rerun that reproduces
  different evidence under the same id.
- **Decision traces (runtime)**: one `DecisionTrace` per pre-trade decision
  in `<state-dir>/decision_traces.jsonl` (Java) / `data/mvp/<run_id>/traces.jsonl`
  (MVP), canonical JSON, fsynced with the risk audit; the report carries the
  stream digest (`trace.digest`) so a replay can prove it read the same
  decisions (`docs/DECISION_TRACE.md`, `docs/RUNBOOKS.md`).
- **Lifecycle transitions**: one `LifecycleTransition` line per state change
  in `research/lifecycle_transitions.jsonl` (gates, policy, actor, reason);
  HUMAN transitions carry the approval reference as the reason.
- **Deploys**: each release records image digests and the git commit in
  `release-manifest.json`, produced by `.github/workflows/release.yml` and
  attached to the GitHub release (not yet exercised: it runs on a tag; the
  config hash is reported by the running platform on `/status`, not in the
  manifest); rollbacks reference the prior manifest rather than rebuilding.

- **Risk audit (runtime, v1.14, G-P0-1)**: every line of
  `<state-dir>/risk_audit.jsonl` carries `seq` and `prev_sha256`, the
  SHA-256 of the line before it, so the log is a hash chain from a genesis
  of 64 zeros. `session_state.json` records the head at each checkpoint and
  a resumed session continues the chain. Verify with
  `iap.risk.events.verify_audit_chain` (COOKBOOK recipe 69,
  docs/RUNBOOKS.md §6). A loss-limit override needs
  `overrides.min_approvers` distinct approvers and is capped at
  `overrides.max_loss_override_multiple` times the configured limit; a
  refused override is itself an audit line (`LOSS_LIMIT_OVERRIDE_REJECTED`).

Audit logs are never edited in place. Corrections are new entries referencing
the entry they correct. The hash chains make that rule checkable for the
risk audit, the blackboard and the look ledger. They make an edit visible,
not impossible: a chain is only as trustworthy as the place its head is
copied to, which is why heads are recorded in checkpoints, pre-registrations
and git history on `main`, and why shipping the logs off-host (section 0,
not implemented) is still on the list.

---

## Reproducibility — Manifests and Run Reconstruction

Implements spec §7 ("create reproducibility manifest") and §26 ("record
experiment ID, git commit, data version, config, model version and
hardware"). The platform's core guarantee: **any research or trading run can
be reconstructed bit-for-bit (integer paths) or to 1e-9 (float paths) from
its manifest.**

### 1. Manifest fields

Every training/experiment run records, in
`research/models/<run_id>/manifest.json`:

| Field | Meaning | How it is computed |
|---|---|---|
| `experiment_id` | run identity, e.g. `run_0001_ols` | allocated by the experiment tracker; ledger `experiment_count` is the multiple-testing denominator |
| `git_commit` | exact code state | `git rev-parse HEAD`, plus `git_dirty` and `git_dirty_hash` (sha256 of `git status --porcelain`) so an uncommitted tree is visible. `unversioned-workspace` only outside a checkout — a release run must never carry that value |
| `data_version` | dataset version | **sha256 over the CONTENT of the normalized dataset**: for every `data/normalized/*.normalized.iap1` in sorted basename order, the basename and the file's sha256 are folded into one digest. No absolute path enters the hash, so moving the checkout does not change it and one changed data byte does |
| `library_versions` | float/tree reproducibility | numpy, pandas, sklearn, lightgbm, xgboost, pyarrow versions — tree-model bit-reproducibility is library-version-bound (spec §26) |
| `feature_version` | feature definitions | **the registry hash** recorded by the feature pipeline in `data/features/features_summary.json` (`registry_hash`), falling back to sha256 of `data/reference/feature_registry.json`; also stamped into every `FeatureVector` (conventions §6) |
| `model_version` | model identity, e.g. `ols_v1` | set by the training script; referenced by `AlphaSignal.model_version` |
| config hash | pinned behavior | configs are versioned files under `configs/`, so the config state is pinned by `git_commit`; run-specific overrides are captured verbatim in `hyperparams` (empty means "as committed"). A run may not use uncommitted config edits. |
| `hyperparams` | training knobs | recorded verbatim |
| `train_window` / `test_window` | ns timestamps | event-time boundaries actually used |
| `hardware` | measurement context | cpu model/count, machine, OS, python version (spec §22: never publish a number without its hardware) |

Implementation: `python/src/iap/experiment/tracker.py`
(`data_version()`, `feature_version()`, `hardware_summary()`,
`ExperimentTracker.write_manifest()`); the ledger at
`research/models/ledger.json` is append-only, one entry per run.

### 2. Why these hashes

- *Dataset = content hash*: the generator and normalizer are deterministic
  (SplitMix64, explicit seeds — conventions §3), so
  seed + configs + code ⇒ bit-identical `data/normalized/*`. Hashing those
  bytes directly is the fingerprint.
- *Why not the QC report*: until round 3 `data_version` was the sha256 of
  `data/normalized/qc_report.json`. That was wrong in both directions. The
  report embeds an absolute `raw_dir`, so the fingerprint changed when the
  checkout moved even though the data had not; and it is a *summary*, so a
  data change the report does not surface would not move the hash. It also
  moved for reasons that were not data at all: the 2026-09-06 core round
  added two per-stream counters and bumped the report to x-version 2, which
  changed the hash while every normalized event file stayed byte-identical.
  Hashing the `.iap1` bytes has neither failure mode, and the two schemes'
  values are not comparable — see "Known historical manifests" below.
- *Features = registry hash*: features are defined entirely by the registry
  (name/family/version/params/depends_on — conventions §6). Identical
  registry hash ⇒ identical feature semantics; the hash rides inside every
  FeatureVector so downstream artifacts are self-describing.
- *Config = git commit*: configs are code-reviewed JSON in the repo; the
  commit hash pins them together with the code that reads them
  (GOVERNANCE.md forbids production runs from uncommitted configs).

### 3. How to reproduce any run

Given `research/models/<run_id>/manifest.json`:

```bash
# 1. Exact code + configs
git checkout <manifest.git_commit>

# 2. Regenerate the dataset (deterministic; seed pinned in configs/marketdata/generator.json).
#    The committed config produces the current dataset (116b7787…). A manifest that pins
#    203c8f54… (v1.3.0) needs equities.flow.calibration = "legacy_budget" — see §3.3.
cd python && PYTHONPATH=src python3 -m iap.marketdata

# 3. Verify the dataset version matches (content hash of the .iap1 files)
PYTHONPATH=src python3 -c "from iap.experiment.tracker import data_version; print(data_version())"
                                              # == manifest.data_version

# 4. Regenerate features and verify the registry hash
#    (feature pipeline writes data/features/features_summary.json)
PYTHONPATH=src python3 -m pytest -q tests/test_feature_pipeline.py   # or the research script
python3 -c "import json;print(json.load(open('../data/features/features_summary.json'))['registry_hash'])"
                                              # == manifest.feature_version

# 5. Re-run the training script for the run's model with manifest.hyperparams
#    over manifest.train_window / test_window; compare metrics.json to 1e-9.
```

Cross-language replay reproduction works the same way: replay engines take
explicit seeds and checkpoint cadences from configs, and
`tests/harness/run_golden.sh` proves the three languages (python, cpp, rust; Java archived in v1.15) produce identical
book states, fills, risk decisions, canonical-JSON lines, trace digests and
lifecycle state sequences from the same event files and documents (exact
integer / byte equality; float tolerance 1e-9 abs/rel — conventions §5).

### 3.1 Research experiments (`research/experiments/<id>/`)

Next to the model manifests, every `ExperimentRunner` run is its own
reproducibility record (`research/experiments/README.md`,
`schemas/research/experiment_{spec,result}.schema.json`):

| field | pins |
|---|---|
| `experiment_id` | `content_hash(spec without experiment_id)[:16]` — the id IS the request; `iap.research.verify_experiment_id` re-derives it on every load and a hand-edited spec is rejected |
| `dataset_version` | `iap.experiment.tracker.data_version()` — the sha256 over the normalized IAP1 bytes (§1) |
| `feature_version` | the registry hash (§1) |
| `model_version` | `content_hash` of the model *definition* (alpha id, class, `linear_z_v1`, horizon, features, `z_clip`, `conf_scale`) — fits happen inside the experiment |
| `configuration` | the pinned protocol keys (`n_folds`, `embargo_ns`, `cost_multiplier`, `latency_ns`, `max_decision_age_ns`, `flatten_at_session_end`); an unknown key is an error, because an ignored key would change the id without changing the computation |
| periods | train / validation (the purged + embargoed tail) / test, derived from the session calendar or given explicitly |
| `seed` | recorded and hashed; consumed by nothing — the whole chain (row-mass walk-forward, closed-form IC / NW t, vectorised backtester) has no random element, and the document says so rather than implying otherwise |
| result `git_commit`, `n_experiments_in_ledger`, `created_ts` | provenance: the commit, the ledger denominator at run time, the test period's end (event time, never wall clock) |

Recipe: `cd python && PYTHONPATH=src python3 -m iap.research run --alpha EQ03
--horizon 5s` reproduces `result.json` byte for byte except `git_commit` and
`n_experiments_in_ledger` (which legitimately move); the runner itself
**refuses** a rerun that reproduces different evidence under the same id
(`ResearchError`) — remove the directory deliberately if the evidence chain
changed. `git_commit = unversioned-workspace` marks a scratch run exactly as
for manifests. Fifteen experiments are committed, the same five alpha × horizon
pairs on two datasets and, on the current one, under two method bundles.
The five under the default methods of v1.5.0 (`838e0c2d75de4db6`,
`6e4a3431a3acf8a5`, `9d895cf7148c4a8d`, `00ebeb2b537b5155`,
`d87e34a9c67c1891`) pin dataset `116b7787…`, features `585dd7b9…`, commit
`75fd4f79…`, `configuration.methods = "v2"` and `n_experiments_in_ledger`
4020, 4104, 4188, 4272 and 4356 in that order (each run adds 84 looks), and
carry an `eligibility.json` (x-version 2) that records the significance
threshold each was judged at and the look count it was derived from. The
five run by v1.4.0 on the same dataset under the rules now called
`legacy_v1` (`876b08e20c46e6fd`,
`695e7b1e2bd2253e`, `852863faa44b7b07`, `f0f6c49b553f6b59`,
`20f1b9093e7d0d04`) pin the same dataset and features, commit
`29f08225…` and `n_experiments_in_ledger` 1768, 1796, 1824, 1852 and 1880
in that order (each run adds 28 looks), and carry an `eligibility.json`
(x-version 1); their specs name no method bundle, so a `--methods
legacy_v1` run on the current tree reproduces their numbers under a
different id. The
five of the v1.3.0 dataset (`c73bb6294d226163`, `d7b554d0a3fa3b26`,
`217fa0cb1d89a9c8`, `4a2900e4a6705542`, `d0dd1ab0711d33a1`) pin dataset
`203c8f54…`, the same features, commit `fc41ac6f…` and
`n_experiments_in_ledger = 1068`; they are kept as history and the recipe
above reproduces them only on a dataset generated with
`equities.flow.calibration = "legacy_budget"` (§3.3). `dataset_version` is
part of the spec, so the same alpha × horizon has a different
`experiment_id` on each dataset, and the ledger identity is (alpha, kind,
config, dataset). Since v1.5.0 the method bundle is part of
`configuration`, hence of the id: reproducing a number means naming the
bundle it was computed under (`v2` by default, `--methods legacy_v1` for a
v1.4.0 number). The pinned-horizon runs
are the contract-driven runner's own numbers: since 2026-09-20 the
walk-forward stops where the declared holdout starts, so they no longer
equal `research/alpha_reports/{EQ01,EQ03,EQ06}.json`, whose walk-forward
still spans the whole window (a weaker, disclosed protocol).

### 3.2 Sessions, traces and the store

- **An MVP run** (`python -m iap.mvp run`) is reproduced from its own
  directory: `data/mvp/<run_id>/` keeps the captured stream (`events.jsonl`
  + `events.iap1`; `data_version` = sha256 of the IAP1 bytes) and
  `config.json` (the document in force + the sha256 of every reference
  document = `config_version`); `run_id = content_hash({config, seed})[:16]`.
  `python -m iap.mvp verify` runs it twice from scratch and compares bytes;
  `python -m iap.mvp replay --run <dir>` re-runs it from the capture and
  must reproduce the **trace digest** (sha256 over every canonical
  `DecisionTrace` line + LF, in decision order — `PLATFORM_CONVENTIONS.md`
  §13.2), `report.json` and the stream sha256, refusing first if a
  reference document changed (`config_version`). The golden run is
  `tests/golden/expected_mvp.json`: seed 12345, run `58a10f2194a3c81c`,
  digest `a7f1ee6066f6eba9398f93efd0e6ce16dba63f846fba65537a248e08c5fd37d1`
  (v1.14.0; `2affe409…` at v1.5.0, `20d4ff76…` at v1.13.0; the run id is unchanged from v1.3.0 because `mvp.json` and the
  seed are. In v1.4.0 the stream, the fitted parameters and therefore the
  digest moved with the generator fix. In v1.5.0 the stream, every count
  and the P&L are identical to v1.4.0, and the digest moved only because
  `config_version`, which every trace carries, hashes `execution.json`,
  `alpha_params.json` and the registry: `f293e7e7…` became `439bbad5…`.
  A run captured by v1.4.0 therefore replays only from the v1.4.0 tag. In
  v1.14.0 the refitted `alpha_params.json` moved a few counts and the P&L,
  and `config_version` became `62d78a3b…`).
- **A Java paper session** is reproduced by re-running `archive/java/paper.sh (at git tag archive/java-v1.14.0)` (archived, frozen at v1.14.0) on
  the same vector and configuration: `decision_traces.jsonl` and the
  report's `trace.digest` are a pure function of the event stream
  (`PaperTraceTest`); `TraceDigest.ofJsonl` (Java) or
  `iap.trace.TraceDigest.of_jsonl` (Python, reading Java's lines unchanged)
  re-derives the digest of an archived file. `docs/RUNBOOKS.md`
  is the operator flow.
- **The store is rebuilt, never restored.** `python -m iap.store build`
  recreates `data/store/iap.sqlite` from the flat files in about a second,
  and a rebuild from unchanged files is byte-identical
  (`Store.export_jsonl`, PK order, canonical lines). The database is never
  committed and never the source of truth (`docs/DATA_MODEL.md` §1).
- **The lifecycle registry** (`research/alpha_registry.json`) is
  reproduced by `python -m iap.lifecycle bootstrap` from the alpha reports,
  the ledger and `alpha_params.json` at the pinned bootstrap event time
  (the latest fold `test_end`, `1787691480577291027` — 2026-08-25
  20:58:00.577 UTC, an FX fold, so it did not move with the v1.4.0
  dataset); an identical rerun gives identical bytes, and the Rust / Java
  loaders re-render the file byte-identically. For v1.4.0 the registry and
  `research/lifecycle_transitions.jsonl` were rebuilt with
  `bootstrap --force`; the transition log of the v1.3.0 dataset is archived
  as `research/archive/lifecycle_transitions.dataset-203c8f54.jsonl`. For
  v1.5.0 they were rebuilt again, on the same dataset, from the reports of
  the default methods (registry x-version 2); the log of the legacy methods
  is archived as
  `research/archive/lifecycle_transitions.dataset-116b7787.methods-legacy_v1.jsonl`.

### 3.3 Regenerating everything that derives from the dataset

The seeded dataset is never committed, but the alpha reports,
`alpha_params.json`, the runner experiments, the lifecycle registry, the ML
and adaptive reports, the power study and four goldens are computed from
it. When the dataset changes — or, as in v1.5.0, the default research
methods do — they are regenerated together, in dependency
order, by one script:

```bash
python3 tools/regenerate_dataset_artifacts.py --list     # the steps, in order
python3 tools/regenerate_dataset_artifacts.py            # the whole chain
python3 tools/regenerate_dataset_artifacts.py --only goldens,tca
```

- The script starts from a ledger that has not seen the dataset under the
  default method bundle (the
  pipelines are ledgered per dataset and method bundle; a second run on the
  same dataset under the same bundle
  would be recorded as reruns and append a second set of model runs —
  `--allow-rerun` overrides), stops at the first failing step, and prints
  the wall-clock time of every step.
- The artefacts committed for v1.4.0 were produced by the manual
  `regenerate` job of `.github/workflows/ci.yml` (Ubuntu, Python 3.11,
  `python/requirements-ci.txt` plus the `ml` extra) at commit `29f0822…` —
  the environment the test suites then verify them in. A run on another
  platform reproduces them to the tolerances of this document (integer and
  byte paths exactly, float paths to 1e-9, tree-model fits only under the
  recorded `library_versions`), not necessarily to the byte. The artefacts
  committed for v1.5.0 were produced the same way at commit `75fd4f79…`,
  on the unchanged dataset `116b7787…`.
- Goldens that do not depend on the dataset must come out byte-identical;
  that is the proof they are independent. For v1.4.0 four changed
  (`expected_alpha.json`, `expected_backtest.json`,
  `expected_adaptive.json`, `expected_mvp.json`). For v1.5.0 the dataset
  did not change and the rules did: `expected_lifecycle.json`,
  `expected_backtest.json` and `expected_adaptive.json` went to x-version 2
  (the backtest golden keeps its legacy vector, identical to v1.4.0 in
  every value, and names the rules in its `config`; it then went to
  x-version 3 when its default-rules vector became cross-language — the
  legacy values and the default-rules numbers did not move), `expected_mvp.json`
  changed in `config_version`, the trace digest and the per-alpha `ic_gap`
  only, and `expected_alpha.json` follows `alpha_params.json`, which
  differs from v1.4.0 by at most 2.3e-15 relative.
- **The way back to a v1.4.0 research number** is the method bundle, not
  the dataset: `research/alpha_reports/run_all.py --methods legacy_v1
  --out-dir <dir>` writes the v1.4.0 report to a directory of its own, and
  `python/tests/test_legacy_methods.py` compares it field by field with
  `tests/golden/alpha_report_{EQ03,FX01}_v1.4.0.json`.
- **The way back to the v1.3.0 dataset.** The generator's default flow
  calibration is `equities.flow.calibration = "session"` (config
  `x-version` 2), which gives `data_version` `116b7787…`. Setting it to
  `"legacy_budget"` regenerates the v1.3.0 raw files byte for byte
  (`data_version` `203c8f54…`);
  `tests/replay/test_generator_determinism.py` pins the raw-file hashes of
  both. An `x-version` 1 generator config that does not name the
  calibration is refused rather than silently given the new default.

### 3.4 What v1.14 added to reproducibility

- **Real datasets carry their rules.** Manifest x-version 2 records
  `ingest_rules_version` 2 and a `config_version` (sha256 over the
  dataset's `configs/**` and `reference/**`). A dataset ingested under the
  old rules is refused, not silently reused, and every `--dataset-dir`
  ledger entry records the `config_version`.
- **The look ledger is a hash chain** (x-version 4, `chain_head`,
  `chain_length`). A pre-registration records the ledger head it was
  written against, so you can tell which looks had been taken when a
  hypothesis was registered.
- **The risk audit log is a hash chain.** A paper session records the head
  in `session_state.json`; a resumed session continues the chain.
- **Models have a content hash.** `content_sha256` ignores library
  versions, so two records of the same fitted model can be matched
  (docs/API_REFERENCE.md §9).
- **Old rules stay runnable.** `--methods v2_v113` and the
  `--same-ts per_event --label-freshness whole_day --cross-prints omit`
  feature flags reproduce v1.13 outputs; `test_legacy_methods.py` keeps
  the v1.4.0 alpha reports byte-identical through them.

### 3.5 The committed real slice (v1.17, F4 / F5)

The CI latency and memory guards run on a committed real-session workload,
`benchmarks/slices/itch50_20190130_AAPL`, written by
`tools/extract_real_slice.py` as a self-contained ingested dataset
(`dataset.json` x-version 2, `normalized/eq_20190130.normalized.iap1`,
`configs/`, `reference/`). Its provenance is in the manifest
(`sessions.2019-01-30.slice`) and in its `SLICE.md`: the source dataset
and its `dataset_version`, the source normalized file sha256, the vendor
file (`01302019.NASDAQ_ITCH50.gz`) and its sha256, day, symbol, instrument
and venue ids, the window start (09:30:00 ET, the open), the window's
original sequence range, the prefix / window event counts, the book depth
at the window start and at the end, the output sha256 and byte size, and
the exact command. The slice's own `dataset_version` is the usual content
hash of its events, so a re-cut slice is a different dataset by
construction; `test_v117_f4_slice_latency.py` pins the manifest to the
bytes, and the normaliser reproduces the slice byte for byte from a raw
JSONL of its events (checked by `tests/harness/check_slice_memory.py`).
The slice is a benchmark workload with recorded provenance, not a research
dataset: it appears in no pre-registration and no ledger entry, and the
guards derived from it (`benchmarks/latency_budget.json`,
`benchmarks/memory_budget.json`, the `t2t realslice` rows of
`benchmarks/results_tick_to_trade.md`) record the CI run and runner CPU
they were measured on, like the F1 coverage floor.

### 4. Release manifests

A production release additionally records: the four language build
fingerprints (compiler/JVM/rustc versions), image digests for every container
in `deployment/docker/docker-compose.yml` / `deployment/k8s/`, and the
`configs/` tree hash. Deployment rollback = redeploy a prior release
manifest; never rebuild-and-hope.

#### 4.1 The second build (v1.17, F1)

`.github/workflows/ci.yml` job `reproducible-build` builds the Python wheel
twice on every push and pull request with `SOURCE_DATE_EPOCH` set to the
commit date (`git log -1 --format=%ct`; setuptools from the hashed image
lock, `pip wheel --no-build-isolation`) and compares the two sha256 digests:
a mismatch **fails the job**. It then builds `Dockerfile.python` twice with
BuildKit (`--no-cache --provenance=false --sbom=false`, the same
`SOURCE_DATE_EPOCH`, `rewrite-timestamp=true` on an OCI output) and compares
the OCI manifest digests. That comparison is **report-only** (a `::warning`
and a job-summary row, never a failure): the base image is already pinned by
digest (docs/GOVERNANCE.md section 1) and the layer timestamps are rewritten, but
what `pip install` writes inside a `RUN` layer (`.pyc` headers and other
install-time state) is not under the build's control. Both results are
written to the job summary as a table (artefact, digest 1, digest 2,
MATCH / MISMATCH); the first runs on the pull request that added the job
are recorded in CHANGELOG.md under v1.17.

#### 4.2 SBOMs (v1.17, F1)

Every GitHub release carries CycloneDX JSON SBOMs as assets next to
`release-manifest.json`: `sbom-image-{python,cpp,rust}.cdx.json` (syft over
the pushed image by digest, attested with `actions/attest-sbom` and pushed to
GHCR next to the build-provenance attestation) and
`sbom-python-requirements-{image,ci}.cdx.json` (`cyclonedx-bom` over the
hashed lock files, each component carrying the pin and its sha256; attested
with the lock file as subject). Verify an image's attestations, provenance
and SBOM alike, with
`gh attestation verify oci://ghcr.io/ashjha0/intraday-alpha-platform-python@<digest> --repo AshJha0/intraday-alpha-platform`.

### 5. Non-negotiables

- No wall-clock on deterministic paths; no unordered-map iteration
  (conventions §3). Anything violating this fails review.
- Generated data is never committed; it is always reproduced from seeds.
- A manifest with `git_commit = unversioned-workspace` or
  `data_version = no-qc-report` is a research scratch run and can never pass
  promotion gate 5 (GOVERNANCE.md).

#### Known historical manifests

The model ledger is append-only history, so manifests from superseded
rounds are retained rather than rewritten. Only the runs pinning the
*current* dataset hash can be replayed with the §3 recipe from this
snapshot and its default configuration:

| runs | `git_commit` | `data_version` | reconstructible here? |
|---|---|---|---|
| `run_0001…run_0007` | `unversioned-workspace` | `4b77389e…` (round 1, pre generator fix) | no |
| `run_0008…run_0021` | `unversioned-workspace` | `9ac06659…` (round 2) | no |
| `run_0022…run_0033` | real 40-hex commits (`3fff88d7…`, `a87ca00f…`, `0768cbe4…`; `git_dirty` true) | `203c8f54…` (round 3 to v1.3.0; equity flow stopped about 40% into each session) | not with the default configuration; yes with `equities.flow.calibration = "legacy_budget"` (§3.3) |
| `run_0034…run_0040` | `29f08225…` (`git_dirty` true; written by the CI `regenerate` job) | `116b7787…` (current, v1.4.0) | yes |
| `run_0041…run_0047` | `75fd4f79…` (`git_dirty` true; written by the CI `regenerate` job for v1.5.0) | `116b7787…` (current; meta-label features not imputed) | yes |

The rule, not the run numbers, is what to check: a run is replayable from
this snapshot iff its `data_version` equals the value
`iap.experiment.tracker.data_version()` returns for the working tree — every
ML rerun appends more runs (four while the linear gate failed, seven now
that it passes and the tree models and the MLP are fitted), so any fixed
list here goes stale.

Two round-3 changes are visible in that table and are the reason it can be
stated at all. First, `git_commit` is now resolved with `git rev-parse HEAD`
plus a `git_dirty` / `git_dirty_hash` pair, so a manifest identifies the
code; every run before `run_0022` was written by a tracker that recorded the
literal string `unversioned-workspace` even inside a git checkout, and those
runs therefore cannot be tied to a revision after the fact. Second,
`data_version` is a sha256 over the **content** of the normalized `.iap1`
files; it used to hash `qc_report.json`, which embedded an absolute
`raw_dir` path — so the fingerprint moved when the checkout moved and did
*not* move when the data changed. `4b77389e…` and `9ac06659…` are values of
that older, path-dependent scheme and are not comparable to `203c8f54…`
or `116b7787…`.

The round-3 dataset regeneration is what retired `9ac06659…`: labels no
longer span halts or stale gaps and the feature engine consumes only
applied events, so the normalized and feature frames genuinely differ.
The v1.4.0 regeneration is what retired `203c8f54…`: the equity flow
calibration was fixed so that flow reaches the close, which changes every
equity file (the FX files are byte-identical).
Only runs pinning the current `data_version` back any number in the current
`research/ml_reports/ML_REPORT.md` (the latest set, `run_0041…run_0047`); the `unversioned-workspace` runs back
the round-1 and round-2 reports preserved in the papers' dated bodies.

A manifest with `git_commit = unversioned-workspace` or
`data_version = no-qc-report` is a research scratch run and can never pass
promotion gate 5 (GOVERNANCE.md) — which is exactly the status of every
run before `run_0022`.

---

## Security Policy — Intraday Alpha Platform

Implements spec §26 (security, governance, reproducibility). Scope: everything
in this repository plus the images built from `deployment/docker/`.

### 1. Dependency pinning

The dependency surface is deliberately tiny and every entry is pinned and
auditable:

| Layer | Dependency set | Pin mechanism |
|---|---|---|
| Python | `python/pyproject.toml` 1.1.0: numpy, pandas, scipy, scikit-learn, `pyarrow` (Parquet), `jsonschema` + `referencing` (offline contract validation), each with lower and upper bounds; extras `ml` (xgboost, lightgbm), `dev` (pytest, pytest-cov, pytest-xdist, pyyaml) | Bounds in `python/pyproject.toml`; exact versions and sha256 hashes (direct and transitive) in `python/requirements-ci.txt` and `python/requirements-image.txt`; CI installs with `pip install --require-hashes -r python/requirements-ci.txt` and `Dockerfile.python` installs from `requirements-image.txt` the same way, so a pin or a hash that does not match fails the install. `Dockerfile.python` installs the package with its declared dependencies and not the `dev` extra. No network or LLM client anywhere on the trading path (`PLATFORM_CONVENTIONS.md` §13.7). |
| C++ | g++/CMake toolchain, GoogleTest, Eigen (system packages) | Debian package versions recorded at image build; no FetchContent/network downloads in CMake (`cpp/CMakeLists.txt` resolves system packages only). |
| Rust | `serde`, `serde_json` (+ `crossbeam` only where justified) — PLATFORM_CONVENTIONS.md §10 | `rust/Cargo.toml` workspace dependencies; **`Cargo.lock` is the pin** — commit it, and never publish a release image built without a lockfile. Every cargo invocation in CI, `run_all.sh` and `Dockerfile.rust` passes `--locked`, and the toolchain is pinned in `rust/rust-toolchain.toml` (both enforced by `check_deployment.py`). The release profile keeps `overflow-checks = true`. |
| Java (archived) | **none at runtime**; JUnit4 + hamcrest, test scope only | the git tag `archive/java-v1.14.0` is frozen at v1.14.0, not built by CI and, since v1.17 (F9), not built into any image either: there is no `java` image to scan or attest. The local JDK build (`bash build.sh`, JUnit4 jar at `/usr/share/java`) exists only for a reviewer replaying the risk goldens against the Java risk mirror. |
| Images | base images in `deployment/docker/*`, `docker-compose.yml`, `deployment/k8s/*` | Third-party and base images are pinned by tag **and** sha256 digest in the repo (enforced by `check_deployment.py`; Dependabot's docker ecosystem proposes bumps). The platform's own images are named `ghcr.io/ashjha0/intraday-alpha-platform-<lang>:<tag>` (python, cpp, rust); **`python tools/release.py publish` pins the Kubernetes manifests by digest** from `release-manifest.json` (v1.17 F2; `docs/GOVERNANCE.md` section 5). |
| SBOM (release assets, v1.17 F1) | `sbom-image-{python,cpp,rust}.cdx.json`, `sbom-python-requirements-{image,ci}.cdx.json` on every GitHub release | CycloneDX JSON. Image SBOMs: syft 1.54.1 (tarball pinned by SHA-256 in `release.yml`) over the pushed image **by digest**, attested with `actions/attest-sbom` and pushed to GHCR next to the build-provenance attestation (`gh attestation verify oci://<image>@<digest> --repo AshJha0/intraday-alpha-platform` lists both). Python SBOMs: `cyclonedx-bom` 7.5.0 over the hashed lock files, so every component carries the pin **and** its sha256; attested with the lock file as subject. The `reproducible-build` CI job builds the wheel twice under a fixed `SOURCE_DATE_EPOCH` and fails on a mismatch (the python image is compared report-only; `docs/GOVERNANCE.md` section 4). |
| GitHub Actions | `.github/workflows/*.yml` | Every `uses:` is a full commit SHA with the version as a trailing comment, runners are `ubuntu-24.04`, workflows declare least-privilege `permissions:` (enforced by `check_deployment.py`; Dependabot updates the SHAs). |

Adding a dependency in any language is a reviewed change (see GOVERNANCE.md)
and requires: why it is needed, its license, its pin, and its removal plan if
it is a stopgap.

### 2. Vulnerability scanning

What runs today. Since v1.14 (G-P1-7) the dependency audits are
**blocking** (the `supply-chain` job in `ci.yml`), as are the lint and
sanitizer steps. The only way past a finding is a bumped dependency or an
entry in `.github/audit-ignores.json` with a reason and an `expires` date;
`tools/audit_ignores.py` fails the job once an entry has expired:

- Python: `pip-audit --strict` against `python/requirements-ci.txt` and
  `python/requirements-image.txt` (`supply-chain`), plus `ruff check`
  (blocking; rules E, W, F, I, UP and B — `ruff.toml`) and
  `ruff format --check` (`advisory` job).
- Rust: `cargo audit --deny warnings` against `rust/Cargo.lock` and a
  generated lockfile for `rust/features_py`, `cargo deny check bans licenses
  sources` (`rust/deny.toml`: crates.io only, permissive licences, no
  wildcards), all in `supply-chain`; `cargo clippy -D warnings` in `rust`.
- Containers and config: `trivy fs` (lockfile vulnerabilities) and
  `trivy config` (Dockerfiles, compose, k8s) at HIGH/CRITICAL in
  `supply-chain`; `trivy image` on all four images (HIGH/CRITICAL with a
  fix) in `images`. The trivy release tarball is pinned by SHA-256.
- C++: an ASan+UBSan build of the full ctest suite (`cpp-sanitizers` job of
  `weekly-structural.yml` since v2.0 stage 11: weekly and on demand, not per pull request).
- Source code: CodeQL for python, java-kotlin, c-cpp, rust (v1.14) and the
  workflows (`codeql.yml`).

What is required but **not implemented**: refreshing/re-digesting
base images monthly (Dependabot opens the PRs; the cadence is not enforced).
Dependabot alerts are a repository setting that is not yet configured
(`docs/GOVERNANCE.md` section 3). Policy: a release is blocked on any
Critical/High finding without a written, time-boxed waiver from the platform
owner; no automation enforces that blocking.

### 3. Secrets and configuration separation

Hard rule: **this repository contains no secrets, and configs are not
secrets.**

- `configs/<domain>/*.json` (instruments, venues, generator, risk limits, execution,
  strategies) are *behavioral configuration*: reviewed, versioned, deployed
  via the `iap-configs` ConfigMap / baked read-only into images. They are
  world-readable by design; nothing in them may be secret.
- Credentials (venue/API keys, DB passwords, Grafana admin) enter only
  through the environment or an orchestrator secret store:
  - docker-compose: `GRAFANA_ADMIN_PASSWORD` must come from the host
    environment or a git-ignored `.env` file — compose fails fast if unset
    (`${GRAFANA_ADMIN_PASSWORD:?...}`).
  - Kubernetes: `Secret` objects created out-of-band (e.g.
    `iap-grafana-admin`), referenced by name only from the manifests
    (`deployment/k8s/grafana.yaml`); never `--from-literal` values committed
    in any script.
- Environment variables carrying secrets are never logged; the structured
  loggers (rust `JsonlLogger`, python logging) log explicit fields only —
  never a dump of `os.environ`/`std::env`.
- Any secret that touches a commit (even briefly) is considered burned:
  rotate it, then purge history.

### 4. Runtime hardening

- All containers run as a dedicated non-root user (uid 10001 for iap images);
  Kubernetes enforces `runAsNonRoot`, `seccompProfile: RuntimeDefault`,
  dropped capabilities, no service-account token mounts
  (`automountServiceAccountToken: false`), a read-only root filesystem on the
  platform-metrics pod (writable `/tmp` emptyDir and its own state PVC only), and the
  namespace carries `pod-security.kubernetes.io/enforce: restricted`.
- Ingress and egress are default-deny; only the flows in
  `deployment/k8s/networkpolicy.yaml` are open (prometheus→platform-metrics:8080,
  prometheus→alertmanager:9093, grafana→prometheus:9090, ingress→grafana:3000,
  DNS for every pod, alertmanager→tcp/443 for the webhook; the operator→admin
  rule went with the Java admin API in v1.17). NetworkPolicies take effect only
  on a CNI that implements them.
- docker-compose publishes 8080, 9090 and 9093 on `127.0.0.1` only; the
  metrics server's bind address is `IAP_BIND_ADDR` (0.0.0.0 inside the containers).
- Raw market data is immutable (spec §1): pipeline outputs are written once
  per dated run; nothing rewrites `data/raw/` in place.
- **Monitoring API exposure** (`python -m iap.metrics serve`, port 8080):
  `/metrics`, `/health`, `/ready` and `/status` are **unauthenticated** and
  must stay behind the NetworkPolicy (only `prometheus → platform-metrics:8080`
  is open) — they expose position, P&L and limit data. Since v1.17 the process
  has NO write surface (the Java `POST /admin/*` API is retired with its
  image; the paragraph below describes it for the record). The write surface was
  authenticated: `POST /admin/{kill,clear,override,roll}` requires a bearer
  token from `$IAP_ADMIN_TOKEN` or `$IAP_ADMIN_TOKEN_FILE` (a Kubernetes
  Secret), compared in constant time; **with no token configured the routes
  are not registered at all** (404) — the platform never exposes an
  unauthenticated kill endpoint. The token is never logged: the audit records
  its sha256 (`PLATFORM_CONVENTIONS.md` §12.5). Handlers run on a bounded pool
  with `maxReqTime`/`maxRspTime`, so a client that connects and stalls cannot
  occupy the server, and — because the metric registry is lock-free (§12.4) —
  cannot stall the trading thread either.
- **Durable state** (`$IAP_STATE_DIR`) holds positions, P&L and the audit
  logs. It is written by the container's non-root user on a mounted
  volume/PVC and must never be baked into an image or committed;
  `.gitignore` excludes it.

### 5. Enforcement status

Several controls named in this policy and in `GOVERNANCE.md` are repository or
operations settings that are **not enforced by any file in this repository and
are not yet configured**: branch protection and required status checks on
`main`, the review counts (two reviewers for risk and contracts changes need a
second maintainer), signed release tags (a practice; nothing verifies it),
Dependabot alerts and security updates, off-host shipping of the audit logs,
image vulnerability scanning before digest-pinning, and delivery of alerts to a
real receiver (the Alertmanager webhook URL is a secret the operator supplies).
`docs/GOVERNANCE.md` lists each with the exact `gh api` commands.
Until they are applied, treat the corresponding statements as requirements.

### 6. Reporting

Suspected vulnerabilities or leaked credentials go to the platform owners
(CODEOWNERS for `deployment/` and `docs/governance/`: today `@AshJha0`) immediately; kill-switch
and incident procedure in `docs/RUNBOOKS.md`
applies if production trading could be affected.

---

## Trust root: maintainer key, pinned version, rotation and revocation

Outside reviewer, 2026-10-18 (trust point): an independently authenticated maintainer-key
fingerprint, a pinned trust-root version, and a key rotation and revocation procedure. The key
registry mechanics (agent keys, blackboard signatures, where the private key lives) are in
[docs/GOVERNANCE.md](GOVERNANCE.md).

### The pinned trust root (version 1)

| Field | Value |
|---|---|
| Trust-root version | **1** (in force since commit `1d48e17`, 2026-10-11) |
| File | `research/agents/trust_roots.json` |
| Canonical sha256 of the file (`json.dumps(sort_keys=True, separators=(",", ":"))`) | `6fb5818c1303c81f74118e64302bb44916e618b755f1af3907720504a11283bb` |
| Maintainer id | `ashjha0` |
| Algorithm | Ed25519 |
| Public key (hex, 32 bytes) | `02a3e278cda3efac7dcd266fb8a3821a1415f0725a73f8b255aac84083c2106c` |
| **Fingerprint** (sha256 of the 32 raw public-key bytes) | `0723075249ff61946e0416f56c681cc961fba94beb13913a2c1c0f398b41eb3f` |
| Revoked keys | none |

The same values are pinned in `PINNED` in [tools/verify_signatures.py](../tools/verify_signatures.py).
The tool fails when the trust root differs from the pin, lists a maintainer that is not pinned, or
still lists a revoked key. A test checks that the pin, this table and the file agree.

Recompute the fingerprint yourself:

```bash
python -c "import hashlib,json;k=json.load(open('research/agents/trust_roots.json'))['maintainers']['ashjha0'];print(hashlib.sha256(bytes.fromhex(k)).hexdigest())"
```

### Verify every signature

```bash
PYTHONPATH=python/src python tools/verify_signatures.py --trust-ref origin/main --strict
```

This checks the trust root against the pin, every tracked signed JSON document (preregs, the REL-08
rule), the agent key registry signature, the main blackboard (chain, anchors, agent signatures) and the
archived fork boards. It prints `OK` or `FAIL <reason>` per item and exits 1 if anything fails. A blackboard anchor whose commit is not in the clone (a shallow fetch) is reported as
`UNVERIFIABLE-HERE`, never `OK`; `--strict` (reviewer and release, on a full-history clone) makes that
fatal too. Signature, chain and trust-root problems always fail.

### Why the repository alone is not an independent anchor

Everything above lives in the repository. Anyone who can rewrite the repository can replace the key,
the pin and this page together, and every check would still pass. The anchor is independent only if
the fingerprint is also published somewhere the repository cannot change, under the owner's own
account, so that a reviewer can compare the two.

#### What the owner must publish (done 2026-10-20 as a public gist; a second place, e.g. the profile README, is still better)

Publish the block below **verbatim** in at least one place outside this repository that only the
owner controls. Two are better:

1. the GitHub profile README of `AshJha0` (the `AshJha0/AshJha0` repository), and
2. the notes of the GitHub release `v1.18.0` (or a pinned gist under the same account).

```text
intraday-alpha-platform maintainer signing key
trust-root version: 1
maintainer id:      ashjha0
algorithm:          Ed25519
public key:         02a3e278cda3efac7dcd266fb8a3821a1415f0725a73f8b255aac84083c2106c
fingerprint sha256: 0723075249ff61946e0416f56c681cc961fba94beb13913a2c1c0f398b41eb3f
trust_roots.json canonical sha256: 6fb5818c1303c81f74118e64302bb44916e618b755f1af3907720504a11283bb
revoked: none
```

Once it is published, add its URL(s) to the table below in a reviewed PR. A reviewer then compares
that out-of-repo block with this table and with `PINNED`. If they disagree, the reviewer should trust
none of the signatures.

| Published at | Date | By |
|---|---|---|
| https://gist.github.com/AshJha0/e1402d88fc53b80fd0d2627e10ee9c3f (public gist, account AshJha0, block verbatim) | 2026-10-20 | AshJha0 (created by Claude at the owner's request) |

### Rotation (planned key change)

1. On the signing machine, generate the new key outside the repository
   (`python -m iap.agents.cli maintainer-keygen --maintainer ID --keyfile PATH`, PATH outside the repo; keep the private key next to the old one and back it up).
2. **Publish first.** Add the new fingerprint, as trust-root version N+1, to the out-of-repo
   locations above, next to the old one. Mark it "pending rotation" until step 5.
3. In one PR: add the new public key to `research/agents/trust_roots.json`. Bump `PINNED` (version,
   canonical sha256, maintainers) and the table on this page. Record the change in `CHANGELOG.md`.
   Sign the PR description, or a `research/agents/rotation_<N+1>.json` note, with the **old** key, so
   the old key vouches for the new one.
4. Re-sign the agent key registry (`agent_pubkeys.sig.json`) with the new key. Do **not** re-sign
   existing preregs. They stay valid history under the key that signed them, and the old key stays in
   the trust root as long as signed documents need it.
5. After merge, run `tools/verify_signatures.py --trust-ref origin/main`, then update the out-of-repo
   block to "version N+1 current".

### Revocation (key compromised or lost)

1. **Publish first.** Update the out-of-repo block: state the compromised fingerprint as REVOKED,
   with the date and the last commit you trust (the last one before any possible misuse).
2. In one PR: remove the key from `research/agents/trust_roots.json` (if it is retained only for
   history, it must be listed under `revoked`). Add its fingerprint to `PINNED["revoked"]`. Bump the
   version. Add a replacement key by the rotation steps above.
3. `tools/verify_signatures.py` then fails every document signed by the revoked key. For each one,
   the owner decides: re-sign with the new key after re-reviewing it (record the decision in
   `CHANGELOG.md`), or withdraw it. A confirmatory look that a revoked key authorised is treated as
   spent and unauthorised until it is re-reviewed.
4. Any commit after the last trusted commit that touches `research/agents/`, `research/preregs/`,
   `research/holdout_registry.json` or `tools/verify_signatures.py` must be audited before it is
   relied on.

### What code cannot enforce

The checks prove that signatures match the pinned key. They cannot prove who holds the private key,
or that the out-of-repo publication happened. Until the owner publishes the block, the trust root is
self-asserted.

---

## Trust roots and model promotion (v1.15, G-P1-2 / G-P0-1)

Pinned version, key fingerprint, rotation and revocation: [docs/GOVERNANCE.md](GOVERNANCE.md).
Verify everything (full-history clone): `PYTHONPATH=python/src python tools/verify_signatures.py --trust-ref origin/main --strict`.

### What a signature proves, and the four controls it is not (v2.0 stage 11, G1)

An Ed25519 signature under a maintainer key proves **integrity**: the signed document (a
pre-registration, a rule, the key registry) did not change after it was signed, and the signer held
the key. It proves nothing about who ran what, or whether anyone outside the project looked. The
v1.19 review (plan section 2.3) found the words "signer != runner" read as an independence claim
where the mechanism was an environment variable; that wording is retired. The programme keeps four
controls apart, each with its own mechanism and its own evidence class (U0 section 10):

| control | what it establishes | mechanism | status on `main` (2026-10-25) |
|---|---|---|---|
| **integrity** | the document is the one that was signed | this page: trust roots read from `origin/main`, `verify_signatures.py --strict`, the governance CI job | enforced |
| **execution separation** | the developer's identity could not read the sealed inputs or alter the run | operating-system accounts (`iap-dev`, `iap-sealed-{a,b,c}`, `iap-runner`), the sealed service over a named pipe, the registered runner CR1 ([docs/GOVERNANCE.md](GOVERNANCE.md)); **not** an environment variable | built and tested on synthetic data; no registered run executed; repository settings of docs/GOVERNANCE.md section 7 not yet set |
| **independent review** | a reviewer approved the protocol before the run | the outside reviewer's written decisions (`docs/release/REVIEWER_*`), the `sealed` environment's required reviewer | decisions on file for F3 and REL-08; the environment does not exist yet |
| **independent reproduction** | a party outside the project recomputed the result | P2 ([../REPRODUCTION.md](REPRODUCTION.md) section 6); gate F7 | open: the pinned image reproduces B8 v2 and the REL-08 scorecard "in the pinned image by CI", no independent party has run it |

A result's class follows from which controls held when it was produced, never from the signature
alone: *independently confirmed* needs execution separation under custody separate from the
developer plus independent review; *mechanically isolated* is CR1 under the developer's custody;
everything produced so far is at most *development-supported*. `iap.agents.analysis_identity.require_independent_runner`
remains in the code as the tamper-evidence check it always was (the declared actor and the absence
of a signing key in the runner's environment); it is not cited as an independence control anywhere
in the documentation.

### What is enforced

- `research/agents/trust_roots.json` lists the maintainers' Ed25519 **public**
  keys. Since v1.15 it is non-empty (maintainer `ashjha0`).
- `research/agents/agent_pubkeys.sig.json` is a maintainer signature over the
  canonical digest of `research/agents/agent_pubkeys.json`. It is required
  even while the registry is empty.
- CI (`governance` job) runs
  `python tests/harness/check_agent_keys.py --trust-ref origin/main --require-signed`
  and `python -m iap.agents.cli verify-board --trust-ref origin/main`. Trust
  roots are read from `origin/main`, so a pull request cannot vouch for a key
  it adds. An unsigned or stale-signed registry, or no trust root at all,
  fails the job.
- Bootstrap: only while `origin/main` lists no maintainer, the working tree's
  roots are used (a warning is printed). After the PR introducing the first
  key merges, this never fires again.
- Model promotion (`python -m iap.mlops promote <id>`,
  `ModelRegistry.promote`) refuses a model whose `content.json` is missing or
  does not match the hash re-derived from the verified artefact, and writes
  `promotion.json`. CI runs `python -m iap.mlops check-promotions`, which
  fails if any promoted model no longer matches.
- Promotion also requires a valid model card (`model_card.json`,
  `iap.mlops.model_card`; write it with `python -m iap.mlops card <id> --spec
  <json>`): data/sessions, prereg id and hash, metrics net of costs,
  calibration (or null), known limitations, intended use and the owner
  sign-off reference, bound to the record hash. A missing or invalid card is
  a `RegistryError`; `check-promotions` re-validates it.

### Where the private key lives

`C:/Users/ashis/.iap_keys/maintainer.key` (JSON `{maintainer id: private key
hex}`), outside the repository. Never commit it, paste it or print it. Keep
an offline backup (encrypted USB / password manager secure note).

### Re-signing after a registry change

```
PYTHONPATH=python/src python -m iap.agents.cli sign-registry \
  --maintainer ashjha0 --keyfile C:/Users/ashis/.iap_keys/maintainer.key
```

Commit the updated `agent_pubkeys.sig.json` with the registry change.

### Rotation (planned)

1. Generate the new key under a new id:
   `python -m iap.agents.cli maintainer-keygen --maintainer ashjha0-2027 --keyfile <new keyfile outside the repo>`
   (adds the public key to `trust_roots.json`).
2. Open a PR with only the `trust_roots.json` change; the old key keeps the
   registry trusted while it is reviewed. Merge it.
3. Re-sign the registry with the new key (`sign-registry --maintainer ashjha0-2027 ...`)
   and, in the same PR, remove the old id from `trust_roots.json`. CI reads
   the roots from `origin/main`, which still lists both, so the new signature
   verifies. Merge.
4. Retire the old private key file (keep the backup until the rotation is on main).

### Restore / compromise

- **Lost key, backup available:** copy the backup to the keyfile path; nothing
  in the repository changes.
- **Lost key, no backup:** the registry cannot be re-signed. The repository
  owner (who controls branch protection) edits `trust_roots.json` to a newly
  generated key and re-signs in one PR, merged with an admin review; until it
  lands on main CI keeps trusting the old root, so the PR's governance check
  fails by design and needs the owner's override. Record the event in the
  CHANGELOG.
- **Compromised key:** same as "no backup", and additionally review every
  registry change signed since the suspected compromise.

---

## Analysis identity: what a signed prereg binds

Review finding 1 (outside reviewer, 2026-10-18): prove that a registration covers every input
that can change a result, and that no entry point skips the check. Code:
[`iap.agents.analysis_identity`](../python/src/iap/agents/analysis_identity.py); tests:
[`test_analysis_identity_v2.py`](../python/tests/test_analysis_identity_v2.py).

### Inputs that can change a result, and how each is bound (identity v2)

| Input | Where it lives | Bound by |
|---|---|---|
| Analysis scripts | the runner and study scripts | `scripts`: path -> sha256 (LF-normalised) |
| Library code: features (registry, spec), labels, cost, fill and venue models | `python/src/iap/**` (every file, package data included) | `code_tree_sha256` |
| Configs: instruments, venues, fee tiers, regulatory fees, venue latency, execution, risk, strategies, prereg templates | `configs/**` | `configs_tree_sha256` |
| Reference data: feature registry JSON | `data/reference/**` | `reference_tree_sha256` |
| Study files outside those trees (calibrations, venue files, predeclarations under `research/`) | declared with `prereg_bind_identity.py bind --inputs` | `inputs`: path -> sha256 |
| Dependencies | `python/requirements-ci.txt` (hashed lock) | `deps_lock_sha256` |
| Installed packages | the runner's environment | `environment`: installed version of every package the lock pins |
| Interpreter | the runner process | `interpreter`: implementation and `major.minor.micro` |
| Datasets | `data/real/<dataset>` | `data_versions`: content hashes of the normalized files; the holdout guard and `dataset_version_of` re-hash the files against the manifest, so a tampered tree is refused, and a re-ingested tree is a different version |
| Hypothesis family, metrics, thresholds | the prereg | `family`, `metrics`, `thresholds` (the runner declares them; they must match) |
| Commit | git | `commit`: HEAD or an ancestor of HEAD |

The check is all-or-nothing: `verify_identity` lists every mismatch and refuses.

### Versions

A block with no `identity_version` is **v1** (scripts, `*.py` code tree, commit, data versions,
family, metrics, thresholds) and is verified exactly as it was when signed. The three v1 documents
signed on main (`research/preregs/b8v2_prereg.json`, `research/v118/null_protocol_prereg.json`,
`research/v118/rel08_rule_signed.json`) keep verifying under v1 and are not re-signed. Their v1
coverage does not include configs, reference data, the lock, the environment or the interpreter;
that gap is stated, not closed retroactively. Every new bind writes v2. An unknown version is
refused.

### Entry points

| Runner | Identity check |
|---|---|
| `tools/r7_confirm.py` | `verify_analysis_identity` -> `verify_identity` (legacy R7 prereg: no block, look spent) |
| `tools/rel06_known_null.py` | `verify_prereg` -> `verify_identity` |
| `research/execution/b8v2/run_b8v2.py` | `verify` -> `verify_identity` |
| `python -m iap.research.rel08_screen` | `gate` -> `rel08_rule.check_identity` -> `verify_identity` |
| `python -m iap.research run` (blackboard preregs) | `prereg_gate.require` -> `verify_identity` |
| REL-08c (`iap.auction.rel08c_extract`), REL-09 (`stale_quote`), REL-10 (`volume_clock_ofi`) | no confirmatory runner on main yet (no signed prereg exists); a runner added later consumes a signed document, which the coverage test forces into the list above |

Two backstops make a skipped check fail closed:

1. **The holdout guard.** A restricted (spent / unread) read authorised by an identity-bound prereg
   document re-checks the bound scripts, trees, lock, environment and interpreter, and that the
   dataset version is one the prereg bound (`holdout_registry.check_prereg_identity`). An entry
   point that forgets its own check still cannot read the holdout under that prereg.
2. **The coverage test.** `test_no_prereg_consumer_skips_the_identity_check` scans `tools/`,
   `research/` and `python/src/iap/` for every file that consumes a signed prereg or rule and fails
   unless it is a listed runner whose identity call is present, or an explicitly listed non-runner
   (binders, signers, `verify_signatures.py`).

### What code cannot enforce

Legacy preregs signed before v1.18 bind nothing; they authorise replays of spent looks only, never
new confirmatory evidence. In-sample reads need no prereg, so the identity binds confirmatory runs,
not exploration. Someone who edits a runner AND this test is stopped by review only.

---

## Data access paths to `data/real`

Review finding 2 (outside reviewer, 2026-10-18): an auditable inventory of every supported route that
reads real data, the guard each uses, evidence that restricted reads are **blocked** (not only
logged), a record of attempted violations, and the unsupported routes stated plainly. Inventory taken
on main at 2026-10-18; guard code is
[`iap.research.holdout_registry`](../python/src/iap/research/holdout_registry.py), tests are
[`test_data_access_blocking.py`](../python/tests/test_data_access_blocking.py) and
[`test_holdout_registry.py`](../python/tests/test_holdout_registry.py).

### How a read is decided

1. **Entry-point guard.** `guard(dataset_dir, purpose, prereg_id=...)` (or `guard_or_exit` /
   `guard_session_label` for CLIs) looks the dataset up by content hash and `dataset_version`, and by
   (session, symbol) scope, in the sealed `research/holdout_registry.json`. It decides from the
   manifest and the registry **before any data file is opened**; spent / unread / unregistered scope is
   refused unless a signed prereg listed for that scope authorises it (and, for an identity-bound
   prereg, its bound code, configs, lock, environment and dataset version match; see
   [docs/GOVERNANCE.md](GOVERNANCE.md)). Only an allowed read re-hashes the files, and a byte
   that differs from the manifest is refused.
2. **The choke point (v2.0 D1).** Every reader calls `iap.core.guard.check(path, purpose)` before it
   opens a file: `iap.core.codec.read_iap1` / `iter_iap1` (normalized events) directly, and every
   parquet / numpy read through the guarded wrappers `iap.io.read_parquet`, `iap.io.parquet_file`,
   `iap.io.read_table` and `iap.io.np_load` (the only module in `iap` that calls `pandas.read_parquet`,
   `pyarrow.parquet.ParquetFile` / `read_table` or `numpy.load`; the AST test
   `test_guard_hook.py::test_no_raw_reader_outside_iap_io` keeps it so). `check` delegates to ONE
   registered hook, `iap.research.holdout_registry.reader_guard` (registered when that module is
   imported; the package root installs a lazy hook that imports it on the first read, and `iap.core`
   itself never names `iap.research`, D-11). With no hook registered `check` fails closed on any file
   inside a real dataset or under `data/real`. A file inside a real dataset is allowed only if a guard
   already allowed that dataset **in this process** (an in-process set; the v1.18 environment variable
   `IAP_HOLDOUT_AUTHORISED` that carried the set to child processes is gone, so a child re-decides and
   without a prereg id only in-sample scope passes), or if a fresh guard with no prereg allows it. A
   manifest-less file under `data/real` is checked by the session date in its name. The hook is
   dataset-granular.
3. **Record.** Every decision, allowed or refused, appends one JSON line to
   `data/real/holdout_access.jsonl`. Lines are hash-chained (`prev_sha256`); an edited, deleted or
   reordered line fails `python -m iap.research.holdout_registry verify-log` (exit 1). A restricted
   read that cannot be logged is refused.

Evidence (tests): a spent dataset under any directory name is refused with no event or feature file
opened (open-call spy); `read_iap1`, `iter_iap1` and `load_features` refuse a restricted dataset before
opening a file or calling `read_parquet`; refusals appear in the log; edit / delete / reorder of a log
line is detected; appends never rewrite earlier bytes; a tampered tree under an in-sample manifest is
refused.

### Supported routes and their guard

Lines refer to main on 2026-10-18. "Backstop" means the route reads through a guarded low-level
reader, so it is blocked even without its own guard.

| Route | Reads | Entry guard | Backstop |
|---|---|---|---|
| `python -m iap.research run` / `combine` / `power-real` | dataset.json, features, normalized | `_guard_dataset` -> `guard(prereg_id=--holdout-prereg-id)` | yes |
| `agents.research_assistant.run_in_sample` (agent tool) | dataset via the `iap.research run` subprocess | `guard(purpose="research_assistant")` | yes |
| `agents.mcp_server` tools | ledger, reports, blackboard (no market data) | n.a. | n.a. |
| `python -m iap.research.screen` | features | `assert_not_holdout` -> `guard` (`--allow-holdout` now lifts only the prereg's exclusion list; the registry still decides) | yes |
| `python -m iap.research.breadth_book` | features | `guard(prereg_id)` | yes |
| `research.two_venue.load_merged_session` | normalized iap1, two venues | `guard_session_label` (selected session) | yes |
| `research.execution_benchmark` (CLI and library), `research/execution/b8/diagnose_depth_walk.py` | normalized iap1 | `check_data_access` -> `guard_or_exit` | yes |
| `research/execution/b8v2/run_b8v2.py` | B8 v2 datasets | `guard(prereg_id, sessions, symbols)` + identity | yes |
| `python -m iap.research.rel08_screen` | screen data | `gate` -> `guard` + state / version + identity | yes |
| `research.rel04c_job.stage_register` | dataset | `guard` | yes |
| `tools/rel06_known_null.py` | features (`pq.read_table`) | `guard_or_exit` | no (pyarrow direct) |
| `python -m iap.paper.daily` | dataset | `is_forbidden` + `guard` (an ingest of a missing dataset reads raw ITCH first, see below) | yes |
| `models.lob_real.build_cache` | normalized -> `.npz` / `.X.npy` caches | `refuse_unit_name` + `guard` | yes |
| `models.lob_train` | LOB caches (`load_unit`) | `refuse_unit_name` per unit (name only) | no |
| `tools/r7_confirm.py` | R7 holdout via guarded `iap.research run` subprocess | owner flag, signed prereg, identity | yes |

#### Routes with no entry guard of their own (blocked by the reader backstop)

These read normalized events or features through the guarded readers, so a restricted dataset is
refused at the first file; they do not log a purpose of their own, and they cannot read restricted
scope at all (no prereg id is passed): `python -m iap.features`, `python -m iap.mvp.real`,
`python -m iap.execution.dark --data-dir`, `python -m iap.execution.calibration --events`,
`python -m iap.execution.volume_curve --events`, `research/alpha_reports/run_all.py --dataset-dir`,
`research/ml_reports/run_ml.py`, `tools/v116_capacity_curve.py`, `tools/v116_monetisation_study.py`,
`tools/extract_real_slice.py`.

#### Routes closed by D1 (v2.0 stage 2): formerly allowlist or name only

Since D1 these read through `iap.io`, so a file inside a registered (spent / unread / unregistered real)
dataset is refused before the library opens it, whatever the CLI argument said:

| Route | Before D1 | Now |
|---|---|---|
| `python -m iap.research.maker_real` (labels at the former `maker_real.py:440`, events, features), `quoting_real` | hard-coded in-sample `SESSIONS`; parquet read directly | `iap.io.read_parquet` / `parquet_file` / `read_table`, the choke point |
| `research.events`, `research.drift`, `portfolio.covariance`, `execution.signal`, `models.lob_real` (events and `.npy` / `.npz` units), `models.lob_train` (tick) | pyarrow / numpy directly | `iap.io`, the choke point (`refuse_unit_name` still applies to cache names under `data/real`) |
| `python -m iap.research.conformal_report --input x.parquet` | none | `iap.io.read_parquet` |
| `python -m iap.mlops` (`np.load` of the monitor input) | none | `iap.io.np_load` |
| `python -m iap.research.auction_real` (incl. `holdout`) | in-sample summary sha | unchanged: CSV reads of extracted auction files (not parquet / numpy); see Residuals |

#### Raw-feed readers (ingest: read raw files to write datasets)

`python -m iap.marketdata ingest` (`Itch50Reader`), `marketdata.process_delete`,
`marketdata.process_iex`, `marketdata.iex` (pcap), `marketdata.breadth`, `auction.rel08c_extract`,
`auction.stream`, `research.rel04c_universe`, `research/v118/rel12_oracle/itch_cross_volume.py`,
`tools/iex_hist_conformance.py`. These do **not** consult the registry: a raw ITCH / IEX file has no
manifest, and ingesting a holdout day is a supported, planned step (R7, IEX overlap days). Their output
is a dataset that every analysis route above guards. `tools/r7_holdout_check.py` checks raw-file
hashes against the signed prereg before an ingest; it is a check, not a guard.

### Unsupported routes

Anything not listed above is **unsupported**, and the platform makes no claim about it: ad-hoc
`pandas.read_parquet` / `pyarrow` / `numpy.load` on files under `data/real` from code outside `iap`
(inside `iap` the AST test forbids them), a notebook or REPL that opens files directly, copying data out
of `data/real`, the raw-feed readers used for analysis rather than ingest, and `Itch50Reader` /
`open_feed` / `load_manifest` / `decode_iap1` called directly. There is no environment variable that
lifts a decision any more. No code can stop a person with file-system access from reading bytes
on their own disk; the controls above make the *supported* routes refuse, record every decision, and
make the record tamper-evident. A result produced through an unsupported route is not evidence under
the release programme.

### Residuals

- The hook is dataset-granular. Authorisation no longer crosses a process boundary: a child process
  that needs restricted scope must receive the prereg id and guard for itself (D-55 lists the one
  route that relied on inheritance, the features step of `research/v118/rel04_verify/run_rel04_verify.py`).
- CSV reads (`iap.auction.*`, `research.auction_real`, `research.conformal_report --input x.csv`) are
  outside the choke point: they read extracted auction files and report inputs, not datasets; a CSV
  copy of a sealed partition is a sealed read by the person who made it (docs/GOVERNANCE.md section 4).
- The log is tamper-evident, not tamper-proof: a person can truncate it at the end, or rewrite the
  whole chain from line one. The log is git-ignored with the data.
- `tools/rel06_known_null.py` (under `tools/`, outside `iap`) still calls `pq.read_table` after its own
  `guard_or_exit`; the sealed partitions of v2.0 are reached only through the sealed service
  ([docs/GOVERNANCE.md](GOVERNANCE.md)), never through these readers.

---

## Sealed data: identities, partitions, the service, and the single-machine limit

v2.0 stage 2, D2 (U0 section 2; v1.19 specification revision 9 D2 as carried). Code:
[`tools/sealed_service.py`](../tools/sealed_service.py) (service, client framing, checkpoint rules),
[`tools/admin/create_sealed_accounts.ps1`](../tools/admin/create_sealed_accounts.ps1) (accounts and ACLs,
run by the owner on 2026-10-25, decision record DR-5),
[`tools/admin/isolation_probe.py`](../tools/admin/isolation_probe.py) and
[`python/tests/isolation/`](../python/tests/isolation) (the identity x partition matrix and the service
core tests). The runner that consumes the service is in [docs/GOVERNANCE.md](GOVERNANCE.md).

### 1. What exists on the workstation

| identity | kind | owns | may do |
|---|---|---|---|
| the owner's normal Windows user (`iap-dev` by role) | development identity | the repository checkout, the research data under `data/` | everything in-sample; **nothing** under `C:\iap-sealed` |
| `iap-sealed-a` | standard local user | `C:\iap-sealed\a` (Track A, the H1 window) | runs one `sealed_service.py serve --partition a` |
| `iap-sealed-b` | standard local user | `C:\iap-sealed\b` (Track B, the ML test year) | runs one service for `b` |
| `iap-sealed-c` | standard local user | `C:\iap-sealed\c` (Track C, forward confirmation) | runs one service for `c` |
| `iap-runner` | standard local user | `C:\iap-sealed\runner` (its private cache) | the self-hosted GitHub runner; the only account the services answer |

Each partition's access list names its owner and SYSTEM only; `BUILTIN\Administrators` was removed as the last
step of the script and the elevated administrator is denied read (observed by the owner, DR-5). The development
identity is denied read, write, enumeration and the two indirect routes (a report path and a cache directory
built under a partition) on all four partitions; the committed test matrix is `test_identity_partitions.py`
(the development half runs on the owner's machine without credentials; the sealed and runner halves need the
accounts' passwords in a file named by `IAP_ISOLATION_CREDENTIALS` and are skipped otherwise).

### 2. The service

One instance per partition, started under that partition's identity, listening on the named pipe
`\\.\pipe\iap-sealed-<partition>`. The pipe is the only route from any other identity to sealed bytes.

**Authentication is the operating system's.** The service impersonates the connected client just long enough to
read the client token's account name and reads the client's process id from the pipe; it refuses any account
other than `iap-runner` and (when `registered_images` is configured) any client whose process image hash is not
a registered runner binary. There is no bearer token and no per-boot token file: a client cannot assert an
identity, the kernel reports it.

**Authorisation is service-side**, in this order, every step audited:

1. caller account, then process image;
2. no pending checkpoint (a read whose checkpoint the runner has not yet published blocks every further read;
   a restart keeps the pending state and re-announces it);
3. the request carries a **signed** prereg (Ed25519 under the trust-root copy pinned in the partition at install),
   whose `registered_run` block names this partition and the requested `dataset_hash_id`;
4. the registration ledger (keyed by the prereg's canonical sha256 and indexed by prereg id) has seen neither:
   **one run per registration**; a second request with the same hash, or the same prereg id re-signed at a new
   commit, is refused and logged;
5. every file of the named dataset still matches its recorded sha256 (**hash verification precedes any read**);
   a mismatch is recorded as a failed registration and cannot be retried without a new signed registration;
6. the ledger entry, the hash-chained audit line and the pending checkpoint are written **before the first byte
   is served** (write-before-serve).

The dataset is streamed file by file with a header (`name`, `size`, `sha256`) the client verifies on arrival.
After the run the runner stores the result hash (`store_result`) and publishes the checkpoint with the commit
that holds it (`publish`); `verify_result` compares a committed hash with the service's copy.

### 3. Checkpoints and the repository anchor

A checkpoint is exactly `{seq, chain_head, utc, prereg_id, dataset_hash_id}`: the audit chain head after the
served read, and two identifiers already public in the repository. It never carries a path, a credential, a
host name or an account name (`checkpoint_problems` enforces the rule; a test greps the committed file). The
runner appends each checkpoint to `research/audit/CHECKPOINTS.jsonl` on the `results` branch, so the sealed
service's private audit chain has a public anchor: `python tools/sealed_service.py verify-checkpoints` checks
the committed file (anyone, CI) and `verify --root <partition>` checks the local chain (the sealed identity).

### 4. The single-machine limit

Everything above runs on one workstation, and the owner is that workstation's administrator, developer,
approver and the holder of every sealed credential. The controls stop the **development identity's processes**:
the owner's own user cannot read, list or write a partition, cannot connect to the service, and cannot start a
registered run without the GitHub environment approval. They do not stop the administrator: an administrator
can take ownership of a partition, read the service's state, reset an account's password or stop the service.
Nothing in this repository claims otherwise. Consequences, stated plainly:

- the evidence class of anything produced through this machinery is **mechanically isolated**, not
  **independently confirmed** (U0 section 2 property 6; section 10 wording); the class upgrades only when an
  approval or custody authority other than the developer exists (owner item O-3);
- the audit chain is tamper-evident, not tamper-proof: a person with administrator rights can rewrite it from
  line one; the committed checkpoints make a rewrite after a publish detectable, not impossible (D-24 stands);
- encryption at rest of the partitions is an operating-system setting (BitLocker or EFS), not something the
  account script enables (defect D-52);
- the process-image check depends on the service being allowed to query the client process; when Windows
  denies that query between two standard users the service refuses (fail closed) and the owner either grants
  the query right or leaves `registered_images` empty and relies on the account check alone (D-48).

### 5. Install (owner)

1. Accounts and partitions: `tools/admin/create_sealed_accounts.ps1` (done, DR-5).
2. An interpreter and a checkout the service accounts can read: CPython 3.12.10 for all users at `C:\Python312`
   with the pinned stack, and the repository cloned at `C:\iap\repo` (read-only to everyone but the owner):
   [`tools/admin/install_all_users_python.md`](../tools/admin/install_all_users_python.md). The service resolves
   `python/src` from its own checkout (or `IAP_REPO` when run from a copy), so it runs from any location.
3. The services: [`tools/admin/install_sealed_services.ps1`](../tools/admin/install_sealed_services.ps1)
   (elevated; asks for each sealed account's password). Per partition it runs `sealed_service.py install` **as the
   sealed identity** (a transient scheduled task: the administrator has no access to the partition by design), which
   creates `C:\iap-sealed\x\service\`, copies `research/agents/trust_roots.json` and writes `config.json` =
   `{"allowed_accounts": ["iap-runner"], "registered_images": []}` (add the sha256 of the runner's `python.exe` to
   enable the image check); runs `sealed_service.py check` the same way; registers the task "IAP sealed service x"
   = `python tools/sealed_service.py serve --partition x --root C:\iap-sealed\x` as that account, "run whether user
   is logged on or not", at system start-up (a service account never logs on, D-64), no time limit; starts it; and
   pings the pipe (`sealed_service.py ping`: the owner's own call gets the audited `caller_account` refusal, which is
   the proof the service is up). It also creates the development-readable `C:\iap\daily` (iap-runner writes, Users
   read) for the DD1 development and validation partitions.
4. Datasets enter a partition in one of two ways, both recorded in `service/datasets.json` with every file's sha256:
   the `ingest` endpoint (DD1: `iap-runner` streams the files, the service writes, re-hashes, checks the content hash
   and keeps the job's row counts; nothing is returned but hashes and those counts), or
   `python tools/sealed_service.py add-dataset --root C:\iap-sealed\x --hash-id <id> --dir <dataset dir>` run by the
   sealed identity for a dataset placed by other means.
5. `iap-runner` reaches the service only through the pipe; it has no entry in any partition ACL except its own.

The DD1 ingestion itself (the free daily route into the four partitions) is the owner's run of the `registered-run`
workflow in mode `dd1`: [docs/GOVERNANCE.md](GOVERNANCE.md).

### 6. Backup and restore (owner)

The partitions have one copy each, on the laptop's own disk. A disk failure would lose Track B and Track C and the
audit chains that make every registered run checkable. The backup is the owner's job, run by hand; nothing in CI or
the runner does it.

**What is backed up.** All four partitions, `C:\iap-sealed\a`, `b`, `c` and `runner`: the datasets, each service's
state (`service\audit.jsonl`, `ledger.json`, `datasets.json`, any pending checkpoint, `config.json`, the pinned trust
roots) and the runner's cache. The repository, the committed checkpoints and the accounts' passwords are not in the
set: the repository is on GitHub, and the passwords belong in the owner's password manager.

**How often.** After each DD1 ingestion has finished and after each registered run has published its checkpoint.
Those are the only events that change a partition, so a backup taken then is complete until the next one. Never
during an ingestion or a run: the script stops the services for the copy.

**Where.** An external drive formatted NTFS (exFAT and FAT32 cannot hold the access lists), encrypted with BitLocker
To Go, unlocked only while a backup or a restore runs, and otherwise unplugged and stored away from the laptop. Keep
at least the last two sets. The BitLocker recovery key goes in the password manager, not on the laptop.

**Backup.** From an elevated PowerShell, with the drive unlocked (here `E:`):

```powershell
.\tools\admin\backup_sealed.ps1 -Destination E:\iap-backup
```

It asks for each partition account's password and, per partition: stops the service; has the partition's own
account write a manifest (`sealed_service.py backup-manifest`: the sha256 of every file and the audit chain head);
copies the partition with `robocopy /B /COPYALL`, which uses the administrator's backup privilege to copy bytes and
access lists without opening the files through the access check; has the account check the copy against the manifest
(`verify-backup`); and restarts the service. The set is a folder `iap-sealed-<UTC stamp>` holding the four copies
(each readable only by its account and SYSTEM, like the original), the four manifests and `backup_manifest.json`,
which records each manifest's sha256, file count and chain head. The console shows counts, chain heads and
robocopy's exit code, never file content. Write the stamp and the four chain heads in your log; a chain head can be
compared with the last line of `research/audit/CHECKPOINTS.jsonl` for that partition.

**Restore.** After a disk replacement, recreate the accounts and the services first
(`create_sealed_accounts.ps1`, `install_sealed_services.ps1`; new passwords are fine), move the empty new partitions
aside or delete them, then:

```powershell
.\tools\admin\restore_sealed.ps1 -BackupSet E:\iap-backup\iap-sealed-<stamp>
```

The script refuses before changing anything if the set has no `backup_manifest.json`, a manifest's sha256 differs
from the one the set recorded, or a target folder exists and is not empty (a live partition is refused too, since
the administrator cannot list it). It then recreates each partition with its access list, copies the files back with
their own access lists, and has each account run `verify-backup` on its restored partition: the audit chain must be
intact and its head and every file's sha256 must equal the manifest. Only then does it start that service. A
partition that fails stays stopped.

**Restore drill.** Every three months, and after any change to these scripts, restore the latest set into a scratch
folder instead of the live one:

```powershell
.\tools\admin\restore_sealed.ps1 -BackupSet E:\iap-backup\iap-sealed-<stamp> -SealedRoot D:\iap-restore-drill
```

A drill never stops or starts the live services. Pass means every partition reports `restored`. Afterwards delete
the drill folder from an elevated shell (`takeown /f D:\iap-restore-drill /r /a`, then remove it), and record the
date and result in your log. A drill that fails means the backups are not usable: take a new set before the next
ingestion.

The limit in section 4 applies here too: the administrator who can copy a partition with backup privilege can also
read the copy. The backup protects against losing the data, not against the owner.

---

## Installing the registered runner (owner steps)

v2.0 stage 2, CR1. Nothing in the repository installs software or changes the machine; every step below is the
owner's, run once. The result is a GitHub Actions self-hosted runner whose Windows service runs as the local
account `iap-runner` (created on 2026-10-25 by `tools/admin/create_sealed_accounts.ps1`, DR-5), picking up only
the `registered-run` workflow. Settings on GitHub are in [docs/GOVERNANCE.md](GOVERNANCE.md) section 7.

### 0. Shell

The workflow's steps run under Windows PowerShell 5.1 (`shell: powershell`), which every Windows installation has;
PowerShell 7 (`pwsh`) is not required and the owner's workstation does not have it (run 40222182056 failed on
`pwsh: command not found`). `gh` is not required on the runner; the API is called with `Invoke-RestMethod`. Each step invokes
`powershell -NoProfile -ExecutionPolicy Bypass` for its own generated script, so the account's execution policy
(default Restricted, which failed run 40226274618) does not matter; no machine-wide policy change is needed.

### 1. An interpreter the service accounts can run

The sealed accounts and `iap-runner` cannot read the owner's profile (`C:\Users\<owner>\...`), so a Python that
lives there is unusable for them. Install CPython 3.12.10 for all users at `C:\Python312` and clone the repository at
`C:\iap\repo`, exactly as [`tools/admin/install_all_users_python.md`](../tools/admin/install_all_users_python.md)
describes (interpreter, pinned stack, checkout ACL, how the runner finds the interpreter, the optional
`TIINGO_API_KEY` in `C:\actions-runner\.env`). The runner's own work directory below holds its checkout. The pinned
stack is also installed by the workflow itself from `python/requirements-ci.txt` (`--require-hashes`).

### 2. The runner work directory

```powershell
# elevated PowerShell
New-Item -ItemType Directory C:\actions-runner | Out-Null
icacls C:\actions-runner /inheritance:r /grant:r "iap-runner:(OI)(CI)F" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"
```

The owner's normal user has no entry: the checkout the runner makes, its logs and its `_work` tree are not
readable by the development identity (property 1).

### 3. Register the runner (GitHub UI gives the token)

Repository -> Settings -> Actions -> Runners -> New self-hosted runner -> Windows x64. Download the runner
package **into `C:\actions-runner` as the administrator** (the repository does not download anything), verify
the SHA-256 GitHub prints, then:

```powershell
cd C:\actions-runner
.\config.cmd --url https://github.com/AshJha0/intraday-alpha-platform --token <registration token> `
  --name iap-runner-ws --labels self-hosted,windows,iap-runner --runnergroup "sealed" `
  --work _work --replace --runasservice --windowslogonaccount iap-runner
```

`--runasservice` with `--windowslogonaccount iap-runner` installs and starts the Windows service
(`actions.runner.AshJha0-intraday-alpha-platform.iap-runner-ws`) under that account. Omit
`--windowslogonpassword`: `config.cmd` then prompts for the password, which keeps it out of the shell
history. There is no `svc.cmd` on Windows (`svc.sh` exists only on Linux and macOS runners); manage the
service with PowerShell: `Get-Service actions.runner.*`, `Stop-Service` / `Start-Service` on that name,
or `services.msc`. The labels must be exactly those the workflow asks for
(`runs-on: [self-hosted, windows, iap-runner]`). Grant `iap-runner` "Log on as a service" if the installer asks
(stage 11 correction, G2: the earlier text named `.\svc.cmd`, which the owner found absent on 2026-10-25).

### 4. Runner group and policy (GitHub, organisation or repository settings)

- Runner group `sealed`: allowed only for this repository and only for the workflow
  `.github/workflows/registered-run.yml` ("Allow selected workflows").
- Actions -> General: "Require approval for all outside collaborators" (fork pull requests never run on the
  self-hosted runner; the workflow has no pull-request trigger in any case).
- The `sealed` environment: required reviewer = the owner's account; no environment secrets and no environment
  variables named `ACTIONS_STEP_DEBUG` or `ACTIONS_RUNNER_DEBUG`. Commands in docs/GOVERNANCE.md 7.2.

### 5. The services the runner talks to

One `tools/sealed_service.py serve` per partition, running as the partition's identity, installed by
`tools/admin/install_sealed_services.ps1` (docs/GOVERNANCE.md section 5). The runner reaches them through `\\.\pipe\iap-sealed-<partition>`; no configuration on the runner
side. The runner's private cache is `C:\iap-sealed\runner\cache` (owned by `iap-runner`), created and emptied by
`tools/runner/run.py` on every run.

### 6. First-run checks (no sealed read)

1. From the runner's service account context the identity check must pass:
   `python tools/runner/run.py whoami` prints `iap-runner` and exits 0 (run it through the workflow's first
   step, or with `psexec -u iap-runner` from an elevated shell).
2. Start `registered-run` from the Actions tab with a prereg that is **not** signed: the run must stop at the
   environment approval, then, once approved, fail in the "Registered run" step with exit 3 and a redacted log
   that names no path and no account. Nothing was requested from any service (its audit shows no `request`
   line for that time).
3. With the dry-registration fixture of `python/tests/runner/` the full procedure is exercised in CI on
   synthetic data; a real registration waits for a signed prereg with a `registered_run` block, the pinned
   image digest in `deployment/docker/IMAGE_DIGEST` (P1), and the owner's words.
4. The first real run of the workflow is the DD1 ingestion in mode `dd1` (no prereg; the same approval and
   identity): [docs/GOVERNANCE.md](GOVERNANCE.md).

### 7. What the owner keeps

The `iap-runner` password (in the password manager, never in the repository or an environment), the runner's
registration (re-run `config.cmd --replace` after a reinstall), and the decision to approve each run. Rotating
the account password means `Stop-Service actions.runner.*`, `.\config.cmd remove --token <removal token>` and
re-registering with `--runasservice --windowslogonaccount iap-runner` (the password is prompted for, never passed
on the command line); check with `Get-Service actions.runner.*` that the service is back and running.

---

## The registered runner: threat model and the six properties

v2.0 stage 2, CR1 (U0 section 2, ninth review G2). The runner is
[`.github/workflows/registered-run.yml`](../.github/workflows/registered-run.yml) executed by a self-hosted
GitHub Actions runner whose service runs as `iap-runner`, driving [`tools/runner/`](../tools/runner)
against the sealed service of [docs/GOVERNANCE.md](GOVERNANCE.md). Tests:
[`python/tests/runner/`](../python/tests/runner). Settings the owner must set: [docs/GOVERNANCE.md](GOVERNANCE.md)
section 7. Install: [docs/GOVERNANCE.md](GOVERNANCE.md).

### Actors and assets

- **Adversary of record: the development identity** (the owner's normal user, every agent session, every
  research script) acting through the repository, the workstation's file system as that user, and GitHub as the
  repository owner's account *without* the approval step. The model does not defend against the workstation's
  administrator or against GitHub itself (section "custody").
- **Assets**: the sealed partitions and any derivative (a listing, a row, a summary statistic, a cache); the
  one-run-per-registration rule; the integrity of a result once produced.

### The procedure

Signed prereg merged (it carries `registered_run` = partition, dataset hash id, pinned image digest, command,
optionally the bound `dataset_files`) -> the owner starts `registered-run` naming the prereg and the bound commit
-> GitHub waits for the `sealed` environment approval -> the self-hosted runner (as `iap-runner`) checks out the
bound commit -> `tools/runner/run.py run`: identity, signature, image digest, `analysis_identity` (freeze
hashes, bound commit is HEAD or an ancestor), dataset id bound -> one `read` to the partition's service (its own
checks, ledger, hash verification, write-before-serve) -> served hashes re-computed and compared with the bound
`dataset_files` -> the registered command runs once in a private cache -> `hashes.json` -> `store_result` ->
commit to the protected `results` branch as `iap-runner` -> `publish` the checkpoint -> the redacted log is the
only artifact -> the cache is removed.

### Property 1: `iap-dev` cannot read sealed inputs or derivatives

| route | control | test |
|---|---|---|
| direct read of a partition | ACL: owner account and SYSTEM only | `test_identity_partitions.py` (dev x {a,b,c,runner}: read denied) |
| directory enumeration | same ACL | same (enumerate denied) |
| indirect: a report path under a partition | same ACL; the research code has no privilege | same (report denied) |
| indirect: a cache directory under a partition | same ACL; `iap-runner` uses its own private cache, removed after the run | same (cache denied); `test_private_cache_is_removed_and_holds_nothing_after_the_run` |
| the job log and artifacts | `RedactingLog`: every line passes `redact` (paths, pipe names, accounts, user and host names, e-mails); the analysis's stdout/stderr are never copied; the only artifact is the redacted log | `test_log_carries_no_path_account_host_or_data`, `test_redact_filter`, `test_workflow_...` (one upload, the log) |
| debug modes | `ACTIONS_STEP_DEBUG` / `ACTIONS_RUNNER_DEBUG` forced `"false"` in the job; must not exist as environment variables or secrets of `sealed` (docs/GOVERNANCE.md 7.2) | workflow test; settings check reproduced with `gh api` |
| the pipe | the service answers only the `iap-runner` account (OS identity); a developer process connecting is refused and audited | `test_only_the_runner_account_is_served`, `test_service_refuses_a_caller_that_is_not_the_runner` |
| the result itself | `result.json` is what the registered analysis chose to write; the prereg's command is reviewed before signing and is part of the signed document | signature tests |

Residual: a registered command that writes sealed rows into `result.json` would publish them. The guard is
review of the command before signing, not code.

### Property 2: `iap-dev` cannot change the workflow, image digest, prereg or secrets without an approval it does not hold

| object | control | test or check |
|---|---|---|
| `.github/workflows/registered-run.yml`, `tools/runner/`, `tools/sealed_service.py`, `tools/admin/`, `research/audit/` | `CODEOWNERS` lines owned by `@AshJha0`; the `main` ruleset requires code-owner review (docs/GOVERNANCE.md 7.1) | `test_codeowners_cover_the_workflow_and_the_runner`; `gh api` check of the ruleset |
| the image digest | bound inside the signed prereg (`registered_run.image_digest`) and compared with `deployment/docker/IMAGE_DIGEST` at the checked-out commit | `test_image_digest_mismatch_is_refused` |
| the prereg | Ed25519 signature under the trust root; any edit invalidates it | `test_tampered_signature_is_refused_before_any_request`, `test_unsigned_prereg_is_refused`, `test_untrusted_signing_key_is_refused` |
| secrets | there are none: the workflow reads no `secrets.*`; the pipe authenticates by OS identity | workflow test (`secrets.` absent) |
| the environment approval | `sealed` requires the owner's review; the runner group is restricted to this repository and to `workflow_dispatch` runs | docs/GOVERNANCE.md 7.2, `gh api` |

Residual: the owner's GitHub account approves and the owner's user develops: the same person. See custody.

### Property 3: one run per registration

The service's ledger is keyed by the canonical sha256 of the signed prereg and indexed by prereg id. A second
request with the same hash, or the same prereg id signed again at a new commit, is refused and audited; a new
registration is a new prereg id. Tests: `test_one_run_per_registration` (service),
`test_dry_registration_runs_once_and_is_refused_the_second_time` (end to end, including the re-signed-at-a-new-
commit case).

### Property 4: hash verification precedes any read; no retry without a new registration

Runner side (before any request): signature, `registered_run` block, image digest, `analysis_identity`
(scripts, code tree, configs, reference data, lock, installed packages, interpreter, bound commit), dataset id
bound. Service side (before any byte): every dataset file re-hashed against its record; a mismatch is a
**failed** registration. Runner side (before the analysis): served file hashes recomputed and compared with
the bound `dataset_files`. Tests: `test_changed_bound_script_fails_the_freeze_hashes`,
`test_bound_commit_that_is_not_an_ancestor_of_head_is_refused`, `test_dataset_id_must_be_bound_by_the_identity`,
`test_hash_mismatch_fails_the_registration_with_no_retry` (service),
`test_tampered_sealed_bytes_fail_the_registration_with_no_retry` and
`test_served_dataset_that_differs_from_the_bound_files_does_not_run` (end to end).

### Property 5: results are append-only

Each run adds one commit to the `results` branch from the `iap-runner` git identity, in a separate worktree:
`results/<prereg>/<seq>/result.json`, `hashes.json` (result hashes, prereg hash, dataset id and files, seq,
chain head, image digest, commit, evidence class) and one line in `research/audit/CHECKPOINTS.jsonl`. The branch
ruleset blocks force pushes and deletion (docs/GOVERNANCE.md 7.3). The service stores its own copy of the result
hash; `tools/runner/run.py verify-result` compares a committed `hashes.json` with it. Tests:
`test_results_branch_appends_and_never_overwrites`, `test_rewritten_result_is_detected`,
`test_result_hash_is_stored_once_and_verified`.

### Property 6: custody

While the owner holds development, administration, approval and sealed credentials alone, a runner result is
**mechanically isolated**, not **independently confirmed**. The runner writes the words into every
`hashes.json` (`"evidence_class": "mechanically isolated"`); release notes and result documents use the same
words (`test_custody_label_is_written_into_every_result`). The class upgrades when an approval or custody
authority other than the developer exists (owner item O-3): a second required reviewer on `sealed`, or a
service and runner host administered by someone else.

### What this model does not cover

- The workstation administrator (the owner). Every control above is a file ACL, a pipe identity check or a
  GitHub setting the administrator can change; see docs/GOVERNANCE.md section 4.
- GitHub as a platform, and the self-hosted runner software itself.
- A registered command that is itself the exfiltration (reviewed before signing).
- The repository is public: GitHub advises against self-hosted runners for public repositories because a
  pull request from a fork can run on them. This workflow has no `pull_request` trigger, the runner group is
  restricted to this repository and to selected workflows, and fork pull requests require approval before any
  workflow runs (docs/GOVERNANCE.md 7.4; defect D-50).

### Open items (defect register)

D-48 process-image query between standard users · D-49 non-development identities need stored credentials to
test · D-50 self-hosted runner on a public repository · D-51 the results ruleset cannot bind an administrator ·
D-52 encryption at rest · D-53 the pinned Linux image cannot execute under `iap-runner` with pipe access, so the
runner verifies and records the digest and executes in the identity-bound host environment · D-54 the sealed
service needs read access to the repository code and a pinned trust-root copy · D-55 routes that relied on the
removed `IAP_HOLDOUT_AUTHORISED` inheritance.

---

## DD1 ingestion runbook: the free daily route into the four partitions (owner)

v2.0 stage 2 DD1 (U0 section 2 partitions, section 3 "Stage 2" DD1, G3, G4, DR-6, DR-7). Code:
[`tools/ingest/dd1_daily.py`](../tools/ingest/dd1_daily.py) (the job), the `ingest` endpoint of
[`tools/sealed_service.py`](../tools/sealed_service.py), [`tools/data_manifest.py`](../tools/data_manifest.py)
(`kind: daily_pit` entries), the install scripts under [`tools/admin/`](../tools/admin), the `dd1` mode of
[`.github/workflows/registered-run.yml`](../.github/workflows/registered-run.yml). Tests on synthetic vendor-format
fixtures: [`python/tests/ingest/`](../python/tests/ingest), `python/tests/isolation/test_sealed_ingest.py`.

Nothing in this repository has ingested anything. The run below is the owner's, executed by the `iap-runner` identity
on the workstation after an environment approval; the development identity (the owner's normal user, the sessions that
write code) never touches a sealed partition and never sees a sealed value, before or after.

### 1. What the job does

| step | who | what | what you see |
|---|---|---|---|
| identity | `iap-runner` | refuses to run as any other account (exit 3) | log line `refused` |
| pull | `iap-runner` | Stooq bulk daily archive, read from your verified download (step 4; or Tiingo if `TIINGO_API_KEY` is in the runner's environment); every Wikipedia revision of the S&P 500 and Nasdaq-100 pages (last per day); EDGAR `form.idx` per quarter for the universe's CIKs; the three French daily factor files; Tiingo's public ticker list for listed last dates | counts per source |
| cut | `iap-runner` | development = to end of `Y-4`, validation = `Y-3`, Track B = `Y-2`, Track C = `Y-1` onward, `Y` = `--year` (the ingestion year) | the windows in the record |
| test | `iap-runner` | the five DD1 tests on the **development partition only**: survivorship, corporate actions, calendar, open/close convention, membership intervals (section 4) | `dd1 test <name>: pass` or `FAIL` |
| write | `iap-runner` | development and validation to `C:\iap\daily\dd1_<Y>\` (you can read them); Track B and C through `\\.\pipe\iap-sealed-b` / `-c` `ingest` (the service writes, re-hashes, records, answers hashes and counts) | `partition b: sealed in b, 5 files, chain head …` |
| record | `iap-runner` | `C:\iap\daily\dd1_<Y>\dataset.json`: hashes, row counts, vendor metadata, test block, `status` | `record written: status complete` |

"Complete" means: every DD1 test passed, both sealed services accepted their partition, the record says
`"status": "complete"`. Anything else is `incomplete`: the development partitions are still written (so the failure
can be examined on them), nothing is sealed, the workflow step fails, and a re-run needs a new purchase year (the job
refuses to overwrite a record; delete `C:\iap\daily\dd1_<Y>` yourself as the administrator if the run must repeat).

### 2. The sequence, once

1. **Python for all users and the checkout outside your profile**:
   [`tools/admin/install_all_users_python.md`](../tools/admin/install_all_users_python.md) (CPython 3.12.10 at
   `C:\Python312`, the pinned stack, the clone at `C:\iap\repo` read-only to the service accounts).
2. **The three sealed services** (elevated PowerShell, from `C:\iap\repo`; asks for each sealed account's password):

```powershell
Set-Location C:\iap\repo
.\tools\admin\install_sealed_services.ps1
```

   It creates `C:\iap\daily` (iap-runner writes, you read), grants each sealed account "Log on as a batch job", runs
   `sealed_service.py install` and `check` **as the account** (you have no access to the partition, by design),
   registers "IAP sealed service a/b/c" (run whether user is logged on or not, at start-up, no time limit, restarted on
   failure), starts them and pings each pipe. Expected ping answer for you: `{"answered": true, "ok": false, "error":
   "caller_account"}` (only `iap-runner` is served; the refusal is audited in the partition).
3. **The runner**: [docs/GOVERNANCE.md](GOVERNANCE.md) (work directory, registration as a Windows service under
   `iap-runner`, the `sealed` environment with you as required reviewer, the first-run checks of its section 6).
   Optional: `TIINGO_API_KEY=<key>` in `C:\actions-runner\.env` selects Tiingo; without it the job uses Stooq.
   **Required: `IAP_SEC_CONTACT=<your name> <your email>`** in the same file, then restart the runner service. The SEC
   answers 403 to any EDGAR request whose User-Agent has no contact email (its fair-access rule; run 40284491015 failed
   on it after 48 minutes). The job refuses to start without an email there (exit 3), and it calls EDGAR, French and
   Tiingo before the membership walk, so a refusing source stops the run in the first minute (D-88).
4. **Download the Stooq archive yourself** (DR-16, D-86). Since October 2026 stooq.com shows scripts a browser check,
   and the job does not try to pass it. In your browser, open <https://stooq.com/db/h/>, take the "U.S." daily ASCII
   archive (`d_us_txt.zip`, several hundred MB), and save it as `C:\iap\inbox\d_us_txt.zip`. Then, in PowerShell:

```powershell
New-Item -ItemType Directory -Force C:\iap\inbox | Out-Null
icacls C:\iap\inbox /grant "iap-runner:(OI)(CI)RX"
(Get-FileHash C:\iap\inbox\d_us_txt.zip -Algorithm SHA256).Hash.ToLower()
```

   Write down the printed hash and the date you downloaded the file. The job refuses before its first request if the
   file is missing, is not a zip, or does not match that hash, so a bad file cannot waste the four-hour membership pull.
   The record stores the hash and `archive_origin: owner_browser_download`.
5. **Probe first (a few minutes)**: run the workflow with `mode` = `dd1-probe` and the same other fields as step 6.
   It checks your zip and SEC contact, then makes one request of each kind the long run makes (Wikipedia revision
   list and one revision per index, EDGAR's ticker map and the 1993 Q1 and current quarter indexes, the three French
   files, Tiingo's ticker list), asks each sealed service for its status as the runner, and checks it can write to
   the output and cache folders. It writes no data. The last log line is `probe passed`; anything else names the
   source that refused (D-100). Start the full run only after a passed probe.
6. **Trigger the ingestion**: GitHub -> Actions -> `registered-run` -> Run workflow with `mode` = `dd1`, `commit` = the
   merge commit of the PR that added this runbook (or any later `main` commit), `prereg` = anything (ignored in this
   mode), `year` = empty (this year), `stooq_sha256` = the hash from step 4 (leave it empty only when Tiingo is set). A failed run can be started again with the same
   fields: busy answers are retried with back-off (D-96), parsed Wikipedia revisions and finished EDGAR quarters are
   read from the runner-only cache (D-87, D-97), and a partition the sealed service already holds is not sent twice
   (D-99), so a re-run resumes rather than starting over.
   The run waits in the `sealed` environment until you approve it; approve it.
7. **Read the redacted log** (the only artifact; paths, accounts and values are redacted). The last lines are the
   per-partition results and `record written: status complete`.
8. **List the record in the manifest** (development identity, your checkout):

```powershell
Copy-Item -Recurse C:\iap\daily\dd1_2026 data\real\daily\dd1_2026
python tools/data_manifest.py generate --data-root data/real
python tools/data_manifest.py check --data-root data/real
```

   The entry carries the four `dataset_hash_id`s (Track B and C from the services' responses), the row counts the job
   itself produced, the vendor metadata and the test block; commit it in a PR titled "DD1 ingestion record <Y>".

### 3. What to send the coordinator

- the workflow run URL and the redacted log's last ten lines;
- the four `dataset_hash_id`s and `status` from `dataset.json` (never the CSV files);
- the `dd1_tests` block (pass/fail and counts only);
- for a failed test, the counts of the failing test and nothing else: a failing survivorship or membership test
  downgrades the daily families to data-ineligible (DR-6 stop condition); it does not relax the test.

### 4. The five tests (development partition only; counts, never values)

| test | passes when | default (defect register) |
|---|---|---|
| survivorship | every name that left the index inside the window has a price row on every calendar session from its first interval start to its last interval end, and after it through 60 sessions (D4's horizon) or its listed last date (Tiingo's public ticker list), whichever is first; failures at most 2 % of that population | D-59 |
| corporate actions | Tiingo: every `splitFactor != 1` day reconciles raw and adjusted closes; Stooq (adjusted series only): no residual close-to-close jump of 40 % or more at a simple split ratio (2, 3, 4, 5, 3/2, 1/2, 1/3, 1/4, 1/5, 2/3, 3/4) within 2 % | D-63 |
| calendar | the panel's sessions (dates on which at least half the month's active names have a row) equal the French factor files' dates inside the panel's range; no weekend rows | D-62 |
| open/close convention | the convention text for the source is written into `metadata.json` (G4 for D3) and the share of opens equal to the previous close is at most 5 % | D-60 |
| membership intervals | per (index, symbol) intervals ordered and non-overlapping; every member has a price row inside an interval; a year counts as reliable when at least 90 % of its parsed revisions are plausible and the median gap between reliable revisions is at most 45 days; years below that are listed as excluded for membership-dependent families; the test fails only when no year in the window qualifies or an interval is malformed | D-42 |

Positive controls (coverage report section 5 item 6) run in CI: a truncated delisting, an unadjusted split and dropped
sessions each fail their test on the synthetic fixture, and a crafted overlapping interval fails the membership test.

### 5. What the run can and cannot show

The ingestion, the sealing and the tests run on one workstation whose administrator is the owner; the evidence class
of what they produce is **mechanically isolated** (docs/GOVERNANCE.md section 4). The Wikipedia membership is a proxy
(every interval is labelled `wikipedia-proxy`); Stooq's series are vendor-adjusted with no raw or dividend table; the
EDGAR CIK map is today's; the exchange calendar is the French library's (CRSP) trading days. Each is a register row,
not a claim.

---

## Repository and operational settings (not enforced by files)

`GOVERNANCE.md` and `docs/GOVERNANCE.md` describe controls. Some of them are enforced
by a file in this repository (CI, `CODEOWNERS`, the structural checks in
`tests/harness/check_deployment.py`); the ones below are **repository or
operations settings that live outside the tree**. Until one is configured, the
corresponding sentence in the governance documents is a requirement, not a fact.

**How this page stays true (v2.0 stage 11, S4).** Section 1 is generated from the live settings:
`python tools/repo_settings_snapshot.py write` reads them with `gh api` (branch rules, rulesets,
Actions permissions, secret scanning, vulnerability reporting) into
[repo_settings_live.json](governance/repo_settings_live.json); `python/tests/test_repo_settings_doc.py` fails
when the matrix below and that snapshot disagree, and the CI `governance` job warns when the live
settings drift from the snapshot. The snapshot was taken on **2026-10-25**; the v1.19 audit
(plan section 2.3) had found 13 contexts documented, 6 in the JSON block and 17 live. Everything
below is run by the repository owner (`AshJha0`), once, with an authenticated `gh` that has admin
rights on the repository. Nothing in this repository applies these settings for you, by design.

### 1. Branch ruleset for `main`: the required-check matrix

Live on 2026-10-25: one ruleset, `protect-main` (active, last edited 2026-10-18), rules `deletion`,
`non_fast_forward`, `pull_request` (0 approving reviews, no code-owner review, no thread-resolution
requirement, merge / squash / rebase allowed) and `required_status_checks` with the **strict**
policy (the branch must be up to date with `main`) over **17 required** contexts. The context name
is the job's `name:` if set, else its id. Each row says which change class the check guards
(research correctness, data integrity, execution, infrastructure) and the risk it catches; the
per-PR cost is the job's wall time on the stage 11 CI run (filled from that run's job table).

<!-- required-check matrix: generated contexts -->
| context | job (workflow) | change class | risk the check catches | per-PR cost |
|---|---|---|---|---|
| `python` | `python` matrix 3.11 (ci.yml) | research correctness, execution, data integrity | the Python reference semantics, the confirmatory gate (R1), the data contracts and leakage canaries, the guard-hook AST test, mypy; the fast suite must RUN on the seeded dataset, not skip | ~10 min |
| `python-312` | `python` matrix 3.12 (ci.yml) | infrastructure | interpreter drift between CI (3.11) and the pinned reproduction image (CPython 3.12.10): a hash or float that moves with the interpreter | ~10 min |
| `python-slow` | `python-slow` (ci.yml) | research correctness | the study-length tests (replays, dependence audits, equivalence runs) that the fast suite excludes | ~12 min |
| `integration` | `integration` (ci.yml) | execution, infrastructure | cross-component runs and same-seed byte determinism of the replay | ~5 min |
| `cpp` | `cpp` (ci.yml): build + ctest since stage 11 | execution (parity-only port, D5(a)) | a Python execution-rule change not ported, or a C++ change that breaks its own tests; benchmark guards, sanitizers and coverage moved weekly (S2) | ~6 min |
| `rust` | `rust` (ci.yml): `risk` + `features` since stage 11 | data integrity, risk | the normative risk engine (PLATFORM_CONVENTIONS section 11.1) and the pyo3 feature engine; the nine frozen crates run weekly, their goldens per PR in `golden` | ~6 min |
| `golden` | `golden` (ci.yml) | execution, data integrity | promotion gate 10: byte-for-byte parity of every language's golden group, including every Rust crate's golden target | ~12 min |
| `deployment` | `deployment` (ci.yml) | infrastructure, documentation truth | structural validation of deployment/, the polyglot FROZEN policy, the headline numbers re-derived from the CI artefacts | ~4 min |
| `CodeQL` | codeql.yml summary check | infrastructure (security) | static analysis findings on Python, C++ and Rust | ~15 min |
| `analyze (rust)` | `analyze` matrix entry rust (codeql.yml) | infrastructure (security) | the Rust extractor must see the real build (D5(a) note in codeql.yml) | ~15 min |
| `governance` | `governance` (ci.yml) | research integrity | an unsigned or stale-signed key registry, a broken blackboard chain, a promoted model without its content hash, a prereg citing an unlisted dataset, a ledger that shrank; the live-settings drift warning (S4) | ~3 min |
| `advisory` | alias of `lint-and-secrets` (ci.yml) | infrastructure, architecture | gitleaks over the whole history, ruff, the import-linter architecture contracts (S3): all blocking. The job was renamed in stage 11; the alias reports under the old context until owner action 1 below | ~3 min |
| `rust-pyo3` | `rust-pyo3` (ci.yml) | data integrity | the Rust feature engine's wheel builds and its parity tests RUN (a skip fails) | ~8 min |
| `supply-chain` | `supply-chain` (ci.yml) | infrastructure (security) | pip-audit of both hashed locks, cargo audit and deny, trivy on lockfiles and Dockerfiles; ignores only with reason and expiry | ~5 min |
| `agent-evals` | `agent-evals` (ci.yml) | research integrity (agents) | a stub-model agent evaluation that regressed against its committed baseline | ~3 min |
| `docs` | `docs` (ci.yml): `check_docs.py --strict` since stage 11 | documentation truth | a dead relative link, an `iap.*` API name that no longer imports, a status phrase older than the last tag | ~3 min |
| `reproducible-build` | `reproducible-build` (ci.yml) | reproduction | the wheel built twice must match (the image compare is report-only) | ~10 min |
<!-- end required-check matrix -->

Not required, by design: `images` (skipped on most PRs; a skipped required check blocks the merge),
`coverage` (the whole-suite floor; to be required once it has a run history), the weekly
`cpp-full`, `cpp-sanitizers` and `rust-workspace` jobs of `weekly-structural.yml` (not on the PR
path), `regenerate` and `risk-fuzz` (dispatch and nightly). Wall time: the v1.19 audit measured
about 14 minutes and about 52 job-minutes per run before stage 11; the stage 11 run's figures are
in the CHANGELOG entry of this stage.

`required_approving_review_count` is 0 because GitHub does not let an author approve their own
pull request and this repository has one maintainer: a count of 1 would make every merge
impossible. The documented policy (GOVERNANCE.md section 1: 1 to 2 reviewers by change class)
needs a second maintainer; when there is one, change the count to 1 (or 2 for the risk-engine and
contracts classes), set `require_code_owner_review` to true and give the second maintainer a line
in `CODEOWNERS`.

#### Owner actions on the ruleset (proposed in stage 11, none applied)

1. **Rename the context `advisory` to `lint-and-secrets`** (then delete the alias job in
   `ci.yml`). The rename is the one change; every other context stays as live. `PUT` replaces the
   whole ruleset, so the payload carries all 17 contexts:

   ```bash
   gh api -X PUT repos/AshJha0/intraday-alpha-platform/rulesets/24455225 --input - <<'JSON'
   {
     "name": "protect-main", "target": "branch", "enforcement": "active",
     "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
     "rules": [
       { "type": "deletion" }, { "type": "non_fast_forward" },
       { "type": "pull_request", "parameters": {
           "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": false,
           "require_code_owner_review": false, "require_last_push_approval": false,
           "required_review_thread_resolution": false,
           "require_extra_approval_for_unattributed_changes": false,
           "allowed_merge_methods": ["merge", "squash", "rebase"] } },
       { "type": "required_status_checks", "parameters": {
           "strict_required_status_checks_policy": true, "do_not_enforce_on_create": false,
           "required_status_checks": [
             { "context": "python", "integration_id": 15368 }, { "context": "python-312", "integration_id": 15368 },
             { "context": "python-slow" }, { "context": "integration", "integration_id": 15368 },
             { "context": "cpp", "integration_id": 15368 }, { "context": "rust", "integration_id": 15368 },
             { "context": "golden", "integration_id": 15368 }, { "context": "deployment", "integration_id": 15368 },
             { "context": "CodeQL", "integration_id": 57789 }, { "context": "analyze (rust)", "integration_id": 15368 },
             { "context": "governance", "integration_id": 15368 }, { "context": "lint-and-secrets", "integration_id": 15368 },
             { "context": "rust-pyo3", "integration_id": 15368 }, { "context": "supply-chain", "integration_id": 15368 },
             { "context": "agent-evals" }, { "context": "docs" }, { "context": "reproducible-build" } ] } } ] }
   JSON
   # check, then refresh the snapshot and the matrix row:
   gh api repos/AshJha0/intraday-alpha-platform/rules/branches/main --jq '.[] | select(.type=="required_status_checks") | .parameters.required_status_checks[].context'
   python tools/repo_settings_snapshot.py write
   ```

2. **`cpp` as a required context: keep.** S2 asked for C++ out of the PR path; the job now costs
   build + ctest only, and dropping the context would leave the C++ port's own tests unrun until
   the weekly job while the port is still the measured hot path in the documentation. Decide
   again when C++ is retired or the golden gate alone is judged enough; the ruleset edit is the
   owner's either way.
3. **The "release tags" ruleset of section 2 does not exist live** (only `protect-main` does):
   apply it or delete section 2's claim.
4. Consider `coverage` as a required context once it has a run history (unchanged recommendation).
5. Actions settings read on 2026-10-25: `allowed_actions: all` and `sha_pinning_required: false`
   (every workflow pins actions by SHA and `check_deployment.py` enforces it, so turning
   `sha_pinning_required` on costs nothing); private vulnerability reporting is **disabled**
   although section 3 asks for it; Dependabot security updates, secret scanning and push
   protection are enabled.

### 2. Release tags

Release tags (`v*`) trigger `.github/workflows/release.yml`, which publishes
images. Make them immutable so a published tag cannot be moved or deleted:

```bash
gh api -X POST repos/AshJha0/intraday-alpha-platform/rulesets --input - <<'JSON'
{
  "name": "release tags",
  "target": "tag",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/tags/v*"], "exclude": [] } },
  "rules": [ { "type": "update" }, { "type": "deletion" }, { "type": "non_fast_forward" } ]
}
JSON
```

**Signed tags are a practice, not an enforced control.** Create release tags
with `git tag -s vX.Y.Z` and verify with `git tag -v vX.Y.Z`. Neither GitHub
rulesets nor any workflow in this repository checks the signature, and none of
the existing tags (`v1.0.1`, `v1.1.0`, `v1.1.1`, `v1.2.0`) is claimed to be
signed. What the release workflow does provide is a build-provenance
attestation per image (see section 5).

### 3. Dependabot alerts, security updates, secret scanning

Version updates are configured by `.github/dependabot.yml` (a file). Alerts and
security updates are settings:

```bash
# Dependabot alerts
gh api -X PUT repos/AshJha0/intraday-alpha-platform/vulnerability-alerts

# Dependabot security updates (automatic PRs for vulnerable dependencies)
gh api -X PUT repos/AshJha0/intraday-alpha-platform/automated-security-fixes

# Secret scanning + push protection (already enabled on 2026-10-03; idempotent)
gh api -X PATCH repos/AshJha0/intraday-alpha-platform --input - <<'JSON'
{ "security_and_analysis": {
    "secret_scanning": { "status": "enabled" },
    "secret_scanning_push_protection": { "status": "enabled" } } }
JSON

# Private vulnerability reporting (gives docs/GOVERNANCE.md section 5 a real channel)
gh api -X PUT repos/AshJha0/intraday-alpha-platform/private-vulnerability-reporting
```

CodeQL needs no setting: `.github/workflows/codeql.yml` uploads results once the
workflow has run on `main`.

### 4. Actions permissions

Workflows declare least-privilege `permissions:` themselves. Also restrict what
Actions may do by default:

```bash
# default GITHUB_TOKEN is read-only; Actions cannot approve pull requests
gh api -X PUT repos/AshJha0/intraday-alpha-platform/actions/permissions/workflow --input - <<'JSON'
{ "default_workflow_permissions": "read", "can_approve_pull_request_reviews": false }
JSON
```

### 5. Release procedure (what the files do and what stays manual)

1. Merge to `main` with `ci` green.
2. `git tag -s vX.Y.Z && git push origin vX.Y.Z`. `release.yml` verifies the
   commit has a green `ci` run, builds the three images, pushes
   `ghcr.io/ashjha0/intraday-alpha-platform-{python,cpp,rust}:vX.Y.Z`
   (the `java` image was retired in v1.17, F9),
   attests build provenance for each, writes `release-manifest.json` (image
   digests) and attaches it to the GitHub release (creating the release with
   generated notes if it does not exist; existing notes are left alone).
3. **Scripted (v1.17 F2):** `python tools/release.py publish --version X.Y.Z`
   downloads `release-manifest.json` from the release and rewrites the
   Kubernetes manifests (`deployment/k8s/{platform-metrics,cronjob-data-pipeline,cronjob-state-backup}.yaml`)
   to `<name>:vX.Y.Z@sha256:...` (`pin-digests` alone, dry-run by default);
   open the "release: pin vX.Y.Z image digests" PR with the result. Compose
   keeps the tag (a `build:` service cannot carry a digest). The repository references
   `v1.5.0` by tag only: those images exist once the `v1.5.0` tag has been
   pushed and `release.yml` has run for it (it ran for the first time for
   `v1.3.0`; no image was ever published for `v1.2.0`, which was tagged
   before the release workflow existed), and the digests are pinned from
   that run's `release-manifest.json` afterwards.
4. Verify provenance before deploying:
   `gh attestation verify oci://ghcr.io/ashjha0/intraday-alpha-platform-python@sha256:<digest> --repo AshJha0/intraday-alpha-platform`.
5. Make the GHCR packages public (or grant the cluster a pull secret):
   packages are created private by default.

`release.yml` can only be exercised in full by pushing a tag, so a release is
its own test. Its `verify-ci` guard failed the v1.4.0 release once on an API
error although CI was green; since v1.5.0 the guard is
`tests/harness/verify_ci_green.py`, which retries an API error, polls a run that has not
finished yet and fails on a failed or missing run, and it can be run alone
against a given commit:
`gh workflow run release.yml --ref <branch> -f dry_run=true -f sha=<40 hex>`.

### 6. Operational settings the platform needs but the repository cannot hold

| Setting | Why it is outside the tree | What exists today |
|---|---|---|
| Off-host shipping of the audit JSONL (`risk_audit.jsonl`, `admin_audit.jsonl`, `config_audit.jsonl`, `decision_traces.jsonl`) | needs a log sink and credentials | **Nothing ships them.** The files are written to the state volume (`/data/state`, PVC `iap-platform-state`). GOVERNANCE.md section 3 requires daily off-host shipping; that is not implemented. |
| Alert delivery | receiver URLs are secrets | Alertmanager routes `page` and `ticket` alerts (v1.12.0); compose defaults to a local echo sink. Supply real URLs: compose `ALERT_PAGE_URL_FILE` / `ALERT_TICKET_URL_FILE`; k8s Secret `iap-alertmanager-webhook` with keys `page_url` and `ticket_url` (docs/RUNBOOKS.md §2). Point the `watchdog` receiver at a dead-man's-switch service to alarm when the heartbeat stops. |
| StorageClass | cluster specific | `storageClassName: standard` in `deployment/k8s/pvc.yaml` is a placeholder: replace it with a class that exists (`kubectl get storageclass`). |
| NetworkPolicy enforcement | needs a CNI that implements it | Policies are in `deployment/k8s/networkpolicy.yaml`; a CNI without NetworkPolicy support ignores them. |
| Image vulnerability scan (`trivy image`) | docs/GOVERNANCE.md asks for it before digest pinning | Not wired into any workflow. |

### 7. v2.0 stage 2: the sealed environment, the registered runner and the results branch

Required by U0 section 2 (properties 1, 2 and 5) and documented in
[docs/GOVERNANCE.md](GOVERNANCE.md). None of this is applied by a file in the repository; the owner
sets it once with an admin `gh`. Each block ends with the `gh api` read that reproduces the check (CR1
acceptance: "a passing test or a documented settings check reproduced by `gh api`"). Status as of 2026-10-25:
**not yet set** (the owner has not run these; the branch `results` and the environment `sealed` did not exist
on GitHub as of 2026-10-25).

#### 7.1 Code-owner review for the runner paths (property 2)

The development identity must not be able to change `.github/workflows/registered-run.yml`, `tools/runner/`,
`tools/sealed_service.py`, `tools/admin/` or `research/audit/` without a review it does not hold. `CODEOWNERS`
names `@AshJha0` for these paths; the `main` ruleset must require code-owner review for them to bind. Because a
pull-request author cannot approve their own pull request, this requires a second maintainer (D-25) or a
narrower ruleset that applies the requirement only when those paths change:

```bash
# a second ruleset on main, scoped by file path to the runner surfaces
gh api -X POST repos/AshJha0/intraday-alpha-platform/rulesets --input - <<'JSON'
{
  "name": "registered-runner surfaces",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] }
  },
  "rules": [
    { "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": true,
        "require_last_push_approval": true,
        "required_review_thread_resolution": true
      } },
    { "type": "file_path_restriction",
      "parameters": { "restricted_file_paths": [".github/workflows/registered-run.yml", "tools/runner/**", "tools/sealed_service.py", "tools/admin/**", "research/audit/**"] } }
  ]
}
JSON
# check
gh api repos/AshJha0/intraday-alpha-platform/rulesets --jq '.[] | {name, enforcement}'
```

Until a second maintainer exists, `require_code_owner_review` on a single-maintainer repository blocks every
merge touching these paths (including this PR's follow-ups); the owner decides between the second maintainer
(O-3, which also upgrades the evidence class) and the file-path restriction above, which stops a push that
changes these paths outside a pull request. Record the choice as a decision record.

#### 7.2 The `sealed` environment (properties 1 and 2)

```bash
# create / update the environment: the owner is the required reviewer, no self-review bypass
gh api -X PUT repos/AshJha0/intraday-alpha-platform/environments/sealed --input - <<'JSON'
{ "wait_timer": 0,
  "prevent_self_review": false,
  "reviewers": [ { "type": "User", "id": OWNER_USER_ID } ],
  "deployment_branch_policy": { "protected_branches": true, "custom_branch_policies": false } }
JSON
# OWNER_USER_ID: gh api users/AshJha0 --jq .id
# check: required reviewer present, no secrets, no debug variables
gh api repos/AshJha0/intraday-alpha-platform/environments/sealed --jq '.protection_rules'
gh api repos/AshJha0/intraday-alpha-platform/environments/sealed/secrets --jq '.total_count'      # must be 0
gh api repos/AshJha0/intraday-alpha-platform/environments/sealed/variables --jq '.variables[].name' # must not list ACTIONS_STEP_DEBUG / ACTIONS_RUNNER_DEBUG
gh api repos/AshJha0/intraday-alpha-platform/actions/variables --jq '.variables[].name'            # same at repository level
gh api repos/AshJha0/intraday-alpha-platform/actions/secrets --jq '.secrets[].name'                # no ACTIONS_STEP_DEBUG
```

`prevent_self_review` is `false` because the owner both starts and approves runs while there is one
maintainer (custody: the class stays "mechanically isolated"); set it `true` when a second reviewer exists.

#### 7.3 The `results` branch (property 5)

Created by the first registered run (orphan branch, committed by `iap-runner`). Protect it before that:

```bash
gh api -X POST repos/AshJha0/intraday-alpha-platform/rulesets --input - <<'JSON'
{
  "name": "results append-only",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/heads/results"], "exclude": [] } },
  "bypass_actors": [],
  "rules": [ { "type": "deletion" }, { "type": "non_fast_forward" } ]
}
JSON
# check
gh api repos/AshJha0/intraday-alpha-platform/rulesets --jq '.[] | select(.name=="results append-only") | .enforcement'
gh api repos/AshJha0/intraday-alpha-platform/branches/results --jq '.protected'
```

No pull-request requirement on `results`: the runner pushes one fast-forward commit per run with the job
token. A repository administrator can still bypass or delete a ruleset (D-51); the service's copy of each
result hash and the published checkpoints make that detectable.

#### 7.4 Actions policy for the self-hosted runner (public repository)

```bash
# the default token stays read-only (section 4); fork pull requests need approval before any workflow runs
gh api repos/AshJha0/intraday-alpha-platform/actions/permissions --jq '{enabled, allowed_actions}'
# the registered runner and its labels (repository-level runner)
gh api repos/AshJha0/intraday-alpha-platform/actions/runners --jq '.runners[] | {name, status, labels: [.labels[].name]}'
```

The registered runner must show the labels `self-hosted`, `windows`, `iap-runner` and no other workflow may
target them (grep `.github/workflows/` for `iap-runner`: only `registered-run.yml`). "Require approval for all
outside collaborators" is set in the Actions settings page (no API field).

#### 7.5 What stays unenforced

A ruleset binds everyone except whoever the ruleset's bypass list names and, in practice, the repository
administrator who can edit the ruleset. On a single-maintainer repository that administrator is the developer.
The register's D-25, D-50 and D-51 record this; the custody label (property 6) is the honest description.
