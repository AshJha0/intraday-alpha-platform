# Level A condition A9 (U0, plan section 9): the operator runbook docs/RUNBOOK_v2.md executed end to
# end by someone other than its author, with a dated record.  This script runs the runbook's checks
# that apply on the owner's Windows workstation, in the runbook's order, and writes the record.
#
#   powershell -ExecutionPolicy Bypass -File C:\iap\repo\tools\release\run_level_a_runbook.ps1 -Operator "Your Name"
#
# Run it from an ordinary (not elevated) PowerShell, in the read-only service checkout C:\iap\repo after
# `git -C C:\iap\repo pull --ff-only`.  It reads no sealed data: the isolation probe and the sealed-service
# pings run as you, the development identity, and must be REFUSED.  Nothing is committed or pushed.
param(
    [Parameter(Mandatory = $true)][string]$Operator,
    [string]$Repo = "C:\iap\repo",
    [string]$Python = "C:\Python312\python.exe"
)
$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = Join-Path $Repo "python\src"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
$commit = (git -C $Repo rev-parse HEAD).Trim()
$out = Join-Path ([Environment]::GetFolderPath("Desktop")) ("RUNBOOK_EXECUTION_" + (Get-Date -Format "yyyy-MM-dd") + ".md")
$rows = @()

function Step([string]$id, [string]$section, [string]$what, [scriptblock]$cmd, [scriptblock]$ok) {
    Write-Host "`n== $id  $what" -ForegroundColor Cyan
    $text = (& $cmd 2>&1 | Out-String)
    $code = $LASTEXITCODE
    $last = ($text.Trim() -split "`r?`n" | Where-Object { $_ } | Select-Object -Last 1)
    $pass = & $ok $code $text
    Write-Host $last
    Write-Host ($(if ($pass) { "PASS" } else { "FAIL" })) -ForegroundColor ($(if ($pass) { "Green" } else { "Red" }))
    $script:rows += "| $id | $section | $what | $(if ($pass) { 'pass' } else { 'FAIL' }) | ``$($last -replace '\|', '/')`` |"
}

Push-Location $Repo
Step "R1" "1" "documentation check (--strict)" { & $Python tests\harness\check_docs.py --strict } { param($c, $t) $c -eq 0 }
Step "R2" "1" "deployment and headline-number checks" { & $Python tests\harness\check_deployment.py; & $Python tests\harness\check_headline_numbers.py } { param($c, $t) $c -eq 0 -and $t -notmatch '\[FAIL\]' }
Push-Location python
Step "R3" "1" "architecture tests" { & $Python -m pytest -q -p no:cacheprovider tests\architecture } { param($c, $t) $c -eq 0 }
Step "R4" "1" "H1 equivalence, conservation, leakage, isolation tests" { & $Python -m pytest -q -p no:cacheprovider tests\equivalence tests\portfolio tests\leakage tests\isolation } { param($c, $t) $c -eq 0 }
Pop-Location
Step "R5" "8" "signatures and trust roots" { & $Python tools\verify_signatures.py --trust-ref origin/main --strict } { param($c, $t) $c -eq 0 }
Step "R6" "8" "data manifest" { & $Python tools\data_manifest.py check } { param($c, $t) $c -eq 0 }
Step "R7" "8" "release evidence manifest" { & $Python tools\release\evidence_manifest.py check --manifest docs\release\v2.0.0_evidence_manifest.json } { param($c, $t) $c -eq 0 }
Step "R8" "6" "isolation probe: the development identity is denied on every partition" { & $Python tools\admin\isolation_probe.py } { param($c, $t) $c -eq 0 }
foreach ($x in "a", "b", "c") {
    # up and refusing you: the service's own refusal (caller_account) or, for a non-elevated account
    # outside the pipe's DACL (D-101), access denied at the pipe; "no such pipe" means the service is down
    Step "R9$x" "6" "sealed service $x is up and refuses this account" { & $Python tools\sealed_service.py ping --partition $x } { param($c, $t) ($t -match 'caller_account|Errno 13|denied') -and ($t -notmatch 'cannot find|Errno 2\b') }
}
Pop-Location

$failed = @($rows | Where-Object { $_ -match '\| FAIL \|' }).Count
$body = @(
    "# Runbook execution record (Level A condition A9)",
    "",
    "- operator: $Operator (not the runbook's author)",
    "- date: $stamp (local time)",
    "- repository commit: ``$commit``",
    "- runbook: docs/RUNBOOK_v2.md, the checks that apply on the owner's workstation (sections 1, 6, 8)",
    "- result: $(if ($failed -eq 0) { 'all checks passed' } else { "$failed check(s) FAILED" })",
    "",
    "| id | runbook section | check | result | last line of output |",
    "|---|---|---|---|---|"
) + $rows + @(
    "",
    "Not run here, with the reason: section 5 (a registered run needs a signed prereg; none exists in v2.0),",
    "section 7 (``reproduce.sh`` needs bash and Docker; CI's ``repro-image`` job reproduces in the pinned image)."
)
$body | Out-File $out -Encoding utf8
Write-Host "`nRecord written to $out" -ForegroundColor Cyan
Write-Host "Send Claude the file (or paste it). $(if ($failed -eq 0) { 'All checks passed.' } else { "$failed check(s) failed." })"
