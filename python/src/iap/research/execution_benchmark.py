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

Holdout discipline: every data directory is checked against the hash-locked
holdout registry (REL-02) before it is read; the benchmark is an in-sample
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

**Quote fading and queue-aware cancels (v1.17 B4 / B5).** Three more
variants on the ``passive`` base (they act on posted children):
``passive+fade`` pulls a resting child when the signal turns adverse by
``--fade-threshold-z`` (needs ``--signal``) or the opposite-side book
pressure reaches ``--fade-pressure`` (works without a signal), and holds /
re-posts / crosses the remainder per ``--fade-action``;
``passive+qcancel`` cancels a resting child whose queue is hopeless
(``--qcancel-max-ahead`` shares ahead, or hazard ``P(fill within
--qcancel-horizon-s) < --qcancel-min-pfill`` with ``--qcancel-hazard`` and
the X-4 execution depletion rate of ``--qcancel-calibration``) and crosses
/ re-posts it per ``--qcancel-action``; ``passive+fade_qcancel`` runs both.
Each pairs with plain ``passive`` on the same windows. The report adds, per
policy, the **fades saved vs fills missed** counterfactual
(:func:`iap.execution.fading.pull_summary`: for every pulled quantity,
whether the market traded through the pulled price inside the horizon and
the markout of the fill the pull forwent, split into adverse fills avoided
and favourable fills missed; labelled a proxy) and the **markout
decomposition** by fill type (passive / aggressive / faded /
cancelled-replaced) and B2 regime with day-clustered standard errors
(v1.17 B6, :mod:`iap.tca.markout_decomposition`), at ``--markout-horizons``.

**Dark venue (v1.17 B10).** ``--venues <venues.json>`` replaces the config
directory's venue table (so a dataset's single lit venue can be joined by
a ``"venue_type": "dark"`` entry); the ``dark`` variant on any base
(``native+dark``, ``passive+dark``, ...) works the same parents with
``ParentOrder.dark_first`` (:mod:`iap.execution.dark`): every step of a
low-urgency parent (``urgency <= --dark-max-urgency``; for
``urgency_policy="alpha"`` parents the B2 regimes in ``--dark-regimes``)
is posted to the dark venue first under the pre-trade controls
(``--dark-max-participation`` of the trailing displayed volume,
``--dark-max-notional`` per parent, venue-type restriction) and the
unfilled remainder goes to the lit book after ``--dark-horizon-s``. Dark
fills come from ``--dark-model`` (a ``DarkFillModel`` document fitted on
the hidden-print proxy by ``python -m iap.execution.dark``; without it the
documented prior, labelled as such) and are **modelled, not measured**: the
report says so wherever it quotes them. Each dark policy pairs with its
base on the same windows; fill type ``dark`` appears in the markout
decomposition (its negated price impact is the adverse selection of the
dark fills). B8 pairs the same variant router-vs-pinned on the Nasdaq /
IEX overlap days (``--venues`` with both lit venues plus the dark one).
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
    FadeParams,
    FillHazardModel,
    ISModel,
    PacingParams,
    ParentOrder,
    QueueCancelParams,
    load_exec_config,
    load_sor_options,
)
from iap.execution.calibration import load_calibration
from iap.execution.child_exec import ChildExecParams
from iap.execution.config import load_venues
from iap.execution.dark import (
    DARK_CONTROL_CODES,
    MODELLED_LABEL,
    DarkControls,
    DarkFillModel,
    DarkFirstParams,
)
from iap.execution.fading import CANCEL_RULES, pull_rows, pull_summary
from iap.execution.optimal import ac_params_from_calibration
from iap.execution.signal import URGENCY_REGIMES, AlphaUrgencySource
from iap.execution.types import FILL_PROVENANCES
from iap.research import execution_metrics as exm
from iap.research.two_venue import (
    ALIGNMENT_RULE,
    IEX_CLOCKS,
    ROUTE_POLICIES,
    SKEW_QUALIFICATION,
    instrument_map,
    load_calibrations,
    merge_events,
    read_events,
    remap_instruments,
    router_from_calibrations,
    symbols_of,
)
from iap.tca.markout import DEFAULT_HORIZONS_NS, build_gated_timeline
from iap.tca.markout_decomposition import FILL_TYPES, decompose, fill_rows, fill_type
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
    "fade": None,  #: B4: quote fading on posted children (passive base only)
    "qcancel": None,  #: B5: queue-aware cancels on posted children (passive base only)
    "fade_qcancel": None,  #: B4 + B5 together (passive base only)
    "dark": None,  #: B10: dark-first posting on any base (needs a dark venue)
    "capped": None,  #: REL-04: IS / POV / aggressive children capped at eligible depth
    "peg": None,  #: REL-04: midpoint-peg children (needs --hidden-prints to fill)
}
#: REL-04 child-execution variants: need no signal
CHILD_VARIANTS: tuple[str, ...] = ("capped", "peg")
DARK_VARIANT = "dark"
#: the variants that pull resting children: they need the ``passive`` base
PULL_VARIANTS: tuple[str, ...] = ("fade", "qcancel", "fade_qcancel")
DEFAULT_MARKOUT_HORIZONS: tuple[str, ...] = ("1s", "5s", "30s")
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


def split_route(name: str) -> tuple[str, str | None]:
    """v1.17 B8: ``"passive@sor+dark"`` -> ``("passive+dark", "sor")``; a bare
    route name is the native style (``"pin_iex"`` -> ``("native", "pin_iex")``);
    no route -> ``(name, None)`` (the pinned SmartOrderRouter, unchanged)."""
    head, plus, variant = name.partition("+")
    if "@" in head:
        style, _, route = head.partition("@")
    elif head in ROUTE_POLICIES:
        style, route = "native", head
    else:
        return name, None
    if route not in ROUTE_POLICIES:
        raise SystemExit(
            f"unknown route {route!r} in {name!r}; choose from {sorted(ROUTE_POLICIES)}"
        )
    return style + plus + variant, route


def parse_policy(name: str) -> tuple[str, str | None]:
    """``"native+urgency"`` -> ``("native", "urgency")``; ``"native"`` -> ``("native", None)``.
    A B8 route (``sor`` / ``pin_*``, :func:`split_route`) is stripped first."""
    base, _, variant = split_route(name)[0].partition("+")
    if base not in POLICIES:
        raise SystemExit(f"unknown policy {name!r}; bases are {sorted(POLICIES)}")
    if variant and variant not in VARIANTS:
        raise SystemExit(f"unknown variant {variant!r} in {name!r}; choose from {sorted(VARIANTS)}")
    if variant in PULL_VARIANTS and base != "passive":
        raise SystemExit(f"{name!r}: the {variant} variant acts on posted children (passive base)")
    return base, variant or None


