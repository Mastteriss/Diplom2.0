; Inno Setup 6 script for DiplomQuality
; Per-user install to %LOCALAPPDATA%\Programs\DiplomQuality
; Upgrades use fixed AppId so program files are replaced; user data in %LOCALAPPDATA%\DiplomQuality\data is never touched by installer

#define AppNameRU "ИС анализа качества продукции"
#define AppNameEN "DiplomQuality"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{E7F4A2B8-9C36-4F42-9B8A-5A7D2F0E6C91}
AppName={#AppNameRU}
AppVersion={#AppVersion}
AppPublisher=Local
AppPublisherURL=
AppSupportURL=
AppUpdatesURL=
DefaultDirName={localappdata}\Programs\DiplomQuality
DefaultGroupName={#AppNameRU}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll
OutputBaseFilename=DiplomQuality-Setup-{#AppVersion}
OutputDir=.
SetupLogging=yes
SolidCompression=yes
Compression=lzma

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\DiplomQuality\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppNameRU}"; Filename: "{app}\DiplomQuality.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppNameRU}"; Filename: "{app}\DiplomQuality.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\DiplomQuality.exe"; Description: "{cm:LaunchProgram,{#AppNameRU}}"; Flags: nowait postinstall skipifsilent

[Code]
var
  DataDir: string;
  UninstallMsg: string;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  if UninstallSilent() then
  begin
    RegWriteStringValue(HKEY_CURRENT_USER, 'Software\DiplomQuality\Uninstall', 'PurgeData', '0');
  end
  else
  begin
    DataDir := ExpandConstant('{localappdata}\DiplomQuality\data');
    UninstallMsg := 'Удалить пользовательские данные?' + #13#10 +
                    'Путь: ' + DataDir + #13#10#13#10 +
                    '(По умолчанию: Нет — данные сохраняются)';
    if MsgBox(UninstallMsg, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      RegWriteStringValue(HKEY_CURRENT_USER, 'Software\DiplomQuality\Uninstall', 'PurgeData', '1')
    else
      RegWriteStringValue(HKEY_CURRENT_USER, 'Software\DiplomQuality\Uninstall', 'PurgeData', '0');
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Purge: string;
  DataDirPath: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if RegQueryStringValue(HKEY_CURRENT_USER, 'Software\DiplomQuality\Uninstall', 'PurgeData', Purge) then
    begin
      if Purge = '1' then
      begin
        DataDirPath := ExpandConstant('{localappdata}\DiplomQuality\data');
        DelTree(DataDirPath, True, True, True);
      end;
    end;
    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, 'Software\DiplomQuality\Uninstall');
  end;
end;
