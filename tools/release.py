#!/usr/bin/env python3
"""Release automation (v1.15, E-P1-1): the v1.14.0 release flow as code.

Every subcommand is a DRY RUN by default: it prints the commands and file
changes it would make and runs only read-only queries (``git show``,
``gh run list``, ...). Pass ``--execute`` to act. The steps encode the
lessons of the v1.14.0 release (2026-10-11):

``prepare-regenerate``  on the merged release branch, before dispatching
    the CI ``regenerate`` job:
    1. reset ``research/experiments.json`` to the baseline ledger on
       ``--baseline-ref`` (default ``origin/main``); a merged branch carries
       looks the regeneration would refuse to repeat;
    2. delete the five ExperimentRunner directories of the current
       generation (``RUNNER_EXPERIMENTS`` of
       tools/regenerate_dataset_artifacts.py: per (alpha, horizon) the
       newest by ``result.json`` ``created_ts``) so CI re-runs them in
       place under the same ids;
    3. commit + push, then dispatch
       ``gh workflow run ci.yml --ref <branch> -f regenerate=true
       -f regenerate_allow_rerun=true``.

``apply-bundle --run-id N``  download the ``regenerated-artefacts``
    artifact of run N and extract it with Python's ``tarfile`` (v1.16:
    the system ``tar`` on Windows is bsdtar, which has no
    ``--force-local``; members are pre-checked and extracted with the
    ``data`` filter), skipping every ``expected_features*`` file whose only difference from
    the working tree is line endings (CRLF-only churn).

``publish --version X.Y.Z``  after the release PR is merged:
    1. require a green ``ci`` run and a green ``codeql`` run (every
       ``analyze`` job) on main for the merge commit;
    2. annotated tag ``vX.Y.Z`` on it, message = the CHANGELOG section;
    3. ``gh release create vX.Y.Z --verify-tag`` with the CHANGELOG section;
    4. watch ``release.yml`` for the tag; when it failed in its verify-ci
       guard (raced the main CI run), ``gh run rerun --failed`` once;
    5. digest pinning (v1.17 WS-F item F2, finding E-5): download the
       ``release-manifest.json`` asset of the release and rewrite every
       ``ghcr.io/ashjha0/intraday-alpha-platform-<lang>`` reference in the
       Kubernetes manifests (``PINNED_MANIFESTS``) to
       ``<name>:vX.Y.Z@sha256:<digest>``; the changed files are left in the
       working tree for a follow-up "release: pin vX.Y.Z image digests" PR
       (``main`` is protected). Compose keeps the tag: a ``build:`` service
       cannot carry a digest;
    6. site check: the GitHub Pages site mentions the full ``vX.Y.Z`` (a
       ``vX.Y`` match is vacuous: the site's roadmap names the next minor
       before it ships).

``pin-digests --manifest FILE [--version X.Y.Z]``  step 5 alone, from a
    local ``release-manifest.json`` (dry run prints the per-file changes;
    ``--execute`` writes them).

In dry-run mode a failed ``publish`` precondition (tag exists, no CHANGELOG
section, main CI not green) is reported as a BLOCKER and the rest of the
plan is still printed; the command then exits non-zero.

``flow``  print the whole sequence above (no queries).
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LEDGER = "research/experiments.json"
EXPERIMENTS_DIR = "research/experiments"
BUNDLE_ARTIFACT = "regenerated-artefacts"
BUNDLE_FILE = "regenerated.tar.gz"
CRLF_SKIP_PREFIX = "expected_features"
SITE_URL = "https://ashjha0.github.io/intraday-alpha-platform/"
IMAGE_PREFIX = "ghcr.io/ashjha0/intraday-alpha-platform-"
#: The deployment manifests `publish` pins by digest (repo-relative). Compose
#: is deliberately absent (its services carry `build:`).
PINNED_MANIFESTS = (
    "deployment/k8s/platform-metrics.yaml",
    "deployment/k8s/cronjob-data-pipeline.yaml",
    "deployment/k8s/cronjob-state-backup.yaml",
)
MANIFEST_ASSET = "release-manifest.json"

#: Mirrors tools/regenerate_dataset_artifacts.py (a test keeps them equal).
RUNNER_EXPERIMENTS = (
    ("EQ03", "1s"),
    ("EQ01", "1s"),
    ("EQ06", "1s"),
    ("EQ03", "5s"),
    ("EQ06", "10s"),
)


class ReleaseError(RuntimeError):
    """A precondition of a release step does not hold."""


@dataclass
class Runner:
    """Runs commands; in dry-run mode only read-only ones.

    ``exec_fn(argv, cwd) -> stdout`` is injectable for tests.
    """

    execute: bool = False
    cwd: Path = REPO
    exec_fn: Callable[[Sequence[str], Path], str] | None = None
    log: list[str] = field(default_factory=list)

    def _exec(self, argv: Sequence[str]) -> str:
        if self.exec_fn is not None:
            return self.exec_fn(list(argv), self.cwd)
        res = subprocess.run(
            list(argv), cwd=self.cwd, capture_output=True, text=True, encoding="utf-8"
        )
        if res.returncode != 0:
            raise ReleaseError(
                f"{shlex.join(argv)} failed ({res.returncode}): {res.stderr.strip()}"
            )
        return res.stdout

    def query(self, argv: Sequence[str]) -> str:
        """A read-only command: runs in dry-run mode too."""
        self.log.append(f"query: {shlex.join(argv)}")
        return self._exec(argv)

    def act(self, argv: Sequence[str]) -> str:
        """A mutating command: printed in dry-run mode, run with --execute."""
        line = shlex.join(argv)
        if not self.execute:
            self.log.append(f"would run: {line}")
            print(f"[dry-run] {line}")
            return ""
        self.log.append(f"run: {line}")
        print(f"$ {line}")
        return self._exec(argv)

    def note(self, msg: str) -> None:
        self.log.append(msg)
        print(("[dry-run] " if not self.execute else "") + msg)


# ------------------------------------------------------------ prepare-regenerate


def current_runner_dirs(repo: Path) -> list[str]:
    """Directory names of the newest ExperimentRunner result per pinned
    (alpha, horizon) — the generation CI's regenerate re-runs in place."""
    newest: dict[tuple[str, str], tuple[int, str]] = {}
    root = repo / EXPERIMENTS_DIR
    for d in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        spec_p, res_p = d / "spec.json", d / "result.json"
        if not (spec_p.is_file() and res_p.is_file()):
            continue
        spec = json.loads(spec_p.read_text(encoding="utf-8"))
        key = (spec.get("alpha_id"), spec.get("horizon"))
        if key not in RUNNER_EXPERIMENTS:
            continue
        created = int(json.loads(res_p.read_text(encoding="utf-8")).get("created_ts", 0))
        if key not in newest or created > newest[key][0]:
            newest[key] = (created, d.name)
    return sorted(name for _, name in newest.values())


