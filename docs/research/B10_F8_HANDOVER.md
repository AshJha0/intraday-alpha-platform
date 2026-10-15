# B10 → F8 hand-over: the dark-first pre-trade controls (Rust-owned)

Plan row F8 (v1.17, owner 2026-10-15): any new pre-trade control an execution
feature needs goes into the Rust risk engine first, with the three risk goldens
regenerated, the Python risk engine mirrored and the Java mirror updated only
so the Rust–Java pair stays byte-identical. B10 (the dark-venue model,
API_TRADING.md §2.17) needs three such controls. This page is the interface
the F8 session implements; the Python mirror already exists in
`iap.execution.dark` (`DarkControls`, `dark_pretrade_check`,
`DARK_CONTROL_CODES`, `DARK_CONTROL_GOLDENS`) and is what the execution
replay consumes until the Rust engine takes over. **No execution, SOR or
dark-venue logic goes to Rust** (that path is C++, F7): only the three
controls below. Every dark fill the controls gate is *modelled, not measured*.

## 1. The controls

| # | control | code | semantics (pure function of its inputs) | default |
|---|---|---|---|---|
| 1 | venue-type restriction | `venue_type` | the venue's type must be one of `allowed_venue_types` | `("dark", "midpoint")` |
| 2 | participation cap | `participation` | with `max_participation > 0`: `qty <= max_participation * trailing_lit_volume`, where `trailing_lit_volume` is the instrument's displayed EXECUTE volume over the trailing `participation_window_ns` at decision time (float compare; a zero trailing volume rejects every positive qty) | `max_participation = 0` (off), window 30 s |
| 3 | dark-fill notional limit | `notional` | with `max_dark_notional > 0`: `dark_notional_so_far + child_notional <= max_dark_notional`, where `dark_notional_so_far` is the parent's cumulative dark-filled notional and `child_notional = qty * qty_unit * midpoint_ticks * tick_size` at the current displayed midpoint rounded against us | `max_dark_notional = 0` (off) |

Evaluation order is fixed — 1, 2, 3 — and the **first failure wins**; the
result is `""` (accepted) or the failing code. `qty <= 0` is a caller error
(Python raises `ValueError`; Rust should reject the request as malformed, not
return a code). A rejected post is not an error: the replay sends the step to
the lit book at once and counts the rejection per code
(`ParentReport.dark_rejected`).

Inputs the engine receives per check (all from the caller; the engine keeps no
state of its own for these controls):

| input | type | source in the replay |
|---|---|---|
| `venue_type` | `lit` / `dark` / `midpoint` | `VenueSpec.venue_type` of the dark venue |
| `qty` | i64 base units, > 0 | the step quantity at the `max_child_qty` split |
| `trailing_lit_volume` | i64 base units, ≥ 0 | `ExecutionReplay._trailing_lit_volume` (displayed EXECUTEs on lit venues of the instrument, `ts >= t - window`) |
| `dark_notional_so_far` | f64 quote ccy, ≥ 0 | `_ParentState.dark_notional` (sum of `qty * qty_unit * price_ticks * tick_size` of the parent's dark fills) |
| `child_notional` | f64 quote ccy, > 0 | `qty * qty_unit * midpoint_ticks(bid, ask, side) * tick_size` |

Configuration (`DarkControls`): `max_participation: f64 >= 0`,
`participation_window_ns: i64 > 0`, `allowed_venue_types: [str]` (each one of
the three types), `max_dark_notional: f64 >= 0`. `DarkControls.to_dict()`
is the JSON shape the benchmark records; the Rust config block should take the
same keys.

## 2. Golden vectors the Rust engine must reproduce

`iap.execution.dark.DARK_CONTROL_GOLDENS`, pinned by
`python/tests/test_v117_dark_venue.py::test_dark_controls_golden_vectors_for_f8`.
Columns: controls (only the non-default keys), `venue_type`, `qty`,
`trailing_lit_volume`, `dark_notional_so_far`, `child_notional` → expected.

| controls | venue_type | qty | trailing | so far | child | expected |
|---|---|---|---|---|---|---|
| defaults | dark | 100 | 0 | 0.0 | 0.0 | `""` |
| defaults | lit | 100 | 0 | 0.0 | 0.0 | `venue_type` |
| `allowed_venue_types=("midpoint",)` | dark | 100 | 0 | 0.0 | 0.0 | `venue_type` |
| `max_participation=0.1` | dark | 100 | 1000 | 0.0 | 0.0 | `""` |
| `max_participation=0.1` | dark | 101 | 1000 | 0.0 | 0.0 | `participation` |
| `max_participation=0.1` | midpoint | 1 | 0 | 0.0 | 0.0 | `participation` |
| `max_dark_notional=10000` | dark | 100 | 0 | 9000.0 | 1000.0 | `""` |
| `max_dark_notional=10000` | dark | 100 | 0 | 9000.0 | 1000.01 | `notional` |
| `max_participation=0.1, max_dark_notional=10000` | dark | 500 | 1000 | 9999.0 | 5.0 | `participation` (order: 2 before 3) |
| `max_participation=0.5, max_dark_notional=10000` | lit | 500 | 1000 | 9999.0 | 5.0 | `venue_type` (order: 1 before 2) |

The F8 golden file should hold exactly these ten rows (plus any the Rust
session adds, mirrored back into `DARK_CONTROL_GOLDENS` so the two stay one
list). Boundary semantics to keep: `<=` on the participation and notional
tests (equality passes), float arithmetic in f64 with no rounding of
`max_participation * trailing_lit_volume`.

## 3. What F8 changes, and what it must not

- Rust risk engine: a `dark_controls` block in the risk config, the three
  checks in the pre-trade path, the result code in the engine's reject
  reasons, the three risk goldens regenerated.
- Python risk engine (`iap.risk`): mirror the block and the checks so the
  risk goldens agree; the replay keeps calling
  `iap.execution.dark.dark_pretrade_check` (the same function the mirror
  should delegate to, or an identical copy — one implementation).
- Java risk mirror: updated only so the Rust–Java pair stays byte-identical.
- Not F8: the dark fill model, the dark-first eligibility, the fill rule, the
  expiry — those are C++ (F7) ports of the Python reference and never Rust.

## 4. Where the Python mirror lives

- `python/src/iap/execution/dark.py` — `DarkControls`, `dark_pretrade_check`,
  `DARK_CONTROL_CODES`, `DARK_CONTROL_GOLDENS`, `midpoint_ticks`.
- `python/src/iap/execution/replay.py` — `_dark_try` (the only call site:
  builds the inputs above and counts rejections), `_trailing_lit_volume`.
- `python/tests/test_v117_dark_venue.py` — the golden-vector test and the
  replay tests of each control's fallback to the lit book.
