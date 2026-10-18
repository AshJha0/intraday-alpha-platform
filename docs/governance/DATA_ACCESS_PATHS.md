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
2. **Reader backstop.** The low-level readers call `reader_guard(path)` before they open a file:
   `iap.core.codec.read_iap1` / `iter_iap1` (normalized events), `iap.alpha.data.load_features`
   (feature parquet) and `iap.models.dataset.load_dataset`. A file inside a real dataset is allowed
   only if a guard already allowed that dataset in this process (or its parent process, via
   `IAP_HOLDOUT_AUTHORISED`), or if a fresh guard with no prereg allows it, which only in-sample scope
   can pass. A manifest-less file under `data/real` is checked by the session date in its name. The
   backstop is dataset-granular.
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

### Routes that are guarded by allowlist or name only

| Route | Check | Gap |
|---|---|---|
| `python -m iap.research.maker_real`, `quoting_real` | hard-coded in-sample `SESSIONS` (argparse choices) | parquet read directly; no registry lookup |
| `python -m iap.research.auction_real` (incl. `holdout`) | in-sample summary sha | no registry lookup |
| `models.lob_train` | `refuse_unit_name` | a renamed cache file is not recognised |
| `python -m iap.research.conformal_report` | none | reads any parquet path given |
| `python -m iap.mlops` | none | `np.load` of any path given |

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
`pandas.read_parquet` / `pyarrow` / `numpy.load` on files under `data/real`, a notebook or REPL that
opens files directly, copying data out of `data/real`, the raw-feed readers used for analysis rather
than ingest, `Itch50Reader` / `open_feed` / `load_manifest` / `decode_iap1` called directly, and setting
`IAP_HOLDOUT_AUTHORISED` by hand. No code can stop a person with file-system access from reading bytes
on their own disk; the controls above make the *supported* routes refuse, record every decision, and
make the record tamper-evident. A result produced through an unsupported route is not evidence under
the release programme.

## Residuals

- The backstop is dataset-granular, and it trusts `IAP_HOLDOUT_AUTHORISED`, which a process inherits
  from a parent that guarded.
- The log is tamper-evident, not tamper-proof: a person can truncate it at the end, or rewrite the
  whole chain from line one. The log is git-ignored with the data.
- `tools/rel06_known_null.py`, `maker_real`, `quoting_real`, `auction_real`, `conformal_report`,
  `mlops` and the LOB cache readers read through pyarrow / numpy directly, with no backstop.
