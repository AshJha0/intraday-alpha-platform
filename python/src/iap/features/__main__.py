"""Feature pipeline: replay normalized data -> feature + label parquet files.

Usage (from python/ with PYTHONPATH=src):

    python3 -m iap.features [--data-dir ../data/normalized]
                            [--out-dir ../data/features]
                            [--configs ../configs]
                            [--registry-out ../data/reference/feature_registry.json]
                            [--cadence-ms 100]
                            [--files eq_20260824.normalized.iap1 ...]
                            [--workers N] [--worker-mem-gb 5]
                            [--engine python|rust]
                            [--feature-set default|extended]
                            [--sampling clock|events:N|volume:N]

v1.14 defaults (store_version 2, recorded in features_summary.json):
``--same-ts coalesce`` (D-P1-1: the events of one instrument sharing an
exchange_ts are one state change: one refresh, one set of samples, one row
at the group's last event; ``per_event`` is the v1.13 semantics),
``--label-freshness trailing`` (D-P2-13: the causal freshness bound;
``whole_day`` is the v1.13 store) and, on an ingested dataset,
``--cross-prints fold`` (D-P2-6: the side-less cross prints of dataset.json
count in volume, never in signed flow; ``omit`` is v1.13).  The v1.13 store
is ``--same-ts per_event --label-freshness whole_day --cross-prints omit``.

Opt-in (v1.12.0): ``--feature-set extended`` appends the Python-only
extended families of :mod:`iap.features.extended` (own feature_version);
``--sampling events:N|volume:N`` emits rows on an event / volume clock
instead of ``--cadence-ms``.  Both default off: the default build is
byte-identical to v1.11.

For every normalized input file (sorted by name = trading-day order; a fresh
engine per day, session profiles carried across days per instrument), the
pipeline replays events through the FeatureEngine and writes, per instrument:

    data/features/features_<instrument_id>.parquet

with columns: instrument_id, exchange_ts, one float64 column per registered
feature (NaN where invalid), a packed validity bitset, and per-horizon label
columns label_mid_<h> / label_cost_<h> / label_valid_<h> / label_reason_<h>
/ label_reopen_<h> (event-time forward returns, mid-to-mid and
cost-adjusted; two-pointer sweep, no lookahead; the reason bitmask explains
every invalid label — iap.labels.LabelReason; label_reopen_<h>, written
since v1.5.0, is the realised reopen return of a row whose label is invalid
for BLACKOUT alone and NaN everywhere else).

A summary JSON is written to data/features/features_summary.json: rows,
valid fraction per family, registry hash, and the data-quality facts a
researcher needs BEFORE trusting a row — ``crossed_frac`` (share of
emissions whose merged book is crossed, i.e. a stale LP quote),
``mean_row_gap_ns`` / ``median_sample_gap_ns``, the pinned
``label_max_age_ns`` freshness bound, ``label_valid_frac_by_horizon``,
``label_zero_frac_by_horizon`` (a 500 ms FX label that is 0 in 99 % of rows
is a sampling artefact, not evidence) and the invalid-label reason counts.

Native backend (``--engine rust``, v1.11.0): the 45 native slots (the
pinned native 40 + 5 alpha inputs, :mod:`iap.features.native`) of every row
are taken from the Rust engine (``iap_features_rs``, pyo3) replaying the same
file at the same cadence; the other 160 features stay Python.  The emitted
rows must line up exactly (instrument + timestamp) or that file keeps the
Python values (warned).  Without the extension the flag falls back to
``python``.  The Python engine still runs for the other families, so this is
a cross-checked swap of the native columns, not a pipeline speed-up.

Everything is deterministic: sorted file order, sorted instrument iteration,
event-time only.

Parallel build (``--workers N``, v1.9.0): the expensive part -- replaying
each day's events through a fresh FeatureEngine -- runs one process per
input file.  The only cross-day state is the expanding per-instrument
SessionProfile, which feeds exactly the four ``norm_*_m5_v1`` features and is
itself fed by profile-independent metrics.  Each worker therefore replays its
day against an empty profile and records, per emitted row, the 5-minute
bucket and the four raw metric values; the parent then walks the days in
order, folding those observations into the carried profiles and rewriting the
four norm values + validity bits exactly as the serial engine would have.
Labels, statistics and parquet writes stay serial and in file order, so the
output is byte-identical to ``--workers 1`` (tested).  The CLI clamps N to
the number of files, the CPU count and ``physical RAM / --worker-mem-gb``
(default 5); on a 16 GB machine use 2-3 workers for real data.

Memory (v1.13.0, E5): events are streamed from the file, never held as a
list; the label mid series, row timestamps and validity are typed arrays;
the float64 row values are spooled to ``<out-dir>/.spool_<pid>`` and read
back one instrument at a time when its row group is written (see
:class:`_InstrumentBuffer`).  Output bytes are unchanged.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import sys
import tempfile
import time
from array import array
from concurrent.futures import ProcessPoolExecutor
from math import isfinite
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from iap.core.codec import iter_iap1, iter_jsonl
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine, iter_group_ends
from iap.features.registry import build_registry, registry_hash, write_registry
from iap.features.rolling import SessionProfile
from iap.features.spec import FAMILY_ORDER
from iap.features.timeofday import PROFILE_METRICS, _metric_value
from iap.labels.labels import (
    FEATURE_STORE_VERSION,
    FRESHNESS_MODES,
    HORIZON_ORDER,
    LABEL_MAX_AGE_FLOOR_NS,
    STORE_FRESHNESS,
    CompactMidSeries,
    LabelReason,
    compute_labels,
    max_sample_age,
)

_REPO = Path(__file__).resolve().parents[4]
_NAN = float("nan")


#: Rows of feature values buffered in RAM before they are appended to the
#: instrument's spool file (v1.13, E5).
_SPOOL_ROWS = 2048


class _InstrumentBuffer:
    """Accumulates emitted rows for one instrument within one input file.

    Memory layout (v1.13, E5): the timestamps live in an ``array('q')``, the
    packed validity in a ``bytearray`` and the mid series in a
    :class:`CompactMidSeries`; the float64 feature values -- by far the
    largest part of a day (rows x features x 8 bytes) -- are appended to a
    per-(file, instrument) spool file on disk in blocks of ``_SPOOL_ROWS``
    rows and read back, one instrument at a time, only when that
    instrument's row group is written.  Value rewrites that used to happen in
    place (the Rust native overlay, the parallel build's norm_* patch) are
    recorded as small per-column arrays and applied at flush time, in the
    same order as before (overlay first, then norms).  The buffer pickles
    (parallel workers return it) because the spool is a named file.
    """

    __slots__ = (
        "ts",
        "bits",
        "series",
        "last_event_ts",
        "last_refresh_seq",
        "spool_path",
        "_pending",
        "_fh",
        "rust_cols",
        "rust_vals",
        "norm_cols",
        "norm_vals",
    )

    def __init__(self, spool_path: str | None = None) -> None:
        self.ts = array("q")
        self.bits = bytearray()
        self.series = CompactMidSeries()
        self.last_event_ts = 0
        self.last_refresh_seq = 0
        self.spool_path = spool_path
        self._pending = array("d")
        self._fh = None
        self.rust_cols: list[int] | None = None
        self.rust_vals: np.ndarray | None = None
        self.norm_cols: list[int] | None = None
        self.norm_vals: np.ndarray | None = None

    def add_values(self, values, nfeat: int) -> None:
        pending = self._pending
        pending.extend(values)
        if len(pending) >= _SPOOL_ROWS * nfeat:
            self._spill()

    def _spill(self) -> None:
        if self._fh is None:
            self._fh = open(self.spool_path, "wb")  # noqa: SIM115 - closed in close()
        self._pending.tofile(self._fh)
        del self._pending[:]

    def close(self) -> None:
        """Finish writing: everything is on disk, no file handle kept."""
        if self._pending or self._fh is None:
            self._spill()
        self._fh.close()
        self._fh = None

    def load_values(self, nfeat: int) -> np.ndarray:
        """The (rows, nfeat) float64 values with the deferred rewrites applied;
        the spool file is deleted."""
        rows = len(self.ts)
        vals = np.fromfile(self.spool_path, dtype=np.float64).reshape(rows, nfeat)
        os.unlink(self.spool_path)
        if self.rust_cols is not None:
            vals[:, self.rust_cols] = self.rust_vals
        if self.norm_cols is not None:
            vals[:, self.norm_cols] = self.norm_vals
        return vals

    def __getstate__(self):
        return {k: getattr(self, k) for k in self.__slots__ if k != "_fh"}

    def __setstate__(self, state) -> None:
        for k, v in state.items():
            setattr(self, k, v)
        self._fh = None


class _ProfileLog:
    """Per-row profile observations recorded by a parallel worker.

    ``curs`` holds the PROFILE_METRICS values row-major as float64 and
    ``have`` one byte per value (0 = the metric was not computable, i.e.
    ``None``) -- typed arrays instead of a tuple of Python objects per row.
    """

    __slots__ = ("buckets", "curs", "have")

    def __init__(self) -> None:
        self.buckets = array("q")
        self.curs = array("d")
        self.have = bytearray()


def _overlay_rust(
    path: Path, contexts, cadence_ns: int, buffers, nfeat: int, coalesce: bool = True
) -> bool:
    """Replace the native slots of ``buffers`` with the Rust engine's output
    for the same file. Returns False (buffers untouched) if rows differ.

    The validity bits are rewritten now; the values are recorded on the
    buffer (``rust_cols`` / ``rust_vals``) and applied when it is flushed."""
    from iap.features.native import NATIVE_NAMES, compute_native_file
    from iap.features.registry import feature_index

    frame = compute_native_file(path, contexts, cadence_ns, engine="rust", coalesce=coalesce)
    idx = feature_index()
    cols = [idx[n] for n in NATIVE_NAMES]
    nbytes = (nfeat + 7) // 8
    plan = []
    for iid in sorted(buffers):
        buf = buffers[iid]
        rows = np.flatnonzero(frame.instrument_id == iid)
        if len(rows) != len(buf.ts) or not np.array_equal(
            frame.timestamp[rows], np.frombuffer(buf.ts, dtype=np.int64)
        ):
            print(
                f"{path.name}: rust rows differ for instrument {iid}; keeping python values",
                file=sys.stderr,
            )
            return False
        plan.append((buf, rows))
    for buf, rows in plan:
        bits = np.frombuffer(buf.bits, dtype=np.uint8).reshape(len(rows), nbytes)
        rv = frame.values[rows]
        ok = frame.validity[rows]
        buf.rust_cols = cols
        buf.rust_vals = np.where(ok, rv, _NAN).astype(np.float64)
        for k, j in enumerate(cols):
            bit = np.uint8(1 << (j & 7))
            byte = bits[:, j >> 3]
            bits[:, j >> 3] = np.where(ok[:, k], byte | bit, byte & ~bit)
    return True


def _make_engine(contexts, cadence_ns: int, profiles, opts):
    """The default FeatureEngine, or the opt-in extended / event-time engine."""
    if opts is None:
        return FeatureEngine(contexts, cadence_ns=cadence_ns, profiles=profiles)
    from iap.features.extended import ExtendedFeatureEngine

    feature_set, sampling, sample_n = opts
    return ExtendedFeatureEngine(
        contexts,
        cadence_ns=cadence_ns,
        profiles=profiles,
        feature_set=feature_set,
        sampling=sampling,
        sample_n=sample_n,
    )


#: ``--cross-prints`` modes (D-P2-6); the default folds them.
CROSS_PRINT_MODES = ("fold", "omit")


def cross_prints_for(path: Path, mode: str = "fold") -> list[tuple[int, int, int]]:
    """``(exchange_ts, instrument_id, shares)`` of the side-less cross prints
    (ITCH ``Q``: opening / closing / halt / IPO crosses) of the session of
    one normalized file, from the ingested dataset's manifest
    (``<path>/../../dataset.json``), sorted by time; empty for ``omit``,
    for a synthetic dataset (no manifest) and for a file of no ingested
    session.  Zero-share and zero-price crosses are not prints."""
    if mode == "omit":
        return []
    root = Path(path).resolve().parent.parent
    if not (root / "dataset.json").is_file():
        return []
    from iap.marketdata.ingest import load_manifest

    manifest = load_manifest(root)  # refuses a pre-v1.14 dataset (D-P1-4)
    stem = Path(path).name.split(".")[0]  # eq_YYYYMMDD
    digits = stem.rpartition("_")[2]
    if len(digits) != 8 or not digits.isdigit():
        return []
    session = manifest["sessions"].get(f"{digits[:4]}-{digits[4:6]}-{digits[6:]}")
    if session is None:
        return []
    ids = {u["symbol"]: int(u["instrument_id"]) for u in manifest["universe"]}
    out = [
        (int(c["exchange_ts"]), ids[symbol], int(c["shares"]))
        for symbol, per in session["per_symbol"].items()
        for c in per.get("crosses", [])
        if c.get("shares", 0) > 0 and c.get("price_e4", 0) > 0
    ]
    return sorted(out)


def _replay_file(
    path: Path,
    contexts,
    cadence_ns: int,
    profiles,
    record: bool,
    engine: str = "python",
    opts=None,
    spool_dir: Path | None = None,
    cross: str = "fold",
    same_ts: str = "coalesce",
):
    """Replay one input file through a fresh engine.

    Returns ``(n_events, (events_processed, vectors_emitted), buffers, logs)``;
    ``logs`` is ``{iid: _ProfileLog}`` when ``record`` (parallel mode).  The
    events are streamed from the file (never held as a list) and the row
    values are spooled under ``spool_dir`` (default: ``iap_feature_spool`` in the
    temp directory), see :class:`_InstrumentBuffer`.
    """
    backend = engine
    engine = _make_engine(contexts, cadence_ns, profiles, opts)
    nfeat = len(engine.feature_names)
    if spool_dir is None:
        spool_dir = Path(tempfile.gettempdir()) / "iap_feature_spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{os.getpid()}_{path.name}"
    buffers: dict[int, _InstrumentBuffer] = {}
    logs: dict[int, _ProfileLog] = {}
    n_events = 0
    nan = _NAN
    # v1.14 (D-P1-1): one refresh / sample / row per (instrument, exchange_ts)
    stream = _iter_events(path)
    pairs = iter_group_ends(stream) if same_ts == "coalesce" else ((ev, True) for ev in stream)
    # v1.14 (D-P2-6): side-less cross prints fold into volume at their time
    crosses = cross_prints_for(path, cross)
    ci = 0
    for ev, end in pairs:
        n_events += 1
        while ci < len(crosses) and crosses[ci][0] < ev.exchange_ts:
            ts, cid, qty = crosses[ci]
            if cid in contexts:
                engine.on_cross_print(cid, ts, qty)
            ci += 1
        vec = engine.apply(ev, end)
        iid = ev.instrument_id
        buf = buffers.get(iid)
        if buf is None:
            buf = buffers[iid] = _InstrumentBuffer(str(spool_dir / f"{stem}_{iid}.f64"))
        buf.last_event_ts = ev.exchange_ts
        st = engine.states[iid]
        # One mid sample per BOOK REFRESH (API_FEATURES §6): a refresh
        # that left the merged view one-sided / stale / halted is
        # recorded as a NON-TRADABLE sample so labels cannot span it.
        if st.refresh_seq != buf.last_refresh_seq:
            buf.last_refresh_seq = st.refresh_seq
            if st.label_tradable:
                buf.series.append(ev.exchange_ts, st.mid, st.spread_ticks * st.tick / 2.0, True)
            else:
                buf.series.append(ev.exchange_ts, nan, nan, False)
        if vec is not None:
            buf.ts.append(vec.timestamp)
            buf.add_values(vec.values, nfeat)
            buf.bits.extend(vec.validity_bits())
            if record:
                # _emit is the last thing apply() does, so the state is still
                # the emission state: these are exactly the values the engine
                # folded into the (here empty) profile
                t = vec.timestamp
                log = logs.get(iid)
                if log is None:
                    log = logs[iid] = _ProfileLog()
                log.buckets.append(SessionProfile.bucket_of(t, st.ctx.clock.offset_seconds(t)))
                for m in PROFILE_METRICS:
                    cur = _metric_value(st, m)
                    if cur is None:
                        log.curs.append(nan)
                        log.have.append(0)
                    else:
                        log.curs.append(cur)
                        log.have.append(1)
    for buf in buffers.values():
        if buf.ts:
            buf.close()
    for ts, cid, qty in crosses[ci:]:  # after the last event (closing cross)
        if cid in contexts:
            engine.on_cross_print(cid, ts, qty)
    counts = (engine.events_processed, engine.vectors_emitted, engine.cross_prints_folded)
    if backend == "rust":
        _overlay_rust(path, contexts, cadence_ns, buffers, nfeat, same_ts == "coalesce")
    return n_events, counts, buffers, logs


def _worker(job):
    """Process-pool entry point: replay one day against an empty profile."""
    path, configs, cadence_ns, backend, opts, spool_dir, cross, same_ts = job
    return _replay_file(
        Path(path),
        build_contexts(configs),
        cadence_ns,
        {},
        record=True,
        engine=backend,
        opts=opts,
        spool_dir=Path(spool_dir),
        cross=cross,
        same_ts=same_ts,
    )


def _patch_norms(
    buffers: dict[int, _InstrumentBuffer],
    logs: dict[int, _ProfileLog],
    profiles: dict,
    norm_idx: list[int],
    nfeat: int,
) -> None:
    """Rewrite one worker day's norm_*_m5_v1 values/bits against the carried
    profiles, folding the day's observations in serial order (mirrors
    timeofday.compute + FeatureEngine._emit + _famutil.put exactly).  The
    bits are patched now, the values are recorded on the buffer
    (``norm_cols`` / ``norm_vals``) and applied when it is flushed."""
    nbytes = (nfeat + 7) // 8
    nm = len(PROFILE_METRICS)
    for iid in sorted(logs):
        log = logs[iid]
        buf = buffers[iid]
        prof = profiles.get(iid)
        if prof is None:
            prof = profiles[iid] = SessionProfile(PROFILE_METRICS)
        rows = len(log.buckets)
        out = np.empty((rows, nm), dtype=np.float64)
        bits = buf.bits
        curs = log.curs
        have = log.have
        for r in range(rows):
            bucket = log.buckets[r]
            base = r * nm
            for q, (m, j) in enumerate(zip(PROFILE_METRICS, norm_idx, strict=True)):
                v = None
                if have[base + q]:
                    n, mean = prof.prior(m, bucket)
                    if n >= SessionProfile.MIN_OBS and mean > 0.0:
                        v = curs[base + q] / mean
                ok = False
                if v is not None:
                    fv = float(v)
                    if isfinite(fv):
                        out[r, q] = fv
                        ok = True
                if not ok:
                    out[r, q] = _NAN
                k = r * nbytes + (j >> 3)
                bit = 1 << (j & 7)
                if ok:
                    bits[k] |= bit
                else:
                    bits[k] &= ~bit & 0xFF
            for q, m in enumerate(PROFILE_METRICS):
                if have[base + q]:
                    prof.update(m, bucket, curs[base + q])
        buf.norm_cols = list(norm_idx)
        buf.norm_vals = out


def _total_ram_bytes() -> int | None:
    """Physical RAM in bytes, or None when it cannot be determined."""
    try:
        if sys.platform == "win32":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            ms = _MemStatus()
            ms.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                return int(ms.ullTotalPhys)
            return None
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return None


def effective_workers(requested: int, n_files: int, worker_mem_gb: float) -> int:
    """Clamp a requested worker count to the number of files, the CPU count
    and ``physical RAM / worker_mem_gb`` (``requested <= 0`` = auto: the
    largest count the clamps allow; ``worker_mem_gb <= 0`` = no RAM cap)."""
    if requested == 1:
        return 1
    cap = min(max(1, n_files), os.cpu_count() or 1)
    ram = _total_ram_bytes()
    if ram is not None and worker_mem_gb > 0:
        cap = min(cap, max(1, int(ram // int(worker_mem_gb * 2**30))))
    return cap if requested <= 0 else max(1, min(requested, cap))


def _iter_events(path: Path):
    """Stream one input file's events in file order (never a full list)."""
    if path.suffix == ".iap1":
        return iter_iap1(path)
    return iter_jsonl(path)


