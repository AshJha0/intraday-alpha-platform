<!-- E0 FREEZE HEADER (path header block; not part of the frozen text; `python/tests/equivalence/test_e0_frozen.py`
strips everything up to and including the END line below and hashes the remainder)
path: research/v119/H1_ALPHA_IMPROVEMENT_SPEC.md
source: E0_H1_ALPHA_IMPROVEMENT_SPEC_rev6.md (E0 revision 6, 2026-10-24 night; the owner's freeze copy, kept outside the repository)
frozen sha256 of the text below this header, byte for byte: e1099e33464c797687297fea75ac34f7bbdab817181f8b66028837751ef0f8b1
(the frozen bytes carry CRLF line endings; .gitattributes marks this file -text so git never normalises them)
freeze decision: E0 revision 6 frozen as the H1 economic specification under the v2.0 unified programme (plan rev 3, U0 rev 1
section 3 "Stage 5"); committed by the stage 5 engine PR (session B, EN3/EN4/EN5, H1 reference and equivalence test)
rule: the text below is frozen. Any change, however small, is a NEW E0 revision: it needs a new freeze decision and a note of
what was observed before the change; this header changes only when that happens. Nothing below is adjustable after an
outcome has been observed; the H1 reference implementation under research/v119/h1_reference/ is written from this text.
END E0 FREEZE HEADER -->
# H1 economic specification (E0, DRAFT revision 6, 2026-10-24 night; planning only; **freeze requested**, section 20)

Revision 6 applies the eighth review's two final accounting corrections (E0-5.1 flatness is a mandatory target, not an
observed-state invariant; E0-5.2 the primary P&L is the cash/position identity, the waterfall an independent
decomposition) and its qualification on the feasibility thresholds. Section 20 lists the changes and requests the
design freeze: after this revision, changes to the H1 contract happen only through the PRs that implement it, each
naming the clause it implements, with the reviewer reading the PR, not a new revision of this file.

Revision 5 closes the seventh review's three remaining accounting items (its E0 disposition): the primary P&L
equation now states its reference-price convention and reconciles to the waterfall term by term (section 10); a
cohort with an outstanding quantity after expiry has a defined state, valuation and attribution (section 7); and
"meaningful" feasibility has numbers (section 6 item 7). Section 19 lists the changes. The six revision-4 design
choices stand as conditionally accepted.

Revision 4 applied the fifth review ("Independent Review, IAP v1.19 E0 Revision 3", APPROVED WITH NAMED CHANGES,
PLANNING ONLY; `C:\Users\ashis\Downloads\IAP_v1.19_E0_Rev3_Independent_Review_Decision_2026-10-24.md`): C1 the
entry-hurdle equation was wrong by construction (it required 2.5x round-trip cost while the text intended 1.5x) and is
corrected; C2 the weak-forecast fallback is removed, the horizon fails closed; C3 the no-trade band is fully specified;
C4 the portfolio projection becomes one fixed constrained least-squares repair with fail-closed infeasibility; C5 cohort
conservation is restated on filled allocations with a deterministic fill-allocation rule; C6 one fill-price convention
with no double counting of latency or spread; C7 retention arithmetic defined; C8 timestamps; C9 effective breadth
labelled and a weight-aware diagnostic added; C10 "simulated" wording; C11 three kinds of session; C12 is applied to the
specification (revision 8). Section 18 lists the changes. Nothing registered beyond these rules, nothing frozen, changed.

Revision 3 answered the fourth review ("Independent Quant Review: Improving Alpha, Sharpe and Trading Profit",
`C:\Users\ashis\Downloads\IAP_v1.19_Independent_Quant_Alpha_Review_2026-10-24.md`), which approved the v1.19
direction with targeted economic-alpha amendments and asked that revision 2 be checked contract by contract and only
the gaps amended. The check is `E0_REV2_GAP_TABLE_FOURTH_REVIEW.md` (7 satisfied, 9 partial, 4 missing); section 17
lists what changed. Every change is a diagnostic, a ledger field or an accounting rule; the registered strategy, the
family, the gate, the hurdle and the frozen documents are untouched.

Revision 2 superseded revision 1 of the same file. Revision 2 answers the third reviewer's design review ("make v1.19 an
economic-research release: one hypothesis through discover, forecast, construct, execute, measure, validate"). The
ten changes they insisted on are all adopted: a portfolio-level net session return with a fixed capital convention as
the primary outcome; a frozen, development-only expected-return forecast in bps; K = 2; cohort accounting with two
ledgers and conservation invariants; explicit portfolio constraints with refusal and underinvestment; lifecycle-cost
trading decisions; a stronger data-suitability test that separates statistical breadth from tradable breadth; a new
E5 economic baseline and attribution package; E4 power on the complete filtered strategy's session statistic; no
change to R0 or X0 and no undeclared selection. Where revision 2 qualifies their advice it says so (section 14).

Nothing here reads data, runs, signs or touches the repository. Every value marked "proposed" is fixed in the X1
prereg before T0 and never after a look. The frozen R0 gate, the X0 eligibility memo and the sealing policy are
consumed, not changed.

## 1. Hypothesis in three propositions; one confirmatory outcome

- **H1-P1 predictability** (diagnostic): extreme intraday idiosyncratic moves over a formation window are followed
  by partial reversal over a fixed horizon.
- **H1-P2 monetisation** (development feasibility): for a mechanically defined eligible subset, the expected reversal
  stays positive after conservative lifecycle costs.
- **H1-P3 portfolio profitability** (the only confirmatory outcome): an intraday-only, dollar- and beta-neutral,
  sector-balanced, constrained, overlapping-cohort portfolio of those trades has positive expected net session return
  on a fixed capital convention.

P1 and P2 are reported; neither is a registered hypothesis. Falsification of P3: the canonical gate does not reject
on the sealed window, or E4 shows the registered effect unreachable, or E3 shows the route cannot support the estimand.

