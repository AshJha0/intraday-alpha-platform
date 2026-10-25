"""The frozen U6 ML protocol (``research/v2/ML_PROTOCOL.md``): hash, block, tiers, grids, delta.

The protocol text is hashed LF-normalised (so that a checkout's line endings do
not matter) and the hash is **frozen here**: :func:`require_frozen` refuses a
protocol whose hash differs from :data:`FROZEN_SHA256`, and every trial ledger
opens with the hash it ran under.  A change to the text is a new revision with a
new hash and a new freeze decision, never an edit of the constant alone.

:func:`load_protocol` returns the machine-readable block of section 12, the
single source of the constants the framework uses.  :func:`derive_delta` is the
documented calculation of section 7 with the block's placeholders.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

__all__ = [
    "CLASS_ORDER",
    "FROZEN_SHA256",
    "PROTOCOL_PATH",
    "ProtocolError",
    "derive_delta",
    "load_protocol",
    "protocol_hash",
    "protocol_text",
    "require_frozen",
    "tier_of",
]

PROTOCOL_PATH = Path(__file__).resolve().parents[4] / "research" / "v2" / "ML_PROTOCOL.md"
#: sha256 of the LF-normalised protocol text, revision 1 (frozen 2026-10-25).
FROZEN_SHA256 = "b37cc5887ce0d8fde9e2f207701d2fa72384add2675f0f40f048130de29ddbe3"
CLASS_ORDER = ("M0", "M1", "M2", "M3", "M4")
_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)\n```", re.S)


class ProtocolError(ValueError):
    """The protocol is missing, altered, or inconsistent with the framework."""


def protocol_text(path: Path | str | None = None) -> str:
    p = Path(path) if path is not None else PROTOCOL_PATH
    if not p.exists():
        raise ProtocolError(f"protocol file {p} is missing")
    return p.read_bytes().decode("utf-8").replace("\r\n", "\n")


def protocol_hash(path: Path | str | None = None) -> str:
    """sha256 of the LF-normalised text."""
    return hashlib.sha256(protocol_text(path).encode("utf-8")).hexdigest()


def require_frozen(path: Path | str | None = None, *, expected: str = FROZEN_SHA256) -> str:
    """Return the hash, or refuse: a fit under an unfrozen or altered protocol never starts."""
    h = protocol_hash(path)
    if h != expected:
        raise ProtocolError(
            f"protocol hash {h} differs from the frozen {expected}: a changed protocol is a new "
            "revision needing a new freeze before any fit"
        )
    return h


def load_protocol(path: Path | str | None = None) -> dict[str, Any]:
    """The machine-readable block of section 12, validated for the fields the framework reads."""
    text = protocol_text(path)
    blocks = _JSON_BLOCK.findall(text)
    if len(blocks) != 1:
        raise ProtocolError(f"the protocol holds {len(blocks)} json blocks; exactly one expected")
    try:
        doc = json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"protocol block is not JSON: {exc}") from None
    for key in (
        "revision",
        "placeholders_filled",
        "feature_groups",
        "class_order",
        "tiers",
        "grids",
        "admission",
        "delta",
        "advancement",
        "sealed_test",
        "seed",
    ):
        if key not in doc:
            raise ProtocolError(f"protocol block lacks {key!r}")
    if tuple(doc["class_order"]) != CLASS_ORDER:
        raise ProtocolError("class order must be M0 < M1 < M2 < M3 < M4")
    for tier, t in doc["tiers"].items():
        if tier not in CLASS_ORDER or int(t["budget"]) < 1:
            raise ProtocolError(f"tier {tier!r} malformed")
        for cls in t["classes"]:
            if cls not in doc["grids"]:
                raise ProtocolError(f"class {cls!r} has no grid")
            if not cls.startswith(tier + "."):
                raise ProtocolError(f"class {cls!r} is not in tier {tier}")
    for tier in ("M1", "M2", "M3"):
        fits = sum(len(doc["grids"][c]) for c in doc["tiers"][tier]["classes"])
        if fits != int(doc["tiers"][tier]["budget"]):
            raise ProtocolError(
                f"tier {tier}: grid has {fits} points but the budget is {doc['tiers'][tier]['budget']}"
            )
    return doc


def tier_of(model_class: str) -> str:
    tier = model_class.split(".", 1)[0]
    if tier not in CLASS_ORDER:
        raise ProtocolError(f"model class {model_class!r} is in no tier")
    return tier


def derive_delta(doc: dict[str, Any]) -> dict[str, Any]:
    """Section 7: ``delta = max(c_turn * tau_inc, 0.25 * mde(G_val, alpha/K, 0.8) * sigma_dU / sqrt(G_val))``.

    ``mde`` is the exact noncentral-t minimum detectable standardised effect of
    :func:`iap.validation.confirmatory.mde_at_t`'s convention (one-sample t on G
    sessions at one-sided alpha / K), so the second term is in bps of C per
    session once multiplied by ``sigma_dU / sqrt(G)``.
    """
    from scipy.stats import t as student_t

    from iap.validation.confirmatory import mde_at_t

    ph = doc["delta"]["placeholders"]
    c_turn = float(ph["c_turn_bps_per_unit_turnover"])
    tau = float(ph["tau_inc_fraction_of_C"])
    sigma = float(ph["sigma_dU_bps"])
    g = int(ph["G_val"])
    k = int(ph["K_tier"])
    alpha = float(doc["admission"]["holm_alpha"]) / k
    tcrit = float(student_t.ppf(1.0 - alpha, g - 1))
    # mde_at_t returns the noncentrality-per-sqrt(n) standardised effect (mean / sd) detectable with power 0.8
    d_std = float(mde_at_t(g, tcrit, 0.8))
    delta_cost = c_turn * tau
    delta_power = 0.25 * d_std * sigma  # d_std is mean/sd, so mean = d_std * sigma_dU (bps)
    delta = max(delta_cost, delta_power)
    return {
        "delta_cost_bps": delta_cost,
        "delta_power_bps": delta_power,
        "standardised_mde": d_std,
        "tcrit": tcrit,
        "alpha_per_test": alpha,
        "delta_bps": delta,
        "label": doc["delta"].get("label", ""),
        "placeholders_filled": bool(doc["placeholders_filled"]),
    }


def check_block_against_prose(doc: dict[str, Any], text: str | None = None) -> None:
    """The numbers the prose states must equal the block's (sections 4, 5, 7, 8, 11)."""
    text = text if text is not None else protocol_text()
    checks = {
        "budget M1": (str(doc["tiers"]["M1"]["budget"]), "| M1 |"),
        "budget M3": (str(doc["tiers"]["M3"]["budget"]), "| M3 |"),
        "resamples": (f"{doc['admission']['bootstrap']['resamples']:,}", "resamples"),
        "seed": (str(doc["seed"]), "seed"),
        "block max": (str(doc["admission"]["block_length"]["max"]), "{1, ..., "),
    }
    for what, (value, where) in checks.items():
        if not any(value in line for line in text.splitlines() if where in line):
            raise ProtocolError(
                f"prose and block disagree on {what}: {value!r} not found near {where!r}"
            )
    d = derive_delta(doc)
    if not math.isclose(round(d["delta_bps"], 2), float(doc["delta"]["value_bps"]), abs_tol=1e-9):
        raise ProtocolError(
            f"delta in the block ({doc['delta']['value_bps']}) is not the documented calculation ({d['delta_bps']:.4f})"
        )
