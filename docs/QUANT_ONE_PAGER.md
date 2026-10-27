# Why D1 and D3 failed: a one-page decomposition

**Evidence class: exploratory, data-ineligible (U0 DR-19).** Every number below comes from the exploratory run on the
DD1 2026 development partition, recorded in
[DD1_2026_EXPLORATORY_RUN_2026-10-26.md](release/evidence/DD1_2026_EXPLORATORY_RUN_2026-10-26.md) (the "run note"),
and from the E5 reports that note identifies by sha256. That data failed all five DD1 data tests and holds mostly
survivors ([DD1_FREE_ROUTE_FINDING_2026-10-26.md](release/evidence/DD1_FREE_ROUTE_FINDING_2026-10-26.md)). Nothing
here is a finding about markets; it explains where the money went in a run that is already on record.

How the breakdown numbers were read. The run note records each family's net mean, standard error, sessions traded,
fills and report sha256, but not the gross-to-net lines inside the report. Those lines were read from a rerun of
`tools/research/dd1_chain.py --exploratory` on branch `v21-track1-quant`. The rerun's D1 and D3 reports hash to the
recorded sha256 once the one identity field this branch adds (`identity.exploratory`) is removed, so they are the
recorded reports, byte for byte. The rerun and its hash check are in
[DD1_2026_EXPLORATORY_RERUN_2026-10-27.md](release/evidence/DD1_2026_EXPLORATORY_RERUN_2026-10-27.md).

## The decomposition

In plain words: a strategy's net return is what its signal earns at the mid price, minus what it pays to trade.
What it pays is how much it trades times what each unit of trading costs. If the signal earns less per unit traded
than a unit of trading costs, no amount of trading fixes it.

Formally, per session on registered capital C (E5 block E, conservative cost column):

    net = gross - cost drag,   cost drag = impact + fees (+ half spread, zero in these runs)
    gross per unit traded = gross / turnover,   cost per unit traded = cost drag / turnover

where gross is the midpoint reference P&L and turnover is the traded notional as a fraction of C per session
(E5 block B).

| family | turnover (share of C per session) | gross (bps) | impact (bps) | fees (bps) | net (bps) | gross per unit traded (bps) | cost per unit traded (bps) | report sha256 |
|---|---|---|---|---|---|---|---|---|
| D1 short-term reversal | 0.1174 | +0.130 | 0.136 | 0.009 | -0.015 | 1.11 | 1.24 | `ac8a4db5...` |
| D3 overnight / close-to-open | 0.7153 | +0.539 | 1.185 | 0.054 | -0.699 | 0.75 | 1.73 | `ff91d126...` |

Sources: net, sessions and sha256 from the run note; gross, impact, fees (E5 block E) and turnover (E5 block B) from
the reports with those sha256. The two per-unit columns are this note's arithmetic: gross or (impact + fees)
divided by turnover. D3's net also carries a +0.0001 bps execution residual.

Worked example, D1: the book traded 0.117 of C per session and earned +0.130 bps of C at the mid, which is
1.11 bps per unit traded. Each unit traded cost 1.24 bps in impact and fees. The signal paid for its own
trading almost exactly and no more, so net is -0.015 bps, 0.03 standard errors from zero: no edge, and no loss
worth the name either.

Worked example, D3: the signal was there before costs. Gross was +0.539 bps of C per session, about four times
D1's. But D3 enters at the close and exits at the next open, so it replaces most of its book every session:
turnover 0.715 of C, six times D1's. Each unit traded earned 0.75 bps and cost 1.73 bps, so costs took 2.3 times
the gross and net was -0.699 bps, 2.8 standard errors below zero (run note). D3 failed on cost per unit traded,
not on the sign of its signal.

## What this says, and what it does not

* Both families were cost-aware by construction: a name enters only when its forecast exceeds 1.5 times its
  round-trip cost (the hurdle). D1 still traded on 681 of 1,626 sessions (run note). The hurdle filters names on
  the model's forecast; the forecast was too optimistic about the gross that arrived.
