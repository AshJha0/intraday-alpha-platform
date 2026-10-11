"""Shared skeleton of the real-data study drivers (v1.14, review E-P1-4).

The maker, quoting and auction study drivers each re-implemented the same
skeleton -- read the pre-registration and pin its SHA-256, check the
blackboard, walk ``(session, symbol)`` units in order, skip units whose
checkpoint exists, write each unit atomically as canonical JSON, log with a
timestamp, report peak RSS, pool session-clustered statistics -- and
imported the shared pieces from one another.  :class:`StudyBase` holds that
skeleton once; a driver subclasses it and implements :meth:`run_unit_doc`
(and optionally :meth:`summarise`).

Nothing here changes a driver's output: the unit file bytes are
``json.dumps(doc, indent=2, sort_keys=True) + "\\n"`` exactly as the
drivers wrote them, and :func:`cluster_stats` / :func:`file_sha256` /
:func:`unit_name` are the drivers' functions moved here verbatim.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def peak_rss_gb() -> float | None:
    """Peak working set of this process (Windows), else ``ru_maxrss``."""
    try:
        if sys.platform == "win32":

            class PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            fn = ctypes.windll.psapi.GetProcessMemoryInfo  # type: ignore[attr-defined]
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
            fn(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
            return pmc.PeakWorkingSetSize / 2**30
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20
    except Exception:  # pragma: no cover - diagnostics only
        return None


def file_sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def unit_name(session: str, symbol: str) -> str:
    return f"{session}_{symbol}"


def dumps_unit(doc: Mapping[str, Any]) -> str:
    """The pinned unit-file serialisation."""
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def cluster_stats(day_means: Sequence[float | None], n_tests: int = 1) -> dict[str, Any]:
    """Session-clustered mean, se, 95 % CI and the Bonferroni CI over ``n_tests``."""
    from scipy import stats

    x = np.asarray([v for v in day_means if v is not None and math.isfinite(v)], dtype=float)
    n = int(x.size)
    out: dict[str, Any] = {"n_sessions": n, "mean": float(x.mean()) if n else None}
    if n < 2:
        out.update(se=None, t=None, ci95=None, ci_bonf=None)
        return out
    se = float(x.std(ddof=1) / math.sqrt(n))
    out["se"] = se
    out["t"] = float(out["mean"] / se) if se > 0 else None
    for key, a in (("ci95", 0.05), ("ci_bonf", 0.05 / n_tests)):
        q = float(stats.t.ppf(1 - a / 2, n - 1))
        out[key] = [out["mean"] - q * se, out["mean"] + q * se]
    return out


class StudyBase:
    """Pre-registered, checkpointed ``(session, symbol)`` unit loop.

    Subclasses set :attr:`sessions` / :attr:`symbols` (or pass them) and
    implement :meth:`run_unit_doc`.  ``prereg_key(cell)`` names the
    blackboard entry each pre-registered cell must carry.
    """

    units_dir_name = "units"

    def __init__(
        self,
        out: Path | str,
        prereg_path: Path | str,
        sessions: Sequence[str],
        symbols: Mapping[str, int],
        *,
        logger: Callable[[str], None] = log,
    ) -> None:
        self.out = Path(out)
        self.prereg_path = Path(prereg_path)
        self.prereg: dict[str, Any] = json.loads(self.prereg_path.read_text(encoding="utf-8"))
        self.prereg_sha = file_sha256(self.prereg_path)
        self.sessions = tuple(sessions)
        self.symbols = dict(symbols)
        self.log = logger
        self.units_dir.mkdir(parents=True, exist_ok=True)

    # -- pre-registration ---------------------------------------------------

    @staticmethod
    def prereg_key(cell: Mapping[str, Any]) -> tuple[str, str]:
        """``(alpha, analysis)`` of a cell's blackboard entry; override per study."""
        raise NotImplementedError

    def check_prereg(self, repo_root: Path | str) -> list[dict]:
        """The blackboard entries of every pre-registered cell (raises if missing)."""
        from iap.agents.prereg_gate import require

        return [require(Path(repo_root), *self.prereg_key(c)) for c in self.prereg["cells"]]

    # -- units --------------------------------------------------------------

    @property
    def units_dir(self) -> Path:
        return self.out / self.units_dir_name

    def unit_path(self, day: int, symbol: str) -> Path:
        return self.units_dir / f"{unit_name(self.sessions[day], symbol)}.json"

    def units(self) -> Iterator[tuple[int, str]]:
        """Every unit in session order, symbols in their given order."""
        for day in range(len(self.sessions)):
            for symbol in self.symbols:
                yield day, symbol

    def write_unit(self, day: int, symbol: str, doc: Mapping[str, Any]) -> Path:
        from iap.experiment.locking import atomic_write_text

        path = self.unit_path(day, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, dumps_unit(doc), encoding="utf-8")
        return path

    def read_units(self) -> list[dict[str, Any]]:
        out = []
        for day, symbol in self.units():
            p = self.unit_path(day, symbol)
            if p.is_file():
                out.append(json.loads(p.read_text(encoding="utf-8")))
        return out

    def run_unit_doc(self, day: int, symbol: str) -> dict[str, Any]:
        """The unit's document (deterministic); implemented by the driver."""
        raise NotImplementedError

    def run_unit(self, day: int, symbol: str) -> bool:
        """Run and write one unit; ``False`` when its checkpoint exists."""
        path = self.unit_path(day, symbol)
        if path.is_file():
            self.log(f"skip {path.name} (done)")
            return False
        self.write_unit(day, symbol, self.run_unit_doc(day, symbol))
        return True

    def run(self) -> int:
        """Every unit not yet checkpointed; returns how many ran."""
        return sum(self.run_unit(day, symbol) for day, symbol in self.units())

    def summarise(self) -> dict[str, Any]:  # pragma: no cover - per study
        raise NotImplementedError


__all__ = [
    "StudyBase",
    "cluster_stats",
    "dumps_unit",
    "file_sha256",
    "log",
    "peak_rss_gb",
    "unit_name",
]
