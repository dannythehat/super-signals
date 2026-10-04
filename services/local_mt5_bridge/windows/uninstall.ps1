param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\SuperSignalsBridge",
    [string]$TaskName = "SuperSignalsLocalBridge",
    [switch]$RemoveLocalFiles
)

$ErrorActionPreference = "Stop"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
if ($RemoveLocalFiles -and (Test-Path $InstallRoot)) {
    Remove-Item -Recurse -Force $InstallRoot
}
Write-Host "Super Signals bridge task removed."
