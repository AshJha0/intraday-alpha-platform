"""iap.ml: chronological splits with purging and embargo, the trial ledger, the admission statistics."""

from __future__ import annotations

import numpy as np
import pytest
from iap.core.rng import SplitMix64
from iap.ml import (
    BudgetExceeded,
    ChronologicalSplit,
    SplitError,
    TrialLedger,
    TrialLedgerError,
    admission_test,
    block_bootstrap_mean,
    block_length_from_dependence,
    choose_candidate,
    holm_within_tier,
    load_protocol,
)
from iap.ml.admission import paired_delta

CAL = tuple(f"2020-01-{d:02d}" for d in range(1, 29))


# --------------------------------------------------------------------------- splits
def test_purge_and_embargo_keep_every_training_label_inside_the_training_window() -> None:
    s = ChronologicalSplit(CAL, "2020-01-14", horizon=3, embargo=5)
    rows = list(CAL) * 2
    t = s.train_mask(rows)
    v = s.validation_mask(rows)
    idx = {c: i for i, c in enumerate(CAL)}
    assert all(idx[r] + 3 <= idx["2020-01-14"] for r, m in zip(rows, t, strict=True) if m)
    assert all(idx[r] >= idx["2020-01-14"] + 1 + 5 for r, m in zip(rows, v, strict=True) if m)
    assert not (t & v).any()
    s.check_no_overlap(rows)
    assert s.train_sessions()[-1] == "2020-01-11" and s.validation_sessions()[0] == "2020-01-20"
    d = s.to_dict()
    assert d["n_train_sessions"] == 11 and d["validation_start"] == "2020-01-20"


def test_embargo_shorter_than_the_horizon_is_refused() -> None:
    with pytest.raises(SplitError, match="embargo"):
        ChronologicalSplit(CAL, "2020-01-14", horizon=5, embargo=3)
    with pytest.raises(SplitError):
        ChronologicalSplit(CAL, "2020-02-01", horizon=1, embargo=1)
    with pytest.raises(SplitError):
        ChronologicalSplit(CAL, "2020-01-14", horizon=1, embargo=1, validation_end="2020-01-10")


def test_unknown_sessions_are_refused() -> None:
    s = ChronologicalSplit(CAL, "2020-01-14", horizon=1, embargo=1)
    with pytest.raises(SplitError):
        s.train_mask(["2019-12-31"])


# --------------------------------------------------------------------------- trial ledger
def test_trial_ledger_opens_with_the_freeze_and_enforces_the_budget(tmp_path) -> None:
    proto = load_protocol()
    led = TrialLedger(tmp_path / "t.json", proto)
    with pytest.raises(TrialLedgerError, match="before the protocol freeze"):
        led.record_fit(
            family_id="d1",
            model_class="M1.ridge",
            group_id="d1.core",
            hyper={},
            split={},
            seed=1,
            complexity=1.0,
            fit_sha256="0" * 64,
        )
    led.freeze("a" * 64, 1)
    with pytest.raises(TrialLedgerError, match="recorded once"):
        led.freeze("a" * 64, 1)
    for i in range(5):
        led.record_fit(
            family_id="d1",
            model_class="M1.ridge" if i < 3 else "M1.elastic_net",
            group_id="d1.core",
            hyper={"i": i},
            split={},
            seed=1,
            complexity=1.0,
            fit_sha256="0" * 64,
        )
    with pytest.raises(BudgetExceeded):
        led.record_fit(
            family_id="d1",
            model_class="M1.ridge",
            group_id="d1.core",
            hyper={"i": 6},
            split={},
            seed=1,
            complexity=1.0,
            fit_sha256="0" * 64,
        )
    assert len(led.fits("M1", "d1")) == 6 and led.entries[-1]["kind"] == "fit_refused"
    # another family has its own budget
    led.record_fit(
        family_id="d3",
        model_class="M1.ridge",
        group_id="d3.core",
        hyper={},
        split={},
        seed=1,
        complexity=1.0,
        fit_sha256="0" * 64,
    )
    led.record_evaluation(1, {"admitted": False})
    with pytest.raises(TrialLedgerError, match="not a fit"):
        led.record_evaluation(0, {})
    led.save()
    again = TrialLedger(tmp_path / "t.json", proto)
    assert again.chain_head == led.chain_head and again.protocol_sha256 == "a" * 64
    # tamper: edit one entry
    import json

    blob = json.loads((tmp_path / "t.json").read_text())
    blob["entries"][2]["hyper"] = {"i": 99}
    (tmp_path / "t.json").write_text(json.dumps(blob))
    with pytest.raises(TrialLedgerError, match="chain head"):
        TrialLedger(tmp_path / "t.json", proto)


