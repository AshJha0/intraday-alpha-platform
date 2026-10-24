# F3: metadata-only inventory of the in-sample session history (2026-10-24; UNSIGNED; no data opened)

Prepared at the outside reviewer's request (`docs/release/REVIEWER_F3_POWER_DOMAIN_DECISION_2026-10-24.md`, part 3): establish, **from metadata only**, whether
a sufficiently long, genuinely in-sample history of comparable session-level observations exists before any serial-dependence diagnostic (D1 of
`F3_OPERATING_DOMAIN_ASSESSMENT.md`) is even considered. F3 stays OPEN; nothing here authorises a diagnostic.

## What was read, and what was not

Read: `research/holdout_registry.json` (dataset labels, states, session dates, symbol lists, and the per-session state records) and the dataset-level
fields `path`, `venue`, `format`, `kind` and `status` of `research/data_manifest.json` (generated from `dataset.json` files only, never from the raw or normalised files).
**Not opened:** any session data (ITCH or IEX), any holdout, spent or unread dataset, any feature or result file, any ledger statistic, any
number derived from market data. No registry guard was bypassed. The counts below are counts of registry entries and dates, not statistics.

## Counts

* Registry: 35 dataset entries (14 `in_sample`, 21 `spent`), 19 distinct session dates (10 with an `in_sample` state, 9 `spent`; the spent dates also carry a
  `reserved_for` / `unread` record for a breadth-book prereg, so they are not available as in-sample). The manifest lists 39 real datasets (24 XNAS ITCH 5.0, 15 IEX DEEP+);
  all 15 IEX datasets are on spent dates.
* **Distinct in-sample sessions: 10.**

| # | session | gap to previous (calendar days) | in-sample datasets (labels, registry) | symbols in the registry entry |
|---|---|---|---|---|
| 1 | 2019-01-30 | | ds3_20190130, ds_20190130, multi7 | 3 (AAPL, MSFT, QQQ); 100 (broad breadth set) |
| 2 | 2019-03-27 | 56 | ds3_20190327, multi7 | 3 |
| 3 | 2019-07-30 | 125 | ds3_20190730, multi7 | 3 |
| 4 | 2019-08-30 | 31 | ds3_20190830, multi7 | 3 |
| 5 | 2019-10-18 | 49 | r7_20191018, r7_smoke_20191018, r7x20_3day | 3; 20 (r7x20) |
| 6 | 2019-10-30 | 12 | ds3_20191030, multi7 | 3 |
| 7 | 2019-12-30 | 61 | ds3_20191230, multi7 | 3 |
| 8 | 2020-01-30 | 31 | ds3_20200130, multi7 | 3 |
| 9 | 2021-07-13 | 530 | r7_20210713, r7x20_3day | 3; 20 |
| 10 | 2021-08-13 | 31 | r7_20210813, r7x20_3day | 3; 20 |

(The manifest also labels some of these datasets `in-sample, spent` in its `status` text. That records that the session's data were consumed by earlier
analyses; the registry state `in_sample` is what the guard enforces. The distinction matters for any prereg that wants to re-use them, and is noted rather than resolved here.)

## Assessment against the reviewer's question

* **Length.** 10 sessions, against the roughly 40 planning heuristic (a heuristic, not a guarantee). Even 40 would not guarantee resolution; at 10 the lag-1
  standard error is about 1/sqrt(10) = 0.32, an interval that cannot sit inside [-0.15, 0.15].
* **Contiguity.** No two in-sample sessions are adjacent trading days; the shortest gap is 12 calendar days and the median about 49. A lag-1 autocorrelation "between consecutive
  sessions" is **not defined** on this set: the series is a sparse, irregular sample of days spread over 2.5 years (with a 530-day gap), so it can say little about the day-to-day
  dependence that matters to sessions run consecutively, and nothing about volatility clustering or common event exposures between adjacent sessions.
* **Comparability.** Every in-sample session has a registry entry with the same three names (AAPL, MSFT, QQQ); one date additionally has a 100-name entry and three dates a 20-name entry. All in-sample datasets are XNAS ITCH. The dates are irregular, so they were not drawn as a contiguous or random sample of sessions (why they were chosen is not recorded in the metadata read here).
* **Symbols as a substitute.** A cross-section of symbols within a session is not a time series of sessions; the broad single day (2019-01-30, 100 names) cannot supply serial dependence.
* **Holdout/spent exclusion.** The additional 9 session dates (2025-11-28 to 2026-06-12) are spent or reserved; they may not be used for diagnostics, design or tuning.

## Conclusion (metadata only)

A sufficiently long, contiguous, genuinely in-sample history of comparable session-level observations **does not exist** in the registry: 10 sparse sessions. Under the reviewer's
stated standard, **D1 would be uninformative and P1 is not usable for real-data inference claims** on the strength of the existing in-sample history. No diagnostic should be
preregistered on this set for the purpose of supporting independence. Any change would need new in-sample sessions acquired under the holdout rules and the owner's data decisions (a new data
purchase or download, and each new session registered as in-sample before it is opened), which is a v1.19 planning question and is not proposed or authorised here.

## Limits of this inventory

Registry state is trusted as the guard enforces it; session content, data quality and per-session completeness were not examined (that would require opening data); the manifest's `spend`
annotations are free text and were read only for the purpose noted; no file other than the two listed was consulted.
