#!/usr/bin/env python3
"""DD1 events: the EDGAR earnings-announcement calendar (8-K item 2.02) for D4, sealed like DD1.

    python tools/ingest/dd1_events.py run --year 2026 --dev-root C:\\iap\\daily --universe <symbols.txt>
    python tools/ingest/dd1_events.py run --dry-run --year 2026 --dev-root <tmp> --sealed-root <tmp>

A companion of ``tools/ingest/dd1_daily.py`` with the same rules: it runs as
``iap-runner`` only (exit 3 otherwise; mocked only in ``--dry-run``), its log is
redacted and says counts, never a CIK, a symbol or a timestamp, and it cuts by
the same U0 section 2 windows (:func:`dd1_daily.windows`).  The adapter is
:mod:`iap.data.sources.edgar_events`; its docstring defines the timing classes,
the point-in-time sessions and the amendment rule.

What one run does:

1. **identity**, and no earlier record for the purchase year;
2. **pull** (``https``, the DD1 allow-list): ``company_tickers.json`` to map the
   ``--universe`` symbols to CIKs (current map, not point in time: D-61 applies
   here too); the three Kenneth French daily files for the session calendar
   (sessions after the files' last date are filled with weekdays and counted as
   ``calendar_extended``); every quarter's ``form.idx`` from ``--first-year``
   (default 2004, item 2.02 starts in August 2004) to today; then one header
   per universe ``8-K``/``8-K/A`` (never a filing body);
3. **build** the announcements (item 2.02 or old item 12, amendments folded);
4. **tests**: header parse failures at most ``max_header_failure_share``; every
   announcement inside the calendar has ``usable_session > as_of_session``;
   ``before_open`` usable on the acceptance day, the others later;
5. **cut** by ``usable_session`` into development, validation, Track B, Track C;
   write the first two under ``--dev-root/dd1_events_<Y>/``, seal B and C
   through the sealed services' ``ingest`` op only when the tests pass;
6. **record** ``dataset.json`` (kind ``daily_pit_events``) with hashes and counts.

Exit codes as DD1: 0 complete, 2 a test failed or a service refused, 3 refused
before any request.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import io
import json
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
for _p in (str(TOOLS), str(REPO / "python" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from iap.data.sources import edgar, edgar_events  # noqa: E402
from iap.data.sources.base import RUNNER_IDENTITY, current_identity  # noqa: E402
from runner import redact  # noqa: E402

from ingest import dd1_daily as dd1  # noqa: E402

SCHEMA = "iap.dd1_events/v1"
FIRST_YEAR = 2004
THRESHOLDS = {"max_header_failure_share": 0.01}
IngestRefused = dd1.IngestRefused


# ---------------------------------------------------------------- the pull --
def map_universe(src: edgar_events.EdgarEventsSource, universe: Iterable[str]) -> dict[int, str]:
    want = {s.strip().upper().replace("-", ".") for s in universe if s.strip()}
    doc = json.loads(src._get_text(edgar.TICKERS_URL))
    entries = list(doc.values()) if isinstance(doc, dict) else list(doc)
    out: dict[int, str] = {}
    for e in entries:
        t = str(e.get("ticker") or "").upper().replace("-", ".")
        if t in want and e.get("cik_str"):
            out.setdefault(int(e["cik_str"]), t)
    return out


def sessions_from_factors(factors: list[dict[str, Any]], today: _dt.date) -> tuple[list[str], int]:
    """French calendar dates, then weekdays up to ``today + 10`` days beyond them."""
    days = sorted({r["date"] for r in factors})
    extended = 0
    d = _dt.date.fromisoformat(days[-1]) if days else _dt.date(FIRST_YEAR, 1, 1)
    while d < today + _dt.timedelta(days=10):
        d += _dt.timedelta(days=1)
        if d.weekday() < 5:
            days.append(d.isoformat())
            extended += 1
    return days, extended


def quarters(first_year: int, today: _dt.date) -> list[tuple[int, int]]:
    return [
        (y, q)
        for y in range(first_year, today.year + 1)
        for q in range(1, 5)
        if _dt.date(y, 3 * (q - 1) + 1, 1) <= today
    ]


def pull_events(
    transport,
    universe: Iterable[str],
    *,
    today: _dt.date,
    first_year: int = FIRST_YEAR,
    adapter_kw: Mapping[str, Any] | None = None,
    log=None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    src = edgar_events.EdgarEventsSource(transport, **(adapter_kw or {}))
    by_cik = map_universe(src, universe)
    factors, _ = dd1.pull_factors(transport, adapter_kw=adapter_kw)
    sessions, extended = sessions_from_factors(factors, today)
    rows: list[edgar_events.IndexRow] = []
    qs = quarters(first_year, today)
    for y, q in qs:
        rows.extend(src.index_rows(y, q, by_cik))
    headers: list[edgar_events.FilingHeader] = []
    failures = 0
    for r in rows:
        try:
            h = src.header(r)
        except ValueError:
            failures += 1
            continue
        if not h.cik:  # a header without a filer CIK keeps the index's
            h = edgar_events.FilingHeader(
                h.accession, h.form, r.cik, h.acceptance_et, h.items, h.date_filed or r.date_filed
            )
        headers.append(h)
    events, stats = edgar_events.announcements(headers, sessions)
    out = []
    for a in events:
        row = a.row()
        row["symbol"] = by_cik.get(a.header.cik, "")
        out.append(row)
    meta = {
        "source": "sec_edgar",
        "quarters_read": len(qs),
        "first_year": first_year,
        "universe_symbols": len(set(universe)),
        "symbols_with_cik": len(by_cik),
        "index_8k_rows": sum(r.form == "8-K" for r in rows),
        "index_8k_a_rows": sum(r.form == "8-K/A" for r in rows),
        "headers_read": len(headers),
        "header_failures": failures,
        "earnings_headers": sum(h.is_earnings for h in headers),
        "announcements": len(out),
        "dropped": {k: stats[k] for k in ("amendment_of_original", "duplicate_accession")},
        "timing": {k: stats[k] for k in ("before_open", "during", "after_close", "non_session")},
        "calendar_sessions": len(sessions) - extended,
        "calendar_extended": extended,
        "requests": src.requests_made,
        "cik_map_as_of": today.isoformat(),
        "contract": edgar_events.DECLARATION.to_dict(),
    }
    if log:
        log(
            f"edgar events: {len(qs)} quarter indexes, {len(rows)} 8-K rows, {len(headers)} headers "
            f"({failures} unreadable), {len(out)} announcements"
        )
    return out, meta


# ----------------------------------------------------------- tests and cut --
def events_tests(
    events: list[dict[str, Any]], meta: Mapping[str, Any], th: Mapping[str, Any] = THRESHOLDS
) -> dict[str, Any]:
    tried = meta["headers_read"] + meta["header_failures"]
    share = meta["header_failures"] / tried if tried else 0.0
    order_bad = sum(
        1
        for e in events
        if e["as_of_session"] and e["usable_session"] and e["usable_session"] <= e["as_of_session"]
    )
    timing_bad = sum(
        1
        for e in events
        if e["usable_session"]
        and ((e["timing"] == "before_open") != (e["usable_session"] == e["acceptance_et"][:10]))
    )
    results = {
        "header_parse": {
            "pass": share <= th["max_header_failure_share"],
            "failure_share": round(share, 6),
        },
        "session_order": {"pass": order_bad == 0, "violations": order_bad},
        "timing_rule": {"pass": timing_bad == 0, "violations": timing_bad},
    }
    return {"pass": all(r["pass"] for r in results.values()), "results": results}


def cut(events: list[dict[str, Any]], year: int) -> dict[str, list[dict[str, Any]]]:
    """By ``usable_session`` (the first session the event may be read); events the
    calendar does not reach are left out and counted in the record."""
    return {
        name: [e for e in events if e["usable_session"] and dd1._in(e["usable_session"], lo, hi)]
        for name, (lo, hi) in dd1.windows(year).items()
    }


FIELDS = ("symbol", *edgar_events.EVENT_FIELDS)


def serialise(
    name: str, events: list[dict[str, Any]], window, year: int
) -> list[tuple[str, bytes]]:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    for e in sorted(events, key=lambda e: (e["usable_session"], e["cik"], e["accession"])):
        w.writerow({f: e.get(f, "") for f in FIELDS})
    meta = {"schema": SCHEMA, "partition": name, "purchase_year": year, "window": list(window)}
    return [
        ("earnings_8k.csv", buf.getvalue().encode()),
        ("partition.json", (json.dumps(meta, indent=1, sort_keys=True) + "\n").encode()),
    ]


# ------------------------------------------------------------------ the run --
def ingest(
    *,
    year: int,
    dev_root: Path,
    universe: Iterable[str] = (),
    transport=dd1.urllib_transport,
    sealed_transport=dd1.pipe_sealed_transport,
    identity: str | None = None,
    today: _dt.date | None = None,
    first_year: int = FIRST_YEAR,
    thresholds: Mapping[str, Any] = THRESHOLDS,
    adapter_kw: Mapping[str, Any] | None = None,
    log: redact.RedactingLog | None = None,
) -> dict[str, Any]:
    log = log or redact.RedactingLog([str(dev_root)])
    today = today or _dt.date.today()
    who = identity if identity is not None else current_identity()
    if who != RUNNER_IDENTITY:
        log("refused: process identity is not the runner account")
        raise IngestRefused(3, "the DD1 events ingestion runs only as the runner account")
    out_dir = Path(dev_root) / f"dd1_events_{year}"
    if (out_dir / "dataset.json").is_file():
        raise IngestRefused(
            3, "a record for this purchase year exists; a re-ingestion is a new year"
        )
    universe = list(universe)
    if not universe:
        raise IngestRefused(3, "no universe symbols given")
    ss = dd1._sealed_service_module()

    events, meta = pull_events(
        transport, universe, today=today, first_year=first_year, adapter_kw=adapter_kw, log=log
    )
    tests = events_tests(events, meta, thresholds)
    for name, res in tests["results"].items():
        log(f"events test {name}: {'pass' if res['pass'] else 'FAIL'}")
    parts = cut(events, year)
    win = dd1.windows(year)
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": "daily_pit_events",
        "purchase_year": year,
        "route": "daily_pit",
        "sources": {"earnings_8k": "sec_edgar", "calendar": "kenneth_french_library"},
        "vendor": meta,
        "windows": {k: list(v) for k, v in win.items()},
        "outside_calendar": sum(1 for e in events if not e["usable_session"]),
        "partitions": {},
        "events_tests": tests,
        "status": "incomplete",
        "evidence_class": "mechanically isolated",
        "utc": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
    }
    for name in ("development", "validation"):
        files = serialise(name, parts[name], win[name], year)
        hashes = dd1.file_hashes(files)
        pdir = out_dir / name
        pdir.mkdir(parents=True, exist_ok=True)
        for n, b in files:
            (pdir / n).write_bytes(b)
        record["partitions"][name] = {
            "location": "development-readable",
            "dir": name,
            "dataset_hash_id": ss.content_hash(hashes),
            "files": hashes,
            "row_counts": {"earnings_8k.csv": len(parts[name])},
        }
        log(f"partition {name}: {len(files)} files written for the development identity")
    sealed_ok = True
    if tests["pass"]:
        for name, part in dd1.SEALED.items():
            files = serialise(name, parts[name], win[name], year)
            hashes = dd1.file_hashes(files)
            resp = sealed_transport(
                part,
                {
                    "op": "ingest",
                    "dataset_hash_id": ss.content_hash(hashes),
                    "files": hashes,
                    "row_counts": {"earnings_8k.csv": len(parts[name])},
                },
                files,
            )
            if not resp.get("ok"):
                sealed_ok = False
                log(f"service {part} refused the ingest: {resp.get('error')}")
                record["partitions"][name] = {
                    "location": f"sealed-{part}",
                    "refused": resp.get("error"),
                }
                continue
            record["partitions"][name] = {
                "location": f"sealed-{part}",
                "dataset_hash_id": resp["dataset_hash_id"],
                "files": resp["files"],
                "row_counts": resp["row_counts"],
                "chain_head": resp.get("chain_head"),
            }
            log(f"partition {name}: sealed in {part}, {len(resp['files'])} files")
    else:
        log("an events test failed: nothing sealed")
    record["status"] = "complete" if tests["pass"] and sealed_ok else "incomplete"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dataset.json").write_text(
        json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    log(f"record written: status {record['status']}")
    if record["status"] != "complete":
        raise IngestRefused(2, "ingestion incomplete (see the record)")
    return record


def expected_requests(
    n_ciks: int, first_year: int, today: _dt.date, eightk_per_cik_year: float = 12.0
) -> dict[str, Any]:
    """Planning figure for a real run: 1 ticker map + 3 French files + one index per
    quarter + one header per universe 8-K; seconds at the adapter's 8 requests/s."""
    qs = len(quarters(first_year, today))
    years = today.year - first_year + 1
    headers = int(n_ciks * years * eightk_per_cik_year)
    total = 4 + qs + headers
    return {
        "quarters": qs,
        "headers": headers,
        "requests": total,
        "hours_at_limit": round(total / edgar_events.DECLARATION.requests_per_second / 3600, 2),
    }


