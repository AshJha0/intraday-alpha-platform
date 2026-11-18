"""The one-look holdout runner on the bars path: every refusal, the one-look
rule and a full run end to end, all on synthetic files with injected fakes
(no download, no market data)."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from iap.agents.planted_dry_run import _check_report
from iap.governance import holdout_registry as hr
from iap.governance import signing
from iap.governance.blackboard import canonical
from iap.research import breadth_holdout as bh

REL = "research/preregs/bh_prereg.json"
THREE = ["AAPL", "MSFT", "QQQ"]
ENV = {"IAP_RUN_ACTOR": "runner-1"}


def _ok_identity(*_a, **_k):
    return {"legacy": False}


def _bad_identity(*_a, **_k):
    raise bh.IdentityError("analysis identity mismatch: code tree differs")


def _raw_bytes(date: str) -> bytes:
    return f"synthetic raw {date}".encode()


def _registry(root: Path, reserved: str = REL, state: str = "unread") -> None:
    sessions = {
        d: [
            {"symbols": THREE, "state": "spent", "spent_by": ["r7"]},
            {"symbols": "*", "state": state, "reserved_for": [reserved]},
        ]
        for d in bh.HOLDOUT_SESSIONS
    }
    doc = hr.seal({"schema": hr.SCHEMA, "x-version": 1, "datasets": {}, "sessions": sessions})
    (root / hr.REGISTRY_REL).write_text(hr.render(doc), encoding="utf-8")


def _doc(root: Path) -> dict:
    uni = root / "research/round1/universe.json"
    return {
        "schema": "iap.prereg_draft/v1",
        "kind": bh.KIND,
        "prereg_id": REL,
        "status": "SIGNED",
        "holdout": {
            "sessions": list(bh.HOLDOUT_SESSIONS),
            "raw_files": [
                {
                    "date": d,
                    "file": f"raw_{d}.gz",
                    "url": "https://emi.nasdaq.com/x",
                    "bytes": len(_raw_bytes(d)),
                    "sha256": hashlib.sha256(_raw_bytes(d)).hexdigest(),
                }
                for d in bh.HOLDOUT_SESSIONS
            ],
        },
        "universe": {
            "path": "research/round1/universe.json",
            "sha256": bh.file_sha256(uni),
            "excluded_symbols": THREE,
        },
        "family": {"name": "fam_x", "parameters": {"lookback": 15}},
        "statistic": {"primary": bh.STATISTIC},
        "decision": {"alpha_one_sided": 0.025, "min_sessions": 9},
        "bars_tool": {
            "path": bh.BARS_TOOL_REL,
            "commit": "abc",
            "tree_sha256": bh.tree_sha256(root, bh.BARS_TOOL_REL),
        },
        "analysis_identity": {"family": "fam_x", "identity_version": 2},
    }


def _sign(root: Path, doc: dict, priv: Ed25519PrivateKey | None = None) -> None:
    key = priv or _KEY
    doc = {k: v for k, v in doc.items() if k != "signature"}
    doc["signed"] = True
    msg = canonical(doc).encode("ascii")
    pub = key.public_key().public_bytes_raw().hex()
    doc["signature"] = {
        "scheme": "ed25519",
        "maintainer": "owner",
        "public_key": pub,
        "message_sha256": hashlib.sha256(msg).hexdigest(),
        "sig": key.sign(msg).hex(),
    }
    (root / REL).write_text(json.dumps(doc), encoding="utf-8")


_KEY = Ed25519PrivateKey.generate()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "research/agents").mkdir(parents=True)
    (tmp_path / "research/preregs").mkdir(parents=True)
    (tmp_path / "research/round1").mkdir(parents=True)
    (tmp_path / "rust/itchbars/src").mkdir(parents=True)
    (tmp_path / "rust/itchbars/src/main.rs").write_text("fn main() {}\n")
    pub = _KEY.public_key().public_bytes_raw().hex()
    (tmp_path / "research/agents/trust_roots.json").write_text(
        json.dumps({"maintainers": {"owner": pub}})
    )
    (tmp_path / "research/round1/universe.json").write_text(
        json.dumps({"symbols": ["AAPL", "AMD", "MSFT", "NVDA", "QQQ", "TSLA"]})
    )
    _registry(tmp_path)
    _sign(tmp_path, _doc(tmp_path))
    return tmp_path


def _pre(root: Path, **kw):
    kw.setdefault("env", ENV)
    kw.setdefault("check_identity", _ok_identity)
    return bh.preflight(root, Path(REL), **kw)


def test_preflight_passes_on_a_consistent_registration(repo):
    plan = _pre(repo)
    assert plan.symbols == ["AMD", "NVDA", "TSLA"]
    assert plan.excluded == THREE


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda d: d.update(kind="other"), "kind"),
        (lambda d: d["holdout"].update(sessions=list(bh.HOLDOUT_SESSIONS)[:8]), "exactly"),
        (lambda d: d["holdout"]["raw_files"].pop(), "raw_files"),
        (lambda d: d["universe"].update(sha256="0" * 64), "universe file"),
        (lambda d: d["family"].update(name="<FILL: family>"), "placeholders"),
        (lambda d: d["family"].pop("parameters"), "family.parameters"),
        (lambda d: d["statistic"].update(primary="ic"), "statistic.primary"),
        (lambda d: d["decision"].update(alpha_one_sided=0.7), "alpha_one_sided"),
        (lambda d: d["decision"].update(min_sessions=12), "min_sessions"),
        (lambda d: d["bars_tool"].update(tree_sha256="0" * 64), "bars_tool"),
        (lambda d: d.pop("analysis_identity"), "analysis_identity"),
        (lambda d: d.update(prereg_id="elsewhere.json"), "prereg_id"),
    ],
)
def test_each_missing_or_mismatched_field_refuses(repo, mutate, match):
    doc = _doc(repo)
    mutate(doc)
    _sign(repo, doc)
    with pytest.raises(bh.LookRefused, match=match):
        _pre(repo)


def test_missing_file_refuses(repo):
    (repo / REL).unlink()
    with pytest.raises(bh.LookRefused, match="does not exist"):
        _pre(repo)


def test_unsigned_draft_refuses(repo):
    doc = _doc(repo)
    doc["signed"] = False
    (repo / REL).write_text(json.dumps(doc))
    with pytest.raises(bh.LookRefused, match="not signed"):
        _pre(repo)


def test_edit_after_signing_refuses(repo):
    doc = json.loads((repo / REL).read_text())
    doc["decision"]["alpha_one_sided"] = 0.1
    (repo / REL).write_text(json.dumps(doc))
    with pytest.raises(bh.LookRefused, match="changed after signing"):
        _pre(repo)


def test_key_outside_the_trust_root_refuses(repo):
    _sign(repo, _doc(repo), Ed25519PrivateKey.generate())
    with pytest.raises(bh.LookRefused, match="trust-root"):
        _pre(repo)


def test_changed_universe_file_refuses(repo):
    (repo / "research/round1/universe.json").write_text(json.dumps({"symbols": ["AMD"]}))
    with pytest.raises(bh.LookRefused, match="universe file"):
        _pre(repo)


def test_changed_bars_tool_refuses(repo):
    (repo / "rust/itchbars/src/main.rs").write_text("fn main() { changed }\n")
    with pytest.raises(bh.LookRefused, match="bars_tool"):
        _pre(repo)


def test_identity_mismatch_refuses(repo):
    with pytest.raises(bh.LookRefused, match="identity mismatch"):
        _pre(repo, check_identity=_bad_identity)


def test_identity_check_receives_the_registered_values(repo):
    seen = {}

    def spy(root, block, **kw):
        seen.update(kw)

    _pre(repo, check_identity=spy)
    assert seen["family"] == "fam_x" and seen["metrics"] == [bh.STATISTIC]
    assert seen["thresholds"] == {"alpha_one_sided": 0.025, "min_sessions": 9}
    assert len(seen["data_versions"]) == 9


def test_runner_must_not_be_the_signer(repo):
    with pytest.raises(bh.LookRefused, match="signer"):
        _pre(repo, env={"IAP_RUN_ACTOR": "owner"})
    with pytest.raises(bh.LookRefused, match="IAP_RUN_ACTOR"):
        _pre(repo, env={})
    with pytest.raises(bh.LookRefused, match="key material"):
        _pre(repo, env={**ENV, "IAP_MAINTAINER_KEY": "x"})


def test_unknown_family_refuses(repo):
    with pytest.raises(bh.LookRefused, match="no implementation"):
        _pre(repo, family_known=lambda f: False)


def test_reservation_for_another_prereg_refuses(repo):
    _registry(repo, reserved="research/preregs/breadth_book_prereg.json")
    with pytest.raises(bh.LookRefused, match="reserved_for"):
        _pre(repo)


def test_spent_sessions_refuse(repo):
    _registry(repo, state="spent")
    with pytest.raises(bh.LookRefused, match="not unread"):
        _pre(repo)


def test_exclusion_must_match_the_registry_spent_names(repo):
    doc = _doc(repo)
    doc["universe"]["excluded_symbols"] = ["AAPL"]
    _sign(repo, doc)
    with pytest.raises(bh.LookRefused, match="spent symbols"):
        _pre(repo)


def test_resealed_by_hand_registry_refuses(repo):
    p = repo / hr.REGISTRY_REL
    p.write_text(p.read_text().replace('"unread"', '"in_sample"'))
    with pytest.raises(bh.LookRefused, match="body_sha256"):
        _pre(repo)


class Fakes:
    def __init__(self, values: dict[str, float], fail: str | None = None) -> None:
        self.values, self.fail, self.calls = values, fail, []

    def download(self, rf, vendor_dir: Path) -> Path:
        self.calls.append(("download", rf["date"]))
        vendor_dir.mkdir(parents=True, exist_ok=True)
        p = vendor_dir / rf["file"]
        p.write_bytes(_raw_bytes(rf["date"]) if rf["date"] != self.fail else b"corrupt")
        return p

    def build(self, raw: Path, out: Path) -> dict:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("bars\n")
        return {"ok": True, "messages": 1}

    def study(self, bars: Path, symbols, family, params) -> dict:
        d = bars.stem[-8:]
        return {"value": self.values[f"{d[:4]}-{d[4:6]}-{d[6:]}"], "names": len(symbols)}

    def hooks(self) -> bh.Hooks:
        return bh.Hooks(self.download, self.build, self.study)


def _run(repo: Path, fakes: Fakes, tmp_path: Path) -> dict:
    plan = _pre(repo)
    return bh.run_look(
        repo,
        plan,
        fakes.hooks(),
        vendor_dir=tmp_path / "vendor",
        bars_dir=tmp_path / "bars",
        log_path=tmp_path / "access.jsonl",
    )


def test_full_fake_run_end_to_end_then_one_look_only(repo, tmp_path):
    vals = {d: 3.0 + 0.1 * i for i, d in enumerate(bh.HOLDOUT_SESSIONS)}
    fakes = Fakes(vals)
    res = _run(repo, fakes, tmp_path)
    assert res["verdict"] == "pass" and res["sessions_used"] == 9
    assert res["universe_names_used"] == 3
    assert not list((tmp_path / "vendor").iterdir())  # every raw file deleted
    assert all(r["raw_deleted"] for r in res["sessions"].values())
    result, marker = bh.result_paths(repo, REL)
    assert json.loads(result.read_text())["verdict"] == "pass" and marker.is_file()
    reg = hr.load_registry(None, repo)
    for d in bh.HOLDOUT_SESSIONS:
        star = reg["sessions"][d][1]
        assert star["state"] == "spent" and star["spent_by"] == [REL]
    assert hr.verify_access_log(tmp_path / "access.jsonl") == []
    # a second invocation refuses and names the existing file, before any hook
    with pytest.raises(bh.LookRefused, match="one look only.*RESULT"):
        _pre(repo)


def test_a_started_but_unfinished_look_is_spent(repo, tmp_path):
    _, marker = bh.result_paths(repo, REL)
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    with pytest.raises(bh.LookRefused, match="one look only.*STARTED"):
        _pre(repo)


def test_bad_raw_hash_makes_the_session_missing_and_the_verdict_incomplete(repo, tmp_path):
    vals = dict.fromkeys(bh.HOLDOUT_SESSIONS, 3.0)
    res = _run(repo, Fakes(vals, fail="2025-12-09"), tmp_path)
    assert res["verdict"] == "incomplete" and res["sessions_used"] == 8
    assert "sha256" in res["sessions"]["2025-12-09"]["error"]
    assert not list((tmp_path / "vendor").iterdir())


def test_null_run_fails(repo, tmp_path):
    vals = {d: (-1) ** i * 1.0 for i, d in enumerate(bh.HOLDOUT_SESSIONS)}
    assert _run(repo, Fakes(vals), tmp_path)["verdict"] == "fail"


def test_verdict_rule():
    nine = dict.fromkeys(bh.HOLDOUT_SESSIONS, 0.0)
    assert bh.verdict({**nine, "2025-11-28": math.nan}, 0.025, 9)["verdict"] == "incomplete"
    pos = {d: 2.0 + 0.1 * i for i, d in enumerate(bh.HOLDOUT_SESSIONS)}
    assert bh.verdict(pos, 0.025, 9)["verdict"] == "pass"
    assert bh.verdict({d: -v for d, v in pos.items()}, 0.025, 9)["verdict"] == "fail"


def test_planted_dry_run_demonstrates_every_verdict():
    report = bh.planted_dry_run(0.025, 9)
    _check_report(report, "planted.json")  # the signer's R4 check
    assert sorted({d["observed"] for d in report["demonstrations"]}) == sorted(bh.VERDICTS)
    json.dumps(report, allow_nan=False)


def test_signing_module_matches_the_test_signer():
    pub = _KEY.public_key().public_bytes_raw().hex()
    assert signing.verify_ed25519(pub, b"m", _KEY.sign(b"m").hex())
