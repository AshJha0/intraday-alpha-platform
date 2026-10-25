# Data sources: free Nasdaq sample days, IEX HIST, process-and-delete, and the v2.0 free data route (v1.17 WS-A; status as of 2026-10-25)

The platform's real-data rule is **free data only, nothing downloaded without
the owner's approval**. This page says which free sources exist, what the
reader does with each, how a full-market day is processed on a machine that
cannot hold two of them, and which numbers are measured. The mapping rules are
in [REAL_DATA.md](REAL_DATA.md) (sections 3.5 and 4.5); the commands are in
[runbooks/RUNBOOK_process_and_delete.md](runbooks/RUNBOOK_process_and_delete.md).
Every number below names the committed artefact it comes from. Sections 8 to 13 (v2.0 stage 2 DD1, decision DR-6 of
the unified specification) record, per free-route source, the public terms read, the use intended and whether that use
is permitted for this non-commercial research; section 14 lists the licence questions for the owner.

## 1. The sources

| source | what it is | reader | cost / access |
|---|---|---|---|
| Nasdaq TotalView-ITCH 5.0 sample days | full-market, order-by-order Nasdaq feed for one past day, `NNNNNNNN.NASDAQ_ITCH50.gz` (or the `S*-v50.txt.gz` names) | `iap.marketdata.ingest` (`Itch50Mapper`), `--format itch50` | free, from the Nasdaq EMI host in the allow-list; a day is 4-18 GB of gzip |
| IEX HIST DEEP+ / DEEP | IEX's own book, published as a daily capture the next day: DEEP+ carries orders (message protocol `0x8005`), DEEP price levels (`0x8004`) | `iap.marketdata.iex.IexReader`, `--format iex-deep-plus` / `iex-deep` | free, no account, T+1 (`iextrading.com`); the codec downloads nothing |
| IEX HIST TOPS | top-of-book quotes and trades (`0x8003`) | `--tops FILE` on an IEX ingest | free, same host; used as an **oracle**, not as a book |
| LOBSTER / Databento samples | free sample files used for the three-name datasets | see REAL_DATA.md sections 4.3-4.4 | free samples only |

The 2019-2020 Nasdaq days are the in-sample set; `research/breadth/candidates.json`
lists ten in-sample days (2019-01-30, 2019-03-27, 2019-07-30, 2019-08-30,
2019-10-18, 2019-10-30, 2019-12-30, 2020-01-30, 2021-07-13, 2021-08-13) with the
source URL, the file name and a note that the sha256 is recorded after download
(Nasdaq publishes no checksum). All ten are ingested (`research/data_manifest.json`,
section 6). On the data root of 2026-10-24 the vendor file was still on disk for
four of them (2019-01-30, 2019-10-18, 2021-07-13, 2021-08-13); the other six were
removed after ingest and are re-downloads that wait for approval
(`data/real/itch/ARCHIVED_GZ_MANIFEST.txt`). The 2025/26 days are the holdout and
are not in that manifest; for AAPL, MSFT and QQQ they are spent in the holdout
registry, for every other name unread (see
[PORTFOLIO_BREADTH.md](PORTFOLIO_BREADTH.md) and REAL_DATA.md section 3.7).

Examples of size, from the committed record: the 2019-01-30 full-market file is
368,366,634 messages (`research/breadth/UNIVERSE_2019-01-30.md`, with its
sha256); the 2019-12-30 file is 268.7 million messages in 3.5 GB of gzip
(REAL_DATA.md section 9).

## 2. IEX HIST: how the reader works, and what was checked

- **Container.** The files are **pcapng** (section header, one interface,
  enhanced packet blocks), not classic pcap as the vendor page says; the reader
  accepts both. Every packet is a UDP/IPv4 IEX-TP v1 segment; heartbeats and
  sequence gaps are counted.
