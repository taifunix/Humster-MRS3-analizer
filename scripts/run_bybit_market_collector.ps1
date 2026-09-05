param(
    [Parameter(Mandatory = $false)]
    [string]$Config
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($Config)) {
    [Console]::Error.WriteLine("Usage: run_bybit_market_collector.ps1 -Config PATH")
    exit 2
}
$configPath = [System.IO.Path]::GetFullPath($Config)
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    [Console]::Error.WriteLine("Config file does not exist: $configPath")
    exit 2
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project virtual environment is missing: $python"
}
& $python -m mrs3.bybit_collector.cli run --config $configPath
exit $LASTEXITCODE
