; Blender MCP (C#) - per-user installer
;
; Installs into the user's roaming profile (no admin rights):
;   %APPDATA%\BlenderMcp\server\BlenderMcp.Server.exe   self-contained MCP server
;   %APPDATA%\BlenderMcp\addon\blender_mcp_bridge.zip   Blender add-on package
; then, for each Blender 4.2+ the user ticks:
;   blender.exe -c extension install-file -r user_default -e <zip>
;   -> %APPDATA%\Blender Foundation\Blender\<ver>\extensions\user_default\blender_mcp_bridge (enabled)
; and registers the server in Claude Desktop's claude_desktop_config.json (classic + Microsoft Store builds).
;
; Build with build-installer.ps1 (publishes the server and zips the add-on first).

#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif
#define AppName "Blender MCP (C#)"
#define ServerExe "BlenderMcp.Server.exe"
#define ExtId "blender_mcp_bridge"
#define McpName "blender-csharp"

[Setup]
AppId={{6B0E2C1D-8F4A-4C77-9E3B-2D5A1F0C9B41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Pete
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
DefaultDirName={userappdata}\BlenderMcp
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\artifacts\installer
OutputBaseFilename=BlenderMcpSetup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\server\{#ServerExe}
CloseApplications=no
SetupLogging=yes

[Files]
Source: "..\artifacts\server\{#ServerExe}"; DestDir: "{app}\server"; Flags: ignoreversion
Source: "..\artifacts\{#ExtId}.zip"; DestDir: "{app}\addon"; Flags: ignoreversion
Source: "..\blender_addon\{#ExtId}\*"; DestDir: "{app}\addon\{#ExtId}"; Excludes: "__pycache__"; Flags: ignoreversion recursesubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Tasks]
Name: "claude"; Description: "Add the MCP server to Claude Desktop (claude_desktop_config.json)"

[UninstallRun]
Filename: "{app}\server\{#ServerExe}"; Parameters: "--unregister --name {#McpName}"; \
  Flags: runhidden waituntilterminated; RunOnceId: "UnregisterClaude"

[UninstallDelete]
Type: files; Name: "{app}\install-state.ini"

[Code]
const
  StateFile = 'install-state.ini';
  MinMajor = 4;
  MinMinor = 2;

var
  BlenderPage: TInputOptionWizardPage;
  BlenderExes: TArrayOfString;
  BlenderVers: TArrayOfString;
  Results: String;
  ClaudeResult: String;

{ ---------------------------------------------------------------- helpers }

function GetPort(Param: String): String;
begin
  Result := ExpandConstant('{param:PORT|9877}');
end;

procedure AddBlender(const Exe, Ver: String);
var
  I, N: Integer;
begin
  if not FileExists(Exe) then
    Exit;
  N := GetArrayLength(BlenderExes);
  for I := 0 to N - 1 do
    if CompareText(BlenderExes[I], Exe) = 0 then
      Exit;
  SetArrayLength(BlenderExes, N + 1);
  SetArrayLength(BlenderVers, N + 1);
  BlenderExes[N] := Exe;
  BlenderVers[N] := Ver;
end;

{ "Blender 5.2" -> "5.2"; falls back to the exe's file version }
function VersionFor(const Dir, Exe: String): String;
var
  Name: String;
  MS, LS: Cardinal;
begin
  Name := ExtractFileName(RemoveBackslashUnlessRoot(Dir));
  if Pos('Blender ', Name) = 1 then
    Result := Copy(Name, 9, Length(Name))
  else if GetVersionNumbers(Exe, MS, LS) then
    Result := IntToStr(MS shr 16) + '.' + IntToStr(MS and $FFFF)
  else
    Result := '?';
end;

procedure ScanFolder(const Root: String);
var
  FR: TFindRec;
  Dir: String;
begin
  if FindFirst(AddBackslash(Root) + '*', FR) then
  try
    repeat
      if (FR.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0) and (FR.Name <> '.') and (FR.Name <> '..') then
      begin
        Dir := AddBackslash(Root) + FR.Name;
        AddBlender(Dir + '\blender.exe', VersionFor(Dir, Dir + '\blender.exe'));
      end;
    until not FindNext(FR);
  finally
    FindClose(FR);
  end;
end;

procedure FindBlenders;
var
  Dir: String;
begin
  SetArrayLength(BlenderExes, 0);
  SetArrayLength(BlenderVers, 0);
  ScanFolder(ExpandConstant('{commonpf64}\Blender Foundation'));
  ScanFolder(ExpandConstant('{localappdata}\Programs\Blender Foundation'));
  if RegQueryStringValue(HKLM64, 'SOFTWARE\BlenderFoundation', 'Install_Dir', Dir) then
    AddBlender(AddBackslash(Dir) + 'blender.exe', VersionFor(Dir, AddBackslash(Dir) + 'blender.exe'));
  if RegQueryStringValue(HKCU, 'SOFTWARE\BlenderFoundation', 'Install_Dir', Dir) then
    AddBlender(AddBackslash(Dir) + 'blender.exe', VersionFor(Dir, AddBackslash(Dir) + 'blender.exe'));
  { Steam }
  AddBlender(ExpandConstant('{commonpf32}\Steam\steamapps\common\Blender\blender.exe'), 'Steam');
end;

function SupportsExtensions(const Ver: String): Boolean;
var
  P, Major, Minor: Integer;
begin
  Result := True; { unknown (e.g. Steam): let blender itself decide }
  P := Pos('.', Ver);
  if P = 0 then
    Exit;
  Major := StrToIntDef(Copy(Ver, 1, P - 1), -1);
  Minor := StrToIntDef(Copy(Ver, P + 1, 2), -1);
  if Major < 0 then
    Exit;
  Result := (Major > MinMajor) or ((Major = MinMajor) and (Minor >= MinMinor));
end;

function IsProcessRunning(const ExeName: String): Boolean;
var
  Code: Integer;
  Tmp: String;
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := False;
  Tmp := ExpandConstant('{tmp}\tasklist.txt');
  if Exec(ExpandConstant('{cmd}'), '/C tasklist /FI "IMAGENAME eq ' + ExeName + '" /NH > "' + Tmp + '"',
          '', SW_HIDE, ewWaitUntilTerminated, Code) and LoadStringsFromFile(Tmp, Lines) then
    for I := 0 to GetArrayLength(Lines) - 1 do
      if Pos(Lowercase(ExeName), Lowercase(Lines[I])) > 0 then
        Result := True;
end;

procedure KillServer;
var
  Code: Integer;
begin
  { Claude Desktop keeps the server exe open; stop it so it can be replaced. Claude restarts it. }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#ServerExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

{ ----------------------------------------------------------- wizard pages }

procedure InitializeWizard;
var
  I, Idx: Integer;
  Caption: String;
begin
  FindBlenders;
  BlenderPage := CreateInputOptionPage(wpSelectTasks,
    'Blender add-on', 'Which Blender installations should get the MCP Bridge add-on?',
    'The add-on is installed into your roaming profile for each selected version and enabled automatically. ' +
    'Close Blender before continuing so it does not overwrite the change when it exits.',
    False, True);
  for I := 0 to GetArrayLength(BlenderExes) - 1 do
  begin
    Caption := 'Blender ' + BlenderVers[I] + '   (' + BlenderExes[I] + ')';
    if not SupportsExtensions(BlenderVers[I]) then
      Caption := Caption + '  - needs Blender 4.2 or newer';
    Idx := BlenderPage.Add(Caption);
    BlenderPage.Values[Idx] := SupportsExtensions(BlenderVers[I]);
    BlenderPage.CheckListBox.ItemEnabled[Idx] := SupportsExtensions(BlenderVers[I]);
  end;
  if GetArrayLength(BlenderExes) = 0 then
    BlenderPage.SubCaptionLabel.Caption :=
      'No Blender installation was found. The add-on package will still be copied to ' +
      ExpandConstant('{userappdata}\BlenderMcp\addon') + ' - install it from Blender: Preferences > Get Extensions > Install from Disk.';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Answer: Integer;
begin
  Result := True;
  if (CurPageID = BlenderPage.ID) then
    while IsProcessRunning('blender.exe') do
    begin
      Answer := SuppressibleMsgBox('Blender is running. Please save your work and close Blender, then click Retry.' + #13#10#13#10 +
        'Ignore installs anyway (you will need to enable the add-on manually if Blender overwrites its preferences on exit).',
        mbConfirmation, MB_ABORTRETRYIGNORE, IDIGNORE);
      if Answer = IDABORT then
      begin
        Result := False;
        Exit;
      end;
      if Answer = IDIGNORE then
        Exit;
    end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  KillServer;
  Result := '';
end;

{ ------------------------------------------------------ install / remove }

function RoamingExtDir(const Ver: String): String;
begin
  Result := ExpandConstant('{userappdata}\Blender Foundation\Blender\') + Ver + '\extensions\user_default\{#ExtId}';
end;

procedure InstallIntoBlender(const Exe, Ver: String; Slot: Integer);
var
  Code: Integer;
  Zip, State: String;
begin
  Zip := ExpandConstant('{app}\addon\{#ExtId}.zip');
  State := ExpandConstant('{app}\' + StateFile);
  WizardForm.StatusLabel.Caption := 'Installing Blender add-on into Blender ' + Ver + '...';
  if Exec(Exe, '--background -c extension install-file -r user_default -e "' + Zip + '"',
          '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0) then
  begin
    SetIniString('blender', 'exe' + IntToStr(Slot), Exe, State);
    Results := Results + '  Blender ' + Ver + ': installed and enabled' + #13#10;
  end
  else if (Ver <> '?') and (Ver <> 'Steam') then
  begin
    { Fallback: copy the files; the user enables it in Preferences > Add-ons. }
    ForceDirectories(RoamingExtDir(Ver));
    Exec(ExpandConstant('{cmd}'), '/C xcopy /E /I /Y /Q "' + ExpandConstant('{app}\addon\{#ExtId}') + '" "' +
         RoamingExtDir(Ver) + '"', '', SW_HIDE, ewWaitUntilTerminated, Code);
    SetIniString('blender', 'dir' + IntToStr(Slot), RoamingExtDir(Ver), State);
    Results := Results + '  Blender ' + Ver + ': copied - enable "Blender MCP Bridge (C#)" in Preferences > Add-ons' + #13#10;
  end
  else
    Results := Results + '  Blender ' + Ver + ': FAILED (exit code ' + IntToStr(Code) + ')' + #13#10;
end;

{ Registration runs from code (not [Run]) so a failure is reported instead of silently ignored. }
procedure RegisterWithClaude;
var
  Code: Integer;
begin
  ClaudeResult := '';
  if not WizardIsTaskSelected('claude') then
    Exit;
  WizardForm.StatusLabel.Caption := 'Registering with Claude Desktop...';
  if Exec(ExpandConstant('{app}\server\{#ServerExe}'), '--register --name {#McpName} --port ' + GetPort(''),
          '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0) then
    ClaudeResult := 'Restart Claude Desktop to load the "{#McpName}" MCP server.'
  else
    ClaudeResult := 'Claude Desktop: registration FAILED (code ' + IntToStr(Code) + '). Check that claude_desktop_config.json ' +
      'is valid JSON and not locked, then run "' + ExpandConstant('{app}\server\{#ServerExe}') + '" --register --name {#McpName}';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  I: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    Results := '';
    for I := 0 to GetArrayLength(BlenderExes) - 1 do
      if BlenderPage.Values[I] then
        InstallIntoBlender(BlenderExes[I], BlenderVers[I], I);
    RegisterWithClaude;
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  I: Integer;
  S: String;
begin
  S := '';
  for I := 0 to GetArrayLength(BlenderExes) - 1 do
    if BlenderPage.Values[I] then
      S := S + Space + 'Blender ' + BlenderVers[I] + NewLine;
  if S = '' then
    S := Space + '(none)' + NewLine;
  Result := MemoDirInfo + NewLine + NewLine + 'Blender add-on:' + NewLine + S;
  if MemoTasksInfo <> '' then
    Result := Result + NewLine + MemoTasksInfo;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID <> wpFinished then
    Exit;
  if Results <> '' then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 + 'Blender:' + #13#10 + Results;
  if ClaudeResult <> '' then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 + ClaudeResult;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  I, Code: Integer;
  State, Exe, Dir: String;
begin
  if CurUninstallStep <> usUninstall then
    Exit;
  KillServer;  { only once the user has confirmed; files in use can't be removed }
  State := ExpandConstant('{app}\' + StateFile);
  for I := 0 to 63 do
  begin
    Exe := GetIniString('blender', 'exe' + IntToStr(I), '', State);
    if (Exe <> '') and FileExists(Exe) then
      Exec(Exe, '--background -c extension remove user_default.{#ExtId}', '', SW_HIDE, ewWaitUntilTerminated, Code);
    Dir := GetIniString('blender', 'dir' + IntToStr(I), '', State);
    if (Dir <> '') and DirExists(Dir) then
      DelTree(Dir, True, True, True);
  end;
end;
