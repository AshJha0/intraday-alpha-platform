# An all-users CPython 3.12.10 for the sealed services and the runner (owner steps)

v2.0 stage 2 DD1. The sealed accounts (`iap-sealed-a/b/c`) and `iap-runner` are standard local users that cannot read
the owner's profile, so a Python installed under `C:\Users\<owner>\...` (the default per-user install) is unusable for
them, and so is a virtual environment created there. Nothing in the repository downloads or installs an interpreter;
these are the owner's steps, run once. 3.12.10 is the interpreter every v1.18 identity-v2 prereg binds and the one the
reproduction image pins (`deployment/docker/Dockerfile.repro`).

## 1. Install for all users at `C:\Python312`

1. Download the Windows x64 installer of CPython **3.12.10** from python.org (`python-3.12.10-amd64.exe`) as the
   administrator; verify the SHA-256 the download page prints.
2. Run it elevated with the all-users options (unattended form; the GUI equivalents are "Customize installation",
   "Install for all users", install path `C:\Python312`, no launcher for all users needed, no PATH change required):

```powershell
.\python-3.12.10-amd64.exe /quiet InstallAllUsers=1 TargetDir=C:\Python312 PrependPath=0 Include_test=0 Include_launcher=0 CompileAll=1
```

3. Check from an elevated shell that every account can run it (the install sets `Users: read and execute` on
   `C:\Python312`; keep it):

```powershell
C:\Python312\python.exe --version
icacls C:\Python312 | findstr /c:"Users"
```

## 2. The pinned research stack, installed into that interpreter

The services import `iap.agents.signing` (which needs `cryptography`) from the repository checkout; the ingestion job
needs nothing beyond the standard library and the `iap` package, but the registered-run workflow installs the whole
pinned stack. Install the lock into the all-users interpreter so both are satisfied (elevated; `--require-hashes`
makes the lock the only source of truth):

```powershell
C:\Python312\python.exe -m pip install --upgrade pip
C:\Python312\python.exe -m pip install --require-hashes -r C:\iap\repo\python\requirements-ci.txt
```

A virtual environment is also fine provided it lives outside any profile (for example `C:\iap\venv`, created with
`C:\Python312\python.exe -m venv C:\iap\venv`) and the install script is pointed at its `python.exe` with `-Python`.

## 3. The checkout the services and the runner read

Clone the repository outside the owner's profile, read-only to everyone but the owner:

```powershell
git clone https://github.com/AshJha0/intraday-alpha-platform C:\iap\repo
icacls C:\iap\repo /inheritance:r /grant:r "Users:(OI)(CI)RX" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" "%USERNAME%:(OI)(CI)F"
```

`C:\iap\repo` is what `install_sealed_services.ps1 -Repo` and the scheduled tasks use as their working directory; the
services resolve `python/src` from that checkout (`tools/sealed_service.py` also honours `IAP_REPO` if the script is
ever run from a copy). Update it with `git -C C:\iap\repo pull` as the owner and restart the three service tasks
(`Restart-ScheduledTask` does not exist: `Stop-ScheduledTask` then `Start-ScheduledTask` on "IAP sealed service x").
The runner has its own checkout under `C:\actions-runner\_work` (docs/GOVERNANCE.md section 2).

## 4. Point the services and the runner at it

- Services: `tools\admin\install_sealed_services.ps1 -Repo C:\iap\repo -Python C:\Python312\python.exe` (the default
  values) registers the three tasks with that interpreter.
- Runner: the GitHub Actions runner uses `python` from `PATH` of the `iap-runner` service; either put `C:\Python312`
  first in the machine `PATH` (System Properties, or `[Environment]::SetEnvironmentVariable('Path', "C:\Python312;" +
  [Environment]::GetEnvironmentVariable('Path', 'Machine'), 'Machine')` elevated, then restart the runner service),
  or write `PATH=C:\Python312;...` into `C:\actions-runner\.env` (the runner reads that file at service start).
- Optional Tiingo key for the ingestion job: a line `TIINGO_API_KEY=<key>` in `C:\actions-runner\.env` (readable only
  by `iap-runner` and administrators, docs/GOVERNANCE.md section 2). It is never a GitHub secret and never printed.