# --------------------------------------------------------------------------- admission
def test_block_length_from_dependence_is_small_for_iid_and_grows_with_autocorrelation() -> None:
    rng = SplitMix64(7)
    iid = np.array([rng.normal() for _ in range(400)])
    assert block_length_from_dependence(iid)["block_length"] <= 2
    ar = np.zeros(400)
    for i in range(1, 400):
        ar[i] = 0.9 * ar[i - 1] + rng.normal()
    assert block_length_from_dependence(ar)["block_length"] >= 5
    with pytest.raises(ValueError):
        block_length_from_dependence(iid[:10])


def test_block_bootstrap_is_seeded_and_centred() -> None:
    x = np.arange(60, dtype=float) % 7 - 3.0
    a = block_bootstrap_mean(x, 5, resamples=300, seed=1)
    b = block_bootstrap_mean(x, 5, resamples=300, seed=1)
    c = block_bootstrap_mean(x, 5, resamples=300, seed=2)
    assert np.array_equal(a, b) and not np.array_equal(a, c)
    assert abs(a.mean() - x.mean()) < 0.2


def test_admission_requires_the_lower_bound_above_delta() -> None:
    rng = SplitMix64(11)
    good = np.array([1.0 + 0.5 * rng.normal() for _ in range(120)])
    null = np.array([0.5 * rng.normal() for _ in range(120)])
    r_good = admission_test(good, delta_bps=0.25, block_length=3, resamples=500, seed=3)
    r_null = admission_test(null, delta_bps=0.25, block_length=3, resamples=500, seed=3)
    assert r_good.admitted and r_good.ci_lower_bps > 0.25 and r_good.p_value < 0.05
    assert not r_null.admitted and r_null.p_value > 0.5
    assert r_good.to_dict()["block_length"] == 3
    # a candidate that is better but by less than delta is not admitted
    small = np.array([0.1 + 0.05 * rng.normal() for _ in range(120)])
    assert not admission_test(small, delta_bps=0.25, block_length=3, resamples=500, seed=3).admitted


def test_paired_delta_refuses_misaligned_sessions() -> None:
    with pytest.raises(ValueError, match="identical sessions"):
        paired_delta([1.0, 2.0], [1.0, 1.0], ["a", "b"], ["a", "c"])
    assert list(paired_delta([1.0, 2.0], [1.0, 1.0], ["a", "b"], ["a", "b"])) == [0.0, 1.0]


def test_holm_within_tier_counts_extra_looks() -> None:
    h = holm_within_tier({"a": 0.001, "b": 0.02, "c": 0.5}, alpha=0.05)
    assert h["K"] == 3 and h["rejected"] == {"a": True, "b": True, "c": False}  # 0.02 <= 0.05 / 2
    h2 = holm_within_tier({"a": 0.001, "b": 0.02, "c": 0.5}, alpha=0.05, extra_looks=10)
    assert h2["K"] == 13 and h2["rejected"]["b"] is False  # 0.02 > 0.05 / 12


def test_lexicographic_choice_prefers_class_then_complexity_then_grid_order() -> None:
    cands = [
        {
            "name": "en",
            "model_class": "M1.elastic_net",
            "complexity": 1.0,
            "grid_index": 4,
            "admitted": True,
        },
        {
            "name": "ridge10",
            "model_class": "M1.ridge",
            "complexity": 1.2,
            "grid_index": 2,
            "admitted": True,
        },
        {
            "name": "ridge1",
            "model_class": "M1.ridge",
            "complexity": 2.5,
            "grid_index": 1,
            "admitted": True,
        },
        {
            "name": "gam",
            "model_class": "M2.gam",
            "complexity": 0.5,
            "grid_index": 0,
            "admitted": True,
        },
        {
            "name": "m0",
            "model_class": "M0.pooled_linear",
            "complexity": 1.0,
            "grid_index": 0,
            "admitted": False,
        },
    ]
    assert (
        choose_candidate(cands)["name"] == "en"
    )  # smallest class-specific complexity within M1 wins; M2 never beats M1
    assert choose_candidate([c for c in cands if c["name"] != "en"])["name"] == "ridge10"
    assert choose_candidate([dict(c, admitted=False) for c in cands]) is None
