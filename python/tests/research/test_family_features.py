"""The family features go through the data contracts and read no later row (v2.0 stage 7)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from iap.data.contracts import check_causality, contract_of, features, lineage
from iap.data.leakage import future_shift_canary, shuffled_label_canary
from iap.research import family_features as ff
from iap.research.loop import build_features, forward_label, load_panel
from stage7_fixtures import manifest, manifest_path, panel_path  # noqa: F401

FEATURE_FNS = [
    ff.feature_common_beta_60,
    ff.feature_common_sigma_60,
    ff.feature_common_adv_20,
    ff.feature_d1_reversal_z,
    ff.feature_d1_reversal_z1,
    ff.feature_d2_residual_momentum,
    ff.feature_d3_overnight_spread,
    ff.feature_d4_filing_reaction_z,
    ff.feature_d4_filing_reaction_z_live5,
]


@pytest.fixture(scope="module")
def frame(panel_path) -> pd.DataFrame:
    f, _sha = load_panel(panel_path)
    return f


def test_every_feature_carries_a_contract_on_the_synthetic_route() -> None:
    reg = features()
    for fn in FEATURE_FNS:
        c = contract_of(fn)
        assert c.feature_id in reg and c.route == "synthetic"
        assert c.total_lag_periods == 1, "a close is not observable at the close (period_end lag 1)"
        assert c.leakage_test_id == "LK-CAUSALITY"
        doc = lineage(c.feature_id)
        assert doc["inputs"]["synthetic.daily_panel"]["availability"] == {
            "basis": "period_end",
            "lag_periods": 1,
        }


@pytest.mark.parametrize("fn", FEATURE_FNS, ids=lambda f: f.__name__)
def test_no_feature_reads_a_later_row(frame, fn) -> None:
    small = frame[frame["name"].isin(sorted(frame["name"].unique())[:8])].reset_index(drop=True)
    n = len(small)
    probes = [n // 2, (3 * n) // 4, n - 1]
    out = check_causality(fn, small, probes=probes)
    assert out["probes"] == len(probes)


def test_a_planted_leak_in_the_same_panel_is_caught(frame) -> None:
    from iap.data.contracts import LeakageDetected

    small = frame[frame["name"].isin(sorted(frame["name"].unique())[:6])].reset_index(drop=True)

    def leaky(fr: pd.DataFrame) -> pd.Series:  # reads the next session's close
        return fr.groupby("name", sort=False)["close"].shift(-1)

    leaky.__name__ = "feature_d1_reversal_z"
    leaky.__iap_contract__ = contract_of(
        ff.feature_d1_reversal_z
    )  # the declaration lies; the check must not
    with pytest.raises(LeakageDetected):
        check_causality(leaky, small, probes=[len(small) // 2, len(small) - 1])


def test_features_are_aligned_by_their_contract_lag(frame) -> None:
    feats = build_features(frame, ["d1.reversal_z"])
    raw = ff.feature_d1_reversal_z(frame)
    one = frame["name"] == "SYN000"
    a = feats.loc[one, "d1.reversal_z"].to_numpy()
    b = raw[one].shift(1).to_numpy()
    ok = np.isfinite(a) & np.isfinite(b)
    assert ok.sum() > 300 and np.allclose(a[ok], b[ok])
    assert not np.isfinite(a[0])


def test_the_planted_reversal_shows_in_the_canaries_through_the_contracted_feature(frame) -> None:
    feats = build_features(frame, ["d1.reversal_z"])
    label = forward_label(frame, 3, exit_at_open=False)
    ok = np.isfinite(feats["d1.reversal_z"]) & np.isfinite(label)
    fs = future_shift_canary(
        feats.loc[ok, "d1.reversal_z"], label[ok], group=frame.loc[ok, "name"], periods=1
    )
    assert fs["positive_control"], fs
    sl = shuffled_label_canary(feats.loc[ok, "d1.reversal_z"], label[ok])
    assert sl["passed"], sl


def test_d2_is_a_negative_control_on_this_fixture(frame) -> None:
    feats = build_features(frame, ["d2.residual_momentum"])
    label = forward_label(frame, 21, exit_at_open=False)
    z = feats["d2.residual_momentum"]
    ok = np.isfinite(z) & np.isfinite(label)
    r = float(np.corrcoef(z[ok], label[ok])[0, 1])
    assert abs(r) < 3.0 / np.sqrt(ok.sum()) + 0.05
