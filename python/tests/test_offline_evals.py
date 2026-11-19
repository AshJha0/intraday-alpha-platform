"""Offline guardrail evals: case-file schema, runners, --check gates, Wilson."""

from __future__ import annotations

import copy
import json

import pytest
from iap.llm import injection_eval, verifier_eval, verify
from iap.llm.verifier_eval import wilson

VCASES = json.loads(verifier_eval.CASES.read_text(encoding="utf-8"))
ICASES = json.loads(injection_eval.CASES.read_text(encoding="utf-8"))
VRESULT = json.loads(verifier_eval.RESULT.read_text(encoding="utf-8"))
IRESULT = json.loads(injection_eval.RESULT.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("k", "n", "lo", "hi"),
    [
        (6, 60, 0.0466428, 0.2014946),
        (0, 10, 0.0, 0.2775328),
        (10, 10, 0.7224672, 1.0),
        (5, 10, 0.2365931, 0.7634069),
    ],
)
def test_wilson_known_values(k, n, lo, hi):
    a, b = wilson(k, n)
    assert a == pytest.approx(lo, abs=1e-6) and b == pytest.approx(hi, abs=1e-6)


def test_wilson_empty_sample_is_uninformative():
    assert wilson(0, 0) == (0.0, 1.0)


def test_verifier_case_file_schema():
    cases = verifier_eval.validate_cases(VCASES)
    assert len(cases) >= 120
    cats = {c["category"] for c in cases}
    for need in (
        "exact", "rounding", "half_edge", "percent", "labelled_stat", "metadata", "derived",
        "number_words", "sign_zero", "scientific", "thousands", "ranges_lists", "units",
        "identifiers", "title", "lookalike_digits", "long_decimal",
    ):  # fmt: skip
        assert need in cats, need
    for kind in ("accept", "reject"):
        assert sum(c["label"] == kind for c in cases) >= 40


def test_verifier_schema_rejects_bad_cases():
    bad = copy.deepcopy(VCASES)
    bad["cases"][0]["label"] = "maybe"
    with pytest.raises(ValueError, match="label"):
        verifier_eval.validate_cases(bad)
    bad = copy.deepcopy(VCASES)
    bad["cases"][1]["id"] = bad["cases"][0]["id"]
    with pytest.raises(ValueError, match="duplicate"):
        verifier_eval.validate_cases(bad)


def test_verifier_result_matches_a_fresh_run_and_every_disagreement_is_classified():
    res = verifier_eval.evaluate(verifier_eval.validate_cases(VCASES))
    assert res["overall"] == VRESULT["overall"]
    assert res["unclassified"] == []
    assert {d["id"] for d in res["disagreements"]} == set(res["defects"]) | set(res["mislabels"])


def test_verifier_check_gate(tmp_path, capsys):
    assert verifier_eval.main(["--check"]) == 0
    worse = copy.deepcopy(VRESULT)
    for key in ("false_accept", "false_reject"):
        worse["overall"][key]["rate"] = -0.01  # below today's rate, which is zero
    p = tmp_path / "old.json"
    p.write_text(json.dumps(worse), encoding="utf-8")
    assert verifier_eval.main(["--check", "--out", str(p)]) == 1
    assert "rate rose" in capsys.readouterr().out
    assert verifier_eval.main(["--check", "--out", str(tmp_path / "none.json")]) == 1


def test_verifier_runner_writes_result(tmp_path):
    out = tmp_path / "r.json"
    assert verifier_eval.main(["--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["overall"] == VRESULT["overall"]


_BY_ID = {c["id"]: c for c in VCASES["cases"]}


_FIXED = sorted(c["id"] for c in VCASES["cases"] if "fixed_defect" in c)


def test_twenty_four_defects_are_recorded_as_fixed():
    assert len(_FIXED) == 24 and VRESULT["defects"] == []


@pytest.mark.parametrize("case_id", _FIXED)
def test_former_verifier_defects_now_pass(tmp_path, case_id):
    case = _BY_ID[case_id]
    verdict, _ = verifier_eval.judge(case, tmp_path)
    assert verdict == case["label"]


def test_long_decimal_no_longer_crashes(tmp_path):
    # v143: a precision beyond the 28-digit decimal context raised InvalidOperation
    assert verify.matches("12345.600000000000000000000000", False, [12345.6])
    assert not verify.matches("12345.600000000000000000000001", False, [12345.6])
    assert not verify.matches("1.0e-400", False, [1e-300])
    case = {**_BY_ID["v143"]}
    assert verifier_eval.judge(case, tmp_path) == ("accept", [])


def test_heldout_file_is_separate_and_gated():
    held = verifier_eval.validate_cases(
        json.loads(verifier_eval.HELDOUT.read_text(encoding="utf-8"))
    )
    assert len(held) >= 40
    assert not {c["id"] for c in held} & set(_BY_ID)
    assert {c["category"] for c in held} == {c["category"] for c in VCASES["cases"]}
    h = VRESULT["heldout"]
    assert h["unclassified"] == [] and h["defects"] == []
    worse = copy.deepcopy(VRESULT)
    worse["heldout"]["overall"]["false_reject"]["rate"] = -0.01  # below today's rate, which is zero
    assert any(m.startswith("held-out") for m in verifier_eval.gate(VRESULT, worse))


def test_injection_case_file_schema():
    cases = injection_eval.validate_cases(ICASES)
    assert len(cases) >= 60
    assert sum(not c["malicious"] for c in cases) >= 15
    cats = {c["category"] for c in cases}
    for need in (
        "direct_override", "fake_system", "roleplay", "tool_smuggling", "encoded", "unicode",
        "hidden_markup", "multilingual", "split_fields", "benign",
    ):  # fmt: skip
        assert need in cats, need
    bad = copy.deepcopy(ICASES)
    bad["cases"][0]["malicious"] = "yes"
    with pytest.raises(ValueError, match="malicious"):
        injection_eval.validate_cases(bad)


def test_structural_control_executes_no_off_allowlist_call(tmp_path):
    for case in ICASES["cases"][:5] + ICASES["cases"][-2:]:
        run = injection_eval.structural(case, tmp_path)
        assert run["payload_delivered"] and run["status"] == "finished"
        assert run["executed_off_allowlist"] == 0
        assert run["refused"] == run["attack_calls"]
        assert run["executed"] == ["read_note", "finish"]


def test_injection_committed_result_and_check_gate(tmp_path, capsys):
    s = IRESULT["structural"]
    assert s["executed_off_allowlist"] == 0 and s["payload_delivered"] == s["cases"]
    assert injection_eval.main(["--check"]) == 0
    worse = copy.deepcopy(IRESULT)
    worse["pattern_flag"]["overall"]["detection"]["rate"] = 1.0
    p = tmp_path / "old.json"
    p.write_text(json.dumps(worse), encoding="utf-8")
    assert injection_eval.main(["--check", "--out", str(p)]) == 1
    assert "detection fell" in capsys.readouterr().out


def test_injection_gate_trips_on_an_executed_off_allowlist_call():
    new = copy.deepcopy(IRESULT)
    new["structural"]["executed_off_allowlist"] = 1
    assert any("off-allowlist" in m for m in injection_eval.gate(new, IRESULT))
