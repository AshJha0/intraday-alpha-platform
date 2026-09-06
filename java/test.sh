#!/usr/bin/env bash
# Compile and run the JUnit4 suite (JUnit jar pinned at /usr/share/java/junit4.jar).
set -euo pipefail
cd "$(dirname "$0")"

JUNIT=/usr/share/java/junit4.jar
HAMCREST=/usr/share/java/hamcrest-core.jar
CP="out/main:$JUNIT:$HAMCREST"

bash build.sh

rm -rf out/test
mkdir -p out/test
find src/test/java -name '*.java' | sort > out/test-sources.txt
javac -Xlint:all -Werror -cp "$CP" -d out/test @out/test-sources.txt

java -cp "out/test:$CP" org.junit.runner.JUnitCore com.iap.AllTests
