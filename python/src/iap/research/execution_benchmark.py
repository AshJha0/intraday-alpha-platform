"""Implementation-shortfall benchmark on replayed sessions (v1.17 WS-B item B1).

    python -m iap.research.execution_benchmark --data-dir data/real/multi7 \\
        [--sessions 20190130,20190327] [--symbols AAPL,MSFT] \\
        [--algos TWAP,VWAP,POV,IS] [--policies native,aggressive,passive] \\
        [--sizes-pct 0.1,0.5,1,2] [--window-min 30] [--out research/execution/benchmark]

For every (session, symbol) stream of a dataset directory the driver cuts
back-to-back parent windows (``--window-min`` each, after ``--warmup-min``),
assigns each window one grid cell (algo x parent size in % of ADV, cycling,
rotated by one cell per session so a cell does not always land at the same
time of day), works every window under every policy with
:class:`iap.execution.ExecutionReplay`, and prices each parent with
:func:`iap.tca.shortfall.parent_costs` (the study's decomposition: Perold
shortfall, spread, fees, the simulator's impact adder, completion of the
unfilled quantity, ``all_in``).

Per cell the report gives the per-parent mean of every cost in bps of the
arrival notional with DAY-CLUSTERED standard errors (one cluster per
session: the cluster-robust sandwich for a mean, a Student-t with G - 1
degrees of freedom via :func:`iap.validation.metrics.day_cluster_inference`,
and a seeded cluster bootstrap over sessions), the fill rate, the maker
share, the X-5 ``depth_exhausted`` share (parents with an aggressive child
cancelled for want of displayed depth, and the qty they left) and the X-7
fill-provenance shares. Policies are compared pairwise against
``--baseline-policy`` on the same windows (paired per-parent differences,
day-clustered).

Every cost is LABELLED with ``ExecConfig.impact_label()``: the simulator
replays the book and ADDS a fitted or linear impact adder; nothing here is
a measured market reaction, and the replayed book never moves for our
fills (``docs/HOW_IT_WORKS.md`` section 3.5).

Holdout discipline: a data directory whose path contains ``holdout`` is
refused unless ``--allow-holdout`` is given; the benchmark is an in-sample
tool. The pre-registration draft for the B7 gate is written by
``--write-prereg-draft`` (NOT signed: the owner signs it with the usual
register tool before any fresh-day run).

Deterministic: same dataset + same arguments => same JSON bytes (sorted
keys; the only wall-clock field is ``generated``, which is omitted when
``--no-timestamp`` is given, as the tests do).

**Signal-aware policies (v1.17 B2 / B3).** A policy name may carry a
variant, ``base+variant``: ``native+urgency`` works the same parents with
``urgency_policy="alpha"`` (B2) on top of the ``native`` child style,
``native+pacing`` with ``pov_signal_pacing`` (POV) / ``slice_signal_tilt``
(TWAP, VWAP, IS) (B3), ``native+ac`` turns the IS parents into plain
Almgren-Chriss parents (the control) and ``native+ac_drift`` adds the
signal as the AC drift term (IS cells only). Every variant pairs with its
own control on the SAME windows (``native+urgency-native``,
``native+ac_drift-native+ac``, ...) beside the baseline pairing. The
signal comes from ``--signal``: an alpha id (``EQ10``, ``EQ05``: the
registered parameters of ``configs/strategies/alpha_params.json`` scored
on ``<data-dir>/features``, standardised per instrument to signal units;
``--signal-refit`` fits in-sample instead, labelled) or ``synthetic`` (a
deterministic sine wave in signal units, for tests). The report carries
the urgency-regime shares per cell (why each child got its urgency).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from iap.core.codec import read_iap1, read_jsonl
from iap.core.rng import SplitMix64
from iap.execution import (
    AlgoType,
    AlphaUrgencyPolicy,
    ArraySignalSource,
    ExecPolicy,
    ExecutionReplay,
    ISModel,
    PacingParams,
    ParentOrder,
    load_exec_config,
    load_sor_options,
)
from iap.execution.optimal import ac_params_from_calibration
from iap.execution.signal import URGENCY_REGIMES, AlphaUrgencySource
from iap.execution.types import FILL_PROVENANCES
from iap.tca.markout import build_gated_timeline
from iap.tca.shortfall import parent_costs
from iap.validation.metrics import BOOTSTRAP_LOW_POWER_DAYS, day_cluster_inference

SEC = 1_000_000_000
DEFAULT_OUT = Path("research/execution/benchmark")
DEFAULT_SIZES_PCT = (0.1, 0.5, 1.0, 2.0)
DEFAULT_ALGOS = ("TWAP", "VWAP", "POV", "IS")
POLICIES: dict[str, ExecPolicy] = {
    "native": ExecPolicy.NATIVE,
    "aggressive": ExecPolicy.AGGRESSIVE,
    "passive": ExecPolicy.PASSIVE,
}
#: v1.17 B2 / B3 policy variants (``base+variant``) and the variant each
#: one is the signal-aware arm of (its control; ``None`` = the base policy).
VARIANTS: dict[str, str | None] = {
    "urgency": None,  #: B2: urgency_policy="alpha"
    "pacing": None,  #: B3: pov_signal_pacing / slice_signal_tilt
    "ac": None,  #: plain Almgren-Chriss IS (the ac_drift control; IS cells only)
    "ac_drift": "ac",  #: B3: AC with the signal as the drift (IS cells only)
}
SYNTHETIC_SIGNAL = "synthetic"
SYNTHETIC_PERIOD_S = 300.0
SYNTHETIC_AMPLITUDE_Z = 1.5
#: costs reported per cell, in bps of the arrival notional
REPORT_COSTS = (
    "all_in",
    "shortfall",
    "trading",
    "opportunity",
    "spread",
    "fees_net",
    "sim_impact",
    "completion",
)
PREREG_DRAFT_NAME = "prereg_DRAFT.json"


# ----------------------------------------------------------------- stats --


def cluster_mean_stats(
    values: list[float], clusters: list[str], seed: int = 0, n_boot: int = 2000
) -> dict[str, object]:
    """Mean of ``values`` with one cluster per distinct ``clusters`` label.

    ``se`` is the cluster-robust (CR1) standard error of the mean,
    ``G / (G - 1) * sum_g (sum_{i in g} (x_i - mean))^2 / N^2``; ``t``,
    ``df = G - 1`` and ``p_student_t`` follow; ``ci95`` is the Student-t
    interval; ``boot_p05`` / ``boot_p95`` resample whole clusters with
    replacement (``numpy.random.default_rng(seed)``). ``None`` where
    undefined (fewer than two clusters), ``low_power`` below
    :data:`iap.validation.metrics.BOOTSTRAP_LOW_POWER_DAYS` clusters.
    """
    n = len(values)
    if n != len(clusters):
        raise ValueError("values and clusters must have the same length")
    labels = sorted(set(clusters))
    g = len(labels)
    out: dict[str, object] = {
        "n": n,
        "n_clusters": g,
        "mean": None,
        "se": None,
        "t": None,
        "df": None,
        "p_student_t": None,
        "ci95": None,
        "boot_p05": None,
        "boot_p95": None,
        "low_power": g < BOOTSTRAP_LOW_POWER_DAYS,
    }
    if n == 0:
        return out
    x = np.asarray(values, dtype=float)
    mean = float(x.mean())
    out["mean"] = mean
    if g < 2:
        return out
    idx = {lab: k for k, lab in enumerate(labels)}
    cid = np.asarray([idx[c] for c in clusters])
    resid = x - mean
    cluster_sum = np.bincount(cid, weights=resid, minlength=g)
    var = g / (g - 1) * float(np.sum(cluster_sum**2)) / float(n * n)
    se = math.sqrt(var)
    out["se"] = se
    inf = day_cluster_inference(mean / se if se > 0 else float("nan"), g)
    out["df"] = g - 1
    if se > 0:
        out["t"] = mean / se
        out["p_student_t"] = inf["p_student_t"]
        half = inf["crit_student_t"] * se
        out["ci95"] = [mean - half, mean + half]
    else:
        out["t"] = None
        out["ci95"] = [mean, mean]
    # Cluster bootstrap: resample sessions, recompute the pooled mean.
    sums = np.bincount(cid, weights=x, minlength=g)
    counts = np.bincount(cid, minlength=g).astype(float)
    rng = np.random.default_rng(int(seed))
    draws = rng.integers(0, g, size=(int(n_boot), g))
    mult = np.stack([np.bincount(row, minlength=g) for row in draws]).astype(float)
    boot = (mult @ sums) / (mult @ counts)
    out["boot_p05"] = float(np.quantile(boot, 0.05))
    out["boot_p95"] = float(np.quantile(boot, 0.95))
    return out


# ------------------------------------------------------------ the grid --


def parent_qty(adv: float, pct_adv: float, lot: int = 100) -> int:
    """``pct_adv`` % of ``adv`` rounded to the nearest ``lot``, at least one lot."""
    return max(lot, int(round(pct_adv / 100.0 * adv / lot)) * lot)


def parse_policy(name: str) -> tuple[str, str | None]:
    """``"native+urgency"`` -> ``("native", "urgency")``; ``"native"`` -> ``("native", None)``."""
    base, _, variant = name.partition("+")
    if base not in POLICIES:
        raise SystemExit(f"unknown policy {name!r}; bases are {sorted(POLICIES)}")
    if variant and variant not in VARIANTS:
        raise SystemExit(f"unknown variant {variant!r} in {name!r}; choose from {sorted(VARIANTS)}")
    return base, variant or None


def control_policy(name: str) -> str | None:
    """The control a signal-aware policy pairs with (``None`` for a base policy)."""
    base, variant = parse_policy(name)
    if variant is None:
        return None
    ctrl = VARIANTS[variant]
    return base if ctrl is None else f"{base}+{ctrl}"


def synthetic_signal(instrument_ids, first_ts: int, last_ts: int) -> ArraySignalSource:
    """A deterministic signal in signal units for tests: one reading per
    second, ``z = A sin(2 pi t / P)`` with ``P`` = 300 s, ``A`` = 1.5."""
    rows = []
    t = first_ts
    while t <= last_ts:
        z = SYNTHETIC_AMPLITUDE_Z * math.sin(
            2.0 * math.pi * ((t - first_ts) / SEC) / SYNTHETIC_PERIOD_S
        )
        rows.append((t, z, z * 1e-4))
        t += SEC
    return ArraySignalSource({iid: list(rows) for iid in instrument_ids}, label=SYNTHETIC_SIGNAL)


def signal_variant(
    p: ParentOrder,
    name: str,
    source,
    *,
    urgency: AlphaUrgencyPolicy,
    pacing: PacingParams,
    ac_params,
) -> ParentOrder | None:
    """``p`` worked under policy ``name``; ``None`` when the variant does not
    apply to this parent's algo (AC variants are IS-only)."""
    base, variant = parse_policy(name)
    p = replace(p, policy=POLICIES[base])
    if variant is None:
        return p
    if variant == "urgency":
        return replace(p, signal_source=source, urgency_policy="alpha", alpha_urgency=urgency)
    if variant == "pacing":
        if p.algo == AlgoType.POV:
            return replace(p, signal_source=source, pov_signal_pacing=True, pacing=pacing)
        return replace(p, signal_source=source, slice_signal_tilt=True, pacing=pacing)
    if p.algo != AlgoType.IS or ac_params is None:
        return None
    p = replace(p, is_model=ISModel.ALMGREN_CHRISS, ac_params=ac_params)
    if variant == "ac":
        return p
    return replace(p, signal_source=source, ac_signal_drift=True, pacing=pacing)


