"""Process memory for the day pipeline (v1.18 REL-13, review finding E-G).

* :func:`peak_rss_bytes` - this process's peak resident set (Windows
  ``PeakWorkingSetSize`` via ``GetProcessMemoryInfo``, the call
  ``tools/bench_feature_memory.py`` uses; POSIX ``ru_maxrss``) and, on POSIX,
  the largest waited-for child's (``RUSAGE_CHILDREN``).  Windows has no
  cheap equivalent for an exited child, so ``children`` is ``None`` there.
* :func:`available_ram_gb` - available physical memory (moved here from
  :mod:`iap.research.maker_real`, which re-exports it).
* :func:`auto_partition_instruments` - ``--partition-instruments`` chosen
  from available RAM when the flag is not given.

Diagnostics only: nothing here changes what a step writes.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

__all__ = [
    "AUTO_GB_PER_INSTRUMENT",
    "AUTO_MAX_INSTRUMENTS",
    "AUTO_RESERVE_GB",
    "DEFAULT_PARTITION_INSTRUMENTS",
    "auto_partition_instruments",
    "available_ram_gb",
    "peak_rss_bytes",
    "peak_rss_fields",
]

#: The hand-set value every run used before REL-13 (and the fallback when
#: the available RAM cannot be read).
DEFAULT_PARTITION_INSTRUMENTS = 10
#: Memory held back for the OS, the merge window and the rest of the process.
AUTO_RESERVE_GB = 3.0
#: Planning figure per instrument of one partition; with the reserve it
#: reproduces the hand-set 10 at 8 GB available (the "< 8 GB peak" target).
AUTO_GB_PER_INSTRUMENT = 0.5
AUTO_MAX_INSTRUMENTS = 50
GB = 1e9


def _win_peak_working_set() -> int:
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
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
    k32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
    psapi = ctypes.WinDLL("psapi")  # type: ignore[attr-defined]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    return int(pmc.PeakWorkingSetSize)


def peak_rss_bytes() -> dict[str, int | None]:
    """``{"self": bytes, "children": bytes | None}``; ``None`` where unknown."""
    try:
        if sys.platform == "win32":
            return {"self": _win_peak_working_set(), "children": None}
        import resource

        scale = 1 if sys.platform == "darwin" else 1024
        own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
        kids = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * scale
        return {"self": int(own), "children": int(kids)}
    except Exception:  # pragma: no cover - diagnostics only
        return {"self": None, "children": None}


def peak_rss_fields(probe=peak_rss_bytes) -> dict[str, float | None]:
    """The step-report fields: ``peak_rss_gb`` (this process) and
    ``peak_rss_children_gb`` (largest child; ``None`` on Windows)."""
    r = probe()

    def gb(v: int | None) -> float | None:
        return None if v is None else round(v / GB, 3)

    return {"peak_rss_gb": gb(r.get("self")), "peak_rss_children_gb": gb(r.get("children"))}


def available_ram_gb() -> float | None:
    """Available physical memory (Windows GlobalMemoryStatusEx, else /proc)."""
    try:
        if sys.platform == "win32":

            class MS(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            ms = MS()
            ms.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))  # type: ignore[attr-defined]
            return ms.ullAvailPhys / 2**30
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 2**20
    except Exception:  # pragma: no cover - diagnostics only
        return None
    return None


def auto_partition_instruments(
    n_symbols: int, ram_gb: float | None
) -> dict[str, float | int | str | None]:
    """``{"instruments": N, "source": "auto", "available_ram_gb": x}``.

    ``N = floor((ram - reserve) / gb_per_instrument)`` clamped to
    ``[1, min(n_symbols, AUTO_MAX_INSTRUMENTS)]``; unknown RAM gives the
    pre-REL-13 default 10 (``source = "auto_default"``)."""
    cap = max(1, min(int(n_symbols), AUTO_MAX_INSTRUMENTS))
    if ram_gb is None:
        return {
            "instruments": min(DEFAULT_PARTITION_INSTRUMENTS, cap),
            "source": "auto_default",
            "available_ram_gb": None,
        }
    n = int((float(ram_gb) - AUTO_RESERVE_GB) // AUTO_GB_PER_INSTRUMENT)
    return {
        "instruments": max(1, min(n, cap)),
        "source": "auto",
        "available_ram_gb": round(float(ram_gb), 2),
    }
