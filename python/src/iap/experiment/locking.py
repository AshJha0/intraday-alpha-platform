"""Cross-process exclusion and atomic writes for the research store.

The research artefacts (``research/experiments.json``,
``research/models/ledger.json``, the per-experiment directories) are plain
files that several automated writers may touch at once.  Two primitives make
a read-modify-write of such a file safe without any dependency:

* :class:`FileLock` — an exclusive lock taken by creating ``<path>.lock``
  with ``O_CREAT | O_EXCL``.  Creation of a new directory entry is atomic on
  every platform and file system this repository runs on (POSIX and
  Windows), so exactly one process wins; the others retry on a bounded
  schedule and then fail loudly with :class:`LockTimeout` naming the lock
  file.  The holder's pid is written into the file for the operator; the
  lock is never broken automatically (a stale lock left by a killed process
  is an operator decision, not something to guess from a clock).
* :func:`atomic_write_text` — write to a sibling temporary file, flush,
  ``fsync`` and ``os.replace`` it over the target, so a reader sees either
  the old document or the new one, never a torn one.

The retry schedule uses ``time.sleep`` and a monotonic deadline.  That is
scheduling, not content: nothing here writes a wall-clock value into any
artefact (PLATFORM_CONVENTIONS.md §3).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

__all__ = ["FileLock", "LockTimeout", "atomic_write_text"]

#: Default bound on how long a writer waits for the lock (seconds).
DEFAULT_TIMEOUT_S = 30.0
#: Pause between acquisition attempts (seconds).
RETRY_INTERVAL_S = 0.01
#: ``os.replace`` attempts when a reader holds the target open (Windows).
_REPLACE_ATTEMPTS = 200


class LockTimeout(RuntimeError):
    """The lock file could not be created within the bounded wait."""


class FileLock:
    """Exclusive advisory lock on ``<target>.lock`` (see module docs).

    Use as a context manager around a read-modify-write of ``target``::

        with FileLock(path):
            doc = json.loads(path.read_text())
            ...
            atomic_write_text(path, render(doc))

    Not re-entrant: a second acquisition by the same process waits like any
    other writer and times out.
    """

    def __init__(
        self,
        target,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        retry_interval_s: float = RETRY_INTERVAL_S,
    ) -> None:
        if timeout_s < 0 or retry_interval_s <= 0:
            raise ValueError("timeout_s must be >= 0 and retry_interval_s > 0")
        target = Path(target)
        self.lock_path = target.with_name(target.name + ".lock")
        self.timeout_s = float(timeout_s)
        self.retry_interval_s = float(retry_interval_s)
        self._held = False

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pass
            except PermissionError:
                # Windows: the previous holder is mid-unlink (delete pending).
                pass
            else:
                try:
                    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
                finally:
                    os.close(fd)
                self._held = True
                return
            if time.monotonic() >= deadline:
                raise LockTimeout(
                    f"could not acquire {self.lock_path} within "
                    f"{self.timeout_s:g}s; another writer holds it — if no "
                    "writer is running, the lock is stale and must be removed "
                    "by hand"
                )
            time.sleep(self.retry_interval_s)

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.unlink(str(self.lock_path))
                return
            except FileNotFoundError:
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(self.retry_interval_s)

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def atomic_write_text(
    path, text: str, encoding: Optional[str] = None, newline: Optional[str] = None
) -> None:
    """Replace ``path`` with ``text`` atomically (temp file + ``os.replace``).

    The temporary file lives in the target's directory (same file system, so
    the replace is a rename) and carries the writer's pid, so two writers
    never share one.  ``encoding`` / ``newline`` are passed to ``open`` and
    default to the platform's, exactly as ``Path.write_text`` does.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(str(tmp), str(path))
                return
            except PermissionError:
                # Windows refuses to replace a file another process has open
                # for reading; readers hold it for microseconds.
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(RETRY_INTERVAL_S)
    finally:
        if tmp.exists():
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