- **Clock.** The capture host stamps in **TAI**: capture stamp minus the IEX-TP
  send time was 37.00 to 38.14 s on the checked slice (TAI - UTC = 37 s).
  The reader takes `exchange_ts` from the messages' own UTC nanosecond
  timestamps, so the offset does not touch the data; `receive_ts` is not
  derived from the capture stamp.
- **Translation.** DEEP+ messages become ITCH messages and the **unchanged**
  `Itch50Mapper` normalises the session (venue `IEXG`, id 102). DEEP has no
  orders, so each price level is modelled by synthetic orders, trades are paired
  to level decrements within the atomic event, and the synthetic ids are counted.
- **The TOPS oracle.** IEX's TOPS quote is the top of the same book that DEEP
  publishes in full, so a book reconstructed from DEEP must reproduce TOPS's
  best bid and offer (price and size on both sides) at every change. `--tops
  FILE` runs that comparison as the session's `book_verification`; it is the
  equivalent of the LOBSTER book check, with an independent vendor-published
  answer.
- **Conformance pass** (`research/v117/IEX_HIST_CONFORMANCE_20261013_TOPS.md`
  and `.json`, tool `tools/iex_hist_conformance.py`): the first 250 MB of the
  2026-10-13 TOPS file (the whole file is 9.74 GB; the slice is the first 2 h
  27 m of the day, 07:07:32 to 09:34:54 New York). 7,253,833 UDP packets, 7,619,846
  messages, **0 unknown types, 0 sequence gaps, 0 crossed quotes**; 437,045
  one-sided quotes (an empty side is price 0 / size 0, as specified); three
  directory rows only, because the directory covers IEX-listed securities (AAPL,
  MSFT, QQQ have none, so the reader synthesises a round-lot-100 row and counts
  it). The file was deleted after the pass.
- **Not covered.** DEEP / DEEP+ captures were not downloaded, and the
  TOPS-versus-DEEP oracle on a real day needs both feeds of one day (about 20 GB
  per the conformance note). The codec is tested on synthetic captures
  (`python/tests/iex_encoder.py`, `test_iex_codec.py`: framing, the
  specification's worked example, ITCH-equivalence of a DEEP+ session, the level
  translation, the oracle). A DEEP+ book is therefore **unverified against real
  IEX data** until a day is ingested.
- **Daily watch.** `tools/r7_watch_nasdaq.py` also reads the IEX HIST index for
  the last 7 UTC days (one JSON GET per day, never a download) and reports DEEP
  and TOPS days with sizes; see
  [runbooks/RUNBOOK_r7_nasdaq_watch.md](runbooks/RUNBOOK_r7_nasdaq_watch.md).

## 3. The approval rule for downloads

1. Nothing is downloaded by default. The IEX codec reads files; it fetches none.
2. `process-day` downloads only with `--download --i-have-approval`, through
   `tools/r7_download.py`, which accepts only the hosts in
   `iap.marketdata.r7plan.ALLOWED_HOSTS`, checks the size and records the sha256.
3. Each day is an owner decision, recorded with its date: the 2019-01-30
   full-market file was approved for the universe measurement; the first 250 MB
   of the 2026-10-13 TOPS file was approved for the conformance pass; the A6
   deletion of the regenerable raw and normalized JSONL of the 21 spent real
   datasets was approved on 2026-10-15.
4. The holdout sessions (`data/real/r7_holdout`, the 2026 holdout) are never
   inputs of the process-and-delete chain, and nothing outside a day's dataset
   directory and its vendor file is deleted.
5. The disk guard never deletes to make room: it refuses.

## 4. Process-and-delete (A2 / A2b)

`python -m iap.marketdata process-day` takes one manifest day through:
disk guard -> approved download -> `ingest --slim` (partitioned normaliser,
`--partition-instruments N`) -> `delete_raw` -> Rust feature build -> `KEEP.json`
(sha256 of every kept file) -> deletion of the normalized twin and the vendor
file. Each step has a marker and resumes; nothing is deleted before the kept
files verify; `--render-cmd` and `--jobs-registry` write a detached `.cmd` wrapper
and a watchdog entry (stall 120 min, two restarts).

