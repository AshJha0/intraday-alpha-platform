# IAP v2.0: back up the sealed partitions to an owner-chosen destination (owner, elevated PowerShell).
#
#   .\tools\admin\backup_sealed.ps1 -Destination E:\iap-backup [-Repo C:\iap\repo] [-Python C:\Python312\python.exe]
#                                   [-SealedRoot C:\iap-sealed] [-Partitions a,b,c,runner]
#
# Runbook: docs/GOVERNANCE.md section 6. Run it after each ingestion and each registered run, when no
# ingestion or run is in flight. The destination is an NTFS volume (exFAT and FAT32 drop the access lists), normally
# an external drive encrypted with BitLocker To Go and kept offline between backups.
#
# What it does, per partition x:
#   1. stops "IAP sealed service x" (so the audit chain does not move during the copy; the service re-announces any
#      pending checkpoint on restart, as after any stop);
#   2. runs `sealed_service.py backup-manifest` AS the partition's account (a transient scheduled task, as in
#      install_sealed_services.ps1): the account hashes its own files and records its audit chain head; the elevated
#      administrator never reads the partition;
#   3. copies the partition with robocopy /B /COPYALL: backup semantics (the administrator's backup and restore
#      privileges) copy bytes and access lists without opening the files through the access check and without the
#      script ever seeing their content; the copy's root carries the same access list as the partition (account and
#      SYSTEM only), so the backup is no more readable than the original;
#   4. runs `sealed_service.py verify-backup` AS the account against the copy: every sha256 and the chain head must
#      match the manifest just written;
#   5. restarts the service task.
# Finally it writes <Destination>\<stamp>\backup_manifest.json: the set's stamp and, per partition, the sha256 of the
# partition manifest, the file count and the chain head. Nothing is decrypted, and no file content is read into this
# console: the output is counts, hashes and robocopy's summary (its file list goes to a log next to the copy).

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Destination,
    [string]$Repo = 'C:\iap\repo',
    [string]$Python = 'C:\Python312\python.exe',
    [string]$SealedRoot = 'C:\iap-sealed',
    [string[]]$Partitions = @('a', 'b', 'c', 'runner')
)

$ErrorActionPreference = 'Stop'

function Assert-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'run from an elevated (Administrator) PowerShell'
    }
}

