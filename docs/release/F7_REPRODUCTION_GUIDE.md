# F7 reproduction guide for the outside reviewer (DRAFT)

Status: **draft from a desk check, 2026-10-22. Nothing in this file was run.** Every command is
copied from a runner or a committed document; every hash below was computed from the result
files in the owner's run worktrees, not from `main` (the results are not committed yet, see
section 8). Fill the `TBD` cells and delete this banner when the result PR lands.

Gate F7 (programme section 9.2): *the outside reviewer reproduces the REL-04b JSON under the
reproducibility contract (section 9.6)*. Deliverable (b) of section 9.3: *reproduce the REL-04b
JSON and one alpha-track scorecard from the review pack on your own machine, with a written note
of what you could not reproduce.* This guide supplements [../REVIEW_PACK.md](../REVIEW_PACK.md)
section 3, whose REL-04b and REL-08 entries are still placeholders.

## 0. Read this first: three tiers, and what each proves

| tier | what you do | market data? | machine | proves |
|---|---|---|---|---|
| **A. Re-derive the decision** | run `analyse` on the shipped raw result JSONs | no | any, minutes | the statistics, decision rule and report follow from the raw cell results; byte-exact |
| **B. Verify the registration** | verify signatures, check the bound commit, scripts and data identifiers | no | any | the run that produced the JSONs was the registered one |
| **C. Re-run from the vendor files** | fetch the exchange files, ingest, run | yes, about 0.1 TB | Windows 11, Python 3.12.10, hours | the raw result JSONs themselves |

Tier A and B need only this repository plus the result files. **Tier C is the only tier that
reproduces the numbers from market data, and it is the one that carries real obstacles** (the
environment identity check, 15 hours of run time, absolute paths in the output; sections 5 and 7).
Say in your note which tiers you reached.

## 1. Environment

* Interpreter: **CPython 3.12.10**. Every signed preregistration of the Windows runs binds
  `cpython-3.12.10`; B8 v2 binds the code tree only (identity v1) and does not check the interpreter.
  CI pins 3.11 for the required `python` check and also runs 3.12 (`.github/workflows/ci.yml`).
* Dependencies: the CI lock `python/requirements-ci.txt` with hashes
  (sha256 of the file at `main` fb8c6657: `2f68340d1a5c28871356287b00f5af907dd94f429c2551dac5cee0a3a114c18e`).
  Use **the lock at the commit you check out**, not at a later one (the lock moved at `9a6b67b1`).

  ```bash
  python3.12 -m venv .venv-ci
  .venv-ci/bin/pip install --require-hashes -r python/requirements-ci.txt     # Windows: .venv-ci\Scripts\pip
  export PYTHONPATH=python/src PYTHONUTF8=1
  ```
* Identity v2 runners (REL-05, REL-09, REL-10, REL-08 screen) also bind the **exact installed
  package set including packages that must be absent**; see the traps in section 7.

## 2. Verify the registration (tier B, no data)

```bash
git clone https://github.com/AshJha0/intraday-alpha-platform.git && cd intraday-alpha-platform
git checkout <bound commit from the table in section 3>
PYTHONPATH=python/src python tools/verify_signatures.py --trust-ref origin/main --strict
```