# ------------------------------------------------------------------ dry run --
def dry_run(year: int, dev_root: Path, sealed_root: Path, **knobs: Any) -> dict[str, Any]:
    """CI: the synthetic EDGAR fixture, mocked identity, in-process sealed services."""
    from ingest import dd1_events_fixture

    ss = dd1._sealed_service_module()
    fx = dd1_events_fixture.build(year, **knobs)
    services: dict[str, Any] = {}
    for part in dd1.SEALED.values():
        root = Path(sealed_root) / part
        state = root / "service"
        state.mkdir(parents=True, exist_ok=True)
        (state / "trust_roots.json").write_text(json.dumps({"maintainers": {"dry-run": "00" * 32}}))
        (state / "config.json").write_text(json.dumps({"allowed_accounts": ["iap-runner"]}))
        services[part] = ss.SealedService(ss.ServiceConfig.load(root, part))

    def sealed_transport(part, request, files):
        buf = io.BytesIO()
        ss.send_files(buf, files)
        buf.seek(0)
        incoming = list(ss._receive_files(buf))
        resp, _ = services[part].handle(request, ss.Caller(account="iap-runner", pid=1), incoming)
        return resp

    log = redact.RedactingLog([str(dev_root), str(sealed_root)])
    tick = [0.0]

    def clock() -> float:
        tick[0] += 10.0
        return tick[0]

    try:
        rec = ingest(
            year=year,
            dev_root=Path(dev_root),
            universe=fx.universe,
            transport=fx.transport,
            sealed_transport=sealed_transport,
            identity="iap-runner",
            today=fx.today,
            first_year=fx.first_year,
            adapter_kw={"clock": clock, "sleep": lambda _s: None},
            log=log,
        )
    except IngestRefused as exc:
        rec_path = Path(dev_root) / f"dd1_events_{year}" / "dataset.json"
        rec = (
            json.loads(rec_path.read_text(encoding="utf-8"))
            if rec_path.is_file()
            else {"status": "refused"}
        )
        rec["exit"] = exc.code
    rec["log"] = log.text()
    rec["requests"] = len(fx.requests)
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="pull, build, test, cut, seal, record")
    r.add_argument("--year", type=int, default=_dt.date.today().year, help="purchase year Y")
    r.add_argument("--dev-root", type=Path, required=True)
    r.add_argument("--universe", type=Path, default=None, help="one symbol per line")
    r.add_argument("--first-year", type=int, default=FIRST_YEAR)
    r.add_argument(
        "--dry-run", action="store_true", help="synthetic fixtures, no network, no identity"
    )
    r.add_argument("--sealed-root", type=Path, default=None)
    r.add_argument("--log", type=Path, default=None)
    e = sub.add_parser("estimate", help="expected request count and hours for a real run")
    e.add_argument("--ciks", type=int, default=650)
    e.add_argument("--first-year", type=int, default=FIRST_YEAR)
    a = ap.parse_args(argv)
    if a.cmd == "estimate":
        print(json.dumps(expected_requests(a.ciks, a.first_year, _dt.date.today())))
        return 0
    if a.dry_run:
        rec = dry_run(
            a.year, a.dev_root, a.sealed_root or Path(tempfile.mkdtemp(prefix="dd1ev-sealed-"))
        )
        text = rec.pop("log")
        if a.log:
            a.log.write_text(text, encoding="utf-8")
        sys.stdout.write(text)
        print(json.dumps({"status": rec["status"], "requests": rec["requests"]}))
        return 0 if rec["status"] == "complete" else 2
    log = redact.RedactingLog([str(a.dev_root)])
    code = 0
    try:
        universe = a.universe.read_text(encoding="utf-8").split() if a.universe else []
        rec = ingest(
            year=a.year, dev_root=a.dev_root, universe=universe, first_year=a.first_year, log=log
        )
        log(f"done: {rec['status']}")
    except IngestRefused as exc:
        code = exc.code
        log(f"exit {code}")
    if a.log:
        a.log.parent.mkdir(parents=True, exist_ok=True)
        a.log.write_text(log.text(), encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
