# IAP v2.0 stage 2 DD1: install the three sealed-data services on this workstation (owner, elevated PowerShell).
#
#   .\tools\admin\install_sealed_services.ps1 [-Repo C:\iap\repo] [-Python C:\Python312\python.exe]
#                                            [-SealedRoot C:\iap-sealed] [-DailyRoot C:\iap\daily] [-Partitions a,b,c]
#
# Prerequisites (docs/governance/DD1_INGESTION_RUNBOOK.md): the accounts and partitions exist
# (create_sealed_accounts.ps1, DR-5); CPython 3.12 is installed for all users (tools/admin/install_all_users_python.md)
# with the pinned stack; the repository is checked out OUTSIDE the owner's profile (default C:\iap\repo), readable by
# the sealed accounts and iap-runner. Nothing is downloaded. Safe to re-run: existing state is kept.
#
# What it does, per partition x in a, b, c:
#   1. asks for iap-sealed-x's password (the one set by create_sealed_accounts.ps1) and grants the account the
#      "Log on as a batch job" right, which a scheduled task with stored credentials needs;
#   2. runs `sealed_service.py install` AS iap-sealed-x (a one-off scheduled task: the elevated administrator has
#      no access to the partition by design, so the account itself creates C:\iap-sealed\x\service\, copies the
#      repository's trust roots and writes config.json = {"allowed_accounts": ["iap-runner"], "registered_images": []});
#   3. runs `sealed_service.py check` the same way and refuses to continue if the state is not readable and writable
#      by the account or `iap.agents.signing` does not import from the all-users interpreter;
#   4. registers the service task "IAP sealed service x": runs `python tools/sealed_service.py serve --partition x
#      --root C:\iap-sealed\x` as iap-sealed-x, "run whether user is logged on or not", at system start-up (a
#      service account never logs on interactively, so an at-log-on trigger would never fire; D-64), no time limit,
#      restarted on failure; and starts it now;
#   5. verifies the pipe answers: `sealed_service.py ping --partition x` from this elevated shell must get the audited
#      refusal `caller_account` (only iap-runner is served; any answer proves the service is up).
# It also creates the development-readable output root (default C:\iap\daily): iap-runner writes, Users read.

[CmdletBinding()]
param(
    [string]$Repo = 'C:\iap\repo',
    [string]$Python = 'C:\Python312\python.exe',
    [string]$SealedRoot = 'C:\iap-sealed',
    [string]$DailyRoot = 'C:\iap\daily',
    [string[]]$Partitions = @('a', 'b', 'c')
)

$ErrorActionPreference = 'Stop'

function Assert-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'run from an elevated (Administrator) PowerShell'
    }
}

