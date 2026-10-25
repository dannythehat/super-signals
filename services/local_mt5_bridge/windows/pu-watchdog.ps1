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
        # Start the existing approved PU Prime MT5 installation in the signed-in
        # desktop session. Never launch a different terminal or log in to a
        # different broker account: main.py checks exact login and server.
        $envPath = Join-Path $InstallRoot '.env'
        if (-not (Test-Path -LiteralPath $envPath)) {
            Record 'Bridge .env missing, trading worker not started'
            Start-Sleep -Seconds ([Math]::Max(10,$CheckSeconds))
            continue
        }
        $terminalLine = [IO.File]::ReadAllLines($envPath) |
            Where-Object { $_.StartsWith('SUPER_SIGNALS_MT5_TERMINAL_PATH=') } |
            Select-Object -First 1
        if (-not $terminalLine) {
            Record 'Terminal path missing from configuration, trading worker not started'
            Start-Sleep -Seconds ([Math]::Max(10,$CheckSeconds))
            continue
        }
        $terminalPath = $terminalLine.Substring('SUPER_SIGNALS_MT5_TERMINAL_PATH='.Length).Trim()
        if (-not (Test-Path -LiteralPath $terminalPath -PathType Leaf)) {
            Record 'Configured MT5 binary missing, trading worker not started'
            Start-Sleep -Seconds ([Math]::Max(10,$CheckSeconds))
            continue
        }
        $terminalPresent = @(Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction Stop |
            Where-Object { $_.ExecutablePath -ieq $terminalPath }).Count -gt 0
        if (-not $terminalPresent) {
            $terminal = Start-Process -FilePath $terminalPath `
                -WorkingDirectory (Split-Path -Parent $terminalPath) `
                -PassThru -ErrorAction Stop
            Record "Configured PU Prime MT5 terminal launched PID=$($terminal.Id)"
            Start-Sleep -Seconds 5
        }
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
