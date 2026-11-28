; Habibi Windows installer (Inno Setup 6). Two separate editions:
;   HABIBI_EDITION=Online  -> dist\HabibiOnlineSetup.exe   (%LOCALAPPDATA%\Programs\Habibi Online)
;   HABIBI_EDITION=Offline -> dist\HabibiOfflineSetup.exe  (%LOCALAPPDATA%\Programs\Habibi)
; Each edition has its own AppId and folder, so they install, update and
; uninstall independently. Per-user install: no admin needed, and Habibi saves
; its settings next to Habibi.exe. Updating keeps your settings.
; The Offline edition keeps the old AppId and folder, so earlier HabibiSetup.exe
; installs (which included offline mode) update into it.

#define AppVersion GetEnv("HABIBI_VERSION")
#if AppVersion == ""
  #undef AppVersion
  #define AppVersion "1.0.0"
#endif

#define Edition GetEnv("HABIBI_EDITION")
#if Edition == ""
  #undef Edition
  #define Edition "Offline"
#endif

#if Edition == "Online"
  #define AppGuid "{{6B1D4F2A-7C3E-4E59-9A21-5A8B1B1B0002}"
  #define AppFolder "Habibi Online"
#else
  #define AppGuid "{{6B1D4F2A-7C3E-4E59-9A21-5A8B1B1B0001}"
  #define AppFolder "Habibi"
#endif

[Setup]
AppId={#AppGuid}
AppName=Habibi {#Edition}
AppVersion={#AppVersion}
AppPublisher=Mehedi
DefaultDirName={localappdata}\Programs\{#AppFolder}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Habibi{#Edition}Setup
SetupIconFile=..\config\habibi.ico
UninstallDisplayIcon={app}\Habibi.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: checkedonce

[Files]
Source: "..\dist\{#Edition}\Habibi\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Habibi {#Edition}"; Filename: "{app}\Habibi.exe"
Name: "{autodesktop}\Habibi {#Edition}"; Filename: "{app}\Habibi.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Habibi.exe"; Description: "Start Habibi {#Edition}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; the user's own logo (⚙ → CHANGE LOGO) lives here
Type: filesandordirs; Name: "{app}\config\logo"
