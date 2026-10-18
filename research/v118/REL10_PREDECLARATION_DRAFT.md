# REL-10 volume-clock OFI and hidden-print imbalance: pre-declaration (DRAFT)

**Status: DRAFT, 2026-10-18. Open parameters are marked `OPEN`. Not set, not signed, not registered.**
The coordinator sets every `OPEN` value and signs under REL-02a (signature binds script fingerprint,
commit, dataset versions, family, metrics, thresholds). No real-data read before the signature; each
study read is one ledger look; an edit after the first look voids the signature. Code:
`python/src/iap/research/volume_clock_ofi.py`; tests `python/tests/test_v118_volume_clock_ofi.py`
(synthetic fixtures only).

## 1. Hypothesis (alpha review hypothesis 5)

Signed flow, order-flow imbalance (OFI) and its persistence, and hidden-print imbalance over the
trailing 1-5 minutes, measured on volume-clock rows, carry the sign of the next 1-5 minute mid move
net of the spread. Treated as an **execution input first**: a maker's side choice and a taker's
timing. The taker use is the least plausible (alpha review); the maker use is evaluated separately
and is not rescued by the taker result or vice versa.

## 2. Data

- Sessions `OPEN-1` (each checked against `holdout_registry.json`); symbols `OPEN-2`.
- Rows: `volume_clock_rows(tape, n_shares)`; `OPEN-3` `n_shares` (one number for every symbol, or a
  declared per-symbol rule; REL-07 note: a single N gives liquid names more rows).
- Trade signs: EXECUTE resting side from the book; TRADE (hidden) print `side` read as the resting
  side. `OPEN-4`: confirm this convention against the ingest for each venue before signing.

## 3. Signals and target (fixed before the look)

- Signals: `ofi_<w>m`, `flow_<w>m`, `hid_imb_<w>m` for w in `OPEN-5` (proposal {1, 3, 5});
  persistence `corr(ofi_prev_<w>m, ofi_<w>m)` is descriptive only.
- **Registered target**: the spread-netted label `sign(signal) * gross_h - cost_h` (bps), gross the
  log mid return over `[ts, ts + h]`, cost the half spreads at entry and exit; h in `OPEN-6`
  (proposal {1, 5} minutes). Labels with `complete = False` are excluded.
- Thresholds `OPEN-7` (one per signal, set from a rule that does not read the study sessions, e.g.
  a fixed quantile of a declared earlier day).

## 4. Execution evaluation (priced, not assumed)

- Taker (`evaluate_taker`): touch entry capped at displayed depth (partial fills), exit at the far
  touch after h, non-overlapping; size `OPEN-8`.
- Maker (`evaluate_maker`): passive entry behind the displayed queue for `OPEN-9` entry window,
  passive exit behind the far-touch queue for `OPEN-10` exit window, remainder crossed; size as
  taker.
- Every trip reports gross, cost and net USD (`net = gross - cost`, checked); unfilled quantity
  and the **missed opportunity** (unfilled qty times the gross move) are reported beside P&L,
  never inside it. Fees `OPEN-11` (list prices from the venue file).

## 5. Metrics and decision rule

- Primary unit: per-session net USD, taker and maker separately (`summarise`, built on
  `study_stats.summarise_study`: every session counts, no interval below 3 sessions).
- **Decision** `OPEN-12` (proposal): per (use x signal x window x horizon) cell, "candidate" only if
  the Bonferroni-adjusted session-clustered lower bound of net USD is above zero and the missed
  opportunity does not exceed `OPEN-13` times the net; "reject" if the interval lies below zero;
  otherwise "underpowered" / "iterate". A positive rank IC or a positive gross label is not by
  itself a continuation reason. No promotion in v1.18.

## 6. Family, MDE and small-G statements

- Family K = |uses| x |signals| x |windows| x |horizons| (proposal 2 x 3 x 3 x 2 = 36), `OPEN-14`;
  Bonferroni at 1 - 0.05/K unless the signed null protocol says otherwise.
- **MDE**: `(t_{0.975, G-1} + z_{0.80}) * sd / sqrt(G)` on the per-session net; at G = 7 about
  1.27 sd (normal approximation 1.06 sd, as `power.mde_usd`), larger after the family correction.
  Rows inside a session overlap (busier periods give denser rows): no row-count standard error is
  used; the session is the cluster.
- **Small-G**: inference rests on G session clusters; for G <= 5 the result is a size statement,
  not a power statement, and no generalisation beyond these sessions is claimed.

## 7. Not claimed

Reconstructed passive fills (back of the displayed queue, no cancel-ahead credit, Level-1 fill
model only, F8); hidden prints are seen only after they print; no latency is modelled in the
taker path (exchange timestamps).