Each result below names three commits, and they differ: the **identity commit** (field
`analysis_identity.commit` in the signed prereg), the **signing commit** (the commit that added the
signature) and the **run commit** (the checkout the owner's run started from). The runners require
the identity commit to be HEAD or an ancestor of HEAD and the `python/src/iap` tree to equal the
bound one. Check out the **signing commit** (the first one that contains the signed prereg) and you
satisfy both.

| result | signed document | identity commit | signing commit | `python/src/iap` tree at signing commit |
|---|---|---|---|---|
| B8 v2 / REL-04b | `research/preregs/b8v2_prereg.json` | `4980fc11bd7542ab238af4577554058279505382` | `83f08210` (PR 271) | `4eb4223e` (same as identity commit) |
| REL-09 | `research/preregs/rel09_prereg.json` | `6dc3783c907231f0bc9ee1e28f2d3ee86909dec3` | `620a1c2c` | `7f505e84` |
| REL-10 | `research/preregs/rel10_prereg.json` | `620a1c2ce9f645f2391872fa858b04b37c8d0c6e` | `b5c22929` | `7f505e84` |
| REL-05 | `research/preregs/rel05_prereg.json` | `aa2d4b8242bd9cc3db92d44d8bd1653e3f14721a` | `fd98fe5e` | `b8deac01` (equals `main` fb8c6657) |
| REL-08 screen | `research/v118/rel08_rule_signed.json` | `739a7e069dceeec32cec37de7a4443730eeac88a` | `aecd59fe` | `eca491a2` (**differs** from `main`) |

Prereg file sha256 (the runner records it in `run_manifest.json`): B8 v2
`1c0191b5bef462f398aa6903feb3291ab0b868ed10ce9816af0dd8c21966df56`, REL-09
`642c8709db57b65ebe61053543ee60fc81761871fb3baccf37d3f8daca89d114`, REL-10
`f2dd068ea86d12de6647351f3eed9ce0f41dbeb7e31bc3d0d3c528f37424ef76`, REL-05
`cb8bf09deaac41cd4f72758fe8ef0b123825006a5da06a6abfc507de0d1d2d8b`.

**Do not run `run` on current `main`** for B8 v2, REL-09, REL-10 or the REL-08 screen: the
`python/src/iap` tree has changed since signing (39 files between `83f08210` and `main`) and the
runner refuses with `analysis identity mismatch`. Run at the signing commit.

## 3. Result A: B8 v2 (REL-04b) JSON

Declaration: `research/execution/b8v2/PREDECLARATION.md` and `predeclaration.json`. Six
policies-by-symbol contrasts (K = 6), evaluation sessions 2025-12-10, 2026-05-15, 2026-05-18
(G = 3: the report says the run reports size, not power).

### 3.1 Tier A: re-derive DECISION.json and REPORT.md (no market data)

Put the eight `benchmark_v2_oos3*_{AAPL,MSFT}.json` files from the result PR into a **copy** of the
output directory (`analyse` overwrites `DECISION.json` and `REPORT.md` in `--out`):

```bash
mkdir -p repro/b8v2 && cp <result files>/benchmark_v2_oos3_AAPL.json <result files>/benchmark_v2_oos3_MSFT.json repro/b8v2/
PYTHONPATH=python/src python research/execution/b8v2/run_b8v2.py analyse --out repro/b8v2
sha256sum repro/b8v2/DECISION.json repro/b8v2/REPORT.md      # compare with section 3.4
```

`analyse` reads only the two headline files (`benchmark_v2_oos3_{AAPL,MSFT}.json`); the six sensitivity
files are not inputs to `analyse`, so they are checked only in tier C.
Expected: byte-identical `DECISION.json` and `REPORT.md`.

### 3.2 Tier C: full re-run

Use the owner's wrapper as the template (`research/execution/b8v2/run_b8v2.cmd`; it hard-codes
`DATA=C:\Work\Claude\...\data\real`, see the findings in the PR description). The core command, from a clean checkout at
`83f08210`, with `IAP_RUN_ACTOR` set to anything but `ashjha0` and no signing key in the environment:

```bash
IAP_RUN_ACTOR=reviewer PYTHONPATH=python/src python research/execution/b8v2/run_b8v2.py run \
    --data-root <your data/real> --out <empty output dir>
```

`run` refuses (exit 2) on: unsigned prereg, identity mismatch, runner equals signer or a signing key
present, a dataset directory whose `dataset_version` differs from the declared one, or a holdout
registry refusal. It suspends and resumes the owner's chain B ingest (`process-iex-day` processes);
on your machine there are none, so that step is a no-op. It runs AAPL, then MSFT, one process at a
time: headline, then sensitivities `s1_no_hidden`, `s2_child_impact`, `s3_v117_child`.

Settings that matter and are pinned in `predeclaration.json`: seed `20260829`, `iex_clock: exchange`,
`impact_model: parent`, `adv_table: session` / `instruments`, `child_variant: capped`, venues file
`research/execution/b8/venues_xnas_iexg_v2.json`, calibration `research/execution/b8/calibration_train3/`
(reused byte for byte; the calibrate days are **not** read).

Time and memory: owner's run 2026-10-19 09:36 to 2026-10-20 01:00 UTC, **15 h 24 min** on the
owner's workstation (the oos6 run took about 95 min for AAPL and 50 min for MSFT per the older
`REPORT_oos6.md` on fewer cells). Peak RAM was not recorded (not recorded in the run manifest); the oos6 report says QQQ
was skipped for memory, so plan for a machine with ample RAM and say what you used.

