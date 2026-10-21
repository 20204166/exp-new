# Standalone online installer. Run from any folder:
# irm https://raw.githubusercontent.com/20204166/exp-new/main/install/install-online.ps1 | iex
param([switch]$System)
$ErrorActionPreference = "Stop"
$base = "https://raw.githubusercontent.com/20204166/exp-new/main/dist"
$connectVersion = "2.2.1.1"
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("system-analyzer-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    $sumsPath = Join-Path $tmp "SHA256SUMS"
    Invoke-WebRequest "$base/SHA256SUMS" -OutFile $sumsPath
    $entries = @{}
    foreach ($entry in Get-Content $sumsPath) {
        if (-not $entry.Trim()) { continue }
        if ($entry -notmatch "^([0-9a-fA-F]{64})\s+([A-Za-z0-9_.-]+)$") {
            throw "Invalid SHA256SUMS entry"
        }
        if ($entries.ContainsKey($matches[2])) { throw "Duplicate SHA256SUMS entry" }
        $entries[$matches[2]] = $matches[1].ToLowerInvariant()
    }
    $appWheels = @($entries.Keys | Where-Object {
        $_ -match "^system_analyzer-(\d+\.\d+\.\d+\.\d+)-py3-none-any\.whl$"
    })
    if ($appWheels.Count -ne 1) { throw "Expected exactly one application wheel in SHA256SUMS" }
    $wheel = $appWheels[0]
    if ($wheel -notmatch "^system_analyzer-(\d+\.\d+\.\d+\.\d+)-py3-none-any\.whl$") {
        throw "Unexpected application wheel filename: $wheel"
    }
    $expectedVersion = $matches[1]
    $wheelPath = Join-Path $tmp $wheel
    Invoke-WebRequest "$base/$wheel" -OutFile $wheelPath
    $actual = (Get-FileHash $wheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $entries[$wheel]) { throw "Application wheel checksum mismatch" }
    $companions = @($entries.Keys | Where-Object {
        $_.StartsWith("expra_connect-$connectVersion-") -and $_.EndsWith(".whl")
    })
    if ($companions.Count -eq 0) { throw "No compatible expra-connect wheels in SHA256SUMS" }
    foreach ($companion in $companions) {
        $companionPath = Join-Path $tmp $companion
        Invoke-WebRequest "$base/$companion" -OutFile $companionPath
        $actual = (Get-FileHash $companionPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $entries[$companion]) { throw "expra-connect wheel checksum mismatch: $companion" }
    }

    function Test-VenvPython {
        param([object]$Py)
        $script = "import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)"
        & $Py -c $script 2>$null
        return ($LASTEXITCODE -eq 0)
    }

    # Candidate list mirrors install/_common.ps1 Get-PythonCandidates so the
    # standalone online installer finds the same interpreters: py launcher
    # with an explicit version, `py -3` (latest 3.x), versioned python3.x
    # commands, plain python3/python, and the per-user python.org install
    # locations under %LOCALAPPDATA%\Programs\Python.
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

    $py = $null
    $base = $null
    $reasons = [System.Collections.Generic.List[string]]::new()
    foreach ($candidate in Get-PythonCandidates) {
        try {
            & $candidate -c "import sys; assert sys.version_info >= (3,10)" 2>$null
            if ($LASTEXITCODE -ne 0) {
                $reasons.Add("'$($candidate -join ' ')' is not a Python 3.10+ interpreter")
                continue
            }
            if (-not (Test-VenvPython $candidate)) { $py = $candidate; break }
            $reasons.Add("'$($candidate -join ' ')' is inside a virtual environment")
            if (-not $base) { $base = $candidate }
        } catch {
            $reasons.Add("'$($candidate -join ' ')' could not be launched")
        }
    }
    # Every candidate was a venv: fall back to its base interpreter so a
    # per-user install works even while a venv is active.
    if (-not $py -and $base) {
        $basePy = & $base -c "import sys; print(sys._base_executable)" 2>$null
        if ($basePy -and (Test-Path $basePy) -and -not (Test-VenvPython $basePy)) {
            $py = @($basePy)
        } else {
            $reasons.Add("virtual-environment base interpreter unavailable: $basePy")
        }
    }
    if (-not $py) {
        Write-Host "No usable Python 3.10+ interpreter was found. Tried:"
        $reasons | ForEach-Object { Write-Host "  - $_" }
        Write-Host "Install Python 3.10 or newer from https://www.python.org/downloads/ (be sure to enable the 'py launcher' and 'Add python.exe to PATH'), or deactivate any active virtual environment and retry."
        throw "System Analyzer requires Python 3.10 or newer. Deactivate any active virtual environment (or run outside it) and retry."
    }
    if (Test-VenvPython $py) { throw "Cannot install: '$($py -join ' ')' is inside a virtual environment (pip disables '--user' inside venvs). Deactivate the venv and retry." }
    if ($env:VIRTUAL_ENV) {
        Write-Warning "Active virtual environment '$env:VIRTUAL_ENV' was not modified. This online installer targets the system/user interpreter. Use install.ps1 -Python <venv-python> to update the active venv."
    }
    $pipArgs = @("-m", "pip", "install", "--force-reinstall")
    if (-not $System) { $pipArgs += "--user" }
    $pipArgs += @("--break-system-packages", "--find-links", $tmp, $wheelPath, "expra-connect==$connectVersion")
    & $py @pipArgs
    if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

    $verify = @'
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
    $verifyPath = Join-Path $tmp "verify_installed.py"
    [System.IO.File]::WriteAllText($verifyPath, $verify)
    $driveRoot = [System.IO.Path]::GetPathRoot((Get-Location).Path)
    Push-Location $driveRoot
    try { & $py $verifyPath $expectedVersion } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw "installed wheel verification failed" }

    if ($System) {
        $launcherCmd = Get-Command system-analyzer -ErrorAction SilentlyContinue
        if ($launcherCmd) {
            $launcherPath = $launcherCmd.Source
            if (-not $launcherPath) { $launcherPath = $launcherCmd.Path }
            $bin = Split-Path $launcherPath -Parent
        } else {
            $scheme = if ($env:OS -eq "Windows_NT") { "nt" } else { "posix_prefix" }
            $bin = & $py -c "import sysconfig;print(sysconfig.get_path('scripts', scheme='$scheme'))"
        }
    } else {
        $scheme = if ($env:OS -eq "Windows_NT") { "nt_user" } else { "posix_user" }
        $bin = & $py -c "import sysconfig;print(sysconfig.get_path('scripts', scheme='$scheme'))"
    }
    if ($env:OS -eq "Windows_NT" -and $bin -and (Test-Path $bin)) {
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $already = ($userPath -split ";") | Where-Object { $_.TrimEnd("\") -eq $bin.TrimEnd("\") }
        if (-not $already) {
            $updated = if ($userPath) { "$bin;$userPath" } else { $bin }
            [Environment]::SetEnvironmentVariable("Path", $updated, "User")
            Write-Host "Added '$bin' to your user PATH."
        }
        $sessionHas = ($env:Path -split ";") | Where-Object { $_.TrimEnd("\") -eq $bin.TrimEnd("\") }
        if (-not $sessionHas) {
            $env:Path = "$bin;$env:Path"
        }
        Write-Host "Installed $wheel. Console scripts: $bin"
        Write-Host "Run now: system-analyzer"
    } else {
        Write-Host "Installed $wheel. Console scripts: $bin"
        Write-Host "Add this directory to PATH if needed, then run: system-analyzer"
    }
} finally { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
