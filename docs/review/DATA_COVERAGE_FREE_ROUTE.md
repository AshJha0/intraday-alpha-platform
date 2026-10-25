# Free data route: coverage, suitability and the survivorship test (v2.0 stage 2 DD1; U0 DR-6)

Status as of 2026-10-25: one live `coverage()` run per source from the development session, **metadata only**: symbol
counts, revision and filing counts, date ranges. No price, return, quote, membership list or filing body was stored or
printed; the numbers below are the whole of what left each call (`tools/free_data_coverage.py`, adapters
`python/src/iap/data/sources/`). Sealing rule (U0 section 2, G3): price data is written only by `iap-runner` into sealed
partitions once the D2 service exists; every adapter's `fetch()` refuses any other identity or target and, until D2,
raises after those checks. Licences: `docs/DATA_SOURCES.md` sections 8 to 14. Pricing of the route not taken:
`DATA_PRICING_v2.md` (DR-6 note). Open decisions: `DEFECT_REGISTER_v1.19.md` D-39 to D-42.

## 1. The route (DR-6, and the source choice recorded here as DR-7)

Owner's rule (2026-10-25): free sources only; any source whose sign-up asks for a payment card is excluded outright;
sources that need no account are preferred; where two free sources cover one need, choose on delisted-name coverage
first, licence clarity second, history length third.

| need | chosen source | alternative | choice and reason (DR-7) |
|---|---|---|---|
| daily prices (open, high, low, close, volume, adjustments) | **Tiingo free tier**, conditional: the owner creates the account and confirms the sign-up issued a key with no card (the pricing page makes no card statement; if a card is asked, Tiingo is excluded and Stooq becomes primary) | Stooq bulk daily archive (no account) | delisted coverage first: Tiingo's public ticker list shows 8,256 inactive US stocks still listed with their last dates (section 2), which is the survivorship population DD1 needs and can be measured before ingestion; Stooq's delisted coverage is inside its archive and could not be measured (D-42). Licence clarity second: Tiingo's "Internal Use Only" is quoted; Stooq's terms were not readable. History third: Tiingo from 1962. Stooq stays the fallback for breadth beyond the free tier's 500 unique symbols per month |
| point-in-time S&P 500 membership | Wikipedia revision history (no account; **proxy**, every interval labelled) | none free | the only free dated source; reliable from 2008-02 (section 2) |
| point-in-time Nasdaq-100 membership | Wikipedia revision history | none free | reliable only from 2025-07 (the page listed names without tickers before); unusable as a development-period universe; recorded, not relied on |
| announcement timestamps | SEC EDGAR submissions API and full index (no account) | none free | public, rate-ruled; D4 becomes filing-date drift (section 4) |
| daily factors | Kenneth French library (no account) | none free | the standard source; 1926 onward |
| intraday breadth | IEX HIST (no account; `tools/iex_hist.py`) | none free | unchanged from v1.17; B2b's parser work |
| order-level data | none | - | H4 data-ineligible (DR-6) |

Excluded outright under the card rule: Databento (card at sign-up, DR-6), Massive/Polygon and Nasdaq Data Link
(subscriptions); their public list prices stay in `DATA_PRICING_v2.md` for the record only.

## 2. Coverage run of 2026-10-25 (counts and ranges; nothing else left the calls)

