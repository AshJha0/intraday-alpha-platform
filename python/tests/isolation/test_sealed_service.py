"""D2 sealed-data service: the decision core, transport-free.

Service-side authorisation decides (caller account, process image, pending
checkpoint, signed prereg naming this partition and dataset, registration
ledger, hash verification before any read, write-before-serve), the
checkpoint content rule, restart reconciliation, result hashes and the frame
protocol.  Synthetic partition in tmp_path; no named pipe."""

from __future__ import annotations

import io
import json

import pytest
from sealed_fixtures import canonical, make_prereg, make_service

RUNNER = "iap-runner"


def _caller(ss, account=RUNNER, image=None):
    return ss.Caller(account=account, pid=4242, image_sha256=image)


def _read(prereg, ds="ds-synthetic-0001"):
    return {"op": "read", "prereg": prereg, "dataset_hash_id": ds}


# ---------------------------------------------------------------- accounts --
def test_only_the_runner_account_is_served(tmp_path):
    ss, svc, (priv, pub), _, _ = make_service(tmp_path)
    prereg = make_prereg(priv, pub)
    for who in ("ashis", "iap-sealed-a", "iap-sealed-b", "Administrator", "SYSTEM", ""):
        resp, stream = svc.handle(_read(prereg), _caller(ss, who))
        assert resp == {"ok": False, "error": "caller_account", "detail": resp["detail"]}
        assert stream is None
    last = json.loads(svc.audit.path.read_text().splitlines()[-1])
    assert last["decision"] == "refused" and last["reason"] == "caller_account"
    # nothing was registered by a refused call
    assert svc.ledger.registrations == {}


def test_process_image_is_checked_when_registered(tmp_path):
    ss, svc, (priv, pub), _, _ = make_service(tmp_path)
    svc.cfg.registered_images = frozenset({"aa" * 32})
    prereg = make_prereg(priv, pub)
    assert svc.handle(_read(prereg), _caller(ss, image=None))[0]["error"] == "caller_image_unknown"
    assert svc.handle(_read(prereg), _caller(ss, image="bb" * 32))[0]["error"] == "caller_image"
    resp, stream = svc.handle(_read(prereg), _caller(ss, image="aa" * 32))
    assert resp["ok"] is True and stream is not None


# ------------------------------------------------------------ the prereg --
def test_unsigned_tampered_and_untrusted_preregs_are_refused(tmp_path):
    ss, svc, (priv, pub), _, _ = make_service(tmp_path)
    good = make_prereg(priv, pub)
    unsigned = {k: v for k, v in good.items() if k != "signature"}
    assert svc.handle(_read(unsigned), _caller(ss))[0]["error"] == "prereg_unsigned"
    tampered = json.loads(json.dumps(good))
    tampered["registered_run"]["dataset_hash_id"] = "ds-other"
    assert svc.handle(_read(tampered, "ds-other"), _caller(ss))[0]["error"] == (
        "prereg_signature_invalid"
    )
    from sealed_fixtures import keypair

    p2, k2 = keypair()
    stranger = make_prereg(p2, k2)  # valid signature, key not in the trust root copy
    assert svc.handle(_read(stranger), _caller(ss))[0]["error"] == "prereg_untrusted_key"
    assert svc.ledger.registrations == {}


def test_prereg_must_name_this_partition_and_the_requested_dataset(tmp_path):
    ss, svc, (priv, pub), _, _ = make_service(tmp_path)
    other_part = make_prereg(priv, pub, partition="c")
    assert svc.handle(_read(other_part), _caller(ss))[0]["error"] == "wrong_partition"
    good = make_prereg(priv, pub)
    assert svc.handle(_read(good, "ds-not-named"), _caller(ss))[0]["error"] == "dataset_not_named"
    unknown = make_prereg(priv, pub, dataset_hash_id="ds-missing")
    assert svc.handle(_read(unknown, "ds-missing"), _caller(ss))[0]["error"] == "dataset_unknown"
    plain = make_prereg(priv, pub)
    del plain["registered_run"]
    from sealed_fixtures import sign_document

    plain = sign_document(plain, priv, pub)
    assert svc.handle(_read(plain), _caller(ss))[0]["error"] == "prereg_not_registered_run"


