"""Model prices and dated pins as configuration (v1.17 E1, review finding E-7).

Prices live in ``configs/reference/llm_prices.json`` (USD per million
tokens, with the ``source`` and the ``checked`` date they were read on) and
the spend policy and per-role pins in ``configs/agents/llm.json``.  Nothing
in code carries a price: :func:`usage_cost` and :func:`max_call_cost` look
the model up in the loaded table and **refuse an unknown model**
(:class:`UnknownModelError`) rather than guess.

A pin is a dated snapshot: ``{"model": "claude-sonnet-5-5", "pinned":
"2026-10-16", "prices_checked": "2026-10-06"}`` - the explicit model id
(never a floating alias), the date the pin was decided and the ``checked``
date of the price table it is costed under - so a session can record exactly
which price table it ran under.

Both files are read from the repository's ``configs/`` tree (``IAP_CONFIG_DIR``
overrides the directory, as in the k8s pods).  Loading is cached per path;
tests pass their own paths.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "AGENT_CONFIG",
    "PRICES_FILE",
    "LlmConfig",
    "ModelPin",
    "ModelPrice",
    "PriceTable",
    "UnknownModelError",
    "config_dir",
    "input_tokens",
    "load_llm_config",
    "load_prices",
    "max_call_cost",
    "usage_cost",
]

PRICES_FILE = "reference/llm_prices.json"
AGENT_CONFIG = "agents/llm.json"
_REPO = Path(__file__).resolve().parents[4]


class UnknownModelError(ValueError):
    """The model has no entry in the price table: the call is refused."""


def config_dir() -> Path:
    env = os.environ.get("IAP_CONFIG_DIR")
    return Path(env) if env else _REPO / "configs"


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True)
class PriceTable:
    models: dict[str, ModelPrice]
    checked: str
    source: str
    path: Path

    def __contains__(self, model: object) -> bool:
        return model in self.models

    def price(self, model: str) -> ModelPrice:
        try:
            return self.models[model]
        except KeyError:
            raise UnknownModelError(
                f"no price for model {model!r}: add it to {self.path} (checked {self.checked}); "
                "a price is never guessed"
            ) from None


def _num(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise ValueError(f"{what}: expected a non-negative number, got {v!r}")
    return float(v)


@lru_cache(maxsize=8)
def _load_prices_cached(path: Path, mtime_ns: int) -> PriceTable:
    doc = json.loads(path.read_text(encoding="utf-8"))
    mult = _num(doc.get("cache_write_multiplier", 1.25), "cache_write_multiplier")
    models: dict[str, ModelPrice] = {}
    for name, row in dict(doc["models"]).items():
        if not isinstance(name, str) or not name or "latest" in name:
            raise ValueError(f"{path}: model ids must be explicit, not {name!r}")
        inp = _num(row["input"], f"{name}.input")
        models[name] = ModelPrice(
            input=inp,
            output=_num(row["output"], f"{name}.output"),
            cache_read=_num(row.get("cache_read", inp * 0.1), f"{name}.cache_read"),
            cache_write=_num(row.get("cache_write", inp * mult), f"{name}.cache_write"),
        )
    if not models:
        raise ValueError(f"{path}: no models")
    checked = str(doc.get("checked") or "")
    if len(checked) != 10:
        raise ValueError(f"{path}: `checked` must be a YYYY-MM-DD date")
    return PriceTable(models=models, checked=checked, source=str(doc.get("source", "")), path=path)


def load_prices(path: str | os.PathLike[str] | None = None) -> PriceTable:
    """The price table (cached by path and mtime; re-read when the file changes)."""
    p = Path(path) if path is not None else config_dir() / PRICES_FILE
    p = p.resolve()
    return _load_prices_cached(p, p.stat().st_mtime_ns)


def _u(usage: Any, name: str) -> int:
    v = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return int(v or 0)


def input_tokens(usage: Any) -> int:
    """Every input token the call was billed for (fresh, cache write, cache read)."""
    return sum(
        _u(usage, n)
        for n in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )


def usage_cost(model: str, usage: Any, table: PriceTable | None = None) -> float:
    """USD for one call's ``usage`` (the bookkeeping figure, not an invoice)."""
    pr = (table or load_prices()).price(model)
    return (
        _u(usage, "input_tokens") * pr.input
        + _u(usage, "cache_creation_input_tokens") * pr.cache_write
        + _u(usage, "cache_read_input_tokens") * pr.cache_read
        + _u(usage, "output_tokens") * pr.output
    ) / 1e6


def max_call_cost(
    model: str,
    prompt_tokens: int,
    max_tokens: int,
    *,
    table: PriceTable | None = None,
    cache_write: bool = False,
) -> float:
    """The most the next call can cost: ``prompt_tokens`` at the input price
    (the cache-write price when ``cache_write``) plus a full ``max_tokens``
    reply at the output price."""
    pr = (table or load_prices()).price(model)
    in_price = pr.cache_write if cache_write else pr.input
    return (max(prompt_tokens, 0) * in_price + max(max_tokens, 0) * pr.output) / 1e6


@dataclass(frozen=True)
class ModelPin:
    role: str
    model: str
    pinned: str
    prices_checked: str

    def as_dict(self) -> dict[str, str]:
        return {"model": self.model, "pinned": self.pinned, "prices_checked": self.prices_checked}


@dataclass(frozen=True)
class LlmConfig:
    monthly_cap_usd: float
    per_run_budget_usd: float
    alert_fraction: float
    pins: dict[str, ModelPin]
    path: Path

    def pin(self, role: str) -> ModelPin:
        try:
            return self.pins[role]
        except KeyError:
            raise KeyError(f"no model pin for role {role!r} in {self.path}") from None


@lru_cache(maxsize=8)
def _load_config_cached(path: Path, mtime_ns: int, prices_path: Path | None) -> LlmConfig:
    doc = json.loads(path.read_text(encoding="utf-8"))
    table = load_prices(prices_path)
    pins: dict[str, ModelPin] = {}
    for role, row in dict(doc["pins"]).items():
        model = str(row["model"])
        table.price(model)  # an unpinnable (unpriced) model is refused at load
        pinned = str(row["pinned"])
        if len(pinned) != 10:
            raise ValueError(f"{path}: pin {role!r}: `pinned` must be a YYYY-MM-DD date")
        pins[role] = ModelPin(role, model, pinned, table.checked)
    frac = _num(doc.get("alert_fraction", 0.8), "alert_fraction")
    if not 0 < frac <= 1:
        raise ValueError(f"{path}: alert_fraction must be in (0, 1]")
    return LlmConfig(
        monthly_cap_usd=_num(doc["monthly_cap_usd"], "monthly_cap_usd"),
        per_run_budget_usd=_num(doc["per_run_budget_usd"], "per_run_budget_usd"),
        alert_fraction=frac,
        pins=pins,
        path=path,
    )


def load_llm_config(
    path: str | os.PathLike[str] | None = None,
    prices_path: str | os.PathLike[str] | None = None,
) -> LlmConfig:
    """``configs/agents/llm.json``: the caps and the dated per-role pins."""
    p = (Path(path) if path is not None else config_dir() / AGENT_CONFIG).resolve()
    pp = Path(prices_path).resolve() if prices_path is not None else None
    return _load_config_cached(p, p.stat().st_mtime_ns, pp)
