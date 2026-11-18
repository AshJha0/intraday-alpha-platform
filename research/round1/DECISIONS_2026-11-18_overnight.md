# Decisions taken on the owner's behalf, 2026-11-18 evening to 2026-11-19 morning

The owner's words, 2026-11-18 about 21:00: "from now till tomorrow morning including during
night time, when I will be sleeping keeping laptop open, I authorise you to take decisions on
my behalf (no money spend, no card details), so that you don't need to wait for me". Earlier
the same evening: "I authorise you to merge PR, release, tag and other things", and on the one
sealed holdout look, "Yes, you sign".

This file lists every decision taken under that delegation, why, and how to reverse it. The
delegation covers this round only and ends on the morning of 2026-11-19. Standing rules that
stayed in force: no spend; no second review; no sign-ups; the standing exclusion of one named exchange group;
no gate weakened to get a pass; no release on a failing or unfinished test run; negative results
reported as found.

| # | time | decision | why | how to reverse |
|---|---|---|---|---|
| 1 | 20:35 | Stopped the first bars chain (it had hung for 33 minutes on a stalled download with no timeout), deleted its partial download and relaunched it with a socket timeout and retries | the job could not finish | none needed: no result was produced by the hung run |
| 2 | 20:40 | The relaunched chain downloads all ten in-sample days again and also writes the closing-auction rows; existing bars are kept and each re-made file must be byte-identical or the day fails | family F3 needs the auction messages, and the raw files had been deleted | delete `data/real/bars/auction_*.csv` |
| 3 | 20:55 | Added `python/src/iap/graph/research_flow.py` to the pass-through list of `test_no_prereg_consumer_skips_the_identity_check` | the module builds an unsigned draft that carries an `analysis_identity` block and reads no signed document; the test's own list exists for that case. No runner was exempted from its identity check | remove the line; the test then fails until the module is registered as a runner |
| 4 | 21:00 | The sealed holdout look is governed by the rule written before any pooled result (`FAMILIES_PRESPEC_2026-11-18.md`, section 5): a candidate needs a positive ten-session mean net, a one-sided p below 0.00357 (session-level t above 3.46) and at least 7 of 10 positive sessions; if none qualifies the holdout stays sealed. The owner's "yes" is applied through that rule, not instead of it | spending the only untouched data on a candidate that failed its own pre-written bar would waste it, and lowering the bar would weaken a gate | the owner may register a look by a different rule at any time; nothing is spent if none qualifies |
| 5 | 22:40 | `iap.research.pnl_power` no longer evaluates the noncentral t itself; it calls `iap.validation.confirmatory.power_at_t` and `mde_at_t`. The generated power table is byte-identical | the repository allows one noncentral-t implementation (`test_r1d_one_mde_implementation`); my module was a second one. The rule was kept and the code changed | revert the commit; the test then fails |
| 6 | 22:45 | The Rust test count quoted in the README parity table and nine other documents went from 376 to 388, with a sentence saying the new figure is counted from the source tree (twelve new `itchbars` tests) and not yet from a main CI run | `check_headline_numbers.py` compares the table with the tree | re-capture the table from the first main CI run after the release |
| 7 | 22:50 | `ml/test_ladder.py::test_runtime_bound` failed once in the full run while the machine was under heavy load and passed when run alone; no code or threshold was changed | a timing bound measured under contention; changing it would weaken a gate | none needed |
| 8 | 23:20 | The pinned checksum in `test_grid_mode_byte_identical_to_before` now hashes integers exactly and floats to nine decimals, and the pins were recomputed from the pre-change code (commit eb8405f3) with that checksum; the current code reproduces both | the original pin hashed raw float text, which passed on Windows and failed on the Linux CI runners because the same sums differ in the last bit. The check still covers every fill, counter and P&L line | restore the raw-text hash; it then fails on Linux |
| 9 | 23:25 | The fixture of `test_parity_counts.py` mirrors the README parity table, so its Rust count went from 376 to 388 with decision 6 | the test compares the README with counts derived from the fixture | revert with decision 6 |
