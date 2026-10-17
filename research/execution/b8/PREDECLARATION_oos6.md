# B8 six-day re-run: pre-declaration (written before any six-day result exists)

Date: 2026-10-17. Status: declared before the run. The two earlier B8 reports
(`REPORT_overlap2.md`, `REPORT_sweep.md`) used 2 days and calibrated in-sample;
they stay as they are.

## Days

Nasdaq / IEX pairs: 2025-11-28, 2025-12-08, 2025-12-09, 2025-12-10 (`data/real/r7_<d>`
and `data/real/iex/iex_<d>`), 2026-05-15 and 2026-05-18 (`data/real/holdout2026`,
one dataset holding both sessions, selected by date label; `data/real/iex/iex_<d>`).

## Headline (out of sample)

- Calibrate on 2025-11-28, 2025-12-08, 2025-12-09 only
  (`two_venue calibrate` -> `research/execution/b8/calibration_train3/`).
- Evaluate on 2025-12-10, 2026-05-15, 2026-05-18 (3 evaluation days, 2 regimes,
  Dec 2025 and May 2026) with that calibration.
- Policies: `pin_nasdaq`, `pin_iex`, `sor`, `sor_sweep`; baseline `pin_nasdaq`.
- Symbols: AAPL and MSFT, one process at a time. QQQ only if the machine is idle.
- Statistic: paired shortfall difference vs `pin_nasdaq`, day-clustered mean,
  Student-t with G-1 = 2 degrees of freedom. Quote the small-sample caveat.
- Fees and venue file unchanged: `venues_xnas_iexg.json` (indicative list prices).

## Supplementary (labelled in-sample)

The pooled six-day run calibrated on all six days. It flatters the router and is
reported only beside the headline, never instead of it.

## Decision rule

A policy counts as better than pinned Nasdaq only if the headline 95 % interval
excludes zero in its favour on both symbols. Anything else is reported as no
evidence, with the sign and size. Negative results are stated first. No
parameter, route, fee or day is changed after seeing a result; a change
means a new, separately labelled run.

## Not claimed

Three evaluation days cannot support a general claim. Venue latency is zero
(exchange timestamps only). Fees are list prices.