function Grant-BatchLogon([string]$Account) {
    # SeBatchLogonRight via secedit (no external tool). Idempotent.
    $sid = (New-Object System.Security.Principal.NTAccount($Account)).Translate([System.Security.Principal.SecurityIdentifier]).Value
    $tmp = Join-Path $env:TEMP ('iap-rights-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $cfg = Join-Path $tmp 'cfg.inf'
    $db = Join-Path $tmp 'cfg.sdb'
    & secedit /export /cfg $cfg /areas USER_RIGHTS | Out-Null
    $lines = Get-Content $cfg
    $key = 'SeBatchLogonRight'
    $found = $false
    $out = foreach ($line in $lines) {
        if ($line -match "^$key\s*=") {
            $found = $true
            if ($line -match [regex]::Escape("*$sid")) { $line } else { "$line,*$sid" }
        } else { $line }
    }
    if (-not $found) {
        $out = @()
        foreach ($line in $lines) {
            $out += $line
            if ($line -match '^\[Privilege Rights\]') { $out += "$key = *$sid" }
        }
    }
    Set-Content -Path $cfg -Value $out -Encoding Unicode
    & secedit /configure /db $db /cfg $cfg /areas USER_RIGHTS | Out-Null
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    Write-Host "right:     Log on as a batch job -> $Account"
}

function Read-PlainPassword([string]$Prompt) {
    $secure = Read-Host -AsSecureString $Prompt
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Invoke-AsAccount([string]$TaskName, [string]$Account, [string]$Password, [string]$Arguments, [int]$TimeoutSec = 120) {
    # Run one command as the account through a transient scheduled task and return its exit code.
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute $Python -Argument $Arguments -WorkingDirectory $Repo
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $TaskName -Action $action -Settings $settings -User $Account -Password $Password -RunLevel Limited | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
        Start-Sleep -Seconds 2
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        $state = (Get-ScheduledTask -TaskName $TaskName).State
    } while ($state -eq 'Running' -and (Get-Date) -lt $deadline)
    $code = $info.LastTaskResult
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    return $code
}

Assert-Elevated
foreach ($path in @($Python, (Join-Path $Repo 'tools\sealed_service.py'), (Join-Path $Repo 'research\agents\trust_roots.json'))) {
    if (-not (Test-Path $path)) { throw "missing: $path" }
}
if ($Repo -like "$env:USERPROFILE*") { throw "the checkout must live outside your profile (the sealed accounts cannot read $env:USERPROFILE); use e.g. C:\iap\repo" }
if ($Python -like "$env:USERPROFILE*") { throw "the interpreter must be an all-users install (tools/admin/install_all_users_python.md), not $Python" }
foreach ($x in $Partitions) {
    if (-not (Get-LocalUser -Name "iap-sealed-$x" -ErrorAction SilentlyContinue)) { throw "account iap-sealed-$x does not exist: run create_sealed_accounts.ps1 first" }
    if (-not (Test-Path (Join-Path $SealedRoot $x))) { throw "partition $SealedRoot\$x does not exist: run create_sealed_accounts.ps1 first" }
}
if (-not (Get-LocalUser -Name 'iap-runner' -ErrorAction SilentlyContinue)) { throw 'account iap-runner does not exist' }

# -- the development-readable output root: iap-runner writes, everyone on the machine reads ------------------------
if (-not (Test-Path $DailyRoot)) { New-Item -ItemType Directory -Path $DailyRoot | Out-Null }
cmd /c "icacls `"$DailyRoot`" /inheritance:r /grant:r `"iap-runner:(OI)(CI)M`" `"Users:(OI)(CI)RX`" `"SYSTEM:(OI)(CI)F`" `"Administrators:(OI)(CI)F`" >nul"
if ($LASTEXITCODE -ne 0) { throw "ACL failed for $DailyRoot" }
Write-Host "daily:     $DailyRoot -> iap-runner writes, Users read"

$trustRoots = Join-Path $Repo 'research\agents\trust_roots.json'
$results = @{}
foreach ($x in $Partitions) {
    $account = "iap-sealed-$x"
    $root = Join-Path $SealedRoot $x
    Write-Host ''
    Write-Host "== partition $x ($account, $root)"
    $pw = Read-PlainPassword "Password for $account"
    Grant-BatchLogon $account

    $code = Invoke-AsAccount "IAP sealed install $x" $account $pw "tools\sealed_service.py install --root `"$root`" --trust-roots `"$trustRoots`""
    if ($code -ne 0) { throw "install as $account failed (exit $code): is $Repo readable by $account and $Python an all-users install?" }
    Write-Host "install:   $root\service (trust roots copied, config.json written)"

    $code = Invoke-AsAccount "IAP sealed check $x" $account $pw "tools\sealed_service.py check --partition $x --root `"$root`""
    if ($code -ne 0) { throw "check as $account failed (exit $code): the service state is not usable by the account" }
    Write-Host "check:     state readable and writable by $account; iap.agents.signing imports"

    $taskName = "IAP sealed service $x"
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute $Python -Argument "tools\sealed_service.py serve --partition $x --root `"$root`"" -WorkingDirectory $Repo
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -User $account -Password $pw -RunLevel Limited -Description "IAP sealed-data service for partition $x (tools/sealed_service.py serve)" | Out-Null
    $pw = $null
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep -Seconds 3
    $state = (Get-ScheduledTask -TaskName $taskName).State
    Write-Host "task:      '$taskName' registered (at start-up, run whether user is logged on or not); state $state"

    $ping = & $Python (Join-Path $Repo 'tools\sealed_service.py') ping --partition $x 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "pipe:      \\.\pipe\iap-sealed-$x answers: $ping (caller_account is the expected refusal for you)"
        $results[$x] = 'up'
    } else {
        Write-Host "pipe:      NO ANSWER on \\.\pipe\iap-sealed-$x ($ping); check Task Scheduler history for '$taskName'"
        $results[$x] = 'down'
    }
}

Write-Host ''
Write-Host 'Summary:'
foreach ($x in $Partitions) { Write-Host "  partition $x : $($results[$x])" }
Write-Host 'Next: install the runner (docs/governance/RUNNER_SETUP.md), then start the registered-run workflow in mode dd1'
Write-Host '(docs/governance/DD1_INGESTION_RUNBOOK.md). The services survive a reboot (at start-up trigger).'
