param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\SuperSignalsBridge"
)

$ErrorActionPreference = "Stop"
$envFile = Join-Path $InstallRoot ".env"
$python = Join-Path $InstallRoot ".venv\Scripts\python.exe"
$logDir = Join-Path $InstallRoot "logs"
$logFile = Join-Path $logDir "bridge.log"

if (-not (Test-Path $envFile)) { throw "Missing $envFile" }
if (-not (Test-Path $python)) { throw "Missing bridge Python environment" }
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    $parts = $line.Split("=", 2)
    if ($parts.Count -eq 2) {
        [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1], "Process")
    }
}

$env:PYTHONPATH = $InstallRoot
& $python -m local_mt5_bridge.main *>> $logFile
exit $LASTEXITCODE
