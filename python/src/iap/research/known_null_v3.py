"""REL-06 known-null protocol v3 (revision 4): the study logic of the runner.

``tools/rel06_known_null_v3.py`` is a thin driver around this module.  The protocol is
``research/v118/NULL_PROTOCOL_v3.md``; the frozen configuration is
``research/v118/known_null_v3_config.json``.  Everything that decides a verdict lives
here or in :mod:`iap.research.known_null_diag` and is testable on synthetic records
without market data:

* :func:`validate_config` - the config carries exactly the frozen values;
* :func:`build_a0_source` - A0 labels: centring, the hard centring invariant and the
  series-used check, before any derangement is applied;
* :func:`run_trials_v3` - one label draw per trial, every K and both statistics (S1, S2);
* :func:`evaluate_size_arm` - cell counts, B blocker, A0 descriptive report, U ratios,
  the 3.4 gate and :func:`iap.research.known_null_diag.primary_verdict`;
* :func:`identity_args_v3` / :func:`verify_binding` - what the signed prereg binds
  (protocol hash, the prereg itself, the runner configuration and the code version).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from iap.research import known_null as kn
from iap.research import known_null_diag as diag

PROTOCOL = "research/v118/NULL_PROTOCOL_v3.md"
CONFIG_PATH = "research/v118/known_null_v3_config.json"
PREREG_PATH = "research/v118/null_protocol_v3_prereg.json"
KIND = "known_null_v3"

#: S1 / S2 of the protocol -> the statistic names of :mod:`iap.research.known_null`
STATISTICS = {"S1": "net_recentred", "S2": "gross"}
KS = ("K3", "K6", "K16")
K_OF = {"K3": 3, "K6": 6, "K16": 16}
GENERATORS = ("A0", "B", "A_v2")
FROZEN_ALPHA = 0.025
R0_ALPHA = 0.05

#: what the signed identity binds beyond ``python/src/iap`` (the tree hash covers
#: known_null.py, known_null_diag.py and this module)
BOUND_SCRIPTS = (
    PROTOCOL,
    CONFIG_PATH,
    "tools/rel06_known_null.py",
    "tools/rel06_known_null_v3.py",
    "python/src/iap/research/known_null.py",
    "python/src/iap/research/known_null_diag.py",
    "python/src/iap/research/known_null_v3.py",
)
METRICS = (
    "a0_conditional_rejection_count_family",
    "b_rejection_rate_family",
    "cr1_se_student_t",
    "gate_understatement_ratio_s2",
    "residual_mean_s2",
    "wild_cluster_webb_full_enumeration_p",
)


class ConfigError(ValueError):
    """The configuration is not the frozen v3 configuration."""


class BindingError(RuntimeError):
    """The signed prereg does not bind this protocol, configuration, code or runner."""


# ------------------------------------------------------------------ config --


def load_config(path: Path) -> dict[str, Any]:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_config(cfg)
    return cfg


def hypothesis_ids(cfg: Mapping[str, Any], k: str) -> list[str]:
    return [h["id"] for h in cfg["variants"][k]]


def required_pairs(cfg: Mapping[str, Any]) -> dict[str, list[str]]:
    """Horizon -> sorted symbols of every (horizon, symbol) used by a hypothesis of any
    family: the groups the centring invariant must cover."""
    pairs: dict[str, set[str]] = {}
    for fam in cfg["variants"].values():
        for h in fam:
            pairs.setdefault(h["horizon"], set()).update(h["symbols"])
    return {h: sorted(s) for h, s in sorted(pairs.items())}


def n_required_groups(cfg: Mapping[str, Any]) -> int:
    return sum(len(v) for v in required_pairs(cfg).values()) * len(cfg["dataset"]["sessions"])


def validate_config(cfg: Mapping[str, Any]) -> None:
    """Refuse any configuration that differs from the frozen protocol values.  The frozen
    values are asserted against the code constants, so config and code cannot drift."""

    def need(cond: bool, msg: str) -> None:
        if not cond:
            raise ConfigError(msg)

    need(cfg.get("kind") == KIND, f"kind must be {KIND!r}")
    need(cfg.get("protocol") == PROTOCOL, "protocol path")
    need(cfg.get("prereg") == PREREG_PATH, "prereg path")
    need(cfg.get("alpha") == FROZEN_ALPHA, "alpha must be 0.025")
    need(cfg.get("alpha_diagnostic_r0") == R0_ALPHA, "R0 diagnostic alpha must be 0.05")
    need(dict(cfg.get("statistics", {})) == STATISTICS, "statistics must be S1, S2")
    sz = cfg["size_arm"]
    need(set(sz) == set(GENERATORS), "size arm generators must be A0, B, A_v2")
    need(sz["A0"]["n_trials"] == len(kn.derangements(len(cfg["dataset"]["sessions"]))), "A0 n")
    need(sz["A0"]["n_trials"] == 1854 and sz["B"]["n_trials"] == 2000, "A0 1854 / B 2000 trials")
    need(sz["A_v2"]["n_trials"] == 500, "A_v2 500 trials")
    need(
        (sz["A0"]["base_seed"], sz["B"]["base_seed"], sz["A_v2"]["base_seed"])
        == (20270101, 20270102, 20270105),
        "size-arm seeds are frozen",
    )
    pa = cfg["power_arm"]
    need(
        (pa["generators"]["A0"]["base_seed"], pa["generators"]["B"]["base_seed"])
        == (20270103, 20270104),
        "power-arm seeds are frozen",
    )
    need(pa["n_trials"] == 200 and pa["pass_rule"] is None, "power arm: 200 trials, no pass rule")
    need([float(d) for d in pa["delta_bps_per_trip"]] == [0.5, 1.0, 2.0, 4.0], "power-arm deltas")
    pr = cfg["pass_rule"]
    need(pr["B_max_count"] == diag.B_MAX_COUNT == 60, "B threshold 60 of 2,000")
    need(pr["A0_max_count"] == diag.A0_MAX_COUNT == 55, "A0 threshold 55 of 1,854")
    need(pr["flag_upper_below"] == 0.02, "over-conservative flag level")
    rm = cfg["residual_mean"]
    need(rm["flag_se"] == diag.FLAG_SE == 4, "residual-mean threshold 4 SE")
    need(rm["b_role"] == "blocks_pass" and rm["a0_role"] == "descriptive", "residual-mean roles")
    need(rm["statistic"] == "S2" and rm["a0_n_eff"] == 7, "residual-mean statistic / n_eff")
    ci = cfg["centring_invariant"]
    need(ci["tol_rel"] == diag.CENTRING_TOL_REL, "centring tolerance")
    need(
        ci["n_groups"] == n_required_groups(cfg),
        f"centring n_groups {ci['n_groups']} != groups derived from the hypothesis lists "
        f"{n_required_groups(cfg)}",
    )
    gate = cfg["gate"]
    need(gate["statistic"] == "S2" and gate["median_threshold"] == 1.0, "gate statistic/threshold")
    need(gate["labels"]["triggered_or_undetermined"] == diag.PASS_LABEL_GATE, "gate label")
    need(gate["labels"]["not_triggered"] == diag.PASS_LABEL_PLAIN, "plain label")
    need(set(cfg["variants"]) == set(KS), "families must be K3, K6, K16")
    ids16 = hypothesis_ids(cfg, "K16")
    need(len(ids16) == diag.N_HYPOTHESES == len(set(ids16)), "K16 must hold 16 distinct ids")
    for k in KS:
        need(len(cfg["variants"][k]) == K_OF[k], f"{k} must hold {K_OF[k]} hypotheses")
    need(cfg["execution"]["venue_id"] == 101 and cfg["registered_size_shares"] == 100, "economics")
    need(cfg["compute_budget_hours"] == 12.0, "compute budget")


# ------------------------------------------------------------ A0 labels ----


@dataclass
class A0Source:
    """Centred labels, the grids the derangement reads, and the copies that passed the
    invariant (``checked``).  ``centred`` and ``grids`` are what the trials use."""

    centred: dict[str, np.ndarray]
    grids: dict[str, np.ndarray]
    checked: dict[str, np.ndarray]
    invariant: dict[str, Any]
    grid_collapse: dict[str, Any]

    def series_used_ok(self, panel: kn.Panel) -> bool:
        """The arrays read by the derangement are bit-identical to those that passed the
        check, and every grid is exactly the grid of the checked array."""
        for h, chk in self.checked.items():
            if not diag.verify_series_used(self.centred[h], chk):
                return False
            if not diag.verify_series_used(self.grids[h], kn._cell_grid(panel, chk)):
                return False
        return True


def build_a0_source(study: kn.Study, cfg: Mapping[str, Any]) -> A0Source:
    """Centre the real labels per (name, horizon, session), run the HARD invariant on the
    centred arrays and the series-used check, all before any derangement is applied.
    Raises :class:`diag.CentringInvariantError` on failure (the run aborts, protocol 3.3)."""
    panel = study.panel
    pairs = required_pairs(cfg)
    req_names = {h: [panel.names.index(s) for s in syms] for h, syms in pairs.items()}
    centred = {h: diag.centre_labels(study.real[h], panel.name, panel.sess) for h in study.horizons}
    checked = {h: np.array(v, copy=True) for h, v in centred.items()}
    inv = diag.check_centring_invariant(
        {h: centred[h] for h in req_names},
        panel.name,
        panel.sess,
        req_names,
        list(range(len(panel.sessions))),
        tol_rel=float(cfg["centring_invariant"]["tol_rel"]),
    )
    if inv["n_groups"] != int(cfg["centring_invariant"]["n_groups"]):
        raise diag.CentringInvariantError(
            f"checked {inv['n_groups']} groups, configuration requires "
            f"{cfg['centring_invariant']['n_groups']}"
        )
    diag.require_centring_invariant(inv)
    grids = {h: kn._cell_grid(panel, centred[h]) for h in study.horizons}
    src = A0Source(centred, grids, checked, inv, _grid_collapse(grids, req_names))
    if not src.series_used_ok(panel):
        raise diag.CentringInvariantError("the derangement input is not the checked array")
    return src


def _grid_collapse(grids: Mapping[str, np.ndarray], req_names: Mapping[str, Sequence[int]]) -> Any:
    """Descriptive: a cell holding several rows keeps one row on the grid the derangement
    reads, so the grid mean of a group need not be exactly zero even though the row mean is.
    Max |grid mean| / max |grid value| over the required groups (no verdict role)."""
    worst = 0.0
    for h, names in req_names.items():
        for n in names:
            for s in range(grids[h].shape[1]):
                v = grids[h][n, s]
                v = v[np.isfinite(v)]
                if v.size:
                    scale = float(np.max(np.abs(v)))
                    if scale > 0:
                        worst = max(worst, abs(float(v.mean())) / scale)
    return {"max_rel_abs_grid_mean": worst, "role": "descriptive only"}


def trial_labels_v3(
    study: kn.Study,
    src: A0Source,
    generator: str,
    trial: int,
    perm: Sequence[int] | None,
    base_seed: int,
) -> dict[str, np.ndarray]:
    """Null labels of one trial.  A0: centred labels, derangement ``perm``.  A_v2: the
    uncentred v2 generator A.  B: unchanged generator B seeded ``SplitMix64(base + trial)``."""
    if generator == "A0":
        assert perm is not None
        return {
            h: kn.labels_a(study.panel, src.centred[h], perm, src.grids[h]) for h in study.horizons
        }
    if generator == "A_v2":
        return kn.trial_labels(study, "A", trial, perm)
    if generator == "B":
        return kn.trial_labels(study, "B", trial, None, base_seed)
    raise ValueError(f"unknown generator {generator!r}")


def _one(
    study: kn.Study,
    labs: Mapping[str, np.ndarray],
    fam: Sequence[kn.Hypothesis],
    g: int,
    alpha: float,
    ok: bool,
    st: str,
    planted: Mapping[str, float] | None,
) -> dict[str, Any]:
    return kn.run_trial(
        study.trips,
        labs,
        fam,
        g,
        alpha=alpha,
        eligible=ok,
        statistic=STATISTICS[st],
        planted=planted,
    )


def run_trials_v3(
    study: kn.Study,
    src: A0Source,
    generator: str,
    trials: Sequence[int],
    perms: Mapping[int, Sequence[int]],
    *,
    base_seed: int,
    alpha: float,
    deltas: Sequence[float] = (),
) -> dict[int, dict[str, Any]]:
    """Trials of one generator.  Size arm (``deltas`` empty): ``{trial: {K: {S: record}}}``.
    Power arm: one draw per trial evaluated at every delta planted on the first hypothesis
    of each family: ``{trial: {K: {"delta=<d>": {S: record}}}}``."""
    ok = bool(study.eligible["gate_eligible"])
    g = len(study.panel.sessions)
    out: dict[int, dict[str, Any]] = {}
    for i in trials:
        labs = trial_labels_v3(study, src, generator, i, perms.get(i), base_seed)
        rec: dict[str, Any] = {}
        for k in KS:
            fam = study.variants[k]

            def one(
                st: str, planted: Mapping[str, float] | None, labs=labs, fam=fam
            ) -> dict[str, Any]:
                return _one(study, labs, fam, g, alpha, ok, st, planted)

            if not deltas:
                rec[k] = {st: one(st, None) for st in STATISTICS}
            else:
                rec[k] = {
                    kn.delta_key(d): {st: one(st, {fam[0].id: float(d)}) for st in STATISTICS}
                    for d in deltas
                }
        out[i] = rec
    return out


# ------------------------------------------------------------- evaluation ---


def at_alpha(rec: Mapping[str, Any], ids: Sequence[str], alpha: float, eligible: bool) -> dict:
    """Re-read a stored trial record at another Holm level (common random numbers): the
    Holm-adjusted p-values do not depend on the level, only the comparison does."""
    confirmed = [
        hid
        for hid, p, m in zip(ids, rec["p_holm"], rec["mean_bps"], strict=True)
        if p is not None and p <= alpha and m is not None and m > 0 and eligible
    ]
    return {**rec, "confirmed": confirmed}


def cell_count(
    trials: Mapping[int, Mapping[str, Any]],
    k: str,
    s: str,
    ids: Sequence[str] | None = None,
    alpha: float | None = None,
    eligible: bool = True,
) -> int:
    """Number of trials with at least one confirmation in cell (K, S); with ``alpha`` the
    record is re-read at that level (R0 diagnostic)."""
    n = 0
    for i in trials:
        rec = trials[i][k][s]
        if alpha is not None:
            assert ids is not None
            rec = at_alpha(rec, ids, alpha, eligible)
        n += 1 if rec["confirmed"] else 0
    return n


def s2_series(
    trials: Mapping[int, Mapping[str, Any]], cfg: Mapping[str, Any]
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    """Per K16 hypothesis, the per-trial S2 session-mean statistic ``m`` and its CR1 SE, in
    trial order.  A missing value is kept as NaN (never dropped)."""
    ids = hypothesis_ids(cfg, "K16")
    ms: dict[str, list[float]] = {h: [] for h in ids}
    ses: dict[str, list[float]] = {h: [] for h in ids}
    for i in sorted(trials):
        rec = trials[i]["K16"]["S2"]
        for j, hid in enumerate(ids):
            m, se = rec["mean_bps"][j], rec["se_bps"][j]
            ms[hid].append(math.nan if m is None else float(m))
            ses[hid].append(math.nan if se is None else float(se))
    return ms, ses


def evaluate_size_arm(
    cfg: Mapping[str, Any],
    a0_trials: Mapping[int, Mapping[str, Any]],
    b_trials: Mapping[int, Mapping[str, Any]],
    *,
    centring_ok: bool,
) -> dict[str, Any]:
    """Mechanical verdict of the size arm from the trial records (protocol 3.3, 3.4, 6)."""
    sz = cfg["size_arm"]
    complete = sorted(a0_trials) == list(range(sz["A0"]["n_trials"])) and sorted(b_trials) == list(
        range(sz["B"]["n_trials"])
    )
    if not complete:
        return {
            "verdict": "INCOMPLETE (not a protocol verdict)",
            "label": "INCOMPLETE (not a protocol verdict)",
            "reason": "trial counts",
            "claim_scope": diag.CLAIM_SCOPE,
            "n_a0": len(a0_trials),
            "n_b": len(b_trials),
        }
    ids16 = hypothesis_ids(cfg, "K16")
    b_counts = {(s, K_OF[k]): cell_count(b_trials, k, s) for s in STATISTICS for k in KS}
    a0_counts = {(s, K_OF[k]): cell_count(a0_trials, k, s) for s in STATISTICS for k in KS}
    m_a0, se_a0 = s2_series(a0_trials, cfg)
    m_b, se_b = s2_series(b_trials, cfg)
    blocker = diag.b_residual_blocker(m_b, ids16, flag_se=float(cfg["residual_mean"]["flag_se"]))
    a0_desc = diag.a0_residual_descriptive(m_a0, ids16, int(cfg["residual_mean"]["a0_n_eff"]))
    u_a0 = {h: diag.understatement_ratio(np.asarray(m_a0[h]), np.asarray(se_a0[h])) for h in ids16}
    u_b = {h: diag.understatement_ratio(np.asarray(m_b[h]), np.asarray(se_b[h])) for h in ids16}
    gate = diag.lost_difficulty(u_a0, u_b, ids16)
    verdict = diag.primary_verdict(
        b_counts,
        a0_counts,
        centring_ok,
        blocker["blocks_pass"],
        gate,
        b_report=blocker,
        a0_report=a0_desc,
    )
    return {
        **verdict,
        "b_counts_of_2000": {f"{s}/K{k}": v for (s, k), v in b_counts.items()},
        "a0_counts_of_1854": {f"{s}/K{k}": v for (s, k), v in a0_counts.items()},
        "gate": gate,
    }


# ------------------------------------------------------------ binding -------


def _norm_sha(path: Path) -> str:
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def identity_args_v3(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """The ``analysis_identity`` arguments of v3: the binder and the runner both call this.
    Thresholds are the whole frozen decision content of the configuration, so a changed
    seed, threshold, role or label changes the bound identity."""
    validate_config(cfg)
    fam_digest = hashlib.sha256(
        json.dumps(cfg["variants"], sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    family = (
        "REL-06 known-null v3: "
        + ", ".join(f"{k} (K={len(v)})" for k, v in sorted(cfg["variants"].items()))
        + f"; hypotheses sha256 {fam_digest}"
    )
    keys = (
        "alpha",
        "alpha_diagnostic_r0",
        "statistics",
        "size_arm",
        "pass_rule",
        "power_arm",
        "residual_mean",
        "centring_invariant",
        "gate",
        "registered_size_shares",
        "configuration",
        "execution",
        "compute_budget_hours",
    )
    return {
        "scripts": sorted(BOUND_SCRIPTS),
        "data_versions": [cfg["dataset"]["dataset_version"]],
        "family": family,
        "metrics": list(METRICS),
        "thresholds": {k: json.loads(json.dumps(cfg[k])) for k in keys},
    }


def verify_binding(
    root: Path, prereg: Path, config_path: Path, cfg: Mapping[str, Any]
) -> dict[str, Any]:
    """Refuse unless the SIGNED prereg binds exactly this protocol hash, this prereg, this
    runner configuration and this code version, and the runner is not the signer.

    1. the prereg signature verifies under the trust root;
    2. ``prereg_id`` is this file's repo path and equals ``cfg['prereg']``; ``protocol``
       equals ``cfg['protocol']`` and ``protocol_sha256`` equals the hash of that file now;
    3. the bound identity equals :func:`identity_args_v3` recomputed from this
       configuration (family, metrics, thresholds, data version), the bound script hashes
       equal the files on disk (protocol, configuration, runner, library, diag), the
       ``python/src/iap`` tree hash matches, and the bound commit is HEAD or its ancestor;
    4. the bound configuration hash equals the file passed as ``--config``;
    5. the actor (``IAP_RUN_ACTOR``) is not the signer."""
    from iap.agents.analysis_identity import (
        IdentityError,
        require_independent_runner,
        verify_identity,
        verify_signed_document,
    )

    root = Path(root).resolve()
    doc = json.loads(Path(prereg).read_text(encoding="utf-8"))
    try:
        verify_signed_document(root, doc)
        rel = Path(prereg).resolve().relative_to(root).as_posix()
        if doc.get("kind") != "analysis_prereg":
            raise BindingError("prereg kind is not analysis_prereg")
        if doc.get("prereg_id") != rel or rel != cfg["prereg"]:
            raise BindingError(f"prereg_id {doc.get('prereg_id')!r} is not {rel!r}/{cfg['prereg']}")
        if doc.get("protocol") != cfg["protocol"]:
            raise BindingError(f"prereg protocol {doc.get('protocol')!r} != {cfg['protocol']!r}")
        proto_hash = _norm_sha(root / cfg["protocol"])
        if doc.get("protocol_sha256") != proto_hash:
            raise BindingError("protocol file hash differs from the signed protocol_sha256")
        block = doc.get("analysis_identity")
        rel_cfg = Path(config_path).resolve().relative_to(root).as_posix()
        if rel_cfg != CONFIG_PATH:
            raise BindingError(f"config path {rel_cfg!r} is not {CONFIG_PATH!r}")
        if not block or (block.get("scripts") or {}).get(CONFIG_PATH) != _norm_sha(
            root / CONFIG_PATH
        ):
            raise BindingError("runner configuration hash differs from the bound hash")
        if (block.get("scripts") or {}).get(PROTOCOL) != proto_hash:
            raise BindingError("the bound identity does not bind this protocol hash")
        args = identity_args_v3(cfg)
        bound = set(block.get("scripts") or {})
        if bound != set(args["scripts"]):
            raise BindingError(f"bound scripts {sorted(bound)} != {args['scripts']}")
        verify_identity(
            root,
            block,
            data_versions=args["data_versions"],
            family=args["family"],
            metrics=args["metrics"],
            thresholds=args["thresholds"],
        )
        actor = require_independent_runner(doc)
    except IdentityError as exc:
        raise BindingError(str(exc)) from exc
    return {
        "prereg": rel,
        "runner_actor": actor,
        "commit_bound": block["commit"],
        "protocol_sha256": proto_hash,
        "config_sha256": _norm_sha(root / CONFIG_PATH),
    }
