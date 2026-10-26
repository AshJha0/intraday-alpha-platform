# COOKBOOK, v2.0: recipes that run on `main` today

Each recipe gives the exact command, what it prints, and the evidence class of anything it
produces (LEARN.md section 6). We ran every command on 2026-10-25 against `main` in the CI
environment (recipe 0) on Linux and pasted the output we saw; on Windows replace `.venv-ci/bin/python` with
`.venv-ci\Scripts\python` and `PYTHONPATH=src` with `$env:PYTHONPATH="src"`. Paths are relative to
the repository root unless a recipe says otherwise.

The v1.x engineering cookbook (200 recipes on the intraday engines, the C++ and Rust ports and the
goldens) is frozen at [../COOKBOOK.md](../COOKBOOK.md); its recipes still run but describe the
v1.x flows, not the v2.0 programme. Method: [LEARN.md](LEARN.md). Package map:
[ARCHITECTURE_v2.md](ARCHITECTURE_v2.md). Operator procedures: [RUNBOOK_v2.md](RUNBOOK_v2.md).

Recipes: 0 the environment · 1 the research loop on a family · 2 add a family specification ·
3 a forecast record and the engine on a fixture · 4 an E5 report · 5 a dataset's integrity evidence
· 6 the leakage canaries · 7 a confirmation profile's domain · 8 the confirmatory gate's refusals ·
9 dry-run the ingestion job · 10 verify the audit anchor and the signatures · 11 what the owner does
(ingest, approve a run) · 12 reproduce a committed result · 13 the documentation checks.

---

## 0. The environment (once)

```bash
python -m venv .venv-ci
.venv-ci/bin/pip install --require-hashes -r python/requirements-ci.txt
```

The hashed lock is the only supported environment for anything compared byte for byte (it is also
what the reproduction image installs, [REPRODUCTION.md](REPRODUCTION.md)). The engine suite that
every other recipe builds on:

```bash
cd python && PYTHONPATH=src ../.venv-ci/bin/python -m pytest -q tests/leakage tests/portfolio tests/execution/test_sim_waterfall.py tests/reports tests/equivalence
```

Expected: `62 passed` in about twenty seconds. Produces nothing with a class; these are
correctness tests on synthetic fixtures.

---

## 1. Run the research loop on a family specification against the synthetic fixture

The loop refuses anything but the development or validation partition, refuses a family whose
gate forbids a fit, fits the M0 forecast, runs the engine, builds the E5 report and records the run
in a selection ledger. The fixture panel is regenerated from its seed and checked against the
committed manifest hash first.

```bash
mkdir -p /tmp/panel /tmp/d1_out
PYTHONPATH=python/src .venv-ci/bin/python -c "import sys, pathlib; sys.path.insert(0, 'python/tests/research/fixtures'); import daily_panel; print(daily_panel.write_fixture(pathlib.Path('/tmp/panel')))"
PYTHONPATH=python/src .venv-ci/bin/python tools/research/run_family.py \
  --spec research/v2/families/D1_short_term_reversal.md \
  --data /tmp/panel/daily_panel.csv \
  --manifest python/tests/research/fixtures/manifest.json \
  --partition development --out /tmp/d1_out --ledger /tmp/d1_out/experiments.json
```

Expected (abridged): a JSON summary with `"route": "synthetic"`, the session counts
(`"traded": 168`), the economic screen, the decision tree (`"outcome": "GREEN-REGISTER-ONLY,
confirmation pending"` on this fixture) and `"report_sha256"` equal to the hash pinned in the
fixture manifest; exit 0. The output directory holds the report, the research ledger, the registry
and the selection ledger with one entry. A second identical run is the same look (the ledger still
holds one entry); a run on `--partition validation` is a different look with its own hash.

A family whose gate forbids a fit is refused before anything runs:

```bash
PYTHONPATH=python/src .venv-ci/bin/python tools/research/run_family.py --spec research/v2/families/D5_close_pressure.md --data /tmp/panel/daily_panel.csv --manifest python/tests/research/fixtures/manifest.json --out /tmp/d5_out --ledger /tmp/d5_out/l.json
```

