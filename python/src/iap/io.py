"""Guarded file readers (v2.0 stage 2, D1): the only place in ``iap`` that
calls ``pandas.read_parquet``, ``pyarrow.parquet.ParquetFile`` /
``read_table`` or ``numpy.load``.

Every wrapper calls :func:`iap.core.guard.check` first, so a read of a file
inside a registered (spent / unread / unregistered real) dataset is refused
before the library opens it, whatever the caller.  The static test
``python/tests/test_guard_hook.py::test_no_raw_reader_outside_iap_io`` keeps
every other module on these wrappers; ``docs/GOVERNANCE.md``
lists the routes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from iap.core import guard

__all__ = ["np_load", "parquet_file", "read_parquet", "read_table"]


def read_parquet(path: Path | str, *args: Any, purpose: str = "read_parquet", **kwargs: Any):
    """``pandas.read_parquet`` after the choke point."""
    guard.check(path, purpose)
    import pandas as pd

    return pd.read_parquet(path, *args, **kwargs)


def parquet_file(path: Path | str, *args: Any, purpose: str = "parquet_file", **kwargs: Any):
    """``pyarrow.parquet.ParquetFile`` after the choke point (row-group readers)."""
    guard.check(path, purpose)
    import pyarrow.parquet as pq

    return pq.ParquetFile(path, *args, **kwargs)


def read_table(path: Path | str, *args: Any, purpose: str = "read_table", **kwargs: Any):
    """``pyarrow.parquet.read_table`` after the choke point."""
    guard.check(path, purpose)
    import pyarrow.parquet as pq

    return pq.read_table(path, *args, **kwargs)


def np_load(path: Path | str, *args: Any, purpose: str = "np_load", **kwargs: Any):
    """``numpy.load`` after the choke point (``.npy`` / ``.npz``, memory maps included)."""
    guard.check(path, purpose)
    import numpy as np

    return np.load(path, *args, **kwargs)
