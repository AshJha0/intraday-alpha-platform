# Learning guide: the project from three angles

Written 2026-10-27 for v2.1. This page is for someone who wants to understand the project well
enough to explain it, defend its choices and admit its gaps: a student reading it to learn, or an
engineer deciding whether to trust it. It adds no results. Every number is copied from
[REPORT.md](REPORT.md) or the [v2.0.0 release notes](release/v2.0.0_RELEASE_NOTES.md), and the
technical drills live in the four one-pagers it points to.

Each angle has three parts: the project in two minutes, the project in ten minutes, and the
hardest questions about the project as a whole with honest answers.

## Shared core: what to say whatever the angle

**The question.** Does a systematic US-equity signal make money after costs, and how would we know?

**The answer so far.** No signal is confirmed. Where we found prediction, it was smaller than the
cost of trading it. The free daily data failed its survivorship test, so the daily question could
not be asked honestly on it. Nothing on `main` is above *development-supported*
([REPORT.md](REPORT.md) section 2).

**The point of the project.** The value is in the instrument, not in a return: data that cannot
see the future, a ledger of every look (6,826 over 640 configurations), tests registered before
the data is read, a cost model that reconciles to cash, and seven evidence classes on every claim.

**The story worth telling.** On 2026-10-25 and 26 the free daily ingestion (DD1) failed several
times before it ran end to end, and when it did, the data itself failed: 315 of 372 index leavers
were missing (85 % against a 2 % limit). The fixes made the job probe first, before the long walk.
The larger lesson is about order: we built custody and signing before testing whether the data
could support a registered run ([REPORT.md](REPORT.md) sections 6 and 7).

## Angle 1: quant research

### In two minutes

We built a research platform whose main job is to stop us fooling ourselves. Every experiment is
counted in a ledger, so with 640 configurations tried, a single result has to clear |t| of about
4.5, not 2. Intraday, our order-flow signals do predict one-second returns on holdout ITCH
sessions (IC +0.0838 and +0.0409), but the edge is a fraction of a basis point against a round
trip of about a basis point, so a taker never trades and quoting loses to adverse selection. Daily,
the free data lost 85 % of the companies that left the index, so a reversal backtest on it would
mostly measure recoveries. We report all of that as negative results, with their classes, rather
than tuning until something passes.

### In ten minutes

1. **The question and the bar** (two minutes): evidence classes, why only a registered single-shot
   test on unread data can confirm ([REPORT.md](REPORT.md) section 3.1, [LEARN.md](LEARN.md)).
2. **Selection accounting** (two minutes): the ledger, the expected maximum |t| under the null,
   deflated Sharpe and PBO in the E5 report ([QUANT_ONE_PAGER.md](QUANT_ONE_PAGER.md)).
3. **Leakage** (one minute): point-in-time membership, event-time labels, purged walk-forward,
   canaries that must be caught ([RESEARCH_VALIDITY.md](RESEARCH_VALIDITY.md)).
4. **The intraday result** (two minutes): statistically clear, economically zero; the four ways
   it failed to pay ([REPORT.md](REPORT.md) section 2, rows 5 to 8).
5. **The daily result** (two minutes): DD1, survivorship, and why switching vendor would not pass
   either (27 % of leavers missing on the Tiingo check).
6. **What would change the conclusion** (one minute): a survivorship-clean point-in-time source,
   and someone other than the developer running the registered test.

### The hardest questions

**After all this, you found nothing. Was it a waste?**
Not as engineering, and not as a finding. Most of the negative results are informative: they say
where the cost wall sits for this kind of signal at this frequency. The waste, if any, was in the
order of work: the custody machinery came before a cheap survivorship probe that would have shown
the daily route could not be used.

**Your intraday t statistics are above 13. Why not call that alpha?**
Because alpha means money after costs, and at 1x costs the net P&L was zero. The statistic says
the signal is not noise. It says nothing about whether it pays. The R7 verdict was later re-issued
as data-ineligible under the stricter v1.18 rule, and we kept both records.

**How do you know your ledger is complete?**
We do not claim it is. It is a lower bound, and its known gaps are listed in
[research/v118/LEDGER_INVENTORY.md](../research/v118/LEDGER_INVENTORY.md). A lower bound still
makes the bar stricter than ignoring the count.

**Isn't a 2 % survivorship limit arbitrary?**
It is a choice, written down before the data was read and not relaxed afterwards. At 85 % missing,
the conclusion does not depend on where between 2 % and 20 % the line sits.

**Why not buy clean data?**
It is a spend decision for the owner, and v2.1 is scoped to legibility, not new research. The
report says plainly that a clean source would change rows 1 to 3 ([REPORT.md](REPORT.md) section 8).

**What would you do with one more month?**
Get a point-in-time daily source, rerun DD1 on it, and run one registered D1 test with someone
else holding the sealed data. That is the only path to the top two evidence classes.

Technical drills: [QUANT_ONE_PAGER.md](QUANT_ONE_PAGER.md) (overfitting, multiple testing,
survivorship, look-ahead, costs, deflated Sharpe) and [AI_ML_ONE_PAGER.md](AI_ML_ONE_PAGER.md).

## Angle 2: C++ and low latency

### In two minutes

