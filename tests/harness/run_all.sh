#!/usr/bin/env bash
# =============================================================================
# tests/harness/run_all.sh — CI entry point (PLATFORM_CONVENTIONS.md §9).
#
# Runs every language's build + test suite with the canonical commands
# (docs/BUILD_NOTES.md) and prints a cross-language parity table:
# language, tests passed, golden-group tests passed.
#
# Usage:
#   tests/harness/run_all.sh                 # full suites (CI mode)
#   tests/harness/run_all.sh --golden-only   # golden groups only (= run_golden.sh)
#
# Golden filters (inspected from each language's actual test naming):
#   python  pytest -k golden            (test_golden.py, test_golden_anomalies.py,
#                                        test_feature_golden.py, test_alpha_golden.py,
#                                        test_portfolio_golden.py, test_tca_golden.py
#                                        + golden-named tests)
#   cpp     ctest -R Golden             (gtest suites Golden, FeatureGolden,
#                                        AlphaGolden, ReplayFillsGolden, SplitMix64Golden)
#   rust    cargo test -p <crate> --test <golden target> for each golden
#           integration-test file (golden_marketdata, golden_book,
#           golden_features, golden_alpha, golden_risk, golden_replay)
#   java    JUnitCore on the *GoldenTest classes (CodecGoldenTest, BookGoldenTest,
#           AnomalyGoldenTest, RiskGoldenTest, ReplayFillsGoldenTest,
#           TcaGoldenTest, PortfolioGoldenTest)
#
# Exit code: 0 iff every selected suite passed.
# =============================================================================
set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HARNESS_DIR/../.." && pwd)"
LOG_DIR="${IAP_HARNESS_LOG_DIR:-$(mktemp -d /tmp/iap-harness.XXXXXX)}"
mkdir -p "$LOG_DIR"

GOLDEN_ONLY=0
[ "${1:-}" = "--golden-only" ] && GOLDEN_ONLY=1

# Rust golden integration-test targets: "<crate>:<test file>" pairs.
RUST_GOLDEN_TARGETS="marketdata:golden_marketdata orderbook:golden_book features:golden_features alpha:golden_alpha risk:golden_risk replay:golden_replay"
# Java golden test classes (JUnit4) — ALL of them (round-3 PLATFORM SEV-2:
# the gate used to run 2 of the 10 classes while README claimed otherwise).
JAVA_GOLDEN_CLASSES="com.iap.AdaptiveGoldenTest com.iap.AlphaGoldenTest com.iap.AnomalyGoldenTest com.iap.BookGoldenTest com.iap.CodecGoldenTest com.iap.FeatureGoldenTest com.iap.PortfolioGoldenTest com.iap.ReplayFillsGoldenTest com.iap.RiskGoldenTest com.iap.TcaGoldenTest"

# Per-language results. TESTS/GOLDEN hold a NUMBER when the suite ran and the
# count could be parsed, and "-" when it did not run or could not be parsed —
# never a fabricated 0 (round-3 PLATFORM: "run_all.sh prints truthful counts").
declare -A TESTS GOLDEN STATUS SECS
OVERALL=0
DEPLOY_STATUS="-"
DEPLOY_DETAIL=""
NUMBERS_STATUS="-"
NUMBERS_DETAIL=""

note() { printf '>> %s\n' "$*"; }

run_logged() { # run_logged <logfile> <cmd...>
    local log="$1"; shift
    ( "$@" ) >"$log" 2>&1
}

