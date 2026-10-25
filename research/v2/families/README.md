# U3 family specifications (v2.0 stage 7)

One file per family (U0 section 3 "Stage 7"; plan sections 4 and 15.2; G4 checklist in
`docs/review/DATA_COVERAGE_FREE_ROUTE.md` section 4). Every file holds, in this order: hypothesis, mechanism, artefacts
that could fake it, falsifier, data gate and G4 outcome, label definition (forward factor-adjusted return in bps at
the horizon, purged for overlap), signal and eligibility rule, development diagnostics (the E5 block list of E0
section 13), registration (family size K and the U13 profile), and a machine-readable JSON block that
`tools/research/run_family.py` (`iap.research.loop`) reads. Rules: each file is complete before any fit; a family with
`fit_allowed: false` (failed or ineligible gate) has no development run and the loop refuses it; a change is a dated
revision appended to the file, never an edit of the frozen sections. `python/tests/research/test_family_specs.py`
checks the sections, the block and this index.

| file | family | class | evidence class | gate outcome | fit allowed | horizons | K | profile |
|---|---|---|---|---|---|---|---|---|
| [D1_short_term_reversal.md](D1_short_term_reversal.md) | D1 short-term reversal | daily | pending | conditional (DD1 survivorship, split-day, calendar, membership-quality tests) | yes (synthetic fixture now; daily route after DD1) | 1d, 3d, 5d | 3 | PF-1 |
| [D2_residual_momentum.md](D2_residual_momentum.md) | D2 residual momentum, skip month | daily | pending | conditional (as D1; 13 months history; factor lag) | yes | 21d | 1 | PF-2 |
| [D3_overnight_asymmetry.md](D3_overnight_asymmetry.md) | D3 overnight asymmetry | daily | pending | conditional (open convention and corporate actions verified in DD1) | yes | c2o | 1 | PF-1 |
| [D4_filing_date_drift.md](D4_filing_date_drift.md) | D4 filing-date drift (EDGAR 8-K / 10-Q; no consensus surprise) | daily | pending | conditional (owner-side EDGAR coverage run, D-43; 60-session retention) | yes | 5d, 20d, 60d | 3 | PF-2 |
| [D5_close_pressure.md](D5_close_pressure.md) | D5 close pressure / rebalance flows | daily | data-ineligible | ineligible on daily data (G4) | no | c2o | 1 | PF-1 |
| [H2_intraday_momentum.md](H2_intraday_momentum.md) | H2 intraday momentum | intraday | pending | gated on E3 (IEX HIST breadth) | no (until E3 green) | 30m, 60m | 2 | PF-1 |
| [H3_etf_lead_lag.md](H3_etf_lead_lag.md) | H3 ETF-constituent lead-lag (feasibility note) | intraday | data-ineligible | ineligible (no sub-second synchronised quotes) | no | 1m, 2m, 5m | 3 | PF-1 |
| [H4_order_flow_imbalance.md](H4_order_flow_imbalance.md) | H4 order-flow imbalance (feasibility note) | intraday | data-ineligible | ineligible (no order-level data, DR-6) | no | 1m, 2m, 5m | 3 | PF-1 |

Evidence classes (U0 section 10): independently confirmed, mechanically isolated, development-supported, exploratory,
rejected, data-ineligible, pending. A family moves from pending to development-supported, exploratory or rejected only
through an E5 report produced by the research loop on the development partition and recorded in the selection
ledger; to the two confirmed classes only through a CR1 run.

H1 (residual reversal, Track A) is frozen in `research/v119/H1_ALPHA_IMPROVEMENT_SPEC.md` and is not re-specified
here.
