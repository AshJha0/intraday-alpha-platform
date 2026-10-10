"""Cost-aware venue router (v1.12 X5, Python only, opt-in).

The pinned router (:mod:`iap.execution.sor`, C++ ``sor.hpp`` hot path,
fills golden) decides on displayed best price with fee/rebate tie-breaks.
This module adds a per-venue model (:mod:`iap.execution.venues_model`) and
three policies; it never changes the pinned router, and nothing uses it
unless a caller passes a :class:`CostAwareRouter` (e.g.
``MakerBacktester(..., router=...)``).

Eligibility is the pinned one (``SmartOrderRouter._eligible``: book
present, not stale, TRADING, latency within ``max_venue_latency_ns``).
Iteration is in ascending venue_id; every tie breaks to the lower venue_id,
so the same books + model + ledger state give the same decision.

Expected all-in costs, in bps of the consolidated mid, positive = cost
(``s`` = +1 buy / -1 sell, fees from the ledger's current tier):

- **aggressive at v**: ``s * (touch_v - mid) / mid + taker_fee_v / mid +
  latency_penalty_bps_per_ms * latency_v``.
- **passive at v** (join our touch)::

      p_v * (-rebate_v/mid - s*(mid - touch_v)/mid + toxicity_v)
        + (1 - p_v) * miss_cost_bps

  i.e. fees - rebate + toxicity - fill-probability-weighted spread capture,
  with ``miss_cost_bps`` (default: cross later, half spread + mean taker
  fee) charged when the post does not fill.

Policies:

(a) :meth:`CostAwareRouter.route_aggressive` / :meth:`route_passive` —
    argmin of the cost above (single venue); ``NO_ROUTE`` when no eligible
    venue quotes the needed side.
(b) :meth:`CostAwareRouter.plan_sweep` — a marketable order across the
    displayed depth of every eligible venue, best price first (ties: lower
    all-in price incl. taker fee, then lower venue_id), up to ``qty`` and
    the limit. With ``stagger="sync_arrival"`` each child's send offset is
    ``max_latency - latency_v`` so all children arrive together (the slow
    venue is sent first; a fast venue cannot leak the sweep). With
    ``stagger="none"`` every offset is 0.
(c) :meth:`CostAwareRouter.allocate_passive` — split a passive quantity
    across eligible venues quoting our side, weight ``p_v / (1 +
    max(toxicity_v, 0) / tox_scale_bps)``; venues with toxicity above
    ``max_toxicity_bps`` get nothing. Integer split by largest remainder
    (ties to the lower venue_id); round lots via ``lot``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from iap.execution.config import SorOptions
from iap.execution.sor import NO_ROUTE, SmartOrderRouter
from iap.execution.venues_model import DEFAULT_TOX_HORIZON, FeeLedger, VenueModel
from iap.orderbook.book import ConsolidatedBook

STAGGER_MODES = ("sync_arrival", "none")


@dataclass(frozen=True)
class RouterOptions:
    """Knobs of the cost-aware router (module docstring)."""

    tox_horizon: str = DEFAULT_TOX_HORIZON
    latency_penalty_bps_per_ms: float = 0.0
    #: cost of a passive post that does not fill; None = half spread + taker fee
    miss_cost_bps: float | None = None
    stagger: str = "sync_arrival"
    sweep_levels: int = 10
    max_toxicity_bps: float = float("inf")
    tox_scale_bps: float = 1.0
    lot: int = 1

    def __post_init__(self) -> None:
        if self.stagger not in STAGGER_MODES:
            raise ValueError(f"stagger must be one of {STAGGER_MODES}")
        if self.sweep_levels <= 0 or self.lot <= 0 or self.tox_scale_bps <= 0:
            raise ValueError("sweep_levels, lot and tox_scale_bps must be > 0")


@dataclass(frozen=True)
class SweepChild:
    """One child of a sweep: send at ``decision_ts + send_offset_ns``."""

    venue_id: int
    price_ticks: int
    qty: int
    send_offset_ns: int
    arrival_offset_ns: int


class CostAwareRouter:
    """Per-venue-model router (module docstring)."""

    def __init__(
        self,
        models: Mapping[int, VenueModel],
        options: RouterOptions | None = None,
        sor_options: SorOptions | None = None,
        ledger: FeeLedger | None = None,
    ) -> None:
        self.models = dict(sorted(models.items()))
        self.options = options or RouterOptions()
        self.ledger = ledger if ledger is not None else FeeLedger(self.models)
        self._gate = SmartOrderRouter(
            {v: m.spec for v, m in self.models.items()}, sor_options or SorOptions()
        )

    # ------------------------------------------------------------ helpers
    def _candidates(self, candidates: Sequence[int] | None) -> list[int]:
        c = sorted(self.models) if candidates is None else sorted(candidates)
        if not c:
            raise ValueError("router: empty candidate venue list")
        return c

    def _touches(self, book: ConsolidatedBook, side_of_book: int, candidates):
        out = []
        for vid in self._candidates(candidates):
            if vid not in self.models:
                continue
            vb = self._gate._eligible(book, vid)
            if vb is None:
                continue
            q = vb.best_bid() if side_of_book == 0 else vb.best_ask()
            if q is not None:
                out.append((vid, vb, q))
        return out

    @staticmethod
    def _mid(book: ConsolidatedBook) -> float | None:
        bb, ba = book.best_bid(), book.best_ask()
        if bb is None or ba is None:
            return None
        return 0.5 * (bb[0] + ba[0])

    def _fee_bps(self, vid: int, ts: int, mid_ticks: float, tick: float) -> tuple[float, float]:
        taker, rebate = self.ledger.quote(vid, ts)
        px = mid_ticks * tick
        return 1e4 * taker / px, 1e4 * rebate / px

    # ------------------------------------------------------------ costs
    def aggressive_costs(
        self, book, side: int, ts: int, tick: float, candidates=None
    ) -> dict[int, float]:
        mid = self._mid(book)
        if mid is None:
            return {}
        s = 1.0 if side == 0 else -1.0
        o = self.options
        out = {}
        for vid, _, (px, _) in self._touches(book, 1 - side, candidates):
            taker_bps, _ = self._fee_bps(vid, ts, mid, tick)
            lat_ms = self.models[vid].one_way_latency_ns / 1e6
            out[vid] = (
                1e4 * s * (px - mid) / mid + taker_bps + o.latency_penalty_bps_per_ms * lat_ms
            )
        return out

    def passive_costs(
        self, book, side: int, ts: int, tick: float, candidates=None
    ) -> dict[int, float]:
        mid = self._mid(book)
        if mid is None:
            return {}
        s = 1.0 if side == 0 else -1.0
        o = self.options
        touches = self._touches(book, side, candidates)
        if o.miss_cost_bps is None:
            fees = [self._fee_bps(v, ts, mid, tick)[0] for v, _, _ in touches]
            half = 1e4 * 0.5 * (book.best_ask()[0] - book.best_bid()[0]) / mid
            miss = half + (sum(fees) / len(fees) if fees else 0.0)
        else:
            miss = o.miss_cost_bps
        out = {}
        for vid, _, (px, _) in touches:
            m = self.models[vid]
            _, rebate_bps = self._fee_bps(vid, ts, mid, tick)
            capture = 1e4 * s * (mid - px) / mid
            fill_cost = -rebate_bps - capture + m.toxicity(o.tox_horizon)
            out[vid] = m.p_fill_touch * fill_cost + (1.0 - m.p_fill_touch) * miss
        return out

    @staticmethod
    def _argmin(costs: Mapping[int, float]) -> int:
        if not costs:
            return NO_ROUTE
        return min(sorted(costs), key=lambda v: costs[v])  # stable: ties -> lower id

    # ------------------------------------------------------------ (a) single venue
    def route_aggressive(
        self, book: ConsolidatedBook, side: int, ts: int, tick: float, candidates=None
    ) -> int:
        return self._argmin(self.aggressive_costs(book, side, ts, tick, candidates))

    def route_passive(
        self, book: ConsolidatedBook, side: int, ts: int, tick: float, candidates=None
    ) -> int:
        return self._argmin(self.passive_costs(book, side, ts, tick, candidates))

    # ------------------------------------------------------------ (b) sweep
    def plan_sweep(
        self,
        book: ConsolidatedBook,
        side: int,
        qty: int,
        ts: int,
        *,
        limit_ticks: int | None = None,
        candidates=None,
    ) -> list[SweepChild]:
        """Children across displayed depth, best price first (module docstring)."""
        if qty <= 0:
            raise ValueError("sweep qty must be > 0")
        levels = []
        for vid, vb, _ in self._touches(book, 1 - side, candidates):
            taker, _ = self.ledger.quote(vid, ts)
            for px, q in vb.depth(1 - side, self.options.sweep_levels):
                if limit_ticks is not None and (
                    px > limit_ticks if side == 0 else px < limit_ticks
                ):
                    break
                key_px = px if side == 0 else -px
                levels.append((key_px, taker, vid, px, q))
        levels.sort(key=lambda r: (r[0], r[1], r[2]))
        take: dict[tuple[int, int], int] = {}
        left = qty
        for _, _, vid, px, q in levels:
            if left <= 0:
                break
            n = min(q, left)
            take[(vid, px)] = take.get((vid, px), 0) + n
            left -= n
        used = sorted({vid for vid, _ in take})
        lat = {v: self.models[v].one_way_latency_ns for v in used}
        max_lat = max(lat.values(), default=0)
        children = []
        for _, _, vid, px, _ in levels:
            n = take.pop((vid, px), 0)
            if n == 0:
                continue
            send = max_lat - lat[vid] if self.options.stagger == "sync_arrival" else 0
            children.append(SweepChild(vid, px, n, send, send + lat[vid]))
        return children

    # ------------------------------------------------------------ (c) passive split
    def allocate_passive(
        self, book: ConsolidatedBook, side: int, qty: int, candidates=None
    ) -> dict[int, int]:
        """Integer split of ``qty`` across venues quoting our side (module docstring)."""
        if qty <= 0:
            raise ValueError("allocation qty must be > 0")
        o = self.options
        w: dict[int, float] = {}
        for vid, _, _ in self._touches(book, side, candidates):
            m = self.models[vid]
            tox = m.toxicity(o.tox_horizon)
            if tox > o.max_toxicity_bps:
                continue
            wt = m.p_fill_touch / (1.0 + max(tox, 0.0) / o.tox_scale_bps)
            if wt > 0.0:
                w[vid] = wt
        if not w:
            return {}
        lots = qty // o.lot
        total = sum(w.values())
        raw = {v: lots * w[v] / total for v in w}
        alloc = {v: int(raw[v]) for v in w}
        rem = lots - sum(alloc.values())
        for v in sorted(w, key=lambda v: (-(raw[v] - alloc[v]), v))[:rem]:
            alloc[v] += 1
        return {v: a * o.lot for v, a in sorted(alloc.items()) if a > 0}
