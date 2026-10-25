#!/usr/bin/env bash
# =============================================================================
# tools/repro/reproduce.sh — reproduce B8 v2 and the REL-08 scorecard from the
# public files of this checkout INSIDE the pinned image, and compare with the
# committed results (v2.0 stage 3 P1; docs/REPRODUCTION.md has the walk-through).
#
#   bash tools/repro/reproduce.sh [--image REF] [--out DIR]
#
# REF defaults to ghcr.io/ashjha0/intraday-alpha-platform-repro@<the one line
# of deployment/docker/IMAGE_DIGEST>. The checkout is mounted read-only at
# /work, the container runs as uid 10001 with --network none, outputs go to
# DIR (default repro_out). Exit 0 = every file of record byte-identical or
# within the declared tolerance (reproduction_report.{json,md} in DIR); 1 = not.
# Needs docker and python3 (for the comparison); nothing else.
# =============================================================================
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
OUT="repro_out"
IMAGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 64 ;;
  esac
done
DIGEST="$(tr -d '[:space:]' < deployment/docker/IMAGE_DIGEST 2>/dev/null || true)"
if [ -z "$IMAGE" ]; then
  if [ -z "$DIGEST" ]; then
    echo "deployment/docker/IMAGE_DIGEST is absent or empty and no --image given" >&2
    exit 2
  fi
  IMAGE="ghcr.io/ashjha0/intraday-alpha-platform-repro@$DIGEST"
fi
echo "image: $IMAGE"
echo "checkout: $ROOT ($(git rev-parse HEAD 2>/dev/null || echo 'not a git checkout'))"

rm -rf "$OUT"
mkdir -p "$OUT/b8v2" "$OUT/rel08" "$OUT/rel08_head" "$OUT/records"
chmod -R a+rwx "$OUT"      # the container's uid 10001 writes here
ABS_OUT="$(cd "$OUT" && pwd)"

run() { # run <record name> <iap-repro args...>
  local name="$1"; shift
  docker run --rm --network none \
    -v "$ROOT:/work:ro" -v "$ABS_OUT:/out" \
    -e IAP_IMAGE_DIGEST="$DIGEST" -e IAP_REPRO_RECORD="/out/records/$name.json" \
    "$IMAGE" "$@"
}

# 1. B8 v2 (REL-04b): `analyse` re-derives DECISION.json and REPORT.md from the
#    two committed headline benchmark JSONs (copied into the output dir: analyse
#    reads its inputs from --out and would overwrite the committed files). Runs
#    the checkout's HEAD code: the decision rule is byte-stable across commits.
cp research/execution/b8v2/benchmark_v2_oos3_AAPL.json \
   research/execution/b8v2/benchmark_v2_oos3_MSFT.json "$OUT/b8v2/"
run b8v2 research/preregs/b8v2_prereg.json -- analyse --out /out/b8v2

# 2. REL-08 scorecard: re-rendered from the committed screen JSON by the screen
#    runner's own renderer, taken from the commit that wrote the committed
#    scorecard (the renderer's units and precision changed in a later commit, so
#    HEAD's renderer presents the same numbers differently: that render is made
#    too, as information, and listed in the report without deciding the status).
if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  echo "shallow clone: the commit that wrote the scorecard cannot be found; clone with full history (fetch-depth 0)" >&2
  exit 2
fi
SCORECARD_COMMIT="$(git log -1 --format=%H -- research/v118/rel08/SCORECARD_ds_20190130.md)"
echo "scorecard commit: $SCORECARD_COMMIT"
run rel08 --code-at "$SCORECARD_COMMIT" --runner tools/repro/render_rel08_scorecard.py \
  research/v118/rel08_rule_signed.json -- \
  --screen research/v118/rel08/screen_ds_20190130.json --out /out/rel08
run rel08_head --runner tools/repro/render_rel08_scorecard.py \
  research/v118/rel08_rule_signed.json -- \
  --screen research/v118/rel08/screen_ds_20190130.json --out /out/rel08_head

# 3. Compare byte for byte (tolerance from the B8 v2 predeclaration as fallback).
python3 tools/repro/compare_reproduction.py --out "$OUT" \
  --tolerance research/execution/b8v2/predeclaration.json \
  --record b8v2="$OUT/records/b8v2.json" --record rel08="$OUT/records/rel08.json" \
  --record rel08_head="$OUT/records/rel08_head.json" \
  --image-digest "$DIGEST" \
  --informational research/v118/rel08/SCORECARD_ds_20190130.md="$OUT/rel08_head/SCORECARD_ds_20190130.md" \
  research/execution/b8v2/DECISION.json="$OUT/b8v2/DECISION.json" \
  research/execution/b8v2/REPORT.md="$OUT/b8v2/REPORT.md" \
  research/v118/rel08/SCORECARD_ds_20190130.md="$OUT/rel08/SCORECARD_ds_20190130.md"