def _flush_file(
    writers: dict[int, pq.ParquetWriter],
    out_dir: Path,
    schema: pa.Schema,
    buffers: dict[int, _InstrumentBuffer],
    names: list[str],
    fam_valid: dict[int, np.ndarray],
    row_counts: dict[int, int],
    label_stats: dict[int, dict],
    label_freshness: str = STORE_FRESHNESS,
) -> None:
    """Write one row group per instrument for the just-processed file."""
    nfeat = len(names)
    nbytes = (nfeat + 7) // 8
    for iid in sorted(buffers):
        buf = buffers.pop(iid)  # one instrument's rows in RAM at a time
        rows = len(buf.ts)
        if rows == 0:
            continue
        vals = buf.load_values(nfeat)
        packed = np.frombuffer(buf.bits, dtype=np.uint8).reshape(rows, nbytes)
        validity = np.unpackbits(packed, axis=1, bitorder="little")[:, :nfeat]
        fam_valid.setdefault(iid, np.zeros(nfeat, dtype=np.int64))
        fam_valid[iid] += validity.sum(axis=0, dtype=np.int64)
        row_counts[iid] = row_counts.get(iid, 0) + rows

        if label_freshness == "trailing":
            # causal bound (R5): each forward sample judged against the
            # trailing median gap; the summary keeps the floor it starts from
            max_age = LABEL_MAX_AGE_FLOOR_NS
            labels = compute_labels(buf.ts, buf.series, buf.last_event_ts, freshness="trailing")
        else:
            max_age = max_sample_age(buf.series)
            labels = compute_labels(buf.ts, buf.series, buf.last_event_ts, max_age_ns=max_age)
        # Data-quality facts a researcher must see before trusting a row
        # (RESEARCH round-3): how many emissions came from a CROSSED merged
        # book (a stale LP quote makes spread_ticks < 0), how far apart the
        # rows are, and how many labels are exact zeros / why they are
        # invalid.
        ts_arr = np.frombuffer(buf.ts, dtype=np.int64)
        gaps = np.diff(ts_arr)
        spread = vals[:, names.index("spread_ticks_v1")]
        spread_ok = validity[:, names.index("spread_ticks_v1")].astype(bool)
        stats = label_stats.setdefault(
            iid,
            {
                "rows": 0,
                "crossed_rows": 0,
                "spread_valid_rows": 0,
                "gap_sum_ns": 0,
                "gap_count": 0,
                "median_sample_gap_ns": 0,
                "label_max_age_ns": 0,
                "series_samples": 0,
                "tradable_samples": 0,
                "by_horizon": {h: {"valid": 0, "zero": 0, "reason": {}} for h in HORIZON_ORDER},
            },
        )
        stats["rows"] += rows
        stats["spread_valid_rows"] += int(spread_ok.sum())
        stats["crossed_rows"] += int(((spread < 0) & spread_ok).sum())
        stats["gap_sum_ns"] += int(gaps.sum()) if gaps.size else 0
        stats["gap_count"] += int(gaps.size)
        stats["median_sample_gap_ns"] = buf.series.median_gap_ns()
        stats["label_max_age_ns"] = max_age
        stats["series_samples"] += len(buf.series)
        stats["tradable_samples"] += int(
            np.count_nonzero(np.frombuffer(buf.series.tradable, np.uint8))
        )
        for h in HORIZON_ORDER:
            lab = labels[h]
            hv = stats["by_horizon"][h]
            valid_mask = np.asarray(lab.valid, dtype=bool)
            hv["valid"] += int(valid_mask.sum())
            lm = np.asarray(lab.mid, dtype=float)
            hv["zero"] += int(((lm == 0.0) & valid_mask).sum())
            for r in lab.reason:
                if r:
                    for nm in LabelReason.describe(r):
                        hv["reason"][nm] = hv["reason"].get(nm, 0) + 1

        cols: dict[str, pa.Array] = {
            "instrument_id": pa.array(np.full(rows, iid, dtype=np.uint32), type=pa.uint32()),
            "exchange_ts": pa.array(ts_arr, type=pa.int64()),
        }
        for j, name in enumerate(names):
            cols[name] = pa.array(vals[:, j], type=pa.float64())
        cols["validity_bits"] = pa.FixedSizeBinaryArray.from_buffers(
            pa.binary(nbytes), rows, [None, pa.py_buffer(buf.bits)]
        )
        for h in HORIZON_ORDER:
            lab = labels[h]
            cols[f"label_mid_{h}"] = pa.array(lab.mid, type=pa.float64())
            cols[f"label_cost_{h}"] = pa.array(lab.cost, type=pa.float64())
            cols[f"label_valid_{h}"] = pa.array(lab.valid, type=pa.bool_())
            cols[f"label_reason_{h}"] = pa.array(lab.reason, type=pa.uint8())
            cols[f"label_reopen_{h}"] = pa.array(lab.reopen_mid, type=pa.float64())
        table = pa.table(cols, schema=schema)
        writer = writers.get(iid)
        if writer is None:
            writer = pq.ParquetWriter(
                out_dir / f"features_{iid}.parquet", schema, compression="zstd"
            )
            writers[iid] = writer
        writer.write_table(table)


