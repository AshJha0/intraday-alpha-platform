"""v1.14 engineering: event cursor, shared replay, study base, registry content hash.

Every new path is pinned against the path it replaces: same events, same
maker results, same unit bytes, unchanged model ids and record hashes.
"""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from iap.backtest.maker import MakerBacktester, MakerConfig, MakerFilter
from iap.core.events import MarketEvent
from iap.mlops import ModelRegistry, TamperError
from iap.mlops.registry import CONTENT, content_sha256
from iap.research.events import EVENT_COLUMNS, EventColumns, read_event_columns
from iap.research.replay import GridCell, SharedReplay
from iap.research.study import StudyBase, cluster_stats, dumps_unit, unit_name
from test_maker_economics import INS, exec_config, make_scores, synth_events
from test_mlops import META, _data


def _mixed(seed: int = 1) -> list[MarketEvent]:
    """The synthetic stream with a second instrument's copy interleaved."""
    evs = synth_events(1500, seed=seed)
    other = [
        dataclasses.replace(e, instrument_id=INS + 1, event_id=e.event_id + 10**6) for e in evs
    ]
    out: list[MarketEvent] = []
    for a, b in zip(evs, other, strict=True):
        out += [b, a]
    return out


# ------------------------------------------------------------ E-P1-3 cursor


@pytest.mark.parametrize("chunk", [1, 7, 65_536])
def test_event_columns_round_trip(chunk):
    evs = synth_events(800)
    cols = EventColumns.from_events(evs, chunk=chunk)
    assert len(cols) == len(evs)
    assert list(cols) == evs
    assert cols[0] == evs[0] and cols[-1] == evs[-1]
    assert all(type(getattr(e, f)) is int for e in cols[:3] for f in EVENT_COLUMNS)
    assert cols[10:20].to_list() == evs[10:20]
    with pytest.raises(IndexError):
        cols[len(evs)]
    assert cols.nbytes == 8 * len(EVENT_COLUMNS) * len(evs)


