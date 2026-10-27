"""A flagship alpha's scores as an execution signal source (v1.17 WS-B item B2, opt-in).

Moved from :mod:`iap.execution.signal` in v2.1 Track 3, unchanged: it builds and scores alphas,
and ``iap.alpha`` sits above ``iap.execution`` (the H1 client and the combined book use
``iap.execution.sim``), so keeping it in the execution package made the two packages import each
other.  The reading semantics (signal units, causal ``at``) are documented in
:mod:`iap.execution.signal`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from iap import io as iap_io
from iap.execution.signal import ArraySignalSource, horizon_seconds, zscore

__all__ = ["AlphaUrgencySource"]


class AlphaUrgencySource(ArraySignalSource):
    """A flagship alpha's scores on a dataset's feature frames, in signal units.

    ``frames`` maps instrument_id to the feature frame the alpha reads
    (``exchange_ts`` + feature columns, as written by ``iap.features``); the
    alpha scores every row, the scores are standardised per instrument to
    ``z`` (mean / std over the finite scored rows of THAT instrument on THIS
    dataset — an in-sample scale, recorded in :meth:`describe`), and each
    row becomes one reading at its ``exchange_ts``.  Instruments outside the
    alpha's universe (EQ10 excludes the ETF) get no readings: every decision
    on them is ``no_signal``.

    ``refit=True`` fits the alpha on the same frames first (in-sample; the
    label columns must be present) instead of using the registered
    parameters — a research variant, labelled ``params="refit:in-sample"``
    and never the gate's arm.
    """

    def __init__(
        self,
        alpha,
        frames: Mapping[int, object],
        *,
        params_label: str = "registered",
    ) -> None:
        import numpy as np

        scored = alpha.score(frames)
        readings: dict[int, list[tuple[int, float, float]]] = {}
        self.scale: dict[int, dict[str, float]] = {}
        for iid, df in scored.items():
            ts = df["exchange_ts"].to_numpy(dtype="int64")
            er = df["expected_return"].to_numpy(dtype=float)
            order = np.argsort(ts, kind="stable")
            ts, er = ts[order], er[order]
            keep = np.ones(len(ts), dtype=bool)
            keep[1:] = ts[1:] > ts[:-1]  # one reading per timestamp: the last row wins
            # (the last of equal timestamps is the latest state): keep the last
            last = np.flatnonzero(keep)
            ends = np.append(last[1:], len(ts)) - 1
            ts, er = ts[ends], er[ends]
            z, mean, std = zscore(er.tolist())
            self.scale[int(iid)] = {"mean": mean, "std": std, "rows": int(len(ts))}
            readings[int(iid)] = list(zip(ts.tolist(), z, er.tolist(), strict=True))
        super().__init__(
            readings,
            horizon_s=horizon_seconds(alpha.horizon),
            label=f"{alpha.alpha_id}:{params_label}",
        )
        self.alpha_id = alpha.alpha_id
        self.horizon = alpha.horizon
        self.params_label = params_label
        self.beta = float(getattr(alpha, "beta", float("nan")))
        self.features_dir: str | None = None

    @classmethod
    def from_dataset(
        cls,
        alpha_id: str,
        features_dir: str | Path,
        *,
        params_path: str | Path | None = None,
        instruments_json: str | Path | None = None,
        refit: bool = False,
        instrument_ids: Sequence[int] | None = None,
    ) -> AlphaUrgencySource:
        """Score ``alpha_id`` over ``features_dir/features_*.parquet``.

        ``params_path`` defaults to the repository's registered
        ``configs/strategies/alpha_params.json`` (its ``feature_version`` must
        match the running registry, as every loader demands).
        ``instruments_json`` re-derives the alpha universe from the dataset's
        instruments (ETF detection); ``refit`` fits on the frames instead.
        """
        import pandas as pd

        from iap.alpha import build, configure_universe, load_params_file

        features_dir = Path(features_dir)
        files = sorted(features_dir.glob("features_*.parquet"))
        if not files:
            raise FileNotFoundError(f"no features_*.parquet under {features_dir}")
        alpha = build(alpha_id)
        if instruments_json is not None:
            configure_universe(instruments_json)
        cols = ["instrument_id", "exchange_ts", *alpha.features]
        if refit:
            cols += [f"label_mid_{alpha.horizon}", f"label_valid_{alpha.horizon}"]
        frames: dict[int, pd.DataFrame] = {}
        for f in files:
            df = iap_io.read_parquet(f, columns=cols, purpose="execution.signal")
            for iid, part in df.groupby("instrument_id", sort=True):
                iid = int(iid)
                if instrument_ids is not None and iid not in instrument_ids:
                    continue
                part = part.drop(columns=["instrument_id"]).reset_index(drop=True)
                frames[iid] = (
                    part if iid not in frames else pd.concat([frames[iid], part], ignore_index=True)
                )
        for iid in frames:
            frames[iid] = (
                frames[iid].sort_values("exchange_ts", kind="stable").reset_index(drop=True)
            )
        if refit:
            alpha.fit(frames)
            label = "refit:in-sample"
        else:
            if params_path is None:
                params_path = Path(__file__).resolve().parents[4] / (
                    "configs/strategies/alpha_params.json"
                )
            models = load_params_file(params_path)
            if alpha_id not in models:
                raise ValueError(f"{params_path}: no parameters for {alpha_id}")
            alpha = models[alpha_id]
            label = f"registered:{Path(params_path).name}"
        src = cls(alpha, frames, params_label=label)
        src.features_dir = str(features_dir)
        return src

    def describe(self) -> dict:
        d = super().describe()
        d.update(
            {
                "alpha_id": self.alpha_id,
                "horizon": self.horizon,
                "params": self.params_label,
                "beta": self.beta,
                "units": "z-score of the alpha's expected_return per instrument on this dataset",
                "scale": {str(k): v for k, v in sorted(self.scale.items())},
                "features_dir": self.features_dir,
            }
        )
        return d
