"""REL-04 capture-ts verification runner (``research/v118/rel04_verify/PREDECLARATION.md``).

None of the subcommands is run by the session that wrote this file.

``pin`` (coordinator, after the REL-04 re-ingest ended) copies the six ``dataset_version``
values of ``data/real/iex_capts`` into ``predeclaration.json``.  It reads only each
``dataset.json``.  Commit the result before ``bind``.

``registry-patch`` (coordinator, after ``pin``) writes a SEALED copy of the holdout registry
with this prereg id added to ``spent_by`` of the six sessions and the six capts datasets
registered as ``spent`` (never ``in_sample``).  ``--out`` is required: the registry file is
replaced by the coordinator, by the normal reviewed edit, not by this command.

``bind`` (anyone, clean checkout) writes the UNSIGNED prereg through
``tools/prereg_bind_identity.py bind`` with the arguments :func:`identity_args` derives from
``predeclaration.json``.  Reads no market data.

``run`` (a non-signing actor, after the prereg is signed and committed) refuses unless, in
this order: the declaration is pinned and the registry carries it; the prereg is signed and
its ``analysis_identity`` matches this checkout; ``IAP_RUN_ACTOR`` is set and is not the
signer and no signing key is in the environment; every one of the twelve dataset directories
passes ``guard_or_exit`` and has its declared ``dataset_version``; enough RAM and disk are
free.  Then, one day at a time: A) the identity comparison of ``normalized/events.parquet``
(every column but ``receive_ts`` identical, row for row), C) the feed-latency summary
already recorded in ``dataset.json``; B) only if A passed on all six days, the features
step of ``process-iex-day`` on each capts dataset.  Writes ``identity_<day>.json``,
``latency_<day>.json``, ``features_<day>.json``, ``DECISION.json`` and ``REPORT.md`` here.

``analyse`` rebuilds ``DECISION.json`` / ``REPORT.md`` from the files already in this
directory (reads no market data).

The capture clock's offset to IEX's send clock is unknown (``iex.CAPTURE_TS_PROVENANCE``):
only within-capture differences are interpretable.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

DECL = HERE / "predeclaration.json"
SELF_REL = "research/v118/rel04_verify/run_rel04_verify.py"
DECL_REL = "research/v118/rel04_verify/predeclaration.json"
DEFAULT_DATA_ROOT = ROOT / "data" / "real"
METRICS = (
    "row_count_difference",
    "mismatching_rows_excluding_receive_ts",
    "receive_ts_before_exchange_rows",
    "receive_ts_differing_rows",
    "feed_latency_negative_share",
)
PURPOSE = "rel04_capts_verify"
RECEIVE = "receive_ts"


class Refused(SystemExit):
    def __init__(self, msg: str) -> None:
        print(f"REFUSED: {msg}", file=sys.stderr)
        super().__init__(2)


def load_decl(path: Path = DECL) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, doc: Any) -> None:
    path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def bound_files(decl: Mapping[str, Any]) -> list[str]:
    return sorted({SELF_REL, DECL_REL})


def days(decl: Mapping[str, Any]) -> list[str]:
    return sorted(decl["days"])


def unpinned(decl: Mapping[str, Any]) -> list[str]:
    return [d for d in days(decl) if not decl["days"][d].get("capts_dataset_version")]


def dataset_versions(decl: Mapping[str, Any]) -> list[str]:
    out = []
    for d in days(decl):
        out += [decl["days"][d]["iex_dataset_version"], decl["days"][d]["capts_dataset_version"]]
    return sorted(v for v in out if v)


def identity_args(decl: Mapping[str, Any]) -> dict[str, Any]:
    """The ``analysis_identity`` arguments: the binder and the runner both call this."""
    if unpinned(decl):
        raise Refused(f"capts_dataset_version not pinned for {unpinned(decl)}: run `pin` first")
    thresholds = {
        "days": copy.deepcopy(decl["days"]),
        "identity": dict(decl["identity"]),
        "features": dict(decl["features"]),
        "latency": dict(decl["latency"]),
        "statistics": dict(decl["statistics"]),
        "decision_rule": dict(decl["decision_rule"]),
        "stop_rules": dict(decl["stop_rules"]),
    }
    return {
        "scripts": bound_files(decl),
        "data_versions": dataset_versions(decl),
        "family": (
            f"REL-04 capture-ts verification: identity of events.parquet except receive_ts, "
            f"feed-latency summary, features; days {','.join(days(decl))}"
        ),
        "metrics": list(METRICS),
        "thresholds": thresholds,
    }


# ---------------------------------------------------------------- registry --
def check_days(decl: Mapping[str, Any]) -> None:
    if unpinned(decl):
        raise Refused(f"capts_dataset_version not pinned for {unpinned(decl)}: run `pin` first")
    dirs = []
    for d in days(decl):
        row = decl["days"][d]
        tag = d.replace("-", "")
        for key, root in (("iex_dir", "iex/"), ("capts_dir", "iex_capts/")):
            if row[key] != f"{root}iex_{tag}":
                raise Refused(f"{d}: {key} {row[key]!r} is not {root}iex_{tag}")
        dirs += [row["iex_dir"], row["capts_dir"]]
    if len(set(dirs)) != len(dirs):
        raise Refused("days: a dataset directory is listed twice")


def check_registry(
    decl: Mapping[str, Any], prereg_id: str, registry: Mapping[str, Any] | None = None
) -> None:
    """Every session is registered and authorises this prereg; every dataset (both ingests) is
    registered as ``spent`` with its version.  Reads the registry only."""
    from iap.research.holdout_registry import load_registry

    reg = registry if registry is not None else load_registry()
    for d in days(decl):
        rules = reg.get("sessions", {}).get(d)
        if not rules:
            raise Refused(f"registry: session {d} is not registered")
        spent = [r for r in rules if r["state"] == "spent"]
        if not spent or any(r["state"] == "in_sample" for r in rules):
            raise Refused(f"registry: session {d} is not a spent session (never in_sample)")
        if any(prereg_id not in r.get("spent_by", []) for r in spent):
            raise Refused(f"registry: {prereg_id} is not in spent_by of {d}: apply registry-patch")
        row = decl["days"][d]
        for key, dkey in (
            ("iex_dataset_version", "iex_dir"),
            ("capts_dataset_version", "capts_dir"),
        ):
            entry = reg.get("datasets", {}).get(row[key])
            if entry is None:
                raise Refused(f"registry: dataset {row[key]} ({row[dkey]}) is not registered")
            if entry["state"] != "spent" or entry["sessions"] != [d]:
                raise Refused(f"registry: dataset {row[key]} is not registered as spent for {d}")


def patch_registry(
    decl: Mapping[str, Any],
    registry: Mapping[str, Any],
    identities: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """The sealed registry with this prereg authorised on the six sessions and the six capts
    datasets registered as ``spent``.  ``identities`` maps a day to ``dataset_identity`` of its
    capts directory.  Nothing else changes."""
    from iap.research.holdout_registry import seal

    prereg_id = decl["prereg"]
    doc = copy.deepcopy(dict(registry))
    for d in days(decl):
        rules = doc["sessions"].get(d)
        if not rules:
            raise Refused(f"registry: session {d} is not registered")
        n = 0
        for r in rules:
            if r["state"] != "spent":
                continue
            n += 1
            if prereg_id not in r["spent_by"]:
                r["spent_by"] = [*r["spent_by"], prereg_id]
        if not n:
            raise Refused(f"registry: session {d} has no spent rule")
        row, ident = decl["days"][d], identities[d]
        if ident["dataset_version"] != row["capts_dataset_version"] or ident["sessions"] != [d]:
            raise Refused(f"{d}: capts dataset identity differs from the pinned declaration")
        doc["datasets"][row["capts_dataset_version"]] = {
            "content_hash": ident["content_hash"],
            "label": f"iex_capts_{d.replace('-', '')}",
            "sessions": [d],
            "state": "spent",
            "symbols": ident["symbols"],
        }
    return seal(doc)


# ------------------------------------------------------------------ verify --
def verify(
    prereg: Path,
    decl: Mapping[str, Any],
    *,
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Signed, identity matches, runner independent. Raises :class:`Refused`."""
    from iap.agents.analysis_identity import (
        IdentityError,
        SignatureError,
        require_independent_runner,
        verify_identity,
        verify_signed_document,
    )

    if not prereg.is_file():
        raise Refused(f"{prereg} does not exist: bind and sign the prereg first")
    doc = json.loads(prereg.read_text(encoding="utf-8"))
    try:
        verify_signed_document(root, doc)
    except SignatureError as exc:
        raise Refused(f"prereg not usable: {exc}") from exc
    args = identity_args(decl)
    try:
        verify_identity(
            root,
            doc.get("analysis_identity"),
            data_versions=args["data_versions"],
            family=args["family"],
            metrics=args["metrics"],
            thresholds=args["thresholds"],
        )
        actor = require_independent_runner(doc, env)
    except IdentityError as exc:
        raise Refused(str(exc)) from exc
    return {
        "prereg": prereg.resolve().relative_to(root.resolve()).as_posix(),
        "prereg_sha256": hashlib.sha256(prereg.read_bytes()).hexdigest(),
        "runner_actor": actor,
        "signer": doc["signature"]["maintainer"],
        "commit_bound": doc["analysis_identity"]["commit"],
    }


