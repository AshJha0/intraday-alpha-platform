"""Per-venue execution model for the cost-aware router (v1.12 X5, Python only).

Opt-in companion to :mod:`iap.execution.sor_v2`. Nothing here changes the
pinned SOR (:mod:`iap.execution.sor`), the simulator fees or the fills
golden: the model only feeds router decisions and a separate fee ledger.

Per venue it holds:

- ``p_fill_touch`` — probability that an order joining the touch fills
  before its TTL. Source order: the calibration document
  (``fill_rates.by_venue[<vid>].p_any_fill``, then ``fill_rates.all``),
  then the venue-model config, then ``default_p_fill``.
- ``toxicity_bps`` — adverse selection of MAKER fills on the venue, keyed by
  markout horizon (``"100ms"``, ``"1s"``), in bps, positive = adverse
  (``-price_impact`` of :mod:`iap.tca.markout`). From config, or measured
  with :func:`toxicity_from_markouts` on a fill list.
- ``latency_ns`` — one-way order latency (the venue's ``latency_mean_ns``
  unless the config overrides it).
- ``tiers`` — a volume-tiered fee schedule (:class:`FeeTier` rows sorted by
  ``min_monthly_shares``). Without tiers the flat ``VenueSpec`` fee and
  rebate apply.

Tiered fees (:class:`FeeLedger`): a monthly volume accumulator per venue.
The tier of a fill is the one reached by the venue's volume in that UTC
calendar month BEFORE the fill (volume is credited after pricing), so the
same fill sequence always yields the same fees. Months roll over on the
UTC month of the fill timestamp; the accumulator never decreases inside a
month.

Config: ``research/execution/venue_model.json`` (``schema``
``"iap.venue_model"``, ``version`` 1), keyed by venue NAME as in
``venues.json``; ``venues.json`` itself is unchanged.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from iap.execution.types import VenueSpec

SCHEMA = "iap.venue_model"
VERSION = 1
DEFAULT_TOX_HORIZON = "1s"


@dataclass(frozen=True)
class FeeTier:
    """One tier: applies once the month's volume reaches ``min_monthly_shares``."""

    min_monthly_shares: int
    taker_fee_per_share: float
    maker_rebate_per_share: float

    def __post_init__(self) -> None:
        if self.min_monthly_shares < 0:
            raise ValueError("min_monthly_shares must be >= 0")


