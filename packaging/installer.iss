; Inno Setup script for Parakeet Dictate. Built by packaging\build.ps1 (pass /DAppVersion and /DRoot).
; Per-user install: no admin rights, installs under %LOCALAPPDATA%\Programs.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Root
  #define Root ".."
#endif
#define AppName "Parakeet Dictate"
#define AppExe "ParakeetDictate.exe"

[Setup]
AppId={{6F3B8A52-1C7E-4E0B-9C1D-5A7E2B4F9D10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
AppComments=Offline push-to-talk dictation with Orca's speech models (Parakeet v2 by default); removes filler words.
DefaultDirName={localappdata}\Programs\{#AppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir={#Root}\build\installer
OutputBaseFilename=ParakeetDictate-Setup-{#AppVersion}
SetupIconFile={#Root}\build\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "autostart"; Description: "Start {#AppName} when I sign in to Windows"; GroupDescription: "Options:"

[InstallDelete]
; The bundled Python and libraries are replaced as a whole, so an upgrade leaves nothing stale behind
; (0.2.0 bundled sherpa-onnx/onnxruntime; 0.3.0 uses transcribe.cpp).
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#Root}\build\dist\ParakeetDictate\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Offline dictation (Ctrl+Q)"
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopApp"

[UninstallDelete]
; The app's own "Start with Windows" toggle creates this too.
Type: files; Name: "{userstartup}\{#AppName}.lnk"
; Generated files, logs and any model copied from WSL / downloaded (~730 MB). Settings in %APPDATA% are kept.
Type: filesandordirs; Name: "{localappdata}\ParakeetDictate"
