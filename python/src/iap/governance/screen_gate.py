"""The net-of-cost screen check a pre-registration may require (v1.17 C6).

Moved out of :mod:`iap.research.screen` in v2.1 Track 3 so that the write broker
(:mod:`iap.governance.broker`) can refuse an unscreened pre-registration without importing the
research package.  :mod:`iap.research.screen` re-exports every name here, so existing callers and
``except ScreenRefusal`` clauses see the same objects.  Pure document checks: no data, no models.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[4]
SCHEMA = "iap.net_of_cost_screen/v1"
STATUS_PASS, STATUS_FAIL, STATUS_NOT_SCREENED = "pass", "fail", "not_screened"


class ScreenRefusal(ValueError):
    """A confirmatory step was asked for a candidate the screen did not pass."""


class ScreenError(ValueError):
    """A screen could not be run as pre-registered."""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def model_identity(alpha_id: str) -> str:
    """The model identity of an alpha id as the screen keys it:
    ``linear_z_v2`` for a ``_z2`` twin, else ``linear_z_v1``."""
    from iap.alpha.linear_v2 import V2_SUFFIX

    return "linear_z_v2" if alpha_id.endswith(V2_SUFFIX) else "linear_z_v1"


def base_alpha_id(alpha_id: str) -> str:
    """``EQ05`` for ``EQ05_z2`` (the screen keys twins by their base id)."""
    from iap.alpha.linear_v2 import V2_SUFFIX

    return alpha_id[: -len(V2_SUFFIX)] if alpha_id.endswith(V2_SUFFIX) else alpha_id


def load_screen(path: str | Path) -> dict[str, Any]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA:
        raise ScreenError(f"{path}: not a screen document (schema {doc.get('schema')!r})")
    return doc


def screen_status(
    doc: Mapping[str, Any], alpha_id: str, horizon: str, model: str = "linear_z_v1"
) -> str:
    """``pass`` / ``fail`` / ``not_screened`` of one identity in a screen document."""
    want = f"{alpha_id}/{horizon}/{model}"
    for row in doc.get("candidates", []):
        if row.get("id") == want:
            return str(row.get("status", STATUS_NOT_SCREENED))
    return STATUS_NOT_SCREENED


def require_screened(
    screen: str | Path | Mapping[str, Any],
    alpha_id: str,
    horizon: str,
    model: str = "linear_z_v1",
) -> dict[str, Any]:
    """Refuse (``ScreenRefusal``) unless ``alpha_id/horizon/model`` PASSED
    the screen ``screen`` (a document or its path).  Returns a small
    provenance block (path, sha256, candidate, status) for the caller's
    report.  Opt-in everywhere: a caller that does not pass a screen is
    unchanged."""
    path: Path | None = None
    if isinstance(screen, (str, Path)):
        path = Path(screen)
        doc = load_screen(path)
    else:
        doc = dict(screen)
    status = screen_status(doc, alpha_id, horizon, model)
    ident = f"{alpha_id}/{horizon}/{model}"
    if status != STATUS_PASS:
        what = "FAILED" if status == STATUS_FAIL else "was NOT screened by"
        raise ScreenRefusal(
            f"{ident} {what} the net-of-cost screen"
            f"{' ' + str(path) if path else ''}: no confirmatory run (v1.17 C6)"
        )
    return {
        "path": _relative(path) if path else None,
        "sha256": file_sha256(path) if path else None,
        "candidate": ident,
        "status": status,
        "prereg_sha256": doc.get("spec", {}).get("prereg", {}).get("sha256"),
    }


def _relative(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return Path(path).resolve().relative_to(_REPO).as_posix()
    except ValueError:
        return Path(path).as_posix()
