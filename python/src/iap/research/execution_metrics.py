"""Execution benchmark v2 metrics (v1.18 REL-03; review ids X-A, X-F, X-G).

The v1 benchmark headline was the Perold implementation shortfall per
parent, in bps of the PARENT notional.  On mostly-unfilled parents that
number is dominated by ``opportunity`` (the unfilled quantity priced at the
arrival-to-end mid move) and the fill rate confounds both ``trading`` and
``opportunity`` (X-A, X-G).  The v2 metrics below separate what was bought
from what was not:

- **primary**: ``cost_per_filled_share_bps`` = (trading + net fees +
  simulated impact) of the FILLED quantity, per filled share, in bps of the
  arrival mid;
- **fill / completion**: ``fill_rate`` (filled / target, quantity-weighted),
  ``completion_ratio`` (share of parents filled in full), ``residual_qty``
  and ``completion_charge`` (finishing the residual at the window end);
- **secondary**: Perold ``shortfall`` per target share (kept from v1);
- **markouts**: ``arrival_markout`` (fill vs the arrival mid) and
  ``post_trade_markout_<h>``.

Every metric is defined in :data:`METRIC_DEFINITIONS` (arrival price,
target quantity, horizon, benchmark convention, sign, unit, denominator) and
``docs/EXECUTION_QUALITY.md`` section 9 repeats the table.  Sign convention
everywhere: POSITIVE = WORSE for the trader (a cost).

Aggregation is quantity-weighted (a ratio of sums, inference by the CR1
cluster-linearised ratio estimator with one cluster per session) AND
per-parent (the distribution: mean, quantiles, a cluster-robust mean over
parents that filled).  Every interval is printed with the number of clusters,
the effective sample size (``n / design effect``, the design effect being
the CR1 variance over the independent-sampling variance) and the minimum
detectable effect (:func:`iap.research.study_stats.mde` on the cluster-level
standard deviation, plus the Student-t version at ``G - 1`` df).  Narrower
intervals are a diagnostic, never a gate.

Size labels use CONSOLIDATED ADV where a table is given
(:func:`size_label`); the venue ADV in ``instruments.json`` is a single
venue's volume, so "2 % of ADV" there is a fraction of a percent of the
consolidated market (X-F).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np

from iap.research.study_stats import mde

#: stated for every metric: where the cost is measured from and in what unit
METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
    "cost_per_filled_share_bps": {
        "arrival_price": "mid at the parent window start (decision = arrival)",
        "target_qty": "parent qty; only the FILLED quantity enters",
        "horizon": "fill time (no post-trade horizon)",
        "benchmark": "arrival mid; trading cost + net fees + simulated impact adder",
        "sign": "positive = worse (cost)",
        "unit": "bps of the arrival mid",
        "denominator": "filled shares x arrival mid (sum over parents for the pooled ratio)",
    },
    "trading_cost_per_filled_share_bps": {
        "arrival_price": "mid at the parent window start",
        "target_qty": "filled qty only",
        "horizon": "fill time",
        "benchmark": "Perold trading cost of the filled quantity (fill price vs arrival mid)",
        "sign": "positive = worse",
        "unit": "bps of the arrival mid",
        "denominator": "filled shares x arrival mid",
    },
    "fill_rate": {
        "arrival_price": "n/a",
        "target_qty": "parent qty",
        "horizon": "parent window",
        "benchmark": "none (a diagnostic, not a target)",
        "sign": "higher = more of the target traded",
        "unit": "fraction",
        "denominator": "sum of parent target qty (qty-weighted); per-parent distribution beside it",
    },
    "completion_ratio": {
        "arrival_price": "n/a",
        "target_qty": "parent qty",
        "horizon": "parent window",
        "benchmark": "none",
        "sign": "higher = more parents finished",
        "unit": "fraction of parents",
        "denominator": "number of parents (filled == target counts as complete)",
    },
    "residual_qty": {
        "arrival_price": "n/a",
        "target_qty": "parent qty",
        "horizon": "window end",
        "benchmark": "target - filled",
        "sign": "positive = unfilled",
        "unit": "shares",
        "denominator": "none (sum; mean per parent beside it)",
    },
    "completion_charge_bps": {
        "arrival_price": "mid at the parent window start",
        "target_qty": "RESIDUAL qty only",
        "horizon": "window end",
        "benchmark": "half-spread at the window end + taker fee, per residual share",
        "sign": "positive = worse",
        "unit": "bps of the arrival mid of the residual",
        "denominator": "residual shares x arrival mid",
    },
    "shortfall_per_target_share_bps": {
        "arrival_price": "mid at the parent window start",
        "target_qty": "parent qty",
        "horizon": "window end (opportunity cost marks the residual at the end mid)",
        "benchmark": "Perold implementation shortfall = trading + opportunity (SECONDARY)",
        "sign": "positive = worse",
        "unit": "bps of the arrival notional of the TARGET",
        "denominator": "target qty x arrival mid",
    },
    "arrival_markout_bps": {
        "arrival_price": "mid at the parent window start",
        "target_qty": "filled qty",
        "horizon": "fill time",
        "benchmark": "side-signed fill price minus arrival mid, quantity-weighted",
        "sign": "positive = worse (paid above / sold below arrival)",
        "unit": "bps of the arrival mid",
        "denominator": "filled shares x arrival mid",
    },
    "post_trade_markout_bps": {
        "arrival_price": "n/a (relative to the fill price)",
        "target_qty": "filled qty",
        "horizon": "per horizon h after each fill (1s, 5s, 30s)",
        "benchmark": "side-signed (fill price - mid at fill + h), quantity-weighted",
        "sign": "positive = worse (the price moved against the fill: adverse selection)",
        "unit": "bps of the fill price",
        "denominator": "filled shares with a defined mid at +h (undefined fills are excluded, "
        "never zero)",
    },
}

#: how many sessions would be needed for a reader to trust a t interval at all
MIN_CLUSTERS_FOR_INTERVAL = 2
POWER = 0.80


def size_label(qty: float, adv: float | None) -> float | None:
    """``qty`` as a percentage of ``adv`` (``None`` when ``adv`` is unknown)."""
    if adv is None or adv <= 0:
        return None
    return 100.0 * float(qty) / float(adv)


def liquidity_bucket(adv: float, edges: Sequence[float]) -> str:
    """Bucket of a symbol's ADV: ``L0`` (least liquid) ... ``L<k>`` by ``edges``."""
    k = 0
    for e in edges:
        if adv >= e:
            k += 1
    return f"L{k}"


