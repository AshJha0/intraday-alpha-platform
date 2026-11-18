"""One pre-registered look at the sealed nine-session breadth holdout, through the bars path.

The plain idea.  The round-1 bars path (``rust/itchbars`` -> one-minute bars ->
:mod:`iap.research.breadth_bars`) reads the raw vendor ITCH file directly.  No
ingested dataset exists, so the holdout registry's dataset guard
(:func:`iap.governance.holdout_registry.guard`) never sees the read.  This module
is the guard for that path and the only supported way to run it on a session
dated 2025 or later.  It refuses before any download unless every condition
below holds, and it takes the look exactly once.

Preconditions (:func:`preflight`, all checked before any hook is called):

1. the pre-registration file exists, is of kind :data:`KIND`, carries no
   ``<FILL`` placeholder, and its Ed25519 signature verifies under
   ``research/agents/trust_roots.json`` (:func:`verify_signed_document`);
2. it names exactly the nine :data:`HOLDOUT_SESSIONS`, one raw file per session
   (name, bytes, sha256), the universe file and its sha256 (which match the
   working tree), the family and every parameter, the primary statistic
   :data:`STATISTIC`, the decision thresholds, the bars tool's tree hash
   (which matches ``rust/itchbars`` now), and an ``analysis_identity`` block
   that :func:`verify_identity` accepts against the checkout (scripts, code
   tree, configs, lock, interpreter, commit) with the same family, metric and
   thresholds;
3. the runner is not the signer (:func:`require_independent_runner`);
4. the registry body hash is intact, and for every session the scope of the
   non-spent symbols is ``unread`` with this pre-registration listed in
   ``reserved_for``; the symbols the registry holds ``spent`` (AAPL, MSFT, QQQ)
   are exactly the ones the pre-registration excludes;
5. no result file and no start marker exists for this pre-registration (one
   look: a second invocation refuses and names the existing file).

The run (:func:`run_look`) writes the start marker first, then per session:
download (injected), check bytes and sha256 against the registration, build
bars (injected), delete the raw file, run the registered family (injected).
It pools the per-session values with :func:`breadth_bars.pool_sessions`, takes
the verdict by :func:`verdict`, writes one result file, and marks the
sessions ``spent`` for the non-spent symbols in the registry (resealed).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iap.governance import holdout_registry as hr
from iap.governance.analysis_identity import (
    IdentityError,
    SignatureError,
    require_independent_runner,
    tree_sha256,
    verify_identity,
    verify_signed_document,
)

KIND = "breadth_bars_holdout"
HOLDOUT_SESSIONS = (
    "2025-11-28", "2025-12-08", "2025-12-09", "2025-12-10", "2025-12-11", "2025-12-12",
    "2026-05-15", "2026-05-18", "2026-06-12",
)  # fmt: skip
STATISTIC = "session_mean_net_bps_per_rebalance_one_sided_student_t"
VERDICTS = ("pass", "fail", "incomplete")
BARS_TOOL_REL = "rust/itchbars"
RESULT_DIR_REL = "research/round1/holdout_look"
PLACEHOLDER = "<FILL"


class LookRefused(ValueError):
    """A precondition of the one holdout look does not hold; nothing was read."""


# ------------------------------------------------------------------ helpers --
def file_sha256(path: Path) -> str:
    """sha256 of the file, text LF-normalised (as the identity block hashes inputs)."""
    data = Path(path).read_bytes()
    try:
        text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        data = text.encode("utf-8")
    except UnicodeDecodeError:
        pass
    return hashlib.sha256(data).hexdigest()


def raw_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _placeholders(obj: Any, path: str = "") -> list[str]:
    if isinstance(obj, Mapping):
        return [p for k, v in obj.items() for p in _placeholders(v, f"{path}.{k}")]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in _placeholders(v, f"{path}[{i}]")]
    return [path or "."] if isinstance(obj, str) and PLACEHOLDER in obj else []


def result_paths(root: Path, prereg_rel: str) -> tuple[Path, Path]:
    """(result file, start marker) for a pre-registration; fixed by its name."""
    stem = Path(prereg_rel).stem
    base = Path(root) / RESULT_DIR_REL
    return base / f"{stem}_RESULT.json", base / f"{stem}_STARTED.json"


def thresholds_of(doc: Mapping[str, Any]) -> dict[str, Any]:
    d = doc.get("decision") or {}
    return {"alpha_one_sided": d.get("alpha_one_sided"), "min_sessions": d.get("min_sessions")}


def data_versions_of(doc: Mapping[str, Any]) -> list[str]:
    """The raw vendor files' sha256 are the data the look may read."""
    return sorted(f["sha256"] for f in (doc.get("holdout") or {}).get("raw_files") or [])


