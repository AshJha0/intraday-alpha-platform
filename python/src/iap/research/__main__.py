"""``python -m iap.research`` — run, list and show experiments; power study.

::

    python -m iap.research [--json-errors] run --alpha EQ03 [--horizon 1s]
                               [--config n_folds=3 ...] [--seed N] [--dry-run]
                               [--tstat-threshold fixed|ledger]
    python -m iap.research list [--alpha EQ03] [--horizon 1s] [--json]
    python -m iap.research show <experiment_id> [--json]
    python -m iap.research power [--levels 0,0.5,1,2] [--seeds 3]
                               [--generator-config PATH] [--out-dir research/power]

``run`` builds the spec (:func:`iap.research.build_spec`), runs it through
:class:`iap.research.ExperimentRunner`, prints the result table, the
verdict, whether the result is gate-eligible and the ledger's
multiple-testing note (the deflated-Sharpe-style "expected max |t| under the
global null" line every report carries), and persists
``research/experiments/<experiment_id>/{spec,result,eligibility}.json`` plus
the ledger.  ``--dry-run`` writes no experiment directory but STILL debits
the looks in the ledger (a dry run is a look).  Paths default to the
checkout; every one can be overridden for a scratch store.  ``--config``
values are parsed as JSON (``n_folds=3``, ``cost_multiplier=2.0``,
``flatten_at_session_end=false``).

``power`` runs the planted-signal power study (:mod:`iap.research.power`)
and writes ``POWER_REPORT.md`` / ``POWER_REPORT.json``.

**For tools.**  ``list --json`` and ``show --json`` print ONE JSON document
on stdout (sorted keys): ``list`` gives ``{"experiments": [...], "skipped":
[...]}`` — each experiment with its spec summary, result, and gate
eligibility; ``skipped`` names every directory that could not be loaded and
why — and ``show`` gives ``{"spec", "result", "gate_eligibility"}``.
``--json-errors`` (a top-level flag, before the subcommand) makes every
failure print exactly one JSON object on stderr,
``{"error": {"code", "message"}}``, with a stable ``code``
(:class:`iap.research.errors.ResearchError`; usage errors are
``usage_error`` and exit 2, everything else exits 1).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

from iap.contracts.types import ExperimentResult, ExperimentSpec
from iap.research.errors import ResearchError
from iap.research.registry import ExperimentRecord, ExperimentRegistry
from iap.research.runner import ExperimentRunner
from iap.research.specs import DEFAULT_SEED, GateEligibility, build_spec
from iap.validation.ledger import ExperimentLedger
from iap.validation.validate import TSTAT_THRESHOLD_POLICIES

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


def _parse_config(items: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            raise ResearchError(f"--config expects key=value, got {item!r}", code="invalid_spec")
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
        (
            "validation period",
            f"[{spec.validation_period.start_ts}, {spec.validation_period.end_ts})",
        ),
        ("test period", f"[{spec.test_period.start_ts}, {spec.test_period.end_ts})"),
        ("seed", str(spec.seed)),
    ]
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"{label:<{width}}  {value}" for label, value in rows)


def render_result(result: ExperimentResult) -> str:
    """The result table followed by the verdict line."""
    width = max(len(label) for label, _, _ in _RESULT_ROWS)
    lines = [
        f"{label:<{width}}  {_fmt(getattr(result, field), spec)}"
        for label, field, spec in _RESULT_ROWS
    ]
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


def render_eligibility(eligibility: GateEligibility) -> str:
    """One line (plus one per reason) on whether the result is gate evidence."""
    if eligibility.eligible:
        note = (
            ""
            if eligibility.periods_verified
            else " (configuration only; periods not verified against a dataset)"
        )
        return f"gate eligible: yes{note}"
    lines = ["gate eligible: NO — recorded and ledgered, but not promotion evidence"]
    lines += [f"  - {reason}" for reason in eligibility.reasons]
    return "\n".join(lines)


def _eligibility_doc(eligibility: GateEligibility) -> dict[str, Any]:
    return {
        "gate_eligible": eligibility.eligible,
        "periods_verified": eligibility.periods_verified,
        "reasons": list(eligibility.reasons),
    }


def _record_doc(registry: ExperimentRegistry, rec: ExperimentRecord) -> dict[str, Any]:
    """The ``--json`` form of one experiment."""
    return {
        "experiment_id": rec.experiment_id,
        "spec": rec.spec.to_dict(),
        "result": rec.result.to_dict(),
        "gate_eligibility": _eligibility_doc(registry.gate_eligibility(rec.experiment_id)),
    }


def _print_json(doc: Any) -> None:
    print(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False))


def _run(args: argparse.Namespace) -> int:
    runner = ExperimentRunner(
        args.features_dir,
        args.ledger,
        args.out_dir,
        args.configs_dir,
        dry_run=args.dry_run,
        repo_root=args.repo_root,
        tstat_threshold=args.tstat_threshold,
    )
    spec = build_spec(
        args.alpha,
        args.horizon,
        _parse_config(args.config),
        seed=args.seed,
        frames=runner.frames(),
        repo_root=args.repo_root,
    )
    print(render_spec(spec))
    print()
    result = runner.run(spec)
    print(render_result(result))
    if runner.last_eligibility is not None:
        print(render_eligibility(runner.last_eligibility))
    print()
    print(render_ledger_note(runner.ledger))
    if args.dry_run:
        print(
            f"\n(dry run: no experiment directory was written; the looks were "
            f"debited in {runner.ledger_path})"
        )
    else:
        print(
            f"\nwrote {runner.experiment_dir(spec.experiment_id)}/"
            f"{{spec,result,eligibility}}.json and {runner.ledger_path}"
        )
    return 0


def _warn_skipped(registry: ExperimentRegistry) -> None:
    for name, reason in registry.skipped:
        print(f"warning: skipped {name}: {reason}", file=sys.stderr)


def _list(args: argparse.Namespace) -> int:
    registry = ExperimentRegistry(args.out_dir)
    records = registry.find(alpha_id=args.alpha, horizon=args.horizon)
    if args.json:
        _print_json(
            {
                "experiments": [_record_doc(registry, rec) for rec in records],
                "skipped": [
                    {"directory": name, "reason": reason} for name, reason in registry.skipped
                ],
            }
        )
        return 0
    _warn_skipped(registry)
    if not records:
        print(f"no experiments under {registry.root}")
        return 0
    print(
        f"{'experiment':<16}  {'alpha':<6}  {'horizon':<7}  {'IC':>10}  "
        f"{'NW t':>8}  {'net bps':>10}  verdict"
    )
    for rec in records:
        r = rec.result
        print(
            f"{rec.experiment_id:<16}  {rec.spec.alpha_id:<6}  {rec.spec.horizon:<7}  "
            f"{r.ic:>+10.6f}  {r.t_stat:>+8.3f}  {r.net_return_bps:>+10.4f}  "
            f"{r.verdict.value}"
        )
    return 0


def _show(args: argparse.Namespace) -> int:
    registry = ExperimentRegistry(args.out_dir)
    rec = registry.load(args.experiment_id)
    if args.json:
        _print_json(_record_doc(registry, rec))
        return 0
    print(render_spec(rec.spec))
    print()
    print(render_result(rec.result))
    print(render_eligibility(registry.gate_eligibility(rec.experiment_id)))
    return 0


def _power(args: argparse.Namespace) -> int:
    from iap.research import power

    try:
        levels = [float(v) for v in args.levels.split(",") if v.strip()]
    except ValueError:
        raise ResearchError(
            f"--levels expects comma-separated numbers, got {args.levels!r}",
            code="power_study_error",
        ) from None
    doc = power.run_power_study(
        args.generator_config,
        args.configs_dir,
        levels=levels,
        n_seeds=args.seeds,
        progress=lambda line: print(line, file=sys.stderr),
    )
    paths = power.write_reports(doc, args.power_out_dir)
    print(power.render_markdown(doc))
    print(f"wrote {paths['md']} and {paths['json']}", file=sys.stderr)
    return 0


class _Parser(argparse.ArgumentParser):
    """argparse with an optional machine-readable usage error."""

    json_errors = False

    def error(self, message: str) -> NoReturn:
        if _Parser.json_errors:
            _emit_error("usage_error", message)
            raise SystemExit(2)
        super().error(message)


def _emit_error(code: str, message: str) -> None:
    """Exactly one JSON object on stderr (``--json-errors``)."""
    print(
        json.dumps(
            {"error": {"code": code, "message": message}}, sort_keys=True, ensure_ascii=True
        ),
        file=sys.stderr,
    )


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m iap.research", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO / "research" / "experiments",
        help="experiments folder (default: research/experiments)",
    )
    parser.add_argument(
        "--json-errors",
        action="store_true",
        help='on failure print one JSON object {"error": {"code", "message"}} on stderr',
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    run = sub.add_parser("run", help="run one experiment")
    run.add_argument("--alpha", required=True, help="flagship alpha id, e.g. EQ03")
    run.add_argument(
        "--horizon", default=None, help="label horizon (default: the alpha's pinned horizon)"
    )
    run.add_argument(
        "--config",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="configuration override (JSON value); repeatable",
    )
    run.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help=f"spec seed, u64 (default {DEFAULT_SEED})"
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and print; no experiment directory is "
        "written, the looks are still debited in the ledger",
    )
    run.add_argument(
        "--tstat-threshold",
        choices=TSTAT_THRESHOLD_POLICIES,
        default="fixed",
        help="PROMOTE t-stat gate: the fixed 3.0 (default) or the ledger's Bonferroni |t|",
    )
    run.add_argument("--features-dir", type=Path, default=REPO / "data" / "features")
    run.add_argument("--ledger", type=Path, default=REPO / "research" / "experiments.json")
    run.add_argument("--configs-dir", type=Path, default=REPO / "configs")
    run.add_argument(
        "--repo-root", type=Path, default=None, help="checkout to version (default: this one)"
    )
    run.set_defaults(func=_run)

    lst = sub.add_parser("list", help="list persisted experiments")
    lst.add_argument("--alpha", default=None)
    lst.add_argument("--horizon", default=None)
    lst.add_argument("--json", action="store_true", help="one JSON document on stdout")
    lst.set_defaults(func=_list)

    show = sub.add_parser("show", help="print one experiment's spec and result")
    show.add_argument("experiment_id")
    show.add_argument("--json", action="store_true", help="one JSON document on stdout")
    show.set_defaults(func=_show)

    power = sub.add_parser("power", help="planted-signal power study")
    power.add_argument(
        "--generator-config",
        type=Path,
        default=REPO / "research" / "power" / "generator_planted.json",
        help="generator config whose planted block is the reference effect",
    )
    power.add_argument(
        "--levels",
        default="0,0.5,1,2",
        help="comma-separated multipliers of the reference effect (0 = null)",
    )
    power.add_argument("--seeds", type=int, default=3, help="generator seeds per cell")
    power.add_argument("--configs-dir", type=Path, default=REPO / "configs")
    power.add_argument(
        "--power-out-dir",
        type=Path,
        default=REPO / "research" / "power",
        help="where POWER_REPORT.{md,json} are written",
    )
    power.set_defaults(func=_power)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    _Parser.json_errors = "--json-errors" in raw
    args = _parser().parse_args(raw)
    try:
        return int(args.func(args))
    except ResearchError as exc:
        if args.json_errors:
            _emit_error(exc.code, str(exc))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