# ----------------------------------------------------------------- python ---
run_python() {
    local t0=$SECONDS ok=1 full="-" gold="-"
    if [ "$GOLDEN_ONLY" -eq 0 ]; then
        note "python: PYTHONPATH=src python3 -m pytest -q"
        if run_logged "$LOG_DIR/python_full.log" \
            env -C "$ROOT/python" PYTHONPATH=src python3 -m pytest -q; then
            full=$(grep -Eo '[0-9]+ passed' "$LOG_DIR/python_full.log" | tail -1 | grep -Eo '[0-9]+')
            [ -n "$full" ] || { full="?"; ok=0; }
        else ok=0; full="FAIL"; fi
    fi
    note "python: PYTHONPATH=src python3 -m pytest -q -k golden"
    if run_logged "$LOG_DIR/python_golden.log" \
        env -C "$ROOT/python" PYTHONPATH=src python3 -m pytest -q -k golden; then
        gold=$(grep -Eo '[0-9]+ passed' "$LOG_DIR/python_golden.log" | tail -1 | grep -Eo '[0-9]+')
        [ -n "$gold" ] || { gold="?"; ok=0; }
    else ok=0; gold="FAIL"; fi
    [ "$GOLDEN_ONLY" -eq 1 ] && full=$gold
    TESTS[python]=$full; GOLDEN[python]=$gold
    STATUS[python]=$([ $ok -eq 1 ] && echo PASS || echo FAIL)
    SECS[python]=$((SECONDS - t0)); [ $ok -eq 1 ] || OVERALL=1
}

# -------------------------------------------------------------------- cpp ---
run_cpp() {
    local t0=$SECONDS ok=1 full="-" gold="-"
    note "cpp: bash build.sh"
    if ! run_logged "$LOG_DIR/cpp_build.log" env -C "$ROOT/cpp" bash build.sh; then
        TESTS[cpp]="-"; GOLDEN[cpp]="-"; STATUS[cpp]="BUILD FAIL"
        SECS[cpp]=$((SECONDS - t0)); OVERALL=1; return
    fi
    if [ "$GOLDEN_ONLY" -eq 0 ]; then
        note "cpp: ctest --test-dir build --output-on-failure"
        if run_logged "$LOG_DIR/cpp_full.log" \
            env -C "$ROOT/cpp" ctest --test-dir build --output-on-failure; then
            full=$(grep -Eo 'out of [0-9]+' "$LOG_DIR/cpp_full.log" | tail -1 | grep -Eo '[0-9]+')
            [ -n "$full" ] || { full="?"; ok=0; }
        else ok=0; full="FAIL"; fi
    fi
    note "cpp: ctest --test-dir build -R Golden"
    if run_logged "$LOG_DIR/cpp_golden.log" \
        env -C "$ROOT/cpp" ctest --test-dir build --output-on-failure -R Golden; then
        gold=$(grep -Eo 'out of [0-9]+' "$LOG_DIR/cpp_golden.log" | tail -1 | grep -Eo '[0-9]+')
        [ -n "$gold" ] || { gold="?"; ok=0; }
    else ok=0; gold="FAIL"; fi
    [ "$GOLDEN_ONLY" -eq 1 ] && full=$gold
    TESTS[cpp]=$full; GOLDEN[cpp]=$gold
    STATUS[cpp]=$([ $ok -eq 1 ] && echo PASS || echo FAIL)
    SECS[cpp]=$((SECONDS - t0)); [ $ok -eq 1 ] || OVERALL=1
}

