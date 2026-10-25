"""``iap.ml``: the U6 ML framework (v2.0 stage 8), under the frozen protocol ``research/v2/ML_PROTOCOL.md``.

| module | what |
|---|---|
| :mod:`.protocol`  | the frozen protocol: hash, machine-readable block, tiers, grids, delta derivation |
| :mod:`.splits`    | chronological development / validation splits with purging and embargo = longest label horizon |
| :mod:`.trials`    | the trial ledger (chain-hashed, protocol hash first, search budgets enforced) |
| :mod:`.models`    | M1 ridge (closed form) and elastic net (cyclic coordinate descent), deterministic, with complexity |
| :mod:`.admission` | paired Delta U, block length from dependence, stationary block bootstrap, Holm within tier, lexicographic choice |
| :mod:`.pipeline`  | the M1 demonstration on the synthetic fixture through the research loop's engine; registry integration |

Nothing here opens a sealed partition: every engine run goes through
:mod:`iap.research.loop`, which refuses anything but the development or
validation partition.  Nothing here imports a network client or reads the wall
clock; every stochastic step is SplitMix64-seeded from the protocol.
"""

from iap.ml.admission import (
    AdmissionResult,
    admission_test,
    block_bootstrap_mean,
    block_length_from_dependence,
    choose_candidate,
    holm_within_tier,
)
from iap.ml.models import ElasticNetModel, RidgeModel
from iap.ml.protocol import (
    PROTOCOL_PATH,
    ProtocolError,
    derive_delta,
    load_protocol,
    protocol_hash,
    require_frozen,
)
from iap.ml.splits import ChronologicalSplit, SplitError
from iap.ml.trials import BudgetExceeded, TrialLedger, TrialLedgerError

__all__ = [
    "PROTOCOL_PATH",
    "AdmissionResult",
    "BudgetExceeded",
    "ChronologicalSplit",
    "ElasticNetModel",
    "ProtocolError",
    "RidgeModel",
    "SplitError",
    "TrialLedger",
    "TrialLedgerError",
    "admission_test",
    "block_bootstrap_mean",
    "block_length_from_dependence",
    "choose_candidate",
    "derive_delta",
    "holm_within_tier",
    "load_protocol",
    "protocol_hash",
    "require_frozen",
]
