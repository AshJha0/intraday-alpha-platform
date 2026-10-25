"""The registered runner (v2.0 stage 2, CR1; U0 section 2).

Executed by the self-hosted GitHub Actions runner that runs as ``iap-runner``
(``.github/workflows/registered-run.yml``, protected environment ``sealed``):

``verify``         signature, freeze hashes, dataset hashes, ``analysis_identity``
                   and the pinned image digest, all before any request
``sealed_client``  the one request to the sealed service (OS-authenticated pipe)
``redact``         the log filter: no path, account, host or data leaves the job
``results``        the append-only ``results`` branch, committed as ``iap-runner``
``run``            the orchestration: verify, request, run once, write result and
                   hashes, commit, publish the checkpoint, upload a redacted log

Nothing here is importable from ``iap``; the runner is a tool, not a library.
"""