# -------------------------------------------------------------- the ledger --
def test_one_run_per_registration(tmp_path):
    ss, svc, (priv, pub), ddir, files = make_service(tmp_path)
    prereg = make_prereg(priv, pub)
    resp, stream = svc.handle(_read(prereg), _caller(ss))
    assert resp["ok"] and resp["seq"] == 1 and resp["files"] == files
    got = list(stream)
    assert [h["file"] for h, _ in got] == sorted(files)
    assert all(h["sha256"] == files[h["file"]] for h, _ in got)
    # second request, same hash: refused before any byte, and audited
    resp2, stream2 = svc.handle(_read(prereg), _caller(ss))
    assert resp2["error"] == "checkpoint_pending"  # the first read is not published yet
    svc.handle(
        {"op": "publish", "commit": "abcdef1234", "checkpoint": resp["checkpoint"]}, _caller(ss)
    )
    resp3, stream3 = svc.handle(_read(prereg), _caller(ss))
    assert resp3["error"] == "already_registered" and stream3 is None
    assert "published" in resp3["detail"]
    # the same prereg id signed again (a new commit): still one registration
    resigned = make_prereg(priv, pub, extra={"bound_commit": "newcommit"})
    assert ss.prereg_hash(resigned) != ss.prereg_hash(prereg)
    assert svc.handle(_read(resigned), _caller(ss))[0]["error"] == "already_registered"
    # a different registration (new prereg id) is a new run
    other = make_prereg(priv, pub, prereg_id="research/preregs/other.json")
    assert svc.handle(_read(other), _caller(ss))[0]["seq"] == 2


def test_hash_mismatch_fails_the_registration_with_no_retry(tmp_path):
    ss, svc, (priv, pub), ddir, files = make_service(tmp_path)
    (ddir / "prices.csv").write_text("date,close\n2020-01-02,9.9\n")  # tampered after sealing
    prereg = make_prereg(priv, pub)
    resp, stream = svc.handle(_read(prereg), _caller(ss))
    assert resp["error"] == "dataset_hash_mismatch" and stream is None
    reg = svc.ledger.registrations[ss.prereg_hash(prereg)]
    assert reg["status"] == "failed"
    assert svc.pending() is None  # nothing was served, nothing to publish
    # restoring the bytes does not help: the registration is spent
    (ddir / "prices.csv").write_text("date,close\n2020-01-02,1.0\n2020-01-03,1.5\n")
    assert svc.handle(_read(prereg), _caller(ss))[0]["error"] == "already_registered"