Expected: `refused: d5: the specification forbids a fit (status 'data-ineligible', gate
'ineligible'); no development run`, exit 2, and no ledger file.

**Evidence class of the report: exploratory** (synthetic route, no claim; the report's
`evidence_class` field says so). The same command on the real daily route, once ingested, produces
at most *development-supported*.

---

## 2. Add a family specification

A family is one Markdown file under [`research/v2/families/`](../research/v2/families/) holding, in order: hypothesis,
mechanism, artefacts that could fake it, falsifier, data gate and its outcome, label definition,
signal and eligibility rule, development diagnostics, registration (family size `K` and the U13
profile) and a machine-readable JSON block the loop reads. The file is complete before any fit; a
family whose gate failed carries `fit_allowed: false` and has no development run.

1. Copy the closest existing specification (`D1_short_term_reversal.md` for a daily family) and
   fill every section; keep the JSON block's keys.
2. Add a row to [`research/v2/families/README.md`](../research/v2/families/README.md) with the evidence class **pending**.
3. Run the specification test:

```bash
cd python && PYTHONPATH=src ../.venv-ci/bin/python -m pytest -q tests/research/test_family_specs.py
```

Expected: every test passes; a missing section, a malformed block or an index row that disagrees
with the file fails with the name of the section. A family moves from *pending* only through an E5
report produced by recipe 1 on the development partition and recorded in the ledger, and to a
confirmed class only through a registered run (recipe 11).

---

## 3. Build a forecast record and run the engine on a fixture

A forecast is one record with seven fields; the registry entry that produced it is content
addressed. The engine refuses a record with a missing or malformed field and a record that is not
yet available at the decision time.

```bash
PYTHONPATH=python/src .venv-ci/bin/python - <<'EOF'
from iap.alpha.forecast import ForecastError, ForecastRecord, consume
from iap.alpha.registry import ForecastRegistry, RegistryEntry
from iap.contracts.versions import content_hash

registry = ForecastRegistry()
rid = registry.register(RegistryEntry(
    family_id="d1", horizon="5d", model_class="pooled_linear",
    parameters={"shrink": 0.5}, feature_set_hash=content_hash({"feature": "rev.r5"}),
    training_window=("2015-01-02", "2019-12-31"), dataset_identity="synthetic@fixture",
    software_version="v2.0-dev", selection_history={"fits_before_this": 0}))
rec = ForecastRecord(family_id="d1", name="AAA", available_ts=1_577_836_800_000_000_000,
                     horizon="5d", mu_hat_bps=12.5, uncertainty_bps=30.0, registry_id=rid)
print("registry id", rid)
print("consumed", consume([rec], t_decision=rec.available_ts, registry=registry)[0].mu_hat_bps, "bps")
try:
    consume([rec], t_decision=rec.available_ts - 1, registry=registry)
except ForecastError as exc:
    print("refused:", exc)
EOF
```

Expected:

```
registry id f1ff1fa044d94975ce5f373d5280d787239226f9169bb2c7b47c58d2d3e797c4
consumed 12.5 bps
refused: forecast d1/AAA/5d is available at 1577836800000000000 > t_decision 1577836799999999999
```

The registry id is deterministic: the same declaration always hashes to the same id. To run the
whole engine (projection, netting, execution, the two ledgers) on a fixture, run the H1
equivalence test, which reproduces the independent H1 reference implementation byte for byte on
its committed fixtures, and the waterfall test, which reconciles the cost waterfall to the cash
identity with partial fills, netting across two families and an overnight carry:

```bash
cd python && PYTHONPATH=src ../.venv-ci/bin/python -m pytest -q tests/equivalence tests/execution/test_sim_waterfall.py tests/portfolio
```

Expected: all pass. **Evidence class: none**; these are fixtures. The reference lives in
[`research/v119/h1_reference/`](../research/v119/h1_reference/) and imports nothing from `iap`.

---

## 4. Produce an E5 report

The builders take a session series (one row per session with its kind) and return tables with
units, counts and an uncertainty field; the inflation checks refuse an omitted session, a second
denominator or a credited refused order.

