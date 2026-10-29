; ─── Gusan — Inno Setup installer ───────────────────────────────────────────
; Builds a single TranscribeApp-Setup.exe that installs the app, registers it
; in Add/Remove Programs, optionally creates a desktop shortcut + start-at-login entry, and
; closes any running instance during silent updates.
;
; Gusan was called Transcribe until 2.0.0. Everything people see says Gusan
; (MyAppName). What existing installs depend on keeps its old name: AppId,
; TranscribeApp.exe, the task names, the data folders {userappdata}\Transcribe
; and {localappdata}\Transcribe (storage.APP_DIR_NAME) and the start-at-login
; Run value (MyRunValueName).

#define MyAppName "Gusan"
; The HKCU Run value name - must equal autostart.VALUE_NAME, the value the app
; itself writes and removes. Never rename it: a second name would start the
; app twice and lose a Task Manager "Disabled" switch.
#define MyRunValueName "Transcribe"
; The name 1.x installers gave the shortcuts (removed in [InstallDelete]).
#define MyOldAppName "Transcribe"
#define MyAppPublisher "Aram Adamyan"
#define MyAppPublisherURL "https://aibuben.xyz"
#define MyAppURL "https://github.com/Aram2K/Gusan"
#define MyAppExeName "TranscribeApp.exe"

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

; The wizard pictures are PNG files and the slideshow loads PNG through
; TBitmapImage.PngImage: both need Inno Setup 6.5.2 or newer (CI has 6.7).
#if VER < EncodeVer(6,5,2)
  #error Inno Setup 6.5.2 or newer is needed to compile this script (PNG wizard pictures)
#endif