Python defines every rule. C++ owns the latency path and Rust the risk engine, and golden vectors
produced by the Python reference make the copies agree byte for byte: 217, 74 and 75 golden tests
in the three languages on the v1.18.0 CI run. On a shared CI runner, in process, the C++ path from
a decoded frame to an order intent measures p50 1,279 ns and p99 2,559 ns. Those are not
colocation numbers and there is no latency edge in them; the point is a hot path we can measure
and keep equal to a slow reference we trust.

### In ten minutes

1. **Why three languages** (two minutes): one semantics, three implementations, golden vectors as
   the contract ([POLYGLOT.md](POLYGLOT.md)).
2. **The hot path** (three minutes): data layout, allocation, branches, what is deliberately not
   on the path ([HOT_PATH.md](HOT_PATH.md)).
3. **Measuring it honestly** (two minutes): fixed seed, warm-up, percentiles, stated hardware,
   what a shared runner cannot tell you
   ([benchmarks/results_tick_to_trade.md](../benchmarks/results_tick_to_trade.md)).
4. **Determinism** (two minutes): the canonical event stream, run-twice and replay digests
   ([MVP.md](MVP.md)).
5. **What we froze and why** (one minute): the polyglot policy freezes duplicated copies so the
   reference can move without three rewrites.

### The hardest questions

**Three languages for one person is over-engineering. Why do it?**
Partly to learn, and we say so. The defensible part is the golden-vector discipline: it is cheap
to keep once built, and it is the only way to prove a fast copy implements the rule you tested.
The cost is real: duplicated copies are frozen by policy, so most changes land in Python only.

**Your latency numbers come from a shared CI runner. What are they worth?**
They show order of magnitude and regressions between commits, measured the same way each time.
They do not show what a pinned core on a quiet host would do, and they say nothing about network,
NIC or venue round trips.

**Where would you look first to make it faster?**
Allocation and cache behaviour on the book update and feature path, measured, not guessed
([HOT_PATH.md](HOT_PATH.md) sections 3 and 4). Not until a profile says so.

**Why not lock-free queues and kernel bypass?**
There is no live venue, so there is nothing to bypass to. Building that machinery without a
measurable need would repeat the governance mistake in a different place.

**How do you know the C++ copy is correct?**
It reproduces the Python reference's golden outputs exactly for integers and bytes and to 1e-9
for floats, in CI, on every per-PR path that touches it.

Technical drills: [CPP_ONE_PAGER.md](CPP_ONE_PAGER.md) (cache, false sharing, memory order,
allocation, golden vectors) and [ALGO_SOR_ONE_PAGER.md](ALGO_SOR_ONE_PAGER.md) (schedules,
routing, queue position, adverse selection).

## Angle 3: engineering leadership

### In two minutes

This is a small, AI-assisted project run like a regulated one: signed pre-registrations, sealed
data under separate operating-system accounts, a registered runner, an outside reviewer and a
hashed release manifest. All of it works and is tested. The honest lesson is about sequencing:
the first registered run never happened, because the free data failed its first quality gate,
and a probe of that data in the first week would have told us months earlier. In v2.1 we stopped
adding controls, cut the documentation from about 140 top-level pages to about 60, removed nine
import cycles and made the project legible from three entry points.

### In ten minutes

1. **What was built and why** (two minutes): the evidence chain from data to claim
   ([GOVERNANCE.md](GOVERNANCE.md), [ARCHITECTURE_v2.md](ARCHITECTURE_v2.md)).
2. **Where we over-invested** (two minutes): custody before data validation
   ([REPORT.md](REPORT.md) section 6).
3. **The DD1 incident** (two minutes): the failure chain, the probe-first fix, and the plain
   process failures, such as a pushed test that hung on a clock that never advanced
   ([REPORT.md](REPORT.md) section 7).
4. **Working with AI agents** (two minutes): parallel sessions, the LLM boundary (the model never
   computes a number, spend capped), what still needs a human ([LLM_SAFETY.md](LLM_SAFETY.md)).
5. **Streamlining** (two minutes): v2.1 Track 0, and the two times our own repo-wide rewrite broke
   hash-pinned and frozen files, caught by CI.

### The hardest questions

**If you were starting again, what would you cut?**
The custody and signing layers would wait until a dataset had passed its quality gate. Keep the
ledger, the evidence classes and the cost reconciliation from day one; they are cheap and they
shaped every honest result.

**How did you decide when to stop?**
When the remaining open gates could only be closed by things outside the project: clean data,
separate custody, an independent reproduction. Adding more controls would not close any of them.

**The code was written with AI assistance. How do you trust it?**
The same way as any code: tests, golden vectors, CI that re-derives headline numbers and fails on
a stale claim, and a human who merges and signs. The model never computes a reported number.

**What went wrong that you would not want repeated?**
Estimates in the wrong unit for months, pushes before local tests finished, and a repo-wide text
rewrite that edited hash-pinned data. Each now has a written rule.

**What does the project prove about you, as opposed to the market?**
That we can build a measurement instrument, write down a bar before looking, and report the
negative answer it gives without softening it.

Technical drills: [AI_ML_ONE_PAGER.md](AI_ML_ONE_PAGER.md) and [LLM_SAFETY.md](LLM_SAFETY.md).
