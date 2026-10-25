"""v2.0 docs rewrite: tests/harness/check_docs.py ``internal_links`` resolves ``#fragment`` anchors."""

from __future__ import annotations

import sys

from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "tests" / "harness"))
try:
    import check_docs
finally:
    sys.path.pop(0)


def test_slug_matches_github_rules():
    assert check_docs._slug("Quick start") == "quick-start"
    assert (
        check_docs._slug("3. The confirmatory gate, in plain words")
        == "3-the-confirmatory-gate-in-plain-words"
    )
    assert check_docs._slug("`iap.core` and friends (v2.0)") == "iapcore-and-friends-v20"
    assert check_docs._slug("[LEARN](LEARN.md) section") == "learn-section"


def test_anchors_number_duplicates_and_skip_fenced_code(tmp_path):
    page = tmp_path / "p.md"
    page.write_text(
        "# Title\n\n## Status\n\n```\n## not a heading\n```\n\n## Status\n\n<a name='explicit'></a>\n",
        encoding="utf-8",
    )
    assert check_docs._anchors(page) == {"title", "status", "status-1", "explicit"}


def test_link_check_fails_on_a_missing_anchor(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\n## Real heading\n", encoding="utf-8")
    (docs / "b.md").write_text(
        "[ok](a.md#real-heading) [self](#here) [bad](a.md#no-such-heading)\n\n## Here\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_docs, "ROOT", tmp_path)
    monkeypatch.setattr(check_docs, "tracked_docs", lambda: [docs / "a.md", docs / "b.md"])
    f = check_docs.Findings()
    check_docs.check_internal_links(f)
    failed = f.failed("internal_links")
    assert len(failed) == 1
    assert failed[0]["where"] == "docs/b.md:1"
    assert "#no-such-heading" in failed[0]["detail"]
