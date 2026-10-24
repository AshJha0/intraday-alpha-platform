"""Point-in-time universe with an as-of availability lag and delisting retention (v2.0 U2).

The rules are E0 section 2's, written once for every route and family:

- **As-of availability.**  A constituent file dated ``D`` (an issuer's daily
  holdings file, an index vendor's membership list) describes the universe on
  ``D`` but is *usable* only from the session at which it has been published:
  the first session on or after ``D`` plus the declared publication lag in
  sessions (:class:`~iap.data.contracts.AvailabilityRule` with basis
  ``publication_date``; E0's "the first session after D + 1 business day" is a
  lag of two sessions when ``D`` is itself a session).  A session before the
  first available file has **no** universe and :meth:`members` refuses it
  instead of reading a later file.
- **No retroactive removal.**  A name dropped by a later file leaves the
  universe only when that later file becomes available.
- **Survivorship.**  A name that later delists stays a member for every
  session on which it was a constituent, through its last traded session
  (``delist_date``, inclusive).
- **Delisting retention.**  For ``retention_sessions`` sessions after its last
  traded session a delisted name is *retained*: not tradable, not a member,
  but present in :meth:`retained` with ``delisted=True`` so that open positions
  are closed out against the delisting valuation and labels that straddle the
  delisting are computed, never silently dropped.

Sessions are ISO dates (``YYYY-MM-DD``) on a caller-supplied calendar (the
exchange calendar of the data manifest; synthetic in tests).  Nothing here
reads data; the survivorship check takes the panel's (symbol, session)
coverage as an argument so the development identity can run it on its own
partitions and ``iap-runner`` on the sealed ones.
"""

from __future__ import annotations

import bisect
import datetime as _dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from iap.data.contracts import AvailabilityRule

__all__ = [
    "ConstituentFile",
    "PITError",
    "PointInTimeUniverse",
    "RetainedName",
    "iso_date",
]


class PITError(ValueError):
    """A point-in-time rule would be violated (lookahead, unknown session, malformed input)."""


def iso_date(text: str) -> str:
    """Validate ``YYYY-MM-DD`` and return it unchanged."""
    if not isinstance(text, str):
        raise PITError(f"date must be an ISO string, got {type(text).__name__}")
    try:
        _dt.date.fromisoformat(text)
    except ValueError as exc:
        raise PITError(f"not an ISO date: {text!r}") from exc
    if len(text) != 10:
        raise PITError(f"date must be YYYY-MM-DD, got {text!r}")
    return text


@dataclass(frozen=True, slots=True)
class ConstituentFile:
    """A dated membership snapshot: the names the file dated ``file_date`` lists."""

    file_date: str
    members: tuple[str, ...]
    source: str = ""

    def __post_init__(self) -> None:
        iso_date(self.file_date)
        if not isinstance(self.members, tuple) or not all(
            isinstance(m, str) and m for m in self.members
        ):
            raise PITError("members must be a tuple of non-empty symbols")
        if len(set(self.members)) != len(self.members):
            raise PITError(f"file {self.file_date} lists a symbol twice")
        object.__setattr__(self, "members", tuple(sorted(self.members)))


@dataclass(frozen=True, slots=True)
class RetainedName:
    """One row of :meth:`PointInTimeUniverse.retained`."""

    symbol: str
    member: bool
    delisted: bool
    tradable: bool
    delist_date: str | None = None