```bash
PYTHONPATH=python/src .venv-ci/bin/python - <<'EOF'
import numpy as np
from iap.reports import SessionRow, block_d_risk_adjusted, check_no_inflation, economic_screen, session_series

rng = np.random.default_rng(3)
rows = []
for i in range(40):
    if i == 10:
        rows.append(SessionRow(f"s{i}", "coverage_failure"))
    elif i % 7 == 3:
        rows.append(SessionRow(f"s{i}", "usable_no_trades"))
    else:
        g = float(rng.normal(250, 400))
        rows.append(SessionRow(f"s{i}", "traded", g - 150.0, g))
C = 1_000_000.0
s = session_series("d1", "5d", rows, C)
check_no_inflation(s, all_session_ids=[r.session_id for r in rows], denominators=[C])
print("sessions", s.counts, "dropped", s.dropped)
scr = economic_screen(s)
print("screen", {k: scr[k] for k in ("mean_bps", "se_bps", "retention", "passed")})
t = block_d_risk_adjusted(s)
print(t.title, t.units)
EOF
```

Expected:

```
sessions {'traded': 34, 'usable_no_trades': 5, 'coverage_failure': 1, 'unpriced_at_close': 0} dropped ['s10']
screen {'mean_bps': 1.0798972808800635, 'se_bps': 0.665449397946791, 'retention': 0.45229602526696144, 'passed': False}
E5 block D: risk-adjusted {'returns': 'bps of C per session', 'sharpe': 'per session (not annualised)'}
```

The no-trade sessions enter the series as zero and are counted; the coverage failure is dropped
and listed; the Sharpe ratio is per session and never annualised over active periods. The full
five-block report with the decision tree is what recipe 1 writes. **Evidence class: exploratory**
on random rows; the class of a real report is the loop's.

---

## 5. Check a dataset's integrity evidence in the manifest

`research/data_manifest.json` lists every dataset a record may cite with the integrity evidence
copied from its ingest (decode counts, content hash, the rebuilt-book check and, where the source
publishes an oracle, the verification against it) reduced to one verdict per session and per
dataset. CI runs the check on every pull request.

```bash
.venv-ci/bin/python tools/data_manifest.py check
```

Expected: `data-manifest: every cited dataset version is listed and carries a verdict`, exit 0. A
prereg or ledger citing a version that is not listed, or whose verdict is `none` or `mismatch`,
fails with the citing file named. To read the verdicts themselves:

```bash
.venv-ci/bin/python - <<'EOF'
import json, sys
sys.path.insert(0, "tools")
from data_manifest import verdicts
m = json.load(open("research/data_manifest.json", encoding="utf-8"))
for version, (verdict, where) in sorted(verdicts(m).items(), key=lambda kv: kv[1][1]):
    print(f"{where:40s} {verdict:20s} {version[:12]}")
EOF
```

Expected: one line per dataset, for example `iex/iex_20251128  clean, match  f977bb3a39d1` (the
rebuilt book is clean and the IEX TOPS oracle agrees) and `iex/iex_20251210  not clean
33d0cbe8a660` (acknowledged as a disclosed limitation in the manifest tool's
`NOT_CLEAN_ACKNOWLEDGED`, never a cleared verdict). A DD1 daily-route record appears as one entry
with four partition hash ids and the verdict `complete` or `incomplete`. **This recipe produces
integrity evidence, not a performance claim**; it carries no evidence class.

---

## 6. Run the leakage canaries

Three canaries, each with a positive control: a feature shifted forward one period scores at
chance while the unshifted one scores above it; a permuted label scores at chance; a planted leak
is caught by the contract's causality check.

```bash
cd python && PYTHONPATH=src ../.venv-ci/bin/python -m pytest -q tests/leakage/test_leakage_canaries.py
```

Expected: `7 passed` (the three canaries, the fixture's structure, and the proof that each canary
fails when it should). On your own panel:

