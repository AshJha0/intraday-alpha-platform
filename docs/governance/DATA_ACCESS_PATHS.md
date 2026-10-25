# Data access paths to `data/real`

Review finding 2 (outside reviewer, 2026-10-18): an auditable inventory of every supported route that
reads real data, the guard each uses, evidence that restricted reads are **blocked** (not only
logged), a record of attempted violations, and the unsupported routes stated plainly. Inventory taken
on main at 2026-10-18; guard code is
[`iap.research.holdout_registry`](../../python/src/iap/research/holdout_registry.py), tests are
[`test_data_access_blocking.py`](../../python/tests/test_data_access_blocking.py) and
[`test_holdout_registry.py`](../../python/tests/test_holdout_registry.py).

## How a read is decided

1. **Entry-point guard.** `guard(dataset_dir, purpose, prereg_id=...)` (or `guard_or_exit` /
   `guard_session_label` for CLIs) looks the dataset up by content hash and `dataset_version`, and by
   (session, symbol) scope, in the sealed `research/holdout_registry.json`. It decides from the
   manifest and the registry **before any data file is opened**; spent / unread / unregistered scope is
   refused unless a signed prereg listed for that scope authorises it (and, for an identity-bound
   prereg, its bound code, configs, lock, environment and dataset version match; see
   [ANALYSIS_IDENTITY.md](ANALYSIS_IDENTITY.md)). Only an allowed read re-hashes the files, and a byte
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

## Supported routes and their guard

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

### Routes with no entry guard of their own (blocked by the reader backstop)

These read normalized events or features through the guarded readers, so a restricted dataset is
refused at the first file; they do not log a purpose of their own, and they cannot read restricted
scope at all (no prereg id is passed): `python -m iap.features`, `python -m iap.mvp.real`,
`python -m iap.execution.dark --data-dir`, `python -m iap.execution.calibration --events`,
`python -m iap.execution.volume_curve --events`, `research/alpha_reports/run_all.py --dataset-dir`,
`research/ml_reports/run_ml.py`, `tools/v116_capacity_curve.py`, `tools/v116_monetisation_study.py`,
`tools/extract_real_slice.py`.

### Routes closed by D1 (v2.0 stage 2): formerly allowlist or name only

Since D1 these read through `iap.io`, so a file inside a registered (spent / unread / unregistered real)
dataset is refused before the library opens it, whatever the CLI argument said:

| Route | Before D1 | Now |
|---|---|---|
| `python -m iap.research.maker_real` (labels at the former `maker_real.py:440`, events, features), `quoting_real` | hard-coded in-sample `SESSIONS`; parquet read directly | `iap.io.read_parquet` / `parquet_file` / `read_table`, the choke point |
| `research.events`, `research.drift`, `portfolio.covariance`, `execution.signal`, `models.lob_real` (events and `.npy` / `.npz` units), `models.lob_train` (tick) | pyarrow / numpy directly | `iap.io`, the choke point (`refuse_unit_name` still applies to cache names under `data/real`) |
| `python -m iap.research.conformal_report --input x.parquet` | none | `iap.io.read_parquet` |
| `python -m iap.mlops` (`np.load` of the monitor input) | none | `iap.io.np_load` |
| `python -m iap.research.auction_real` (incl. `holdout`) | in-sample summary sha | unchanged: CSV reads of extracted auction files (not parquet / numpy); see Residuals |

### Raw-feed readers (ingest: read raw files to write datasets)

`python -m iap.marketdata ingest` (`Itch50Reader`), `marketdata.process_delete`,
`marketdata.process_iex`, `marketdata.iex` (pcap), `marketdata.breadth`, `auction.rel08c_extract`,
`auction.stream`, `research.rel04c_universe`, `research/v118/rel12_oracle/itch_cross_volume.py`,
`tools/iex_hist_conformance.py`. These do **not** consult the registry: a raw ITCH / IEX file has no
manifest, and ingesting a holdout day is a supported, planned step (R7, IEX overlap days). Their output
is a dataset that every analysis route above guards. `tools/r7_holdout_check.py` checks raw-file
hashes against the signed prereg before an ingest; it is a check, not a guard.

## Unsupported routes

Anything not listed above is **unsupported**, and the platform makes no claim about it: ad-hoc
`pandas.read_parquet` / `pyarrow` / `numpy.load` on files under `data/real` from code outside `iap`
(inside `iap` the AST test forbids them), a notebook or REPL that opens files directly, copying data out
of `data/real`, the raw-feed readers used for analysis rather than ingest, and `Itch50Reader` /
`open_feed` / `load_manifest` / `decode_iap1` called directly. There is no environment variable that
lifts a decision any more. No code can stop a person with file-system access from reading bytes
on their own disk; the controls above make the *supported* routes refuse, record every decision, and
make the record tamper-evident. A result produced through an unsupported route is not evidence under
the release programme.

## Residuals

- The hook is dataset-granular. Authorisation no longer crosses a process boundary: a child process
  that needs restricted scope must receive the prereg id and guard for itself (D-55 lists the one
  route that relied on inheritance, the features step of `research/v118/rel04_verify/run_rel04_verify.py`).
- CSV reads (`iap.auction.*`, `research.auction_real`, `research.conformal_report --input x.csv`) are
  outside the choke point: they read extracted auction files and report inputs, not datasets; a CSV
  copy of a sealed partition is a sealed read by the person who made it (SEALED_DATA.md section 4).
- The log is tamper-evident, not tamper-proof: a person can truncate it at the end, or rewrite the
  whole chain from line one. The log is git-ignored with the data.
- `tools/rel06_known_null.py` (under `tools/`, outside `iap`) still calls `pq.read_table` after its own
  `guard_or_exit`; the sealed partitions of v2.0 are reached only through the sealed service
  ([SEALED_DATA.md](SEALED_DATA.md)), never through these readers.
