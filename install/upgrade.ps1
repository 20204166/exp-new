# Upgrade to a given wheel, or build the latest wheel then install it.
param([string]$Version, [string]$Python)
. (Join-Path $PSScriptRoot "_common.ps1")

$py = Resolve-Python $Python
Require-Python $py
Write-Host "Before:"; Show-InstalledVersion $py
if ($Version) {
    $wheel = Get-WheelPath $Version
} else {
    & (Join-Path $PSScriptRoot "build.ps1") -Python $Python
    $wheel = Get-WheelPath
}
Write-Host "Upgrading to: $(Split-Path $wheel -Leaf)"
Verify-ReleaseArtifacts $py $wheel
Remove-InstalledPackage $py
Install-ReleaseWheels $py $wheel
Verify-InstalledWheel $py (Get-WheelVersion $wheel)
Write-Host "After:"; Show-InstalledVersion $py
Write-Host "Rollback: rollback.ps1 -Version <previous>"
