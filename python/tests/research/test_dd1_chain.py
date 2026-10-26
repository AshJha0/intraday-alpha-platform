"""The research chain on DD1-format files (synthetic only): the panel adapter
(:mod:`iap.research.dd1_panel`) refuses what it may not open and maps the DD1
tables to the loop's columns point in time; the loop blanks non-member rows; and
``tools/research/dd1_chain.py`` runs panel -> D1..D4 -> E5 -> M0 vs M1 end to end
on a synthetic set written by ``dd1_daily.dry_run`` (plumbing, never a finding)."""

from __future__ import annotations

import importlib.util
import json
import sys
import time

import numpy as np
import pandas as pd
import pytest
from conftest import REPO_ROOT
from iap.research import dd1_panel
from iap.research.loop import build_features

sys.path.insert(0, str(REPO_ROOT / "tools"))

from ingest import dd1_daily as job  # noqa: E402
from ingest import dd1_fixture as fx  # noqa: E402


def _tool():
    spec = importlib.util.spec_from_file_location(
        "dd1_chain_tool", REPO_ROOT / "tools" / "research" / "dd1_chain.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def dd1_set(tmp_path_factory):
    """The DD1 tests' own twelve-name dry run: the exact output format of the job."""
    t = tmp_path_factory.mktemp("dd1_panel")
    rec = job.dry_run(2026, t / "dev", t / "sealed")
    assert rec["status"] == "complete"
    return t / "dev" / "dd1_2026", rec


@pytest.fixture(scope="module")
def panel(dd1_set):
    frame, manifest = dd1_panel.build_panel(dd1_set[0])
    return frame, manifest


# ------------------------------------------------------------------ the adapter --
def test_panel_has_the_loop_columns_and_the_daily_pit_route(panel, dd1_set):
    frame, manifest = panel
    assert tuple(frame.columns) == dd1_panel.PANEL_COLUMNS
    assert manifest["route"] == "daily_pit" and manifest["sealed"] is False
    assert set(manifest["partitions"]) == {"development", "validation"}
    dev, val = manifest["partitions"]["development"], manifest["partitions"]["validation"]
    assert dev[1] <= "2022-12-31" < val[0] <= val[1] <= "2023-12-31"
    assert frame["session"].max() == val[1], "nothing past the validation window"
    assert set(frame["name"]) == set(fx.SYMBOLS)
    for col in ("auction_volume", "open_volume", "sector"):
        assert col in manifest["estimated_columns"]


def test_membership_is_point_in_time(panel, dd1_set):
    """The delisted name leaves the universe on its exit revision and the joiner enters on its own."""
    frame, _ = panel
    rec = dd1_set[1]
    mem = pd.read_csv(dd1_set[0] / "development" / "membership.csv", dtype=str)
    sp = mem.loc[mem["index"] == "sp500"]
    exit_day = sp.loc[sp["symbol"] == fx.DELISTED, "end"].dropna().min()
    join_day = sp.loc[sp["symbol"] == fx.JOINER, "start"].min()
    g = frame.loc[frame["name"] == fx.DELISTED]
    assert (g.loc[g["session"] < exit_day, "member"] == 1).all()
    assert (g.loc[g["session"] >= exit_day, "member"] == 0).all()
    assert (g["session"] >= exit_day).any(), "the delisted name still trades after it left"
    j = frame.loc[frame["name"] == fx.JOINER]
    assert (j.loc[j["session"] < join_day, "member"] == 0).all()
    assert (j.loc[j["session"] >= join_day, "member"] == 1).all()
    assert rec["dd1_tests"]["pass"]


def test_market_return_is_the_members_equal_weighted_return(panel):
    frame, _ = panel
    f = frame.sort_values(["name", "session"])
    prev = f.groupby("name")["close"].shift(1)
    f = f.assign(r=f["close"] / prev - 1.0)
    s = f["session"].unique()[100]
    day = f.loc[(f["session"] == s) & (f["member"] == 1)]
    assert day["mkt_ret"].iloc[0] == pytest.approx(day["r"].mean(), rel=1e-12)


def test_filings_mark_the_first_session_on_or_after_the_filing_date(panel, dd1_set):
    frame, _ = panel
    fil = pd.read_csv(dd1_set[0] / "development" / "filings.csv", dtype=str)
    fin = fil.loc[fil["form"].isin(dd1_panel.FINANCIAL_FORMS)]
    assert not fil.loc[~fil["form"].isin(dd1_panel.FINANCIAL_FORMS)].empty, "8-Ks are present"
    sessions = np.array(sorted(frame["session"].unique()))
    marked = set(
        zip(
            frame.loc[frame["filing"] == 1, "name"],
            frame.loc[frame["filing"] == 1, "session"],
            strict=True,
        )
    )
    expect = set()
    for sym, d in zip(fin["symbol"], fin["filing_date"], strict=True):
        i = int(np.searchsorted(sessions, d))
        if i < len(sessions) and sessions[i] <= "2022-12-31":
            expect.add((sym, sessions[i]))
    dev_marked = {k for k in marked if k[1] <= "2022-12-31"}
    assert dev_marked == expect


def test_panel_bytes_are_deterministic(dd1_set, tmp_path):
    p1, m1 = dd1_panel.write_panel(dd1_set[0], tmp_path / "a")
    p2, m2 = dd1_panel.write_panel(dd1_set[0], tmp_path / "b")
    assert p1.read_bytes() == p2.read_bytes()
    assert json.loads(m1.read_text())["sha256"] == json.loads(m2.read_text())["sha256"]


def test_a_tampered_partition_file_is_refused(dd1_set, tmp_path):
    import shutil

    copy = tmp_path / "dd1_2026"
    shutil.copytree(dd1_set[0], copy)
    p = copy / "development" / "prices.csv"
    p.write_bytes(p.read_bytes().replace(b"AAA,", b"AAB,", 1))
    with pytest.raises(dd1_panel.PanelRefused, match="sha256"):
        dd1_panel.build_panel(copy)


def test_an_incomplete_record_is_refused(dd1_set, tmp_path):
    import shutil

    copy = tmp_path / "dd1_2026"
    shutil.copytree(dd1_set[0], copy)
    rp = copy / "dataset.json"
    rec = json.loads(rp.read_text())
    rec["status"] = "incomplete"
    rp.write_text(json.dumps(rec))
    with pytest.raises(dd1_panel.PanelRefused, match="incomplete"):
        dd1_panel.build_panel(copy)


def test_a_record_naming_a_sealed_partition_as_readable_is_refused(dd1_set, tmp_path):
    import shutil

    copy = tmp_path / "dd1_2026"
    shutil.copytree(dd1_set[0], copy)
    rp = copy / "dataset.json"
    rec = json.loads(rp.read_text())
    rec["partitions"]["validation"]["dir"] = "../elsewhere"
    rp.write_text(json.dumps(rec))
    with pytest.raises(dd1_panel.PanelRefused, match="escapes"):
        dd1_panel.build_panel(copy)


def test_a_sealed_root_is_refused(monkeypatch, dd1_set):
    monkeypatch.setattr(dd1_panel, "is_sealed_target", lambda _p: True)
    with pytest.raises(dd1_panel.PanelRefused, match="sealed"):
        dd1_panel.read_record(dd1_set[0])


# ------------------------------------------------------------------- the loop --
def test_build_features_blanks_signals_outside_the_universe(panel):
    frame, _ = panel
    feats = build_features(frame, ["d1.reversal_z"])
    out = frame["member"].to_numpy() != 1
    assert out.any()
    assert feats.loc[out, "d1.reversal_z"].isna().all()
    assert feats.loc[out, "common.sigma_60"].notna().any(), "common features keep their history"
    assert feats.loc[~out, "d1.reversal_z"].notna().any()


# ------------------------------------------------------------------ the chain --
def test_the_chain_runs_end_to_end_on_a_synthetic_dd1_set(tmp_path, capsys):
    """CI: dry-run DD1 set -> panel -> D1..D4 development runs -> E5 -> M0 vs M1, in about a minute."""
    tool = _tool()
    t0 = time.perf_counter()
    rc = tool.main(
        [
            "--synthetic",
            "--out",
            str(tmp_path),
            "--identity",
            "dev-test",
            *tool.CI_ARGS,
        ]
    )
    elapsed = time.perf_counter() - t0
    assert rc == 0
    s = json.loads(capsys.readouterr().out)
    assert s["route"] == "daily_pit"
    assert set(s["families"]) == {"D1", "D2", "D3", "D4"}
    for fam, v in s["families"].items():
        assert len(v["report_sha256"]) == 64, fam
        assert v["sessions"]["trading"] > 0, fam
    # the planted reversal and overnight drift reach the trade path through the DD1 format
    # (D1 needs more names than the CI set holds; the default 250-name run trades it)
    assert s["families"]["D2"]["cohorts_opened"] > 0 and s["families"]["D2"]["fills"] > 0
    assert s["families"]["D3"]["cohorts_opened"] > 0 and s["families"]["D3"]["fills"] > 0
    assert s["ml"]["outcome"].startswith("refused by the protocol"), (
        "the frozen protocol (revision 1) refuses fits off the synthetic route"
    )
    assert s["ml_synthetic"]["n_fits"] >= 1, "the M1 grid is fitted on the DD1-shaped panel"
    ledger = json.loads((tmp_path / "selection_ledger.json").read_text())
    assert ledger, "every run is ledgered"
    assert elapsed < 120, f"the chain took {elapsed:.0f} s"


def test_m1_records_an_unfitted_tier_instead_of_raising(tmp_path):
    """Regression: D2's 253-session lookback on a short development partition left no
    purged training row and the first M1 grid point raised; the tier is now recorded as
    not fitted and the incumbent stays."""
    tool = _tool()
    dataset = tool.synthetic_dataset(
        tmp_path / "dd1", extra_symbols=40, first=__import__("datetime").date(2021, 1, 4)
    )
    panel, manifest = dd1_panel.write_panel(dataset, tmp_path / "panel")
    man = json.loads(manifest.read_text())
    man.update(route="synthetic", dataset_id=man["dataset_id"] + ".relabelled_synthetic")
    mpath = tmp_path / "panel" / "manifest_synthetic.json"
    mpath.write_text(json.dumps(man))
    from iap.ml.pipeline import run_m1_demo

    rep = run_m1_demo(tool.SPECS["D2"], panel, mpath, out_dir=tmp_path / "ml", identity="dev-test")
    assert rep["n_fits"] == 0 and rep["candidates"] == []
    assert rep["decision"]["carried"] == "M0 (incumbent stays)"
    trial = json.loads((tmp_path / "ml" / "trial_ledger.json").read_text())
    assert any(e["kind"] == "m1_not_fitted" for e in trial["entries"])
