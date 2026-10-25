# H1 reference implementation (v2.0 stage 5, E0 commit)

A small, direct implementation of the frozen H1 economic specification
([`../H1_ALPHA_IMPROVEMENT_SPEC.md`](../H1_ALPHA_IMPROVEMENT_SPEC.md), E0 revision 6,
sections 3-10) on committed synthetic fixtures, and the yardstick of the
H1-equivalence test (`python/tests/equivalence/test_h1_equivalence.py`): the generic
engine (`iap.alpha.forecast` / `iap.alpha.registry`, `iap.portfolio.ledger`,
`iap.portfolio.project`, `iap.execution.sim`, `iap.reports.e5`, driven by the H1 client
`iap.alpha.h1`) must reproduce this folder's research ledger, executable ledger, orders,
fills and R_d series **byte for byte**. The test stays in CI forever (plan V2-C1).

| file | what |
|---|---|
| `fixtures.py` | the synthetic panel: own SplitMix64, additions-only shocks, multiplicative prices (no transcendental function, so the bytes are platform-exact); 80 names in 4 sectors, a market ETF and 4 sector ETFs; 20 estimation sessions and 3 trading sessions of 24 five-minute bars; planted faults: a stale quote, a quote that disappears before arrival, a halt in life and a halt through the close (overnight carry), a displayed-depth collapse, missing estimation bars for one name, thin-depth and thin-volume names. Scenarios: `reversal` (kappa 0.5) and `no_reversal` (kappa 0: both horizons fail E0 section 4's quality rule and nothing trades). |
| `fixtures/manifest.json` | sha256 of every fixture array and table; re-derived by the test before anything runs |
| `reference.py` | E0 sections 3-10 written directly, importing nothing from `iap`: signal, forecast fit with the fail-closed rule, construction (own SciPy call for the repair), the two ledgers (allocation rule, crossing, `closing`), execution under the conservative column, the session value |

Arithmetic conventions the engine shares with the reference (part of what
"equivalent" means): sums of products via `numpy.dot` over arrays in (session, bar,
name) order; standard deviations with `ddof=1`; `floor(x + 0.5)` share rounding;
dictionaries iterated in sorted-name order; cash and positions updated per fill in
that order. The repair's floating-point result is not required to agree bit for bit
(the two SciPy calls assemble the constraints differently); integer share rounding
absorbs that, and every compared quantity is an integer, a timestamp, a recorded
forecast, or a price derived from integers and decision mids.

The fixture is synthetic and planted; its R_d values are mechanics, not evidence.
Nothing here reads data, and no number here is a trading claim. Defects D-35 to
D-38 (`docs/review/DEFECT_REGISTER_v1.19.md`) record the defaults the reference and
the client take where E0 leaves a choice.

```bash
python research/v119/h1_reference/fixtures.py
```

rewrites `fixtures/manifest.json`; run it only when the generator changes, and
record why in the CHANGELOG (the equivalence test then re-asserts on the new bytes).
