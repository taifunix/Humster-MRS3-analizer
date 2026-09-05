param(
    [Parameter(Mandatory = $false)]
    [string]$Config
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($Config)) {
    [Console]::Error.WriteLine("Usage: install_bybit_market_collector_task.ps1 -Config PATH")
    exit 2
}
$configPath = [System.IO.Path]::GetFullPath($Config)
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    [Console]::Error.WriteLine("Config file does not exist: $configPath")
    exit 2
}

$runner = Join-Path $PSScriptRoot "run_bybit_market_collector.ps1"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    [Console]::Error.WriteLine("Collector runner is missing: $runner")
    exit 2
}
$action = New-ScheduledTaskAction -Execute "PowerShell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Config `"$configPath`""
$trigger = New-ScheduledTaskTrigger -AtStartup -RandomDelay (New-TimeSpan -Seconds 30)
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances Ignore
Register-ScheduledTask -TaskName "MRS_BybitMarketCollector" -Action $action -Trigger $trigger -Settings $settings -User "SYSTEM" -RunLevel Highest -Force
