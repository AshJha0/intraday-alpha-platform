# DP1 data pricing (v2.0 U0 stage 0; read-only; nothing bought, nothing downloaded)

Status as of 2026-10-24: method and script committed; **every vendor cost column reads `awaiting key`** because
no vendor key had been supplied when this document was written (owner action O-2). When the owner names the
environment variables that hold the keys, one command fills the columns without a purchase or a download
(section 2). Plan of record: `IAP_v2.0_Unified_Programme_Plan_2026-10-24.md` revision 3 section 3; specification
`v2_spec/IAP_v2.0_U0_Unified_Specification.md` stage 0 and section 9 (G4). Predecessor: `DATA_ROUTE_PROPOSAL_v1.19.md`
(IEX HIST depth and parse cost; still the free route's evidence).

## 1. What is priced

| item | content | why the programme needs it |
|---|---|---|
| (i) daily point-in-time set | opens, closes, volumes for every US listing including delisted names; constituents of a broad index as dated files; delistings; corporate actions; an announcement calendar | the daily families D1-D4 and the ML programme (plan section 15.3); the sealed Track B test and Track C confirmation partitions (U0 section 2) |
| (ii) intraday breadth | 1-minute bars with synchronised quotes, 500+ names, 24-36 months | H1's suitability test (E3), H1-H2 at breadth, the cost-model calibrator (plan section 3) |
| (iii) bounded order-level | the approved tier-2 list: 40 XNAS.ITCH MBO sessions, AAPL/MSFT/QQQ (`research/r7/r7_tier2_dates.json`) | H4 only, if its gate passes |

## 2. Method

`tools/data_pricing.py` prints one row per vendor product with coverage, cost and the unit price, from **metadata
and cost endpoints only**:

- **Databento** (`DATABENTO_API_KEY`): `metadata.get_cost` returns the exact price of a request without
  transferring it (the vendor's documentation: cost "respects any discounts provided by flat rate plans"; up to 2,000
  symbols or `ALL_SYMBOLS`); `metadata.get_dataset_range` gives the dataset's coverage; `metadata.list_unit_prices`
  gives $/GB per schema. The client is wrapped so that only `metadata` is reachable: `timeseries` and `batch` raise.
  Item (iii) reuses `tools/r7_databento_fetch.py`'s estimator (same request as v1.15).
- **Polygon, now Massive** (`POLYGON_API_KEY`): subscription-priced, no cost endpoint. The row carries the public
  list price (section 3); `--probe` adds one reference-metadata request per needed table with `limit=1` and reports
  whether the key is entitled to it. Off by default.
- **Nasdaq Data Link / Sharadar** (`NASDAQ_DATA_LINK_API_KEY`): subscription-priced, no cost endpoint. `--probe`
  calls each needed table's `/metadata.json` (columns and status, no rows). Off by default.

Windows: daily from 2006-01-01 (clipped to the dataset's own start), intraday the last 36 complete months, both
ending at the start of the pricing month (`--as-of`, default today). Symbols: `ALL_SYMBOLS` is the committed default
and an **upper bound** for a 500-name universe; `--symbols-file` prices the owner's list instead (the issuers' SPY and
QQQ holdings lists are the plan's floor universe; this document does not fetch them). Keys are read from the
environment, never printed; error text is scrubbed. Tests: `python/tests/test_data_pricing_tool.py` (a fake client;
a download attempt fails the test).

```bash
python tools/data_pricing.py --as-of 2026-10-24
```

```bash
python tools/data_pricing.py --as-of 2026-10-24 --probe --json research/v2/data_pricing_2026-10-24.json
```

## 3. Cost and coverage table (run 2026-10-24 without keys; `--as-of 2026-10-24`)

| vendor | item | product | window | symbols | coverage | cost_usd | unit_price_usd_per_gb | notes |
|---|---|---|---|---|---|---|---|---|
| databento | i | daily OHLCV, consolidated (EQUS.SUMMARY ohlcv-1d) | 2006-01-01..2026-10-01 | ALL_SYMBOLS | awaiting key | awaiting key | awaiting key | prices only: constituents, delistings, actions and announcements are separate products (section 4) |
| databento | i | daily OHLCV, Nasdaq venue (XNAS.ITCH ohlcv-1d) | 2006-01-01..2026-10-01 | ALL_SYMBOLS | awaiting key | awaiting key | awaiting key | venue-only daily bars; alternative to the consolidated summary |
| databento | ii | 1-minute bars, Nasdaq venue (XNAS.ITCH ohlcv-1m) | 2023-10-01..2026-10-01 | ALL_SYMBOLS | awaiting key | awaiting key | awaiting key | pair with bbo-1m for quotes |
| databento | ii | 1-minute BBO snapshots, Nasdaq venue (XNAS.ITCH bbo-1m) | 2023-10-01..2026-10-01 | ALL_SYMBOLS | awaiting key | awaiting key | awaiting key | quotes synchronised to the bar grid |
| databento | ii | 1-minute bars, consolidated (EQUS.MINI ohlcv-1m) | 2023-10-01..2026-10-01 | ALL_SYMBOLS | awaiting key | awaiting key | awaiting key | consolidated across US venues |
| databento | ii | 1-minute BBO snapshots, consolidated (EQUS.MINI bbo-1m) | 2023-10-01..2026-10-01 | ALL_SYMBOLS | awaiting key | awaiting key | awaiting key | consolidated NBBO-like quotes on the bar grid |
| databento | iii | XNAS.ITCH mbo, tier-2 list (40 sessions, AAPL/MSFT/QQQ) | 2025-01-15..2026-09-22 | AAPL,MSFT,QQQ | awaiting key | awaiting key | awaiting key | the approved bounded order-level set; H4 only |
| polygon (massive) | i+ii | Stocks plan (minute aggregates, quotes, trades, reference, corporate actions, financials) | 20+ years on the Advanced tier | all US listings | subscription | list price: Massive Stocks Advanced $199/month, individual use (read 2026-10-24); entitlement awaiting key | n/a (subscription) | no cost endpoint |
| nasdaq data link (sharadar) | i | Sharadar SEP + TICKERS + ACTIONS + EVENTS + SP500 + DAILY | 1990s onward (vendor statement) | all US listings incl. delisted | subscription | price page not readable without login; awaiting key | n/a (subscription) | no cost endpoint |

Public figures read on 2026-10-24 (list prices and plan descriptions, not quotes; the owner verifies before any
purchase):

- **Databento** pricing page: historical data is usage-based, billed by uncompressed binary size in $/GB; a batch
  download is billed once and re-downloadable free for 30 days; **Standard** plan $199/month (live data, no licence
  fees, "16+ years of L0 history, 1 year of L1, 1 month of L2 and L3", more history pay-as-you-go); **Plus** $1,750
  in licence fees per month on an annual contract (adds external distribution and 16+ years of L1); **Unlimited**
  $4,500 per month (16+ years across all schemas); $125 of free credit for new users, expiring six months after
  sign-up. The page gives no per-dataset $/GB figures; `list_unit_prices` does, with a key. US equities datasets
  listed: `XNAS.ITCH`, `XNYS.PILLAR`, `ARCX.PILLAR`, `XASE.PILLAR`, `XCHI.PILLAR`, `BATS.PITCH`, `BATY.PITCH`,
  `EDGA.PITCH`, `EDGX.PITCH`, `MEMX.MEMOIR`, `EPRL.DOM`, `IEXG.TOPS`, `XCIS.TRADESBBO`, `XNAS.BASIC`, `XPSX.ITCH`,
  `XBOS.ITCH`, `OCEA.MEMOIR`, `EQUS.MINI`, `EQUS.SUMMARY`, plus three reference datasets (corporate actions, security
  master, adjustment factors). The reference API documents point-in-time corporate actions (`pit` flag) and a
  security master; its pricing is not on the pages read.
- **Massive (Polygon)** Stocks tiers, monthly billing, each labelled "Individual use": Basic $0 (2 years, end of day,
  5 calls/minute), Starter $29 (5 years, 15-minute delayed), Developer $79 (10 years, trades), Advanced $199 (20+
  years, real-time, quotes, trades, financials). Minute aggregates, reference data and corporate actions are in every
  tier; annual billing saves 20 %. Business pricing is a separate page.
- **Sharadar** (vendor site): "active and delisted coverage goes back to the 90s", "point-in-time ready", fourteen
  tables including prices, fund prices, actions, material corporate events, tickers and metadata, S&P 500
  constituents; sold a la carte or as a bundle; a free entry tier covers the 30 Dow names. Prices are behind the
  Nasdaq Data Link login and were not read.

## 4. G4 suitability checklist per family (U0 section 9; the gates named here bind U3 and DD1)

| family | data the hypothesis needs | daily set (i) | intraday (ii) | gate before any fit |
|---|---|---|---|---|
| D1 short-term reversal (1-5 d) | point-in-time constituents, delistings, split-adjusted closes, volumes | Sharadar SEP + TICKERS + SP500, or Polygon aggregates + reference + a constituent source | not needed | survivorship test (delisted names present), split-day reconciliation, calendar test |
| D2 residual momentum (6-12 m, skip month) | as D1 plus 13+ months of history before the first development session and a factor model's inputs | as D1 | not needed | as D1; factor returns from the same route |
| D3 overnight / close-to-open | **open and close conventions** (official auction prints or first/last trade), dividends and splits applied consistently to both, ex-dates | Sharadar SEP (open, close, closeadj, dividends) or Polygon daily aggregates with the open/close convention documented; Databento `ohlcv-1d` is venue (XNAS) or consolidated summary with its own open/close definition | optional (to check the convention against the auction) | the open/close convention written in `data_manifest` and tested on sample days before D3's first fit (U0 stage 2 DD1) |
| D4 post-announcement drift (5-60 d) | **point-in-time announcement timestamps** (date and before/after market) and **surprise data** (actual versus a pre-announcement expectation) | Sharadar EVENTS and SF1 give dated filings and reported fundamentals; Polygon financials give filing dates; **neither priced source here supplies consensus expectations**: a surprise must be built from the vendor's own prior-period data (a disclosed proxy) or bought separately | not needed | D4 is **data-ineligible until** a point-in-time announcement timestamp source and a surprise definition are recorded in its U3 specification |
| D5 close-pressure / rebalance | intra-session flow into the close, index rebalance and ETF creation calendars | **ineligible on daily data** (G4): the effect lives inside the last hour | needs (ii) plus an event calendar | not eligible before intraday breadth data exist |
| H1 residual reversal (15/30 min; frozen E0) | minute bars with synchronised quotes for 500+ names, 24-36 months; SPY and sector ETFs on the same clock | not sufficient | Databento `ohlcv-1m` + `bbo-1m` (XNAS venue or EQUS.MINI consolidated), or IEX HIST (free, parse-bound) | E3 suitability test on the chosen route; one route per experiment |
| H2 momentum / H3 lead-lag / H4 OFI | as H1; H4 adds order-level depth on a bounded universe | not sufficient | as H1; H4: item (iii) | H3/H4 gates per plan section 15.8 |

Common to every row: constituents must be dated files usable under the as-of rule (`iap.data.pit`), the route is
recorded in every feature contract (`docs/DATA_CONTRACTS.md`), and the partitions of U0 section 2 are cut by calendar
before any fit.

## 5. Licence summaries (public pages read 2026-10-24; what was and was not readable)

| vendor | readable on 2026-10-24 | what it says | owner reads before purchase (O-5) |
|---|---|---|---|
| Databento | pricing page, API reference | Standard plan: "no license fees" and live data for one account; Plus adds "external distribution", so Standard is internal use; historical downloads are billed once and re-downloadable for 30 days; exchange licence fees apply to live Plus/Unlimited | the Terms and Conditions page renders only in a browser session; its clauses on derived data, retention after cancellation and per-user scope were not read |
| Massive (Polygon) | pricing page, terms hub | every Stocks tier is "Individual use"; a "Non-pros only" page is linked; three agreements apply by context (Website, Individuals, Businesses) and signed order forms fall under the Businesses terms | the Individuals and Businesses terms themselves (redistribution, derived data, display, retention) were not reachable by URL |
| Nasdaq Data Link / Sharadar | vendor site | coverage statements only (section 3) | the Nasdaq Data Link terms of use and the Sharadar product licence (personal versus professional, redistribution, retention) sit behind the login |

None of the three pages read grants redistribution; the repository's standing rule (nothing vendor-derived committed
beyond aggregate results, `docs/DATA_SOURCES.md`) covers every route until the owner records the licence read.

## 6. Stop condition and the decision this prepares

U0 section 6: "DP1 shows no affordable daily set with constituents and delistings -> free-data fallback with
survivorship limits stated, Track C confirmation deferred." The condition cannot be evaluated until the cost columns
are filled. What this document establishes without a key: two vendors (Sharadar, Massive) sell item (i) as a
subscription whose list price is of the order of $200 per month or less, with point-in-time constituents only in
Sharadar's SP500 table or an owner-supplied constituent file; Databento prices item (ii) exactly by metadata; item
(iii) is the v1.15 estimate re-run. The purchase decision (O-2, "approve each purchase with its figure") follows the
filled table, not this draft.

## 7. Record

- 2026-10-24: script and document committed (session B, stage 1); no key supplied; no request made to any vendor API;
  web pages read: Databento pricing, venues-and-datasets, API reference (`get_cost`, `list_unit_prices`, reference
  corporate actions); Massive pricing and terms hub; Sharadar site. Decisions the specification does not settle are
  defects D-33 and D-34 in `DEFECT_REGISTER_v1.19.md`.
