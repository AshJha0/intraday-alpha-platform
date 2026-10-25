"""CR1 adversarial tests (U0 section 2, properties 1-5, each with a test here;
property 6 is wording, tested in test_runner_governance.py).

A dry registration on synthetic data runs once and is refused the second
time; every verification happens before any request; the log carries no
path, account or data; results are committed by the iap-runner identity to
an append-only branch and a rewritten result is detectable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from runner import redact, results, run, verify
from runner_fixtures import IMAGE, World, git


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _run(world: World, **kw):
    log = redact.RedactingLog([str(world.root)])
    kw.setdefault("identity", "iap-runner")
    kw.setdefault("transport", world.transport)
    kw.setdefault("cache_root", world.root.parent / "runner-cache")
    rec = run.registered_run(world.root, world.prereg_rel, log=log, **kw)
    return rec, log


# ----------------------------------------------------------- the dry run --
def test_dry_registration_runs_once_and_is_refused_the_second_time(world):
    rec, log = _run(world)
    assert rec["seq"] == 1 and rec["evidence_class"] == "mechanically isolated"
    assert rec["dataset_hash_id"] == world.dataset_hash_id
    assert world.ops() == ["read", "store_result", "publish"]
    # the result landed on the results branch, committed by iap-runner
    sha = rec["result_commit"]
    assert git(world.root, "rev-parse", "results") == sha
    assert (
        git(world.root, "log", "-1", "--format=%an <%ae>", sha)
        == "iap-runner <iap-runner@localhost>"
    )
    tree = git(world.root, "ls-tree", "-r", "--name-only", sha).splitlines()
    slug = results.slug(world.prereg_rel)
    assert f"results/{slug}/0001/result.json" in tree
    assert f"results/{slug}/0001/hashes.json" in tree
    assert "research/audit/CHECKPOINTS.jsonl" in tree
    cp_line = json.loads(git(world.root, "show", f"{sha}:research/audit/CHECKPOINTS.jsonl"))
    assert cp_line["partition"] == "b" and cp_line["checkpoint"]["seq"] == 1
    assert world.ss.checkpoint_problems(cp_line["checkpoint"]) == []
    result = json.loads(git(world.root, "show", f"{sha}:results/{slug}/0001/result.json"))
    assert result["bytes"] == sum(len((world.ddir / n).read_bytes()) for n in world.files)
    # the service published the checkpoint and holds the result hash
    assert world.service.pending() is None
    reg = world.service.ledger.registrations[rec["prereg_sha256"]]
    assert reg["status"] == "published" and reg["commit"] == sha
    assert reg["result_sha256"] == rec["result_sha256"]
    # property 3: the second run of the same registration is refused, nothing else happens
    world.calls.clear()
    with pytest.raises(run.RunRefused) as exc:
        _run(world)
    assert exc.value.code == 2 and "already_registered" in str(exc.value)
    assert world.ops() == ["read"]
    assert git(world.root, "rev-parse", "results") == sha  # no second commit
    # a new commit of the same prereg id is still the same registration
    world.script.write_text(world.script.read_text() + "# touched\n")
    git(world.root, "commit", "-q", "-am", "new commit")
    world.commit = git(world.root, "rev-parse", "HEAD")
    world.write_prereg()
    git(world.root, "commit", "-q", "-am", "re-signed prereg")
    world.commit = git(world.root, "rev-parse", "HEAD")
    world.write_prereg()
    git(world.root, "commit", "-q", "-am", "re-signed prereg at the new commit")
    with pytest.raises(run.RunRefused, match="already_registered"):
        _run(world)


def test_private_cache_is_removed_and_holds_nothing_after_the_run(world, tmp_path):
    cache_root = tmp_path / "runner-cache"
    _run(world, cache_root=cache_root)
    assert list(cache_root.iterdir()) == []


# ---------------------------------------------------- property 1: the log --
def test_log_carries_no_path_account_host_or_data(world, tmp_path):
    rec, log = _run(world)
    text = log.text()
    assert "ROW" not in text and "verdict" not in text  # the analysis's stdout never reaches it
    for forbidden in (
        str(world.root),
        str(tmp_path),
        "iap-runner",
        "iap-sealed",
        os.environ.get("USERNAME", "ashis"),
        os.environ.get("COMPUTERNAME", "NO-HOST-NAME"),
        "\\\\.\\pipe",
    ):
        assert forbidden.lower() not in text.lower(), forbidden
    assert "sealed read served: seq 1" in text and "checkpoint seq 1 published" in text


@pytest.mark.parametrize(
    "line, must_not_contain",
    [
        (r"cache at C:\iap-sealed\runner\cache\run-abc", ["C:\\", "iap-sealed", "runner"]),
        ("wrote /home/iap-runner/out/result.json", ["/home", "iap-runner"]),
        (r"pipe \\.\pipe\iap-sealed-b refused", ["\\\\.", "iap-sealed"]),
        ("mail iap-runner@localhost", ["@", "iap-runner"]),
        ("secret-dir-name/x", ["secret-dir-name"]),
    ],
)
def test_redact_filter(line, must_not_contain):
    out = redact.redact(line, ["secret-dir-name/x"])
    for s in must_not_contain:
        assert s.lower() not in out.lower(), (out, s)


# ------------------------------------- property 4: verification before any read --
def test_tampered_signature_is_refused_before_any_request(world):
    doc = json.loads((world.root / world.prereg_rel).read_text())
    doc["registered_run"]["command"] = [sys.executable, "-c", "print('evil')"]
    (world.root / world.prereg_rel).write_text(json.dumps(doc))
    with pytest.raises(run.RunRefused) as exc:
        _run(world)
    assert exc.value.code == 3 and "signature" in str(exc.value)
    assert world.ops() == []
    assert world.service.ledger.registrations == {}


def test_unsigned_prereg_is_refused(world):
    doc = json.loads((world.root / world.prereg_rel).read_text())
    del doc["signature"]
    (world.root / world.prereg_rel).write_text(json.dumps(doc))
    with pytest.raises(run.RunRefused, match="signature"):
        _run(world)
    assert world.ops() == []


def test_untrusted_signing_key_is_refused(world):
    from sealed_fixtures import keypair, sign_document

    p2, k2 = keypair()
    world.write_prereg(sign_document(world.body(), p2, k2))
    with pytest.raises(run.RunRefused, match="trust-root"):
        _run(world)
    assert world.ops() == []


def test_image_digest_mismatch_is_refused(world):
    (world.root / "deployment" / "docker" / "IMAGE_DIGEST").write_text("sha256:" + "00" * 32 + "\n")
    with pytest.raises(run.RunRefused, match="image digest"):
        _run(world)
    assert world.ops() == []
    (world.root / "deployment" / "docker" / "IMAGE_DIGEST").unlink()
    with pytest.raises(run.RunRefused, match="IMAGE_DIGEST missing"):
        _run(world)
    (world.root / "deployment" / "docker" / "IMAGE_DIGEST").write_text("latest\n")
    with pytest.raises(run.RunRefused, match="sha256 image digest"):
        _run(world)


def test_changed_bound_script_fails_the_freeze_hashes(world):
    world.script.write_text(world.script.read_text() + "\nprint('changed after signing')\n")
    with pytest.raises(run.RunRefused) as exc:
        _run(world)
    assert exc.value.code == 3 and "changed since signing" in str(exc.value)
    assert world.ops() == []


def test_bound_commit_that_is_not_an_ancestor_of_head_is_refused(world):
    from sealed_fixtures import sign_document

    body = world.body()
    body["analysis_identity"]["commit"] = "0" * 40
    world.write_prereg(sign_document(body, world.priv, world.pub))
    with pytest.raises(run.RunRefused) as exc:
        _run(world)
    assert exc.value.code == 3 and "not HEAD or an ancestor" in str(exc.value)
    assert world.ops() == []


def test_commit_after_the_bound_one_passes_only_with_unchanged_bound_hashes(world):
    """The prereg is committed on top of the commit it binds (an ancestor of HEAD
    is allowed); a later commit that changes a bound script is refused."""
    (world.root / "NOTES.md").write_text("unrelated")
    git(world.root, "add", "-A")
    git(world.root, "commit", "-q", "-m", "unrelated commit")
    rec, _ = _run(world)
    assert rec["seq"] == 1
    from sealed_fixtures import sign_document

    bound_before = world.body(prereg_id="research/preregs/second.json")  # binds the current hashes
    world.script.write_text(world.script.read_text() + "# changed")
    git(world.root, "commit", "-q", "-am", "changed the bound script after binding")
    world2 = world
    world2.prereg_rel = "research/preregs/second.json"
    world2.write_prereg(sign_document(bound_before, world.priv, world.pub))
    with pytest.raises(run.RunRefused, match="changed since signing"):
        _run(world2)


def test_dataset_id_must_be_bound_by_the_identity(world):
    other = world.body()
    other["registered_run"]["dataset_hash_id"] = "ff" * 32
    other["registered_run"]["dataset_files"] = {}
    from sealed_fixtures import sign_document

    world.write_prereg(sign_document(other, world.priv, world.pub))
    with pytest.raises(run.RunRefused, match="not among the bound data_versions"):
        _run(world)
    assert world.ops() == []


def test_prereg_without_registered_run_block_is_refused(world):
    body = world.body()
    del body["registered_run"]
    from sealed_fixtures import sign_document

    world.write_prereg(sign_document(body, world.priv, world.pub))
    with pytest.raises(run.RunRefused, match="registered_run"):
        _run(world)
    assert world.ops() == []


def test_served_dataset_that_differs_from_the_bound_files_does_not_run(world):
    """The service's record was changed (an extra file) after the prereg bound the
    file set: the bytes arrive, the hashes disagree, the analysis never runs."""
    extra = world.ddir / "extra.csv"
    extra.write_bytes(b"x\n")
    import hashlib

    ds = json.loads((world.part_root / "service" / "datasets.json").read_text())
    ds[world.dataset_hash_id]["files"]["extra.csv"] = hashlib.sha256(b"x\n").hexdigest()
    (world.part_root / "service" / "datasets.json").write_text(json.dumps(ds))
    with pytest.raises(run.RunRefused, match="differs from the bound dataset_files") as exc:
        _run(world)
    assert exc.value.code == 2
    assert world.ops() == ["read"]  # served, but no result stored, nothing committed
    assert (
        subprocess.run(
            ["git", "-C", str(world.root), "rev-parse", "--verify", "--quiet", "results"],
            capture_output=True,
        ).returncode
        != 0
    )


def test_tampered_sealed_bytes_fail_the_registration_with_no_retry(world):
    (world.ddir / "prices.csv").write_text("date,close\n2020-01-02,9.9\n")
    with pytest.raises(run.RunRefused, match="dataset_hash_mismatch"):
        _run(world)
    (world.ddir / "prices.csv").write_text("date,close\n2020-01-02,1.0\n2020-01-03,1.5\n")
    with pytest.raises(run.RunRefused, match="already_registered"):
        _run(world)


# ----------------------------------------------------- identity of the process --
def test_only_the_runner_account_may_run(world):
    for who in ("ashis", "iap-sealed-b", "Administrator", "SYSTEM", ""):
        with pytest.raises(run.RunRefused) as exc:
            _run(world, identity=who)
        assert exc.value.code == 3
    assert world.ops() == []


def test_service_refuses_a_caller_that_is_not_the_runner(world):
    world.caller_account = "ashis"  # the kernel says the pipe client is the developer
    with pytest.raises(run.RunRefused, match="caller_account"):
        _run(world)
    assert world.service.ledger.registrations == {}


# ------------------------------------------------ property 5: append-only results --
def test_rewritten_result_is_detected(world):
    rec, _ = _run(world)
    sha = rec["result_commit"]
    assert (
        run.verify_result(world.root, world.prereg_rel, sha, 1, transport=world.transport) is True
    )
    # an attacker rewrites the result and its hashes.json on the branch
    wt = world.root.parent / "attacker"
    git(world.root, "worktree", "add", "-q", str(wt), "results")
    slug = results.slug(world.prereg_rel)
    (wt / "results" / slug / "0001" / "result.json").write_text(
        json.dumps({"bytes": 1, "verdict": "PROMOTE"})
    )
    import hashlib

    h = json.loads((wt / "results" / slug / "0001" / "hashes.json").read_text())
    h["result_sha256"] = hashlib.sha256(
        json.dumps({"bytes": 1, "verdict": "PROMOTE"}).encode()
    ).hexdigest()
    (wt / "results" / slug / "0001" / "hashes.json").write_text(json.dumps(h))
    git(wt, "commit", "-q", "-am", "rewrite")
    bad = git(wt, "rev-parse", "HEAD")
    assert (
        run.verify_result(world.root, world.prereg_rel, bad, 1, transport=world.transport) is False
    )
    assert (
        run.verify_result(world.root, world.prereg_rel, bad, 7, transport=world.transport) is False
    )


def test_results_branch_appends_and_never_overwrites(world, tmp_path):
    sha1 = results.commit_result(
        world.root, prereg_id="research/preregs/x.json", seq=1, partition="b",
        result_files={"result.json": b"{}"}, hashes={"result_sha256": "0" * 64},
        checkpoint={"seq": 1, "chain_head": "0" * 64, "utc": "t", "prereg_id": "x", "dataset_hash_id": "d"},
    )  # fmt: skip
    sha2 = results.commit_result(
        world.root, prereg_id="research/preregs/x.json", seq=2, partition="b",
        result_files={"result.json": b"{}"}, hashes={"result_sha256": "1" * 64},
        checkpoint={"seq": 2, "chain_head": "1" * 64, "utc": "t", "prereg_id": "x", "dataset_hash_id": "d"},
    )  # fmt: skip
    assert git(world.root, "rev-parse", f"{sha2}^") == sha1  # history preserved
    lines = git(world.root, "show", f"{sha2}:research/audit/CHECKPOINTS.jsonl").splitlines()
    assert [json.loads(x)["checkpoint"]["seq"] for x in lines] == [1, 2]
    with pytest.raises(FileExistsError, match="one result per run"):
        results.commit_result(
            world.root, prereg_id="research/preregs/x.json", seq=2, partition="b",
            result_files={"result.json": b"{}"}, hashes={}, checkpoint={"seq": 2},
        )  # fmt: skip
    assert results.read_result_hash(world.root, sha1, "research/preregs/x.json", 1) == "0" * 64
    assert results.read_result_hash(world.root, sha1, "research/preregs/x.json", 9) is None
    # the bound checkout was never touched
    assert git(world.root, "status", "--porcelain") == ""


def test_verify_module_is_a_registered_identity_runner():
    src = verify.__file__ and open(verify.__file__, encoding="utf-8").read()
    assert "verify_identity(" in src and "verify_signed_document(" in src


def test_image_digest_reader_accepts_a_trailing_note(tmp_path):
    (tmp_path / "deployment" / "docker").mkdir(parents=True)
    (tmp_path / "deployment" / "docker" / "IMAGE_DIGEST").write_text(IMAGE + "  built 2026-10-25\n")
    assert verify.read_image_digest(tmp_path) == IMAGE
