"""tools/r7_watch_nasdaq.py: listing parse, classification, exit codes. No network."""

from __future__ import annotations

import importlib.util
import json
import sys

from conftest import REPO_ROOT

FIXTURE = REPO_ROOT / "python" / "tests" / "fixtures" / "nasdaq_itch_listing.html"


def _load():
    spec = importlib.util.spec_from_file_location(
        "r7_watch_nasdaq", REPO_ROOT / "tools/r7_watch_nasdaq.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["r7_watch_nasdaq"] = mod
    spec.loader.exec_module(mod)
    return mod


w = _load()
HTML = FIXTURE.read_text(encoding="utf-8")
NEW_ROW = (
    ' 9/30/2026  2:00 PM  15000000000 <A HREF="/ITCH/Nasdaq%20ITCH/S092926-v50.txt.gz">'
    "S092926-v50.txt.gz</A><br>"
)


def _no_net(*_a, **_k):
    raise AssertionError("network used in a test")


def test_parse_fixture():
    e = {x["name"]: x for x in w.parse_listing(HTML)}
    assert e["S061226-v50.txt.gz"]["bytes"] == 17894268560
    assert e["S061226-v50.txt.gz"]["listed"] == "2026-06-23T15:17"
    assert e["NOII"]["is_dir"] and e["NOII"]["bytes"] is None
    assert e["FEB 2022 Files"]["is_dir"]


def test_session_dates():
    assert w.session_date("S121025-v50.txt.gz") == "2025-12-10"
    assert w.session_date("01302019.NASDAQ_ITCH50.gz") == "2019-01-30"
    assert w.session_date("itch50_05_18.gz", "2026-06-09T15:30") == "2026-05-18"
    assert w.session_date("01302019.NASDAQ_ITCH50.gz.md5sum") is None
    assert w.session_date("S010303-v2.zip") is None


def test_committed_known_list_matches_fixture(capsys):
    # The committed known-list was built from this listing: nothing is new.
    rc = w.main(["--listing-file", str(FIXTURE)], fetch=_no_net)
    assert rc == w.EXIT_NOTHING
    assert "nothing new" in capsys.readouterr().out


def test_new_usable_day_and_notify(tmp_path):
    known = tmp_path / "known.json"
    known.write_text(json.dumps(w.known_doc(w.parse_listing(HTML), "2026-10-11")))
    sent = []
    out = tmp_path / "r.json"
    rc = w.main(
        ["--known", str(known), "--json-out", str(out), "--notify"],
        fetch=lambda url, timeout: HTML.replace("</pre>", NEW_ROW + "</pre>"),
        notifier=lambda t, m: sent.append((t, m)),
    )
    assert rc == w.EXIT_NEW
    rep = json.loads(out.read_text())
    (day,) = rep["usable_new_days"]
    assert day["session"] == "2026-09-29" and day["weekday"] == "Tue"
    assert not day["spent"] and not day["candidate"]
    assert sent and "2026-09-29" in sent[0][1]


def test_spent_candidate_weekend_and_size_change():
    known = {"entries": [{"name": "S061226-v50.txt.gz", "bytes": 1}]}
    entries = [
        {"name": "S061226-v50.txt.gz", "bytes": 2, "listed": "2026-06-23T15:17", "is_dir": False},
        {"name": "05152026.NASDAQ_ITCH50.gz", "bytes": 9, "listed": "x", "is_dir": False},
        {"name": "S121025-v50.txt.gz", "bytes": 9, "listed": "x", "is_dir": False},
        {"name": "S101026-v50.txt.gz", "bytes": 9, "listed": "x", "is_dir": False},  # Saturday
        {"name": "notes.txt", "bytes": 9, "listed": "x", "is_dir": False},
    ]
    r = w.compare(entries, known, {"2025-12-10"})
    assert r["changed_size"] == [{"name": "S061226-v50.txt.gz", "known_bytes": 1, "bytes": 2}]
    by = {x["name"]: x for x in r["new"]}
    assert by["05152026.NASDAQ_ITCH50.gz"]["spent"]
    assert by["S121025-v50.txt.gz"]["candidate"]
    assert not by["S101026-v50.txt.gz"]["is_weekday"]
    assert not by["notes.txt"]["itch50_full_day"]
    assert r["usable_new_days"] == []


def test_update_known_appends(tmp_path):
    known = tmp_path / "known.json"
    known.write_text(json.dumps(w.known_doc(w.parse_listing(HTML), "2026-10-11")))
    n0 = len(json.loads(known.read_text())["entries"])
    page = HTML.replace("</pre>", NEW_ROW + "</pre>")
    assert w.main(["--known", str(known), "--update-known"], fetch=lambda u, t: page) == 10
    doc = json.loads(known.read_text())
    assert len(doc["entries"]) == n0 + 1
    assert w.main(["--known", str(known)], fetch=lambda u, t: page) == 0


def test_errors_exit_1(tmp_path):
    def boom(*_a):
        raise OSError("down")

    assert w.main(["--known", str(tmp_path / "k.json")], fetch=boom) == w.EXIT_ERROR
    assert w.main(["--known", str(tmp_path / "k.json")], fetch=lambda u, t: "<html/>") == 1
