"""The H1-equivalence test (v2.0 stage 5; plan V2-C1: this test stays in CI forever).

The generic engine (EN1 forecasts, EN2 ledgers, EN3 projection, EN4 execution,
EN5 session series), driven by the H1 client ``iap.alpha.h1``, must reproduce
the direct reference implementation of E0 sections 3-10 under
``research/v119/h1_reference/`` **byte for byte** on the committed synthetic
fixtures: the research ledger, the executable ledger, the orders, the fills
and the R_d series, serialised as canonical JSON.  A second scenario plants
no reversal, so both horizons fail E0 section 4's quality rule and both
implementations must trade nothing while counting every session as a usable
session with R_d = 0.

The fixture generator is re-hashed against ``fixtures/manifest.json`` first:
the equivalence is a statement about these bytes.
"""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest
from conftest import REPO_ROOT
from iap.contracts.versions import canonical_json

REF_DIR = REPO_ROOT / "research" / "v119" / "h1_reference"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"h1_reference_{name}", REF_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


fixtures = _load("fixtures")
reference = _load("reference")


@pytest.fixture(scope="module")
def panels():
    return {sc: fixtures.build(scenario=sc) for sc in fixtures.SCENARIOS}


def test_fixtures_match_the_committed_manifest(panels):
    manifest = json.loads((REF_DIR / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
    for sc, fx in panels.items():
        assert fixtures.digest(fx) == manifest[sc], (
            f"fixture {sc} differs from the committed manifest"
        )


def _compare(ref: dict, eng: dict) -> None:
    for h in ref["variants"]:
        r, e = ref["variants"][h], eng["variants"][h]
        for part in ("research_ledger", "executable_ledger", "orders", "fills", "r_bps"):
            assert canonical_json(r[part]) == canonical_json(e[part]), f"{h}: {part} differs"
    assert canonical_json(
        {h: (f["rho"], f["se"], f["n"], f["passed"]) for h, f in ref["fits"].items()}
    ) == canonical_json(
        {h: (f["rho"], f["se"], f["n"], f["passed"]) for h, f in eng["fits"].items()}
    )


@pytest.mark.parametrize("scenario", sorted(fixtures.SCENARIOS))
def test_engine_reproduces_the_reference_byte_for_byte(panels, scenario):
    from iap.alpha import h1

    fx = panels[scenario]
    ref = reference.run(fx)
    eng = h1.run(fx)
    _compare(ref, eng)
    if scenario == "reversal":
        for h, v in eng["variants"].items():
            assert v["fills"] and v["orders"], f"{h}: the reversal fixture must trade"
            assert any(o["outstanding"] for o in v["orders"]) or any(
                o["cancelled"] for o in v["orders"]
            )
            assert any(s["closing_cohorts"] for s in v["executable_ledger"]) or any(
                s["carried"] for s in v["executable_ledger"]
            )
            for w, s in zip(
                v["waterfalls"], v["executable_ledger"], strict=True
            ):  # the identity, re-stated
                total = (
                    w["midpoint_reference_pnl"]
                    + w["latency_drift"]
                    - w["half_spread"]
                    - w["impact"]
                    - w["fees"]
                    + w["execution_residual"]
                    - w["financing"]
                )
                assert abs(total - s["net_pnl"]) <= 1e-9 * max(1.0, abs(s["net_pnl"]))
        assert all(f["passed"] for f in eng["fits"].values())
    else:
        for h, v in eng["variants"].items():
            assert not v["fills"] and not v["orders"] and not v["research_ledger"], (
                f"{h}: fail closed means no trade"
            )
            assert [r for _s, r in v["r_bps"]] == [0.0] * fx["config"]["n_trading"]
            assert v["session_counts"]["usable_no_trades"] == fx["config"]["n_trading"]
        assert not any(f["passed"] for f in eng["fits"].values())


def test_reference_imports_nothing_from_iap():
    import ast

    for name in ("fixtures", "reference"):
        tree = ast.parse((REF_DIR / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] == "iap" for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "iap"
