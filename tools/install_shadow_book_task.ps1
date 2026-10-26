# Installs the Track C shadow book as a Windows scheduled task (docs/RUNBOOKS.md).
# The OWNER runs this once, after registration (configs/paper/shadow_book.json filled in). Nothing else
# installs it. Runs tools\run_shadow_book.cmd Monday-Friday at 17:30 local time (after the US close
# for a London clock; pass -At for another zone). -WhatIf prints the task without registering it.
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "IAP-TrackC-ShadowBook",
    [string]$At = "17:30",
    [string]$Out = "",
    [string]$Inbox = ""
)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$cmd = Join-Path $repo "tools\run_shadow_book.cmd"
if (-not (Test-Path $cmd)) { throw "missing $cmd" }
if ($Inbox -and -not $Out) { $Out = Join-Path $repo "data\shadow_book" }
$taskArgs = ""
if ($Out) { $taskArgs = "`"$Out`"" }
if ($Inbox) { $taskArgs = "$taskArgs `"$Inbox`"" }
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$cmd`" $taskArgs" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled task running $cmd $taskArgs at $At Mon-Fri")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description "IAP Track C forward shadow book: paper only, counts-only output" | Out-Null
    Write-Output "registered $TaskName ($At Mon-Fri): $cmd $taskArgs"
}
