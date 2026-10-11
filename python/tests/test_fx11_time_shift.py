"""FX11 time-shift failure: leak or fast signal? (v1.15, R-P1-3).

Synthetic, test-only data: a 1-second log-mid path with volatility bursts
(the FX11 proxy events), sampled every 15 s like the bundled FX streams,
with FX11's two input features rebuilt from the path and a 5m forward
label.  Worlds (:data:`WORLDS`):

- ``fast``: causal features; after each burst the mid drifts on in the
  burst direction with an exponential kernel (tau 45 s) - a genuinely fast
  causal signal FX11 can exploit;
- ``leak15`` / ``leak30`` / ``leak45``: no drift at all (nothing to
  predict), but the features read the path 15 / 30 / 45 s AHEAD of the
  row's stamp - bar-close stamping, forward-fill or a join off by 1-3 rows.

The one-row leak fails the row shift (FX11 did not).  ``leak45`` and
``fast`` both pass the row shift and fail the time shift, like FX11: the
v1.14 gate's two numbers cannot tell them apart.  The v1.15 triage
(detector 2c, ``iap.validation.leakage``) separates them by causality - the
raw-event recompute probe fails every planted leak - and by the lag profile
for leaks shorter than its first lag.  FX11's real pipeline is probed on the
committed FX golden vector, and its committed v1.14 numbers
(``research/alpha_reports/FX11.json``) are fed to the classifier.  See
``docs/research/FX11_FINDING.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from iap.alpha.fx import FX11MacroSurpriseResponse
from iap.core.codec import read_jsonl
from iap.validation.leakage import (
    LeakageTester,
    RecomputeSource,
    classify_time_shift_failure,
    engine_frame_builder,
)

NS_S = 1_000_000_000
T_S = 6 * 3600  # seconds per synthetic session
ROW_S = 15  # row spacing (the FX median row gap is ~15 s)
H_S = 300  # 5m label horizon
WARM_S = 360
SIGMA = 1e-5  # per-second log-mid volatility
JUMP = 20 * SIGMA  # burst size: ~90 % of the minute's variance in 10 s
TAU_S = 45.0  # decay of the fast causal drift
REPO = Path(__file__).resolve().parents[2]
_FX11_INPUTS = ("vol_ratio_w10s_w1m_v1", "ret_vol_adj_1m_v1")


def _path(seed: int, drift: float) -> np.ndarray:
    """Log-mid at every second: noise + bursts (+ a causal drift after
    each burst of ``drift x burst`` spread over an exp(-s/TAU) kernel)."""
    rng = np.random.default_rng(seed)  # test-only fixture data
    inc = rng.standard_normal(T_S + 1) * SIGMA
    inc[0] = 0.0
    events = np.flatnonzero(rng.random(T_S + 1) < 1 / 900)
    s = np.arange(1, 600)
    kern = np.exp(-(s - 1) / TAU_S) - np.exp(-s / TAU_S)
    for te in events:
        j = JUMP * (1 if rng.random() < 0.5 else -1)
        inc[te] += j
        end = min(T_S + 1, te + 600)
        inc[te + 1 : end] += drift * j * kern[: end - te - 1]
    return np.cumsum(inc)


def _features(lm: np.ndarray, ahead: int) -> pd.DataFrame:
    """FX11's inputs at every row the prefix ``lm`` can emit.  ``ahead`` > 0
    plants the leak: the row at t reads the path at t + ahead."""
    n = len(lm)
    dlm = np.diff(lm, prepend=lm[0])
    cs2 = np.cumsum(dlm * dlm)
    t = np.arange(WARM_S, n, ROW_S)
    src = t + ahead
    ok = src < n
    s = np.where(ok, src, 0)
    rv10 = np.sqrt(np.maximum(cs2[s] - cs2[s - 10], 0.0) / 10)
    rv60 = np.sqrt(np.maximum(cs2[s] - cs2[s - 60], 0.0) / 60)
    ratio = rv10 / (rv60 + 1e-12)
    ret = (lm[s] - lm[s - 60]) / (rv60 + 1e-12)
    return pd.DataFrame(
        {
            "exchange_ts": t.astype(np.int64) * NS_S,
            "vol_ratio_w10s_w1m_v1": np.where(ok, ratio, np.nan),
            "ret_vol_adj_1m_v1": np.where(ok, ret, np.nan),
        }
    )


#: world -> (causal drift share, seconds of look-ahead in the features)
WORLDS = {
    "fast": (1.5, 0),
    "leak15": (0.0, ROW_S),  # one-row stamping leak
    "leak30": (0.0, 2 * ROW_S),
    "leak45": (0.0, 3 * ROW_S),  # e.g. a 1m bar stamped near its open
}


def _world(kind: str, n_inst: int = 4):
    drift, ahead = WORLDS[kind]
    paths = {101 + k: _path(7 + k, drift) for k in range(n_inst)}
    frames = {}
    for iid, lm in paths.items():
        df = _features(lm, ahead)
        t = (df["exchange_ts"].to_numpy() // NS_S).astype(int)
        ok = t + H_S < len(lm)
        lab = np.full(len(t), np.nan)
        lab[ok] = lm[t[ok] + H_S] - lm[t[ok]]
        df["label_mid_5m"] = lab
        df["label_valid_5m"] = ok
        frames[iid] = df

    def build(events):  # the feature pipeline under test, on a raw prefix
        n = len(events)
        return {iid: _features(lm[:n], ahead) for iid, lm in paths.items()}

    source = RecomputeSource(events=list(range(T_S + 1)), build_frames=build)
    return frames, source


class _FX11:
    """FX11's own signal code, scored with unit confidence (no fit)."""

    horizon = "5m"

    def __init__(self) -> None:
        self.alpha = FX11MacroSurpriseResponse()

    def score(self, frames):
        out = {}
        for iid, df in frames.items():
            er = self.alpha.raw_signal(df).to_numpy(dtype=float)
            out[iid] = pd.DataFrame(
                {
                    "exchange_ts": df["exchange_ts"].to_numpy(),
                    "expected_return": er,
                    "confidence": np.where(np.isfinite(er), 1.0, 0.0),
                }
            )
        return out


