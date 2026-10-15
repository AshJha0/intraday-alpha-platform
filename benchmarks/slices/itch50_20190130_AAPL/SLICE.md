# Sample slice `itch50_20190130_AAPL` (committed real-session workload)

Written by `tools/extract_real_slice.py`; the provenance below is also in `dataset.json` (`sessions.2019-01-30.slice`). Benchmark workload only: not a research dataset, listed in no pre-registration.

| field | value |
|---|---|
| source dataset | `ds3_20190130` (`dataset_version` `06419d749910a8f0…`) |
| vendor file | `01302019.NASDAQ_ITCH50.gz` sha256 `8c97b5b13bc451c0…` |
| day / symbol | 2019-01-30 / AAPL (instrument 1, venue XNAS = 101) |
| window start | 2019-01-30T09:30:00-05:00 (`1548858600000000000` ns UTC) |
| window | 64000 events, original sequences 30200..94199, `1548858600000036157`..`1548858723554188841` |
| prefix | 1132 book-reconstruction ADDs (orders resting at T0) |
| book at T0 | 1132 orders, 257 bid / 225 ask levels |
| book at end | 39774 orders, 2702 bid / 2651 ask levels |
| window event types | ADD 49482, CANCEL 9197, EXECUTE 2119, MODIFY 125, STATUS 1, TRADE 3076 |
| output | `normalized/eq_20190130.normalized.iap1` 4689536 bytes, sha256 `ce37133c2b41dfaf038df9bfc51126404674603cdf387b8d44f4cbd976b47848` |
| command | `python tools/extract_real_slice.py --dataset C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/ds3_20190130 --date 2019-01-30 --symbol AAPL --start 09:30:00 --events 64000 --out benchmarks/slices/itch50_20190130_AAPL` |
