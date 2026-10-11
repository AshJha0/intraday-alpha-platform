#!/usr/bin/env bash
# Replay the golden vectors and print book summaries + throughput.
set -euo pipefail
cd "$(dirname "$0")"

bash build.sh
java -cp out/main com.iap.replay.Demo ../tests/golden
