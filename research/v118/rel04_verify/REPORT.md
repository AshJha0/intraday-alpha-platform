# REL-04 capture-ts verification: result under the pre-declaration

Declaration: `research/v118/rel04_verify/predeclaration.json`. Identity: **pass**. Features: built.

Failures first.

| day | pass | rows ref | rows capts | non-receive mismatches | receive_ts differing | capts receive < exchange | failed clauses |
|---|---|---|---|---|---|---|---|
| 2025-11-28 | yes | 1969356 | 1969356 | 0 | 1969356 | 0 | - |
| 2025-12-08 | yes | 5590938 | 5590938 | 0 | 5590938 | 0 | - |
| 2025-12-09 | yes | 4829986 | 4829986 | 0 | 4829986 | 0 | - |
| 2025-12-10 | yes | 5614106 | 5614106 | 0 | 5614106 | 0 | - |
| 2026-05-15 | yes | 7268375 | 7268375 | 0 | 7268375 | 0 | - |
| 2026-05-18 | yes | 4884147 | 4884147 | 0 | 4884147 | 0 | - |

Feed latency recorded in dataset.json (within-capture only; the offset to IEX's send clock is unknown):

| day | n | negative | negative share | mean ns | min ns | max ns | ~p50 bucket | ~p99 bucket |
|---|---|---|---|---|---|---|---|---|
| 2025-11-28 | 148115121 | 0 | 0.000000 | 37000390172.709335 | 37000009603 | 37791514984 | 35 | 35 |
| 2025-12-08 | 317211767 | 0 | 0.000000 | 37000387900.44857 | 37000009056 | 38144101028 | 35 | 35 |
| 2025-12-09 | 295426880 | 0 | 0.000000 | 37000451532.06289 | 37000009335 | 37892426191 | 35 | 35 |
| 2025-12-10 | 428701502 | 0 | 0.000000 | 37001024592.36578 | 37000009002 | 37727298722 | 35 | 35 |
| 2026-05-15 | 437934866 | 0 | 0.000000 | 37000845662.91092 | 37000009182 | 38249720543 | 35 | 35 |
| 2026-05-18 | 548325540 | 0 | 0.000000 | 37004658254.79991 | 37000008991 | 37948897194 | 35 | 35 |

Inference: none: no p-value, interval or comparison across days is claimed; counts are exact

Can claim:
- on six IEX overlap days the capture-timestamp ingest changed receive_ts and nothing else in events.parquet (or exactly which columns and rows differ)
- the recorded within-capture feed-latency distribution per day, as a descriptive count summary
- features for the six capture-timestamp datasets exist, built by the declared command

Cannot claim:
- the absolute IEX feed latency: the capture clock's source and its offset to IEX's send clock are unknown (iex.CAPTURE_TS_PROVENANCE)
- the skew against any Nasdaq stamp
- that negative latencies are errors or that positive ones are physical delays
- any signal, edge, fill or profit result, any holdout result, any significance