def control_policy(name: str) -> str | None:
    """The control a signal-aware policy pairs with (``None`` for a base policy)."""
    base, variant = parse_policy(name)
    if variant is None:
        return None
    ctrl = VARIANTS[variant]
    route = split_route(name)[1]
    if route is not None:
        base = route if base == "native" else f"{base}@{route}"
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
    fade: FadeParams | None = None,
    queue_cancel: QueueCancelParams | None = None,
    dark: DarkFirstParams | None = None,
    child_exec: ChildExecParams | None = None,
) -> ParentOrder | None:
    """``p`` worked under policy ``name``; ``None`` when the variant does not
    apply to this parent's algo (AC variants are IS-only)."""
    base, variant = parse_policy(name)
    p = replace(p, policy=POLICIES[base])
    if variant is None:
        return p
    if variant == DARK_VARIANT:
        if dark is None:
            raise SystemExit("the dark variant needs a dark venue (--venues) and --dark-model")
        return replace(p, dark_first=dark)
    if variant in CHILD_VARIANTS:
        ce = child_exec or ChildExecParams()
        style = "midpoint_peg" if variant == "peg" else "capped_ioc"
        return replace(p, child_exec=replace(ce, style=style))
    if variant in PULL_VARIANTS:
        if variant in ("fade", "fade_qcancel"):
            if fade is None:
                raise SystemExit("the fade variant needs --fade-threshold-z or --fade-pressure")
            p = replace(p, fade=fade, signal_source=source)
        if variant in ("qcancel", "fade_qcancel"):
            if queue_cancel is None:
                raise SystemExit(
                    "the qcancel variant needs --qcancel-max-ahead or --qcancel-min-pfill"
                )
            p = replace(p, queue_cancel=queue_cancel)
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
    pass_index: int = 0,
    n_passes: int = 1,
    id_offset: int = 0,
    cell_offset: int = 0,
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
    shift = pass_index * window_ns // max(1, n_passes)
    starts = cut_windows(first_ts + shift, last_ts, window_ns, warmup_ns)
    for k, start in enumerate(starts):
        pct, algo = cells[(cell_offset + k + session_index) % len(cells)]
        side = rng.randint(0, 1)
        out.append(
            (
                ParentOrder(
                    parent_id=id_offset + k + 1,
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


MAX_COVER_PASSES = 6


def make_parent_passes(
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
    cover_all_cells: bool = False,
    **kw,
) -> list[list[tuple[ParentOrder, float]]]:
    """The parents of one stream as one or more PASSES (v1.18 REL-03, X-E).

    Without ``cover_all_cells`` (the v1.17 rule, the default) there is one
    pass: back-to-back windows, cell ``(k + session) % n_cells``.  The day
    holds ``(session - 2 * warmup) / window`` windows (12 for a 30-minute
    window on a 6.5 h session) while the grid has ``sizes x algos`` cells
    (16 by default), so some cells never ran on a day.  With
    ``cover_all_cells`` extra passes are added, pass ``j`` starting
    ``j / n_passes`` of a window later, until the day holds at least one
    window per cell; the cell index runs on across passes, so every cell
    runs at least once per day.  Passes overlap in time: each is replayed
    in its own :class:`ExecutionReplay` so parents of different passes never
    interact (every parent still sees the untouched replayed book, as in v1).
    Parent ids are unique within the stream.
    """
    n_cells = len(sizes_pct) * len(algos)
    if not cover_all_cells:
        return [
            make_parents(
                first_ts, last_ts, instrument_id, adv, sizes_pct, algos, session_index, rng,
                window_ns=window_ns, warmup_ns=warmup_ns, **kw,
            )
        ]  # fmt: skip
    n = 1
    while n < MAX_COVER_PASSES:
        total = sum(
            len(cut_windows(first_ts + j * window_ns // n, last_ts, window_ns, warmup_ns))
            for j in range(n)
        )
        if total >= n_cells:
            break
        n += 1
    out: list[list[tuple[ParentOrder, float]]] = []
    done = 0
    for j in range(n):
        ps = make_parents(
            first_ts, last_ts, instrument_id, adv, sizes_pct, algos, session_index, rng,
            window_ns=window_ns, warmup_ns=warmup_ns, pass_index=j, n_passes=n,
            id_offset=done, cell_offset=done, **kw,
        )  # fmt: skip
        done += len(ps)
        out.append(ps)
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


def parent_extras(
    row: dict,
    p: ParentOrder,
    timeline,
    tick: float,
    venue_adv: float,
    consolidated_adv: float | None,
    liq_edges: list[float],
    markout: dict[str, list[float]],
    span: tuple[int, int],
) -> dict[str, object]:
    """v1.18 REL-03 columns of one parent row: the arrival mid, the v2 metric
    inputs (``m_*``), the regime / liquidity labels and the post-trade
    markout accumulators (:mod:`iap.research.execution_metrics`)."""
    mid = timeline.mid_at(p.start_ts)
    r = dict(row)
    r["decision_mid"] = mid
    out: dict[str, object] = {"decision_mid": mid}
    for k, v in exm.parent_metrics(r).items():
        out[f"m_{k}"] = v
    i = timeline.prevailing(p.start_ts)
    out["spread_regime"] = (
        exm.spread_regime(timeline.half_spread(i) / tick) if i is not None else None
    )
    third = max(1, (span[1] - span[0]) // 3)
    out["tod"] = ("open", "mid", "close")[min(2, max(0, (p.start_ts - span[0]) // third))]
    adv = consolidated_adv if consolidated_adv else venue_adv
    out["liquidity"] = exm.liquidity_bucket(adv, liq_edges)
    out["size_pct_venue_adv"] = exm.size_label(p.qty, venue_adv)
    out["size_pct_consolidated_adv"] = exm.size_label(p.qty, consolidated_adv)
    out["markout_w"] = markout
    return out


def _cell_summary(rows: list[dict], seed: int) -> dict[str, object]:
    clusters = [r["session"] for r in rows]
    qty = sum(r["qty"] for r in rows)
    filled = sum(r["filled_qty"] for r in rows)
    maker = sum(r["maker_qty"] for r in rows)
    prov: dict[str, int] = {}
    urg: dict[str, int] = {}
    urg_qty: dict[str, int] = {}
    types: dict[str, int] = {}
    pulls: dict[str, int] = {}
    pulled: dict[str, int] = {}
    for r in rows:
        for k, v in r["provenance_qty"].items():
            prov[k] = prov.get(k, 0) + v
        for k, v in r.get("fill_type_qty", {}).items():
            types[k] = types.get(k, 0) + v
        for k, v in r.get("pulls", {}).items():
            pulls[k] = pulls.get(k, 0) + v
        for k, v in r.get("pulled_qty", {}).items():
            pulled[k] = pulled.get(k, 0) + v
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
        # v1.17 B6: which kind of fill the filled quantity was
        "fill_type_share_of_filled": (
            {k: types[k] / filled for k in FILL_TYPES if k in types} if filled else {}
        ),
        # v1.17 B4 / B5: pulls of resting children by rule (empty for a plain cell)
        "pulls": {
            "children": {k: pulls[k] for k in CANCEL_RULES if k in pulls},
            "pulled_qty": {k: pulled[k] for k in CANCEL_RULES if k in pulled},
            "lost_race": sum(r.get("pulls_lost_race", 0) for r in rows),
            "held_unsent_qty": sum(r.get("held_unsent_qty", 0) for r in rows),
        },
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
    # REL-04: every way a parent under-delivers is a COUNTER here, never silent
    out["failures"] = {
        "incomplete_parents": sum(1 for r in rows if r["filled_qty"] < r["qty"]),
        "incomplete_share": sum(1 for r in rows if r["filled_qty"] < r["qty"]) / len(rows),
        "residual_qty": sum(r["qty"] - r["filled_qty"] for r in rows),
        "depth_exhausted_children": sum(r["depth_exhausted"] for r in rows),
        "cap_no_depth_posts": sum(r.get("cap_no_depth", 0) for r in rows),
        "cap_no_depth_parents": sum(1 for r in rows if r.get("cap_no_depth", 0) > 0),
        "cap_withheld_qty": sum(r.get("cap_withheld_qty", 0) for r in rows),
        "reposts": sum(r.get("reposts", 0) for r in rows),
        "carry_unsent_qty": sum(r.get("carry_unsent_qty", 0) for r in rows),
        "note": "residual is reported, never filled; depth_exhausted is the legacy MARKET "
        "walk running out of depth, cap_no_depth a capped post that found no eligible depth",
    }
    if rows and "m_fill_cost" in rows[0]:
        out["metrics_v2"] = exm.aggregate(rows)
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


def check_data_access(data_dir: Path, *, allow_holdout: bool | str = False) -> None:
    """The single entry-point data-access check of the benchmark: delegates to the
    REL-02 hash-locked holdout registry (``iap.research.holdout_registry``), which
    replaced the v1.17 path-substring rule. A legacy ``allow_holdout`` bool
    authorises nothing; a signed prereg id does."""
    from iap.research.holdout_registry import guard_or_exit

    guard_or_exit(data_dir, "execution_benchmark", allow_holdout)


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
    adv_table: Path | bool | None = None,
    baseline_policy: str = "native",
    max_events: int = 0,
    allow_holdout: bool | str = False,
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
    fade_threshold_z: float = 0.0,
    fade_pressure: float = 0.0,
    fade_action: str = "hold",
    fade_max: int = 3,
    qcancel_max_ahead: int = 0,
    qcancel_min_pfill: float = 0.0,
    qcancel_horizon_s: float = 1.0,
    qcancel_hazard: Path | None = None,
    qcancel_calibration: Path | None = None,
    qcancel_action: str = "cross",
    markout_horizons: tuple[str, ...] = DEFAULT_MARKOUT_HORIZONS,
    venues: Path | None = None,
    dark_venue_id: int = 0,
    dark_model: Path | None = None,
    dark_horizon_s: float = 30.0,
    dark_max_urgency: float = 0.5,
    dark_regimes: tuple[str, ...] = ("agree",),
    dark_max_participation: float = 0.1,
    dark_participation_window_s: float = 30.0,
    dark_max_notional: float = 0.0,
    second_data_dir: Path | None = None,
    iex_clock: str = "exchange",
    venue_calibration: Path | None = None,
    router_tox_horizon: str = "1s",
    router_fill_horizon_s: float = 1.0,
    more_pairs: tuple[tuple[Path, Path], ...] = (),
    cover_all_cells: bool = False,
    consolidated_adv: Path | None = None,
    hidden_prints: bool = False,
    peg_ahead_qty: int = 0,
    md_delay_ns: int = 0,
    venue_inbound_delay_ns: dict[str, int] | None = None,
    child_exec: ChildExecParams | None = None,
    paired_v2: bool = False,
    family_k: int = 1,
) -> dict[str, object]:
    """Run the grid; returns the JSON document (see the module docstring).

    ``second_data_dir`` (v1.17 B8) is the IEX dataset of the same days:
    each session is the merged two-venue stream (``iap.research.two_venue``,
    ``iex_clock`` = its alignment rule; only sessions present in both are
    run) and the venue table is the union of both datasets' venues unless
    ``venues`` is given. Route policies (``sor``, ``pin_nasdaq``,
    ``pin_iex``, or ``style@route``) pin every child to one venue or route
    it through the ``sor_v2`` cost-aware router built from the per-venue
    calibration documents in ``venue_calibration`` (the VenueModel defaults
    without them). ``more_pairs`` adds further ``(Nasdaq dataset, IEX
    dataset)`` day pairs (one session each on the overlap days), re-keyed
    onto ``data_dir``'s instrument ids by symbol; ``data_dir``'s configs
    (instruments, ADV) serve every day.

    ``cover_all_cells`` (v1.18 REL-03, X-E; default off = the v1.17 rotation,
    so committed JSONs reproduce) adds overlapping passes until every grid
    cell runs on every day (:func:`make_parent_passes`).  ``consolidated_adv``
    is a JSON ``{symbol: consolidated daily shares}``: sizes are then also
    labelled in % of consolidated ADV (X-F); the grid itself is still sized
    from the venue ADV in ``instruments.json``.

    REL-04 fidelity (all opt-in, default off): ``hidden_prints`` lets
    non-displayed TRADE prints fill resting children and midpoint pegs;
    ``peg_ahead_qty`` is the modelled midpoint queue; ``md_delay_ns`` delays
    child decisions of the ``capped`` / ``peg`` variants by the market-data
    delay; ``venue_inbound_delay_ns`` maps a venue name to its inbound delay
    (IEX: 350_000, the speed bump); ``child_exec`` holds the cap knobs of the
    ``capped`` / ``peg`` variants (:mod:`iap.execution.child_exec`).

    ``venues`` replaces the venue table; the ``dark_*`` knobs build the B10
    :class:`DarkFirstParams` of the ``dark`` variant (``dark_model`` = a
    fitted ``DarkFillModel`` JSON, else the documented prior).

    ``fade_*`` / ``qcancel_*`` build the B4 :class:`FadeParams` and B5
    :class:`QueueCancelParams` of the ``fade`` / ``qcancel`` variants (a zero
    knob is that leg off; the fade's signal leg needs ``signal``; the
    hazard leg needs ``qcancel_hazard``, a ``FillHazardModel`` JSON, and
    reads the X-4 execution depletion rate from ``qcancel_calibration`` when
    given). ``markout_horizons`` name the B6 decomposition horizons
    (``iap.tca.markout.DEFAULT_HORIZONS_NS`` keys).

    ``signal`` names the B2 / B3 signal source: ``"none"`` (no variant
    policy allowed), ``"synthetic"`` or an alpha id. The ``signal_*`` knobs
    are the thresholds in signal units (``AlphaUrgencyPolicy`` /
    ``PacingParams``); ``ac_*`` build the Almgren-Chriss parameters of the
    ``ac`` / ``ac_drift`` variants from the simulator's impact slope
    (``sigma = price * ac_daily_vol`` per sqrt(trading day), ADV from the
    instrument spec, horizon = the window).
    """
    data_dir = Path(data_dir)
    check_data_access(data_dir, allow_holdout=allow_holdout)
    if config_dir is None:
        config_dir = data_dir / "configs"
        if not config_dir.is_dir():
            config_dir = Path(__file__).resolve().parents[4] / "configs"
    config_dir = Path(config_dir)
    for pol in policies:
        parse_policy(pol)
    if baseline_policy not in policies:
        raise SystemExit(f"baseline policy {baseline_policy!r} is not in --policies")
    if signal == "none" and any(
        parse_policy(pol)[1] not in (None, DARK_VARIANT, *PULL_VARIANTS, *CHILD_VARIANTS)
        for pol in policies
    ):
        raise SystemExit("signal-aware policies need --signal (an alpha id or 'synthetic')")
    pull_variants_used = {parse_policy(pol)[1] for pol in policies} & set(PULL_VARIANTS)
    fade_params: FadeParams | None = None
    if fade_threshold_z > 0.0 or fade_pressure > 0.0:
        if fade_threshold_z > 0.0 and signal == "none":
            raise SystemExit("--fade-threshold-z needs --signal (or use --fade-pressure alone)")
        fade_params = FadeParams(
            threshold_z=fade_threshold_z,
            pressure_threshold=fade_pressure,
            action=fade_action,
            max_fades=fade_max,
            max_age_ns=int(signal_max_age_s * SEC),
        )
    qcancel_params: QueueCancelParams | None = None
    if qcancel_max_ahead > 0 or qcancel_min_pfill > 0.0:
        hazard = None if qcancel_hazard is None else FillHazardModel.load(qcancel_hazard)
        cal = load_calibration(qcancel_calibration)
        dep = None
        if cal is not None:
            dep = cal.queue_exec_depletion_hazard  # X-4: executions only
        qcancel_params = QueueCancelParams(
            max_ahead_qty=qcancel_max_ahead,
            min_p_fill=qcancel_min_pfill,
            horizon_ns=int(qcancel_horizon_s * SEC),
            action=qcancel_action,
            hazard=hazard,
            depletion_per_s=dep,
        )
    for name in markout_horizons:
        if name not in DEFAULT_HORIZONS_NS:
            raise SystemExit(
                f"unknown markout horizon {name!r}; choose from {list(DEFAULT_HORIZONS_NS)}"
            )
    mk_horizons = {h: DEFAULT_HORIZONS_NS[h] for h in markout_horizons}
    pull_doc: dict[str, object] = {
        "variants": sorted(pull_variants_used),
        "fade": None
        if fade_params is None
        else {
            "threshold_z": fade_params.threshold_z,
            "pressure_threshold": fade_params.pressure_threshold,
            "action": fade_params.action,
            "max_fades": fade_params.max_fades,
            "max_age_s": signal_max_age_s,
        },
        "queue_cancel": None
        if qcancel_params is None
        else {
            "max_ahead_qty": qcancel_params.max_ahead_qty,
            "min_p_fill": qcancel_params.min_p_fill,
            "horizon_s": qcancel_horizon_s,
            "action": qcancel_params.action,
            "hazard": None if qcancel_hazard is None else str(qcancel_hazard),
            "hazard_clock": None if qcancel_params.hazard is None else qcancel_params.hazard.clock,
            "depletion_rate": (
                "none (0)" if qcancel_params.depletion_per_s is None else "exec_hazard_per_s (X-4)"
            ),
            "calibration": None if qcancel_calibration is None else str(qcancel_calibration),
        },
        "rules": list(CANCEL_RULES),
    }
    urgency_policy = AlphaUrgencyPolicy(
        threshold_z=signal_threshold_z,
        passive_urgency=signal_passive_urgency,
        max_age_ns=int(signal_max_age_s * SEC),
    )
    pacing_params = PacingParams(gain=signal_gain, max_age_ns=int(signal_max_age_s * SEC))
    algo_types = tuple(AlgoType[a] for a in algos)
    cfg = load_exec_config(config_dir, adv_table=adv_table)
    cfg = replace(
        cfg,
        seed=seed,
        impact_model=impact_model,
        impact_exponent=impact_exponent if impact_model == "parent" else 1.0,
        walk_depth_levels=walk_depth_levels,
    )
    if venues is not None:
        cfg = replace(cfg, venues=load_venues(venues))
    elif second_data_dir is not None:
        merged_venues = dict(cfg.venues)
        merged_venues.update(
            load_venues(Path(second_data_dir) / "configs" / "venues" / "venues.json")
        )
        cfg = replace(cfg, venues=merged_venues)
    if venue_inbound_delay_ns:
        by_name = {sp.name: v for v, sp in cfg.venues.items()}
        unknown = sorted(set(venue_inbound_delay_ns) - set(by_name))
        if unknown:
            raise SystemExit(f"--inbound-delay names unknown venues {unknown} ({sorted(by_name)})")
        cfg = replace(
            cfg,
            venues={
                v: replace(
                    sp, inbound_delay_ns=venue_inbound_delay_ns.get(sp.name, sp.inbound_delay_ns)
                )
                for v, sp in cfg.venues.items()
            },
        )
    sor = load_sor_options(config_dir / "execution" / "execution.json")
    rkw = {
        "hidden_prints": hidden_prints,
        "peg_ahead_qty": peg_ahead_qty,
        "md_delay_ns": md_delay_ns,
    }
    # v1.17 B8: route policies (pinned venue or the sor_v2 router)
    if iex_clock not in IEX_CLOCKS:
        raise SystemExit(f"--iex-clock must be one of {IEX_CLOCKS}")
    routes_used = sorted({r for pol in policies if (r := split_route(pol)[1]) is not None})
    venue_by_name = {s.name: v for v, s in cfg.venues.items()}
    pin_of: dict[str, int] = {}
    for r in routes_used:
        vname = ROUTE_POLICIES[r]
        if vname is not None:
            if vname not in venue_by_name:
                raise SystemExit(
                    f"route {r!r} pins to venue {vname} but the venue table has "
                    f"{sorted(venue_by_name)} (--second-data-dir / --venues)"
                )
            pin_of[r] = venue_by_name[vname]
    cal_docs = None if venue_calibration is None else load_calibrations(venue_calibration)
    router = None
    router_fp = None
    sweep_router = None
    if "sor" in routes_used:
        router, router_fp = router_from_calibrations(
            cfg.venues,
            cal_docs,
            tox_horizon=router_tox_horizon,
            fill_horizon_s=router_fill_horizon_s,
            sor_options=sor,
        )
    if "sor_sweep" in routes_used:
        # v1.17.1: the same router with an all-in sweep over the walk depth
        sweep_router, sweep_fp = router_from_calibrations(
            cfg.venues,
            cal_docs,
            tox_horizon=router_tox_horizon,
            fill_horizon_s=router_fill_horizon_s,
            sor_options=sor,
            sweep_rank="all_in",
            sweep_levels=walk_depth_levels,
        )
        if router is None:
            router, router_fp = sweep_router, sweep_fp
    lit_venues = sorted(v for v, s in cfg.venues.items() if s.is_lit)
    if not lit_venues:
        raise SystemExit("the venue table has no lit venue")
    taker_fee = cfg.venue(lit_venues[0]).taker_fee_per_share
    # v1.17 B10: the dark-first arm (modelled, not measured)
    dark_used = any(parse_policy(pol)[1] == DARK_VARIANT for pol in policies)
    dark_params: DarkFirstParams | None = None
    dark_doc: dict[str, object] = {"used": False, "label": MODELLED_LABEL}
    if dark_used:
        dark_vid = dark_venue_id
        if dark_vid == 0:
            for v, s in cfg.venues.items():
                if not s.is_lit:
                    dark_vid = v
                    break
        if dark_vid == 0 or cfg.venues[dark_vid].is_lit:
            raise SystemExit(
                "the dark variant needs a venue with venue_type dark / midpoint (--venues)"
            )
        if dark_model is not None:
            model = DarkFillModel.load(dark_model)
        else:
            model = DarkFillModel.prior(int(dark_horizon_s * SEC))
        controls = DarkControls(
            max_participation=dark_max_participation,
            participation_window_ns=int(dark_participation_window_s * SEC),
            max_dark_notional=dark_max_notional,
        )
        dark_params = DarkFirstParams(
            model=model,
            venue_id=dark_vid,
            controls=controls,
            max_urgency=dark_max_urgency,
            regimes=tuple(dark_regimes),
            horizon_ns=int(dark_horizon_s * SEC),
        )
        dark_doc = {
            "used": True,
            "label": MODELLED_LABEL,
            "venue_id": dark_vid,
            "venue": cfg.venues[dark_vid].name,
            "venue_type": cfg.venues[dark_vid].venue_type,
            "fee_per_share": cfg.venues[dark_vid].taker_fee_per_share,
            "model": None if dark_model is None else str(dark_model),
            "model_doc": model.to_dict(),
            "horizon_s": dark_horizon_s,
            "max_urgency": dark_max_urgency,
            "regimes": list(dark_regimes),
            "controls": controls.to_dict(),
            "control_codes": list(DARK_CONTROL_CODES),
            "fill_rule": (
                "fills at the first event inside the horizon where p_fill(lit volume since the "
                "post, spread at the post, qty + own dark queue) >= u (one seeded uniform per "
                "child); price = displayed midpoint rounded against us; unfilled remainder to "
                "the lit book in the base child style"
            ),
            "b8_pairing": (
                "B8 runs the same base+dark variant with --venues naming both lit venues "
                "(Nasdaq + IEX) and the dark one on the overlap days, paired router-vs-pinned "
                "on the same windows"
            ),
        }
    names = _symbols(config_dir)
    want_symbols = set(symbols)
    files = [
        p
        for p in _session_files(data_dir)
        if not sessions or any(s in _session_label(p) for s in sessions)
    ]
    if not files:
        raise SystemExit(f"no session of {data_dir} matches --sessions {sessions}")
    second_of: dict[str, Path] = {}
    unmatched: list[str] = []
    id_map: dict[int, int] | None = None
    merge_counts: dict[str, dict[str, int]] = {}
    if second_data_dir is not None:
        second_files = {_session_label(p): p for p in _session_files(Path(second_data_dir))}
        for sp in files:
            lab = _session_label(sp)
            if lab in second_files:
                second_of[lab] = second_files[lab]
            else:
                unmatched.append(lab)
        files = [sp for sp in files if _session_label(sp) in second_of]
        if not files:
            raise SystemExit(f"no session of {data_dir} has a match in {second_data_dir}")
        id_map = instrument_map(symbols_of(data_dir), symbols_of(Path(second_data_dir)))
    id_map_of: dict[str, dict[int, int] | None] = {lab: id_map for lab in second_of}
    primary_map_of: dict[str, dict[int, int]] = {}
    # v1.17 B8: further (Nasdaq day, IEX day) dataset pairs, keyed onto the
    # first dataset's instrument ids by symbol (its configs are the run's)
    for nd, xd in more_pairs:
        if second_data_dir is None:
            raise SystemExit("more day pairs need --second-data-dir (the first pair's IEX dataset)")
        nd, xd = Path(nd), Path(xd)
        pm = instrument_map(symbols_of(data_dir), symbols_of(nd))
        xm = instrument_map(symbols_of(data_dir), symbols_of(xd))
        sec = {_session_label(sp): sp for sp in _session_files(xd)}
        for sp in _session_files(nd):
            lab = _session_label(sp)
            if sessions and not any(x in lab for x in sessions):
                continue
            if lab not in sec:
                # a multi-session Nasdaq dataset can be paired day by day with
                # one IEX dataset per day: a session another pair matches is
                # not unmatched
                if lab not in second_of and lab not in unmatched:
                    unmatched.append(lab)
                continue
            if lab in second_of:
                raise SystemExit(f"session {lab} appears in more than one --pair-dir")
            if lab in unmatched:
                unmatched.remove(lab)
            files.append(sp)
            second_of[lab] = sec[lab]
            id_map_of[lab] = xm
            primary_map_of[lab] = pm
    if more_pairs:
        files = sorted(files, key=_session_label)
    # two-venue mode only: drop unwanted symbols before the merge (memory)
    keep_ids = None
    if second_of and want_symbols:
        keep_ids = {iid for iid, sym in names.items() if sym in want_symbols}
    window_ns = int(window_min * 60 * SEC)
    warmup_ns = int(warmup_min * 60 * SEC)
    rng = SplitMix64(seed)
    rows: dict[str, list[dict]] = {p: [] for p in policies}
    dark_totals: dict[str, dict[str, object]] = {}
    pull_rows_of: dict[str, list[dict]] = {p: [] for p in policies}
    pull_lost: dict[str, int] = {p: 0 for p in policies}
    mk_rows_of: dict[str, list[dict]] = {p: [] for p in policies}
    venue_rows_of: dict[str, dict[str, list[dict]]] = {}
    sweep_unsent: dict[str, int] = {}
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
    consolidated: dict[str, float] = {}
    if consolidated_adv is not None:
        consolidated = {
            str(k): float(v)
            for k, v in json.loads(Path(consolidated_adv).read_text(encoding="utf-8")).items()
            if not str(k).startswith("_")
        }
    adv_pool = sorted(consolidated.values() or [i.adv for i in cfg.instruments.values()])
    liq_edges = (
        [adv_pool[len(adv_pool) // 3], adv_pool[(2 * len(adv_pool)) // 3]]
        if len(adv_pool) >= 3
        else []
    )
    for s_index, path in enumerate(files):
        label = _session_label(path)
        session_labels.append(label)
        if keep_ids is not None and label in second_of:
            # two-venue mode with --symbols: stream the files, keep the symbols
            pm = primary_map_of.get(label)
            raw = keep_ids if pm is None else {k for k, v in pm.items() if v in keep_ids}
            events = read_events(path, raw)
        else:
            events = _read_events(path)
        if max_events > 0:
            events = events[:max_events]
        if label in primary_map_of:
            events = remap_instruments(events, primary_map_of[label])
        if label in second_of:
            xm = id_map_of[label]
            if keep_ids is not None:
                xkeep = {k for k, v in (xm or {}).items() if v in keep_ids}
                if xm is None:
                    xkeep = set(keep_ids)
                other = read_events(second_of[label], xkeep)
            else:
                other = _read_events(second_of[label])
            if max_events > 0:
                other = other[:max_events]
            cnt: dict[str, int] = {}
            events = merge_events(
                events,
                other,
                id_map=id_map_of[label],
                iex_clock=iex_clock,
                keep=keep_ids,
                counters=cnt,
            )
            merge_counts[label] = cnt
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
            passes = make_parent_passes(
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
                cover_all_cells=cover_all_cells,
            )
            for parents in passes:
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
                            p,
                            pol,
                            src,
                            urgency=urgency_policy,
                            pacing=pacing_params,
                            ac_params=acp,
                            fade=fade_params,
                            queue_cancel=qcancel_params,
                            dark=dark_params,
                            child_exec=child_exec,
                        )
                        if q is not None:
                            worked.append(q)
                            cells_of.append((p, pct))
                    if not worked:
                        continue
                    route = split_route(pol)[1]
                    if route is not None and route in pin_of:
                        worked = [replace(q, venue_id=pin_of[route]) for q in worked]
                    if route == "sor":
                        res = ExecutionReplay(
                            cfg, worked, sor, router=router, router_fill_prob=router_fp, **rkw
                        ).run(evs)
                    elif route == "sor_sweep":
                        res = ExecutionReplay(
                            cfg,
                            worked,
                            sor,
                            router=sweep_router,
                            router_fill_prob=router_fp,
                            router_sweep=True,
                            **rkw,
                        ).run(evs)
                        sweep_unsent[pol] = sweep_unsent.get(pol, 0) + res.sweep_unsent_qty
                    else:
                        res = ExecutionReplay(cfg, worked, sor, **rkw).run(evs)
                    fills_of: dict[int, list] = {p.parent_id: [] for p in worked}
                    for f in res.fills:
                        fills_of[f.parent_id].append(f)
                    orders_of = {o.order_id: o for kids in res.children.values() for o in kids}
                    # v1.17 B6: every fill priced by type / regime, clustered by session
                    frows = fill_rows(
                        res.fills,
                        orders_of,
                        timeline,
                        ins.tick_size,
                        qty_unit=ins.qty_unit,
                        horizons_ns=mk_horizons,
                        cluster=label,
                    )
                    mk_rows_of[pol].extend(frows)
                    if routes_used:
                        for f, fr in zip(res.fills, frows, strict=True):
                            venue_rows_of.setdefault(pol, {}).setdefault(
                                cfg.venues[f.venue_id].name, []
                            ).append(fr)
                    # v1.17 B4: the pulls, priced after the fact (counterfactual)
                    if res.pulls:
                        pull_rows_of[pol].extend(
                            pull_rows(
                                res.pulls,
                                timeline,
                                ins.tick_size,
                                horizons_ns=mk_horizons,
                                qty_unit=ins.qty_unit,
                                rules=CANCEL_RULES,
                                cluster=label,
                            )
                        )
                        pull_lost[pol] += sum(1 for r in res.pulls if r.outcome == "lost_race")
                    # v1.18 REL-03: qty-weighted post-trade markout per parent, COST sign
                    # (positive = adverse); [sum(qty * bps), qty] per horizon, undefined fills out
                    mk_of: dict[int, dict[str, list[float]]] = {}
                    for f, fr in zip(res.fills, frows, strict=True):
                        acc = mk_of.setdefault(f.parent_id, {})
                        for h, v in fr["markout_bps"].items():
                            if v is None:
                                continue
                            a = acc.setdefault(h, [0.0, 0.0])
                            a[0] += -float(v) * f.qty
                            a[1] += f.qty
                    for (p0, pct), p in zip(cells_of, worked, strict=True):
                        row = parent_costs(
                            p, fills_of[p.parent_id], timeline, ins.tick_size, taker_fee
                        )
                        row.update(
                            parent_extras(
                                row,
                                p,
                                timeline,
                                ins.tick_size,
                                ins.adv,
                                consolidated.get(sym),
                                liq_edges,
                                mk_of.get(p.parent_id, {}),
                                (timeline.ts[0], timeline.ts[-1]),
                            )
                        )
                        rep = res.parents[p.parent_id]
                        type_qty: dict[str, int] = {}
                        venue_qty: dict[str, int] = {}
                        for f in fills_of[p.parent_id]:
                            k = fill_type(f, orders_of[f.order_id])
                            type_qty[k] = type_qty.get(k, 0) + f.qty
                            vn = cfg.venues[f.venue_id].name
                            venue_qty[vn] = venue_qty.get(vn, 0) + f.qty
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
                                "fill_type_qty": type_qty,
                                "venue_qty": venue_qty,
                                "pulls": rep.pulls,
                                "pulled_qty": rep.pulled_qty,
                                "pulls_lost_race": rep.pulls_lost_race,
                                "held_unsent_qty": rep.held_unsent_qty,
                                "dark_children": rep.dark_children,
                                "dark_posted_qty": rep.dark_posted_qty,
                                "dark_filled_qty": rep.dark_filled_qty,
                                "dark_expired_qty": rep.dark_expired_qty,
                                "dark_unsent_qty": rep.dark_unsent_qty,
                                "dark_rejected": rep.dark_rejected,
                                "cap_no_depth": rep.cap_no_depth,
                                "cap_withheld_qty": rep.cap_withheld_qty,
                                "reposts": rep.reposts,
                                "carry_unsent_qty": rep.carry_unsent_qty,
                                "residual_qty": rep.residual_qty,
                            }
                        )
                        rows[pol].append(row)
                        if rep.dark_children or rep.dark_rejected:
                            tot = dark_totals.setdefault(
                                pol,
                                {
                                    "label": MODELLED_LABEL,
                                    "parents": 0,
                                    "dark_children": 0,
                                    "dark_posted_qty": 0,
                                    "dark_filled_qty": 0,
                                    "dark_expired_qty": 0,
                                    "dark_unsent_qty": 0,
                                    "dark_rejected": {},
                                },
                            )
                            tot["parents"] += 1  # type: ignore[operator]
                            for k in (
                                "dark_children",
                                "dark_posted_qty",
                                "dark_filled_qty",
                                "dark_expired_qty",
                                "dark_unsent_qty",
                            ):
                                tot[k] += getattr(rep, k)  # type: ignore[operator]
                            rej = tot["dark_rejected"]
                            assert isinstance(rej, dict)
                            for k, n in rep.dark_rejected.items():
                                rej[k] = rej.get(k, 0) + n
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
    # v1.18 REL-04b (opt-in): paired v2 ratio differences vs the baseline over
    # every cell, with the per-session sums and a family-adjusted interval
    paired_v2_doc: dict[str, object] = {}
    if paired_v2:
        for pol in policies:
            if pol == baseline_policy or not rows[pol] or "m_fill_cost" not in rows[pol][0]:
                continue
            paired_v2_doc[f"{pol}-{baseline_policy}"] = exm.paired_v2(
                rows[baseline_policy], rows[pol], family_k=family_k
            )
    # v1.18 REL-03: coverage (n per day and cell) and the slices of the v2 metrics
    expected_cells = [
        f"{pol}|{algo}|{pct:g}" for pol in policies for algo in algos for pct in sizes_pct
    ]
    coverage: dict[str, object] = {}
    for lab in session_labels:
        per_cell: dict[str, int] = {}
        for pol in policies:
            for r in rows[pol]:
                if r["session"] == lab:
                    k = f"{pol}|{r['algo']}|{r['size_pct_adv']:g}"
                    per_cell[k] = per_cell.get(k, 0) + 1
        coverage[lab] = {
            "n_parents_by_cell": dict(sorted(per_cell.items())),
            "cells_missing": [k for k in expected_cells if k not in per_cell],
        }
    slices: dict[str, object] = {}
    for pol in policies:
        if rows[pol] and "m_fill_cost" in rows[pol][0]:
            slices[pol] = {
                "by_spread_regime": exm.split_by(rows[pol], "spread_regime"),
                "by_time_of_day": exm.split_by(rows[pol], "tod"),
                "by_size_pct_adv": exm.split_by(rows[pol], "size_pct_adv"),
                "by_liquidity_bucket": exm.split_by(rows[pol], "liquidity"),
            }
    fades: dict[str, object] = {}
    markouts: dict[str, object] = {}
    for pol in policies:
        if pull_rows_of[pol]:
            fades[pol] = {
                "fade": pull_summary(
                    [r for r in pull_rows_of[pol] if str(r["rule"]).startswith("fade:")],
                    list(mk_horizons),
                    lost_race=pull_lost[pol],
                    rules=("fade:signal", "fade:pressure"),
                ),
                "queue": pull_summary(
                    [r for r in pull_rows_of[pol] if str(r["rule"]).startswith("queue:")],
                    list(mk_horizons),
                    lost_race=pull_lost[pol],
                    rules=("queue:ahead", "queue:p_fill"),
                ),
            }
        if mk_rows_of[pol]:
            markouts[pol] = decompose(mk_rows_of[pol], list(mk_horizons))
    doc: dict[str, object] = {
        "schema": "iap.execution_benchmark/v1",
        "metrics_schema": "iap.execution_metrics/v2",
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
            "cover_all_cells": cover_all_cells,
            "size_basis": (
                "% of the venue ADV in instruments.json (one venue, NOT consolidated); "
                "size_pct_consolidated_adv is on each parent row"
                + ("" if not consolidated else f" from {consolidated_adv}")
            ),
        },
        "simulator": {
            "impact_model": impact_model,
            "adv_table": (
                "static"
                if cfg.adv_by_session is None
                else ("instruments.json" if adv_table is True else str(adv_table))
            ),
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
        "coverage_by_session": coverage,
        "metrics_v2_slices": slices,
        "metric_definitions": exm.METRIC_DEFINITIONS,
        "paired_vs_baseline": paired,
        # v1.17 B4 / B5 / B6
        "pull_rules": pull_doc,
        "fill_types": list(FILL_TYPES),
        "markout_horizons": list(mk_horizons),
        "fades_saved_vs_fills_missed": fades,
        "markout_decomposition": markouts,
        # v1.17 B10 (modelled, not measured)
        "dark": {**dark_doc, "totals": dark_totals},
    }
    if second_data_dir is not None or routes_used:
        doc["two_venue"] = two_venue_block(
            rows,
            venue_rows_of,
            policies,
            list(mk_horizons),
            routes={r: (cfg.venues[pin_of[r]].name if r in pin_of else None) for r in routes_used},
            second_data_dir=None if second_data_dir is None else str(second_data_dir),
            iex_clock=iex_clock,
            unmatched=unmatched,
            merge_counts=merge_counts,
            calibration=None if venue_calibration is None else str(venue_calibration),
            router_models={
                str(v): {"p_fill_touch": m.p_fill_touch, "toxicity_bps": dict(m.toxicity_bps)}
                for v, m in (router.models.items() if router is not None else ())
            },
            router_tox_horizon=router_tox_horizon,
            router_fill_horizon_s=router_fill_horizon_s,
            baseline_policy=baseline_policy,
            seed=seed,
        )
        if sweep_unsent:
            # v1.17.1 sor_sweep: child qty beyond both books' displayed depth (not sent)
            doc["two_venue"]["sweep_unsent_qty"] = dict(sorted(sweep_unsent.items()))
    if paired_v2:
        doc["paired_v2_vs_baseline"] = paired_v2_doc
    if timestamp:
        doc["generated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return doc


def two_venue_block(
    rows: dict[str, list[dict]],
    venue_rows_of: dict[str, dict[str, list[dict]]],
    policies: tuple[str, ...],
    horizons: list[str],
    seed: int = 0,
    **meta: object,
) -> dict[str, object]:
    """v1.17 B8: per policy, the venue split (fill qty share per venue) and
    the markout of each venue's fills by horizon (day-clustered); plus the
    run's routing / alignment metadata. The shortfall / fill-rate cells and
    the paired router-vs-pinned differences are the benchmark's own
    ``cells`` / ``paired_vs_baseline``."""
    split: dict[str, object] = {}
    vmk: dict[str, object] = {}
    for pol in policies:
        tot: dict[str, int] = {}
        for r in rows[pol]:
            for vn, q in r.get("venue_qty", {}).items():
                tot[vn] = tot.get(vn, 0) + q
        filled = sum(tot.values())
        split[pol] = {
            "filled_qty": filled,
            "venue_qty": dict(sorted(tot.items())),
            "venue_share": {vn: (q / filled if filled else None) for vn, q in sorted(tot.items())},
        }
        per = venue_rows_of.get(pol, {})
        vmk[pol] = {}
        for vn in sorted(per):
            cell: dict[str, object] = {"n_fills": len(per[vn])}
            for h in horizons:
                vals: list[float] = []
                cl: list[str] = []
                for fr in per[vn]:
                    v = fr["markout_bps"][h]
                    if v is not None:
                        vals.append(float(v))
                        cl.append(str(fr["cluster"]))
                cell[h] = cluster_mean_stats(vals, cl)
            vmk[pol][vn] = cell  # type: ignore[index]
    baseline = str(meta.get("baseline_policy", ""))
    pooled: dict[str, object] = {}
    pairs: dict[str, list[dict[str, object]]] = {}
    if baseline in rows:
        keyed = {(r["session"], r["symbol"], r["window"]): r for r in rows[baseline]}
        for pol in policies:
            if pol == baseline:
                continue
            pooled[f"{pol}-{baseline}"] = _paired_summary(rows[baseline], rows[pol], seed)
            out_rows = []
            for r in rows[pol]:
                b = keyed.get((r["session"], r["symbol"], r["window"]))
                if b is None:
                    continue
                out_rows.append(
                    {
                        "session": r["session"],
                        "symbol": r["symbol"],
                        "window": r["window"],
                        "algo": r["algo"],
                        "size_pct_adv": r["size_pct_adv"],
                        "fill_rate_diff": r["filled_qty"] / r["qty"] - b["filled_qty"] / b["qty"],
                        "costs_bps_diff": {k: _bps(r, k) - _bps(b, k) for k in REPORT_COSTS},
                    }
                )
            pairs[f"{pol}-{baseline}"] = out_rows
    return {
        "item": "v1.17 B8",
        "alignment": ALIGNMENT_RULE,
        "feed_latency_skew_note": SKEW_QUALIFICATION,
        **meta,
        "paired_pooled": pooled,
        "paired_rows": pairs,
        "venue_split": split,
        "venue_markouts_bps": vmk,
    }


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


def _two_venue_lines(doc: dict) -> list[str]:
    tv = doc.get("two_venue")
    if not tv:
        return []
    hz = doc["markout_horizons"]
    out = [
        "## Two venues: router vs pinned (v1.17 B8)",
        "",
        f"Second dataset `{tv['second_data_dir']}` (IEX clock `{tv['iex_clock']}`); "
        f"alignment: {tv['alignment']}. Routes: "
        + ", ".join(f"`{r}` -> {v or 'sor_v2 router'}" for r, v in tv["routes"].items())
        + f". Router calibration: `{tv['calibration'] or 'VenueModel defaults (uncalibrated)'}`."
        + (
            f" Sessions without an IEX match (not run): {', '.join(tv['unmatched'])}."
            if tv["unmatched"]
            else ""
        ),
        "",
        "| policy | filled qty | venue share | "
        + " | ".join(f"markout {h} by venue" for h in hz)
        + " |",
        "|---|---:|---|" + "---|" * len(hz),
    ]
    for pol, sp in tv["venue_split"].items():
        share = ", ".join(
            f"{vn} {x:.1%}" if x is not None else f"{vn} -" for vn, x in sp["venue_share"].items()
        )
        cols = []
        for h in hz:
            cols.append(
                "; ".join(
                    f"{vn} {_stat(c[h])}" for vn, c in tv["venue_markouts_bps"].get(pol, {}).items()
                )
                or "-"
            )
        out.append(f"| {pol} | {sp['filled_qty']} | {share or '-'} | " + " | ".join(cols) + " |")
    if tv.get("paired_pooled"):
        out += [
            "",
            "Pooled over every cell (same parents, `other - baseline`, bps; ± day-clustered SE):",
            "",
            "| pair | n | all-in | shortfall | spread | fees net | fill rate diff |",
            "|---|---:|---|---|---|---|---|",
        ]
        for k, v in tv["paired_pooled"].items():
            c = v["costs_bps"]
            out.append(
                f"| {k} | {v['n_pairs']} | {_stat(c['all_in'])} | {_stat(c['shortfall'])} | "
                f"{_stat(c['spread'])} | {_stat(c['fees_net'])} | {_stat(v['fill_rate_diff'])} |"
            )
    out += [
        "",
        "Shortfall, fill rate and the paired router-vs-pinned differences are in the cells and "
        "paired tables above (same parents and windows for every route).",
        "",
    ]
    return out


def _ratio(s: dict | None, digits: int = 2) -> str:
    """``estimate ± se [ci]`` with n, clusters, ESS and MDE beside it."""
    if not s or s.get("estimate") is None:
        return "—"
    out = f"{s['estimate']:.{digits}f}"
    if s.get("se") is not None:
        lo, hi = s["ci95"]
        out += f" ± {s['se']:.{digits}f} [{lo:.{digits}f}, {hi:.{digits}f}]"
        ess = s.get("ess")
        m = s.get("mde_t")
        out += f" (G={s['n_clusters']}, ESS {'—' if ess is None else f'{ess:.1f}'}"
        out += f", MDE {'—' if m is None else f'{m:.{digits}f}'})"
    else:
        out += f" (G={s['n_clusters']}: no interval)"
    return out


def _metrics_v2_lines(doc: dict) -> list[str]:
    cells = [(k, c["metrics_v2"]) for k, c in doc["cells"].items() if "metrics_v2" in c]
    if not cells:
        return []
    lines = [
        "",
        "## Metrics v2 (REL-03; cost per FILLED share is primary, Perold shortfall secondary)",
        "",
        "Positive = worse for the trader. Pooled values are quantity-weighted ratios of sums "
        "(cluster-linearised CR1 SE, one cluster per session, Student-t at G − 1 df); ESS = "
        "parents / design effect; MDE = minimum detectable effect (80 % power, two-sided 5 %, "
        "Student-t). Narrower intervals are a diagnostic, never a gate.",
        "",
        "| cell | n | cost / filled share (bps) | fill rate | completion ratio | residual share of target | completion charge (bps, residual) | Perold shortfall / target share (bps) | per-parent median cost (bps) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for key, m in cells:
        pp = m["per_parent"]["cost_per_filled_share_bps"]
        lines.append(
            f"| {key} | {m['n_parents']} | {_ratio(m['cost_per_filled_share_bps'])} | "
            f"{_ratio(m['fill_rate'], 3)} | {_ratio(m['completion_ratio'], 3)} | "
            f"{_f(m['residual_qty']['share_of_target'], 3)} | {_ratio(m['completion_charge_bps'])} | "
            f"{_ratio(m['shortfall_per_target_share_bps'])} | {_f(pp['q50'])} |"
        )
    cov = doc.get("coverage_by_session", {})
    missing = {lab: c["cells_missing"] for lab, c in cov.items() if c["cells_missing"]}
    lines += [
        "",
        "Grid coverage: "
        + (
            "every cell ran on every session."
            if not missing
            else "cells that did NOT run on a session: "
            + "; ".join(f"{lab}: {len(v)}" for lab, v in sorted(missing.items()))
            + " (use `--cover-all-cells`)."
        ),
    ]
    lines += ["", "### Metric definitions", "", exm.definitions_markdown(), ""]
    return lines


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
    lines += _metrics_v2_lines(doc)
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
    fades = doc.get("fades_saved_vs_fills_missed", {})
    if fades:
        lines += [
            "",
            "## Fades saved vs fills missed (v1.17 B4 / B5; counterfactual, touch-through proxy)",
            "",
            "| policy | rules | pulls | pulled qty | lost race | horizon | would fill | saved (n, mean bps, ccy) | missed (n, mean bps, ccy) | net bps of would-fill notional |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for pol, blocks in fades.items():
            for kind, fs in blocks.items():
                if not fs["n_pulls"]:
                    continue
                for h, row in fs["horizons"].items():
                    sv, ms = row["saved"], row["missed"]
                    lines.append(
                        f"| {pol} | {kind} | {fs['n_pulls']} | {fs['pulled_qty']} | {fs['lost_race']} | {h} | "
                        f"{row['would_fill']} ({row['would_fill_qty']}) | "
                        f"{sv['n']}, {_f(sv['bps_mean'])}, {_f(sv['ccy'])} | "
                        f"{ms['n']}, {_f(ms['bps_mean'])}, {_f(ms['ccy'])} | {_f(row['net_bps'])} |"
                    )
    mkd = doc.get("markout_decomposition", {})
    if mkd:
        lines += [
            "",
            "## Markout by fill type and regime (v1.17 B6; bps, ± day-clustered SE)",
            "",
            "| policy | cell | fills | qty | "
            + " | ".join(f"markout {h}" for h in doc["markout_horizons"])
            + " | "
            + " | ".join(f"price impact {h}" for h in doc["markout_horizons"])
            + " |",
            "|---|---|---|---|" + "---|" * (2 * len(doc["markout_horizons"])),
        ]
        for pol, d in mkd.items():
            cells_ = [("all", d["all"])]
            cells_ += [(f"type {k}", v) for k, v in d["by_fill_type"].items()]
            cells_ += [(f"regime {k}", v) for k, v in d["by_regime"].items()]
            for name, c in cells_:
                hs = c["horizons"]
                mk = " | ".join(_stat(hs[h]["markout"]) for h in doc["markout_horizons"])
                pi = " | ".join(_stat(hs[h]["price_impact"]) for h in doc["markout_horizons"])
                lines.append(f"| {pol} | {name} | {c['n_fills']} | {c['qty']} | {mk} | {pi} |")
    dk = doc.get("dark", {})
    if dk.get("used"):
        lines += [
            "",
            f"## Dark venue (v1.17 B10; **{dk['label']}**)",
            "",
            f"Venue `{dk['venue']}` (id {dk['venue_id']}, type {dk['venue_type']}, fee "
            f"{dk['fee_per_share']} / share); model "
            + (
                f"`{dk['model']}` (fitted on {', '.join(dk['model_doc']['sessions'])}; "
                f"{dk['model_doc']['n_samples']} proxy samples)"
                if dk["model_doc"]["fitted"]
                else "the documented PRIOR (not fitted)"
            )
            + f"; horizon {dk['horizon_s']:g} s; controls {dk['controls']}. "
            + dk["fill_rule"]
            + ".",
            "",
            "| policy | parents | dark children | posted | filled dark | expired to lit | "
            "unsent | rejected |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for pol, t in dk["totals"].items():
            rej = ", ".join(f"{k} {n}" for k, n in sorted(t["dark_rejected"].items())) or "-"
            lines.append(
                f"| {pol} | {t['parents']} | {t['dark_children']} | {t['dark_posted_qty']} | "
                f"{t['dark_filled_qty']} | {t['dark_expired_qty']} | {t['dark_unsent_qty']} | "
                f"{rej} |"
            )
        lines += ["", dk["b8_pairing"] + "."]
    lines += [
        "",
        *_two_venue_lines(doc),
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
        "- fades saved vs fills missed (B4 / B5): for every quantity a rule pulled, whether the "
        "market traded THROUGH the pulled price inside the horizon (the proxy for 'it would have "
        "filled') and the markout of that forgone fill — `saved` = it would have been adverse, "
        "`missed` = it would have been favourable; `lost race` = the fill landed before the "
        "cancel arrived (simulator rule 7). A counterfactual on a book our order never moved.",
        "- markout by fill type (B6): passive = a first post that rested and filled, aggressive "
        "= a taker fill, faded = a maker fill that landed after a pull cancel was sent, "
        "cancelled_replaced = a maker fill of a child re-sent after a cancel; regimes are the "
        "B2 urgency regimes (plain without a signal). SEs cluster by session.",
        "- dark (B10): fill type `dark` and every dark row above are MODELLED, NOT MEASURED — "
        "a logistic fill probability fitted on Nasdaq inside-spread hidden prints as the proxy "
        "(no dark-pool data exists); the negated price impact of the `dark` type is the adverse "
        "selection of those modelled fills.",
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
        "pull_rules": doc.get("pull_rules"),
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
    ap.add_argument(
        "--adv-table",
        default="static",
        help="v1.17 A5 (X-9): per-session ADV for the impact adders: 'static' (default, the "
        "dataset-level adv), 'instruments' (the config dir's instruments.json adv_by_session) "
        "or a path (an instruments.json or a plain ADV table, iap.reference.session_adv)",
    )
    ap.add_argument("--baseline-policy", default="native")
    ap.add_argument("--max-events", type=int, default=0, help="per session (smoke runs / tests)")
    ap.add_argument("--allow-holdout", action="store_true")
    ap.add_argument(
        "--cover-all-cells",
        action="store_true",
        help="REL-03: add overlapping passes so every grid cell runs on every day (default: "
        "the v1.17 rotation, 12 windows for 16 cells)",
    )
    ap.add_argument(
        "--hidden-prints",
        action="store_true",
        help="REL-04: non-displayed TRADE prints fill resting children and midpoint pegs",
    )
    ap.add_argument("--peg-ahead-qty", type=int, default=0, help="REL-04: modelled midpoint queue")
    ap.add_argument(
        "--md-delay-ns",
        type=int,
        default=0,
        help="REL-04: market-data delay of capped / peg children",
    )
    ap.add_argument(
        "--inbound-delay",
        action="append",
        default=[],
        metavar="VENUE=NS",
        help="REL-04: inbound order/cancel delay of a venue (IEX speed bump: IEXG=350000)",
    )
    ap.add_argument("--child-max-levels", type=int, default=3, help="REL-04 capped / peg variants")
    ap.add_argument("--child-depth-fraction", type=float, default=1.0)
    ap.add_argument("--child-max-slippage-ticks", type=int, default=0)
    ap.add_argument("--child-max-participation", type=float, default=0.0)
    ap.add_argument("--child-repost-interval-s", type=float, default=1.0)
    ap.add_argument("--child-max-reposts", type=int, default=20)
    ap.add_argument("--child-peg-deadline-s", type=float, default=0.0)
    ap.add_argument(
        "--consolidated-adv",
        type=Path,
        default=None,
        help="REL-03: JSON {symbol: consolidated daily shares}; labels sizes in %% of "
        "consolidated ADV",
    )
    ap.add_argument("--no-timestamp", action="store_true")
    ap.add_argument(
        "--paired-v2",
        action="store_true",
        help="REL-04b: paired v2 ratio differences vs --baseline-policy over all cells",
    )
    ap.add_argument("--family-k", type=int, default=1, help="REL-04b: Bonferroni family size")
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
    fg = ap.add_argument_group("quote fading / queue-aware cancels / markouts (v1.17 B4-B6)")
    fg.add_argument(
        "--fade-threshold-z",
        type=float,
        default=0.0,
        help="B4: pull a resting child when the signal is adverse by this many signal units "
        "(needs --signal; 0 = off)",
    )
    fg.add_argument(
        "--fade-pressure",
        type=float,
        default=0.0,
        help="B4: pull when the opposite side's share of the two L1 sizes >= this (0.5-1; 0 = off)",
    )
    fg.add_argument("--fade-action", choices=("hold", "repost", "cross"), default="hold")
    fg.add_argument("--fade-max", type=int, default=3, help="fades per chain of posts")
    fg.add_argument(
        "--qcancel-max-ahead",
        type=int,
        default=0,
        help="B5: cancel when ahead_qty > this (0 = off)",
    )
    fg.add_argument(
        "--qcancel-min-pfill",
        type=float,
        default=0.0,
        help="B5: cancel when hazard P(fill within --qcancel-horizon-s) < this (0 = off)",
    )
    fg.add_argument("--qcancel-horizon-s", type=float, default=1.0)
    fg.add_argument(
        "--qcancel-hazard", type=Path, default=None, help="FillHazardModel JSON (iap.fill_hazard/1)"
    )
    fg.add_argument(
        "--qcancel-calibration",
        type=Path,
        default=None,
        help="calibration JSON estimated with depletion_split=True (X-4 execution depletion rate)",
    )
    fg.add_argument("--qcancel-action", choices=("cross", "repost"), default="cross")
    fg.add_argument(
        "--markout-horizons",
        default=",".join(DEFAULT_MARKOUT_HORIZONS),
        help="B6 decomposition horizons (keys of iap.tca.markout.DEFAULT_HORIZONS_NS)",
    )
    dg = ap.add_argument_group("dark venue (v1.17 B10; modelled, not measured)")
    dg.add_argument("--venues", type=Path, default=None, help="venues.json replacing the config's")
    dg.add_argument("--dark-venue-id", type=int, default=0, help="0 = first non-lit venue")
    dg.add_argument("--dark-model", type=Path, default=None, help="DarkFillModel JSON (else prior)")
    dg.add_argument("--dark-horizon-s", type=float, default=30.0)
    dg.add_argument("--dark-max-urgency", type=float, default=0.5)
    dg.add_argument("--dark-regimes", default="agree", help="B2 regimes that post dark first")
    dg.add_argument("--dark-max-participation", type=float, default=0.1)
    dg.add_argument("--dark-participation-window-s", type=float, default=30.0)
    dg.add_argument("--dark-max-notional", type=float, default=0.0, help="0 = off")
    tg = ap.add_argument_group("two venues: router vs pinned (v1.17 B8)")
    tg.add_argument(
        "--second-data-dir",
        type=Path,
        default=None,
        help="the IEX dataset of the same days (merged two-venue replay)",
    )
    tg.add_argument("--iex-clock", choices=IEX_CLOCKS, default="exchange")
    tg.add_argument(
        "--pair-dir",
        nargs=2,
        action="append",
        default=[],
        metavar=("NASDAQ_DIR", "IEX_DIR"),
        help="a further (Nasdaq dataset, IEX dataset) day pair; repeat per overlap day",
    )
    tg.add_argument(
        "--venue-calibration",
        type=Path,
        default=None,
        help="directory of venue_calibration_*.json (python -m iap.research.two_venue calibrate)",
    )
    tg.add_argument("--router-tox-horizon", default="1s")
    tg.add_argument("--router-fill-horizon-s", type=float, default=1.0)
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
        adv_table=(
            None
            if args.adv_table == "static"
            else (True if args.adv_table == "instruments" else Path(args.adv_table))
        ),
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
        fade_threshold_z=args.fade_threshold_z,
        fade_pressure=args.fade_pressure,
        fade_action=args.fade_action,
        fade_max=args.fade_max,
        qcancel_max_ahead=args.qcancel_max_ahead,
        qcancel_min_pfill=args.qcancel_min_pfill,
        qcancel_horizon_s=args.qcancel_horizon_s,
        qcancel_hazard=args.qcancel_hazard,
        qcancel_calibration=args.qcancel_calibration,
        qcancel_action=args.qcancel_action,
        markout_horizons=_csv(args.markout_horizons),
        venues=args.venues,
        dark_venue_id=args.dark_venue_id,
        dark_model=args.dark_model,
        dark_horizon_s=args.dark_horizon_s,
        dark_max_urgency=args.dark_max_urgency,
        dark_regimes=_csv(args.dark_regimes),
        dark_max_participation=args.dark_max_participation,
        dark_participation_window_s=args.dark_participation_window_s,
        dark_max_notional=args.dark_max_notional,
        second_data_dir=args.second_data_dir,
        iex_clock=args.iex_clock,
        venue_calibration=args.venue_calibration,
        router_tox_horizon=args.router_tox_horizon,
        router_fill_horizon_s=args.router_fill_horizon_s,
        more_pairs=tuple((Path(a), Path(b)) for a, b in args.pair_dir),
        cover_all_cells=args.cover_all_cells,
        consolidated_adv=args.consolidated_adv,
        hidden_prints=args.hidden_prints,
        peg_ahead_qty=args.peg_ahead_qty,
        md_delay_ns=args.md_delay_ns,
        venue_inbound_delay_ns={
            k: int(v) for k, _, v in (a.partition("=") for a in args.inbound_delay)
        }
        or None,
        child_exec=ChildExecParams(
            max_levels=args.child_max_levels,
            depth_fraction=args.child_depth_fraction,
            max_slippage_ticks=args.child_max_slippage_ticks,
            max_participation=args.child_max_participation,
            repost_interval_ns=int(args.child_repost_interval_s * SEC),
            max_reposts=args.child_max_reposts,
            deadline_ns=int(args.child_peg_deadline_s * SEC),
        ),
        paired_v2=args.paired_v2,
        family_k=args.family_k,
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
