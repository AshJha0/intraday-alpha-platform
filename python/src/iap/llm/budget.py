"""Session budget caps and the USD estimate from ``usage``.

Prices are USD per million tokens (Anthropic first-party list prices as of
2026-10); cache writes are billed at 1.25x input.  The estimate is the
bookkeeping figure the cap is enforced on, not an invoice.  Before every
model call the next call is projected at the last call's input plus a full
``max_tokens_per_call`` output, so the cap is not overshot by one call.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

#: model -> (input, output, cache read) USD per MTok
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
DEFAULT_MODEL = "claude-opus-5-5"
EVAL_MODEL = "claude-haiku-5-5"
CACHE_WRITE_MULT = 1.25


class BudgetExceeded(RuntimeError):
    pass


def _u(usage: Any, name: str) -> int:
    v = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return int(v or 0)


def usage_cost(model: str, usage: Any) -> float:
    if model not in PRICES:
        raise ValueError(f"no price for model {model!r}; add it to iap.llm.budget.PRICES")
    pin, pout, pread = PRICES[model]
    return (
        _u(usage, "input_tokens") * pin
        + _u(usage, "cache_creation_input_tokens") * pin * CACHE_WRITE_MULT
        + _u(usage, "cache_read_input_tokens") * pread
        + _u(usage, "output_tokens") * pout
    ) / 1e6


def input_tokens(usage: Any) -> int:
    return sum(
        _u(usage, n)
        for n in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )


@dataclass
class Budget:
    max_tool_calls: int = 40
    max_tokens: int = 400_000
    max_usd: float = 2.0
    max_preregs: int = 3
    max_tokens_per_call: int = 4096
    tool_calls: int = 0
    tokens: int = 0
    usd: float = 0.0
    preregs: int = 0
    model_calls: int = 0
    last_input_tokens: int = 0
    refusals: list[str] = field(default_factory=list)

    def charge(self, model: str, usage: Any) -> None:
        self.model_calls += 1
        self.last_input_tokens = input_tokens(usage)
        self.tokens += self.last_input_tokens + _u(usage, "output_tokens")
        self.usd += usage_cost(model, usage)

    def check_model_call(self, model: str) -> None:
        if model not in PRICES:
            raise ValueError(f"no price for model {model!r}; add it to iap.llm.budget.PRICES")
        pin, pout, _ = PRICES[model]
        projected = (self.last_input_tokens * pin + self.max_tokens_per_call * pout) / 1e6
        if self.usd + projected > self.max_usd:
            raise BudgetExceeded(
                f"usd cap: spent ~${self.usd:.4f}; the next call could reach "
                f"${self.usd + projected:.4f} > ${self.max_usd:.2f}"
            )
        if self.tokens + self.last_input_tokens + self.max_tokens_per_call > self.max_tokens:
            raise BudgetExceeded(f"token cap: {self.tokens} of {self.max_tokens} used")

    def check_tool_call(self) -> None:
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded(f"tool-call cap: {self.max_tool_calls} reached")
        self.tool_calls += 1

    def check_prereg(self) -> None:
        if self.preregs >= self.max_preregs:
            raise BudgetExceeded(
                f"look budget: {self.max_preregs} pre-registrations per session reached"
            )

    def summary(self) -> dict[str, Any]:
        d = asdict(self)
        d["usd"] = round(self.usd, 6)
        return d