### 3.3 Data you must fetch (Tier C)

| dataset dir under `data/real/` | `dataset_version` (registry / manifest) | raw file | raw sha256 | where from |
|---|---|---|---|---|
| `r7_20251210` | `dea2a6b03ae35df24e5c8751bea8ddcc0f9ca8b93e02c9e2fdb656b6ab62fd80` | `S121025-v50.txt.gz`, 11,557,662,295 B | `5a64714a79df1b5ea987b66e12d70005beeefe1cf6cea8c151d1adc842275741` | Nasdaq `emi.nasdaq.com/ITCH/Nasdaq%20ITCH/S121025-v50.txt.gz` (`research/r7/r7_candidates.json`) |
| `holdout2026` (both May sessions) | `d7f7ffd14fbc363550a5bc9afd886e36df0d851511f840615f1f089b22f5f237` | `itch50_05_15.gz` 13,047,496,628 B and `itch50_05_18.gz` 16,150,095,810 B | `42bcd01d...915c` and `c6a3394e...05ec` (full values in `research/data_manifest.json`) | Nasdaq ITCH directory, listed in `research/r7/nasdaq_listing_known.json` |
| `iex/iex_20251210` | `33d0cbe8a6601850472f584a919b98211dd1841af6667b4cd6faf22c51ffc157` | `20251210_IEXTP1_DPLS1.0.pcap.gz`, 11,453,956,522 B | `d9d2a708f52a5f8efcf4ef450a8d79f0a0ed95553f25e46c05f720df6d3ac30f` (plus TOPS `59476671...f2dc`, 12,292,715,623 B, used in ingest only) | IEX HIST, free daily files, `iextrading.com/trading/market-data/` |
| `iex/iex_20260515` | `bd6f9c707d898007ca281ff093602176c39ac1cf45e0894808e7d1f14bdd3904` | `20260515_IEXTP1_DPLS1.0.pcap.gz`, 11,905,260,029 B | `1b917e6101c1e1af9e082a5c79961e97e66d123d94aefbe7db0efc154ce2c683` | IEX HIST |
| `iex/iex_20260518` | `ba2022d0d53c013be27f9b3ce6eeef13baa4280009ef32f6c2761b43e61037f4` | `20260518_IEXTP1_DPLS1.0.pcap.gz`, 14,210,389,328 B | `a5239f606eb25a11b6621a379e4c6a77ea96545bee6e2482ee5e72d15a6173a2` | IEX HIST |

`dataset_version` is the sha256 over the normalized IAP1 bytes (`docs/governance/REPRODUCIBILITY.md`
section 1); the ingest is deterministic, so the same raw bytes and the same ingest code give the
same version, and the runner checks it before any analysis. Ingest procedure:
`docs/runbooks/RUNBOOK_process_and_delete.md` ("IEX HIST days", `process-iex-day`) for the IEX
days, `python -m iap.marketdata ingest --format itch50 ...` for Nasdaq
(`docs/REAL_DATA.md` section 3, `docs/runbooks/RUNBOOK_rel07_breadth_labels.md` for an example).
**The exact ingest invocation per dataset is not recorded in the repository** (not recorded in the repository); the
per-session `inputs` and `raw_sha256` in `research/data_manifest.json` are.

**Licensing.** The owner cannot redistribute any of this, raw or normalized (`docs/REAL_DATA.md`
section 2.1: Nasdaq lists historical TotalView-ITCH as not eligible for redistribution; derived
statistics are published). Nasdaq ITCH files come from Nasdaq's public sample directory under
Nasdaq's terms; IEX HIST files are free but under IEX's terms. You fetch them yourself; the pack
ships hashes only. Also needed, committed: the calibration files, venue file and
`consolidated_adv.json`, all fingerprinted in the prereg `scripts` block.

### 3.4 Expected output hashes (B8 v2, `research/execution/b8v2/`)

