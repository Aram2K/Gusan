# Gusan 2.0.0 — implementation plan

Branch: `prep-2.0.0` (from `main` at v1.9.4, 0d80157). Work stays uncommitted until the founder approves commits.
Visual spec: the approved mockups (Design canvas "Gusan · all pages"; source modules in the redesign scratchpad `v2/pages/*.py`,
library `v2/gusan_ds.py`, brief `v2/BRIEF.md` §0 and `v2/CALENDAR.md`). Integration maps: `v2/map/*.json` (file:line for every hook).
Baseline: 635 tests passed, 110 skipped (`venv\Scripts\python.exe -m pytest -q tests`, ~14.5 min).

## 1. What 2.0.0 is

- One main window "Gusan": sidebar (Home, Dictation, Smart Actions PRO, Meetings PRO, Live Assistant PRO, Audio to text, Dictionary;
  account row + one quiet Upgrade; Settings). Each feature page carries its own settings in a right-hand panel; changes apply instantly.
- Slim Settings (General, Microphone, Speech & AI, Privacy & updates) and Account & plan.
- New 5-step onboarding (Hello → speech model with "Best for your computer" → mic check → try Alt+R → sign up with Google or email).
- Real model names under plain names everywhere. Calm Pro: PRO tags, one sidebar Upgrade, the current Pro window (renamed only).
- Audio to text (unlimited, friendly), Live Assistant page that makes "hidden on screen share" obvious (incognito icon, "Hidden" chip).
- Google Calendar: Coming up list, per-meeting bell, reminder toast (1 min before → 5 min after), recording prefilled from the calendar.
- User-visible rename Transcribe → Gusan. Everything existing installs depend on keeps its name (data folder, keyring, AppId,
  `TranscribeApp.exe`, `TranscribeApp-Windows-Setup.exe`, repo, port 47823 + handshake, function slugs, config keys, telemetry names).

## 2. Architecture (pragmatic: reuse the proven backend)

AppController (main.py) stays the one brain. New pieces:

| Piece | File(s) | Role |
|---|---|---|
| App info | `app_info.py` | APP_NAME "Gusan", APP_VERSION "2.0.0", resource_path; no Qt |
| Theme | `ui/theme.py`, `assets/fonts/` | Tokens from the mockups (current app colours), QSS for v2 windows, font loading |
| Components | `ui/components/*.py` | Sidebar, PageHeader, SettingsRail/SettingRow, ProTag/FreeBadge, Card/GlassCard, Toggle, Keycaps, Chip, Segmented, ModelCard, Toast host |
| Shell | `ui/shell.py` | QMainWindow: sidebar + lazy QStackedWidget page factories (error card per page), routing `open(route, **opts)` |
| Instant apply | main.py `set_setting/set_settings`, `sig_setting_changed` | Write cfg in place, debounced save (250 ms), side-effect table (hotkeys, privacy, autostart, model preload, overlay) |
| Models | `hardware.py`, `model_manager.py` | Hardware snapshot + recommendation rule; one shared download manager (dedupe, progress, finished) |
| Keys/engines/account | `api_keys.py`, `engine_choice.py`, `account_actions.py`, `history_actions.py`, `updater.py` | Logic lifted out of ui/settings.py so pages never build the old window |
| Files | `file_jobs.py`, `transcript_store.py` | Qt-free job runner + FileJobController; transcripts saved and listed |
| Meetings | `meeting_session.py` (+ `meeting_store.py` additions) | Recording engine without widgets; pause, two meters, my notes, metadata |
| Calendar | `calendar_sync.py` (extended), `calendar_service.py` | Poller, cached Coming up, reminder signal, saved prompted/muted state |
| Pages | `ui/pages/{home,dictation,smart_actions,meetings,live_assistant,audio_to_text,dictionary,settings,account}.py` | The mockups, bound to the services |
| Onboarding | `ui/onboarding.py` (rewritten), `ui/auth_sheet.py`, `mic_monitor.py` | The 5-step wizard; sign-in sheet reused by show_auth_gate |
| Floating | `ui/live_assist.py` (Hidden chip), `ui/overlay.py` (rename), `ui/floating/meeting_reminder.py`, `ui/tray_menu.py` | |
| Retired at the end | `ui/settings.py`, `ui/history.py`, `ui/meeting_detail.py`, old MeetingsWindow UI, `ui/shiny_tabs.py`, `ui/mode_combo.py` | After every caller moved; tests retargeted. Wave 4: done - nothing imports them (plus `ui/onboarding.py`, `ui/file_transcribe.py`; guard: tests/test_routes_surfaces.py TestRetiredWindows); the kept helpers live in `ui/recording_prompts.py` and `meeting_share.py`. The files themselves wait for the founder's OK to delete |

