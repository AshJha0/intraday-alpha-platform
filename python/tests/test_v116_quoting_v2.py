"""v1.16: quoting study v2 protocol (signed prereg on main's board, validity gate)."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from iap.agents import signing
from iap.agents.blackboard import canonical
from iap.agents.broker import WriteBroker
from iap.research import quoting_real as qr

ROOT = Path(__file__).resolve().parents[2]
DRAFT = ROOT / "research" / "quoting_real" / "prereg_v2_DRAFT.json"


def test_draft_has_the_owner_approved_values():
    d = json.loads(DRAFT.read_text(encoding="utf-8"))
    assert {c["id"]: c["alpha_weight"] for c in d["cells"]} == {
        "EQ01": 7.0,
        "EQ05": 15.0,
        "EQ10": 12.0,
        "QNOSKEW": 0.0,
    }
    assert d["quoting_config"]["inv_skew_ticks"] == 1.0
    assert d["validity_gate_threshold"] == 0.10
    assert d["signed"] is False
    assert not any(str(v).startswith("TO_CONFIRM") for v in d["quoting_config"].values())
    # every quoting_config key is a QuotingConfig field
    cfg = qr.quoting_config(d, 7.0, 0)
    assert (cfg.skew_mode, cfg.alpha_weight, cfg.inv_skew_ticks) == ("ticks", 7.0, 1.0)


def test_unknown_quoting_key_refused():
    with pytest.raises(SystemExit, match="unknown keys"):
        qr.quoting_config({"quoting_config": {"bogus": 1}}, 1.0, 0)


def _signed_root(tmp_path: Path) -> tuple[Path, dict]:
    """A throwaway repo root: trust root + board + a v2 doc registered and signed."""
    root = tmp_path / "repo"
    (root / "research" / "agents").mkdir(parents=True)
    (root / "research" / "quoting_real").mkdir(parents=True)
    shutil.copy(DRAFT, root / "research" / "quoting_real" / "prereg_v2_DRAFT.json")
    priv, pub = signing.generate_keypair()
    (root / "research" / "agents" / "trust_roots.json").write_text(
        json.dumps({"maintainers": {"owner": pub}}), encoding="ascii"
    )
    (root / "research" / "experiments.json").write_text(
        json.dumps({"entries": [], "total_experiments": 0}), encoding="ascii"
    )
    doc = json.loads(DRAFT.read_text(encoding="utf-8"))
    dh = hashlib.sha256(DRAFT.read_bytes()).hexdigest()
    br = WriteBroker(root, {"owner"}, fingerprinter=lambda a: None)
    for c in doc["cells"]:
        br.preregister("owner", c["id"], f"{c['horizon']}:{doc['horizon_tag']}", f"x {dh} y", 1)
    doc.update(
        signed=True,
        on_blackboard=True,
        open_for_owner=[],
        registration={
            "draft_file": "research/quoting_real/prereg_v2_DRAFT.json",
            "draft_sha256": dh,
        },
    )
    msg = canonical(doc).encode("ascii")
    sig = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv)).sign(msg).hex()
    doc["signature"] = {
        "scheme": "ed25519",
        "maintainer": "owner",
        "public_key": pub,
        "message_sha256": hashlib.sha256(msg).hexdigest(),
        "sig": sig,
    }
    return root, doc


def test_v2_gate_accepts_a_signed_registered_doc_and_refuses_tampering(tmp_path):
    root, doc = _signed_root(tmp_path)
    assert len(qr.check_prereg_v2(root, doc)) == 4
    bad = json.loads(json.dumps(doc))
    bad["cells"][0]["alpha_weight"] = 99.0
    with pytest.raises(SystemExit, match="signature"):
        qr.check_prereg_v2(root, bad)
    (root / "research" / "quoting_real" / "prereg_v2_DRAFT.json").write_text("{}", encoding="ascii")
    with pytest.raises(SystemExit, match="draft"):
        qr.check_prereg_v2(root, doc)


def test_v2_gate_refuses_unsigned_draft():
    with pytest.raises(SystemExit, match="not signed"):
        qr.check_prereg_v2(ROOT, json.loads(DRAFT.read_text(encoding="utf-8")))


def _unit(session, frac, net, warm=False):
    r = {k: 0.0 for k in qr.PARTS}
    r.update(
        net=net, n_quote_fills=5, max_abs_inventory=100, frac_decisions_skew_changed_quote=frac
    )
    ctl = {k: 0.0 for k in qr.PARTS}
    ctl.update(n_quote_fills=5, max_abs_inventory=100)
    return {
        "session": session,
        "symbol": "AAPL",
        "warmup_only": warm,
        "results": {"EQ01": r, "QNOSKEW": ctl},
    }


@pytest.mark.parametrize(("frac", "inert"), [(0.05, True), (0.25, False)])
def test_summary_validity_gate(tmp_path, frac, inert):
    units = tmp_path / "units"
    units.mkdir()
    for i, s in enumerate(["d1", "d2", "d3", "d4"]):
        (units / f"{s}.json").write_text(json.dumps(_unit(s, frac, 1.0 + i)), encoding="utf-8")
    prereg = {
        "cells": [
            {"id": "EQ01", "alpha": "EQ01"},
            {"id": "QNOSKEW", "alpha": None},
        ],
        "validity_gate_threshold": 0.10,
    }
    row = qr.summarise(units, prereg)["cells"]["EQ01"]
    assert row["valid"] is (not inert)
    assert row["verdict"].startswith("SKEW INERT") is inert
