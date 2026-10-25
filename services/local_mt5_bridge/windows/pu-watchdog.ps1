# Keep exactly one PU Prime bridge active in the existing interactive Windows session.
# This cannot restart a GUI MT5 terminal after a reboot with NO user logged on.
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\SuperSignalsBridge",
    [int]$CheckSeconds = 20
)
$ErrorActionPreference = 'Continue'
$run = Join-Path $InstallRoot 'local_mt5_bridge\windows\run-safe.ps1'
$logs = Join-Path $InstallRoot 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$watchLog = Join-Path $logs 'pu-watchdog.log'
function Record([string]$message) {
    Add-Content -Path $watchLog -Value ("{0} {1}" -f (Get-Date).ToUniversalTime().ToString('o'),$message)
}
if (-not (Test-Path $run)) { throw "Missing safe bridge runner $run" }
Record 'Interactive-session watchdog started'
while ($true) {
    try {
        # Python venv on Windows normally reports TWO processes: python.exe
        # wrapper and its child interpreter. Any presence means do NOT start
        # another worker, because broker trade processing must be single-writer.
        $workers = @(Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $_.Name -match '^pythonw?\.exe$' -and
                $_.CommandLine -match 'local_mt5_bridge.main'
            })
        if ($workers.Count -eq 0) {
            $startup = "-NoProfile -ExecutionPolicy Bypass -File `"$run`" -InstallRoot `"$InstallRoot`""
            $child = Start-Process -FilePath "$PSHOME\powershell.exe" `
                -ArgumentList $startup -WorkingDirectory $InstallRoot `
                -WindowStyle Minimized -PassThru -ErrorAction Stop
            Record "Worker launcher started PID=$($child.Id)"
        }
    } catch {
        # Log only exception type to avoid accidentally persisting secrets.
        Record ("Worker watchdog error: " + $_.Exception.GetType().Name)
    }
    Start-Sleep -Seconds ([Math]::Max(10,$CheckSeconds))
}