| file | bytes | sha256 |
|---|---|---|
| `DECISION.json` | 14,814 | `eb04cd67e147b3a25f5bfbd319a461fd30955c9ff530f9a37659c3ae396613d3` |
| `REPORT.md` | 1,748 | `e292cfe47eb6f2da42133020d86707515faaf80900be68a5c6cdb9f56b77b954` |
| `benchmark_v2_oos3_AAPL.json` | 2,306,082 | `6b43f80728f56c8eb169634efba7bcdecf49b640b04d9e7b6dc47ecb7b05d4d4` |
| `benchmark_v2_oos3_MSFT.json` | 2,267,404 | `24cec5c6b24da44e9f6b3700c2bf679d738884ab52149506e3f2a3cc8b526970` |
| `benchmark_v2_oos3_s1_no_hidden_AAPL.json` | 2,303,464 | `cdad9777b9c72795c53776e6038577eb00eac570f7de0e144ee5897944ab58fa` |
| `benchmark_v2_oos3_s1_no_hidden_MSFT.json` | 2,264,586 | `11131ed3fd3d178a456c4f1e91a1f2b52c4ab8c6d5740fdae947e4118af66b54` |
| `benchmark_v2_oos3_s2_child_impact_AAPL.json` | 2,306,893 | `f9ff9ed92898e09b03d9c4b81699aaef98f62df0808f20154cf5147c0d800487` |
| `benchmark_v2_oos3_s2_child_impact_MSFT.json` | 2,268,074 | `7bbb1d416310d05b9e634889b38da1effa1f1bfadc7fde95591ae4ed276b99bd` |
| `benchmark_v2_oos3_s3_v117_child_AAPL.json` | 2,301,029 | `01fee693102f9a3db2baea724c8740f375a894ed1f6429eff28b64bc824220f7` |
| `benchmark_v2_oos3_s3_v117_child_MSFT.json` | 2,260,523 | `32a63354327394606b4a1d216403013223e028966c491d160fb1a26c1eaa3c50` |
| `BENCHMARK_v2_oos3_AAPL.md` | 219,110 | `8655e1ea41f6cb6a2a57f64d5c194234a5d0e16ef92e5adcfcfcd440454f092e` |
| `BENCHMARK_v2_oos3_MSFT.md` | 204,585 | `2da470a614cb3990b57da9c5f620e4ddb135da44ccb93e1f9b675ab6954c4faa` |
| `BENCHMARK_v2_oos3_s1_no_hidden_AAPL.md` | 218,575 | `75889ac2e3bc428418d7ef4b1fff9cafacd81ef191e2b3f680ab78dca652f2af` |
| `BENCHMARK_v2_oos3_s1_no_hidden_MSFT.md` | 203,762 | `1ea7462c36377e0e1ab655f4a9d1998271d598771df55ea9f529d2b3009e717f` |
| `BENCHMARK_v2_oos3_s2_child_impact_AAPL.md` | 219,067 | `ad9c97f3ea6ca768f2f2eb3b6de72fa0e634871de4e5933be35fbcb0d331eb9f` |
| `BENCHMARK_v2_oos3_s2_child_impact_MSFT.md` | 204,522 | `255ef830e7a6713cd6e57d62450e100f308185480046f5a5d8230c3fdcd1c877` |
| `BENCHMARK_v2_oos3_s3_v117_child_AAPL.md` | 217,546 | `5e3f9dc158ec24b2584da4f993a37ff8d9d1ac69a194a0196a071c293a92bb9b` |
| `BENCHMARK_v2_oos3_s3_v117_child_MSFT.md` | 202,695 | `4c0ae9906fec186ce351fe58f26034bb1e0a11c8670142b0f0ced4a1eb69ac7e` |
| `run_manifest.json` (not reproducible: timestamps, actor) | | `see section 6` |

**Expected headline** (what a correct reproduction shows): all six contrasts `no_evidence
(underpowered)`; none of `pin_iex`, `sor`, `sor_sweep` better than pinned Nasdaq on both symbols
(`REPORT.md`).

**Byte-identity caveat for the `benchmark_*.json` and `BENCHMARK_*.md` files.** They embed
absolute paths of the owner's machine (`data_dir`, `config_dir`, `grid.size_basis`,
`two_venue.calibration`, `two_venue.second_data_dir`, all `C:\Work\Claude\...`). On your machine
those five fields differ by construction. Compare after masking them, then apply the declared
tolerance (bps within 0.01, fractions within 0.0001, counts exact; `predeclaration.json`,
`reproducibility_tolerance`). `DECISION.json` and `REPORT.md` contain no path and should be
byte-identical if the cells agree.