What is kept: `features/`, `dataset.json`, `configs/`, `reference/`,
`normalized/qc_report.json` and `KEEP.json`. What research reads is the IAP1
event file, the Parquet and the features.

**Measured sizing (committed record).**

| fact | value | source |
|---|---|---|
| the machine | 16 GB memory, 28 GB free disk when the runbook was written | runbook |
| layout cost of the three-name day, before slim | 0.55 GB per million events (raw JSONL + normalized JSONL + IAP1 + Parquet) | `research/breadth/UNIVERSE_2019-01-30.md` |
| the first 100-name day, as estimated before slim | 70-100 M events, 40-50 GB at peak; refused by the disk guard with 22 GB free | same |
| the symbol-scaled rate estimate | overstates a wide universe many times: 830 GB estimated against about 50 GB expected for 100 names on 2019-01-30 | CHANGELOG, A2b |
| the normalized JSONL twin | 3x the bytes of the IAP1 file; `--slim` does not write it | CHANGELOG, A2b |
| A6 deletion | 151 GB freed; IAP1, Parquet, features and manifests kept | CHANGELOG, A2b |
| memory | one partition of N instruments plus one event-time window instead of the whole day; outputs byte-identical to the unpartitioned run (tested on the synthetic pipeline and an ingested ITCH day) | CHANGELOG, A2 |

`--peak-gb` overrides the guard's estimate with a measured value, and
`breadth-universe` records the universe's share of the day's displayed volume
for future estimates. `process_report.json` in each dataset directory holds the
timings, the estimate, the guard reading and what was deleted; those files live
under `data/real`, which is not committed.

**The completed 100-name day (2019-01-30, `breadth/ds_20190130`; this
machine, 16 GB; `process_report.json`).** The report's last run resumed from
markers (download, ingest and delete_raw were skipped), so it carries no
end-to-end wall clock and none is quoted. Its recorded facts: the disk guard
ran with a measured `peak_gb` override of 60 GB against the 829.95 GB
symbol-scaled estimate and passed with 153.23 GB free; the Rust feature build
of 100 feature files took 21,878 s (about 6.1 h); `KEEP.json` covers 207 kept
files; the normalized IAP1 and Parquet were then deleted, freeing 7.24 GB.
These figures come from one run on one machine and are not a benchmark.

## 5. The A4 IEX overlap set

The nine Nasdaq 2025/26 sessions on disk are also on IEX HIST. At v1.18.0 all
nine are processed (the table's "state" column was last updated for the first
six on 2026-10-17 and is completed below from the data manifest). `python -m
iap.marketdata process-iex-day` (RUNBOOK_process_and_delete.md, IEX section)
pairs each with the same day's IEX book for AAPL, MSFT, QQQ: DEEP+ is the
book feed, TOPS its oracle (`book_verification`); the TOPS capture is read
once for the three names' quotes and deleted before DEEP+ is downloaded (one
vendor file on disk at a time), and after the feature build only features,
manifests and `KEEP.json` stay. Approval (owner, 2026-10-15, verbatim in
`research/v117/iex_overlap_manifest.json`): "A4 (IEX overlap days, needs your
download approvals), = approved". Sizes are the HIST index of 2026-10-15; the
sha256 of each capture is recorded in the day's `process_report.json` and, for
the first six days, in docs/research/IEX_OVERLAP_SET.md.