| source | live call | headline counts | date ranges | requests |
|---|---|---|---|---|
| Tiingo (public supported-tickers list; no key used) | run | 109,127 tickers listed; 24,703 on US exchanges; **16,590 US stocks**, all dated; **8,256 inactive** (last date more than 30 days before 2026-10-25: delisted or halted names still listed); 8,047 US ETFs; 4,253 US stocks with history starting before 2010; by exchange: NASDAQ 9,409, NYSE 6,309, AMEX 330, BATS 273, NYSE MKT 213, NYSE ARCA 53 | first dates 1962-01-02 to 2026-10-23; last dates 1997-04-01 to 2026-10-23; inactive names' last dates 1997-04-01 to 2026-09-24 | 1 |
| Tiingo (authenticated metadata probe) | **needs owner's `TIINGO_API_KEY`** (owner creates the free account; if the sign-up asks for a card, Tiingo is excluded) | - | - | 0 |
| Stooq (bulk listing page) | attempted; the page is a JavaScript verification and consent dialog (D-42), 0 archives parsed | - | - | 1 |
| Wikipedia, S&P 500 page | run | 3,154 revisions; 248 months sampled (first revision of each month); **218 months parse to a plausible constituent table** (480-520 rows), 30 do not; from the 218 monthly snapshots: **989 membership intervals, 946 distinct symbols, 487 closed intervals**; largest parsed table 505 rows | revisions 2005-09-14 to 2026-10-21; **earliest reliable revision 2008-02-10**, latest 2026-10-08 | 513 (both pages) |
| Wikipedia, Nasdaq-100 page | run | 2,105 revisions; 253 months sampled; **13 months reliable** (95-110 rows), 240 not (the page listed company names without tickers for most of its history); 117 intervals, 117 symbols, 16 closed; largest parsed table 102 rows | revisions 2004-02-21 to 2026-10-24; earliest reliable 2025-07-11 | (above) |
| SEC EDGAR (50-CIK sample via the submissions API) | **not run from this session**: `www.sec.gov` and `data.sec.gov` answered every scripted request 403 (Akamai) whatever the User-Agent (D-41); the adapter is tested on fixtures; the call runs from the owner's workstation with `IAP_SEC_CONTACT` set | - | - | 0 of a planned 51 |
| Kenneth French (three archives read in memory, discarded) | run | FF3 daily 26,317 rows, 4 factor columns; FF5 daily 15,897 rows, 6 columns; momentum daily 26,216 rows, 1 column | FF3 1926-07-01 to 2026-08-31; FF5 1963-07-01 to 2026-08-31; momentum 1926-11-03 to 2026-08-31 | 3 |

Reading the Wikipedia numbers: monthly sampling gives month-granular interval starts and merges changes inside a month,
so 989 intervals over 18.7 years (about 53 per year, 487 of them closed) is an upper bound on the resolution this run
can see, not the index's change count; ingestion parses every revision (3,154) and gets the day of each edit. The 30
unreliable S&P months are early (2005-09 to 2008-01, before the table carried tickers) and scattered format changes;
their share and dates are the membership-quality statistic DD1 measures per year (D-40).

Command (metadata only; the JSON holds counts and ranges):

```bash
python tools/free_data_coverage.py --source tiingo,stooq,wiki_membership,edgar,french --ciks 50 --json research/v2/free_route_coverage.json
```

## 3. What each number says about suitability

- **Survivorship population exists.** Tiingo lists 8,256 inactive US stocks with their last dates; a dataset whose
  universe is built from Wikipedia intervals will contain names that left the index and later delisted, and the
  ingestion can check, name by name, that their price history runs to their last date. That check is the survivorship
  test of section 5; today's run shows only that the population is listed, not that its history is complete.
- **Membership proxy usable for the S&P 500 from 2008-02**, under the proxy label and the quality test; Y-4 development
  windows (U0 section 2, Y the year ingestion happens) fit inside that. **Not usable for the Nasdaq-100** before
  2025-07: the Nasdaq-100 proxy is recorded and excluded from the development universe.
- **Factors** cover every daily window the families need, with a publication lag that is a declared default (D-39).
- **Announcement timestamps** are available in principle (public API with acceptance timestamps) and unmeasured from this
  session; D4's eligibility waits for the owner-side run.
- **Opens**: Tiingo's end-of-day rows carry `open` and `adjOpen` (documentation); whether the open is the official
  auction print or the first trade is not stated on the public pages and is the D3 convention test of DD1.

## 4. G4 suitability checklist per family on the free route (U0 section 9; binds U3 and DD1)