def test_read_event_columns_matches_row_filter(tmp_path):
    evs = _mixed()
    table = pa.table({c: [getattr(e, c) for e in evs] for c in EVENT_COLUMNS})
    path = tmp_path / "events.parquet"
    pq.write_table(table, path, row_group_size=257)
    end = evs[len(evs) // 2].exchange_ts
    want = [e for e in evs if e.instrument_id == INS and e.exchange_ts <= end]
    assert read_event_columns(path, INS, end).to_list() == want
    assert read_event_columns(path, INS).to_list() == [e for e in evs if e.instrument_id == INS]
    assert len(read_event_columns(path, 999)) == 0


# ------------------------------------------------------- E-P1-2 shared replay


def _summary(res):
    s = res.summary()
    t = res.trips
    return json.dumps(s, sort_keys=True, default=str), t.to_json(orient="split")


def test_shared_replay_identical_to_independent_runs():
    evs = _mixed()
    scores = make_scores([e for e in evs if e.instrument_id == INS])
    rng = np.random.default_rng(3)
    allow = rng.random(len(scores)) < 0.6
    h = 1_000_000_000
    cells = {
        "taker/gated": GridCell(MakerConfig(qty=100, ttl_ns=h, horizon_ns=h)),
        "taker/ungated": GridCell(MakerConfig(qty=100, margin_bps=-1e9)),
        "taker/filter": GridCell(MakerConfig(qty=100), allow),
        "passive/gated": GridCell(
            MakerConfig(qty=100, exit="passive", exit_timeout_ns=h, exit_reprices=1)
        ),
    }
    shared = SharedReplay(evs, INS)
    assert len(shared) == len(evs) // 2
    got = shared.run_grid(exec_config(50_000), cells, scores)
    assert list(got) == list(cells)
    for key, cell in cells.items():
        ref = MakerBacktester(exec_config(50_000), cell.config).run_instrument(
            evs, scores, INS, allow=cell.allow
        )
        assert _summary(got[key]) == _summary(ref), key
    with pytest.raises(ValueError, match="no event"):
        SharedReplay(evs, 999).run(exec_config(), MakerConfig(), scores)


# --------------------------------------------------------- E-P1-4 study base


class _Toy(StudyBase):
    @staticmethod
    def prereg_key(cell):
        return cell["alpha"], f"{cell['horizon']}:toy"

    def run_unit_doc(self, day, symbol):
        return {"session": self.sessions[day], "symbol": symbol, "x": [day, 0.5]}


def test_study_base_loop_and_checkpoints(tmp_path):
    pre = tmp_path / "prereg.json"
    pre.write_text(json.dumps({"cells": [{"alpha": "EQ01", "horizon": "1s"}]}), encoding="utf-8")
    st = _Toy(tmp_path / "out", pre, ("s1", "s2"), {"AAPL": 1, "MSFT": 2}, logger=lambda m: None)
    assert len(st.prereg_sha) == 64
    assert list(st.units()) == [(0, "AAPL"), (0, "MSFT"), (1, "AAPL"), (1, "MSFT")]
    assert st.run() == 4 and st.run() == 0  # second pass is all checkpoints
    p = st.unit_path(1, "MSFT")
    assert p.name == f"{unit_name('s2', 'MSFT')}.json"
    doc = {"session": "s2", "symbol": "MSFT", "x": [1, 0.5]}
    assert p.read_bytes().decode() == json.dumps(doc, indent=2, sort_keys=True) + "\n"
    assert dumps_unit(doc) == p.read_text(encoding="utf-8")
    assert len(st.read_units()) == 4


def test_cluster_stats_pinned():
    s = cluster_stats([1.0, 2.0, None, float("nan"), 3.0], n_tests=2)
    assert s["n_sessions"] == 3 and s["mean"] == 2.0
    assert s["se"] == pytest.approx(1 / np.sqrt(3))
    assert s["ci_bonf"][0] < s["ci95"][0] < 2.0 < s["ci95"][1] < s["ci_bonf"][1]
    assert cluster_stats([1.0])["se"] is None


# ------------------------------------------------- E-P2-5 registry content hash


def _fit(seed=0):
    X, y = _data(seed=seed)
    return MakerFilter("meta_gbm", tau=0.5).fit(X, y)


def test_content_hash_stable_and_version_free(tmp_path):
    a, b = _fit()._model, _fit()._model
    assert content_sha256(a) == content_sha256(b)
    assert content_sha256(a) != content_sha256(_fit(seed=1)._model)
    # the library version stamp is pickled state but not model state
    from sklearn.linear_model import LogisticRegression

    est = LogisticRegression().fit(*_data(n=200))
    assert "_sklearn_version" in est.__getstate__()
    before = content_sha256(est)
    est._sklearn_version = "0.0.0"
    assert content_sha256(est) == before
    est.coef_ = est.coef_ + 1e-12
    assert content_sha256(est) != before


def test_registry_sidecar_keeps_digests(tmp_path):
    reg_a = tmp_path / "a"
    rec = _fit().register(reg_a, exploratory=True, **META)
    reg = ModelRegistry(reg_a)
    d = reg_a / rec.model_id
    record_bytes = (d / "record.json").read_bytes()
    assert json.loads(record_bytes) == rec.to_dict()
    assert "content_sha256" not in rec.to_dict()  # record/id/record_hash unchanged
    digest = reg.content_hash(rec.model_id)
    assert digest == content_sha256(_fit()._model)
    assert [r.model_id for r in reg.find_by_content(digest)] == [rec.model_id]
    assert reg.verify_content(rec.model_id) == digest
    # an older record without a sidecar is backfilled, not rewritten
    (d / CONTENT).unlink()
    assert reg.content_hash(rec.model_id) is None
    assert reg.verify_content(rec.model_id) == digest
    assert (d / "record.json").read_bytes() == record_bytes
    (d / CONTENT).write_text(json.dumps({"content_sha256": "0" * 64, "scheme": 1}))
    with pytest.raises(TamperError, match="content hash"):
        reg.verify_content(rec.model_id)
