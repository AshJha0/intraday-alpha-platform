#!/usr/bin/env python3
"""tests/harness/check_docker_build.py — prove the three images can be built.

Round-3 PLATFORM finding SEV-1: "the C++ and Rust images cannot be built from
this repository" — their build-time test steps read `configs/` and
`data/reference/`, which the Dockerfiles never copied, and with no
`.dockerignore` a host `cpp/build/CMakeCache.txt` poisoned the C++ build.

With a Docker daemon this script builds all three images for real. Without one
(the usual case in this environment — `docker info` reports no server) it does
the next best thing, which is what actually catches the bug class:

  1. materialise a CLEAN checkout of the working tree into a temp dir: every
     file `git ls-files` tracks (so staged moves — `git mv` — are honoured
     before they are committed), at its working-tree content, and nothing
     else — no cpp/build, no rust/target, exactly what a fresh checkout plus
     `.dockerignore` gives Docker as a build context;
  2. apply the repository's `.dockerignore` to that clone, so the context is
     byte-for-byte what `docker build` would send;
  3. assert every `COPY <src> <dst>` of every stage resolves inside it;
  4. assert the paths each stage's BUILD-TIME and RUN-TIME commands read
     resolve too — `<golden>/../../configs` for the C++ golden tests and
     bench_all, `../../configs` and `../../data/reference` for the Rust
     workspace tests, `/app/configs` and the MVP config for the Python image's
     `iap.metrics` / `iap.mvp` entrypoints (v1.17 F9: the Java image is
     retired; its Dockerfile is kept, unbuilt, under archive/java/).
     The C++/Rust build stages are compiled by tests/harness/run_all.sh in the
     same CI run, so their commands are covered there; this script asserts
     their INPUTS, which is the half that was broken.

Usage:
    python3 tests/harness/check_docker_build.py [--docker] [--keep]

    --docker  force a real `docker build` of all three images (fails if no
              daemon is reachable)
    --keep    leave the temp clone in place for inspection
"""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKER_DIR = ROOT / "deployment" / "docker"

RESULTS: list[tuple[str, bool, str]] = []


def record(case: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((case, ok, detail))
    print(f"  [{'ok  ' if ok else 'FAIL'}] {case}{': ' + detail if detail else ''}")


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 1800):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def docker_daemon_available() -> bool:
    if not shutil.which("docker"):
        return False
    proc = run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=60)
    return proc.returncode == 0 and proc.stdout.strip() != ""


