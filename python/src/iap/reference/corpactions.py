"""Corporate actions: an owner-supplied CSV table and an adjustment API.

The table is a CSV file the owner writes or exports (nothing is downloaded;
docs/REAL_DATA.md §5), header row required, columns in this order::

    ex_date,symbol,action,ratio_new,ratio_old,cash_amount,new_symbol

* ``SPLIT``          — ``ratio_new`` new shares for every ``ratio_old`` old
  ones, both positive integers (``4,1`` = 4-for-1; ``1,10`` = 1-for-10
  reverse split).  Other columns empty.
* ``DIVIDEND``       — ``cash_amount`` per share as a decimal string in the
  quote currency (``0.82``).  Other columns empty.
* ``SYMBOL_CHANGE``  — ``symbol`` becomes ``new_symbol`` on ``ex_date``.

``ex_date`` is the first session on which the action is in the prices.

**Point-in-time rule.**  Every query takes ``date`` (the session an
observation was made on) and ``as_of`` (the session whose basis the caller
wants, ``as_of >= date``); only actions with ``date < ex_date <= as_of``
apply.  An action that goes ex after ``as_of`` is not known yet and is
never applied, so re-basing history to an earlier ``as_of`` reproduces what
a study run on that day would have seen.

Split factors are exact :class:`fractions.Fraction` values.  Integer
conversions use the pinned rules: adjusted ticks round half up, adjusted
quantities round down (a fractional share does not rest in a book).
Dividend adjustment needs the close before each ex-date, supplied by the
caller, and returns a research double.
"""

from __future__ import annotations

import csv
import datetime as _dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

CSV_COLUMNS = (
    "ex_date",
    "symbol",
    "action",
    "ratio_new",
    "ratio_old",
    "cash_amount",
    "new_symbol",
)
ACTIONS = ("SPLIT", "DIVIDEND", "SYMBOL_CHANGE")


class CorporateActionError(ValueError):
    """The corporate-actions table or a query against it is invalid."""


@dataclass(frozen=True)
class CorporateAction:
    """One row of the table (unused fields are ``None``)."""

    ex_date: str
    symbol: str
    action: str
    ratio_new: int | None = None
    ratio_old: int | None = None
    cash_amount: Fraction | None = None
    new_symbol: str | None = None

    def to_dict(self) -> dict:
        return {
            "ex_date": self.ex_date,
            "symbol": self.symbol,
            "action": self.action,
            "ratio_new": self.ratio_new,
            "ratio_old": self.ratio_old,
            "cash_amount": None if self.cash_amount is None else str(self.cash_amount),
            "new_symbol": self.new_symbol,
        }


def _positive_int(text: str, what: str, line: int) -> int:
    if not text.isdigit() or int(text) < 1:
        raise CorporateActionError(f"line {line}: {what} must be a positive integer, got {text!r}")
    return int(text)


def _parse_row(row: dict[str, str], line: int) -> CorporateAction:
    values = {k: (row.get(k) or "").strip() for k in CSV_COLUMNS}
    try:
        _dt.date.fromisoformat(values["ex_date"])
    except ValueError:
        raise CorporateActionError(
            f"line {line}: ex_date must be YYYY-MM-DD, got {values['ex_date']!r}"
        ) from None
    if not values["symbol"]:
        raise CorporateActionError(f"line {line}: symbol is empty")
    action = values["action"].upper()

    def only(*allowed: str) -> None:
        extra = [
            k
            for k in ("ratio_new", "ratio_old", "cash_amount", "new_symbol")
            if values[k] and k not in allowed
        ]
        if extra:
            raise CorporateActionError(f"line {line}: {action} does not take {extra}")

    if action == "SPLIT":
        only("ratio_new", "ratio_old")
        return CorporateAction(
            values["ex_date"],
            values["symbol"],
            action,
            ratio_new=_positive_int(values["ratio_new"], "ratio_new", line),
            ratio_old=_positive_int(values["ratio_old"], "ratio_old", line),
        )
    if action == "DIVIDEND":
        only("cash_amount")
        try:
            cash = Fraction(Decimal(values["cash_amount"]))
        except (InvalidOperation, ValueError):
            raise CorporateActionError(
                f"line {line}: cash_amount must be a decimal number, got {values['cash_amount']!r}"
            ) from None
        if cash <= 0:
            raise CorporateActionError(f"line {line}: cash_amount must be > 0")
        return CorporateAction(values["ex_date"], values["symbol"], action, cash_amount=cash)
    if action == "SYMBOL_CHANGE":
        only("new_symbol")
        if not values["new_symbol"] or values["new_symbol"] == values["symbol"]:
            raise CorporateActionError(f"line {line}: new_symbol must be a different symbol")
        return CorporateAction(
            values["ex_date"], values["symbol"], action, new_symbol=values["new_symbol"]
        )
    raise CorporateActionError(
        f"line {line}: unknown action {values['action']!r}; known: {ACTIONS}"
    )


