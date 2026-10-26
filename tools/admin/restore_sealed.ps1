# IAP v2.0: restore sealed partitions from a backup set made by backup_sealed.ps1 (owner, elevated PowerShell).
#
#   .\tools\admin\restore_sealed.ps1 -BackupSet E:\iap-backup\iap-sealed-20261026T120000Z [-Repo C:\iap\repo]
#                                    [-Python C:\Python312\python.exe] [-SealedRoot C:\iap-sealed] [-Partitions a,b,c,runner]
#
# Runbook and restore drill: docs/governance/SEALED_DATA.md section 6. For a drill, point -SealedRoot at an empty
# scratch directory (e.g. D:\iap-restore-drill); the live partitions are never a drill target.
#
# Refuses, before changing anything:
#   - a backup set without backup_manifest.json, a partition the set does not hold, or a set that recorded failures;
#   - a partition manifest whose sha256 differs from the one the set recorded (a swapped or edited manifest);
#   - a target that exists and is not empty, or that this shell cannot list (a live partition is unreadable to the
#     administrator by design, so it is refused too: move or remove it first, deliberately).
# Then, per partition x:
#   1. stops "IAP sealed service x" if it is registered;
#   2. creates the target with the partition access list (the account and SYSTEM only, no inheritance, no
#      Administrators: create_sealed_accounts.ps1) and copies the set's files with robocopy /B /COPYALL, which
#      restores every file's own access list, owner and audit entries;
#   3. runs `sealed_service.py verify-backup` AS the partition's account: its audit chain must be intact and its head
#      and every file's sha256 must equal the manifest; for a, b and c it also runs `sealed_service.py check`;
#   4. starts the service only when step 3 passed (the service appends a "start" line, so its head moves on after the
#      check, as it does at every start). A failed partition stays stopped; the script exits 1.
# Nothing is decrypted and no file content is read into this console.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackupSet,
    [string]$Repo = 'C:\iap\repo',
    [string]$Python = 'C:\Python312\python.exe',
    [string]$SealedRoot = 'C:\iap-sealed',
    [string[]]$Partitions = @('a', 'b', 'c', 'runner')
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) { Write-Host "REFUSED: $Message"; exit 2 }

function Get-AccountFor([string]$x) { if ($x -eq 'runner') { 'iap-runner' } else { "iap-sealed-$x" } }

function Test-EmptyTarget([string]$Dir) {
    if (-not (Test-Path -LiteralPath $Dir)) { return $true }
    try {
        $e = [System.IO.Directory]::EnumerateFileSystemEntries($Dir).GetEnumerator()
        return -not $e.MoveNext()
    } catch {
        return $false  # cannot list: treat as not empty
    }
}

# -- checks before anything changes (no elevation needed; nothing is written) --------------------------------------
$topPath = Join-Path $BackupSet 'backup_manifest.json'
if (-not (Test-Path -LiteralPath $topPath)) { Fail "no backup_manifest.json in $BackupSet" }
$top = Get-Content -Raw -LiteralPath $topPath | ConvertFrom-Json
if ($top.schema -ne 'iap.sealed_backup_set/v1') { Fail "unknown backup set schema $($top.schema)" }
if ($top.failed -and @($top.failed).Count -gt 0) { Fail "the set recorded failed partitions: $($top.failed -join ', ')" }
foreach ($x in $Partitions) {
    if ($x -notin @('a', 'b', 'c', 'runner')) { Fail "unknown partition $x" }
    $rec = $top.partitions.$x
    if (-not $rec) { Fail "the set holds no partition $x" }
    $mfile = Join-Path $BackupSet "manifests\$x.json"
    if (-not (Test-Path -LiteralPath $mfile)) { Fail "missing manifest for $x" }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $mfile).Hash.ToLower() -ne $rec.manifest_sha256) {
        Fail "manifest for $x does not match the sha256 the set recorded"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $BackupSet $x))) { Fail "the set has no copy of $x" }
    $target = Join-Path $SealedRoot $x
    if (-not (Test-EmptyTarget $target)) { Fail "target $target is not empty (or cannot be listed); restore only into an empty target" }
}

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail 'run from an elevated (Administrator) PowerShell'
}
foreach ($path in @($Python, (Join-Path $Repo 'tools\sealed_service.py'))) { if (-not (Test-Path $path)) { Fail "missing: $path" } }
foreach ($x in $Partitions) {
    if (-not (Get-LocalUser -Name (Get-AccountFor $x) -ErrorAction SilentlyContinue)) {
        Fail "account $(Get-AccountFor $x) does not exist: run create_sealed_accounts.ps1 first (new passwords are fine)"
    }
}
Write-Host "Restore set $($top.stamp) into $SealedRoot for: $($Partitions -join ', ')"
if ((Read-Host 'Type RESTORE to continue') -ne 'RESTORE') { Fail 'not confirmed' }