def prepare_regenerate(
    r: Runner,
    branch: str,
    baseline_ref: str = "origin/main",
    runner_dirs: Sequence[str] | None = None,
    regenerate_only: str = "",
) -> None:
    head = r.query(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()
    if r.execute and head != branch:
        raise ReleaseError(f"checked out {head!r}, expected the release branch {branch!r}")
    if r.query(["git", "status", "--porcelain"]).strip():
        raise ReleaseError("working tree is not clean")
    baseline = r.query(["git", "show", f"{baseline_ref}:{LEDGER}"])
    json.loads(baseline)  # fail before touching anything if it is not a ledger
    ledger = r.cwd / LEDGER
    if not ledger.is_file() or ledger.read_text(encoding="utf-8") != baseline:
        r.note(f"reset {LEDGER} to {baseline_ref}")
        if r.execute:
            ledger.write_text(baseline, encoding="utf-8", newline="\n")
    else:
        r.note(f"{LEDGER} already equals {baseline_ref}")
    dirs = list(runner_dirs) if runner_dirs is not None else current_runner_dirs(r.cwd)
    if len(dirs) != len(RUNNER_EXPERIMENTS):
        r.note(f"warning: {len(dirs)} runner dirs found, expected {len(RUNNER_EXPERIMENTS)}")
    for name in dirs:
        r.act(["git", "rm", "-r", "-q", f"{EXPERIMENTS_DIR}/{name}"])
    r.act(["git", "add", LEDGER])
    r.act(
        [
            "git",
            "commit",
            "-m",
            f"release: reset experiments ledger to {baseline_ref} and drop the "
            f"{len(dirs)} runner experiment dirs for CI regeneration",
        ]
    )
    r.act(["git", "push", "origin", branch])
    dispatch = [
        "gh",
        "workflow",
        "run",
        "ci.yml",
        "--ref",
        branch,
        "-f",
        "regenerate=true",
        "-f",
        "regenerate_allow_rerun=true",
    ]
    if regenerate_only:
        dispatch += ["-f", f"regenerate_only={regenerate_only}"]
    r.act(dispatch)


# ------------------------------------------------------------------ apply-bundle


def _crlf_only(a: bytes, b: bytes) -> bool:
    return a != b and a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n")


def bundle_plan(bundle: Path, repo: Path) -> tuple[list[str], list[str]]:
    """``(extract, skipped)`` member names of a regenerated bundle; an
    ``expected_features*`` member that differs from the working tree only
    in line endings is skipped."""
    extract: list[str] = []
    skipped: list[str] = []
    with tarfile.open(bundle, "r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            name = m.name
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ReleaseError(f"unsafe path in bundle: {name}")
            local = repo / name
            if Path(name).name.startswith(CRLF_SKIP_PREFIX) and local.is_file():
                data = tar.extractfile(m).read()
                if _crlf_only(data, local.read_bytes()):
                    skipped.append(name)
                    continue
            extract.append(name)
    return extract, skipped


def _extract(bundle: Path, dest: Path, names: Sequence[str]) -> None:
    """Extract ``names`` (already path-checked by :func:`bundle_plan`)."""
    want = set(names)
    with tarfile.open(bundle, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name in want]
        tar.extractall(dest, members=members, filter="data")


def apply_bundle(r: Runner, run_id: str, download_dir: Path | None = None) -> list[str]:
    work = download_dir or Path(tempfile.mkdtemp(prefix="iap-regen-"))
    bundle = work / BUNDLE_FILE
    if not bundle.is_file():
        # downloading writes only into the scratch dir: safe in dry-run too
        r.query(["gh", "run", "download", str(run_id), "-n", BUNDLE_ARTIFACT, "-D", str(work)])
    if not bundle.is_file():
        raise ReleaseError(f"{bundle} not found after download")
    extract, skipped = bundle_plan(bundle, r.cwd)
    for name in skipped:
        r.note(f"skip CRLF-only change: {name}")
    if extract:
        line = f"extract {len(extract)} files from {bundle} into {r.cwd} (python tarfile)"
        if not r.execute:
            r.log.append(f"would run: {line}")
            print(f"[dry-run] {line}")
        else:
            r.log.append(f"run: {line}")
            print(f"$ {line}")
            _extract(bundle, r.cwd, extract)
    r.note(f"{len(extract)} files to apply, {len(skipped)} skipped (CRLF only)")
    return extract


# ----------------------------------------------------------------------- publish


def changelog_section(text: str, version: str) -> str:
    """The body of ``## vX.Y.Z ...`` up to the next ``## `` heading."""
    pat = re.compile(rf"^## v{re.escape(version)}\b.*$", re.M)
    m = pat.search(text)
    if not m:
        raise ReleaseError(f"CHANGELOG.md has no '## v{version}' section")
    nxt = re.search(r"^## ", text[m.end() :], re.M)
    body = text[m.end() : m.end() + nxt.start()] if nxt else text[m.end() :]
    return body.strip() + "\n"


def stale_unreleased(text: str, version: str) -> str | None:
    """An ``## Unreleased ...`` heading still naming this release's vX.Y(.Z), if any.

    A bare ``## Unreleased`` (the next cycle) is fine; ``## Unreleased (v1.15)``
    while publishing 1.15.0 means the heading was not renamed.
    """
    major_minor = ".".join(version.split(".")[:2])
    pat = re.compile(rf"^## +Unreleased\b.*\bv{re.escape(major_minor)}(?:\.\d+)?\b.*$", re.M | re.I)
    m = pat.search(text)
    return m.group(0).strip() if m else None


def main_ci_green(r: Runner, sha: str, workflow: str = "ci.yml") -> bool:
    """A completed, successful push run of ``workflow`` on main for ``sha``.

    For ``codeql.yml`` a successful run means every ``analyze`` matrix job
    passed (the run concludes ``success`` only when all its jobs do)."""
    out = r.query(
        [
            "gh",
            "run",
            "list",
            "--workflow",
            workflow,
            "--branch",
            "main",
            "--commit",
            sha,
            "--json",
            "databaseId,status,conclusion,event",
        ]
    )
    runs = [x for x in json.loads(out or "[]") if x.get("event") == "push"]
    return any(x.get("status") == "completed" and x.get("conclusion") == "success" for x in runs)


def release_run(r: Runner, tag: str) -> dict | None:
    out = r.query(
        [
            "gh",
            "run",
            "list",
            "--workflow",
            "release.yml",
            "--branch",
            tag,
            "--json",
            "databaseId,status,conclusion",
        ]
    )
    runs = json.loads(out or "[]")
    return runs[0] if runs else None


def verify_ci_raced(r: Runner, run_id: int) -> bool:
    out = r.query(["gh", "run", "view", str(run_id), "--json", "jobs"])
    jobs = json.loads(out or "{}").get("jobs", [])
    return any(j.get("name") == "verify-ci" and j.get("conclusion") == "failure" for j in jobs)


def site_mentions(version: str, fetch: Callable[[str], str] | None = None) -> bool:
    """The page names the full ``vX.Y.Z`` (not ``vX.Y.Z0`` or ``vX.Y.Z.1``)."""
    pat = re.compile(rf"v{re.escape(version)}(?!\.?\d)")
    if fetch is None:

        def fetch(url: str) -> str:
            with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (fixed https URL)
                return resp.read().decode("utf-8", "replace")

    return pat.search(fetch(SITE_URL)) is not None


def publish(
    r: Runner,
    version: str,
    poll_s: float = 30.0,
    max_polls: int = 60,
    fetch: Callable[[str], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    manifest_fetch: Callable[[Runner, str], dict] | None = None,
    pinned_files: Sequence[str] = PINNED_MANIFESTS,
) -> None:
    if manifest_fetch is None:
        manifest_fetch = download_manifest
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ReleaseError(f"version must be X.Y.Z, got {version!r}")
    tag = f"v{version}"
    r.query(["git", "fetch", "origin", "main", "--tags"])
    sha = r.query(["git", "rev-parse", "origin/main"]).strip()
    blockers: list[str] = []

    def block(msg: str) -> None:
        if r.execute:
            raise ReleaseError(msg)
        blockers.append(msg)
        r.note(f"BLOCKER: {msg}")

    if r.query(["git", "tag", "--list", tag]).strip():
        block(f"tag {tag} already exists")
    changelog = r.query(["git", "show", f"{sha}:CHANGELOG.md"])
    try:
        section = changelog_section(changelog, version)
    except ReleaseError as e:
        block(str(e))
        section = f"(CHANGELOG section for {tag} missing)\n"
    stale = stale_unreleased(changelog, version)
    if stale:
        block(f"CHANGELOG.md heading {stale!r} not renamed to '## {tag} — <date>'")
    if not section.strip() or section.startswith("(CHANGELOG section"):
        if r.execute:
            raise ReleaseError(f"CHANGELOG section for {tag} is missing or empty")
    if not main_ci_green(r, sha):
        block(f"no green ci run on main for {sha}: tag only after main CI is green")
    if not main_ci_green(r, sha, "codeql.yml"):
        block(
            f"no green codeql run on main for {sha}: tag only after every CodeQL "
            "analyze job is green"
        )
    notes = Path(tempfile.mkdtemp(prefix="iap-rel-")) / f"{tag}.md"
    notes.write_text(section, encoding="utf-8", newline="\n")
    r.note(f"release notes from CHANGELOG ({len(section.splitlines())} lines): {notes}")
    r.act(["git", "tag", "-a", tag, sha, "-F", str(notes)])
    r.act(["git", "push", "origin", tag])
    r.act(
        ["gh", "release", "create", tag, "--verify-tag", "--title", tag, "--notes-file", str(notes)]
    )
    if not r.execute:
        r.note(
            f"then: watch release.yml for {tag}; rerun once if verify-ci raced; "
            f"pin {', '.join(PINNED_MANIFESTS)} to the manifest digests "
            f"(python tools/release.py pin-digests --manifest release-manifest.json "
            f"--version {version} --execute); site check"
        )
        if blockers:
            raise ReleaseError("dry run found blockers: " + "; ".join(blockers))
        return
    rerun = False
    for _ in range(max_polls):
        run = release_run(r, tag)
        if run and run.get("status") == "completed":
            if run.get("conclusion") == "success":
                break
            if not rerun and verify_ci_raced(r, run["databaseId"]):
                r.act(["gh", "run", "rerun", str(run["databaseId"]), "--failed"])
                rerun = True
                sleep(poll_s)
                continue
            raise ReleaseError(f"release.yml run {run['databaseId']} {run.get('conclusion')}")
        sleep(poll_s)
    else:
        raise ReleaseError("release.yml did not finish in time")
    pinned = pin_digests(r, manifest_fetch(r, tag), version, pinned_files)
    r.note(
        f"pinned {sum(pinned.values())} image reference(s) in {len(pinned)} manifest(s); "
        f"commit them as 'release: pin {tag} image digests' (a PR: main is protected)"
    )
    for _ in range(max_polls):
        if site_mentions(version, fetch):
            r.note(f"site shows {tag}")
            return
        sleep(poll_s)
    raise ReleaseError(f"site {SITE_URL} does not mention {tag}")


# ----------------------------------------------------------------- pin-digests

_IMAGE_REF = re.compile(
    rf"(?P<name>{re.escape(IMAGE_PREFIX)}(?P<lang>[a-z]+))"
    r"(?::(?P<tag>v\d+\.\d+\.\d+))?(?:@sha256:[0-9a-f]{64})?"
)


def manifest_digests(manifest: dict, version: str | None = None) -> dict[str, str]:
    """``{language: "sha256:..."}`` of a ``release-manifest.json`` document
    (release.yml schema: ``images: [{language, name, digest, reference}]``).
    With ``version`` the manifest's ``tag`` must be ``vVERSION``."""
    if version is not None and manifest.get("tag") != f"v{version}":
        raise ReleaseError(f"manifest tag {manifest.get('tag')!r} is not v{version}")
    out: dict[str, str] = {}
    for img in manifest.get("images") or []:
        lang, digest, name = img.get("language"), img.get("digest"), img.get("name")
        if not (isinstance(lang, str) and isinstance(digest, str) and isinstance(name, str)):
            raise ReleaseError(f"malformed image entry in manifest: {img!r}")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ReleaseError(f"{lang}: digest {digest!r} is not sha256:<64 hex>")
        if name != IMAGE_PREFIX + lang:
            raise ReleaseError(f"{lang}: image name {name!r} is not {IMAGE_PREFIX}{lang}")
        out[lang] = digest
    if not out:
        raise ReleaseError("manifest lists no images")
    return out


def pin_text(text: str, tag: str, digests: dict[str, str]) -> tuple[str, int]:
    """Rewrite every iap image reference in ``text`` to ``name:tag@digest``;
    returns the new text and the number of references changed. A language
    the manifest does not list is an error (never leave one image unpinned)."""
    changed = 0
    missing: set[str] = set()

    def repl(m: re.Match) -> str:
        nonlocal changed
        lang = m.group("lang")
        if lang not in digests:
            missing.add(lang)
            return m.group(0)
        new = f"{m.group('name')}:{tag}@{digests[lang]}"
        if new != m.group(0):
            changed += 1
        return new

    out = _IMAGE_REF.sub(repl, text)
    if missing:
        raise ReleaseError(f"manifest has no digest for image(s): {', '.join(sorted(missing))}")
    return out, changed


def pin_digests(
    r: Runner, manifest: dict, version: str, files: Sequence[str] = PINNED_MANIFESTS
) -> dict[str, int]:
    """Pin ``files`` (repo-relative) to the digests of ``manifest``;
    ``{file: references changed}``. Dry run reports, ``--execute`` writes."""
    digests = manifest_digests(manifest, version)
    tag = f"v{version}"
    result: dict[str, int] = {}
    for rel in files:
        path = r.cwd / rel
        if not path.is_file():
            raise ReleaseError(f"{rel} not found")
        text = path.read_text(encoding="utf-8")
        new, n = pin_text(text, tag, digests)
        result[rel] = n
        if n == 0:
            r.note(f"{rel}: already pinned to {tag}")
            continue
        r.note(f"{rel}: pin {n} image reference(s) to {tag}@sha256:...")
        if r.execute:
            path.write_text(new, encoding="utf-8", newline="\n")
    return result


def download_manifest(r: Runner, tag: str) -> dict:
    """The ``release-manifest.json`` asset of release ``tag`` (a read-only
    download into a scratch dir)."""
    work = Path(tempfile.mkdtemp(prefix="iap-manifest-"))
    r.query(
        [
            "gh",
            "release",
            "download",
            tag,
            "--pattern",
            MANIFEST_ASSET,
            "--dir",
            str(work),
            "--clobber",
        ]
    )
    path = work / MANIFEST_ASSET
    if not path.is_file():
        raise ReleaseError(f"release {tag} has no {MANIFEST_ASSET} asset")
    return json.loads(path.read_text(encoding="utf-8"))


# -------------------------------------------------------------------------- CLI

FLOW = """\
release flow (tools/release.py, dry-run by default; --execute to act):
 1. merge the working branches into release/vX.Y.Z (one PR the owner merges)
 2. python tools/release.py prepare-regenerate --branch release/vX.Y.Z
      reset research/experiments.json to origin/main, drop the 5 runner dirs,
      commit + push, gh workflow run ci.yml -f regenerate=true -f regenerate_allow_rerun=true
 3. python tools/release.py apply-bundle --run-id <regenerate run id>
      gh run download -n regenerated-artefacts; extract with python tarfile,
      skipping CRLF-only expected_features*; review the diff, commit, push
 4. owner merges the release PR (merge commit)
 5. python tools/release.py publish --version X.Y.Z
      main ci + codeql green -> annotated tag from CHANGELOG -> gh release --verify-tag
      -> release.yml (rerun if verify-ci raced) -> k8s manifests pinned to
      name:vX.Y.Z@sha256:<digest> from release-manifest.json -> site shows vX.Y.Z
      (needs a '## vX.Y.Z' CHANGELOG heading: rename 'Unreleased' first)
 6. open the 'release: pin vX.Y.Z image digests' PR with the rewritten
      deployment/k8s manifests (python tools/release.py pin-digests ... --execute
      re-runs that step from a local release-manifest.json)
"""


def main(argv: Sequence[str] | None = None, runner: Runner | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--execute", action="store_true", help="act (default: dry run)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare-regenerate")
    p.add_argument("--branch", required=True)
    p.add_argument("--baseline-ref", default="origin/main")
    p.add_argument("--runner-dir", action="append", help="override the runner dirs to drop")
    p.add_argument("--regenerate-only", default="")
    a = sub.add_parser("apply-bundle")
    a.add_argument("--run-id", required=True)
    a.add_argument("--download-dir", type=Path)
    pub = sub.add_parser("publish")
    pub.add_argument("--version", required=True)
    pin = sub.add_parser("pin-digests")
    pin.add_argument("--manifest", type=Path, required=True, help="a release-manifest.json")
    pin.add_argument("--version", default=None, help="X.Y.Z the manifest must carry")
    pin.add_argument("--file", action="append", help="override the manifests to pin")
    sub.add_parser("flow")
    args = ap.parse_args(argv)
    r = runner or Runner(execute=args.execute)
    if args.execute:
        r.execute = True
    try:
        if args.cmd == "flow":
            print(FLOW, end="")
        elif args.cmd == "prepare-regenerate":
            prepare_regenerate(
                r, args.branch, args.baseline_ref, args.runner_dir, args.regenerate_only
            )
        elif args.cmd == "apply-bundle":
            apply_bundle(r, args.run_id, args.download_dir)
        elif args.cmd == "pin-digests":
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            version = args.version or str(manifest.get("tag", "")).lstrip("v")
            if not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise ReleaseError(f"cannot tell the version from {args.manifest} (--version)")
            pin_digests(r, manifest, version, args.file or PINNED_MANIFESTS)
        else:
            publish(r, args.version)
    except ReleaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
