# build_installer.ps1
# Builds Inno Setup installer for DiplomQuality
# - Reads APP_VERSION from app.config
# - Ensures frozen bundle exists (runs build.ps1 if missing)
# - Locates ISCC.exe (PATH, common Inno Setup 6 paths, scoop shim)
# - Runs ISCC with /DAppVersion

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$PackagingDir = $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$BundleExe = Join-Path $PackagingDir "dist\DiplomQuality\DiplomQuality.exe"
$BuildScript = Join-Path $PackagingDir "build.ps1"
$IssScript = Join-Path $PackagingDir "installer.iss"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python not found at $Python"
}

# Read APP_VERSION from app.config
$Version = & $Python -c "from app.config import APP_VERSION; print(APP_VERSION)" 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Failed to read APP_VERSION from app.config: $Version"
}
$Version = $Version.Trim()

Write-Host "App version: $Version"

# Ensure bundle exists
if (-not (Test-Path -LiteralPath $BundleExe)) {
    Write-Host "Bundle not found, running build.ps1..."
    & $BuildScript
    if ($LASTEXITCODE -ne 0) {
        throw "build.ps1 failed"
    }
}

# Locate ISCC.exe
$Iscc = $null

# Try PATH first
$IsccPath = (Get-Command "ISCC.exe" -ErrorAction SilentlyContinue).Source
if ($IsccPath) {
    $Iscc = $IsccPath
}

# Common Inno Setup 6 paths
if (-not $Iscc) {
    $Candidates = @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    foreach ($c in $Candidates) {
        if (Test-Path -LiteralPath $c) {
            $Iscc = $c
            break
        }
    }
}

# Scoop shim
if (-not $Iscc) {
    $ScoopShim = Join-Path $env:USERPROFILE "scoop\shims\iscc.exe"
    if (Test-Path -LiteralPath $ScoopShim) {
        $Iscc = $ScoopShim
    }
}

if (-not $Iscc) {
    throw "ISCC.exe not found. Install Inno Setup 6 (e.g. scoop install extras/inno-setup or winget install JRSoftware.InnoSetup)"
}

Write-Host "Using ISCC: $Iscc"

# Run ISCC
& $Iscc "/DAppVersion=$Version" $IssScript 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "ISCC failed with exit code $LASTEXITCODE"
}

# Find produced setup exe
$SetupExe = Get-ChildItem -Path $PackagingDir -Filter "DiplomQuality-Setup-$Version*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $SetupExe) {
    # Fallback: look for any setup exe
    $SetupExe = Get-ChildItem -Path $PackagingDir -Filter "DiplomQuality-Setup-*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
}

if ($SetupExe) {
    Write-Host "Setup produced: $($SetupExe.FullName)"
} else {
    Write-Host "Setup exe not found in $PackagingDir"
}
