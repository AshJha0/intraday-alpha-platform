# One look at the sealed breadth holdout on bars: what the rules require

Written 2026-11-18, before any 2025/26 data was downloaded or read. Nothing here is signed or
registered. The runner is `tools/breadth_bars_holdout.py` (library
`iap.research.breadth_holdout`, tests `python/tests/research/test_breadth_holdout.py`); the
pre-registration draft is
[breadth_bars_holdout_DRAFT.json](../preregs/drafts/breadth_bars_holdout_DRAFT.json) with its
[rendered form](../preregs/drafts/breadth_bars_holdout_DRAFT.md).

## The short version

The owner decided on 2026-11-18 to spend the one look at the nine sealed sessions in this release.
The code can be ready for that; the rules are not yet. Three things stand in the way, and each needs
the owner, not a session:

1. **The sessions are reserved for another study.** The registry reserves them for
   `research/preregs/breadth_book_prereg.json`, which registers a different study and records that
   no look was taken under it. No written rule moves a reservation. The smallest consistent route is
   an owner decision record plus a reviewed registry edit (section 2).
2. **Signing on the owner's behalf is ruled out today.** `docs/governance/OWNER_SIGNATURE_CHECKLIST.md`
   says, citing decision U0 DR-2, that the owner's key is used by the owner alone and that no session
   signs on the owner's behalf. The plan of 2026-11-18 (the coordinating session signs) contradicts
   that. Either the owner signs, or the owner records a decision that replaces DR-2 for this document
   (section 3).
3. **Nine sessions can only detect a very large effect.** The Round 0 stop rule permits the
   registration only if it says it looks for a net Sharpe of about 17 or more (section 5).

## 1. What the holdout registry checks before a read

Code: `python/src/iap/governance/holdout_registry.py`.

- `load_registry` refuses a registry whose `body_sha256` does not match its content. Every edit goes
  through `seal` and shows in review (CODEOWNERS).
- `guard(dataset_dir, purpose, prereg_id, sessions, symbols)` decides from `dataset.json` and the
  registry before any data file is opened. It finds the dataset by `dataset_version` or content hash,
  then judges each (session, symbol) by the session's rules. `in_sample` passes. `unregistered`
  fails. `spent` or `unread` needs a `prereg_id` that appears in `spent_by` or `reserved_for` for
  that scope, and that `prereg_is_signed` accepts.
- `prereg_is_signed` checks the prereg file's Ed25519 signature under
  `research/agents/trust_roots.json` (`analysis_identity.verify_signed_document`). It requires
  `signed: true`, a maintainer listed in the trust root with the same public key, a
  `message_sha256` equal to the hash of the canonical body, and a valid signature.
- `check_prereg_identity`: if the prereg has an `analysis_identity` block, `environment_mismatches`
  must find nothing. That covers the bound scripts, the `python/src/iap` code tree, `configs/`,
  `data/reference/`, declared inputs, the dependency lock and installed packages, the interpreter,
  and the image digest when one is bound. The dataset version must be among the bound
  `data_versions`.
- Runners also call `verify_identity`. It checks everything above, plus that the bound commit is HEAD
  or an ancestor of HEAD, and that the family, metrics and thresholds the runner passes equal the
  bound ones. `require_independent_runner` refuses when `IAP_RUN_ACTOR` is missing or equals the
  signer, or when signing-key variables are set.
- Every decision is appended to the hash-chained access log `data/real/holdout_access.jsonl`.

So a signed pre-declaration must match on: the session and symbol scope (through `reserved_for`);
the dataset versions; family; metrics; thresholds; scripts; code tree hash; configs, reference,
lock, environment and interpreter (identity v2); and a commit that HEAD descends from.

## 2. The reservation and the existing breadth pre-registration

In the registry, each of the nine sessions has two rules. AAPL, MSFT and QQQ are `spent` (R7 and
later replays). The rule for `*` (every other symbol) is `unread`, with
`reserved_for: ["research/preregs/breadth_book_prereg.json"]`. A test asserts that reservation
(`python/tests/test_holdout_registry.py`, around line 90).

**What `breadth_book_prereg.json` registers.** Signed 2026-10-16 by `ashjha0`, it is plan item C8,
the v1.17 cross-sectional book. Its contents:

- score `eq11`, from the feature store;
- linear centred-rank weights through the cost-aware optimizer, 5-minute rebalance on the feature
  clock, dollar, beta and sector neutral, at a notional of 1,000,000;
- impact from the square-root cost model;
- universe `research/breadth/universe.json`: 100 names ranked by share volume, minus AAPL, MSFT, QQQ;
- statistic: day-clustered t of daily net P&L of at least 2.0, rank IC above 0, deflated Sharpe of
  at least 0.95, and at least 9 days.

It was registered under option (a), with the C6 net-of-cost screen as a hard gate. The screen
refused the candidate, so its recorded outcome is "no confirmatory look (documented negative
result)", and it builds no holdout dataset (`docs/PORTFOLIO_BREADTH.md`).