def check_datasets(decl: Mapping[str, Any], data_root: Path, prereg_id: str) -> dict[str, str]:
    """``guard_or_exit`` on all twelve directories, then the declared ``dataset_version``."""
    from iap.research.holdout_registry import HoldoutRefused, dataset_identity, guard_or_exit

    out = {}
    for d in days(decl):
        row = decl["days"][d]
        for key, ver in (
            ("iex_dir", "iex_dataset_version"),
            ("capts_dir", "capts_dataset_version"),
        ):
            path = data_root / row[key]
            if not path.is_dir():
                raise Refused(f"{d}: {row[key]} is missing under {data_root}")
            guard_or_exit(path, PURPOSE, prereg_id)
            try:
                got = dataset_identity(path)["dataset_version"]
            except HoldoutRefused as exc:
                raise Refused(str(exc)) from exc
            if got != row[ver]:
                raise Refused(f"{row[key]}: dataset_version {got} != declared {row[ver]}")
            out[row[key]] = got
    return out


def check_resources(
    decl: Mapping[str, Any],
    data_root: Path,
    free_gb: Callable[[], float | None] | None = None,
    disk_gb: Callable[[Path], float] | None = None,
) -> dict[str, float]:
    from iap.marketdata.runmem import available_ram_gb

    ram = (free_gb or available_ram_gb)()
    disk = (disk_gb or (lambda p: shutil.disk_usage(p).free / 1e9))(data_root)
    sr = decl["stop_rules"]
    if ram is not None and ram < float(sr["min_free_ram_gb"]):
        raise Refused(f"available RAM {ram:.1f} GB < declared minimum {sr['min_free_ram_gb']} GB")
    if disk < float(sr["min_free_disk_gb"]):
        raise Refused(f"free disk {disk:.1f} GB < declared minimum {sr['min_free_disk_gb']} GB")
    return {"free_ram_gb": -1.0 if ram is None else float(ram), "free_disk_gb": float(disk)}