@dataclass(frozen=True)
class VenueModel:
    """Router-facing model of one venue (module docstring)."""

    spec: VenueSpec
    p_fill_touch: float = 0.5
    toxicity_bps: Mapping[str, float] = field(default_factory=dict)
    latency_ns: int | None = None
    tiers: tuple[FeeTier, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.p_fill_touch <= 1.0:
            raise ValueError(f"venue {self.spec.venue_id}: p_fill_touch must be in [0, 1]")
        mins = [t.min_monthly_shares for t in self.tiers]
        if mins and (mins != sorted(mins) or len(set(mins)) != len(mins) or mins[0] != 0):
            raise ValueError(
                f"venue {self.spec.venue_id}: tiers must start at 0 and strictly increase"
            )

    @property
    def venue_id(self) -> int:
        return self.spec.venue_id

    @property
    def one_way_latency_ns(self) -> int:
        return self.spec.latency_mean_ns if self.latency_ns is None else self.latency_ns

    def toxicity(self, horizon: str = DEFAULT_TOX_HORIZON) -> float:
        return float(self.toxicity_bps.get(horizon, 0.0))

    def tier_index(self, monthly_shares: int) -> int:
        idx = 0
        for i, t in enumerate(self.tiers):
            if monthly_shares >= t.min_monthly_shares:
                idx = i
        return idx

    def fees(self, monthly_shares: int = 0) -> tuple[float, float]:
        """(taker fee, maker rebate) per share at that month-to-date volume."""
        if not self.tiers:
            return self.spec.taker_fee_per_share, self.spec.maker_rebate_per_share
        t = self.tiers[self.tier_index(monthly_shares)]
        return t.taker_fee_per_share, t.maker_rebate_per_share


def month_key(ts_ns: int) -> int:
    """UTC ``YYYYMM`` of a nanosecond epoch timestamp."""
    d = _dt.datetime.fromtimestamp(ts_ns // 1_000_000_000, tz=_dt.UTC)
    return d.year * 100 + d.month


class FeeLedger:
    """Monthly volume accumulator -> tier -> per-share fee/rebate (deterministic)."""

    def __init__(self, models: Mapping[int, VenueModel]) -> None:
        self._models = dict(sorted(models.items()))
        self._volume: dict[tuple[int, int], int] = {}
        self.total_fees = 0.0

    def monthly_volume(self, venue_id: int, ts_ns: int) -> int:
        return self._volume.get((venue_id, month_key(ts_ns)), 0)

    def tier(self, venue_id: int, ts_ns: int) -> int:
        return self._models[venue_id].tier_index(self.monthly_volume(venue_id, ts_ns))

    def quote(self, venue_id: int, ts_ns: int) -> tuple[float, float]:
        """(taker fee, maker rebate) per share for the NEXT fill at ``ts_ns``."""
        return self._models[venue_id].fees(self.monthly_volume(venue_id, ts_ns))

    def record(self, venue_id: int, qty: int, ts_ns: int, maker: bool) -> float:
        """Price one fill at the current tier, then credit its volume.

        Returns the fee (negative = rebate received)."""
        if qty <= 0:
            raise ValueError("fill qty must be > 0")
        taker, rebate = self.quote(venue_id, ts_ns)
        fee = (-rebate if maker else taker) * qty
        key = (venue_id, month_key(ts_ns))
        self._volume[key] = self._volume.get(key, 0) + qty
        self.total_fees += fee
        return fee


def _fill_prob_from_calibration(calibration, venue_id: int) -> float | None:
    if calibration is None:
        return None
    fr = calibration.doc.get("fill_rates") or {}
    cell = (fr.get("by_venue") or {}).get(str(venue_id)) or {}
    p = cell.get("p_any_fill")
    if p is None:
        p = (fr.get("all") or {}).get("p_any_fill")
    return None if p is None else float(p)


def build_venue_models(
    venues: Mapping[int, VenueSpec],
    config: Mapping | None = None,
    *,
    calibration=None,
    toxicity: Mapping[int, Mapping[str, float]] | None = None,
    default_p_fill: float = 0.5,
) -> dict[int, VenueModel]:
    """One :class:`VenueModel` per venue (sources and precedence: module docstring).

    ``config`` is a loaded ``venue_model.json`` document (or None);
    ``toxicity`` (e.g. from :func:`toxicity_from_markouts`) overrides the
    config's toxicity per venue."""
    rows: Mapping = {}
    if config is not None:
        if config.get("schema") != SCHEMA or config.get("version") != VERSION:
            raise ValueError(f"not an {SCHEMA} v{VERSION} document")
        rows = config.get("venues") or {}
    out: dict[int, VenueModel] = {}
    for vid, spec in sorted(venues.items()):
        row = rows.get(spec.name) or {}
        p = _fill_prob_from_calibration(calibration, vid)
        if p is None:
            p = float(row.get("p_fill_touch", default_p_fill))
        tox = dict(row.get("toxicity_bps") or {})
        if toxicity is not None and vid in toxicity:
            tox.update(toxicity[vid])
        tiers = tuple(
            FeeTier(
                int(t["min_monthly_shares"]),
                float(t["taker_fee_per_share"]),
                float(t["maker_rebate_per_share"]),
            )
            for t in (row.get("fee_tiers") or ())
        )
        lat = row.get("latency_ns")
        out[vid] = VenueModel(
            spec=spec,
            p_fill_touch=p,
            toxicity_bps={k: float(v) for k, v in sorted(tox.items())},
            latency_ns=None if lat is None else int(lat),
            tiers=tiers,
        )
    return out


def load_venue_model_config(path: str | Path) -> dict:
    """Read and version-check a ``venue_model.json`` document."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA or doc.get("version") != VERSION:
        raise ValueError(f"{path}: not an {SCHEMA} v{VERSION} document")
    return doc


def toxicity_from_markouts(
    fills: Sequence,
    timeline,
    horizons: Sequence[str] = ("100ms", "1s"),
    min_fills: int = 2,
) -> dict[int, dict[str, float]]:
    """Per-venue MAKER adverse selection (bps, positive = adverse) at the
    given :mod:`iap.tca.markout` horizon names; cells below ``min_fills``
    are omitted (undefined, never zero)."""
    from iap.tca.markout import DEFAULT_HORIZONS_NS, MAKER, markout_report

    hz = {h: DEFAULT_HORIZONS_NS[h] for h in horizons}
    maker = [f for f in fills if f.liquidity == MAKER]
    if len(maker) < min_fills:
        return {}
    rep = markout_report(maker, timeline, horizons=hz, min_fills=min_fills)
    out: dict[int, dict[str, float]] = {}
    for vid, cell in rep["by_venue"].items():  # type: ignore[union-attr]
        row = {}
        for h in hz:
            pi = cell["horizons"][h]["price_impact_bps"]
            if pi is not None:
                row[h] = -float(pi)
        if row:
            out[int(vid)] = row
    return out
