"""Track C forward shadow book (iap.paper.shadow_book) on synthetic sessions only."""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from iap.paper import shadow_book as sb

FROZEN = "ab" * 32
NS = 10**9
T0 = 1_700_000_000 * NS
STEP = 600 * NS
SESSION = 39 * STEP
CAL = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]


def _cfg(calendar, T_min=3, m_min=2, grid=4) -> sb.ShadowConfig:
    return sb.ShadowConfig(
        frozen_configuration_sha256=FROZEN,
        capital=1_000_000.0,
        calendar=tuple(calendar),
        T_min=T_min,
        m_min=m_min,
        grid_points=grid,
        min_time_coverage=0.05,
        min_grid_coverage=0.5,
        participation_cap=0.05,
        min_ticket=1,
        forbidden_datasets=("holdout2026", "*sealed*"),
    )


def _market(ts: int, mid: float, gap: bool = False) -> dict:
    q = None
    if not gap:
        q = {"ts": ts, "bid": mid - 0.01, "ask": mid + 0.01, "bid_size": 5000, "ask_size": 5000}
    return {"quote": q, "volume_5m": 200_000}


def _write(inbox: Path, session: str, i: int, *, gaps: int = 0, frozen: str = FROZEN) -> Path:
    """Four decisions: long AAA / short BBB, flat at the last one (flat at close)."""
    decisions = []
    for k in range(4):
        ts = T0 + i * 86_400 * NS + k * STEP
        mids = {"AAA": 100.0 + i + 0.1 * k, "BBB": 50.0 - 0.05 * k}
        targets = {"AAA": 0.10, "BBB": -0.05} if k < 3 else {}
        markets = {n: _market(ts + 1, m, gap=k < gaps) for n, m in mids.items()}
        decisions.append({"ts": ts, "targets": targets, "mids": mids, "markets": markets})
    doc = {
        "session": session,
        "frozen_configuration_sha256": frozen,
        "step_ns": STEP,
        "session_ns": SESSION,
        "decisions": decisions,
        "close_mids": decisions[-1]["mids"],
    }
    inbox.mkdir(parents=True, exist_ok=True)
    p = inbox / f"{session}.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


@pytest.fixture
def book(tmp_path):
    inbox, out = tmp_path / "inbox", tmp_path / "book"
    for i, s in enumerate(CAL):
        _write(inbox, s, i)
    return inbox, out, _cfg(CAL)


