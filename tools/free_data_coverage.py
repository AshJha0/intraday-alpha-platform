#!/usr/bin/env python3
"""Free data route: run every source adapter's ``coverage()`` (v2.0 stage 2 DD1; U0 DR-6).

    python tools/free_data_coverage.py [--source tiingo,stooq,wiki_membership,edgar,french]
                                       [--ciks 50] [--no-wiki-sample] [--json OUT]

Metadata only: counts and date ranges, as each adapter's ``coverage()`` defines
them (``python/src/iap/data/sources/``).  Nothing is written except the JSON
summary the caller asks for, which holds counts and ranges and no value.  This
is the only module of the free route that imports a network client
(``urllib``); the adapters take the transport it builds.  Keys: ``TIINGO_API_KEY``
is read by the Tiingo adapter if present (no request is authenticated unless
``--tiingo-probe TICKER`` is given) and never printed.  Nothing here can call
``fetch()``: that is the ``iap-runner`` ingestion after D2.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.data.sources import ADAPTERS  # noqa: E402

ALLOWED_HOSTS = frozenset(
    {
        "api.tiingo.com",
        "apimedia.tiingo.com",
        "stooq.com",
        "en.wikipedia.org",
        "www.sec.gov",
        "data.sec.gov",
        "mba.tuck.dartmouth.edu",
    }
)


def urllib_transport(url: str, headers: Mapping[str, str], *, timeout: float = 60.0) -> bytes:
    """One GET; https and the allow-list only; the body as bytes."""
    scheme, sep, rest = url.partition("://")
    host = rest.split("/", 1)[0].split("@")[-1].split(":")[0].lower() if sep else ""
    if scheme != "https" or host not in ALLOWED_HOSTS:
        raise ValueError(f"URL not on the allow-list (https only): {url}")
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - allow-listed https
        return r.read()


def run(
    sources: list[str],
    *,
    transport=urllib_transport,
    ciks: int = 50,
    wiki_sample: bool = True,
    tiingo_probe: str | None = None,
    today: _dt.date | None = None,
) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in sources:
        cls = ADAPTERS[name]
        adapter = cls(transport, today=today)
        try:
            if name == "edgar":
                cov = adapter.coverage(sample_size=ciks)
            elif name == "wiki_membership":
                cov = adapter.coverage(sample=wiki_sample)
            elif name == "tiingo":
                cov = adapter.coverage(probe_ticker=tiingo_probe)
            else:
                cov = adapter.coverage()
            out[name] = cov.to_dict()
        except (urllib.error.URLError, ValueError, PermissionError, RuntimeError) as exc:
            out[name] = {"source": name, "error": type(exc).__name__, "detail": str(exc)[:300]}
    return out


def render(results: dict[str, dict]) -> str:
    lines = []
    for name, doc in results.items():
        lines.append(f"== {name} ==")
        if "error" in doc:
            lines.append(f"  error: {doc['error']}: {doc['detail']}")
            continue
        for k, v in doc["counts"].items():
            lines.append(f"  {k}: {v}")
        for k, v in doc["ranges"].items():
            lines.append(f"  {k}: {v[0]} .. {v[1]}")
        for n in doc["notes"]:
            lines.append(f"  note: {n}")
        lines.append(f"  requests_made: {doc['requests_made']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=",".join(ADAPTERS), help="comma-separated adapter names")
    ap.add_argument("--ciks", type=int, default=50, help="EDGAR CIK sample size")
    ap.add_argument("--no-wiki-sample", action="store_true", help="revision counts only, no parsing")
    ap.add_argument("--tiingo-probe", default=None, help="one metadata request for this ticker (needs the key)")
    ap.add_argument("--json", default=None, help="write the counts/ranges document here")
    a = ap.parse_args(argv)
    sources = [s.strip() for s in a.source.split(",") if s.strip()]
    unknown = [s for s in sources if s not in ADAPTERS]
    if unknown:
        ap.error(f"unknown source(s) {unknown}; known: {sorted(ADAPTERS)}")
    results = run(sources, ciks=a.ciks, wiki_sample=not a.no_wiki_sample, tiingo_probe=a.tiingo_probe)
    print(render(results))
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
