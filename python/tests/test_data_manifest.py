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


def _dataset(root, rel, version, date="2025-11-28", keep=False, tops=None):
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
    _dataset(root, "ds2", V2)
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
