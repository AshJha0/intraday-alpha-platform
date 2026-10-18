# REL-09 cross-venue stale-quote protection: pre-declaration (DRAFT)

**Status: DRAFT, 2026-10-18. Open parameters are marked `OPEN`. Not set, not signed, not registered.**
The coordinator sets every `OPEN` value and signs under REL-02a (the signature binds the analysis
script fingerprint, commit, dataset versions, family, metrics and thresholds). No real-data read
happens before the signature; each study read is one ledger look. Any edit after the first look
voids the signature. Code: `python/src/iap/research/stale_quote.py`; tests
`python/tests/test_v118_stale_quote.py` (synthetic fixtures only).

## 1. Hypothesis (alpha review hypothesis 4)

A causal cross-venue stale-quote flag, used to pull a resting maker quote, raises the maker's net
P&L. The headline hope is that it removes **25 %** of the adverse-selection (markout) line; that
figure is the **hypothesis, not the criterion**. The criterion is on incremental net maker P&L
including the priced cost of the fills the filter prevents (section 5).

## 2. Data

- Sessions: the Nasdaq/IEX overlap days (`docs/research/IEX_OVERLAP_SET.md`). `OPEN-1`: the exact
  list of G sessions, each checked against `holdout_registry.json` (state and prior looks; the B8
  out-of-sample days 2025-12-10, 2026-05-15, 2026-05-18 have been read by B8 for a different
  question; whether they count as fresh for REL-09 is the coordinator's call and must be written
  here).
- Stream: `two_venue.load_merged_session` / `merge_events` with `iex_clock="exchange"`; both
  `exchange_ts` and `receive_ts` kept. Decisions on `receive_ts`, evaluation on `exchange_ts`.
  Receive times carry `two_venue.SKEW_QUALIFICATION`; no latency figure is published from them.
- Symbols: `OPEN-2` (proposal: AAPL, MSFT; QQQ only if the machine is idle).
- Quoting venue: `OPEN-3` (proposal: XNAS, with IEXG as the "other" venue; IEXG-as-quoter is a
  second, separately labelled family member if included).

## 3. Indicator (fixed before the look)

Side stale at `t` iff quote age >= `min_age_ns` and the other venue's mid has moved against the side
by >= `min_displacement_ticks` since the side's last best-price change, both read on receive time.

- `OPEN-4` `min_age_ns` (proposal: 1 ms) and `OPEN-5` `min_displacement_ticks` (proposal: 1.0).
  One pair only, or a declared grid whose size enters the family K (section 6). No tuning on the
  study sessions.

## 4. Quoter (both policies, paired on the same events)

`QuoterConfig`: lot `OPEN-6` (proposal 100), refresh `OPEN-7` (proposal 1 s), cancel latency
`OPEN-8` (proposal 350 us, the REL-04 IEX inbound figure, as a stress value), markout horizon
`OPEN-9` (proposal 1 s; 100 ms and 500 ms reported as secondaries, not decision metrics), rebate /
fee per share `OPEN-10` (from `research/execution/b8/venues_xnas_iexg_v2.json`, indicative list
prices), inventory cap `OPEN-11` (proposal 5 lots). Join-the-touch, back of the displayed queue,
cancels ahead not credited (conservative). Inventory is marked to the consolidated session-end mid.

## 5. Metrics and decision rule

- **Primary unit**: per-session incremental net maker P&L, USD, filtered minus baseline
  (`summarise_sessions`): every listed session counts (no fills = 0).
- Decomposition reported per session and in total: spread captured, rebates and fees, markout,
  inventory; and by fill set: missed fills (baseline fills the filter prevented, fully priced),
  extra fills, common fills. Identities `net = spread + fees + markout + inventory` and
  `d_net = net(extra) - net(missed) + d_net(common)` are checked in the output.
- Hypothesis metric (descriptive): adverse-selection reduction `1 - markout_filtered /
  markout_baseline`, compared with 25 %.
- **Decision** (`OPEN-12`, proposal): "candidate" only if the session-clustered 95 % t interval
  (G-1 df) of incremental net USD lies above zero AND the mean exceeds the MDE floor `OPEN-13`;
  "reject" if the interval lies below zero or the point estimate is <= 0 with the interval
  excluding the pre-declared minimum economic effect `OPEN-14`; otherwise "underpowered" or
  "iterate". A positive adverse-selection reduction with non-positive incremental net is a reject,
  never a candidate. No promotion: there is no untouched confirmation sample in v1.18.

## 6. Family, MDE and small-G statements

- Family K = number of (symbol x quoting venue x indicator setting) cells, `OPEN-15`; correction
  Bonferroni at 1 - 0.05/K unless the signed null protocol says otherwise.
- **MDE**: at G sessions the minimum detectable mean incremental net is
  `(t_{0.975, G-1} + z_{0.80}) * sd / sqrt(G)`; at G = 7 that is about **1.27 sd** of the
  per-session incremental net (normal approximation 1.06 sd, reported by `summarise_sessions` as
  `mde_usd`). sd is not known before the look; the output states the MDE at the observed sd and
  the effective sample size (G).
- **Small-G**: inference rests on G session clusters; for G <= 5 the result is a size statement,
  not a power statement, and no generalisation beyond these sessions is claimed. "Underpowered" is
  a permitted verdict.

## 7. Not claimed

Fills are reconstructed, not observed: queue position at the back of the displayed level, no hidden
liquidity ahead, no cancel-ahead credit, fill model validated at Level 1 only (F8). Receive-time
ordering is the capture host's, with an unidentified clock offset. Fees are list prices.