function Read-PlainPassword([string]$Prompt) {
    $secure = Read-Host -AsSecureString $Prompt
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Invoke-AsAccount([string]$TaskName, [string]$Account, [string]$Password, [string]$Arguments, [int]$TimeoutSec = 3600) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute $Python -Argument $Arguments -WorkingDirectory $Repo
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
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

function Set-OwnerOnlyAcl([string]$Dir, [string]$Account) {
    cmd /c "icacls `"$Dir`" /setowner $Account >nul 2>&1"
    cmd /c "icacls `"$Dir`" /grant:r ${Account}:(OI)(CI)F SYSTEM:(OI)(CI)F Administrators:(OI)(CI)F >nul 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "ACL grant failed for $Dir" }
    cmd /c "icacls `"$Dir`" /inheritance:r >nul 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "inheritance removal failed for $Dir" }
    cmd /c "icacls `"$Dir`" /remove:g Administrators >nul 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "Administrators removal failed for $Dir" }
}

$live = ([IO.Path]::GetFullPath($SealedRoot).TrimEnd('\') -eq 'C:\iap-sealed')
if (-not (Test-Path $SealedRoot)) { New-Item -ItemType Directory -Path $SealedRoot | Out-Null }
$results = [ordered]@{}
foreach ($x in $Partitions) {
    $account = Get-AccountFor $x
    $target = Join-Path $SealedRoot $x
    $task = "IAP sealed service $x"
    Write-Host ''
    Write-Host "== partition $x ($account -> $target)"
    # a drill (any other -SealedRoot) never stops or starts the live services
    $hasTask = $live -and [bool](Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue)
    if ($hasTask) { Stop-ScheduledTask -TaskName $task; Write-Host "service:   '$task' stopped" }
    $pw = Read-PlainPassword "Password for $account"
    # the manifest goes where the account can read it (the set itself is Administrators and SYSTEM only)
    $stage = Join-Path $env:ProgramData ("iap-restore-" + [guid]::NewGuid().ToString('N'))
    try {
        if (-not (Test-Path $target)) { New-Item -ItemType Directory -Path $target | Out-Null }
        Set-OwnerOnlyAcl $target $account
        $log = Join-Path $env:ProgramData "iap-restore-robocopy-$x.log"
        & robocopy (Join-Path $BackupSet $x) $target /E /B /COPYALL /DCOPY:DAT /XJ /R:1 /W:1 /NP /NFL /NDL /NJH /UNILOG:$log | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy failed (exit $LASTEXITCODE; see $log)" }
        Write-Host "copy:      restored (robocopy exit $LASTEXITCODE, log $log)"

        New-Item -ItemType Directory -Path $stage | Out-Null
        Copy-Item (Join-Path $BackupSet "manifests\$x.json") (Join-Path $stage "$x.json")
        cmd /c "icacls `"$stage`" /grant ${account}:(OI)(CI)R >nul 2>&1"
        $code = Invoke-AsAccount "IAP sealed restore verify $x" $account $pw "tools\sealed_service.py verify-backup --partition $x --root `"$target`" --manifest `"$(Join-Path $stage "$x.json")`""
        if ($code -ne 0) { throw "verify-backup as $account failed (exit $code): the chain head or a file hash differs from the manifest" }
        Write-Host "verify:    chain head $($top.partitions.$x.chain_head) and $($top.partitions.$x.file_count) file hashes match"
        if ($x -ne 'runner') {
            $code = Invoke-AsAccount "IAP sealed restore check $x" $account $pw "tools\sealed_service.py check --partition $x --root `"$target`""
            if ($code -ne 0) { throw "check as $account failed (exit $code): the restored service state is not usable" }
            Write-Host 'check:     service state readable and writable by the account'
        }
        if ($hasTask) { Start-ScheduledTask -TaskName $task; Write-Host "service:   '$task' started" }
        $results[$x] = 'restored'
    } catch {
        Write-Host "FAILED:    $x : $($_.Exception.Message) (service left stopped)"
        $results[$x] = 'FAILED'
    } finally {
        $pw = $null
        Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    }
}

Write-Host ''
Write-Host 'Summary:'
foreach ($x in $Partitions) { Write-Host "  partition $x : $($results[$x])" }
if ($results.Values -contains 'FAILED') { exit 1 }