@pytest.fixture(scope="module")
def results():
    out = {}
    for kind in WORLDS:
        frames, source = _world(kind)
        out[kind] = LeakageTester().run(_FX11(), frames, recompute=source).to_dict()
    return out


def _ratios(res):
    return [p["ic"] / res["ic_unshifted"] for p in res["time_shift_profile"]]


def test_one_row_leak_is_already_caught_by_the_row_shift(results):
    """FX11 PASSED the row shift (0.0717 / 0.0995 = 0.72 >= 0.47), so it is
    not a leak confined to one 15 s row: that leak collapses there."""
    res = results["leak15"]
    assert res["ic_unshifted"] > 0.03
    assert res["shift_ok"] is False and res["passed"] is False


def test_fx11_gate_profile_is_reproduced_by_both_a_leak_and_a_fast_signal(results):
    """The two numbers the v1.14 gate looks at cannot tell a 45 s leak from
    a fast causal signal: both pass the row shift and fail the time shift."""
    for kind in ("fast", "leak45"):
        res = results[kind]
        assert res["ic_unshifted"] > 0.03, kind  # suspicious: the detector applies
        assert res["shift_ok"] is True, kind
        assert res["time_shift_ok"] is False and res["passed"] is False, kind
        assert res["ic_time_shifted"] > 0.0, kind  # same sign, decayed


def test_planted_leaks_are_caught_by_the_recompute_probe(results):
    for kind in ("leak30", "leak45"):
        res = results[kind]
        assert res["recompute_ok"] is False, kind
        assert res["truncation_ok"] is True, kind  # the leak is in the features
        assert res["time_shift_class"] == "leak_suspect", kind


def test_short_leak_profile_is_a_cliff(results):
    """A leak shorter than the first profile lag is gone at that lag: the
    profile alone flags it, even if the recompute probe had passed."""
    res = results["leak30"]
    assert abs(_ratios(res)[0]) < 0.25
    lagged = [p["ic"] for p in res["time_shift_profile"]]
    kw = {"label_guard_ok": True, "truncation_ok": True, "recompute_ok": True}
    assert classify_time_shift_failure(res["ic_unshifted"], lagged, **kw) == "leak_suspect"


def test_longer_leak_profile_alone_is_not_decisive(results):
    """Honest limit: a 45 s leak decays like a fast signal across the
    profile lags, so without the causal probes it would read "fast".  The
    recompute probe, not the profile, is what rules a leak out."""
    res = results["leak45"]
    lagged = [p["ic"] for p in res["time_shift_profile"]]
    kw = {"label_guard_ok": True, "truncation_ok": True, "recompute_ok": True}
    assert classify_time_shift_failure(res["ic_unshifted"], lagged, **kw) == "fast_causal"


