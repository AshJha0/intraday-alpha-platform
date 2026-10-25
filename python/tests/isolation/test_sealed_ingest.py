"""D2 service, DD1 ``ingest`` op: runner account only, declared hashes must equal
the content hash, bytes must match their headers, a known id is refused, a
failed ingest leaves no files, the record is what ``add-dataset`` writes (so a
later signed ``read`` serves it), the response carries hashes and the caller's
counts and never content, and the frame protocol round-trips the upload."""

from __future__ import annotations

import io
import json

from sealed_fixtures import make_prereg, make_service

RUNNER = "iap-runner"


def _files():
    return [("prices.csv", b"date,close\n2024-01-02,1\n"), ("meta.json", b"{}\n")]


def _request(ss, files, **over):
    hashes = {n: ss.sha256_bytes(b) for n, b in files}
    req = {
        "op": "ingest",
        "dataset_hash_id": ss.content_hash(hashes),
        "files": hashes,
        "row_counts": {"prices.csv": 1, "meta.json": 0},
    }
    req.update(over)
    return req


def _incoming(ss, files):
    buf = io.BytesIO()
    ss.send_files(buf, files)
    buf.seek(0)
    return list(ss._receive_files(buf))


def test_ingest_is_runner_only_and_records_the_dataset(tmp_path):
    ss, svc, _, _, _ = make_service(tmp_path)
    files = _files()
    req = _request(ss, files)
    for who in ("ashis", "iap-sealed-b", "Administrator"):
        resp, _ = svc.handle(req, ss.Caller(account=who, pid=1), _incoming(ss, files))
        assert resp["error"] == "caller_account"
    assert not (svc.cfg.root / "datasets" / req["dataset_hash_id"]).exists()
    resp, stream = svc.handle(req, ss.Caller(account=RUNNER, pid=1), _incoming(ss, files))
    assert stream is None and resp["ok"] is True
    assert resp["files"] == req["files"] and resp["row_counts"] == req["row_counts"]
    assert set(resp) == {"ok", "dataset_hash_id", "partition", "files", "row_counts", "chain_head"}
    rec = svc.datasets()[req["dataset_hash_id"]]
    assert rec["files"] == req["files"] and rec["row_counts"] == req["row_counts"]
    for n, b in files:
        assert (svc.cfg.root / rec["dir"] / n).read_bytes() == b
    last = json.loads(svc.audit.path.read_text().splitlines()[-1])
    assert last["decision"] == "ingested" and last["files"] == 2
    assert resp["chain_head"] == svc.audit.head()


def test_second_ingest_of_the_same_id_is_refused(tmp_path):
    ss, svc, _, _, _ = make_service(tmp_path)
    files = _files()
    req = _request(ss, files)
    assert svc.handle(req, ss.Caller(account=RUNNER), _incoming(ss, files))[0]["ok"]
    resp, _ = svc.handle(req, ss.Caller(account=RUNNER), _incoming(ss, files))
    assert resp["error"] == "dataset_exists"


