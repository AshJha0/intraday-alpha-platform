"""Automatic leakage testing (spec §13: "test feature leakage automatically").

Two independent detectors, both mandatory in the promotion pipeline:

1. **Label-column guard** — the model is scored twice on the same frames,
   once as-is and once with EVERY label column replaced by garbage
   (0.12345 constants).  Any difference in output proves the scoring path
   reads labels (the canonical catastrophic leak).  Alphas may read labels
   in fit() only.

2. **Shift-by-one test** (conventions §7: "shift-by-one destroys IC") —
   compare the IC of score_t vs label_t with the IC of score_{t-1} vs
   label_t (scores lagged one emission).  A genuine microstructure signal
   loses part of its IC under a one-event lag but not implausibly much,
   and its unlagged IC is bounded by realistic signal-to-noise.  A signal
   whose unlagged IC is suspicious while collapsing under the lag has the
   signature of lookahead (a feature built from the same events the label
   measures) and is flagged for manual review; promotion is blocked.

   **Calibration (pinned, round-3).**  The old detector only fired above
   |IC| > 0.15 and was only ever tested at IC > 0.9, so a shifted-label
   leak worth IC 0.12 — twelve times the PROMOTE gate — passed silently.
   Two changes:

   - ``suspicious_ic`` is a multiple of the promotion gate, not an absolute
     number: ``LEAK_IC_MULTIPLE (3) x min_oos_ic (0.010) = 0.030``.  Any
     alpha strong enough to matter is strong enough to be checked.
   - the collapse test is scaled by how much of the horizon one emission
     actually consumes.  On a stream whose rows are 15 s apart a genuine
     1 s alpha MUST collapse under a one-row shift — that is row spacing,
     not lookahead.  The required survival ratio is therefore

         required = collapse_ratio * (1 - min(1, median_row_gap / horizon))

     so it is 0 when a row already spans the whole horizon (no signal is
     expected to survive: the test cannot discriminate and never fires) and
     the full ``collapse_ratio`` when rows are dense relative to the
     horizon.  ``median_row_gap`` is measured from the frames themselves.

3. **Engine-level leak probe** (``truncation_probe``): re-score the model on
   frames truncated at each of several anchors and assert the score at the
   anchor is bit-identical to the score computed with the full frame.  A
   scoring path that peeks at a later row changes; a causal one cannot.

4. **Recompute probe** (``recompute_probe``, opt-in — it needs the raw
   events and a feature builder, so it is not part of :meth:`LeakageTester.run`
   and adds nothing to :class:`LeakageResult`).  Detector 3 truncates
   PRECOMPUTED feature frames, so it can only see look-ahead in the scoring
   path.  A feature that was itself built with look-ahead — a centred
   window, a full-sample normalisation, a join on a later timestamp — is
   already baked into every row of the frame, and truncating the frame
   changes nothing: the leak is invisible to it.  The recompute probe
   truncates the RAW EVENTS instead: for a small number of anchor positions
   it rebuilds the features from the event prefix ``events[:p + 1]`` and
   requires, for every instrument, the last row of the rebuilt frame to be
   bit-identical (feature columns and the model's score) to the same row of
   the frame built from all events.  A causal pipeline cannot differ; any
   column that does is named in the result.  Cost: one feature rebuild per
   anchor, hence the small default.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from iap.validation.metrics import HORIZONS_NS, ic

_GARBAGE = 0.12345


@dataclass
class LeakageResult:
    label_guard_ok: bool  # False = score() output depends on labels
    ic_unshifted: float
    ic_shifted: float
    shift_ok: bool  # False = lookahead signature
    passed: bool
    truncation_ok: bool = True  # False = score() depends on future rows
    suspicious_ic: float = 0.0  # threshold actually applied
    required_shift_ratio: float = 0.0  # |ic_shifted|/|ic_unshifted| required
    median_row_gap_ns: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RecomputeProbeResult:
    """Outcome of :meth:`LeakageTester.recompute_probe`."""

    ok: bool
    n_anchors: int
    #: ``{"instrument_id", "event_index", "row", "column"}`` per difference;
    #: ``column`` is a feature column, ``expected_return`` or ``confidence``
    mismatches: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def leaky_columns(self) -> list[str]:
        """Distinct offending columns (sorted)."""
        return sorted({m["column"] for m in self.mismatches})


def engine_frame_builder(
    configs_dir, cadence_ns: int = 0
) -> Callable[[Sequence], dict[int, pd.DataFrame]]:
    """A ``build_frames(events)`` for :meth:`LeakageTester.recompute_probe`
    that replays events through the reference ``FeatureEngine`` (a fresh
    engine per call) and returns feature-only frames: ``exchange_ts`` + one
    float64 column per registry feature, NaN where invalid.  No labels —
    they look ahead by definition and are not part of the probe."""
    from iap.features.context import build_contexts
    from iap.features.engine import FeatureEngine
    from iap.features.registry import build_registry

    contexts = build_contexts(configs_dir)
    names = [s.name for s in build_registry()]

    def build(events: Sequence) -> dict[int, pd.DataFrame]:
        engine = FeatureEngine(contexts, cadence_ns=cadence_ns)
        ts: dict[int, list[int]] = {}
        rows: dict[int, list[np.ndarray]] = {}
        for ev in events:
            vec = engine.apply(ev)
            if vec is None:
                continue
            vals = np.asarray(vec.values, dtype=float).copy()
            vals[~np.asarray(vec.validity, dtype=bool)] = np.nan
            ts.setdefault(vec.instrument_id, []).append(vec.timestamp)
            rows.setdefault(vec.instrument_id, []).append(vals)
        out: dict[int, pd.DataFrame] = {}
        for iid in sorted(rows):
            frame = pd.DataFrame(np.vstack(rows[iid]), columns=names)
            frame.insert(0, "exchange_ts", np.asarray(ts[iid], dtype=np.int64))
            out[iid] = frame
        return out

    return build


def _same(a, b) -> bool:
    if isinstance(a, (float, np.floating)) and isinstance(b, (float, np.floating)):
        return bool(a == b or (np.isnan(a) and np.isnan(b)))
    return bool(a == b)


def _obfuscate_labels(frames: Mapping[int, pd.DataFrame]) -> dict[int, pd.DataFrame]:
    out: dict[int, pd.DataFrame] = {}
    for iid, df in frames.items():
        df2 = df.copy()
        for c in df2.columns:
            if c.startswith(("label_mid_", "label_cost_")):
                df2[c] = _GARBAGE
            elif c.startswith("label_valid_"):
                df2[c] = True
        out[iid] = df2
    return out


#: Multiple of the PROMOTE IC gate above which the shift test applies.
LEAK_IC_MULTIPLE = 3.0
#: Pinned PROMOTE IC gate (mirrors validation.validate.GATES).
MIN_OOS_IC_GATE = 0.010


@dataclass
class LeakageResultExtras:
    """Diagnostics attached to a leakage run (JSON-serializable)."""

    suspicious_ic: float
    required_ratio: float
    median_row_gap_ns: int
    horizon_ns: int


class LeakageTester:
    """Runs the detectors against a fitted model on test frames."""

    def __init__(
        self,
        suspicious_ic: float = LEAK_IC_MULTIPLE * MIN_OOS_IC_GATE,
        collapse_ratio: float = 0.5,
    ) -> None:
        self.suspicious_ic = suspicious_ic
        self.collapse_ratio = collapse_ratio

    def label_guard(self, model, frames: Mapping[int, pd.DataFrame]) -> bool:
        """True when score() is invariant to label-column contents."""
        try:
            a = model.score(frames)
            b = model.score(_obfuscate_labels(frames))
        except Exception:
            return False  # scoring crashed on obfuscated labels -> reads them
        for iid in a:
            xa = a[iid]["expected_return"].to_numpy()
            xb = b[iid]["expected_return"].to_numpy()
            ca = a[iid]["confidence"].to_numpy()
            cb = b[iid]["confidence"].to_numpy()
            if not (
                np.array_equal(xa, xb, equal_nan=True) and np.array_equal(ca, cb, equal_nan=True)
            ):
                return False
        return True

    def shift_test(self, model, frames: Mapping[int, pd.DataFrame]) -> dict[str, float]:
        """(ic_unshifted, ic_shifted) pooled across the model's universe."""
        scores = model.score(frames)
        h = model.horizon
        xs, ys, xs_lag, ys_lag = [], [], [], []
        for iid, sc in scores.items():
            df = frames[iid]
            er = sc["expected_return"].to_numpy(dtype=float).copy()
            er[sc["confidence"].to_numpy(dtype=float) <= 0.0] = np.nan
            lab = df[f"label_mid_{h}"].to_numpy(dtype=float).copy()
            lab[~df[f"label_valid_{h}"].to_numpy(dtype=bool)] = np.nan
            xs.append(er)
            ys.append(lab)
            xs_lag.append(er[:-1])  # score_{t-1}
            ys_lag.append(lab[1:])  # label_t
        ic0 = ic(np.concatenate(xs), np.concatenate(ys))
        ic1 = ic(np.concatenate(xs_lag), np.concatenate(ys_lag))
        return {"ic_unshifted": ic0, "ic_shifted": ic1}

    @staticmethod
    def median_row_gap_ns(frames: Mapping[int, pd.DataFrame]) -> int:
        """Median gap between consecutive emission timestamps (pooled)."""
        gaps = []
        for df in frames.values():
            ts = df["exchange_ts"].to_numpy(dtype=np.int64)
            if ts.size >= 2:
                gaps.append(np.diff(ts))
        if not gaps:
            return 0
        allg = np.concatenate(gaps)
        allg = allg[allg > 0]
        return int(np.median(allg)) if allg.size else 0

    def truncation_probe(
        self, model, frames: Mapping[int, pd.DataFrame], n_probes: int = 8
    ) -> bool:
        """Score() must not depend on rows after the row being scored.

        Re-scores the model on frames truncated at a set of anchors and
        compares the anchor row's output with the full-frame output.
        """
        full = model.score(frames)
        for iid, sc in full.items():
            df = frames[iid]
            n = len(df)
            if n < 4:
                continue
            anchors = sorted({max(1, int(n * (k + 1) / (n_probes + 1))) for k in range(n_probes)})
            for a in anchors:
                trunc = {
                    j: (d.iloc[:a].reset_index(drop=True) if j == iid else d)
                    for j, d in frames.items()
                }
                try:
                    part = model.score(trunc)
                except Exception:
                    return False
                if iid not in part or len(part[iid]) != a:
                    return False
                for col in ("expected_return", "confidence"):
                    got = part[iid][col].to_numpy()[a - 1]
                    want = sc[col].to_numpy()[a - 1]
                    if not (got == want or (np.isnan(got) and np.isnan(want))):
                        return False
        return True

    def recompute_probe(
        self,
        model,
        events: Sequence,
        build_frames: Callable[[Sequence], Mapping[int, pd.DataFrame]],
        n_probes: int = 3,
    ) -> RecomputeProbeResult:
        """Features (and the model's score) recomputed from TRUNCATED raw
        events must equal the full-run values (detector 4, module docs).

        ``events`` is the event list in processing order and
        ``build_frames(events)`` the feature pipeline under test (for the
        reference engine: :func:`engine_frame_builder`); it must be
        deterministic.  ``n_probes`` anchor positions are spread evenly over
        the list (the last event is never an anchor: its prefix is the full
        run).  ``model`` may be ``None`` to probe the features alone.
        Columns starting with ``label_`` are ignored.
        """
        n = len(events)
        if n < 2 or n_probes < 1:
            raise ValueError("recompute_probe needs >= 2 events and n_probes >= 1")
        full = build_frames(events)
        full_scores = model.score(full) if model is not None else {}
        positions = sorted(
            {min(n - 2, max(0, int(n * (k + 1) / (n_probes + 1)) - 1)) for k in range(n_probes)}
        )
        mismatches: list[dict] = []
        for p in positions:
            part = build_frames(events[: p + 1])
            part_scores = model.score(part) if model is not None and part else {}
            for iid in sorted(part):
                rows = len(part[iid])
                if rows == 0:
                    continue
                row = rows - 1
                where = {"instrument_id": int(iid), "event_index": int(p), "row": int(row)}
                if iid not in full or len(full[iid]) <= row:
                    mismatches.append({**where, "column": "<row missing from full run>"})
                    continue
                for column in part[iid].columns:
                    if str(column).startswith("label_"):
                        continue
                    if column not in full[iid].columns or not _same(
                        part[iid][column].iloc[row], full[iid][column].iloc[row]
                    ):
                        mismatches.append({**where, "column": str(column)})
                if iid in part_scores and iid in full_scores:
                    for column in ("expected_return", "confidence"):
                        if not _same(
                            part_scores[iid][column].iloc[row], full_scores[iid][column].iloc[row]
                        ):
                            mismatches.append({**where, "column": column})
        return RecomputeProbeResult(
            ok=not mismatches, n_anchors=len(positions), mismatches=mismatches
        )

    def run(
        self, model, frames: Mapping[int, pd.DataFrame], probe_truncation: bool = True
    ) -> LeakageResult:
        guard = self.label_guard(model, frames)
        shifts = self.shift_test(model, frames)
        ic0, ic1 = shifts["ic_unshifted"], shifts["ic_shifted"]
        gap = self.median_row_gap_ns(frames)
        horizon_ns = HORIZONS_NS[model.horizon]
        # A one-row shift on a stream whose rows already span the horizon
        # destroys ANY signal: scale the required survival accordingly.
        scale = 1.0 - min(1.0, gap / float(horizon_ns)) if horizon_ns else 0.0
        required = self.collapse_ratio * scale
        shift_ok = True
        if np.isfinite(ic0) and abs(ic0) > self.suspicious_ic and required > 0:
            survived = np.isfinite(ic1) and abs(ic1) >= required * abs(ic0)
            shift_ok = bool(survived)
        trunc_ok = self.truncation_probe(model, frames) if probe_truncation else True
        return LeakageResult(
            label_guard_ok=guard,
            ic_unshifted=float(ic0) if np.isfinite(ic0) else float("nan"),
            ic_shifted=float(ic1) if np.isfinite(ic1) else float("nan"),
            shift_ok=shift_ok,
            passed=guard and shift_ok and trunc_ok,
            truncation_ok=trunc_ok,
            suspicious_ic=float(self.suspicious_ic),
            required_shift_ratio=float(required),
            median_row_gap_ns=int(gap),
        )
