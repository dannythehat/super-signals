# Install a logon-based watchdog using the existing Administrator desktop session.
# Deliberately NO Windows password required or stored in this task.
# IMPORTANT: GUI MT5 needs an interactive session. A cold reboot with nobody
# logged in remains unverified; this task alone is NOT a 24/7 boot guarantee.
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\SuperSignalsBridge"
)
$ErrorActionPreference = 'Stop'
$watch = Join-Path $InstallRoot 'local_mt5_bridge\windows\pu-watchdog.ps1'
$run = Join-Path $InstallRoot 'local_mt5_bridge\windows\run-safe.ps1'
if (-not (Test-Path $watch) -or -not (Test-Path $run)) {
    throw 'Safe runner/watchdog not installed; aborting'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$user = $identity.Name
$taskName = 'SuperSignals-PU-Worker-Watchdog'
$cmd = "-NoProfile -ExecutionPolicy Bypass -File `"$watch`" -InstallRoot `"$InstallRoot`""
$action = New-ScheduledTaskAction -Execute "$PSHOME\powershell.exe" `
    -Argument $cmd -WorkingDirectory $InstallRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user `
    -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $action `
    -Principal $principal -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 3
$task = Get-ScheduledTask -TaskName $taskName
[pscustomobject]@{
    Name=$task.TaskName
    State=$task.State.ToString()
    LoggedInUser=$user
    RebootWithNoUserLoggedIn='NOT VALIDATED, INTERACTIVE MT5 REQUIRED'
} | Format-List