# ------------------------------------------------------------------- rust ---
run_rust() {
    local t0=$SECONDS ok=1 full="-" gold="-"
    if [ "$GOLDEN_ONLY" -eq 0 ]; then
        note "rust: cargo test (workspace)"
        if run_logged "$LOG_DIR/rust_full.log" env -C "$ROOT/rust" cargo test; then
            full=$(grep -Eo 'test result: ok\. [0-9]+ passed' "$LOG_DIR/rust_full.log" \
                   | grep -Eo '[0-9]+' | paste -sd+ | bc)
            [ -n "$full" ] || { full="?"; ok=0; }
        else ok=0; full="FAIL"; fi
    fi
    : >"$LOG_DIR/rust_golden.log"
    for pair in $RUST_GOLDEN_TARGETS; do
        local crate="${pair%%:*}" target="${pair##*:}"
        note "rust: cargo test -p $crate --test $target"
        if ! ( cd "$ROOT/rust" && cargo test -p "$crate" --test "$target" ) \
             >>"$LOG_DIR/rust_golden.log" 2>&1; then
            ok=0
        fi
    done
    gold=$(grep -Eo 'test result: ok\. [0-9]+ passed' "$LOG_DIR/rust_golden.log" \
           | grep -Eo '[0-9]+' | paste -sd+ | bc)
    [ -n "$gold" ] || { gold="?"; ok=0; }
    [ "$GOLDEN_ONLY" -eq 1 ] && full=$gold
    TESTS[rust]=$full; GOLDEN[rust]=$gold
    STATUS[rust]=$([ $ok -eq 1 ] && echo PASS || echo FAIL)
    SECS[rust]=$((SECONDS - t0)); [ $ok -eq 1 ] || OVERALL=1
}

# ------------------------------------------------------------------- java ---
run_java() {
    local t0=$SECONDS ok=1 full="-" gold="-"
    local JUNIT=/usr/share/java/junit4.jar HAMCREST=/usr/share/java/hamcrest-core.jar
    if [ "$GOLDEN_ONLY" -eq 0 ]; then
        note "java: bash build.sh && bash test.sh"
        if run_logged "$LOG_DIR/java_full.log" env -C "$ROOT/java" bash test.sh; then
            full=$(grep -Eo 'OK \([0-9]+ tests?\)' "$LOG_DIR/java_full.log" | grep -Eo '[0-9]+')
            [ -n "$full" ] || { full="?"; ok=0; }
        else ok=0; full="FAIL"; fi
    else
        note "java: bash build.sh + compile tests"
        if ! run_logged "$LOG_DIR/java_build.log" env -C "$ROOT/java" bash build.sh; then
            TESTS[java]="-"; GOLDEN[java]="-"; STATUS[java]="BUILD FAIL"
            SECS[java]=$((SECONDS - t0)); OVERALL=1; return
        fi
        # compile the test sources (same as test.sh, without running the suite)
        if ! ( cd "$ROOT/java" && rm -rf out/test && mkdir -p out/test && \
               find src/test/java -name '*.java' | sort > out/test-sources.txt && \
               javac -Xlint:all -Werror -cp "out/main:$JUNIT:$HAMCREST" -d out/test @out/test-sources.txt ) \
             >>"$LOG_DIR/java_build.log" 2>&1; then
            ok=0
        fi
    fi
    note "java: JUnitCore $JAVA_GOLDEN_CLASSES"
    # shellcheck disable=SC2086
    if ( cd "$ROOT/java" && java -cp "out/test:out/main:$JUNIT:$HAMCREST" \
             org.junit.runner.JUnitCore $JAVA_GOLDEN_CLASSES ) \
         >"$LOG_DIR/java_golden.log" 2>&1; then
        gold=$(grep -Eo 'OK \([0-9]+ tests?\)' "$LOG_DIR/java_golden.log" | grep -Eo '[0-9]+')
        [ -n "$gold" ] || { gold="?"; ok=0; }
    else ok=0; gold="FAIL"; fi
    [ "$GOLDEN_ONLY" -eq 1 ] && full=$gold
    TESTS[java]=$full; GOLDEN[java]=$gold
    STATUS[java]=$([ $ok -eq 1 ] && echo PASS || echo FAIL)
    SECS[java]=$((SECONDS - t0)); [ $ok -eq 1 ] || OVERALL=1
}