function Read-PlainPassword([string]$Prompt) {
    $secure = Read-Host -AsSecureString $Prompt
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Invoke-AsAccount([string]$TaskName, [string]$Account, [string]$Password, [string]$Arguments, [int]$TimeoutSec = 3600) {
    # Run one command as the account through a transient scheduled task and return its exit code.
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
    # The partition's access list (create_sealed_accounts.ps1): the account and SYSTEM, no inheritance, no Administrators.
    cmd /c "icacls `"$Dir`" /grant:r ${Account}:(OI)(CI)F SYSTEM:(OI)(CI)F Administrators:(OI)(CI)F >nul 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "ACL grant failed for $Dir" }
    cmd /c "icacls `"$Dir`" /inheritance:r >nul 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "inheritance removal failed for $Dir" }
    cmd /c "icacls `"$Dir`" /remove:g Administrators >nul 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "Administrators removal failed for $Dir" }
}

function Get-AccountFor([string]$x) { if ($x -eq 'runner') { 'iap-runner' } else { "iap-sealed-$x" } }

# -- checks before anything changes -------------------------------------------------------------------------------
Assert-Elevated
foreach ($x in $Partitions) { if ($x -notin @('a', 'b', 'c', 'runner')) { throw "unknown partition $x" } }
if (-not (Test-Path $Destination)) { throw "destination $Destination does not exist (plug in and unlock the backup drive)" }
$destFull = (Resolve-Path $Destination).Path
if ($destFull -like "$SealedRoot*") { throw "the destination may not be inside $SealedRoot" }
$fs = (Get-Volume -DriveLetter $destFull.Substring(0, 1)).FileSystem
if ($fs -ne 'NTFS') { throw "destination volume is $fs; access lists need NTFS (reformat the backup drive as NTFS)" }
foreach ($path in @($Python, (Join-Path $Repo 'tools\sealed_service.py'))) { if (-not (Test-Path $path)) { throw "missing: $path" } }
foreach ($x in $Partitions) {
    if (-not (Get-LocalUser -Name (Get-AccountFor $x) -ErrorAction SilentlyContinue)) { throw "account $(Get-AccountFor $x) does not exist" }
}
Write-Host 'Run this only when no ingestion and no registered run is in flight: the sealed services are stopped for the copy.'
if ((Read-Host 'Type BACKUP to continue') -ne 'BACKUP') { throw 'not confirmed' }

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$set = Join-Path $destFull "iap-sealed-$stamp"
$manifests = Join-Path $set 'manifests'
New-Item -ItemType Directory -Path $manifests | Out-Null
# The set: Administrators and SYSTEM only; each partition account may write its own manifest (granted below).
cmd /c "icacls `"$set`" /inheritance:r /grant:r Administrators:(OI)(CI)F SYSTEM:(OI)(CI)F >nul 2>&1"
if ($LASTEXITCODE -ne 0) { throw "ACL failed for $set" }

$entries = [ordered]@{}
$failed = @()
foreach ($x in $Partitions) {
    $account = Get-AccountFor $x
    $root = Join-Path $SealedRoot $x
    $copy = Join-Path $set $x
    $mfile = Join-Path $manifests "$x.json"
    Write-Host ''
    Write-Host "== partition $x ($account)"
    $pw = Read-PlainPassword "Password for $account"
    $task = "IAP sealed service $x"
    $hasTask = [bool](Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue)
    if ($hasTask) { Stop-ScheduledTask -TaskName $task; Write-Host "service:   '$task' stopped" }
    try {
        cmd /c "icacls `"$manifests`" /grant ${account}:(OI)(CI)M >nul 2>&1"
        cmd /c "icacls `"$set`" /grant ${account}:RX >nul 2>&1"
        $code = Invoke-AsAccount "IAP sealed backup manifest $x" $account $pw "tools\sealed_service.py backup-manifest --partition $x --root `"$root`" --out `"$mfile`""
        if ($code -ne 0) { throw "backup-manifest as $account failed (exit $code; 1 = the audit chain is broken)" }

        New-Item -ItemType Directory -Path $copy | Out-Null
        Set-OwnerOnlyAcl $copy $account
        $log = Join-Path $set "robocopy-$x.log"
        & robocopy $root $copy /E /B /COPYALL /DCOPY:DAT /XJ /R:1 /W:1 /NP /NFL /NDL /NJH /UNILOG:$log | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy failed for $x (exit $LASTEXITCODE; see $log)" }
        Write-Host "copy:      $copy (robocopy exit $LASTEXITCODE, log $log)"

        $code = Invoke-AsAccount "IAP sealed backup verify $x" $account $pw "tools\sealed_service.py verify-backup --partition $x --root `"$copy`" --manifest `"$mfile`""
        if ($code -ne 0) { throw "the copy of $x does not match its manifest (exit $code)" }
        Write-Host 'verify:    the copy matches (every sha256 and the chain head)'

        $doc = Get-Content -Raw $mfile | ConvertFrom-Json  # the manifest: names and hashes only
        $entries[$x] = [ordered]@{
            manifest_sha256 = (Get-FileHash -Algorithm SHA256 $mfile).Hash.ToLower()
            file_count      = $doc.file_count
            chain_head      = $doc.chain_head
        }
        Write-Host "record:    $($doc.file_count) files, chain head $($doc.chain_head)"
    } catch {
        Write-Host "FAILED:    $x : $($_.Exception.Message)"
        $failed += $x
    } finally {
        $pw = $null
        cmd /c "icacls `"$manifests`" /remove:g $account >nul 2>&1"
        cmd /c "icacls `"$set`" /remove:g $account >nul 2>&1"
        if ($hasTask) { Start-ScheduledTask -TaskName $task; Write-Host "service:   '$task' restarted" }
    }
}

$top = [ordered]@{ schema = 'iap.sealed_backup_set/v1'; stamp = $stamp; partitions = $entries; failed = $failed }
$top | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $set 'backup_manifest.json')
Write-Host ''
Write-Host "Backup set: $set"
if ($failed.Count -gt 0) { Write-Host "INCOMPLETE: $($failed -join ', ') failed; this set is not a full backup."; exit 1 }
Write-Host 'Complete. Eject the drive and keep it offline; record the stamp and chain heads in your log.'
