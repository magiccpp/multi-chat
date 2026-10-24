; Inno Setup script for MultiChat-Setup.exe. Built by .github/workflows/windows.yml (or locally:
;   iscc /DMyAppVersion=0.1.0 clients\desktop\installer\multichat.iss   after building dist\MultiChat with PyInstaller).
; Installs per user (no administrator rights needed), closes a running MultiChat first, keeps settings on upgrade.

#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif

[Setup]
AppId={{8B5C3F2A-6E1D-4C7B-9A2E-5D4F3C2B1A09}
AppName=MultiChat
AppVersion={#MyAppVersion}
AppVerName=MultiChat {#MyAppVersion}
AppPublisher=MultiChat
AppPublisherURL=https://github.com/magiccpp/multi-chat
AppSupportURL=https://github.com/magiccpp/multi-chat
VersionInfoVersion={#MyAppVersion}
DefaultDirName={autopf}\MultiChat
DefaultGroupName=MultiChat
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=MultiChat-Setup
SetupIconFile=multichat.ico
UninstallDisplayIcon={app}\MultiChat.exe
UninstallDisplayName=MultiChat
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "Start MultiChat when I sign in (keeps the runner available to the models)"; GroupDescription: "Other:"; Flags: unchecked

[InstallDelete]
; drop files of the previous version (PySide6 plugins change between releases)
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\MultiChat\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\MultiChat"; Filename: "{app}\MultiChat.exe"
Name: "{autodesktop}\MultiChat"; Filename: "{app}\MultiChat.exe"; Tasks: desktopicon
Name: "{userstartup}\MultiChat"; Filename: "{app}\MultiChat.exe"; Tasks: autostart

[Run]
Filename: "{app}\MultiChat.exe"; Description: "{cm:LaunchProgram,MultiChat}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM MultiChat.exe"; Flags: runhidden; RunOnceId: "StopMultiChat"

[Code]
// MultiChat keeps running in the tray after its window is closed; stop it so its files can be replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM MultiChat.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(500);
  Result := '';
end;
