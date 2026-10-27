# Exploratory D1-D4 rerun after the D-106 and D-107 fixes (2026-10-27)

**Evidence class: exploratory, data-ineligible (U0 DR-19).** Same data, same limits as
[DD1_2026_EXPLORATORY_RUN_2026-10-26.md](DD1_2026_EXPLORATORY_RUN_2026-10-26.md) (the "first run"): the DD1 2026
record failed all five data tests and holds mostly survivors. Nothing below is a finding, a frozen candidate or a
registration.

## What was run

`tools/research/dd1_chain.py --dataset <dev-root>/dd1_2026 --exploratory --ml-family ""` on branch
`v21-track1-quant` (base main `5fb22ad2`), own ledger under the run's working directory. Development partition
2006-11-07 to 2022-12-30; 1,626 trading sessions, as in the first run. Wall clock per family: D1 856 s, D2 341 s,
D3 367 s, D4 346 s (the machine was running a test suite alongside, so these are not comparable with the first
run's timings).

## Why D2 and D4 never traded in the first run

The defect register's suspects (D2's 253-session lookback, one sector per name, too few filings) were not the
cause. Reproduced on the same panel:

* D2: the M0 fit on the estimation window gave rho = -7.71 bps per unit z (se 1.03, n = 670,385), a reversal, not
  momentum. M0's quality rule (t >= 2 with rho > 0) failed, so M0 forecast nothing and every rebalance session was
  refused. The refusal was reported as "fewer than 10 names per leg (0 eligible after the hurdle)", which hid the
  real reason.
* D4: M0 also failed (rho = +3.44, se 5.55, n = 10,450), and the registered signal is finite only on the session
  after a filing: a median of 2 names per trading session (max 61). With 10 names per leg and tails of 0.5, a
  cohort needs at least 20 eligible names, so even a passing model would almost never trade.

## The fixes (exploratory path only)

* A failed M0 is now refused by name: "no forecast: the incumbent model failed its quality rule"
  (`iap.research.loop.run_engine`). This applies to every run.
* With `exploratory=True` only: a failed M0 is replaced by `M0.exploratory_ungated`, which trades the family's
  registered direction (positive signal is long) at the fitted magnitude |rho|. The registered hypothesis is costed
  even when the estimation window rejects it.
* With `exploratory=True` only: D4 uses `d4.filing_reaction_z_live5`, the latest filing reaction held for five
  sessions, so a session's cohort pools the week's filers. Contracted and causality-tested like every other
  feature (`python/tests/research/test_family_features.py`).
* Both substitutions are written into the report identity (`identity.exploratory`). The family specification
  files are unchanged.

## Results (conservative cost column, net bps per session on registered capital)

| family | model | sessions traded | cohorts | fills | screen mean (bps) | standard error (bps) | report sha256 |
|---|---|---|---|---|---|---|---|
| D1 short-term reversal | M0 (passed) | 681 | 345 | 19,642 | -0.015 | 0.467 | `48136fa2ad85840c27db2cc34553644591dd169cea4f7be98818e7bae34fa968` |
| D2 residual momentum | M0 ungated | 738 | 34 | 5,047 | -0.607 | 0.837 | `d6faa15ffd70477fd40c39a645886eeae2c384d810bc3561d994230a38e20f81` |
| D3 overnight / close-to-open | M0 (passed) | 801 | 682 | 42,360 | -0.699 | 0.248 | `5e22399723920ea6c751e1b56568295a5e806ae11cecd4d19d00b33b613eb0c9` |
| D4 filing-date drift (live 5) | M0 ungated | 802 | 291 | 25,186 | +0.187 | 0.145 | `25068c0dce2e5140c6dde6190fec49259d2e82c5888fdbdf4f66fed9ffb850b5` |

D1 and D3 reproduce the first run byte for byte: removing `identity.exploratory` from these reports gives the
first run's sha256 (`ac8a4db5...` and `ff91d126...`). Their gross-to-net breakdown is in
[QUANT_ONE_PAGER.md](../../QUANT_ONE_PAGER.md).

## How to read it

In plain words: once D2 and D4 trade, D2 loses money before costs and D4 makes a little, not enough to count.
None of the four screens passed.

Formally: D2's gross (midpoint reference) was -0.547 bps per session, so its loss is the signal, not costs; that
matches its estimation-window fit, which had the opposite sign to the hypothesis. D4's net of +0.187 bps is 1.3
standard errors above zero, with a 95 % interval of -0.098 to +0.472 bps; the economic screen's hurdle is 1.0 bps,
so it failed. D4's turnover was 0.018 of C per session, so costs were small (0.011 bps per session).

Worked example, D4: gross 0.198 bps, impact 0.009, fees 0.002, net 0.187. It comes from a survivor-biased record and
from a variant chosen after the registered signal failed to trade, so it carries one undisclosed look on top of
the ledger's. It is a reason to wire in the 8-K Item 2.02 calendar (D-95), not a result.
