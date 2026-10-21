# Shared helpers for the System Analyzer install/release scripts (PowerShell:
# native Windows, also works on macOS/Linux under pwsh).
#
# Safety contract (applies to every script that dot-sources this one):
#   * no Administrator requirement; no permanent execution-policy change;
#   * explicit Python target (repo .venv first, then an auto-found 3.10+
#     interpreter; override via -Python);
#   * clear version display; idempotent where practical.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-PackageDir {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Test-IsWindows {
    if (Get-Variable IsWindows -ErrorAction SilentlyContinue) {
        return [bool]$IsWindows
    }
    return $env:OS -eq "Windows_NT"
}

function Get-RepoVenvPython {
    $repo = Get-PackageDir
    if (Test-IsWindows) {
        foreach ($path in @(
            (Join-Path $repo ".venv\Scripts\python.exe"),
            (Join-Path $repo ".venv\Scripts\python"),
            (Join-Path $repo ".venv\bin\python.exe")
        )) {
            if (Test-Path $path) { return @($path) }
        }
    } else {
        foreach ($path in @(
            (Join-Path $repo ".venv/bin/python"),
            (Join-Path $repo ".venv/bin/python3")
        )) {
            if (Test-Path $path) { return @($path) }
        }
    }
    return $null
}

# Ordered candidate interpreters: the Windows `py` launcher first (modern
# reliable way to pick a specific Python on Windows), then named python3.x
# commands, then common python.org install locations, then bare python.
function Get-PythonCandidates {
    $candidates = [System.Collections.Generic.List[object]]::new()
    foreach ($m in @("3.14", "3.13", "3.12", "3.11", "3.10")) {
        if (Get-Command "py" -ErrorAction SilentlyContinue) {
            $candidates.Add(@("py", "-$m"))
        }
    }
    if (Get-Command "py" -ErrorAction SilentlyContinue) {
        $candidates.Add(@("py", "-3"))
    }
    foreach ($name in @("python3.14", "python3.13", "python3.12", "python3.11", "python3.10", "python3", "python")) {
        if (Get-Command $name -ErrorAction SilentlyContinue) { $candidates.Add(@($name)) }
    }
    foreach ($path in @(
        "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
    )) {
        if (Test-Path $path) { $candidates.Add(@($path)) }
    }
    return $candidates
}

function Get-PythonVersion {
    param([object]$Py)
    $out = & $Py -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    return ($out | Select-Object -First 1)
}

# System Analyzer uses PEP 604 union types (e.g. `threading.Event | None`) so
# it requires Python 3.10+. Fail fast with a clear message instead of an
# import-time TypeError on older interpreters (e.g. macOS 3.9 / older Windows).
function Test-PythonGe310 {
    param([object]$Py)
    $v = Get-PythonVersion $Py
    if (-not $v) { return $false }
    $parts = $v.Split(".")
    $major = [int]$parts[0]; $minor = [int]$parts[1]
    return ($major -gt 3 -or ($major -eq 3 -and $minor -ge 10))
}

function Get-PythonGe310 {
    foreach ($candidate in Get-PythonCandidates) {
        if (Test-PythonGe310 $candidate) { return $candidate }
    }
    return $null
}

function Test-VenvPython {
    param([object]$Py)
    $script = "import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)"
    & $Py -c $script 2>$null
    return ($LASTEXITCODE -eq 0)
}

# Return the first 3.10+ interpreter that is NOT inside a virtual environment.
# Used by install-user.ps1 so a per-user install always lands in a real
# interpreter regardless of whether a venv (e.g. the repo .venv) is active;
# pip refuses --user inside venvs ("User site-packages are not visible in
# this virtualenv").
function Get-SystemPythonGe310 {
    foreach ($candidate in Get-PythonCandidates) {
        if ((Test-PythonGe310 $candidate) -and -not (Test-VenvPython $candidate)) {
            return $candidate
        }
    }
    return $null
}

function Require-Python {
    param([object]$Py)
    if (-not (Test-PythonGe310 $Py)) {
        $v = Get-PythonVersion $Py
        throw "System Analyzer requires Python 3.10 or newer; this interpreter is $v ($Py). Install a newer Python (python.org/Homebrew) or use a 3.10+ virtual environment, then retry."
    }
}

function Resolve-Python {
    param([string]$Python)
    if ($Python) { return @($Python) }
    $repoPy = Get-RepoVenvPython
    if ($repoPy) { return $repoPy }
    $found = Get-PythonGe310
    if ($found) { return $found }
    return @("python")
}

