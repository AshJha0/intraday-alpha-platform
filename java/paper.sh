#!/usr/bin/env bash
# Paper-trading mode (spec paper/shadow deployment): stream the golden EQ
# vector through book -> features -> alphas -> portfolio -> risk -> execution,
# serve /metrics|/health|/status on the config port (execution.json
# monitoring.port, default 8080) and write out/paper_session_report.json.
#
# Usage: ./paper.sh [extra PaperTrading flags...]
#   e.g. ./paper.sh --mode realtime --speed 60
set -euo pipefail
cd "$(dirname "$0")"

./build.sh
java -cp out/main com.iap.platform.PaperTrading \
    --configs ../configs \
    --events ../tests/golden/events_eq_mbo.jsonl \
    --mode asap \
    --port config \
    --report out/paper_session_report.json \
    "$@"
