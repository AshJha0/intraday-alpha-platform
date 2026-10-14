"""Forward paper trade, as infrastructure (v1.16 plan item 9).

``python -m iap.paper.daily --date YYYY-MM-DD`` runs the current rule on one
new real session through the MVP loop and writes a dated report directory:
the MVP runs, a live-vs-backtest TCA comparison (:mod:`iap.paper.tca_compare`),
drift and alpha-decay monitors (:mod:`iap.paper.monitors`) and a dry
kill-switch drill (:mod:`iap.paper.drill`).  Runbook:
docs/runbooks/RUNBOOK_paper_trade.md.

Infrastructure, not a money claim: the v1.16 in-sample study found EQ10 /
EQ05 never clear taker costs, so the daily P&L is reported, never promoted.
"""
