# Safe foreground PU Prime bridge runner.
# Uses the user's existing interactive MT5 desktop and process-local credentials.
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\SuperSignalsBridge"
)
$ErrorActionPreference = 'Stop'
$envFile = Join-Path $InstallRoot '.env'
$python = Join-Path $InstallRoot '.venv\Scripts\python.exe'
$logs = Join-Path $InstallRoot 'logs'
if (-not (Test-Path -LiteralPath $envFile)) { throw 'PU bridge configuration absent' }
if (-not (Test-Path -LiteralPath $python)) { throw 'PU bridge Python missing' }
New-Item -ItemType Directory -Path $logs -Force | Out-Null

# No raw env output: bridge token must remain private.
foreach ($line in [IO.File]::ReadAllLines($envFile)) {
    $entry = $line.Trim()
    if (!$entry -or $entry.StartsWith('#')) { continue }
    $parts = $entry.Split('=', 2)
    if ($parts.Count -eq 2) {
        [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1], 'Process')
    }
}
$env:PYTHONPATH = $InstallRoot
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdout = Join-Path $logs "pu-worker-$stamp.stdout.log"
$stderr = Join-Path $logs "pu-worker-$stamp.stderr.log"
# PowerShell 5.1 incorrectly treats Python's normal logging on stderr
# as a NativeCommandError under $ErrorActionPreference='Stop'.
# Native redirection avoids this and preserves process exit status.
$child = Start-Process -FilePath $python -ArgumentList '-m local_mt5_bridge.main' `
    -WorkingDirectory $InstallRoot -Wait -PassThru `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr
exit $child.ExitCode
