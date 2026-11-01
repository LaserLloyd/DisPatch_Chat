; DisPatch Chat: per-user Windows installer (Inno Setup 6.3 or newer).
;
; Build (from this folder, on Windows):
;     iscc /DAppVersion=4.3.0 dispatch.iss
; It needs build\uv.exe (a uv release for x86_64-pc-windows-msvc); README.md says
; where to get it. .github/workflows/windows-installer.yml does all of this on
; windows-latest.
;
; What the installed app looks like (see launcher.ps1 for the run side):
;   {app} = %LOCALAPPDATA%\Programs\DisPatch    program files, uv.exe, uv's Python
;   %LOCALAPPDATA%\DisPatch\data                chats, settings, uploads
;   %LOCALAPPDATA%\DisPatch\logs                launcher and server logs
;   %LOCALAPPDATA%\DisPatch\launcher.json       listen address; 127.0.0.1 by default
;
; No administrator rights anywhere: per-user install, per-user scheduled task,
; no firewall rule, no service. DisPatch listens on 127.0.0.1 until the user
; changes launcher.json (docs/deploy-windows.md, "Use it from your phone").
; Uninstall keeps %LOCALAPPDATA%\DisPatch unless the user says Yes to deleting it
; (or passes /DELETEDATA=1 to a silent uninstall).

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#define AppName "DisPatch"
#define RepoRoot "..\.."

#if !FileExists(AddBackslash(SourcePath) + "build\uv.exe")
  #error build\uv.exe is missing: put a Windows x86_64 uv.exe there first (see README.md)
#endif

