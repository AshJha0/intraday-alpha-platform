"""Alias of :mod:`iap.governance.claims` (moved in v2.1 Track 3 to break the research import cycles).

The old import path keeps working: this module replaces itself in ``sys.modules`` with the
governance module, so ``iap.research.claims`` and ``iap.governance.claims`` are the same object and a
monkeypatch on either name reaches the code that runs.
"""

import sys

from iap.governance import claims as _impl

sys.modules[__name__] = _impl
