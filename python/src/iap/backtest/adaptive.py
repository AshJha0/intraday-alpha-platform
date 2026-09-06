"""Adaptive walk-forward deployment backtest (spec §20 steps 12-13).

Replays the sample as a *deployment*: the alpha is fitted once on a warmup
window, then walks forward block by block; at every block boundary the
drift monitors and the rolling realized IC are evaluated, the refit policy
decides whether to re-fit on the trailing window, and the lifecycle state
machine decides whether the alpha may allocate at all.  The resulting
deployed score series is run through the standard research backtester, so
the exact accounting identity of :mod:`iap.backtest.engine` is preserved
untouched.

Round-3 pinned rules:

- the rolling-IC baseline is **out of sample**: the warmup is split, the
  model is fitted on its first ``BASELINE_FIT_FRAC`` and the baseline bucket
  ICs come from the purged held-out tail.  An in-sample baseline made
  ``ic_z`` biased negative and produced 20-40 "drift" refits per alpha in
  1.5 days that were really the IS/OOS gap;
- an evaluation whose MATURED set gained no new rows since the last counted
  evaluation is **uninformative**: it moves no lifecycle counter and cannot
  trigger a drift refit.  Six re-reads of a frozen 2-hour IC window after
  the feed goes quiet are one reading, not six consecutive breaches.

No-lookahead guarantees (enforced, not assumed):

- every (re)fit at event time T trains only on rows with
  ``ts + horizon_ns + embargo_ns < T`` inside the trailing
  ``train_window_ns`` — checked with an explicit runtime assertion that
  raises RuntimeError on violation;
- rolling IC at T uses only MATURED rows (``ts + horizon_ns <= T``): the
  label of a row is knowable only after its horizon has elapsed;
- drift monitors at T read feature/signal values of rows strictly before
  T (features are causal by the factory contract);
- scores for rows in ``[T_k, T_{k+1})`` come from the model fitted at or
  before ``T_k`` — the shift test in the suite verifies that mutating
  data after any time leaves all earlier deployed scores bit-identical.

Warmup rows never trade (confidence forced 0).  A RETIRED lifecycle state
halts allocation for the following block (scores gated to er=0, conf=0)
while *shadow* scoring continues ungated for monitoring, so the pinned
re-activation rule remains reachable.

Everything is deterministic: block boundaries are pure event-time
arithmetic, policies are pure functions, no wall-clock, no RNG.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional

import numpy as np
import pandas as pd

from iap.adaptive.drift import (
    DriftBaseline,
    ICBaseline,
    capture_baseline,
    capture_ic_baseline,
    ks_test,
    psi,
    rolling_ic_z,
)
from iap.adaptive.lifecycle import (
    ACTIVE,
    RETIRED,
    LifecycleConfig,
    LifecycleLog,
    LifecycleTracker,
)
from iap.adaptive.refit import RefitContext, RefitPolicy
from iap.backtest.engine import Backtester, BacktestResult
from iap.validation.metrics import HORIZONS_NS, ic

#: Fraction of the warmup used to FIT the model whose out-of-sample tail
#: pins the IC baseline (pinned, API_ADAPTIVE section 4).
BASELINE_FIT_FRAC = 2.0 / 3.0
#: Minimum number of NEW matured rows for an evaluation to count as new
#: evidence (pinned, API_ADAPTIVE section 6).
MIN_NEW_MATURED_ROWS = 1


@dataclass
class AdaptiveResult:
    """One adaptive deployment run (one alpha x one policy)."""

    alpha_id: str
    policy: dict                      # policy.describe()
    deploy_start: int
    n_blocks: int
    n_evals: int
    n_informative_evals: int          # evaluations with new matured evidence
    refit_events: List[dict]          # {"ts", "block", "reasons", "n_train"}
    eval_rows: List[dict]             # per-eval monitor readouts
    transitions: List[dict]           # lifecycle transitions (dicts)
    final_state: str
    drift_event_count: int            # evals with any PSI > pinned threshold
    backtest: BacktestResult
    deployed_ic: Optional[float]      # IC of gated deployed scores, matured rows
    scores: Dict[int, pd.DataFrame] = field(repr=False, default_factory=dict)

    @property
    def refit_count(self) -> int:
        return len(self.refit_events)


class AdaptiveDeployment:
    """Walk-forward deployment of one alpha over feature frames.

    Precomputes (data-only, shared across policies): the alpha's event-time
    span, block boundaries, warmup baselines (feature + signal PSI
    baselines, research IC baseline) and per-eval drift readouts.  ``run``
    then replays deployment under any refit policy.
    """

    def __init__(
        self,
        model_factory: Callable[[], object],
        frames: Mapping[int, pd.DataFrame],
        adaptive_cfg: dict,
        backtester: Backtester,
        psi_threshold: float,
    ) -> None:
        self.factory = model_factory
        probe = model_factory()
        self.alpha_id = probe.alpha_id
        self.asset_class = "FX" if probe.asset_class == "FX" else "EQUITY"
        self.horizon = probe.horizon
        self.horizon_ns = HORIZONS_NS[probe.horizon]
        self.features = tuple(probe.features)
        self.cfg = adaptive_cfg
        self.backtester = backtester
        self.psi_threshold = float(psi_threshold)

        universe = probe.universe(list(frames))
        if not universe:
            raise ValueError(f"{self.alpha_id}: empty universe for provided frames")
        self.uframes: Dict[int, pd.DataFrame] = {
            i: frames[i].reset_index(drop=True) for i in universe
        }
        self.universe = universe
        self._ts = {i: df["exchange_ts"].to_numpy(dtype=np.int64)
                    for i, df in self.uframes.items()}

        t0 = min(int(t[0]) for t in self._ts.values() if len(t))
        t1 = max(int(t[-1]) for t in self._ts.values() if len(t))
        self.t0, self.t1 = t0, t1
        self.deploy_start = t0 + int(adaptive_cfg["warmup_ns"])
        if self.deploy_start >= t1:
            raise ValueError(
                f"{self.alpha_id}: warmup_ns consumes the whole sample "
                f"({self.deploy_start} >= {t1})"
            )
        block_ns = int(adaptive_cfg["block_ns"])
        bounds = [self.deploy_start]
        while bounds[-1] + block_ns <= t1:
            bounds.append(bounds[-1] + block_ns)
        self.block_bounds = bounds  # boundary k starts block k
        self._labels = {}
        for i, df in self.uframes.items():
            lab = df[f"label_mid_{self.horizon}"].to_numpy(dtype=float).copy()
            lab[~df[f"label_valid_{self.horizon}"].to_numpy(dtype=bool)] = np.nan
            self._labels[i] = lab

        self._capture_baselines(probe)
        self._precompute_drift(probe)

    # -- baselines --------------------------------------------------------

    def _window(self, lo: int, hi: int) -> Dict[int, pd.DataFrame]:
        out = {}
        for i, df in self.uframes.items():
            m = (self._ts[i] >= lo) & (self._ts[i] < hi)
            out[i] = df[m].reset_index(drop=True)
        return out

    def _pooled_signal(self, probe, wframes: Mapping[int, pd.DataFrame]) -> np.ndarray:
        """Pooled finite raw-signal values over a window (parameter-free).
        Empty when any universe frame has no rows (cross-sectional alphas
        need the whole universe; a silent partial answer would lie)."""
        if any(len(df) == 0 for df in wframes.values()):
            return np.empty(0)
        vals = []
        for i, sig in probe.signals(wframes).items():
            v = sig.to_numpy(dtype=float)
            vals.append(v[np.isfinite(v)])
        return np.concatenate(vals) if vals else np.empty(0)

    def _pooled_feature(self, name: str, wframes: Mapping[int, pd.DataFrame]) -> np.ndarray:
        vals = []
        for df in wframes.values():
            if name in df.columns and len(df):
                v = df[name].to_numpy(dtype=float)
                vals.append(v[np.isfinite(v)])
        return np.concatenate(vals) if vals else np.empty(0)

    def _capture_baselines(self, probe) -> None:
        warm = self._window(self.t0, self.deploy_start)
        src = (
            f"warmup window [{self.t0}, {self.deploy_start}) of the bundled "
            f"feature frames, universe {self.universe}"
        )
        self.signal_baseline_sample = self._pooled_signal(probe, warm)
        self.signal_baseline: Optional[DriftBaseline] = None
        if self.signal_baseline_sample.size >= 100:
            self.signal_baseline = capture_baseline(
                self.signal_baseline_sample, "signal",
                f"run_{self.alpha_id.lower()}_signal",
                alpha_id=self.alpha_id, source=src,
            )
        self.feature_baselines: Dict[str, DriftBaseline] = {}
        self.feature_baseline_samples: Dict[str, np.ndarray] = {}
        for f in self.features:
            v = self._pooled_feature(f, warm)
            self.feature_baseline_samples[f] = v
            if v.size >= 100:
                self.feature_baselines[f] = capture_baseline(
                    v, "feature", f"run_{self.alpha_id.lower()}_feature_{f}",
                    alpha_id=self.alpha_id, source=src,
                )

        # Research IC baseline — OUT OF SAMPLE (pinned, API_ADAPTIVE section
        # 4).  The deployed model is fitted on the whole warmup, so scoring
        # it on its own warmup rows produces an in-sample baseline: ic_mean
        # is optimistic, live ic_z is biased negative and drift-triggered
        # refits fire on the IS/OOS gap instead of on drift.  Split the
        # warmup: fit on its first BASELINE_FIT_FRAC, take the baseline from
        # the held-out tail (purged the same way a fold is).
        model = self._fit_at(self.deploy_start)
        self.initial_model_params = model.params()

        split_ts = self.t0 + int(
            (self.deploy_start - self.t0) * BASELINE_FIT_FRAC
        )
        baseline_model = self._fit_at(split_ts)
        self.baseline_split_ts = split_ts
        self.baseline_model_params = baseline_model.params()
        bscores = baseline_model.score(self.uframes)
        ts_l, x_l, y_l = [], [], []
        for i in self.universe:
            t = self._ts[i]
            # held-out tail of the warmup, purged against the fit window
            m = (t >= split_ts) & (
                t + self.horizon_ns + self.embargo_ns < self.deploy_start)
            er = bscores[i]["expected_return"].to_numpy(dtype=float).copy()
            er[bscores[i]["confidence"].to_numpy(dtype=float) <= 0.0] = np.nan
            ts_l.append(t[m])
            x_l.append(er[m])
            y_l.append(self._labels[i][m])
        ts_a = np.concatenate(ts_l) if ts_l else np.empty(0, np.int64)
        x_a = np.concatenate(x_l) if x_l else np.empty(0)
        y_a = np.concatenate(y_l) if y_l else np.empty(0)
        self.ic_baseline_rows = int(np.sum(np.isfinite(x_a) & np.isfinite(y_a)))
        try:
            self.ic_baseline: Optional[ICBaseline] = capture_ic_baseline(
                ts_a, x_a, y_a,
                name=f"run_{self.alpha_id.lower()}_ic",
                alpha_id=self.alpha_id, horizon=self.horizon,
                bucket_ns=int(self.cfg["ic_bucket_ns"]),
                source=(f"{src}; OOS tail [{split_ts}, {self.deploy_start}) of "
                        f"the warmup, model fitted on its first "
                        f"{BASELINE_FIT_FRAC:.0%}"),
                min_buckets=int(self.cfg["min_ic_buckets"]),
                baseline_kind="oos",
            )
        except ValueError:
            self.ic_baseline = None  # honest: warmup too thin for an IC baseline

    @property
    def embargo_ns(self) -> int:
        return int(self.cfg["embargo_ns"])

    # -- drift readouts (data-only, shared across policies) ---------------

    def _precompute_drift(self, probe) -> None:
        monitor_ns = int(self.cfg["monitor_window_ns"])
        min_n = int(self.cfg["min_psi_samples"])
        self.drift_rows: Dict[int, dict] = {}
        for tb in self.block_bounds[1:]:
            wframes = self._window(tb - monitor_ns, tb)
            psi_by, ks_by = {}, {}
            sig = self._pooled_signal(probe, wframes)
            if self.signal_baseline is not None:
                psi_by["signal"] = psi(self.signal_baseline, sig, min_samples=min_n)
                if sig.size >= min_n and self.signal_baseline_sample.size:
                    d, p = ks_test(self.signal_baseline_sample, sig)
                    ks_by["signal"] = {"d": d, "p": p}
            for f in self.features:
                if f not in self.feature_baselines:
                    continue
                v = self._pooled_feature(f, wframes)
                psi_by[f"feature:{f}"] = psi(
                    self.feature_baselines[f], v, min_samples=min_n
                )
                if v.size >= min_n:
                    d, p = ks_test(self.feature_baseline_samples[f], v)
                    ks_by[f"feature:{f}"] = {"d": d, "p": p}
            self.drift_rows[tb] = {"psi": psi_by, "ks": ks_by}

    # -- fitting ----------------------------------------------------------

    def _train_window(self, fit_ts: int) -> Dict[int, pd.DataFrame]:
        lo = fit_ts - int(self.cfg["train_window_ns"])
        out = {}
        for i, df in self.uframes.items():
            t = self._ts[i]
            m = (t >= lo) & (t + self.horizon_ns + self.embargo_ns < fit_ts)
            out[i] = df[m].reset_index(drop=True)
        return out

    def _fit_at(self, fit_ts: int):
        """Fresh model fitted on the trailing purged window ending at fit_ts.
        Raises RuntimeError if any training row could see past fit_ts."""
        train = self._train_window(fit_ts)
        for i, df in train.items():
            if len(df):
                last = int(df["exchange_ts"].iloc[-1])
                if last + self.horizon_ns + self.embargo_ns >= fit_ts:
                    raise RuntimeError(
                        f"{self.alpha_id}: lookahead in refit at {fit_ts}: train row "
                        f"{last} + horizon + embargo reaches the fit time"
                    )
        model = self.factory()
        model.fit(train)
        return model

    # -- deployment replay -------------------------------------------------

    def run(
        self,
        policy: RefitPolicy,
        lifecycle_cfg: Optional[LifecycleConfig] = None,
        lifecycle_log: Optional[LifecycleLog] = None,
        policy_label: Optional[str] = None,
    ) -> AdaptiveResult:
        cfg = self.cfg
        ic_window_ns = int(cfg["ic_window_ns"])
        min_buckets = int(cfg["min_ic_buckets"])
        block_ns = int(cfg["block_ns"])

        model = self._fit_at(self.deploy_start)
        last_fit_ns = self.deploy_start
        refit_events: List[dict] = [{
            "ts": self.deploy_start, "block": 0,
            "reasons": ["initial deployment fit"],
            "n_train": int(model.params().get("n_train", 0)),
        }]

        # assembled (shadow, ungated) deployed scores
        er_asm = {i: np.zeros(len(self.uframes[i])) for i in self.universe}
        conf_asm = {i: np.zeros(len(self.uframes[i])) for i in self.universe}
        gate = {i: np.ones(len(self.uframes[i]), dtype=bool) for i in self.universe}

        def apply_model(from_ts: int) -> None:
            scores = model.score(self.uframes)
            for i in self.universe:
                m = self._ts[i] >= from_ts
                er_asm[i][m] = scores[i]["expected_return"].to_numpy(dtype=float)[m]
                conf_asm[i][m] = scores[i]["confidence"].to_numpy(dtype=float)[m]

        apply_model(self.deploy_start)

        tracker = LifecycleTracker(
            alpha_id=self.alpha_id,
            config=lifecycle_cfg or LifecycleConfig(-1.0, -1.0, 10**9, 1),
            policy=policy_label if policy_label is not None else policy.name,
            log=lifecycle_log,
        )

        eval_rows: List[dict] = []
        drift_event_count = 0
        last_matured = (-1, -1)   # (n matured pairs, last matured ts)
        for block_idx, tb in enumerate(self.block_bounds[1:], start=1):
            # rolling realized IC over matured rows of the trailing window
            ts_l, x_l, y_l = [], [], []
            for i in self.universe:
                t = self._ts[i]
                m = (t >= tb - ic_window_ns) & (t + self.horizon_ns <= tb)
                er = er_asm[i].copy()
                er[conf_asm[i] <= 0.0] = np.nan
                ts_l.append(t[m])
                x_l.append(er[m])
                y_l.append(self._labels[i][m])
            ts_a = np.concatenate(ts_l)
            x_a = np.concatenate(x_l)
            y_a = np.concatenate(y_l)
            if self.ic_baseline is not None:
                icw = rolling_ic_z(self.ic_baseline, ts_a, x_a, y_a, min_buckets)
                rolling_ic, ic_z, n_ic_buckets = icw.rolling_ic, icw.z, icw.n_buckets
            else:
                rolling_ic, ic_z, n_ic_buckets = None, None, 0

            # Informative evaluation (pinned, API_ADAPTIVE section 6): the
            # matured set must have gained new rows since the last COUNTED
            # evaluation.  After a feed goes quiet the IC window content is
            # frozen; re-reading it is one reading, not six breaches.
            usable = np.isfinite(x_a) & np.isfinite(y_a)
            matured = (int(usable.sum()),
                       int(ts_a[usable].max()) if usable.any() else -1)
            informative = (
                matured[0] - last_matured[0] >= MIN_NEW_MATURED_ROWS
                or matured[1] > last_matured[1]
            )
            if informative:
                last_matured = matured

            state = tracker.update(tb, rolling_ic, informative=informative)
            if state == RETIRED:
                # halt allocation for the coming block (through end-of-data
                # after the final evaluation — no eval can lift it)
                hi = tb + block_ns if tb != self.block_bounds[-1] else self.t1 + 1
                for i in self.universe:
                    t = self._ts[i]
                    gate[i][(t >= tb) & (t < hi)] = False

            drift = self.drift_rows[tb]
            psi_vals = [v for v in drift["psi"].values() if v is not None]
            psi_max = max(psi_vals) if psi_vals else None
            if psi_max is not None and psi_max > self.psi_threshold:
                drift_event_count += 1

            # An uninformative evaluation carries no new IC evidence, so it
            # cannot trigger a drift refit either (the same ic_z recomputed
            # from an unchanged matured set is not a second observation).
            ctx = RefitContext(
                now_ns=tb, last_fit_ns=last_fit_ns,
                psi_by_series=drift["psi"],
                ic_z=ic_z if informative else None,
            )
            decision = policy.should_refit(ctx)
            if decision.refit:
                model = self._fit_at(tb)
                last_fit_ns = tb
                apply_model(tb)
                refit_events.append({
                    "ts": tb, "block": block_idx,
                    "reasons": decision.reasons,
                    "n_train": int(model.params().get("n_train", 0)),
                })

            eval_rows.append({
                "ts": tb,
                "block": block_idx,
                "psi": drift["psi"],
                "psi_max": psi_max,
                "ks": drift["ks"],
                "rolling_ic": rolling_ic,
                "ic_z": ic_z,
                "n_ic_buckets": n_ic_buckets,
                "n_matured_pairs": matured[0],
                "informative": bool(informative),
                "state": state,
                "refit": bool(decision.refit),
            })

        # gated deployed scores: warmup never trades; RETIRED blocks flat
        scores_out: Dict[int, pd.DataFrame] = {}
        for i in self.universe:
            g = gate[i] & (self._ts[i] >= self.deploy_start)
            er = np.where(g, er_asm[i], 0.0)
            conf = np.where(g, conf_asm[i], 0.0)
            scores_out[i] = pd.DataFrame({
                "exchange_ts": self._ts[i],
                "expected_return": er,
                "confidence": conf,
            })

        bt = self.backtester.run(
            frames=self.uframes, scores=scores_out, asset_class=self.asset_class
        )

        # deployed OOS IC on matured post-warmup rows (gated scores)
        x_l, y_l = [], []
        for i in self.universe:
            t = self._ts[i]
            m = t >= self.deploy_start
            er = scores_out[i]["expected_return"].to_numpy(dtype=float).copy()
            er[scores_out[i]["confidence"].to_numpy(dtype=float) <= 0.0] = np.nan
            x_l.append(er[m])
            y_l.append(self._labels[i][m])
        deployed_ic = ic(np.concatenate(x_l), np.concatenate(y_l))

        return AdaptiveResult(
            alpha_id=self.alpha_id,
            policy=policy.describe(),
            deploy_start=self.deploy_start,
            n_blocks=len(self.block_bounds) - 1,
            n_evals=len(eval_rows),
            n_informative_evals=sum(1 for r in eval_rows if r["informative"]),
            refit_events=refit_events,
            eval_rows=eval_rows,
            transitions=[tr.to_dict() for tr in tracker.transitions],
            final_state=tracker.state,
            drift_event_count=drift_event_count,
            backtest=bt,
            deployed_ic=float(deployed_ic) if np.isfinite(deployed_ic) else None,
            scores=scores_out,
        )
