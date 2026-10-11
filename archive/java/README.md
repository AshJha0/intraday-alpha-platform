# Archived: the Java engine (frozen at v1.14.0)

This directory is the Java port of the platform (`com.iap.*`: codec, order
book, replay, features, alpha, risk, execution / SOR, contracts / trace,
lifecycle, portfolio, TCA, backtest, adaptive monitors, and the paper-trading
platform vertical). It was FROZEN by [docs/POLYGLOT.md](../../docs/POLYGLOT.md)
and is archived in v1.15 (plan item G-P1-3, owner-approved).

**State at archive time: tag `v1.14.0` (commit `e15abab`).** At that tag CI
ran the full JUnit4 suite green: 575 tests, of which the seventeen
`*GoldenTest` classes are 124 golden-group tests, byte-identical to the
Python reference goldens in `tests/golden/`.

What archiving means:

- No CI job builds or tests this code any more: the `java` job, the Java
  golden group in `tests/harness/run_all.sh` / `run_golden.sh`, the Java
  engine of the nightly risk differential fuzz and the CodeQL `java-kotlin`
  analysis were removed. The parity table has three language rows
  (python / cpp / rust).
- The Java FROZEN paths were removed from `tests/harness/polyglot_policy.json`;
  the archive is not maintained. Goldens may move on without it, so after
  v1.14.0 this code is not guaranteed to match the current `tests/golden/`.
- The deployment still builds the `java` platform image from this frozen copy
  (`deployment/docker/Dockerfile.java` copies `archive/java` to `java` in the
  build stage), so `docker-compose.yml` and `deployment/k8s/java-platform.yaml`
  are unchanged.

## How to restore

The sources build in place (`bash archive/java/build.sh`), but the tests,
`demo.sh` and `paper.sh` use repo-relative paths (`../tests/golden`,
`../configs`) that assume the original location. To bring the engine back:

```bash
git mv archive/java java
# then revert the v1.15 G-P1-3 change set (see CHANGELOG.md, v1.15 Unreleased):
#   the `java` job in .github/workflows/ci.yml (and its entry in images.needs),
#   setup-java + junit4 in the `golden` job,
#   run_java / JAVA_GOLDEN_CLASSES in tests/harness/run_all.sh,
#   the Java engine in python/tools/risk_fuzz_nightly.py and risk-fuzz.yml,
#   java-kotlin in codeql.yml, the Java FROZEN entries in polyglot_policy.json,
#   the java row of the README parity table and the four-language counts.
cd java && bash build.sh && bash test.sh   # JUnit4 jars at /usr/share/java
```

Then re-add `java` to the `protect-main` required status checks
(docs/governance/REPO_SETTINGS.md). The simplest exact restore is
`git revert` of the archive commit, or `git checkout v1.14.0 -- java`.
