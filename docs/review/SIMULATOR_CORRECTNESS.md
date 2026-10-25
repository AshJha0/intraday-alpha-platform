# Simulator correctness: reviewer questions mapped to tests (finding 5)

> **Level 2: not validated.** The fill model passes Level 1 only (deterministic
> replay validation on synthetic and replayed books). No real orders or real
> fills exist (decision D4(a)), so nothing below is empirical evidence that
> simulated fills match what a venue would have given. See section 4.

Scope: the Python reference simulator `iap.execution.simulator.ExecutionSimulator`
(rules 1-10 in its module docstring) and `iap.execution.replay.ExecutionReplay`.
All tests are synthetic and deterministic (hand-built books or seeded
Hypothesis strategies). Paths are relative to `python/tests/`. Run them with:

```bash
cd python && PYTHONPATH=src python -m pytest -q test_review_simulator_correctness.py test_rel04_child_execution.py test_execution_rules.py test_property_replay.py test_property_book.py test_book.py test_v114_execution.py test_rel03_execution_metrics.py test_tca.py
```

## 1. Question -> test map

| Reviewer question | Rule | Test(s) |
|---|---|---|
| Double consumption of displayed liquidity | 3b, 4 | `test_rel04_child_execution.py::test_displayed_liquidity_is_never_consumed_twice`; `::test_eligible_depth_is_net_of_consumption_and_bounded_by_levels_and_band`; `::test_hidden_print_pool_is_shared_with_the_displayed_queue_once`; `test_execution_rules.py::test_queue_crossed_display_is_consumed_once_until_it_changes`; `::test_queue_same_price_children_share_one_print_and_queue_behind_each_other`; `::test_simulated_fills_never_mutate_the_replayed_book`; `test_v114_execution.py::test_hidden_prints_are_opt_in_and_never_double_count_a_displayed_print`; `test_review_simulator_correctness.py::test_fill_quantity_never_exceeds_the_print_across_two_of_our_resting_orders`; property: `test_property_replay.py::test_replay_conserves_quantity_and_fills_from_the_book` |
| Fill before order eligibility (arrival / activation) | 2, 9 | `test_execution_rules.py::test_latency_no_fill_before_arrival_and_ordered_activation`; `::test_activation_order_is_arrival_then_order_id`; `::test_activation_happens_before_the_event_is_applied`; `::test_processing_order_expiry_precedes_activation_and_tracking_precedes_apply`; `::test_expiry_expires_pending_and_resting_orders_before_activation`; `test_rel04_child_execution.py::test_market_data_delay_orders_decision_before_arrival`; `::test_price_moves_before_arrival_leave_the_ioc_unfilled_at_its_limit`; property: `test_execution_rules.py::test_property_fill_never_outside_arrival_and_expiry` |
| Cancel vs fill ordering under latency | 7 | `test_execution_rules.py::test_cancel_cannot_undo_an_earlier_fill`; `::test_cancel_never_overtakes_its_order`; `::test_cancel_consumes_one_jitter_draw_and_ties_activate_first`; `::test_cancel_arrivals_are_applied_in_effective_time_order`; `test_rel04_child_execution.py::test_cancel_arriving_after_a_full_fill_is_rejected_and_never_erases_the_fill`; `::test_cancel_that_lands_before_the_fill_event_wins`; `::test_cancel_cannot_overtake_its_own_order`; `::test_inbound_delay_applies_to_orders_and_cancels_and_defaults_off`; `test_v114_execution.py::test_amend_down_keeps_priority_after_latency` |
| Queue position and cancellations ahead | 4 | `test_execution_rules.py::test_queue_entry_ahead_equals_displayed_depth`; `::test_queue_execute_depletes_ahead_then_fills`; `::test_queue_cancel_ahead_reduces_position_deterministically`; `::test_queue_cancel_decrement_floors_at_zero`; `::test_queue_cancel_from_behind_does_not_advance_us`; `::test_queue_trade_through_fills_at_our_price_bounded_by_traded_volume`; `::test_queue_single_share_trade_through_cannot_fill_a_million`; `::test_queue_marketable_add_consumes_queue_ahead`; `test_v114_execution.py::test_modify_decrease_ahead_credits_and_behind_does_not` |
| Feed gaps (stale book, recovery) | 8 | `test_book.py::test_gap_marks_stale_and_drops_book_events`; `::test_snapshot_recovery_rebuilds_book_and_clears_stale`; `::test_mid_burst_gap_marks_burst_broken`; `test_property_book.py::test_feed_gap_marks_stale_and_freezes_depth`; `test_rel04_child_execution.py::test_stale_book_blocks_every_fill_and_cancels_aggressive_arrivals`; `::test_gapped_venue_leaves_a_capped_parent_with_owed_quantity_not_a_fill`; `::test_halted_venue_blocks_fills_like_a_gap`; `test_execution_rules.py::test_resting_order_is_not_consumed_while_gated_and_not_crossed_on_reopen_if_uncrossed`; **new**: `test_review_simulator_correctness.py::test_trade_print_through_our_price_while_stale_never_fills_a_resting_order`, `::test_snapshot_recovery_keeps_the_pre_gap_queue_and_treats_snapshot_orders_as_ahead` |
| Duplicate messages | book | `test_book.py::test_duplicate_sequence_dropped_and_counted`; `test_rel04_child_execution.py::test_duplicate_execute_is_dropped_and_fills_once`; `test_property_book.py::test_reorder_window_recovers_permuted_arrival` |
| Partial fills | 3, 4 | `test_execution_rules.py::test_market_partial_remainder_cancelled`; `::test_ioc_fills_what_it_can_then_cancels`; `::test_fok_all_or_none`; `test_rel04_child_execution.py::test_partial_fill_then_cancel_keeps_the_fill_and_cancels_only_the_remainder`; `::test_capped_ioc_takes_only_eligible_depth_then_reposts_after_replenishment` |
| Rejected orders | 8, 10 | `test_rel04_child_execution.py::test_rejected_order_post_only_cross_is_cancelled_with_its_reason`; `::test_invalid_orders_are_refused_at_submit`; `test_execution_rules.py::test_cancel_and_validation`; `::test_venue_not_trading_rejects_every_aggressive_type` |
| Residual and opportunity-cost visibility | replay, TCA | `test_rel04_child_execution.py::test_incomplete_parent_reports_residual_at_window_end_never_a_fill`; `::test_end_of_session_residual_is_cancelled_not_filled`; `::test_replay_window_end_expires_the_child_and_reports_the_residual`; `::test_exhausted_depth_is_a_failure_counter_and_nothing_is_manufactured`; `test_rel03_execution_metrics.py::test_unfilled_parent_has_no_per_share_cost_and_a_full_residual`; `::test_incomplete_parents_report_residual_and_never_a_fill`; `test_tca.py::test_perold_hand_case_buy` (opportunity cost = unfilled x drift), `::test_perold_identity_components_sum_to_total`; `test_mvp_child_policy.py::test_study_costs_price_unfilled_quantity_and_add_up` |
| Determinism | all | `test_property_replay.py::test_replay_is_deterministic`; `test_replay.py::test_replay_deterministic`; the C++/Rust golden parity suites |

