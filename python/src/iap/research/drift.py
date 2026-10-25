"""Feature drift monitor (v1.15): reference sessions vs target sessions.

Compares the per-feature distributions of a *reference* set of feature-store
directories (the in-sample sessions) with a *target* set (e.g. the R7
holdout), per symbol (``instrument_id``) and pooled, and writes a JSON and a
markdown report.  Statistics per feature:

* **PSI** over ``psi_bins`` reference-quantile bins (target mass outside
  the reference range falls in the first / last bin), ``eps`` flooring
  empty bins;
* **KS** statistic, the max |CDF_ref - CDF_target| over the edges of a fine
  equal-width histogram (``fine_bins`` bins over the reference [min, max],
  with under/overflow bins) -- exact up to that resolution;
* **mean shift** and **std ratio**: ``(mean_t - mean_r) / std_r`` and
  ``std_t / std_r``.

**Descriptive only.**  The report never changes a pre-registration, a gate
or a result; it is produced after ingest and before the confirmatory run and
reported alongside the results (docs/R7_DATA_PLAN.md §3).

**Low memory, chunk-invariant.**  Feature files are streamed in parquet
record batches.  Pass 1 over the reference collects the range and moments;
pass 2 fills integer fine histograms for both sets.  The PSI bins are cut at
reference quantiles read off the fine histogram, so every statistic is a
function of additive counts and sums: the chunked result equals the
in-memory one (histograms exactly; moments to float rounding).

Store format: every ``features_<instrument_id>.parquet`` in a directory
(:mod:`iap.features.__main__`); the feature columns are the float columns
other than the label columns.  NaN (invalid) values are counted and skipped.

CLI::

    python -m iap.research.drift --ref <dir> [<dir> ...] --target <dir> [...] --out <dir>
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

POOLED = "pooled"
NON_FEATURE = ("instrument_id", "exchange_ts", "validity_bits")
LABEL_PREFIXES = ("label_",)


@dataclass(frozen=True)
class DriftThresholds:
    psi_warn: float = 0.10
    psi_alert: float = 0.25
    ks_alert: float = 0.10
    mean_shift_alert: float = 0.50  # in reference-std units
    std_ratio_alert: float = 2.0  # flag if ratio > x or < 1/x
    min_rows: int = 100  # fewer valid rows on either side -> "insufficient"


class _Acc:
    """Additive per-(group, feature) accumulators."""

    __slots__ = ("n", "s", "ss", "lo", "hi", "nan", "hist")

    def __init__(self) -> None:
        self.n = 0
        self.s = 0.0
        self.ss = 0.0
        self.lo = math.inf
        self.hi = -math.inf
        self.nan = 0
        self.hist: np.ndarray | None = None

    def moments(self, x: np.ndarray, nan: int) -> None:
        self.nan += nan
        if x.size:
            self.n += int(x.size)
            self.s += float(x.sum())
            self.ss += float(np.dot(x, x))
            self.lo = min(self.lo, float(x.min()))
            self.hi = max(self.hi, float(x.max()))

    @property
    def mean(self) -> float:
        return self.s / self.n if self.n else math.nan

    @property
    def std(self) -> float:
        if self.n < 2:
            return math.nan
        var = (self.ss - self.s * self.s / self.n) / (self.n - 1)
        return math.sqrt(max(var, 0.0))


Chunk = dict  # {"instrument_id": array, <feature>: array, ...}


def feature_columns(names: Iterable[str]) -> list[str]:
    return [
        n
        for n in names
        if n not in NON_FEATURE and not any(n.startswith(p) for p in LABEL_PREFIXES)
    ]


def iter_store(dirs: Iterable[str | Path], batch_rows: int = 65_536) -> Iterator[Chunk]:
    """Stream ``{column: ndarray}`` record batches from feature-store dirs."""
    import pyarrow as pa
    from iap import io as iap_io

    for d in dirs:
        files = sorted(Path(d).glob("features_*.parquet"))
        if not files:
            raise FileNotFoundError(f"no features_*.parquet in {d}")
        for f in files:
            pf = iap_io.parquet_file(f, purpose="drift")
            names = [
                fld.name
                for fld in pf.schema_arrow
                if fld.name == "instrument_id"
                or (pa.types.is_floating(fld.type) and fld.name in feature_columns([fld.name]))
            ]
            for batch in pf.iter_batches(batch_size=batch_rows, columns=names):
                yield {
                    n: batch.column(i).to_numpy(zero_copy_only=False)
                    for i, n in enumerate(batch.schema.names)
                }


def _groups(chunk: Chunk, per_symbol: bool) -> Iterator[tuple[str, np.ndarray | None]]:
    yield POOLED, None
    if per_symbol and "instrument_id" in chunk:
        ids = np.asarray(chunk["instrument_id"])
        for iid in np.unique(ids):
            yield str(int(iid)), ids == iid


def _features(chunk: Chunk, wanted: list[str] | None) -> list[str]:
    cols = feature_columns(chunk)
    return [c for c in cols if wanted is None or c in wanted]


def _pass_moments(chunks: Iterable[Chunk], wanted, per_symbol) -> dict:
    accs: dict[tuple[str, str], _Acc] = {}
    for chunk in chunks:
        feats = _features(chunk, wanted)
        for g, mask in _groups(chunk, per_symbol):
            for f in feats:
                col = np.asarray(chunk[f], dtype=np.float64)
                if mask is not None:
                    col = col[mask]
                ok = np.isfinite(col)
                accs.setdefault((g, f), _Acc()).moments(col[ok], int(col.size - ok.sum()))
    return accs


def _edges(acc: _Acc, fine_bins: int) -> np.ndarray | None:
    if acc.n == 0 or not math.isfinite(acc.lo):
        return None
    lo, hi = acc.lo, acc.hi
    if hi <= lo:
        hi = lo + max(abs(lo), 1.0) * 1e-9
    return np.linspace(lo, hi, fine_bins + 1)


def _hist(x: np.ndarray, edges: np.ndarray) -> np.ndarray:
    # bins: [underflow, fine_0 .. fine_{k-1}, overflow]; the top edge is closed
    idx = np.searchsorted(edges, x, side="right")
    idx[x == edges[-1]] = len(edges) - 1
    return np.bincount(idx, minlength=len(edges) + 1).astype(np.int64)


def _pass_hist(chunks, wanted, per_symbol, edges_by_key, accs) -> None:
    for chunk in chunks:
        feats = _features(chunk, wanted)
        for g, mask in _groups(chunk, per_symbol):
            for f in feats:
                edges = edges_by_key.get((g, f))
                col = np.asarray(chunk[f], dtype=np.float64)
                if mask is not None:
                    col = col[mask]
                col = col[np.isfinite(col)]
                if edges is None or not col.size:
                    continue
                acc = accs.setdefault((g, f), _Acc())
                h = _hist(col, edges)
                acc.hist = h if acc.hist is None else acc.hist + h


def _psi_ks(ref: np.ndarray, tgt: np.ndarray, psi_bins: int, eps: float) -> tuple[float, float]:
    rn, tn = ref.sum(), tgt.sum()
    rc, tc = np.cumsum(ref) / rn, np.cumsum(tgt) / tn
    ks = float(np.max(np.abs(rc - tc)))
    # PSI bins: cut the fine histogram at reference quantiles (contiguous groups)
    cuts = sorted(
        {int(np.searchsorted(rc, q, side="left")) for q in np.arange(1, psi_bins) / psi_bins}
    )
    bounds = [0, *[c + 1 for c in cuts if 0 <= c < len(ref) - 1], len(ref)]
    bounds = sorted(set(bounds))
    p = np.add.reduceat(ref, bounds[:-1]) / rn
    q = np.add.reduceat(tgt, bounds[:-1]) / tn
    p, q = np.maximum(p, eps), np.maximum(q, eps)
    return float(np.sum((q - p) * np.log(q / p))), ks


def _row(ra: _Acc, ta: _Acc, th: DriftThresholds, psi_bins: int, eps: float) -> dict:
    out = {
        "n_ref": ra.n,
        "n_target": ta.n,
        "nan_ref": ra.nan,
        "nan_target": ta.nan,
        "mean_ref": ra.mean,
        "mean_target": ta.mean,
        "std_ref": ra.std,
        "std_target": ta.std,
        "psi": None,
        "ks": None,
        "mean_shift_std": None,
        "std_ratio": None,
        "flags": [],
        "status": "insufficient",
    }
    if ra.n < th.min_rows or ta.n < th.min_rows or ra.hist is None or ta.hist is None:
        return out
    psi, ks = _psi_ks(ra.hist, ta.hist, psi_bins, eps)
    sd = ra.std
    shift = (ta.mean - ra.mean) / sd if sd and sd > 0 else (0.0 if ta.mean == ra.mean else math.inf)
    ratio = ta.std / sd if sd and sd > 0 else (1.0 if not ta.std else math.inf)
    flags = []
    if psi >= th.psi_alert:
        flags.append("psi")
    if ks >= th.ks_alert:
        flags.append("ks")
    if abs(shift) >= th.mean_shift_alert:
        flags.append("mean_shift")
    if ratio >= th.std_ratio_alert or ratio <= 1.0 / th.std_ratio_alert:
        flags.append("std_ratio")
    status = "drift" if flags else ("warn" if psi >= th.psi_warn else "ok")
    out.update(psi=psi, ks=ks, mean_shift_std=shift, std_ratio=ratio, flags=flags, status=status)
    return out


def compute_drift(
    ref_chunks,
    target_chunks,
    *,
    features: list[str] | None = None,
    thresholds: DriftThresholds | None = None,
    per_symbol: bool = True,
    fine_bins: int = 2000,
    psi_bins: int = 10,
    eps: float = 1e-4,
) -> dict:
    """Drift report from chunk sources.

    ``ref_chunks`` is called (or iterated) twice, so pass a zero-argument
    callable returning a fresh iterator (or a list) for streaming input.
    ``target_chunks`` likewise (it is read twice as well)."""
    th = thresholds or DriftThresholds()

    def it(src):
        return src() if callable(src) else iter(src)

    ref = _pass_moments(it(ref_chunks), features, per_symbol)
    tgt = _pass_moments(it(target_chunks), features, per_symbol)
    edges = {k: _edges(a, fine_bins) for k, a in ref.items()}
    _pass_hist(it(ref_chunks), features, per_symbol, edges, ref)
    _pass_hist(it(target_chunks), features, per_symbol, edges, tgt)

    groups: dict[str, dict] = {}
    for g, f in sorted(set(ref) | set(tgt)):
        groups.setdefault(g, {})[f] = _row(
            ref.get((g, f), _Acc()), tgt.get((g, f), _Acc()), th, psi_bins, eps
        )
    summary = {}
    for g, rows in groups.items():
        counts: dict[str, int] = {}
        for r in rows.values():
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        summary[g] = {
            "features": len(rows),
            "status_counts": dict(sorted(counts.items())),
            "drifted": sorted(f for f, r in rows.items() if r["status"] == "drift"),
        }
    return {
        "schema": 1,
        "descriptive_only": True,
        "note": "Descriptive only: never changes a pre-registration, a gate or a result.",
        "thresholds": asdict(th),
        "params": {"fine_bins": fine_bins, "psi_bins": psi_bins, "eps": eps},
        "summary": summary,
        "groups": groups,
    }


def _fmt(x) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.4g}"
    return str(x)


def to_markdown(rep: dict, max_rows: int = 50) -> str:
    lines = [
        "# Feature drift report",
        "",
        f"_{rep['note']}_",
        "",
        f"Inputs: ref = {rep.get('ref', [])}, target = {rep.get('target', [])}",
        "",
        "Thresholds: " + ", ".join(f"{k}={v}" for k, v in rep["thresholds"].items()),
        "",
        "## Summary",
        "",
        "| group | features | status counts | drifted |",
        "|---|---|---|---|",
    ]
    for g, s in rep["summary"].items():
        drifted = ", ".join(s["drifted"][:10]) + (" ..." if len(s["drifted"]) > 10 else "")
        lines.append(f"| {g} | {s['features']} | {s['status_counts']} | {drifted or '-'} |")
    for g, rows in rep["groups"].items():
        ranked = sorted(rows.items(), key=lambda kv: -(kv[1]["psi"] or -1.0))[:max_rows]
        lines += [
            "",
            f"## {g} (top {len(ranked)} by PSI)",
            "",
            "| feature | status | PSI | KS | mean shift (ref std) | std ratio | n ref | n target |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for f, r in ranked:
            lines.append(
                f"| {f} | {r['status']} | {_fmt(r['psi'])} | {_fmt(r['ks'])} | "
                f"{_fmt(r['mean_shift_std'])} | {_fmt(r['std_ratio'])} | {r['n_ref']} | "
                f"{r['n_target']} |"
            )
    return "\n".join(lines) + "\n"


def _json_safe(o):
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_json_safe(v) for v in o]
    return o


def write_report(rep: dict, out: str | Path) -> tuple[Path, Path]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    jp, mp = out / "drift_report.json", out / "drift_report.md"
    jp.write_text(json.dumps(_json_safe(rep), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    mp.write_text(to_markdown(rep), encoding="utf-8")
    return jp, mp


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m iap.research.drift", description=__doc__.split("\n")[0]
    )
    ap.add_argument("--ref", nargs="+", required=True, help="reference feature-store dirs")
    ap.add_argument("--target", nargs="+", required=True, help="target feature-store dirs")
    ap.add_argument("--out", required=True)
    ap.add_argument("--features", nargs="*", help="restrict to these feature columns")
    ap.add_argument("--pooled-only", action="store_true")
    ap.add_argument("--batch-rows", type=int, default=65_536)
    ap.add_argument("--fine-bins", type=int, default=2000)
    ap.add_argument("--psi-bins", type=int, default=10)
    d = DriftThresholds()
    for k, v in asdict(d).items():
        ap.add_argument(f"--{k.replace('_', '-')}", type=type(v), default=v)
    a = ap.parse_args(argv)
    th = DriftThresholds(**{k: getattr(a, k) for k in asdict(d)})
    rep = compute_drift(
        lambda: iter_store(a.ref, a.batch_rows),
        lambda: iter_store(a.target, a.batch_rows),
        features=a.features or None,
        thresholds=th,
        per_symbol=not a.pooled_only,
        fine_bins=a.fine_bins,
        psi_bins=a.psi_bins,
    )
    rep["ref"] = [str(p) for p in a.ref]
    rep["target"] = [str(p) for p in a.target]
    jp, mp = write_report(rep, a.out)
    s = rep["summary"].get(POOLED, {})
    print(f"pooled: {s.get('status_counts')}; wrote {jp} and {mp} (descriptive only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
