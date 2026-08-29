#!/usr/bin/env bash
# Build the IAP Java core (javac only, no Maven — see PLATFORM_CONVENTIONS.md §9).
# -Xlint:all -Werror: the build must be warning-free.
set -euo pipefail
cd "$(dirname "$0")"

rm -rf out/main
mkdir -p out/main
find src/main/java -name '*.java' | sort > out/main-sources.txt
javac -Xlint:all -Werror -d out/main @out/main-sources.txt
echo "build OK: $(wc -l < out/main-sources.txt) main sources -> out/main"
