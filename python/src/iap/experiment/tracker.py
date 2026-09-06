"""Experiment tracker: run ids, ledger and training artifacts (spec §14/§26).

Every model fit on the platform is recorded under ``research/models/<run_id>/``
with three files:

- ``manifest.json`` — {experiment_id, git_commit, data_version, feature_version,
  model_version, hyperparams, train_window, test_window, hardware} — the full
  reproducibility record required by spec §26;
- ``metrics.json``  — whatever metrics the caller reports (IC, economics, ...);
- ``model.pkl``     — the pickled fitted estimator.

The tracker also maintains ``research/models/ledger.json``: a monotone
experiment counter plus one row per run.  The counter is the input to the
multiple-testing note every research report must carry (conventions §7).

Versioning sources (pinned, round-3):

- ``git_commit``      : ``git rev-parse HEAD`` if the repo is a git checkout,
  else the literal string ``"unversioned-workspace"``.  The manifest also
  records ``git_dirty`` (``git status --porcelain`` non-empty) and
  ``git_dirty_hash`` (sha256 of that output) so a run made on a modified
  tree is never mistaken for the commit it claims.
- ``data_version``    : sha256 over the CONTENT of the normalized dataset —
  for each ``*.normalized.iap1`` file in sorted BASENAME order, the basename
  and the sha256 of its bytes are folded into one hash.  It is therefore
  invariant under moving the checkout (the old fingerprint hashed
  ``qc_report.json``, which embeds the absolute ``raw_dir``) and it changes
  when one byte of the data changes (the old one did not — it hashed a
  report, not the data).
- ``feature_version`` : the registry hash recorded by the feature pipeline in
  ``data/features/features_summary.json`` (falls back to sha256 of the
  registry file bytes when the summary is absent).
- ``library_versions``: numpy / scikit-learn / lightgbm / xgboost versions —
  tree-model bit reproducibility is version-bound.

A manifest additionally carries the feature list, the target column and the
fold definitions the run used, so "which rows and which columns" is
answerable from the artefact alone (spec §14/§26).
"""

from __future__ import annotations

import hashlib
import json
import pickle
import platform
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

_REPO = Path(__file__).resolve().parents[4]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=10
        )
        if out.returncode == 0:
            return out.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def git_commit(repo_root: Optional[Path] = None) -> str:
    """Current git commit, or ``"unversioned-workspace"`` outside a checkout."""
    root = Path(repo_root) if repo_root is not None else _REPO
    out = _git(root, "rev-parse", "HEAD")
    return out.strip() if out is not None else "unversioned-workspace"


def git_status(repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """``{"git_commit", "git_dirty", "git_dirty_hash"}`` for a manifest."""
    root = Path(repo_root) if repo_root is not None else _REPO
    commit = git_commit(root)
    porcelain = _git(root, "status", "--porcelain")
    if porcelain is None:
        return {"git_commit": commit, "git_dirty": None, "git_dirty_hash": None}
    dirty = porcelain.strip()
    return {
        "git_commit": commit,
        "git_dirty": bool(dirty),
        "git_dirty_hash": (
            hashlib.sha256(dirty.encode()).hexdigest() if dirty else None
        ),
    }


def data_version(repo_root: Optional[Path] = None) -> str:
    """Content fingerprint of the normalized dataset (pinned).

    sha256 over ``basename \n sha256(bytes) \n`` of every
    ``*.normalized.iap1`` file in sorted basename order.  No absolute path
    ever enters the hash, so moving the checkout does not change it; one
    changed byte of data does.
    """
    root = Path(repo_root) if repo_root is not None else _REPO
    norm = root / "data" / "normalized"
    if not norm.is_dir():
        return "no-normalized-data"
    files = sorted(norm.glob("*.normalized.iap1"), key=lambda p: p.name)
    if not files:
        return "no-normalized-data"
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode())
        h.update(b"\n")
        h.update(_sha256_file(f).encode())
        h.update(b"\n")
    return h.hexdigest()


def library_versions() -> Dict[str, str]:
    """Versions of the libraries whose output is version-bound (spec §26)."""
    import importlib

    out: Dict[str, str] = {}
    for mod in ("numpy", "pandas", "sklearn", "lightgbm", "xgboost", "pyarrow"):
        try:
            out[mod] = str(importlib.import_module(mod).__version__)
        except Exception:  # not installed / no __version__: recorded as absent
            out[mod] = "absent"
    return out


def feature_version(repo_root: Optional[Path] = None) -> str:
    """Feature registry hash as recorded by the feature pipeline."""
    root = Path(repo_root) if repo_root is not None else _REPO
    summary = root / "data" / "features" / "features_summary.json"
    if summary.is_file():
        with open(summary) as f:
            payload = json.load(f)
        rh = payload.get("registry_hash")
        if isinstance(rh, str) and rh:
            return rh
    registry = root / "data" / "reference" / "feature_registry.json"
    if registry.is_file():
        return _sha256_file(registry)
    return "no-feature-registry"