def cut_windows(first_ts: int, last_ts: int, window_ns: int, warmup_ns: int) -> list[int]:
    """Window start times: back-to-back from ``first + warmup`` while the
    window ends before ``last - warmup``."""
    out: list[int] = []
    start = first_ts + warmup_ns
    while start + window_ns <= last_ts - warmup_ns:
        out.append(start)
        start += window_ns
    return out


def make_parents(
    first_ts: int,
    last_ts: int,
    instrument_id: int,
    adv: float,
    sizes_pct: tuple[float, ...],
    algos: tuple[AlgoType, ...],
    session_index: int,
    rng: SplitMix64,
    *,
    window_ns: int,
    warmup_ns: int,
    slices: int,
    participation: float,
    max_child_qty: int,
    urgency: float,
) -> list[tuple[ParentOrder, float]]:
    """The parents of one (session, instrument) stream with their size cell.

    ``first_ts`` / ``last_ts`` bound the windows (the first and last
    two-sided market state of the session, so every window lies inside the
    TCA timeline). Cells are ``sizes x algos`` (size outer); window ``k`` of session ``s``
    gets cell ``(k + s) % n_cells`` so every cell visits every slot of the
    day across sessions; the side comes from ``rng`` (one SplitMix64 stream
    per run, in stream order, so the draw sequence is pinned by the seed).
    """
    cells = [(pct, algo) for pct in sizes_pct for algo in algos]
    out: list[tuple[ParentOrder, float]] = []
    starts = cut_windows(first_ts, last_ts, window_ns, warmup_ns)
    for k, start in enumerate(starts):
        pct, algo = cells[(k + session_index) % len(cells)]
        side = rng.randint(0, 1)
        out.append(
            (
                ParentOrder(
                    parent_id=k + 1,
                    instrument_id=instrument_id,
                    venue_id=0,
                    side=side,
                    qty=parent_qty(adv, pct),
                    algo=algo,
                    start_ts=start,
                    end_ts=start + window_ns,
                    slices=slices,
                    participation=participation,
                    risk_aversion=1.0,
                    max_child_qty=max_child_qty,
                    urgency=urgency,
                ),
                pct,
            )
        )
    return out


