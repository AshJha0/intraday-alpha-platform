# Breadth book pre-registration (v1.17 C8): DRAFT, unsigned

Rendering of `research/preregs/breadth_book_prereg_DRAFT.json`. Status: **DRAFT. Not signed,
not on the blackboard, not approved for any run.** Claude drafted it; the owner signs and registers
it from `.venv-ci` with `--require-ci-env`.

## Decision: option (a), chosen by the owner on 2026-10-16

The owner chose **option (a)** (owner's words: "go with your recommendation"; the recommendation was (a)): the book is registered with the C6 screen as a hard gate. The confirmatory run refuses every candidate, the expected outcome is no confirmatory look (a documented negative result), the holdout stays unread, and no breadth holdout dataset is built or downloaded under this registration. The draft is still unsigned: signing it is the owner's confirmation. Options (b) and (c) below are kept as the record of what was considered.

### Options considered

The C6 net-of-cost screen this pre-registration depends on, `research/breadth/screen/SCREEN_multi7.md`
(data `research/breadth/screen/screen_multi7.json`, sha256 `b860740c…91ac`), reads:

> **0 pass, 22 fail, 1 not screened** of 23 candidates.

No candidate passed, so the draft offered three options:

| option | what it means | expected outcome |
|---|---|---|
| **(a)** Register the book with the screen as a hard gate | the confirmatory run passes `--require-screen`; every book candidate failed or was not screened, so the run refuses before it reads any data | no confirmatory look: a documented negative result. The holdout stays unread |
| **(b)** Register a named candidate with a screen override | candidate **EQ10/1s/linear_z_v2** (book score `eq10_beta_gap`). The run omits `--require-screen`, and the ledger records the override (screen path, sha256, FAIL status, the owner's reason) | one confirmatory look on the holdout. The prior is strongly negative |
| **(c)** Defer C8 to v1.18 | no registration in v1.17, which ships the in-sample book. New candidates come from screening the 100-name breadth days (2019/20 only) or from a different size | nothing charged. The holdout stays unread |

Why EQ10/1s/linear_z_v2 for (b): the screen's gate statistic is the net day-clustered t. Among the
candidates the book can run (`eq11` → EQ11/15m, not screened; `eq10_beta_gap` → EQ10/1s), it has the
best reading. Screen-wide, the best reading is EQ09/10s/linear_z_v1, but the book has no score for it.

| candidate | gross IC | net IC | net t (HAC) | net t (day) | net / trip | cost / trip | days | pairs | result |
|---|---|---|---|---|---|---|---|---|---|
| EQ10/1s/linear_z_v2 | 0.0702 | -0.5932 | -39.13 | -19.12 | -1.975 bps | 2.015 bps | 6 | 1465672 | fail (all four net rules) |
| EQ10/1s/linear_z_v1 | 0.0757 | -0.5922 | -39.11 | -21.49 | -1.972 bps | 2.015 bps | 6 | 1465920 | fail (all four net rules) |
| EQ09/10s/linear_z_v1 (no book score) | 0.0058 | -0.4612 | -15.12 | -8.93 | -1.987 bps | 2.015 bps | 6 | 1464987 | fail (all four net rules) |
| EQ11/15m/linear_z_v1 (`eq11`) | | | | | | | | | not screened |

## Hypothesis

The cross-sectional book runs on the breadth holdout: the 2025/26 Nasdaq days, for the breadth names
outside AAPL / MSFT / QQQ. It uses the registered score, the cost-aware optimizer and the registered
notional. The hypothesis is that it earns a mean daily net P&L (gross − spread − fee − impact) with a
day-clustered t ≥ 2.0, a positive mean rank IC, and a deflated Sharpe ≥ 0.95 at the registered N.

## Book (C7 report defaults, `book_multi7.json`)

The score is `eq11` under (a) and `eq10_beta_gap` under (b). The other settings are:

- linear rank weights, `alpha_bps` 5
- rebalance every 5 min on the feature-row clock: max age 60 s, exec delay 1 s, flat at session end
- dollar, beta and **sector** neutral, bound 0. Sectors come from `configs/reference/sectors.json`, a public-knowledge taxonomy; the owner may replace it with a vendor file. Sector neutrality is off only on the 3-name multi7 report
- notional 1,000,000, with a size sweep of 0.25× to 4×
- gross cap 1, `w_max` 0.1, participation cap 1 % ADV, turnover cap 0.5
- risk aversion 1000, EWMA λ 0.94, Dykstra projection
- impact from the cost model's sqrt rule (100 bps at one ADV)
- universe: `research/breadth/universe.json` minus AAPL, MSFT, QQQ

## Statistic

- **Confirmatory:** mean daily net P&L, with a t clustered by session day (n_days − 1 df), from `breadth_report.cluster_mean`.
- **Secondary:** the mean rank IC in the v1.17 gate form (`gate_statistics`, STATS_REVISION v1.17, methods v2), day-clustered, sign +1.
- **Deflated Sharpe:** `deflated_sharpe_block` on the daily net P&L at the registered N = `distinct_experiments` + 1 at the signing commit. At this draft N is **410** (409 + 1 on main `c5a9b11`), recomputed at signing. The holdout's own ledger is never used (rule R-1).
- **Acceptance:**

  | statistic | threshold |
  |---|---|
  | net t | ≥ 2.0 |
  | mean net | ≥ 0 |
  | rank IC | ≥ 0 |
  | DSR | ≥ 0.95 |
  | days | ≥ 9 |

## Holdout (plan rule, from manifests only)

The holdout days are the 2025/26 Nasdaq days for the breadth names outside AAPL / MSFT / QQQ:

**2025-11-28, 2025-12-08, 2025-12-09, 2025-12-10, 2025-12-11, 2025-12-12, 2026-05-15, 2026-05-18, 2026-06-12**

The list was computed from the `sessions` and `universe` keys of the `dataset.json` manifests of
`r7_2025*`, `r7_20260612` and `holdout2026`. No feature, label or statistic was read. Every existing
dataset on these days holds AAPL / MSFT / QQQ only, and those names are spent on R7. The JSON lists
those nine dataset_versions.

No breadth dataset exists on these days yet. After signature the holdout dataset is built under a
path containing `holdout`, and its dataset_version is added before the single confirmatory run.

## Screen dependency, ledger, ordering

The screen is `research/breadth/screen/screen_multi7.json`, sha256 `b860740cb7230dc69372ec9c71214766a597f730a7f7415c8b19b7bb605891ac`,
with status 0 pass / 22 fail / 1 not screened. The status for each book candidate:

| book candidate | status |
|---|---|
| EQ11/15m/v1 | not screened |
| EQ10/1s/v1 | fail |
| EQ10/1s/v2 | fail |

The ledger is charged as follows:

- registration: one `prereg` look
- confirmatory run: one `breadth_book` look, scoped to the holdout dataset
- option (b) only: one override entry
- the screen's 23 `net_of_cost_screen` looks are already charged

Ordering: sign → register → build the holdout dataset → add its version → one run → outcome on the board.

## Dry run

There is no validator for the `iap.prereg_draft/v1` format, and `WriteBroker.preregister` has no
`--dry-run` flag. `python/tests/test_v117_breadth_prereg_draft.py` checks the draft instead:

- the schema, the shared prereg keys and the unsigned or undecided state
- the screen sha256 and counts
- the option (b) numbers against the screen
- the holdout days
- the screen gate the registration shares: it calls `require_screened` and `breadth_book.screen_check` and confirms that each book candidate is refused

Nothing is registered or signed.