def test_fast_causal_profile_decays_gradually_and_keeps_its_sign(results):
    res = results["fast"]
    assert res["recompute_ok"] is True and res["truncation_ok"] is True
    r = _ratios(res)
    assert r[0] >= 0.25 and all(x > 0 for x in r)
    assert r[0] >= r[1] >= r[2]  # monotone decay
    assert res["time_shift_class"] == "fast_causal"


def test_fast_causal_pass_is_opt_in(results):
    assert results["fast"]["passed"] is False  # default: still blocks promotion
    on = LeakageTester(accept_fast_causal=True)
    for kind, want in (("fast", True), ("leak45", False)):
        frames, source = _world(kind)
        assert on.run(_FX11(), frames, recompute=source).passed is want, kind


def test_causality_unproven_without_a_recompute_probe():
    frames, _ = _world("fast")
    res = LeakageTester(accept_fast_causal=True).run(_FX11(), frames)
    assert res.time_shift_class == "causality_unproven" and res.passed is False


def test_fx11_inputs_are_causal_in_the_reference_engine():
    """FX11's real feature pipeline (reference FeatureEngine, every event an
    emission) rebuilt from every 20th prefix of the committed FX golden
    vector: its inputs and score at the prefix end equal the full run.  A
    stamping / forward-fill / off-by-one leak would differ at the anchor."""
    events = read_jsonl(REPO / "tests" / "golden" / "events_fx_quote.jsonl")
    build = engine_frame_builder(REPO / "configs", cadence_ns=0)
    probe = LeakageTester().recompute_probe(_FX11(), events, build, n_probes=40)
    assert probe.ok and probe.n_anchors == 40, probe.mismatches[:3]

    def leaky(evs):  # the same pipeline with the features read one row ahead
        out = {}
        for iid, df in build(evs).items():
            df = df.copy()
            for c in _FX11_INPUTS:
                df[c] = df[c].shift(-1)
            out[iid] = df
        return out

    bad = LeakageTester().recompute_probe(_FX11(), events, leaky, n_probes=40)
    assert not bad.ok and set(_FX11_INPUTS) <= set(bad.leaky_columns)


def test_fx11_committed_profile_classifies_fast_causal():
    """FX11's committed v1.14 numbers: IC 0.0995 unlagged, 0.0717 one row
    (15.4 s) later, 0.0048 at 75 s - same sign, decaying, and every causal
    probe green (label guard, truncation, recompute)."""
    rep = json.loads((REPO / "research" / "alpha_reports" / "FX11.json").read_text("utf-8"))
    lk = rep["leakage"]
    assert lk["time_shift_ok"] is False  # the v1.14 failure under review
    assert lk["recompute_ok"] is True and lk["truncation_ok"] and lk["label_guard_ok"]
    cls = classify_time_shift_failure(
        lk["ic_unshifted"],
        [lk["ic_shifted"], lk["ic_time_shifted"]],
        label_guard_ok=lk["label_guard_ok"],
        truncation_ok=lk["truncation_ok"],
        recompute_ok=lk["recompute_ok"],
        shift_ok=lk["shift_ok"],
    )
    assert cls == "fast_causal"


def test_classifier_shapes():
    kw = {"label_guard_ok": True, "truncation_ok": True, "recompute_ok": True}
    assert classify_time_shift_failure(0.1, [0.06, 0.03, 0.01], **kw) == "fast_causal"
    assert classify_time_shift_failure(0.1, [0.01, 0.0, 0.0], **kw) == "leak_suspect"  # cliff
    assert classify_time_shift_failure(0.1, [0.05, -0.03], **kw) == "leak_suspect"  # flip
    assert classify_time_shift_failure(0.1, [0.04, 0.08], **kw) == "leak_suspect"  # rises
    assert classify_time_shift_failure(-0.1, [-0.06, -0.02], **kw) == "fast_causal"
    unproven = {**kw, "recompute_ok": None}
    assert classify_time_shift_failure(0.1, [0.06], **unproven) == "causality_unproven"
    trunc_bad = {**kw, "truncation_ok": False}
    assert classify_time_shift_failure(0.1, [0.06], **trunc_bad) == "leak_suspect"
