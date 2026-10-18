"""Cross-venue stale-quote protection for a maker (v1.18 REL-09, alpha hypothesis 4).

Study code only. Nothing here reads real data: the study read on the seven
Nasdaq/IEX overlap days is a ledger look that needs the signed
pre-declaration (``research/v118/REL09_PREDECLARATION_DRAFT.md``, a DRAFT
with open parameters). Tests run on synthetic fixtures.

**Input.** One merged two-venue stream (:func:`iap.research.two_venue.merge_events`),
which keeps each event's original ``exchange_ts`` and its ``receive_ts``.
The study needs both: decisions are taken on what the quoter has RECEIVED
(``receive_ts``), evaluation (markouts, fills) is on the exchange clock.

**Causal stale-quote indicator** (:class:`StaleQuoteMonitor`). Events are
applied in ``(receive_ts, event_id)`` order to one :class:`OrderBook` per
(venue, instrument). For a quote on venue ``v`` the indicator at time ``t``
reads only events received at or before ``t``:

- *quote age*: ``t`` minus the receive time of ``v``'s last best-price change
  on that side;
- *displacement*: the other venue's mid at ``t`` minus its mid at the moment
  ``v``'s side last changed, in ticks (signed; a falling other-venue mid
  threatens a resting bid, a rising one a resting ask).

The side is flagged stale when ``age >= min_age_ns`` and the displacement
against it is at least ``min_displacement_ticks``. The rule is the
hypothesis the pre-declaration fixes; it is not tuned here.

**Reconstructed eligible resting orders** (:func:`simulate_quoter`). A
one-lot-at-a-time quoter joins the best bid and ask on one venue at every
refresh instant of a fixed grid (``refresh_ns``) and after each own fill.
Its queue ahead is the displayed level quantity at the post; displayed
executions at that venue, side and price consume the queue ahead first and
then fill the quote (partial fills kept). A price improvement on the
quote's side does not move the quote until the next refresh. Cancels ahead
are NOT credited (conservative: the quote fills later or not at all). The
filtered policy is the same quoter that also pulls a side while the
indicator flags it, the pull taking effect ``cancel_latency_ns`` after the
decision; it re-posts when the flag clears. Both policies run on the same
events (paired by session).

**Markouts and P&L.** For a fill of signed quantity ``s*q`` (``s = +1``
buy) at price ``p``: ``m0`` is the QUOTING venue's own mid just before the
fill (what the maker believed it was earning half a spread against), ``mh``
the CONSOLIDATED mid (both venues' books, exchange clock) at the markout
horizon and ``mT`` the consolidated mid at the end of the session. A stale
quote's loss therefore shows in ``markout`` (the other venue had already
moved), not in ``spread``:

- ``spread  = s*q*(m0 - p)``  (spread captured against the mid)
- ``markout = s*q*(mh - m0)`` (adverse selection over the horizon)
- ``inventory = s*q*(mT - mh)`` (holding the position to the close mark)
- ``fees = q * rebate_per_share`` (negative when the maker pays)

so ``spread + markout + inventory`` is exactly the mark-to-market of the
fill at ``mT`` and ``net = that + fees`` (tested against an independent
cash-plus-inventory computation).

**Incremental net maker P&L** (:func:`incremental`): filtered minus
baseline, decomposed into the four components AND by fill set. Fills are
matched by the displayed execution that produced them
(``(exec event_id, side)``): ``missed`` are baseline fills the filter
prevented (their P&L is what the filter gives up, priced, not dropped),
``extra`` are filtered-only fills, ``common`` are matched. Identity:
``d_net = net(extra) - net(missed) + d_net(common)``.

**Statistics** (:func:`summarise_sessions`): the per-session incremental
net USD is the unit (every session counts, a session with no fills is 0);
Student-t with G-1 df when G >= 3 (:mod:`iap.research.study_stats`); the
MDE at G and the small-G statement are in every output. The adverse
selection reduction ``1 - markout_filtered / markout_baseline`` is reported
as the hypothesis metric; the 25 % figure is the HYPOTHESIS, never the
decision criterion (the pre-declared criterion is on incremental net P&L).

Python reference, plain loops, no RNG: output is deterministic.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from iap.core.events import EventType, MarketEvent, Side
from iap.orderbook.book import OrderBook
from iap.research.study_stats import MIN_SESSIONS_FOR_CI, _t975, mde

BOOK_EVENTS = (
    EventType.ADD,
    EventType.MODIFY,
    EventType.CANCEL,
    EventType.EXECUTE,
    EventType.QUOTE,
    EventType.SNAPSHOT,
)
#: hypothesis 4 headline: the share of the adverse-selection line the flag is
#: hoped to remove. A hypothesis, NOT the decision criterion.
HYPOTHESIS_AS_REDUCTION = 0.25
COMPONENTS = ("spread", "fees", "markout", "inventory")
SMALL_G_STATEMENT = (
    "inference rests on G session clusters; for G <= 5 the result is a size statement, "
    "not a power statement, and no generalisation beyond these sessions is claimed"
)


@dataclass(frozen=True)
class StaleRule:
    """The stale-quote rule (all values set in the pre-declaration)."""

    min_age_ns: int
    min_displacement_ticks: float


@dataclass(frozen=True)
class QuoterConfig:
    """The reconstructed quoter. ``venue_id`` is the venue it rests on."""

    venue_id: int
    instrument_id: int
    tick_size: float
    lot: int = 100
    refresh_ns: int = 1_000_000_000
    cancel_latency_ns: int = 0
    markout_ns: int = 1_000_000_000
    rebate_per_share: float = 0.0
    max_inventory_lots: int = 5


@dataclass
class Fill:
    session: str
    ts: int
    exec_event_id: int
    side: int  # Side.BID = we bought, Side.ASK = we sold
    price: float
    qty: int
    spread: float = 0.0
    fees: float = 0.0
    markout: float = 0.0
    inventory: float = 0.0
    stale_at_fill: bool = False

    @property
    def net(self) -> float:
        return self.spread + self.fees + self.markout + self.inventory

    @property
    def key(self) -> tuple[int, int]:
        return (self.exec_event_id, self.side)


@dataclass
class PolicyResult:
    policy: str
    session: str
    fills: list[Fill] = field(default_factory=list)
    pulls: int = 0

    def totals(self) -> dict[str, float]:
        out = {c: math.fsum(getattr(f, c) for f in self.fills) for c in COMPONENTS}
        out["net"] = math.fsum(f.net for f in self.fills)
        out["fills"] = float(len(self.fills))
        out["qty"] = float(sum(f.qty for f in self.fills))
        return out


# --------------------------------------------------------------- books --


def _mid(book: OrderBook) -> float | None:
    b, a = book.best_bid(), book.best_ask()
    if b is None or a is None or b[0] >= a[0]:
        return None
    return 0.5 * (b[0] + a[0])


class StaleQuoteMonitor:
    """Causal per-venue books in RECEIVE order and the stale indicator.

    Call :meth:`on_event` with events in non-decreasing ``receive_ts``;
    :meth:`stale` and :meth:`state` then read only what has been received."""

    def __init__(self, instrument_id: int, venues: Sequence[int], rule: StaleRule) -> None:
        if len(venues) != 2:
            raise ValueError("the stale-quote indicator needs exactly two venues")
        self.iid = instrument_id
        self.venues = tuple(venues)
        self.rule = rule
        self.books = {v: OrderBook(instrument_id, v) for v in venues}
        self.now = -1
        # (venue, side) -> (receive time of last best-price change, other-venue mid then)
        self._anchor: dict[tuple[int, int], tuple[int, float | None]] = {}
        self._best: dict[tuple[int, int], int | None] = {}

    def other(self, venue: int) -> int:
        return self.venues[1] if venue == self.venues[0] else self.venues[0]

    def on_event(self, ev: MarketEvent) -> None:
        if ev.instrument_id != self.iid or ev.venue_id not in self.books:
            return
        if ev.receive_ts < self.now:
            raise ValueError("StaleQuoteMonitor needs events in receive-time order")
        self.now = ev.receive_ts
        book = self.books[ev.venue_id]
        book.apply(ev)  # every event: the book's sequence check sees no holes
        if ev.event_type not in BOOK_EVENTS:
            return
        other_mid = _mid(self.books[self.other(ev.venue_id)])
        for side, best in ((Side.BID, book.best_bid()), (Side.ASK, book.best_ask())):
            px = None if best is None else best[0]
            k = (ev.venue_id, int(side))
            if k not in self._best or self._best[k] != px:
                self._best[k] = px
                self._anchor[k] = (self.now, other_mid)
        # an anchor taken while the other book had no mid takes the first one
        # received afterwards (still causal: never a later-received mid than now)
        for k, (t_a, m_a) in self._anchor.items():
            if m_a is None:
                m = _mid(self.books[self.other(k[0])])
                if m is not None:
                    self._anchor[k] = (t_a, m)

    def state(self, venue: int, side: int, t: int) -> tuple[int | None, float | None]:
        """(quote age ns, displacement against the side in ticks) at ``t``."""
        a = self._anchor.get((venue, int(side)))
        if a is None:
            return None, None
        age = t - a[0]
        m_then = a[1]
        m_now = _mid(self.books[self.other(venue)])
        if m_then is None or m_now is None:
            return age, None
        d = m_now - m_then
        # bid threatened by a falling other mid, ask by a rising one
        against = -d if int(side) == Side.BID else d
        return age, against

    def stale(self, venue: int, side: int, t: int) -> bool:
        age, against = self.state(venue, side, t)
        if age is None or against is None:
            return False
        return age >= self.rule.min_age_ns and against >= self.rule.min_displacement_ticks


def receive_order(events: Iterable[MarketEvent]) -> list[MarketEvent]:
    """Events sorted by ``(receive_ts, event_id)`` (stable, deterministic)."""
    return sorted(events, key=lambda e: (e.receive_ts, e.event_id))


def consolidated_mid_path(
    events: Sequence[MarketEvent], instrument_id: int, venues: Sequence[int]
) -> tuple[list[int], list[float]]:
    """Exchange-clock consolidated mid (best bid across venues, best ask across
    venues), sampled after every book event; the evaluation timeline."""
    books = {v: OrderBook(instrument_id, v) for v in venues}
    ts: list[int] = []
    mids: list[float] = []
    for ev in sorted(events, key=lambda e: (e.exchange_ts, e.event_id)):
        if ev.instrument_id != instrument_id or ev.venue_id not in books:
            continue
        books[ev.venue_id].apply(ev)
        if ev.event_type not in BOOK_EVENTS:
            continue
        bids = [b[0] for b in (bk.best_bid() for bk in books.values()) if b is not None]
        asks = [a[0] for a in (bk.best_ask() for bk in books.values()) if a is not None]
        if not bids or not asks or max(bids) >= min(asks):
            continue
        m = 0.5 * (max(bids) + min(asks))
        if ts and ts[-1] == ev.exchange_ts:
            mids[-1] = m
        else:
            ts.append(ev.exchange_ts)
            mids.append(m)
    return ts, mids


def _mid_at(ts: Sequence[int], mids: Sequence[float], t: int) -> float | None:
    lo, hi = 0, len(ts)
    while lo < hi:
        mid = (lo + hi) // 2
        if ts[mid] <= t:
            lo = mid + 1
        else:
            hi = mid
    return None if lo == 0 else mids[lo - 1]


# ------------------------------------------------------------- quoter --


@dataclass
class _Quote:
    side: int
    price: int
    ahead: int
    qty: int
    live_from: int  # receive time from which it can fill (post)
    pull_at: int | None = None


def simulate_quoter(
    events: Sequence[MarketEvent],
    cfg: QuoterConfig,
    rule: StaleRule,
    *,
    session: str,
    filtered: bool,
    venues: Sequence[int],
) -> PolicyResult:
    """Run one policy on one session (module docstring: the reconstruction).

    ``events`` is the merged stream of the session; only ``cfg.instrument_id``
    is used. Decisions are on receive time; a displayed execution fills the
    quote when it is received after the quote went live."""
    res = PolicyResult(policy="filtered" if filtered else "baseline", session=session)
    mon = StaleQuoteMonitor(cfg.instrument_id, venues, rule)
    quotes: dict[int, _Quote | None] = {Side.BID: None, Side.ASK: None}
    inv = 0
    cap = cfg.max_inventory_lots * cfg.lot
    next_refresh: int | None = None
    pulled: dict[int, bool] = {Side.BID: False, Side.ASK: False}
    book = mon.books[cfg.venue_id]

    def post(side: int, t: int) -> None:
        best = book.best_bid() if side == Side.BID else book.best_ask()
        if best is None:
            quotes[side] = None
            return
        if (side == Side.BID and inv >= cap) or (side == Side.ASK and inv <= -cap):
            quotes[side] = None
            return
        px, lvl = best
        quotes[side] = _Quote(side=side, price=px, ahead=lvl, qty=cfg.lot, live_from=t)

    for ev in receive_order(events):
        if ev.instrument_id != cfg.instrument_id:
            continue
        t = ev.receive_ts
        # 1. our resting quote can be hit by a displayed execution on our venue
        if ev.event_type == EventType.EXECUTE and ev.venue_id == cfg.venue_id:
            o = book.order(ev.order_id)
            if o is not None:
                eside, epx = int(o[0]), int(o[1])
                q = quotes.get(eside)
                live = q is not None and q.live_from < t and (q.pull_at is None or t < q.pull_at)
                if q is not None and live and epx == q.price:
                    take = ev.qty
                    used = min(q.ahead, take)
                    q.ahead -= used
                    take -= used
                    fq = min(take, q.qty)
                    if fq > 0:
                        q.qty -= fq
                        inv += fq if eside == Side.BID else -fq
                        res.fills.append(
                            Fill(
                                session=session,
                                ts=ev.exchange_ts,
                                exec_event_id=ev.event_id,
                                side=eside,
                                price=epx * cfg.tick_size,
                                qty=fq,
                                stale_at_fill=mon.stale(cfg.venue_id, eside, t),
                            )
                        )
                        if q.qty == 0:
                            quotes[eside] = None
        mon.on_event(ev)
        # 2. pulls that have taken effect
        for side in (Side.BID, Side.ASK):
            q = quotes[side]
            if q is not None and q.pull_at is not None and t >= q.pull_at:
                quotes[side] = None
        # 3. refresh grid
        if next_refresh is None:
            next_refresh = (t // cfg.refresh_ns + 1) * cfg.refresh_ns
        refresh = t >= next_refresh
        if refresh:
            while next_refresh <= t:
                next_refresh += cfg.refresh_ns
        for side in (Side.BID, Side.ASK):
            flag = filtered and mon.stale(cfg.venue_id, side, t)
            q = quotes[side]
            if flag:
                if not pulled[side]:
                    res.pulls += 1
                pulled[side] = True
                if q is not None and q.pull_at is None:
                    q.pull_at = t + cfg.cancel_latency_ns
                    if cfg.cancel_latency_ns == 0:
                        quotes[side] = None
                continue
            was_pulled = pulled[side]
            pulled[side] = False
            if refresh or q is None or was_pulled:
                post(side, t)
    price_fills(res.fills, events, cfg, venues)
    return res


def price_fills(
    fills: list[Fill], events: Sequence[MarketEvent], cfg: QuoterConfig, venues: Sequence[int]
) -> None:
    """Fill in the four P&L components: spread against the quoting venue's own
    mid just before the fill, markout and inventory against the consolidated mid."""
    ts, mids = consolidated_mid_path(events, cfg.instrument_id, venues)
    ots, omids = consolidated_mid_path(events, cfg.instrument_id, (cfg.venue_id,))
    if not ts:
        return
    m_end = mids[-1] * cfg.tick_size
    for f in fills:
        m0 = _mid_at(ots, omids, f.ts - 1)  # the quoting venue's mid just before the fill
        mh = _mid_at(ts, mids, f.ts + cfg.markout_ns)
        m0 = f.price if m0 is None else m0 * cfg.tick_size
        mh = m0 if mh is None else mh * cfg.tick_size
        s = 1.0 if f.side == Side.BID else -1.0
        f.spread = s * f.qty * (m0 - f.price)
        f.markout = s * f.qty * (mh - m0)
        f.inventory = s * f.qty * (m_end - mh)
        f.fees = f.qty * cfg.rebate_per_share


def mark_to_market(fills: Sequence[Fill], end_mid: float, rebate_per_share: float) -> float:
    """Independent check: cash + inventory at ``end_mid`` + fees."""
    cash = 0.0
    inv = 0
    for f in fills:
        s = 1 if f.side == Side.BID else -1
        cash -= s * f.qty * f.price
        inv += s * f.qty
    return cash + inv * end_mid + rebate_per_share * sum(f.qty for f in fills)


# -------------------------------------------------------- incremental --


def _sum(fills: Iterable[Fill]) -> dict[str, float]:
    fl = list(fills)
    out = {c: math.fsum(getattr(f, c) for f in fl) for c in COMPONENTS}
    out["net"] = math.fsum(f.net for f in fl)
    out["fills"] = float(len(fl))
    out["qty"] = float(sum(f.qty for f in fl))
    return out


def incremental(baseline: PolicyResult, filtered: PolicyResult) -> dict[str, Any]:
    """Filtered minus baseline on one session, by component and by fill set."""
    if baseline.session != filtered.session:
        raise ValueError("incremental needs the same session for both policies")
    b = {f.key: f for f in baseline.fills}
    fz = {f.key: f for f in filtered.fills}
    missed = [b[k] for k in sorted(b) if k not in fz]
    extra = [fz[k] for k in sorted(fz) if k not in b]
    common_b = [b[k] for k in sorted(b) if k in fz]
    common_f = [fz[k] for k in sorted(fz) if k in b]
    tb, tf = _sum(baseline.fills), _sum(filtered.fills)
    delta = {k: tf[k] - tb[k] for k in (*COMPONENTS, "net", "fills", "qty")}
    cb, cf = _sum(common_b), _sum(common_f)
    return {
        "session": baseline.session,
        "baseline": tb,
        "filtered": tf,
        "delta": delta,
        "missed": _sum(missed),
        "extra": _sum(extra),
        "common_delta": {k: cf[k] - cb[k] for k in (*COMPONENTS, "net")},
        "pulls": filtered.pulls,
        "missed_fills_priced": True,
    }


def as_reduction(baseline_markout: float, filtered_markout: float) -> float | None:
    """``1 - filtered/baseline`` adverse-selection markout (None if baseline >= 0)."""
    if baseline_markout >= 0.0:
        return None
    return 1.0 - filtered_markout / baseline_markout


def summarise_sessions(
    rows: Sequence[Mapping[str, Any]], *, alpha: float = 0.05, power: float = 0.80
) -> dict[str, Any]:
    """Session-clustered summary of :func:`incremental` rows (module docs)."""
    rows = sorted(rows, key=lambda r: str(r["session"]))
    x = [float(r["delta"]["net"]) for r in rows]
    g = len(x)
    mean = math.fsum(x) / g if g else None
    sd = (
        math.sqrt(math.fsum((v - mean) ** 2 for v in x) / (g - 1))
        if g >= 2 and mean is not None
        else None
    )
    ci = None
    t_stat = None
    if g >= MIN_SESSIONS_FOR_CI and sd is not None and sd > 0 and mean is not None:
        se = sd / math.sqrt(g)
        half = _t975(g - 1) * se
        ci = (mean - half, mean + half)
        t_stat = mean / se
    base_mk = math.fsum(float(r["baseline"]["markout"]) for r in rows)
    filt_mk = math.fsum(float(r["filtered"]["markout"]) for r in rows)
    comp = {c: math.fsum(float(r["delta"][c]) for r in rows) for c in (*COMPONENTS, "net")}
    return {
        "unit": "per-session incremental net maker P&L, USD (filtered - baseline)",
        "G": g,
        "df": g - 1 if g else None,
        "mean_usd": mean,
        "sd_usd": sd,
        "ci95_usd": ci,
        "t": t_stat,
        "mde_usd": None if sd is None else mde(sd, g, alpha=alpha, power=power),
        "decomposition_total_usd": comp,
        "missed_net_usd": math.fsum(float(r["missed"]["net"]) for r in rows),
        "extra_net_usd": math.fsum(float(r["extra"]["net"]) for r in rows),
        "as_reduction": as_reduction(base_mk, filt_mk),
        "as_reduction_hypothesis": HYPOTHESIS_AS_REDUCTION,
        "as_reduction_is_criterion": False,
        "small_g": SMALL_G_STATEMENT,
    }


def run_session(
    events: Sequence[MarketEvent],
    cfg: QuoterConfig,
    rule: StaleRule,
    *,
    session: str,
    venues: Sequence[int],
) -> dict[str, Any]:
    """Paired baseline vs filtered on one session -> one :func:`incremental` row."""
    base = simulate_quoter(events, cfg, rule, session=session, filtered=False, venues=venues)
    filt = simulate_quoter(events, cfg, rule, session=session, filtered=True, venues=venues)
    row = incremental(base, filt)
    row["config"] = asdict(cfg)
    row["rule"] = asdict(rule)
    return row
