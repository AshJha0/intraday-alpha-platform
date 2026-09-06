#!/usr/bin/env bash
# =============================================================================
# tests/harness/run_golden.sh — cross-language golden suite, one command
# (PLATFORM_CONVENTIONS.md §5 / spec §21).
#
# Runs ONLY each language's golden test group and prints the parity table.
# Implemented as the --golden-only mode of run_all.sh so the two entry points
# can never disagree about filters or parsing.
# =============================================================================
set -euo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run_all.sh" --golden-only
