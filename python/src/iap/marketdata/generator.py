"""Seeded synthetic market-data generator (equities MBO + FX quotes).

Design (all randomness is SplitMix64 — conventions section 3; identical seed =>
bit-identical output files):

- Equities (SYN.EQ.001..010 + SYN.ETF.IDX, venues XV1/XV2): full MBO streams.
  * Efficient price: ONE shared per-INSTRUMENT price process (2-state
    regime-switching volatility, Markov switching per 1s step), precomputed
    per session on a 1-second grid from a dedicated per-instrument SplitMix64
    stream ("eqmid"). Both venues of an instrument quote around the SAME
    efficient price, so the consolidated multi-venue book is essentially
    never crossed (target < 2% of event states; venue-independent mids in an
    earlier design left it crossed ~95% of the time — a research-validity
    bug, fixed and pinned here).
  * Per-venue microstructure: each venue stream adds a small mean-reverting
    AR(1) noise to the efficient price (sigma/rho/bound from config, bounded
    well below half the typical spread) and its own latency on receive_ts.
  * Order flow: clustered via a self-exciting intensity (Hawkes-style):
    executions kick excitation up, exponential decay per slot; inter-arrival
    times are exponential in the current intensity.
  * Queue dynamics: the generator maintains a real internal ``OrderBook`` per
    stream, so ADD/MODIFY/CANCEL/EXECUTE reference live FIFO state — EXECUTE
    always fills the FIFO head of the best opposite level, occasional
    aggressive ADDs cross the book (marketable), far orders are pruned with
    real CANCELs.
  * Session structure: open auction (STATUS AUCTION + auction TRADE prints +
    STATUS TRADING), one intraday HALT for a configured instrument/session
    (optionally followed by a re-opening auction: STATUS AUCTION, crossing
    ADDs that rest during the call, EXECUTEs that uncross them, auction
    TRADE prints, STATUS TRADING — ``halt.reopen_auction``, off by default so
    the pinned dataset is unchanged), close auction (STATUS AUCTION + prints
    + STATUS CLOSE). Multi-day capable; books and sequences persist across
    sessions.
  * Flow calibration (``equities.flow.calibration``, since v1.4.0).  The
    self-exciting multiplier ``(1 + excitation)`` shortens every
    inter-arrival time, so a base rate of ``slots / duration`` would spend
    ``slots_per_stream`` slots long before the close.  With the default,
    ``"session"``, the base rate is
    ``slots_per_stream * excitation_time_factor / duration`` where
    ``excitation_time_factor`` = E[1 / (1 + excitation)] over the slot chain
    (:func:`excitation_time_factor`, a pinned SplitMix64 estimate that
    depends only on ``flow`` and ``mix``), and there is no slot budget: flow
    runs until the close, so ``slots_per_stream`` is the EXPECTED number of
    flow slots per stream and session, spread over the whole session.
    ``"legacy_budget"`` is the v1.3.0 rule, kept so the v1.3.0 dataset
    (``data_version`` ``203c8f54...``) and the fixed golden vectors stay
    reproducible byte for byte: ``slots_per_stream`` is a hard budget and
    the base rate is ``slots / duration * 1.30``, uncalibrated for the
    excitation, so the last continuous event of a stream falls 38-43% into
    the session and nothing follows until the close auction.  Under
    ``"legacy_budget"`` only, ``equities.fill_session: true`` (the v1.3.0
    opt-in) removes the budget and keeps that uncalibrated rate (about 2.5
    times the events).  The golden-vector builders below and in
    ``golden_anomalies`` select ``"legacy_budget"`` explicitly: they cut a
    fixed number of events from the start of a stream and are test inputs,
    not the dataset.
- FX (8 G10 pairs, venues LP1/LP2/PRI): QUOTE (full L1 side replace) + TRADE
  streams; one shared regime-switching mid per pair, venue-specific spreads
  and venue-specific latency in receive_ts.
- QC anomalies (disabled for golden vectors): sequence gaps (with full
  SNAPSHOT recovery bursts so books can recover), exact duplicates, out-of-
  order arrivals (latency spikes), invalid events, receive_ts < exchange_ts
  violations. The generator reports exactly what it injected so the QC
  pipeline can be tested against ground truth.

- Planted effects (``planted``, ALL OFF by default — the pinned dataset and
  the golden vectors are byte-identical with the block absent or at its
  defaults; opt-in configs live outside ``configs/``, see
  ``research/power/generator_planted.json``).  They exist so the validation
  chain can be scored against a KNOWN truth (``iap.research.power``):
  * ``order_flow`` — informed aggressor flow with a price-impact kernel.
    The efficient path is precomputed, so its future increments are known
    to the generator; with ``strength`` s > 0 the aggressor of an EXECUTE is
    a buyer with probability ``0.5 + 0.5 * s * tanh(g / scale)`` where
    ``g = sum_{j=0..K-1} decay^j * (path[k+1+j] - path[k+j])`` is the
    kernel-weighted efficient-price move over the NEXT ``kernel_steps``
    seconds and ``scale`` its low-vol-regime standard deviation.  Trade
    sign therefore leads the mid along an exponential kernel, exactly the
    footprint of permanent impact, on every venue consistently (the shared
    efficient price is untouched, so the consolidated book stays
    uncrossed).  The draw that picked the aggressor at 0.5 is the same draw:
    no extra random number is consumed, enabled or not.
  * ``lead_lag`` — the ``leader`` instrument's efficient-price move over
    grid step ``k - lag_steps``, IN TICKS, is added, times ``beta``, to
    every other equity's efficient-price move at step ``k``.  Ticks, not
    returns: every instrument's own innovation has the same tick volatility
    (``vol_regimes.sigma_ticks_per_s``), so ``beta`` is the planted move per
    unit of the follower's own noise and the lagged correlation it creates
    is ``beta / sqrt(1 + beta^2)`` whatever the price levels are.  The
    leader's path comes from its own dedicated RNG stream, so computing it
    first changes no draw.
  * ``break`` — a mid-sample parameter break: from ``at_fraction`` of the
    run onward (sessions are equal slices of [0, 1)) both planted
    strengths are multiplied by ``post_multiplier`` (0 = the effect dies,
    -1 = it reverses).

Raw files are written in *arrival order* (sorted by receive_ts) with
``event_id`` assigned 1..N per file.
"""

from __future__ import annotations

import itertools
import json
import math
from pathlib import Path

from iap.core.codec import write_jsonl
from iap.core.events import SYNTHETIC_ID_BASE, EventType, MarketEvent, SessionStatus, Side
from iap.core.rng import SplitMix64
from iap.orderbook.book import OrderBook
from iap.reference.refdata import Instrument, ReferenceData, Venue

NS = 1_000_000_000

