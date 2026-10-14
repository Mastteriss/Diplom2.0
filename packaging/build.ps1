# Сборка frozen-версии (PyInstaller onedir) в packaging/dist.
# distpath/workpath заданы явно, чтобы результат лежал рядом со сборкой,
# а не в корне репозитория (по умолчанию PyInstaller пишет в ./dist от cwd).

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Не найден Python виртуального окружения: $Python"
}

& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Устанавливаю PyInstaller в .venv ..."
    & $Python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить PyInstaller." }
}

$Spec = Join-Path $PSScriptRoot "diplom.spec"
$DistDir = Join-Path $PSScriptRoot "dist"
$WorkDir = Join-Path $PSScriptRoot "build"

Push-Location $Root
try {
    & $Python -m PyInstaller $Spec --noconfirm --clean --distpath $DistDir --workpath $WorkDir
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с кодом $LASTEXITCODE." }
}
finally {
    Pop-Location
}

$Exe = Join-Path $DistDir "DiplomQuality\DiplomQuality.exe"
if (-not (Test-Path -LiteralPath $Exe)) { throw "Сборка не найдена: $Exe" }
Write-Host "Готово: $Exe"