## 4. Result B: one alpha-track scorecard

Pick one. Recommended order for a reviewer who cannot afford 15 hours: **REL-05** (12 minutes,
three small sessions, runs on current `main`), then **REL-10** (22 minutes), **REL-09** (75
minutes). The **REL-08 screen** is the literal "alpha-track scorecard" but is the hardest to
reproduce (section 4.4).

All four use the same shape: signed prereg, a non-signing `IAP_RUN_ACTOR`, the datasets on disk
at the declared `dataset_version`, a `run` command and an `analyse` command that rebuilds
`DECISION.json` and `REPORT.md` from the saved rows without market data.

### 4.1 REL-05 real-tape fill test (Level 1 only)

```bash
# at the signing commit fd98fe5e (or current main: same python/src/iap tree b8deac01)
IAP_RUN_ACTOR=reviewer PYTHONPATH=python/src PYTHONUTF8=1 python research/execution/rel05/run_rel05.py run \
    --data-root <your data/real>          # refuses if an output file already exists: use a fresh checkout
```

Data: the seven `ds3_*` Nasdaq sessions (2019-01-30, 03-27, 07-30, 08-30, 10-30, 12-30, 2020-01-30),
`dataset_version`s in `research/execution/rel05/run_manifest.json` (below) and in
`research/data_manifest.json`; raw: Nasdaq ITCH sample files `01302019.NASDAQ_ITCH50.gz` and
siblings from the Nasdaq public directory. Requires at least **6.0 GB free RAM** (declared stop
rule; owner's run saw 8.5 GB free). Wall: 13:13 to 13:24 UTC, **11 min 44 s**.
Expected: `DECISION.json` has `model_supported_on_all_symbols: false` and states in `cannot_claim` that
Level 2 is not reachable (decision D4a).

### 4.2 REL-09 stale-quote protection

```bash
IAP_RUN_ACTOR=reviewer PYTHONPATH=python/src PYTHONUTF8=1 python research/v118/rel09/run_rel09.py run \
    --data-root <your data/real> --out <empty dir>
```

Data: 11 datasets (r7_20251128/08/09/10, iex_20251128/08/09/10, holdout2026, iex_20260515/18; versions
in `research/v118/rel09/predeclaration.json`). Selected age `25000000` ns. Wall 11:17:57 to
12:32:52 UTC, **75 min**. `analyse --out <dir>` re-derives the decision from `rows_*.json` (no data).

### 4.3 REL-10 volume-clock OFI

Same shape: `python research/v118/rel10/run_rel10.py run --data-root ... --out ...`; same 11 datasets;
wall 12:33:44 to 12:55:46 UTC, **22 min**; `analyse` reads `trips_evaluate.json`.

### 4.4 REL-08 screen scorecard (`SCORECARD_ds_20190130.md`)

Command from `python/src/iap/research/rel08_screen.py`:

```bash
IAP_RUN_ACTOR=reviewer python -m iap.research.rel08_screen \
    --dataset-dir <data/real/breadth/ds_20190130_rel07> --tag ds_20190130 --out <dir> --no-timestamp
```

but the dataset directory is not a download: the owner's wrapper (`data/real/run_rel08_screen.cmd`,
**not in the repository**) first ingests the 100-name slim 2019-01-30 Nasdaq file
(`01302019.NASDAQ_ITCH50.gz`, `--symbols` from `research/breadth/universe.json`,
`--partition-instruments 10 --slim`), checks `dataset_version ==
9a1297a161fc69c47a770dcab4e3e09d5ecd34e1d2a96b44209011485038041d`, builds the store with
`python -m iap.features --engine rust --workers 1 --xasset-reference symbol_map --labels-v2 1m`
and then runs the screen. Ingest alone is quoted at up to about 6 h in the wrapper comments. The
`--engine rust` step needs the Rust feature binary; how a reviewer gets it is not documented (unverified).
It also runs at a commit with `python/src/iap` tree `eca491a2` (not `main`).

Expected: every variant (V1, V2, V5, V6, V9, V10, V11) `underpowered`; summary `continue: []`.
**What will differ**: the committed screen JSON was produced without `--no-timestamp` (field
`generated`) and records the ledger position (`ledger.looks[].total_after`, 6827 to 6833), which
depends on how many looks `research/experiments.json` held when it ran; the screen also **appends
seven looks to that tracked ledger**. Compare after dropping `generated` and `ledger`.

