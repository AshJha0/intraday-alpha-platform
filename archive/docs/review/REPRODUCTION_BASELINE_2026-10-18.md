# Baseline reproduction record (2026-10-18)

**Result: REPRODUCED (within the declared tolerance).** Every non-provenance value matches; differences are listed below.

- Repository: `https://github.com/AshJha0/intraday-alpha-platform.git`
- Commit: `8fb189b67bd6a2b6a2c868ed378d2a5a92c628f0`
- Clean clone: `C:\Users\ashis\AppData\Local\Temp\claude\C--Work-Claude\1fdb53d2-02b4-4f3c-b54f-469a9746bed3\scratchpad\iap-repro-ag1ww63l\repo` (temporary, deleted after the run unless --keep)
- Interpreter: `3.12.10` (CPython), `Windows-11-10.0.26200-SP0`
- Lock file: `python/requirements-ci.txt` sha256 `945e9c0dcf3328a1cf81a20fd97eea52fb28d2a98c8aa97636da90298d951f20`, installed with `--require-hashes`
- Packages installed: 34 (full list in the JSON record)

## Commands

| # | command | exit |
|---|---|---|
| 1 | `git clone --quiet --no-hardlinks https://github.com/AshJha0/intraday-alpha-platform.git C:\Users\ashis\AppData\Local\Temp\claude\C--Work-Claude\1fdb53d2-02b4-4f3c-b54f-469a9746bed3\scratchpad\iap-repro-ag1ww63l\repo` | 0 |
| 2 | `git -c advice.detachedHead=false checkout --quiet 8fb189b67bd6a2b6a2c868ed378d2a5a92c628f0` | 0 |
| 3 | `git rev-parse HEAD` | 0 |
| 4 | `C:\Users\ashis\AppData\Local\Programs\Python\Python312\python.exe -m venv C:\Users\ashis\AppData\Local\Temp\claude\C--Work-Claude\1fdb53d2-02b4-4f3c-b54f-469a9746bed3\scratchpad\iap-repro-ag1ww63l\venv` | 0 |
| 5 | `C:\Users\ashis\AppData\Local\Temp\claude\C--Work-Claude\1fdb53d2-02b4-4f3c-b54f-469a9746bed3\scratchpad\iap-repro-ag1ww63l\venv\Scripts\python.exe -m pip install --quiet --disable-pip-version-check --no-input --require-hashes --only-binary :all: -r python/requirements-ci.txt` | 0 |
| 6 | `C:\Users\ashis\AppData\Local\Temp\claude\C--Work-Claude\1fdb53d2-02b4-4f3c-b54f-469a9746bed3\scratchpad\iap-repro-ag1ww63l\venv\Scripts\python.exe -c import json,sys,platform,importlib.metadata as m;print(json.dumps({'python_version':sys.version.split()[0],'implementation':platform.python_implementation(),'executable':sys.executable,'packages':{d.metadata['Name']:d.version for d in m.distributions()}}))` | 0 |
| 7 | `C:\Users\ashis\AppData\Local\Temp\claude\C--Work-Claude\1fdb53d2-02b4-4f3c-b54f-469a9746bed3\scratchpad\iap-repro-ag1ww63l\venv\Scripts\python.exe tools/v118_baseline.py --check` | 0 |
| 8 | `C:\Users\ashis\AppData\Local\Temp\claude\C--Work-Claude\1fdb53d2-02b4-4f3c-b54f-469a9746bed3\scratchpad\iap-repro-ag1ww63l\venv\Scripts\python.exe tools/v118_baseline.py` | 0 |
| 9 | `git status --porcelain` | 0 |

## Comparison with the committed baseline_metrics.json (programme 9.6)

- Byte-identical: **False**
- Committed sha256: `23fd6f95dde455ed1f0068db445f56cac108c3fff5c32a53b975bb83c1c94c22`
- Reproduced sha256: `f5b53612c3193e6f2717ee0b3b721ce4884d2c019c9615ad9e2e03b3f0189c27`
- Expected provenance differences: 1
- Within-tolerance differences: 0
- Discrepancies: **0**

### Expected provenance differences

| path | committed | reproduced |
|---|---|---|
| `commit` | `8a43652c172b081f680ccfa1e3448d5904745ede` | `8fb189b67bd6a2b6a2c868ed378d2a5a92c628f0` |

