#!/usr/bin/env bash
# Paper-trading mode (spec paper/shadow deployment): stream the golden EQ
# vector through book -> features -> alphas -> portfolio -> risk -> execution,
# serve /metrics|/health|/ready|/status (and POST /admin/* when an admin token
# is configured) on the config port (execution.json monitoring.port, default
# 8080), write out/paper_session_report.json, and checkpoint the durable state
# (risk snapshot, session accounting, risk/config/admin audit JSONL) to
# out/state -- PLATFORM_CONVENTIONS.md §12.3.
#
# Usage: bash paper.sh [extra PaperTrading flags...]
#   e.g. bash paper.sh --mode realtime --speed 60
#        bash paper.sh --resume                     # continue from out/state
#        IAP_ADMIN_TOKEN=... bash paper.sh --mode realtime --speed 60
#
# --configs may be overridden by $IAP_CONFIG_DIR (§12.2); pass --configs
# explicitly here so the repo checkout is the default in a source tree.
set -euo pipefail
cd "$(dirname "$0")"

bash build.sh
java -cp out/main com.iap.platform.PaperTrading \
    --configs ../configs \
    --events ../tests/golden/events_eq_mbo.jsonl \
    --mode asap \
    --port config \
    --report out/paper_session_report.json \
    --state-dir out/state \
    "$@"