def test_write_before_serve_and_restart_reconciliation(tmp_path):
    ss, svc, (priv, pub), _, _ = make_service(tmp_path)
    prereg = make_prereg(priv, pub)
    resp, stream = svc.handle(_read(prereg), _caller(ss))
    # the ledger entry, audit line and pending checkpoint exist BEFORE the stream is consumed
    assert svc.ledger.registrations[resp["prereg_sha256"]]["status"] == "served"
    assert svc.pending() == resp["checkpoint"]
    audit = [json.loads(x) for x in svc.audit.path.read_text().splitlines()]
    assert audit[-1]["decision"] == "allowed" and audit[-1]["seq"] == 1
    assert resp["checkpoint"]["chain_head"] == ss.sha256_bytes(
        svc.audit.path.read_bytes().splitlines()[-1]
    )
    list(stream)
    # "killed" before publish: a fresh instance keeps the pending checkpoint and refuses reads
    svc2 = ss.SealedService(svc.cfg)
    assert svc2.pending() == resp["checkpoint"]
    other = make_prereg(priv, pub, prereg_id="research/preregs/other.json")
    assert svc2.handle(_read(other), _caller(ss))[0]["error"] == "checkpoint_pending"
    assert svc2.handle({"op": "status"}, _caller(ss))[0]["pending"] == resp["checkpoint"]
    # publish needs the exact checkpoint and a commit
    bad = dict(resp["checkpoint"], seq=99)
    assert svc2.handle({"op": "publish", "commit": "abcdef1", "checkpoint": bad}, _caller(ss))[0][
        "error"
    ] == "checkpoint_mismatch"
    assert svc2.handle(
        {"op": "publish", "commit": "not-hex", "checkpoint": resp["checkpoint"]}, _caller(ss)
    )[0]["error"] == "commit_invalid"
    ok = svc2.handle(
        {"op": "publish", "commit": "0123abcd", "checkpoint": resp["checkpoint"]}, _caller(ss)
    )[0]
    assert ok == {"ok": True, "seq": 1}
    assert svc2.pending() is None
    assert svc2.handle(_read(other), _caller(ss))[0]["ok"] is True
    assert svc2.audit.verify() == []


def test_broken_audit_chain_refuses_to_start(tmp_path):
    ss, svc, _, _, _ = make_service(tmp_path)
    lines = svc.audit.path.read_text().splitlines()
    svc.audit.path.write_text("\n".join([lines[0].replace("start", "begin"), *lines[1:]]) + "\n")
    # one line is fine (genesis); append a second then tamper the first
    svc.audit.append({"event": "x"})
    text = svc.audit.path.read_text().splitlines()
    text[0] = text[0].replace('"begin"', '"start"')
    svc.audit.path.write_text("\n".join(text) + "\n")
    assert svc.audit.verify() != []
    with pytest.raises(SystemExit, match="audit chain broken"):
        ss.SealedService(svc.cfg)


# ------------------------------------------------------------- checkpoints --
def test_checkpoint_content_rule(tmp_path):
    ss, svc, (priv, pub), _, _ = make_service(tmp_path)
    resp, stream = svc.handle(_read(make_prereg(priv, pub)), _caller(ss))
    cp = resp["checkpoint"]
    assert set(cp) == set(ss.CHECKPOINT_FIELDS)
    assert ss.checkpoint_problems(cp) == []
    text = json.dumps(cp)
    for forbidden in ("iap-sealed", "iap-runner", "C:\\", "\\\\.\\pipe", str(tmp_path), "ashis"):
        assert forbidden not in text
    # the rule catches what it must
    assert ss.checkpoint_problems({**cp, "path": "x"})
    assert ss.checkpoint_problems({**cp, "prereg_id": r"C:\iap-sealed\b\x.json"})
    assert ss.checkpoint_problems({**cp, "dataset_hash_id": "iap-runner"})
    assert ss.checkpoint_problems({k: v for k, v in cp.items() if k != "utc"})
    assert ss.checkpoint_problems({**cp, "chain_head": "short"})


def test_committed_checkpoint_file_is_verified(tmp_path):
    ss, svc, (priv, pub), _, _ = make_service(tmp_path)
    resp, _ = svc.handle(_read(make_prereg(priv, pub)), _caller(ss))
    cp = resp["checkpoint"]
    f = tmp_path / "CHECKPOINTS.jsonl"
    f.write_text(json.dumps({"partition": "b", "checkpoint": cp}) + "\n")
    assert ss.verify_checkpoints(f) == []
    f.write_text(
        json.dumps({"partition": "b", "checkpoint": cp})
        + "\n"
        + json.dumps({"partition": "b", "checkpoint": cp})  # seq does not rise
        + "\n"
        + json.dumps({"partition": "b", "checkpoint": {**cp, "seq": 3, "host": "ws"}})
        + "\n"
        + json.dumps({"partition": "z", "checkpoint": cp})
        + "\nnot json\n"
    )
    problems = ss.verify_checkpoints(f)
    assert any("does not rise" in p for p in problems)
    assert any("forbidden field host" in p for p in problems)
    assert any("partition in a/b/c" in p for p in problems)
    assert any("not JSON" in p for p in problems)
    assert ss.verify_checkpoints(tmp_path / "missing.jsonl") == []


