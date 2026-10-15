# Breadth universe: the selection rule (v1.17, A3)

Fixed BEFORE any breadth statistic is computed (owner decision 2026-10-15; this
file is the record; the owner registers it on the blackboard with the breadth
pre-registration, plan C8).

**Rule (`breadth_universe/v1`):** the top 100 Nasdaq-listed symbols by displayed
continuous-session volume on **2019-01-30** (shares executed against displayed
orders, ITCH `E` and printable `C`, between system events `Q` and `M`); ETFs
(`etp_flag Y`) kept and flagged; test securities (`authenticity T`) excluded;
opening / closing crosses and non-displayed prints are reported beside the
ranking and do not rank. The universe is fixed on the selection day: a symbol
that later delists, halts or changes id stays in the list and is reported as
missing on that day (survivorship is explicit, never silently repaired).

**Why displayed volume on one in-sample day:** it is the quantity the
execution studies work against (posting, taking, routing all see displayed
depth), it needs no price history, and one day fixes the list before any
2025/26 statistic exists. The holdout for names outside AAPL / MSFT / QQQ is the
2025/26 Nasdaq days (plan C8): no statistic was ever computed on those names
there.

**How it is measured:** `python -m iap.marketdata breadth-universe --input
01302019.NASDAQ_ITCH50.gz --date 2019-01-30 --top 100 --out
research/breadth/universe.json` (one streaming pass; `iap.marketdata.breadth`;
nothing downloaded). `universe.json` records the rule version, the source
file's sha256, every symbol's three volumes, the ETF flags, the next 50 and the
excluded test securities.

**Status:** rule fixed 2026-10-16; `universe.json` produced 2026-10-15 from the
approved 2019-01-30 download (`UNIVERSE_2019-01-30.md`).