## 5. Expected sha256 of every other result file

REL-09, `research/v118/rel09/` (worktree `wt-q-rel09`):

| file | bytes | sha256 |
|---|---|---|
| `DECISION.json` | 2,806 | `e56757a6d752d94cb7bc5c4d3c9ddc852b78165b8e89db8e5368e3448fae394f` |
| `REPORT.md` | 953 | `b938e2e6c3918b393c1ce4cee09a1fbd9d8bbfd47739261fff82db10cf2dc294` |
| `rows_calibrate.json` | 18,459 | `e896f28d4501103e1e181ab190cefe43281c173feecd9cbaa6c188b2c8030003` |
| `rows_evaluate.json` | 12,339 | `d23a543679c5845c42c32f9f7576c251c7fbe9e744a8ee4a4a0a3bcecf208dbb` |
| `run_manifest.json` (not reproducible) | | `950be4b5e518057f0b8576aaac3583df3ea00a2cc31d76170e93386254dc477f` |

REL-10, `research/v118/rel10/` (`wt-q-rel10`):

| file | bytes | sha256 |
|---|---|---|
| `DECISION.json` | 19,104 | `e0513ea0fd84ed42d5160e1277ff1ddfb93abe94626d3678eb1417dac6725c04` |
| `REPORT.md` | 4,012 | `c3a61984cc2f756de3e3d84ea25076882f9a1e329602799b28742a5c901276d4` |
| `calibration.json` | 593 | `b5933edd9f368ab6c84bf3480c0a49382bfbf453da99f632fac888eabbe685b1` |
| `trips_evaluate.json` | 1,624,453 | `eceeeef8d84b5017ae352ad28b2e0ec3c393de57d58fc3102b0402be322de312` |
| `run_manifest.json` (not reproducible) | | `a9b2b42a25ca2edf44e248821629f92460522d01b44b0b12e08a225501da1adb` |

REL-05, `research/execution/rel05/` (`wt-q-rel05`):

| file | bytes | sha256 |
|---|---|---|
| `DECISION.json` | 2,037 | `4d20e8aa029606d4d611a5635dde8be693440f98de8e65b8a87eeeecdd07c860` |
| `REPORT.md` | 1,320 | `29d3f58eeac2a1388bb293eac7371cba352e8c7013570d83a3ada13146d7f155` |
| `tape_AAPL.json` | 5,173 | `13a5a3d493768be0b6c8b809eaaee5b6100c54ada771fc846f6e6ff77a5e6fef` |
| `tape_MSFT.json` | 5,254 | `d307e91c37c4ef41e7fc9d04b82c1df1b04ecead26c750d61f2a49c7e0e26434` |
| `tape_QQQ.json` | 5,255 | `6206a6573161b9d1a2c892d684d51505524941940cd65bfa9b3e861db9774a64` |
| `run_manifest.json` (not reproducible) | | `b84ec9105f6d2fea975b38eef1dec4bea6325a178cb20b0d62a232058d06df70` |

REL-08 screen, `research/v118/rel08/` (`wt-rel08-run`, uncommitted):

| file | bytes | sha256 |
|---|---|---|
| `screen_ds_20190130.json` (contains `generated` and ledger positions) | 19,941 | `37bbe74f92bc52459cdabed0efd1dec30914f09ffa997c869f16fc26be9cd30b` |
| `SCORECARD_ds_20190130.md` | 4,829 | `ecb14c27303de35a7c3250dde8fa8b698e5d187d69fd586e4e74c009c2a6dce2` |

B8 v2 `run_manifest.json`: `commit_bound 4980fc11...`, prereg sha `1c0191b5...`, actor `b8v2-runner`,
signer `ashjha0`, 2026-10-19T09:36:05Z to 2026-10-20T01:00:14Z. It carries timestamps and the
actor, so expect it to differ; compare the `datasets` map and `prereg_sha256` only.

Dataset versions for the REL-05 sessions: 2019-01-30 `06419d74...7293`, 2019-03-27 `7c6ddcf3...5784`,
2019-07-30 `3731bbd4...2ced`, 2019-08-30 `ad9a62de...5498`, 2019-10-30 `517f666c...f005`,
2019-12-30 `53f48feb...540a`, 2020-01-30 `edb9b750...1e19` (full values in the run manifest).

