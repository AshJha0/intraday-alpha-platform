"""Model cards for registry models (v1.15).

A model card is ``model_card.json`` in the model's registry directory.  It
is REQUIRED at promotion (:meth:`iap.mlops.registry.ModelRegistry.promote`):
a missing or invalid card raises :class:`RegistryError`.

Fields (schema :data:`CARD_SCHEMA`):

* ``model_id`` / ``record_hash`` / ``name`` -- bind the card to one record;
* ``data`` -- ``dataset_version``, ``date_range`` and the ``sessions`` used;
* ``prereg`` -- ``{"id", "hash"}`` copied from the record (both ``None`` and
  ``exploratory: true`` for an exploratory model);
* ``metrics_net_of_costs`` -- a non-empty dict of numbers, net of costs;
* ``calibration`` -- calibration / coverage figures, or ``None`` when not
  available (never omitted);
* ``known_limitations`` -- non-empty list of strings;
* ``intended_use`` -- non-empty string;
* ``owner_signoff`` -- non-empty reference (e.g. a PR or blackboard entry);
* ``card_hash`` -- SHA-256 of the canonical card without ``card_hash``.

:func:`generate_card` fills the record-derived fields; the human fields
(limitations, intended use, sign-off) must be supplied.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

CARD = "model_card.json"
CARD_SCHEMA = 1
REQUIRED = (
    "schema",
    "model_id",
    "record_hash",
    "name",
    "data",
    "prereg",
    "exploratory",
    "metrics_net_of_costs",
    "calibration",
    "known_limitations",
    "intended_use",
    "owner_signoff",
    "card_hash",
)


def _canonical(obj: Any) -> str:
    from iap.mlops.registry import canonical

    return canonical(obj)


def card_hash(card: dict) -> str:
    from iap.mlops.registry import sha256

    body = {k: v for k, v in card.items() if k != "card_hash"}
    return sha256(_canonical(body))


def generate_card(
    rec,
    *,
    metrics_net_of_costs: dict,
    known_limitations: list[str],
    intended_use: str,
    owner_signoff: str,
    sessions: list[str] | None = None,
    calibration: dict | None = None,
) -> dict:
    """A card for :class:`ModelRecord` ``rec`` (validated before return)."""
    card = {
        "schema": CARD_SCHEMA,
        "model_id": rec.model_id,
        "record_hash": rec.record_hash,
        "name": rec.name,
        "data": {
            "dataset_version": rec.dataset_version,
            "date_range": list(rec.date_range),
            "sessions": [str(s) for s in (sessions or [])],
        },
        "prereg": {"id": rec.prereg_id, "hash": rec.prereg_hash},
        "exploratory": bool(rec.exploratory),
        "metrics_net_of_costs": json.loads(_canonical(metrics_net_of_costs)),
        "calibration": None if calibration is None else json.loads(_canonical(calibration)),
        "known_limitations": [str(x) for x in known_limitations],
        "intended_use": str(intended_use),
        "owner_signoff": str(owner_signoff),
    }
    card["card_hash"] = card_hash(card)
    problems = validate_card(card, rec)
    if problems:
        raise ValueError("invalid model card: " + "; ".join(problems))
    return card


def validate_card(card: Any, rec) -> list[str]:
    """Problems with ``card`` against record ``rec``; ``[]`` = valid."""
    if not isinstance(card, dict):
        return ["card is not a JSON object"]
    p = [f"missing field {k!r}" for k in REQUIRED if k not in card]
    if p:
        return p
    if card["schema"] != CARD_SCHEMA:
        p.append(f"schema {card['schema']!r} != {CARD_SCHEMA}")
    if card["model_id"] != rec.model_id:
        p.append("model_id does not match the record")
    if card["record_hash"] != rec.record_hash:
        p.append("record_hash does not match the record")
    if card["name"] != rec.name:
        p.append("name does not match the record")
    data = card["data"]
    if not isinstance(data, dict) or data.get("dataset_version") != rec.dataset_version:
        p.append("data.dataset_version does not match the record")
    elif list(data.get("date_range") or []) != list(rec.date_range):
        p.append("data.date_range does not match the record")
    elif not isinstance(data.get("sessions"), list):
        p.append("data.sessions must be a list")
    pre = card["prereg"]
    if (
        not isinstance(pre, dict)
        or pre.get("hash") != rec.prereg_hash
        or pre.get("id") != (rec.prereg_id)
    ):
        p.append("prereg id/hash does not match the record")
    if bool(card["exploratory"]) != bool(rec.exploratory):
        p.append("exploratory flag does not match the record")
    m = card["metrics_net_of_costs"]
    if not isinstance(m, dict) or not m:
        p.append("metrics_net_of_costs must be a non-empty object")
    elif not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        for v in m.values()
    ):
        p.append("metrics_net_of_costs values must be finite numbers")
    if card["calibration"] is not None and not isinstance(card["calibration"], dict):
        p.append("calibration must be an object or null")
    lim = card["known_limitations"]
    if (
        not isinstance(lim, list)
        or not lim
        or not all(isinstance(x, str) and x.strip() for x in lim)
    ):
        p.append("known_limitations must be a non-empty list of strings")
    for k in ("intended_use", "owner_signoff"):
        if not isinstance(card[k], str) or not card[k].strip():
            p.append(f"{k} must be a non-empty string")
    if card["card_hash"] != card_hash(card):
        p.append("card_hash mismatch")
    return p


def write_card(root: str | Path, card: dict) -> Path:
    path = Path(root) / card["model_id"] / CARD
    path.write_text(json.dumps(card, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_card(root: str | Path, model_id: str) -> dict | None:
    path = Path(root) / model_id / CARD
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"__unparseable__": True}
