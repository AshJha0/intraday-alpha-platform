# Installing the registered runner (owner steps)

v2.0 stage 2, CR1. Nothing in the repository installs software or changes the machine; every step below is the
owner's, run once. The result is a GitHub Actions self-hosted runner whose Windows service runs as the local
account `iap-runner` (created on 2026-10-25 by `tools/admin/create_sealed_accounts.ps1`, DR-5), picking up only
the `registered-run` workflow. Settings on GitHub are in [REPO_SETTINGS.md](REPO_SETTINGS.md) section 7.

## 0. Shell

The workflow's steps run under Windows PowerShell 5.1 (`shell: powershell`), which every Windows installation has;
PowerShell 7 (`pwsh`) is not required and the owner's workstation does not have it (run 40222182056 failed on
`pwsh: command not found`). `gh` is not required on the runner; the API is called with `Invoke-RestMethod`.

## 1. An interpreter the service accounts can run

The sealed accounts and `iap-runner` cannot read the owner's profile (`C:\Users\<owner>\...`), so a Python that
lives there is unusable for them. Install CPython 3.12.10 for all users at `C:\Python312` and clone the repository at
`C:\iap\repo`, exactly as [`tools/admin/install_all_users_python.md`](../../tools/admin/install_all_users_python.md)
describes (interpreter, pinned stack, checkout ACL, how the runner finds the interpreter, the optional
`TIINGO_API_KEY` in `C:\actions-runner\.env`). The runner's own work directory below holds its checkout. The pinned
stack is also installed by the workflow itself from `python/requirements-ci.txt` (`--require-hashes`).

## 2. The runner work directory

```powershell
# elevated PowerShell
New-Item -ItemType Directory C:\actions-runner | Out-Null
icacls C:\actions-runner /inheritance:r /grant:r "iap-runner:(OI)(CI)F" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"
```

The owner's normal user has no entry: the checkout the runner makes, its logs and its `_work` tree are not
readable by the development identity (property 1).

## 3. Register the runner (GitHub UI gives the token)

Repository -> Settings -> Actions -> Runners -> New self-hosted runner -> Windows x64. Download the runner
package **into `C:\actions-runner` as the administrator** (the repository does not download anything), verify
the SHA-256 GitHub prints, then:

```powershell
cd C:\actions-runner
.\config.cmd --url https://github.com/AshJha0/intraday-alpha-platform --token <registration token> `
  --name iap-runner-ws --labels self-hosted,windows,iap-runner --runnergroup "sealed" `
  --work _work --replace --runasservice --windowslogonaccount iap-runner
```

`--runasservice` with `--windowslogonaccount iap-runner` installs and starts the Windows service
(`actions.runner.AshJha0-intraday-alpha-platform.iap-runner-ws`) under that account. Omit
`--windowslogonpassword`: `config.cmd` then prompts for the password, which keeps it out of the shell
history. There is no `svc.cmd` on Windows (`svc.sh` exists only on Linux and macOS runners); manage the
service with PowerShell: `Get-Service actions.runner.*`, `Stop-Service` / `Start-Service` on that name,
or `services.msc`. The labels must be exactly those the workflow asks for
(`runs-on: [self-hosted, windows, iap-runner]`). Grant `iap-runner` "Log on as a service" if the installer asks
(stage 11 correction, G2: the earlier text named `.\svc.cmd`, which the owner found absent on 2026-10-25).

## 4. Runner group and policy (GitHub, organisation or repository settings)

- Runner group `sealed`: allowed only for this repository and only for the workflow
  `.github/workflows/registered-run.yml` ("Allow selected workflows").
- Actions -> General: "Require approval for all outside collaborators" (fork pull requests never run on the
  self-hosted runner; the workflow has no pull-request trigger in any case).
- The `sealed` environment: required reviewer = the owner's account; no environment secrets and no environment
  variables named `ACTIONS_STEP_DEBUG` or `ACTIONS_RUNNER_DEBUG`. Commands in REPO_SETTINGS.md 7.2.

## 5. The services the runner talks to

One `tools/sealed_service.py serve` per partition, running as the partition's identity, installed by
`tools/admin/install_sealed_services.ps1` (SEALED_DATA.md section 5). The runner reaches them through `\\.\pipe\iap-sealed-<partition>`; no configuration on the runner
side. The runner's private cache is `C:\iap-sealed\runner\cache` (owned by `iap-runner`), created and emptied by
`tools/runner/run.py` on every run.

## 6. First-run checks (no sealed read)

1. From the runner's service account context the identity check must pass:
   `python tools/runner/run.py whoami` prints `iap-runner` and exits 0 (run it through the workflow's first
   step, or with `psexec -u iap-runner` from an elevated shell).
2. Start `registered-run` from the Actions tab with a prereg that is **not** signed: the run must stop at the
   environment approval, then, once approved, fail in the "Registered run" step with exit 3 and a redacted log
   that names no path and no account. Nothing was requested from any service (its audit shows no `request`
   line for that time).
3. With the dry-registration fixture of `python/tests/runner/` the full procedure is exercised in CI on
   synthetic data; a real registration waits for a signed prereg with a `registered_run` block, the pinned
   image digest in `deployment/docker/IMAGE_DIGEST` (P1), and the owner's words.
4. The first real run of the workflow is the DD1 ingestion in mode `dd1` (no prereg; the same approval and
   identity): [DD1_INGESTION_RUNBOOK.md](DD1_INGESTION_RUNBOOK.md).

## 7. What the owner keeps

The `iap-runner` password (in the password manager, never in the repository or an environment), the runner's
registration (re-run `config.cmd --replace` after a reinstall), and the decision to approve each run. Rotating
the account password means `Stop-Service actions.runner.*`, `.\config.cmd remove --token <removal token>` and
re-registering with `--runasservice --windowslogonaccount iap-runner` (the password is prompted for, never passed
on the command line); check with `Get-Service actions.runner.*` that the service is back and running.