| date | Nasdaq dataset | TOPS gz | DEEP+ gz | DEEP gz (not downloaded) | state |
|---|---|---|---|---|---|
| 2025-11-28 | r7_20251128 | 4.37 GB | 4.31 GB | 4.60 GB | done 2026-10-16: oracle match 435,973 / 72,883 / 998,642 compared, 0 mismatched; 0.325 GB kept (IAP1 + Parquet + features); captures deleted |
| 2025-12-08 | r7_20251208 | 9.30 GB | 8.99 GB | 9.43 GB | done 2026-10-16: oracle match 832,770 / 152,908 / 3,496,249 compared, 0 mismatched; 0.671 GB kept; captures deleted |
| 2025-12-09 | r7_20251209 | 8.61 GB | 8.43 GB | 8.89 GB | done 2026-10-16: oracle match 711,762 / 101,120 / 3,045,150 compared, 0 mismatched; 0.706 GB kept; captures deleted |
| 2025-12-10 | r7_20251210 | 12.29 GB | 11.45 GB | 11.74 GB | done 2026-10-16: oracle match 580,122 / 257,034 / 3,763,770 compared, 0 mismatched; 0.678 GB kept; captures deleted |
| 2025-12-11 | r7_20251211 | 11.15 GB | 10.55 GB | 11.00 GB | processed (dataset `iex/iex_20251211` with `KEEP.json` in `research/data_manifest.json`); oracle counts are in its `process_report.json`, not copied here |
| 2025-12-12 | r7_20251212 | 13.67 GB | 12.71 GB | 12.98 GB | processed (`iex/iex_20251212`, same record) |
| 2026-05-15 | holdout2026 | 12.15 GB | 11.91 GB | 12.60 GB | done 2026-10-16: oracle match 706,497 / 131,039 / 4,554,801 compared, 0 mismatched; 1.001 GB kept; captures deleted |
| 2026-05-18 | holdout2026 | 14.87 GB | 14.21 GB | 14.90 GB | done 2026-10-17: oracle match 1,262,241 / 80,378 / 2,403,225 compared, 0 mismatched; 0.765 GB kept; captures deleted |
| 2026-06-12 | r7_20260612 | 15.62 GB | 14.69 GB | 15.33 GB | processed (`iex/iex_20260612`, same record) |

**Capture-timestamp re-ingest (v1.18 REL-04).** Six of these days (2025-11-28,
12-08, 12-09, 12-10, 2026-05-15, 05-18) were ingested a second time with
`--capture-ts` into `iex_capts/`, so `receive_ts` is the capture host's packet
stamp. The signed verification of 2026-10-23
(`research/v118/rel04_verify/REPORT.md`) found the identity test passed on all
six days: only `receive_ts` differs from the A4 datasets, and no within-capture
latency is negative. The offset between the capture clock and IEX's send clock
is unknown, so an absolute IEX feed latency and the skew against a Nasdaq stamp
are not claimed.

The rolling "recent IEX days" list (`tools/iex_hist.py recent --n 5`: the
latest five HIST days with TOPS and DEEP+, `research/v117/iex_recent_manifest.json`)
is listed only: no approval sentence, not downloaded, and `tools/iex_download.py`
refuses it.

## 6. Provenance: the data manifest and pre-ingest hashes (v1.18 REL-12)

- **Manifest.** `research/data_manifest.json` lists every dataset on the data
  root (`tools/data_manifest.py generate --data-root data/real`): path,
  `dataset_version`, every vendor input's file, sha256 and bytes as recorded at
  ingest, the TOPS oracle capture a `process_report.json` records, the
  `KEEP.json` sha256 of a processed-and-deleted day, and the purpose / spend
  status the v1.18 baseline records. It reads `dataset.json`, `KEEP.json` and
  `process_report.json` only, never the raw or normalized files. The cited
  synthetic and legacy versions are listed with their provenance (`non_disk`).
- **CI.** The governance job runs `tools/data_manifest.py check`: every dataset
  version a committed preregistration or ledger cites must be listed. The data
  is git-ignored, so the committed manifest is the list; regenerate and commit
  it after each ingest (`check --data-root data/real` says when it is stale).
- **Hashes before an ingest.** `tools/r7_download.py` and `tools/iex_download.py`
  hash while streaming and refuse a file whose sha256 differs from the
  manifest's (default since v1.18; `--no-verify-sha` restores the size-only
  check). A manifest without a sha256 gets the computed one in the result,
  marked `sha256_verified: false`, to pin before the ingest.
