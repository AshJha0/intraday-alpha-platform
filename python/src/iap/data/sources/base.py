"""Shared machinery of the free-data source adapters (v2.0 stage 2 DD1; U0 DR-6).

Every adapter in this package has the same shape:

``declaration``   a :class:`iap.data.contracts.DatasetContract` plus the facts the
                  contract vocabulary does not carry: the licence id (one section
                  of ``docs/DATA_SOURCES.md``), the public access rule, the
                  User-Agent and the request rate the source asks for.
``coverage()``    **counts and date ranges only**: symbol counts, first and last
                  dates, revision or filing counts.  Never a price, a return, a
                  quote or a filing body.  This is U0 section 2's rule that
                  "coverage counts come from the vendor's metadata, not from the
                  data", applied at the adapter.
``fetch()``       refuses unless the process identity is ``iap-runner`` AND the
                  target is a sealed partition (the ingestion-time sealing rule of
                  U0 section 2, G3).  Until the D2 sealed service exists the call
                  can never succeed: it raises :class:`SealedServiceUnavailable`
                  after the identity and target checks pass.

No network module is imported here or in any adapter (``iap.data`` is a guarded
package of ``python/tests/test_import_policy.py``).  Every adapter takes a
``transport``: a callable ``(url, headers) -> bytes`` supplied by the caller.
``tools/free_data_coverage.py`` supplies the real one (``urllib`` with the
source's User-Agent); the unit tests supply recorded or synthetic responses, so
CI makes no request.  Rate limiting is the adapter's job (the SEC's 10 requests
per second, Tiingo's hourly and daily quotas, Wikimedia's courtesy limit) and is
driven by an injectable clock so it is tested without sleeping.
"""

from __future__ import annotations

import collections
import datetime as _dt
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from iap.data.contracts import DatasetContract

__all__ = [
    "RUNNER_IDENTITY",
    "SEALED_ROOTS",
    "Coverage",
    "RateLimiter",
    "SealedServiceUnavailable",
    "SealingRefused",
    "SourceAdapter",
    "SourceDeclaration",
    "Transport",
    "current_identity",
    "is_sealed_target",
    "parse_date",
    "require_runner",
]

#: The ingestion identity of U0 section 2.
RUNNER_IDENTITY = "iap-runner"
#: Partition roots created on 2026-10-25 (U0 decision record): a sealed target lives under one.
SEALED_ROOTS = (PureWindowsPath(r"C:\iap-sealed"), PurePosixPath("/iap-sealed"))

Transport = Callable[[str, Mapping[str, str]], bytes]


class SealingRefused(PermissionError):
    """``fetch`` called by the wrong identity or towards an unsealed target."""


class SealedServiceUnavailable(RuntimeError):
    """Identity and target are right, but the D2 sealed service does not exist yet."""


def current_identity() -> str:
    """The OS user name of this process (Windows ``USERNAME``, else ``USER``/``LOGNAME``)."""
    for var in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    try:  # pragma: no cover - platform fallback
        import getpass

        return getpass.getuser()
    except Exception:  # pragma: no cover
        return ""


def is_sealed_target(target: str | os.PathLike[str] | None) -> bool:
    """True when ``target`` is an absolute path strictly under a sealed partition root."""
    if target is None:
        return False
    text = os.fspath(target)
    if not text:
        return False
    win = PureWindowsPath(text)
    if win.drive:
        parts = [p.lower() for p in win.parts]
        root_parts = [p.lower() for p in SEALED_ROOTS[0].parts]
        return parts[: len(root_parts)] == root_parts and len(parts) > len(root_parts)
    posix = PurePosixPath(text.replace("\\", "/"))
    return posix.is_absolute() and posix.parts[:2] == SEALED_ROOTS[1].parts and len(posix.parts) > 2


def require_runner(target: Any, *, identity: str | None = None) -> None:
    """Raise :class:`SealingRefused` unless ``identity`` is the runner and ``target`` is sealed."""
    who = current_identity() if identity is None else identity
    if who != RUNNER_IDENTITY:
        raise SealingRefused(
            f"fetch() runs only as {RUNNER_IDENTITY!r} (U0 section 2, G3); this process is {who!r}. "
            "The development identity gets coverage() only."
        )
    if not is_sealed_target(target):
        raise SealingRefused(
            f"fetch() writes only into a sealed partition under {SEALED_ROOTS[0]} (or /iap-sealed); "
            f"got {target!r}"
        )