## 2. Data, point-in-time universe and tradable breadth

| item | rule (proposed) |
|---|---|
| route | the one chosen after `DATA_ROUTE_PROPOSAL_v1.19.md` (all-IEX free or all-vendor); never mixed within the experiment |
| statistical universe | constituents of QQQ and SPY per the issuers' dated daily holdings files with an **as-of availability rule**: a holdings file dated D is usable from the first session after D + 1 business day (publication lag), never on D itself; identifier changes, delistings, splits and mergers handled by a registered mapping table built before T0; survivorship: names that later delist stay in the universe for the sessions they were constituents |
| quote eligibility (E3) | quotes on the chosen venue in at least 90 % of the session's grid minutes; quote age at the decision timestamp at most 5 s; no locked or crossed quote at the decision minute (else the name is ineligible at that rebalance, never repriced) |
| **tradable universe** (E3 reports it separately) | the quote-eligible names that also pass: price >= $5; venue-volume floor (set by E3 from calibration months, stated as venue volume); displayed depth at the touch at least the intended child size at the median rebalance; **shortable**: in the absence of borrow data, the short side excludes the bottom liquidity tercile and any name on a registered hard-to-borrow proxy list, stated as an assumption, with a sensitivity that excludes the bottom half |
| reference returns | market = SPY; sector = the SPDR sector ETF per name (REL-07 map extended), from the same venue's mids at the same timestamps; the sector return is **orthogonalised against the market return** on the estimation window so the two coefficients are identifiable (the collinearity handling the reviewer asked for) |
| timestamps (C8) | three instants per decision: t_data = the data cutoff, every input has receive time <= t_decision (no separate pre-decision safety lag; the cutoff is the decision instant itself); t_decision = the grid time; t_arrival = t_decision + the declared entry latency (proposed 500 ms), the instant the simulated order reaches the venue; stock and ETF mids are taken from the same clock; event-time versus receive-time handling is stated per route |
| grid | 5-minute decision grid 09:45 to 15:30 for 30-minute holdings and 09:45 to 15:45 for 15-minute holdings; **every cohort expires by 16:00; a flat book at the close is the mandatory target** (intraday-only rule; a closeout failure is represented, valued and flagged per sections 7 and 10, never hidden) |
| data split | calibration = sessions before T0: development = first two-thirds by date, check = last third; sealed window = sessions after T0 and after D2's isolation tests, read once at registered completion |
| exclusions (mechanical, declared) | names halted that session; names with a corporate action effective that day; sessions failing the R0 section 3 coverage rule |

## 3. Signal: one frozen factor model, no look-ahead, binary eligibility

- Factor model over formation window F (15 or 30 min):
  r_i(F) = b_im r_m(F) + b_is r_s_perp(F) + e_i(F), with betas from the previous 20 sessions' 5-minute returns (no
  same-day data), the sector return orthogonalised against the market, shrinkage toward (1, 0) with a fixed weight
  (proposed 0.3), minimum 15 valid sessions else ineligible.
