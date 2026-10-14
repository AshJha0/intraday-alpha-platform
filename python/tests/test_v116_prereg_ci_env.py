"""v1.16: registration-time check of local deps against the CI lock."""

from __future__ import annotations

import io

import pytest
from iap.agents import fingerprint as fp

LOCK = """\
# pip-compile output
numpy==2.4.6 \
    --hash=sha256:aaaa \
    --hash=sha256:bbbb
    # via -r requirements-ci.in, pandas, scipy
Pandas==3.0.6 ; python_version >= "3.10" \
    --hash=sha256:cccc
scipy==1.17.1
    # via scikit-learn
scikit-learn==1.7.0
"""

CI = {"numpy": "2.4.6", "pandas": "3.0.6", "scipy": "1.17.1"}


@pytest.fixture
def lock(tmp_path):
    p = tmp_path / "requirements-ci.txt"
    p.write_text(LOCK, encoding="utf-8")
    return p


def test_parse_fixture_lock(lock):
    assert fp.ci_lock_deps(lock) == CI


def test_parse_missing_pin(tmp_path):
    p = tmp_path / "r.txt"
    p.write_text("numpy==2.4.6\n", encoding="utf-8")
    assert fp.ci_lock_deps(p) == {"numpy": "2.4.6", "pandas": None, "scipy": None}


def test_real_lock_pins_all():
    assert all(fp.ci_lock_deps().values())


def test_compare():
    assert fp.compare_deps(CI, CI) == []
    local = {**CI, "numpy": "2.5.3", "scipy": "1.18.1"}
    assert fp.compare_deps(local, CI) == [
        ("numpy", "2.5.3", "2.4.6"),
        ("scipy", "1.18.1", "1.17.1"),
    ]


def test_check_match(lock):
    out = io.StringIO()
    assert fp.check_registration_env(lock=lock, local=dict(CI), out=out) is True
    assert "OK" in out.getvalue()


def test_check_mismatch_warns(lock):
    out = io.StringIO()
    local = {**CI, "numpy": "2.5.3"}
    assert fp.check_registration_env(lock=lock, local=local, out=out) is False
    assert "WARNING" in out.getvalue() and "numpy local=2.5.3 ci=2.4.6" in out.getvalue()


def test_check_mismatch_refuses(lock):
    with pytest.raises(SystemExit) as e:
        fp.check_registration_env(True, lock=lock, local={**CI, "pandas": None}, out=io.StringIO())
    assert e.value.code == 2
