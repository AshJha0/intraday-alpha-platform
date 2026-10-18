# Enforced portfolio limits and kill switch (v1.18 REL-17)

Code: `python/src/iap/risk/portfolio_limits.py` (limits, guard, kill switch),
`python/src/iap/portfolio/enforced.py` (optimizer under the limits).
Config: `configs/risk/portfolio_limits.json` (`x-version` 1, `limits_version` recorded in every refusal).
Tests: `python/tests/test_v118_portfolio_limits.py`.

This layer sits above the order-level hard risk engine (`iap.risk.engine`, Rust parity), which is
unchanged. Weights are fractions of capital.

## Limits

| key | rule | where enforced |
|---|---|---|
| `gross_max` | `sum |w|` | optimizer (L1 ball) + check |
| `net_max` | `|sum w|` | optimizer (halfspace pair) + check |
| `name_max` | `|w_i|` | optimizer (box) + check |
| `sector_net_max` | `|sum_{i in s} w_i|` per sector | optimizer (exposure row) + check |
| `sector_gross_max` | `sum_{i in s} |w_i|` per sector | check only (non-linear): an optimizer result over it is refused |
| `beta_max` | `|sum beta_i w_i|`, missing beta = 1.0 | optimizer (exposure row) + check |
| `participation_max` | `|w_i - w_prev_i| capital / ADV_i`; a name without ADV may not trade | optimizer (per-name trade cap) + check |
| `signal_share_max` | sleeve gross / book gross | `check_signal_mix` |
| `signal_corr_max` | ex-ante sleeve correlation under Sigma | `check_signal_mix` |
| `daily_loss_limit` | `(day start equity - equity) / capital` | `PortfolioGuard.mark` trips the kill switch |
| `drawdown_stop` | `(peak equity - equity) / capital` | `PortfolioGuard.mark` trips the kill switch |

A missing input is a breach, not a pass. A wrong `x-version`, an unknown key, or a negative, NaN or
boolean bound fails the load.

## Refusal points

- `solve_enforced(...)`: refuses before solving when the kill switch is latched; otherwise solves with
  the Dykstra projection and refuses any result that still breaches a limit.
- `PortfolioGuard.check_weights(w, ids, ...)`: any weight set.
- `PortfolioGuard.check_order(instrument, signed_notional, positions, ...)`: pre-trade check on the
  post-trade book. A breach the book already had is refused only if the order makes it worse, so
  risk-reducing orders go through.

## Kill switch

Trips on a loss limit, on `halt(reason)` (operator or monitoring) or on a non-finite equity mark. It
latches: recovery and a new trading day do not clear it. Only `reset(operator, reason)` clears it,
and a reset is refused while a loss limit is still breached. Every trip and reset goes to `history`
and to the `HaltSink`.

## Monitoring hook (REL-16)

`HaltSink` is a one-method protocol, `report_halt(source, reason, active)`. The REL-16 monitor passes
an object with that method to `PortfolioGuard(sink=...)` and shows HALT while `active` is true. The
reverse direction (monitor says HALT, book stops) is `guard.halt(reason)`.

## Paper and live order paths

Any paper or live order path calls `guard.check_order(...)` before it sends and treats
`LimitRefused` as a reject. Wiring into the REL-05 recorder and the REL-18 order state is left to
REL-18, which owns those files.
