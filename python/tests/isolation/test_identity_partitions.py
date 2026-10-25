"""D2: every identity x partition pair (U0 section 2), five operations each:
read, write, enumerate, indirect access through a generated report path and
through a cache directory.

Expected matrix: an identity may do all five on its OWN partition and none on
any other; the development identity (the owner's normal user) may do none on
any partition.  The probe is ``tools/admin/isolation_probe.py``.

How each identity runs:

* development: the probe runs in this process when ``C:\\iap-sealed`` exists
  (the owner's workstation after ``tools/admin/create_sealed_accounts.ps1``);
  skipped in CI, where the directory does not exist.
* ``iap-sealed-a`` / ``-b`` / ``-c`` / ``iap-runner``: a runas-style harness
  starts the probe under that account with ``Start-Process -Credential`` (the
  Secondary Logon service), reading the account passwords from the JSON file
  named by ``IAP_ISOLATION_CREDENTIALS`` (``{"iap-sealed-a": "...", ...}``,
  outside the repository, owner-only ACL).  Skipped when the variable, the
  file, the accounts or the partitions are absent (CI and any machine that is
  not the owner's).  ``IAP_ISOLATION_PYTHON`` names an interpreter the
  accounts can execute (default: this one).

The probe never prints contents or listings, so this test never moves a
sealed byte anywhere; the partitions are expected to be empty or to hold
only the service's own state.

The matrix logic (:func:`expected`, :func:`violations`) is unit-tested on
synthetic probe output so CI proves the harness itself, not only the skip."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT

PROBE = REPO_ROOT / "tools" / "admin" / "isolation_probe.py"
ROOT = Path(os.environ.get("IAP_SEALED_ROOT", r"C:\iap-sealed"))
OWNERS = {"a": "iap-sealed-a", "b": "iap-sealed-b", "c": "iap-sealed-c", "runner": "iap-runner"}
ACCOUNTS = tuple(OWNERS.values())
OPERATIONS = ("read", "write", "enumerate", "report", "cache")
PAIRS = [(who, part) for who in ("dev", *ACCOUNTS) for part in OWNERS]


def expected(identity: str, partition: str) -> str:
    return "ok" if OWNERS[partition] == identity else "denied"


def violations(identity: str, report: dict) -> list[str]:
    out = []
    for part, want_owner in OWNERS.items():
        got = report["partitions"].get(part)
        if got is None:
            out.append(f"{identity} x {part}: partition not probed")
            continue
        want = expected(identity, part)
        for op in OPERATIONS:
            if got.get(op) != want:
                out.append(f"{identity} x {part}: {op} = {got.get(op)!r}, expected {want!r}")
    return out


# ---------------------------------------------------------------- the harness --
def _accounts_present() -> bool:
    if sys.platform != "win32" or not ROOT.is_dir():
        return False
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "(Get-LocalUser).Name -join ','"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    names = set(out.stdout.strip().split(","))
    return all(a in names for a in ACCOUNTS)


def _credentials() -> dict[str, str] | None:
    p = os.environ.get("IAP_ISOLATION_CREDENTIALS")
    if not p or not Path(p).is_file():
        return None
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except ValueError:
        return None


def run_probe_as(account: str, password: str, out_dir: Path) -> dict:
    """Start the probe under ``account`` and read its JSON from a directory both
    users can reach (``%PUBLIC%``)."""
    python = os.environ.get("IAP_ISOLATION_PYTHON", sys.executable)
    out_file = out_dir / f"probe-{account}.json"
    if out_file.exists():
        out_file.unlink()
    script = f"""