# --------------------------------------------------------------- the run --


def _session_files(data_dir: Path) -> list[Path]:
    for sub in (data_dir / "normalized", data_dir):
        iap1 = sorted(sub.glob("eq_*.normalized.iap1"))
        jsonl = sorted(sub.glob("eq_*.normalized.jsonl"))
        if iap1 or jsonl:
            # Prefer the binary codec where both exist (same events, faster).
            by_stem = {p.name.rsplit(".", 1)[0]: p for p in jsonl}
            by_stem.update({p.name.rsplit(".", 1)[0]: p for p in iap1})
            return [by_stem[k] for k in sorted(by_stem)]
    raise SystemExit(f"no eq_*.normalized.{{iap1,jsonl}} under {data_dir} (or its normalized/)")


def _read_events(path: Path):
    return read_iap1(path) if path.suffix == ".iap1" else read_jsonl(path)


def _session_label(path: Path) -> str:
    return path.name.split(".")[0].removeprefix("eq_")


def _symbols(config_dir: Path) -> dict[int, str]:
    doc = json.loads((config_dir / "instruments" / "instruments.json").read_text(encoding="utf-8"))
    return {
        int(r["instrument_id"]): str(r.get("symbol", r["instrument_id"]))
        for r in doc["instruments"]
    }


def _bps(row: dict[str, float], key: str) -> float:
    return 1e4 * row[key] / row["notional"]


