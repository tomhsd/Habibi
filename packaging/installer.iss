; Habibi Windows installer (Inno Setup 6) — builds dist\HabibiSetup.exe
; Installs per-user into %LOCALAPPDATA%\Programs\Habibi (no admin needed, and
; Habibi can save its settings next to Habibi.exe). Updating keeps your settings.

#define AppVersion GetEnv("HABIBI_VERSION")
#if AppVersion == ""
  #undef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6B1D4F2A-7C3E-4E59-9A21-5A8B1B1B0001}
AppName=Habibi
AppVersion={#AppVersion}
AppPublisher=Mehedi
DefaultDirName={localappdata}\Programs\Habibi
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=HabibiSetup
SetupIconFile=..\config\habibi.ico
UninstallDisplayIcon={app}\Habibi.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: checkedonce

[Files]
Source: "..\dist\Habibi\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Habibi"; Filename: "{app}\Habibi.exe"
Name: "{autodesktop}\Habibi"; Filename: "{app}\Habibi.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Habibi.exe"; Description: "Start Habibi"; Flags: nowait postinstall skipifsilent
