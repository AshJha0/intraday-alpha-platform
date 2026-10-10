"""``python -m iap.auction`` - extract the NOII stream, then backtest.

    python -m iap.auction extract --itch FILE --date YYYY-MM-DD \\
        --symbols AAPL MSFT --out DIR
    python -m iap.auction backtest --stream DIR [DIR ...] --cross-type C \\
        --decision-s 300 --exit cross [--mids CSV] --out REPORT.json

``backtest`` refuses to run unless the hypothesis ``AUC01`` at horizon
``<cross_type>-<decision_s>s`` (e.g. ``C-300s``) is pre-registered on the
research blackboard (:mod:`iap.agents.prereg_gate`, read only);
``--no-prereg`` makes the run exploratory and says so.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from iap.auction.features import auction_features
from iap.auction.strategy import AuctionStrategyConfig, backtest, walk_forward
from iap.auction.stream import AuctionStream, extract_auction_stream, read_auction_stream
from iap.auction.targets import auction_targets
from iap.backtest.costs import CostModel

ALPHA_ID = "AUC01"
REPO_ROOT = Path(__file__).resolve().parents[4]


def _extract(a: argparse.Namespace) -> int:
    s = extract_auction_stream(a.itch, symbols=a.symbols, date=a.date)
    s.write(a.out)
    print(
        f"{a.out}: {s.meta['noii_messages']} NOII snapshots, {s.meta['cross_messages']} "
        f"cross prints (file has {s.meta['noii_messages_in_file']} NOII messages)"
    )
    return 0


def _backtest(a: argparse.Namespace) -> int:
    horizon = f"{a.cross_type}-{int(a.decision_s)}s"
    if a.no_prereg:
        print("NOT pre-registered (--no-prereg): exploratory, not evidence for a gate")
        prereg = None
    else:
        from iap.agents.prereg_gate import PreregistrationError, require

        try:
            prereg = require(REPO_ROOT, ALPHA_ID, horizon, a.prereg_board)
        except PreregistrationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    stream = AuctionStream.concat(read_auction_stream(d) for d in a.stream)
    mids = pd.read_csv(a.mids, dtype={"symbol": str}) if a.mids else None
    labels = auction_targets(auction_features(stream.noii, a.cross_type), stream.crosses, mids)
    cfg = AuctionStrategyConfig(
        cross_type=a.cross_type,
        decision_s=a.decision_s,
        threshold=a.threshold,
        exit=a.exit,
        qty=a.qty,
        default_half_spread_bps=a.half_spread_bps,
    )
    cm = CostModel.load(a.execution_config)
    report = {
        "alpha": ALPHA_ID,
        "horizon": horizon,
        "preregistered": prereg is not None,
        "mid_source": str(labels["mid_source"].iloc[0]) if len(labels) else None,
        "in_sample": backtest(labels, cfg, cm).to_dict(),
    }
    if a.walk_forward:
        report["walk_forward"] = walk_forward(labels, cfg, cm, n_folds=a.folds).to_dict()
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    if a.out:
        Path(a.out).write_text(text + "\n")
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m iap.auction", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="ITCH 5.0 file -> auction-imbalance stream directory")
    e.add_argument("--itch", required=True)
    e.add_argument("--date", required=True, help="session date YYYY-MM-DD")
    e.add_argument("--symbols", nargs="+", default=None)
    e.add_argument("--out", required=True)
    b = sub.add_parser("backtest", help="features, targets, backtest and walk-forward")
    b.add_argument("--stream", nargs="+", required=True)
    b.add_argument("--cross-type", choices=("C", "O"), default="C")
    b.add_argument("--decision-s", type=float, default=300.0)
    b.add_argument("--threshold", type=float, default=0.2)
    b.add_argument("--exit", choices=("cross", "taker"), default="cross")
    b.add_argument("--qty", type=int, default=100)
    b.add_argument("--half-spread-bps", type=float, default=1.0)
    b.add_argument("--mids", default=None, help="CSV symbol,ts,mid[,half_spread]")
    b.add_argument(
        "--execution-config", default=str(REPO_ROOT / "configs/execution/execution.json")
    )
    b.add_argument("--walk-forward", action="store_true")
    b.add_argument("--folds", type=int, default=4)
    b.add_argument("--no-prereg", action="store_true")
    b.add_argument("--prereg-board", default=None)
    b.add_argument("--out", default=None)
    a = p.parse_args(argv)
    return _extract(a) if a.cmd == "extract" else _backtest(a)


if __name__ == "__main__":
    raise SystemExit(main())