[Setup]
; Never change AppId: it is how an upgrade finds the previous install.
AppId={{6B1F3C1E-4D2A-4F7B-9E3C-0D15C4A7B243}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=DisPatch Chat
AppPublisherURL=https://github.com/LaserLloyd/DisPatch_Chat
AppSupportURL=https://github.com/LaserLloyd/DisPatch_Chat/issues
DefaultDirName={localappdata}\Programs\DisPatch
; A fixed folder: the uninstaller deletes the program tree it made, so it must
; never be a folder that holds anything else.
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
LicenseFile={#RepoRoot}\LICENSE
SetupIconFile={#RepoRoot}\frontend\static\favicon.ico
UninstallDisplayIcon={app}\dispatch.ico
UninstallDisplayName={#AppName}
OutputDir=Output
OutputBaseFilename=DisPatch-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
; launcher.ps1 stops the running server itself (PrepareToInstall below); the
; Restart Manager would only offer to close the user's browser.
CloseApplications=no
RestartApplications=no

[Tasks]
Name: "autostart"; Description: "Start DisPatch when I sign in to Windows"; Flags: unchecked
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The app. Test suites, caches and any local virtualenv stay out.
Source: "{#RepoRoot}\backend\*"; DestDir: "{app}\backend"; \
    Excludes: ".venv,\tests,__pycache__,*.pyc,.pytest_cache,.ruff_cache"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\frontend\static\*"; DestDir: "{app}\frontend\static"; \
    Excludes: "__pycache__"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\frontend\overlays\*"; DestDir: "{app}\frontend\overlays"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\apps\*"; DestDir: "{app}\apps"; \
    Excludes: "tests,__pycache__,*.pyc,conftest.py"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\frontend\static\favicon.ico"; DestDir: "{app}"; DestName: "dispatch.ico"; Flags: ignoreversion
; The runner.
Source: "launcher.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "build\uv.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
#if FileExists(AddBackslash(SourcePath) + "build\uv-LICENSE-MIT")
Source: "build\uv-LICENSE-MIT"; DestDir: "{app}\bin"; Flags: ignoreversion
#endif
#if FileExists(AddBackslash(SourcePath) + "build\uv-LICENSE-APACHE")
Source: "build\uv-LICENSE-APACHE"; DestDir: "{app}\bin"; Flags: ignoreversion
#endif

[InstallDelete]
; An upgrade replaces the program tree instead of layering on it, so a module a
; newer version removed cannot linger. backend\.venv and uv's Python stay (the
; Setup step re-syncs them).
Type: filesandordirs; Name: "{app}\backend\app"
Type: filesandordirs; Name: "{app}\frontend"
Type: filesandordirs; Name: "{app}\apps"

[Dirs]
Name: "{localappdata}\DisPatch"; Flags: uninsneveruninstall
Name: "{localappdata}\DisPatch\data"; Flags: uninsneveruninstall
Name: "{localappdata}\DisPatch\logs"; Flags: uninsneveruninstall

[Icons]
Name: "{autoprograms}\DisPatch\DisPatch"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\launcher.ps1"" -Action Open"; \
    WorkingDir: "{app}"; IconFilename: "{app}\dispatch.ico"; Comment: "Open DisPatch in your browser"
Name: "{autoprograms}\DisPatch\Stop DisPatch"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\launcher.ps1"" -Action Stop"; \
    WorkingDir: "{app}"; IconFilename: "{app}\dispatch.ico"; Comment: "Stop the DisPatch server"
Name: "{autoprograms}\DisPatch\DisPatch logs"; Filename: "{localappdata}\DisPatch\logs"
Name: "{autoprograms}\DisPatch\Uninstall DisPatch"; Filename: "{uninstallexe}"
Name: "{autodesktop}\DisPatch"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\launcher.ps1"" -Action Open"; \
    WorkingDir: "{app}"; IconFilename: "{app}\dispatch.ico"; Tasks: desktopicon

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\launcher.ps1"" -Action Open"; \
    Description: "Open DisPatch now"; Flags: postinstall nowait skipifsilent runhidden; Check: SetupSucceeded

[UninstallDelete]
; Everything the program folder grew after install: uv's Python, its cache, the
; virtualenv and bytecode. Your data is not in here.
Type: filesandordirs; Name: "{app}\backend"
Type: filesandordirs; Name: "{app}\frontend"
Type: filesandordirs; Name: "{app}\apps"
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\uv-cache"
Type: filesandordirs; Name: "{app}\bin"
Type: dirifempty; Name: "{app}"

[Code]
var
  SetupOk: Boolean;
  DeleteData: Boolean;

function PowerShellExe(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

{ Runs launcher.ps1 with a hidden window and waits. Returns its exit code, or -1
  when it could not be started at all. }
function RunLauncher(const Launcher, Args: String): Integer;
var
  Code: Integer;
begin
  Result := -1;
  if not FileExists(Launcher) then
    Exit;
  if Exec(PowerShellExe(),
          '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Launcher + '" ' + Args,
          ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code) then
    Result := Code;
  Log(Format('launcher.ps1 %s exit code %d', [Args, Result]));
end;

function SetupSucceeded(): Boolean;
begin
  Result := SetupOk;
end;

{ An upgrade: stop the running server first, or its python.exe keeps files in
  the virtualenv locked. Uses the launcher of the version being replaced. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  RunLauncher(ExpandConstant('{app}\launcher.ps1'), '-Action Stop');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Args: String;
  Code: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    WizardForm.StatusLabel.Caption :=
      'Setting up Python and the app''s libraries. The first install downloads them and can take a few minutes...';
    Args := '-Action Setup';
    if WizardIsTaskSelected('autostart') then
      Args := Args + ' -Autostart';
    Code := RunLauncher(ExpandConstant('{app}\launcher.ps1'), Args);
    SetupOk := (Code = 0);
    if SetupOk then
      Log('DisPatch setup finished: OK')
    else
    begin
      Log('DisPatch setup finished: FAILED');
      SuppressibleMsgBox(
        'DisPatch was copied, but setting up Python and its libraries failed ' +
        '(an internet connection is needed for the first install).' + #13#10#13#10 +
        'The details are in ' + ExpandConstant('{localappdata}\DisPatch\logs') + '. ' +
        'Opening DisPatch from the Start menu tries the setup again.',
        mbError, MB_OK, IDOK);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    { Stop the server and remove the scheduled task before the files go. }
    RunLauncher(ExpandConstant('{app}\launcher.ps1'), '-Action Uninstall');
    if UninstallSilent() then
      DeleteData := (ExpandConstant('{param:DELETEDATA|0}') = '1')
    else
      DeleteData := (MsgBox(
        'Also delete your DisPatch data?' + #13#10#13#10 +
        'Your chats, settings, uploads and logs are in ' + ExpandConstant('{localappdata}\DisPatch') + '.' + #13#10 +
        'Choose No to keep them (a later install picks them up again).',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES);
  end;
  if (CurUninstallStep = usPostUninstall) and DeleteData then
  begin
    if DelTree(ExpandConstant('{localappdata}\DisPatch'), True, True, True) then
      Log('deleted the DisPatch data folder')
    else
      Log('could not delete the DisPatch data folder completely');
  end;
end;
