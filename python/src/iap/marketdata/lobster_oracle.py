"""External book oracle: LOBSTER orderbook file vs the platform book (REL-12).

:class:`LobsterConverter` stops at the first divergence.  This oracle uses
the same mapping (seeding, back-fill, unseen-order handling) but compares
**every** row, level by level, and never stops, so it can report a match
rate per level and classify every mismatch.  The book is not
re-synchronised after a difference: a persistent error shows as many rows,
and *onsets* (a level that mismatches when it matched on the previous row)
are counted separately, attributed to the LOBSTER message type of the row.

This checks the book reconstruction rules, not the ITCH 5.0 byte parser:
LOBSTER is derived from ITCH, but no ITCH bytes are read here
(docs/RESEARCH_NOTES.md).

Usage::

    python -I -m iap.marketdata.lobster_oracle MESSAGE.csv ORDERBOOK.csv \
        --date 2012-06-21 --out summary.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from iap.marketdata.lobster import _ASK, _BID, _CANCEL, _MODIFY, LobsterConverter, _int

MISMATCH_CLASSES = ("price", "size", "missing_level", "extra_level")
MAX_EXAMPLES = 20


class LobsterOracle(LobsterConverter):
    """Full-file, level-by-level comparison (never stops at a divergence)."""

    def __init__(self, *args, resync: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        #: resync=True: after a mismatching row, force the visible window back
        #: to LOBSTER's, so each row tests ONE message's transition.
        self.resync = resync
        self.resyncs = 0
        self.rejected_by_type: dict[str, int] = {}
        self._row_mtype = 0
        self._row_unseen = False
        self._row_oid = 0
        #: real orders whose size a resync changed (attribution is a guess)
        self._resync_oids: set[int] = set()
        #: onset cause: deepest level (window edge) vs levels 1..n-1 split by
        #: whether the row's message referenced an order we never saw added
        self.onsets_by_cause: dict[str, int] = {}
        self.rows = 0
        self.rows_full_match = 0
        self.rejected_events = 0
        self.level_match: dict[str, list[int]] = {}
        self.level_rows: dict[str, list[int]] = {}
        self.mismatch_classes: dict[str, int] = {c: 0 for c in MISMATCH_CLASSES}
        self.onsets_by_type: dict[str, int] = {}
        self.onsets_by_level: dict[str, list[int]] = {}
        self.examples: dict[str, list[dict]] = {}
        self._prev_ok: dict[str, list[bool]] = {}

    # record the message type that produced each row
    def _apply_message(self, out, line, ts, fields):  # type: ignore[override]
        self._row_mtype = _int(fields[1], "event type", line)
        self._row_unseen = False
        self._row_oid = _int(fields[2], "order id", line)
        super()._apply_message(out, line, ts, fields)

    def _unseen_order(self, out, ts, mtype, side, price, size):  # type: ignore[override]
        self._row_unseen = True
        super()._unseen_order(out, ts, mtype, side, price, size)

    def _first_row(self, out, ts, fields, ref):  # type: ignore[override]
        self._row_mtype = _int(fields[1], "event type", 1)
        super()._first_row(out, ts, fields, ref)

    def _diverge(self, detail: dict) -> None:  # keep going; count instead
        if detail.get("reason") == "the platform book rejected a mapped event":
            self.rejected_events += 1
            key = str(self._row_mtype)
            self.rejected_by_type[key] = self.rejected_by_type.get(key, 0) + 1

    def _verify(self, out, index, ts, ref):  # type: ignore[override]
        n = self.levels
        if not self.level_match:
            for name in ("ask", "bid"):
                self.level_match[name] = [0] * n
                self.level_rows[name] = [0] * n
                self.onsets_by_level[name] = [0] * n
                self._prev_ok[name] = [True] * n
        row_ok = True
        for side, name in ((_ASK, "ask"), (_BID, "bid")):
            levels = ref[side]
            horizon = self._horizon[side]
            if horizon is not None:
                for price, size in levels:
                    beyond = price > horizon if side == _ASK else price < horizon
                    if beyond:
                        have = self._book.level_qty(side, price)
                        if have < size:
                            self.counters["backfilled_levels"] += 1
                            self._seed(out, ts, side, price, size - have)
            ours = self._book.depth(side, n)
            for k in range(n):
                exp = levels[k] if k < len(levels) else None
                act = ours[k] if k < len(ours) else None
                self.level_rows[name][k] += 1
                ok = exp == act
                if ok:
                    self.level_match[name][k] += 1
                else:
                    row_ok = False
                    if act is None:
                        cls = "missing_level"
                    elif exp is None:
                        cls = "extra_level"
                    elif exp[0] != act[0]:
                        cls = "price"
                    else:
                        cls = "size"
                    self.mismatch_classes[cls] += 1
                    if self._prev_ok[name][k]:
                        key = str(self._row_mtype)
                        self.onsets_by_type[key] = self.onsets_by_type.get(key, 0) + 1
                        self.onsets_by_level[name][k] += 1
                        if k == n - 1:
                            cause = "deepest_level"
                        elif self._row_oid in self._resync_oids:
                            cause = "inner_level_message_on_order_resync_changed"
                        elif self._row_unseen:
                            cause = "inner_level_message_on_unseen_order"
                        else:
                            cause = "inner_level_message_on_known_order"
                        self.onsets_by_cause[cause] = self.onsets_by_cause.get(cause, 0) + 1
                        bucket = self.examples.setdefault(cause, [])
                        if len(bucket) < MAX_EXAMPLES:
                            bucket.append(
                                {
                                    "row": index + 1,
                                    "message_type": self._row_mtype,
                                    "side": name,
                                    "level": k + 1,
                                    "class": cls,
                                    "expected": list(exp) if exp else None,
                                    "actual": list(act) if act else None,
                                }
                            )
                self._prev_ok[name][k] = ok or self.resync
            if self.resync and ours != levels:
                self._resync_side(out, ts, side, levels, n)
            if len(levels) < n or horizon is None:
                self._horizon[side] = None
            else:
                worst = levels[-1][0]
                self._horizon[side] = max(horizon, worst) if side == _ASK else min(horizon, worst)
        self.rows += 1
        self.rows_full_match += row_ok
        self._rows_verified = index + 1

    def _reduce(self, out, ts, side: int, price: int, qty: int) -> None:
        """Remove ``qty`` resting at a level: seed orders first, then real
        orders newest first (LOBSTER does not say which order went)."""
        seeds = self._seeds.get((side, price))
        if seeds:
            take = min(qty, sum(x[1] for x in seeds))
            self._consume_seed(out, ts, side, price, take, _MODIFY)
            self.counters["events_on_seed_orders"] -= 1
            qty -= take
        if qty <= 0:
            return
        oids = [o for o, v in self._orders.items() if v[0] == side and v[1] == price]
        for oid in reversed(oids):
            rem = self._orders[oid][2]
            take = min(rem, qty)
            self._resync_oids.add(oid)
            qty -= take
            if take == rem:
                del self._orders[oid]
                self._emit(out, ts, _CANCEL, side, price, 0, oid, 0)
            else:
                self._orders[oid][2] = rem - take
                self._emit(out, ts, _MODIFY, side, price, rem - take, oid, 0)
            if qty <= 0:
                return

    def _resync_side(self, out, ts, side: int, levels, n: int) -> None:
        self.resyncs += 1
        target = dict(levels)
        full = len(levels) == n
        worst = levels[-1][0] if levels else None

        def inside(price: int) -> bool:
            if not full:
                return True
            return price <= worst if side == _ASK else price >= worst

        prices = {p for p, _ in self._book.depth(side, 10_000) if inside(p)} | set(target)
        for price in prices:
            have = self._book.level_qty(side, price)
            want = target.get(price, 0)
            if have < want:
                self._seed(out, ts, side, price, want - have)
            elif have > want:
                self._reduce(out, ts, side, price, have - want)

    def summary(self) -> dict:
        n = self.levels
        pooled = []
        for k in range(n):
            m = self.level_match["ask"][k] + self.level_match["bid"][k]
            r = self.level_rows["ask"][k] + self.level_rows["bid"][k]
            pooled.append(round(m / r, 6) if r else None)
        per_side = {
            name: [round(self.level_match[name][k] / self.level_rows[name][k], 6) for k in range(n)]
            for name in ("ask", "bid")
        }
        return {
            "levels": n,
            "rows": self.rows,
            "messages_read": self.messages_read,
            "message_counts": self.message_counts,
            "rows_full_book_match": self.rows_full_match,
            "full_book_match_rate": round(self.rows_full_match / self.rows, 6)
            if self.rows
            else None,
            "match_rate_per_level_pooled": pooled,
            "match_rate_per_level": per_side,
            "mismatch_level_rows_by_class": self.mismatch_classes,
            "onsets_by_message_type": dict(sorted(self.onsets_by_type.items())),
            "onsets_by_level": self.onsets_by_level,
            "onsets_by_cause": dict(sorted(self.onsets_by_cause.items())),
            "mode": "resync" if self.resync else "free_running",
            "resyncs": self.resyncs,
            "rejected_events": self.rejected_events,
            "rejected_events_by_message_type": dict(sorted(self.rejected_by_type.items())),
            "converter_counters": self.counters,
            "first_onset_examples_by_cause": dict(sorted(self.examples.items())),
        }


def run_oracle(
    message_path: str | Path,
    orderbook_path: str | Path,
    date: str,
    limit_messages: int | None = None,
    resync: bool = False,
) -> dict:
    oracle = LobsterOracle(date, 0, message_path, orderbook_path, limit_messages, resync=resync)
    for _ in oracle.events():
        pass
    return oracle.summary()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("message")
    ap.add_argument("orderbook")
    ap.add_argument("--date", required=True)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--resync", action="store_true")
    a = ap.parse_args(argv)
    result = run_oracle(a.message, a.orderbook, a.date, a.limit, a.resync)
    text = json.dumps(result, indent=2, sort_keys=True)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