**Can the bars study run under it?** No. Every element differs: the data path (raw bars, not
features), the score, the universe (500 names by notional, against 100 by share volume), the
weighting, costs and statistic. Running a decile book of another score under C8's id would be the
"different analysis under the same signed text" that the identity binding exists to stop. The
document is signed, and editing it voids the signature.

**What the rules say about using a reserved holdout for another pre-registration.** Nothing
explicit. The registry docstring defines `reserved_for` as "the signed prereg documents allowed to
read it", and the only way to change it is an edit plus `seal`, reviewed. No document gives a rule
for releasing a reservation. The deviation notes (for example `research/r7/R7_DEVIATION_lot_size.md`)
record owner decisions on a running study; none moves a holdout from one study to another. A new
signed pre-declaration plus a deviation note is therefore not enough on its own: the registry must
also name the new document, and only the owner can authorise that edit.

**Smallest rule-consistent route (needs the owner's explicit decision):**

1. A decision record written and committed before signing, for example
   `research/round1/DECISION_breadth_holdout_reservation.md`. It quotes the owner's words of
   2026-11-18 and states that C8 closed without a look, so its reservation is released, and that the
   nine `*` scopes are re-reserved for `research/preregs/breadth_bars_holdout_prereg.json` only. Add
   a line in `CHANGELOG.md`, which is where earlier signature decisions were recorded.
2. A registry edit in its own reviewed commit. In each of the nine `*` rules, `reserved_for` becomes
   `["research/preregs/breadth_bars_holdout_prereg.json"]`. Keep C8 out of the list, so the released
   study cannot also read the sessions. Then run
   `python -m iap.governance.holdout_registry check` and update the assertion in
   `test_holdout_registry.py` in the same commit.
3. The old reservation is then history in git and in the decision record. `breadth_book_prereg.json`
   stays as it is: signed, unedited, outcome "no confirmatory look".

The runner enforces step 2. It refuses unless the `*` scope of every session is `unread` and lists
the new document in `reserved_for` (`test_reservation_for_another_prereg_refuses`).

## 3. How a document is signed here

- **Key.** Ed25519, maintainer `ashjha0`, public key
  `02a3e278...c2106c` in `research/agents/trust_roots.json`. It is pinned with its fingerprint in
  `tools/verify_signatures.py` (`PINNED`).
- **Where the private key lives.** Outside the repository. `tools/prereg_bind_identity.py` defaults to
  `C:/Users/ashis/.iap_keys/maintainer.key` (a JSON object keyed by maintainer id). On this machine
  that file is present (79 bytes), next to a `researcher.key`. I did not open, print, copy or move
  either file, so I have not checked that it derives the trust-root key. The sign tool checks that
  before signing.
- **Sign.** `tools/prereg_bind_identity.py sign --prereg <file>`. It refuses in these cases: the
  document is already signed; there is no `analysis_identity`; the working tree is dirty (except the
  prereg); the protocol changed since bind; no planted dry run is bound, or the dry run fails to
  demonstrate a verdict value (R4); `verify_identity` fails at HEAD; or the key does not match the
  trust root.
- **Bind first.** `tools/prereg_bind_identity.py bind` requires a clean tree and a committed protocol,
  scripts and inputs. It writes an unsigned `analysis_prereg` document holding only the identity and
  `dry_run` blocks. `tools/breadth_bars_holdout.py assemble` then copies the filled draft's study
  fields into that file and takes `bars_tool` from the bound commit. It refuses placeholders and any
  disagreement with the bound family, data versions, metric or thresholds.
- **Verify.** `PYTHONPATH=python/src python tools/verify_signatures.py [--trust-ref origin/main]
  [--strict]` checks every tracked signed JSON document, the agent key registry and the blackboards.
- **What CI checks** (`.github/workflows/ci.yml`, job `governance`): verify-board against
  `origin/main`, the agent key registry, `tools/prereg_touch_check.py` (warning only),
  `tools/data_manifest.py check` (every 64-hex value under a dataset-named key in a prereg must be a
  listed dataset or a recorded raw input), and the ledger and board monotone check. A change under
  `research/preregs/**` also runs the slow Python suite.
- **`--require-ci-env`.** The registration scripts (`tools/breadth_book_register.py`,
  `tools/b7_shortfall_register.py`) call `fingerprint.check_registration_env`. With the flag, they
  refuse when installed packages differ from `python/requirements-ci.txt`. The note is: register from
  `.venv-ci`, built from the hashed lock (`docs/RUNBOOKS.md`). This look has no blackboard
  registration script. The equivalent protection is identity v2: it binds the lock and the installed
  packages, and both sign and run refuse on drift. So bind, sign and run all from
  `.venv-ci\Scripts\python.exe`.
- **Who signs.** See the short version, item 2: DR-2 reserves signing to the owner. Under
  `require_independent_runner` the run must use another actor id in any case.
- **Image digest.** By default bind binds `deployment/docker/IMAGE_DIGEST`. The runner then refuses
  unless `IAP_IMAGE_DIGEST` declares that image. The bars tool runs in WSL, outside that image, so I
  propose `--no-image-digest`, with that reason written in the decision record. That is the owner's
  call.

## 4. The raw-data gap and the guard that closes it

The bars path never opens an ingested dataset. `rust/itchbars` reads the vendor `.gz`, and
`breadth_bars.load_panel` reads the bars CSV with `pandas.read_csv`. CSV is not one of the guarded
`iap.io` readers, and a bars file has no `dataset.json`. So `guard`, the `reader_guard` backstop and
`check_prereg_identity` never see the read. `docs/GOVERNANCE.md` already lists raw-feed readers as
"do not consult the registry". Today only date checks stand in the way: `itch_bars_chain.py` refuses
2025 and later, and `breadth_bars_pooled.py` refuses bars files dated 2025 or later.

`iap.research.breadth_holdout` is the guard for this route. Before any download it requires:

| check | refusal test |
|---|---|
| prereg present, kind `breadth_bars_holdout`, no `<FILL` placeholder, not a DRAFT | `test_missing_file_refuses`, `test_each_missing_or_mismatched_field_refuses`, `test_unsigned_draft_refuses` |
| Ed25519 signature valid under the trust root; no edit after signing | `test_edit_after_signing_refuses`, `test_key_outside_the_trust_root_refuses` |
| exactly the nine sessions, one raw file each (name, url, bytes, sha256) | parametrised field test |
| universe file and its sha256 match the tree | `test_changed_universe_file_refuses` |
| family, parameters, primary statistic, alpha and min sessions present and valid | parametrised field test |
| `rust/itchbars` tree hash matches the registered one | `test_changed_bars_tool_refuses` |
| `verify_identity` passes, with data versions = the raw sha256, and the registered family, metric and thresholds | `test_identity_mismatch_refuses`, `test_identity_check_receives_the_registered_values` |
| runner is not the signer and holds no key | `test_runner_must_not_be_the_signer` |
| family has an implementation in this checkout | `test_unknown_family_refuses` |
| registry intact; each `*` scope `unread` and reserved for this prereg; the excluded symbols are exactly the registry's spent ones | `test_reservation_for_another_prereg_refuses`, `test_spent_sessions_refuse`, `test_exclusion_must_match_the_registry_spent_names`, `test_resealed_by_hand_registry_refuses` |
| no result file and no start marker exist | `test_full_fake_run_end_to_end_then_one_look_only`, `test_a_started_but_unfinished_look_is_spent` |

Then the run proceeds as follows:

1. It writes the start marker `research/round1/holdout_look/<stem>_STARTED.json`, opened exclusively,
   so a crash still leaves the look spent and a re-run refused.
2. It appends one record to the hash-chained access log.
3. For each session: download, check bytes and sha256 against the prereg, build bars, delete the raw
   file (always), then run the registered family on the universe minus AAPL, MSFT and QQQ.
4. It pools the session values and computes the verdict by code: `pass`, `fail` or `incomplete`.
5. It writes `<stem>_RESULT.json` exclusively.
6. It marks the nine `*` scopes `spent` with `spent_by` the prereg, and reseals the registry.

**What it cannot stop.** Someone who runs `itchbars` by hand on a 2025/26 file, or edits the date
checks, is outside every supported route. Review stops that, not code, as `docs/GOVERNANCE.md` says
of unsupported routes. The bars files of the nine sessions stay under `data/real/bars_holdout/`
(git-ignored) after the look; by then they are spent.

**Coverage test.** `test_no_prereg_consumer_skips_the_identity_check` lists the new module as a
runner. On this branch that test already fails for an unrelated committed file,
`python/src/iap/graph/research_flow.py`. That is a separate fix.

## 5. Power and the stop rule

`iap.research.pnl_power.mde_sharpe(9)` is 16.94 at K = 1, 19.48 at K = 2 and 22.09 at K = 4.
`research/round0/ROUND0_GATES.md` section 2 says: "a confirmatory study is registered only if its
sealed session count reaches the table's entry for the Sharpe it looks for and the K it registers".
The draft therefore states that it looks for a net Sharpe of 17 or more at K = 1. It also states that
a fail says nothing about a realistic Sharpe. Registering more than one family would raise the floor
and waste the look, which is why the family is chosen in-sample first.

## 6. Other points that need a decision

- **Ledger.** The C8 registration went through `WriteBroker.preregister` and the experiment ledger.
  This look has no broker path. Whether it should debit a look in `research/experiments.json` (and
  how N counts it) is not settled by any rule I found. I propose one ledger entry, written by hand
  in the same commit as the result, through the existing ledger tooling. The coordinator decides.
- **Bars of a family not yet written.** `default_study` runs `breadth_bars.run_study` for
  `vwap_deviation` and `residual_reversion`. For any other family it calls
  `iap.research.breadth_families.run_family(panel, rows, family=..., **params)` and reads
  `["net_bps_per_rebalance"]["mean"]`. That module is not in this worktree; confirm the interface
  when it lands, before binding.
- **Signer and runner independence**: if the coordinator both signs and runs, `IAP_RUN_ACTOR` passes
  mechanically. In substance the same party does both. Record that in the decision record.
