"""AI4 DeepLOB training and in-sample evaluation on the real LOB cache (v1.16).

IN-SAMPLE model development for the AI4 rebuild (research/r7/AI4_DEFERRED.md):
reads only the cache written by :mod:`iap.models.lob_real` (which refuses the
R7 holdout sessions) and refuses any holdout unit itself.

Protocol (chronological, pooled across symbols as the owner decided for
AI4-DEEPLOB-1):

- units are ``<session>_<symbol>``; the ``--val`` sessions (default the last
  two in-sample sessions by date) are validation, the rest training;
- the column normaliser is fitted on the training units only (streamed);
- training windows are every ``--stride``-th labelled window end, capped at
  ``--max-train`` (seeded subsample); DeepLOB / MLP train with a streaming
  loop (memory O(batch)); the sklearn baselines (``iap.models.
  deeplob_baselines.BASELINES``) fit on a seeded subsample of
  ``--baseline-train`` windows (flattened windows are ``W x 4L`` wide);
- evaluation decides every ``horizon`` events on the validation units (the
  taker trades are non-overlapping at that horizon anyway) and scores each
  model with :func:`net_of_cost_mid` (the ``net_of_cost`` rule of the
  baselines module on the cache's mid and its spread rebuilt from the tick
  offsets, plus a taker fee in bp per round trip), per symbol and pooled;
- :func:`iap.models.deeplob_baselines.accept` is reported per symbol with the
  registered ``min_confidence`` / ``min_trades``.

This is model SELECTION on in-sample data. It is not a confirmatory look and
writes nothing to the ledger; the one holdout test needs a new owner-signed
pre-registration.

CLI: ``python -m iap.models.lob_train --cache DIR --out DIR [options]``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from iap.models.deeplob import N_CLASSES
from iap.models.deeplob_baselines import (
    BASELINES,
    Z_95_ONE_SIDED,
    accept,
    directions,
    macro_f1,
)
from iap.models.lob_real import (
    WindowIndex,
    _refuse_holdout,
    fit_column_normalizer,
    load_unit,
    unit_paths,
)

__all__ = ["net_of_cost_mid", "train_eval"]


def net_of_cost_mid(
    mid: np.ndarray,
    spread_px: np.ndarray,
    end_idx: np.ndarray,
    direction: np.ndarray,
    horizon: int,
    fee_bp: float = 0.0,
) -> dict[str, Any]:
    """Taker P&L per non-overlapping trade, in bp: enter at ``t`` crossing
    half the spread, exit at ``t + horizon`` crossing half the spread, plus
    ``fee_bp`` per round trip (the ``deeplob_baselines.net_of_cost`` rule on
    a mid series and a spread series instead of an absolute snapshot)."""
    end_idx = np.asarray(end_idx, dtype=np.int64)
    direction = np.asarray(direction, dtype=np.int64)
    sig = np.flatnonzero(direction != 0)
    keep: list[int] = []
    nxt = -np.inf
    for k in sig:
        t = end_idx[k]
        if t >= nxt and t + horizon < len(mid):
            keep.append(k)
            nxt = t + horizon
    trades = np.asarray(keep, dtype=np.int64)
    if trades.size == 0:
        return {"n_signals": int(sig.size), "n_trades": 0, "mean_net_bp": 0.0, "std_net_bp": 0.0,
                "lower95_bp": 0.0, "gross_bp": 0.0, "cost_bp": 0.0}  # fmt: skip
    t = end_idx[trades]
    gross = direction[trades] * (mid[t + horizon] / mid[t] - 1.0) * 1e4
    cost = 0.5 * (spread_px[t] / mid[t] + spread_px[t + horizon] / mid[t + horizon]) * 1e4 + fee_bp
    net = gross - cost
    n = net.size
    sd = float(net.std(ddof=1)) if n > 1 else 0.0
    mean = float(net.mean())
    return {
        "n_signals": int(sig.size),
        "n_trades": int(n),
        "mean_net_bp": mean,
        "std_net_bp": sd,
        "lower95_bp": float(mean - Z_95_ONE_SIDED * sd / np.sqrt(n)) if n > 1 else float("-inf"),
        "gross_bp": float(gross.mean()),
        "cost_bp": float(cost.mean()),
    }


def _split(
    units: Sequence[Path], val_sessions: Sequence[str] | None
) -> tuple[list[Path], list[Path]]:
    sessions = sorted({p.name.split("_")[0] + "_" + p.name.split("_")[1] for p in units})
    for p in units:
        _refuse_holdout(p.name)

    def date_of(sess: str) -> str:
        return sess.split("_")[-1]

    by_date = sorted(sessions, key=date_of)
    val = set(val_sessions) if val_sessions else set(by_date[-2:])
    tr = [p for p in units if not any(p.name.startswith(v + "_") for v in val)]
    va = [p for p in units if any(p.name.startswith(v + "_") for v in val)]
    return tr, va


def _symbol(p: Path) -> str:
    return p.name.removesuffix(".meta.npz").rsplit("_", 1)[-1]


def _torch_fit(
    arch: str,
    wi: WindowIndex,
    idx: np.ndarray,
    *,
    epochs: int,
    batch: int,
    lr: float,
    seed: int,
    log,
):
    import torch
    from torch import nn

    from iap.models.deeplob_torch import DeepLOBNet, MLPBaselineNet

    torch.manual_seed(seed)
    w, f = wi.window, wi.units[0].shape[1]
    net = DeepLOBNet(levels=f // 4) if arch == "deeplob" else MLPBaselineNet(w, f)
    opt = torch.optim.Adam(net.parameters(), lr=lr)
    loss_fn = nn.CrossEntropyLoss()
    rng = np.random.default_rng(seed)
    net.train()
    for ep in range(epochs):
        perm = rng.permutation(idx)
        total, nb, t0 = 0.0, 0, time.time()
        for s in range(0, len(perm), batch):
            b = perm[s : s + batch]
            if len(b) < 2:
                continue
            xb, yb = wi.batch(b)
            xt = torch.as_tensor(xb)
            xt = xt.unsqueeze(1) if arch == "deeplob" else xt
            opt.zero_grad()
            loss = loss_fn(net(xt), torch.as_tensor(yb))
            loss.backward()
            opt.step()
            total += float(loss.detach())
            nb += 1
        log(
            f"{arch} epoch {ep + 1}/{epochs}: loss {total / max(nb, 1):.4f} ({time.time() - t0:.0f}s)"
        )
    net.eval()
    return net


def _torch_proba(arch: str, net, wi: WindowIndex, idx: np.ndarray, batch: int) -> np.ndarray:
    import torch

    out = np.empty((len(idx), N_CLASSES), dtype=np.float32)
    with torch.no_grad():
        for s in range(0, len(idx), batch):
            xb, _ = wi.batch(idx[s : s + batch])
            xt = torch.as_tensor(xb)
            xt = xt.unsqueeze(1) if arch == "deeplob" else xt
            out[s : s + len(xb)] = torch.softmax(net(xt), dim=1).numpy()
    return out


def _sk_proba(model, wi: WindowIndex, idx: np.ndarray, batch: int = 4096) -> np.ndarray:
    out = np.empty((len(idx), N_CLASSES), dtype=np.float32)
    for s in range(0, len(idx), batch):
        xb, _ = wi.batch(idx[s : s + batch])
        out[s : s + len(xb)] = model.predict_proba(xb)
    return out


def train_eval(
    cache: Path,
    out: Path,
    *,
    models: Sequence[str] = ("deeplob", "linear", "gbm"),
    window: int = 100,
    horizon: int = 50,
    threshold: float = 2e-5,
    stride: int = 50,
    max_train: int = 300_000,
    baseline_train: int = 30_000,
    epochs: int = 3,
    batch: int = 256,
    lr: float = 1e-3,
    min_confidence: float = 0.6,
    min_trades: int = 30,
    taker_fee_per_share: float = 0.003,
    val_sessions: Sequence[str] | None = None,
    seed: int = 20261014,
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    logf = (out / "train.log").open("a", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    tr_paths, va_paths = _split(unit_paths(cache), val_sessions)
    if not tr_paths or not va_paths:
        raise ValueError("need training and validation units")
    log(f"train units {len(tr_paths)}, val units {len(va_paths)}: {[p.name for p in va_paths]}")
    tr_units = [load_unit(p) for p in tr_paths]
    norm = fit_column_normalizer([u[2] for u in tr_units])
    tr = WindowIndex([(u[2], u[1]) for u in tr_units], window=window, horizon=horizon,
                     threshold=threshold, normalizer=norm, stride=stride)  # fmt: skip
    rng = np.random.default_rng(seed)
    tr_idx = np.arange(len(tr))
    if len(tr_idx) > max_train:
        tr_idx = np.sort(rng.choice(tr_idx, max_train, replace=False))
    labels = tr.label_array()[tr_idx]
    log(
        f"train windows {len(tr_idx)} (of {len(tr)}), class shares {np.bincount(labels, minlength=3) / len(labels)}"
    )

    va_units = [load_unit(p) for p in va_paths]
    # decide every `horizon` events on validation (non-overlapping taker trades)
    va = WindowIndex([(u[2], u[1]) for u in va_units], window=window, horizon=horizon,
                     threshold=threshold, normalizer=norm, stride=horizon)  # fmt: skip
    va_idx = np.arange(len(va))
    y_va = va.label_array()
    vu, vt = va.ends()
    log(f"validation decisions {len(va_idx)}")

    probas: dict[str, np.ndarray] = {}
    for name in models:
        t0 = time.time()
        if name in ("deeplob", "mlp"):
            net = _torch_fit(
                name, tr, tr_idx, epochs=epochs, batch=batch, lr=lr, seed=seed, log=log
            )
            import torch

            torch.save(net.state_dict(), out / f"{name}.pt")
            probas[name] = _torch_proba(name, net, va, va_idx, batch * 4)
        elif name in BASELINES:
            b_idx = tr_idx if len(tr_idx) <= baseline_train else np.sort(
                rng.choice(tr_idx, baseline_train, replace=False))  # fmt: skip
            xb, yb = tr.batch(b_idx)
            model = BASELINES[name](seed).fit(xb, yb)
            del xb
            probas[name] = _sk_proba(model, va, va_idx)
        else:
            raise ValueError(f"unknown model {name!r}")
        log(f"{name}: trained + scored in {time.time() - t0:.0f}s")

    symbols = sorted({_symbol(p) for p in va_paths})
    results: dict[str, Any] = {"by_symbol": {}, "pooled": {}}
    for name, proba in probas.items():
        pred = proba.argmax(axis=1)
        d = directions(proba, min_confidence)
        per_sym: dict[str, Any] = {}
        pooled_net: list[dict] = []
        for sym in symbols:
            nets = []
            for u, p in enumerate(va_paths):
                if _symbol(p) != sym:
                    continue
                m = vu == u
                ts, mid, x = va_units[u]
                tick = float(np.load(p)["tick"])
                spread_px = (
                    np.asarray(x[:, 0], dtype=np.float64) - np.asarray(x[:, 2], dtype=np.float64)
                ) * tick
                fee_bp = 2.0 * taker_fee_per_share / float(np.median(mid)) * 1e4
                nets.append((mid, spread_px, vt[m], d[m], fee_bp))
            # per symbol: concatenate trade P&Ls across its sessions
            rs = [net_of_cost_mid(*n[:4], horizon, n[4]) for n in nets]
            pooled_net += rs
            per_sym[sym] = _combine(rs)
            sel = np.isin(vu, [u for u, q in enumerate(va_paths) if _symbol(q) == sym])
            per_sym[sym]["accuracy"] = float((pred[sel] == y_va[sel]).mean()) if sel.any() else None
        results["by_symbol"][name] = per_sym
        results["pooled"][name] = {**_combine(pooled_net), "accuracy": float((pred == y_va).mean()),
                                   "macro_f1": macro_f1(y_va, pred)}  # fmt: skip
    acceptance = {}
    if "deeplob" in probas and all(b in probas for b in BASELINES):
        for sym in symbols:
            res = {m: {"net": results["by_symbol"][m][sym]} for m in probas}
            acceptance[sym] = accept(res, "deeplob", min_trades=min_trades)
    doc = {
        "status": "IN-SAMPLE MODEL SELECTION (not a confirmatory look; no ledger entry)",
        "cache": str(cache),
        "train_units": [p.name for p in tr_paths],
        "val_units": [p.name for p in va_paths],
        "params": {
            "window": window,
            "horizon": horizon,
            "threshold": threshold,
            "stride": stride,
            "max_train": max_train,
            "baseline_train": baseline_train,
            "epochs": epochs,
            "batch": batch,
            "lr": lr,
            "min_confidence": min_confidence,
            "min_trades": min_trades,
            "taker_fee_per_share": taker_fee_per_share,
            "seed": seed,
        },  # fmt: skip
        "normalizer": norm.to_dict(),
        "results": results,
        "acceptance_in_sample": acceptance,
    }
    (out / "ai4_insample.json").write_text(
        json.dumps(doc, indent=1, default=float) + "\n", encoding="utf-8"
    )
    log(f"wrote {out / 'ai4_insample.json'}")
    logf.close()
    return doc


def _combine(rs: Sequence[dict]) -> dict[str, Any]:
    """Pool per-session trade summaries (count-weighted mean, pooled sd)."""
    n = sum(r["n_trades"] for r in rs)
    if n == 0:
        return {"n_trades": 0, "n_signals": sum(r["n_signals"] for r in rs), "mean_net_bp": 0.0,
                "std_net_bp": 0.0, "lower95_bp": 0.0, "gross_bp": 0.0, "cost_bp": 0.0}  # fmt: skip
    mean = sum(r["mean_net_bp"] * r["n_trades"] for r in rs) / n
    ss = sum((r["n_trades"] - 1) * r["std_net_bp"] ** 2 + r["n_trades"] * (r["mean_net_bp"] - mean) ** 2
             for r in rs if r["n_trades"])  # fmt: skip
    sd = float(np.sqrt(ss / (n - 1))) if n > 1 else 0.0
    return {
        "n_trades": int(n),
        "n_signals": int(sum(r["n_signals"] for r in rs)),
        "mean_net_bp": float(mean),
        "std_net_bp": sd,
        "lower95_bp": float(mean - Z_95_ONE_SIDED * sd / np.sqrt(n)) if n > 1 else float("-inf"),
        "gross_bp": float(sum(r["gross_bp"] * r["n_trades"] for r in rs) / n),
        "cost_bp": float(sum(r["cost_bp"] * r["n_trades"] for r in rs) / n),
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--models", nargs="+", default=["deeplob", "linear", "gbm"])
    ap.add_argument("--stride", type=int, default=50)
    ap.add_argument("--max-train", type=int, default=300_000)
    ap.add_argument("--baseline-train", type=int, default=30_000)
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument(
        "--val", nargs="*", default=None, help="validation sessions (default: last two)"
    )
    a = ap.parse_args(argv)
    train_eval(a.cache, a.out, models=a.models, stride=a.stride, max_train=a.max_train,
               baseline_train=a.baseline_train, epochs=a.epochs, batch=a.batch, val_sessions=a.val)  # fmt: skip
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
