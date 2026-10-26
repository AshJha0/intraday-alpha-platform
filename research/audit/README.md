# research/audit

`CHECKPOINTS.jsonl` is the repository anchor of the sealed services' audit chains (v2.0 stage 2, D2 / CR1;
`docs/GOVERNANCE.md` section 3). The registered runner appends one line per served read on the
protected `results` branch: `{"partition": "a"|"b"|"c", "checkpoint": {seq, chain_head, utc, prereg_id,
dataset_hash_id}}`, and nothing else (`python tools/sealed_service.py verify-checkpoints` enforces the
content rule and the sequence order). The file does not exist on `main` until the first registered run's
branch is merged; this README is the placeholder that keeps the directory and its CODEOWNERS line.
