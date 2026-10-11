"""v1.15 trust: content hash blocking at promotion (G-P0-1) and enabled
trust roots (G-P1-2)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from iap.agents import signing, trust
from iap.backtest.maker import MakerFilter
from iap.mlops.__main__ import main as mlops_main
from iap.mlops.registry import CONTENT, PROMOTION, ModelRegistry, RegistryError, TamperError
from test_mlops import META, _data

REPO = Path(__file__).resolve().parents[2]


def _registered(tmp_path):
    X, y = _data(seed=0)
    rec = MakerFilter("meta_gbm", tau=0.5).fit(X, y).register(tmp_path, exploratory=True, **META)
    return ModelRegistry(tmp_path), rec.model_id


# ------------------------------------------------------------------ G-P0-1


def test_promote_with_matching_hash(tmp_path):
    reg, mid = _registered(tmp_path)
    doc = reg.promote(mid)
    assert doc["content_sha256"] == reg.content_hash(mid)
    assert reg.promoted() == [mid]
    assert reg.check_promotions() == []
    assert mlops_main(["--registry", str(tmp_path), "check-promotions"]) == 0


def test_promote_missing_hash_raises(tmp_path):
    reg, mid = _registered(tmp_path)
    (tmp_path / mid / CONTENT).unlink()
    with pytest.raises(RegistryError, match="no content hash"):
        reg.promote(mid)
    assert not (tmp_path / mid / PROMOTION).exists()  # no backfill, no promotion
    assert mlops_main(["--registry", str(tmp_path), "promote", mid]) == 2


def test_promote_mismatched_hash_raises(tmp_path):
    reg, mid = _registered(tmp_path)
    (tmp_path / mid / CONTENT).write_text(json.dumps({"content_sha256": "0" * 64, "scheme": 1}))
    with pytest.raises(TamperError, match="mismatch at promotion"):
        reg.promote(mid)
    assert not (tmp_path / mid / PROMOTION).exists()


def test_check_promotions_fails_after_tamper(tmp_path):
    reg, mid = _registered(tmp_path)
    reg.promote(mid)
    (tmp_path / mid / CONTENT).unlink()
    assert reg.check_promotions()
    assert mlops_main(["--registry", str(tmp_path), "check-promotions"]) == 1
    reg.verify_content(mid)  # backfilled sidecar matches again ...
    assert reg.check_promotions() == []
    doc = json.loads((tmp_path / mid / PROMOTION).read_text())
    (tmp_path / mid / PROMOTION).write_text(json.dumps({**doc, "content_sha256": "1" * 64}))
    assert reg.check_promotions() == [f"model {mid}: promotion.json does not match"]


def test_empty_registry_has_no_promotions(tmp_path):
    assert mlops_main(["--registry", str(tmp_path / "none"), "check-promotions"]) == 0


# ------------------------------------------------------------------ G-P1-2


def test_enabled_roots_require_signature_even_when_empty():
    mpriv, mpub = signing.generate_keypair()
    roots = {"maintainers": {"m": mpub}}
    assert trust.verify_registry({}, None, None) == []  # legacy: no roots, nothing to trust
    assert trust.verify_registry({}, None, None, require_signature=True)
    assert trust.verify_registry({}, None, {"maintainers": {}}, require_signature=True)
    assert trust.verify_registry({}, None, roots)  # unsigned empty registry
    assert trust.verify_registry({}, trust.sign_registry(mpriv, "m", {}), roots) == []
    _, other_pub = signing.generate_keypair()
    signed = trust.sign_registry(mpriv, "m", {})
    assert trust.verify_registry({}, signed, {"maintainers": {"m": other_pub}})


def test_committed_registry_is_maintainer_signed():
    roots = trust.read_roots(REPO)
    assert roots["maintainers"], "trust roots must be enabled"
    for pub in roots["maintainers"].values():
        assert len(bytes.fromhex(pub)) == 32
    sig = json.loads((REPO / trust.REGISTRY_SIG_RELPATH).read_text(encoding="ascii"))
    assert trust.verify_registry(trust.load_pubkeys(REPO), sig, roots, require_signature=True) == []
    rc = subprocess.run(
        [sys.executable, str(REPO / "tests/harness/check_agent_keys.py"), "--require-signed"],
        capture_output=True,
        check=False,
    ).returncode
    assert rc == 0


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_trust_ref_bootstrap_and_no_self_vouching(tmp_path):
    root = tmp_path
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    rp = root / trust.TRUST_ROOTS_RELPATH
    rp.parent.mkdir(parents=True)
    rp.write_text(json.dumps({"maintainers": {}}))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "branch", "base")
    _, pub_a = signing.generate_keypair()
    rp.write_text(json.dumps({"maintainers": {"a": pub_a}}))
    # bootstrap: base has no trust root, the tree's first root is used
    assert trust.read_roots(root, "base") == {"maintainers": {"a": pub_a}}
    _git(root, "commit", "-qam", "root a")
    _git(root, "branch", "-f", "base")
    _, pub_b = signing.generate_keypair()
    rp.write_text(json.dumps({"maintainers": {"b": pub_b}}))
    # once the ref has a root, a change cannot swap in its own
    assert trust.read_roots(root, "base") == {"maintainers": {"a": pub_a}}
