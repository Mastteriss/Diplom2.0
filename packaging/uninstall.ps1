# uninstall.ps1
# Fallback uninstaller
# Removes install dir and shortcut. Leaves user data unless -PurgeData is passed.

param(
    [switch]$PurgeData,
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\DiplomQuality"
)

$ErrorActionPreference = "Stop"

$DataDir = "$env:LOCALAPPDATA\DiplomQuality\data"
$ShortcutPath = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\DiplomQuality.lnk"

# Remove shortcut
if (Test-Path -LiteralPath $ShortcutPath) {
    Remove-Item -LiteralPath $ShortcutPath -Force
    Write-Host "Removed shortcut: $ShortcutPath"
}

# Remove install dir
if (Test-Path -LiteralPath $InstallDir) {
    Remove-Item -LiteralPath $InstallDir -Recurse -Force
    Write-Host "Removed install dir: $InstallDir"
}

# Remove data if requested
if ($PurgeData) {
    if (Test-Path -LiteralPath $DataDir) {
        Remove-Item -LiteralPath $DataDir -Recurse -Force
        Write-Host "Removed user data: $DataDir"
    } else {
        Write-Host "User data not found: $DataDir"
    }
} else {
    Write-Host "User data preserved (use -PurgeData to remove): $DataDir"
}

Write-Host "Uninstall complete."
