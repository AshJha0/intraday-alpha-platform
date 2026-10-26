# Intraday Alpha Platform: one-page summary

*As of 2026-10-26, v2.0.0. The long form is [docs/REPORT.md](docs/REPORT.md).*

**Question.** Does a systematic US-equity signal make money after costs, and how would we know?

**Answer so far.** No signal is confirmed. Where we found prediction, it was smaller than the
cost of trading it. The free daily data failed its survivorship test (85 % of index leavers
missing against a 2 % limit), so the daily question could not be asked honestly on it.
Nothing on main is above *development-supported*.

**What was built** (tested in CI, synthetic and development fixtures):

- An event-driven pipeline from raw ITCH, IEX and MBO feeds through book reconstruction,
  205 registered features, 24 alphas, portfolio construction, a fail-closed risk engine,
  execution algorithms, smart order routing, a fill simulator and TCA. One command runs the
  whole loop on one instrument and reproduces its trace digest.
- Python as the reference, with C++ (latency path) and Rust (risk engine) held equal to it
  by golden vectors: 217, 74 and 75 golden tests on the v1.18.0 CI run.
- Evidence machinery: a ledger of every look (6,826 over 640 configurations), signed
  pre-registrations, sealed test data, leakage canaries, a cost waterfall that reconciles to
  cash, and seven evidence classes on every claim.

**Measured** (shared CI hardware, in-process, not colocation): C++ tick-to-trade p50 1,279 ns,
p99 2,559 ns on an AMD EPYC 7763 runner without core pinning.

**Main negative results.**

| result | class |
|---|---|
| free daily route fails all five DD1 data tests | rejected |
| intraday EQ10 / EQ05: holdout IC +0.084 / +0.041, net P&L zero at 1x costs | data-ineligible |
| taker on 480 cells: no trade, edge 0.1 to 0.2 bp against 0.8 to 1.2 bp cost | rejected |
| two-sided quoting: loses USD 5.0k to 6.9k per session | rejected |
| known-null calibration: about 4.3 % against a 2.5 % target | rejected |

**What we would change.** We built custody and signing controls before testing whether the
data could support a registered run. A first-week survivorship probe would have found the
DD1 failure months earlier.

**Where to start.** Quant research: [docs/REPORT.md](docs/REPORT.md) sections 2 and 3.
C++ and latency: [docs/POLYGLOT.md](docs/POLYGLOT.md) and
[benchmarks/results_tick_to_trade.md](benchmarks/results_tick_to_trade.md).
Engineering leadership: [docs/REPORT.md](docs/REPORT.md) sections 6 and 7.
