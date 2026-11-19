---
name: itch-day-bars
description: Build full-market one-minute bars for one in-sample Nasdaq ITCH day with tools/itch_bars_chain.py (download, itchbars in WSL, delete the raw file). Use when someone asks to build, make or add ITCH bars, breadth bars, or minute bars for a session date. Refuses any session dated 2025 or later.
---

# ITCH day to one-minute bars

## Before anything

- Get the session date (`YYYY-MM-DD`). If it is `2025-01-01` or later, REFUSE: the 2025/26
  sessions are a sealed holdout (`research/holdout_registry.json`). Do not download, build
  or read them, and do not look for another way to.
- The date must be an `insample` day in `research/breadth/candidates.json`; the tool
  refuses anything else.
- The run downloads a large vendor file. Ask the person for explicit approval for this
  date first. `--i-have-approval` records that approval; never pass it without it.

## Steps

1. Build the binary once, inside WSL (Windows blocks the built exe on this machine):

   ```
   wsl -e bash -lc "cd /mnt/c/<path to repo>/rust && CARGO_TARGET_DIR=$HOME/iap-itchbars-target cargo build -p itchbars --release"
   ```

   The chain expects `$HOME/iap-itchbars-target/release/itchbars` in WSL.
2. From the repository root (Windows side), run one day:

   ```
   python tools/itch_bars_chain.py --i-have-approval --date <YYYY-MM-DD>
   ```

   It prints one JSON record per day and appends it to `data/real/bars/chain_log.jsonl`.
   Bars land in `data/real/bars/bars_<YYYYMMDD>.csv`. A day whose bars exist is skipped.
3. Report from the printed record only: `ok`, the message and symbol counts from
   `itchbars`, `bars_bytes`, `bars_sha256`, and whether the raw file was deleted. On
   `ok: false`, report `error` as printed.

## Guardrails

- No session dated 2025 or later, ever (holdout). No holdout read of any kind.
- No download without the person's approval in this conversation.
- No signing, registration or ledger entry; building bars is not a test.
- No number that is not copied from the printed record or the chain log.
