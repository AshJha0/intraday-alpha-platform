"""The research flow wired to the real memory, holdout registry and number verifier.

:func:`build_wired_flow` returns the graph of :mod:`iap.graph.research_flow`
with three of its five injected callables backed by existing code:

* ``search_prior`` - :meth:`iap.memory.Memory.prior_tests` over the
  experiment ledger, plus the rule :func:`same_test` below;
* ``is_holdout`` - :mod:`iap.governance.holdout_registry`; a dataset the
  registry does not hold as ``in_sample`` is refused, including one it does
  not know at all;
* ``verify`` - the exact-decimal number rules of :mod:`iap.llm.verify`,
  applied to the run's facts.

The in-sample runner and the narrative writer stay injected.  The defaults
are stubs: :func:`stub_runner` returns fixed numbers and
:func:`template_writer` writes the facts into a fixed sentence.  Nothing here
calls a model, opens a network connection or reads market data.

When a prior hit counts as "the same test" (:data:`SAME_TEST_RULE`)
-------------------------------------------------------------------
A ledger entry of ``research/experiments.json`` counts only when all three hold:

1. its ``alpha_id`` equals the hypothesis ``alpha_id`` (case-insensitive);
2. its configured horizon (``config.horizon``, else
   ``config.candidate.horizon``) equals the hypothesis horizon exactly
   (``1s`` does not match ``1s:quoting-insample``);
3. its universe matches: ``config.universe`` equals ``universe.name``
   (case-insensitive), or ``config.symbols`` (else
   ``config.candidate.symbols``) is the same set as ``universe.symbols``.

Text similarity never makes a hit "the same test"; BM25 only ranks the
candidates.  Limits: an entry that records no universe never counts, so a
re-run of an old test whose entry lacks ``symbols`` is shown as prior art but
does not stop the flow; a renamed alpha id or a horizon written another way
(``60s`` against ``1m``) is not caught; and only the ledger is checked, not
result documents that were never entered in it.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from iap.governance import holdout_registry
from iap.governance.untrusted import sanitise
from iap.llm import verify as numverify
from iap.memory.corpus import LEDGER
from iap.memory.search import REPO_ROOT, Memory

from .core import Graph
from .research_flow import VERDICTS, ResearchDeps, build_research_graph

SAME_TEST_RULE = (
    "same alpha_id (case-insensitive), the exact same horizon, and the same universe "
    "(config.universe == universe.name, or config.symbols == universe.symbols as sets) "
    "in one ledger entry; text similarity alone never counts"
)
#: candidate ledger entries examined per hypothesis, and document hits kept for context
MAX_LEDGER_CANDIDATES = 50
MAX_DOCUMENT_HITS = 3
#: the numeric facts a narrative may quote
NUMERIC_FACTS = ("t_stat", "n_sessions", "expected_sign", "min_abs_t", "min_sessions")
STUB_RUN = {"t_stat": 2.4, "mean_bps": 0.8, "n_sessions": 20}


# ------------------------------------------------------------- prior tests --
def _cfg(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    cfg = entry.get("config")
    return cfg if isinstance(cfg, Mapping) else {}


def _from_cfg(entry: Mapping[str, Any], key: str) -> Any:
    cfg = _cfg(entry)
    if cfg.get(key) is not None:
        return cfg[key]
    cand = cfg.get("candidate")
    return cand.get(key) if isinstance(cand, Mapping) else None


def _symbols(value: Any) -> frozenset[str] | None:
    if isinstance(value, (list, tuple)) and value and all(isinstance(s, str) for s in value):
        return frozenset(s.upper() for s in value)
    return None


def same_test(entry: Mapping[str, Any], hypothesis: Mapping[str, Any]) -> tuple[bool, str]:
    """Whether ledger ``entry`` already ran ``hypothesis`` (:data:`SAME_TEST_RULE`),
    with the reason in words."""
    if str(entry.get("alpha_id", "")).lower() != str(hypothesis.get("alpha_id", "")).lower():
        return False, "different alpha_id"
    horizon = _from_cfg(entry, "horizon")
    if horizon is None or str(horizon).strip().lower() != str(hypothesis.get("horizon")).lower():
        return False, f"horizon {horizon!r} differs"
    uni = hypothesis.get("universe") if isinstance(hypothesis.get("universe"), Mapping) else {}
    name = _from_cfg(entry, "universe")
    if isinstance(name, str) and name.lower() == str(uni.get("name", "")).lower():
        return True, "same alpha_id, horizon and universe name"
    have, want = _symbols(_from_cfg(entry, "symbols")), _symbols(uni.get("symbols"))
    if have is None and not isinstance(name, str):
        return False, "entry records no universe"
    if have is not None and have == want:
        return True, "same alpha_id, horizon and symbol set"
    return False, "universe differs"


def _ledger_entries(root: Path) -> dict[str, dict[str, Any]]:
    path = root / LEDGER
    if not path.is_file():
        return {}
    entries = json.loads(path.read_text(encoding="utf-8")).get("entries", [])
    return {str(e.get("key", "")): e for e in entries}


def memory_search_prior(
    memory: Memory,
) -> Callable[[Mapping[str, Any]], list[dict[str, Any]]]:
    """``search_prior`` backed by ``memory``: ledger entries of the same alpha id
    (judged by :func:`same_test`) and a few result documents for context."""
    entries = _ledger_entries(memory.root)

    def search_prior(h: Mapping[str, Any]) -> list[dict[str, Any]]:
        alpha = str(h.get("alpha_id") or "")
        if not alpha:
            return []
        res = memory.prior_tests(
            alpha=alpha,
            text=" ".join(str(x) for x in (h.get("horizon"), h.get("mechanism")) if x),
            k=MAX_LEDGER_CANDIDATES,
        )
        out: list[dict[str, Any]] = []
        for hit in res["ledger"]:
            entry = entries.get(hit.path.split("#", 1)[1] if "#" in hit.path else "", {})
            same, why = same_test(entry, h) if entry else (False, "entry not readable")
            out.append({"id": hit.citation, "kind": "ledger", "same_test": same, "reason": why})
        for hit in res["documents"][:MAX_DOCUMENT_HITS]:
            out.append(
                {
                    "id": hit.citation,
                    "kind": hit.kind,
                    "same_test": False,
                    "reason": "text match only",
                    "heading": sanitise(hit.heading, 200),
                }
            )
        return out

    return search_prior


# ----------------------------------------------------------------- holdout --
def registry_is_holdout(
    root: Path | None = None, registry: Path | str | Mapping[str, Any] | None = None
) -> Callable[[str], bool]:
    """``is_holdout`` backed by the holdout registry.  True (refuse) unless
    ``name`` is a dataset label or dataset version the registry holds as
    ``in_sample`` and :func:`holdout_registry.refuse_unit_name` passes it.
    A missing or edited registry raises :class:`HoldoutRefused` here."""
    reg = (
        registry
        if isinstance(registry, Mapping)
        else holdout_registry.load_registry(registry, root)
    )
    datasets = reg.get("datasets", {})
    states = {str(e.get("label")): e.get("state") for e in datasets.values()}
    states.update({k: e.get("state") for k, e in datasets.items()})

    def is_holdout(name: str) -> bool:
        if states.get(name) != "in_sample":
            return True
        try:
            holdout_registry.refuse_unit_name(name, registry=reg)
        except holdout_registry.HoldoutRefused:
            return True
        return False

    return is_holdout


# ---------------------------------------------------------------- verifier --
def artefact_evidence(doc: Any) -> list[tuple[str | None, float]]:
    """``(field, value)`` pairs a text about ``doc`` may quote.

    The evidence subtrees of :func:`iap.llm.verify.evidence` when ``doc`` has
    any; otherwise (an alpha report keeps its statistics at the top level)
    every stored number of ``doc`` with its field name, free-text fields
    excluded.  The second case is weaker: counts and ids count as evidence."""
    found = numverify.evidence(doc)
    return found if found else numverify.evidence({"result": doc})


def text_problems(text: str, evidence: list[tuple[str | None, float]]) -> list[str]:
    """The number rules of :mod:`iap.llm.verify` applied to ``text`` against
    ``evidence``: every number written must equal (or round at the precision
    shown to) a stored value, a labelled number (``t = 2.4``, ``IC 0.041``)
    must match a field of that statistic, and no number may be written in
    words or with look-alike letters.  ``[]`` means the text passes."""
    problems: list[str] = []
    words = numverify.number_words(text)
    if words:
        problems.append(f"numbers written as words: {words}")
    disguised = numverify.disguised_numbers(text)
    if disguised:
        problems.append(f"numbers written with look-alike letters: {disguised}")
    bad = []
    # _numbers_with_labels is verify.py's own reader (label binding included);
    # reusing it keeps one definition of "a number in the text".
    for tok, pct, family in numverify._numbers_with_labels(text):
        pool = [
            v for k, v in evidence if family is None or (k and numverify.field_in_family(k, family))
        ]
        if not numverify.matches(tok, pct, pool):
            bad.append(tok + ("%" if pct else "") + (f" ({family})" if family else ""))
    if bad:
        problems.append(f"numbers not in the evidence: {bad}")
    return problems


def narrative_problems(text: str, facts: Mapping[str, Any]) -> list[str]:
    """Why ``text`` may not stand as the narrative; ``[]`` means it may:
    :func:`text_problems` with the run's numeric facts as the only evidence,
    and the text must name the verdict code computed."""
    evidence = numverify.evidence({"result": {k: facts[k] for k in NUMERIC_FACTS if k in facts}})
    problems = text_problems(text, evidence)
    verdict = str(facts.get("verdict", ""))
    if verdict in VERDICTS and verdict not in text:
        problems.append(f"the verdict {verdict!r} is not stated")
    return problems


def verify_narrative(text: str, facts: Mapping[str, Any]) -> bool:
    return not narrative_problems(text, facts)


# ------------------------------------------------------------------- stubs --
def stub_runner(draft: Mapping[str, Any]) -> dict[str, Any]:
    """Fixed numbers; reads nothing.  Replace with a real in-sample runner."""
    return dict(STUB_RUN)


def template_writer(facts: Mapping[str, Any]) -> str:
    """A writer that only copies facts into a sentence (stands in for a model)."""
    return (
        f"Verdict {facts['verdict']}: t = {facts['t_stat']} over "
        f"{facts['n_sessions']} sessions against |t| >= {facts['min_abs_t']}."
    )


def load_runner(spec: str) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """``package.module:function`` to a callable (for ``--runner``)."""
    mod, _, fn = spec.partition(":")
    if not mod or not fn:
        raise ValueError(f"runner must be module:function, got {spec!r}")
    runner = getattr(importlib.import_module(mod), fn)
    if not callable(runner):
        raise ValueError(f"{spec} is not callable")
    return runner


# ----------------------------------------------------------------- factory --
def wired_deps(
    *,
    root: Path = REPO_ROOT,
    memory: Memory | None = None,
    registry: Path | str | Mapping[str, Any] | None = None,
    run_in_sample: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    write: Callable[[Mapping[str, Any]], str] | None = None,
    thresholds: Mapping[str, Any] | None = None,
) -> ResearchDeps:
    """The research flow's dependencies with memory, registry and verifier wired."""
    extra: dict[str, Any] = {"thresholds": dict(thresholds)} if thresholds else {}
    return ResearchDeps(
        search_prior=memory_search_prior(memory or Memory(Path(root))),
        run_in_sample=run_in_sample or stub_runner,
        is_holdout=registry_is_holdout(Path(root), registry),
        write=write or template_writer,
        verify=verify_narrative,
        **extra,
    )


def build_wired_flow(**kwargs: Any) -> Graph:
    """:func:`build_research_graph` over :func:`wired_deps` (same keywords)."""
    return build_research_graph(wired_deps(**kwargs))
