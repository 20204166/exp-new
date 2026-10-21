# Install System Analyzer into the CURRENT USER's Python WITHOUT a virtual
# environment, so `system-analyzer` runs directly from any directory.
#
#   install-user.ps1               # per-user install (no venv)
#   install-user.ps1 -Version 1.2.2.0   # install a specific wheel from dist/
#   install-user.ps1 -Python C:\Python312\python.exe   # pin the interpreter
#
# Auto-finds a Python 3.10+ interpreter (py launcher, python3.x, python.org
# paths) and prints the exact PATH step for the scripts directory.
param([string]$Version, [string]$Python)
. (Join-Path $PSScriptRoot "_common.ps1")

if ($Python) { $py = @($Python) } else {
    $py = Get-SystemPythonGe310
    if (-not $py) {
        throw "no system Python 3.10+ interpreter found outside a virtual environment. Deactivate the venv or pass -Python <path>."
    }
}
Require-Python $py
if (Test-VenvPython $py) {
    throw "'$($py -join ' ')' is inside a virtual environment; a per-user install is impossible (pip disables '--user' inside venvs). Deactivate the venv or pass -Python <path>."
}

$wheel = Get-WheelPath $Version
Write-Host "System interpreter: $($py -join ' ') (Python $(Get-PythonVersion $py))"
Write-Host "Verifying wheel: $(Split-Path $wheel -Leaf)"
Verify-ReleaseArtifacts $py $wheel
Invoke-Versioned $py "maintenance._release" @("verify-wheel", $wheel)

Write-Host "Installing into the user environment (no venv)..."
Remove-InstalledPackage $py
$dist = Join-Path (Get-PackageDir) "dist"
Install-UserWheels $py @("--find-links", $dist, $wheel, "expra-connect==2.2.1.1")
Verify-InstalledWheel $py (Get-WheelVersion $wheel)

$binDir = & $py -c "import os,sysconfig;print(sysconfig.get_path('scripts', scheme='nt_user' if os.name=='nt' else 'posix_user'))" 2>$null
if (-not $binDir) { $binDir = if ($env:OS -eq "Windows_NT") { Join-Path $env:APPDATA "Python\Scripts" } else { Join-Path $HOME ".local\bin" } }
Write-Host "Installed. Console scripts are in: $binDir"
$launcher = Join-Path $binDir "system-analyzer.exe"
if (-not (Test-Path $launcher)) { $launcher = Join-Path $binDir "system-analyzer" }
if (-not (Test-Path $launcher)) { throw "Install completed but launcher was not found in $binDir" }

$sep = if ($env:OS -eq "Windows_NT") { ";" } else { ":" }
$already = ($env:PATH -split [regex]::Escape($sep)) | Where-Object { $_ -eq $binDir }
if ($already) {
    Write-Host "Launcher found on PATH: run 'system-analyzer'"
} else {
    Write-Host "Add it to PATH once:"
    if ($env:OS -eq "Windows_NT") {
        Write-Host "  setx PATH `"$binDir;%PATH%`""
    } else {
        Write-Host "  echo 'export PATH=\"$binDir:`$PATH\"' >> `$HOME/.zshrc   # (or ~/.bashrc for bash)"
    }
    Write-Host "Then open a new terminal and run: system-analyzer"
}
Write-Host "Direct launcher path: $launcher"
Write-Host "Verify: system-analyzer-snapshot --help"
