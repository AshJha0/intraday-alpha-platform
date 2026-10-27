"""Alias of :mod:`iap.governance.approvals` (moved in v2.1 Track 3 to break the agents/lifecycle import cycle).

The old import path keeps working: this module replaces itself in ``sys.modules`` with the
governance module, so ``iap.agents.approvals`` and ``iap.governance.approvals`` are the same object and a
monkeypatch on either name reaches the code that runs.
"""

import sys

from iap.governance import approvals as _impl

sys.modules[__name__] = _impl
