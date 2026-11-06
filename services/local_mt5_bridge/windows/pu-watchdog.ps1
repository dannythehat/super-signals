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
function Get-ConfiguredWorkers($Health, $Processes, [string]$Profile, [string]$Account, [string]$WorkerId, [string]$TerminalPath) {
    @($Processes | Where-Object {
        $Health -and $_.ProcessId -eq $Health.pid -and
        $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'local_mt5_bridge.main' -and
        $Health.profile -ceq $Profile -and $Health.account_ref -ceq $Account -and
        $Health.worker_id -ceq $WorkerId -and $Health.terminal_path -ieq $TerminalPath
    })
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
        # Match this installation/profile/account. A worker for another terminal
        # must never suppress recovery, and a stalled worker is reported, not killed.
        $settings = @{}
        foreach ($line in [IO.File]::ReadAllLines($envPath)) {
            $parts = $line.Split('=',2)
            if ($parts.Count -eq 2) {$settings[$parts[0].Trim()] = $parts[1].Trim()}
        }
        $profile = $settings['SUPER_SIGNALS_LOCAL_BRIDGE_PROFILE']
        if (-not $profile) {$profile = 'super-signals'}
        $account = $settings['SUPER_SIGNALS_LOCAL_BRIDGE_ACCOUNT_ID']
        $workerId = $settings['SUPER_SIGNALS_LOCAL_BRIDGE_WORKER_ID']
        if (-not $workerId) {$workerId = 'super-signals-' + $env:COMPUTERNAME.ToLower()}
        $journal = $settings['SUPER_SIGNALS_LOCAL_BRIDGE_JOURNAL']
        if (-not $journal) {$journal = Join-Path $env:PROGRAMDATA 'SuperSignals\bridge\command-journal.sqlite3'}
        $hash = [Security.Cryptography.SHA256]::Create()
        try {$digest = ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes("$profile|$account|$workerId")))).Replace('-','').ToLower().Substring(0,12)}
        finally {$hash.Dispose()}
        $healthFile = Join-Path (Split-Path -Parent $journal) "worker-$digest.health.json"
        $health = $null
        try {$health = Get-Content -Raw -LiteralPath $healthFile -ErrorAction Stop | ConvertFrom-Json} catch {}
        $allWorkers = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'local_mt5_bridge.main'
        })
        $workers = @(Get-ConfiguredWorkers $health $allWorkers $profile $account $workerId $terminalPath)
        if ($workers.Count -gt 0) {
            $progressAge = $null
            if ($health.last_progress_utc) {$progressAge = ([DateTime]::UtcNow - [DateTime]::Parse($health.last_progress_utc).ToUniversalTime()).TotalSeconds}
            if ($null -eq $progressAge -or $progressAge -gt 90 -or $health.state -ne 'ready') {
                Record ("Configured worker degraded state=" + $health.state + '; no process stopped')
            }
        }
        # During a slow startup, telemetry might not exist yet. An interpreter
        # launched by THIS venv is sufficient to suppress duplicate startup only.
        $starting = @($allWorkers | Where-Object {$_.CommandLine -like "*$InstallRoot*"})
        if ($workers.Count -eq 0 -and $starting.Count -gt 0) {
            Record 'Configured installation has an unverified worker; recovery held, no duplicate started'
            Start-Sleep -Seconds ([Math]::Max(10,$CheckSeconds))
            continue
        }
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