def test_hash_and_declaration_mismatches_leave_nothing_behind(tmp_path):
    ss, svc, _, _, _ = make_service(tmp_path)
    files = _files()
    good = _request(ss, files)
    # a claimed id that is not the content hash
    wrong_id = {**good, "dataset_hash_id": "ab" * 32}
    assert svc.handle(wrong_id, ss.Caller(account=RUNNER), _incoming(ss, files))[0]["error"] == (
        "dataset_hash_mismatch"
    )
    # bytes that do not match the declared sha256 of their name
    tampered = [("prices.csv", b"date,close\n2024-01-02,2\n"), files[1]]
    resp, _ = svc.handle(good, ss.Caller(account=RUNNER), _incoming(ss, tampered))
    assert resp["error"] == "file_hash_mismatch"
    # a file not declared
    extra = [*files, ("extra.csv", b"x")]
    resp, _ = svc.handle(good, ss.Caller(account=RUNNER), _incoming(ss, extra))
    assert resp["error"] == "file_unexpected"
    # a declared file that never arrives
    resp, _ = svc.handle(good, ss.Caller(account=RUNNER), _incoming(ss, files[:1]))
    assert resp["error"] == "files_incomplete"
    # a path that escapes the dataset directory
    escaping = [("../escape.csv", b"x")]
    bad = _request(ss, escaping)
    resp, _ = svc.handle(bad, ss.Caller(account=RUNNER), _incoming(ss, escaping))
    assert resp["error"] == "file_name_invalid"
    assert not (svc.cfg.root / "escape.csv").exists()
    # only the fixture's own dataset directory exists: every refused ingest cleaned up
    assert {p.name for p in (svc.cfg.root / "datasets").iterdir()} == {"ds-synthetic-0001"}
    assert good["dataset_hash_id"] not in svc.datasets()
    # malformed requests
    for over in (
        {"dataset_hash_id": "nothex"},
        {"files": {}},
        {"row_counts": {"prices.csv": -1}},
        {"row_counts": {"prices.csv": "1"}},
    ):
        resp, _ = svc.handle({**good, **over}, ss.Caller(account=RUNNER), _incoming(ss, files))
        assert resp["ok"] is False, over


def test_an_ingested_dataset_is_served_by_a_signed_read(tmp_path):
    ss, svc, (priv, pub), _, _ = make_service(tmp_path)
    files = _files()
    req = _request(ss, files)
    assert svc.handle(req, ss.Caller(account=RUNNER), _incoming(ss, files))[0]["ok"]
    prereg = make_prereg(priv, pub, dataset_hash_id=req["dataset_hash_id"])
    resp, stream = svc.handle(
        {"op": "read", "prereg": prereg, "dataset_hash_id": req["dataset_hash_id"]},
        ss.Caller(account=RUNNER),
    )
    assert resp["ok"] is True and resp["files"] == req["files"]
    served = {h["file"]: data for h, data in stream}
    assert served == dict(files)


def test_frame_protocol_round_trips_an_ingest(tmp_path):
    ss, svc, _, _, _ = make_service(tmp_path)
    files = _files()
    req = _request(ss, files)
    wire = io.BytesIO()
    ss.write_frame(wire, json.dumps(req, sort_keys=True).encode())
    ss.send_files(wire, files)
    wire.seek(0)
    out = io.BytesIO()
    ss.serve_connection(svc, ss.Caller(account=RUNNER, pid=7), wire, out)
    out.seek(0)
    resp, streamed = ss.receive(out)
    assert resp["ok"] is True and streamed == []  # an ingest response streams nothing back
    assert req["dataset_hash_id"] in svc.datasets()


def test_install_and_check_create_the_state_the_service_needs(tmp_path):
    from sealed_fixtures import keypair, load_tool

    ss = load_tool("sealed_service", "tools/sealed_service.py")
    _, pub = keypair()
    roots = tmp_path / "trust_roots.json"
    roots.write_text(json.dumps({"maintainers": {"test": pub}}))
    root = tmp_path / "sealed" / "a"
    state = ss.install_state(root, roots)
    assert (state / "trust_roots.json").read_text() == roots.read_text()
    cfg = json.loads((state / "config.json").read_text())
    assert cfg == {"allowed_accounts": ["iap-runner"], "registered_images": []}
    # a second install keeps an edited config and refreshes a rotated trust root
    (state / "config.json").write_text(
        json.dumps({"allowed_accounts": ["iap-runner"], "registered_images": ["aa" * 32]})
    )
    roots.write_text(json.dumps({"maintainers": {"test": pub, "second": pub}}))
    ss.install_state(root, roots)
    assert "second" in (state / "trust_roots.json").read_text()
    assert json.loads((state / "config.json").read_text())["registered_images"] == ["aa" * 32]
    assert ss.check_install(root, "a") == []
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"maintainers": {}}))
    import pytest

    with pytest.raises(SystemExit):
        ss.install_state(tmp_path / "sealed" / "b", empty)
    assert ss.check_install(tmp_path / "sealed" / "b", "b")
