"""Dated US equity regulatory fee schedule (v1.15, X-P1-1).

``configs/reference/regulatory_fee_schedule.json`` holds two tables of
dated periods: the SEC Section 31 transaction fee (USD per million of
notional) and the FINRA Trading Activity Fee (USD per share, capped per
trade). Both are charged on SELL fills of EQUITY/ETF venues only. A period
covers fills with ``from_ns <= ts < until_ns`` (``until_ns`` absent =
open-ended); a fill outside every period of a table raises (fail closed —
the schedule only covers windows whose rate was verified).

The arithmetic is pinned and shared bit-for-bit with the C++
(``iap/execution/regulatory_fees.hpp``) and Rust (``venue::regfees``)
ports, checked against ``tests/golden/expected_regulatory_fees.json``::

    sec   = usd_per_million * notional / 1e6
    taf   = min(per_share * qty, max_per_trade)
    total = sec + taf            (0.0 on a buy)

Off by default everywhere (``ExecConfig.regulatory_fees is None``), so v1
numbers are unchanged; ``configs/prereg/v2.json`` turns it on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: Repo-relative location of the shipped schedule.
SCHEDULE_RELPATH = "reference/regulatory_fee_schedule.json"


@dataclass(frozen=True, slots=True)
class SecPeriod:
    from_ns: int
    until_ns: int | None
    usd_per_million: float


@dataclass(frozen=True, slots=True)
class TafPeriod:
    from_ns: int
    until_ns: int | None
    per_share: float
    max_per_trade: float


def _find(periods, ts: int, what: str):
    for p in periods:
        if p.from_ns <= ts and (p.until_ns is None or ts < p.until_ns):
            return p
    raise ValueError(f"no {what} period covers exchange_ts {ts}")


def _check_ordered(periods, what: str) -> None:
    if not periods:
        raise ValueError(f"regulatory fee schedule: empty {what} table")
    for i, p in enumerate(periods):
        if p.until_ns is not None and p.until_ns <= p.from_ns:
            raise ValueError(f"{what} period {i}: until_ns <= from_ns")
        if p.until_ns is None and i != len(periods) - 1:
            raise ValueError(f"{what} period {i}: only the last period may be open-ended")
        if i and periods[i - 1].until_ns is not None and p.from_ns < periods[i - 1].until_ns:
            raise ValueError(f"{what} period {i} overlaps the previous one")


@dataclass(frozen=True)
class RegulatoryFeeSchedule:
    sec: tuple[SecPeriod, ...]
    taf: tuple[TafPeriod, ...]

    def __post_init__(self) -> None:
        _check_ordered(self.sec, "sec_section31")
        _check_ordered(self.taf, "finra_taf")
        for p in self.sec:
            if p.usd_per_million < 0:
                raise ValueError("sec_section31 rate must be >= 0")
        for p in self.taf:
            if p.per_share < 0 or p.max_per_trade < 0:
                raise ValueError("finra_taf rates must be >= 0")

    @classmethod
    def from_dict(cls, root: dict) -> RegulatoryFeeSchedule:
        def until(row):
            v = row.get("until_ns")
            return None if v is None else int(v)

        sec = tuple(
            SecPeriod(int(r["from_ns"]), until(r), float(r["usd_per_million"]))
            for r in root["sec_section31"]
        )
        taf = tuple(
            TafPeriod(int(r["from_ns"]), until(r), float(r["per_share"]), float(r["max_per_trade"]))
            for r in root["finra_taf"]
        )
        return cls(sec, taf)

    @classmethod
    def load(cls, path) -> RegulatoryFeeSchedule:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def sec_rate(self, ts: int) -> SecPeriod:
        return _find(self.sec, ts, "sec_section31")

    def taf_rate(self, ts: int) -> TafPeriod:
        return _find(self.taf, ts, "finra_taf")

    def fee(self, side: int, qty: float, notional: float, ts: int) -> float:
        """Regulatory fee of one equity fill (``side`` 0 buy / 1 sell)."""
        if side != 1:
            return 0.0
        s = self.sec_rate(ts)
        t = self.taf_rate(ts)
        sec = s.usd_per_million * notional / 1e6
        taf = min(t.per_share * qty, t.max_per_trade)
        return sec + taf


def load_default_schedule(config_dir) -> RegulatoryFeeSchedule:
    """The shipped schedule under a ``configs/`` directory."""
    return RegulatoryFeeSchedule.load(Path(config_dir) / SCHEDULE_RELPATH)


def prereg_wants_regulatory_fees(prereg: dict) -> bool:
    """True iff a prereg config opts in (``configs/prereg/v2.json``)."""
    v = prereg.get("regulatory_fees", False)
    if not isinstance(v, bool):
        raise ValueError("prereg 'regulatory_fees' must be a bool")
    return v
