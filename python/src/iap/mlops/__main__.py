"""CLI: ``python -m iap.mlops {list,show,verify,lineage,monitor}``.

``monitor`` reads two ``.npz`` files (reference and current); every array is
a feature column except the reserved names ``__pred__`` (model output) and
``__y__`` (realised target).  It prints the JSON report (or writes it with
``--out``) and exits 1 on ALERT.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from iap.mlops.monitoring import MonitorThresholds, monitor
from iap.mlops.registry import ModelRegistry, RegistryError

RESERVED = ("__pred__", "__y__")


def _split(path: str) -> tuple[dict, np.ndarray | None, np.ndarray | None]:
    with np.load(path) as z:
        feats = {k: z[k] for k in z.files if k not in RESERVED}
        pred = z["__pred__"] if "__pred__" in z.files else None
        y = z["__y__"] if "__y__" in z.files else None
    return feats, pred, y


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m iap.mlops")
    ap.add_argument("--registry", default="research/models/registry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for c in ("show", "verify", "lineage"):
        sub.add_parser(c).add_argument("model_id")
    m = sub.add_parser("monitor")
    m.add_argument("--reference", required=True)
    m.add_argument("--current", required=True)
    m.add_argument("--kind", choices=("classifier", "regressor"), required=True)
    m.add_argument("--model-id")
    m.add_argument("--psi-alert", type=float, default=MonitorThresholds.psi_alert)
    m.add_argument("--ece-max", type=float, default=MonitorThresholds.ece_max)
    m.add_argument("--ic-decay", type=float, default=MonitorThresholds.ic_decay)
    m.add_argument("--out")
    a = ap.parse_args(argv)
    reg = ModelRegistry(a.registry)
    try:
        if a.cmd == "list":
            for r in reg.list():
                tag = "exploratory" if r.exploratory else f"prereg {r.prereg_hash[:12]}"
                print(f"{r.model_id[:16]}  {r.name:<20} {r.kind:<10} {r.dataset_version}  {tag}")
            return 0
        if a.cmd in ("show", "verify"):
            rec = reg.verify(a.model_id) if a.cmd == "verify" else reg.get(a.model_id)
            print(json.dumps(rec.to_dict(), indent=2, sort_keys=True))
            return 0
        if a.cmd == "lineage":
            for r in reg.lineage(a.model_id):
                print(f"{r.model_id}  {r.name}")
            return 0
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    rf, rp, ry = _split(a.reference)
    cf, cp, cy = _split(a.current)
    th = MonitorThresholds(psi_alert=a.psi_alert, ece_max=a.ece_max, ic_decay=a.ic_decay)
    rep = monitor(
        kind=a.kind,
        ref_features=rf,
        cur_features=cf,
        ref_pred=rp,
        cur_pred=cp,
        ref_y=ry,
        cur_y=cy,
        thresholds=th,
        model_id=a.model_id,
    )
    text = json.dumps(rep, indent=2, sort_keys=True)
    if a.out:
        Path(a.out).write_text(text + "\n")
    print(text if not a.out else f"{rep['status']}: {', '.join(rep['alerts']) or 'no alerts'}")
    return 1 if rep["status"] == "ALERT" else 0


if __name__ == "__main__":
    raise SystemExit(main())