```bash
PYTHONPATH=python/src .venv-ci/bin/python - <<'EOF'
from iap.data import synthetic
from iap.data.leakage import run_canaries
panel = synthetic.make_canary_panel()
res = run_canaries(panel, synthetic.feature_canary_signal, synthetic.LABEL, synthetic.feature_canary_planted_leak)
for name, r in res.items():
    if isinstance(r, dict):
        print(name, "passed" if r["passed"] else "FAILED")
EOF
```

Expected: `future_shift passed`, `shuffled_label passed`, `planted_leak passed`. Replace the
feature function and the label column with yours; `run_canaries` raises `CanaryFailure` on any
failure. Every development run on a real panel must run this on its own panel
(`iap/data/leakage.py`). No evidence class: a passing canary is a precondition, not a result.

---

## 7. Read a confirmation profile's declared domain

A profile's operating domain is read from its committed evidence file
([`research/v2/confirmation_evidence/`](../research/v2/confirmation_evidence/)) and nothing else
decides it.

```bash
PYTHONPATH=python/src .venv-ci/bin/python - <<'EOF'
import json
from iap.validation.profiles import PROFILES, operating_domain
for pid in sorted(PROFILES):
    print(pid, json.dumps(operating_domain(pid), indent=1, default=str)[:600])
EOF
```

Expected (abridged): for `PF-1`, `P-HAC-L5` with generators `iid_normal`, `garch11` and `t3`,
`"T_min": 120`, `"reference_passes": true`, `"reference_power_passes": true`, `"acf1_max": 0.0`;
`P-CBB` with `"reference_passes": false` (no domain); for `PF-2`, `P-HAC-L20` with `iid_normal`,
`ar1` at rho 0.1 to 0.3, `garch11` and `t3`, `"T_min": 250`; for `PF-3`, `None` (deferred: every
PF-3 registration is refused). The specification and the results table are
[`python/tests/spec/CONFIRMATION_PROFILES.md`](../python/tests/spec/CONFIRMATION_PROFILES.md)
sections 6 and 10; the framework is
[`research/v2/CONFIRMATION_FRAMEWORK.md`](../research/v2/CONFIRMATION_FRAMEWORK.md). The fast
test suite is `python -m pytest -q tests/validation/test_profiles.py` from `python/`. A
registration is valid only inside the domain of the profile it names (LEARN.md section 4). No
evidence class: this is the gate's own validation record.

---

## 8. See the confirmatory gate refuse

R0 is frozen and its implementation fails closed. The tests are the executable specification:

```bash
cd python && PYTHONPATH=src ../.venv-ci/bin/python -m pytest -q tests/spec/test_r0_frozen.py tests/test_confirmatory.py tests/test_confirmatory_refusals.py
```

Expected: `123 passed, 6 skipped` (the skipped cases compare against `statsmodels`, which the lock
does not pin). `test_r0_frozen.py` hashes the specification's bytes; a one-character edit of
[`python/tests/spec/CONFIRMATORY_TEST_SPEC.md`](../python/tests/spec/CONFIRMATORY_TEST_SPEC.md)
fails it, which is what "frozen" means. The refusal tests enumerate every `refused: <reason>` of
R0 section 5.7 and prove that only `iap.validation.confirmatory.decide` constructs PROMOTE. The
adversarial Monte Carlo suite itself is `python/tests/spec/r3_suite.py` (slow; its results are
committed in `research/validation/R0_RESULTS.json`).

---

## 9. Dry-run the ingestion job on synthetic vendor-format fixtures

The real run is the owner's (recipe 11). The dry run answers every vendor request from fixtures,
mocks the runner identity and writes only under temporary directories; it exercises the pull, the
partition cut, the five DD1 tests and the sealed `ingest` endpoint end to end.

```bash
PYTHONPATH=python/src .venv-ci/bin/python tools/ingest/dd1_daily.py run --dry-run --year 2026 --dev-root /tmp/dd1dev --sealed-root /tmp/dd1sealed
```

Expected (last lines):

```
dd1 test open_close_convention: pass
dd1 test membership_intervals: pass
partition development: 5 files written for the development identity
partition validation: 5 files written for the development identity
partition b: sealed in b, 5 files, chain head <16 hex>
partition c: sealed in c, 5 files, chain head <16 hex>
record written: status complete
{"status": "complete", "requests": 160}
```

