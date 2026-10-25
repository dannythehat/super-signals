# Register one-shot health check every 60 seconds instead of running an endless
# watchdog inside Task Scheduler. Requires elevated PowerShell in MT5 desktop.
param([string]$InstallRoot = "$env:LOCALAPPDATA\SuperSignalsBridge")
$ErrorActionPreference = 'Stop'
$check = Join-Path $InstallRoot 'local_mt5_bridge\windows\pu-recovery-once.ps1'
$safe = Join-Path $InstallRoot 'local_mt5_bridge\windows\run-safe.ps1'
if (-not (Test-Path -LiteralPath $check -PathType Leaf)) { throw 'missing_recovery_check' }
if (-not (Test-Path -LiteralPath $safe -PathType Leaf)) { throw 'missing_safe_runner' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalObject = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principalObject.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'elevated_powershell_required'
}
$user = $identity.Name
$taskName = 'SuperSignals-PU-Worker-Watchdog'
$arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $check + '" -InstallRoot "' + $InstallRoot + '"'
$action = New-ScheduledTaskAction -Execute "$PSHOME\powershell.exe" -Argument $arguments -WorkingDirectory $InstallRoot
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
# A periodic short-lived check returns normally (task state Ready is expected).
# Runs only in the active interactive user's session.
$periodic = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($logon, $periodic) -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 3
$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
[pscustomobject]@{
    Name=$taskName
    State=$task.State.ToString()
    LastTaskResult=$info.LastTaskResult
    NextRunTime=$info.NextRunTime
    User=$user
    Recovery='Interactive logon and every 60 seconds'
    ColdBootWithoutLogin='NOT VERIFIED'
} | Format-List