* Impact is most of the cost in both runs (the half spread line is zero in the daily auction model; fees are
  small). The impact model is the platform's own daily auction model, not a calibrated one, so the split between
  gross and cost is only as good as that model.
* The data hold mostly survivors, which favours long-run winners. A real edge would be expected to look better
  here, not worse.

## Questions to check your understanding

**What is overfitting in a backtest, and how do you see it?**
Plain idea: the rule learned the sample's noise, so it looks good on the data it was chosen on and not elsewhere.
Formally, the in-sample winner of a search is biased upwards; the bias grows with the number of configurations
tried and shrinks with sample length. You see it as a gap between in-sample and out-of-sample performance that is
systematic across splits, which is what PBO measures. Example: the synthetic notebook
([signal_to_net.ipynb](../research/notebooks/signal_to_net.ipynb)) sweeps eight lookbacks and reports PBO by CSCV
on the sweep.

**What is the probability of backtest overfitting (PBO)?**
Cut the session series into S blocks; for every way of choosing half the blocks as in-sample, find the in-sample
best configuration and see where it ranks out of sample. PBO is the share of those splits where it ranks at or
below the out-of-sample median (Bailey, Borwein, Lopez de Prado and Zhu 2017). Pure noise gives about 0.5; a
genuinely best configuration gives about 0. Worked example with two candidates and four blocks, checkable by hand:
`python/tests/reports/test_e5_overfitting.py::test_pbo_counts_the_splits_where_the_in_sample_winner_loses` (PBO =
4/6). Implementation: `iap.validation.cpcv.probability_of_backtest_overfitting`, reported by E5 block F
(`iap.reports.block_f_overfitting`).

**What is the deflated Sharpe ratio?**
Plain idea: the best of many tries looks good by luck, so judge it against what luck alone would give. Formally,
DSR is the probabilistic Sharpe ratio evaluated at SR0, the expected maximum Sharpe of N skill-less trials, with
the standard error widened for skew and fat tails (Bailey and Lopez de Prado 2014):
DSR = Phi((SR - SR0) sqrt(T - 1) / sqrt(1 - skew SR + (kurt - 1)/4 SR^2)). Worked example: ten sessions
alternating 0 and 1 bps give SR = 0.949 per session and, with one trial, DSR = Phi(2.846) = 0.998; with two trials
SR0 = 0.173 and DSR falls (`test_e5_overfitting.py`, first two tests). N comes from the selection ledger, never
from the report's author.

**How do you handle multiple testing across many signals?**
Count every look, not only the ones that made it into the write-up, and correct for the count. This repository
records every development run in a selection ledger and uses Holm over the registered family for confirmatory
claims (defect register D-01, D-02, D-08 in
[DEFECT_REGISTER_v1.19.md](review/DEFECT_REGISTER_v1.19.md)). For a single selected strategy the deflated Sharpe
ratio is the same idea in one number.

**What is survivorship bias, and does it touch these results?**
A dataset that drops delisted names keeps the winners, so long-run returns look better than they were. The DD1 2026
price history lacks most delisted names (free-route finding). It biases towards a positive result; D1 and D3 came
out at or below zero anyway.

**What is look-ahead, and how is it prevented here?**
Using information at a time it was not yet known. Every feature is declared with an availability rule (a close is
usable the session after, lag 1) and checked by a planted-leak test that perturbs later rows and requires the
feature not to move (`iap.data.contracts.check_causality`, run on every family feature by
`python/tests/research/test_family_features.py`). Filings are usable from the session after their acceptance date
(D4 specification).

**Why do costs matter more than the signal for short-horizon strategies?**
Cost drag is turnover times unit cost; a short horizon means high turnover, so a small unit cost becomes a large
drag. The break-even question is gross per unit traded against cost per unit traded, the two columns of the table
above.

**What would you change first?**
Fewer, larger trades: a lower turnover version of the same signal (a longer holding period or a no-trade band)
keeps more of the gross per unit traded. That is a new hypothesis and would need its own registration; it is not
a reading of this run.
