"""SEC EDGAR filing index adapter (U0 DR-6: D4 becomes a filing-date drift family).

Two public endpoints, both free, both governed by the SEC's access rules
(``https://www.sec.gov/os/accessing-edgar-data``: a declarative User-Agent of
the form ``company-or-name contact``, at most 10 requests per second, no
scraping beyond that; see ``docs/DATA_SOURCES.md``):

- the **full index** ``https://www.sec.gov/Archives/edgar/full-index/<YYYY>/QTR<q>/form.idx``:
  every filing of a quarter (form type, company, CIK, date filed, path), the
  bulk ingestion path (``fetch``);
- the **submissions API** ``https://data.sec.gov/submissions/CIK<10 digits>.json``:
  one issuer's filings with ``form``, ``filingDate``, ``acceptanceDateTime`` (the
  timestamp D4 needs: before or after the session), ``accessionNumber``.

``coverage()`` takes a sample of CIKs, calls the submissions API once per CIK
and returns counts of 8-K and 10-Q (and 10-K) filings and their first and last
filing dates, plus whether acceptance timestamps are present.  The filing
bodies are never requested.  CIKs come from the SEC's ``company_tickers.json``
(ticker to CIK map, also public) or a list the caller passes.

No consensus data exists on this route, so D4's surprise is not defined; the
family is specified as drift after the filing timestamp (coverage report,
section 4) or stays ineligible.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Iterable
from typing import Any

from iap.data.contracts import AvailabilityRule, DatasetContract, dataset
from iap.data.sources.base import Coverage, SourceAdapter, SourceDeclaration, Transport

__all__ = ["DECLARATION", "EdgarSource", "parse_form_index", "summarise_submissions"]

FULL_INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/form.idx"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FORMS = ("8-K", "10-Q", "10-K")
CONTACT_ENV = "IAP_SEC_CONTACT"
_DEFAULT_CONTACT = "https://github.com/AshJha0/intraday-alpha-platform"

DECLARATION = SourceDeclaration(
    contract=dataset(
        DatasetContract(
            dataset_id="edgar.filings",
            source="sec_edgar",
            # a filing accepted on D (any hour) is usable from the next session; an
            # intra-session rule by acceptance hour is a D4 specification decision
            availability=AvailabilityRule("publication_date", 1),
            adjustment="none",
            missing_policy="ineligible",
            route="daily_pit",
            leakage_test_id="LK-CAUSALITY",
            fields=("cik", "form", "filing_date", "acceptance_datetime", "accession"),
        )
    ),
    licence_id="sec_edgar",
    access="public; User-Agent 'name contact'; <= 10 requests/second (SEC access rules)",
    user_agent=f"intraday-alpha-platform research {os.environ.get(CONTACT_ENV, _DEFAULT_CONTACT)}",
    requests_per_second=8.0,
)


def parse_form_index(text: str) -> Counter[str]:
    """Form-type counts of one ``form.idx`` (header skipped at the dashed line)."""
    counts: Counter[str] = Counter()
    started = False
    for line in text.splitlines():
        if not started:
            if line.startswith("---"):
                started = True
            continue
        if not line.strip():
            continue
        counts[line[:12].strip()] += 1
    return counts


def summarise_submissions(doc: dict[str, Any]) -> dict[str, Any]:
    """Per-CIK counts and date ranges from one submissions document (recent block)."""
    recent = doc.get("filings", {}).get("recent", {})
    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    accepted = recent.get("acceptanceDateTime", []) or []
    out: dict[str, Any] = {"cik": int(doc.get("cik", 0) or 0), "recent_total": len(forms)}
    for want in FORMS:
        idx = [i for i, f in enumerate(forms) if f == want]
        out[f"{want}_count"] = len(idx)
        if idx:
            ds = sorted(dates[i] for i in idx if i < len(dates))
            out[f"{want}_first"] = ds[0]
            out[f"{want}_last"] = ds[-1]
            out[f"{want}_with_acceptance"] = sum(
                1 for i in idx if i < len(accepted) and accepted[i]
            )
    out["older_files"] = len(doc.get("filings", {}).get("files", []) or [])
    return out


class EdgarSource(SourceAdapter):
    declaration = DECLARATION

    def __init__(self, transport: Transport | None = None, **kw: Any) -> None:
        super().__init__(transport, **kw)

    def sample_ciks(self, n: int) -> list[int]:
        """The first ``n`` CIKs of the SEC's ticker map (ordered by the SEC, roughly by size)."""
        doc = json.loads(self._get_text(TICKERS_URL))
        rows = list(doc.values()) if isinstance(doc, dict) else list(doc)
        ciks: list[int] = []
        for row in rows:
            cik = int(row.get("cik_str", 0) or 0)
            if cik and cik not in ciks:
                ciks.append(cik)
            if len(ciks) >= n:
                break
        return ciks

    def submissions(self, cik: int) -> dict[str, Any]:
        return json.loads(self._get_text(SUBMISSIONS_URL.format(cik=cik), {"Host": "data.sec.gov"}))

    def coverage(self, *, ciks: Iterable[int] | None = None, sample_size: int = 50) -> Coverage:
        cov = self._coverage()
        cik_list = list(ciks) if ciks is not None else self.sample_ciks(sample_size)
        cov.counts["ciks_sampled"] = len(cik_list)
        totals: Counter[str] = Counter()
        firsts: dict[str, str] = {}
        lasts: dict[str, str] = {}
        with_acc: Counter[str] = Counter()
        truncated = 0
        for cik in cik_list:
            s = summarise_submissions(self.submissions(cik))
            truncated += int(s["older_files"] > 0)
            for form in FORMS:
                totals[form] += s[f"{form}_count"]
                with_acc[form] += s.get(f"{form}_with_acceptance", 0)
                if f"{form}_first" in s:
                    firsts[form] = min(firsts.get(form, s[f"{form}_first"]), s[f"{form}_first"])
                    lasts[form] = max(lasts.get(form, s[f"{form}_last"]), s[f"{form}_last"])
        for form in FORMS:
            key = form.replace("-", "").lower()
            cov.counts[f"{key}_filings"] = totals[form]
            cov.counts[f"{key}_with_acceptance_ts"] = with_acc[form]
            if form in firsts:
                cov.ranges[f"{key}_filing_dates"] = (firsts[form], lasts[form])
        cov.counts["ciks_with_older_filing_files"] = truncated
        cov.notes.append(
            "counts are from each CIK's 'recent' block (up to ~1,000 filings); CIKs flagged with "
            "older files have more history in paged files, read at ingestion; filing bodies never requested"
        )
        cov.requests_made = self.requests_made
        return cov