[Setup]
AppId={{8E3B7C8A-9D54-4F61-9F6C-2E8C7F0A1B23}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppPublisherURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
; New installs go to ...\Gusan; an upgrade keeps its folder (...\Transcribe).
DefaultDirName={autopf}\{#MyAppName}
UsePreviousAppDir=yes
; The Start Menu folder becomes "Gusan" on upgrades too; the 1.x "Transcribe"
; folder and its shortcuts are removed in [InstallDelete].
DefaultGroupName={#MyAppName}
UsePreviousGroup=no
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputBaseFilename=TranscribeApp-Setup
OutputDir=.
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
CloseApplications=force
RestartApplications=yes
; Don't ask the user to choose components/etc — make the install fast.
DisableReadyPage=yes
DisableFinishedPage=no
ShowLanguageDialog=no
; Pictures (installer\wizard, drawn by installer\make_wizard_art.py). The large
; one is on the finish page only: this installer has no welcome page. The small
; one is at the top right of every other page. Setup picks the file that fits
; the screen scale best, so there is one file per scale.
WizardImageFile=wizard\finish_202x386.png,wizard\finish_269x515.png,wizard\finish_336x643.png,wizard\finish_403x772.png,wizard\finish_430x824.png,wizard\finish_498x953.png,wizard\finish_534x1022.png
WizardSmallImageFile=wizard\small_58.png,wizard\small_70.png,wizard\small_77.png,wizard\small_97.png,wizard\small_116.png,wizard\small_124.png,wizard\small_143.png,wizard\small_159.png

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
; The finish page. Setup adds "Click Finish to exit Setup." under the text.
FinishedHeadingLabel={#MyAppName} is ready
FinishedLabel=Press Alt + R in any app and talk.
FinishedLabelNoIcons=Press Alt + R in any app and talk.

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
; Fresh installs only: after that the app's own Settings checkbox owns the
; setting, and an update's wizard must not silently re-tick or untick it.
Name: "startup";     Description: "Start {#MyAppName} automatically when Windows starts"; GroupDescription: "Startup options:"; Check: not IsUpgrade

[InstallDelete]
; Upgrades from 1.x: the shortcuts were called "Transcribe". The new ones are
; called "Gusan" ([Icons]), so without this the Start Menu and the desktop
; would show both. {autoprograms}\Transcribe was the 1.x {group}.
Type: files; Name: "{autoprograms}\{#MyOldAppName}\{#MyOldAppName}.lnk"
Type: files; Name: "{autoprograms}\{#MyOldAppName}\Uninstall {#MyOldAppName}.lnk"
Type: dirifempty; Name: "{autoprograms}\{#MyOldAppName}"
Type: files; Name: "{autodesktop}\{#MyOldAppName}.lnk"
; Deliberately NOT the Startup-folder shortcut that installers before 1.9.4
; made ({userstartup}\Transcribe.lnk): the app moves it to the Run value on
; its first launch and keeps Task Manager's on/off switch with it
; (autostart.LEGACY_SHORTCUT). Deleting it here would quietly turn start at
; login off for those users. This installer never creates a startup shortcut.

[Files]
; The slideshow pictures: compiled into Setup but never installed (dontcopy).
; [Code] extracts them to {tmp} while the Installing page shows; Setup deletes
; them when it exits. They stay at the top of this section on purpose: with
; solid compression Setup must unpack every file listed above the one it
; extracts. A PNG is compressed already, so it is stored as it is
; (nocompression); solidbreak starts their own block.
; The list (and the frame times in [Code]) follows installer\wizard\slides.json.
Source: "wizard\slide1_1.png"; Flags: dontcopy noencryption nocompression solidbreak
Source: "wizard\slide1_2.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide1_3.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide1_4.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide1_5.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide1_6.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide2_1.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide2_2.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide2_3.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide2_4.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide2_5.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide2_6.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide3_1.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide3_2.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide3_3.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide3_4.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide3_5.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide3_6.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide4_1.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide4_2.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide4_3.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide4_4.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide4_5.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide5_1.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide5_2.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide5_3.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide5_4.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide5_5.png"; Flags: dontcopy noencryption nocompression
Source: "wizard\slide5_6.png"; Flags: dontcopy noencryption nocompression
Source: "..\dist\TranscribeApp\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}";        Filename: "{app}\{#MyAppExeName}"; Parameters: "--show-settings"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"; Parameters: "--show-settings"; Tasks: desktopicon

[Registry]
; Start at login: the same HKCU Run value the app manages from Settings
; (autostart.py), so Task Manager > Startup apps lists and toggles it. Only a
; fresh install writes it (the task exists only then) - so it also runs at the
; next login if the app isn't opened before. Per-user installs only: an
; all-users install may run elevated as ANOTHER account, whose HKCU this would
; be; there each user's first launch registers from {app}\autostart.default.
; The old Startup-folder shortcut (installers before 1.9.4) is migrated by the
; app, which first reads whether Task Manager had it switched off.
; The value keeps its 1.x name (MyRunValueName = autostart.VALUE_NAME); Task
; Manager shows "Gusan" from the exe's version info.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyRunValueName}"; ValueData: """{app}\{#MyAppExeName}"" --background"; Tasks: startup; Check: not IsAdminInstallMode

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--show-settings"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{userappdata}\Transcribe\action_models"
; The optional NVIDIA GPU acceleration download (gpu_accel.py, ~736 MB) -
; kept in Local AppData so it never syncs with a roaming profile.
Type: filesandordirs; Name: "{localappdata}\Transcribe\gpu"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-tiny"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-base"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-small"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-medium"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-large-v3-turbo"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-large-v3"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"

[Code]
// The slideshow below the progress bar of the Installing page is the only
// thing here that is just for looks, so it is built to fail quietly: nothing
// of it runs in /SILENT or /VERYSILENT, no error leaves the timer, and every
// way out of Setup (the next page, the end of the install, Cancel) stops it.
// There is no timer class in Pascal Script, so the Windows timer is used, the
// way the Inno Setup example CodeDll.iss does it.
function SetTimer(hWnd: HWND; nIDEvent: UINT_PTR; uElapse: UINT; lpTimerFunc: NativeInt): UINT_PTR;
  external 'SetTimer@user32.dll stdcall';
function KillTimer(hWnd: HWND; nIDEvent: UINT_PTR): BOOL;
  external 'KillTimer@user32.dll stdcall';

var
  AnalyticsPage: TInputOptionWizardPage;
  UpgradeInstall: Boolean;
  SlideTimer: UINT_PTR;
  SlideImage: TBitmapImage;
  SlideFiles: array of String;
  SlideTicks: array of Integer;
  SlideCount: Integer;
  SlideIndex: Integer;
  SlideWait: Integer;

// An earlier version is installed in this install mode: this run updates it.
function IsUpgrade(): Boolean;
begin
  Result := UpgradeInstall;
end;

function FindPreviousInstall(): Boolean;
var
  Key, Uninst: string;
  RootKey: Integer;
begin
  Key := ExpandConstant('Software\Microsoft\Windows\CurrentVersion\Uninstall\{#emit SetupSetting("AppId")}_is1');
  // Only the hive of THIS install mode: another account's all-users install
  // doesn't make a per-user install an update (and vice versa).
  if IsAdminInstallMode then
    RootKey := HKLM
  else
    RootKey := HKCU;
  Uninst := '';
  RegQueryStringValue(RootKey, Key, 'UninstallString', Uninst);
  Result := Uninst <> '';
end;

// Stops the timer. Safe to call at any time, any number of times. The global
// is cleared first, so a timer message that is still queued finds 0 and does
// nothing (KillTimer does not remove messages that are already posted).
procedure StopSlideTimer;
var
  TimerId: UINT_PTR;
begin
  TimerId := SlideTimer;
  SlideTimer := 0;
  if TimerId <> 0 then
    KillTimer(0, TimerId);
end;

// Stops the timer and releases the picture control. Never raises.
procedure StopSlides;
var
  OldImage: TBitmapImage;
begin
  StopSlideTimer;
  OldImage := SlideImage;
  SlideImage := nil;
  if OldImage <> nil then
  begin
    try
      OldImage.Free;
    except
      Log('Slideshow: ' + GetExceptionMessage);
    end;
  end;
end;

// One picture of the slideshow and how long it stays, in ticks of 100 ms.
procedure AddSlide(Pic: String; Ticks: Integer);
begin
  SetArrayLength(SlideFiles, SlideCount + 1);
  SetArrayLength(SlideTicks, SlideCount + 1);
  SlideFiles[SlideCount] := Pic;
  SlideTicks[SlideCount] := Ticks;
  SlideCount := SlideCount + 1;
end;

// installer\wizard\slides.json: 5 slides (Dictation, Smart Actions, Meetings,
// Live Assistant, Audio to text), each shown as a few pictures in a row,
// 6 seconds a slide. The last picture of a slide stays the longest.
procedure LoadSlideList;
begin
  SlideCount := 0;
  AddSlide('slide1_1.png', 6);
  AddSlide('slide1_2.png', 6);
  AddSlide('slide1_3.png', 7);
  AddSlide('slide1_4.png', 7);
  AddSlide('slide1_5.png', 7);
  AddSlide('slide1_6.png', 27);
  AddSlide('slide2_1.png', 6);
  AddSlide('slide2_2.png', 6);
  AddSlide('slide2_3.png', 9);
  AddSlide('slide2_4.png', 9);
  AddSlide('slide2_5.png', 9);
  AddSlide('slide2_6.png', 21);
  AddSlide('slide3_1.png', 8);
  AddSlide('slide3_2.png', 9);
  AddSlide('slide3_3.png', 10);
  AddSlide('slide3_4.png', 9);
  AddSlide('slide3_5.png', 11);
  AddSlide('slide3_6.png', 13);
  AddSlide('slide4_1.png', 7);
  AddSlide('slide4_2.png', 8);
  AddSlide('slide4_3.png', 10);
  AddSlide('slide4_4.png', 15);
  AddSlide('slide4_5.png', 20);
  AddSlide('slide5_1.png', 8);
  AddSlide('slide5_2.png', 7);
  AddSlide('slide5_3.png', 11);
  AddSlide('slide5_4.png', 8);
  AddSlide('slide5_5.png', 15);
  AddSlide('slide5_6.png', 11);
end;

// Shows picture N (already extracted to {tmp}) and starts its time.
procedure ShowSlide(N: Integer);
begin
  SlideImage.PngImage.LoadFromFile(ExpandConstant('{tmp}') + '\' + SlideFiles[N]);
  SlideWait := SlideTicks[N];
end;

// Runs every 100 ms, but only while Setup handles its messages (between two
// installed files), so a very large file just holds the picture for a moment.
// An error here must never reach Windows: the slideshow stops and the last
// picture stays.
procedure SlideTimerProc(Arg1: HWND; Arg2: UINT; Arg3: UINT_PTR; Arg4: DWORD);
begin
  if (SlideTimer = 0) or (SlideImage = nil) or (WizardForm = nil) then
    Exit;
  try
    SlideWait := SlideWait - 1;
    if SlideWait <= 0 then
    begin
      SlideIndex := SlideIndex + 1;
      if SlideIndex >= SlideCount then
        SlideIndex := 0;
      ShowSlide(SlideIndex);
    end;
  except
    StopSlideTimer;
  end;
end;

// Called when the Installing page appears, before the first file is copied.
// Not in silent installs. Any error just means: no slideshow.
procedure StartSlides;
var
  I, W, H, T, Room: Integer;
begin
  if WizardSilent then
    Exit;
  if SlideTimer <> 0 then
    Exit;
  try
    // The pictures are 5:2. The size comes from the progress bar and the page
    // (the wizard is bigger than its design size and the sizes change with
    // the screen scale): as wide as the bar if there is room, else smaller.
    W := WizardForm.ProgressGauge.Width;
    T := WizardForm.ProgressGauge.Top + WizardForm.ProgressGauge.Height + ScaleY(10);
    Room := WizardForm.InstallingPage.Height - T - ScaleY(4);
    H := W * 2 div 5;
    if H > Room then
      H := Room;
    W := H * 5 div 2;
    if H >= ScaleY(100) then
    begin
      LoadSlideList;
      for I := 0 to SlideCount - 1 do
        ExtractTemporaryFile(SlideFiles[I]);
      SlideImage := TBitmapImage.Create(WizardForm);
      SlideImage.Stretch := True;
      SlideImage.Left := WizardForm.ProgressGauge.Left + (WizardForm.ProgressGauge.Width - W) div 2;
      SlideImage.Top := T;
      SlideImage.Width := W;
      SlideImage.Height := H;
      SlideImage.Parent := WizardForm.InstallingPage;
      SlideIndex := 0;
      ShowSlide(0);
      SlideTimer := SetTimer(0, 0, 100, CreateCallback(@SlideTimerProc));
    end;
  except
    Log('Slideshow: ' + GetExceptionMessage);
    StopSlides;
  end;
end;

procedure InitializeWizard;
begin
  AnalyticsPage := CreateInputOptionPage(
    wpSelectTasks,
    'Privacy',
    'Anonymous usage analytics',
    'Help improve {#MyAppName} by sharing safe usage events. Analytics is optional. It never includes audio, transcription text, clipboard content, API keys, file paths, microphone names, or window titles.',
    False,
    False
  );
  AnalyticsPage.Add('Share anonymous usage analytics (recommended)');
  AnalyticsPage.Values[0] := True;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpInstalling then
    StartSlides
  else
  begin
    // Any other page: the slideshow is over (no-op if it never started).
    StopSlides;
    // The finish page of an update says so. Both headings are one line, so
    // the text under it keeps its place.
    if (CurPageID = wpFinished) and IsUpgrade then
      WizardForm.FinishedHeadingLabel.Caption := '{#MyAppName} is updated';
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ConsentDir, Choice: string;
begin
  // The install is done: the slideshow ends before anything else happens here.
  if CurStep = ssPostInstall then
    StopSlides;
  if (CurStep = ssPostInstall) and (not IsUpgrade) then
  begin
    // The start-at-login box, next to the exe (autostart.INSTALL_DEFAULT_NAME)
    // so every user of an all-users install gets it. The timestamp makes each
    // install's choice new to a config left by an earlier install; each user's
    // first launch adopts it once. Removed with {app} on uninstall.
    if WizardIsTaskSelected('startup') then
      Choice := 'on'
    else
      Choice := 'off';
    SaveStringToFile(ExpandConstant('{app}\autostart.default'),
      Choice + ' ' + GetDateTimeString('yyyy-mm-dd hh:nn:ss', '-', ':'), False);
    // Ticked on purpose: no stale Task Manager switch-off (e.g. from the
    // portable zip) may keep the fresh entry disabled.
    if (Choice = 'on') and (not IsAdminInstallMode) then
      RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run', '{#MyRunValueName}');
  end;
  if (CurStep = ssPostInstall) and (not WizardSilent) then
  begin
    ConsentDir := ExpandConstant('{userappdata}\Transcribe');
    ForceDirectories(ConsentDir);
    // Always write the marker once the wizard has been seen; the app uses
    // it only to set analytics_consent_applied. The user's actual choice
    // is reflected by the default analytics_enabled value and the in-app
    // Settings toggle.
    SaveStringToFile(ConsentDir + '\analytics_consent.accepted', 'accepted', False);
    if not AnalyticsPage.Values[0] then
    begin
      // User opted out during install: write a declined marker so the
      // app can honour it on first launch.
      SaveStringToFile(ConsentDir + '\analytics_consent.declined', 'declined', False);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  RunValue: string;
begin
  // The app may have written the Run value itself (Settings toggle), so
  // remove it whether or not the install task created it - but only when it
  // starts THIS install, not a portable copy or another install.
  // Known gap: on an all-users install each user's Run value lives in their
  // own HKCU; only the uninstalling account's is removed here. Others are left
  // pointing at the removed exe, which Windows simply skips at sign-in.
  if (CurUninstallStep = usUninstall) and
     RegQueryStringValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#MyRunValueName}', RunValue) and
     (Pos(Lowercase(ExpandConstant('{app}\{#MyAppExeName}')), Lowercase(RunValue)) > 0) then
  begin
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#MyRunValueName}');
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run', '{#MyRunValueName}');
  end;
end;

function InitializeSetup(): Boolean;
begin
  // Decided once, before anything is written (the uninstall key of THIS
  // install appears at the end of it).
  UpgradeInstall := FindPreviousInstall();
  Result := True;
end;

// Runs on every way out of Setup, also after Cancel (then neither ssPostInstall
// nor the finish page is reached).
procedure DeinitializeSetup;
begin
  StopSlides;
end;
