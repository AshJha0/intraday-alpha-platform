"""v1.18 REL-13 (review finding E-G): the day pipelines resume byte-identically
after a crash at every step, choose ``--partition-instruments`` from the
available RAM when it is not given, and log peak RSS in every step.

A crash is simulated with an exception from the step's marker seam
(``_marker``): either *after* the step's work and before its marker is
written (the worst case: the work is redone on resume), or *before* the
next step starts (its marker check).  The resumed run must leave exactly the
bytes of an uninterrupted run.  Synthetic tiny inputs only."""

from __future__ import annotations

import datetime as _dt
import json
import shutil
from pathlib import Path

import pytest
from conftest import CONFIGS_DIR
from iap.marketdata import process_delete as pdm
from iap.marketdata import process_iex as pix
from iap.marketdata import runmem
from itch50_encoder import build_session
from test_process_delete import DATE as ITCH_DATE
from test_process_delete import _features_fake, _manifest

ITCH_STEPS = ["download", "ingest", "delete_raw", "features", "keep", "delete"]
IEX_STEPS = [
    "download_tops",
    "tops_extract",
    "delete_tops",
    "download_book",
    "ingest",
    "delete_raw",
    "features",
    "keep",
    "delete",
]
#: timings / free space / RSS differ run to run; everything else must match
_VOLATILE = {"process_report.json", "features.log"}


class Crash(RuntimeError):
    pass


class _FrozenDatetime:
    """``datetime`` with a fixed ``now`` (KEEP.json's ``written_utc``)."""

    UTC = _dt.UTC
    date = _dt.date

    class datetime(_dt.datetime):  # noqa: N801 - mirrors the module attribute
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=tz)


@pytest.fixture(scope="module", autouse=True)
def _frozen_clock():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pdm, "_dt", _FrozenDatetime)
        mp.setattr(pix, "_dt", _FrozenDatetime)
        yield


def _crashing_marker(real, step: str, phase: str):
    """``_marker`` that raises on the step's marker write ("after") or on its
    first marker check ("before")."""
    seen: dict[str, int] = {}

    def marker(ds: Path, name: str) -> Path:
        if name == step:
            seen[name] = seen.get(name, 0) + 1
            if (phase == "before" and seen[name] == 1) or (phase == "after" and seen[name] == 2):
                raise Crash(f"killed {phase} {step}")
        return real(ds, name)

    return marker


