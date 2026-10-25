# Reproduction in the pinned image (v2.0 stage 3, P1)

The canonical execution environment of v2.0 is a pinned Linux container (programme plan rev 3,
section 3): every confirmatory run and every registered analysis executes in it, and its digest is
bound into the prereg. This page is the one-page guide: what the image is, how it is pinned, the
exact commands that reproduce the two public results inside it, the status vocabulary, and what an
independent party runs for P2. It supersedes the tier A and B parts of
[release/F7_REPRODUCTION_GUIDE.md](release/F7_REPRODUCTION_GUIDE.md); that guide's tier C (re-running
from the vendor files) is unchanged and still needs the market data.

## 1. The image

| what | where |
|---|---|
| Dockerfile | `deployment/docker/Dockerfile.repro` |
| contents | CPython 3.12.10 (`python:3.12.10-slim-bookworm`, pinned by digest) plus every package of the hashed CI lock `python/requirements-ci.txt` installed with `--require-hashes`, `git`, a non-root user `iap` (uid 10001). No repository code: the checkout is mounted at `/work`, so one image serves every commit |
| entrypoint | `iap-repro` (`deployment/docker/repro_entrypoint.py`): runs a named runner by its `analysis_identity`, section 3 |
| registry | `ghcr.io/ashjha0/intraday-alpha-platform-repro`, tags `inputs-<hash12>` and `sha-<commit12>`; **the digest is the identity** |
| digest of record | `deployment/docker/IMAGE_DIGEST`: one line, `sha256:<64 hex>`. What `tools/runner` pulls, what `tools/prereg_bind_identity.py bind` binds as `analysis_identity.image_digest`, what every new prereg references |
| build inputs | `python tools/repro/image_inputs.py` lists them (the Dockerfile and every file it `COPY`s); `--hash` is one sha256 over them, stored on the image as the OCI label `org.iap.repro.inputs_sha256` |
| no toolchains | B8 v2 `analyse` and the scorecard render are Python; the full REL-08 screen needs a feature store built with the Rust engine, which is market data and not a public file (F7 guide section 4.4). A toolchain is added only when a reproduction target needs one |
| no network | nothing in a reproduction fetches anything; every run below passes `--network none`, so that is a property of the run, not a promise |

**How the digest gets into git** (`.github/workflows/repro-image.yml`, every pull request and every
push to `main`): the job computes the inputs hash, pulls the image named by `IMAGE_DIGEST` and reads
its label. Equal: the image is current, nothing is built. Different (or no digest yet): the image is
built, pushed to GHCR, and the new digest is committed to the pull request branch by
`github-actions[bot]`; a push made with `GITHUB_TOKEN` starts no workflow, so the job then
dispatches `ci.yml` and itself on the branch (`workflow_dispatch` is the documented exception) and
the new head gets its checks. If a required check still shows as missing on that head, a person
pushes an empty commit. On `main` (the lock changed under a PR that did not carry the digest) the
job opens a follow-up pull request `repro-image/digest-<sha12>` instead. From a fork, or on a
Dependabot pull request (read-only token), the image is built but not pushed, the digest of record
is unchanged, and the job summary says what a maintainer runs:

```bash
gh workflow run repro-image.yml --ref <branch>
```

An image build is not bit-reproducible (apt, timestamps), so "same inputs" is decided by the
label, never by rebuilding; the digest changes only when the Dockerfile, the lock or the entrypoint
change. The image is scanned with trivy (fixable HIGH/CRITICAL, report-only: a finding is a lock
or base bump for Dependabot, not a failed reproduction).

## 2. What is reproduced, and the outcome

Two results, from public files only, inside the image, compared byte for byte with the committed
files. CI runs this on every pull request (`repro-image` job); `tools/repro/reproduce.sh` is the
same procedure for a person with docker.