def spread_regime(half_spread_ticks: float) -> str:
    """Arrival spread regime: tight (<= 1 tick wide), normal (<= 3), wide."""
    w = 2.0 * half_spread_ticks
    if w <= 1.0 + 1e-9:
        return "tight"
    if w <= 3.0 + 1e-9:
        return "normal"
    return "wide"


def _t_crit(df: int, q: float) -> float:
    from scipy import stats

    return float(stats.t.ppf(q, df)) if df >= 1 else float("nan")


def ratio_stats(num: Sequence[float], den: Sequence[float], clusters: Sequence[str]) -> dict:
    """Pooled ratio ``sum(num) / sum(den)`` with the CR1 cluster-linearised
    standard error (one cluster per label), Student-t interval at ``G - 1`` df,
    design effect, effective sample size and minimum detectable effect.

    ``n`` counts parents (rows), ``n_clusters`` sessions; ``mde`` is
    :func:`iap.research.study_stats.mde` applied to the per-cluster standard
    deviation (``se * sqrt(G)``, normal approximation) and ``mde_t`` the
    Student-t version ``(t_{0.975} + t_{power}) * se`` at ``G - 1`` df.
    """
    n = len(num)
    if n != len(den) or n != len(clusters):
        raise ValueError("num, den and clusters must have the same length")
    labels = sorted(set(clusters))
    g = len(labels)
    out: dict[str, object] = {
        "n": n,
        "n_clusters": g,
        "estimate": None,
        "se": None,
        "ci95": None,
        "df": None,
        "design_effect": None,
        "ess": None,
        "mde": None,
        "mde_t": None,
    }
    d_sum = float(np.sum(den))
    if n == 0 or d_sum <= 0.0:
        return out
    ratio = float(np.sum(num)) / d_sum
    out["estimate"] = ratio
    if g < MIN_CLUSTERS_FOR_INTERVAL:
        return out
    idx = {lab: k for k, lab in enumerate(labels)}
    cid = np.asarray([idx[c] for c in clusters])
    resid = np.asarray(num, dtype=float) - ratio * np.asarray(den, dtype=float)
    cluster_sum = np.bincount(cid, weights=resid, minlength=g)
    var = g / (g - 1) * float(np.sum(cluster_sum**2)) / (d_sum * d_sum)
    se = math.sqrt(var)
    out["se"] = se
    out["df"] = g - 1
    half = _t_crit(g - 1, 0.975) * se
    out["ci95"] = [ratio - half, ratio + half]
    # independent-sampling variance of the same ratio (rows as clusters)
    iid_var = n / (n - 1) * float(np.sum(resid**2)) / (d_sum * d_sum) if n > 1 else float("nan")
    if iid_var > 0 and math.isfinite(iid_var):
        deff = var / iid_var
        out["design_effect"] = deff
        out["ess"] = n / deff if deff > 0 else None
    out["mde"] = mde(se * math.sqrt(g), g)
    out["mde_t"] = (_t_crit(g - 1, 0.975) + _t_crit(g - 1, POWER)) * se
    return out


