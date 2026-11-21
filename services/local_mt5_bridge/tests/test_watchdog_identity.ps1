$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot '../windows/pu-watchdog.ps1'
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $scriptPath),[ref]$tokens,[ref]$errors)
if ($errors.Count) {throw ($errors | Out-String)}
# Load ONLY the pure identity function, never the watchdog/start/recovery loop.
$fn=$ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-ConfiguredWorkers'},$true)
Invoke-Expression $fn.Extent.Text
$h=[pscustomobject]@{pid=10;profile='pu-prime';account_ref='demo-a';worker_id='worker-a';terminal_path='fixture-terminal'}
$p=[pscustomobject]@{ProcessId=10;Name='python.exe';CommandLine='python -m local_mt5_bridge.main'}
if (@(Get-ConfiguredWorkers $h @($p) 'pu-prime' 'demo-a' 'worker-a' 'fixture-terminal').Count -ne 1) {throw 'Exact identity not recognised'}
foreach ($dimension in @('profile','account_ref','worker_id','terminal_path','pid')) {
    $copy=$h | ConvertTo-Json | ConvertFrom-Json
    if ($dimension -eq 'pid') {$copy.pid=11} else {$copy.$dimension='another-worker'}
    if (@(Get-ConfiguredWorkers $copy @($p) 'pu-prime' 'demo-a' 'worker-a' 'fixture-terminal').Count -ne 0) {throw "Wrong $dimension accepted"}
}
$p.CommandLine='python unrelated.py'
if (@(Get-ConfiguredWorkers $h @($p) 'pu-prime' 'demo-a' 'worker-a' 'fixture-terminal').Count) {throw 'Reused unrelated PID accepted'}
'PASS: exact identity, other profiles/accounts, PID reuse; no watchdog executed'
