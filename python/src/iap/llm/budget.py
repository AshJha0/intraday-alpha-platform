"""Session budget caps, the USD estimate from ``usage`` and, since v1.17 E1,
the link to the shared spend ledger.

Prices are **configuration**: ``configs/reference/llm_prices.json`` read by
:mod:`iap.llm.prices` (an unknown model is refused, never priced by guess).
The estimate is the bookkeeping figure the caps are enforced on, not an
invoice.  Before every model call the next call is projected at the last
call's input plus a full ``max_tokens_per_call`` output, so no cap is
overshot by one call.

Three caps, in the order checked by :meth:`Budget.check_model_call`:

* the **monthly cap** on the shared ledger (``monthly_cap_usd``, default 30):
  month-to-date read from the ledger file on *every* call plus the next
  call's maximum cost, so two sessions in two processes cannot together pass
  it; refused with :class:`BudgetExceeded` *before* the call is made;
* the **per-run budget** (``max_usd``, default ``per_run_budget_usd`` = 1):
  this session's own spend;
* the token and tool-call caps.

A :class:`Budget` without a ledger (``ledger=None``, the test default with
the scripted client) enforces only its own caps; :func:`iap.llm.agent.run_session`
refuses a real ``anthropic`` client on such a budget, so every live path
records spend.  Dated pins (``role`` -> model, pinned date, price-table
date) come from ``configs/agents/llm.json``: research = Sonnet 5.5, ops /
docs / eval = Haiku 5.5.

Opt-in growing-context projection (``context_growth=True``): in an agent loop
the next request carries the whole previous request, the previous reply and
the tool results appended since, so the next input is projected as
``last_input + last_output + pending_tool_tokens`` (the caller reports tool
result sizes via :meth:`Budget.add_pending_tool_tokens`); the first call is
seeded with ``prompt_estimate_tokens``; and the projected input is priced at
the cache-write rate, the most expensive way it can be billed.  The default
keeps the pinned v1.14 projection.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from iap.llm import prices as _prices
from iap.llm.prices import ModelPin, UnknownModelError, input_tokens, usage_cost

__all__ = [
    "CACHE_WRITE_MULT",
    "DEFAULT_MODEL",
    "DEFAULT_PER_RUN_USD",
    "EVAL_MODEL",
    "PINNED_MODELS",
    "PRICES",
    "Budget",
    "BudgetExceeded",
    "input_tokens",
    "pin_for",
    "usage_cost",
]


class BudgetExceeded(RuntimeError):
    pass


class _PriceView(Mapping[str, tuple[float, float, float]]):
    """Read-only ``model -> (input, output, cache read)`` view of the price
    file, kept for callers that iterate the priced models (``--model``
    choices).  Re-reads when the file changes; never holds a price itself."""

    def __getitem__(self, model: str) -> tuple[float, float, float]:
        p = _prices.load_prices().price(model)
        return (p.input, p.output, p.cache_read)

    def __iter__(self):
        return iter(_prices.load_prices().models)

    def __len__(self) -> int:
        return len(_prices.load_prices().models)

    def __contains__(self, model: object) -> bool:
        return model in _prices.load_prices().models


#: pinned model id -> (input, output, cache read) USD per MTok, from the file
PRICES: Mapping[str, tuple[float, float, float]] = _PriceView()


def _cfg() -> _prices.LlmConfig:
    return _prices.load_llm_config()


def pin_for(role: str) -> ModelPin:
    """The dated pin of ``role`` (``research``, ``ops``, ``docs``, ``eval``)."""
    return _cfg().pin(role)


#: Role -> explicit model id (G-P2-12: never a floating alias).  The dated
#: snapshot (pinned date, price-table date) is :func:`pin_for`.
PINNED_MODELS: dict[str, str] = {r: p.model for r, p in _cfg().pins.items()}
DEFAULT_MODEL = PINNED_MODELS["research"]
EVAL_MODEL = PINNED_MODELS["eval"]
DEFAULT_PER_RUN_USD = _cfg().per_run_budget_usd
CACHE_WRITE_MULT = 1.25  # documented ratio; the costed value is the file's cache_write


@dataclass
class Budget:
    max_tool_calls: int = 40
    max_tokens: int = 400_000
    max_usd: float = DEFAULT_PER_RUN_USD
    max_preregs: int = 3
    max_tokens_per_call: int = 4096
    tool_calls: int = 0
    tokens: int = 0
    usd: float = 0.0
    preregs: int = 0
    model_calls: int = 0
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    context_growth: bool = False
    prompt_estimate_tokens: int = 0
    pending_tool_tokens: int = 0
    refusals: list[str] = field(default_factory=list)
    #: v1.17 E1: the shared ledger (None = this session's caps only)
    ledger: Any = field(default=None, repr=False, compare=False)
    session_id: str = ""
    run_id: str = ""
    role: str = "research"
    pin: ModelPin | None = field(default=None, repr=False, compare=False)

    _LEDGER_FIELDS = ("ledger", "pin")

    def charge(self, model: str, usage: Any) -> None:
        self.model_calls += 1
        self.last_input_tokens = input_tokens(usage)
        self.last_output_tokens = _prices._u(usage, "output_tokens")
        self.pending_tool_tokens = 0
        self.tokens += self.last_input_tokens + self.last_output_tokens
        self.usd += usage_cost(model, usage)
        if self.ledger is not None:
            self.ledger.record(
                model=model,
                usage=usage,
                session=self.session_id,
                run=self.run_id,
                role=self.role,
                pin=self.pin if self.pin and self.pin.model == model else None,
            )

    def check_model_call(self, model: str) -> None:
        """Refuse the next call before it is made: monthly cap on the shared
        ledger first, then this session's USD and token caps."""
        try:
            pr = _prices.load_prices().price(model)
        except UnknownModelError as exc:
            raise BudgetExceeded(str(exc)) from exc
        next_in = self.projected_input_tokens()
        growth = self.context_growth
        if self.ledger is not None:
            try:
                self.ledger.check_call(model, next_in, self.max_tokens_per_call, cache_write=growth)
            except RuntimeError as exc:  # MonthlyCapExceeded
                raise BudgetExceeded(str(exc)) from exc
        in_price = pr.cache_write if growth else pr.input
        projected = (next_in * in_price + self.max_tokens_per_call * pr.output) / 1e6
        if self.usd + projected > self.max_usd:
            raise BudgetExceeded(
                f"usd cap: spent ~${self.usd:.4f}; the next call could reach "
                f"${self.usd + projected:.4f} > ${self.max_usd:.2f}"
            )
        if self.tokens + next_in + self.max_tokens_per_call > self.max_tokens:
            raise BudgetExceeded(f"token cap: {self.tokens} of {self.max_tokens} used")

    def add_pending_tool_tokens(self, n: int) -> None:
        """Record the (estimated) size of a tool result to be sent next call."""
        if n < 0:
            raise ValueError("n must be >= 0")
        self.pending_tool_tokens += int(n)

    def projected_input_tokens(self) -> int:
        if not self.context_growth:
            return self.last_input_tokens
        if self.model_calls == 0:
            return max(self.prompt_estimate_tokens, 0) + self.pending_tool_tokens
        return self.last_input_tokens + self.last_output_tokens + self.pending_tool_tokens

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
        d = {k: v for k, v in self.__dict__.items() if k not in self._LEDGER_FIELDS}
        d["refusals"] = list(self.refusals)
        d["usd"] = round(self.usd, 6)
        d["ledger"] = str(self.ledger.path) if self.ledger is not None else None
        d["pin"] = self.pin.as_dict() if self.pin is not None else None
        return d
