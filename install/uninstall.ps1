# Uninstall System Analyzer from the target environment.
param([string]$Python)
. (Join-Path $PSScriptRoot "_common.ps1")

$py = Resolve-Python $Python
Require-Python $py
Write-Host "Before:"; Show-InstalledVersion $py
& $py -m pip uninstall -y system-analyzer
if ($LASTEXITCODE -ne 0) { throw "pip uninstall failed (exit $LASTEXITCODE)" }
Write-Host "Uninstalled."