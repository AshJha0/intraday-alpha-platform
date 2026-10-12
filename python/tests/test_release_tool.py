"""v1.15 E-P1-1: tools/release.py with mocked git / gh (no network, no repo writes)."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest
from conftest import REPO_ROOT


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


rel = _load("iap_release_tool", "tools/release.py")


class FakeExec:
    """Answers commands by longest matching argv prefix; records every call."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, cwd):
        self.calls.append(argv)
        best = None
        for prefix, out in self.answers.items():
            p = list(prefix)
            if argv[: len(p)] == p and (best is None or len(p) > len(best[0])):
                best = (p, out)
        if best is None:
            return ""
        out = best[1]
        return out(argv) if callable(out) else out


def _runner(tmp_path, answers, execute=False):
    fake = FakeExec(answers)
    return rel.Runner(execute=execute, cwd=tmp_path, exec_fn=fake), fake


def _experiment(root: Path, name, alpha, horizon, created):
    d = root / "research" / "experiments" / name
    d.mkdir(parents=True)
    (d / "spec.json").write_text(json.dumps({"alpha_id": alpha, "horizon": horizon}))
    (d / "result.json").write_text(json.dumps({"created_ts": created}))


def test_runner_list_matches_regenerate_tool():
    regen = _load("iap_regen_tool", "tools/regenerate_dataset_artifacts.py")
    assert rel.RUNNER_EXPERIMENTS == regen.RUNNER_EXPERIMENTS


def test_current_runner_dirs_picks_newest_generation(tmp_path):
    for i, (a, h) in enumerate(rel.RUNNER_EXPERIMENTS):
        _experiment(tmp_path, f"old{i}", a, h, 100)
        _experiment(tmp_path, f"new{i}", a, h, 200)
    _experiment(tmp_path, "other", "FX01", "1s", 999)
    assert rel.current_runner_dirs(tmp_path) == [f"new{i}" for i in range(5)]


BASELINE = json.dumps({"entries": [{"kind": "prereg"}]}, indent=2) + "\n"


def test_prepare_regenerate_dry_run_reproduces_v114_steps(tmp_path):
    for i, (a, h) in enumerate(rel.RUNNER_EXPERIMENTS):
        _experiment(tmp_path, f"run{i}", a, h, 5)
    ledger = tmp_path / "research" / "experiments.json"
    ledger.write_text('{"entries": []}\n')
    r, fake = _runner(
        tmp_path,
        {
            ("git", "rev-parse"): "release/v1.15.0\n",
            ("git", "status"): "",
            ("git", "show"): BASELINE,
        },
    )
    rel.prepare_regenerate(r, "release/v1.15.0")
    would = [line for line in r.log if line.startswith("would run")]
    assert [w.split()[3] for w in would[:5]] == ["rm"] * 5
    assert would[-1] == (
        "would run: gh workflow run ci.yml --ref release/v1.15.0 "
        "-f regenerate=true -f regenerate_allow_rerun=true"
    )
    assert any("git push origin release/v1.15.0" in w for w in would)
    # dry run: nothing written, only read-only commands executed
    assert ledger.read_text() == '{"entries": []}\n'
    assert all(c[1] in ("rev-parse", "status", "show") for c in fake.calls)
    assert ["git", "show", "origin/main:research/experiments.json"] in fake.calls


def test_prepare_regenerate_execute_resets_ledger(tmp_path):
    for i, (a, h) in enumerate(rel.RUNNER_EXPERIMENTS):
        _experiment(tmp_path, f"run{i}", a, h, 5)
    ledger = tmp_path / "research" / "experiments.json"
    ledger.write_text('{"entries": []}\n')
    r, fake = _runner(
        tmp_path,
        {
            ("git", "rev-parse"): "release/v1.15.0\n",
            ("git", "status"): "",
            ("git", "show"): BASELINE,
        },
        execute=True,
    )
    rel.prepare_regenerate(r, "release/v1.15.0")
    assert ledger.read_text() == BASELINE
    assert ["git", "rm", "-r", "-q", "research/experiments/run0"] in fake.calls
    assert fake.calls[-1][:3] == ["gh", "workflow", "run"]