def _cell_summary(rows: list[dict], seed: int) -> dict[str, object]:
    clusters = [r["session"] for r in rows]
    qty = sum(r["qty"] for r in rows)
    filled = sum(r["filled_qty"] for r in rows)
    maker = sum(r["maker_qty"] for r in rows)
    prov: dict[str, int] = {}
    urg: dict[str, int] = {}
    urg_qty: dict[str, int] = {}
    for r in rows:
        for k, v in r["provenance_qty"].items():
            prov[k] = prov.get(k, 0) + v
        for k, v in r.get("urgency_children", {}).items():
            urg[k] = urg.get(k, 0) + v
        for k, v in r.get("urgency_qty", {}).items():
            urg_qty[k] = urg_qty.get(k, 0) + v
    n_children = sum(urg.values())
    n_urg_qty = sum(urg_qty.values())
    out: dict[str, object] = {
        "n_parents": len(rows),
        "n_sessions": len(set(clusters)),
        "qty": qty,
        "filled_qty": filled,
        "fill_rate": filled / qty if qty else None,
        "maker_share_of_filled": maker / filled if filled else None,
        "depth_exhausted": {
            "parents": sum(1 for r in rows if r["depth_exhausted"] > 0),
            "parent_share": sum(1 for r in rows if r["depth_exhausted"] > 0) / len(rows),
            "children": sum(r["depth_exhausted"] for r in rows),
            "qty": sum(r["depth_exhausted_qty"] for r in rows),
            "qty_share_of_unfilled": (
                sum(r["depth_exhausted_qty"] for r in rows) / (qty - filled)
                if qty > filled
                else None
            ),
        },
        "provenance_share_of_filled": {k: prov[k] / filled for k in sorted(prov)} if filled else {},
        # v1.17 B2 / B3: why the children got their urgency (empty for a plain cell)
        "urgency_share_of_children": (
            {k: urg[k] / n_children for k in sorted(urg)} if n_children else {}
        ),
        "urgency_share_of_qty": (
            {k: urg_qty[k] / n_urg_qty for k in sorted(urg_qty)} if n_urg_qty else {}
        ),
        "costs_bps": {
            key: cluster_mean_stats([_bps(r, key) for r in rows], clusters, seed=seed)
            for key in REPORT_COSTS
        },
    }
    return out


def _paired_summary(base: list[dict], other: list[dict], seed: int) -> dict[str, object]:
    """``other - base`` per parent (same windows, same order), bps, clustered."""
    keyed = {(r["session"], r["symbol"], r["window"]): r for r in base}
    diffs: dict[str, list[float]] = {k: [] for k in REPORT_COSTS}
    clusters: list[str] = []
    fill_diff: list[float] = []
    for r in other:
        b = keyed.get((r["session"], r["symbol"], r["window"]))
        if b is None:
            continue
        clusters.append(r["session"])
        fill_diff.append(r["filled_qty"] / r["qty"] - b["filled_qty"] / b["qty"])
        for k in REPORT_COSTS:
            diffs[k].append(_bps(r, k) - _bps(b, k))
    return {
        "n_pairs": len(clusters),
        "fill_rate_diff": cluster_mean_stats(fill_diff, clusters, seed=seed),
        "costs_bps": {k: cluster_mean_stats(v, clusters, seed=seed) for k, v in diffs.items()},
    }


