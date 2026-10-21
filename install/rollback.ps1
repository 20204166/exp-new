# Roll back to a previous wheel kept in dist/. Usage: rollback.ps1 -Version <v>
param([string]$Version, [string]$Python)
. (Join-Path $PSScriptRoot "_common.ps1")

if (-not $Version) { throw "usage: rollback.ps1 -Version <version>" }
$py = Resolve-Python $Python
Require-Python $py
$wheel = Get-WheelPath $Version
Write-Host "Rolling back to: $(Split-Path $wheel -Leaf)"
Verify-ReleaseArtifacts $py $wheel
Install-ReleaseWheels $py $wheel
Write-Host "Restored:"; Show-InstalledVersion $py