# ------------------------------------------------------------------ context --
def dockerignore_rules() -> list[str]:
    f = ROOT / ".dockerignore"
    if not f.exists():
        return []
    return [
        ln.strip()
        for ln in f.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def ignored(rel: str, rules: list[str]) -> bool:
    for rule in rules:
        pat = rule.rstrip("/")
        if fnmatch.fnmatch(rel, pat) or rel.startswith(pat + "/"):
            return True
        # `**/__pycache__/` style
        if pat.startswith("**/") and (fnmatch.fnmatch(rel, pat[3:]) or f"/{pat[3:]}" in f"/{rel}"):
            return True
    return False


def build_context(dest: Path) -> tuple[int, int]:
    """Materialise the tracked tree (index paths, working-tree content) into
    dest, then delete everything .dockerignore excludes.

    `git ls-files` rather than `git clone`: a clone reproduces HEAD, which
    would silently test the PREVIOUS layout while a restructure (git mv +
    edits) is staged but not yet committed. On a committed tree the two are
    identical. A tracked file deleted from the working tree is skipped, so
    the result is what the next commit would contain."""
    proc = run(["git", "ls-files", "-z", "--cached", "--full-name"], cwd=ROOT, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {proc.stderr.strip()}")
    dest.mkdir(parents=True, exist_ok=True)
    for rel in proc.stdout.split("\0"):
        if not rel:
            continue
        src = ROOT / rel
        if not src.is_file():
            continue  # staged deletion / rename source
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    rules = dockerignore_rules()
    removed = 0
    kept = 0
    for path in sorted(dest.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        rel = str(path.relative_to(dest))
        if ignored(rel, rules):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
            removed += 1
        elif path.is_file():
            kept += 1
    return kept, removed


COPY_RE = re.compile(r"^COPY\s+(?:--from=(\S+)\s+)?(.+)$", re.M)
FROM_RE = re.compile(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", re.M | re.I)


def stages(text: str) -> list[tuple[str | None, list[tuple[str | None, list[str]]]]]:
    """[(stage name, [(from-stage or None, [src..., dst])])] per FROM block."""
    text = re.sub(r"\\\n\s*", " ", text)
    out = []
    positions = [(m.start(), m.group(2)) for m in FROM_RE.finditer(text)]
    for i, (start, name) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        copies = [(m.group(1), m.group(2).split()) for m in COPY_RE.finditer(text[start:end])]
        out.append((name, copies))
    return out


def check_copy_sources(ctx: Path) -> None:
    problems = []
    for df in sorted(DOCKER_DIR.glob("Dockerfile.*")):
        for _stage_name, copies in stages(df.read_text()):
            for from_stage, args in copies:
                if from_stage is not None:
                    continue  # a previous stage's filesystem
                for src in args[:-1]:
                    if not (ctx / src).exists():
                        problems.append(
                            f"{df.name}: COPY {src} — missing from a clean build context"
                        )
    record("copy_sources_exist_in_clean_context", not problems, "; ".join(problems) or "all stages")


def check_build_inputs(ctx: Path) -> None:
    """The paths each image's build-time and run-time commands actually read."""
    problems = []

    # C++: cpp/CMakeLists.txt compiles IAP_GOLDEN_DIR = <src>/../tests/golden;
    # test_alpha_golden.cpp and test_replay_fills.cpp read
    # <golden>/../../configs; bench_all reads the same path AT RUNTIME.
    for rel in [
        "tests/golden/events_eq_mbo.jsonl",
        "configs/strategies/alpha_params.json",
        "configs/venues/venues.json",
    ]:
        if not (ctx / rel).exists():
            problems.append(f"cpp: {rel} absent (ctest / bench_all read it)")

    # Rust: rules.rs -> ../../configs/risk/risk.json;
    # golden_alpha.rs -> configs/strategies/alpha_params.json;
    # golden_features.rs -> ../../data/reference/feature_registry.json.
    for rel in ["configs/risk/risk.json", "data/reference/feature_registry.json"]:
        if not (ctx / rel).exists():
            problems.append(f"rust: {rel} absent (cargo test reads it)")

    # Python: `python -m iap.metrics serve` reads $IAP_CONFIG_DIR/risk/risk.json
    # and `python -m iap.mvp run` the MVP config plus the references it names
    # (configs/mvp/*, configs/strategies/alpha_params.json,
    # research/alpha_registry.json, all COPYed by Dockerfile.python).
    for rel in [
        "configs/risk/risk.json",
        "configs/mvp/mvp.json",
        "configs/strategies/alpha_params.json",
        "research/alpha_registry.json",
        "python/src/iap/metrics/__main__.py",
    ]:
        if not (ctx / rel).exists():
            problems.append(f"python: {rel} absent (the entrypoints read it)")

    # The context must NOT carry host build state: that is the CMakeCache bug.
    for rel in ["cpp/build", "rust/target"]:
        if (ctx / rel).exists():
            problems.append(f"{rel} leaked into the build context (.dockerignore must exclude it)")
    record(
        "build_and_runtime_inputs_present",
        not problems,
        "; ".join(problems) or "cpp / rust / python inputs resolve",
    )


def check_real_docker_build(force: bool) -> None:
    if not docker_daemon_available():
        if force:
            record(
                "docker_build_all_images",
                False,
                "--docker requested but no Docker daemon is reachable",
            )
        else:
            record(
                "docker_build_all_images",
                True,
                "SKIPPED — no Docker daemon; the context checks above are "
                "the substitute (see this script's docstring)",
            )
        return
    for df in ["Dockerfile.python", "Dockerfile.cpp", "Dockerfile.rust"]:
        tag = "iap/check-" + df.split(".")[1].lower() + ":harness"
        proc = run(
            ["docker", "build", "-f", f"deployment/docker/{df}", "-t", tag, "."],
            cwd=ROOT,
            timeout=3600,
        )
        if proc.returncode != 0:
            record(
                "docker_build_all_images",
                False,
                f"{df}: {(proc.stdout + proc.stderr).strip()[-600:]}",
            )
            return
    record("docker_build_all_images", True, "3 images built")


def main() -> int:
    force_docker = "--docker" in sys.argv
    keep = "--keep" in sys.argv
    print("docker build verification (PLATFORM_CONVENTIONS.md §12.7)")
    tmp = Path(tempfile.mkdtemp(prefix="iap-build-context-"))
    ctx = tmp / "context"
    try:
        kept, removed = build_context(ctx)
        record(
            "clean_clone_context",
            True,
            f"{kept} files kept, {removed} paths excluded by .dockerignore",
        )
        check_copy_sources(ctx)
        check_build_inputs(ctx)
        check_real_docker_build(force_docker)
    finally:
        if keep:
            print(f"  context kept at {ctx}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    failed = [c for c, ok, _ in RESULTS if not ok]
    print()
    print(f"docker build checks: {len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    if failed:
        print("  FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