Exit 0. The log never prints a price, a return, a filing or a symbol. The positive controls (a
truncated delisting, an unadjusted split, dropped sessions, an overlapping membership interval)
are in `python/tests/ingest/test_dd1_daily.py` (`18 passed`). **Evidence class of a real
ingestion: mechanically isolated** (one workstation, the owner its administrator).

---

## 10. Verify the audit anchor, the signatures and the runner identity

```bash
.venv-ci/bin/python tools/sealed_service.py verify-checkpoints research/audit/CHECKPOINTS.jsonl
PYTHONPATH=python/src .venv-ci/bin/python tools/verify_signatures.py --trust-ref origin/main --strict
PYTHONPATH=python/src .venv-ci/bin/python tools/runner/run.py whoami
```

Expected: `checkpoints ok: research/audit/CHECKPOINTS.jsonl` (the committed checkpoints carry only
sequence, chain head, time, prereg id and dataset id, in chain order); every signed document
verifies against the trust roots read from `origin/main` (needs a full-history clone); `whoami`
prints your account and exits 3, which is the runner refusing to proceed as anyone but
`iap-runner`. The runner and service suites run on synthetic fixtures in CI:

```bash
cd python && PYTHONPATH=src ../.venv-ci/bin/python -m pytest -q tests/runner tests/isolation
```

Expected: `33 passed` and `29 passed, 20 skipped` (the identity-by-partition matrix needs the
Windows accounts and `IAP_ISOLATION_CREDENTIALS`; it skips elsewhere). A signature proves integrity
and nothing else ([governance/TRUST_ROOTS.md](GOVERNANCE.md)).

---

## 11. What the owner does: ingest, and approve a registered run

Both are owner actions on the workstation and in the GitHub UI; nothing in this repository performs
them, and no session may. The runbooks are the procedure; this recipe only says where they are.

- **Ingest the free daily route**: [governance/DD1_INGESTION_RUNBOOK.md](GOVERNANCE.md)
  section 2 (interpreter for all users, the three sealed services, the runner, trigger
  `registered-run` in `dd1` mode, approve the `sealed` environment, read the redacted log, list the
  record in the manifest). Nothing has been ingested as of 2026-10-25.
- **Install the runner once**: [governance/RUNNER_SETUP.md](GOVERNANCE.md); the
  repository settings it needs (the `sealed` environment with the owner as required reviewer, the
  `results` branch ruleset) are [governance/REPO_SETTINGS.md](GOVERNANCE.md) section 7
  and are not yet set.
- **Approve a registered run**: [RUNBOOK_v2.md](RUNBOOK_v2.md) section 5 (the preconditions
  anyone can verify without a sealed read, the approval, where the result lands and how to verify
  it). The result's class is *mechanically isolated* while the owner holds every credential.

---

## 12. Reproduce a committed result in the pinned image

```bash
bash tools/repro/reproduce.sh
```

Expected: the image pulled by the digest in `deployment/docker/IMAGE_DIGEST`, the B8 v2 decision
and report and the REL-08 scorecard re-derived and compared byte for byte, exit 0. The full
procedure, the status vocabulary ("reproduced in the pinned image by CI" is not "reproduced
independently") and what an independent party runs are [REPRODUCTION.md](REPRODUCTION.md).
**Evidence class of both results: development-supported** ([REVIEW_PACK.md](../archive/docs/REVIEW_PACK.md)
section 0).

---

## 13. The documentation checks

```bash
PYTHONPATH=python/src .venv-ci/bin/python tests/harness/check_docs.py --strict
```

Expected: `PASS` for `internal_links` (every relative link and every `#anchor` resolves),
`diagram_index`, `api_names` (every `iap.*` name in an API document imports), `status_freshness`,
`release_truth` and `latency_claims`, then `0 failure(s), 0 stale status row(s)`. The `docs` CI job
runs the same command; a dead link, a vanished symbol or an undated "pending" phrase fails it.
