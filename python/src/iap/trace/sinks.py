"""Trace sinks — implementations of :class:`iap.contracts.protocols.TraceSink`.

* :class:`MemoryTraceSink` keeps the traces in a list (tests, digests).
* :class:`JsonlTraceSink` appends one canonical JSON line per trace to a
  file and flushes after every line, so a crash loses nothing that was
  emitted; the bytes are exactly what :class:`~iap.trace.digest.TraceDigest`
  hashes.
* :class:`StoreTraceSink` writes each trace into a :class:`iap.store.Store`
  (document + normalized decomposition, one transaction).
* :class:`MultiSink` fans one ``emit`` out to several sinks in order.

Every sink validates the trace against its schema before persisting it
(``validate_typed``) and never mutates it.  Ordering is the caller's: the
loop emits in ``(event_ts, sequence)`` order and the sinks preserve it.
"""

from __future__ import annotations

from pathlib import Path
from typing import IO, List, Optional, Sequence, Tuple, Union

from iap.contracts.protocols import TraceSink
from iap.contracts.types import DecisionTrace
from iap.contracts.validate import validate_typed
from iap.contracts.versions import canonical_json
from iap.store.db import Store
from iap.trace.digest import TraceDigest

__all__ = ["JsonlTraceSink", "MemoryTraceSink", "MultiSink", "StoreTraceSink"]


class MemoryTraceSink:
    """Keeps every emitted trace, in order, plus a running digest."""

    def __init__(self) -> None:
        self._traces: List[DecisionTrace] = []
        self.digest = TraceDigest()

    @property
    def traces(self) -> Tuple[DecisionTrace, ...]:
        return tuple(self._traces)

    def emit(self, trace: DecisionTrace) -> None:
        validate_typed(trace)
        self._traces.append(trace)
        self.digest.update(trace)

    def __len__(self) -> int:
        return len(self._traces)


class JsonlTraceSink:
    """One canonical JSON line per trace, flushed on every emit.

    ``append=False`` (default) truncates an existing file so a re-run
    produces byte-identical output; ``append=True`` continues a file (the
    resume path).  Use as a context manager or call :meth:`close`.
    """

    def __init__(self, path: Union[str, Path], append: bool = False) -> None:
        self.path = Path(path)
        self._fh: Optional[IO[str]] = open(self.path, "a" if append else "w",
                                           encoding="ascii", newline="\n")
        self.digest = TraceDigest()

    def emit(self, trace: DecisionTrace) -> None:
        if self._fh is None:
            raise RuntimeError(f"JsonlTraceSink({self.path}) is closed")
        validate_typed(trace)
        line = canonical_json(trace.to_dict())
        self._fh.write(line)
        self._fh.write("\n")
        self._fh.flush()
        self.digest.update_line(line)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "JsonlTraceSink":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class StoreTraceSink:
    """Writes each trace to the store (:meth:`iap.store.Store.insert_trace`,
    which validates and decomposes it in one transaction)."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.digest = TraceDigest()

    def emit(self, trace: DecisionTrace) -> None:
        self.store.insert_trace(trace)
        self.digest.update(trace)


class MultiSink:
    """Fans out ``emit`` to every sink, in the given order.  The first
    failing sink stops the fan-out (the exception propagates), so a
    partial write is visible rather than silently skipped."""

    def __init__(self, *sinks: TraceSink) -> None:
        for sink in sinks:
            if not isinstance(sink, TraceSink):
                raise TypeError(f"MultiSink: {type(sink).__name__} has no emit()")
        self._sinks: Tuple[TraceSink, ...] = tuple(sinks)

    @property
    def sinks(self) -> Sequence[TraceSink]:
        return self._sinks

    def emit(self, trace: DecisionTrace) -> None:
        for sink in self._sinks:
            sink.emit(trace)

    def close(self) -> None:
        """Close every sink that has a ``close``."""
        for sink in self._sinks:
            close = getattr(sink, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "MultiSink":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