# ------------------------------------------------------------------ verdict --
def verdict(
    session_values: Mapping[str, float], alpha_one_sided: float, min_sessions: int
) -> dict[str, Any]:
    """The registered decision, computed by code.

    ``incomplete`` when fewer than ``min_sessions`` sessions gave a finite value
    (the look is spent all the same); otherwise ``pass`` when the mean is above
    zero and the one-sided Student t p-value across sessions is at most
    ``alpha_one_sided``; else ``fail``."""
    from iap.research.breadth_bars import pool_sessions

    vals = [float(v) for v in session_values.values() if v is not None and math.isfinite(v)]
    pooled = pool_sessions(vals) if vals else {"n": 0}
    n = int(pooled.get("n", 0))
    if n < int(min_sessions):
        out = "incomplete"
    elif pooled["mean"] > 0 and pooled["p_one_sided"] <= float(alpha_one_sided):
        out = "pass"
    else:
        out = "fail"
    return {"verdict": out, "pooled": pooled, "sessions_used": n}


# ---------------------------------------------------------------- preflight --
@dataclass(frozen=True)
class Plan:
    """What a passed preflight hands to the run."""

    prereg_rel: str
    doc: dict[str, Any]
    symbols: list[str]
    excluded: list[str]
    result_path: Path
    marker_path: Path


