# install.ps1
# Fallback installer (no external tools required)
# Copies dist\DiplomQuality\* to %LOCALAPPDATA%\Programs\DiplomQuality
# Creates Start Menu shortcut via WScript.Shell
# Re-runnable as update (overwrites program files only). Never touches %LOCALAPPDATA%\DiplomQuality\data

param(
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\DiplomQuality"
)

$ErrorActionPreference = "Stop"

$PackagingDir = $PSScriptRoot
$SourceDir = Join-Path $PackagingDir "dist\DiplomQuality"

if (-not (Test-Path -LiteralPath $SourceDir)) {
    throw "Source bundle not found: $SourceDir. Run build.ps1 first."
}

# Create/install dir
if (-not (Test-Path -LiteralPath $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}

# Copy files (overwrite)
Write-Host "Installing to $InstallDir..."
Copy-Item -Path "$SourceDir\*" -Destination $InstallDir -Recurse -Force

# Create Start Menu shortcut
$StartMenuDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
if (-not (Test-Path -LiteralPath $StartMenuDir)) {
    New-Item -ItemType Directory -Path $StartMenuDir -Force | Out-Null
}

$ShortcutPath = Join-Path $StartMenuDir "DiplomQuality.lnk"
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = Join-Path $InstallDir "DiplomQuality.exe"
$Shortcut.WorkingDirectory = $InstallDir
$Shortcut.Description = "ИС анализа качества продукции"
$Shortcut.Save()

Write-Host "Shortcut created: $ShortcutPath"
Write-Host "Install complete."
