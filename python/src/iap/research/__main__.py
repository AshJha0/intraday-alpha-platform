"""``python -m iap.research`` — run, list and show experiments; power study.

::

    python -m iap.research [--json-errors] run --alpha EQ03 [--horizon 1s]
                               [--config n_folds=3 ...] [--seed N] [--dry-run]
                               [--methods v2|legacy_v1|v3|v4]
    python -m iap.research list [--alpha EQ03] [--horizon 1s] [--json]
    python -m iap.research show <experiment_id> [--json]
    python -m iap.research power [--levels 0,0.5,1] [--seeds 20]
                               [--sessions 1,2,4] [--break-levels 1]
                               [--gate-looks N] [--jobs N]
                               [--generator-config PATH] [--power-out-dir research/power]
    python -m iap.research power-real --dataset-dir DIR [--seeds 20] [--restart]
    python -m iap.research combine [--asset-class EQUITY|FX|all] [--dataset-dir DIR]

``run``, ``combine``, ``power`` and ``power-real`` refuse unless their
(alpha, horizon) pairs are pre-registered on research/agents/blackboard.jsonl
(:mod:`iap.agents.prereg_gate`; ``combine`` checks the combination id);
``--no-prereg`` makes the run exploratory and says so.

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
``flatten_at_session_end=false``).  ``--methods`` names the research method
bundle (:mod:`iap.validation.methods`): ``v2``, the default, or
``legacy_v1``, the rules up to v1.4.0 — it is the ``methods`` key of the
configuration and therefore part of the experiment id.  (The v1.3.0 /
v1.4.0 flag ``--tstat-threshold`` is gone: the ledger threshold is part of
``v2`` and the fixed 3.0 part of ``legacy_v1``.)

``combine`` evaluates the signal combinations (:mod:`iap.combine.report`):
``python -m iap.research combine [--asset-class EQUITY|FX|all] [--method
equal_weight,ridge,...] [--members EQ01,EQ03,...] [--horizon 5s]
[--dry-run]`` validates each (asset class, method) combination as an alpha,
debits its looks and writes ``research/combination/``.  An asset class with
no rows in the dataset (FX on an equity feed) is skipped with a progress
line; none with rows is an error.

``power`` runs the planted-signal power study (:mod:`iap.research.power`)
and writes ``POWER_REPORT.md`` / ``POWER_REPORT.json``.  ``power-real``
(:mod:`iap.research.power_real`) is its counterpart on an ingested dataset;
it appends every finished run to ``REAL_POWER_CHECKPOINT.jsonl`` beside the
report and resumes from it when rerun (``--restart`` discards it).

**For tools.**  ``list --json`` and ``show --json`` print ONE JSON document
on stdout (sorted keys): ``list`` gives ``{"experiments": [...], "skipped":
[...]}`` — each experiment with its spec summary, result, and gate
eligibility; ``skipped`` names every directory that could not be loaded and
why — and ``show`` gives ``{"spec", "result", "gate_eligibility"}``.
``--json-errors`` (a top-level flag, before the subcommand) makes every
failure print exactly one JSON object on stderr,
``{"error": {"code", "message"}}``, with a stable ``code``
(:class:`iap.research.errors.ResearchError`; usage errors are
``usage_error`` and exit 2, everything else exits 1).  ``run`` exits 3 when
the result's IC contradicts the pre-registered sign: the verdict is printed,
but it is not evidence for the registered hypothesis.
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
from iap.validation.methods import METHODS

REPO = Path(__file__).resolve().parents[4]
#: exit status of ``run`` when the result contradicts the pre-registered direction
DIRECTION_CONTRADICTED_EXIT = 3

#: (label, field, format) rows of the result table, in reading order.
_RESULT_ROWS = (
    ("IC (the gate's: pooled OOS, z)", "ic", "+.6f"),
    ("rank IC", "rank_ic", "+.6f"),
    ("t-stat (the gate's)", "t_stat", "+.4f"),
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
    """One line (plus one per reason) on whether the result is gate evidence,
    and the significance threshold the result was judged at when recorded."""
    lines: list[str] = []
    if eligibility.significance_threshold is not None:
        source = (
            f"ledger Bonferroni |t| at {eligibility.threshold_looks} looks"
            if eligibility.threshold_looks is not None
            else "fixed"
        )
        lines.append(f"PROMOTE t threshold: {eligibility.significance_threshold:.4f} ({source})")
    if eligibility.eligible:
        note = (
            ""
            if eligibility.periods_verified
            else " (configuration only; periods not verified against a dataset)"
        )
        lines.append(f"gate eligible: yes{note}")
        return "\n".join(lines)
    lines.append("gate eligible: NO — recorded and ledgered, but not promotion evidence")
    lines += [f"  - {reason}" for reason in eligibility.reasons]
    return "\n".join(lines)


def _eligibility_doc(eligibility: GateEligibility) -> dict[str, Any]:
    return {
        "gate_eligible": eligibility.eligible,
        "periods_verified": eligibility.periods_verified,
        "reasons": list(eligibility.reasons),
        "methods": eligibility.methods,
        "significance_threshold": eligibility.significance_threshold,
        "threshold_looks": eligibility.threshold_looks,
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


def _dataset_versions(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """``--dataset-dir``: run on an ingested dataset (docs/REAL_DATA.md).

    Points every path that was left at its checkout default into the
    dataset directory and returns its ``(dataset_version, feature_version)``
    — read from ``dataset.json`` and ``features/features_summary.json`` — so
    the spec, the experiment id and the ledger entries carry the REAL
    dataset's version and its looks are never pooled with synthetic ones.
    """
    if args.dataset_dir is None:
        return None, None
    root = Path(args.dataset_dir)
    try:
        dataset_version = json.loads((root / "dataset.json").read_text())["dataset_version"]
        feature_version = json.loads((root / "features" / "features_summary.json").read_text())[
            "registry_hash"
        ]
    except (OSError, ValueError, KeyError) as exc:
        raise ResearchError(
            f"--dataset-dir {root}: needs dataset.json (python -m iap.marketdata ingest) and "
            f"features/features_summary.json (python -m iap.features): {exc!r}",
            code="invalid_spec",
        ) from exc
    for name, default, target in (
        ("features_dir", REPO / "data" / "features", root / "features"),
        ("configs_dir", REPO / "configs", root / "configs"),
        ("ledger", REPO / "research" / "experiments.json", root / "research" / "experiments.json"),
        ("out_dir", REPO / "research" / "experiments", root / "research" / "experiments"),
    ):
        if getattr(args, name) == default:
            setattr(args, name, target)
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    # A real dataset numbers its own instruments (QQQ may be id 3): the
    # alphas' constituent / ETF universe must come from its config.
    from iap.alpha import configure_universe

    configure_universe(args.configs_dir / "instruments" / "instruments.json")
    return dataset_version, feature_version


def _check_prereg(args: argparse.Namespace) -> dict | None:
    """Pre-registration gate: before any data is read (AL02).

    Returns the blackboard entry, or ``None`` under ``--no-prereg``."""
    if args.no_prereg:
        print("NOT pre-registered (--no-prereg): exploratory, not evidence for a gate")
        return None
    from iap.agents.prereg_gate import PreregistrationError, board_path, require
    from iap.research.specs import pinned_horizon

    root = args.repo_root if args.repo_root is not None else REPO
    horizon = args.horizon or pinned_horizon(args.alpha)
    try:
        entry = require(root, args.alpha, horizon, args.prereg_board)
    except PreregistrationError as exc:
        raise ResearchError(str(exc), code="not_preregistered") from exc
    print(
        f"pre-registered: {args.alpha}/{horizon} on {args.prereg_board or board_path(root)} "
        f"(entry {entry['seq']})"
    )
    return entry


def _check_prereg_pairs(args: argparse.Namespace, pairs: list[tuple[str, str]], what: str) -> None:
    """Pre-registration gate for a study over several (alpha, horizon) pairs."""
    if args.no_prereg:
        print(
            f"NOT pre-registered (--no-prereg): {what} is exploratory, not evidence for a gate",
            file=sys.stderr,
        )
        return
    from iap.agents.prereg_gate import PreregistrationError, board_path, require

    root = args.repo_root if getattr(args, "repo_root", None) is not None else REPO
    for alpha_id, horizon in pairs:
        try:
            entry = require(root, alpha_id, horizon, args.prereg_board)
        except PreregistrationError as exc:
            raise ResearchError(str(exc), code="not_preregistered") from exc
        print(
            f"pre-registered: {alpha_id}/{horizon} on "
            f"{args.prereg_board or board_path(root)} (entry {entry['seq']})",
            file=sys.stderr,
        )


def _declared_pairs() -> list[tuple[str, str]]:
    """The (alpha, declared horizon) pairs the power studies test."""
    from iap.research import power_real

    return [(d["alpha_id"], d["horizon"]) for d in power_real.detectors()]


def _direction_check(entry: dict | None, result: Any) -> bool:
    """Whether the result's IC has the pre-registered sign (``True`` without a prereg)."""
    if entry is None:
        return True
    expected = int(entry["body"]["expected_sign"])
    observed = 1 if result.ic > 0 else -1 if result.ic < 0 else 0
    if observed == expected:
        print(f"pre-registered direction {expected:+d} confirmed (IC {result.ic:+.4f})")
        return True
    print(
        f"PRE-REGISTERED DIRECTION CONTRADICTED: expected sign {expected:+d}, observed IC "
        f"{result.ic:+.4f}; the {result.verdict.value} verdict does not stand as evidence "
        "for the registered hypothesis"
    )
    return False


