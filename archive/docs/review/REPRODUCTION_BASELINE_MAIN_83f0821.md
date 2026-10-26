# Baseline reproduction record (2026-10-18)

**Result: NOT REPRODUCED.** 1 command(s) failed; 27 discrepancies.

- Repository: `https://github.com/AshJha0/intraday-alpha-platform.git`
- Commit: `83f08210879263e03d2b8d068cc4bc21f04ed76d`
- Clean clone: `C:\Users\ashis\AppData\Local\Temp\claude\C--Work-Claude\1fdb53d2-02b4-4f3c-b54f-469a9746bed3\scratchpad\iap-repro-us39nphe\repo` (temporary, deleted after the run unless --keep)
- Interpreter: `3.12.10` (CPython), `Windows-11-10.0.26200-SP0`
- Lock file: `python/requirements-ci.txt` sha256 `3a4352e0d19d1a41021264ef8d382472f7c668520c42535e5f45971600a28375`, installed with `--require-hashes`
- Packages installed: 36 (full list in the JSON record)

## Commands

| # | command | exit |
|---|---|---|
| 1 | `git clone --quiet --no-hardlinks https://github.com/AshJha0/intraday-alpha-platform.git C:\Users\ashis\AppData\Local\Temp\claude\C--Work-Claude\1fdb53d2-02b4-4f3c-b54f-469a9746bed3\scratchpad\iap-repro-us39nphe\repo` | 0 |
| 2 | `git -c advice.detachedHead=false checkout --quiet 83f08210879263e03d2b8d068cc4bc21f04ed76d` | 0 |
| 3 | `git rev-parse HEAD` | 0 |
| 4 | `C:\Users\ashis\AppData\Local\Programs\Python\Python312\python.exe -m venv C:\Users\ashis\AppData\Local\Temp\claude\C--Work-Claude\1fdb53d2-02b4-4f3c-b54f-469a9746bed3\scratchpad\iap-repro-us39nphe\venv` | 0 |
| 5 | `C:\Users\ashis\AppData\Local\Temp\claude\C--Work-Claude\1fdb53d2-02b4-4f3c-b54f-469a9746bed3\scratchpad\iap-repro-us39nphe\venv\Scripts\python.exe -m pip install --quiet --disable-pip-version-check --no-input --require-hashes --only-binary :all: -r python/requirements-ci.txt` | 0 |
| 6 | `C:\Users\ashis\AppData\Local\Temp\claude\C--Work-Claude\1fdb53d2-02b4-4f3c-b54f-469a9746bed3\scratchpad\iap-repro-us39nphe\venv\Scripts\python.exe -c import json,sys,platform,importlib.metadata as m;print(json.dumps({'python_version':sys.version.split()[0],'implementation':platform.python_implementation(),'executable':sys.executable,'packages':{d.metadata['Name']:d.version for d in m.distributions()}}))` | 0 |
| 7 | `C:\Users\ashis\AppData\Local\Temp\claude\C--Work-Claude\1fdb53d2-02b4-4f3c-b54f-469a9746bed3\scratchpad\iap-repro-us39nphe\venv\Scripts\python.exe tools/v118_baseline.py --check` | 1 |
| 8 | `C:\Users\ashis\AppData\Local\Temp\claude\C--Work-Claude\1fdb53d2-02b4-4f3c-b54f-469a9746bed3\scratchpad\iap-repro-us39nphe\venv\Scripts\python.exe tools/v118_baseline.py` | 0 |
| 9 | `git status --porcelain` | 0 |

## Comparison with the committed baseline_metrics.json (programme 9.6)

- Byte-identical: **False**
- Committed sha256: `f5b53612c3193e6f2717ee0b3b721ce4884d2c019c9615ad9e2e03b3f0189c27`
- Reproduced sha256: `42c5e2aa0f2af6c16db4e3a2e4fe7d8fbd117e9d75ff2cbe39ea7a1c1007d291`
- Expected provenance differences: 1
- Within-tolerance differences: 0
- Discrepancies: **27**

### Expected provenance differences

| path | committed | reproduced |
|---|---|---|
| `commit` | `8fb189b67bd6a2b6a2c868ed378d2a5a92c628f0` | `83f08210879263e03d2b8d068cc4bc21f04ed76d` |

### Discrepancies

| path | committed | reproduced |
|---|---|---|
| `ledger.bonferroni_t_threshold` | `4.4466258141863175` | `4.484021317917086` |
| `ledger.distinct_experiments` | `422` | `640` |
| `ledger.document_datasets_summary` | `4` | `8` |
| `ledger.entries` | `422` | `640` |
| `ledger.entries_by_dataset_version_prefix.535f4868` | `None` | `78` |
| `ledger.entries_by_dataset_version_prefix.dea2a6b0` | `None` | `84` |
| `ledger.entries_by_dataset_version_prefix.ee4c2187` | `24` | `72` |
| `ledger.entries_by_dataset_version_prefix.none` | `39` | `47` |
| `ledger.entries_without_dataset_version.entries` | `39` | `47` |
| `ledger.entries_without_dataset_version.kinds.experiment_runner` | `25` | `33` |
| `ledger.entries_without_dataset_version.looks` | `734` | `1242` |
| `ledger.looks_by_dataset_version_prefix.535f4868` | `None` | `78` |
| `ledger.looks_by_dataset_version_prefix.dea2a6b0` | `None` | `84` |
| `ledger.looks_by_dataset_version_prefix.ee4c2187` | `24` | `448` |
| `ledger.looks_by_dataset_version_prefix.none` | `734` | `1242` |
| `ledger.looks_by_kind.combination` | `1098` | `1478` |
| `ledger.looks_by_kind.execution_study` | `None` | `206` |
| `ledger.looks_by_kind.experiment_runner` | `720` | `1228` |
| `ledger.real_data_looks_per_document_summary` | `24` | `448` |
| `ledger.recomputed_chain_head` | `1f95affdee760e380149f400b4db86f0b3e87ae0a17181890ede3301116e489d` | `5dc957df3bf2674307ebe8da14a3cd037a54c3fc013b8b049a353817a752e669` |
| `ledger.sha256` | `218967c800bc2a24b27424d79f8f6b842041235b5aadbd184fc9e16c0aeb4cc7` | `e1f15f6889a3d2b504aa8a5dd66c2b78497f15b6697f00e6beb635a32197994e` |
| `ledger.stored_chain_head` | `1f95affdee760e380149f400b4db86f0b3e87ae0a17181890ede3301116e489d` | `5dc957df3bf2674307ebe8da14a3cd037a54c3fc013b8b049a353817a752e669` |
| `ledger.sum_of_entry_counts` | `5732` | `6826` |
| `ledger.total_looks` | `5732` | `6826` |
| `toolchain.ci_yml_sha256` | `52c100b9f9a3e3faca20d5f8db7a2b0c77d686ce3c4e8f3e9f3c27bf9ec899fc` | `160b6ced4afc49db8ad80dc0397219758c0dcb015f1f7b8b02ceceb90c181c2d` |
| `toolchain.requirements_ci_hash_lines` | `808` | `812` |
| `toolchain.requirements_ci_sha256` | `945e9c0dcf3328a1cf81a20fd97eea52fb28d2a98c8aa97636da90298d951f20` | `3a4352e0d19d1a41021264ef8d382472f7c668520c42535e5f45971600a28375` |