## 2. Modelling assumptions (each is a choice, not a measurement)

1. **Queue entry** at the back of the displayed level: `ahead_qty` = displayed
   depth at our price on activation (rule 4).
2. **Cancels at our level are assumed ahead of us** unless the cancelled order
   is known to have joined after us; a cancel never fills us. This is
   optimistic for queue progress (a real cancel may be from behind).
3. **Snapshot orders after a gap are assumed ahead of us**, and our pre-gap
   `ahead_qty` is kept even if the recovered level is smaller. Nothing that
   may have traded during the gap credits us.
4. **Trade-through fills** at our limit, bounded by the traded volume net of
   the queue ahead; one print is shared once across all our children.
5. **Market impact** is a formula (linear or parent model, rule 6/6b), not a
   response of the replayed book: our fills never change the replayed stream.
6. **Latency** is a fixed decomposition plus seeded jitter; the venue
   acknowledgement is implied by activation; IEX's 350 us speed bump is
   optional (`inbound_delay_ns`).
7. **Hidden liquidity** exists only as printed non-displayed executions
   (`hidden_prints`, opt-in); `peg_ahead_qty` for the midpoint book defaults
   to 0, an optimistic upper bound.
8. **Venue gating**: stale / halted / not-TRADING books produce no fill of
   any kind; reopen fills at the touch, bounded as rule 4.
9. **Residual is never filled**: it is reported and priced by the completion
   charge and opportunity cost.

## 3. What the tests do and do not prove

They prove the simulator applies its stated rules consistently, deterministically
and without accounting leaks (no double consumption, no fill before arrival or
after expiry/cancel, quantity conservation). They do not prove the rules match
venue behaviour.

## 4. Claims this simulator cannot support without real fills

- Absolute fill rates or fill probabilities of passive orders (queue model,
  assumptions 2-3).
- Absolute implementation shortfall, markout or adverse selection of a live
  strategy; only relative comparisons of policies on the same replay are
  supported, and those inherit the same assumptions.
- Market impact of our own orders (assumption 5) and therefore capacity.
- Hidden / midpoint liquidity capture (assumption 7).
- Latency advantage, order-entry latency or cross-venue skew (no measured
  order path; capture timestamps carry an unknown clock offset).
- Any maker / quoting profitability, rebate capture or queue-priority edge.
- That the Level 1 fill model generalises to held-out days with real orders
  (Level 2, not reachable under D4(a)).

## 5. Traps pinned in stage 11 (S5; `tests/execution/test_simulator_traps.py`, defect D-68)

Each is a modelling limitation stated as a test of what the simulator DOES, so a change is visible;
none is validated against real fills.

| trap | what the simulator does | why it is a trap | test |
|---|---|---|---|
| **C-as-EXECUTE through the cross** | an ITCH `C` (executed with price) becomes an EXECUTE that trades the book order at the BOOK price; the quoted print price is ignored unless `ExecutionSimulator(exec_print_price=True)` (and the ingest's `exec_prints`), in which case the fill is at the printed price. A NON-printable `C` (a cross execution already in the `Q` print) is emitted by the ingest as MODIFY / CANCEL and can never fill a child | an opening or closing cross print is credited to a resting child at its limit, or (opt-in) at the cross price, with no auction participation model | `test_execute_with_a_different_print_price_fills_at_the_book_price_by_default`, `test_exec_print_price_opt_in_fills_at_the_printed_price`, `test_non_printable_c_is_a_size_reduction_for_the_ingest_never_an_execute` |
| **self-matching crossing ADD** | a marketable ADD is expanded against the pre-event displayed opposite depth and the pool reaches our resting child in queue order | the simulator has no participant identity; a venue with self-match prevention would cancel or re-price an order crossing the same participant's resting child, so a strategy that quotes both sides can be credited with fills a venue would have refused | `test_crossing_add_fills_our_resting_order_without_self_match_prevention` |
| **odd lots** | a print of 1-99 shares is one pool like a round lot, and odd-lot displayed size counts in `ahead_qty` | odd-lot executions do not set the NBBO and are reported differently by venues; a child filled share for share by odd-lot prints may be optimistic for queue progress | `test_odd_lot_prints_are_ordinary_pools`, `test_odd_lot_displayed_depth_counts_toward_queue_position` |
