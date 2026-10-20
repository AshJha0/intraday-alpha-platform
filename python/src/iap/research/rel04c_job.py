"""REL-04c job: single-venue execution universe on the three full-market days.

Stages, each a separate process launched by
``research/execution/rel04c/run_rel04c.cmd`` (so each logs its own peak RSS),
each resumable from a ``_DONE_<stage>`` marker beside the dataset::

    verify   sha256 + size of the three ITCH files against the pinned values
    select   one pass over 2019-10-18 + directory prefixes of the later days
             -> research/execution/rel04c/universe.json (rel04c_universe/v1)
    ingest   --day D, in date order, into ONE multi-session dataset
             (--defer-normalize for the first two; the third normalises all,
             --partition-instruments auto from available RAM, --slim)
    register tools/data_manifest.py generate + holdout_registry seed, then the
             holdout guard must say in_sample for the dataset
    volume   Nasdaq per-symbol session volume (dataset.json) vs the
             owner-supplied Yahoo Finance consolidated daily volume; writes the
             ratio report and consolidated_adv.json for the benchmark
    bench    --bucket L1|L2|L3: benchmark v2 (REL-03) on the bucket's names, a
             pre-declared SECONDARY result; refuses while the pre-declaration
             has an open parameter or is not frozen
    cleanup  delete the dataset's raw/ intermediate (the normalised events, the
             dataset.json and the benchmark outputs are kept; the ITCH inputs
             are never touched)

Nothing here downloads anything.  Every refusal exits 2 with one line saying
why; a stage that fails leaves no marker, so a re-launch redoes it.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from iap.research import rel04c_universe as ru

REPO = Path(__file__).resolve().parents[4]
WORK_REL = "research/execution/rel04c"
DATASET_NAME = "r7x20_3day"

#: the three full-market days on disk (data/real/itch/r7); hashes as recorded in
#: the inputs of r7_20191018 / r7_20210713 / r7_20210813 dataset.json
DAYS: tuple[dict[str, Any], ...] = (
    {
        "date": "2019-10-18",
        "file": "S101819-v50.txt.gz",
        "bytes": 3951201663,
        "sha256": "390acb4df5a01d3d8e6c34557858604ac7c655c8edc0a9e312da3c032c5d8da4",
    },
    {
        "date": "2021-07-13",
        "file": "S071321-v50.txt.gz",
        "bytes": 5996745270,
        "sha256": "b39b72621a8bc3ff92601701e0e9bd93afbdf30fea2fd68cdbe83c6b0bfb8ccd",
    },
    {
        "date": "2021-08-13",
        "file": "S081321-v50.txt.gz",
        "bytes": 4889328604,
        "sha256": "64f4f1b076164ee72971d87fb548ae73cb924e2e45a73de059cc77328bcde4e7",
    },
)
STAGES = ("verify", "select", "ingest", "register", "volume", "bench", "cleanup")
BUCKETS = tuple(b[0] for b in ru.LIQ_BUCKETS)
#: Nasdaq-only share of consolidated volume outside this band is flagged
RATIO_BAND = (0.05, 0.60)
#: most names the owner may list in ``_unavailable`` of yahoo_volume.json
MAX_UNAVAILABLE = 6
#: pre-declaration fields that must be set (non-null) before ``bench`` runs
REQUIRED_PARAMS = (
    "algos",
    "policies",
    "sizes_pct_venue_adv",
    "sizes_target_pct_consolidated_adv",
    "window_min",
    "mde_report",
)


class JobRefused(RuntimeError):
    """A precondition of a stage is not met; nothing was written."""


class Paths:
    def __init__(self, data_root: Path, work: Path) -> None:
        self.data_root = Path(data_root)
        self.inputs = self.data_root / "itch" / "r7"
        self.dataset = self.data_root / "rel04c" / DATASET_NAME
        self.markers = self.data_root / "rel04c" / f"{DATASET_NAME}.markers"
        self.log = self.data_root / "rel04c" / "rel04c_job.jsonl"
        self.work = Path(work)
        self.universe = self.work / "universe.json"
        self.predecl = self.work / "predeclaration.json"
        self.yahoo = self.work / "yahoo_volume.json"
        self.volume_report = self.work / "volume_check.json"
        self.consolidated_adv = self.work / "consolidated_adv.json"
        self.bench_out = self.work / "benchmark"

    def marker(self, stage: str) -> Path:
        return self.markers / f"_DONE_{stage}"

    def done(self, stage: str) -> bool:
        return self.marker(stage).is_file()

    def mark(self, stage: str) -> None:
        self.markers.mkdir(parents=True, exist_ok=True)
        self.marker(stage).write_text(_now(), encoding="ascii")


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _log(paths: Paths, rec: Mapping[str, Any]) -> None:
    paths.log.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _now(), **rec}, sort_keys=True) + "\n")
    print(json.dumps(rec, sort_keys=True), flush=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _require(paths: Paths, *stages: str) -> None:
    for s in stages:
        if not paths.done(s):
            raise JobRefused(f"stage {s!r} has not completed (no marker {paths.marker(s)})")


def _load_universe(paths: Paths) -> dict[str, Any]:
    if not paths.universe.is_file():
        raise JobRefused(f"{paths.universe} missing: run the select stage")
    doc = json.loads(paths.universe.read_text(encoding="utf-8"))
    if doc.get("rule_version") != ru.RULE_VERSION:
        raise JobRefused(f"universe rule {doc.get('rule_version')!r} != {ru.RULE_VERSION!r}")
    syms = list(doc.get("symbols") or [])
    if not set(ru.ANCHORS) <= set(syms):
        raise JobRefused(f"universe lacks the anchors {list(ru.ANCHORS)}")
    if len(syms) != len(set(syms)) or len(syms) > 2 + 9 * ru.PER_CELL:
        raise JobRefused("universe has duplicates or more names than the rule allows")
    return doc


# ------------------------------------------------------------------ stages --
def stage_verify(paths: Paths, *, hasher: Callable[[Path], str] = _sha256) -> dict[str, Any]:
    out = []
    for d in DAYS:
        p = paths.inputs / d["file"]
        if not p.is_file():
            raise JobRefused(f"input {p} is missing (nothing is downloaded)")
        size = p.stat().st_size
        if size != d["bytes"]:
            raise JobRefused(f"{p.name}: {size} bytes, pinned {d['bytes']}")
        got = hasher(p)
        if got != d["sha256"]:
            raise JobRefused(f"{p.name}: sha256 {got} != pinned {d['sha256']}")
        out.append({"file": d["file"], "sha256": got})
    return {"inputs": out}


def stage_select(
    paths: Paths,
    *,
    measure: Callable[[Path], dict[str, Any]] = ru.measure_day,
    directory: Callable[[Path], set[str]] = ru.directory_symbols,
) -> dict[str, Any]:
    _require(paths, "verify")
    first, *later = DAYS
    measured = measure(paths.inputs / first["file"])
    present = {d["date"]: directory(paths.inputs / d["file"]) for d in later}
    doc = ru.select(
        measured,
        source_file=first["file"],
        source_sha256=first["sha256"],
        present_later=present,
    )
    paths.universe.parent.mkdir(parents=True, exist_ok=True)
    paths.universe.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"n_symbols": len(doc["symbols"]), "cells": doc["cells"]}


def _dataset_sessions(ds: Path) -> list[str]:
    man = ds / "dataset.json"
    if not man.is_file():
        return []
    return sorted(json.loads(man.read_text(encoding="utf-8")).get("sessions", {}))


def stage_ingest(
    paths: Paths,
    date: str,
    *,
    ingest_main: Callable[[list[str]], int] | None = None,
    ram_gb: Callable[[], float | None] | None = None,
) -> dict[str, Any]:
    from iap.marketdata.runmem import auto_partition_instruments, available_ram_gb

    _require(paths, "verify", "select")
    order = [d["date"] for d in DAYS]
    if date not in order:
        raise JobRefused(f"--day {date} is not one of {order}")
    k = order.index(date)
    for prev in order[:k]:
        if not paths.done(f"ingest_{prev}"):
            raise JobRefused(f"ingest {date} before {prev}: days run in date order")
    have = _dataset_sessions(paths.dataset)
    if have != order[:k]:
        # a crash mid-session leaves an unknown partial session: start over
        if paths.dataset.exists():
            shutil.rmtree(paths.dataset)
        for d in order:
            paths.marker(f"ingest_{d}").unlink(missing_ok=True)
        raise JobRefused(
            f"dataset held sessions {have}, expected {order[:k]}: dataset and ingest markers "
            "removed, re-launch to redo the ingest from the first day"
        )
    syms = _load_universe(paths)["symbols"]
    choice = auto_partition_instruments(len(syms), (ram_gb or available_ram_gb)())
    day = DAYS[k]
    argv = [
        "--format",
        "itch50",
        "--input",
        str(paths.inputs / day["file"]),
        "--date",
        date,
        "--symbols",
        ",".join(syms),
        "--out",
        str(paths.dataset),
        "--partition-instruments",
        str(int(choice["instruments"] or 1)),
        "--slim",
    ]
    if k < len(order) - 1:
        argv.append("--defer-normalize")
    if ingest_main is None:
        from iap.marketdata.ingest import main as _ingest

        ingest_main = _ingest
    paths.dataset.parent.mkdir(parents=True, exist_ok=True)
    rc = ingest_main(argv)
    if rc != 0:
        raise RuntimeError(f"ingest {date} exited {rc}")
    return {"date": date, "partition": choice, "deferred": k < len(order) - 1}


def stage_register(
    paths: Paths,
    *,
    manifest_main: Callable[[list[str]], int] | None = None,
    seed_main: Callable[[list[str]], int] | None = None,
    guard: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    _require(paths, *(f"ingest_{d['date']}" for d in DAYS))
    have = _dataset_sessions(paths.dataset)
    if have != [d["date"] for d in DAYS]:
        raise JobRefused(f"dataset sessions {have} != the three days")
    if manifest_main is None:
        sys.path.insert(0, str(REPO / "tools"))
        from data_manifest import main as _manifest  # type: ignore[import-not-found]

        manifest_main = _manifest
    if seed_main is None or guard is None:
        from iap.research import holdout_registry as hr

        seed_main = seed_main or hr.main
        guard = guard or hr.guard
    if manifest_main(["generate", "--data-root", str(paths.data_root)]) != 0:
        raise RuntimeError("data_manifest generate failed")
    if seed_main(["seed", "--data-root", str(paths.data_root)]) != 0:
        raise RuntimeError("holdout_registry seed failed")
    rec = guard(paths.dataset, purpose="rel04c-register")
    if rec.get("state") != "in_sample":
        raise JobRefused(f"holdout guard says {rec.get('state')!r}, not in_sample")
    return {"holdout_state": rec.get("state"), "dataset_version": rec.get("dataset_version")}


def nasdaq_volumes(ds: Path) -> dict[str, dict[str, int]]:
    """``{date: {symbol: shares}}`` from dataset.json ``per_symbol.volume``
    (regular-session Nasdaq prints incl. the opening / closing cross)."""
    doc = json.loads((ds / "dataset.json").read_text(encoding="utf-8"))
    return {
        d: {s: int(r["volume"]) for s, r in sess["per_symbol"].items()}
        for d, sess in doc["sessions"].items()
    }


def volume_report(
    nasdaq: Mapping[str, Mapping[str, int]], yahoo: Mapping[str, Any]
) -> dict[str, Any]:
    """Ratio Nasdaq / consolidated per (date, symbol), Yahoo split-undone.

    ``yahoo`` = ``{"_source": ..., "split_factor": {date: {sym: f}}, "volume":
    {date: {sym: shares}}}``; Yahoo's history is split-ADJUSTED (AAPL's 2019
    volume is shown x4 after the 2020 4:1 split, its 2021 volume x1), so each
    value is divided by the symbol's cumulative split factor from that date to
    the retrieval date, as the owner records it.  A null value refuses: the report is never computed from a guess."""
    if not yahoo.get("_source"):
        raise JobRefused("yahoo_volume.json has no _source (URL + retrieval date)")
    vol = yahoo.get("volume") or {}
    split = yahoo.get("split_factor") or {}
    # ``_unavailable``: {symbol: reason} for names Yahoo has no data for (delisted /
    # acquired); recorded as unavailable, no ratio, no consolidated ADV.  Anything
    # else missing still refuses.
    unavailable = yahoo.get("_unavailable") or {}
    if not isinstance(unavailable, Mapping):
        raise JobRefused("yahoo_volume.json _unavailable must be {symbol: reason}")
    for sym, reason in unavailable.items():
        if not isinstance(reason, str) or not reason.strip():
            raise JobRefused(f"yahoo_volume.json _unavailable[{sym}] has no reason")
    if len(unavailable) > MAX_UNAVAILABLE:
        raise JobRefused(
            f"{len(unavailable)} names listed unavailable, more than {MAX_UNAVAILABLE}"
        )
    unavailable_seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    flags: list[str] = []
    adv: dict[str, list[float]] = {}
    for date, by_sym in sorted(nasdaq.items()):
        for sym, n in sorted(by_sym.items()):
            if sym in unavailable:
                if any(sym in (vol.get(d) or {}) for d in vol):
                    raise JobRefused(f"{sym} is both listed unavailable and has a volume")
                unavailable_seen.add(sym)
                rows.append(
                    {
                        "date": date,
                        "symbol": sym,
                        "nasdaq": n,
                        "consolidated": None,
                        "ratio": None,
                        "status": "unavailable",
                        "reason": unavailable[sym],
                    }
                )
                continue
            raw = (vol.get(date) or {}).get(sym)
            f = (split.get(date) or {}).get(sym)
            if raw is None or f is None:
                raise JobRefused(f"yahoo volume or split factor for {sym} on {date} is missing")
            cons = float(raw) / float(f)
            ratio = n / cons if cons > 0 else None
            row = {"date": date, "symbol": sym, "nasdaq": n, "consolidated": cons, "ratio": ratio}
            if ratio is None or not (RATIO_BAND[0] <= ratio <= RATIO_BAND[1]):
                row["flag"] = "outside_band"
                flags.append(f"{sym}@{date}")
            rows.append(row)
            adv.setdefault(sym, []).append(cons)
    if set(unavailable) - unavailable_seen:
        raise JobRefused(
            f"_unavailable names not in the dataset: {sorted(set(unavailable) - unavailable_seen)}"
        )
    ratios = sorted(float(r["ratio"]) for r in rows if r["ratio"] is not None)
    return {
        "schema": "iap.rel04c_volume_check/v1",
        "source": yahoo["_source"],
        "band": list(RATIO_BAND),
        "rows": rows,
        "median_ratio": ratios[len(ratios) // 2] if ratios else None,
        "validated_symbols": sorted(adv),
        "unavailable": {k: unavailable[k] for k in sorted(unavailable)},
        "flagged": flags,
        "consolidated_adv": {s: sum(v) / len(v) for s, v in sorted(adv.items())},
        "note": (
            "Nasdaq-only regular-session volume is a fraction of consolidated volume "
            "(Yahoo's daily figure includes all venues, TRF prints and extended hours); "
            "a flag is a prompt to look, not a failure. consolidated_adv is the mean over "
            "the three benchmark days themselves (contemporaneous, not a trailing ADV)"
        ),
    }


def stage_volume(paths: Paths) -> dict[str, Any]:
    _require(paths, "register")
    if not paths.yahoo.is_file():
        raise JobRefused(f"{paths.yahoo} missing: the owner fills it from Yahoo Finance")
    yahoo = json.loads(paths.yahoo.read_text(encoding="utf-8"))
    rep = volume_report(nasdaq_volumes(paths.dataset), yahoo)
    paths.volume_report.write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n", "utf-8")
    adv = {"_source": f"mean Yahoo consolidated volume, {yahoo['_source']}"}
    adv.update({s: round(v) for s, v in rep["consolidated_adv"].items()})
    if rep["unavailable"]:
        adv["_unavailable"] = rep["unavailable"]
        adv["_note"] = (
            f"{len(rep['unavailable'])} names have no consolidated-volume validation and no "
            "consolidated ADV (sized on Nasdaq venue ADV only); the median Nasdaq/consolidated "
            f"ratio is computed on the {len(rep['validated_symbols'])} validated names only"
        )
    paths.consolidated_adv.write_text(json.dumps(adv, indent=2, sort_keys=True) + "\n", "utf-8")
    return {
        "median_ratio": rep["median_ratio"],
        "flagged": rep["flagged"],
        "n_validated": len(rep["validated_symbols"]),
        "unavailable": sorted(rep["unavailable"]),
    }


def load_predeclaration(paths: Paths) -> dict[str, Any]:
    if not paths.predecl.is_file():
        raise JobRefused(f"{paths.predecl} missing")
    doc = json.loads(paths.predecl.read_text(encoding="utf-8"))
    if doc.get("status") != "frozen" or not doc.get("frozen_by") or not doc.get("frozen_utc"):
        raise JobRefused("pre-declaration is a draft: status must be 'frozen' with frozen_by/_utc")
    params = doc.get("parameters") or {}
    open_ = [k for k in REQUIRED_PARAMS if params.get(k) in (None, "", [])]
    if open_:
        raise JobRefused(f"pre-declaration has open parameters: {open_}")
    if doc.get("result_class") != "secondary":
        raise JobRefused("REL-04c is a SECONDARY result: result_class must be 'secondary'")
    return doc


def bench_argv(paths: Paths, bucket: str, doc: Mapping[str, Any], syms: Sequence[str]) -> list[str]:
    p = doc["parameters"]
    return [
        "--data-dir",
        str(paths.dataset),
        "--out",
        str(paths.bench_out),
        "--tag",
        f"rel04c_{bucket}",
        "--symbols",
        ",".join(syms),
        "--algos",
        ",".join(p["algos"]),
        "--policies",
        ",".join(p["policies"]),
        "--sizes-pct",
        # the benchmark sizes parents on the VENUE (Nasdaq) ADV; the consolidated
        # target is recorded beside it and every parent row carries
        # size_pct_consolidated_adv from --consolidated-adv
        ",".join(f"{x:g}" for x in p["sizes_pct_venue_adv"]),
        "--window-min",
        f"{float(p['window_min']):g}",
        "--consolidated-adv",
        str(paths.consolidated_adv),
        "--cover-all-cells",
    ]


def stage_bench(
    paths: Paths, bucket: str, *, bench_main: Callable[[list[str]], int] | None = None
) -> dict[str, Any]:
    _require(paths, "register", "volume")
    if bucket not in BUCKETS:
        raise JobRefused(f"--bucket {bucket!r} not in {BUCKETS}")
    doc = load_predeclaration(paths)
    syms = _load_universe(paths)["buckets"].get(bucket) or []
    if not syms:
        raise JobRefused(f"bucket {bucket} has no names")
    if not paths.consolidated_adv.is_file():
        raise JobRefused("consolidated_adv.json missing: run the volume stage")
    if bench_main is None:
        from iap.research.execution_benchmark import main as _bench

        bench_main = _bench
    argv = bench_argv(paths, bucket, doc, syms)
    rc = bench_main(argv)
    if rc not in (0, None):
        raise RuntimeError(f"benchmark {bucket} exited {rc}")
    return {"bucket": bucket, "symbols": list(syms)}


def stage_cleanup(paths: Paths) -> dict[str, Any]:
    _require(paths, *(f"bench_{b}" for b in BUCKETS))
    raw = paths.dataset / "raw"
    freed = 0
    if raw.is_dir():
        freed = sum(f.stat().st_size for f in raw.rglob("*") if f.is_file())
        shutil.rmtree(raw)
    return {"freed_gb": round(freed / 1e9, 2)}


# --------------------------------------------------------------------- CLI --
def run_stage(paths: Paths, stage: str, *, day: str | None = None, bucket: str | None = None):
    name = stage if stage not in ("ingest", "bench") else f"{stage}_{day or bucket}"
    if paths.done(name):
        _log(paths, {"stage": name, "status": "skipped (marker)"})
        return None
    t0 = time.perf_counter()
    if stage == "verify":
        detail = stage_verify(paths)
    elif stage == "select":
        detail = stage_select(paths)
    elif stage == "ingest":
        if day is None:
            raise JobRefused("ingest needs --day")
        detail = stage_ingest(paths, day)
    elif stage == "register":
        detail = stage_register(paths)
    elif stage == "volume":
        detail = stage_volume(paths)
    elif stage == "bench":
        if bucket is None:
            raise JobRefused("bench needs --bucket")
        detail = stage_bench(paths, bucket)
    elif stage == "cleanup":
        detail = stage_cleanup(paths)
    else:  # pragma: no cover - argparse choices
        raise JobRefused(f"unknown stage {stage}")
    from iap.marketdata.runmem import peak_rss_fields

    rec = {
        "stage": name,
        "status": "ok",
        "seconds": round(time.perf_counter() - t0, 1),
        **peak_rss_fields(),
        **detail,
    }
    _log(paths, rec)
    paths.mark(name)
    return rec


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m iap.research.rel04c_job", description=__doc__)
    ap.add_argument("stage", choices=STAGES)
    ap.add_argument("--day", choices=[d["date"] for d in DAYS])
    ap.add_argument("--bucket", choices=BUCKETS)
    ap.add_argument("--data-root", type=Path, default=REPO / "data" / "real")
    ap.add_argument("--work", type=Path, default=REPO / WORK_REL)
    a = ap.parse_args(argv)
    paths = Paths(a.data_root, a.work)
    try:
        run_stage(paths, a.stage, day=a.day, bucket=a.bucket)
    except JobRefused as exc:
        _log(paths, {"stage": a.stage, "status": "refused", "reason": str(exc)})
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
