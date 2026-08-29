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

Versioning sources (pinned):

- ``git_commit``      : ``git rev-parse HEAD`` if the repo is a git checkout,
  else the literal string ``"unversioned-workspace"``;
- ``data_version``    : sha256 of ``data/normalized/qc_report.json`` bytes;
- ``feature_version`` : the registry hash recorded by the feature pipeline in
  ``data/features/features_summary.json`` (falls back to sha256 of the
  registry file bytes when the summary is absent).
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


def git_commit(repo_root: Optional[Path] = None) -> str:
    """Current git commit, or ``"unversioned-workspace"`` outside a checkout."""
    root = Path(repo_root) if repo_root is not None else _REPO
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unversioned-workspace"


def data_version(repo_root: Optional[Path] = None) -> str:
    """sha256 of the normalization QC report (the dataset fingerprint)."""
    root = Path(repo_root) if repo_root is not None else _REPO
    qc = root / "data" / "normalized" / "qc_report.json"
    if not qc.is_file():
        return "no-qc-report"
    return _sha256_file(qc)


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
    ) -> Dict[str, Any]:
        """Write ``manifest.json`` for a run; returns the manifest dict.

        ``train_window`` / ``test_window`` are ``{"start_ts": ns, "end_ts": ns}``
        (test_window may be ``{}`` for a final refit with no held-out set).
        """
        for w, label in ((train_window, "train_window"),
                         (test_window, "test_window")):
            if w and ("start_ts" not in w or "end_ts" not in w):
                raise ValueError(f"{label} must carry start_ts/end_ts")
        manifest = {
            "experiment_id": run_id,
            "git_commit": git_commit(self.repo_root),
            "data_version": data_version(self.repo_root),
            "feature_version": feature_version(self.repo_root),
            "model_version": model_version,
            "hyperparams": hyperparams,
            "train_window": train_window,
            "test_window": test_window,
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