def distribution(values: Sequence[float]) -> dict[str, object]:
    """Per-parent distribution: n, mean, sd and the 10 / 25 / 50 / 75 / 90 % quantiles."""
    if not values:
        return {"n": 0, "mean": None, "sd": None, "q10": None, "q25": None, "q50": None,
                "q75": None, "q90": None}  # fmt: skip
    x = np.asarray(values, dtype=float)
    q = np.quantile(x, [0.10, 0.25, 0.50, 0.75, 0.90])
    return {
        "n": int(x.size),
        "mean": float(x.mean()),
        "sd": float(x.std(ddof=1)) if x.size > 1 else None,
        "q10": float(q[0]),
        "q25": float(q[1]),
        "q50": float(q[2]),
        "q75": float(q[3]),
        "q90": float(q[4]),
    }


def _bps(num: float, den: float) -> float | None:
    return None if den <= 0 else 1e4 * num / den


def parent_metrics(row: Mapping[str, float]) -> dict[str, float | None]:
    """The v2 per-parent quantities from a ``parent_costs`` row (currency).

    Needs ``qty``, ``filled_qty``, ``decision_mid``, ``trading``, ``fees_net``,
    ``sim_impact``, ``completion``, ``shortfall``.  Denominators are carried
    so the pooled ratios are ratios of sums, not means of ratios.
    """
    mid = float(row["decision_mid"])
    qty = float(row["qty"])
    filled = float(row["filled_qty"])
    residual = qty - filled
    cost = float(row["trading"]) + float(row["fees_net"]) + float(row["sim_impact"])
    return {
        "residual_qty": residual,
        "completed": 1.0 if filled >= qty else 0.0,
        "fill_rate": filled / qty if qty > 0 else None,
        "filled_notional": filled * mid,
        "residual_notional": residual * mid,
        "target_notional": qty * mid,
        "fill_cost": cost,
        "trading_cost": float(row["trading"]),
        "cost_per_filled_share_bps": _bps(cost, filled * mid),
        "trading_cost_per_filled_share_bps": _bps(float(row["trading"]), filled * mid),
        "completion_charge_bps": _bps(float(row["completion"]), residual * mid),
        "shortfall_per_target_share_bps": _bps(float(row["shortfall"]), qty * mid),
    }