- **Known gap.** The TOPS captures of 2026-05-15 and 05-18 are in
  docs/research/IEX_OVERLAP_SET.md but in no small file on disk, so the
  manifest reproduces 8 of that document's 10 hashes
  (`python/tests/test_data_manifest.py` pins the two missing).
- **State at v1.18.0.** The committed manifest lists 39 datasets
  (`research/data_manifest.json`, regenerated from the data root).

## 7. IEX HIST terms of use

> **TODO (owner):** record the IEX HIST terms here (the terms page URL, the
> date read, and what they say about research use, redistribution and
> attribution). The committed documents only state "free, no account, T+1"
> (§1); the terms themselves are not recorded anywhere in the repository, and
> a Claude session cannot read them without a download. Until this is filled
> in, nothing derived from IEX data beyond aggregate results is committed
> (docs/REAL_DATA.md, "Never redistributed").

## 8. Tiingo end-of-day (v2.0 free route; licence id `tiingo`)

Adapter `python/src/iap/data/sources/tiingo.py` (contract `tiingo.daily`). Pages read on 2026-10-25: the pricing page
(`tiingo.com/about/pricing`), the end-of-day documentation (`tiingo.com/documentation/end-of-day`) and the product page.
The terms-of-service document itself was not reachable by URL (`/about/terms`, `/about/terms-of-service`,
`/about/terms-of-use` return 404); what follows is the pricing page's licence row and its footnote.

| fact | public text (quoted) | source |
|---|---|---|
| free tier | "$0/month"; "Max requests per hour: 50"; "Max requests per day: 1,000"; "Unique symbols per month: 500"; "Max bandwidth per month: 1 GB" | pricing page |
| licence row | Starter and Power tiers: "Internal Use Only" | pricing page |
| footnote | "Internal use means you may only use the data for your own personal use" and "you may not display or share the data with another person or organization" | pricing page |
| commercial | "$50/month (or $499/year) for internal commercial use"; "For redistribution use, our pricing is explicit and predictable on our Product pages" | pricing page |
| adjustments | "This methodology incorporates both split and dividend adjustments" (raw and adjusted columns both served: `adjClose`, `divCash`, `splitFactor`) | end-of-day documentation |
| coverage | "Data going back from 1962"; the supported-tickers list (`apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip`) is public and lists inactive names with their last date | product page, documentation |
| card | the page makes no statement; sign-up is "Sign-Up for Free" (DR-6: the owner will not place a card; an account without one is the condition for this source) | pricing page |

