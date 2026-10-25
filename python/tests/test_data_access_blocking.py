"""Review finding 2: restricted reads are BLOCKED before any data byte is read,
not merely logged; refusals are logged; the access log is append-only and
hash-chained; the low-level readers refuse on their own.

Synthetic datasets in tmp_path, decided against the committed registry
(2025-12-08 AAPL is spent, 2019-01-30 is in-sample); nothing under data/real
is read.
"""

from __future__ import annotations

import builtins
import hashlib
import json
from pathlib import Path

import pytest
from iap.research import holdout_registry as hr

SPENT_DAY = "2025-12-08"
IN_DAY = "2019-01-30"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv(hr.LOG_ENV, str(tmp_path / "access.jsonl"))
    monkeypatch.setattr(hr, "_AUTHORISED_ROOTS", set())  # D1: in-process, no env override
    return tmp_path / "access.jsonl"


def make(root: Path, name: str, day: str, *, tamper_manifest_version: bool = False) -> Path:
    d = root / name
    (d / "normalized").mkdir(parents=True)
    (d / "features").mkdir()
    body = f"events {name} {day}".encode()
    fname = f"eq_{day.replace('-', '')}.normalized.iap1"
    (d / "normalized" / fname).write_bytes(body)
    (d / "features" / "features_1.parquet").write_bytes(b"not read")
    sha = hashlib.sha256(body).hexdigest()
    man = {
        "kind": "real",
        "dataset_version": "fake" if tamper_manifest_version else hr.content_hash({fname: sha}),
        "normalized": {"files": {fname: sha}},
        "sessions": {day: {}},
        "universe": [{"symbol": "AAPL"}],
    }
    (d / "dataset.json").write_text(json.dumps(man), encoding="utf-8")
    return d


class Opened:
    """Records every data file opened through builtins.open / Path.open."""

    def __init__(self, monkeypatch, root: Path):
        self.paths: list[str] = []
        real_open, real_path_open = builtins.open, Path.open
        root_s = str(root)

        def spy(file, *a, **k):
            if str(file).startswith(root_s) and not str(file).endswith("dataset.json"):
                self.paths.append(str(file))
            return real_open(file, *a, **k)

        def pspy(self_p, *a, **k):
            if str(self_p).startswith(root_s) and self_p.name != "dataset.json":
                self.paths.append(str(self_p))
            return real_path_open(self_p, *a, **k)

        monkeypatch.setattr(builtins, "open", spy)
        monkeypatch.setattr(Path, "open", pspy)


def test_restricted_dataset_is_refused_before_any_data_file_is_opened(tmp_path, monkeypatch):
    d = make(tmp_path / "data", "renamed_fresh", SPENT_DAY)
    spy = Opened(monkeypatch, tmp_path / "data")
    with pytest.raises(hr.HoldoutRefused, match="SPENT"):
        hr.guard(d, purpose="test")
    assert spy.paths == []  # only dataset.json was read: no event or feature byte


def test_in_sample_read_rehashes_and_a_tampered_tree_is_refused(tmp_path):
    d = make(tmp_path, "ok", IN_DAY)
    rec = hr.guard(d, purpose="test")
    assert rec["decision"] == "allowed" and rec["content_verified"] is True
    f = next((d / "normalized").iterdir())
    f.write_bytes(b"holdout bytes under an in-sample manifest")
    with pytest.raises(hr.HoldoutRefused, match="content differs"):
        hr.guard(d, purpose="test")


def test_refusals_are_logged_and_the_log_is_a_hash_chain(tmp_path, _env):
    d_bad = make(tmp_path, "bad", SPENT_DAY)
    d_ok = make(tmp_path, "ok", IN_DAY)
    hr.guard(d_ok, purpose="a")
    with pytest.raises(hr.HoldoutRefused):
        hr.guard(d_bad, purpose="b")
    hr.guard(d_ok, purpose="c")
    lines = _env.read_bytes().splitlines()
    recs = [json.loads(x) for x in lines]
    assert [r["decision"] for r in recs] == ["allowed", "refused", "allowed"]
    assert "SPENT" in recs[1]["reason"]
    assert recs[0]["prev_sha256"] == hr.GENESIS
    assert recs[1]["prev_sha256"] == hashlib.sha256(lines[0]).hexdigest()
    assert hr.verify_access_log(_env) == []
    assert hr.main(["verify-log", "--log", str(_env)]) == 0


