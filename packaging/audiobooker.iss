; Inno Setup script for Audiobooker.
; Build:  iscc packaging\audiobooker.iss   (after running PyInstaller, see build_windows.ps1)

#define AppName "Audiobooker"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{8C3E4B6A-5F1D-4A7B-9E2C-AB0D0C1E7F42}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Audiobooker
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Audiobooker-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\Audiobooker.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\launcher\Audiobooker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\uv\uv.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\app\audiobooker\*"; DestDir: "{app}\app\audiobooker"; Excludes: "__pycache__,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\app\requirements.txt"; DestDir: "{app}\app"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\Audiobooker.exe"; IconFilename: "{app}\icon.ico"
Name: "{group}\{#AppName} (repair setup)"; Filename: "{app}\Audiobooker.exe"; Parameters: "--repair"; IconFilename: "{app}\icon.ico"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Audiobooker.exe"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\Audiobooker.exe"; Description: "Launch {#AppName} (first launch downloads ~3–4 GB)"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; The private Python/PyTorch runtime is rebuilt on demand, so always remove it.
Type: filesandordirs; Name: "{localappdata}\Audiobooker\runtime"
Type: filesandordirs; Name: "{localappdata}\Audiobooker\jobs"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if DirExists(ExpandConstant('{localappdata}\Audiobooker')) then
      if MsgBox('Also delete downloaded voice models, your voice clips and settings?' + #13#10 +
                '(' + ExpandConstant('{localappdata}\Audiobooker') + ')',
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(ExpandConstant('{localappdata}\Audiobooker'), True, True, True);
  end;
end;
