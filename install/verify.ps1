# Verify a built wheel before installing it: no forbidden content, expected
# members, entry points present, and (when present) the SHA256SUMS entry.
param([string]$Version, [string]$Python)
. (Join-Path $PSScriptRoot "_common.ps1")

$py = Resolve-Python $Python
Require-Python $py
$wheel = Get-WheelPath $Version
Write-Host "Verifying: $wheel"
Verify-ReleaseArtifacts $py $wheel