# ------------------------------------------------------------- deployment ---
# Structural validation of deployment/ (GOVERNANCE.md §1 "the structural
# validation used in CI", PLATFORM_CONVENTIONS.md §12.7): promtool rules +
# config + rule unit tests, docker compose config, Dockerfile COPY sources,
# k8s manifests and singleton shape, ConfigMap sync, dashboard metric
# provenance, and the completeness of the Java golden gate.
run_deployment() {
    local t0=$SECONDS
    note "deployment: python3 tests/harness/check_deployment.py"
    if run_logged "$LOG_DIR/deployment.log" \
        python3 "$HARNESS_DIR/check_deployment.py"; then
        DEPLOY_STATUS=PASS
    else
        DEPLOY_STATUS=FAIL
        OVERALL=1
    fi
    DEPLOY_DETAIL=$(grep -E '^deployment checks:' "$LOG_DIR/deployment.log" \
                    | tail -1)
    # Headline numbers vs artefacts. Exit 2 = only the RESEARCH ledger rows are
    # stale (they move on every research rerun) — reported, not a build break;
    # exit 1 = a platform number is wrong, which is.
    note "docs: python3 tests/harness/check_headline_numbers.py"
    python3 "$HARNESS_DIR/check_headline_numbers.py" \
        >"$LOG_DIR/headline_numbers.log" 2>&1
    case $? in
        0) NUMBERS_STATUS=PASS ;;
        2) NUMBERS_STATUS="STALE (research ledger)" ;;
        *) NUMBERS_STATUS=FAIL; OVERALL=1 ;;
    esac
    NUMBERS_DETAIL=$(grep -E '^(FAILED|STALE|all headline)' \
                     "$LOG_DIR/headline_numbers.log" | tail -1)
    SECS[deployment]=$((SECONDS - t0))
}

# -------------------------------------------------------------------- main --
MODE=$([ "$GOLDEN_ONLY" -eq 1 ] && echo "golden groups only" || echo "full suites")
note "intraday-alpha-platform harness — $MODE (logs: $LOG_DIR)"
run_python
run_cpp
run_rust
run_java
if [ "$GOLDEN_ONLY" -eq 0 ] && [ "${IAP_SKIP_DEPLOYMENT:-0}" != "1" ]; then
    run_deployment
fi

echo
echo "===================== cross-language parity table ====================="
if [ "$GOLDEN_ONLY" -eq 1 ]; then
    printf '%-8s | %-14s | %-6s | %s\n' language "golden passed" time status
    printf '%s\n' "---------+----------------+--------+-------"
    for lang in python cpp rust java; do
        printf '%-8s | %-14s | %4ss | %s\n' \
            "$lang" "${GOLDEN[$lang]}" "${SECS[$lang]}" "${STATUS[$lang]}"
    done
else
    printf '%-8s | %-12s | %-14s | %-6s | %s\n' language "tests passed" "golden passed" time status
    printf '%s\n' "---------+--------------+----------------+--------+-------"
    for lang in python cpp rust java; do
        printf '%-8s | %-12s | %-14s | %4ss | %s\n' \
            "$lang" "${TESTS[$lang]}" "${GOLDEN[$lang]}" "${SECS[$lang]}" "${STATUS[$lang]}"
    done
    if [ "$DEPLOY_STATUS" != "-" ]; then
        printf '%-8s | %-12s | %-14s | %4ss | %s\n' \
            deployment "-" "-" "${SECS[deployment]}" "$DEPLOY_STATUS"
        printf '%-8s | %-12s | %-14s | %4ss | %s\n' \
            numbers "-" "-" "-" "$NUMBERS_STATUS"
    fi
fi
echo "======================================================================="
if [ -n "$DEPLOY_DETAIL" ]; then
    echo "$DEPLOY_DETAIL"
fi
if [ -n "$NUMBERS_DETAIL" ]; then
    echo "headline numbers: $NUMBERS_DETAIL"
fi
echo "(a '-' count means the suite did not run in this mode; '?' means it ran"
echo " but its count could not be parsed — both are treated as a failure.)"

if [ "$OVERALL" -eq 0 ]; then
    note "PARITY OK — all languages passed ($MODE)."
else
    note "PARITY BROKEN — inspect logs in $LOG_DIR"
fi
exit "$OVERALL"
