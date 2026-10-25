"""Kenneth French data library adapter (U0 DR-6: daily factors for D2 and the residual step).

The library (``https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html``)
publishes zipped CSVs of daily factor returns, updated monthly.  The files this
route needs:

| file | columns |
|---|---|
| ``F-F_Research_Data_Factors_daily_CSV.zip``      | Mkt-RF, SMB, HML, RF |
| ``F-F_Research_Data_5_Factors_2x3_daily_CSV.zip`` | Mkt-RF, SMB, HML, RMW, CMA, RF |
| ``F-F_Momentum_Factor_daily_CSV.zip``             | Mom |

``coverage()`` downloads each archive **into memory**, reads the first and last
date and the row count, and discards it: nothing is written, no value is
returned.  (The files are small; there is no metadata endpoint.)  The factor
returns themselves are ingested only by ``fetch``.

Availability: the library posts a month's daily rows some weeks after month
end.  The contract declares ``publication_date`` with a 25-session lag (about
five weeks) as the conservative default; D-39 in the defect register records
it, and DD1 can tighten it per file from the library's observed posting dates.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

from iap.data.contracts import AvailabilityRule, DatasetContract, dataset
from iap.data.sources.base import Coverage, SourceAdapter, SourceDeclaration

__all__ = ["DECLARATION", "FILES", "FrenchSource", "summarise_factor_csv"]

BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
FILES = {
    "ff3_daily": "F-F_Research_Data_Factors_daily_CSV.zip",
    "ff5_daily": "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip",
    "mom_daily": "F-F_Momentum_Factor_daily_CSV.zip",
}

DECLARATION = SourceDeclaration(
    contract=dataset(
        DatasetContract(
            dataset_id="french.factors_daily",
            source="kenneth_french_library",
            availability=AvailabilityRule("publication_date", 25),
            adjustment="none",
            missing_policy="ineligible",
            route="daily_pit",
            leakage_test_id="LK-CAUSALITY",
            fields=("date", "mkt_rf", "smb", "hml", "rmw", "cma", "mom", "rf"),
        )
    ),
    licence_id="kenneth_french",
    access="public download, no account; monthly updates",
    user_agent="intraday-alpha-platform/2.0 (free data route coverage; in-memory range check)",
    requests_per_second=0.5,
)

_DATE_ROW = re.compile(r"^\s*(\d{8})\s*,")


def summarise_factor_csv(blob: bytes) -> dict[str, Any]:
    """``{"rows", "first", "last", "columns"}`` of a French daily CSV (zip or bare)."""
    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
            text = zf.read(name).decode("utf-8", errors="replace")
    else:
        text = blob.decode("utf-8", errors="replace")
    rows = 0
    first = last = None
    columns: list[str] = []
    for line in text.splitlines():
        m = _DATE_ROW.match(line)
        if m:
            d = m.group(1)
            rows += 1
            first = first or d
            last = d
        elif not columns and line.strip().startswith(","):
            columns = [c.strip() for c in line.split(",")[1:] if c.strip()]
    if not rows:
        raise ValueError("no YYYYMMDD rows found: not a French daily file")
    iso = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"  # noqa: E731
    return {"rows": rows, "first": iso(first), "last": iso(last), "columns": columns}


class FrenchSource(SourceAdapter):
    declaration = DECLARATION

    def coverage(self, *, files: dict[str, str] | None = None) -> Coverage:
        cov = self._coverage()
        for key, name in (files or FILES).items():
            info = summarise_factor_csv(self._get(BASE + name))
            cov.counts[f"{key}_rows"] = info["rows"]
            cov.counts[f"{key}_columns"] = len(info["columns"])
            cov.ranges[f"{key}_dates"] = (info["first"], info["last"])
        cov.notes.append(
            "archives read in memory for the row count and date range only, then discarded; "
            "nothing written"
        )
        cov.requests_made = self.requests_made
        return cov
