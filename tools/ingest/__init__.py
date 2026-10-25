"""``tools/ingest``: the DD1 ingestion job (v2.0 stage 2; U0 section 2, section 3 "Stage 2").

``dd1_daily.py`` runs as ``iap-runner`` only; ``dd1_fixture.py`` builds the
synthetic vendor-format fixtures the dry run and CI use.  Nothing here is
imported by ``iap``.
"""
