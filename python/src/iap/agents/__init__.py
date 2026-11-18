"""Agent layer: controls that let research agents work without fooling the platform.

Off the trading path by construction (``tests/test_import_policy.py`` keeps every
trading-path package from importing this one).  Standard library plus
``cryptography`` (Ed25519 agent signatures, :mod:`iap.governance.signing`, v1.10).

- The governance primitives (``untrusted``, ``citations``, ``blackboard``, ``broker``,
  ``mcp_server``, ``workflow``, ``approvals``, ``prereg_gate``, ``reserve``) live in
  :mod:`iap.governance` (moved in v2.1 Track 3; the ``iap.agents`` aliases were removed
  2026-11-18).  The identity, ``fingerprint``, ``signing`` and ``trust`` aliases stay;
  scripts bound by signed pre-registrations import them by this path.
- ``evals``      - each control must fail when removed.
- ``eval_harness`` - fixed agent task set, stub model, scored against a baseline.
"""
