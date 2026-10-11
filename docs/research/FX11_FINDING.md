# FX11 time-shift failure: leak or fast signal? (v1.15, R-P1-3)

**Verdict: fast, not leak.** No look-ahead was found in FX11's pipeline.
FX11's IC is consistent with a fast causal signal, one that loses most of
its predictive power within about a minute of a proxy event. The v1.14
time-shift detector cannot make this call from its own two numbers.
Causality is established by other evidence: the raw-event recompute
probe, the truncation probe, the label guard and the line-by-line audit
below. **FX11's registry status is unchanged (RESEARCH).** A human reviews
this finding before any status change.

Scope: committed artefacts (`research/alpha_reports/FX11.json`), the
committed FX golden vector and synthetic data only. No feature store was
regenerated.

## 1. What failed

From `research/alpha_reports/FX11.json` (`leakage`, 5m horizon, median row
gap 15.35 s):

| lag of the score | IC | ratio to unlagged | detector |
|---|---|---|---|
| 0 | 0.0995 | 1.00 | |
| 1 row (about 15 s) | 0.0717 | 0.72 | row shift: needs 0.47, **passes** |
| 75 s (h/4) | 0.0048 | 0.05 | time shift: needs 0.35, **fails** |

Label guard, truncation probe and recompute probe (3 anchors on raw FX
events) all passed. The IC keeps its sign and decays. It does not fall
away at the first step.

## 2. Synthetic experiment (`python/tests/test_fx11_time_shift.py`)

The test builds a 1-second log-mid path with volatility bursts (FX11's
proxy events) and samples it every 15 s. It rebuilds FX11's two inputs
(`vol_ratio_w10s_w1m_v1`, `ret_vol_adj_1m_v1`) from the path and scores
FX11's own `raw_signal`. The tester is the production `LeakageTester`.

| world | IC0 | row shift (15 s) | time shift (75 s) | row / time detector | recompute |
|---|---|---|---|---|---|
| `leak15`: features read the path 15 s ahead | 0.040 | 0.007 | — | **fails** / passes | fails |
| `leak45`: features read the path 45 s ahead | 0.104 | 0.072 | 0.010 | passes / **fails** | fails |
| `fast`: causal; drift after a burst, tau 45 s | 0.060 | 0.037 | 0.013 | passes / **fails** | passes |
| **FX11 (committed)** | 0.0995 | 0.0717 | 0.0048 | passes / **fails** | passes |

What this shows:

- A leak confined to one row (stamping or a join off by one) would have
  failed FX11's row shift. FX11 passed it, so that class is ruled out by
  the committed numbers alone.
- A 45 s leak reproduces FX11's gate numbers almost exactly, and so does a
  fast causal signal. **The gate profile alone cannot decide.** The test
  `test_longer_leak_profile_alone_is_not_decisive` pins this limit.
- Every planted leak fails the raw-event recompute probe. It is the
  discriminating check: a feature that reads ahead differs at the end of a
  truncated prefix. FX11 passed it in the v1.14 run. In this PR, FX11's
  real pipeline (reference `FeatureEngine`, one emission per event) passes
  it at 40 anchors on the committed FX golden vector. With FX11's inputs
  shifted one row ahead, the same probe fails
  (`test_fx11_inputs_are_causal_in_the_reference_engine`).

## 3. Timestamp audit of FX11's pipeline

| step | code | finding |
|---|---|---|
| emission stamp | `features/engine.py` `_maybe_emit`: `vec = self._emit(st, t)` at the event time `t`, after the event is applied | stamped at the triggering event, never at a bar open; cadence only skips emissions |
| same-timestamp events | v1.14 coalescing (D-P1-1): one refresh, sample and row per `(instrument, exchange_ts)` | a row cannot see later events with its own timestamp |
| `rvol_w10s`, `rvol_w1m` | `features/volatility.py`, windows `(t-w, t]` | causal |
| `vol_ratio_w10s_w1m` | `rvol_10s / (rvol_1m + EPS)` | causal |
| `ret_vol_adj_1m` | `features/price.py`, `ret_log_1m / (rvol_w1m + EPS)` | causal; the return ends at the row's own mid |
| FX11 `raw_signal` | `alpha/fx.py`: rising edge `flag & ~prev` uses row i-1; `searchsorted(..., side="right") - 1` takes the latest event at or before the row | causal (the truncation probe also passed) |
| label | `labels/labels.py`: `m(t)` = latest sample with `ts <= t`; forward = latest sample with `ts <= t+h` | anchored on the same state as the features; no overlap with feature inputs |
| no forward-fill or resampling | FX11 reads per-row features. Nothing is resampled or joined across instruments. | n/a |

## 4. Why the IC is fast

FX11 trades the vol-adjusted 1-minute return at a vol-acceleration burst.
On the synthetic generator, bursts come from regime switches and clustered
flow. That kind of continuation is short-lived. Labels anchored more than
about 1 minute after the burst carry little of it, so a 75 s lag removes
most of the IC. The `fast` world reproduces the shape: ratios of 0.62
(row), 0.40, 0.29 and 0.21 across the 19 s, 38 s and 75 s profile lags.

## 5. Gate change (detector 2c, `iap.validation.leakage`)

On a time-shift failure, the result now carries:

- `time_shift_profile`: IC at lags of h/16, h/8 and h/4;
- `time_shift_class`, which takes one of these values:
  - `causality_unproven`: the recompute probe did not run;
  - `leak_suspect`: a causal probe failed, or the profile has a cliff at the
    first step (below 0.25), a sign flip or a rise;
  - `fast_causal`: otherwise.

`passed` is unchanged by default, so a time-shift failure still blocks.
`LeakageTester(accept_fast_causal=True)` is an opt-in that lets
`fast_causal` pass. Reports of alphas that pass the time shift are
byte-identical, because the new keys appear only on a failure.

## 6. Limits and what a reviewer should decide

- The recompute probe in the v1.14 run covered the first 12,000 events of
  one FX file at 3 anchors. A pipeline leak shows up on every row, so this
  is adequate for code-level look-ahead. It says nothing about data-level
  issues: a vendor timestamp that is itself late.
- FX11's real profile at h/16 and h/8 has not been measured, because that
  needs the feature frames. The next research rerun records it
  automatically (`time_shift_profile`).
- If FX11 is fast, its 5m horizon is mis-specified. A short-horizon version
  (for example 30 s to 1 m) is the honest research follow-up. Separately,
  FX11 is REJECT on costs (net PnL at 1x cost is negative, and 0 of 4 folds
  survive), so the leak question does not make it promotable.
- Decision for the human reviewer: whether to (a) leave FX11 in RESEARCH,
  (b) re-admit it as CANDIDATE with a note that it is `fast_causal`, or
  (c) turn on `accept_fast_causal` for the pipeline. This PR does none of
  these.