- Standardised residual z_i = e_i(F) / s_i, s_i = trailing 20-session standard deviation of the name's F-minute
  residuals; floor s_i at a declared minimum (proposed the 5th percentile of the universe's s on the estimation window)
  so near-zero volatility cannot manufacture a large z.
- Signal S_i = - z_i. **Invalid or missing inputs yield an ineligible name, never a signal**: incomplete formation
  window, stale (> 5 s) or crossed quote, ETF and stock timestamps out of synchrony beyond 1 s, corporate-action
  discontinuity, ill-conditioned factor estimate (condition number above a declared bound).
- No momentum, order-flow, sentiment, nonlinear or regime features in the confirmatory strategy. Regime results are
  reported by bucket (E5), not modelled.

## 4. Expected-return forecast in bps (frozen, development-only, outcome-dependent and recorded as such)

- One pooled linear reversal coefficient per registered horizon H: mu_hat_i(H) = - rho_H x e_i(F) in bps, with rho_H
  estimated on the development subset by least squares of the future factor-adjusted H-minute return on the residual,
  shrunk toward zero by a fixed rule (proposed: James-Stein-type shrinkage with the shrinkage weight fixed at
  estimation time from the fit's standard error, formula written in the prereg).
- The fit is outcome-dependent estimation on development data. It is recorded in the ledger as an **outcome
  exposure** (section 12), it is frozen for the check subset and the sealed window, and no alternative model is fitted
  or compared.
- **Fail closed (C2).** If rho_H fails the registered quality rule on the development subset (95 % interval includes
  zero, or rho_H negative, or the fit's effective sample below the m_min of R0 section 3), that horizon **generates no
  new trades anywhere** (development check, calibration looks, sealed window): its sessions are usable sessions with
  zero trades and enter R_d as zero under section 10. There is no alternate rule, no ranking-only fallback, no refit.
  The quality rule is itself an outcome-dependent activation decided once on development data; it is recorded as an
  outcome exposure (section 12) and the verdict for a horizon that fails it is "no forecast, no trades, variant
  retained in the family at K = 2" (the Holm family is not shrunk after the fact). A missing forecast is no trade.
- The forecast is used for the entry hurdle (section 8) and for nothing else: weights are not scaled by mu_hat
  (limits estimation noise); E5 reports forecast calibration (bias, slope, uncertainty by decile).

## 5. Registered family: K = 2

| id | formation F | holding H | factor adjustment |
|---|---|---|---|
| H1-15 | 15 min | 15 min | market + sector (orthogonalised) |
| H1-30 | 30 min | 30 min | market + sector (orthogonalised) |

Chosen now for simplicity and power, before any outcome comparison; market-only variants are dropped by design, not
by result. Holm over K = 2 at alpha_op 0.020 (first-step threshold 0.010). Decay diagnostics at other horizons (E5)
are reported and never promote a third horizon.

## 6. Portfolio construction: deterministic, feasible, refusable

Target weights at each decision time per variant, as fractions of the **registered capital C** (proposed
$1,000,000; the denominator of every return in this document):

1. Eligible set: names in the tradable universe with a valid S_i and a forecast clearing the entry hurdle (section 8).
2. Legs: long the top and short the bottom of S_i among eligible names (proposed 20 % tails of the eligible set at
   that rebalance, minimum 10 names per leg).
3. Raw weights: inverse trailing volatility within each leg, each leg summing to the cohort gross target.
4. **Projection onto hard constraints (C4): one fixed constrained least-squares repair**, w* = argmin ||w - w_raw||^2
   subject to: sum w = 0 (dollar-neutral); |beta' w| <= 0.05 (proposed; same betas as the signal); per-sector
   |sum_{i in s} w_i| <= 2 % of C (proposed); |w_i| <= 2 % of C; sign(w_i) = sign(w_raw_i) or w_i = 0 (a long never
   becomes a short); sum |w_i| <= cohort gross cap; |w_i| <= the liquidity cap of item 5. A strictly convex quadratic
   objective over linear constraints has a unique solution, so no tie-break is needed; solver: the repository's
   existing bounded least-squares routine or SciPy's `lsq_linear`/`minimize` with method, tolerance (proposed 1e-9 on
   the constraints, in weight units) and iteration cap fixed in the prereg and recorded with the implementation
   identity. **Fail closed**: infeasible, non-converged or tolerance-violating solutions skip the cohort (recorded as a
   refusal with the active constraint set); there is no sequential repair pass. This is portfolio repair toward the
   inverse-volatility raw book, not an alpha optimiser: w_raw does not depend on forecasts and no risk-aversion or
   covariance parameter enters.
5. Participation and liquidity: each child order at most a declared fraction of the venue's trailing 5-minute
   volume in that name (proposed 5 %); a name whose order would exceed it is scaled down, then dropped if below a
   minimum ticket.
6. **No-trade band (C3)** and netting against the executable ledger (section 7). Unit: the aggregate net order per
   name at a decision time, in bps of C. Rule: after the projection (item 4), the liquidity scaling (item 5) and the
   netting of entering against expiring cohorts, a **discretionary** net order (one that is neither a mandatory expiry
   exit, a risk liquidation nor a pre-close liquidation) with |net order| below the band is not sent and the existing
   aggregate position stands; mandatory orders are always sent in full. Threshold: one value chosen once on the
   development subset from the grid {2, 5, 10} bps of C by the outcome-blind objective "smallest threshold whose
   turnover reduction relative to no band exceeds 10 %", tie to the smaller value; if none qualifies, the band is 0
   (no band). Feasibility: if withholding an order leaves the executable book outside a hard constraint, that order is
   sent regardless of the band (the band never overrides a constraint). Application is at the aggregate-order level
   only; cohort ledgers record the intended quantity as not submitted (section 7). Fixed in X1 before T0.
7. **Refusal and underinvestment (meaningful feasibility, revision 5)**: a cohort is **skipped** (no orders, recorded
   as a refusal with the binding constraint) if any of the following holds after the item-4 repair and the item-5
   liquidity scaling: fewer than 10 names per leg; deployed gross below 50 % of the cohort gross target (proposed);
   any single name above 15 % of the cohort's gross (proposed); the repair infeasible or out of tolerance. Otherwise
   the cohort trades at the reduced size the rules produced; nothing is re-scaled up to restore gross. **Returns are
   always divided by C**, never by deployed gross, so unused capital shows as zero return, not as leverage. The two
   proposed percentages are fixed in X1 before T0, are not chosen on returns, and are **never relaxed after an
   unfavourable result**; E3 reports how many rebalances they leave feasible and E4 whether that leaves enough power
   (eighth review, choice 6).

Capital allocation across overlapping cohorts: C is split equally across the number of cohorts that can be
simultaneously live (3 for 15-minute holdings on a 5-minute grid, 6 for 30-minute), so total gross never exceeds C.
Hard constraints: dollar, beta, sector, single name, gross, participation. Diagnostics only: industry, size, momentum,
liquidity and volatility exposures (reported in E5, not constrained).

No covariance-based optimiser in v1.19; the existing breadth-book optimiser is not used for H1's confirmatory book
(it introduces tunable parameters); the item-4 repair has no covariance, risk-aversion or forecast input. The revision-1 development rules for a turnover setting are withdrawn.

## 7. Cohort accounting: two ledgers, conservation invariants

- **Research cohort ledger**: every signal, intended position, formation time, expiry time, forecast, and
  theoretical attribution per cohort and name. Per proposed trade (revision 3, the fourth review's trade-level
  economic ledger): signal timestamp (when every input became available), forecast in bps and horizon, current
  aggregate position, target position, net order, expected entry cost and expected exit cost (conservative column),
  cost cushion B = mu_hat - (C_entry + C_exit), risk-constraint status and liquidity status (pass or refuse, with the
  reason), incremental market and sector exposure, incremental portfolio variance under the frozen diagnostic
  covariance (the 20-session sample covariance of 5-minute residuals; diagnostic only, never used for weights),
  expected net contribution, simulated fill and subsequent P&L (every fill in this document is **simulated**; C10),
  and the attribution of that P&L to signal, cost, risk and execution. These fields make every accept or reject
  decision auditable; they change no decision.
- **Quantity states (C5)**, per name and decision time, in the executable ledger and per cohort: intended (target
  change from the research ledger), submitted (after band and netting), filled (simulated), allocated (filled
  quantity assigned to cohorts), outstanding (submitted minus filled, carried to the next decision time for mandatory
  orders, cancelled for discretionary ones). **Allocation rule**: a net fill is allocated first to expiring cohorts'
  exits in expiry order (oldest first), then to entering cohorts pro rata to their intended quantities, integer shares
  by largest remainder, ties to the lower cohort id; the rule is deterministic and recorded. A partially filled expiry
  liquidation leaves the unfilled remainder as an **outstanding mandatory order** on the executable book, re-sent at
  the next decision time, valued meanwhile at the stress column and flagged; it is never erased and a forced valuation
  is never reported as a completed liquidation.
- **Residual expired-cohort positions (revision 5).** A cohort whose expiry exit is not fully filled enters the state
  `closing` (between `live` and `closed`): its remaining quantity stays allocated to it in the executable ledger, its
  mandatory exit order is re-sent at every decision time until filled or until the pre-close liquidation, and any P&L
  accruing on the residual after the scheduled expiry is attributed to **execution residual**, not to the signal
  (the research ledger's signal attribution closes at the scheduled expiry at the then-current mid). A `closing`
  residual counts toward gross, the single-name cap and participation like any position, so a new cohort cannot be
  sized as if the residual were gone. If a residual survives the close (halt through the close), section 10's
  unpriced-position rule applies. The count and gross of `closing` residuals is reported per session.
- **Executable portfolio ledger**: aggregate holdings per name, net orders (the difference between the aggregate
  target and the current aggregate holding at each decision time, after the no-trade band), simulated fills, fees,
  execution costs, realised and unrealised P&L marked at mid, cash, and capital usage.
- Netting: an expiring cohort's exit and a new cohort's entry in the same name produce one net order; costs are
  charged on net executed quantity exactly once.
- Invariants, tested in CI on synthetic fixtures and asserted on every real run: sum of **allocated (filled)** cohort
  quantities equals aggregate holdings (intended and submitted quantities are reconciled separately: intended =
  submitted + withheld; submitted = filled + outstanding + cancelled); aggregate position changes equal executed net fills; gross exposure never exceeds the cap; cash
  plus marked positions reconciles to equity within the declared tolerance; every cost charged exactly once; partial
  fills create no fictitious positions; no expired cohort is in state `live` after its expiry (it is `closed` or
  `closing`); **closeout (E0-5.1)**: every unit of inventory at the close is either liquidated by a simulated fill or
  carried as explicitly represented residual inventory with the stress-column valuation and a `closeout_failure` flag
  naming the cause (halt, no quote, depth exhausted); no closing fill is ever generated without a quote and depth to
  support it; residual inventory opens the next session as inventory in Equity_open (section 10) and is liquidated by
  a mandatory order at the first valid quote, its P&L attributed to execution residual; a loss is charged once, in the
  session whose valuation moved.
- Independent reconstruction: E2's acceptance is zero unexplained difference between the portfolio ledger's net P&L
  and a separately written cash/position/cost reconstruction.

## 8. Trading decisions with lifecycle costs

- Expected net alpha for a prospective entry: A_net = mu_hat - C_entry - C_exit, where entry and exit costs are the
  **conservative** column (section 9) for the actual order size and the name's conditions at t.
- **Entry rule (C1, corrected)**: with C_RT = C_entry + C_exit under the conservative column, entry only if
  **mu_hat > 1.5 x C_RT**, equivalently A_net > 0.5 x C_RT. The multiplier is fixed at **1.5** on gross forecast over
  round-trip cost (not optimised). Revision 3 wrote A_net > 1.5 x C_RT, which is mu_hat > 2.5 x C_RT and was not the
  intent; the corrected rule is the registered one. Entry also requires that the
  portfolio remains feasible after the projection of section 6; otherwise no entry (recorded as a rejected trade with
  its reason).
- Holding: an open cohort is held to expiry; entry costs are sunk and are not re-charged at hold decisions; the
  only in-life changes are the mandatory expiry exit, a risk liquidation (beta or sector tolerance breached after
  fills, halt, data gap) and the netting of section 7. No signal-driven early exit in v1.19.
- Exits: at expiry, aggressive at the conservative column; a position that cannot be priced or exited under the
  declared model (missing exit quote, halt through the close) is valued at the last valid mid minus the stress column
  and flagged; the treatment is fixed here, never chosen after seeing P&L.
- States of the deterministic machine: ineligible -> eligible -> forecast -> hurdle -> feasibility -> net order ->
  fill or refusal -> live cohort -> expiry or risk exit -> closed. Every failed check yields no new trade.

## 9. Execution model and cost columns

Four layers, named in every table: quoted market conditions (observed); deterministic marketable-order simulation
(model); impact and slippage sensitivity (model, uncalibrated); actual execution evidence (**not available**; E1).

- **One fill-price convention (C6)**: P_fill = M_arrival + side x (spread_arrival / 2 + impact), where M_arrival and
  spread_arrival are the mid and quoted spread at t_arrival (C8), and impact is a deterministic function of order
  size over displayed depth at the touch at t_arrival and over trailing 5-minute venue volume (coefficients fixed;
  the three columns differ only in the impact multiplier 1x / 1.5x / 2x and in the depth assumption below). The
  decision-to-arrival mid movement (M_arrival - M_decision) is **not** a cost term: it is reported as a separate
  attribution line ("latency drift") in the waterfall, so the waterfall reconciles exactly to fill-price P&L against
  the decision mid. Fills limited to displayed depth at t_arrival (conservative and stress columns: 75 % and 50 % of
  displayed depth, to reflect depth that is not there on arrival), the remainder refused (not filled at a worse price
  by assumption).
- Columns: base (arrival half-spread, dated fees, impact 1x, full displayed depth), conservative (arrival
  half-spread, dated fees, impact 1.5x, 75 % of displayed depth), stress (arrival half-spread, dated fees, impact 2x,
  50 % of displayed depth). No column counts the spread or the latency movement twice. The decision quantity and the
  primary outcome use the **conservative** column.
- Failure cases handled deterministically: quote disappears before arrival (refuse), insufficient depth (partial,
  remainder refused), price through the entry threshold (refuse), participation breach (scale or refuse), halt during
  life (risk exit at first valid quote after resumption, else stress valuation), exit liquidity deterioration (stress
  column), data gap (stress valuation and flag), mandatory pre-close liquidation.
- Passive execution: a labelled sensitivity only, expected value = P(fill) x E[P&L | fill] minus opportunity cost,
  with the hazard model's P(fill) stated as unvalidated; it never rescues a strategy that fails under the
  conservative aggressive case.
- Financing: intraday-only, no overnight financing; borrow cost assumed zero for the easy-to-borrow set and the
  short-side exclusions of section 2 state the assumption.

## 10. Primary outcome, session value, economic screen

- Session value for a variant: R_d = NetPnL_d / C, in bps of the registered capital. **Primary P&L identity (E0-5.2,
  one convention, general under netting, partial fills and residuals)**:
  NetPnL_d = Cash_close + sum_i q_i,close x V_i,close - Equity_open, where Cash is the executable book's cash after
  every signed simulated fill at its section-9 fill price (conservative column) and every fee; q_i,close is the
  aggregate inventory at the close (zero when the closeout target is met); V_i,close is the registered closeout
  valuation (the last valid mid under the stress column, section 7); Equity_open = Cash_open + sum_i q_i,open x
  V_i,open with V_i,open the previous session's V_i,close for carried residual inventory, so no loss is charged twice
  or omitted across sessions. No entry-to-exit pairing is used in the primary identity.
  Reference-price convention for the **waterfall** (section 11), an independently reconstructed decomposition of the
  identity above, never its definition: midpoint-reference P&L is measured from the decision
  mid at entry to the decision mid at the scheduled expiry; **latency drift** = (M_arrival - M_decision) summed over
  entries and exits with sign; **half-spread** = spread_arrival / 2 per fill; **impact** per fill; **fees**;
  **execution residual** = P&L on `closing` residuals after scheduled expiry (section 7). Identity enforced by E2's
  reconstruction: midpoint-reference P&L + latency drift - half-spread - impact - fees + execution residual =
  NetPnL_d, exactly, per session, with every term from the executable ledger. The former wording "minus slippage"
  is withdrawn: there is no slippage term separate from latency drift and impact.
- **Three kinds of session (C11)**: a *usable session with no trades* (eligibility, hurdle, band or refusals left
  nothing to trade, or the horizon failed closed under section 4) contributes R_d = 0 and is counted; a *session
  failing R0 section 3 coverage* is mechanically dropped before analysis under R0's rule and listed; a *session with an
  unpriced open position at the close* (halt through the close, missing exit quote) is a usable session whose R_d
  carries the stress-column valuation of that position and a failure flag, never a silent zero. The counts of all
  three are reported with every result.
- The canonical gate tests H0: E[R_d] = 0 against E[R_d] > 0, one-sided, Holm over K = 2, session as the unit.
- **Economic screen, separate from significance** (proposed, with its justification): the registered economic
  alternative for E4 and the hurdle for a `candidate` verdict is **E[R_d] >= +1.0 bps of C per session under the
  conservative column, with gross-to-net retention >= 50 %** (net at least half of gross). Justification: at the
  expected turnover (about 6 cohorts x 2 legs x 20 % tails, netted) the conservative cost column is itself of the
  order of 1 to 2 bps of C per session, so the hurdle requires the strategy to earn at least as much as it pays in
  costs; it is not derived from the revision-1 per-name figure, which had no denominator. **The 1 to 2 bps cost figure
  is a planning assumption, not trading evidence; E2 and E5 replace it with measured development-subset costs (C7).**
  E4 may show it unreachable; then stage 5 is RED or GREEN-REGISTER-ONLY, and the hurdle is not lowered.
- **Retention arithmetic (C7)**: retention = sum over sessions of NetPnL_d / sum over sessions of GrossPnL_d
  (aggregate, not an average of per-session ratios), with GrossPnL_d the midpoint-reference P&L before any cost;
  defined only when the aggregate gross is positive; if the aggregate gross is zero or negative the screen **fails**
  (no retention value is reported) and the result is "not viable" or "insufficient evidence" by the E5 tree.
- **Screen versus gate**: the R0 gate tests E[R_d] = 0 against > 0; passing it does not establish E[R_d] >= +1.0 bps.
  The screen is a separate requirement on the point estimate with its interval reported; a `candidate` verdict needs
  both the gate and the screen, and neither substitutes for the other.
- No Sharpe target. Net Sharpe from the session series with a block-bootstrap interval is report-only.

## 11. Risk dashboard and P&L waterfall (every run, every report)

Per session: net return (primary), maximum drawdown and 5 % expected shortfall of the session series (diagnostic);
per rebalance: market beta, sector exposures, gross and net exposure, concentration (hard controls, breaches refuse
the rebalance); turnover per rebalance and session; cost-to-gross-alpha ratio per trade and session; participation per
order; worst-session contribution over the sample. Waterfall per session and cumulative (revision 5, matching section
10 exactly): midpoint-reference P&L, plus latency drift, minus half-spread, minus impact, minus fees, plus execution
residual, equals net; financing is zero intraday and shown as a zero line; components non-overlapping by construction
of the reference convention and reconciled to zero by E2. Capacity: the full strategy re-run at 0.5x, 1x, 2x and 5x C, reporting the
share of orders exceeding touch depth, participation, estimated impact, refused orders, net executable exposure,
missed opportunity and concentration in the least liquid names; labelled model sensitivities, never market capacity.

Revision 3 additions (fourth review), all diagnostic, the registered construction fixed:

- **Cost of neutrality table**, per session and cumulative, one row per construction stage: raw eligible signal book;
  dollar-neutral; beta-neutral; sector-constrained; single-name and gross capped; participation-limited; executable
  net-order book. Columns: expected gross alpha (sum of forecasts x weights), modelled conservative cost, market beta,
  largest sector net, gross, number of names. The table shows what each constraint costs in alpha and buys in risk; no
  stage is selected afterwards.
- **Exposure-efficiency diagnostic**: R_gross_d = NetPnL_d / (declared gross-exposure denominator, the cohort gross
  targets actually deployed), reported beside the canonical R_d = NetPnL_d / C and labelled so; capital utilisation =
  deployed gross / C per rebalance. Survivors of the liquidity filters are never re-scaled to restore full gross.
- **Prohibited Sharpe inflation**, enforced by E5's acceptance test and by the report template: no omission of
  no-trade or refused sessions (they enter R_d as zero); no removal of regimes or dates; no re-scaling of returns after
  seeing their distribution; no leverage change after seeing results; no annualisation over active periods only; no
  exclusion of costly liquidations; no crediting of unfilled or refused orders with the forecast return; one fixed
  denominator C for the confirmatory statistic.
- **Effective breadth**: eligible-name count, the average pairwise residual correlation rho_bar on the development
  subset, the effective number of independent bets N_eff = N / (1 + (N - 1) rho_bar) (equal-weight approximation,
  stated as such) and the incremental variance reduction from the last decile of names added; reported because the
  diversification multiplier sqrt(N / (1 + (N-1) rho)) saturates under residual correlation, so symbol counts overstate
  breadth. **N_eff is a labelled equal-weight, equal-variance illustration, not an effective-bet count for the
  constrained long-short book (C9)**; the book-level diagnostic is the **variance-contribution table** under the
  frozen diagnostic covariance: share of portfolio variance by name (top 10 and Herfindahl), by sector, and by the
  first five principal components, with the share of variance explained by the market and sector factors after
  neutralisation. Neither number is used to set weights in v1.19.

## 12. Selection accounting (the experiment ledger)

Every development action is one of: **analysis event** (a recorded run), **candidate configuration** (a parameter
or model evaluated), **outcome exposure** (a result that could influence a choice), **registered hypothesis**,
**calibration look** (authorised check-subset analysis), **sealed analysis** (the single final one). The family's
selection history carries all of them. Declared development actions: E3 pilot (analysis event, no outcome
exposure); quote-quality, liquidity, depth and participation thresholds chosen on coverage, staleness, spread, depth
and participation only (no outcome exposure); the no-trade band chosen on turnover and feasibility only (no outcome
exposure); the two rho_H fits (outcome exposures, one per horizon); the E5 baseline tables on the development subset
(outcome exposure); E4 (reuses E5 summaries); X0; X1; X2 calibration looks (two, one per variant); X3 (two). No
forecast-model comparison on the check subset; no revision on the sealed window. The revision-1 rule that chose the
spread multiple on gross IC is withdrawn (it was an outcome exposure presented as outcome-blind).

**Firewall (fifth review, section 4).** Development diagnostics, including every section-17 and section-18
addition, may support the E5 viability decision but may not change H1's factor model, horizons, eligibility rule,
forecast method, portfolio construction, execution rule, cost column, capital, family or statistical gate. Any such
modification is a new candidate with its own selection history, registered separately, never the frozen H1. The
tunables that exist are listed with their fixing rule: the two rho_H fits (development, outcome exposure, frozen); the
weak-forecast quality rule (fail closed, section 4); the no-trade band (outcome-blind grid, section 6); the E3 venue-
volume floor, quote-age bound and condition-number bound (outcome-blind, fixed before any outcome analysis, logged);
the shortability exclusion (diagnostic sensitivity, no switching); capacity multiples and latency steps (reported,
the primary 1x and 500 ms never switched after results); the E5 verdict (stop, or a separately registered candidate).

## 13. E3, E4 and E5 contracts (what stage 5 must produce before X0)

- **E3 (data and tradability suitability)**, criteria fixed before the pilot: point-in-time membership with the as-of
  rule; quote coverage and quote age at decision timestamps; stock and ETF synchronisation; locked and crossed quote
  frequency; venue representativeness against Nasdaq mids on the six overlap days; event-time versus receive-time
  handling; corporate actions and halts; shortability assumptions; liquidity and participation; **statistical breadth
  and tradable breadth reported as two numbers**, plus effective breadth (rho_bar, N_eff, incremental variance
  reduction; section 11) and the residual-covariance concentration (share of residual variance in the first five
  principal components); percentage of names eligible for H1 and percentage of rebalances
  with a feasible portfolio; expected data-accrual rate. RED if the route cannot support the estimand.
- **E4 (portfolio-level power and feasibility)**: computed on the complete filtered strategy's session return R_d
  (not on an unfiltered signal), from the development subset's dispersion and the R5/E3 dependence diagnostics:
  expected effect under the registered alternative; session dispersion and its uncertainty; required independent
  sessions for 80 % power at the Holm floor for both variants; dependence assumptions; family size; feasible accrual
  rate; expected completion horizon; whether the effect is economically meaningful after conservative costs; whether
  the required G is inside an R0-validated domain. If completion is too long, the hurdle and the test are not
  weakened; the exit state is GREEN-REGISTER-ONLY or RED.
- **E5 (economic baseline and attribution package)**, one report on development data only, never touching the
  sealed window. Revision 3 completes the contract against the fourth review's blocks A-D:

  A. **Signal**: IC and rank IC by horizon; conditional forward return by residual decile **and by tail bin (|z| in
  2-2.5, 2.5-3, > 3)** to show whether extreme residuals deteriorate toward continuation; **long-leg and short-leg
  alpha reported separately** (E[r_adj | z < -z0] and E[-r_adj | z > z0]); decay curve A(h) at 5, 10, 15, 20, 30, 45, 60
  min; forecast calibration (bias, slope, uncertainty by decile); coverage (share of grid minutes with a valid signal);
  **quote-age sensitivity** (results by quote-age bucket and with the entry latency stepped 0.5 s, 1 s, 2 s, 5 s);
  diagnostics by forecast-magnitude, liquidity, spread-to-forecast ratio, time-of-day, factor-model stability (rolling
  beta drift, condition number) and event versus non-event bucket (registered event calendar: index rebalances, FOMC,
  earnings days from the calendar available at T0); regime attribution. **Rule**: no bucket result becomes a filter,
  a leg restriction or a threshold in the confirmatory strategy; any such change is a new candidate with fresh
  selection accounting and is outside v1.19.

  B. **Portfolio**: gross portfolio return; the cost-of-neutrality table (section 11); **return lost to liquidity and
  participation restrictions** (alpha of the names dropped or scaled, under the same forecast); net return; effective
  independent breadth (section 11); capital utilisation and R_gross (section 11); concentration (largest name, largest
  sector, top-10 share of P&L); turnover per rebalance and session; gross-to-net retention; constraint activity and
  underinvestment frequency.

  C. **Execution**: the **three return measurements** for the same cohorts (mid-to-mid reversal; executable entry to
  executable exit under the conservative column; the difference, labelled "statistical alpha lost in execution");
  spread, fees, financing (zero intraday), modelled impact and slippage as separate waterfall lines; **exit cost as its
  own line**; entry-latency sensitivity; rejected-trade attribution (shares refused on cost, on liquidity, on
  participation, on portfolio constraints, and the expected gross alpha lost through each); the cost-cushion
  distribution B (section 7) with the share of executed trades whose cushion is below 0.5 bps; capacity (0.5x to 5x C);
  cost stress (base, conservative, stress columns side by side).

  D. **Risk-adjusted**: net session Sharpe with a block-bootstrap interval and the dependence assumption stated;
  drawdown; 5 % expected shortfall; worst-session concentration; factor exposures (market, sector, and the diagnostic
  size, momentum, liquidity, volatility exposures); **leave-one-session-out sensitivity** of the mean and Sharpe;
  return stability across regimes; session count and effective sample size.

  E. **Economic sensitivity budget** (fourth review section 9): the gross-to-net waterfall in bps of C per session
  with uncertainty; arithmetic break-even sensitivities (net change from a stated change in gross alpha, in spread and
  fees, in impact and slippage, and in all costs), each labelled an arithmetic scenario, not additive and not a
  forecast; the first-order decomposition dSR ~ dmu / sigma - (mu / sigma^2) dsigma to separate return improvement
  from volatility reduction. No feature is assigned a Sharpe gain.

  Every table shows units, sample counts, dataset identity and uncertainty where meaningful; statistical integrity
  (dataset identities, look counts, selection history) is reported as before. **Verdict** in {economically viable, not
  viable, insufficient evidence} as a development statement, not a confirmatory one, reached through the **E5 decision
  tree** (fourth review section 8), each branch recorded as the canonical outcome: (1) E3 RED -> stop, data suitability
  before alpha research; (2) no credible development-stage residual predictability -> "H1 economically unsupported",
  no rescue filter; (3) predictability but the fixed strategy fails the conservative-cost hurdle -> "not viable" with
  the loss attributed to signal weakness, turnover, spread, impact, liquidity or constraints from blocks B and C;
  (4) viable but E4 cannot show power for an economically meaningful effect -> GREEN-REGISTER-ONLY, confirmation
  pending; (5) powered but X0 `not eligible` for the exact session series -> the gate refuses PROMOTE; (6) otherwise
  X1 -> X2 -> X3 under their separate authorisations. A negative branch is a diagnosis of what prevents profitability,
  and is reported as such.

## 14. Where revision 2 qualifies the reviewer's advice

- The pooled forecast is adopted, but used only as the entry hurdle, not for sizing; and with the fallback to a fixed
  ranking plus cost filter when the development fit is weak, declared before the check subset.
- The covariance-based objective they wrote down is recorded as the general form and deliberately not implemented in
  v1.19 (their own preference); the deterministic projection replaces it.
- Borrow: no borrow data exist on either route; the short-side liquidity exclusion is an assumption with a
  sensitivity, stated as such, not a validated shortability model.
- E5 is adopted as a separate PR subject to the owner's approval (plan decision 15); if declined, its sections fold
  into E2, E3 and E4.
- The economic hurdle is restated in portfolio units with a denominator and a cost-based justification; it is still
  proposed and E4 decides whether it is reachable.

## 15. Three levels of success (the release can report any of them honestly)

Level 1: H1 reproducible end to end with correct accounting, valid data handling, cost scenarios and full diagnostics,
even if the development P&L is negative. Level 2: development evidence of positive net profitability under
conservative assumptions, a feasible book, and E4 showing an adequately powered confirmation is attainable; not a
confirmed edge. Level 3: X0 `eligible`, the registered window complete, and the canonical gate's permitted verdict
recorded; a `candidate` is still not permission to trade.

## 16. Open items for the owner and reviewers

1. Approve E0 revision 4 as the stage-5 design (E5 approved as a separate PR, decision 15 (a)).
2. The route (decision 2) before E3 is designed in detail.
3. The proposed numbers of sections 2 to 10 (as-of lag, latency, tolerances, caps, participation, tails, minimum
   names, hurdle), to be fixed in X1 before T0.
4. Whether the short-side exclusion is an acceptable borrow assumption for the chosen route.
5. Fifth reviewer: confirm that revision 4 resolves C1 to C7 and clarifies C8 to C11 (C12 is in specification
   revision 8), or name what is still open; in particular confirm the C1 choice (mu_hat > 1.5 x C_RT) and the C2
   choice (fail closed, variant retained in the family).

## 17. Revision 3 change list (fourth review; amendments only)

| gap-table rows | change | section |
|---|---|---|
| 7, 12 | trade-level ledger fields: expected entry and exit cost, cushion B, status and reason, incremental exposure and variance under a frozen diagnostic covariance, expected net contribution, simulated result, attribution | 7 |
| 8 | cost-of-neutrality table by construction stage | 11, 13 (E5 B) |
| 9 | R_gross and capital utilisation as labelled diagnostics; no re-scaling of survivors | 11 |
| 10 | explicit prohibition list against Sharpe inflation, enforced by E5 acceptance | 11 |
| 1 | effective breadth (rho_bar, N_eff, incremental variance reduction) and residual-covariance concentration in E3 and E5 | 11, 13 (E3, E5 B) |
| 3, 4, 6 | E5 signal block: eight diagnostic buckets, long/short split, tail bins, no-bucket-becomes-a-filter rule | 13 (E5 A) |
| 5, 14 | E5 execution block: three return measurements, exit cost line, latency steps, rejection shares and lost alpha, cushion distribution | 13 (E5 C) |
| 16 | E5 risk block: leave-one-session-out, effective sample size | 13 (E5 D) |
| 18 | economic sensitivity budget with the dSR decomposition | 13 (E5 E) |
| 17 | E5 decision tree with the six branches | 13 |
| 2, 11, 13, 15, 19, 20 | already satisfied in revision 2; recorded, no change | gap table |

Not adopted, with reasons: an additive forecast-uncertainty margin M (the fixed 1.5x multiplicative hurdle already
plays that role and a second constant would be a second tunable); the illustrative waterfall numbers in the review
(they are not targets); any nonlinear tail threshold (reported, never fitted).

## 18. Revision 4 change list (fifth review, C1-C12)

| item | change | section |
|---|---|---|
| C1 | entry rule corrected to mu_hat > 1.5 x C_RT (A_net > 0.5 x C_RT); the revision-3 form required 2.5x and was an error | 8 |
| C2 | weak-forecast fallback removed; horizon fails closed with zero-trade sessions; variant stays in the K = 2 family | 4 |
| C3 | no-trade band: unit bps of C on the aggregate discretionary net order; grid {2, 5, 10}; outcome-blind turnover objective; tie to smaller; 0 if none; never overrides a constraint or a mandatory order | 6 |
| C4 | projection = one constrained least-squares repair with all constraints jointly, unique solution, fixed solver and tolerances, fail-closed infeasibility; no sequential repair | 6 |
| C5 | quantity states intended / submitted / filled / allocated / outstanding; deterministic fill allocation (expiring first, then pro rata, largest remainder); conservation on allocated quantities; partial expiry fills stay outstanding and flagged | 7 |
| C6 | P_fill = M_arrival + side x (spread_arrival / 2 + impact); latency drift is an attribution line, not a cost; columns differ by impact multiplier and depth assumption only | 9 |
| C7 | retention = aggregate net / aggregate gross, defined only for positive gross, else the screen fails; the 1-2 bps cost figure labelled a planning assumption; gate and screen are separate requirements | 10 |
| C8 | t_data <= t_decision, t_arrival = t_decision + 500 ms; no separate pre-decision lag | 2 |
| C9 | N_eff labelled an illustration; weight-aware variance-contribution table added; neither sets weights | 11 |
| C10 | every fill is "simulated"; shortability proxy and displayed depth are assumptions | 7, 9 |
| C11 | usable-no-trade (zero), coverage-dropped (R0 rule), unpriced-position (stress valuation + flag) sessions distinguished and counted | 10 |
| C12 | specification revision 8: workstream table lists E0 and E5, B1 marked withdrawn, precedence rule | spec |
| firewall | the reviewer's firewall sentence and the tunables list with fixing rules | 12 |

## 19. Revision 5 change list (seventh review's E0 disposition)

| item | change | section |
|---|---|---|
| residual expired-cohort positions | `closing` state; residual stays allocated, re-sent each decision time, counts toward caps; post-expiry P&L attributed to execution residual, signal attribution closes at scheduled expiry | 7 |
| meaningful feasibility | skip rules with numbers: < 10 names per leg, deployed gross < 50 % of target, single name > 15 % of cohort gross, repair infeasible; no re-scaling up | 6 |
| P&L and reference-price reconciliation | primary equation on fill prices; waterfall terms named with their reference prices; identity enforced by E2; "slippage" withdrawn | 10, 11 |

Design choices the reviewer left open and revision 4 made (conditionally accepted by the seventh review): C1 the 1.5x-gross form; C2
fail closed rather than any alternate rule; C3 the grid and objective; C4 least-squares repair rather than a hand-written
sequential algorithm; C6 depth haircuts of 75 % and 50 % in the conservative and stress columns as the replacement for
the withdrawn "slippage" and "full spread" terms.

## 20. Revision 6 change list and freeze request (eighth review)

| item | change | section |
|---|---|---|
| E0-5.1 | flat close is a mandatory target; invariant replaced by explicit residual representation, stress valuation, `closeout_failure` flag, next-session carry in Equity_open, single charge | 2, 7 |
| E0-5.2 | primary P&L = Cash_close + marked inventory - Equity_open; waterfall is an independent decomposition | 10, 11 |
| choice 6 | feasibility thresholds never relaxed after an unfavourable result; E3/E4 report their cost | 6 |

**Freeze request.** The owner is asked to declare E0 revision 6 the frozen H1 design (decision U-18). After the
freeze: implementation PRs cite the clause they implement; a discrepancy found during implementation is a defect
against the PR or an amendment with its own decision record, reviewed on the PR; the file is not re-issued. The six
revision-4 design choices and the proposed numbers remain "proposed" until X1 fixes them before T0, as the file says.