**Use intended**: ingestion by `iap-runner` into the sealed partitions of U0 section 2; development and validation
reads by the owner's identity; aggregate research results (ledgers, reports) committed; no price row, no quote, no
derived series redistributed. **Permitted?** Personal internal use of the free tier is what the page grants; this
repository is one person's non-commercial research, which fits "your own personal use". Two points are not settled by
the public page and are questions for the owner (section 14): whether committing aggregate statistics computed from the
data is "sharing the data" (the repository's standing rule already commits aggregates only), and whether the free tier's
500 unique symbols per month allows a 500-plus-name universe plus delisted names (it does not in one month; ingestion
would be spread over months or use Stooq for breadth). The key lives in `TIINGO_API_KEY`, is read by the adapter and
never printed.

## 9. Stooq bulk daily files (v2.0 free route; licence id `stooq`)

Adapter `python/src/iap/data/sources/stooq.py` (contract `stooq.daily`). Pages tried on 2026-10-25: the bulk database
listing (`stooq.com/db/h/`) and the help pages (`stooq.com/pomoc/?q=11`, `?q=29`, `?q=1`). **None was readable from this
session**: the site answers a scripted request with a JavaScript proof-of-work verification page ("This site requires
JavaScript to verify your browser") and a browser with a consent dialog whose manage-options control did not open; the
help pages returned empty bodies. No terms text is therefore quoted here.

| fact | status | source |
|---|---|---|
| what the archive is | `d_us_txt.zip`, daily ASCII files for US listings, one file per symbol, documented layout `data/daily/us/<market>/` | Stooq's documentation as known to the adapter; not re-read on 2026-10-25 |
| terms of use | **not read** | the pages above were not readable |
| adjustment | the contract declares `split_dividend`; DD1's corporate-action test verifies it on sample days before any fit | contract default, not a vendor statement |

**Use intended**: the same as Tiingo (sealed ingestion of the bulk archive, aggregates only committed). **Permitted?**
Unknown until the owner reads the terms in a browser session and records them here (question in section 14). Until
then Stooq is the listed fallback for breadth, not the primary source, and `fetch()` stays refused for everyone.

## 10. Wikipedia constituent tables (v2.0 free route; licence id `wikimedia`; a labelled proxy)

Adapter `python/src/iap/data/sources/wiki_membership.py` (contract `wikipedia.index_membership`, every interval
labelled `wikipedia-proxy`). Pages read on 2026-10-25: the Wikimedia Foundation Terms of Use (sections 4, 7 and 12),
`Wikipedia:Reusing Wikipedia content`, and `API:Etiquette` on mediawiki.org.

| fact | public text (quoted) | source |
|---|---|---|
| licence | text is licensed under the "Creative Commons Attribution-ShareAlike 4.0 International License" ("CC BY-SA 4.0") | Terms of Use section 7.1 |
| attribution | credit "Through hyperlink (where possible) or URL to the page or pages that you are reusing" | Terms of Use section 7.7 |
| share-alike | modified or added text must be licensed "under CC BY-SA 4.0 or later" and the change indicated | Terms of Use section 7.8 |
| automated use | prohibited when "abusive or disruptive of the services"; "By using our APIs, you agree to abide by all applicable policies governing the use of the APIs" | Terms of Use sections 4 and 12 |
| etiquette | "Set an informative User-Agent string with contact information, or you may be IP-blocked without notice"; make requests "in series rather than in parallel"; use `maxlag` for non-interactive tasks | API:Etiquette |
| reuse | "To re-distribute a text page in any form, provide credit to the authors" | Reusing Wikipedia content |

**Use intended**: read past revisions of two list pages through the public API, serially, with the repository's
User-Agent (adapter declaration); derive dated membership intervals (ticker, start, end, revision id) as an input of the
point-in-time universe; store the intervals in the sealed partitions and the development set; commit counts and the
proxy label, never the page text. **Permitted?** Yes for reading and for deriving facts: the API is public and the use
is serial and small (about 500 requests for a coverage run). The derived interval table is a database of facts rather
than a copy of the text; if any such table is ever committed or published, it will carry the CC BY-SA 4.0 attribution
(page URL and revision ids) and the proxy label, and the share-alike clause then applies to that table (question in
section 14 on whether the owner accepts CC BY-SA for a published membership table).

## 11. SEC EDGAR filing indexes (v2.0 free route; licence id `sec_edgar`)

Adapter `python/src/iap/data/sources/edgar.py` (contract `edgar.filings`). Pages read on 2026-10-25: "Accessing EDGAR
data" (`sec.gov/os/accessing-edgar-data`), the SEC privacy and security policy (`sec.gov/about/privacy-information`),
the developer page (`sec.gov/developer`) and the EDGAR APIs page.

| fact | public text (quoted) | source |
|---|---|---|
| reuse | SEC.gov content "may be copied or further distributed by users of the web site without the SEC's permission"; "consider appropriate citation to the SEC as the source" | privacy and security policy |
| rate | "Current max request rate: 10 requests/second"; the SEC "reserves the right to limit request rates to preserve fair access for all users" | accessing EDGAR data |
| user agent | declare one in the form "Sample Company Name AdminContact@<sample company domain>.com" | accessing EDGAR data |
| automation | "unclassified" bots or automated tools are not allowed to crawl the site; "We reserve the right to block IP addresses that submit excessive requests" | developer page, privacy policy |
| APIs | "These APIs do not require any authentication or API keys to access"; the submissions object holds "at least one year's of filing or to 1,000 (whichever is more) of the most recent filings"; "data.sec.gov does not support Cross Origin Resource Scripting (CORS)" | EDGAR APIs page |
| trademarks | the SEC seal, logos and the EDGAR name are not to be used in a trade name or to imply affiliation | privacy and security policy |

**Use intended**: filing form types, filing dates and acceptance timestamps of 8-K, 10-Q and 10-K filings by CIK, from
the full index and the submissions API, at most eight requests per second (adapter limiter), with a declarative
User-Agent; no filing body is read. **Permitted?** Yes: public government data with a stated access rule that the
adapter enforces. One operational point: the contact in the User-Agent is read from `IAP_SEC_CONTACT` and defaults to
the repository URL; the SEC's example uses an e-mail address, and the owner decides which contact to declare (section
14). Scripted requests from the session's environment were answered 403 by the SEC's CDN on 2026-10-25 whatever the
User-Agent, so the live coverage call runs from the owner's workstation (coverage report, section 2).

## 12. Kenneth French data library (v2.0 free route; licence id `kenneth_french`)

Adapter `python/src/iap/data/sources/french.py` (contract `french.factors_daily`). Pages read on 2026-10-25: the data
library index and the Fama/French factors description page.

| fact | public text (quoted) | source |
|---|---|---|
| copyright | the only statement on the page: "Copyright Eugene F. Fama and Kenneth R. French" | data library index |
| coverage | daily factors "July 1, 1926 - August 31, 2026"; the market factor is the "value-weight return of all CRSP firms incorporated in the US and listed on the NYSE, AMEX, or NASDAQ" | factors description page |
| update | "We reconstruct the full history of returns each month when we update the portfolios" (no posted schedule for the daily files) | data library index |

**Use intended**: three zipped CSVs of daily factor returns downloaded by `iap-runner`, used as the factor model's
inputs for D2 and the residual step, cited as "Kenneth R. French data library" in every report; never redistributed.
**Permitted?** The library is published for free download with no terms page; academic practice is use with citation.
The absence of an explicit licence is recorded as a question for the owner (section 14), not a blocker: the files are
inputs, nothing derived from them is republished, and the citation is given.

## 13. IEX HIST (v2.0 free route for intraday breadth; licence id `iex_hist`)

Section 7 above holds the owner's open note D-22: the IEX HIST terms are not yet recorded in this repository. The free
route keeps IEX HIST as the intraday source (DR-6; adapter and parser work are B2b's, not this PR's). Read on
2026-10-25 from the IEX product page: HIST is described as "Free historical data downloads from IEX equities available
on a T+1 basis" and the fees table lists Equities HIST as "Free"; the page links "Terms", "Web Portal Terms &
Conditions" and "View Market Data Documentation and Agreements" without naming a licence for HIST. The owner's note
(D-22) stays the record of the terms; this section is its placeholder, so that every source of the route has a licence
id.

## 14. Licence questions for the owner (not blockers; defaults in brackets)

1. **Tiingo**: does committing aggregate statistics computed from Tiingo data count as "display or share the data"?
   [default: no; the repository commits aggregates only, as for every vendor]. Is the 500-unique-symbols-per-month
   free-tier limit acceptable for a universe above that size? [default: ingestion spread over months; Stooq for breadth
   once its terms are read].
2. **Stooq**: read the terms of use in a browser session and record them in section 9 [default until then: Stooq is
   the fallback, `fetch()` refused].
3. **Wikipedia**: is a CC BY-SA 4.0 attribution and share-alike acceptable on any published membership table derived
   from the revisions? [default: yes, with page URL and revision ids; nothing published yet].
4. **SEC EDGAR**: which contact goes into the User-Agent (`IAP_SEC_CONTACT`)? [default: the repository URL].
5. **Kenneth French**: accept use with citation in the absence of a terms page? [default: yes].
6. **IEX HIST**: D-22 stands (record the terms).
