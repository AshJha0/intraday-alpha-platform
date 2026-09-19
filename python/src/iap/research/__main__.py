"""``python -m iap.research`` — run, list and show experiments.

::

    python -m iap.research run --alpha EQ03 [--horizon 1s] [--config n_folds=3 ...]
                               [--seed N] [--dry-run]
    python -m iap.research list [--alpha EQ03] [--horizon 1s]
    python -m iap.research show <experiment_id>

``run`` builds the spec (:func:`iap.research.build_spec`), runs it through
:class:`iap.research.ExperimentRunner`, prints the result table, the
verdict and the ledger's multiple-testing note (the deflated-Sharpe-style
"expected max |t| under the global null" line every report carries), and
persists ``research/experiments/<experiment_id>/{spec,result}.json`` plus
the ledger unless ``--dry-run``.  Paths default to the checkout; every one
can be overridden for a scratch store.  ``--config`` values are parsed as
JSON (``n_folds=3``, ``cost_multiplier=2.0``, ``flatten_at_session_end=false``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from iap.contracts.types import ExperimentResult, ExperimentSpec
from iap.research.errors import ResearchError
from iap.research.registry import ExperimentRegistry
from iap.research.runner import ExperimentRunner
from iap.research.specs import DEFAULT_SEED, build_spec
from iap.validation.ledger import ExperimentLedger

REPO = Path(__file__).resolve().parents[4]

#: (label, field, format) rows of the result table, in reading order.
_RESULT_ROWS = (
    ("IC (pooled OOS, z)", "ic", "+.6f"),
    ("rank IC", "rank_ic", "+.6f"),
    ("NW t-stat", "t_stat", "+.4f"),
    ("NW lags", "nw_lags", "d"),
    ("hit rate", "hit_rate", ".4f"),
    ("turnover (flips/h)", "turnover", ".2f"),
    ("fold sign consistency", "fold_consistency", ".2f"),
    ("folds", "n_folds", "d"),
    ("leakage passed", "leakage_passed", ""),
    ("hypothesis sign confirmed", "hypothesis_sign_confirmed", ""),
    ("holdout gross return (bps)", "gross_return_bps", "+.4f"),
    ("holdout transaction cost (bps)", "transaction_cost_bps", ".4f"),
    ("holdout net return (bps)", "net_return_bps", "+.4f"),
    ("holdout max drawdown (bps)", "max_drawdown_bps", ".4f"),
    ("holdout Sharpe (ann.)", "sharpe", "+.4f"),
    ("experiments in ledger", "n_experiments_in_ledger", "d"),
    ("git commit", "git_commit", ""),
    ("created_ts (test end, ns)", "created_ts", "d"),
)


def _parse_config(items: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            raise ResearchError(f"--config expects key=value, got {item!r}")
        try:
            out[key] = json.loads(raw)
        except ValueError:
            out[key] = raw
    return out


def _fmt(value: Any, spec: str) -> str:
    if spec == "":
        return str(value).lower() if isinstance(value, bool) else str(value)
    return format(value, spec)


def render_spec(spec: ExperimentSpec) -> str:
    """Human-readable spec block."""
    cfg = ", ".join(f"{k}={_fmt(v, '')}" for k, v in spec.configuration.items())
    rows = [
        ("experiment", spec.experiment_id),
        ("alpha", spec.alpha_id),
        ("horizon", spec.horizon),
        ("dataset_version", spec.dataset_version),
        ("feature_version", spec.feature_version),
        ("model_version", str(spec.model_version)),
        ("configuration", cfg),
        ("train period", f"[{spec.train_period.start_ts}, {spec.train_period.end_ts})"),
        ("validation period",
         f"[{spec.validation_period.start_ts}, {spec.validation_period.end_ts})"),
        ("test period", f"[{spec.test_period.start_ts}, {spec.test_period.end_ts})"),
        ("seed", str(spec.seed)),
    ]
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"{label:<{width}}  {value}" for label, value in rows)


def render_result(result: ExperimentResult) -> str:
    """The result table followed by the verdict line."""
    width = max(len(label) for label, _, _ in _RESULT_ROWS)
    lines = [f"{label:<{width}}  {_fmt(getattr(result, field), spec)}"
             for label, field, spec in _RESULT_ROWS]
    lines.append("")
    lines.append(f"VERDICT: {result.verdict.value}")
    return "\n".join(lines)


def render_ledger_note(ledger: ExperimentLedger) -> str:
    """The multiple-testing denominator note (conventions §7)."""
    return (
        f"{ledger.note()}\n"
        f"ledger: {ledger.total_experiments} looks over "
        f"{ledger.distinct_experiments} distinct configurations; expected max |t| "
        f"under the global null ~{ledger.expected_max_null_t():.2f}, Bonferroni "
        f"|t| >= {ledger.bonferroni_t_threshold():.2f}."
    )


def _run(args: argparse.Namespace) -> int:
    runner = ExperimentRunner(
        args.features_dir, args.ledger, args.out_dir, args.configs_dir,
        dry_run=args.dry_run, repo_root=args.repo_root,
    )
    spec = build_spec(
        args.alpha, args.horizon, _parse_config(args.config), seed=args.seed,
        frames=runner.frames(), repo_root=args.repo_root,
    )
    print(render_spec(spec))
    print()
    result = runner.run(spec)
    print(render_result(result))
    print()
    print(render_ledger_note(runner.ledger))
    if args.dry_run:
        print("\n(dry run: nothing was written)")
    else:
        print(f"\nwrote {runner.experiment_dir(spec.experiment_id)}/{{spec,result}}.json "
              f"and {runner.ledger_path}")
    return 0


def _list(args: argparse.Namespace) -> int:
    registry = ExperimentRegistry(args.out_dir)
    records = registry.find(alpha_id=args.alpha, horizon=args.horizon)
    if not records:
        print(f"no experiments under {registry.root}")
        return 0
    print(f"{'experiment':<16}  {'alpha':<6}  {'horizon':<7}  {'IC':>10}  "
          f"{'NW t':>8}  {'net bps':>10}  verdict")
    for rec in records:
        r = rec.result
        print(f"{rec.experiment_id:<16}  {rec.spec.alpha_id:<6}  {rec.spec.horizon:<7}  "
              f"{r.ic:>+10.6f}  {r.t_stat:>+8.3f}  {r.net_return_bps:>+10.4f}  "
              f"{r.verdict.value}")
    return 0


def _show(args: argparse.Namespace) -> int:
    rec = ExperimentRegistry(args.out_dir).load(args.experiment_id)
    print(render_spec(rec.spec))
    print()
    print(render_result(rec.result))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m iap.research",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, default=REPO / "research" / "experiments",
                        help="experiments folder (default: research/experiments)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one experiment")
    run.add_argument("--alpha", required=True, help="flagship alpha id, e.g. EQ03")
    run.add_argument("--horizon", default=None,
                     help="label horizon (default: the alpha's pinned horizon)")
    run.add_argument("--config", action="append", default=[], metavar="KEY=VALUE",
                     help="configuration override (JSON value); repeatable")
    run.add_argument("--seed", type=int, default=DEFAULT_SEED,
                     help=f"spec seed, u64 (default {DEFAULT_SEED})")
    run.add_argument("--dry-run", action="store_true",
                     help="compute and print, write nothing")
    run.add_argument("--features-dir", type=Path, default=REPO / "data" / "features")
    run.add_argument("--ledger", type=Path, default=REPO / "research" / "experiments.json")
    run.add_argument("--configs-dir", type=Path, default=REPO / "configs")
    run.add_argument("--repo-root", type=Path, default=None,
                     help="checkout to version (default: this one)")
    run.set_defaults(func=_run)

    lst = sub.add_parser("list", help="list persisted experiments")
    lst.add_argument("--alpha", default=None)
    lst.add_argument("--horizon", default=None)
    lst.set_defaults(func=_list)

    show = sub.add_parser("show", help="print one experiment's spec and result")
    show.add_argument("experiment_id")
    show.set_defaults(func=_show)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ResearchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
