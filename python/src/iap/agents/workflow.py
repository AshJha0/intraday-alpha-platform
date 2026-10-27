"""Alias of :mod:`iap.governance.workflow` (moved in v2.1 Track 3 to break the agents/llm/research import cycles).

The old import path keeps working: this module replaces itself in ``sys.modules`` with the
governance module, so ``iap.agents.workflow`` and ``iap.governance.workflow`` are the same object and a
monkeypatch on either name reaches the code that runs.
"""

import sys

from iap.governance import workflow as _impl

if __name__ == "__main__":
    raise SystemExit(_impl.main())
else:
    sys.modules[__name__] = _impl
