"""``iap.store`` — the platform data model (``schemas/sql/iap_v1.sql``) over
``sqlite3``, plus importers for every flat-file research artefact.

The store is a derived, rebuildable index: the JSON / JSONL / Parquet
artefacts stay the source of truth (``docs/DATA_MODEL.md``).

    from iap.store import Store
    store = Store.open("data/store/iap.sqlite"); store.init()
    store.insert_trace(trace); print(store.explain(parent_order_id))
"""

from iap.store.db import LIFECYCLE_STATES, STAGE_TABLES, Store
from iap.store.ddl import DDL_X_VERSION, ddl_path, load_ddl, split_statements
from iap.store.importers import (
    ImportReport,
    import_all,
    import_alpha_registry,
    import_alpha_reports,
    import_baselines,
    import_experiment_documents,
    import_experiments_ledger,
    import_lifecycle_log,
    import_lifecycle_transitions,
    import_model_runs,
    import_reference,
    import_tca_orders,
)

__all__ = [
    "DDL_X_VERSION",
    "ImportReport",
    "LIFECYCLE_STATES",
    "STAGE_TABLES",
    "Store",
    "ddl_path",
    "import_all",
    "import_alpha_registry",
    "import_alpha_reports",
    "import_baselines",
    "import_experiment_documents",
    "import_experiments_ledger",
    "import_lifecycle_log",
    "import_lifecycle_transitions",
    "import_model_runs",
    "import_reference",
    "import_tca_orders",
    "load_ddl",
    "split_statements",
]