## 6. Known non-reproducible, or depends on data you must fetch

1. **Everything in tier C needs vendor files you must fetch yourself** (about 0.1 TB compressed for
   B8 v2; Nasdaq ITCH and IEX HIST, sections 3.3 and 4). The owner cannot send them or the
   normalized datasets. The pack ships hashes; if your fetch yields a different raw sha256 the
   data changed upstream and nothing downstream is expected to match.
2. **IEX download URLs** in `research/v117/iex_overlap_manifest.json` carry `generation=` tokens
   and may no longer resolve; the file names and sha256 values are the identifier.
3. **Absolute paths in the raw benchmark JSON** (five fields, section 3.4): byte-identity of
   `benchmark_*.json` is impossible on another machine unless the data root is the owner's path.
4. **The REL-08 screen JSON** contains a wall-clock stamp and the ledger position, and the screen
   writes into the tracked `research/experiments.json`.
5. **`run_manifest.json`** files: timestamps, actor, free RAM.
6. **Any `run`** on a commit whose `python/src/iap` tree differs from the signed one is refused by
   design (section 2), and the identity-v2 runners also refuse on a different dependency set
   (section 7). That is a refusal, not a reproduction failure; report it as such.
7. **The tolerance contract** (bps 0.01, fractions 0.0001, counts exact) applies to the raw cells
   only where byte identity is not achieved; every difference must be listed in your note.
8. **Not reproducible at all without the owner**: nothing in the signed chain depends on the
   owner's private key (verification uses the public trust root), and no result depends on a
   private file other than the market data above. Ledger look counts in per-dataset ledgers under
   `data/real` are outside git (`REVIEW_PACK.md` section 5).

## 7. Traps a reviewer will hit (so you do not lose a day)

* **Environment identity (REL-05, REL-08, REL-09, REL-10).** These identity-v2 preregs bind the
  CPython patch version (`cpython-3.12.10`), the lock hash, and the installed package set
  including *absent* markers. REL-05 and REL-08 mark `hypothesis` and `coverage[toml]` absent;
  REL-09 and REL-10 mark `hypothesis` absent and require `coverage 7.16.2`, `colorama 0.4.6` and
  `tzdata 2026.5` present; the lock hash differs per prereg (`2f68340d1a5c` for REL-05/08,
  `3a4352e0d19d` for REL-09/10). Yet the CI lock lists `hypothesis` and `coverage[toml]`, so
  "install the lock with `--require-hashes`" **does not** produce the bound environment. `colorama`
  is Windows-only, so macOS/Linux cannot match REL-09/10 at all. Until this is resolved, tier C of
  REL-09/10 realistically means: Windows, CPython 3.12.10, lock at the signing commit, then
  `pip uninstall hypothesis`, and compare `python -m iap.agents.analysis_identity` output (see
  `environment_mismatches`) before launching.
* **B8 v2 is the portable one** (identity v1: scripts, `python/src/iap` code tree, data versions;
  no interpreter or package check), but its output was produced on CPython 3.12.10 and Windows;
  float results on another libm or NumPy build may differ in the last bits, hence the declared
  tolerance.
* `run_b8v2.py` suspends processes whose command line contains `process-iex-day`; it will do
  nothing on a clean machine and touches no other process.
* Runs append to `data/real/holdout_access.jsonl` and (as seen in the owner's worktrees) modify the
  tracked `research/holdout_registry.json`; use a throw-away clone.
* REL-05 and the owner's wrappers refuse if an output already exists: use an empty output directory.
* `python/requirements-ci.txt` has moved since signing. Check out the signing commit, not `main`,
  or the lock hash will not match.

## 8. What the result PR must add before this guide is final

* Commit the result files (section 3.4, 5) so a reviewer can run tier A without asking: B8 v2
  results are currently untracked in the owner's `wt-b8v2-run`; REL-05, 09, 10 results in
  `wt-q-rel05`, `wt-q-rel09`, `wt-q-rel10`; REL-08 in `wt-rel08-run`.
* Replace the `TBD` placeholders in `docs/REVIEW_PACK.md` section 3 with a pointer to this file
  and the commit that carries the results.
