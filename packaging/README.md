# Packaging notes (tool-facing)

- App version: read from `app.config.APP_VERSION` at build time (build_installer.ps1). Do not hard-code elsewhere.
- Frozen bundle: `packaging/dist/DiplomQuality/DiplomQuality.exe` (onedir). User data lives outside install dir at `%LOCALAPPDATA%\DiplomQuality\data`. Upgrades replace program files only.
- Launcher: `DiplomQuality.exe [--host H] [--port P] [--no-browser] [--version] [--data-dir DIR]`
- Inno Setup installer (`installer.iss`):
  - Fixed `AppId` for upgrade detection (re-runnable to update).
  - Per-user: `DefaultDirName={localappdata}\Programs\DiplomQuality`, `PrivilegesRequired=lowest`.
  - Copies `dist\DiplomQuality\*` to `{app}`.
  - On uninstall, asks whether to delete `%LOCALAPPDATA%\DiplomQuality\data` (default No).
- Build: `packaging/build_installer.ps1` reads version, ensures bundle exists, locates ISCC.exe, runs ISCC with `/DAppVersion`.
- Fallback: `packaging/install.ps1` / `packaging/uninstall.ps1` (no external tools). `install.ps1` overwrites program files only; `uninstall.ps1` leaves data unless `-PurgeData`.
- Do not edit: `run_diplom.py`, `diplom.spec`, `build.ps1`. Main README is owned by another agent.
