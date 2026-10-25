"""U3 family specifications (v2.0 stage 7): every file complete before any fit, the index consistent, gates honoured."""

from __future__ import annotations

import json
import re

import pytest
from iap.research.loop import SpecError, load_family_spec
from stage7_fixtures import FAMILIES, manifest, manifest_path, panel_path  # noqa: F401

FILES = sorted(p for p in FAMILIES.glob("*.md") if p.name != "README.md")
EXPECTED = {
    "D1_short_term_reversal.md": ("d1", True, "PF-1", 3),
    "D2_residual_momentum.md": ("d2", True, "PF-2", 1),
    "D3_overnight_asymmetry.md": ("d3", True, "PF-1", 1),
    "D4_filing_date_drift.md": ("d4", True, "PF-2", 3),
    "D5_close_pressure.md": ("d5", False, "PF-1", 1),
    "H2_intraday_momentum.md": ("h2", False, "PF-1", 2),
    "H3_etf_lead_lag.md": ("h3", False, "PF-1", 3),
    "H4_order_flow_imbalance.md": ("h4", False, "PF-1", 3),
}


def test_the_eight_families_are_specified() -> None:
    assert [p.name for p in FILES] == sorted(EXPECTED)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_every_file_is_complete_and_consistent(path) -> None:
    spec = load_family_spec(path)
    fam, fit, profile, k = EXPECTED[path.name]
    assert spec.family_id == fam
    assert spec.fit_allowed is fit
    assert spec.doc["profile"] == profile
    assert spec.doc["registered_family_size"] == k
    text = path.read_text(encoding="utf-8")
    # the ten sections in order
    heads = re.findall(r"^## (\d+)\. ", text, re.M)
    assert [int(h) for h in heads] == list(range(1, 11))
    # the label is a forward return in bps, purged
    assert "bps" in text and "purg" in text.lower()
    # the E5 block list is named
    assert re.search(r"blocks? A-E|A signal|A\. \*\*Signal", text)
    # evidence class words are the U0 section 10 vocabulary
    assert spec.doc["evidence_class"] in {
        "independently confirmed",
        "mechanically isolated",
        "development-supported",
        "exploratory",
        "rejected",
        "data-ineligible",
        "pending",
    }


def test_ineligible_and_gated_families_have_no_fit_and_say_so() -> None:
    for name in ("D5_close_pressure.md", "H3_etf_lead_lag.md", "H4_order_flow_imbalance.md"):
        spec = load_family_spec(FAMILIES / name)
        assert spec.doc["status"] == "data-ineligible"
        assert spec.doc["routes"] == [] and spec.doc["features"] == []
        assert re.search(r"no development\s+run", (FAMILIES / name).read_text(encoding="utf-8"))
    h2 = load_family_spec(FAMILIES / "H2_intraday_momentum.md")
    assert h2.doc["data_gate"]["outcome"] == "gated" and not h2.fit_allowed
    assert h2.doc["routes"] == ["iex_hist"]


def test_d4_is_filing_date_drift_without_consensus_surprise() -> None:
    text = (FAMILIES / "D4_filing_date_drift.md").read_text(encoding="utf-8")
    assert "no consensus" in text.lower() and "EDGAR" in text and "acceptance" in text
    assert "D-43" in text


def test_d3_gate_names_open_convention_and_corporate_actions() -> None:
    spec = load_family_spec(FAMILIES / "D3_overnight_asymmetry.md")
    conds = " ".join(spec.doc["data_gate"]["conditions"]).lower()
    assert "open convention" in conds and "corporate action" in conds
    assert spec.primary_horizon == "c2o"


def test_readme_index_lists_every_family_with_its_evidence_class() -> None:
    readme = (FAMILIES / "README.md").read_text(encoding="utf-8")
    header = next(line for line in readme.splitlines() if line.startswith("| file |"))
    assert "evidence class" in header
    for p in FILES:
        assert f"[{p.name}]({p.name})" in readme
        spec = load_family_spec(p)
        row = next(line for line in readme.splitlines() if p.name in line)
        assert spec.doc["evidence_class"] in row
        assert spec.doc["profile"] in row


def test_loader_refuses_an_inconsistent_block(tmp_path) -> None:
    src = (FAMILIES / "D5_close_pressure.md").read_text(encoding="utf-8")
    bad = src.replace('"fit_allowed": false', '"fit_allowed": true')
    p = tmp_path / "D5_bad.md"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(SpecError, match="fit_allowed true"):
        load_family_spec(p)
    missing = re.sub(r"^## 4\. Falsifier", "## 4. Something else", src, flags=re.M)
    p.write_text(missing, encoding="utf-8")
    with pytest.raises(SpecError, match="Falsifier"):
        load_family_spec(p)
    block = json.loads(re.search(r"```json\n(.*?)\n```", src, re.S).group(1))
    del block["falsifier" if "falsifier" in block else "profile"]
    p.write_text(
        re.sub(r"```json\n.*?\n```", "```json\n" + json.dumps(block) + "\n```", src, flags=re.S),
        encoding="utf-8",
    )
    with pytest.raises(SpecError, match="lacks"):
        load_family_spec(p)
