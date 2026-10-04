"""``iap.store`` — the platform data model (``schemas/sql/iap_v2.sql``) over
``sqlite3``, plus importers for every flat-file research artefact.

The store is a derived, rebuildable index: the JSON / JSONL / Parquet
artefacts stay the source of truth (``docs/DATA_MODEL.md``).

    from iap.store import Store
    store = Store.open("data/store/iap.sqlite"); store.init()
    store.insert_trace(trace); print(store.explain(parent_order_id))
"""

from iap.store.db import (
    LEGACY_METHODS,
    LIFECYCLE_STATES,
    STAGE_TABLES,
    Store,
    StoreVersionError,
    methods_of,
)
from iap.store.ddl import DDL_X_VERSION, ddl_path, load_ddl, split_statements
from iap.store.importers import (
    ImportReport,
    import_all,
    import_alpha_registry,
    import_alpha_reports,
    import_baselines,
    import_experiment_documents,
    import_experiments_ledger,
    import_lifecycle_archive,
    import_lifecycle_log,
    import_lifecycle_transitions,
    import_model_runs,
    import_reference,
    import_tca_orders,
    ledger_entry_scope,
    resolve_current_scope,
)

__all__ = [
    "DDL_X_VERSION",
    "ImportReport",
    "LEGACY_METHODS",
    "LIFECYCLE_STATES",
    "STAGE_TABLES",
    "Store",
    "StoreVersionError",
    "ddl_path",
    "import_all",
    "import_alpha_registry",
    "import_alpha_reports",
    "import_baselines",
    "import_experiment_documents",
    "import_experiments_ledger",
    "import_lifecycle_archive",
    "import_lifecycle_log",
    "import_lifecycle_transitions",
    "import_model_runs",
    "import_reference",
    "import_tca_orders",
    "ledger_entry_scope",
    "load_ddl",
    "methods_of",
    "resolve_current_scope",
    "split_statements",
]
