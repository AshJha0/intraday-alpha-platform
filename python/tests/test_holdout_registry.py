"""Hash-locked holdout registry (v1.18 REL-02b): content identity, fail-closed, bypasses.

Synthetic datasets in tmp_path only; the committed ``research/holdout_registry.json`` is
read, never written.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from iap.agents import signing
from iap.agents.blackboard import canonical
from iap.research import holdout_registry as hr

REPO = Path(__file__).resolve().parents[2]
HOLDOUT2026 = "d7f7ffd14fbc363550a5bc9afd886e36df0d851511f840615f1f089b22f5f237"
MULTI7 = "ee4c218738683e5bc4249aa4a9e7302f32261c5855e97b3d337862b334333fb4"


@pytest.fixture(autouse=True)
def _log(tmp_path, monkeypatch):
    monkeypatch.setenv(hr.LOG_ENV, str(tmp_path / "access.jsonl"))
    return tmp_path / "access.jsonl"


def make_dataset(
    root: Path,
    name: str,
    sessions: list[str],
    symbols=("AAPL", "MSFT", "QQQ"),
    version: str | None = None,
    payload: bytes | None = None,
    kind: str = "real",
) -> Path:
    d = root / name
    (d / "normalized").mkdir(parents=True)
    body = payload if payload is not None else f"events of {name} {sessions}".encode()
    fname = f"eq_{sessions[0].replace('-', '')}.normalized.iap1"
    (d / "normalized" / fname).write_bytes(body)
    man = {
        "kind": kind,
        "dataset_version": version or hashlib.sha256(body).hexdigest(),
        "normalized": {"files": {fname: hashlib.sha256(body).hexdigest()}},
        "sessions": {s: {} for s in sessions},
        "universe": [{"symbol": s} for s in symbols],
    }
    (d / "dataset.json").write_text(json.dumps(man), encoding="utf-8")
    return d


def registry_for(*dirs: Path, states: dict[str, dict]) -> dict:
    """A sealed registry whose datasets are the given dirs, ``states`` maps
    session -> rule list."""
    datasets = {}
    for d in dirs:
        ident = hr.dataset_identity(d)
        datasets[ident["dataset_version"]] = {
            "label": d.name,
            "content_hash": ident["content_hash"],
            "sessions": ident["sessions"],
            "symbols": ident["symbols"],
        }
    return hr.seal({"schema": hr.SCHEMA, "datasets": datasets, "sessions": states})


IN = [{"symbols": "*", "state": "in_sample"}]
SPENT = [{"symbols": "*", "state": "spent", "spent_by": ["P1"]}]
UNREAD = [{"symbols": "*", "state": "unread", "reserved_for": ["research/preregs/p.json"]}]


# ------------------------------------------------------------- the file --
def test_committed_registry_is_sealed_and_seeded_from_the_inventory():
    reg = hr.load_registry(root=REPO)
    assert reg["body_sha256"] == hr.body_sha256(reg)
    by_label = {e["label"]: e for e in reg["datasets"].values()}
    assert by_label["holdout2026"]["state"] == "spent"
    assert by_label["r7_holdout"]["state"] == "spent"
    assert by_label["multi7"]["state"] == "in_sample"
    assert by_label["r7x20_3day"]["state"] == "in_sample"  # REL-04c 3-day dataset (2019/2021 days)
    assert by_label["ds_20190130"]["state"] == "in_sample"  # the 100-name breadth day
    # the 2025/26 days: spent for the three registered names, UNREAD for every other name
    rules = reg["sessions"]["2025-12-08"]
    assert rules[0]["state"] == "spent" and rules[0]["symbols"] == ["AAPL", "MSFT", "QQQ"]
    assert rules[1]["state"] == "unread" and rules[1]["symbols"] == "*"
    assert "research/preregs/breadth_book_prereg.json" in rules[1]["reserved_for"]


def test_edited_registry_without_reseal_is_refused(tmp_path):
    p = tmp_path / "reg.json"
    doc = json.loads((REPO / hr.REGISTRY_REL).read_text(encoding="utf-8"))
    doc["datasets"][HOLDOUT2026]["state"] = "in_sample"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(hr.HoldoutRefused, match="body_sha256"):
        hr.load_registry(p)
    p.write_text(hr.render(hr.seal(doc)), encoding="utf-8")  # resealed: loads (review's job)
    assert hr.load_registry(p)["datasets"][HOLDOUT2026]["state"] == "in_sample"


def test_missing_registry_fails_closed(tmp_path):
    with pytest.raises(hr.HoldoutRefused, match="missing"):
        hr.load_registry(tmp_path / "nope.json")


# ------------------------------------------------------------ decisions --
def test_in_sample_passes_and_is_logged(tmp_path, _log):
    d = make_dataset(tmp_path, "a", ["2019-01-30"])
    reg = registry_for(d, states={"2019-01-30": IN})
    rec = hr.guard(d, purpose="t", registry=reg)
    assert rec["decision"] == "allowed" and rec["state"] == "in_sample"
    line = json.loads(_log.read_text().splitlines()[-1])
    assert line["decision"] == "allowed" and line["purpose"] == "t"


def test_spent_and_unread_refused_without_prereg_and_logged(tmp_path, _log):
    s = make_dataset(tmp_path, "s", ["2026-05-15"])
    u = make_dataset(tmp_path, "u", ["2025-12-08"])
    reg = registry_for(s, u, states={"2026-05-15": SPENT, "2025-12-08": UNREAD})
    for d, word in ((s, "SPENT"), (u, "UNREAD")):
        with pytest.raises(hr.HoldoutRefused, match=word):
            hr.guard(d, purpose="t", registry=reg)
    refused = [json.loads(x) for x in _log.read_text().splitlines()]
    assert [r["decision"] for r in refused] == ["refused", "refused"]


def test_unregistered_real_dataset_fails_closed_synthetic_passes(tmp_path):
    d = make_dataset(tmp_path, "new", ["2024-03-04"])
    reg = registry_for(states={"2019-01-30": IN})
    with pytest.raises(hr.HoldoutRefused, match="not in research/holdout_registry.json"):
        hr.guard(d, purpose="t", registry=reg)
    syn = make_dataset(tmp_path, "syn", ["2024-03-04"], kind="synthetic")
    assert hr.guard(syn, purpose="t", registry=reg)["state"] == "synthetic"
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(hr.HoldoutRefused, match="no dataset.json"):
        hr.guard(bare, purpose="t", registry=reg)


def _signed_prereg(root: Path, rel: str, *, tamper: bool = False) -> None:
    priv, pub = signing.generate_keypair()
    (root / "research" / "agents").mkdir(parents=True, exist_ok=True)
    (root / "research" / "agents" / "trust_roots.json").write_text(
        json.dumps({"maintainers": {"owner": pub}}), encoding="ascii"
    )
    body = {"signed": True, "hypothesis": "h"}
    msg = canonical(body).encode("ascii")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sig = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv)).sign(msg).hex()
    doc = {
        **body,
        "signature": {
            "scheme": "ed25519",
            "maintainer": "owner",
            "public_key": pub,
            "message_sha256": hashlib.sha256(msg).hexdigest(),
            "sig": sig,
        },
    }
    if tamper:
        doc["hypothesis"] = "changed after signing"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc), encoding="utf-8")


def test_unread_scope_opens_only_for_its_signed_prereg(tmp_path):
    root = tmp_path / "repo"
    u = make_dataset(tmp_path, "u", ["2025-12-08"])
    reg = registry_for(u, states={"2025-12-08": UNREAD})
    rel = "research/preregs/p.json"
    _signed_prereg(root, rel)
    rec = hr.guard(u, purpose="t", prereg_id=rel, registry=reg, root=root)
    assert rec["state"] == "unread" and rec["authorised_by"] == rel
    with pytest.raises(hr.HoldoutRefused, match="not listed"):
        hr.guard(u, purpose="t", prereg_id="research/preregs/other.json", registry=reg, root=root)
    _signed_prereg(root, rel, tamper=True)  # document changed after signing
    with pytest.raises(hr.HoldoutRefused, match="not a signed prereg"):
        hr.guard(u, purpose="t", prereg_id=rel, registry=reg, root=root)


def test_spent_scope_replay_needs_a_listed_and_signed_id(tmp_path):
    s = make_dataset(tmp_path, "s", ["2026-05-15"])
    reg = registry_for(s, states={"2026-05-15": SPENT})
    # "P1" is listed in spent_by but is no signed prereg anywhere: B8-style replay fails closed
    with pytest.raises(hr.HoldoutRefused, match="not a signed prereg"):
        hr.guard(s, purpose="t", prereg_id="P1", registry=reg, root=tmp_path / "repo")


# -------------------------------------------------------------- bypasses --
def test_bypass_renamed_directory(tmp_path):
    d = make_dataset(tmp_path, "holdout2026", ["2026-05-15"])
    reg = registry_for(d, states={"2026-05-15": SPENT})
    renamed = tmp_path / "totally_in_sample"
    d.rename(renamed)
    with pytest.raises(hr.HoldoutRefused, match="SPENT"):
        hr.guard(renamed, purpose="t", registry=reg)


def test_bypass_innocent_name_does_not_free_a_spent_dataset_and_holdout_name_does_not_block(
    tmp_path,
):
    spent = make_dataset(tmp_path, "plain_name", ["2026-05-15"])
    free = make_dataset(tmp_path, "holdout_mock", ["2019-01-30"])
    reg = registry_for(spent, free, states={"2026-05-15": SPENT, "2019-01-30": IN})
    with pytest.raises(hr.HoldoutRefused):
        hr.guard(spent, purpose="t", registry=reg)
    assert hr.guard(free, purpose="t", registry=reg)["state"] == "in_sample"  # no substring check


def test_bypass_symlink(tmp_path):
    d = make_dataset(tmp_path, "real_one", ["2026-05-15"])
    reg = registry_for(d, states={"2026-05-15": SPENT})
    link = tmp_path / "harmless"
    try:
        os.symlink(d, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("cannot create a symlink here")
        import subprocess

        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(d)], capture_output=True)
        if r.returncode != 0:  # a directory junction needs no privilege
            pytest.skip("neither symlink nor junction could be created")
    with pytest.raises(hr.HoldoutRefused, match="SPENT"):
        hr.guard(link, purpose="t", registry=reg)
    with pytest.raises(hr.HoldoutRefused, match="SPENT"):  # a path INSIDE the link too
        hr.guard(link / "normalized", purpose="t", registry=reg)


def test_bypass_copied_directory(tmp_path):
    d = make_dataset(tmp_path, "orig", ["2026-05-15"])
    reg = registry_for(d, states={"2026-05-15": SPENT})
    copy = tmp_path / "elsewhere" / "copy"
    shutil.copytree(d, copy)
    with pytest.raises(hr.HoldoutRefused, match="SPENT"):
        hr.guard(copy, purpose="t", registry=reg)
    # a copy with an EDITED manifest version and relabelled session is still caught: the
    # registry is looked up by the content hash of the bytes and holds the real sessions
    man = json.loads((copy / "dataset.json").read_text())
    man["dataset_version"] = "f" * 64
    man["sessions"] = {"2019-01-30": {}}
    (copy / "dataset.json").write_text(json.dumps(man))
    with pytest.raises(hr.HoldoutRefused, match="SPENT"):
        hr.guard(copy, purpose="t", registry=reg)


def test_bypass_tampered_bytes_do_not_match_the_manifest(tmp_path):
    d = make_dataset(tmp_path, "d", ["2019-01-30"])
    reg = registry_for(d, states={"2019-01-30": IN})
    (d / "normalized" / "eq_20190130.normalized.iap1").write_bytes(b"swapped bytes")
    with pytest.raises(hr.HoldoutRefused, match="content differs"):
        hr.guard(d, purpose="t", registry=reg)


def test_bypass_date_label_selection_from_a_multi_session_dataset(tmp_path):
    d = make_dataset(tmp_path, "mix", ["2019-01-30", "2026-05-15"])
    reg = registry_for(d, states={"2019-01-30": IN, "2026-05-15": SPENT})
    assert hr.guard(d, purpose="t", sessions=["2019-01-30"], registry=reg)["state"] == "in_sample"
    with pytest.raises(hr.HoldoutRefused, match="2026-05-15"):
        hr.guard(d, purpose="t", sessions=["2026-05-15"], registry=reg)
    with pytest.raises(hr.HoldoutRefused, match="2026-05-15"):  # no selection = all sessions
        hr.guard(d, purpose="t", registry=reg)
    with pytest.raises(hr.HoldoutRefused, match="not in this dataset"):
        hr.guard(d, purpose="t", sessions=["2024-01-02"], registry=reg)
    # relabel the manifest so the spent day claims to be the in-sample day: the registry's
    # sessions for this content hash win
    man = json.loads((d / "dataset.json").read_text())
    man["sessions"] = {"2019-01-30": {}, "2019-03-27": {}}
    (d / "dataset.json").write_text(json.dumps(man))
    with pytest.raises(hr.HoldoutRefused, match="SPENT"):
        hr.guard(d, purpose="t", registry=reg)


def test_symbol_scope_unread_for_names_outside_the_three(tmp_path):
    rules = [
        {"symbols": ["AAPL", "MSFT", "QQQ"], "state": "spent", "spent_by": ["x"]},
        {"symbols": "*", "state": "unread", "reserved_for": ["p.json"]},
    ]
    d = make_dataset(tmp_path, "b", ["2025-12-08"], symbols=("AAPL", "ZZZZ"))
    reg = registry_for(d, states={"2025-12-08": rules})
    with pytest.raises(hr.HoldoutRefused, match="UNREAD"):
        hr.guard(d, purpose="t", registry=reg)  # ZZZZ is unread: the stricter state wins
    with pytest.raises(hr.HoldoutRefused, match="SPENT"):
        hr.guard(d, purpose="t", symbols=["AAPL"], registry=reg)


def test_refuse_unit_name_by_date_and_label():
    reg = hr.load_registry(root=REPO)
    for name in ("r7_holdout", "r7_20251128", "ds_20260612", "holdout2026", "x_20260515_AAPL.npy"):
        with pytest.raises(hr.HoldoutRefused):
            hr.refuse_unit_name(name, registry=reg)
    hr.refuse_unit_name("ds3_20190130_AAPL.X.npy", registry=reg)
    with pytest.raises(hr.HoldoutRefused):  # an unknown date is refused too
        hr.refuse_unit_name("ds_20240102", registry=reg)


# ------------------------------------------------- the entry points call it --
def _committed_clone(tmp_path: Path, name: str, version: str, sessions: list[str]) -> Path:
    """A dataset the COMMITTED registry knows by version (no bytes needed)."""
    d = tmp_path / name
    d.mkdir()
    (d / "dataset.json").write_text(
        json.dumps(
            {
                "kind": "real",
                "dataset_version": version,
                "sessions": {s: {} for s in sessions},
                "universe": [{"symbol": s} for s in ("AAPL", "MSFT", "QQQ")],
            }
        ),
        encoding="utf-8",
    )
    return d


def test_every_entry_point_refuses_the_spent_holdout_under_an_innocent_name(tmp_path):
    h = _committed_clone(tmp_path, "innocent", HOLDOUT2026, ["2026-05-15", "2026-05-18"])
    from iap.research import __main__ as cli
    from iap.research import execution_benchmark, screen, two_venue

    class A:  # what the research CLI hands its guard
        dataset_dir = h
        command = "run"
        holdout_prereg_id = None

    with pytest.raises(cli.ResearchError, match="SPENT"):
        cli._guard_dataset(A)
    with pytest.raises(SystemExit, match="SPENT"):
        hr.guard_or_exit(h, "execution_benchmark", True)  # the bool authorises nothing
    with pytest.raises(screen.ScreenError, match="SPENT"):
        screen.assert_not_holdout(h, "other", _Spec())
    with pytest.raises(SystemExit, match="SPENT"):
        two_venue.load_merged_session(h, h, "20260515")
    # breadth book returns 2 before reading anything
    from iap.research import breadth_book

    assert breadth_book.main(["--dataset-dir", str(h)]) == 2
    assert execution_benchmark  # imported: its guard call is the one-line shared function


class _Spec:
    excluded_dataset_versions: tuple = ()


def test_in_sample_dataset_clone_passes_the_cli_guard(tmp_path):
    d = _committed_clone(tmp_path, "whatever", MULTI7, ["2019-01-30"])
    from iap.research import __main__ as cli

    class A:
        dataset_dir = d
        command = "run"
        holdout_prereg_id = None

    cli._guard_dataset(A)  # no raise


def test_research_assistant_has_no_prereg_id_so_only_in_sample_passes(tmp_path):
    from iap.agents import research_assistant as ra

    assert ra.holdout_patterns(tmp_path) == [] or "*holdout*" not in ra.holdout_patterns(tmp_path)
