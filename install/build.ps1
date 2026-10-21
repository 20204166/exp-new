# Build the universal System Analyzer wheel and one paired release bundle.
# Example: .\build.ps1 -Variant native -NativeConnectWheel C:\path\to\expra_connect-...whl
param(
    [string]$Python,
    [string]$Bump = "auto",
    [ValidateSet("universal", "native")][string]$Variant = "universal",
    [string]$NativeConnectWheel
)
. (Join-Path $PSScriptRoot "_common.ps1")

$py = Resolve-Python $Python
Require-Python $py
$here = Get-PackageDir
$Variant = $Variant.ToLowerInvariant()
$buildRoot = $null
$versionPath = Join-Path $here "maintenance\_version.py"
$versionBackup = [System.IO.File]::ReadAllText($versionPath)
$buildSucceeded = $false

try {
    if ($Variant -eq "native") {
        if (-not $NativeConnectWheel) {
            throw "Native variant requires -NativeConnectWheel PATH."
        }
        $companionWheel = [System.IO.Path]::GetFullPath($NativeConnectWheel)
        Invoke-Versioned $py "maintenance._release" @(
            "verify-companion-wheel", $companionWheel, "--native"
        )
    } else {
        if ($NativeConnectWheel) {
            throw "-NativeConnectWheel requires -Variant native."
        }
        $companionWheel = Join-Path $here "dist\expra_connect-2.2.1.1-py3-none-any.whl"
        Invoke-Versioned $py "maintenance._release" @(
            "verify-companion-wheel", $companionWheel
        )
    }
    Write-Host "Preparing build inputs and version..."
    Invoke-Versioned $py "maintenance._release" @(
        "prepare-build", "--package-dir", $here, "--bump", $Bump
    )

    $buildRoot = Join-Path ([System.IO.Path]::GetTempPath()) "system-analyzer-release-$([guid]::NewGuid().ToString('N'))"
    $source = Join-Path $buildRoot "source"
    $artifacts = Join-Path $buildRoot "artifacts"
    New-Item -ItemType Directory -Path $source, $artifacts -Force | Out-Null
    Copy-Item (Join-Path $here "pyproject.toml"), (Join-Path $here "README.md"),
        (Join-Path $here "main.py"), (Join-Path $here "algo.py") -Destination $source
    Copy-Item (Join-Path $here "maintenance") -Destination $source -Recurse

    Push-Location $here
    try {
        $appVersion = (& $py -c 'from maintenance._version import __version__; print(__version__)').Trim()
        $appWheel = Join-Path $here "dist\system_analyzer-$appVersion-py3-none-any.whl"
        $wheelIsCurrent = $false
        if (Test-Path $appWheel) {
            & $py -c 'import sys; from pathlib import Path; from maintenance._release import _collect_package_inputs, _read_manifest_from_wheel, diff_manifests; raise SystemExit(0 if not diff_manifests(_read_manifest_from_wheel(Path(sys.argv[1])), _collect_package_inputs(Path(sys.argv[2]))).has_changes else 1)' $appWheel $here
            $wheelIsCurrent = ($LASTEXITCODE -eq 0)
        }
        if (-not $wheelIsCurrent) {
            Write-Host "Building universal system-analyzer wheel..."
            & $py -m pip wheel $source --no-deps --no-build-isolation --wheel-dir $artifacts
            if ($LASTEXITCODE -ne 0) { throw "pip wheel failed (exit $LASTEXITCODE)" }
            $builtWheels = @(Get-ChildItem $artifacts -Filter "*.whl")
            if ($builtWheels.Count -ne 1) {
                throw "Expected one universal harness wheel, found $($builtWheels.Count)."
            }
            $appWheel = $builtWheels[0].FullName
        }
    } finally {
        Pop-Location
    }
    Invoke-Versioned $py "maintenance._release" @("verify-wheel", $appWheel)
    $appVersion = (& $py -c 'import sys; from pathlib import Path; from maintenance._release import _wheel_version; print(_wheel_version(Path(sys.argv[1])))' $appWheel).Trim()
    $bundleDir = Join-Path $here "dist\$Variant\$appVersion"
    Invoke-Versioned $py "maintenance._release" @(
        "create-bundle", $appWheel, $companionWheel,
        $bundleDir, "--variant", $Variant
    )
    $rootAppWheel = Join-Path $here "dist\system_analyzer-$appVersion-py3-none-any.whl"
    if ([System.IO.Path]::GetFullPath($appWheel) -ne [System.IO.Path]::GetFullPath($rootAppWheel)) {
        Copy-Item $appWheel -Destination (Join-Path $here "dist") -Force
    }
    if ($Variant -eq "native") {
        Copy-Item $companionWheel -Destination (Join-Path $here "dist") -Force
    }
    Invoke-Versioned $py "maintenance._release" @(
        "sync-artifacts", "--package-dir", $here
    )
    $buildSucceeded = $true
} finally {
    if ($buildRoot) { Remove-Item $buildRoot -Recurse -Force -ErrorAction SilentlyContinue }
    if (-not $buildSucceeded) {
        [System.IO.File]::WriteAllText($versionPath, $versionBackup)
    }
}

Write-Host "Built $Variant bundle: $bundleDir"
Write-Host "Building does not install."