| result | runner | inputs (committed) | reproduces | code that runs |
|---|---|---|---|---|
| B8 v2 (REL-04b) | `research/execution/b8v2/run_b8v2.py analyse` (the bound runner of `research/preregs/b8v2_prereg.json`) | `benchmark_v2_oos3_{AAPL,MSFT}.json` | `DECISION.json`, `REPORT.md` | the checkout's HEAD: the decision rule has been byte-stable across every commit since the run |
| REL-08 scorecard | `tools/repro/render_rel08_scorecard.py` (the screen runner's own `render_scorecard`) | `research/v118/rel08/screen_ds_20190130.json` | `SCORECARD_ds_20190130.md` | the tree at the commit that wrote the committed scorecard (`iap-repro --code-at`): a later commit changed the scorecard's units and precision, so HEAD's renderer presents the same numbers differently. That render is made too and listed as information; it never decides the status |

Outcome per file (`tools/repro/compare_reproduction.py`): **byte-identical**; **within-tolerance**
(JSON only: every difference numeric and inside the result's declared tolerance,
`reproducibility.tolerance` of the B8 v2 predeclaration: 0.01 bps, 0.0001 for fractions, counts
exact, every difference listed); **differs**. The report (`reproduction_report.md` and `.json`:
sha256 and size of every file, the outcome per pair, the identity record of each run, the image
digest) is a CI artefact and the job summary; the job fails when a file of record is not
byte-identical or within tolerance.

Both results re-derive byte for byte in the suite as well, without docker
(`python/tests/test_repro_image.py`: `test_b8v2_decision_and_report_rederive_byte_for_byte`,
`test_rel08_scorecard_rerenders_byte_for_byte_at_its_own_commit`). The identity record of each
in-image run states how the tree that ran differs from the tree the signature bound (for B8 v2 the
`python/src/iap` tree differs from the bound v1 `code_tree_sha256`; for REL-08 the tree at the
scorecard's commit differs from the rule's bound tree): a re-derivation from the committed raw
results is not a re-run of the registered analysis, and the report says so.

## 3. The exact commands

With docker, from the repository root, at any commit that carries `deployment/docker/IMAGE_DIGEST`:

```bash
bash tools/repro/reproduce.sh                      # image by digest of record, output in repro_out/
bash tools/repro/reproduce.sh --image ghcr.io/ashjha0/intraday-alpha-platform-repro@sha256:<digest> --out <dir>
```

What the script does, step by step (so it can be done by hand):

```bash
DIGEST=$(cat deployment/docker/IMAGE_DIGEST)
IMAGE=ghcr.io/ashjha0/intraday-alpha-platform-repro@$DIGEST
mkdir -p repro_out/b8v2 repro_out/rel08 repro_out/records && chmod -R a+rwx repro_out
cp research/execution/b8v2/benchmark_v2_oos3_AAPL.json research/execution/b8v2/benchmark_v2_oos3_MSFT.json repro_out/b8v2/

# B8 v2: analyse re-derives DECISION.json and REPORT.md from the two headline JSONs
docker run --rm --network none -v "$PWD:/work:ro" -v "$PWD/repro_out:/out" \
  -e IAP_IMAGE_DIGEST="$DIGEST" -e IAP_REPRO_RECORD=/out/records/b8v2.json \
  "$IMAGE" research/preregs/b8v2_prereg.json -- analyse --out /out/b8v2

# REL-08 scorecard: rendered by the renderer at the commit that wrote the committed scorecard
AT=$(git log -1 --format=%H -- research/v118/rel08/SCORECARD_ds_20190130.md)
docker run --rm --network none -v "$PWD:/work:ro" -v "$PWD/repro_out:/out" \
  -e IAP_IMAGE_DIGEST="$DIGEST" -e IAP_REPRO_RECORD=/out/records/rel08.json \
  "$IMAGE" --code-at "$AT" --runner tools/repro/render_rel08_scorecard.py research/v118/rel08_rule_signed.json -- \
  --screen research/v118/rel08/screen_ds_20190130.json --out /out/rel08

# compare byte for byte; exit 0 = reproduced
python3 tools/repro/compare_reproduction.py --out repro_out --tolerance research/execution/b8v2/predeclaration.json \
  --record b8v2=repro_out/records/b8v2.json --record rel08=repro_out/records/rel08.json --image-digest "$DIGEST" \
  research/execution/b8v2/DECISION.json=repro_out/b8v2/DECISION.json \
  research/execution/b8v2/REPORT.md=repro_out/b8v2/REPORT.md \
  research/v118/rel08/SCORECARD_ds_20190130.md=repro_out/rel08/SCORECARD_ds_20190130.md
```

`iap-repro` (the entrypoint) in general:

```text
iap-repro [--root /work] [--runner REL] [--code-at COMMIT] [--require-identity] [--actor NAME] [--list] PREREG [-- RUNNER_ARGS...]
```

It loads the signed prereg or rule, verifies the signature under the checkout's trust root, resolves
the runner (the one bound script named `run_*.py`, or `--runner`; a runner outside the bound scripts
is reported as such), compares what is about to run with the bound identity (scripts, code tree,
inputs, configs, reference data, lock, installed packages, interpreter, image digest) and the commit
ancestry, prints every mismatch, and runs the runner with `PYTHONPATH=/work/python/src`,
`IAP_RUN_ACTOR=<actor>` and no signing key in the environment. `--require-identity` turns any
mismatch into a refusal (exit 2): that is the registered runner's mode. Without it the mismatches
are recorded in `$IAP_REPRO_RECORD` and the run proceeds: that is the re-derivation mode above.
`--code-at COMMIT` exports `python/src`, `tools`, `configs`, `data/reference`, `schemas`, the lock,
the bound scripts and the runner at that commit from the checkout's history and runs those.
`--list` does everything but run.

Without docker, the same re-derivations run in `.venv-ci` (F7 guide tier A): the two tests named
in section 2, or the commands above with `python` in place of `docker run ... "$IMAGE"`.

## 4. Identity: paths and the image digest

* **Runners write relative paths.** `iap.research.execution_benchmark.portable_path` records every
  path field of a benchmark result (`data_dir`, `config_dir`, `grid.size_basis`,
  `two_venue.calibration`, `two_venue.second_data_dir`, `queue_cancel.calibration`) repo-relative
  with `/` separators (else relative to the working directory, else absolute in POSIX form). A
  result produced on the laptop and one produced at `/work` record the same strings, so the raw
  benchmark JSONs of a future run compare byte for byte instead of after masking five fields. The
  committed B8 v2 JSONs predate this and still carry the owner's absolute paths (F7 guide section
  3.4); they are inputs here, not outputs, so nothing in this page depends on them.
