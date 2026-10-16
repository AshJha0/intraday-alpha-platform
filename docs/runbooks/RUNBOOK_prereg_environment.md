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

## C8 breadth book (v1.17, option a)

Run these from the repository root in PowerShell. First build the one-time `.venv-ci` (see above), then:

```powershell
git pull
$env:PYTHONUTF8 = "1"
.\.venv-ci\Scripts\python tools\breadth_book_register.py . --dry-run --require-ci-env
.\.venv-ci\Scripts\python tools\breadth_book_register.py . --require-ci-env
git add research\preregs\breadth_book_prereg.json
git commit -m "C8: register the breadth book (option a, screen refused)"
```

A good dry run prints the following lines. The sha256 values are whatever is on disk:

```
[ci-env] deps match requirements-ci.txt: OK
draft option (a) ok; screen research/breadth/screen/screen_multi7.json sha256 matches the pin
[dry-run] draft sha256 <64 hex>
[dry-run] screen gate for EQ11/15m/linear_z_v1: EQ11/15m/linear_z_v1 was NOT screened by the net-of-cost screen ...: no confirmatory run (v1.17 C6)
[dry-run] would call WriteBroker.preregister(EQ11/15m:breadth-book-c8, screen=...) -> refused (expected under option a); would write research/preregs/breadth_book_prereg.json
```

A good registration prints `maintainer key matches trust root ashjha0`, then
`C8-BREADTH-BOOK: screen gate REFUSED: EQ11/15m/linear_z_v1 was NOT screened ...`, then
`C8-BREADTH-BOOK: wrote research/preregs/breadth_book_prereg.json (sha256 ...)`.

That file is signed. Its status reads "REGISTERED under option (a) ... no confirmatory
look", `on_blackboard` is false and `registration.holdout_read` is false. The refusal
happens before any ledger debit, so no look is charged. No data is read and no holdout
dataset is built or downloaded.

The tool exits with an error, and writes nothing, in any of these cases:
- the draft's decision is not option (a)
- the screen's sha256 differs from the pinned value
- the output file already exists
