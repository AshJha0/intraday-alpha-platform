"""Native feature backends: the Python reference or the Rust engine (v1.11 E2).

The Rust crate ``rust/features`` computes a documented 45-slot sub-vector of
the 205-feature registry -- the 40 pinned native features of API_FEATURES.md
§3 plus 5 auxiliary alpha inputs -- and is golden-parity-tested against the
same vectors as the Python reference.  ``rust/features_py`` exposes it to
Python through pyo3 as the optional extension module ``iap_features_rs``
(built with maturin; not installed by default, there is no Rust toolchain
requirement for the Python package).

:func:`compute_native` replays an event stream with either backend and
returns the same :class:`NativeFrame` (numpy arrays, slot order =
:data:`NATIVE_NAMES`)::

    from iap.features.native import compute_native, resolve_engine
    frame = compute_native(events, contexts, cadence_ns=0, engine=resolve_engine("rust"))

``engine="rust"`` raises if the extension is missing; :func:`resolve_engine`
is the fallback policy (``"rust"`` -> ``"python"`` with a warning when the
extension is not importable).  The remaining 160 registry features exist only
in Python.
"""

from __future__ import annotations

import importlib
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from iap.core.codec import encode_iap1, read_iap1, read_jsonl
from iap.core.events import MarketEvent
from iap.features.context import InstrumentContext

ENGINES = ("python", "rust")

#: Registry names of the 45 native slots, in the Rust engine's slot order
#: (rust/features/src/names.rs; tests assert the extension agrees).
NATIVE_NAMES: tuple[str, ...] = (
    "ofi_l1_w1s_v1",
    "ofi_l1_w5s_v1",
    "ofi_l1_w30s_v1",
    "ofi_l3_w1s_v1",
    "ofi_l3_w5s_v1",
    "ofi_l3_w30s_v1",
    "ofi_l5_w1s_v1",
    "ofi_l5_w5s_v1",
    "ofi_l5_w30s_v1",
    "ofi_l10_w1s_v1",
    "ofi_l10_w5s_v1",
    "ofi_l10_w30s_v1",
    "imbalance_l1_v1",
    "imbalance_l3_v1",
    "imbalance_l5_v1",
    "imbalance_l10_v1",
    "mid_price_v1",
    "microprice_v1",
    "micro_mid_dev_bps_v1",
    "spread_ticks_v1",
    "spread_bps_v1",
    "depth_bid_l1_v1",
    "depth_ask_l1_v1",
    "depth_bid_l5_v1",
    "depth_ask_l5_v1",
    "depth_bid_l10_v1",
    "depth_ask_l10_v1",
    "signed_volume_w1s_v1",
    "signed_volume_w10s_v1",
    "signed_volume_w1m_v1",
    "trade_imbalance_w1s_v1",
    "trade_imbalance_w10s_v1",
    "trade_imbalance_w1m_v1",
    "rvol_w10s_v1",
    "rvol_w1m_v1",
    "rvol_w5m_v1",
    "ret_simple_1s_v1",
    "ret_log_1s_v1",
    "ret_log_10s_v1",
    "ret_log_1m_v1",
    "ofi_norm_l1_w1s_v1",
    "ofi_norm_l5_w1s_v1",
    "ofi_norm_l5_w5s_v1",
    "ret_vol_adj_10s_v1",
    "vol_regime_ratio_v1",
)
#: The first NATIVE_COUNT slots are the pinned native 40.
NATIVE_COUNT = 40

_EXT = "iap_features_rs"


def rust_extension():
    """The ``iap_features_rs`` module, or None when it is not installed."""
    try:
        return importlib.import_module(_EXT)
    except ImportError:
        return None


def rust_available() -> bool:
    """True when the Rust extension is importable."""
    return rust_extension() is not None


def resolve_engine(requested: str) -> str:
    """Fallback policy: ``rust`` without the extension -> ``python`` (warned)."""
    if requested not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, got {requested!r}")
    if requested == "rust" and not rust_available():
        warnings.warn(
            "iap_features_rs is not installed (build: maturin develop -m "
            "rust/features_py/Cargo.toml); falling back to the Python engine",
            RuntimeWarning,
            stacklevel=2,
        )
        return "python"
    return requested