function Get-WheelPath {
    param([string]$Version)
    $dist = Join-Path (Get-PackageDir) "dist"
    if ($Version) {
        $w = Join-Path $dist "system_analyzer-$Version-py3-none-any.whl"
        if (-not (Test-Path $w)) { throw "wheel not found for version ${Version}: $w" }
        return $w
    }
    $wheels = @(Get-ChildItem (Join-Path $dist "system_analyzer-*.whl") -ErrorAction SilentlyContinue)
    if ($wheels.Count -eq 0) { throw "no wheel in $dist -- run build.ps1 first" }
    $sorted = $wheels | Sort-Object {
        if ($_.BaseName -match "system_analyzer-(\d+)\.(\d+)\.(\d+)\.(\d+)") {
            [version]"$($matches[1]).$($matches[2]).$($matches[3]).$($matches[4])"
        } else { [version]"0.0.0.0" }
    }
    return $sorted[-1].FullName
}

function Show-InstalledVersion {
    param([object]$Py)
    & $Py -m pip show system-analyzer 2>$null | Select-String "^Version:"
}

function Remove-InstalledPackage {
    param([object]$Py)
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        & $Py -m pip show system-analyzer 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { return }
        Write-Host "Removing existing system-analyzer distribution (pass $attempt)..."
        & $Py -m pip uninstall -y system-analyzer
        if ($LASTEXITCODE -ne 0) { throw "pip uninstall failed (exit $LASTEXITCODE)" }
    }
    & $Py -m pip show system-analyzer 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { throw "could not remove every system-analyzer distribution" }
}

function Get-WheelVersion {
    param([string]$Wheel)
    $name = Split-Path $Wheel -Leaf
    if ($name -notmatch "^system_analyzer-(\d+\.\d+\.\d+\.\d+)-py3-none-any\.whl$") {
        throw "unexpected wheel filename: $name"
    }
    return $matches[1]
}

function Get-CompanionWheelPath {
    return (Join-Path (Get-PackageDir) "dist\expra_connect-2.2.1.1-py3-none-any.whl")
}

function Verify-ReleaseArtifacts {
    param([object]$Py, [string]$AppWheel)
    $dist = Join-Path (Get-PackageDir) "dist"
    Invoke-Versioned $Py "maintenance._release" @(
        "verify-release-artifacts", $AppWheel, "--dist-dir", $dist
    )
}

function Install-ReleaseWheels {
    param([object]$Py, [string]$AppWheel)
    $dist = Join-Path (Get-PackageDir) "dist"
    & $Py -m pip install --find-links $dist $AppWheel "expra-connect==2.2.1.1"
    if ($LASTEXITCODE -ne 0) { throw "release wheel install failed (exit $LASTEXITCODE)" }
}

function Verify-InstalledWheel {
    param([object]$Py, [string]$ExpectedVersion)
    $script = @'
import importlib.metadata as metadata
import sys

expected = sys.argv[1]
actual = metadata.version("system-analyzer")
if actual != expected:
    raise SystemExit(f"installed version {actual} does not match wheel version {expected}")
import maintenance
import main
print(f"Installed system-analyzer {actual}")
print(f"  maintenance: {maintenance.__file__}")
print(f"  main: {main.__file__}")
'@
    $verifyPath = Join-Path ([System.IO.Path]::GetTempPath()) ("system-analyzer-verify-" + [guid]::NewGuid() + ".py")
    try {
        [System.IO.File]::WriteAllText($verifyPath, $script)
        Push-Location ([System.IO.Path]::GetPathRoot((Get-PackageDir)))
        try { & $Py $verifyPath $ExpectedVersion } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { throw "installed wheel verification failed" }
    } finally {
        Remove-Item $verifyPath -ErrorAction SilentlyContinue
    }
}

# Install one wheel per-user, handling old pip that lacks the flag. The
# --break-system-packages flag is a no-op where PEP 668 does not apply.
function Install-UserWheels {
    param([object]$Py, [string[]]$Wheels)
    $argsWith = @("-m", "pip", "install", "--user", "--force-reinstall", "--break-system-packages") + $Wheels
    $out = & $Py @argsWith 2>&1
    if ($LASTEXITCODE -eq 0) { return }
    $joined = ($out -join "`n")
    if ($joined -match "no such option") {
        $argsPlain = @("-m", "pip", "install", "--user", "--force-reinstall") + $Wheels
        $out = & $Py @argsPlain 2>&1
        if ($LASTEXITCODE -ne 0) { throw ($out -join "`n") }
        return
    }
    throw $joined
}

function Invoke-Versioned {
    param([object]$Py, [string]$Module, [string[]]$Arguments)
    Push-Location (Get-PackageDir)
    try {
        & $Py -m $Module @Arguments
        if ($LASTEXITCODE -ne 0) { throw "exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
}
