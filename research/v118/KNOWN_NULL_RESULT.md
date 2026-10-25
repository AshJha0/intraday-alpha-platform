# REL-06 known-null study: PASS, size untested (cost-dominated null) (v1.18, protocol `research/v118/NULL_PROTOCOL.md`)

Run 2026-10-18 13:27-14:16 BST (49 min, `--workers 2`, the IEX ingest running beside it) from a clean checkout of
`origin/main` at `b3be34f`, under the signed prereg `research/v118/null_protocol_prereg.json` (identity bound at
`4a6f473`; verified by the runner: signature, scripts, code tree, commit ancestry, data version, family, metrics,
thresholds), runner actor `claude-rel06-runner` (not the signer). Dataset `data/real/multi7`
(`ee4c2187...`, `in_sample`). Canonical output: `research/v118/known_null_result.json` (sha256 `5731a1ab...605081`; since v2.0 stage 11 a release asset of v1.18.0, not a tree file: `python tools/result_artefacts.py fetch research/v118/known_null_result.json`; 1,000 trials per generator,
every seed, permutation and AR(1) parameter recorded). No trial outcome was examined before the run completed.

> This inference rests on G = 7 session clusters. For G <= 5 the known-null protocol reports size, not power.
> Intervals are Student-t at G-1 = 6 degrees of freedom; the minimum detectable effect and the effective sample
> size are printed beside every interval.

## Verdict: PASS, size untested: cost-dominated null (flagged over-conservative in every cell)

| generator | K | family-level false confirmations (95 % Wilson) | per hypothesis, worst (95 % Wilson) | pooled per hypothesis | pass | over-conservative |
|---|---|---|---|---|---|---|
| A (session derangements) | 3 | 0 / 1000 = 0.00 % (0.00-0.38 %) | 0 / 1000 (0.00-0.38 %) | 0 / 3000 (0.00-0.13 %) | yes | yes |
| A | 6 | 0 / 1000 = 0.00 % (0.00-0.38 %) | 0 / 1000 (0.00-0.38 %) | 0 / 6000 (0.00-0.06 %) | yes | yes |
| A | 16 | 0 / 1000 = 0.00 % (0.00-0.38 %) | 0 / 1000 (0.00-0.38 %) | 0 / 16000 (0.00-0.02 %) | yes | yes |
| B (AR(1) noise) | 3 | 0 / 1000 = 0.00 % (0.00-0.38 %) | 0 / 1000 (0.00-0.38 %) | 0 / 3000 (0.00-0.13 %) | yes | yes |
| B | 6 | 0 / 1000 = 0.00 % (0.00-0.38 %) | 0 / 1000 (0.00-0.38 %) | 0 / 6000 (0.00-0.06 %) | yes | yes |
| B | 16 | 0 / 1000 = 0.00 % (0.00-0.38 %) | 0 / 1000 (0.00-0.38 %) | 0 / 16000 (0.00-0.02 %) | yes | yes |

Pass rule (section 7): point <= 5.0 % and Wilson upper <= 7.0 %, per hypothesis and family level, every
generator and K. Over-conservative flag: Wilson upper < 2.0 % (reported, does not fail). Eligibility (REL-02d):
the registered configuration is gate-eligible. Every p was the Webb wild-cluster full enumeration (6^7).

## Read this before citing the pass

The pass is **uninformative about the statistical test's size**. Under both generators the gross P&L is zero-mean
by construction, and every trip pays the recorded spread, fees, impact and completion charge (mean fixed cost
1.34-1.62 bps per trip by hypothesis; `trip_tables` in the JSON). So the true net mean per round trip under these
nulls is about -1.4 bps, deep inside the one-sided null region (net <= 0), not on its boundary. Measured: the
median per-trial session-mean net is -1.34 to -1.52 bps across cells, the largest of any trial is -0.48 bps, and no
raw (pre-Holm) p-value in any trial is <= 0.05. The machinery cannot manufacture a confirmation of noise **because
noise loses the spread**; whether CR1 + Webb at G = 7 holds its nominal 5 % at a zero-net boundary is not tested
by this protocol as written.

Consequence for gate F3: the protocol is met as registered, but its power to detect an over-sized test is nil.
The remedy is a new registered protocol (section 7: "a new registered protocol, not a threshold edit"), for
example a cost-neutral null (gross labels shifted so the net mean is exactly zero per hypothesis) or the same
generators applied to the gross statistic, before the outside reviewer (section 9.3) approves the method.
