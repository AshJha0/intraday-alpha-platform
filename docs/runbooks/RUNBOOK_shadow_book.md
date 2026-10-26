# Runbook: Track C forward shadow book (daily, paper only)

`python -m iap.paper.shadow_book` (v2.0 U0 stage 10) fills the forward window of the
Track C confirmation one session at a time, under the accrual rule of
`research/v2/U13_CONFIRMATION_PROTOCOL.md` section 8.

> **Paper only.** The module imports no broker, OMS, socket or HTTP code (a test scans
> its imports) and refuses to run in a process that has loaded a broker client. Live
> orders are not authorised (U-13).
>
> Not to be confused with [RUNBOOK_paper_trade.md](RUNBOOK_paper_trade.md) (the v1.16 MVP
> paper trade) or [RUNBOOK_shadow_deployment.md](RUNBOOK_shadow_deployment.md) (the Java
> shadow stack).

## What one run does

1. Takes the next session of the registered calendar that has no row yet.
2. Reads `<inbox>\<session>.json`: the frozen combined-book configuration's target
   weights per decision, the decision mids, the market at arrival per name, and the
   close mids. The file must carry the frozen configuration's SHA-256 from the config.
   The path passes the data-read guard (`iap.core.guard.check`) and the forbidden-dataset
   list; a sealed partition is refused.
3. Turns each decision's targets into paper orders against the paper position and
   prices them with the one EN4 fill convention (`iap.execution.sim.simulate`, 500 ms
   arrival latency) under the three cost columns. Each column (base, conservative,
   stress) is its own paper book. The U13 series is the conservative column.
4. Applies the usability rule (`m_min` usable units, time and grid coverage; thresholds
   in the config) and appends one row to `chain.jsonl`. The row holds counts, the input
   file's SHA-256 and the SHA-256 of `sealed\<session>.json`, which holds the P&L. Each row
   carries the previous row's hash, so an edit, deletion or reorder anywhere breaks
   `verify`.

The job prints counts and the accrual state (`accruing`, `complete`, `pending`) only.

## Configuration (fixed before T0)

`configs/paper/shadow_book.json` and its calendar `configs/paper/shadow_book_calendar.json`
are templates. The registration replaces `frozen_configuration_sha256`, `capital`, `T_min`,
`m_min`, `grid_points` and the calendar. With the shipped placeholders (empty calendar) the
job refuses to start.

## Commands

```bash
python -m iap.paper.shadow_book run --config configs/paper/shadow_book.json --inbox data/shadow_book_inbox --out data/shadow_book
```

```bash
python -m iap.paper.shadow_book status --config configs/paper/shadow_book.json --out data/shadow_book
```

```bash
python -m iap.paper.shadow_book verify --config configs/paper/shadow_book.json --out data/shadow_book
```

```bash
python -m iap.paper.shadow_book mark-missing --config configs/paper/shadow_book.json --out data/shadow_book --session 2027-01-05 --reason "feed outage, no recoverable data"
```

Exit codes: `0` appended, already recorded, not yet due, or calendar exhausted; `2` refused
(the reason is on stderr and in `shadow_book.log`).

## Refusals and what to do

| Message | Meaning | Action |
|---|---|---|
| `input for D is missing` | the inbox has no file for the next session | produce it; if the data is truly lost, `mark-missing` with a reason (counts as attempted, not usable) |
| `D is out of order` | a later session was asked for | run the next session first; nothing is ever skipped |
| `already recorded from different input bytes` | the inbox file of a recorded day changed | investigate; the chain is append-only, the first record stands |
| `not produced by the frozen configuration` | the file's configuration hash differs | the producer is not running the frozen configuration |
| `chain broken` / `altered` | a row or sealed file was edited | stop; restore from backup; record the incident |
| `the window is complete` / `pending` | accrual has ended | no more rows; the runner does the single read |

Re-running a recorded day with the same input is a no-op.

## No peeking and the single read

Until completion, only counts are visible (`status`). `iap.paper.shadow_book.read_series`
serves the identity `iap-runner` only, only when the state is `complete`, and only once: it
appends a `read` row with the series hash before it returns. Keep `sealed\` readable by the
runner account only (an NTFS ACL the owner sets); the code does not set file permissions.

## Scheduling (owner)

`tools\install_shadow_book_task.ps1` registers the task `IAP-TrackC-ShadowBook`, which runs
`tools\run_shadow_book.cmd` Monday to Friday at 17:30 local time. Run it with `-WhatIf` first.
Nothing installs it automatically.

```bash
powershell -ExecutionPolicy Bypass -File tools/install_shadow_book_task.ps1 -WhatIf
```

## Known gaps

- The producer of the inbox files (the frozen configuration run on real family forecasts
  each session) is not built: it needs the families wired through `BookPanel.forecasts`.
- `accrual_state` mirrors `iap.validation.pf3.accrual_status` and should call it once that
  module is on main.
- Carried inventory across a `mark-missing` session is valued from the last recorded close.
- The single read here does not yet go through `iap.validation.profiles.read_series`.