## 3. Waves (file ownership keeps parallel agents from colliding)

1. **Foundation** (sequential): app_info, theme + fonts, components, shell with placeholder pages, controller routing
   (`show_main`, every `show_*`, tray default, IPC), set_setting pipeline, `setApplicationName("Gusan")`, version 2.0.0,
   `tests_qt/` (offscreen smoke tests) and the PNG renderer for visual review. Gate: tests green, shell renders, app starts.
2. **Services** (parallel, no page UI): A hardware+model_manager · B api_keys+engine_choice+account_actions+history_actions+updater ·
   C file_jobs+transcript_store · D meeting_session+meeting_store (only agent allowed in main.py's AudioRecorder) ·
   E calendar_sync+calendar_service · F live card Hidden chip + incognito icon + overlay/actions rename. Each with pure tests.
3. **Pages** (parallel, one owner per file): home+dictation · smart_actions+dictionary · meetings · live_assistant ·
   audio_to_text · settings+account · onboarding+auth_sheet+mic_monitor · meeting_reminder+tray_menu.
4. **Integration** (sequential): wire services/pages into AppController, tray v2, calendar reminders, onboarding hooks, remove the
   per-use tries balloon, retire old windows, retarget tests.
5. **Review loop**: full tests + tests_qt; every page rendered offscreen and compared with its mockup by a critic; fixes; dev build
   started with a temporary profile to walk onboarding and every page; then the real dev build restarted.
6. **Ship prep**: installer (name split, InstallDelete old shortcuts, Run value kept), build.yml (version resource, Mac plist names),
   release notes `docs/release-notes/v2.0.0.md`, guard tests (no old name on screen, kept identifiers, updater asset). No tag/push
   without the founder.

Every wave ends with: full test suite, tests_qt, a reviewer agent, and fixes before the next wave starts.

## 4. Defaults chosen (the founder can change any)

| Topic | Default for 2.0.0 |
|---|---|
| Headline font | Outfit if the founder OKs bundling it (Google Fonts, OFL licence); until then Segoe UI Variable Display (Windows) / system font (Mac) |
| Google Calendar | UI built; Connect is enabled only when a Google OAuth "Desktop app" client id/secret is configured (today it is blank). Needs the founder to create one in Google Cloud Console with the Calendar API on |
| Calendar window | 12 hours (as the code); no "Tomorrow" rows |
| "Only meetings with a call link" | New filter in calendar_sync (Meet / Zoom / Teams / Webex links) |
| Tray | Keeps one "Upgrade to Pro…" row for Free users |
| Usage stats in onboarding | On by default with neutral wording (today's behaviour), easy to untick |
| Sign-up headline | "Keep your words." kept until the founder changes it |
| Dictation bar tries line | Removed (counter lives on the Smart Actions page) |
| File Summary tab | Not in 2.0.0 (transcript view: Copy, Export, Save Word) |
| Mac .app / bundle id | Unchanged (TranscribeApp.app); menu bar, prompts and DMG volume say Gusan |
| Auto account gate at launch | Off for existing users (onboarding covers new users) |
| Old Settings window | Retired; `show_settings` opens the new Settings page |

## 5. Risks and how they are handled

- main.py is shared by every area → only Wave 1 and Wave 4 edit the controller; Wave 2 D edits only AudioRecorder.
- Meeting engine extraction can break recording → characterization tests (test_meeting_library harness) run before and after.
- Config is a module-level dict read by AudioRecorder → set_setting mutates in place, never rebinds.
- Updater must keep working for 1.9.x users → asset name, repo and tag format unchanged; guard test.
- CI stubs PySide6 in tests/ → new pure modules stay Qt-free; Qt tests live in tests_qt/ (separate run).