# ---------------------------------------------------------- A: identity --
def _chunks(path: Path, rows: int):
    """Fixed-size ``pyarrow.Table`` slices of a parquet file (row-group boundaries ignored)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    buf: list[Any] = []
    n = 0
    for batch in pq.ParquetFile(path).iter_batches(batch_size=rows):
        buf.append(batch)
        n += batch.num_rows
        while n >= rows:
            t = pa.Table.from_batches(buf)
            yield t.slice(0, rows)
            rest = t.slice(rows)
            buf = rest.to_batches() if rest.num_rows else []
            n = rest.num_rows
    if n:
        yield pa.Table.from_batches(buf)


def compare_events(
    reference: Path, candidate: Path, *, chunk_rows: int = 2_000_000, max_examples: int = 5
) -> dict[str, Any]:
    """Compare two ``events.parquet`` files column by column, row for row.  Every column but
    ``receive_ts`` must be equal (a null equals a null); ``receive_ts`` is only characterised."""
    import numpy as np
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    fa, fb = pq.ParquetFile(reference), pq.ParquetFile(candidate)
    sa = [(f.name, str(f.type)) for f in fa.schema_arrow]
    sb = [(f.name, str(f.type)) for f in fb.schema_arrow]
    res: dict[str, Any] = {
        "rows_reference": fa.metadata.num_rows,
        "rows_candidate": fb.metadata.num_rows,
        "row_count_difference": fb.metadata.num_rows - fa.metadata.num_rows,
        "schema_equal": sa == sb,
        "columns": [c for c, _ in sb],
        "mismatches": {},
        "examples": {},
        "receive_ts": {},
        "instrument_rows": {},
    }
    if sa != sb or res["row_count_difference"] != 0 or RECEIVE not in dict(sb):
        res["mismatching_rows_excluding_receive_ts"] = None
        res["compared"] = False
        return res
    cols = [c for c, _ in sb]
    mism = dict.fromkeys(cols, 0)
    examples: dict[str, list] = {c: [] for c in cols}
    rec = {
        "differing_rows": 0,
        "before_exchange_rows": 0,
        "after_exchange_rows": 0,
        "reference_not_equal_exchange_rows": 0,
    }
    inst: dict[int, int] = {}
    offset = 0
    for ta, tb in zip(_chunks(reference, chunk_rows), _chunks(candidate, chunk_rows), strict=True):
        for c in cols:
            a, b = ta.column(c), tb.column(c)
            eq = pc.fill_null(pc.equal(a, b), False)
            both_null = pc.and_(pc.is_null(a), pc.is_null(b))
            ok = pc.or_(eq, both_null)
            bad = int(len(ok) - pc.sum(pc.cast(ok, "int64")).as_py())
            if c == RECEIVE:
                rec["differing_rows"] += bad
                continue
            mism[c] += bad
            if bad and len(examples[c]) < max_examples:
                idx = np.flatnonzero(~ok.to_numpy(zero_copy_only=False))[:max_examples]
                for i in idx.tolist():
                    if len(examples[c]) < max_examples:
                        examples[c].append(
                            {
                                "row": offset + i,
                                "reference": a[i].as_py(),
                                "candidate": b[i].as_py(),
                            }
                        )
        ex = tb.column("exchange_ts").to_numpy(zero_copy_only=False)
        rb = tb.column(RECEIVE).to_numpy(zero_copy_only=False)
        ra = ta.column(RECEIVE).to_numpy(zero_copy_only=False)
        rec["before_exchange_rows"] += int((rb < ex).sum())
        rec["after_exchange_rows"] += int((rb > ex).sum())
        rec["reference_not_equal_exchange_rows"] += int((ra != ex).sum())
        ids, cnt = np.unique(
            tb.column("instrument_id").to_numpy(zero_copy_only=False), return_counts=True
        )
        for i, n in zip(ids.tolist(), cnt.tolist(), strict=True):
            inst[int(i)] = inst.get(int(i), 0) + int(n)
        offset += tb.num_rows
    res.update(
        compared=True,
        mismatches={c: n for c, n in mism.items() if c != RECEIVE},
        examples={c: v for c, v in examples.items() if v},
        receive_ts=rec,
        instrument_rows={str(k): inst[k] for k in sorted(inst)},
        mismatching_rows_excluding_receive_ts=sum(n for c, n in mism.items() if c != RECEIVE),
    )
    return res


def day_verdict(res: Mapping[str, Any], source_receive_ts: str | None) -> dict[str, Any]:
    """The declared per-day pass rule, with every failed clause named."""
    failed = []
    if not res.get("schema_equal"):
        failed.append("schema differs")
    if res.get("row_count_difference") != 0:
        failed.append("row counts differ")
    if res.get("compared"):
        if res["mismatching_rows_excluding_receive_ts"] != 0:
            failed.append("columns other than receive_ts differ")
        if res["receive_ts"]["before_exchange_rows"] != 0:
            failed.append("candidate receive_ts < exchange_ts")
        if res["receive_ts"]["differing_rows"] == 0:
            failed.append("receive_ts identical in every row (capture stamp has no effect)")
    elif not failed:
        failed.append("not compared")
    if source_receive_ts != "iex_capture":
        failed.append(f"dataset.json source.receive_ts is {source_receive_ts!r}, not 'iex_capture'")
    return {"pass": not failed, "failed_clauses": failed}


def identity_one(decl: Mapping[str, Any], data_root: Path, day: str) -> dict[str, Any]:
    row, ident = decl["days"][day], decl["identity"]
    ref = data_root / row["iex_dir"] / ident["file"]
    cand = data_root / row["capts_dir"] / ident["file"]
    for p in (ref, cand):
        if not p.is_file():
            raise Refused(f"{day}: {p} is missing")
    man = json.loads((data_root / row["capts_dir"] / "dataset.json").read_text(encoding="utf-8"))
    res = compare_events(
        ref,
        cand,
        chunk_rows=int(ident["chunk_rows"]),
        max_examples=int(ident["max_examples_per_column"]),
    )
    src = (man.get("source") or {}).get("receive_ts")
    return {"day": day, "source_receive_ts": src, **res, "verdict": day_verdict(res, src)}


# ----------------------------------------------------------- C: latency --
def _bucket(hist: Mapping[str, int], q: float) -> int | None:
    total = sum(hist.values())
    if not total:
        return None
    run = 0
    for k in sorted(hist, key=int):
        run += hist[k]
        if run >= q * total:
            return int(k)
    return None


def latency_one(decl: Mapping[str, Any], data_root: Path, day: str) -> dict[str, Any]:
    """Descriptive summary of the feed latency recorded in ``dataset.json``; no event is read."""
    row = decl["days"][day]
    man = json.loads((data_root / row["capts_dir"] / "dataset.json").read_text(encoding="utf-8"))
    sess = (man.get("sessions") or {}).get(day)
    if sess is None:
        raise Refused(f"{day}: dataset.json has no session {day}")
    lat = ((sess.get("messages") or {}).get("iex") or {}).get("feed_latency")
    if lat is None:
        raise Refused(f"{day}: dataset.json records no feed_latency (not a --capture-ts ingest)")
    raw = sess.get("raw") or {}
    n = lat["n"]
    hist = lat.get("log2_histogram") or {}
    return {
        "day": day,
        "n": n,
        "negative": lat["negative"],
        "negative_share": None if not n else lat["negative"] / n,
        "mean_ns": lat["mean_ns"],
        "min_ns": lat["min_ns"],
        "max_ns": lat["max_ns"],
        "log2_histogram": hist,
        "approx_p50_log2_bucket": _bucket(hist, 0.5),
        "approx_p99_log2_bucket": _bucket(hist, 0.99),
        "capture_ts_before_exchange": raw.get("capture_ts_before_exchange"),
        "capture_ts_absent": raw.get("capture_ts_absent"),
        "bucket_note": "bucket k holds latencies in [2^k - 1, 2^(k+1) - 2] ns; non-negative values only",
        "provenance": man.get("capture_ts_provenance")
        or (sess.get("messages") or {}).get("iex", {}).get("capture_ts_provenance"),
    }


# ------------------------------------------------------------ B: features --
def features_argv(decl: Mapping[str, Any], ds: Path, python: str = sys.executable) -> list[str]:
    f = decl["features"]
    return [
        python, "-m", "iap.features",
        "--data-dir", str(ds / "normalized"),
        "--out-dir", str(ds / "features"),
        "--configs", str(ds / "configs"),
        "--registry-out", str(ds / "features" / "feature_registry.json"),
        "--workers", str(f["workers"]),
        "--engine", str(f["engine"]),
    ]  # fmt: skip


def features_one(
    decl: Mapping[str, Any],
    data_root: Path,
    day: str,
    *,
    run: Callable[..., Any] = subprocess.run,
    python: str = sys.executable,
) -> dict[str, Any]:
    """The features step of ``process-iex-day`` on one capts dataset (the guard ran first and
    its authorisation travels to the child through ``IAP_HOLDOUT_AUTHORISED``)."""
    ds = data_root / decl["days"][day]["capts_dir"]
    out = ds / "features"
    if out.is_dir() and list(out.glob("features_*.parquet")):
        raise Refused(f"{day}: {out} already holds features: one run only")
    argv = features_argv(decl, ds, python)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONPATH=str(ROOT / "python" / "src"))
    proc = run(argv, capture_output=True, text=True, check=False, env=env, cwd=str(ROOT / "python"))
    (ds / "features.log").write_text(
        (proc.stdout or "") + (proc.stderr or ""), encoding="utf-8", newline="\n"
    )
    if proc.returncode != 0:
        raise Refused(f"{day}: iap.features exited {proc.returncode}; see {ds / 'features.log'}")
    files = sorted(out.glob("features_*.parquet"))
    if not files:
        raise Refused(f"{day}: iap.features wrote no features_*.parquet under {out}")
    return {
        "day": day,
        "argv": [a if a != python else "<python>" for a in argv],
        "feature_files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
    }


# ---------------------------------------------------------------- decision --
def decide(
    decl: Mapping[str, Any],
    ident: Mapping[str, Mapping[str, Any]],
    lat: Mapping[str, Mapping[str, Any]],
    feats: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    per_day = {d: ident[d]["verdict"] for d in days(decl)}
    ok = all(v["pass"] for v in per_day.values())
    return {
        "x-version": 1,
        "declaration": DECL_REL,
        "identity": "pass" if ok else "fail",
        "per_day": per_day,
        "features_built": sorted(feats),
        "features_status": "built"
        if len(feats) == len(days(decl))
        else "not built (identity failed)",
        "latency_days": sorted(lat),
        "no_inference": decl["statistics"]["inference"],
        "can_claim": decl["claims"]["can_claim"],
        "cannot_claim": decl["claims"]["cannot_claim"],
    }


def report(decl: Mapping[str, Any], dec: Mapping[str, Any], ident, lat) -> str:
    lines = [
        "# REL-04 capture-ts verification: result under the pre-declaration",
        "",
        f"Declaration: `{dec['declaration']}`. Identity: **{dec['identity']}**. "
        f"Features: {dec['features_status']}.",
        "",
        "Failures first.",
        "",
        "| day | pass | rows ref | rows capts | non-receive mismatches | receive_ts differing | capts receive < exchange | failed clauses |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for d in sorted(days(decl), key=lambda x: (dec["per_day"][x]["pass"], x)):
        r = ident[d]
        rc = r.get("receive_ts") or {}
        lines.append(
            f"| {d} | {'yes' if r['verdict']['pass'] else 'NO'} | {r['rows_reference']} | "
            f"{r['rows_candidate']} | {r.get('mismatching_rows_excluding_receive_ts')} | "
            f"{rc.get('differing_rows')} | {rc.get('before_exchange_rows')} | "
            f"{'; '.join(r['verdict']['failed_clauses']) or '-'} |"
        )
    lines += [
        "",
        "Feed latency recorded in dataset.json (within-capture only; the offset to IEX's send "
        "clock is unknown):",
        "",
        "| day | n | negative | negative share | mean ns | min ns | max ns | ~p50 bucket | ~p99 bucket |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for d in days(decl):
        x = lat[d]
        share = "-" if x["negative_share"] is None else f"{x['negative_share']:.6f}"
        lines.append(
            f"| {d} | {x['n']} | {x['negative']} | {share} | {x['mean_ns']} | {x['min_ns']} | "
            f"{x['max_ns']} | {x['approx_p50_log2_bucket']} | {x['approx_p99_log2_bucket']} |"
        )
    lines += ["", f"Inference: {dec['no_inference']}", "", "Can claim:"]
    lines += [f"- {x}" for x in dec["can_claim"]]
    lines += ["", "Cannot claim:", *[f"- {x}" for x in dec["cannot_claim"]], ""]
    return "\n".join(lines)


# -------------------------------------------------------------------- cmds --
def _result_files(out: Path) -> list[Path]:
    return [p for pat in ("identity_*.json", "latency_*.json", "features_*.json", "DECISION.json")
            for p in out.glob(pat)]  # fmt: skip


def cmd_run(
    a: argparse.Namespace,
    *,
    identity: Callable[..., dict] | None = None,
    latency: Callable[..., dict] | None = None,
    features: Callable[..., dict] | None = None,
    free_gb: Callable[[], float | None] | None = None,
    disk_gb: Callable[[Path], float] | None = None,
) -> int:
    decl = load_decl(a.decl)
    root = Path(a.root)
    check_days(decl)
    check_registry(decl, decl["prereg"])
    prereg = (root / decl["prereg"]) if a.prereg is None else Path(a.prereg)
    rec = verify(prereg, decl, root=root)
    data_root = Path(a.data_root)
    rec["datasets"] = check_datasets(decl, data_root, rec["prereg"])
    rec.update(check_resources(decl, data_root, free_gb, disk_gb))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if _result_files(out):  # one run only: an existing result is never overwritten
        raise Refused(f"{out} holds results: a parameter change is a new declaration, not a rerun")
    rec["started_utc"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    ident_f, lat_f, feat_f = (
        identity or identity_one,
        latency or latency_one,
        features or features_one,
    )
    ident, lat, feats = {}, {}, {}
    for d in days(decl):
        print(f"[rel04v] A identity {d}", flush=True)
        ident[d] = ident_f(decl, data_root, d)
        write_json(out / f"identity_{d}.json", ident[d])
    for d in days(decl):
        print(f"[rel04v] C latency {d}", flush=True)
        lat[d] = lat_f(decl, data_root, d)
        write_json(out / f"latency_{d}.json", lat[d])
    if all(ident[d]["verdict"]["pass"] for d in days(decl)):
        for d in days(decl):
            print(f"[rel04v] B features {d}", flush=True)
            feats[d] = feat_f(decl, data_root, d)
            write_json(out / f"features_{d}.json", feats[d])
    else:
        print("[rel04v] identity failed on at least one day: features not built", flush=True)
    rec["ended_utc"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    write_json(out / "run_manifest.json", rec)
    return cmd_analyse(a)


def cmd_analyse(a: argparse.Namespace) -> int:
    decl = load_decl(a.decl)
    out = Path(a.out)
    ident, lat, feats = {}, {}, {}
    for d in days(decl):
        for store, stem, need in ((ident, "identity", True), (lat, "latency", True),
                                  (feats, "features", False)):  # fmt: skip
            p = out / f"{stem}_{d}.json"
            if p.is_file():
                store[d] = json.loads(p.read_text(encoding="utf-8"))
            elif need:
                raise Refused(f"{p} missing: run first")
    dec = decide(decl, ident, lat, feats)
    write_json(out / "DECISION.json", dec)
    (out / "REPORT.md").write_text(report(decl, dec, ident, lat), encoding="utf-8", newline="\n")
    print(f"wrote {out / 'DECISION.json'} and REPORT.md")
    return 0


def cmd_pin(a: argparse.Namespace) -> int:
    from iap.research.holdout_registry import dataset_identity

    decl = load_decl(a.decl)
    data_root = Path(a.data_root)
    for d in days(decl):
        ds = data_root / decl["days"][d]["capts_dir"]
        man_p = ds / "dataset.json"
        if not man_p.is_file():
            raise Refused(f"{d}: {man_p} missing: the REL-04 ingest of this day has not finished")
        man = json.loads(man_p.read_text(encoding="utf-8"))
        if man.get("kind") != "real" or sorted(man.get("sessions") or {}) != [d]:
            raise Refused(f"{d}: dataset.json is not a real one-session dataset for {d}")
        if (man.get("source") or {}).get("receive_ts") != "iex_capture":
            raise Refused(f"{d}: dataset.json source.receive_ts is not 'iex_capture'")
        ver = dataset_identity(ds, rehash=False)["dataset_version"]
        if not ver:
            raise Refused(f"{d}: dataset.json has no dataset_version")
        old = decl["days"][d].get("capts_dataset_version")
        if old and old != ver:
            raise Refused(
                f"{d}: already pinned to {old}, found {ver}: a change is a new declaration"
            )
        decl["days"][d]["capts_dataset_version"] = ver
    write_json_like_decl(a.decl, decl)
    print("pinned; commit predeclaration.json, then bind")
    return 0


def write_json_like_decl(path: Path, decl: Mapping[str, Any]) -> None:
    Path(path).write_text(json.dumps(decl, indent=2) + "\n", encoding="utf-8", newline="\n")


def cmd_registry_patch(a: argparse.Namespace) -> int:
    from iap.research.holdout_registry import dataset_identity, load_registry, render

    decl = load_decl(a.decl)
    check_days(decl)
    data_root = Path(a.data_root)
    idents = {d: dataset_identity(data_root / decl["days"][d]["capts_dir"]) for d in days(decl)}
    doc = patch_registry(decl, load_registry(a.registry), idents)
    Path(a.out).write_text(render(doc), encoding="utf-8", newline="\n")
    print(f"wrote {a.out}; review the diff, then replace research/holdout_registry.json")
    return 0


def cmd_bind(a: argparse.Namespace) -> int:
    decl = load_decl(a.decl)
    root = Path(a.root)
    check_days(decl)
    args = identity_args(decl)
    sys.path.insert(0, str(ROOT / "tools"))
    import prereg_bind_identity as pbi  # type: ignore[import-not-found]

    argv: list[str] = ["--root", str(root), "bind", "--protocol", decl["protocol"]]
    argv += ["--out", decl["prereg"], "--scripts", *args["scripts"]]
    argv += ["--data-version", *args["data_versions"], "--family", args["family"]]
    argv += ["--metric", *args["metrics"], "--thresholds", json.dumps(args["thresholds"])]
    if a.dry_run:
        argv.append("--dry-run")
    return int(pbi.main(argv))


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--decl", type=Path, default=DECL)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pin", help="write the six capts dataset versions into the declaration")
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    g = sub.add_parser("registry-patch", help="write the sealed registry with this prereg added")
    g.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    g.add_argument("--registry", type=Path, default=None)
    g.add_argument("--out", type=Path, required=True)
    b = sub.add_parser("bind", help="write the UNSIGNED prereg (clean checkout)")
    b.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("run", help="verify, guard, identity, latency, features, analyse")
    r.add_argument("--prereg", type=Path, default=None)
    r.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    r.add_argument("--out", type=Path, default=HERE)
    n = sub.add_parser("analyse", help="DECISION.json + REPORT.md from the result files")
    n.add_argument("--out", type=Path, default=HERE)
    a = ap.parse_args(argv)
    return {"pin": cmd_pin, "registry-patch": cmd_registry_patch, "bind": cmd_bind,
            "run": cmd_run, "analyse": cmd_analyse}[a.cmd](a)  # fmt: skip


if __name__ == "__main__":
    raise SystemExit(main())
