"""Build hook: ship the repository's ``schemas/`` inside the wheel.

``schemas/`` (JSON Schemas, the SQL DDL, the migration log) is the source of
truth and lives at the repository root, outside the Python package.  A
non-editable install (``pip install ./python``, the Docker images) therefore
had no schemas to validate against unless ``$IAP_SCHEMA_DIR`` pointed at a
checkout (review 2026-09-20, finding 4).  This ``build_py`` hook copies
``<repo>/schemas`` into the build tree as ``iap/_schemas`` so the wheel
carries a byte-identical copy; :func:`iap.contracts.versions.schema_dir`
resolves ``$IAP_SCHEMA_DIR`` -> the checkout -> that packaged copy, in this
order.  ``tests/integration/test_installed_package.py`` asserts the packaged
copy equals the repository copy file for file.

Everything else (metadata, dependencies, entry points) stays in
``pyproject.toml``; this file only exists because setuptools has no
declarative way to include data that lives outside the package directory.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

HERE = Path(__file__).resolve().parent
SCHEMAS = HERE.parent / "schemas"
PACKAGED = ("iap", "_schemas")
SUFFIXES = (".json", ".sql", ".md")


def schema_files(root: Path):
    """Every schema file under ``root`` (sorted, relative), never caches."""
    return sorted(
        p.relative_to(root)
        for p in root.rglob("*")
        if p.is_file() and p.suffix in SUFFIXES and "__pycache__" not in p.parts
    )


class build_py(_build_py):  # noqa: N801 (setuptools naming)
    def run(self) -> None:
        super().run()
        if not SCHEMAS.is_dir():
            # Building from an sdist / a tree without the repository: the
            # installed package then needs $IAP_SCHEMA_DIR (schema_dir()).
            return
        target = Path(self.build_lib, *PACKAGED)
        if target.exists():
            shutil.rmtree(target)
        for rel in schema_files(SCHEMAS):
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SCHEMAS / rel, dst)
        # bdist_wheel packages the whole build tree (and writes RECORD from
        # it), so nothing else needs registering.  The marker names the
        # provenance for anyone reading site-packages.
        (target / "PACKAGED_FROM").write_text(
            "schemas/ of the repository checkout at build time; "
            "the repository copy is the source of truth.\n", encoding="ascii")


setup(cmdclass={"build_py": build_py})