class PointInTimeUniverse:
    """The universe as it was knowable on each session (module docstring)."""

    def __init__(
        self,
        sessions: Sequence[str],
        files: Iterable[ConstituentFile],
        availability: AvailabilityRule,
        delistings: Mapping[str, str] | None = None,
        retention_sessions: int = 0,
    ) -> None:
        if not sessions:
            raise PITError("a universe needs a session calendar")
        cal = [iso_date(s) for s in sessions]
        if cal != sorted(cal) or len(set(cal)) != len(cal):
            raise PITError("sessions must be strictly increasing ISO dates")
        self._sessions: list[str] = cal
        self._index: dict[str, int] = {s: i for i, s in enumerate(cal)}
        if (
            not isinstance(availability, AvailabilityRule)
            or availability.basis != "publication_date"
        ):
            raise PITError("availability must be an AvailabilityRule with basis 'publication_date'")
        self._availability = availability
        if (
            not isinstance(retention_sessions, int)
            or isinstance(retention_sessions, bool)
            or retention_sessions < 0
        ):
            raise PITError("retention_sessions must be an int >= 0")
        self._retention = retention_sessions
        self._files: list[ConstituentFile] = sorted(files, key=lambda f: f.file_date)
        if not self._files:
            raise PITError("a universe needs at least one constituent file")
        dates = [f.file_date for f in self._files]
        if len(set(dates)) != len(dates):
            raise PITError("two constituent files share a date")
        # available_from[i] = session index at which file i may first be read (None = never on this calendar)
        self._available_from: list[int | None] = []
        for f in self._files:
            p = bisect.bisect_left(
                self._sessions, f.file_date
            )  # first session on or after the file date
            a = self._availability.available_index(p)
            self._available_from.append(a if a < len(self._sessions) else None)
        self._delist: dict[str, str] = {}
        for sym, d in (delistings or {}).items():
            if not isinstance(sym, str) or not sym:
                raise PITError("delisting symbol must be a non-empty string")
            self._delist[sym] = iso_date(d)

    # ------------------------------------------------------------------ calendar
    @property
    def sessions(self) -> tuple[str, ...]:
        return tuple(self._sessions)

    @property
    def availability(self) -> AvailabilityRule:
        return self._availability

    @property
    def retention_sessions(self) -> int:
        return self._retention

    def session_index(self, session: str) -> int:
        iso_date(session)
        try:
            return self._index[session]
        except KeyError:
            raise PITError(f"{session} is not a session of this calendar") from None

    # ------------------------------------------------------------------ membership
    def file_in_force(self, session: str) -> ConstituentFile:
        """The latest constituent file available as of ``session``; refuses if none is."""
        i = self.session_index(session)
        best: ConstituentFile | None = None
        for f, a in zip(self._files, self._available_from, strict=True):
            if a is not None and a <= i:
                best = f  # files are date-sorted, so the last available one is the latest
        if best is None:
            first = self._files[0]
            raise PITError(
                f"no constituent file is available as of {session}: the earliest file is dated "
                f"{first.file_date} and becomes usable with lag {self._availability.lag_periods} session(s); "
                "refusing to read a later file"
            )
        return best

    def available_from(self, file_date: str) -> str | None:
        """The first session on which the file dated ``file_date`` may be read (None if beyond the calendar)."""
        for f, a in zip(self._files, self._available_from, strict=True):
            if f.file_date == file_date:
                return None if a is None else self._sessions[a]
        raise PITError(f"no constituent file dated {file_date}")

    def is_delisted_on(self, symbol: str, session: str) -> bool:
        """True once ``session`` is strictly after the name's last traded session."""
        d = self._delist.get(symbol)
        return d is not None and session > d

    def members(self, session: str) -> tuple[str, ...]:
        """Tradable constituents on ``session`` under the as-of rule (sorted)."""
        f = self.file_in_force(session)
        return tuple(m for m in f.members if not self.is_delisted_on(m, session))

    def retained(self, session: str) -> tuple[RetainedName, ...]:
        """Members plus delisted names inside the retention window (sorted by symbol)."""
        f = self.file_in_force(session)
        i = self.session_index(session)
        rows: dict[str, RetainedName] = {}
        for m in f.members:
            if not self.is_delisted_on(m, session):
                rows[m] = RetainedName(
                    m, member=True, delisted=False, tradable=True, delist_date=self._delist.get(m)
                )
        if self._retention:
            # a name delisted on session index j is retained for sessions j+1 .. j+retention
            for sym, d in self._delist.items():
                j = bisect.bisect_right(self._sessions, d) - 1  # the last traded session's index
                if j < i <= j + self._retention and self._was_member_at_delisting(sym, d):
                    rows.setdefault(
                        sym,
                        RetainedName(
                            sym, member=False, delisted=True, tradable=False, delist_date=d
                        ),
                    )
        return tuple(rows[k] for k in sorted(rows))

    def _was_member_at_delisting(self, symbol: str, delist_date: str) -> bool:
        """A retained name must have been a constituent on its last traded session (or the nearest earlier one)."""
        j = bisect.bisect_right(self._sessions, delist_date) - 1
        if j < 0:
            return False
        try:
            f = self.file_in_force(self._sessions[j])
        except PITError:
            return False
        return symbol in f.members

    # ------------------------------------------------------------------ audits
    def membership_table(self) -> list[dict[str, object]]:
        """One row per session: the file in force, member count, delisted-but-retained count."""
        out: list[dict[str, object]] = []
        for s in self._sessions:
            try:
                f = self.file_in_force(s)
            except PITError:
                out.append({"session": s, "file_date": None, "members": 0, "retained_delisted": 0})
                continue
            rows = self.retained(s)
            out.append(
                {
                    "session": s,
                    "file_date": f.file_date,
                    "members": sum(r.member for r in rows),
                    "retained_delisted": sum(r.delisted for r in rows),
                }
            )
        return out

    def survivorship_check(self, coverage: Mapping[str, Iterable[str]]) -> list[str]:
        """Findings (empty = pass) against a panel's ``{symbol: sessions with a row}``.

        Every member must have a row on every session it is a member of (a
        missing delisted name is the survivorship defect DD1 tests for), and no
        name may have a row after its last traded session plus the retention
        window (a row there means the panel was not point-in-time).
        """
        have: dict[str, set[str]] = {k: set(v) for k, v in coverage.items()}
        findings: list[str] = []
        for s in self._sessions:
            try:
                members = self.members(s)
            except PITError:
                continue
            for m in members:
                if s not in have.get(m, set()):
                    findings.append(
                        f"{m} is a member on {s} but the panel has no row (survivorship gap)"
                    )
        for sym, d in self._delist.items():
            j = bisect.bisect_right(self._sessions, d) - 1
            limit = (
                self._sessions[min(j + self._retention, len(self._sessions) - 1)] if j >= 0 else d
            )
            for s in sorted(have.get(sym, set())):
                if s > limit:
                    findings.append(
                        f"{sym} delisted {d} has a panel row on {s}, after its retention window"
                    )
        return findings