def test_appends_never_rewrite_earlier_lines(tmp_path, _env):
    d = make(tmp_path, "ok", IN_DAY)
    hr.guard(d, purpose="a")
    before = _env.read_bytes()
    hr.guard(d, purpose="b")
    assert _env.read_bytes().startswith(before)


@pytest.mark.parametrize("attack", ["edit", "delete", "reorder"])
def test_tampered_log_is_detected(tmp_path, _env, attack):
    d = make(tmp_path, "bad", SPENT_DAY)
    for _ in range(3):
        with pytest.raises(hr.HoldoutRefused):
            hr.guard(d, purpose="probe")
    lines = _env.read_bytes().splitlines()
    if attack == "edit":
        lines[1] = lines[1].replace(b'"refused"', b'"allowed"')
    elif attack == "delete":
        del lines[1]
    else:
        lines[1], lines[2] = lines[2], lines[1]
    _env.write_bytes(b"\n".join(lines) + b"\n")
    assert hr.verify_access_log(_env)
    assert hr.main(["verify-log", "--log", str(_env)]) == 1


def test_legacy_unchained_prefix_is_accepted_but_not_after_the_chain(tmp_path, _env):
    _env.write_text('{"decision": "allowed"}\n', encoding="utf-8")
    hr.guard(make(tmp_path, "ok", IN_DAY), purpose="a")
    assert hr.verify_access_log(_env) == []
    with _env.open("a", encoding="utf-8") as f:
        f.write('{"decision": "allowed"}\n')
    assert any("unchained" in m for m in hr.verify_access_log(_env))


# ------------------------------------------------------ reader backstop --
def test_iap1_readers_refuse_a_restricted_dataset_before_opening(tmp_path, monkeypatch):
    from iap.core import codec

    d = make(tmp_path / "data", "fresh", SPENT_DAY)
    f = next((d / "normalized").iterdir())
    spy = Opened(monkeypatch, tmp_path / "data")
    with pytest.raises(hr.HoldoutRefused):
        codec.read_iap1(f)
    with pytest.raises(hr.HoldoutRefused):
        next(codec.iter_iap1(f))
    assert spy.paths == []


def test_feature_loaders_refuse_a_restricted_dataset_before_reading(tmp_path, monkeypatch):
    import pandas as pd
    from iap.alpha.data import load_features

    d = make(tmp_path, "fresh", SPENT_DAY)
    monkeypatch.setattr(pd, "read_parquet", lambda *a, **k: pytest.fail("parquet was read"))
    with pytest.raises(hr.HoldoutRefused):
        load_features(d / "features")


def test_reader_accepts_what_the_entry_point_guard_authorised(tmp_path):
    d = make(tmp_path, "ok", IN_DAY)
    hr.guard(d, purpose="entry point")
    assert str(hr.dataset_root(d)) in hr._authorised()
    hr.reader_guard(next((d / "normalized").iterdir()))  # no second decision needed


def test_reader_passes_synthetic_and_files_outside_datasets(tmp_path):
    d = make(tmp_path, "syn", SPENT_DAY)
    man = json.loads((d / "dataset.json").read_text(encoding="utf-8"))
    (d / "dataset.json").write_text(json.dumps({**man, "kind": "synthetic"}), encoding="utf-8")
    hr.reader_guard(next((d / "normalized").iterdir()))
    loose = tmp_path / "loose.iap1"
    loose.write_bytes(b"x")
    hr.reader_guard(loose)


def test_manifestless_file_under_data_real_is_checked_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(hr, "_REAL_ROOT", tmp_path)
    p = tmp_path / "copies" / "eq_20251208.normalized.iap1"
    p.parent.mkdir()
    p.write_bytes(b"x")
    with pytest.raises(hr.HoldoutRefused, match="2025-12-08"):
        hr.reader_guard(p)


def test_screen_allow_holdout_no_longer_skips_the_registry():
    import inspect

    from iap.research import screen

    src = inspect.getsource(screen._main)
    i = src.index("if args.allow_holdout:")
    branch = src[i : src.index("else:", i)]
    assert "assert_not_holdout(" in branch  # the registry guard runs on both branches
