"""What the research checks record in, or read from, a real dataset's manifest (v1.9 R1, R4).

Moved, unchanged, from :mod:`iap.validation.sessions` in v2.1 Track 3: it writes the marketdata
manifest, and :mod:`iap.validation` sits above :mod:`iap.marketdata`, so having it in validation
made the two packages import each other.  :func:`book_scope_for_dataset` (R4) moved with it for
the same reason.  The record itself comes from
:func:`iap.validation.sessions.stratified_day_sample`.  The manifest's other fields and its
``dataset_version`` are untouched: the version hashes the normalized files, not the manifest.
"""

from __future__ import annotations

import json
from pathlib import Path

from iap.marketdata.ingest import MANIFEST_NAME, load_manifest

__all__ = ["book_scope_for_dataset", "record_day_sampling"]


def record_day_sampling(dataset_dir: str | Path, record: dict) -> dict:
    """Store ``record`` as ``"sampling"`` in a real dataset's manifest and
    return the updated manifest.  Every selected day must be a session of
    the dataset (fail closed: a record naming a day that was never ingested
    describes another dataset)."""
    manifest = load_manifest(dataset_dir)
    missing = sorted(set(record.get("selected", ())) - set(manifest.get("sessions", {})))
    if missing:
        raise ValueError(f"sampling record names days the dataset does not hold: {missing}")
    manifest["sampling"] = record
    path = Path(dataset_dir) / MANIFEST_NAME
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def book_scope_for_dataset(dataset_dir: str | Path | None) -> str:
    """``"single_venue"`` for an ingested real dataset (one exchange's own
    book: ITCH and LOBSTER are Nasdaq-only, there is no consolidated NBBO
    and a crossed book cannot be a stale other-venue quote), else
    ``"consolidated"`` (the synthetic multi-venue books)."""
    if dataset_dir is None or not (Path(dataset_dir) / "dataset.json").is_file():
        return "consolidated"
    manifest = load_manifest(dataset_dir)
    return "single_venue" if manifest.get("source", {}).get("venue") else "consolidated"
