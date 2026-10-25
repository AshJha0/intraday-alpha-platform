# Installing the registered runner (owner steps)

v2.0 stage 2, CR1. Nothing in the repository installs software or changes the machine; every step below is the
owner's, run once. The result is a GitHub Actions self-hosted runner whose Windows service runs as the local
account `iap-runner` (created on 2026-10-25 by `tools/admin/create_sealed_accounts.ps1`, DR-5), picking up only
the `registered-run` workflow. Settings on GitHub are in [REPO_SETTINGS.md](REPO_SETTINGS.md) section 7.

## 1. An interpreter the service accounts can run

The sealed accounts and `iap-runner` cannot read the owner's profile (`C:\Users\<owner>\...`), so a Python that
lives there is unusable for them. Install CPython 3.12 for all users (for example `C:\Python312`), or copy the
repository's CI virtual environment to a readable location, and keep the repository checkout used by the runner
outside the owner's profile (the runner's own work directory below is fine). The pinned stack is installed by the
workflow itself from `python/requirements-ci.txt` (`--require-hashes`).

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
  --work _work --replace --runasservice --windowslogonaccount iap-runner --windowslogonpassword <password of iap-runner>
```

`--runasservice` with `--windowslogonaccount iap-runner` installs the Windows service under that account
(`.\svc.cmd status` / `start` / `stop` manage it). The labels must be exactly those the workflow asks for
(`runs-on: [self-hosted, windows, iap-runner]`). Grant `iap-runner` "Log on as a service" if the installer asks.

## 4. Runner group and policy (GitHub, organisation or repository settings)

- Runner group `sealed`: allowed only for this repository and only for the workflow
  `.github/workflows/registered-run.yml` ("Allow selected workflows").
- Actions -> General: "Require approval for all outside collaborators" (fork pull requests never run on the
  self-hosted runner; the workflow has no pull-request trigger in any case).
- The `sealed` environment: required reviewer = the owner's account; no environment secrets and no environment
  variables named `ACTIONS_STEP_DEBUG` or `ACTIONS_RUNNER_DEBUG`. Commands in REPO_SETTINGS.md 7.2.

## 5. The services the runner talks to

One `tools/sealed_service.py serve` per partition, running as the partition's identity (SEALED_DATA.md
section 5). The runner reaches them through `\\.\pipe\iap-sealed-<partition>`; no configuration on the runner
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

## 7. What the owner keeps

The `iap-runner` password (in the password manager, never in the repository or an environment), the runner's
registration (re-run `config.cmd --replace` after a reinstall), and the decision to approve each run. Rotating
the account password means `.\config.cmd remove` and re-registering with the new one.
