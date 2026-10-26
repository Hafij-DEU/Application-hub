; Inno Setup script: turns "dist\App Hub\" into AppHubSetup-<version>.exe
;   ISCC.exe /DAppVersion=1.0.0 packaging\windows\installer.iss
; Installs for all users to Program Files: App Hub's administrator helper must not live in a
; folder that programs without administrator rights can change.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{3E8B5C21-7A4D-4F0E-B6C9-2D1A8F5E7B43}
AppName=App Hub
AppVersion={#AppVersion}
AppVerName=App Hub {#AppVersion}
UninstallDisplayName=App Hub
AppPublisher=Md Hafijur Rahman
AppPublisherURL=https://github.com/Hafij-DEU/Application-hub
AppCopyright=© 2026 Md Hafijur Rahman
DefaultDirName={autopf}\App Hub
DefaultGroupName=App Hub
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=..\..\dist
OutputBaseFilename=AppHubSetup-{#AppVersion}
SetupIconFile=..\..\data\app-hub.ico
UninstallDisplayIcon={app}\AppHub.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
MinVersion=10.0.17763
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\..\dist\App Hub\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\App Hub"; Filename: "{app}\AppHub.exe"
Name: "{group}\App Hub Updates"; Filename: "{app}\AppHub.exe"; Parameters: "--page updates"
Name: "{autodesktop}\App Hub"; Filename: "{app}\AppHub.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AppHub.exe"; Description: "Start App Hub"; Flags: nowait postinstall skipifsilent
; App Hub updating itself passes /RELAUNCH, so it opens again after a silent update.
Filename: "{app}\AppHub.exe"; Flags: nowait runasoriginaluser; Check: ShouldRelaunch

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Code]
function ShouldRelaunch: Boolean;
var
  i: Integer;
begin
  Result := False;
  for i := 1 to ParamCount do
    if CompareText(ParamStr(i), '/RELAUNCH') = 0 then
      Result := True;
end;