#: Golden-vector seeds (pinned; tests/golden depends on them).
GOLDEN_EQ_SEED = 4242424242
GOLDEN_FX_SEED = 8484848484

_DEFAULT_CONFIG = {
    "seed": 20260829,
    "sessions": 2,
    "equities": {
        "slots_per_stream": 3850,
        "vol_regimes": {"sigma_ticks_per_s": [0.15, 0.4], "switch_prob_per_s": 0.0007},
        "venue_noise": {"rho": 0.9, "sigma_ticks": 0.12, "max_ticks": 0.45},
        "flow": {
            "excitation_kick": 1.4,
            "excitation_decay": 0.82,
            "max_excitation": 8.0,
            "calibration": "session",
        },
        "book": {"max_resting_orders": 160, "qty_lots_max": 10},
        "mix": {"add": 0.46, "cancel": 0.26, "modify": 0.12, "execute": 0.16},
        "halt": {
            "instrument": "SYN.EQ.007",
            "session_index": 0,
            "duration_s": 300,
            "reopen_auction": False,
            "reopen_call_s": 60,
        },
        "auction_prints": 3,
        "fill_session": False,
    },
    "fx": {
        "slots_per_pair": 3300,
        "vol_regimes": {"sigma_ticks": [0.8, 3.0], "switch_prob": 0.003},
        "spread_ticks": {"PRI": 2, "LP1": 3, "LP2": 5},
        "trade_prob": 0.12,
        "qty_units_max": 20,
    },
    "anomalies": {
        "gap_prob": 0.0006,
        "gap_max_events": 3,
        "dup_prob": 0.0015,
        "ooo_prob": 0.001,
        "invalid_prob": 0.0005,
        "ts_violation_prob": 0.0005,
    },
    # Planted effects of known size (module docs).  Off: strength / beta 0
    # and no break.  With these defaults no code path below differs from the
    # generator without the block, and no RNG draw is added or reordered.
    "planted": {
        "order_flow": {"strength": 0.0, "kernel_decay": 0.7, "kernel_steps": 5},
        "lead_lag": {"beta": 0.0, "leader": "SYN.ETF.IDX", "lag_steps": 1},
        "break": {"at_fraction": None, "post_multiplier": 1.0},
    },
}


#: ``x-version`` of a generator config document (``load_generator_config``):
#: 2 since v1.4.0, when the default equity flow calibration became "session".
GENERATOR_CONFIG_X_VERSION = 2

#: ``equities.flow.calibration`` values (module docs).
FLOW_CALIBRATION_SESSION = "session"
FLOW_CALIBRATION_LEGACY = "legacy_budget"
FLOW_CALIBRATIONS = (FLOW_CALIBRATION_SESSION, FLOW_CALIBRATION_LEGACY)

#: Pinned estimator of :func:`excitation_time_factor`: one SplitMix64 stream
#: with this seed, this many slots.  Independent of the dataset seed, so the
#: factor is a pure function of the flow parameters.
_CALIBRATION_SEED = 0x1A9CA11B
_CALIBRATION_SLOTS = 1 << 17

_TIME_FACTOR_CACHE: dict[tuple[float, float, float, float], float] = {}


def excitation_time_factor(
    excitation_kick: float, excitation_decay: float, max_excitation: float, execute_prob: float
) -> float:
    """E[1 / (1 + excitation)] over the equity slot chain.

    A flow slot waits ``Exp(1) / (lambda0 * (1 + excitation))``; each slot
    decays the excitation by ``excitation_decay`` and, with probability
    ``execute_prob`` (the aggression share of the mix), kicks it up by
    ``excitation_kick`` capped at ``max_excitation``.  The mean wait of a
    slot is therefore ``factor / lambda0``, and ``lambda0 = slots * factor /
    duration`` spreads ``slots`` expected slots over ``duration``.

    Estimated on a pinned SplitMix64 stream (``_CALIBRATION_SEED``,
    ``_CALIBRATION_SLOTS`` slots): deterministic, the same in every run, and
    cached per parameter set.
    """
    key = (
        float(excitation_kick),
        float(excitation_decay),
        float(max_excitation),
        float(execute_prob),
    )
    cached = _TIME_FACTOR_CACHE.get(key)
    if cached is not None:
        return cached
    kick, decay, cap, prob = key
    rng = SplitMix64(_CALIBRATION_SEED)
    excitation = 0.0
    total = 0.0
    for _ in range(_CALIBRATION_SLOTS):
        total += 1.0 / (1.0 + excitation)
        excitation *= decay
        if rng.uniform() < prob:
            excitation = min(excitation + kick, cap)
    factor = total / _CALIBRATION_SLOTS
    _TIME_FACTOR_CACHE[key] = factor
    return factor


