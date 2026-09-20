"""The executable MVP — one complete institutional trading loop (``python -m iap.mvp``).

Market data (seeded generator) -> per-venue books + consolidated book ->
feature engine -> three fitted ``linear_z_v1`` alphas (EQ01 / EQ03 / EQ06)
ensembled -> single-stock mean-variance portfolio target -> hard risk on
every child -> TWAP / POV / IS scheduling -> SOR over three venues ->
execution simulator (queue position, partial fills) -> TCA -> attribution
-> one :class:`~iap.contracts.types.DecisionTrace` per decision (JSONL +
SQLite) -> report.  Deterministic and replayable bit-for-bit
(``docs/MVP.md``).

Modules:

- :mod:`iap.mvp.config`   — ``configs/mvp/mvp.json`` (fail-fast validated)
- :mod:`iap.mvp.feed`     — seeded generation -> normalisation -> the captured
  event stream (``events.jsonl`` + IAP1) and its ``MarketDataSource``
- :mod:`iap.mvp.alpha`    — streaming ``Alpha`` adapters + the ensemble
- :mod:`iap.mvp.portfolio`— the ``PortfolioConstructor``
- :mod:`iap.mvp.adapters` — protocol adapters over risk / algos / SOR /
  simulator / TCA
- :mod:`iap.mvp.engine`   — ``MvpEngine``, the event loop
- :mod:`iap.mvp.report`   — ``report.json`` / ``report.md``
- :mod:`iap.mvp.session`  — ``run_session``: feed -> engine -> sinks -> store -> report
- :mod:`iap.mvp.__main__` — ``run`` / ``replay`` / ``verify`` / ``explain``
"""

from iap.mvp.config import MvpConfig, load_config  # noqa: F401
from iap.mvp.engine import MvpEngine  # noqa: F401
from iap.mvp.session import RunResult, run_session  # noqa: F401

__all__ = ["MvpConfig", "MvpEngine", "RunResult", "load_config", "run_session"]