def _build_schema(names: list[str]) -> pa.Schema:
    nbytes = (len(names) + 7) // 8
    fields = [
        pa.field("instrument_id", pa.uint32()),
        pa.field("exchange_ts", pa.int64()),
    ]
    fields += [pa.field(n, pa.float64()) for n in names]
    fields.append(pa.field("validity_bits", pa.binary(nbytes)))
    for h in HORIZON_ORDER:
        fields.append(pa.field(f"label_mid_{h}", pa.float64()))
        fields.append(pa.field(f"label_cost_{h}", pa.float64()))
        fields.append(pa.field(f"label_valid_{h}", pa.bool_()))
        fields.append(pa.field(f"label_reason_{h}", pa.uint8()))
        fields.append(pa.field(f"label_reopen_{h}", pa.float64()))
    return pa.schema(fields)


def _build_days(
    files,
    workers,
    spool,
    contexts,
    cadence_ns,
    profiles,
    backend,
    opts,
    names,
    configs,
    consume,
    cross: str = "fold",
    same_ts: str = "coalesce",
) -> None:
    """Replay every day (serially, or ``workers`` processes) and hand each
    day's buffers to ``consume`` in file (= trading-day) order."""
    if workers <= 1:
        for path in files:
            n_events, counts, buffers, _ = _replay_file(
                path,
                contexts,
                cadence_ns,
                profiles,
                record=False,
                engine=backend,
                opts=opts,
                spool_dir=spool,
                cross=cross,
                same_ts=same_ts,
            )
            consume(path, n_events, counts, buffers)
        return
    norm_idx = [names.index(f"norm_{m}_m5_v1") for m in PROFILE_METRICS]
    jobs = [
        (str(p), str(configs), cadence_ns, backend, opts, str(spool), cross, same_ts) for p in files
    ]
    # Under `python -m iap.features` this file runs as `__main__`, and a
    # spawn-start worker (Windows, macOS) does not re-import a package's
    # `__main__`, so `__main__._worker` cannot be unpickled there. Submit
    # the function from the importable module name instead.
    worker = importlib.import_module("iap.features.__main__")._worker
    with ProcessPoolExecutor(max_workers=workers) as pool:
        # map() yields in submission (= trading-day) order, so the
        # profile carry, labels and parquet row groups stay serial
        for path, (n_events, counts, buffers, logs) in zip(
            files, pool.map(worker, jobs), strict=True
        ):
            _patch_norms(buffers, logs, profiles, norm_idx, len(names))
            consume(path, n_events, counts, buffers)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m iap.features", description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=str(_REPO / "data" / "normalized"))
    ap.add_argument("--out-dir", default=str(_REPO / "data" / "features"))
    ap.add_argument("--configs", default=str(_REPO / "configs"))
    ap.add_argument(
        "--registry-out", default=str(_REPO / "data" / "reference" / "feature_registry.json")
    )
    ap.add_argument(
        "--cadence-ms",
        type=int,
        default=100,
        help="emission cadence per instrument (0 = every event)",
    )
    ap.add_argument(
        "--files", nargs="*", default=None, help="specific input file names inside --data-dir"
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=1,
        help="processes replaying days in parallel (1 = serial, 0 = auto); "
        "output is byte-identical to the serial build",
    )
    ap.add_argument(
        "--worker-mem-gb",
        type=float,
        default=5.0,
        help="per-worker memory budget that caps --workers by physical RAM (0 = no cap)",
    )
    ap.add_argument(
        "--label-freshness",
        choices=FRESHNESS_MODES,
        default=STORE_FRESHNESS,
        help="label freshness bound: trailing (default since v1.14; causal, R5: trailing "
        "median quote gap) or whole_day (legacy, the stores published before v1.14)",
    )
    ap.add_argument(
        "--same-ts",
        choices=("coalesce", "per_event"),
        default="coalesce",
        help="coalesce (default since v1.14, D-P1-1): one refresh / sample / row per "
        "(instrument, exchange_ts); per_event: the v1.13 semantics, a refresh and a row "
        "after every event (reproduces the stores published before v1.14)",
    )
    ap.add_argument(
        "--cross-prints",
        choices=CROSS_PRINT_MODES,
        default="fold",
        help="ingested datasets: fold the side-less opening / closing / halt cross prints "
        "of dataset.json into volume, participation, the volume profile and the volume "
        "clock, never into signed flow (fold, default since v1.14) or leave them out (omit)",
    )
    ap.add_argument(
        "--engine",
        choices=("python", "rust"),
        default="python",
        help="backend for the 45 native feature slots: python (default) or rust "
        "(iap_features_rs, falls back to python when not installed)",
    )
    ap.add_argument(
        "--feature-set",
        choices=("default", "extended"),
        default="default",
        help="default (the pinned registry) or extended (+ the opt-in v1.12 "
        "Python-only families, appended; own feature_version)",
    )
    ap.add_argument(
        "--sampling",
        default="clock",
        help="row clock: clock (--cadence-ms, default), events:N (every N applied "
        "events) or volume:N (every N traded shares)",
    )
    args = ap.parse_args(argv)
    sampling, _, sample_n = args.sampling.partition(":")
    if sampling == "clock" and not sample_n:
        sample_n_i = 0
    elif sampling in ("events", "volume") and sample_n.isdigit() and int(sample_n) > 0:
        sample_n_i = int(sample_n)
    else:
        ap.error("--sampling must be clock, events:N or volume:N (N > 0)")
    opts = None
    if args.feature_set != "default" or sampling != "clock":
        opts = (args.feature_set, sampling, sample_n_i)
    from iap.features.native import resolve_engine

    backend = resolve_engine(args.engine)

    t_start = time.time()
    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.files:
        files = [data_dir / f for f in sorted(args.files)]
    else:
        files = sorted(data_dir.glob("*.normalized.iap1"))
        if not files:
            files = sorted(data_dir.glob("*.normalized.jsonl"))
    if not files:
        raise SystemExit(f"no normalized input files in {data_dir}")

    contexts = build_contexts(args.configs)
    if args.feature_set == "extended":
        from iap.features.extended import (
            EXT_FAMILY_ORDER,
            extended_registry,
            extended_registry_hash,
            write_extended_registry,
        )

        write_extended_registry(args.registry_out)
        registry = extended_registry()
        family_order = FAMILY_ORDER + EXT_FAMILY_ORDER
        reg_hash = extended_registry_hash()
    else:
        write_registry(args.registry_out)
        registry = build_registry()
        family_order = FAMILY_ORDER
        reg_hash = registry_hash()
    names = [s.name for s in registry]
    schema = _build_schema(names)

    writers: dict[int, pq.ParquetWriter] = {}
    profiles: dict[int, SessionProfile] = {}
    fam_valid: dict[int, np.ndarray] = {}
    row_counts: dict[int, int] = {}
    label_stats: dict[int, dict] = {}
    total_events = 0
    total_vectors = 0
    total_crosses = 0

    cadence_ns = args.cadence_ms * 1_000_000
    workers = effective_workers(args.workers, len(files), args.worker_mem_gb)
    if workers != args.workers:
        print(f"--workers {args.workers} -> {workers} (files / CPUs / RAM cap)", file=sys.stderr)

    def _consume(path: Path, n_events: int, counts, buffers) -> None:
        nonlocal total_events, total_vectors, total_crosses
        total_events += counts[0]
        total_vectors += counts[1]
        total_crosses += counts[2]
        _flush_file(
            writers,
            out_dir,
            schema,
            buffers,
            names,
            fam_valid,
            row_counts,
            label_stats,
            args.label_freshness,
        )
        print(f"{path.name}: {n_events} events -> {counts[1]} vectors", file=sys.stderr)

    # row values are spooled here while a day is replayed (see
    # _InstrumentBuffer); removed when the build ends
    spool = out_dir / f".spool_{os.getpid()}"
    try:
        _build_days(
            files,
            workers,
            spool,
            contexts,
            cadence_ns,
            profiles,
            backend,
            opts,
            names,
            args.configs,
            _consume,
            cross=args.cross_prints,
            same_ts=args.same_ts,
        )
    finally:
        shutil.rmtree(spool, ignore_errors=True)

    for writer in writers.values():
        writer.close()

    fam_slices: dict[str, list[int]] = {f: [] for f in family_order}
    for i, s in enumerate(registry):
        fam_slices[s.family].append(i)
    per_instrument = {}
    for iid in sorted(row_counts):
        rows = row_counts[iid]
        counts = fam_valid[iid]
        st = label_stats.get(iid, {})
        by_h = st.get("by_horizon", {})
        gap_n = st.get("gap_count", 0)
        per_instrument[str(iid)] = {
            "symbol": contexts[iid].symbol,
            "rows": rows,
            "valid_fraction_by_family": {
                fam: round(float(counts[idx].sum()) / (rows * len(idx)), 6)
                for fam, idx in fam_slices.items()
            },
            # data-quality facts (see the module docstring)
            "crossed_frac": (
                round(st["crossed_rows"] / st["spread_valid_rows"], 6)
                if st.get("spread_valid_rows")
                else None
            ),
            "mean_row_gap_ns": (int(st["gap_sum_ns"] / gap_n) if gap_n else None),
            "median_sample_gap_ns": st.get("median_sample_gap_ns"),
            "label_max_age_ns": st.get("label_max_age_ns"),
            "tradable_sample_frac": (
                round(st["tradable_samples"] / st["series_samples"], 6)
                if st.get("series_samples")
                else None
            ),
            "label_valid_frac_by_horizon": {h: round(by_h[h]["valid"] / rows, 6) for h in by_h},
            "label_zero_frac_by_horizon": {
                h: (round(by_h[h]["zero"] / by_h[h]["valid"], 6) if by_h[h]["valid"] else None)
                for h in by_h
            },
            "label_invalid_reasons_by_horizon": {
                h: dict(sorted(by_h[h]["reason"].items())) for h in by_h
            },
        }
    summary = {
        "store_version": FEATURE_STORE_VERSION,
        "label_freshness": args.label_freshness,
        "same_ts": args.same_ts,
        "registry_hash": reg_hash,
        "registered_features": len(registry),
        "cadence_ms": args.cadence_ms,
        "label_horizons": list(HORIZON_ORDER),
        "files": [f.name for f in files],
        "events_processed": total_events,
        "vectors_emitted": total_vectors,
        "runtime_seconds": round(time.time() - t_start, 2),
        "instruments": per_instrument,
    }
    if args.feature_set != "default":  # keys absent on the default path
        summary["feature_set"] = args.feature_set
    if sampling != "clock":
        summary["sampling"] = args.sampling
    if backend != "python":  # key absent on the default path: summary bytes unchanged
        summary["native_engine"] = backend
    if total_crosses or args.cross_prints != "fold":  # absent on synthetic data
        summary["cross_prints"] = {"mode": args.cross_prints, "folded": total_crosses}
    with open(out_dir / "features_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
    print(
        json.dumps({k: v for k, v in summary.items() if k != "instruments"}, indent=2),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