def hardware_summary() -> Dict[str, Any]:
    """CPU / platform summary recorded in every manifest (spec §26)."""
    model_name = "unknown"
    cpu_count = 0
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name") and model_name == "unknown":
                    model_name = line.split(":", 1)[1].strip()
                if line.startswith("processor"):
                    cpu_count += 1
    except OSError:
        pass
    return {
        "cpu_model": model_name,
        "cpu_count": cpu_count,
        "machine": platform.machine(),
        "system": platform.system(),
        "python": platform.python_version(),
    }


class ExperimentTracker:
    """Manages run ids, the experiment ledger, and per-run artifacts."""

    LEDGER = "ledger.json"

    def __init__(self, models_dir: Optional[Path] = None,
                 repo_root: Optional[Path] = None) -> None:
        self.repo_root = Path(repo_root) if repo_root is not None else _REPO
        self.models_dir = (
            Path(models_dir)
            if models_dir is not None
            else self.repo_root / "research" / "models"
        )
        self.models_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ ledger

    def _ledger_path(self) -> Path:
        return self.models_dir / self.LEDGER

    def read_ledger(self) -> Dict[str, Any]:
        path = self._ledger_path()
        if path.is_file():
            with open(path) as f:
                ledger = json.load(f)
            if "experiment_count" not in ledger or "runs" not in ledger:
                raise ValueError(f"corrupt experiment ledger: {path}")
            return ledger
        return {"experiment_count": 0, "runs": []}

    def _write_ledger(self, ledger: Dict[str, Any]) -> None:
        with open(self._ledger_path(), "w") as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
            f.write("\n")

    @property
    def experiment_count(self) -> int:
        """Total experiments recorded (multiple-testing denominator)."""
        return self.read_ledger()["experiment_count"]

    # -------------------------------------------------------------- runs

    def new_run(self, name: str) -> str:
        """Allocate the next sequential run id (``run_NNNN_<name>``)."""
        if not name or any(c in name for c in "/\\ "):
            raise ValueError(f"invalid run name: {name!r}")
        ledger = self.read_ledger()
        seq = ledger["experiment_count"] + 1
        run_id = f"run_{seq:04d}_{name}"
        ledger["experiment_count"] = seq
        ledger["runs"].append({"run_id": run_id, "name": name})
        (self.models_dir / run_id).mkdir(parents=True, exist_ok=True)
        self._write_ledger(ledger)
        return run_id

    def run_dir(self, run_id: str) -> Path:
        return self.models_dir / run_id

    # ----------------------------------------------------------- artifacts

    def write_manifest(
        self,
        run_id: str,
        model_version: str,
        hyperparams: Dict[str, Any],
        train_window: Dict[str, int],
        test_window: Dict[str, int],
        features: Optional[list] = None,
        target: Optional[str] = None,
        folds: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Write ``manifest.json`` for a run; returns the manifest dict.

        ``train_window`` / ``test_window`` are ``{"start_ts": ns, "end_ts": ns}``
        (test_window may be ``{}`` for a final refit with no held-out set).
        ``features`` / ``target`` / ``folds`` record WHICH columns and WHICH
        rows the run used — without them a manifest cannot reproduce a fit
        (spec §14/§26).
        """
        for w, label in ((train_window, "train_window"),
                         (test_window, "test_window")):
            if w and ("start_ts" not in w or "end_ts" not in w):
                raise ValueError(f"{label} must carry start_ts/end_ts")
        manifest = {
            "experiment_id": run_id,
            **git_status(self.repo_root),
            "data_version": data_version(self.repo_root),
            "feature_version": feature_version(self.repo_root),
            "model_version": model_version,
            "hyperparams": hyperparams,
            "features": list(features) if features is not None else None,
            "target": target,
            "train_window": train_window,
            "test_window": test_window,
            "folds": list(folds) if folds is not None else None,
            "library_versions": library_versions(),
            "hardware": hardware_summary(),
        }
        with open(self.run_dir(run_id) / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.write("\n")
        return manifest

    def write_metrics(self, run_id: str, metrics: Dict[str, Any]) -> None:
        with open(self.run_dir(run_id) / "metrics.json", "w") as f:
            json.dump(metrics, f, indent=2, sort_keys=True)
            f.write("\n")

    def save_model(self, run_id: str, model: Any) -> Path:
        path = self.run_dir(run_id) / "model.pkl"
        with open(path, "wb") as f:
            pickle.dump(model, f)
        return path

    def load_model(self, run_id: str) -> Any:
        path = self.run_dir(run_id) / "model.pkl"
        if not path.is_file():
            raise ValueError(f"no model.pkl for run {run_id}")
        with open(path, "rb") as f:
            return pickle.load(f)
