"""``iap.data.sources``: adapters for the free data route (v2.0 stage 2 DD1; U0 DR-6).

| module | source | what it covers |
|---|---|---|
| :mod:`.tiingo`          | Tiingo EOD (free tier, key in ``TIINGO_API_KEY``) | daily OHLCV; delisted tickers listed |
| :mod:`.stooq`           | Stooq bulk daily files (no account)               | daily OHLCV, US listings |
| :mod:`.wiki_membership` | Wikipedia revision history (MediaWiki API)         | dated S&P 500 and Nasdaq-100 membership intervals (**proxy**) |
| :mod:`.edgar`           | SEC EDGAR full index and filing timestamps          | 8-K and 10-Q filing dates by CIK (D4 as filing-date drift) |
| :mod:`.french`          | Kenneth French data library                          | daily factor files |

Each adapter declares its :class:`~iap.data.sources.base.SourceDeclaration`
(dataset contract, licence id, access rule), returns counts and ranges from
``coverage()`` and refuses ``fetch()`` outside the ``iap-runner`` identity and a
sealed partition.  No network module is imported anywhere in this package; the
transport is injected (``tools/free_data_coverage.py``).
"""

from iap.data.sources.base import (
    Coverage,
    RateLimiter,
    SealedServiceUnavailable,
    SealingRefused,
    SourceAdapter,
    SourceDeclaration,
    require_runner,
)
from iap.data.sources.edgar import EdgarSource
from iap.data.sources.french import FrenchSource
from iap.data.sources.stooq import StooqSource
from iap.data.sources.tiingo import TiingoSource
from iap.data.sources.wiki_membership import WikiMembershipSource

ADAPTERS = {
    "tiingo": TiingoSource,
    "stooq": StooqSource,
    "wiki_membership": WikiMembershipSource,
    "edgar": EdgarSource,
    "french": FrenchSource,
}

__all__ = [
    "ADAPTERS",
    "Coverage",
    "EdgarSource",
    "FrenchSource",
    "RateLimiter",
    "SealedServiceUnavailable",
    "SealingRefused",
    "SourceAdapter",
    "SourceDeclaration",
    "StooqSource",
    "TiingoSource",
    "WikiMembershipSource",
    "require_runner",
]