# ----------------------------------------------------------------- results --
def test_result_hash_is_stored_once_and_verified(tmp_path):
    ss, svc, (priv, pub), _, _ = make_service(tmp_path)
    resp, stream = svc.handle(_read(make_prereg(priv, pub)), _caller(ss))
    list(stream)
    h = resp["prereg_sha256"]
    svc.handle({"op": "publish", "commit": "abcdef12", "checkpoint": resp["checkpoint"]}, _caller(ss))
    assert svc.handle({"op": "store_result", "prereg_sha256": "nope", "result_sha256": "a" * 64}, _caller(ss))[0]["error"] == "unknown_registration"
    assert svc.handle({"op": "store_result", "prereg_sha256": h, "result_sha256": "short"}, _caller(ss))[0]["error"] == "result_sha256_invalid"
    assert svc.handle({"op": "store_result", "prereg_sha256": h, "result_sha256": "a" * 64}, _caller(ss))[0]["ok"]
    assert svc.handle({"op": "store_result", "prereg_sha256": h, "result_sha256": "b" * 64}, _caller(ss))[0]["error"] == "result_already_stored"
    assert svc.handle({"op": "verify_result", "prereg_sha256": h, "result_sha256": "a" * 64}, _caller(ss))[0]["match"] is True
    assert svc.handle({"op": "verify_result", "prereg_sha256": h, "result_sha256": "b" * 64}, _caller(ss))[0]["match"] is False


def test_unknown_op_is_refused_and_audited(tmp_path):
    ss, svc, _, _, _ = make_service(tmp_path)
    assert svc.handle({"op": "delete_everything"}, _caller(ss))[0]["error"] == "unknown_op"
    assert json.loads(svc.audit.path.read_text().splitlines()[-1])["reason"] == "unknown_op"


# ----------------------------------------------------------------- framing --
def test_frame_protocol_round_trip(tmp_path):
    ss, svc, (priv, pub), ddir, files = make_service(tmp_path)
    req = io.BytesIO()
    ss.write_frame(req, json.dumps(_read(make_prereg(priv, pub))).encode())
    req.seek(0)
    out = io.BytesIO()
    ss.serve_connection(svc, _caller(ss), req, out)
    out.seek(0)
    response, got = ss.receive(out)
    assert response["ok"] and [h["file"] for h, _ in got] == sorted(files)
    assert got[1][1] == (ddir / "prices.csv").read_bytes()
    # a refused request carries no file frames
    req = io.BytesIO()
    ss.write_frame(req, json.dumps({"op": "status"}).encode())
    req.seek(0)
    out = io.BytesIO()
    ss.serve_connection(svc, _caller(ss, "ashis"), req, out)
    out.seek(0)
    response, got = ss.receive(out)
    assert response["error"] == "caller_account" and got == []


def test_receive_rejects_bytes_that_differ_from_the_header(tmp_path):
    ss, *_ = make_service(tmp_path)
    out = io.BytesIO()
    ss.write_frame(out, json.dumps({"ok": True, "files": {"a": "x"}}).encode())
    ss.write_frame(out, json.dumps({"file": "a", "size": 1, "sha256": "0" * 64}).encode())
    ss.write_frame(out, b"Z")
    out.seek(0)
    with pytest.raises(ValueError, match="differ"):
        ss.receive(out)


def test_canonical_prereg_hash_is_the_registration_key(tmp_path):
    ss, _, (priv, pub), _, _ = make_service(tmp_path)
    p = make_prereg(priv, pub)
    assert ss.prereg_hash(p) == ss.sha256_bytes(canonical(p).encode("ascii"))
