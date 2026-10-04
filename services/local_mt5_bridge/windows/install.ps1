param(
    [Parameter(Mandatory = $true)]
    [string]$EnvFile,
    [string]$InstallRoot = "$env:LOCALAPPDATA\SuperSignalsBridge",
    [string]$TaskName = "SuperSignalsLocalBridge"
)

$ErrorActionPreference = "Stop"
$serviceRoot = Split-Path -Parent $PSScriptRoot
$packageSource = $serviceRoot
$requirements = Join-Path $serviceRoot "requirements.txt"
$runner = Join-Path $PSScriptRoot "run.ps1"

if (-not (Test-Path $EnvFile)) { throw "Environment file not found: $EnvFile" }
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python launcher not found. Install 64-bit Python 3.11 first."
}

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot "logs") | Out-Null
$packageDestination = Join-Path $InstallRoot "local_mt5_bridge"
New-Item -ItemType Directory -Force -Path $packageDestination | Out-Null
Copy-Item -Recurse -Force (Join-Path $packageSource "*") $packageDestination
Copy-Item -Force $runner (Join-Path $InstallRoot "run.ps1")
Copy-Item -Force $EnvFile (Join-Path $InstallRoot ".env")

$venv = Join-Path $InstallRoot ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    & py -3.11 -m venv $venv
}
$python = Join-Path $venv "Scripts\python.exe"
& $python -m pip install --upgrade pip
& $python -m pip install -r $requirements

$taskRunner = Join-Path $InstallRoot "run.ps1"
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$taskRunner`" -InstallRoot `"$InstallRoot`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 20 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Super Signals outbound local MT5 bridge (separate from GoldThinker)" `
    -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Super Signals bridge installed and started."
Write-Host "Task: $TaskName"
Write-Host "Log:  $(Join-Path $InstallRoot 'logs\bridge.log')"
