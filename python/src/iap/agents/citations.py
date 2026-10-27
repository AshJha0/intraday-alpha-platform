"""Alias of :mod:`iap.governance.citations` (moved in v2.1 Track 3 to break the agents/llm/research import cycles).

The old import path keeps working: this module replaces itself in ``sys.modules`` with the
governance module, so ``iap.agents.citations`` and ``iap.governance.citations`` are the same object and a
monkeypatch on either name reaches the code that runs.
"""

import sys

from iap.governance import citations as _impl

sys.modules[__name__] = _impl