* **`analysis_identity` accepts the container path.** Every path in a block is repo-relative and
  every check takes the checkout root, so a block built at `C:\Work\...` verifies at `/work`
  (test `test_identity_verifies_at_a_relocated_posix_root`). The image sets `safe.directory=*`
  so `git` reads a checkout owned by another uid, and never writes byte-code into it.
* **`analysis_identity.image_digest`.** `build_identity(..., image_digest=...)` binds the
  reproduction image; `tools/prereg_bind_identity.py bind` takes it from
  `deployment/docker/IMAGE_DIGEST` by default (`--image-digest` / `--no-image-digest` override). A
  block that carries it verifies only when `IAP_IMAGE_DIGEST` in the runner's environment equals
  it; the registered-run workflow sets that variable from the same digest it pulls, so the check is
  "you are inside the image you said you are". A block without it verifies as before, so every
  earlier v2 prereg stays valid.

## 5. Status vocabulary

The programme plan (section 8, stage 3) closes gate F7 only on **reproduced independently**: a
party other than the owner, not an agent on the owner's machine, runs the reproduction and files a
note. The spec's P2 acceptance records exactly one of:

| status | meaning | who |
|---|---|---|
| **reproduced independently** | an independent party ran section 3 (and, for tier C, the full re-run) and every file of record was byte-identical or within tolerance; their note is filed verbatim under `docs/release/` | the independent party |
| **attempted, discrepancies unresolved** | a reproduction was run and at least one file of record differs beyond tolerance, with every difference listed | whoever ran it |
| **not attempted** | no reproduction run | |

What this PR reaches, stated as such: **reproduced in the pinned image by CI**. The owner's own CI,
on GitHub-hosted runners, inside the pinned image by digest, with `--network none`, from public
files only, re-derived the B8 v2 decision and report and the REL-08 scorecard byte for byte (the
`repro-image` job of the merged commit; its report is the job summary and artefact
`reproduction-<sha>`). That is a reproduction by the owner's infrastructure, not by an independent
party, and it re-derives committed raw results rather than re-running the registered analyses from
market data (F7 guide tier A, in the image). F7 stays open until P2.

## 6. P2 hand-over: what an independent party runs

1. A Linux machine with docker (any distribution; no Python needed beyond the system `python3`
   for the comparison script). Clone the repository at the merged commit of this PR or later:
   `git clone https://github.com/AshJha0/intraday-alpha-platform.git`.
2. Verify the registrations without data: `PYTHONPATH=python/src python3 tools/verify_signatures.py --trust-ref origin/main --strict`
   (F7 guide section 2; the trust root is in the repository, nothing depends on the owner's key).
3. Pull the image **by digest** (`deployment/docker/IMAGE_DIGEST`), never by tag, and run section 3.
   Record the digest you pulled, the commit you ran at, and the `reproduction_report.md`.
4. Optionally verify the image's provenance: it is built by `repro-image.yml` from this
   repository (OCI labels `org.opencontainers.image.source` and `.revision` name the commit).
5. Tier C (the market data) is the F7 guide, sections 3.2 to 3.3: the data must be fetched under
   the vendors' terms; the owner cannot send it.
6. File your note verbatim under `docs/release/` with exactly one status of section 5, every
   difference listed, and what you could not run. That note, not this page, closes or keeps open F7.

## 7. Owner actions

None are needed for the mechanism above: `GITHUB_TOKEN` with `packages: write` publishes to GHCR
under this repository, as `release.yml` already does. If the package
`intraday-alpha-platform-repro` is created private (the organisation default), set it to public in
the package settings so an independent party can pull by digest without a token; the CI job logs in
and is unaffected either way. Naming the P2 party is the owner's decision (programme plan,
stage 3 exit).