def test_rows_are_hash_chained_and_tamper_evident(book):
    inbox, out, cfg = book
    for s in CAL[:2]:
        assert sb.run_session(s, inbox, out, cfg)[0] == "recorded"
    rows = sb.verify_chain(out)
    assert [r["seq"] for r in rows] == [0, 1]
    assert rows[0]["prev_sha256"] == sb.GENESIS
    assert rows[1]["prev_sha256"] == rows[0]["row_sha256"]
    chain = out / "chain.jsonl"
    good = chain.read_text(encoding="utf-8")
    lines = good.splitlines()
    # an edited row body breaks its hash
    chain.write_text(good.replace('"usable":true', '"usable":false', 1), encoding="utf-8")
    with pytest.raises(sb.ShadowBookError, match="chain broken"):
        sb.verify_chain(out)
    # a deleted first row breaks the links
    chain.write_text(lines[1] + "\n", encoding="utf-8")
    with pytest.raises(sb.ShadowBookError, match="chain broken"):
        sb.verify_chain(out)
    # an edited sealed record is caught through its hash in the chain
    chain.write_text(good, encoding="utf-8")
    sealed = out / "sealed" / f"{CAL[0]}.json"
    sealed.write_text(sealed.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(sb.ShadowBookError, match="altered"):
        sb.verify_chain(out)


def test_missing_session_is_refused_not_skipped(book):
    inbox, out, cfg = book
    sb.run_session(CAL[0], inbox, out, cfg)
    (inbox / f"{CAL[1]}.json").unlink()
    with pytest.raises(sb.ShadowBookError, match="missing"):
        sb.run_session(CAL[1], inbox, out, cfg)
    with pytest.raises(sb.ShadowBookError, match="out of order"):
        sb.run_session(CAL[2], inbox, out, cfg)
    assert len(sb.verify_chain(out)) == 1
    # recorded on purpose with a reason: attempted, not usable; then the next session proceeds
    with pytest.raises(sb.ShadowBookError, match="reason"):
        sb.mark_missing(CAL[1], " ", out, cfg)
    assert sb.mark_missing(CAL[1], "feed outage", out, cfg)["usable"] is False
    assert sb.run_session(CAL[2], inbox, out, cfg)[0] == "recorded"
    st = sb.status(out, cfg)
    assert (st["attempted"], st["usable"], st["dropped"]) == (3, 2, 1)


def test_rerun_of_a_day_is_idempotent_and_changed_input_refused(book):
    inbox, out, cfg = book
    first = sb.run_session(CAL[0], inbox, out, cfg)
    before = (out / "chain.jsonl").read_bytes()
    assert sb.run_session(CAL[0], inbox, out, cfg) == ("already recorded", first[1])
    assert (out / "chain.jsonl").read_bytes() == before
    _write(inbox, CAL[0], 7)
    with pytest.raises(sb.ShadowBookError, match="different input"):
        sb.run_session(CAL[0], inbox, out, cfg)


def test_status_rows_and_cli_show_counts_only(book, capsys):
    inbox, out, cfg = book
    _, row = sb.run_session(CAL[0], inbox, out, cfg)
    for blob in (row, sb.status(out, cfg), sb.verify_chain(out)):
        text = re.sub("[0-9a-f]{64}", "", json.dumps(blob))  # hashes may spell words
        for word in ("pnl", "return", "price", "cash", "positions", "spread", "impact", "fee"):
            assert word not in text, word
    (out.parent / "cal.json").write_text(json.dumps({"sessions": CAL}), encoding="utf-8")
    d = {k: getattr(cfg, k) for k in sb.ShadowConfig.__slots__}
    d["calendar"], d["forbidden_datasets"] = "cal.json", list(cfg.forbidden_datasets)
    cfg_path = out.parent / "cfg.json"
    cfg_path.write_text(json.dumps(d), encoding="utf-8")
    capsys.readouterr()
    assert (
        sb.main(["run", "--config", str(cfg_path), "--inbox", str(inbox), "--out", str(out)]) == 0
    )
    assert sb.main(["status", "--config", str(cfg_path), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "pnl" not in printed and "return" not in printed
    assert '"attempted":2' in printed
    (inbox / f"{CAL[2]}.json").unlink()
    assert (
        sb.main(["run", "--config", str(cfg_path), "--inbox", str(inbox), "--out", str(out)]) == 2
    )


def test_single_read_runner_only_after_completion(book):
    inbox, out, cfg = book
    for s in CAL[:2]:
        sb.run_session(s, inbox, out, cfg)
    with pytest.raises(sb.ShadowBookError, match="accruing"):
        sb.read_series(out, cfg, sb.RUNNER_IDENTITY)
    sb.run_session(CAL[2], inbox, out, cfg)
    assert sb.status(out, cfg)["state"] == "complete"
    with pytest.raises(sb.ShadowBookError, match="window is complete"):
        sb.run_session(CAL[3], inbox, out, cfg)
    with pytest.raises(sb.ShadowBookError, match="counts only"):
        sb.read_series(out, cfg, "dev")
    doc = sb.read_series(out, cfg, sb.RUNNER_IDENTITY)
    assert [r["session"] for r in doc["series"]] == CAL[:3]
    s0 = doc["series"][0]
    assert set(s0) == {"session", "base", "conservative", "stress"}
    assert s0["base"] >= s0["conservative"] >= s0["stress"]  # same orders, rising cost
    assert sb.verify_chain(out)[-1]["kind"] == "read"
    with pytest.raises(sb.ShadowBookError, match="already been read"):
        sb.read_series(out, cfg, sb.RUNNER_IDENTITY)


def test_coverage_rule_drops_a_session(tmp_path):
    inbox, out = tmp_path / "inbox", tmp_path / "book"
    _write(inbox, CAL[0], 0, gaps=3)  # three of four decisions have no quote
    _, row = sb.run_session(CAL[0], inbox, out, _cfg(CAL))
    assert row["usable"] is False and row["usable_units"] == 1
    assert any("m_min" in r for r in row["drop_reasons"])


def test_pending_when_cap_reached_first(tmp_path):
    out = tmp_path / "book"
    cfg = _cfg(CAL, T_min=3)
    for s in CAL[:3]:
        sb.mark_missing(s, "synthetic outage", out, cfg)
    assert sb.status(out, cfg)["state"] == "pending"  # cap floor(1.25 * 3) = 3
    assert sb.accrual_state(248, 300, 250) == "accruing"
    assert sb.accrual_state(250, 305, 250) == "complete"
    assert sb.accrual_state(249, 312, 250) == "pending"


def test_refusals_frozen_hash_sealed_inputs_calendar(tmp_path):
    out = tmp_path / "book"
    _write(tmp_path / "inbox", CAL[0], 0, frozen="cd" * 32)
    with pytest.raises(sb.ShadowBookError, match="frozen configuration"):
        sb.run_session(CAL[0], tmp_path / "inbox", out, _cfg(CAL))
    sealed_inbox = tmp_path / "holdout2026"
    _write(sealed_inbox, CAL[0], 0)
    with pytest.raises(sb.ShadowBookError, match="forbidden"):
        sb.run_session(CAL[0], sealed_inbox, out, _cfg(CAL))
    with pytest.raises(sb.ShadowBookError, match="calendar"):
        sb.run_session("2026-02-02", tmp_path / "inbox", out, _cfg(CAL))
    assert not (out / "chain.jsonl").exists()


def test_never_places_a_real_order(book, monkeypatch):
    mods: set[str] = set()
    for node in ast.walk(ast.parse(Path(sb.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    allowed = {
        "__future__", "argparse", "datetime", "fnmatch", "hashlib", "json", "math", "sys",
        "collections.abc", "dataclasses", "pathlib", "typing",
        "iap.contracts.versions", "iap.core", "iap.execution.sim",
    }  # fmt: skip
    assert mods <= allowed, mods - allowed
    inbox, out, cfg = book
    monkeypatch.setitem(sys.modules, "ib_insync", object())
    with pytest.raises(sb.ShadowBookError, match="broker"):
        sb.run_session(CAL[0], inbox, out, cfg)


def test_shipped_config_template_refuses_until_registered():
    with pytest.raises(sb.ShadowBookError, match="calendar"):
        sb.ShadowConfig.load(REPO_ROOT / "configs" / "paper" / "shadow_book.json")