def aggregate(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """The v2 block of one cell / slice: quantity-weighted pooled ratios with
    inference, and the per-parent distribution (module docstring).

    Each row is a benchmark parent row carrying the ``parent_metrics`` keys
    (``m_*`` prefixed by the benchmark), ``session`` and, optionally,
    ``markout_w`` = ``{h: [sum(qty * cost_bps), qty_defined]}``.
    """
    clusters = [str(r["session"]) for r in rows]
    n = len(rows)

    def col(key: str) -> list[float]:
        return [float(r[key]) for r in rows]  # type: ignore[arg-type]

    filled_n = col("m_filled_notional")
    target_n = col("m_target_notional")
    resid_n = col("m_residual_notional")
    qty = col("qty")
    filled = col("filled_qty")
    out: dict[str, object] = {
        "n_parents": n,
        "n_sessions": len(set(clusters)),
        "definitions": "docs/EXECUTION_QUALITY.md section 9 (METRIC_DEFINITIONS)",
        "sign": "positive = worse for the trader",
    }

    # pooled, quantity-weighted (ratio of sums); bps = 1e4 * ratio
    def pooled(num_key: str, den: list[float]) -> dict[str, object]:
        s = ratio_stats(col(num_key), den, clusters)
        for k in ("estimate", "se", "mde", "mde_t"):
            if s[k] is not None:
                s[k] = 1e4 * float(s[k])  # type: ignore[arg-type]
        if s["ci95"] is not None:
            s["ci95"] = [1e4 * float(v) for v in s["ci95"]]  # type: ignore[union-attr]
        return s

    out["cost_per_filled_share_bps"] = pooled("m_fill_cost", filled_n)
    out["trading_cost_per_filled_share_bps"] = pooled("m_trading_cost", filled_n)
    out["completion_charge_bps"] = pooled("completion", resid_n)
    out["shortfall_per_target_share_bps"] = pooled("shortfall", target_n)
    fr = ratio_stats(filled, qty, clusters)
    out["fill_rate"] = fr
    out["completion_ratio"] = ratio_stats(col("m_completed"), [1.0] * n, clusters)
    resid = col("m_residual_qty")
    out["residual_qty"] = {
        "total": float(sum(resid)),
        "mean_per_parent": (sum(resid) / n) if n else None,
        "share_of_target": (sum(resid) / sum(qty)) if sum(qty) > 0 else None,
    }
    # arrival markout = the filled quantity's trading cost against the arrival mid
    out["arrival_markout_bps"] = out["trading_cost_per_filled_share_bps"]
    # post-trade markouts (cost sign: positive = adverse), qty-weighted by horizon
    hs = sorted({h for r in rows for h in (r.get("markout_w") or {})})  # type: ignore[attr-defined]
    post: dict[str, object] = {}
    for h in hs:
        num = [float((r.get("markout_w") or {}).get(h, [0.0, 0.0])[0]) for r in rows]  # type: ignore[attr-defined]
        den = [float((r.get("markout_w") or {}).get(h, [0.0, 0.0])[1]) for r in rows]  # type: ignore[attr-defined]
        s = ratio_stats(num, den, clusters)  # already in bps x qty units
        post[h] = s
    out["post_trade_markout_bps"] = post
    # per-parent distribution (parents that filled something)
    per_parent = [
        float(r["m_cost_per_filled_share_bps"])  # type: ignore[arg-type]
        for r in rows
        if r.get("m_cost_per_filled_share_bps") is not None
    ]
    out["per_parent"] = {
        "cost_per_filled_share_bps": distribution(per_parent),
        "fill_rate": distribution([a / b for a, b in zip(filled, qty, strict=True) if b > 0]),
        "parents_with_no_fill": sum(1 for f in filled if f <= 0),
    }
    return out


def split_by(rows: Sequence[Mapping[str, object]], key: str) -> dict[str, dict[str, object]]:
    """``aggregate`` per distinct value of ``key`` (regime, size, liquidity)."""
    out: dict[str, dict[str, object]] = {}
    for v in sorted({str(r[key]) for r in rows if r.get(key) is not None}):
        out[v] = aggregate([r for r in rows if str(r.get(key)) == v])
    return out


def definitions_markdown() -> str:
    """The metric-definition table (for the report and docs)."""
    head = (
        "| metric | arrival price | target qty | horizon | benchmark | sign | unit | denominator |"
    )
    lines = [head, "|---|---|---|---|---|---|---|---|"]
    for name, d in METRIC_DEFINITIONS.items():
        lines.append(
            f"| `{name}` | {d['arrival_price']} | {d['target_qty']} | {d['horizon']} | "
            f"{d['benchmark']} | {d['sign']} | {d['unit']} | {d['denominator']} |"
        )
    return "\n".join(lines)