def _load_doc(root: Path, prereg: Path) -> tuple[str, dict[str, Any]]:
    p = prereg if prereg.is_absolute() else root / prereg
    if not p.is_file():
        raise LookRefused(f"pre-registration {prereg} does not exist")
    try:
        rel = p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise LookRefused(f"{prereg} is outside the repository") from exc
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise LookRefused(f"{rel} is not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise LookRefused(f"{rel} is not a JSON object")
    return rel, doc


def _check_fields(root: Path, rel: str, doc: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    bad: list[str] = []
    if doc.get("kind") != KIND:
        bad.append(f"kind {doc.get('kind')!r} is not {KIND!r}")
    if "DRAFT" in str(doc.get("status", "")).upper() and doc.get("signed") is not True:
        bad.append("the document is a DRAFT")
    if doc.get("prereg_id") != rel:
        bad.append(f"prereg_id {doc.get('prereg_id')!r} is not its own path {rel!r}")
    holes = _placeholders({k: v for k, v in doc.items() if k != "signature"})
    if holes:
        bad.append(f"unfilled placeholders at {holes[:8]}")
    hold = doc.get("holdout") or {}
    if list(hold.get("sessions") or []) != list(HOLDOUT_SESSIONS):
        bad.append(f"holdout.sessions must be exactly {list(HOLDOUT_SESSIONS)}")
    raw = hold.get("raw_files") or []
    if sorted(f.get("date") for f in raw) != list(HOLDOUT_SESSIONS) or any(
        not f.get("file")
        or not f.get("url")
        or not f.get("bytes")
        or len(str(f.get("sha256", ""))) != 64
        for f in raw
    ):
        bad.append("holdout.raw_files must give date, file, url, bytes and sha256 for each session")
    uni = doc.get("universe") or {}
    upath = root / str(uni.get("path", ""))
    symbols: list[str] = []
    if not uni.get("path") or not upath.is_file():
        bad.append(f"universe file {uni.get('path')!r} missing")
    elif file_sha256(upath) != uni.get("sha256"):
        bad.append(f"universe file {uni['path']} changed since registration (sha256)")
    else:
        symbols = [str(s) for s in json.loads(upath.read_text(encoding="utf-8"))["symbols"]]
    excluded = [str(s) for s in uni.get("excluded_symbols") or []]
    fam = doc.get("family") or {}
    if not fam.get("name") or not isinstance(fam.get("parameters"), Mapping):
        bad.append("family.name and family.parameters are required")
    if (doc.get("statistic") or {}).get("primary") != STATISTIC:
        bad.append(f"statistic.primary must be {STATISTIC!r}")
    th = thresholds_of(doc)
    a, m = th["alpha_one_sided"], th["min_sessions"]
    if not isinstance(a, (int, float)) or not 0 < a < 0.5:
        bad.append(f"decision.alpha_one_sided {a!r} must be a number in (0, 0.5)")
    if not isinstance(m, int) or not 2 <= m <= len(HOLDOUT_SESSIONS):
        bad.append(f"decision.min_sessions {m!r} must be an integer in [2, 9]")
    tool = doc.get("bars_tool") or {}
    if tool.get("path") != BARS_TOOL_REL or not tool.get("commit"):
        bad.append(f"bars_tool must name path {BARS_TOOL_REL!r} and a commit")
    elif tree_sha256(root, BARS_TOOL_REL) != tool.get("tree_sha256"):
        bad.append(f"{BARS_TOOL_REL} tree differs from the registered bars_tool.tree_sha256")
    ident = doc.get("analysis_identity") or {}
    if not ident:
        bad.append("no analysis_identity block (bind it with tools/prereg_bind_identity.py)")
    elif ident.get("family") != fam.get("name"):
        bad.append("analysis_identity.family differs from family.name")
    if bad:
        raise LookRefused(f"{rel}: " + "; ".join(bad))
    return symbols, excluded


def _check_registry(reg: Mapping[str, Any], rel: str, excluded: Sequence[str]) -> None:
    bad = []
    index = reg.get("sessions") or {}
    for s in HOLDOUT_SESSIONS:
        rules = index.get(s) or []
        star = [r for r in rules if r.get("symbols") == "*"]
        spent = sorted(
            sym
            for r in rules
            if r.get("state") == "spent" and r.get("symbols") != "*"
            for sym in r["symbols"]
        )
        if spent != sorted(excluded):
            bad.append(f"{s}: registry spent symbols {spent} != excluded {sorted(excluded)}")
        if len(star) != 1 or star[0].get("state") != "unread":
            state = star[0].get("state") if star else "no rule"
            bad.append(f"{s}: non-spent scope is {state}, not unread")
        elif rel not in (star[0].get("reserved_for") or []):
            bad.append(
                f"{s}: {rel} is not in reserved_for {star[0].get('reserved_for')} "
                "(the reservation must be amended by the owner first)"
            )
    if bad:
        raise LookRefused("holdout registry: " + "; ".join(bad))


def preflight(
    root: Path,
    prereg: Path,
    *,
    registry_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    check_identity: Callable[..., Any] = verify_identity,
    family_known: Callable[[str], bool] | None = None,
) -> Plan:
    """Every refusal path; returns the plan only when all preconditions hold."""
    root = Path(root)
    rel, doc = _load_doc(root, Path(prereg))
    result_path, marker_path = result_paths(root, rel)
    for p in (result_path, marker_path):
        if p.exists():
            raise LookRefused(
                f"one look only: {p.relative_to(root).as_posix()} already exists for {rel}; "
                "the look was taken (or started) and is not re-run"
            )
    try:
        verify_signed_document(root, doc)
    except SignatureError as exc:
        raise LookRefused(f"{rel}: {exc}") from exc
    symbols, excluded = _check_fields(root, rel, doc)
    fam = doc["family"]["name"]
    if family_known is not None and not family_known(fam):
        raise LookRefused(f"family {fam!r} has no implementation in this checkout")
    try:
        check_identity(
            root,
            doc["analysis_identity"],
            data_versions=data_versions_of(doc),
            family=fam,
            metrics=[STATISTIC],
            thresholds=thresholds_of(doc),
        )
        require_independent_runner(doc, env)
    except IdentityError as exc:
        raise LookRefused(f"{rel}: {exc}") from exc
    try:
        reg = hr.load_registry(registry_path, root)
    except hr.HoldoutRefused as exc:
        raise LookRefused(str(exc)) from exc
    _check_registry(reg, rel, excluded)
    used = [s for s in symbols if s not in set(excluded)]
    return Plan(rel, doc, used, excluded, result_path, marker_path)


# ---------------------------------------------------------------------- run --
@dataclass(frozen=True)
class Hooks:
    """The three side-effecting steps, injected so tests drive the flow with fakes.

    ``download(raw_file, vendor_dir) -> Path``; ``build_bars(raw, out) -> dict``
    (``{"ok": bool, ...}``); ``study(bars_path, symbols, family, parameters) -> dict``
    with ``value`` = mean net bps per rebalance for the session."""

    download: Callable[[Mapping[str, Any], Path], Path]
    build_bars: Callable[[Path, Path], Mapping[str, Any]]
    study: Callable[[Path, Sequence[str], str, Mapping[str, Any]], Mapping[str, Any]]


def mark_spent(reg: Mapping[str, Any], sessions: Sequence[str], prereg_rel: str) -> dict[str, Any]:
    """The registry with the non-spent scope of ``sessions`` set ``spent`` by
    ``prereg_rel`` (``reserved_for`` kept as history), resealed."""
    doc = json.loads(json.dumps(reg))
    for s in sessions:
        for r in doc["sessions"][s]:
            if r.get("symbols") == "*" and r.get("state") == "unread":
                r["state"] = "spent"
                r["spent_by"] = sorted(set(r.get("spent_by") or []) | {prereg_rel})
    return hr.seal(doc)


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _write_json(path: Path, doc: Mapping[str, Any], exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x" if exclusive else "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(doc, indent=2, sort_keys=True, allow_nan=True) + "\n")


def run_look(
    root: Path,
    plan: Plan,
    hooks: Hooks,
    *,
    vendor_dir: Path,
    bars_dir: Path,
    registry_path: Path | None = None,
    log_path: Path | None = None,
) -> dict[str, Any]:
    """Take the one look.  The start marker is written (exclusively) before the
    first download, so a crash leaves the look spent and a re-run refused."""
    root = Path(root)
    doc, rel = plan.doc, plan.prereg_rel
    prereg_sha = raw_sha256(root / rel)
    _write_json(
        plan.marker_path, {"prereg": rel, "prereg_sha256": prereg_sha, "started_utc": _now()}, True
    )
    hr._log(
        log_path,
        {
            "ts": _now(),
            "purpose": "breadth_bars_holdout look",
            "prereg_id": rel,
            "decision": "allowed",
            "state": "unread",
            "sessions": list(HOLDOUT_SESSIONS),
        },
        must=True,
    )
    fam = doc["family"]["name"]
    params = dict(doc["family"]["parameters"])
    raw_by_date = {f["date"]: f for f in doc["holdout"]["raw_files"]}
    per_session: dict[str, dict[str, Any]] = {}
    values: dict[str, float] = {}
    for s in HOLDOUT_SESSIONS:
        rf = raw_by_date[s]
        rec: dict[str, Any] = {"file": rf["file"]}
        raw: Path | None = None
        try:
            raw = Path(hooks.download(rf, vendor_dir))
            if raw.stat().st_size != int(rf["bytes"]) or raw_sha256(raw) != rf["sha256"]:
                raise ValueError("raw file bytes or sha256 differ from the registration")
            out = Path(bars_dir) / f"bars_holdout_{s.replace('-', '')}.csv"
            built = dict(hooks.build_bars(raw, out))
            rec["bars"] = built
            if not built.get("ok"):
                raise ValueError("bars build failed")
        except Exception as exc:  # a failed session is reported; the look stays spent
            rec["error"] = f"{type(exc).__name__}: {exc}"
            out = None
        finally:
            if raw is not None and raw.exists():
                raw.unlink()
                rec["raw_deleted"] = True
        if out is not None:
            try:
                res = dict(hooks.study(out, plan.symbols, fam, params))
                rec["study"] = {k: v for k, v in res.items() if k != "per_rebalance"}
                values[s] = float(res["value"])
            except Exception as exc:
                rec["error"] = f"{type(exc).__name__}: {exc}"
        per_session[s] = rec
    th = thresholds_of(doc)
    v = verdict(values, th["alpha_one_sided"], th["min_sessions"])
    result = {
        "kind": f"{KIND}_result",
        "prereg": rel,
        "prereg_sha256": prereg_sha,
        "family": fam,
        "parameters": params,
        "statistic": STATISTIC,
        "thresholds": th,
        "universe_names_used": len(plan.symbols),
        "excluded_symbols": plan.excluded,
        "session_values_net_bps": values,
        "sessions": per_session,
        **v,
        "finished_utc": _now(),
    }
    _write_json(plan.result_path, result, True)
    reg_file = Path(registry_path) if registry_path else root / hr.REGISTRY_REL
    reg = hr.load_registry(reg_file)
    reg_file.write_text(hr.render(mark_spent(reg, HOLDOUT_SESSIONS, rel)), encoding="utf-8")
    return result


# ------------------------------------------------------------ planted dry run --
def planted_dry_run(alpha_one_sided: float, min_sessions: int) -> dict[str, Any]:
    """The R4 report (:mod:`iap.agents.planted_dry_run`): the verdict rule on
    planted session values only, every verdict value demonstrated."""
    import numpy as np

    rng = np.random.default_rng(20261118)
    dates = list(HOLDOUT_SESSIONS)

    def plant(xs: Sequence[float]) -> dict[str, float]:
        return {d: float(x) for d, x in zip(dates, xs, strict=True)}

    cases = [
        (
            "pass",
            "strong planted edge: 9 sessions, mean 3 bps, sd 1",
            plant(3 + rng.standard_normal(9)),
        ),
        ("fail", "planted null: 9 sessions, mean 0, sd 1", plant(rng.standard_normal(9) - 0.2)),
        ("fail", "planted loss: 9 sessions, mean -2 bps, sd 1", plant(-2 + rng.standard_normal(9))),
        ("incomplete", "one session missing (NaN)", plant([3.0] * 8 + [math.nan])),
    ]
    dems = []
    for expected, what, vals in cases:
        v = verdict(vals, alpha_one_sided, min_sessions)
        n_obs = sum(1 for x in vals.values() if math.isfinite(x))
        dems.append(
            {
                "planted": {
                    "input": what,
                    "expected": expected,
                    "observations": n_obs,
                    "values": {k: (x if math.isfinite(x) else None) for k, x in vals.items()},
                },
                "observed": v["verdict"],
                "pooled": {
                    k: x
                    for k, x in v["pooled"].items()
                    if isinstance(x, (int, float)) and math.isfinite(x)
                },
            }
        )
    return {
        "kind": "planted_dry_run",
        "rule": f"{KIND} verdict: alpha_one_sided {alpha_one_sided}, min_sessions {min_sessions}",
        "verdict_values": list(VERDICTS),
        "demonstrations": dems,
        "statistics_planted": True,
        "confirmation_data_read": False,
    }
