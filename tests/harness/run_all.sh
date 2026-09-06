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
#   python  pytest -k golden            (test_golden.py, test_feature_golden.py,
#                                        test_alpha_golden.py, test_portfolio_golden.py,
#                                        test_tca_golden.py + golden-named tests)
#   cpp     ctest -R Golden             (gtest suites Golden, FeatureGolden,
#                                        AlphaGolden, ReplayFillsGolden, SplitMix64Golden)
#   rust    cargo test -p <crate> --test <golden target> for each golden
#           integration-test file (golden_marketdata, golden_book,
#           golden_features, golden_alpha, golden_risk, golden_replay)
#   java    JUnitCore on the *GoldenTest classes (CodecGoldenTest, BookGoldenTest)
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
# Java golden test classes (JUnit4).
JAVA_GOLDEN_CLASSES="com.iap.CodecGoldenTest com.iap.BookGoldenTest"

# Per-language results
declare -A TESTS GOLDEN STATUS SECS
OVERALL=0

note() { printf '>> %s\n' "$*"; }

run_logged() { # run_logged <logfile> <cmd...>
    local log="$1"; shift
    ( "$@" ) >"$log" 2>&1
}

# ----------------------------------------------------------------- python ---
run_python() {
    local t0=$SECONDS ok=1 full=0 gold=0
    if [ "$GOLDEN_ONLY" -eq 0 ]; then
        note "python: PYTHONPATH=src python3 -m pytest -q"
        if run_logged "$LOG_DIR/python_full.log" \
            env -C "$ROOT/python" PYTHONPATH=src python3 -m pytest -q; then
            full=$(grep -Eo '[0-9]+ passed' "$LOG_DIR/python_full.log" | tail -1 | grep -Eo '[0-9]+')
        else ok=0; fi
    fi
    note "python: PYTHONPATH=src python3 -m pytest -q -k golden"
    if run_logged "$LOG_DIR/python_golden.log" \
        env -C "$ROOT/python" PYTHONPATH=src python3 -m pytest -q -k golden; then
        gold=$(grep -Eo '[0-9]+ passed' "$LOG_DIR/python_golden.log" | tail -1 | grep -Eo '[0-9]+')
    else ok=0; fi
    [ "$GOLDEN_ONLY" -eq 1 ] && full=$gold
    TESTS[python]=${full:-0}; GOLDEN[python]=${gold:-0}
    STATUS[python]=$([ $ok -eq 1 ] && echo PASS || echo FAIL)
    SECS[python]=$((SECONDS - t0)); [ $ok -eq 1 ] || OVERALL=1
}

# -------------------------------------------------------------------- cpp ---
run_cpp() {
    local t0=$SECONDS ok=1 full=0 gold=0
    note "cpp: bash build.sh"
    if ! run_logged "$LOG_DIR/cpp_build.log" env -C "$ROOT/cpp" bash build.sh; then
        TESTS[cpp]=0; GOLDEN[cpp]=0; STATUS[cpp]="BUILD FAIL"
        SECS[cpp]=$((SECONDS - t0)); OVERALL=1; return
    fi
    if [ "$GOLDEN_ONLY" -eq 0 ]; then
        note "cpp: ctest --test-dir build --output-on-failure"
        if run_logged "$LOG_DIR/cpp_full.log" \
            env -C "$ROOT/cpp" ctest --test-dir build --output-on-failure; then
            full=$(grep -Eo 'out of [0-9]+' "$LOG_DIR/cpp_full.log" | tail -1 | grep -Eo '[0-9]+')
        else ok=0; fi
    fi
    note "cpp: ctest --test-dir build -R Golden"
    if run_logged "$LOG_DIR/cpp_golden.log" \
        env -C "$ROOT/cpp" ctest --test-dir build --output-on-failure -R Golden; then
        gold=$(grep -Eo 'out of [0-9]+' "$LOG_DIR/cpp_golden.log" | tail -1 | grep -Eo '[0-9]+')
    else ok=0; fi
    [ "$GOLDEN_ONLY" -eq 1 ] && full=$gold
    TESTS[cpp]=${full:-0}; GOLDEN[cpp]=${gold:-0}
    STATUS[cpp]=$([ $ok -eq 1 ] && echo PASS || echo FAIL)
    SECS[cpp]=$((SECONDS - t0)); [ $ok -eq 1 ] || OVERALL=1
}

# ------------------------------------------------------------------- rust ---
run_rust() {
    local t0=$SECONDS ok=1 full=0 gold=0
    if [ "$GOLDEN_ONLY" -eq 0 ]; then
        note "rust: cargo test (workspace)"
        if run_logged "$LOG_DIR/rust_full.log" env -C "$ROOT/rust" cargo test; then
            full=$(grep -Eo 'test result: ok\. [0-9]+ passed' "$LOG_DIR/rust_full.log" \
                   | grep -Eo '[0-9]+' | paste -sd+ | bc)
        else ok=0; fi
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
    [ "$GOLDEN_ONLY" -eq 1 ] && full=$gold
    TESTS[rust]=${full:-0}; GOLDEN[rust]=${gold:-0}
    STATUS[rust]=$([ $ok -eq 1 ] && echo PASS || echo FAIL)
    SECS[rust]=$((SECONDS - t0)); [ $ok -eq 1 ] || OVERALL=1
}

# ------------------------------------------------------------------- java ---
run_java() {
    local t0=$SECONDS ok=1 full=0 gold=0
    local JUNIT=/usr/share/java/junit4.jar HAMCREST=/usr/share/java/hamcrest-core.jar
    if [ "$GOLDEN_ONLY" -eq 0 ]; then
        note "java: bash build.sh && bash test.sh"
        if run_logged "$LOG_DIR/java_full.log" env -C "$ROOT/java" bash test.sh; then
            full=$(grep -Eo 'OK \([0-9]+ tests?\)' "$LOG_DIR/java_full.log" | grep -Eo '[0-9]+')
        else ok=0; fi
    else
        note "java: bash build.sh + compile tests"
        if ! run_logged "$LOG_DIR/java_build.log" env -C "$ROOT/java" bash build.sh; then
            TESTS[java]=0; GOLDEN[java]=0; STATUS[java]="BUILD FAIL"
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
    else ok=0; fi
    [ "$GOLDEN_ONLY" -eq 1 ] && full=$gold
    TESTS[java]=${full:-0}; GOLDEN[java]=${gold:-0}
    STATUS[java]=$([ $ok -eq 1 ] && echo PASS || echo FAIL)
    SECS[java]=$((SECONDS - t0)); [ $ok -eq 1 ] || OVERALL=1
}

# -------------------------------------------------------------------- main --
MODE=$([ "$GOLDEN_ONLY" -eq 1 ] && echo "golden groups only" || echo "full suites")
note "intraday-alpha-platform harness — $MODE (logs: $LOG_DIR)"
run_python
run_cpp
run_rust
run_java

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
fi
echo "======================================================================="

if [ "$OVERALL" -eq 0 ]; then
    note "PARITY OK — all languages passed ($MODE)."
else
    note "PARITY BROKEN — inspect logs in $LOG_DIR"
fi
exit "$OVERALL"
