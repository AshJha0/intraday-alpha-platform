"""v1.18 REL-12: the data manifest (tools/data_manifest.py) and the
pre-ingest sha256 verification of the download tools (tools/r7_download.py,
used by tools/iex_download.py)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re

from conftest import REPO_ROOT


def _tool(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dm = _tool("data_manifest")
V1, V2 = "a" * 64, "b" * 64


def _dataset(root, rel, version, date="2025-11-28", keep=False, tops=None, book_check=None):
    d = root / rel
    d.mkdir(parents=True)
    (d / "dataset.json").write_text(
        json.dumps(
            {
                "dataset_version": version,
                "kind": "real",
                "source": {"venue": "XNAS", "format": "itch50"},
                "normalized": {"files": {"eq.normalized.iap1": "c" * 64}},
                "sessions": {
                    date: {
                        "format": "itch50",
                        "symbols": ["AAPL"],
                        "inputs": [{"file": "f.gz", "sha256": "d" * 64, "bytes": 5}],
                        "raw": {"sha256": "e" * 64},
                        **({"book_check": book_check} if book_check is not None else {}),
                    }
                },
            }
        )
    )
    if keep:
        (d / "KEEP.json").write_text('{"schema": "iap.process_delete.keep/v1"}')
    if tops:
        (d / "process_report.json").write_text(
            json.dumps({"steps": [{"step": "download_tops", "sha256": tops, "bytes": 9}]})
        )
    return d


def test_generate_lists_every_dataset_json_without_reading_data(tmp_path):
    root = tmp_path / "real"
    _dataset(root, "ds3_20190130", V1, date="2019-01-30")
    _dataset(root, "iex/iex_20251128", V2, keep=True, tops="f" * 64)
    # a large file next to dataset.json is never opened: make it unreadable as JSON
    (root / "ds3_20190130" / "raw.jsonl").write_bytes(b"\xff" * 64)
    m = dm.generate(root)
    by = {d["path"]: d for d in m["datasets"]}
    assert set(by) == {"ds3_20190130", "iex/iex_20251128"}
    a, b = by["ds3_20190130"], by["iex/iex_20251128"]
    assert a["dataset_version"] == V1 and a["keep_json_sha256"] is None
    assert a["sessions"]["2019-01-30"]["inputs"] == [
        {"file": "f.gz", "sha256": "d" * 64, "bytes": 5}
    ]
    assert a["sessions"]["2019-01-30"]["status"]["spend"] == "in-sample, spent"
    assert (
        b["keep_json_sha256"]
        == hashlib.sha256((root / b["path"] / "KEEP.json").read_bytes()).hexdigest()
    )
    assert b["oracle_inputs"] == [{"feed": "TOPS", "sha256": "f" * 64, "bytes": 9}]
    assert b["sessions"]["2025-11-28"]["status"]["spend"] == "holdout read once, then reused"
    assert dm.listed_versions(m) >= {V1, V2, *dm.NON_DISK}


def test_path_status_overrides_the_date_status(tmp_path):
    root = tmp_path / "real"
    _dataset(root, "breadth/ds_20190130", V1, date="2019-01-30")
    (entry,) = dm.generate(root)["datasets"]
    assert entry["status"]["spend"] == "features built, never read"
    assert entry["sessions"]["2019-01-30"]["status"] == entry["status"]


def test_cited_versions_walks_dataset_keys_only():
    doc = {
        "dataset_version": V1,
        "nested": [{"nasdaq_dataset_version": V2}],
        "dataset_version_notes": {"9" * 64: "a note"},
        "code_sha256": "8" * 64,  # not a dataset key: ignored
        "dataset_version_text": "recorded at ingest",
    }
    assert dm.cited_versions(doc) == {V1, V2, "9" * 64}


def test_check_flags_an_unlisted_citation_and_a_stale_manifest(tmp_path):
    repo = tmp_path / "repo"
    (repo / "research" / "preregs").mkdir(parents=True)
    (repo / "research" / "preregs" / "x_prereg.json").write_text(
        json.dumps({"dataset_version": V2})
    )
    root = tmp_path / "real"
    _dataset(root, "ds", V1)
    man = repo / "research" / "data_manifest.json"
    man.write_text(json.dumps(dm.generate(root)))
    problems = dm.check(man, repo)
    assert problems == [f"research/preregs/x_prereg.json cites unlisted dataset {V2}"]
    _dataset(root, "ds2", V2, book_check={"clean": True})  # D3: a cited dataset needs a verdict
    assert dm.check(man, repo, data_root=root)[-1].startswith("data_manifest.json is stale")
    man.write_text(json.dumps(dm.generate(root)))
    assert dm.check(man, repo, data_root=root) == []


def test_committed_manifest_covers_every_committed_citation():
    """The CI gate (governance job) on the repository itself."""
    assert dm.check(dm.DEFAULT_OUT, REPO_ROOT) == []


def test_committed_manifest_reproduces_the_iex_overlap_hashes():
    """REL-12 gate: the manifest reproduces the vendor hashes in
    docs/research/IEX_OVERLAP_SET.md.  Known gap, pinned so it cannot grow:
    the TOPS captures of 2026-05-15 / 05-18 are recorded in no small file on
    disk (their days have no process_report.json)."""
    m = json.loads(dm.DEFAULT_OUT.read_text(encoding="utf-8"))
    shas = {i["sha256"] for d in m["datasets"] for s in d["sessions"].values() for i in s["inputs"]}
    shas |= {o["sha256"] for d in m["datasets"] for o in d.get("oracle_inputs", [])}
    doc = (REPO_ROOT / "docs" / "research" / "IEX_OVERLAP_SET.md").read_text(encoding="utf-8")
    missing = set(re.findall(r"\b[0-9a-f]{64}\b", doc)) - shas
    assert missing == {
        "a583bc3163efa28452d037f0eff081ffae3d0c8ec8777c5401694741f0ccfae9",
        "c478b36a1c1488cec829847638e67e110c24005f99ccb651b9dc4ed0f5bf8513",
    }


# ------------------------------------------------- download sha verification


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _day(data: bytes, sha: str | None):
    return {
        "date": "2025-12-08",
        "file": "f.gz",
        "url": "https://emi.nasdaq.com/ITCH/f.gz",
        "bytes": len(data),
        "sha256": sha,
    }


def test_download_verifies_the_manifest_sha_by_default(tmp_path):
    dl = _tool("r7_download")
    data = b"0123456789"
    good = hashlib.sha256(data).hexdigest()
    r = dl.download_one(_day(data, good), tmp_path, lambda u: _Resp(data))
    assert r["status"] == "downloaded" and r["sha256_verified"] and r["sha256"] == good
    # present + right sha: skipped after a verify; right size, wrong bytes: re-fetched
    assert dl.download_one(_day(data, good), tmp_path, lambda u: _Resp(data))["status"] == "present"
    (tmp_path / "f.gz").write_bytes(b"x" * 10)
    calls = []
    r = dl.download_one(_day(data, good), tmp_path, lambda u: calls.append(u) or _Resp(data))
    assert r["status"] == "downloaded" and len(calls) == 1
    # a mismatch fails and never replaces the file
    (tmp_path / "f.gz").unlink()
    r = dl.download_one(_day(data, "0" * 64), tmp_path, lambda u: _Resp(data))
    assert r["status"] == "failed" and r["reason"] == "sha256 mismatch"
    assert not (tmp_path / "f.gz").exists()


def test_download_without_a_manifest_sha_records_it_unverified(tmp_path):
    dl = _tool("r7_download")
    data = b"abc"
    r = dl.download_one(_day(data, None), tmp_path, lambda u: _Resp(data))
    assert r["status"] == "downloaded" and r["sha256_verified"] is False
    assert r["sha256"] == hashlib.sha256(data).hexdigest()


def test_no_verify_sha_is_the_old_size_only_check(tmp_path):
    dl = _tool("r7_download")
    data = b"0123456789"
    r = dl.download_one(_day(data, "0" * 64), tmp_path, lambda u: _Resp(data), verify_sha=False)
    assert r["status"] == "downloaded" and r["sha256_verified"] is False


def test_recorded_raw_input_hash_is_a_listed_citation_but_unknown_hash_is_not():
    """A study that reads a raw vendor file (REL-08c) cites the file's sha256; that is
    covered when the file is a recorded input of a listed dataset, and nothing else is."""
    dm = _tool("data_manifest")
    known = "a" * 64
    manifest = {
        "datasets": [
            {
                "dataset_version": "b" * 64,
                "sessions": {"2019-01-30": {"inputs": [{"sha256": known}]}},
            }
        ],
        "non_disk": [],
    }
    listed = dm.listed_versions(manifest) | dm.listed_inputs(manifest)
    assert known in listed
    assert "b" * 64 in listed
    assert "c" * 64 not in listed


# ------------------------------------------------------------------------
# v2.0 stage 2 D3 (defect D-05): integrity evidence and verdicts per dataset


def _evidence_dataset(root, rel, version, date, *, book_check=None, book_verification=None):
    d = _dataset(root, rel, version, date=date)
    doc = json.loads((d / "dataset.json").read_text())
    doc["ingest_rules_version"] = 2
    doc["normalized"]["qc_totals"] = {"events_in": 10, "events_out": 10, "invalid": 0}
    s = doc["sessions"][date]
    s["messages"] = {"total": 1000, "skipped_unknown": 0}
    s["per_symbol"] = {"AAPL": {"events": 6}, "MSFT": {"events": 4}}
    if book_check is not None:
        s["book_check"] = book_check
    if book_verification is not None:
        s["book_verification"] = book_verification
    (d / "dataset.json").write_text(json.dumps(doc))
    return d


CLEAN = {
    "clean": True,
    "crossing_adds": 0,
    "crossed_on_resume": 0,
    "dropped": 0,
    "per_symbol": {
        "AAPL": {"events_applied": 6, "crossed_at_end": False},
        "MSFT": {"events_applied": 4, "crossed_at_end": False},
    },
}
DIRTY = {**CLEAN, "clean": False, "crossing_adds": 2}
MATCH = {
    "AAPL": {"status": "match", "compared": 5, "mismatched": 0, "oracle": "tops"},
    "MSFT": {"status": "match", "compared": 3, "mismatched": 0, "oracle": "tops"},
}
MISMATCH = {**MATCH, "MSFT": {**MATCH["MSFT"], "status": "mismatch", "mismatched": 1}}


def test_evidence_copies_decode_counts_hash_and_verdicts_from_dataset_json(tmp_path):
    root = tmp_path / "real"
    _evidence_dataset(root, "itch", V1, "2019-01-30", book_check=CLEAN)
    _evidence_dataset(root, "iex/x", V2, "2025-11-28", book_check=CLEAN, book_verification=MATCH)
    by = {d["path"]: d for d in dm.generate(root)["datasets"]}
    ev = by["itch"]["evidence"]
    assert ev["decode"] == {
        "ingest_rules_version": 2,
        "qc_totals": {"events_in": 10, "events_out": 10, "invalid": 0},
        "sessions": 1,
        "messages_total": 1000,
        "events": 10,
    }
    assert ev["hash"]["dataset_version"] == V1 and ev["hash"]["normalized_files"] == 1
    sess = ev["sessions"]["2019-01-30"]
    assert sess["book_check"]["events_applied"] == 10 and sess["book_verification"] is None
    assert sess["verdict"] == "clean" and ev["verdict"] == "clean"  # ITCH: no vendor oracle
    iex = by["iex/x"]["evidence"]
    bv = iex["sessions"]["2025-11-28"]["book_verification"]
    assert bv["status"] == "match" and bv["compared"] == 8 and bv["mismatched"] == 0
    assert iex["verdict"] == "clean, match"
    assert dm.generate(root)["schema"] == "iap.data_manifest/v2"


def test_verdict_rules_and_the_weakest_session_wins():
    sv = dm.session_verdict
    assert sv(None, None) == "none"
    assert sv({"clean": None}, None) == "none"
    assert sv({"clean": True}, None) == "clean"
    assert sv({"clean": True}, {"status": "match"}) == "clean, match"
    assert sv({"clean": False}, {"status": "match"}) == "not clean"
    assert sv({"clean": True}, {"status": "mismatch"}) == "mismatch"
    assert dm.dataset_verdict(["clean, match", "clean", "not clean"]) == "not clean"
    assert dm.dataset_verdict(["clean, match", "mismatch"]) == "mismatch"
    assert dm.dataset_verdict([]) == "none"


def _repo_citing(tmp_path, *versions):
    repo = tmp_path / "repo"
    (repo / "research" / "preregs").mkdir(parents=True)
    (repo / "research" / "preregs" / "x_prereg.json").write_text(
        json.dumps({"dataset_versions": list(versions)})
    )
    return repo


def test_check_refuses_a_cited_dataset_without_evidence_or_with_a_dirty_book(tmp_path):
    """The D3 acceptance: a result citing a dataset with no committed clean/match
    verdict fails the harness; so does a synthetic 'dirty' fixture."""
    root = tmp_path / "real"
    _evidence_dataset(root, "no_evidence", V1, "2019-01-30")  # ingested with --no-book-check
    _evidence_dataset(root, "dirty", V2, "2025-11-28", book_check=DIRTY, book_verification=MATCH)
    bad = "9" * 64
    _evidence_dataset(
        root, "mismatch", bad, "2025-12-08", book_check=CLEAN, book_verification=MISMATCH
    )
    repo = _repo_citing(tmp_path, V1, V2, bad)
    man = repo / "research" / "data_manifest.json"
    man.write_text(json.dumps(dm.generate(root)))
    problems = dm.check(man, repo)
    assert len(problems) == 3
    assert any("no_evidence" in p and "verdict 'none'" in p for p in problems)
    assert any("dirty" in p and "'not clean'" in p and "not acknowledged" in p for p in problems)
    assert any("mismatch" in p and "verdict 'mismatch'" in p for p in problems)
    # an uncited dirty dataset is listed, recorded as such, and is not a failure
    repo2 = _repo_citing(tmp_path / "two")
    man2 = repo2 / "research" / "data_manifest.json"
    man2.write_text(json.dumps(dm.generate(root)))
    assert dm.check(man2, repo2) == []


def test_not_clean_acknowledgement_is_bound_to_its_counters(tmp_path, monkeypatch):
    root = tmp_path / "real"
    _evidence_dataset(root, "dirty", V2, "2025-11-28", book_check=DIRTY, book_verification=MATCH)
    repo = _repo_citing(tmp_path, V2)
    man = repo / "research" / "data_manifest.json"
    man.write_text(json.dumps(dm.generate(root)))
    monkeypatch.setattr(
        dm,
        "NOT_CLEAN_ACKNOWLEDGED",
        {V2: {"crossing_adds": 2, "crossed_on_resume": 0, "dropped": 0, "acknowledged": "test"}},
    )
    assert dm.check(man, repo) == []
    monkeypatch.setattr(
        dm,
        "NOT_CLEAN_ACKNOWLEDGED",
        {V2: {"crossing_adds": 1, "crossed_on_resume": 0, "dropped": 0, "acknowledged": "test"}},
    )
    (problem,) = dm.check(man, repo)
    assert "acknowledged for" in problem and "'crossing_adds': 2" in problem


def test_committed_manifest_every_cited_dataset_has_a_verdict_and_the_not_clean_are_named():
    """On the repository: the four acknowledged not-clean versions are the two IEX
    days 2025-12-10 and 2026-05-18 and their capture-timestamp twins; every other
    on-disk dataset is clean (ITCH, no oracle) or clean + TOPS match (IEX)."""
    man = json.loads(dm.DEFAULT_OUT.read_text(encoding="utf-8"))
    assert man["schema"] == "iap.data_manifest/v2"
    by_verdict: dict[str, list[str]] = {}
    for d in man["datasets"]:
        by_verdict.setdefault(d["evidence"]["verdict"], []).append(d["path"])
    assert set(by_verdict) == {"clean", "clean, match", "not clean"}
    assert sorted(by_verdict["not clean"]) == [
        "iex/iex_20251210",
        "iex/iex_20260518",
        "iex_capts/iex_20251210",
        "iex_capts/iex_20260518",
    ]
    assert all(p.startswith("iex") for p in by_verdict["clean, match"])
    versions = {d["path"]: d["dataset_version"] for d in man["datasets"]}
    assert set(dm.NOT_CLEAN_ACKNOWLEDGED) == {versions[p] for p in by_verdict["not clean"]}
    for d in man["non_disk"]:
        assert d["evidence"]["verdict"] not in ("none", "")
    assert dm.evidence_problems(man, set(versions.values()) | set(dm.NON_DISK)) == []