def test_prepare_regenerate_refuses_wrong_branch_or_dirty_tree(tmp_path):
    r, _ = _runner(tmp_path, {("git", "rev-parse"): "main\n"}, execute=True)
    with pytest.raises(rel.ReleaseError, match="expected the release branch"):
        rel.prepare_regenerate(r, "release/v1.15.0")
    r, _ = _runner(tmp_path, {("git", "rev-parse"): "b\n", ("git", "status"): " M x\n"})
    with pytest.raises(rel.ReleaseError, match="not clean"):
        rel.prepare_regenerate(r, "b")


def _bundle(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_apply_bundle_skips_crlf_only_feature_goldens(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests" / "golden").mkdir(parents=True)
    (repo / "tests" / "golden" / "expected_features.json").write_bytes(b'{\r\n"a": 1\r\n}\r\n')
    (repo / "tests" / "golden" / "expected_features_anomalies.json").write_bytes(b'{"a": 1}\n')
    dl = tmp_path / "dl"
    dl.mkdir()
    _bundle(
        dl / rel.BUNDLE_FILE,
        {
            "tests/golden/expected_features.json": b'{\n"a": 1\n}\n',  # CRLF only
            "tests/golden/expected_features_anomalies.json": b'{"a": 2}\n',  # real change
            "research/experiments.json": b"{}\n",
        },
    )
    r, fake = _runner(repo, {})
    applied = rel.apply_bundle(r, "123", dl)
    assert applied == [
        "tests/golden/expected_features_anomalies.json",
        "research/experiments.json",
    ]
    assert any("skip CRLF-only change: tests/golden/expected_features.json" in x for x in r.log)
    tar_line = next(x for x in r.log if x.startswith("would run: tar"))
    assert "--force-local -xzf" in tar_line
    assert fake.calls == []  # bundle already present: no download; dry run: no tar


def test_apply_bundle_rejects_unsafe_paths(tmp_path):
    dl = tmp_path / "dl"
    dl.mkdir()
    _bundle(dl / rel.BUNDLE_FILE, {"../evil": b"x"})
    r, _ = _runner(tmp_path, {})
    with pytest.raises(rel.ReleaseError, match="unsafe"):
        rel.apply_bundle(r, "1", dl)


CHANGELOG = """# Changelog

## Unreleased

- next

## v1.15.0 — 2026-10-20

Release summary.

- item

## v1.14.0 — 2026-10-11

old
"""


def test_changelog_section():
    assert rel.changelog_section(CHANGELOG, "1.15.0") == "Release summary.\n\n- item\n"
    with pytest.raises(rel.ReleaseError):
        rel.changelog_section(CHANGELOG, "9.9.9")


def _publish_answers(ci_conclusion="success", release_runs=None, jobs=None, codeql="success"):
    release_runs = release_runs or [
        [{"databaseId": 7, "status": "completed", "conclusion": "success"}]
    ]
    seq = iter(release_runs)
    return {
        ("git", "rev-parse"): "abc123\n",
        ("git", "tag", "--list"): "",
        ("git", "show"): CHANGELOG,
        ("gh", "run", "list", "--workflow", "ci.yml"): json.dumps(
            [{"databaseId": 1, "status": "completed", "conclusion": ci_conclusion, "event": "push"}]
        ),
        ("gh", "run", "list", "--workflow", "codeql.yml"): json.dumps(
            [{"databaseId": 2, "status": "completed", "conclusion": codeql, "event": "push"}]
        ),
        ("gh", "run", "list", "--workflow", "release.yml"): lambda argv: json.dumps(next(seq)),
        ("gh", "run", "view"): json.dumps({"jobs": jobs or []}),
    }


def test_publish_dry_run(tmp_path):
    r, fake = _runner(tmp_path, _publish_answers())
    rel.publish(r, "1.15.0")
    would = [x for x in r.log if x.startswith("would run")]
    assert would[0].startswith("would run: git tag -a v1.15.0 abc123 -F ")
    assert would[1] == "would run: git push origin v1.15.0"
    assert would[2].startswith("would run: gh release create v1.15.0 --verify-tag")
    assert not any(c[:2] == ["git", "tag"] and "-a" in c for c in fake.calls)


def test_publish_refuses_before_main_ci_green(tmp_path):
    r, _ = _runner(tmp_path, _publish_answers(ci_conclusion="failure"))
    with pytest.raises(rel.ReleaseError, match="main CI is green"):
        rel.publish(r, "1.15.0")


def test_publish_reruns_release_once_when_verify_ci_raced(tmp_path):
    failed = [{"databaseId": 7, "status": "completed", "conclusion": "failure"}]
    ok = [{"databaseId": 7, "status": "completed", "conclusion": "success"}]
    answers = _publish_answers(
        release_runs=[failed, ok],
        jobs=[{"name": "verify-ci", "conclusion": "failure"}],
    )
    r, fake = _runner(tmp_path, answers, execute=True)
    pages = []
    rel.publish(
        r, "1.15.0", fetch=lambda url: pages.append(url) or "<h2>v1.15.0</h2>", sleep=lambda s: None
    )
    assert ["gh", "run", "rerun", "7", "--failed"] in fake.calls
    assert pages == [rel.SITE_URL]
    assert any(c[:3] == ["gh", "release", "create"] and "--verify-tag" in c for c in fake.calls)


def test_publish_does_not_rerun_other_failures(tmp_path):
    failed = [{"databaseId": 7, "status": "completed", "conclusion": "failure"}]
    answers = _publish_answers(
        release_runs=[failed], jobs=[{"name": "build", "conclusion": "failure"}]
    )
    r, _ = _runner(tmp_path, answers, execute=True)
    with pytest.raises(rel.ReleaseError, match="failure"):
        rel.publish(r, "1.15.0", fetch=lambda url: "", sleep=lambda s: None)


def test_cli_flow_and_default_dry_run(capsys):
    assert rel.main(["flow"]) == 0
    out = capsys.readouterr().out
    assert "regenerate_allow_rerun=true" in out and "--force-local" in out
    r = rel.Runner(exec_fn=FakeExec(_publish_answers()))
    assert rel.main(["publish", "--version", "1.15.0"], runner=r) == 0
    assert r.execute is False


def test_site_check_needs_full_version():
    # the site's roadmap names the next minor before it ships: "v1.15" is vacuous
    assert not rel.site_mentions("1.15.0", fetch=lambda u: "roadmap: v1.15 next; v1.14.0")
    assert not rel.site_mentions("1.15.0", fetch=lambda u: "v1.15.01 v1.15.0.1")
    assert rel.site_mentions("1.15.0", fetch=lambda u: "<h2>v1.15.0</h2>")
    assert rel.site_mentions("1.15.0", fetch=lambda u: "released v1.15.0.")


def test_publish_dry_run_reports_all_blockers_and_prints_plan(tmp_path):
    answers = _publish_answers(ci_conclusion="failure")
    answers[("git", "show")] = "# Changelog\n\n## Unreleased (v1.15)\n\n- x\n"
    r, fake = _runner(tmp_path, answers)
    with pytest.raises(rel.ReleaseError, match="CHANGELOG.md has no.*main CI is green"):
        rel.publish(r, "1.15.0")
    assert sum(x.startswith("BLOCKER") for x in r.log) == 3  # missing, not renamed, CI
    assert any(x.startswith("would run: gh release create v1.15.0") for x in r.log)
    assert not any(c[:2] == ["git", "tag"] and "-a" in c for c in fake.calls)


def test_publish_execute_stops_at_first_blocker(tmp_path):
    answers = _publish_answers()
    answers[("git", "tag", "--list")] = "v1.15.0\n"
    r, fake = _runner(tmp_path, answers, execute=True)
    with pytest.raises(rel.ReleaseError, match="already exists"):
        rel.publish(r, "1.15.0", fetch=lambda u: "", sleep=lambda s: None)
    assert not any(c[:2] == ["gh", "release"] for c in fake.calls)


@pytest.mark.parametrize("codeql", ["failure", "in_progress", None])
def test_publish_requires_green_codeql(tmp_path, codeql):
    answers = _publish_answers(codeql=codeql or "success")
    if codeql == "in_progress":
        answers[("gh", "run", "list", "--workflow", "codeql.yml")] = json.dumps(
            [{"databaseId": 2, "status": "in_progress", "conclusion": "", "event": "push"}]
        )
    if codeql is None:
        answers[("gh", "run", "list", "--workflow", "codeql.yml")] = "[]"
    # dry run: listed as a blocker, plan still printed
    r, _ = _runner(tmp_path, answers)
    with pytest.raises(rel.ReleaseError, match="codeql"):
        rel.publish(r, "1.15.0")
    assert any(x.startswith("BLOCKER: no green codeql") for x in r.log)
    assert any(x.startswith("would run: gh release create") for x in r.log)
    # execute: stops before tagging
    r, fake = _runner(tmp_path, answers, execute=True)
    with pytest.raises(rel.ReleaseError, match="codeql"):
        rel.publish(r, "1.15.0", fetch=lambda u: "", sleep=lambda s: None)
    assert not any(c[:3] == ["git", "tag", "-a"] or c[:2] == ["gh", "release"] for c in fake.calls)
    assert ["gh", "run", "list", "--workflow", "codeql.yml"] == fake.calls[-1][:5]


def test_stale_unreleased_heading():
    assert rel.stale_unreleased(CHANGELOG, "1.15.0") is None  # bare Unreleased = next cycle
    text = "# C\n\n## Unreleased (v1.15)\n\n- x\n\n## v1.15.0 — d\n\n- y\n"
    assert rel.stale_unreleased(text, "1.15.0") == "## Unreleased (v1.15)"
    assert rel.stale_unreleased(text, "1.16.0") is None
    assert rel.stale_unreleased(text.replace("v1.15)", "v1.150)"), "1.15.0") is None


def test_publish_execute_fails_on_unrenamed_unreleased(tmp_path):
    answers = _publish_answers()
    answers[("git", "show")] = "# Changelog\n\n## Unreleased (v1.15)\n\n- x\n"
    r, fake = _runner(tmp_path, answers, execute=True)
    with pytest.raises(rel.ReleaseError, match="no '## v1.15.0' section"):
        rel.publish(r, "1.15.0", fetch=lambda u: "", sleep=lambda s: None)
    answers[("git", "show")] = CHANGELOG.replace("## Unreleased", "## Unreleased (v1.15.0)")
    r, fake = _runner(tmp_path, answers, execute=True)
    with pytest.raises(rel.ReleaseError, match="not renamed"):
        rel.publish(r, "1.15.0", fetch=lambda u: "", sleep=lambda s: None)
    assert not any(c[:2] == ["git", "tag"] and "-a" in c for c in fake.calls)
    assert not any(c[:2] == ["gh", "release"] for c in fake.calls)


def test_publish_execute_fails_on_empty_section(tmp_path):
    answers = _publish_answers()
    answers[("git", "show")] = "# Changelog\n\n## v1.15.0 — d\n\n## v1.14.0\n\nold\n"
    r, fake = _runner(tmp_path, answers, execute=True)
    with pytest.raises(rel.ReleaseError, match="missing or empty"):
        rel.publish(r, "1.15.0", fetch=lambda u: "", sleep=lambda s: None)
    assert not any(c[:2] == ["gh", "release"] for c in fake.calls)