def _snapshot(root: Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name in _VOLATILE or p.name.startswith("_DONE_"):
            continue
        data = p.read_bytes()
        for form in {str(root), str(root).replace("\\", "\\\\"), root.as_posix()}:
            data = data.replace(form.encode(), b"<ROOT>")  # tmp dirs differ per run
        out[p.relative_to(root).as_posix()] = data
    return out


def _marker_names(root: Path) -> set[str]:
    return {p.name for p in root.rglob("_DONE_*")}


# ------------------------------------------------------------ Nasdaq ITCH


_INPUTS: dict[str, Path] = {}


def _inputs(kind: str, build) -> Path:
    """Build a case's vendor files once per process (the encoders' gzip
    headers carry the build time) so every run reads the same bytes."""
    if kind not in _INPUTS:
        import tempfile

        d = Path(tempfile.mkdtemp(prefix=f"rel13_{kind}_")) / "vendor"
        d.mkdir()
        build(d)
        _INPUTS[kind] = d
    return _INPUTS[kind]


def _itch_case(tmp_path: Path):
    def build(d: Path) -> None:
        enc, _ = build_session(9, symbols=("AAPL", "MSFT"), n_actions=400)
        enc.write(d / "12302019.NASDAQ_ITCH50.gz", compress=True)

    vendor = tmp_path / "vendor"
    shutil.copytree(_inputs("itch", build), vendor)
    src = vendor / "12302019.NASDAQ_ITCH50.gz"
    man = _manifest(tmp_path, src)
    out = tmp_path / "out"

    def go():
        return pdm.process_day(
            man,
            ITCH_DATE,
            vendor,
            out,
            ["AAPL", "MSFT"],
            configs_dir=CONFIGS_DIR,
            partition_instruments=1,
            min_free_gb=0.0,
            log=lambda line: None,
            run=_features_fake(None),
        )

    return tmp_path, go


@pytest.fixture(scope="module")
def itch_reference(tmp_path_factory):
    root, go = _itch_case(tmp_path_factory.mktemp("itch_ref"))
    go()
    return _snapshot(root), _marker_names(root)


@pytest.mark.parametrize("phase", ["after", "before"])
@pytest.mark.parametrize("step", ITCH_STEPS)
def test_process_day_resumes_byte_identically(tmp_path, monkeypatch, itch_reference, step, phase):
    if phase == "before" and step == ITCH_STEPS[0]:
        pytest.skip("nothing has run before the first step")
    ref, ref_markers = itch_reference
    root, go = _itch_case(tmp_path)
    real = pdm._marker
    monkeypatch.setattr(pdm, "_marker", _crashing_marker(real, step, phase))
    with pytest.raises(Crash):
        go()
    assert not _marker_names(root) >= {"_DONE_ALL"}
    monkeypatch.setattr(pdm, "_marker", real)
    rep = go()
    assert _snapshot(root) == ref
    assert _marker_names(root) == ref_markers
    statuses = {s["step"]: s["status"] for s in rep["steps"]}
    i = ITCH_STEPS.index(step)
    assert all(statuses[s].startswith("skipped") for s in ITCH_STEPS[:i])
    assert all(statuses[s] == "ok" for s in ITCH_STEPS[i:])


# ------------------------------------------------------------------- IEX


def _iex_case(tmp_path: Path):
    from test_process_iex import DATE, SYMS, _vendor
    from test_process_iex import _features_fake as iex_features
    from test_process_iex import _manifest as iex_manifest

    def build(d: Path) -> None:
        scratch = d.parent / "build"
        scratch.mkdir()
        v, _, _ = _vendor(scratch)
        for f in v.iterdir():
            shutil.move(str(f), d / f.name)

    vendor = tmp_path / "vendor"
    shutil.copytree(_inputs("iex", build), vendor)
    man = iex_manifest(tmp_path, vendor)
    out = tmp_path / "out"

    def go():
        return pix.process_iex_day(
            man,
            DATE,
            vendor,
            out,
            SYMS,
            configs_dir=CONFIGS_DIR,
            partition_instruments=1,
            min_free_gb=0.0,
            log=lambda line: None,
            run=iex_features,
        )

    return tmp_path, go


@pytest.fixture(scope="module")
def iex_reference(tmp_path_factory):
    root, go = _iex_case(tmp_path_factory.mktemp("iex_ref"))
    go()
    return _snapshot(root), _marker_names(root)


@pytest.mark.slow
@pytest.mark.parametrize("phase", ["after", "before"])
@pytest.mark.parametrize("step", IEX_STEPS)
def test_process_iex_day_resumes_byte_identically(
    tmp_path, monkeypatch, iex_reference, step, phase
):
    if phase == "before" and step == IEX_STEPS[0]:
        pytest.skip("nothing has run before the first step")
    ref, ref_markers = iex_reference
    root, go = _iex_case(tmp_path)
    real = pix._marker
    monkeypatch.setattr(pix, "_marker", _crashing_marker(real, step, phase))
    with pytest.raises(Crash):
        go()
    monkeypatch.setattr(pix, "_marker", real)
    rep = go()
    assert _snapshot(root) == ref
    assert _marker_names(root) == ref_markers
    statuses = {s["step"]: s["status"] for s in rep["steps"]}
    i = IEX_STEPS.index(step)
    assert all(statuses[s].startswith("skipped") for s in IEX_STEPS[:i])
    assert all(statuses[s] == "ok" for s in IEX_STEPS[i:])


# ------------------------------------------------- partitioning and RSS


@pytest.mark.parametrize(
    ("ram", "n_symbols", "want", "source"),
    [
        (8.0, 100, 10, "auto"),  # the hand-set value at the target footprint
        (16.0, 100, 26, "auto"),
        (64.0, 1000, runmem.AUTO_MAX_INSTRUMENTS, "auto"),
        (3.2, 100, 1, "auto"),  # never below one instrument
        (16.0, 3, 3, "auto"),  # never more than the universe
        (None, 100, 10, "auto_default"),  # RAM unknown: the old default
        (None, 2, 2, "auto_default"),
    ],
)
def test_auto_partition_instruments(ram, n_symbols, want, source):
    got = runmem.auto_partition_instruments(n_symbols, ram)
    assert got["instruments"] == want and got["source"] == source


def test_resolve_partition_given_value_is_unchanged(monkeypatch):
    monkeypatch.setattr(pdm, "available_ram_gb", lambda: 1000.0)
    assert pdm.resolve_partition(7, 100) == (
        7,
        {"instruments": 7, "source": "given", "available_ram_gb": None},
    )
    assert pdm.resolve_partition(None, 100)[0] is None
    n, entry = pdm.resolve_partition("auto", 100)
    assert n == runmem.AUTO_MAX_INSTRUMENTS and entry["available_ram_gb"] == 1000.0
    with pytest.raises(ValueError):
        pdm.resolve_partition("ten", 100)


def test_cli_partition_flag_auto_by_default_and_given_passthrough(tmp_path, monkeypatch):
    seen: list = []

    def fake(*args, **kw):
        seen.append(kw["partition_instruments"])
        return {"steps": []}

    monkeypatch.setattr(pdm, "process_day", fake)
    base = ["--manifest", "m", "--date", ITCH_DATE, "--vendor-dir", "v", "--out-root", "o"]
    base += ["--symbols", "AAPL"]
    assert pdm.main(base) == 0
    assert pdm.main([*base, "--partition-instruments", "4"]) == 0
    assert seen == ["auto", 4]
    with pytest.raises(SystemExit):
        pdm.main([*base, "--partition-instruments", "0"])
    # the rendered wrapper passes a given value through and omits auto
    syms = tmp_path / "u.json"
    syms.write_text(json.dumps(["AAPL"]), encoding="utf-8")
    r = [*base[:-2], "--symbols-file", str(syms), "--render-cmd", str(tmp_path / "a.cmd")]
    assert pdm.main(r) == 0
    assert "--partition-instruments" not in (tmp_path / "a.cmd").read_text()
    r[-1] = str(tmp_path / "b.cmd")
    assert pdm.main([*r, "--partition-instruments", "4"]) == 0
    assert "--partition-instruments 4" in (tmp_path / "b.cmd").read_text()


def test_peak_rss_fields_with_a_fake_probe():
    assert runmem.peak_rss_fields(lambda: {"self": 7_500_000_000, "children": None}) == {
        "peak_rss_gb": 7.5,
        "peak_rss_children_gb": None,
    }
    assert runmem.peak_rss_fields(lambda: {"self": None, "children": 2_000_000})[
        "peak_rss_children_gb"
    ] == pytest.approx(0.002)
    real = runmem.peak_rss_bytes()
    assert real["self"] is None or real["self"] > 0


def test_every_step_logs_peak_rss(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pdm,
        "peak_rss_fields",
        lambda: {"peak_rss_gb": 1.25, "peak_rss_children_gb": None},
    )
    root, go = _itch_case(tmp_path)
    rep = go()
    assert [s["step"] for s in rep["steps"]] == ITCH_STEPS
    assert all(s["peak_rss_gb"] == 1.25 for s in rep["steps"])
    assert rep["partition"] == {"instruments": 1, "source": "given", "available_ram_gb": None}
    on_disk = json.loads((root / "out" / "ds_20191230" / "process_report.json").read_text())
    assert all("peak_rss_gb" in s and "peak_rss_children_gb" in s for s in on_disk["steps"])
