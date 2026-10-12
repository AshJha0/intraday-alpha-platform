"""R7-CONFIRM-1 confirmatory run (H4: EQ10@1s and EQ05@1s on the 7 R7 holdout sessions).

Implements the signed pre-registration ``research/r7/r7_confirm_prereg.json``.

Two modes::

    python tools/r7_confirm.py --dry-run [--dataset-dir data/real/r7_holdout]
    python tools/r7_confirm.py --i-am-the-owner [--dataset-dir data/real/r7_holdout]

``--dry-run`` (safe, read-only): verifies the prereg (signed, Ed25519 signature
against research/agents/trust_roots.json, fingerprints), data/real/R7_SIGNED.ok,
dataset sessions == prereg holdout_days, the features directory, the
pre-registration gate for EQ10/1s and EQ05/1s (``prereg_gate.require``, read
only), the deviation note, that no look was taken yet, and the ledger threshold
in force; then prints the exact live command.  It computes no IC, runs no
experiment and writes nothing.

Live mode is the confirmatory run and is HUMAN-ONLY
(``iap.agents.workflow.HUMAN_ONLY_OPS``: ``confirmatory_run``).  It re-runs every
dry-run check (any failure aborts before the first look), then for EQ10 then
EQ05 runs EXACTLY ONCE::

    python -m iap.research run --alpha A --horizon 1s --methods v4 --dataset-dir <ds>

(that is the ONE look per hypothesis; a failed look is never retried, and a
failure after EQ10's look stops the script).  It then computes the descriptive
per-session IC series, the deflated Sharpe ratio on it, the gate, the
2025-12-10 / 2025-11-28 exclusion sensitivity (no further look: the same data),
and writes research/r7/r7_confirm_result.json + .md.  It does NOT record the
outcome; it prints the human-only command (tools/r7_record_outcome.py).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python" / "src"))

import numpy as np  # noqa: E402

PREREG_REL = "research/r7/r7_confirm_prereg.json"
DEVIATION_REL = "research/r7/R7_DEVIATION_lot_size.md"
RESULT_REL = "research/r7/r7_confirm_result.json"
RESULT_MD_REL = "research/r7/r7_confirm_result.md"
DEFAULT_DATASET = Path("data/real/r7_holdout")
SIGNED_OK_REL = Path("data/real/R7_SIGNED.ok")
ALPHAS = ("EQ10", "EQ05")
HORIZON = "1s"
METHODS = "v4"
HYPOTHESIS = "H4"
EXPECTED_DATASET_VERSION = "ca5d079831be4e63aa76e350bb840a29a6964c0152f4bbbcce5010269a193854"
EXCLUDED_SENSITIVITY = ("2025-12-10", "2025-11-28")  # FOMC, holiday-thin
DSR_MIN = 0.95
EXPECTED_FEATURE_FILES = (
    "features_summary.json",
    "feature_registry.json",
    "features_1.parquet",
    "features_2.parquet",
    "features_3.parquet",
)
LOOK_EXIT_OK = (0, 3)  # 3 = the run finished but contradicted the registered sign
NS_DAY = 86_400 * 1_000_000_000


class CheckError(RuntimeError):
    """A precondition failed; nothing was looked at."""


# ---------------------------------------------------------------- checks --
def sha256(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def load_prereg(root: Path) -> dict:
    p = root / PREREG_REL
    if not p.is_file():
        raise CheckError(f"{PREREG_REL} missing")
    return json.loads(p.read_text(encoding="utf-8"))


def verify_prereg(root: Path, doc: Mapping[str, Any]) -> str:
    """Signed, on the blackboard, Ed25519 signature valid under the trust root."""
    from iap.agents import signing, trust
    from iap.agents.blackboard import canonical

    if doc.get("signed") is not True or doc.get("on_blackboard") is not True:
        raise CheckError("prereg is not signed / not on the blackboard")
    if doc.get("open_for_owner"):
        raise CheckError(f"prereg has open owner items: {doc['open_for_owner']}")
    sig = doc.get("signature")
    if not isinstance(sig, dict) or sig.get("scheme") != "ed25519":
        raise CheckError("prereg has no ed25519 signature block")
    roots = trust.read_roots(root) or {}
    pub = (roots.get("maintainers") or {}).get(sig.get("maintainer"))
    if pub is None or pub != sig.get("public_key"):
        raise CheckError("prereg signing key is not the trust-root maintainer key")
    body = {k: v for k, v in doc.items() if k != "signature"}
    msg = canonical(body).encode("ascii")
    if hashlib.sha256(msg).hexdigest() != sig.get("message_sha256"):
        raise CheckError("prereg message sha256 mismatch (document changed after signing)")
    if not signing.verify_ed25519(pub, msg, sig.get("sig", "")):
        raise CheckError("prereg Ed25519 signature INVALID")
    return pub


def verify_fingerprints(root: Path, doc: Mapping[str, Any]) -> None:
    for rel, want in (doc.get("fingerprints") or {}).items():
        if rel == "algorithm":
            continue
        p = root / rel
        if not p.is_file() or sha256(p) != want:
            raise CheckError(f"fingerprint mismatch for {rel}")


def verify_signed_ok(root: Path, signed_ok: Path) -> None:
    if not signed_ok.is_file():
        raise CheckError(f"{signed_ok} missing")
    text = signed_ok.read_text(encoding="utf-8-sig")
    kv = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    want = kv.get("r7_confirm_prereg.json", "").strip()
    if want != sha256(root / PREREG_REL):
        raise CheckError(f"{signed_ok}: r7_confirm_prereg.json sha256 does not match the prereg")


def load_dataset(ds: Path) -> dict:
    p = ds / "dataset.json"
    if not p.is_file():
        raise CheckError(f"{p} missing")
    return json.loads(p.read_text())


def verify_dataset(ds: Path, manifest: Mapping[str, Any], doc: Mapping[str, Any]) -> None:
    sessions = sorted(manifest.get("sessions") or {})
    if sessions != sorted(doc["holdout_days"]):
        raise CheckError(
            f"dataset sessions {sessions} != prereg holdout_days {doc['holdout_days']}"
        )
    if manifest.get("dataset_version") != EXPECTED_DATASET_VERSION:
        raise CheckError(
            f"dataset_version {manifest.get('dataset_version')} != recorded "
            f"{EXPECTED_DATASET_VERSION} (deviation note)"
        )
    syms = sorted(u["symbol"] for u in manifest.get("universe", []))
    if syms != sorted(doc["symbols"]):
        raise CheckError(f"dataset symbols {syms} != prereg symbols {doc['symbols']}")
    feats = ds / "features"
    missing = [f for f in EXPECTED_FEATURE_FILES if not (feats / f).is_file()]
    if missing:
        raise CheckError(f"{feats}: missing {missing} (feature build not finished?)")
    if not (ds / "configs" / "instruments" / "instruments.json").is_file():
        raise CheckError(f"{ds}/configs/instruments/instruments.json missing")


def verify_gate(root: Path, doc: Mapping[str, Any]) -> dict[str, dict]:
    """``prereg_gate.require`` (read only) and the entry is the one the prereg names."""
    from iap.agents.prereg_gate import PreregistrationError, require

    by_alpha = {e["alpha_id"]: e for e in doc["registration"]["entries"]}
    out = {}
    for a in ALPHAS:
        try:
            entry = require(root, a, HORIZON)
        except PreregistrationError as exc:
            raise CheckError(f"prereg gate refused {a}/{HORIZON}: {exc}") from exc
        reg = by_alpha.get(a)
        if reg is None or entry["seq"] != reg["blackboard_seq"]:
            raise CheckError(f"{a}/{HORIZON}: blackboard entry {entry['seq']} is not the prereg's")
        if entry["body"]["prereg_id"] != reg["prereg_id"]:
            raise CheckError(f"{a}/{HORIZON}: prereg_id mismatch")
        if int(entry["body"].get("expected_sign", 0)) != 1:
            raise CheckError(f"{a}/{HORIZON}: registered sign is not +1")
        out[a] = entry
    return out


def experiments_dir(ds: Path) -> Path:
    return ds / "research" / "experiments"


def look_marker(ds: Path, alpha: str) -> Path:
    return ds / "research" / f"R7_CONFIRM_LOOK_{alpha}_{HORIZON}.started"


def verify_no_prior_look(ds: Path) -> None:
    """Refuse when a look for these alphas was already taken on this dataset."""
    for a in ALPHAS:
        if look_marker(ds, a).exists():
            raise CheckError(f"{look_marker(ds, a)} exists: the look was already taken")
    d = experiments_dir(ds)
    if d.is_dir():
        for spec in d.glob("*/spec.json"):
            s = json.loads(spec.read_text())
            if s.get("alpha_id") in ALPHAS and s.get("horizon") == HORIZON:
                raise CheckError(f"{spec.parent} already holds a {s['alpha_id']}/{HORIZON} run")


def ledger_thresholds(root: Path, ds: Path) -> dict[str, Any]:
    """The Bonferroni |t| threshold in force (read only).

    The prereg debited its looks on the repo ledger (research/experiments.json);
    ``run --dataset-dir`` judges against the dataset's own ledger.  The gate uses
    the STRICTER of the two (resolved ambiguity, see the module docs)."""
    from iap.validation.ledger import ExperimentLedger

    repo = ExperimentLedger(root / "research" / "experiments.json")
    dsl_path = ds / "research" / "experiments.json"
    dsl = ExperimentLedger(dsl_path) if dsl_path.is_file() else None
    return {
        "repo_ledger_total": repo.total_experiments,
        "repo_ledger_t": repo.bonferroni_t_threshold(),
        "dataset_ledger_total": None if dsl is None else dsl.total_experiments,
        "dataset_ledger_t": None if dsl is None else dsl.bonferroni_t_threshold(),
    }


def live_command(ds: Path) -> str:
    return f"python tools/r7_confirm.py --i-am-the-owner --dataset-dir {ds.as_posix()}"


def run_command(alpha: str, ds: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "iap.research",
        "run",
        "--alpha",
        alpha,
        "--horizon",
        HORIZON,
        "--methods",
        METHODS,
        "--dataset-dir",
        str(ds),
    ]


def preflight(root: Path, ds: Path, signed_ok: Path) -> dict[str, Any]:
    """Every precondition; raises :class:`CheckError` on the first failure."""
    doc = load_prereg(root)
    verify_prereg(root, doc)
    print("ok  prereg signed; Ed25519 signature valid under the trust root")
    verify_fingerprints(root, doc)
    print("ok  prereg fingerprints match")
    verify_signed_ok(root, signed_ok)
    print(f"ok  {signed_ok} present and names this prereg")
    if not (root / DEVIATION_REL).is_file():
        raise CheckError(f"{DEVIATION_REL} missing (must be recorded before the look)")
    print(f"ok  deviation note {DEVIATION_REL} present")
    manifest = load_dataset(ds)
    verify_dataset(ds, manifest, doc)
    print(f"ok  dataset {ds}: sessions == holdout_days; features present")
    entries = verify_gate(root, doc)
    for a, e in entries.items():
        print(f"ok  prereg gate {a}/{HORIZON}: blackboard entry {e['seq']}")
    verify_no_prior_look(ds)
    print("ok  no prior look on this dataset")
    thr = ledger_thresholds(root, ds)
    print(
        f"ok  ledger threshold in force: repo {thr['repo_ledger_total']} looks -> |t| >= "
        f"{thr['repo_ledger_t']:.4f}; dataset ledger "
        + (
            "empty (the run debits its own looks)"
            if thr["dataset_ledger_total"] is None
            else f"{thr['dataset_ledger_total']} looks -> |t| >= {thr['dataset_ledger_t']:.4f}"
        )
    )
    return {"prereg": doc, "manifest": manifest, "entries": entries, "thresholds": thr}


# -------------------------------------------------- per-session statistics --
def session_day(ts_ns: np.ndarray) -> np.ndarray:
    """ISO date of each UTC exchange timestamp (a US session lies in one UTC day)."""
    days = (np.asarray(ts_ns, dtype=np.int64) // NS_DAY).astype("datetime64[D]")
    return np.datetime_as_string(days, unit="D")


def pooled_rows(
    frames: Mapping[int, Any], model: Any, ic_rows: str, single_venue: bool
) -> dict[str, np.ndarray]:
    """Raw signal, scored 1s label, timestamp and uncrossed mask pooled over the
    alpha's universe, with the research pipeline's definitions (``model.signals``,
    :func:`iap.labels.frames.scored_labels`, crossed = ``spread_ticks_v1 < 0`` unless
    the dataset is one venue's own book)."""
    from iap.labels.frames import scored_labels

    universe = model.universe(sorted(frames))
    sigs = model.signals({i: frames[i] for i in universe})
    ts, xs, ys, unc = [], [], [], []
    for iid, sig in sigs.items():
        df = frames[iid]
        lab, _ = scored_labels(df, model.horizon, ic_rows)
        if single_venue or "spread_ticks_v1" not in df.columns:
            crossed = np.zeros(len(df), dtype=bool)
        else:
            sp = df["spread_ticks_v1"].to_numpy(dtype=float)
            crossed = np.isfinite(sp) & (sp < 0.0)
        ts.append(df["exchange_ts"].to_numpy(dtype=np.int64))
        xs.append(np.asarray(sig, dtype=float))
        ys.append(lab)
        unc.append(~crossed)
    if not ts:
        raise ValueError("empty universe")
    return {
        "ts": np.concatenate(ts),
        "x": np.concatenate(xs),
        "y": np.concatenate(ys),
        "unc": np.concatenate(unc),
    }


def per_session_ic(rows: Mapping[str, np.ndarray]) -> dict[str, float | None]:
    """Pooled uncrossed IC per session day (``iap.validation.metrics.ic``)."""
    from iap.validation.metrics import ic

    day = session_day(rows["ts"])
    out: dict[str, float | None] = {}
    for d in sorted(set(day.tolist())):
        m = (day == d) & rows["unc"]
        v = ic(rows["x"][m], rows["y"][m])
        out[d] = float(v) if math.isfinite(v) else None
    return out


def pooled_stats(rows: Mapping[str, np.ndarray], keep: np.ndarray, horizon: str) -> dict:
    """Descriptive pooled uncrossed raw-signal IC and its pooled-slope HAC t."""
    from iap.validation.metrics import (
        HORIZONS_NS,
        auto_hac_lags,
        ic,
        n_ts_buckets,
        pooled_slope_hac_tstat,
    )

    m = keep & rows["unc"]
    ts, x, y = rows["ts"][m], rows["x"][m], rows["y"][m]
    lags = auto_hac_lags(n_ts_buckets(ts), HORIZONS_NS[horizon])
    v = ic(x, y)
    t = pooled_slope_hac_tstat(ts, x, y, lags=lags, day_ns=NS_DAY) if ts.size else float("nan")
    return {
        "raw_ic": float(v) if math.isfinite(v) else None,
        "raw_hac_t": float(t) if math.isfinite(t) else None,
        "hac_lags": int(lags),
        "n_pairs": int(np.sum(np.isfinite(x) & np.isfinite(y))),
    }


def dsr_block(series: Sequence[float | None], n_trials: int, n_looks: int | None) -> dict:
    from iap.validation.deflated import deflated_sharpe_block

    vals = [float(v) for v in series if v is not None]
    return deflated_sharpe_block(
        vals, int(n_trials), n_looks=n_looks, trials_basis="distinct_configurations"
    )


def gate(ic_value: float | None, t: float | None, threshold: float, dsr: float | None) -> dict:
    """Prereg gate: HAC t >= ledger threshold AND DSR > 0.95 (IC > 0 is H4's sign)."""
    reasons = []
    if ic_value is None or not ic_value > 0:
        reasons.append(f"IC {ic_value} is not > 0")
    if t is None or not t >= threshold:
        reasons.append(f"HAC t {t} < threshold {threshold:.4f}")
    if dsr is None or not dsr > DSR_MIN:
        reasons.append(f"DSR {dsr} is not > {DSR_MIN}")
    return {"passed": not reasons, "reasons": reasons, "threshold": threshold}


def sensitivity(
    rows: Mapping[str, np.ndarray],
    ics: Mapping[str, float | None],
    n_trials: int,
    n_looks: int | None,
    threshold: float,
    horizon: str,
) -> dict:
    """Full sample vs without 2025-12-10 and 2025-11-28: same data, no new look;
    reported alongside, never decides."""
    day = session_day(rows["ts"])
    out = {}
    for label, excl in (("all_sessions", ()), ("excl_2025-12-10_2025-11-28", EXCLUDED_SENSITIVITY)):
        keep = ~np.isin(day, list(excl))
        series = {d: v for d, v in ics.items() if d not in excl}
        pooled = pooled_stats(rows, keep, horizon)
        d = dsr_block(list(series.values()), n_trials, n_looks)
        out[label] = {
            "excluded": list(excl),
            "sessions": sorted(series),
            "per_session_ic": series,
            "mean_session_ic": (
                float(np.mean([v for v in series.values() if v is not None]))
                if any(v is not None for v in series.values())
                else None
            ),
            **pooled,
            "deflated_sharpe": d,
            "descriptive_gate_on_raw": gate(
                pooled["raw_ic"], pooled["raw_hac_t"], threshold, d.get("dsr")
            ),
        }
    return out


def verdict(look_ok: bool, g: Mapping[str, Any]) -> str:
    """Prereg decision rule: pass -> confirmed; any failure is final -> rejected."""
    if not look_ok:
        return "no_result"
    return "confirmed" if g["passed"] else "rejected"


# ------------------------------------------------------------------ live --
def take_look(
    alpha: str, ds: Path, root: Path, runner: Callable[..., Any] = subprocess.run
) -> dict[str, Any]:
    """THE one look: one ``iap.research run`` subprocess, never retried."""
    marker = look_marker(ds, alpha)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(datetime.datetime.now(datetime.UTC).isoformat(), encoding="ascii")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "python" / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PYTHONUTF8", "1")
    cmd = run_command(alpha, ds)
    print("LOOK:", " ".join(cmd), flush=True)
    proc = runner(cmd, cwd=str(root), env=env, capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout or "")
    sys.stderr.write(proc.stderr or "")
    out: dict[str, Any] = {"alpha": alpha, "command": cmd, "exit_code": proc.returncode}
    exp_id = None
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "experiment":
            exp_id = parts[1]
    out["experiment_id"] = exp_id
    if proc.returncode not in LOOK_EXIT_OK or exp_id is None:
        out["ok"] = False
        return out
    edir = experiments_dir(ds) / exp_id
    try:
        out["result"] = json.loads((edir / "result.json").read_text())
        out["eligibility"] = json.loads((edir / "eligibility.json").read_text())
        out["spec"] = json.loads((edir / "spec.json").read_text())
    except (OSError, ValueError) as exc:
        out["ok"] = False
        out["error"] = f"could not read {edir}: {exc!r}"
        return out
    out["ok"] = True
    return out


def git_commit(root: Path) -> str | None:
    p = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return p.stdout.strip() or None


def load_frames(ds: Path):
    from iap.alpha import configure_universe
    from iap.alpha.data import load_features

    configure_universe(ds / "configs" / "instruments" / "instruments.json")
    return load_features(ds / "features")


def analyse(
    alpha: str,
    look: Mapping[str, Any],
    frames: Mapping[int, Any],
    ds: Path,
    thresholds: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    from iap.alpha import build
    from iap.validation.deflated import effective_trials
    from iap.validation.ledger import ExperimentLedger
    from iap.validation.methods import methods
    from iap.validation.sessions import book_scope_for_dataset

    res = look.get("result") or {}
    elig = look.get("eligibility") or {}
    run_t = elig.get("significance_threshold")
    threshold = max(float(thresholds["repo_ledger_t"]), float(run_t or 0.0))
    dsl = ExperimentLedger(ds / "research" / "experiments.json")
    n_trials = effective_trials(dsl, include_new=False)  # the look is recorded already
    n_looks = dsl.total_experiments
    model = build(alpha)
    model.horizon = HORIZON
    rows = pooled_rows(
        frames, model, methods(METHODS).ic_rows, book_scope_for_dataset(ds) == "single_venue"
    )
    ics = per_session_ic(rows)
    sens = sensitivity(rows, ics, n_trials, n_looks, threshold, HORIZON)
    d = sens["all_sessions"]["deflated_sharpe"]
    g = gate(res.get("ic"), res.get("t_stat"), threshold, d.get("dsr"))
    return {
        "alpha": f"{alpha}@{HORIZON}",
        "prereg_id": entry["body"]["prereg_id"],
        "blackboard_seq": entry["seq"],
        "experiment_id": look.get("experiment_id"),
        "exit_code": look.get("exit_code"),
        "result": res,
        "eligibility": elig,
        "gate_threshold": {
            "used": threshold,
            "run_significance_threshold": run_t,
            "run_threshold_looks": elig.get("threshold_looks"),
            "repo_ledger_t": thresholds["repo_ledger_t"],
            "repo_ledger_total": thresholds["repo_ledger_total"],
            "rule": "max(run's ledger threshold, repo-ledger Bonferroni |t|)",
        },
        "dsr": {"n_trials": n_trials, "n_looks": n_looks, **d},
        "per_session_ic": ics,
        "gate": g,
        "verdict": verdict(bool(look.get("ok")), g),
        "net_pnl_descriptive": {
            k: res.get(k)
            for k in (
                "gross_return_bps",
                "transaction_cost_bps",
                "net_return_bps",
                "sharpe",
                "max_drawdown_bps",
            )
        },
        "sensitivity": sens,
    }


def render_md(doc: Mapping[str, Any]) -> str:
    lines = [
        "# R7-CONFIRM-1 result (H4)",
        "",
        f"- dataset_version: `{doc['dataset_version']}`",
        f"- git commit: `{doc['git_commit']}`",
        f"- run: {doc['run_utc']}",
        f"- deviation: {doc['deviation_note']}",
        "",
        "| alpha | experiment | IC | HAC t | threshold | DSR | verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in doc["results"]:
        res = r.get("result") or {}
        lines.append(
            f"| {r['alpha']} | {r.get('experiment_id')} | {res.get('ic')} | {res.get('t_stat')} "
            f"| {r.get('gate_threshold', {}).get('used')} | {r.get('dsr', {}).get('dsr')} "
            f"| **{r['verdict']}** |"
        )
    for r in doc["results"]:
        lines += ["", f"## {r['alpha']}", ""]
        if r.get("gate"):
            lines.append(f"- gate passed: {r['gate']['passed']}; reasons: {r['gate']['reasons']}")
        for d, v in (r.get("per_session_ic") or {}).items():
            lines.append(f"- session {d}: IC {v}")
        for k, s in (r.get("sensitivity") or {}).items():
            lines.append(
                f"- sensitivity {k}: raw IC {s['raw_ic']}, raw HAC t {s['raw_hac_t']}, "
                f"mean session IC {s['mean_session_ic']}, DSR {s['deflated_sharpe'].get('dsr')} "
                "(descriptive, never decides)"
            )
        lines.append(f"- net P&L (descriptive only): {r.get('net_pnl_descriptive')}")
    lines += ["", "Outcome NOT recorded. Human-only commands:", ""]
    lines += [f"    {c}" for c in doc["record_commands"]]
    return "\n".join(lines) + "\n"


def record_commands(results: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        f"python tools/r7_record_outcome.py --i-am-the-owner --prereg-id {r['prereg_id']} "
        f"--outcome {r['verdict']} --ref {RESULT_REL}#{r['alpha']}"
        for r in results
        if r.get("verdict") in ("confirmed", "rejected")
    ]


def live(root: Path, ds: Path, signed_ok: Path, runner=subprocess.run, frames_loader=None) -> int:
    from iap.agents.workflow import require_human

    require_human("owner", "confirmatory_run", set())
    pre = preflight(root, ds, signed_ok)  # raises before the first look
    looks = []
    for alpha in ALPHAS:
        look = take_look(alpha, ds, root, runner)
        looks.append(look)
        if not look["ok"]:
            print(f"STOP: the {alpha} look failed (exit {look['exit_code']}); never rerun.")
            break
    frames = (frames_loader or load_frames)(ds) if any(x["ok"] for x in looks) else {}
    results = []
    for look in looks:
        a = look["alpha"]
        if look["ok"]:
            results.append(analyse(a, look, frames, ds, pre["thresholds"], pre["entries"][a]))
        else:
            results.append(
                {
                    "alpha": f"{a}@{HORIZON}",
                    "prereg_id": pre["entries"][a]["body"]["prereg_id"],
                    "experiment_id": look.get("experiment_id"),
                    "exit_code": look["exit_code"],
                    "verdict": "no_result",
                    "error": look.get("error", "look failed; see the run output"),
                }
            )
    doc = {
        "schema": "iap.r7_confirm_result/v1",
        "prereg": PREREG_REL,
        "prereg_sha256": sha256(root / PREREG_REL),
        "hypothesis": HYPOTHESIS,
        "dataset_dir": ds.as_posix(),
        "dataset_version": pre["manifest"]["dataset_version"],
        "git_commit": git_commit(root),
        "run_utc": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "methods": METHODS,
        "deviation_note": DEVIATION_REL + " (owner option A, recorded before the look)",
        "decision_rule": pre["prereg"]["decision"],
        "gate_rule": pre["prereg"]["gate"],
        "cost_sensitivity": "not computed here: reported-only per prereg, never decides",
        "results": results,
        "outcome_recorded": False,
    }
    doc["record_commands"] = record_commands(results)
    (root / RESULT_REL).write_text(
        json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    (root / RESULT_MD_REL).write_text(render_md(doc), encoding="utf-8")
    print(f"wrote {RESULT_REL} and {RESULT_MD_REL}")
    print("Outcome NOT recorded. To record (HUMAN-ONLY):")
    for c in doc["record_commands"]:
        print("   ", c)
    return 0 if all(r["verdict"] != "no_result" for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="checks only; no look")
    mode.add_argument("--i-am-the-owner", action="store_true", help="THE confirmatory look")
    ap.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--root", type=Path, default=REPO)
    ap.add_argument("--signed-ok", type=Path, default=None)
    a = ap.parse_args(argv)
    root = a.root.resolve()
    ds = a.dataset_dir if a.dataset_dir.is_absolute() else root / a.dataset_dir
    signed_ok = a.signed_ok or root / SIGNED_OK_REL
    try:
        if a.dry_run:
            preflight(root, ds, signed_ok)
            print("\nDRY RUN OK - no IC computed, nothing written. Live (owner only):")
            print("   ", live_command(a.dataset_dir))
            return 0
        return live(root, ds, signed_ok)
    except CheckError as exc:
        print(f"ABORT (no look taken): {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
