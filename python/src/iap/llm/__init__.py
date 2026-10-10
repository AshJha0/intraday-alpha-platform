"""LLM research agent (v1.11, AI1/AI2): a Claude model driving research
through the agent governance of :mod:`iap.agents`.

The boundary (docs/governance/GOVERNANCE.md §2b): the model proposes
hypotheses, pre-registers them through the signed
:class:`~iap.agents.broker.WriteBroker` (one look each), asks for gated runs
and files findings.  It never computes a number: every number in a finding
must be copied from a tool result and is verified post hoc against the
artefact it cites (:mod:`iap.llm.verify`).  The ``anthropic`` SDK is optional
(``pip install -e "python[llm]"``); tests and CI use the scripted client in
:mod:`iap.llm.fake` - no network, no spend.
"""
