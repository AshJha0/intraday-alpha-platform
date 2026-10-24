"""R4 (v1.19 / v2.0 stage 1): planted dry run before signature (defect D-09).

Unit tests of :mod:`iap.agents.planted_dry_run` and the three regression
fixtures the specification names: the REL-08 C1, REL-08c n = 0 and A0
signatures of v1.18, rebuilt from their committed historical result files,
must be REFUSED by the dry-run check; a complete planted dry run passes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from iap.agents import planted_dry_run as pdr

REPO = Path(__file__).resolve().parents[2]

A0_TOLERANCE = 55  # null protocol v3 section 6: A0 exact count <= 55 of 1,854 (option A)


def _write_report(root: Path, rel: str, report: dict) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return rel


def _report(values, demonstrations, **extra) -> dict:
    return {
        "kind": pdr.DRY_RUN_KIND,
        "rule": "test rule",
        "verdict_values": list(values),
        "demonstrations": list(demonstrations),
        "statistics_planted": True,
        "confirmation_data_read": False,
        **extra,
    }


def _dem(observed: str, **planted) -> dict:
    return {"planted": {"input": f"planted for {observed}", **planted}, "observed": observed}


COMPLETE = _report(
    ["continue", "stop", "underpowered"],
    [
        _dem("continue", effect_bps=5.0, observations=74),
        _dem("stop", effect_bps=0.0, observations=74),
        _dem("underpowered", effect_bps=5.0, observations=4),
    ],
)


# --------------------------------------------------------------- unit tests --
def test_complete_dry_run_builds_a_block_and_signs(tmp_path):
    rel = _write_report(tmp_path, "research/x/dryrun.json", COMPLETE)
    block = pdr.build_dry_run_block(tmp_path, rel)
    assert block["path"] == rel
    assert block["sha256"] == hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    assert block["verdict_values"] == ["continue", "stop", "underpowered"]
    assert block["demonstrated"] == {"continue": 1, "stop": 1, "underpowered": 1}
    assert pdr.require_dry_run(tmp_path, {"dry_run": block}) == block


def test_missing_block_is_refused(tmp_path):
    with pytest.raises(pdr.DryRunRefused, match="no dry_run block"):
        pdr.require_dry_run(tmp_path, {"signed": False})


def test_altered_report_is_refused(tmp_path):
    rel = _write_report(tmp_path, "r/d.json", COMPLETE)
    block = pdr.build_dry_run_block(tmp_path, rel)
    (tmp_path / rel).write_text(json.dumps(COMPLETE) + "\n", encoding="utf-8")  # re-rendered
    with pytest.raises(pdr.DryRunRefused, match="changed since it was bound"):
        pdr.require_dry_run(tmp_path, {"dry_run": block})
    (tmp_path / rel).unlink()
    with pytest.raises(pdr.DryRunRefused, match="does not exist"):
        pdr.require_dry_run(tmp_path, {"dry_run": block})


def test_undemonstrated_verdict_value_is_refused(tmp_path):
    rep = _report(["continue", "stop"], [_dem("stop", observations=10)])
    rel = _write_report(tmp_path, "r/d.json", rep)
    with pytest.raises(
        pdr.DryRunRefused, match=r"does not demonstrate verdict value\(s\) \['continue'\]"
    ):
        pdr.build_dry_run_block(tmp_path, rel)
    assert pdr.undemonstrated_values(rep) == ["continue"]


def test_demonstration_on_an_empty_arm_demonstrates_nothing(tmp_path):
    rep = _report(["continue"], [_dem("continue", observations=0)])
    rel = _write_report(tmp_path, "r/d.json", rep)
    with pytest.raises(pdr.DryRunRefused, match="empty arm"):
        pdr.build_dry_run_block(tmp_path, rel)
    assert pdr.demonstrated_values(rep) == {}


def test_observed_outside_the_declared_values_is_refused(tmp_path):
    rep = _report(["continue"], [_dem("continue"), _dem("passed_not_expanded")])
    rel = _write_report(tmp_path, "r/d.json", rep)
    with pytest.raises(pdr.DryRunRefused, match="not a declared verdict value"):
        pdr.build_dry_run_block(tmp_path, rel)


def test_a_dry_run_that_read_confirmation_data_is_refused(tmp_path):
    rep = {**COMPLETE, "confirmation_data_read": True}
    rel = _write_report(tmp_path, "r/d.json", rep)
    with pytest.raises(pdr.DryRunRefused, match="confirmation_data_read"):
        pdr.build_dry_run_block(tmp_path, rel)
    rep = dict(COMPLETE)
    del rep["confirmation_data_read"]
    rel = _write_report(tmp_path, "r/d2.json", rep)
    with pytest.raises(pdr.DryRunRefused, match="confirmation_data_read"):
        pdr.build_dry_run_block(tmp_path, rel)


def test_a_planted_input_that_misses_its_verdict_is_refused(tmp_path):
    """A planted strong edge that comes back ``underpowered`` is a mismatch."""
    rep = _report(
        ["continue", "underpowered"],
        [
            _dem("underpowered", expected="continue", effect_bps=5.0, observations=74),
            _dem("underpowered", expected="underpowered", observations=4),
        ],
    )
    rel = _write_report(tmp_path, "r/d.json", rep)
    assert pdr.mismatches(rep) == [{"index": 0, "expected": "continue", "observed": "underpowered"}]
    assert pdr.demonstrated_values(rep) == {"underpowered": 1}
    with pytest.raises(pdr.DryRunRefused, match="1 planted for 'continue' produced 'underpowered'"):
        pdr.build_dry_run_block(tmp_path, rel)


def test_block_verdict_values_must_match_the_report(tmp_path):
    rel = _write_report(tmp_path, "r/d.json", COMPLETE)
    block = pdr.build_dry_run_block(tmp_path, rel)
    block["verdict_values"] = ["continue", "stop"]
    with pytest.raises(pdr.DryRunRefused, match="differ from the report"):
        pdr.require_dry_run(tmp_path, {"dry_run": block})


def test_non_dry_run_document_is_refused(tmp_path):
    rel = _write_report(tmp_path, "r/d.json", {"kind": "analysis_prereg"})
    with pytest.raises(pdr.DryRunRefused, match="is not a 'planted_dry_run' document"):
        pdr.load_report(tmp_path, rel)


# -------------------------------------------- the three historical refusals --
def _historical_rel08_c1() -> dict:
    """REL-08 on ds_20190130 (v1.18): C1 counted the leading gap, every variant
    failed C1 and returned ``underpowered``; ``continue`` / ``stop`` /
    ``passed_not_expanded`` were unreachable (ANOMALY_REL08_INVESTIGATION 2c)."""
    from iap.research.rel08_screen import OUTCOMES

    doc = json.loads(
        (REPO / "research/v118/rel08/screen_ds_20190130.json").read_text(encoding="utf-8")
    )
    dems = []
    for d in doc["decisions"]:
        v = next(v for v in doc["variants"] if v["id"] == d["id"])
        dems.append(
            {
                "planted": {
                    "input": f"historical variant {d['id']} on ds_20190130",
                    "observations": v["n_usable"],
                    "max_gap_min": v["max_gap_min"],
                    "criteria": d["criteria"],
                },
                "observed": d["outcome"],
            }
        )
    assert {d["observed"] for d in dems} == {"underpowered"}
    assert all(not d["planted"]["criteria"]["C1"] for d in dems)
    return _report(OUTCOMES, dems, rule="REL-08 continuation rule (historical C1)")


def _historical_rel08c_n0() -> dict:
    """REL-08c (v1.18): the imbalance arm had 0 trades at the registered times on
    2019 data, so the rule could only return ``underpowered``."""
    doc = json.loads((REPO / "research/auction/rel08c/DECISION.json").read_text(encoding="utf-8"))
    s = doc["summary"]
    assert s["n_imbalance_trades"] == 0 and s["verdict"] == "underpowered"
    dem = {
        "planted": {
            "input": "historical REL-08c imbalance arm, registered decision times, 2019-01-30",
            "observations": s["n_imbalance_trades"],
            "n_sessions": s["n_sessions"],
            "reasons": s["reasons"],
        },
        "observed": s["verdict"],
    }
    return _report(["continue", "reject", "underpowered"], [dem], rule="REL-08c (historical n = 0)")


def _historical_a0() -> dict:
    """A0 of the null protocol v3 (v1.18): the exact-count acceptance line
    (<= 55 of 1,854, i.e. 2.97 %) is failed by a correct procedure about a
    third of the time.  F3 diagnostic D1.3 (`d13_a.json`): 40 planted null
    matrices (iid entries, H0 true by construction), each enumerated over the
    1,854 derangements; 32 % of them exceed 2.97 %.  Every one of those
    matrices was built to produce ``PASS``; the ones that produced ``FAIL`` are
    mismatches, so the rule could not have been signed as written."""
    doc = json.loads((REPO / "research/v118/f3_diagnostic/d13_a.json").read_text(encoding="utf-8"))
    rates = doc["rates"]
    rates = rates["a"] if isinstance(rates, dict) else [r[0] for r in rates]
    assert len(rates) == doc["n_matrices"] == 40
    tol = A0_TOLERANCE / 1854
    dems = [
        {
            "planted": {
                "input": f"F3 D1.3 null matrix {i} (model a, iid entries, true mean 0)",
                "expected": "PASS",
                "observations": 1854,
                "rate": r,
                "tolerance": tol,
            },
            "observed": "PASS" if r <= tol else "FAIL",
        }
        for i, r in enumerate(rates)
    ]
    n_fail = sum(d["observed"] == "FAIL" for d in dems)
    assert (
        n_fail == round(doc["frac_matrices_rate_gt_2.97pct"][0] * 40)
        if isinstance(doc["frac_matrices_rate_gt_2.97pct"], list)
        else n_fail > 0
    )
    return _report(["PASS", "FAIL"], dems, rule="null protocol v3 A0 (historical planted null)")


@pytest.mark.parametrize(
    ("build", "missing", "hint"),
    [
        (_historical_rel08_c1, ["stop", "continue", "passed_not_expanded"], "does not demonstrate"),
        (_historical_rel08c_n0, ["continue", "reject", "underpowered"], "empty arm"),
        # the 13 FAIL matrices are mismatches, so FAIL is never demonstrated either
        (_historical_a0, ["FAIL"], "13 planted for 'PASS' produced 'FAIL'"),
    ],
    ids=["rel08_c1", "rel08c_n0", "a0"],
)
def test_historical_signatures_would_have_been_refused(tmp_path, build, missing, hint):
    rep = build()
    assert pdr.undemonstrated_values(rep) == missing
    rel = _write_report(tmp_path, "research/dryrun/historical.json", rep)
    with pytest.raises(pdr.DryRunRefused) as e:
        pdr.build_dry_run_block(tmp_path, rel)
    msg = str(e.value)
    assert hint in msg
    if "does not demonstrate" in msg:
        assert str(missing) in msg
    # a block forged by hand around the same report is refused at signing too
    forged = {
        "path": rel,
        "sha256": hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest(),
        "verdict_values": rep["verdict_values"],
    }
    with pytest.raises(pdr.DryRunRefused, match="does not demonstrate|did not produce"):
        pdr.require_dry_run(tmp_path, {"dry_run": forged})


def test_a0_fixture_has_the_recorded_share_of_mismatches():
    rep = _historical_a0()
    bad = pdr.mismatches(rep)
    assert len(bad) == 13 and all(m["expected"] == "PASS" and m["observed"] == "FAIL" for m in bad)
    assert pdr.demonstrated_values(rep) == {"PASS": 27}


def test_the_three_fixtures_pass_once_every_verdict_is_demonstrated(tmp_path):
    """The same rules sign once a dry run produces every value (what R4 asks for)."""
    for build in (_historical_rel08_c1, _historical_rel08c_n0, _historical_a0):
        rep = build()
        rep["demonstrations"] = [
            d
            for d in rep["demonstrations"]
            if d["planted"].get("expected", d["observed"]) == d["observed"]
        ]
        rep["demonstrations"] += [_dem(v, observations=10) for v in pdr.undemonstrated_values(rep)]
        rel = _write_report(tmp_path, f"r/{build.__name__}.json", rep)
        block = pdr.build_dry_run_block(tmp_path, rel)
        assert pdr.undemonstrated_values(rep) == []
        assert (
            pdr.require_dry_run(tmp_path, {"dry_run": block})["demonstrated"]
            == block["demonstrated"]
        )


# ------------------------------------------------------- rel08_rule_sign --
def test_rel08_rule_sign_refuses_a_draft_without_a_dry_run(tmp_path, monkeypatch):
    tool = REPO / "tools" / "rel08_rule_sign.py"
    spec = importlib.util.spec_from_file_location("rel08_rule_sign", tool)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _Draft:
        doc: dict = {"signed": False}
        thresholds: dict = {}
        path = "research/v118/rel08_rule_DRAFT.json"

    monkeypatch.setattr(mod, "load_draft", lambda root: _Draft())
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: type("P", (), {"stdout": "", "returncode": 0})(),
    )
    with pytest.raises(SystemExit, match="planted dry run \\(R4\\): no dry_run block"):
        mod.main([str(tmp_path), "--dry-run"])