class CorporateActions:
    """The corporate-actions table with point-in-time adjustment queries."""

    def __init__(self, actions: list[CorporateAction] | None = None) -> None:
        self._actions = sorted(
            actions or [], key=lambda a: (a.ex_date, a.symbol, ACTIONS.index(a.action))
        )
        seen: set[tuple[str, str, str]] = set()
        for a in self._actions:
            key = (a.ex_date, a.symbol, a.action)
            if key in seen:
                raise CorporateActionError(f"duplicate corporate action {key}")
            seen.add(key)

    @classmethod
    def load_csv(cls, path: str | Path) -> CorporateActions:
        """Read and validate the CSV table (schema in the module docs)."""
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None or tuple(reader.fieldnames) != CSV_COLUMNS:
                raise CorporateActionError(
                    f"{path}: header must be exactly {','.join(CSV_COLUMNS)}, "
                    f"got {reader.fieldnames}"
                )
            return cls([_parse_row(row, i) for i, row in enumerate(reader, start=2)])

    def __len__(self) -> int:
        return len(self._actions)

    def actions(self) -> list[CorporateAction]:
        """Every action, sorted by (ex_date, symbol, action)."""
        return list(self._actions)

    def for_symbol(self, symbol: str, start_date: str = "", end_date: str = "") -> list:
        """Actions of ``symbol`` with ``start_date <= ex_date <= end_date``
        (an empty bound is open)."""
        return [
            a
            for a in self._actions
            if a.symbol == symbol
            and (not start_date or a.ex_date >= start_date)
            and (not end_date or a.ex_date <= end_date)
        ]

    # -- the point-in-time window --------------------------------------

    @staticmethod
    def _window(date: str, as_of: str) -> None:
        _dt.date.fromisoformat(date)
        _dt.date.fromisoformat(as_of)
        if as_of < date:
            raise CorporateActionError(
                f"as_of {as_of} is before the observation date {date}: adjusting to an "
                "earlier basis would apply actions in reverse"
            )

    def _between(self, symbol: str, action: str, date: str, as_of: str) -> list[CorporateAction]:
        self._window(date, as_of)
        return [
            a
            for a in self._actions
            if a.symbol == symbol and a.action == action and date < a.ex_date <= as_of
        ]

    # -- splits (exact) -------------------------------------------------

    def split_factor(self, symbol: str, date: str, as_of: str) -> Fraction:
        """Shares on ``as_of`` per share on ``date`` (1 when no split between)."""
        factor = Fraction(1)
        for a in self._between(symbol, "SPLIT", date, as_of):
            factor *= Fraction(a.ratio_new, a.ratio_old)
        return factor

    def adjust_price(self, price: Fraction | int, symbol: str, date: str, as_of: str) -> Fraction:
        """A ``date`` price expressed in the ``as_of`` share basis (exact)."""
        return Fraction(price) / self.split_factor(symbol, date, as_of)

    def adjust_price_ticks(self, price_ticks: int, symbol: str, date: str, as_of: str) -> int:
        """Split-adjusted ticks, rounded half up to a whole tick (pinned)."""
        exact = self.adjust_price(price_ticks, symbol, date, as_of)
        return (2 * exact.numerator + exact.denominator) // (2 * exact.denominator)

    def adjust_qty(self, qty: int, symbol: str, date: str, as_of: str) -> int:
        """Split-adjusted quantity, rounded down to a whole unit (pinned)."""
        exact = Fraction(qty) * self.split_factor(symbol, date, as_of)
        return exact.numerator // exact.denominator

    # -- dividends (research doubles) -----------------------------------

    def dividend_factor(
        self, symbol: str, date: str, as_of: str, closes: Mapping[str, float]
    ) -> float:
        """Total-return price factor for the cash dividends between the dates.

        Multiplying a ``date`` price by the factor removes the ex-dividend
        drops up to ``as_of``: the product over each dividend of
        ``1 - cash / close_before_ex``.  ``closes`` maps a session date to
        the unadjusted close; the close used for a dividend is the one of
        the latest session strictly before its ex-date, which must be
        present.
        """
        factor = 1.0
        for a in self._between(symbol, "DIVIDEND", date, as_of):
            before = [d for d in closes if d < a.ex_date]
            if not before:
                raise CorporateActionError(
                    f"dividend {symbol} ex {a.ex_date}: no close before the ex-date was supplied"
                )
            close = float(closes[max(before)])
            if not close > float(a.cash_amount):
                raise CorporateActionError(
                    f"dividend {symbol} ex {a.ex_date}: cash {a.cash_amount} is not below the "
                    f"previous close {close}"
                )
            factor *= 1.0 - float(a.cash_amount) / close
        return factor

    def total_return_price(
        self, price: float, symbol: str, date: str, as_of: str, closes: Mapping[str, float]
    ) -> float:
        """Split- and dividend-adjusted price in the ``as_of`` basis."""
        return (
            float(price)
            / float(self.split_factor(symbol, date, as_of))
            * self.dividend_factor(symbol, date, as_of, closes)
        )

    # -- symbol lineage -------------------------------------------------

    def symbol_as_of(self, symbol: str, date: str, as_of: str) -> str:
        """The name on ``as_of`` of the security called ``symbol`` on ``date``."""
        self._window(date, as_of)
        current, cursor = symbol, date
        while True:
            changes = self._between(current, "SYMBOL_CHANGE", cursor, as_of)
            if not changes:
                return current
            current, cursor = changes[0].new_symbol, changes[0].ex_date

    def symbol_on(self, symbol: str, as_of: str, date: str) -> str:
        """The name on ``date`` of the security called ``symbol`` on ``as_of``."""
        self._window(date, as_of)
        current, cursor = symbol, as_of
        while True:
            changes = [
                a
                for a in self._actions
                if a.action == "SYMBOL_CHANGE"
                and a.new_symbol == current
                and date < a.ex_date <= cursor
            ]
            if not changes:
                return current
            last = changes[-1]
            current = last.symbol
            cursor = (_dt.date.fromisoformat(last.ex_date) - _dt.timedelta(days=1)).isoformat()