@dataclass
class NativeFrame:
    """Emitted rows of the 45 native slots (row-major numpy arrays)."""

    instrument_id: np.ndarray  # uint32 (n,)
    timestamp: np.ndarray  # int64 (n,)
    values: np.ndarray  # float64 (n, 45); NaN where invalid
    validity: np.ndarray  # bool (n, 45)
    events_processed: int
    vectors_emitted: int
    engine: str

    @property
    def names(self) -> tuple[str, ...]:
        return NATIVE_NAMES

    def validity_bits(self) -> np.ndarray:
        """Little-endian packed validity bitset per row, (n, 6) uint8."""
        return np.packbits(self.validity, axis=1, bitorder="little")


def _ticks(contexts: dict[int, InstrumentContext]) -> dict[int, float]:
    return {int(i): float(c.tick_size) for i, c in contexts.items()}


def _from_ext(d: dict, engine: str = "rust") -> NativeFrame:
    return NativeFrame(
        instrument_id=d["instrument_id"],
        timestamp=d["timestamp"],
        values=d["values"],
        validity=d["validity"],
        events_processed=int(d["events_processed"]),
        vectors_emitted=int(d["vectors_emitted"]),
        engine=engine,
    )


def _python_frame(
    events: Sequence[MarketEvent], contexts: dict[int, InstrumentContext], cadence_ns: int
) -> NativeFrame:
    from iap.features.engine import FeatureEngine  # local: heavy import
    from iap.features.registry import feature_index

    idx = feature_index()
    cols = np.array([idx[n] for n in NATIVE_NAMES], dtype=np.intp)
    eng = FeatureEngine(contexts, cadence_ns=cadence_ns)
    iids: list[int] = []
    ts: list[int] = []
    vals: list[list[float]] = []
    valid: list[list[bool]] = []
    for ev in events:
        vec = eng.apply(ev)
        if vec is not None:
            iids.append(vec.instrument_id)
            ts.append(vec.timestamp)
            vals.append(vec.values)
            valid.append(vec.validity)
    n = len(ts)
    full_v = np.asarray(vals, dtype=np.float64).reshape(n, -1)
    full_b = np.asarray(valid, dtype=bool).reshape(n, -1)
    values = np.ascontiguousarray(full_v[:, cols]) if n else np.zeros((0, len(cols)))
    validity = np.ascontiguousarray(full_b[:, cols]) if n else np.zeros((0, len(cols)), bool)
    return NativeFrame(
        instrument_id=np.asarray(iids, dtype=np.uint32),
        timestamp=np.asarray(ts, dtype=np.int64),
        values=values,
        validity=validity,
        events_processed=eng.events_processed,
        vectors_emitted=eng.vectors_emitted,
        engine="python",
    )


def compute_native(
    events: Sequence[MarketEvent],
    contexts: dict[int, InstrumentContext],
    cadence_ns: int = 0,
    engine: str = "python",
) -> NativeFrame:
    """Replay ``events`` and return the 45 native slots with ``engine``.

    The Python backend runs the full reference engine (all 205 features) and
    selects the native columns; the Rust backend computes only the 45 slots.
    """
    if engine == "python":
        return _python_frame(events, contexts, cadence_ns)
    if engine != "rust":
        raise ValueError(f"engine must be one of {ENGINES}, got {engine!r}")
    ext = rust_extension()
    if ext is None:
        raise RuntimeError("engine='rust' but the iap_features_rs extension is not installed")
    data = encode_iap1(list(events))
    return _from_ext(ext.replay_bytes(data, _ticks(contexts), cadence_ns, "iap1"))


def compute_native_file(
    path: str | Path,
    contexts: dict[int, InstrumentContext],
    cadence_ns: int = 0,
    engine: str = "python",
) -> NativeFrame:
    """Like :func:`compute_native` on a normalized file (``.iap1`` or JSONL);
    the Rust backend reads and decodes the file natively."""
    path = Path(path)
    if engine == "rust":
        ext = rust_extension()
        if ext is None:
            raise RuntimeError("engine='rust' but the iap_features_rs extension is not installed")
        return _from_ext(ext.replay_file(str(path), _ticks(contexts), cadence_ns))
    events = read_iap1(path) if path.suffix == ".iap1" else read_jsonl(path)
    return compute_native(events, contexts, cadence_ns, engine)
