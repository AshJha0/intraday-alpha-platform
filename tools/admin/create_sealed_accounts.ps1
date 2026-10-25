# IAP v2.0, U0 section 2: create the sealed-data identities and partitions on this workstation.
# Run from an elevated (Administrator) PowerShell. Safe to re-run: existing accounts are kept, existing directories are
# re-secured. Creates four standard (non-administrator) local users and four directories each readable and writable by
# exactly one of them. Nothing is downloaded, nothing is read.
# Revision 5: the final display expects denial for the administrator (correct end state). Revision 4: explicit Administrators entry kept until the last step (a fresh directory lost it on inheritance removal).
# Revision 3: no recursive flag (directories are empty; new files inherit) and no stderr capture, which made an
# empty-directory notice fatal. Revision 2: descriptions shortened to Windows' 48-character limit; ownership is set BEFORE the administrator's own
# access is removed; a directory the administrator can no longer open is first reclaimed with takeown.

$ErrorActionPreference = 'Stop'
$root = 'C:\iap-sealed'
$accounts = @(
    @{ Name = 'iap-sealed-a'; Dir = "$root\a";      Desc = 'IAP sealed partition A (Track A, H1 window)' },
    @{ Name = 'iap-sealed-b'; Dir = "$root\b";      Desc = 'IAP sealed partition B (Track B, ML test)' },
    @{ Name = 'iap-sealed-c'; Dir = "$root\c";      Desc = 'IAP sealed partition C (Track C, forward)' },
    @{ Name = 'iap-runner';   Dir = "$root\runner"; Desc = 'IAP registered-run executor identity' }
)
foreach ($a in $accounts) { if ($a.Desc.Length -gt 48) { throw "description too long: $($a.Name)" } }

if (-not (Test-Path $root)) { New-Item -ItemType Directory -Path $root | Out-Null }

foreach ($a in $accounts) {
    if (Get-LocalUser -Name $a.Name -ErrorAction SilentlyContinue) {
        Write-Host "exists:    $($a.Name)"
    } else {
        $pw = Read-Host -AsSecureString "Password for $($a.Name) ($($a.Desc))"
        New-LocalUser -Name $a.Name -Password $pw -FullName $a.Name -Description $a.Desc `
            -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
        Add-LocalGroupMember -Group 'Users' -Member $a.Name
        Write-Host "created:   $($a.Name)"
    }

    if (-not (Test-Path $a.Dir)) {
        New-Item -ItemType Directory -Path $a.Dir | Out-Null
    } else {
        # A previous run may have removed the administrator's access; reclaim it so the ACL can be rewritten.
        cmd /c "takeown /f `"$($a.Dir)`" /a >nul 2>&1"
        cmd /c "icacls `"$($a.Dir)`" /grant:r Administrators:(OI)(CI)F >nul 2>&1"
    }

    # 1. Ownership first, while the administrator still has access. Needs SeRestorePrivilege, which icacls enables for
    #    an elevated administrator; if Windows still refuses, ownership stays with Administrators, which is acceptable:
    #    isolation rests on the ACL below, and an administrator can take ownership in any case (the stated limit).
    cmd /c "icacls `"$($a.Dir)`" /setowner $($a.Name) >nul 2>&1"
    if ($LASTEXITCODE -ne 0) { Write-Host "owner:     left as Administrators for $($a.Dir)" }
    else { Write-Host "owner:     $($a.Name)" }

    # 2. Access list: give the account, SYSTEM and (temporarily) Administrators explicit entries, drop inheritance, then
    #    remove Administrators last, so the administrator never loses access before the final step. End state: the
    #    account and SYSTEM only (no Administrators, so an accidental elevated research process does not read it).
    cmd /c "icacls `"$($a.Dir)`" /grant:r $($a.Name):(OI)(CI)F SYSTEM:(OI)(CI)F Administrators:(OI)(CI)F >nul 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "ACL grant failed for $($a.Dir)" }
    cmd /c "icacls `"$($a.Dir)`" /inheritance:r >nul 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "inheritance removal failed for $($a.Dir)" }
    cmd /c "icacls `"$($a.Dir)`" /remove:g Administrators >nul 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "Administrators removal failed for $($a.Dir)" }
    Write-Host "partition: $($a.Dir) -> $($a.Name) and SYSTEM only"
}

Write-Host ''
Write-Host 'End-state check: the elevated administrator must now be DENIED on every partition (that is the proof that'
Write-Host 'BUILTIN\Administrators is gone; the access list is readable only by the owning account and SYSTEM):'
foreach ($a in $accounts) { cmd /c "icacls `"$($a.Dir)`" 2>&1 | findstr /c:denied /c:SYSTEM /c:$($a.Name)" }
Write-Host ''
Write-Host 'Verification: from your normal (non-administrator) PowerShell, each Get-ChildItem below must be denied:'
foreach ($a in $accounts) { Write-Host "  Get-ChildItem $($a.Dir)" }