def _merge_config(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_config(out[k], v)
        else:
            out[k] = v
    return out


def load_generator_config(path=None) -> dict:
    """Load configs/marketdata/generator.json merged over built-in defaults.

    ``x-version`` 2 (v1.4.0) is the document whose default equity flow
    calibration is ``"session"``.  An ``x-version`` 1 document was written
    against the v1.3.0 rule: loading it under the new default would produce
    a different dataset without anyone asking for it, so it is rejected
    unless it names ``equities.flow.calibration`` itself (``"legacy_budget"``
    reproduces the v1.3.0 data).  A document without ``x-version`` takes the
    current defaults.
    """
    cfg = dict(_DEFAULT_CONFIG)
    if path is not None:
        with open(path, encoding="utf-8") as f:
            file_cfg = json.load(f)
        version = file_cfg.pop("x-version", None)
        file_cfg.pop("description", None)
        if version is not None and version not in (1, GENERATOR_CONFIG_X_VERSION):
            raise ValueError(
                f"{path}: unsupported generator config x-version {version!r} "
                f"(this build reads 1 and {GENERATOR_CONFIG_X_VERSION})"
            )
        explicit = file_cfg.get("equities", {}).get("flow", {}).get("calibration")
        if version == 1 and explicit is None:
            raise ValueError(
                f"{path}: x-version 1 generator config without equities.flow.calibration — "
                'since v1.4.0 the default is "session" (flow to the close), which is not the '
                "dataset this document was written for. Set equities.flow.calibration to "
                '"legacy_budget" to reproduce the v1.3.0 data, or to "session" and bump '
                f"x-version to {GENERATOR_CONFIG_X_VERSION}."
            )
        cfg = _merge_config(cfg, file_cfg)
    return cfg


class _EffPrice:
    """Shared per-INSTRUMENT efficient price (regime-switching vol, pinned).

    The path is precomputed per session on a fixed 1-second grid from a
    dedicated per-instrument SplitMix64 stream, so every venue of the
    instrument reads the SAME efficient price at any event time regardless
    of how venue streams interleave their own draws.  Level and regime
    persist across sessions.
    """

    __slots__ = (
        "inst",
        "rng",
        "mid_f",
        "regime",
        "open_ns",
        "path",
        "flow_strength",
        "flow_weights",
        "flow_scale",
    )

    STEP_NS = 1_000_000_000  # 1-second efficient-price grid

    def __init__(self, inst: Instrument, rng: SplitMix64) -> None:
        self.inst = inst
        self.rng = rng
        self.mid_f = float(inst.ref_price_ticks)
        self.regime = 0
        self.open_ns = 0
        self.path: list[float] = [self.mid_f]
        # Planted informed flow (None = off): per-step strength, kernel
        # weights and the scale of the kernel-weighted move.
        self.flow_strength: list[float] | None = None
        self.flow_weights: list[float] = []
        self.flow_scale = 1.0

    @classmethod
    def session_steps(cls, open_ns: int, close_ns: int) -> int:
        """Grid steps ``new_session`` precomputes for this window."""
        return int((close_ns - open_ns) // cls.STEP_NS) + 2

    def new_session(
        self, open_ns: int, close_ns: int, vol_cfg: dict, drift_ticks: list[float] | None = None
    ) -> None:
        """Precompute this session's path, continuing from the prior level.

        ``drift_ticks`` (planted lead-lag; ``None`` = off) is one extra move
        in ticks per grid step, applied after the step's own innovation.
        """
        steps = self.session_steps(open_ns, close_ns)
        sigma = vol_cfg["sigma_ticks_per_s"]
        switch = vol_cfg["switch_prob_per_s"]
        lo = 0.5 * self.inst.ref_price_ticks
        hi = 2.0 * self.inst.ref_price_ticks
        rng = self.rng
        mid = self.mid_f
        regime = self.regime
        path = [mid]
        for _ in range(steps):
            if rng.uniform() < switch:
                regime ^= 1
            mid += rng.normal() * sigma[regime]
            if drift_ticks is not None:
                mid += drift_ticks[len(path) - 1]
            mid = min(max(mid, lo), hi)
            path.append(mid)
        self.open_ns = open_ns
        self.path = path
        self.mid_f = path[-1]  # carried into the next session
        self.regime = regime

    def step_moves(self) -> list[float]:
        """Per-step efficient-price moves (ticks) of the current session."""
        path = self.path
        return [path[k + 1] - path[k] for k in range(len(path) - 1)]

    def set_informed_flow(
        self, strength: list[float] | None, decay: float, kernel_steps: int, sigma_ticks: float
    ) -> None:
        """Arm (or, with ``None``, disarm) planted informed flow for the
        current session: ``strength[k]`` applies to trades in grid step k."""
        self.flow_strength = strength
        self.flow_weights = [decay**j for j in range(kernel_steps)]
        self.flow_scale = sigma_ticks * math.sqrt(sum(w * w for w in self.flow_weights))

    def buy_probability(self, ts: int) -> float:
        """P(aggressor is the buyer) for an execution at ``ts``: exactly 0.5
        unless informed flow is armed (module docs, ``order_flow``)."""
        if self.flow_strength is None:
            return 0.5
        path = self.path
        last = len(path) - 1
        k = min(max((ts - self.open_ns) // self.STEP_NS, 0), last)
        strength = self.flow_strength[min(k, len(self.flow_strength) - 1)]
        if strength == 0.0:
            return 0.5
        move = 0.0
        for j, w in enumerate(self.flow_weights):
            if k + 1 + j > last:
                break
            move += w * (path[k + 1 + j] - path[k + j])
        return 0.5 + 0.5 * strength * math.tanh(move / self.flow_scale)

    def mid_at(self, ts: int) -> float:
        """Efficient price at event time ts (grid floor; clamped to session)."""
        k = (ts - self.open_ns) // self.STEP_NS
        if k < 0:
            k = 0
        last = len(self.path) - 1
        if k > last:
            k = last
        return self.path[int(k)]


class _Stream:
    """Per venue+instrument stream state (sequence, latency, internal book)."""

    __slots__ = (
        "inst",
        "venue",
        "book",
        "seq",
        "last_receive",
        "next_order",
        "next_trade",
        "mid_f",
        "regime",
        "noise",
        "excitation",
    )

    def __init__(self, inst: Instrument, venue: Venue) -> None:
        self.inst = inst
        self.venue = venue
        self.book = OrderBook(inst.instrument_id, venue.venue_id)
        self.seq = 0
        self.last_receive = 0
        base = (venue.venue_id << 56) | (inst.instrument_id << 40)
        self.next_order = base + 1
        self.next_trade = base + 1
        # mid_f/regime: FX pair-level state (equities read the shared
        # _EffPrice instead; for them mid_f caches the venue-effective mid).
        self.mid_f = float(inst.ref_price_ticks)
        self.regime = 0
        self.noise = 0.0  # equities: venue-specific AR(1) microstructure
        self.excitation = 0.0

    def new_order_id(self) -> int:
        oid = self.next_order
        self.next_order += 1
        return oid

    def new_trade_id(self) -> int:
        tid = self.next_trade
        self.next_trade += 1
        return tid


class MarketDataGenerator:
    """Deterministic synthetic generator over the configured universe."""

    def __init__(self, refdata: ReferenceData, config: dict | None = None) -> None:
        self.ref = refdata
        self.cfg = _merge_config(_DEFAULT_CONFIG, config or {})
        self.seed: int = self.cfg["seed"]
        self.injected: dict[str, int] = {
            "gaps": 0,
            "gap_missing_events": 0,
            "duplicates": 0,
            "out_of_order": 0,
            "invalid": 0,
            "ts_violations": 0,
        }
        # Persistent stream state across sessions.
        self._eq_streams: dict[tuple[int, int], _Stream] = {}
        self._fx_streams: dict[tuple[int, int], _Stream] = {}
        self._eq_prices: dict[int, _EffPrice] = {}  # per-instrument shared mid
        self._rngs: dict[tuple[str, int, int], SplitMix64] = {}
        self._check_flow()
        self._check_planted()

    # ------------------------------------------------------------------ flow

    def _check_flow(self) -> None:
        """Validate the equity flow calibration (module docs); fail fast."""
        eq = self.cfg["equities"]
        flow, mix = eq["flow"], eq["mix"]
        calibration = flow["calibration"]
        if calibration not in FLOW_CALIBRATIONS:
            raise ValueError(
                f"equities.flow.calibration must be one of {list(FLOW_CALIBRATIONS)}, "
                f"got {calibration!r}"
            )
        if calibration == FLOW_CALIBRATION_SESSION and eq["fill_session"]:
            raise ValueError(
                "equities.fill_session is the v1.3.0 opt-in of the legacy flow rule: set "
                'equities.flow.calibration to "legacy_budget" with it, or drop it '
                '(the default calibration "session" always carries flow to the close)'
            )
        if not 0.0 < float(flow["excitation_decay"]) < 1.0:
            raise ValueError("equities.flow.excitation_decay must be in (0, 1)")
        if float(flow["excitation_kick"]) < 0.0 or float(flow["max_excitation"]) < 0.0:
            raise ValueError("equities.flow.excitation_kick / max_excitation must be >= 0")
        passive = float(mix["add"]) + float(mix["cancel"]) + float(mix["modify"])
        if not 0.0 <= passive <= 1.0:
            raise ValueError("equities.mix add + cancel + modify must be in [0, 1]")

    def eq_time_factor(self) -> float:
        """:func:`excitation_time_factor` of this generator's flow config."""
        eq = self.cfg["equities"]
        flow, mix = eq["flow"], eq["mix"]
        passive = float(mix["add"]) + float(mix["cancel"]) + float(mix["modify"])
        return excitation_time_factor(
            flow["excitation_kick"],
            flow["excitation_decay"],
            flow["max_excitation"],
            max(0.0, 1.0 - passive),
        )

    # --------------------------------------------------------------- planted

    def _check_planted(self) -> None:
        """Validate the ``planted`` block (module docs); fail fast."""
        planted = self.cfg["planted"]
        flow, lead, brk = planted["order_flow"], planted["lead_lag"], planted["break"]
        if not 0.0 <= float(flow["strength"]) < 1.0:
            raise ValueError("planted.order_flow.strength must be in [0, 1)")
        if not 0.0 < float(flow["kernel_decay"]) <= 1.0:
            raise ValueError("planted.order_flow.kernel_decay must be in (0, 1]")
        if int(flow["kernel_steps"]) < 1:
            raise ValueError("planted.order_flow.kernel_steps must be >= 1")
        if not math.isfinite(float(lead["beta"])):
            raise ValueError("planted.lead_lag.beta must be finite")
        if int(lead["lag_steps"]) < 1:
            raise ValueError(
                "planted.lead_lag.lag_steps must be >= 1 (a contemporaneous link is not a lead)"
            )
        if float(lead["beta"]) != 0.0 and not any(
            i.symbol == lead["leader"] for i in self.ref.instruments("EQUITY")
        ):
            raise ValueError(
                f"planted.lead_lag.leader {lead['leader']!r} is not an equity "
                "instrument of the reference data"
            )
        at = brk["at_fraction"]
        if at is not None and not 0.0 < float(at) < 1.0:
            raise ValueError("planted.break.at_fraction must be in (0, 1) or null")
        post = float(brk["post_multiplier"])
        if not math.isfinite(post) or abs(post * float(flow["strength"])) >= 1.0:
            raise ValueError(
                "planted.break.post_multiplier must be finite and keep "
                "|post_multiplier * order_flow.strength| < 1"
            )

    def _planted_multipliers(self, session_index: int, sessions: int, steps: int) -> list[float]:
        """Per-grid-step multiplier of the planted strengths for one session:
        1 before the break, ``post_multiplier`` from it on (all 1 without)."""
        brk = self.cfg["planted"]["break"]
        at = brk["at_fraction"]
        if at is None:
            return [1.0] * steps
        post = float(brk["post_multiplier"])
        return [
            post if (session_index + k / steps) / sessions >= float(at) else 1.0
            for k in range(steps)
        ]

    # ----------------------------------------------------------------- utils

    def _rng(self, kind: str, a: int, b: int) -> SplitMix64:
        """Deterministic per-(kind, instrument, venue) RNG, stable across calls."""
        key = (kind, a, b)
        rng = self._rngs.get(key)
        if rng is None:
            rng = SplitMix64(self.seed ^ (a << 32) ^ (b << 16) ^ len(kind))
            self._rngs[key] = rng
        return rng

    @staticmethod
    def _emit(
        stream: _Stream,
        out: list[MarketEvent],
        rng: SplitMix64,
        ts: int,
        event_type: int,
        side: int = 0,
        price: int = 0,
        qty: int = 0,
        order_id: int = 0,
        trade_id: int = 0,
    ) -> MarketEvent:
        """Append one event with the next sequence and a latency-modelled receive_ts."""
        stream.seq += 1
        venue = stream.venue
        latency = venue.latency_mean_ns + rng.below(venue.latency_jitter_ns + 1)
        receive = ts + latency
        if receive <= stream.last_receive:
            receive = stream.last_receive + 1
        stream.last_receive = receive
        ev = MarketEvent(
            0,
            stream.inst.instrument_id,
            venue.venue_id,
            ts,
            receive,
            stream.seq,
            int(event_type),
            int(side),
            price,
            qty,
            order_id,
            trade_id,
        )
        stream.book.apply(ev)
        out.append(ev)
        return ev

    def _snapshot_burst(
        self, stream: _Stream, out: list[MarketEvent], rng: SplitMix64, ts: int
    ) -> None:
        """Emit a full-book SNAPSHOT burst from the stream's internal state."""
        records: list[tuple[int, int, int, int]] = []  # (side, price, qty, oid)
        for side in (int(Side.BID), int(Side.ASK)):
            for level in stream.book._sorted_levels(side):
                for oid, q in level.orders.items():
                    # Synthetic (id-less) resting orders are re-emitted id-less.
                    records.append((side, level.price, q, oid if oid < SYNTHETIC_ID_BASE else 0))
        if not records:  # nothing to recover; emit a heartbeat to advance
            self._emit(stream, out, rng, ts, EventType.HEARTBEAT)
            return
        n = len(records)
        for i, (side, price, q, oid) in enumerate(records):
            self._emit(
                stream,
                out,
                rng,
                ts,
                EventType.SNAPSHOT,
                side=side,
                price=price,
                qty=q,
                order_id=oid,
                trade_id=n - 1 - i,
            )

    # -------------------------------------------------------------- equities

    def _eq_add(
        self,
        stream: _Stream,
        out: list[MarketEvent],
        rng: SplitMix64,
        ts: int,
        forced_side: int | None = None,
    ) -> None:
        cfg = self.cfg["equities"]
        mid = max(11, int(round(stream.mid_f)))
        side = (
            forced_side
            if forced_side is not None
            else (int(Side.BID) if rng.uniform() < 0.5 else int(Side.ASK))
        )
        offset = rng.below(6)
        if offset == 0 and forced_side is None:
            # Aggressive marketable ADD: cross the venue's own touch, sized
            # to be fully consumed so no crossing remainder ever rests (the
            # consolidated multi-venue book must stay uncrossed).
            opp = int(Side.ASK) if side == Side.BID else int(Side.BID)
            level = stream.book._best_level(opp)
            if level is not None:
                head_qty = next(iter(level.orders.values()))
                qty = min(head_qty, stream.inst.lot_size * rng.randint(1, 3))
                self._emit(
                    stream,
                    out,
                    rng,
                    ts,
                    EventType.ADD,
                    side=side,
                    price=level.price,
                    qty=qty,
                    order_id=stream.new_order_id(),
                )
                return
            offset = 1  # empty opposite side: fall through to a passive ADD
        offset = max(offset, 1)
        price = mid - offset if side == Side.BID else mid + offset
        if price < 1:
            price = 1
        qty = stream.inst.lot_size * rng.randint(1, cfg["book"]["qty_lots_max"])
        self._emit(
            stream,
            out,
            rng,
            ts,
            EventType.ADD,
            side=side,
            price=price,
            qty=qty,
            order_id=stream.new_order_id(),
        )

    def _eq_slot(
        self, stream: _Stream, price: _EffPrice, out: list[MarketEvent], rng: SplitMix64, ts: int
    ) -> None:
        """Generate one flow slot (1-2 events) for an equity stream."""
        cfg = self.cfg["equities"]
        book = stream.book
        # Venue-effective mid = shared efficient price + bounded AR(1)
        # venue microstructure noise (well below half the typical spread).
        ncfg = cfg["venue_noise"]
        noise = ncfg["rho"] * stream.noise + ncfg["sigma_ticks"] * rng.normal()
        bound = ncfg["max_ticks"]
        stream.noise = min(max(noise, -bound), bound)
        stream.mid_f = price.mid_at(ts) + stream.noise
        stream.excitation *= cfg["flow"]["excitation_decay"]

        # Reprice: cancel this venue's resting orders that the efficient
        # price has moved through (bid above / ask below the venue mid).
        # Market makers pull stale quotes; without this the consolidated
        # multi-venue book stays crossed until random flow cleans up.
        vmid = int(round(stream.mid_f))
        stale = [
            o
            for o in book.resting_orders()
            if (o[1] == Side.BID and o[2] > vmid) or (o[1] == Side.ASK and o[2] < vmid)
        ]
        for oid, side, p, q in stale:
            self._emit(
                stream, out, rng, ts, EventType.CANCEL, side=side, price=p, qty=q, order_id=oid
            )

        # Keep both sides populated.
        if book.best_bid() is None:
            self._eq_add(stream, out, rng, ts, forced_side=int(Side.BID))
            return
        if book.best_ask() is None:
            self._eq_add(stream, out, rng, ts, forced_side=int(Side.ASK))
            return

        mix = cfg["mix"]
        u = rng.uniform()
        if u < mix["add"]:
            self._eq_add(stream, out, rng, ts)
        elif u < mix["add"] + mix["cancel"]:
            orders = book.resting_orders()
            if not orders:
                self._eq_add(stream, out, rng, ts)
                return
            oid, side, price, qty = orders[rng.below(len(orders))]
            self._emit(
                stream,
                out,
                rng,
                ts,
                EventType.CANCEL,
                side=side,
                price=price,
                qty=qty,
                order_id=oid,
            )
        elif u < mix["add"] + mix["cancel"] + mix["modify"]:
            orders = book.resting_orders()
            if not orders:
                self._eq_add(stream, out, rng, ts)
                return
            oid, side, price, qty = orders[rng.below(len(orders))]
            new_qty = stream.inst.lot_size * rng.randint(1, cfg["book"]["qty_lots_max"])
            if new_qty == qty:
                new_qty += stream.inst.lot_size
            self._emit(
                stream,
                out,
                rng,
                ts,
                EventType.MODIFY,
                side=side,
                price=price,
                qty=new_qty,
                order_id=oid,
            )
        else:
            # Aggression: EXECUTE against the FIFO head of the best opposite
            # level, plus the tape TRADE print.  buy_probability is exactly
            # 0.5 unless planted informed flow is armed; the draw is the
            # same draw either way.
            aggressor = (
                int(Side.BID) if rng.uniform() < price.buy_probability(ts) else int(Side.ASK)
            )
            resting = int(Side.ASK) if aggressor == Side.BID else int(Side.BID)
            level = book._best_level(resting)
            if level is None:
                self._eq_add(stream, out, rng, ts)
                return
            head_id, head_qty = next(iter(level.orders.items()))
            price = level.price
            fill = min(head_qty, stream.inst.lot_size * rng.randint(1, 3))
            self._emit(
                stream,
                out,
                rng,
                ts,
                EventType.EXECUTE,
                side=resting,
                price=price,
                qty=fill,
                order_id=head_id,
            )
            self._emit(
                stream,
                out,
                rng,
                ts,
                EventType.TRADE,
                side=aggressor,
                price=price,
                qty=fill,
                trade_id=stream.new_trade_id(),
            )
            stream.excitation = min(
                stream.excitation + cfg["flow"]["excitation_kick"],
                cfg["flow"]["max_excitation"],
            )
        # Depth pruning with a real CANCEL of the order farthest from mid.
        if book.order_count_total() > cfg["book"]["max_resting_orders"]:
            mid = int(round(stream.mid_f))
            far = max(book.resting_orders(), key=lambda o: (abs(o[2] - mid), o[0]))
            self._emit(
                stream,
                out,
                rng,
                ts,
                EventType.CANCEL,
                side=far[1],
                price=far[2],
                qty=far[3],
                order_id=far[0],
            )

    def _reopen_auction(
        self, stream: _Stream, out: list[MarketEvent], rng: SplitMix64, t_end: int, price: _EffPrice
    ) -> None:
        """Re-opening auction after a halt (pinned synthetic model).

        STATUS AUCTION opens the call ``reopen_call_s`` before ``t_end``;
        during the call two crossing ADDs (a bid at the best ask, an ask at
        the best bid) REST — the book is crossed, nothing matches while the
        status is AUCTION — then the venue uncrosses them with two EXECUTEs,
        prints the auction TRADEs at the mid, and STATUS TRADING resumes
        continuous matching exactly at ``t_end``.
        """
        cfg = self.cfg["equities"]
        book = stream.book
        lot = stream.inst.lot_size
        call_ns = int(cfg["halt"]["reopen_call_s"]) * NS
        if call_ns < (cfg["auction_prints"] + 5) * 1_000_000:
            raise ValueError("halt.reopen_call_s too short for the auction messages")
        t = t_end - call_ns
        self._emit(stream, out, rng, t, EventType.STATUS, qty=int(SessionStatus.AUCTION))
        bb, ba = book.best_bid(), book.best_ask()
        if bb is not None and ba is not None:
            qty = lot * rng.randint(1, 3)
            t += 1_000_000
            bid_id = stream.new_order_id()
            self._emit(
                stream,
                out,
                rng,
                t,
                EventType.ADD,
                side=int(Side.BID),
                price=ba[0],
                qty=qty,
                order_id=bid_id,
            )
            t += 1_000_000
            ask_id = stream.new_order_id()
            self._emit(
                stream,
                out,
                rng,
                t,
                EventType.ADD,
                side=int(Side.ASK),
                price=bb[0],
                qty=qty,
                order_id=ask_id,
            )
            # Uncross: the venue matches the two auction orders together.
            t += 1_000_000
            self._emit(
                stream,
                out,
                rng,
                t,
                EventType.EXECUTE,
                side=int(Side.BID),
                price=ba[0],
                qty=qty,
                order_id=bid_id,
            )
            t += 1_000_000
            self._emit(
                stream,
                out,
                rng,
                t,
                EventType.EXECUTE,
                side=int(Side.ASK),
                price=bb[0],
                qty=qty,
                order_id=ask_id,
            )
            mid = max(1, int(round(price.mid_at(t))))
            for i in range(cfg["auction_prints"]):
                t += 1_000_000
                self._emit(
                    stream,
                    out,
                    rng,
                    t,
                    EventType.TRADE,
                    side=i % 2,
                    price=mid,
                    qty=lot * rng.randint(1, 20),
                    trade_id=stream.new_trade_id(),
                )
        self._emit(stream, out, rng, t_end, EventType.STATUS, qty=int(SessionStatus.TRADING))

    def _eq_session_stream(
        self,
        stream: _Stream,
        price: _EffPrice,
        rng: SplitMix64,
        open_ns: int,
        close_ns: int,
        slots: int,
        halt_window: tuple[int, int] | None,
        anomalies: dict | None,
        max_events: int | None = None,
    ) -> list[MarketEvent]:
        """One session of MBO flow for one equity stream.

        ``price`` is the instrument's SHARED efficient price (already
        advanced to this session via ``new_session``); the stream only adds
        its venue noise on top.
        """
        cfg = self.cfg["equities"]
        out: list[MarketEvent] = []
        t = open_ns
        stream.mid_f = price.mid_at(t)
        mid = max(11, int(round(stream.mid_f)))
        lot = stream.inst.lot_size

        # Open auction.
        self._emit(stream, out, rng, t, EventType.STATUS, qty=int(SessionStatus.AUCTION))
        for i in range(cfg["auction_prints"]):
            t += 1_000_000
            self._emit(
                stream,
                out,
                rng,
                t,
                EventType.TRADE,
                side=i % 2,
                price=mid,
                qty=lot * rng.randint(1, 20),
                trade_id=stream.new_trade_id(),
            )
        t += 1_000_000
        self._emit(stream, out, rng, t, EventType.STATUS, qty=int(SessionStatus.TRADING))

        # Seed the book on the very first session.
        if stream.book.order_count_total() == 0:
            for offset in range(1, 7):
                for _ in range(2):
                    t += 1_000
                    for side, px in (
                        (int(Side.BID), mid - offset),
                        (int(Side.ASK), mid + offset),
                    ):
                        self._emit(
                            stream,
                            out,
                            rng,
                            t,
                            EventType.ADD,
                            side=side,
                            price=max(px, 1),
                            qty=lot * rng.randint(1, cfg["book"]["qty_lots_max"]),
                            order_id=stream.new_order_id(),
                        )

        duration_s = max((close_ns - t) / NS, 1.0)
        halted = halt_window is None
        if cfg["flow"]["calibration"] == FLOW_CALIBRATION_SESSION:
            # Calibrated: ``slots`` EXPECTED slots over the session and no
            # budget, so flow runs until the close (module docs).
            lambda0 = slots * self.eq_time_factor() / duration_s
            budget = itertools.count()
        else:
            # v1.3.0 rule: the slot budget ends continuous flow well before
            # the close; fill_session draws slots until the close.
            lambda0 = slots / duration_s * 1.30
            budget = itertools.count() if cfg["fill_session"] else range(slots)
        for _ in budget:
            rate = lambda0 * (1.0 + stream.excitation)
            t += int(rng.exponential(rate) * NS) + 1
            if not halted and t >= halt_window[0]:
                self._emit(
                    stream, out, rng, halt_window[0], EventType.STATUS, qty=int(SessionStatus.HALT)
                )
                t = halt_window[1]
                if cfg["halt"].get("reopen_auction"):
                    self._reopen_auction(stream, out, rng, t, price)
                else:
                    self._emit(
                        stream, out, rng, t, EventType.STATUS, qty=int(SessionStatus.TRADING)
                    )
                halted = True
            if t >= close_ns - 2_000_000:
                break
            if anomalies is not None and rng.uniform() < anomalies["gap_prob"]:
                missing = rng.randint(1, anomalies["gap_max_events"])
                stream.seq += missing
                self.injected["gaps"] += 1
                self.injected["gap_missing_events"] += missing
                self._snapshot_burst(stream, out, rng, t)
                continue
            self._eq_slot(stream, price, out, rng, t)
            if max_events is not None and len(out) >= max_events:
                break

        # Close auction (printed at the shared efficient price so both
        # venues of the instrument print the same close).
        close_mid = max(1, int(round(price.mid_at(close_ns))))
        self._emit(stream, out, rng, close_ns, EventType.STATUS, qty=int(SessionStatus.AUCTION))
        for i in range(cfg["auction_prints"]):
            self._emit(
                stream,
                out,
                rng,
                close_ns + (i + 1) * 1_000_000,
                EventType.TRADE,
                side=i % 2,
                price=close_mid,
                qty=lot * rng.randint(1, 20),
                trade_id=stream.new_trade_id(),
            )
        self._emit(
            stream,
            out,
            rng,
            close_ns + (cfg["auction_prints"] + 1) * 1_000_000,
            EventType.STATUS,
            qty=int(SessionStatus.CLOSE),
        )
        return out

    # -------------------------------------------------------------------- FX

    def _fx_pair_session(
        self,
        inst: Instrument,
        streams: list[_Stream],
        rng: SplitMix64,
        open_ns: int,
        close_ns: int,
        slots: int,
        anomalies: dict | None,
        max_events: int | None = None,
    ) -> list[MarketEvent]:
        """One session of QUOTE+TRADE flow for one FX pair across venues."""
        cfg = self.cfg["fx"]
        out: list[MarketEvent] = []
        pair_mid = streams[0].mid_f
        regime = streams[0].regime
        vol = cfg["vol_regimes"]
        t = open_ns
        duration_s = max((close_ns - open_ns) / NS, 1.0)
        lambda0 = slots / duration_s * 1.05

        for _ in range(slots):
            t += int(rng.exponential(lambda0) * NS) + 1
            if t >= close_ns:
                break
            if rng.uniform() < vol["switch_prob"]:
                regime ^= 1
            pair_mid += rng.normal() * vol["sigma_ticks"][regime]
            lo = 0.5 * inst.ref_price_ticks
            hi = 2.0 * inst.ref_price_ticks
            pair_mid = min(max(pair_mid, lo), hi)
            stream = streams[rng.below(len(streams))]
            if anomalies is not None and rng.uniform() < anomalies["gap_prob"]:
                if stream.book.order_count_total() > 0:
                    missing = rng.randint(1, anomalies["gap_max_events"])
                    stream.seq += missing
                    self.injected["gaps"] += 1
                    self.injected["gap_missing_events"] += missing
                    self._snapshot_burst(stream, out, rng, t)
                    continue
            mid = int(round(pair_mid))
            bb, ba = stream.book.best_bid(), stream.book.best_ask()
            if bb is not None and ba is not None and rng.uniform() < cfg["trade_prob"]:
                aggressor = int(Side.BID) if rng.uniform() < 0.5 else int(Side.ASK)
                price = ba[0] if aggressor == Side.BID else bb[0]
                self._emit(
                    stream,
                    out,
                    rng,
                    t,
                    EventType.TRADE,
                    side=aggressor,
                    price=price,
                    qty=rng.randint(1, cfg["qty_units_max"]),
                    trade_id=stream.new_trade_id(),
                )
            else:
                spread = cfg["spread_ticks"][stream.venue.venue]
                half = spread // 2
                bid = mid - half
                ask = bid + spread + rng.below(2)
                self._emit(
                    stream,
                    out,
                    rng,
                    t,
                    EventType.QUOTE,
                    side=int(Side.BID),
                    price=max(bid, 1),
                    qty=rng.randint(1, cfg["qty_units_max"]),
                    order_id=stream.new_order_id(),
                )
                self._emit(
                    stream,
                    out,
                    rng,
                    t,
                    EventType.QUOTE,
                    side=int(Side.ASK),
                    price=max(ask, 2),
                    qty=rng.randint(1, cfg["qty_units_max"]),
                    order_id=stream.new_order_id(),
                )
            if max_events is not None and len(out) >= max_events:
                break
        streams[0].mid_f = pair_mid
        streams[0].regime = regime
        return out

    # ------------------------------------------------------- anomaly injection

    def _inject_file_anomalies(
        self, events: list[MarketEvent], rng: SplitMix64
    ) -> list[MarketEvent]:
        """Duplicate / out-of-order / invalid / ts-violation injection.

        Operates on one raw file's events; returns the final arrival-ordered
        list. Gap injection happens at generation time (with SNAPSHOT
        recovery); this pass handles the remaining anomaly classes.
        """
        an = self.cfg["anomalies"]
        clones: list[MarketEvent] = []
        last_exch: dict[tuple[int, int], int] = {}
        for ev in events:
            stream_key = (ev.venue_id, ev.instrument_id)
            prev_exch = last_exch.get(stream_key, 0)
            last_exch[stream_key] = ev.exchange_ts
            if ev.event_type in (EventType.SNAPSHOT, EventType.STATUS):
                continue  # keep recovery/status paths clean
            u = rng.uniform()
            if u < an["dup_prob"]:
                dup = MarketEvent(**ev.to_dict())
                dup.receive_ts = ev.receive_ts + 1_500
                clones.append(dup)
                self.injected["duplicates"] += 1
            elif u < an["dup_prob"] + an["ooo_prob"]:
                ev.receive_ts += 2_000_000_000  # 2s latency spike => late arrival
                self.injected["out_of_order"] += 1
            elif u < an["dup_prob"] + an["ooo_prob"] + an["invalid_prob"]:
                bad = MarketEvent(**ev.to_dict())
                bad.receive_ts = ev.receive_ts + 800
                if rng.below(2) == 0:
                    bad.event_type = 0  # unknown type
                else:
                    bad.side = 9  # impossible side
                clones.append(bad)
                self.injected["invalid"] += 1
            elif u < (
                an["dup_prob"] + an["ooo_prob"] + an["invalid_prob"] + an["ts_violation_prob"]
            ):
                # Guard: only on events spaced >= 1ms from their stream
                # predecessor, so the violation never also inverts arrival
                # order (which would contaminate the out-of-order counts).
                if ev.exchange_ts - prev_exch >= 1_000_000:
                    ev.receive_ts = ev.exchange_ts - 50_000
                    self.injected["ts_violations"] += 1
        events.extend(clones)
        return self._finalize_file(events)

    @staticmethod
    def _finalize_file(events: list[MarketEvent]) -> list[MarketEvent]:
        """Sort into arrival order and assign event_id 1..N."""
        events.sort(
            key=lambda e: (
                e.receive_ts,
                e.exchange_ts,
                e.venue_id,
                e.instrument_id,
                e.sequence,
                e.event_type,
            )
        )
        for i, ev in enumerate(events):
            ev.event_id = i + 1
        return events

    # ------------------------------------------------------------ run driver

    def generate_run(self, raw_dir) -> dict:
        """Generate the full configured run into ``raw_dir``; return stats."""
        raw_dir = Path(raw_dir)
        raw_dir.mkdir(parents=True, exist_ok=True)
        sessions = self.cfg["sessions"]
        dates = self.ref.trading_days[:sessions]
        if len(dates) < sessions:
            raise ValueError(f"calendar has {len(dates)} trading days, need {sessions}")
        an = self.cfg["anomalies"]
        halt_cfg = self.cfg["equities"]["halt"]
        eq_slots = self.cfg["equities"]["slots_per_stream"]
        fx_slots = self.cfg["fx"]["slots_per_pair"]

        eq_venues = self.ref.venues("EQUITY")
        fx_venues = self.ref.venues("FX")
        stats = {"files": {}, "sessions": len(dates), "total_events": 0}

        for si, date in enumerate(dates):
            eq_open, eq_close = self.ref.session_bounds_ns("EQUITY", date)
            fx_open, fx_close = self.ref.session_bounds_ns("FX", date)

            eq_events: list[MarketEvent] = []
            vol_cfg = self.cfg["equities"]["vol_regimes"]
            planted = self.cfg["planted"]
            flow_cfg, lead_cfg = planted["order_flow"], planted["lead_lag"]
            flow_on = float(flow_cfg["strength"]) != 0.0
            lead_on = float(lead_cfg["beta"]) != 0.0
            multipliers: list[float] = []
            leader_moves: list[float] = []
            leader_id = -1
            if flow_on or lead_on:
                steps = _EffPrice.session_steps(eq_open, eq_close)
                multipliers = self._planted_multipliers(si, len(dates), steps)
            if lead_on:
                # The leader's path first: it has its own dedicated RNG
                # stream, so advancing it early changes no draw.
                leader = self.ref.instrument(lead_cfg["leader"])
                leader_id = leader.instrument_id
                lead_price = self._eq_prices.get(leader_id)
                if lead_price is None:
                    lead_price = _EffPrice(leader, self._rng("eqmid", leader_id, 0))
                    self._eq_prices[leader_id] = lead_price
                lead_price.new_session(eq_open, eq_close, vol_cfg)
                leader_moves = lead_price.step_moves()
            for inst in self.ref.instruments("EQUITY"):
                # Advance the instrument's SHARED efficient price for this
                # session (dedicated per-instrument RNG stream, so the path
                # is identical for every venue regardless of interleaving).
                price = self._eq_prices.get(inst.instrument_id)
                if price is None:
                    price = _EffPrice(inst, self._rng("eqmid", inst.instrument_id, 0))
                    self._eq_prices[inst.instrument_id] = price
                if not lead_on:
                    price.new_session(eq_open, eq_close, vol_cfg)
                elif inst.instrument_id != leader_id:
                    lag = int(lead_cfg["lag_steps"])
                    beta = float(lead_cfg["beta"])
                    price.new_session(
                        eq_open,
                        eq_close,
                        vol_cfg,
                        drift_ticks=[
                            beta * multipliers[k] * leader_moves[k - lag] if k >= lag else 0.0
                            for k in range(len(multipliers))
                        ],
                    )
                if flow_on:
                    price.set_informed_flow(
                        [float(flow_cfg["strength"]) * m for m in multipliers],
                        float(flow_cfg["kernel_decay"]),
                        int(flow_cfg["kernel_steps"]),
                        float(vol_cfg["sigma_ticks_per_s"][0]),
                    )
                for venue in eq_venues:
                    key = (inst.instrument_id, venue.venue_id)
                    stream = self._eq_streams.get(key)
                    if stream is None:
                        stream = _Stream(inst, venue)
                        self._eq_streams[key] = stream
                    rng = self._rng("eq", inst.instrument_id, venue.venue_id)
                    halt_window = None
                    if inst.symbol == halt_cfg["instrument"] and si == halt_cfg["session_index"]:
                        halt_at = eq_open + (eq_close - eq_open) * 3 // 10
                        halt_window = (halt_at, halt_at + halt_cfg["duration_s"] * NS)
                    eq_events.extend(
                        self._eq_session_stream(
                            stream,
                            price,
                            rng,
                            eq_open,
                            eq_close,
                            eq_slots,
                            halt_window,
                            an,
                        )
                    )
            eq_events = self._inject_file_anomalies(eq_events, self._rng("anom-eq", si, 0))
            eq_path = raw_dir / f"eq_{date.replace('-', '')}.jsonl"
            write_jsonl(eq_path, eq_events)
            stats["files"][eq_path.name] = len(eq_events)
            stats["total_events"] += len(eq_events)

            fx_events: list[MarketEvent] = []
            for inst in self.ref.instruments("FX"):
                streams = []
                for venue in fx_venues:
                    key = (inst.instrument_id, venue.venue_id)
                    stream = self._fx_streams.get(key)
                    if stream is None:
                        stream = _Stream(inst, venue)
                        self._fx_streams[key] = stream
                    streams.append(stream)
                rng = self._rng("fx", inst.instrument_id, 0)
                fx_events.extend(
                    self._fx_pair_session(inst, streams, rng, fx_open, fx_close, fx_slots, an)
                )
            fx_events = self._inject_file_anomalies(fx_events, self._rng("anom-fx", si, 0))
            fx_path = raw_dir / f"fx_{date.replace('-', '')}.jsonl"
            write_jsonl(fx_path, fx_events)
            stats["files"][fx_path.name] = len(fx_events)
            stats["total_events"] += len(fx_events)

        stats["injected_anomalies"] = dict(self.injected)
        return stats


# ------------------------------------------------------------- golden vectors


def generate_golden_eq(
    refdata: ReferenceData, seed: int = GOLDEN_EQ_SEED, n: int = 2000
) -> list[MarketEvent]:
    """Pinned single-instrument (SYN.EQ.001 @ XV1) clean MBO golden vector.

    No anomalies, no halt; exactly ``n`` events in exchange-time order with
    event_id 1..n and contiguous sequences.  The vector is a fixed test
    input cut from the start of a stream: it keeps the v1.3.0 flow rule
    (``legacy_budget``) so its bytes never move with the dataset default.
    """
    gen = MarketDataGenerator(
        refdata,
        {"seed": seed, "equities": {"flow": {"calibration": FLOW_CALIBRATION_LEGACY}}},
    )
    inst = refdata.instrument("SYN.EQ.001")
    venue = refdata.venue("XV1")
    stream = _Stream(inst, venue)
    rng = SplitMix64(seed)
    date = refdata.trading_days[0]
    open_ns, close_ns = refdata.session_bounds_ns("EQUITY", date)
    price = _EffPrice(inst, gen._rng("eqmid", inst.instrument_id, 0))
    price.new_session(open_ns, close_ns, gen.cfg["equities"]["vol_regimes"])
    events = gen._eq_session_stream(
        stream,
        price,
        rng,
        open_ns,
        close_ns,
        slots=n + 200,
        halt_window=None,
        anomalies=None,
        max_events=n + 20,
    )
    events = events[:n]
    if len(events) != n:
        raise RuntimeError(f"golden EQ vector produced {len(events)} < {n} events")
    for i, ev in enumerate(events):
        ev.event_id = i + 1
    return events


def generate_golden_fx(
    refdata: ReferenceData, seed: int = GOLDEN_FX_SEED, n: int = 800
) -> list[MarketEvent]:
    """Pinned EUR/USD QUOTE+TRADE golden vector across LP1/LP2/PRI.

    No anomalies; exactly ``n`` events in exchange-time order with event_id
    1..n; per-venue sequences contiguous.
    """
    gen = MarketDataGenerator(refdata, {"seed": seed})
    inst = refdata.instrument("EUR/USD")
    streams = [_Stream(inst, refdata.venue(v)) for v in ("LP1", "LP2", "PRI")]
    rng = SplitMix64(seed)
    date = refdata.trading_days[0]
    open_ns, close_ns = refdata.session_bounds_ns("FX", date)
    events = gen._fx_pair_session(
        inst,
        streams,
        rng,
        open_ns,
        close_ns,
        slots=n + 200,
        anomalies=None,
        max_events=n + 4,
    )
    events = events[:n]
    if len(events) != n:
        raise RuntimeError(f"golden FX vector produced {len(events)} < {n} events")
    for i, ev in enumerate(events):
        ev.event_id = i + 1
    return events
