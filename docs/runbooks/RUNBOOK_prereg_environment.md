# Runbook: register pre-registrations in the CI environment

A pre-registration's fingerprint records `deps` (installed numpy, pandas, scipy).
`iap.agents.prereg_gate.require` refuses a registered run whose current deps differ
("deps changed since pre-registration"). CI installs `python/requirements-ci.txt`, so a
prereg registered with other versions is refused in CI (this happened to R7 in v1.15).
Register from a venv built from that lock. Nothing here changes existing registrations.

## Check (any registration script built on the helper)

Registration scripts call `iap.agents.fingerprint.check_registration_env`. It prints a
warning when the local numpy/pandas/scipy differ from the lock; `--require-ci-env`
refuses (exit 2) instead:

```powershell
python tools\quoting_v2_register.py . --dry-run --require-ci-env
```

## One-time: build the CI venv (owner, from the repo root, Windows)

Same Python as CI's main job (3.11) and the same install command as `.github/workflows/ci.yml`.

```powershell
py -3.11 -m venv .venv-ci
.\.venv-ci\Scripts\python -m pip install --upgrade pip
.\.venv-ci\Scripts\python -m pip install --require-hashes -r python\requirements-ci.txt
```

The lock includes `cryptography` (signing). The package itself is not installed: the
registration scripts put `python/src` on `sys.path`, as CI does with `PYTHONPATH`.

## Each registration

```powershell
$env:PYTHONUTF8 = "1"
.\.venv-ci\Scripts\python tools\<script>_register.py . --dry-run --require-ci-env
.\.venv-ci\Scripts\python tools\<script>_register.py . --require-ci-env
```

The first line must print `[ci-env] deps match requirements-ci.txt: OK`. When the lock
is bumped, rebuild the venv (`Remove-Item -Recurse -Force .venv-ci`, then the steps above) before
registering. `.venv-ci/` is git-ignored.