@dataclass(frozen=True, slots=True)
class SourceDeclaration:
    """A dataset contract and the access facts that sit beside it."""

    contract: DatasetContract
    licence_id: str
    access: str
    user_agent: str
    requests_per_second: float

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.contract.to_dict(),
            "licence_id": self.licence_id,
            "access": self.access,
            "requests_per_second": self.requests_per_second,
        }


@dataclass(slots=True)
class Coverage:
    """What ``coverage()`` may return: counts, ranges and notes.  No values."""

    source: str
    as_of: str
    counts: dict[str, int] = field(default_factory=dict)
    ranges: dict[str, tuple[str, str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    requests_made: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "as_of": self.as_of,
            "counts": dict(self.counts),
            "ranges": {k: list(v) for k, v in self.ranges.items()},
            "notes": list(self.notes),
            "requests_made": self.requests_made,
        }


class RateLimiter:
    """A sliding-window limiter: at most ``per_second`` calls in any one second and,
    optionally, ``per_hour`` in any hour; ``clock`` and ``sleep`` are injectable."""

    def __init__(
        self,
        per_second: float,
        per_hour: int | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_second <= 0:
            raise ValueError("per_second must be positive")
        self.per_second = per_second
        self.per_hour = per_hour
        self._clock = clock
        self._sleep = sleep
        self._recent: collections.deque[float] = collections.deque()
        self.waits = 0

    def acquire(self) -> None:
        while True:
            now = self._clock()
            while self._recent and now - self._recent[0] > 3600.0:
                self._recent.popleft()
            last_second = [t for t in self._recent if now - t < 1.0]
            wait = 0.0
            if len(last_second) >= self.per_second:
                wait = max(wait, 1.0 - (now - last_second[0]))
            if self.per_hour is not None and len(self._recent) >= self.per_hour:
                wait = max(wait, 3600.0 - (now - self._recent[0]))
            if wait <= 0:
                self._recent.append(now)
                return
            self.waits += 1
            self._sleep(wait)


class SourceAdapter:
    """Base class: holds the declaration, the transport and the limiter."""

    declaration: SourceDeclaration

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        today: _dt.date | None = None,
    ) -> None:
        self._transport = transport
        self.limiter = RateLimiter(self.declaration.requests_per_second, clock=clock, sleep=sleep)
        self.today = today or _dt.date.today()
        self.requests_made = 0

    # -- transport -----------------------------------------------------------
    def _get(self, url: str, headers: Mapping[str, str] | None = None) -> bytes:
        if self._transport is None:
            raise RuntimeError(
                f"{type(self).__name__} has no transport: pass one (tools/free_data_coverage.py "
                "supplies urllib; tests supply fixtures). iap.data imports no network module."
            )
        if not url.startswith("https://"):
            raise ValueError(f"https only: {url}")
        self.limiter.acquire()
        h = {"User-Agent": self.declaration.user_agent}
        if headers:
            h.update(headers)
        self.requests_made += 1
        return self._transport(url, h)

    def _get_text(self, url: str, headers: Mapping[str, str] | None = None) -> str:
        return self._get(url, headers).decode("utf-8", errors="replace")

    # -- the two public methods ----------------------------------------------
    def coverage(self) -> Coverage:  # pragma: no cover - abstract
        raise NotImplementedError

    def fetch(self, target: str | os.PathLike[str], *, identity: str | None = None) -> None:
        """Ingest into a sealed partition.  Refuses outside the runner identity and a
        sealed target; until the D2 sealed service exists, raises after the checks."""
        require_runner(target, identity=identity)
        raise SealedServiceUnavailable(
            "identity and target accepted, but the D2 sealed service (U0 section 2) does not exist "
            "yet; DD1 ingestion lands after D2. No data written."
        )

    def _coverage(self) -> Coverage:
        return Coverage(source=self.declaration.contract.source, as_of=self.today.isoformat())


def parse_date(text: str) -> _dt.date:
    """``YYYY-MM-DD`` (with or without a time suffix) or ``YYYYMMDD`` to a date."""
    t = text.strip()
    if len(t) >= 8 and t[:8].isdigit() and (len(t) == 8 or not t[8].isdigit()):
        return _dt.date(int(t[:4]), int(t[4:6]), int(t[6:8]))
    return _dt.date.fromisoformat(t[:10])
