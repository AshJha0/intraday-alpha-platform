"""Agent layer: controls that let research agents work without fooling the platform.

Off the trading path by construction (``tests/test_import_policy.py`` keeps every
trading-path package from importing this one).  Standard library plus
``cryptography`` (Ed25519 agent signatures, :mod:`iap.governance.signing`, v1.10).

- ``untrusted``  - free text from ledgers, reports and agents is data, never instructions.
- ``citations``  - resolve ``experiment:<id>`` / ``alpha:<id>`` / ``lifecycle:<n>`` references.
- ``blackboard`` - append-only, hash-chained JSONL of tasks, claims, findings, pre-registrations.
- ``broker``     - the only writer; validates, attributes and replays.
- ``mcp_server`` - read-only Model Context Protocol server over stdio.
- ``evals``      - each control must fail when removed.
- ``workflow``   - the agent job (v1.15): propose (one ledger look) -> draft prereg ->
  HUMAN sign-off; confirmatory runs, outcomes and promotion are human-only.
- ``eval_harness`` - fixed agent task set, stub model, scored against a baseline.
"""
