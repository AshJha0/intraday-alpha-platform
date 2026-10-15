"""tools/r7_watch_nasdaq.py: listing parse, classification, IEX HIST index, exit codes.

No network: every fetch is injected.
"""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest
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


def _no_iex(*_a, **_k):
    return "[]"


TODAY = w.date(2026, 10, 15)
IEX_DOC = json.dumps(
    [
        {
            "link": "https://example.invalid/20261014_IEXTP1_DEEP1.0.pcap.gz",
            "date": "20261014",
            "feed": "DEEP",
            "version": "1.0",
            "protocol": "IEXTP1",
            "size": "2147483648",
        },
        {
            "link": "https://example.invalid/20261014_IEXTP1_TOPS1.6.pcap.gz",
            "date": "20261014",
            "feed": "TOPS",
            "version": "1.6",
            "protocol": "IEXTP1",
            "size": 123456789,
        },
        {
            "link": "https://example.invalid/20261014_IEXTP1_DEEP+1.0.pcap.gz",
            "date": "20261014",
            "feed": "DEEP+",
            "version": "1.0",
            "protocol": "IEXTP1",
            "size": "99",
        },
        {
            "link": "https://example.invalid/x",
            "date": "20261014",
            "feed": "OTHER",
            "version": "1",
            "protocol": "IEXTP1",
            "size": "1",
        },
    ]
)


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
    rc = w.main(["--listing-file", str(FIXTURE)], fetch=_no_net, fetch_iex=_no_iex)
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
        fetch_iex=_no_iex,
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
    args = ["--known", str(known), "--update-known"]
    assert w.main(args, fetch=lambda u, t: page, fetch_iex=_no_iex) == 10
    doc = json.loads(known.read_text())
    assert len(doc["entries"]) == n0 + 1
    assert w.main(["--known", str(known)], fetch=lambda u, t: page, fetch_iex=_no_iex) == 0


def test_errors_exit_1(tmp_path):
    def boom(*_a):
        raise OSError("down")

    k = str(tmp_path / "k.json")
    assert w.main(["--known", k], fetch=boom, fetch_iex=_no_iex) == w.EXIT_ERROR
    assert w.main(["--known", k], fetch=lambda u, t: "<html/>", fetch_iex=_no_iex) == 1


# --- IEX HIST index (v1.17 WS0 item 0.9) ---


def test_iex_dates_are_utc_today_minus_1_to_7():
    ds = w.iex_dates(TODAY)
    assert ds == [
        "20261014",
        "20261013",
        "20261012",
        "20261011",
        "20261010",
        "20261009",
        "20261008",
    ]
    assert w.iex_dates(TODAY, 2) == ["20261014", "20261013"]


def test_parse_iex_index_keeps_deep_and_tops_with_sizes():
    feeds = w.parse_iex_index(IEX_DOC, "20261014")
    assert [(f["feed"], f["version"], f["size"]) for f in feeds] == [
        ("DEEP", "1.0", 99),  # DEEP+ normalised to DEEP
        ("DEEP", "1.0", 2147483648),
        ("TOPS", "1.6", 123456789),
    ]
    assert all(f["protocol"] == "IEXTP1" for f in feeds)
    # the undated form (dict keyed by date) and empty bodies are accepted
    assert w.parse_iex_index(json.dumps({"20261014": json.loads(IEX_DOC)}), "20261014") == feeds
    assert w.parse_iex_index(json.dumps({"20261013": []}), "20261014") == []
    assert w.parse_iex_index("", "20261014") == []
    with pytest.raises(ValueError):
        w.parse_iex_index('"nope"', "20261014")


def test_iex_report_one_get_per_day_errors_informational():
    calls = []

    def fetch_iex(d, timeout):
        calls.append((d, timeout))
        if d == "20261014":
            return IEX_DOC
        if d == "20261012":
            raise OSError("HTTP 404")
        return "[]"

    rep = w.iex_report(w.iex_dates(TODAY), {"2026-10-09"}, fetch_iex, timeout=7.5)
    assert [c[0] for c in calls] == w.iex_dates(TODAY) and all(c[1] == 7.5 for c in calls)
    assert rep["days_with_data"] == ["2026-10-14"] and rep["new_days"] == ["2026-10-14"]
    by = {r["date"]: r for r in rep["days"]}
    assert by["2026-10-12"]["error"].startswith("OSError") and by["2026-10-12"]["feeds"] == []
    assert by["2026-10-13"]["error"] is None
    text = "\n".join(w.summarize_iex(rep))
    assert "1 of the last 7 days" in text and "2026-10-14 NEW: DEEP 1.0 99 B" in text
    assert "TOPS 1.6 123,456,789 B" in text and "error OSError" in text
    assert w.summarize_iex(None) == []


def test_main_iex_is_informational_and_in_json_and_notify(tmp_path):
    known = tmp_path / "known.json"
    known.write_text(json.dumps(w.known_doc(w.parse_listing(HTML), "2026-10-11")))
    out, sent = tmp_path / "r.json", []
    fetch_iex = lambda d, t: IEX_DOC if d == "20261014" else "[]"  # noqa: E731
    # nothing new on Nasdaq, a new IEX day: exit 0, iex_new, a toast
    rc = w.main(
        ["--known", str(known), "--json-out", str(out), "--notify"],
        fetch=lambda u, t: HTML,
        notifier=lambda t, m: sent.append((t, m)),
        fetch_iex=fetch_iex,
        today=TODAY,
    )
    assert rc == w.EXIT_NOTHING
    rep = json.loads(out.read_text())
    assert rep["iex_new"] is True and rep["iex"]["days_with_data"] == ["2026-10-14"]
    assert len(rep["iex"]["days"]) == 7
    assert all("link" in f for r in rep["iex"]["days"] for f in r["feeds"])
    assert sent == [("1 new IEX HIST day(s)", "IEX: 2026-10-14")]
    # --update-known records the IEX day; the next run is quiet
    assert (
        w.main(
            ["--known", str(known), "--update-known"],
            fetch=lambda u, t: HTML,
            fetch_iex=fetch_iex,
            today=TODAY,
        )
        == 0
    )
    assert json.loads(known.read_text())["iex_days"] == ["2026-10-14"]
    sent.clear()
    rc = w.main(
        ["--known", str(known), "--json-out", str(out), "--notify"],
        fetch=lambda u, t: HTML,
        notifier=lambda t, m: sent.append((t, m)),
        fetch_iex=fetch_iex,
        today=TODAY,
    )
    assert rc == 0 and sent == [] and json.loads(out.read_text())["iex_new"] is False
    # both new: one toast naming both, exit 10 from Nasdaq
    known.write_text(json.dumps(w.known_doc(w.parse_listing(HTML), "2026-10-11")))
    rc = w.main(
        ["--known", str(known), "--notify"],
        fetch=lambda u, t: HTML.replace("</pre>", NEW_ROW + "</pre>"),
        notifier=lambda t, m: sent.append((t, m)),
        fetch_iex=fetch_iex,
        today=TODAY,
    )
    assert rc == w.EXIT_NEW
    assert sent == [
        ("1 new Nasdaq ITCH day(s); 1 new IEX HIST day(s)", "Nasdaq: 2026-09-29; IEX: 2026-10-14")
    ]
    # --no-iex: no IEX GET at all
    rc = w.main(
        ["--known", str(known), "--json-out", str(out), "--no-iex"],
        fetch=lambda u, t: HTML,
        fetch_iex=_no_net,
    )
    assert rc == 0 and json.loads(out.read_text())["iex"] is None
