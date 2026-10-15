# IEX HIST conformance pass: 2026-10-13 TOPS 1.6 (v1.17, A1)

Owner-approved download of ONE feed-day, 2026-10-15: the first 250 MB of
`20261013_IEXTP1_TOPS1.6.pcap.gz` (the whole file is 9.74 GB; a gzip prefix
decompresses sequentially, so the slice is the first 2 h 27 m of the day,
07:07:32 to 09:34:54 New York, 1.17 GB of pcapng). The file was deleted
after the pass; only this summary and
`IEX_HIST_CONFORMANCE_20261013_TOPS.json` (counters, no vendor bytes) are
kept. Tool: `tools/iex_hist_conformance.py`.

| check | result |
|---|---|
| container | **pcapng** (section header, one interface, enhanced packet blocks), not classic pcap as the HIST page says; `iap.marketdata.iex` reads both |
| packets / segments | 7,253,833 UDP packets, every one an IEX-TP v1 segment; 2,694 heartbeats; 0 non-IPv4, 0 non-UDP, 0 VLAN tags |
| message protocol | TOPS `0x8003` only; one session id |
| sequence | 0 gaps over 7,619,846 messages |
| message types | `Q` 7,425,025 · `T` 125,422 · `I` 28,606 · `P` 13,889 · `H` 13,282 · `O` 13,274 · `A` 339 · `S` 3 · `D` 3 · `X` 3; **0 unknown types, 0 short blocks** |
| system events | `O` 07:07:32, `S` 08:00:00, `R` 09:30:00 (New York), in order |
| timestamps | every message inside the session date |
| quotes | 0 crossed; 437,045 one-sided (an empty side is price 0 / size 0, as specified); sizes never negative |
| symbols | 13,274 quoted; **3 directory rows** (`D` covers IEX-listed securities only, so AAPL / MSFT / QQQ have none: the reader synthesises a round-lot-100 row for a universe symbol and counts `directory_synthesised`) |
| capture clock | capture stamp minus IEX-TP send time = 37.00 to 38.14 s: the capture host stamps in TAI (TAI - UTC = 37 s). The reader takes `exchange_ts` from the messages' own UTC timestamps, so this does not matter for the data; `receive_ts` is not derived from the capture stamp |

What is NOT covered: DEEP / DEEP+ captures (not downloaded) and the TOPS-vs-DEEP
oracle on a real day, which needs both feeds of one day (about 20 GB).
