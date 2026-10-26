# Data and feature contracts (v2.0 U2; one page)

Every dataset and every feature the research loop consumes is declared before any fit, in code, with the six
lineage facts the plan names (plan section 5): **source**, **availability-timestamp rule**, **adjustment**,
**missing-value policy**, **route** and the **leakage test** that guards it. The declarations live in
`python/src/iap/data/contracts.py` (`iap.data.contracts.DatasetContract`, `iap.data.contracts.FeatureContract`), the
point-in-time universe in `iap.data.pit`, the canaries in `iap.data.leakage`, the synthetic fixture and its
contracted features in `iap.data.synthetic`. Tests: `python/tests/leakage/` (run on every PR by the `python` job).

## 1. The contract

| field | dataset | feature | rule |
|---|---|---|---|
| id | `vendor.table` | `family.feature` | lower-case dotted identifiers; a feature function is named `feature_<id with _>` |
| source | vendor, venue or `synthetic` | resolved from its inputs | never inferred from a path |
| availability | `receive_ts` (lag 0), `period_end` (lag >= 1), `publication_date` (lag >= 1) | `inputs` + own lag >= 0 | a record of period `p` is usable at decision index `>= p + lag`; a close is not observable at the close; a file dated `D` is not usable on `D` |
| adjustment | `none`, `split`, `split_dividend`, `total_return` | same vocabulary | declared, not guessed from the vendor's column names |
| missing policy | `ineligible`, `forward_fill` (with a maximum), `zero` | same | `ineligible` is E0 section 3's rule: a missing input yields an ineligible name, never a value |
| route | `daily_pit`, `intraday_vendor`, `iex_hist`, `order_level_vendor`, `synthetic` | must equal every input's route | one route per experiment, never mixed (plan section 3) |
| leakage test | `LK-CAUSALITY`, `LK-FUTURE-SHIFT`, `LK-SHUFFLED-LABEL`, `LK-PLANTED-LEAK` | same | the canary that guards the declaration |

**Enforced at import.** A feature is registered only through the `@feature(FeatureContract(...))` decorator, which
also resolves its lineage (sources, total lag against the raw grid, route consistency) and refuses an input that is
neither a registered dataset nor a registered feature. Every module that defines `feature_*` functions ends with
`enforce_module(__name__)`, which raises `MissingContractError` (an `ImportError`) for any feature function without a
contract, so the import fails; an architecture test checks that no feature module omits the call. `lineage(feature_id)`
returns the full document (the registry's `feature_set_hash` is the content hash of it).

## 2. Point-in-time universe (`iap.data.pit.PointInTimeUniverse`)

E0 section 2's rules, written once for every family: a constituent file dated `D` is usable from the first session
on or after `D` plus the declared publication lag in sessions (E0's "first session after D + 1 business day" is a lag
of two sessions when `D` is a session; the value is a contract field, defect register D-28); a session before the
first available file has no universe and `members()` refuses it rather than read a later file; a name dropped by a
later file leaves only when that file becomes available (no retroactive edits); a name that later delists stays a
member through its last traded session (survivorship); for `retention_sessions` after that it is *retained*
(`retained()`: present, not tradable, flagged `delisted`) so closeouts and straddling labels are computed, never
dropped. `survivorship_check(coverage)` reports members without panel rows and rows after the retention window; DD1
runs it on every partition the development identity may read.

## 3. The leakage canary suite (`iap.data.leakage.run_canaries`)

| canary | what must happen | positive control |
|---|---|---|
| `LK-FUTURE-SHIFT` | the feature shifted by +1 period (the value of `t-1` used at `t`) scores at chance against the label | the unshifted feature scores above chance (D-29 records the reading of "future-shift") |
| `LK-SHUFFLED-LABEL` | a SplitMix64-permuted label scores at chance | the true label scores above chance |
| `LK-PLANTED-LEAK` | a deliberately leaked feature is caught by the contract check | the causal control passes the same check |

"At chance" is `|IC| <= 3 / sqrt(n)` (null IC standard deviation about `1/sqrt(n)`), IC the Pearson correlation
over finite pairs. The contract check (`check_causality`) is a truncation test: the feature recomputed on the frame
cut at every probe row must equal the full-frame value at that row; a feature that reads a later row differs, whatever
its declaration says. The planted leak in `iap.data.synthetic` is labelled a detector fixture in its own contract.
A canary that fails raises `CanaryFailure`; a detector without its positive control would pass everything and is
therefore itself a failure.

## 4. What this does not do

No data is read from disk or the network here; ingestion (DD1) runs as `iap-runner` and records the contracts' ids
in the data manifest. The vocabularies above are the stage-1 defaults; a family that needs another value adds it by
PR with a defect-register entry, never by bypassing the registry.
