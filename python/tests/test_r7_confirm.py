"""tools/r7_confirm.py: synthetic data only, the look subprocess is faked.

Nothing here reads data/real, runs ``iap.research run`` or writes a ledger,
the blackboard or an outcome."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from iap.agents import signing
from iap.agents.blackboard import canonical

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rc = _load("r7_confirm", "tools/r7_confirm.py")
ro = _load("r7_record_outcome", "tools/r7_record_outcome.py")

DAY0 = np.datetime64("2025-12-08", "ns").astype(np.int64)
NS_DAY = 86_400 * 10**9
DAYS = ["2025-11-28", "2025-12-08", "2025-12-09", "2025-12-10"]


# ------------------------------------------------------------ fixtures --
def _ts(day: str, n: int) -> np.ndarray:
    start = np.datetime64(day, "ns").astype(np.int64) + 14 * 3600 * 10**9
    return start + np.arange(n, dtype=np.int64) * 10**9


def _rows(day_ics: dict[str, float], n: int = 2000, seed: int = 0):
    rng = np.random.default_rng(seed)
    ts, xs, ys = [], [], []
    for d, rho in day_ics.items():
        x = rng.standard_normal(n)
        y = rho * x + np.sqrt(1 - rho**2) * rng.standard_normal(n)
        ts.append(_ts(d, n))
        xs.append(x)
        ys.append(y)
    ts = np.concatenate(ts)
    return {
        "ts": ts,
        "x": np.concatenate(xs),
        "y": np.concatenate(ys),
        "unc": np.ones(ts.size, dtype=bool),
    }


class FakeModel:
    horizon = "1s"

    def universe(self, ids):
        return [i for i in ids if i in (1, 2)]

    def signals(self, frames):
        return {i: frames[i]["sig"] for i in frames}


def _frame(day: str, n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n)
    return pd.DataFrame(
        {
            "exchange_ts": _ts(day, n),
            "sig": x,
            "label_mid_1s": 0.3 * x + rng.standard_normal(n),
            "label_valid_1s": np.ones(n, dtype=bool),
            "spread_ticks_v1": np.where(np.arange(n) % 10 == 0, -1.0, 1.0),
        }
    )


# ---------------------------------------------------- per-session IC --
def test_per_session_ic_matches_pearson_per_day():
    rows = _rows({"2025-12-08": 0.2, "2025-12-09": 0.05})
    out = rc.per_session_ic(rows)
    assert sorted(out) == ["2025-12-08", "2025-12-09"]
    day = rc.session_day(rows["ts"])
    for d, v in out.items():
        m = day == d
        assert v == pytest.approx(np.corrcoef(rows["x"][m], rows["y"][m])[0, 1])
    assert out["2025-12-08"] > out["2025-12-09"]


def test_per_session_ic_drops_crossed_rows():
    rows = _rows({"2025-12-08": 0.2})
    rows["unc"][:1000] = False
    rows["y"][:1000] = -rows["x"][:1000]  # crossed rows anti-correlated: must not count
    v = rc.per_session_ic(rows)["2025-12-08"]
    assert v == pytest.approx(np.corrcoef(rows["x"][1000:], rows["y"][1000:])[0, 1])


def test_pooled_rows_uses_universe_labels_and_crossed_flag():
    frames = {
        i: pd.concat([_frame(d, 300, i * 10 + k) for k, d in enumerate(DAYS[:2])])
        for i in (1, 2, 3)
    }
    for f in frames.values():
        f.reset_index(drop=True, inplace=True)
    frames[1].loc[5, "label_valid_1s"] = False
    rows = rc.pooled_rows(frames, FakeModel(), "valid_only", single_venue=False)
    assert rows["x"].size == 1200  # instrument 3 is outside the universe
    assert np.isnan(rows["y"][5])  # invalid label is not scored
    assert (~rows["unc"]).sum() == 120  # every 10th row crossed
    sv = rc.pooled_rows(frames, FakeModel(), "valid_only", single_venue=True)
    assert sv["unc"].all()  # one venue's own book: nothing is "crossed"
    ics = rc.per_session_ic(rows)
    assert set(ics) == set(DAYS[:2]) and all(v > 0 for v in ics.values())


# ---------------------------------------------------------- gate/verdict --
def test_gate_requires_t_threshold_dsr_and_positive_ic():
    assert rc.gate(0.05, 6.0, 5.0, 0.99)["passed"]
    assert not rc.gate(0.05, 4.9, 5.0, 0.99)["passed"]
    assert not rc.gate(0.05, 6.0, 5.0, 0.95)["passed"]  # strictly > 0.95
    assert not rc.gate(0.05, 6.0, 5.0, None)["passed"]
    assert not rc.gate(-0.05, 6.0, 5.0, 0.99)["passed"]
    assert rc.verdict(True, rc.gate(0.05, 6.0, 5.0, 0.99)) == "confirmed"
    assert rc.verdict(True, rc.gate(0.05, 1.0, 5.0, 0.99)) == "rejected"
    assert rc.verdict(False, rc.gate(0.05, 6.0, 5.0, 0.99)) == "no_result"


def test_dsr_on_session_series_uses_n_trials():
    series = [0.07, 0.06, 0.08, 0.075, 0.065, 0.07, 0.072]
    a = rc.dsr_block(series, 1, None)
    b = rc.dsr_block(series, 500, 1000)
    assert a["n_days"] == 7 and b["n_trials"] == 500
    assert a["dsr"] >= b["dsr"]  # more trials deflate more
    assert b["dsr_at_looks"] <= b["dsr"]
    assert rc.dsr_block([0.1, None, 0.2], 3, None)["n_days"] == 2


def test_sensitivity_excludes_fomc_and_holiday_without_new_data():
    day_ics = {"2025-11-28": -0.3, "2025-12-08": 0.1, "2025-12-09": 0.12, "2025-12-10": -0.3}
    day_ics |= {"2025-12-11": 0.11, "2025-12-12": 0.09, "2026-06-12": 0.1}
    rows = _rows(day_ics, n=1500)
    ics = rc.per_session_ic(rows)
    s = rc.sensitivity(rows, ics, 10, 100, 3.0, "1s")
    full, excl = s["all_sessions"], s["excl_2025-12-10_2025-11-28"]
    assert len(full["sessions"]) == 7 and len(excl["sessions"]) == 5
    assert "2025-12-10" not in excl["per_session_ic"] and "2025-11-28" not in excl["sessions"]
    assert excl["mean_session_ic"] > full["mean_session_ic"]
    assert excl["n_pairs"] == 5 * 1500
    assert excl["raw_ic"] > full["raw_ic"]
    assert excl["deflated_sharpe"]["n_days"] == 5


# ------------------------------------------------------- dry-run checks --
def _make_root(tmp_path: Path, *, sessions=None, sign=True, tamper=False):
    root = tmp_path / "repo"
    (root / "research" / "r7").mkdir(parents=True)
    (root / "research" / "agents").mkdir(parents=True)
    (root / "configs" / "prereg").mkdir(parents=True)
    (root / "configs" / "prereg" / "v2.json").write_text("{}", encoding="ascii")
    priv, pub = signing.generate_keypair()
    (root / "research" / "agents" / "trust_roots.json").write_text(
        json.dumps({"maintainers": {"ashjha0": pub}}), encoding="ascii"
    )
    days = ["2025-11-28", "2025-12-08", "2025-12-09", "2025-12-10"]
    days += ["2025-12-11", "2025-12-12", "2026-06-12"]
    doc = {
        "signed": True,
        "on_blackboard": True,
        "open_for_owner": [],
        "symbols": ["AAPL", "MSFT", "QQQ"],
        "holdout_days": days,
        "fingerprints": {
            "algorithm": "sha256",
            "configs/prereg/v2.json": hashlib.sha256(b"{}").hexdigest(),
        },
        "decision": "a failure is final",
        "gate": "HAC t >= ledger threshold; DSR > 0.95",
        "registration": {
            "entries": [
                {"alpha_id": "EQ10", "prereg_id": "p10", "blackboard_seq": 7},
                {"alpha_id": "EQ05", "prereg_id": "p05", "blackboard_seq": 8},
            ]
        },
    }
    if sign:
        msg = canonical(doc).encode("ascii")
        sig = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv)).sign(msg).hex()
        doc["signature"] = {
            "scheme": "ed25519",
            "maintainer": "ashjha0",
            "public_key": pub,
            "message_sha256": hashlib.sha256(msg).hexdigest(),
            "sig": sig,
        }
    if tamper:
        doc["holdout_days"] = days[:-1] + ["2026-06-13"]
    p = root / rc.PREREG_REL
    p.write_text(json.dumps(doc), encoding="utf-8")
    (root / rc.DEVIATION_REL).write_text("note", encoding="utf-8")
    ok = root / "data" / "real" / "R7_SIGNED.ok"
    ok.parent.mkdir(parents=True)
    ok.write_text(
        f"﻿sha=x\nr7_confirm_prereg.json={hashlib.sha256(p.read_bytes()).hexdigest()}\n",
        encoding="utf-8",
    )
    ds = root / "data" / "real" / "r7_holdout"
    (ds / "features").mkdir(parents=True)
    (ds / "configs" / "instruments").mkdir(parents=True)
    (ds / "configs" / "instruments" / "instruments.json").write_text("{}")
    for f in rc.EXPECTED_FEATURE_FILES:
        (ds / "features" / f).write_text("x")
    manifest = {
        "dataset_version": rc.EXPECTED_DATASET_VERSION,
        "sessions": {d: {} for d in (sessions or days)},
        "universe": [{"symbol": s} for s in ("AAPL", "MSFT", "QQQ")],
    }
    (ds / "dataset.json").write_text(json.dumps(manifest))
    return root, ds, ok


@pytest.fixture
def no_board(monkeypatch):
    """The blackboard gate and ledgers are out of scope here."""

    def gate(root, doc):
        return {
            "EQ10": {"seq": 7, "body": {"prereg_id": "p10"}},
            "EQ05": {"seq": 8, "body": {"prereg_id": "p05"}},
        }

    monkeypatch.setattr(rc, "verify_gate", gate)
    monkeypatch.setattr(
        rc,
        "ledger_thresholds",
        lambda root, ds: {
            "repo_ledger_total": 5360,
            "repo_ledger_t": 4.9,
            "dataset_ledger_total": None,
            "dataset_ledger_t": None,
        },
    )


def test_dry_run_passes_on_a_consistent_fixture(tmp_path, no_board, capsys):
    root, ds, ok = _make_root(tmp_path)
    rc_ = rc.main(
        ["--dry-run", "--root", str(root), "--dataset-dir", str(ds), "--signed-ok", str(ok)]
    )
    out = capsys.readouterr().out
    assert rc_ == 0 and "DRY RUN OK" in out and "--i-am-the-owner" in out


@pytest.mark.parametrize(
    "mutate, needle",
    [
        ("unsigned", "signature"),
        ("tampered", "sha256 mismatch"),
        ("sessions", "!= prereg holdout_days"),
        ("features", "missing"),
        ("signed_ok", "R7_SIGNED.ok"),
        ("deviation", "DEVIATION"),
        ("prior_look", "already taken"),
        ("dataset_version", "dataset_version"),
    ],
)
def test_dry_run_failure_modes(tmp_path, no_board, capsys, mutate, needle):
    kw = {}
    if mutate == "unsigned":
        kw["sign"] = False
    if mutate == "tampered":
        kw["tamper"] = True
    if mutate == "sessions":
        kw["sessions"] = ["2025-12-08"]
    root, ds, ok = _make_root(tmp_path, **kw)
    if mutate == "tampered":
        ok.write_text("r7_confirm_prereg.json=" + rc.sha256(root / rc.PREREG_REL), encoding="utf-8")
    if mutate == "features":
        (ds / "features" / "features_2.parquet").unlink()
    if mutate == "signed_ok":
        ok.unlink()
    if mutate == "deviation":
        (root / rc.DEVIATION_REL).unlink()
    if mutate == "prior_look":
        rc.look_marker(ds, "EQ05").parent.mkdir(parents=True, exist_ok=True)
        rc.look_marker(ds, "EQ05").write_text("t")
    if mutate == "dataset_version":
        m = json.loads((ds / "dataset.json").read_text())
        m["dataset_version"] = "0" * 64
        (ds / "dataset.json").write_text(json.dumps(m))
    code = rc.main(
        ["--dry-run", "--root", str(root), "--dataset-dir", str(ds), "--signed-ok", str(ok)]
    )
    assert code == 2
    assert needle in capsys.readouterr().err


def test_mode_is_required():
    with pytest.raises(SystemExit):
        rc.main([])


# ---------------------------------------------------------------- live --
class FakeRun:
    def __init__(self, ds: Path, codes: dict[str, int]):
        self.ds, self.codes, self.calls = ds, codes, []

    def __call__(self, cmd, **kw):
        alpha = cmd[cmd.index("--alpha") + 1]
        self.calls.append(cmd)
        code = self.codes[alpha]
        exp = f"exp{alpha}"
        if code in (0, 3):
            d = self.ds / "research" / "experiments" / exp
            d.mkdir(parents=True)
            (d / "result.json").write_text(json.dumps({"ic": 0.07, "t_stat": 9.0}))
            (d / "eligibility.json").write_text(
                json.dumps({"significance_threshold": 4.0, "threshold_looks": 90})
            )
            (d / "spec.json").write_text(json.dumps({"alpha_id": alpha, "horizon": "1s"}))
        return types.SimpleNamespace(returncode=code, stdout=f"experiment  {exp}\n", stderr="")


def test_live_aborts_before_any_look_when_a_precondition_fails(tmp_path, no_board):
    root, ds, ok = _make_root(tmp_path, sign=False)
    run = FakeRun(ds, {"EQ10": 0, "EQ05": 0})
    with pytest.raises(rc.CheckError):
        rc.live(root, ds, ok, runner=run)
    assert run.calls == []
    assert not (root / rc.RESULT_REL).exists()


def test_live_stops_after_failed_eq10_look_and_never_retries(tmp_path, no_board):
    root, ds, ok = _make_root(tmp_path)
    run = FakeRun(ds, {"EQ10": 1, "EQ05": 0})
    assert rc.live(root, ds, ok, runner=run, frames_loader=lambda d: {}) == 1
    assert [c[c.index("--alpha") + 1] for c in run.calls] == ["EQ10"]
    doc = json.loads((root / rc.RESULT_REL).read_text())
    assert [r["verdict"] for r in doc["results"]] == ["no_result"]
    assert doc["record_commands"] == [] and doc["outcome_recorded"] is False
    # a second invocation refuses: the look was taken
    with pytest.raises(rc.CheckError, match="already taken"):
        rc.live(root, ds, ok, runner=run)
    assert len(run.calls) == 1


def test_live_runs_each_look_once_and_writes_result(tmp_path, no_board, monkeypatch):
    root, ds, ok = _make_root(tmp_path)
    run = FakeRun(ds, {"EQ10": 0, "EQ05": 1})
    seen = []

    def fake_analyse(alpha, look, frames, ds_, thr, entry):
        seen.append(alpha)
        return {
            "alpha": f"{alpha}@1s",
            "prereg_id": entry["body"]["prereg_id"],
            "experiment_id": look["experiment_id"],
            "result": look["result"],
            "verdict": "confirmed",
        }

    monkeypatch.setattr(rc, "analyse", fake_analyse)
    assert rc.live(root, ds, ok, runner=run, frames_loader=lambda d: {}) == 1
    cmds = [" ".join(c[1:]) for c in run.calls]
    assert cmds == [
        f"-m iap.research run --alpha {a} --horizon 1s --methods v4 --dataset-dir {ds}"
        for a in ("EQ10", "EQ05")
    ]
    assert seen == ["EQ10"]
    doc = json.loads((root / rc.RESULT_REL).read_text())
    assert doc["dataset_version"] == rc.EXPECTED_DATASET_VERSION
    assert doc["results"][0]["experiment_id"] == "expEQ10"
    assert doc["results"][1]["verdict"] == "no_result"
    assert len(doc["record_commands"]) == 1 and "--prereg-id p10" in doc["record_commands"][0]
    assert (root / rc.RESULT_MD_REL).is_file()


def test_record_outcome_refuses_a_verdict_other_than_the_computed_one(tmp_path):
    (tmp_path / "research" / "r7").mkdir(parents=True)
    (tmp_path / ro.RESULT_REL).write_text(
        json.dumps({"results": [{"prereg_id": "p10", "verdict": "rejected"}]})
    )
    with pytest.raises(SystemExit, match="computed verdict rejected"):
        ro.main(
            [
                "--i-am-the-owner",
                "--prereg-id",
                "p10",
                "--outcome",
                "confirmed",
                "--ref",
                "x",
                "--root",
                str(tmp_path),
            ]
        )


# ------------------------------------------------- owner-approved crash retry --
def _crash(tmp_path):
    root, ds, ok = _make_root(tmp_path)
    run = FakeRun(ds, {"EQ10": 1, "EQ05": 0})
    assert rc.live(root, ds, ok, runner=run, frames_loader=lambda d: {}) == 1
    return root, ds, ok


def test_crash_retry_needs_the_note_and_runs_once(tmp_path, no_board, monkeypatch):
    root, ds, ok = _crash(tmp_path)
    run = FakeRun(ds, {"EQ10": 0, "EQ05": 0})
    with pytest.raises(rc.CheckError, match="crash_retry.md missing"):
        rc.live(root, ds, ok, runner=run, retry=True)
    assert run.calls == []
    (root / rc.RETRY_NOTE_REL).write_text("note", encoding="utf-8")
    monkeypatch.setattr(
        rc,
        "analyse",
        lambda a, look, *x: {
            "alpha": f"{a}@1s",
            "prereg_id": x[-1]["body"]["prereg_id"],
            "experiment_id": look["experiment_id"],
            "verdict": "confirmed",
        },
    )
    assert rc.live(root, ds, ok, runner=run, frames_loader=lambda d: {}, retry=True) == 0
    assert [c[c.index("--alpha") + 1] for c in run.calls] == ["EQ10", "EQ05"]
    assert (root / "research" / "r7" / "r7_confirm_result_crash1.json").is_file()
    assert rc.retry_marker(ds, "EQ10").exists()
    with pytest.raises(rc.CheckError):  # never a second retry
        rc.live(root, ds, ok, runner=run, retry=True)
    assert len(run.calls) == 2


def test_crash_retry_refused_when_the_look_produced_output(tmp_path, no_board):
    root, ds, ok = _crash(tmp_path)
    (root / rc.RETRY_NOTE_REL).write_text("note", encoding="utf-8")
    (ds / "research" / "experiments.json").write_text("{}")
    with pytest.raises(rc.CheckError, match="debited"):
        rc.preflight(root, ds, ok, retry=True)
    (ds / "research" / "experiments.json").unlink()
    d = ds / "research" / "experiments" / "x"
    d.mkdir(parents=True)
    (d / "spec.json").write_text("{}")
    with pytest.raises(rc.CheckError, match="produced output"):
        rc.preflight(root, ds, ok, retry=True)