| family | needs | free-route source | eligible? | gate before any fit |
|---|---|---|---|---|
| D1 short-term reversal (1-5 d) | point-in-time constituents, delistings, split-adjusted closes, volumes | Tiingo daily (or Stooq) + Wikipedia S&P 500 intervals (proxy) | **eligible if coverage holds**: the S&P 500 proxy from 2008-02 and the survivorship test (section 5) pass | survivorship test, split-day reconciliation, calendar test, membership-quality test per year |
| D2 residual momentum (6-12 m, skip month) | as D1 plus 13+ months of prior history and factor inputs | as D1 + Kenneth French daily factors (25-session lag) | **eligible if coverage holds** (as D1); factor lag applied as declared | as D1; factor returns from the same route with their lag in the contract |
| D3 overnight / close-to-open | open and close conventions, dividends and splits applied to both | Tiingo rows have `open`, `adjOpen`, `close`, `adjClose`, `divCash`, `splitFactor`; **the source has opens**, their convention (auction print or first trade) is undocumented on the public pages | **conditional**: eligible only after the convention is tested on sample days and written in `data_manifest` | D3 convention test in DD1 |
| D4 post-announcement drift | point-in-time announcement timestamps and a surprise measure | EDGAR acceptance timestamps (8-K, 10-Q, 10-K); **no consensus data on the free route** | **becomes a filing-date drift family** (drift after the filing timestamp, no surprise); eligible only once the EDGAR coverage call has run owner-side and U3 writes the drift definition | EDGAR run; acceptance-hour rule (D-41) decided in the D4 specification |
| D5 close-pressure / rebalance | intra-session flow into the close | daily data only | **ineligible on daily data** (G4) | needs intraday breadth plus an event calendar |
| H1 residual reversal, H2 momentum (intraday) | minute bars with quotes, 500+ names, 24-36 months | IEX HIST (TOPS/DEEP+, T+1, free) | eligible via IEX HIST, parse-bound (B2b reopened) | E3 suitability test on the IEX route; one route per experiment |
| H3 lead-lag | as H1 | IEX HIST | as H1 | plan section 15.8 gate |
| H4 order-flow imbalance | order-level depth on a bounded universe | none (no purchase) | **ineligible** (DR-6) | - |

## 5. The survivorship test DD1 runs after D2 (design; nothing run)

Run as `iap-runner` during ingestion, before any partition is sealed, with only pass/fail counts reported to the
development identity (U0 section 2: no summary statistic of a sealed partition).

1. **Population.** From the Wikipedia S&P 500 intervals (every revision parsed), the set L of symbols whose last
   interval closed before the ingestion date; from Tiingo's supported-tickers list, the set I of inactive US stocks.
   Report |L|, |L ∩ I|, |L \ I| (left the index but still active or renamed) and the symbols of L absent from the
   source altogether (count only).
2. **History completeness.** For every symbol in L ∩ I the price history must run from its first interval start to
   its listed last date with no gap longer than the calendar's longest holiday run; count the failures. Threshold
   (default, D-40): at most 2 % of L ∩ I may fail; above that the daily families are data-ineligible, the test is
   not relaxed (DR-6 stop condition).
3. **Return-date retention.** A symbol removed from the index on revision date R must still carry prices for the
   sessions after R up to the family's longest horizon (60 sessions for D4; 21 for D1/D2), so that an exit is priced
   at the exit, not at the last in-index close; count the symbols short of that.
4. **Name changes and ticker reuse.** Symbols that appear in two disjoint intervals with a gap (reused tickers) are
   listed by count; ingestion keys the universe by (symbol, interval) and never by symbol alone.
5. **Membership quality per year.** The share of revisions in each year that parse to a plausible table and the
   median gap, in days, between consecutive reliable revisions. Years below the threshold (default: 90 % reliable and
   median gap at most 45 days) are excluded from the development window for the membership-dependent families.
6. **Control.** The same tests on the synthetic canary panel (`iap.data.synthetic`) with a planted delisting whose
   history is truncated: the test must fail on the plant (positive control) and pass on the clean panel.

What the test cannot do: certify that Wikipedia's editor dates equal the index provider's effective dates. That gap is
the proxy label's reason, stays in every result that uses the universe, and is the first thing a bought constituent
file would replace if the card decision ever changed.

## 6. Record

- 2026-10-25: adapters, tests and this report; live coverage calls as in section 2 (Tiingo list, Wikipedia twice, the
  second with the widened template parser; French; Stooq attempted; EDGAR not reachable). 513 Wikipedia requests at one
  per second with the repository's User-Agent. Nothing stored; nothing bought; no account created; no sealed read.
