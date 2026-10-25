# Short idempotent check for PU Prime MT5 and cloud bridge.
# Task Scheduler invokes at user logon, then every minute while signed in.
# No broker orders, passwords or automatic Administrator login.
param([string]$InstallRoot = "$env:LOCALAPPDATA\SuperSignalsBridge")
$ErrorActionPreference = 'Stop'
$logDir = Join-Path $InstallRoot 'logs'
$envFile = Join-Path $InstallRoot '.env'
$runner = Join-Path $InstallRoot 'local_mt5_bridge\windows\run-safe.ps1'
$watchLog = Join-Path $logDir 'pu-recovery.log'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
function Log([string]$message) {
    Add-Content -LiteralPath $watchLog -Value ("{0} {1}" -f [DateTime]::UtcNow.ToString('o'), $message)
}
try {
    if (-not [Environment]::UserInteractive) {
        Log 'Skipped: no interactive desktop'
        exit 2
    }
    if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw 'safe_runner_missing' }
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { throw 'bridge_configuration_missing' }
    $terminalEntry = [IO.File]::ReadAllLines($envFile) | Where-Object { $_.StartsWith('SUPER_SIGNALS_MT5_TERMINAL_PATH=') } | Select-Object -First 1
    if (-not $terminalEntry) { throw 'terminal_path_configuration_missing' }
    $terminal = $terminalEntry.Substring('SUPER_SIGNALS_MT5_TERMINAL_PATH='.Length).Trim().Trim('"')
    if (-not (Test-Path -LiteralPath $terminal -PathType Leaf)) { throw 'configured_pu_terminal_missing' }

    $terminalProcesses = @(Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" | Where-Object { $_.ExecutablePath -ieq $terminal })
    if ($terminalProcesses.Count -eq 0) {
        $proc = Start-Process -FilePath $terminal -WorkingDirectory (Split-Path -Parent $terminal) -PassThru
        Log "MT5 started PID=$($proc.Id)"
        Start-Sleep -Seconds 6
    }

    # Windows virtual-environment python wrapper and Python311 child are
    # one real worker. Never kill one just because two processes are visible.
    $workerProcesses = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^pythonw?\.exe$' -and
        $_.CommandLine -match 'local_mt5_bridge\.main'
    })
    if ($workerProcesses.Count -eq 0) {
        $args = '-NoProfile -ExecutionPolicy Bypass -File "' + $runner + '" -InstallRoot "' + $InstallRoot + '"'
        $p = Start-Process -FilePath "$PSHOME\powershell.exe" -ArgumentList $args -WorkingDirectory $InstallRoot -WindowStyle Minimized -PassThru
        Log "Worker launcher started PID=$($p.Id)"
    }
    exit 0
}
catch {
    # Exception type only, because .env contains secrets.
    Log ("Recovery failed: " + $_.Exception.GetType().Name)
    exit 1
}