$ErrorActionPreference = 'Stop'
$sec = ConvertTo-SecureString -String $env:IAP_PROBE_PASSWORD -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential ('{account}', $sec)
Start-Process -FilePath '{python}' -ArgumentList @('-I', '{PROBE}', '--root', '{ROOT}', '--out', '{out_file}') `
  -Credential $cred -WorkingDirectory '{out_dir}' -Wait -WindowStyle Hidden
"""
    env = dict(os.environ, IAP_PROBE_PASSWORD=password)
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=300,
    )
    if not out_file.is_file():
        pytest.fail(
            f"probe under {account} produced no report (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:500]}"
        )
    doc = json.loads(out_file.read_text(encoding="utf-8"))
    out_file.unlink()
    return doc


@pytest.fixture(scope="module")
def reports() -> dict[str, dict]:
    """One probe run per identity (the harness is slow); pairs read from it."""
    if not _accounts_present():
        pytest.skip(f"sealed accounts / {ROOT} absent: not the owner's workstation")
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("isolation_probe", PROBE)
    probe = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(probe)
    out = {"dev": probe.run(ROOT)}
    if out["dev"]["identity"] in ACCOUNTS:
        pytest.skip("the test process is a sealed identity, not the development identity")
    creds = _credentials()
    if creds:
        public = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "iap-isolation"
        public.mkdir(parents=True, exist_ok=True)
        for account in ACCOUNTS:
            if account in creds:
                out[account] = run_probe_as(account, creds[account], public)
    return out


@pytest.mark.parametrize("identity, partition", PAIRS, ids=[f"{w}-x-{p}" for w, p in PAIRS])
def test_identity_partition_pair(reports, identity, partition):
    if identity not in reports:
        pytest.skip(
            f"no credentials for {identity} (set IAP_ISOLATION_CREDENTIALS to run the "
            "sealed and runner identities)"
        )
    rep = reports[identity]
    if identity != "dev":
        assert rep["identity"] == identity, "the probe did not run under the requested account"
    got = rep["partitions"][partition]
    want = expected(identity, partition)
    bad = {op: got.get(op) for op in OPERATIONS if got.get(op) != want}
    assert bad == {}, f"{identity} on partition {partition}: {bad} (expected {want!r} for all)"


# ------------------------------------------------ the matrix logic, in CI too --
def _synthetic(identity: str, leak: tuple[str, str] | None = None) -> dict:
    parts = {
        p: {op: expected(identity, p) for op in OPERATIONS} for p in OWNERS
    }
    if leak:
        parts[leak[0]][leak[1]] = "ok"
    return {"identity": identity, "partitions": parts}


def test_expected_matrix_is_one_owner_per_partition_and_dev_nowhere():
    for part, owner in OWNERS.items():
        assert expected(owner, part) == "ok"
        assert expected("dev", part) == "denied"
        for other in ACCOUNTS:
            if other != owner:
                assert expected(other, part) == "denied"


@pytest.mark.parametrize("identity", ["dev", *ACCOUNTS])
def test_violations_catch_every_leak(identity):
    assert violations(identity, _synthetic(identity)) == []
    for part in OWNERS:
        if OWNERS[part] == identity:
            continue
        for op in OPERATIONS:
            bad = violations(identity, _synthetic(identity, leak=(part, op)))
            assert bad == [f"{identity} x {part}: {op} = 'ok', expected 'denied'"]
    assert violations(identity, {"partitions": {}}) == [
        f"{identity} x {p}: partition not probed" for p in OWNERS
    ]


def test_probe_reports_ok_on_a_writable_tree_and_touches_nothing_lasting(tmp_path):
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("isolation_probe", PROBE)
    probe = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(probe)
    for p in OWNERS:
        (tmp_path / p).mkdir()
    (tmp_path / "a" / "seed.bin").write_bytes(b"\1")
    doc = probe.run(tmp_path)
    assert set(doc["partitions"]) == set(OWNERS)
    assert all(v == "ok" for part in doc["partitions"].values() for v in part.values())
    assert sorted(x.name for x in tmp_path.rglob("*")) == ["a", "b", "c", "runner", "seed.bin"]
    assert doc["partitions"]["runner"]["read"] == "ok"  # empty partition: via the probe's marker
    assert probe.run(tmp_path / "nowhere")["partitions"]["a"]["enumerate"] == "missing"


def test_probe_output_holds_outcomes_only(tmp_path):
    """No listing, no content: every value of the report is an outcome word."""
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("isolation_probe", PROBE)
    probe = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(probe)
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "SECRET-NAME.csv").write_text("secret,content")
    doc = probe.run(tmp_path, ("a",))
    text = json.dumps(doc)
    assert "SECRET" not in text and "content" not in text
    assert all(
        v in ("ok", "denied", "missing") or v.startswith("error:")
        for part in doc["partitions"].values()
        for v in part.values()
    )
