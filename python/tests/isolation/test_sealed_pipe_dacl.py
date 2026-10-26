"""The sealed services' pipe DACL (run 40307510915: the runner got PermissionError).

Windows' default named-pipe DACL gives full control to SYSTEM, Administrators and
the creator, and read only to everyone else, so a standard account such as
``iap-runner`` could not open the pipe for writing.  The service now creates the
pipe with an explicit DACL for its allowed accounts (``pipe_sddl``).  The last
test serves a real pipe on Windows, as the current user, with the current user as
the only allowed account, and talks to it through the job's own client code.
"""

from __future__ import annotations

import dataclasses
import getpass
import os
import sys
import threading

import pytest
from sealed_fixtures import load_tool, make_service

ss = load_tool("sealed_service", "tools/sealed_service.py")


def test_sddl_grants_read_write_to_the_allowed_sids_only():
    sddl = ss.pipe_sddl(["S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1002"])
    assert sddl.startswith("D:P"), "protected: no inherited entries"
    assert "(A;;GA;;;SY)" in sddl and "(A;;GA;;;OW)" in sddl
    for sid in ("S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1002", "BA"):
        assert f"(A;;0x12019f;;;{sid})" in sddl
    for broad in (";;;WD)", ";;;AU)", ";;;BU)", ";;;AN)"):
        assert broad not in sddl, "no Everyone, Authenticated Users, Users or Anonymous"


@pytest.mark.parametrize("bad", [[], ["iap-runner"], ["S-1-5-21-1;;;WD"], ["WD"]])
def test_sddl_refuses_anything_but_string_sids(bad):
    with pytest.raises(ValueError):
        ss.pipe_sddl(bad)


@pytest.mark.skipif(sys.platform != "win32", reason="named pipes and SIDs are Windows-only")
def test_a_real_pipe_serves_the_allowed_account(tmp_path):
    me = getpass.getuser()
    _, svc, _, _, _ = make_service(tmp_path, "b")
    svc.cfg = dataclasses.replace(svc.cfg, allowed_accounts=frozenset({me}))
    name = rf"\\.\pipe\iap-test-{os.getpid()}-{tmp_path.name}"
    t = threading.Thread(target=ss.serve_pipe, args=(svc, name), kwargs={"once": True}, daemon=True)
    t.start()
    try:
        import time

        resp = None
        for _ in range(50):  # the server thread creates the pipe asynchronously
            try:
                resp, _ = ss.request_over_pipe(name, {"op": "status"})
                break
            except FileNotFoundError:
                time.sleep(0.1)
        assert resp is not None, "the pipe was never created"
        assert resp["ok"] is True and resp["partition"] == "b"
    finally:
        t.join(timeout=10)
    assert not t.is_alive()
    import json

    lines = svc.audit.path.read_text(encoding="utf-8").splitlines()
    created = [r for r in map(json.loads, lines) if r.get("event") == "pipe"]
    assert created and "0x12019f" in created[-1]["sddl"]