def _runner_gate(args: argparse.Namespace, entry: dict | None):
    """The runner's own pre-registration check (the CLI checked once already, before
    reading data; this one travels with the runner). ``None`` under ``--no-prereg``."""
    if entry is None:
        return None
    from iap.agents.prereg_gate import require

    root = args.repo_root if args.repo_root is not None else REPO

    def gate(spec) -> None:
        require(root, spec.alpha_id, spec.horizon, args.prereg_board)

    return gate


def _run(args: argparse.Namespace) -> int:
    prereg_entry = _check_prereg(args)
    dataset_version, feature_version = _dataset_versions(args)
    runner = ExperimentRunner(
        args.features_dir,
        args.ledger,
        args.out_dir,
        args.configs_dir,
        dry_run=args.dry_run,
        repo_root=args.repo_root,
        normalized_dir=args.normalized_dir,
        gate=_runner_gate(args, prereg_entry),
        exploratory=bool(args.no_prereg),
    )
    configuration = _parse_config(args.config)
    if args.methods is not None:
        if configuration.get("methods", args.methods) != args.methods:
            raise ResearchError(
                f"--methods {args.methods} contradicts --config methods={configuration['methods']}",
                code="invalid_spec",
            )
        configuration["methods"] = args.methods
    spec = build_spec(
        args.alpha,
        args.horizon,
        configuration,
        seed=args.seed,
        frames=runner.frames(),
        repo_root=args.repo_root,
        dataset_version=dataset_version,
        feature_version=feature_version,
    )
    print(render_spec(spec))
    print()
    result = runner.run(spec)
    direction_ok = _direction_check(prereg_entry, result)
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
    return 0 if direction_ok else DIRECTION_CONTRADICTED_EXIT


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
    # ``dataset`` (first 8 hex of dataset_version) tells apart the same
    # alpha x horizon run on different datasets: the folder keeps the
    # experiments of every dataset the ledger has seen.
    print(
        f"{'experiment':<16}  {'alpha':<6}  {'horizon':<7}  {'dataset':<8}  {'IC':>10}  "
        f"{'NW t':>8}  {'net bps':>10}  verdict"
    )
    for rec in records:
        r = rec.result
        print(
            f"{rec.experiment_id:<16}  {rec.spec.alpha_id:<6}  {rec.spec.horizon:<7}  "
            f"{rec.spec.dataset_version[:8]:<8}  "
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


def _numbers(text: str, flag: str, cast) -> list:
    try:
        return [cast(v) for v in text.split(",") if v.strip()]
    except ValueError:
        raise ResearchError(
            f"{flag} expects comma-separated numbers, got {text!r}",
            code="power_study_error",
        ) from None


def _power(args: argparse.Namespace) -> int:
    from iap.research import power

    _check_prereg_pairs(args, _declared_pairs(), "the power study")

    doc = power.run_power_study(
        args.generator_config,
        args.configs_dir,
        levels=_numbers(args.levels, "--levels", float),
        n_seeds=args.seeds,
        sessions=_numbers(args.sessions, "--sessions", int) if args.sessions else None,
        break_levels=_numbers(args.break_levels, "--break-levels", float),
        gate_looks=args.gate_looks,
        jobs=args.jobs if args.jobs else power.default_jobs(),
        progress=lambda line: print(line, file=sys.stderr, flush=True),
    )
    paths = power.write_reports(doc, args.power_out_dir)
    print(power.render_markdown(doc))
    print(f"wrote {paths['md']} and {paths['json']}", file=sys.stderr)
    return 0


def _power_real(args: argparse.Namespace) -> int:
    from iap.research import power_real

    _check_prereg_pairs(args, _declared_pairs(), "the real-data power study")

    out_dir = args.power_out_dir or args.dataset_dir / "research" / "power"
    doc = power_real.run_real_power_study(
        args.dataset_dir,
        levels=_numbers(args.levels, "--levels", float),
        break_levels=_numbers(args.break_levels, "--break-levels", float),
        n_seeds=args.seeds,
        sessions=_numbers(args.sessions, "--sessions", int) if args.sessions else None,
        gate_looks=args.gate_looks,
        progress=lambda line: print(line, file=sys.stderr, flush=True),
        checkpoint=out_dir / "REAL_POWER_CHECKPOINT.jsonl",
        restart=args.restart,
    )
    paths = power_real.write_reports(doc, out_dir)
    print(power_real.render_markdown(doc))
    print(f"wrote {paths['md']} and {paths['json']}", file=sys.stderr)
    return 0


def _combine_gate(args: argparse.Namespace):
    """The pre-registration hook for ``combine``: (combination id, horizon) must be on the board."""
    if args.no_prereg:
        print("NOT pre-registered (--no-prereg): exploratory, not evidence for a gate")
        return None
    from iap.agents.prereg_gate import PreregistrationError, board_path, require

    root = args.repo_root if args.repo_root is not None else REPO

    def gate(combination_id: str, horizon: str) -> None:
        try:
            entry = require(root, combination_id, horizon, args.prereg_board)
        except PreregistrationError as exc:
            raise ResearchError(str(exc), code="not_preregistered") from exc
        print(
            f"pre-registered: {combination_id}/{horizon} on "
            f"{args.prereg_board or board_path(root)} (entry {entry['seq']})"
        )

    return gate


def _combine(args: argparse.Namespace) -> int:
    from iap.combine import report as combine_report
    from iap.combine.weights import METHODS as COMBINATION_METHODS
    from iap.lifecycle.config import load_policy_config

    names = [m for m in args.method.split(",") if m] if args.method else list(COMBINATION_METHODS)
    classes = (
        list(combine_report.ASSET_CLASSES) if args.asset_class == "all" else [args.asset_class]
    )
    members = None
    if args.members:
        if len(classes) != 1:
            raise ResearchError("--members needs one --asset-class", code="invalid_spec")
        members = {classes[0]: [m for m in args.members.split(",") if m]}
    repo = args.repo_root if args.repo_root is not None else REPO
    dataset_version, feature_version = _dataset_versions(args)
    if args.dataset_dir is not None:
        if args.normalized_dir is None:
            args.normalized_dir = args.dataset_dir / "normalized"
        if args.combine_out_dir == REPO / "research" / "combination":
            args.combine_out_dir = args.dataset_dir / "research" / "combination"
    try:
        result = combine_report.run_combination(
            repo,
            asset_classes=classes,
            method_names=names,
            members=members,
            horizon=args.horizon,
            features_dir=args.features_dir,
            normalized_dir=args.normalized_dir,
            configs_dir=args.configs_dir,
            ledger_path=args.ledger,
            progress=lambda line: print(line, file=sys.stderr),
            dataset_version=dataset_version,
            feature_version=feature_version,
            gate=_combine_gate(args),
        )
    except ValueError as exc:
        raise ResearchError(str(exc), code="invalid_spec") from exc
    # an ingested dataset carries no strategy configs: the lifecycle policy
    # (the correlation threshold) then comes from the checkout
    strategies = args.configs_dir / "strategies"
    if not (strategies / "lifecycle.json").is_file():
        strategies = repo / "configs" / "strategies"
    policy = load_policy_config(strategies / "lifecycle.json", strategies / "strategies.json")
    threshold = policy.gates.max_cross_alpha_correlation
    print(combine_report.render_markdown(result["document"], threshold))
    if args.dry_run:
        print("dry run: no report written (the looks are debited)", file=sys.stderr)
        return 0
    paths = combine_report.write_reports(result, args.combine_out_dir, threshold)
    print("wrote " + ", ".join(str(p) for p in paths.values()), file=sys.stderr)
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
        "--no-prereg",
        action="store_true",
        help="skip the pre-registration check (exploratory run; stated in the output)",
    )
    run.add_argument(
        "--prereg-board",
        type=Path,
        default=None,
        help="blackboard holding the pre-registrations (default research/agents/blackboard.jsonl)",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and print; no experiment directory is "
        "written, the looks are still debited in the ledger",
    )
    run.add_argument(
        "--methods",
        choices=sorted(METHODS),
        default=None,
        help="research method bundle: v2 (default), legacy_v1 (the rules up to v1.4.0) "
        "v3 (opt-in v1.9 research-validity rules, RESEARCH_VALIDITY.md 1a) "
        "or v4 (v3 plus report-only CPCV / PBO / deflated Sharpe, 1b)",
    )
    run.add_argument(
        "--dataset-dir",
        type=Path,
        default=None,
        help="an ingested real dataset (python -m iap.marketdata ingest): features, configs, "
        "ledger and experiments default to that directory and its dataset_version is recorded",
    )
    run.add_argument("--features-dir", type=Path, default=REPO / "data" / "features")
    run.add_argument(
        "--normalized-dir",
        type=Path,
        default=None,
        help="normalized events for the recompute leakage probe "
        "(default: <features-dir>/../normalized)",
    )
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
        default="0,0.5,1",
        help="comma-separated multipliers of the reference effect (0 = null)",
    )
    power.add_argument("--seeds", type=int, default=20, help="generator seeds per cell")
    power.add_argument(
        "--sessions",
        default="",
        help="comma-separated session counts to evaluate; the largest is generated "
        "(default: 1,2,4,... up to the generator config's sessions)",
    )
    power.add_argument(
        "--break-levels",
        default="1",
        help="comma-separated non-zero levels of the mid-sample break scenario",
    )
    power.add_argument(
        "--gate-looks",
        type=int,
        default=None,
        help="look count of the PROMOTE t threshold in force (default: ledger_looks "
        "of the committed promotion reports under research/alpha_reports)",
    )
    power.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="worker processes (0 = one per core, at most 8); the report does not depend on it",
    )
    power.add_argument("--configs-dir", type=Path, default=REPO / "configs")
    power.add_argument(
        "--power-out-dir",
        type=Path,
        default=REPO / "research" / "power",
        help="where POWER_REPORT.{md,json} are written",
    )
    power.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="checkout holding the pre-registration board (default: this one)",
    )
    power.add_argument(
        "--no-prereg",
        action="store_true",
        help="skip the pre-registration check on the declared detectors (exploratory run)",
    )
    power.add_argument(
        "--prereg-board",
        type=Path,
        default=None,
        help="blackboard holding the pre-registrations (default research/agents/blackboard.jsonl)",
    )
    power.set_defaults(func=_power)

    preal = sub.add_parser(
        "power-real", help="planted-signal power study on an ingested real dataset"
    )
    preal.add_argument("--dataset-dir", type=Path, required=True)
    preal.add_argument(
        "--levels", default="0,0.005,0.01,0.02,0.04", help="planted ICs (comma-separated)"
    )
    preal.add_argument("--break-levels", default="0.02", help="planted ICs of the break scenario")
    preal.add_argument("--seeds", type=int, default=20, help="shifted-background seeds")
    preal.add_argument("--sessions", default=None, help="session grid (default 2,4,..,all)")
    preal.add_argument("--gate-looks", type=int, default=None)
    preal.add_argument(
        "--power-out-dir",
        type=Path,
        default=None,
        help="default <dataset-dir>/research/power (git-ignored with the data)",
    )
    preal.add_argument(
        "--restart",
        action="store_true",
        help="discard REAL_POWER_CHECKPOINT.jsonl and start over (default: resume from it)",
    )
    preal.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="checkout holding the pre-registration board (default: this one)",
    )
    preal.add_argument(
        "--no-prereg",
        action="store_true",
        help="skip the pre-registration check on the declared detectors (exploratory run)",
    )
    preal.add_argument(
        "--prereg-board",
        type=Path,
        default=None,
        help="blackboard holding the pre-registrations (default research/agents/blackboard.jsonl)",
    )
    preal.set_defaults(func=_power_real)

    combine = sub.add_parser("combine", help="signal combination report")
    combine.add_argument(
        "--asset-class", choices=("all", "EQUITY", "FX"), default="all", help="default: both"
    )
    combine.add_argument(
        "--method",
        default=None,
        help="comma-separated combination methods (default: all four; each is an experiment)",
    )
    combine.add_argument(
        "--members",
        default=None,
        help="comma-separated member alpha ids (default: every alpha of the asset class)",
    )
    combine.add_argument(
        "--horizon", default=None, help="label horizon (default: the members' median horizon)"
    )
    combine.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and print; write no report (the looks are still debited)",
    )
    combine.add_argument("--features-dir", type=Path, default=REPO / "data" / "features")
    combine.add_argument("--normalized-dir", type=Path, default=None)
    combine.add_argument("--ledger", type=Path, default=REPO / "research" / "experiments.json")
    combine.add_argument("--configs-dir", type=Path, default=REPO / "configs")
    combine.add_argument("--repo-root", type=Path, default=None)
    combine.add_argument(
        "--dataset-dir",
        type=Path,
        default=None,
        help="run on an ingested dataset (docs/REAL_DATA.md); reports go under it",
    )
    combine.add_argument(
        "--combine-out-dir",
        type=Path,
        default=REPO / "research" / "combination",
        help="where REPORT.md, COMBINATION.json, reports/ and signal_correlation.json go",
    )
    combine.add_argument(
        "--no-prereg",
        action="store_true",
        help="skip the pre-registration check (exploratory run; stated in the output)",
    )
    combine.add_argument(
        "--prereg-board",
        type=Path,
        default=None,
        help="blackboard holding the pre-registrations (default research/agents/blackboard.jsonl)",
    )
    combine.set_defaults(func=_combine)
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
