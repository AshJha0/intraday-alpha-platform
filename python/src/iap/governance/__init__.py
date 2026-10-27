"""Governance primitives shared by the research loop, the LLM layer and the agent layer.

Moved out of :mod:`iap.agents` in v2.1 Track 3 so that ``iap.research`` and ``iap.llm`` can use them
without importing the agent layer (which imports both): that dependency was the source of the
agents <-> llm and agents <-> research import cycles.  The modules are unchanged; the old
``iap.agents.<name>`` paths are aliases (see each shim there).

- ``untrusted``  - free text from ledgers, reports and agents is data, never instructions.
- ``citations``  - resolve ``experiment:<id>`` / ``alpha:<id>`` / ``lifecycle:<n>`` references.
- ``blackboard`` - append-only, hash-chained JSONL of tasks, claims, findings, pre-registrations.
- ``broker``     - the only writer; validates, attributes and replays.
- ``signing`` / ``trust`` - Ed25519 agent signatures and the trust roster.
- ``fingerprint`` - code fingerprint of an alpha.
- ``prereg_gate`` / ``analysis_identity`` - the pre-registration gate and the bound analysis identity.
- ``workflow``   - propose -> draft prereg -> HUMAN sign-off.
- ``mcp_server`` - read-only Model Context Protocol server over stdio.
- ``screen_gate`` - the net-of-cost screen check a pre-registration may require.
- ``claims`` / ``holdout_registry`` - the claims register and the holdout session registry
  (moved from ``iap.research``; same aliasing).

"""