def run_benchmark(
    data_dir: Path,
    *,
    config_dir: Path | None = None,
    sessions: tuple[str, ...] = (),
    symbols: tuple[str, ...] = (),
    algos: tuple[str, ...] = DEFAULT_ALGOS,
    policies: tuple[str, ...] = tuple(POLICIES),
    sizes_pct: tuple[float, ...] = DEFAULT_SIZES_PCT,
    window_min: float = 30.0,
    warmup_min: float = 5.0,
    slices: int = 5,
    participation: float = 0.10,
    max_child_qty: int = 1000,
    urgency: float = 0.5,
    seed: int = 20260829,
    impact_model: str = "child",
    impact_exponent: float = 1.0,
    walk_depth_levels: int = 10,
    baseline_policy: str = "native",
    max_events: int = 0,
    allow_holdout: bool = False,
    timestamp: bool = True,
    signal: str = "none",
    signal_features: Path | None = None,
    signal_params: Path | None = None,
    signal_refit: bool = False,
    signal_threshold_z: float = 1.0,
    signal_passive_urgency: float = 0.1,
    signal_gain: float = 0.5,
    signal_max_age_s: float = 10.0,
    ac_risk_aversion: float = 1e-6,
    ac_daily_vol: float = 0.015,
) -> dict[str, object]:
    """Run the grid; returns the JSON document (see the module docstring).

    ``signal`` names the B2 / B3 signal source: ``"none"`` (no variant
    policy allowed), ``"synthetic"`` or an alpha id. The ``signal_*`` knobs
    are the thresholds in signal units (``AlphaUrgencyPolicy`` /
    ``PacingParams``); ``ac_*`` build the Almgren-Chriss parameters of the
    ``ac`` / ``ac_drift`` variants from the simulator's impact slope
    (``sigma = price * ac_daily_vol`` per sqrt(trading day), ADV from the
    instrument spec, horizon = the window).
    """
    data_dir = Path(data_dir)
    if "holdout" in str(data_dir).lower() and not allow_holdout:
        raise SystemExit(
            f"{data_dir}: looks like a holdout dataset; the benchmark is in-sample only "
            "(--allow-holdout overrides, for a signed pre-registered run)"
        )
    if config_dir is None:
        config_dir = data_dir / "configs"
        if not config_dir.is_dir():
            config_dir = Path(__file__).resolve().parents[4] / "configs"
    config_dir = Path(config_dir)
    for pol in policies:
        parse_policy(pol)
    if baseline_policy not in policies:
        raise SystemExit(f"baseline policy {baseline_policy!r} is not in --policies")
    if signal == "none" and any(parse_policy(pol)[1] for pol in policies):
        raise SystemExit("signal-aware policies need --signal (an alpha id or 'synthetic')")
    urgency_policy = AlphaUrgencyPolicy(
        threshold_z=signal_threshold_z,
        passive_urgency=signal_passive_urgency,
        max_age_ns=int(signal_max_age_s * SEC),
    )
    pacing_params = PacingParams(gain=signal_gain, max_age_ns=int(signal_max_age_s * SEC))
    algo_types = tuple(AlgoType[a] for a in algos)
    cfg = load_exec_config(config_dir)
    cfg = replace(
        cfg,
        seed=seed,
        impact_model=impact_model,
        impact_exponent=impact_exponent if impact_model == "parent" else 1.0,
        walk_depth_levels=walk_depth_levels,
    )
    sor = load_sor_options(config_dir / "execution" / "execution.json")
    taker_fee = cfg.venue(sorted(cfg.venues)[0]).taker_fee_per_share
    names = _symbols(config_dir)
    want_symbols = set(symbols)
    files = [
        p
        for p in _session_files(data_dir)
        if not sessions or any(s in _session_label(p) for s in sessions)
    ]
    if not files:
        raise SystemExit(f"no session of {data_dir} matches --sessions {sessions}")
    window_ns = int(window_min * 60 * SEC)
    warmup_ns = int(warmup_min * 60 * SEC)
    rng = SplitMix64(seed)
    rows: dict[str, list[dict]] = {p: [] for p in policies}
    session_labels: list[str] = []
    n_events = 0
    source = None
    signal_doc: dict[str, object] = {"source": "none"}
    if signal not in ("none", SYNTHETIC_SIGNAL):
        source = AlphaUrgencySource.from_dataset(
            signal,
            signal_features or (data_dir / "features"),
            params_path=signal_params,
            instruments_json=config_dir / "instruments" / "instruments.json",
            refit=signal_refit,
        )
        signal_doc = source.describe()
    elif signal == SYNTHETIC_SIGNAL:
        signal_doc = {
            "source": SYNTHETIC_SIGNAL,
            "period_s": SYNTHETIC_PERIOD_S,
            "amplitude_z": SYNTHETIC_AMPLITUDE_Z,
            "note": "deterministic sine wave in signal units (tests only, no alpha)",
        }
    if signal != "none":
        signal_doc.update(
            {
                "threshold_z": signal_threshold_z,
                "passive_urgency": signal_passive_urgency,
                "pacing_gain": signal_gain,
                "pacing_min_scale": pacing_params.min_scale,
                "pacing_max_scale": pacing_params.max_scale,
                "max_age_s": signal_max_age_s,
                "ac_risk_aversion": ac_risk_aversion,
                "ac_daily_vol": ac_daily_vol,
                "units": "z-score of the alpha's expected_return per instrument on this dataset",
            }
        )
    for s_index, path in enumerate(files):
        label = _session_label(path)
        session_labels.append(label)
        events = _read_events(path)
        if max_events > 0:
            events = events[:max_events]
        n_events += len(events)
        per_instrument: dict[int, list] = {}
        for ev in events:
            per_instrument.setdefault(ev.instrument_id, []).append(ev)
        for iid in sorted(per_instrument):
            sym = names.get(iid, str(iid))
            if want_symbols and sym not in want_symbols:
                continue
            evs = per_instrument[iid]
            ins = cfg.instrument(iid)
            timeline = build_gated_timeline(evs, iid, ins.tick_size)
            if not timeline.ts:
                continue  # never two-sided: nothing to benchmark
            parents = make_parents(
                timeline.ts[0],
                timeline.ts[-1],
                iid,
                ins.adv,
                sizes_pct,
                algo_types,
                s_index,
                rng,
                window_ns=window_ns,
                warmup_ns=warmup_ns,
                slices=slices,
                participation=participation,
                max_child_qty=max_child_qty,
                urgency=urgency,
            )
            if not parents:
                continue
            src = source
            if signal == SYNTHETIC_SIGNAL:
                src = synthetic_signal([iid], timeline.ts[0], timeline.ts[-1])
            for pol in policies:
                worked = []
                cells_of = []
                for p, pct in parents:
                    acp = None
                    if parse_policy(pol)[1] in ("ac", "ac_drift") and p.algo == AlgoType.IS:
                        price = timeline.mid_at(p.start_ts)
                        if math.isfinite(price) and price > 0:
                            acp = ac_params_from_calibration(
                                sigma=price * ac_daily_vol,
                                adv_per_day=ins.adv,
                                price=price,
                                horizon_s=window_ns / SEC,
                                risk_aversion=ac_risk_aversion,
                                config=cfg,
                            )
                    q = signal_variant(
                        p, pol, src, urgency=urgency_policy, pacing=pacing_params, ac_params=acp
                    )
                    if q is not None:
                        worked.append(q)
                        cells_of.append((p, pct))
                if not worked:
                    continue
                res = ExecutionReplay(cfg, worked, sor).run(evs)
                fills_of: dict[int, list] = {p.parent_id: [] for p in worked}
                for f in res.fills:
                    fills_of[f.parent_id].append(f)
                for (p0, pct), p in zip(cells_of, worked, strict=True):
                    row = parent_costs(p, fills_of[p.parent_id], timeline, ins.tick_size, taker_fee)
                    rep = res.parents[p.parent_id]
                    row.update(
                        {
                            "session": label,
                            "symbol": sym,
                            "instrument_id": iid,
                            "window": p0.parent_id,
                            "algo": p.algo.name,
                            "size_pct_adv": pct,
                            "policy": pol,
                            "side": p.side,
                            "children": rep.children,
                            "depth_exhausted": rep.depth_exhausted,
                            "depth_exhausted_qty": rep.depth_exhausted_qty,
                            "provenance_qty": rep.provenance_qty,
                            "urgency_children": rep.urgency_children,
                            "urgency_qty": rep.urgency_qty,
                            "sor_no_route": res.sor_no_route,
                        }
                    )
                    rows[pol].append(row)
    cells: dict[str, object] = {}
    by_symbol: dict[str, object] = {}
    for pol in policies:
        for algo in algos:
            for pct in sizes_pct:
                sel = [r for r in rows[pol] if r["algo"] == algo and r["size_pct_adv"] == pct]
                if not sel:
                    continue
                key = f"{pol}|{algo}|{pct:g}"
                cells[key] = _cell_summary(sel, seed)
                for sym in sorted({r["symbol"] for r in sel}):
                    by_symbol[f"{key}|{sym}"] = _cell_summary(
                        [r for r in sel if r["symbol"] == sym], seed
                    )
    paired: dict[str, object] = {}
    for pol in policies:
        controls = [] if pol == baseline_policy else [baseline_policy]
        ctrl = control_policy(pol)
        if ctrl is not None and ctrl in rows and ctrl not in controls and ctrl != pol:
            controls.append(ctrl)
        for control in controls:
            for algo in algos:
                for pct in sizes_pct:
                    base = [
                        r for r in rows[control] if r["algo"] == algo and r["size_pct_adv"] == pct
                    ]
                    other = [r for r in rows[pol] if r["algo"] == algo and r["size_pct_adv"] == pct]
                    if base and other:
                        paired[f"{pol}-{control}|{algo}|{pct:g}"] = _paired_summary(
                            base, other, seed
                        )
    doc: dict[str, object] = {
        "schema": "iap.execution_benchmark/v1",
        "item": "v1.17 WS-B B1",
        "data_dir": str(data_dir),
        "config_dir": str(config_dir),
        "sessions": session_labels,
        "n_events": n_events,
        "grid": {
            "algos": list(algos),
            "policies": list(policies),
            "sizes_pct_adv": list(sizes_pct),
            "window_min": window_min,
            "warmup_min": warmup_min,
            "slices": slices,
            "pov_participation": participation,
            "max_child_qty": max_child_qty,
            "urgency": urgency,
            "seed": seed,
            "baseline_policy": baseline_policy,
            "max_events": max_events,
        },
        "simulator": {
            "impact_model": impact_model,
            "impact_exponent": cfg.impact_exponent,
            "walk_depth_levels": walk_depth_levels,
            "cost_label": cfg.impact_label(),
            "note": "costs are replay + adder: simulated fills never move the replayed book",
        },
        "inference": {
            "clusters": "one per session (trading day)",
            "se": "cluster-robust sandwich of the per-parent mean (CR1), Student-t with G-1 df",
            "bootstrap": "cluster bootstrap over sessions, 2000 draws, seeded",
            "low_power_below_sessions": BOOTSTRAP_LOW_POWER_DAYS,
        },
        "provenances": list(FILL_PROVENANCES),
        "urgency_regimes": [*URGENCY_REGIMES, "plain"],
        "signal": signal_doc,
        "n_parents": {pol: len(rows[pol]) for pol in policies},
        "cells": cells,
        "cells_by_symbol": by_symbol,
        "paired_vs_baseline": paired,
    }
    if timestamp:
        doc["generated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return doc


# ------------------------------------------------------------- rendering --


def _f(v, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "n/a"
    return f"{v:.{digits}f}"


def _stat(s: dict) -> str:
    if s["mean"] is None:
        return "n/a"
    if s["se"] is None:
        return f"{_f(s['mean'])} (1 session)"
    lo, hi = s["ci95"]
    return f"{_f(s['mean'])} ± {_f(s['se'])} [{_f(lo)}, {_f(hi)}]"


def render(doc: dict) -> str:
    sim = doc["simulator"]
    g = doc["grid"]
    lines = [
        "# Implementation-shortfall benchmark (v1.17 B1)",
        "",
        f"Dataset `{doc['data_dir']}`, sessions {', '.join(doc['sessions'])} "
        f"({doc['n_events']} events). Every cost below is **{sim['cost_label']}** — "
        f"{sim['note']}. Walk depth {sim['walk_depth_levels']} levels.",
        "",
        f"Grid: algos {', '.join(g['algos'])}; policies {', '.join(g['policies'])}; "
        f"parent sizes {', '.join(f'{x:g}' for x in g['sizes_pct_adv'])} % of ADV; "
        f"windows of {g['window_min']:g} min after {g['warmup_min']:g} min warm-up, "
        f"{g['slices']} slices, POV {g['pov_participation']:.0%}, seed {g['seed']}.",
        "",
        f"Per-parent means in bps of the arrival notional, ± day-clustered SE "
        f"[95 % Student-t CI, {doc['inference']['se']}]. Cells with fewer than "
        f"{doc['inference']['low_power_below_sessions']} sessions are low-power.",
        "",
    ]
    sig = doc.get("signal", {"source": "none"})
    if sig.get("source", "none") != "none":
        lines += [
            f"Signal-aware arms (v1.17 B2 / B3): source `{sig['source']}` "
            f"({sig.get('units', 'signal units')}); urgency threshold |z| ≥ "
            f"{sig['threshold_z']:g}, agree posts at urgency {sig['passive_urgency']:g}; "
            f"pacing scale = clip(1 − {sig['pacing_gain']:g} · alignment, "
            f"{sig['pacing_min_scale']:g}, {sig['pacing_max_scale']:g}); readings older than "
            f"{sig['max_age_s']:g} s are stale. A `base+variant` policy works the SAME windows as "
            "`base` and pairs with it below; `ac_drift` pairs with `ac`.",
            "",
        ]
    lines += [
        "## Cells (pooled over symbols)",
        "",
        "| policy | algo | size % ADV | n | sessions | all-in | shortfall | spread | fees | sim impact | completion | fill rate | maker share | depth-exhausted parents | depth-exhausted qty share of unfilled | provenance shares of filled | urgency shares of children |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, c in doc["cells"].items():
        pol, algo, pct = key.split("|")
        cb = c["costs_bps"]
        de = c["depth_exhausted"]
        prov = ", ".join(f"{k} {v:.0%}" for k, v in c["provenance_share_of_filled"].items())
        urg = ", ".join(f"{k} {v:.0%}" for k, v in c.get("urgency_share_of_children", {}).items())
        lines.append(
            f"| {pol} | {algo} | {pct} | {c['n_parents']} | {c['n_sessions']} | "
            f"{_stat(cb['all_in'])} | {_stat(cb['shortfall'])} | {_stat(cb['spread'])} | "
            f"{_stat(cb['fees_net'])} | {_stat(cb['sim_impact'])} | {_stat(cb['completion'])} | "
            f"{_f(c['fill_rate'], 3)} | {_f(c['maker_share_of_filled'], 3)} | "
            f"{de['parents']} ({de['parent_share']:.0%}) | {_f(de['qty_share_of_unfilled'], 3)} | "
            f"{prov or 'none'} | {urg or '—'} |"
        )
    if doc["paired_vs_baseline"]:
        lines += [
            "",
            f"## Paired (same windows; `other − control`, bps; baseline `{g['baseline_policy']}`, "
            "signal-aware arms also against their own plain control)",
            "",
            "| comparison | algo | size % ADV | pairs | all-in diff | shortfall diff | completion diff | fill-rate diff |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for key, p in doc["paired_vs_baseline"].items():
            cmp, algo, pct = key.split("|")
            lines.append(
                f"| {cmp} | {algo} | {pct} | {p['n_pairs']} | {_stat(p['costs_bps']['all_in'])} | "
                f"{_stat(p['costs_bps']['shortfall'])} | {_stat(p['costs_bps']['completion'])} | "
                f"{_stat(p['fill_rate_diff'])} |"
            )
    lines += [
        "",
        "## Reading it",
        "",
        "- `all_in = shortfall + fees_net + sim_impact + completion` per parent; the unfilled "
        "quantity is priced at the mid move and the cost of crossing at the window end, so not "
        "trading is never free.",
        "- `sim_impact` is the configured adder (" + sim["cost_label"] + "), not a measured "
        "reaction; the replayed book does not move for our fills.",
        "- `depth-exhausted` (X-5): aggressive children cancelled because the walk ran out of "
        "displayed depth within the configured levels — the silent under-fill on thin names.",
        "- provenance (X-7): which simulator rule produced the filled quantity "
        "(aggressive_walk, exec_print, marketable_add, hidden_print, cross, reopen_touch).",
        "- Standard errors cluster by session; with few sessions the Student-t CI is wide by "
        "design and `low_power` is set in the JSON.",
        "- urgency shares (B2 / B3): why each child of a signal-aware parent got its urgency "
        "(agree = posted on a favourable signal, adverse = crossed on an adverse one, neutral = "
        "inside the threshold, no_signal / stale = no usable reading, plain = a pacing-only arm).",
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------- prereg --


def prereg_draft(doc: dict) -> dict[str, object]:
    """The UNSIGNED pre-registration draft of the B7 gate on this grid."""
    g = doc["grid"]
    return {
        "schema": "iap.execution_benchmark_prereg/v1",
        "id": "B1-IS-BENCHMARK-1",
        "status": "DRAFT - not signed, not on the blackboard, not approved for any fresh-day run",
        "signed": False,
        "signature": None,
        "on_blackboard": False,
        "source": "IAP_v1.17_Plan.md section 6, items B1 and B7",
        "in_sample_sessions": doc["sessions"],
        "holdout": "data/real/r7_holdout and the 2026 holdout are never read by this benchmark; "
        "the signed test runs once on the IEX fresh days and the Nasdaq/IEX overlap days (B7)",
        "grid": {
            "algos": g["algos"],
            "policies": g["policies"],
            "sizes_pct_adv": g["sizes_pct_adv"],
            "window_min": g["window_min"],
            "warmup_min": g["warmup_min"],
            "slices": g["slices"],
            "pov_participation": g["pov_participation"],
            "max_child_qty": g["max_child_qty"],
            "urgency": g["urgency"],
            "seed": g["seed"],
        },
        "simulator": doc["simulator"],
        "signal": doc.get("signal", {"source": "none"}),
        "expected_effect": "to be filled from the in-sample run before signing (B2/B3)",
        "primary_metric": "all_in bps per parent (shortfall + fees_net + sim_impact + completion), "
        "labelled " + doc["simulator"]["cost_label"],
        "gate_B7": {
            "comparison": "signal-aware algo (B2 urgency / B3 pacing) vs the plain algo of the "
            "same name on the SAME windows, same policy, same size cell",
            "statistic": "paired per-parent difference signal-aware - plain in all_in bps, "
            "mean with day-clustered SE (one cluster per session), Student-t with G - 1 df",
            "direction": "one-sided: signal-aware cheaper (difference < 0)",
            "alpha": 0.05,
            "minimum_effect_bps": 2.0,
            "fill_rate_condition": "signal-aware fill rate not lower than plain by more than "
            "0.02 (paired, same clustering)",
            "multiplicity": "Holm over the four algos; a cell passes only if both the cost and "
            "the fill-rate conditions hold",
            "eligibility": "a cell whose depth_exhausted parent share exceeds 0.25 under either "
            "arm is reported but not gate-eligible (its cost is the walk cap, not the algo)",
            "cost_basis": "the signed run uses configs/prereg/v2.json realism opt-ins and "
            "impact_model=parent with the fitted exponent; the exponent and its fit support "
            "are recorded here by fingerprint before signing, never read at run time",
            "report_always": [
                "depth_exhausted parent share and qty share of unfilled",
                "fill provenance shares",
                "per-symbol cells",
                "low_power flag when sessions < "
                + str(doc["inference"]["low_power_below_sessions"]),
            ],
            "no_look": "the fresh days are not read until this draft is signed and registered "
            "(tools/*_register.py, --require-ci-env)",
        },
    }


# ---------------------------------------------------------------- main --


def _csv(s: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in s.split(",") if x.strip())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--data-dir", required=True, type=Path)
    ap.add_argument("--config-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--tag", default=None, help="file name tag (default: the data dir name)")
    ap.add_argument(
        "--sessions", default="", help="comma list of session substrings (e.g. 20190130)"
    )
    ap.add_argument("--symbols", default="", help="comma list of symbols")
    ap.add_argument("--algos", default=",".join(DEFAULT_ALGOS))
    ap.add_argument("--policies", default=",".join(POLICIES))
    ap.add_argument("--sizes-pct", default=",".join(f"{x:g}" for x in DEFAULT_SIZES_PCT))
    ap.add_argument("--window-min", type=float, default=30.0)
    ap.add_argument("--warmup-min", type=float, default=5.0)
    ap.add_argument("--slices", type=int, default=5)
    ap.add_argument("--pov-participation", type=float, default=0.10)
    ap.add_argument("--max-child-qty", type=int, default=1000)
    ap.add_argument("--urgency", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=20260829)
    ap.add_argument("--impact-model", choices=("child", "parent"), default="child")
    ap.add_argument("--impact-exponent", type=float, default=1.0)
    ap.add_argument("--walk-depth", type=int, default=10)
    ap.add_argument("--baseline-policy", default="native")
    ap.add_argument("--max-events", type=int, default=0, help="per session (smoke runs / tests)")
    ap.add_argument("--allow-holdout", action="store_true")
    ap.add_argument("--no-timestamp", action="store_true")
    ap.add_argument("--write-prereg-draft", action="store_true")
    sg = ap.add_argument_group("signal-aware arms (v1.17 B2 / B3)")
    sg.add_argument(
        "--signal",
        default="none",
        help="'none', 'synthetic' or an alpha id (EQ10, EQ05, ...) scored on --signal-features",
    )
    sg.add_argument(
        "--signal-features", type=Path, default=None, help="default <data-dir>/features"
    )
    sg.add_argument(
        "--signal-params",
        type=Path,
        default=None,
        help="default configs/strategies/alpha_params.json",
    )
    sg.add_argument(
        "--signal-refit", action="store_true", help="fit the alpha in-sample (labelled)"
    )
    sg.add_argument("--signal-threshold-z", type=float, default=1.0)
    sg.add_argument("--signal-passive-urgency", type=float, default=0.1)
    sg.add_argument("--signal-gain", type=float, default=0.5)
    sg.add_argument("--signal-max-age-s", type=float, default=10.0)
    sg.add_argument("--ac-risk-aversion", type=float, default=1e-6)
    sg.add_argument("--ac-daily-vol", type=float, default=0.015)
    args = ap.parse_args(argv)
    doc = run_benchmark(
        args.data_dir,
        config_dir=args.config_dir,
        sessions=_csv(args.sessions),
        symbols=_csv(args.symbols),
        algos=_csv(args.algos),
        policies=_csv(args.policies),
        sizes_pct=tuple(float(x) for x in _csv(args.sizes_pct)),
        window_min=args.window_min,
        warmup_min=args.warmup_min,
        slices=args.slices,
        participation=args.pov_participation,
        max_child_qty=args.max_child_qty,
        urgency=args.urgency,
        seed=args.seed,
        impact_model=args.impact_model,
        impact_exponent=args.impact_exponent,
        walk_depth_levels=args.walk_depth,
        baseline_policy=args.baseline_policy,
        max_events=args.max_events,
        allow_holdout=args.allow_holdout,
        timestamp=not args.no_timestamp,
        signal=args.signal,
        signal_features=args.signal_features,
        signal_params=args.signal_params,
        signal_refit=args.signal_refit,
        signal_threshold_z=args.signal_threshold_z,
        signal_passive_urgency=args.signal_passive_urgency,
        signal_gain=args.signal_gain,
        signal_max_age_s=args.signal_max_age_s,
        ac_risk_aversion=args.ac_risk_aversion,
        ac_daily_vol=args.ac_daily_vol,
    )
    tag = args.tag or Path(args.data_dir).name
    args.out.mkdir(parents=True, exist_ok=True)
    js = args.out / f"benchmark_{tag}.json"
    md = args.out / f"BENCHMARK_{tag}.md"
    js.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    md.write_text(render(doc), encoding="utf-8", newline="\n")
    print(f"wrote {js}\nwrote {md}")
    if args.write_prereg_draft:
        pd = args.out / PREREG_DRAFT_NAME
        pd.write_text(
            json.dumps(prereg_draft(doc), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"wrote {pd} (DRAFT, unsigned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
