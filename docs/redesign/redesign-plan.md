# Gusan redesign plan (v1.10 to v1.14)

This is for the founder's approval. It pulls together three design concepts, three reviews of them, six audits of the current app, four research briefs and the usage data.

- The structure comes from the **Library-centric** concept, which two of the three reviewers picked.
- From the **Hub** concept it takes the truthful status line, the full mapping of old to new, the accessibility plan and the rule that each setting has one home.
- From the **Tray-first** concept it takes the "Foundations first" release, the Now section and the command search.

**Facts I checked in the code (read-only, working-copy line numbers):**

**Language modes**
- **Auto-detect and Multilingual are not the same.** In live on-device dictation, `"auto"` detects the language once per dictation and keeps it (`_session_lang`, main.py:1660-1673, reset per recording at 1057). `"multi"` detects again on every chunk. In file transcription and Voxtral the two behave the same (main.py:1571, 1864).
- **"Clean up transcriptions" never adds punctuation or capitals** (text_cleanup.py docstring). It removes noise tags like `[BLANK_AUDIO]` and repeated invented phrases. By contrast, the Gemini and managed-cloud (Gusan Cloud in the new app) speech prompts do ask for punctuation (main.py:1756; transcribe-proxy:62).

**Privacy**
- **Privacy mode skips saving dictation history** (main.py:3532) and the meeting's history copy (ui/meetings.py:1356). It still saves meeting folders. Two places wrongly say otherwise: the load_config comment (main.py:230-232) and the Settings tooltip.
- **Privacy mode does *not* stop usage statistics** (telemetry.py:120-127). Notion sharing asks first rather than being blocked (ui/meetings.py:1492-1500). Live Assist falls back to on-device AI or nothing, and takes no screenshots (actions.py:420; live_assist.py:1191).

**Keys and accounts**
- **API keys live in the OS keyring** (storage.py; `_SECRET_CFG_KEYS` in main.py). They are scoped per account, together with the engine choices, through `user_secrets` and `secrets_owner` (entitlements.py:217-270). The Notion token is plain text in config.json.
- `managed_provider` defaults to gemini at runtime (main.py:1802; entitlements.py:224). It isn't in DEFAULT.
- `google_stt_model` has no UI. `mistral_stt_model` is set only by the single Voxtral card (settings.py:1435-1442).
- Config-only keys the concepts missed: `action_api_model_recap` and `action_api_reasoning_effort` (action_api.py:284, 801). The legacy key `live_assist_auto` exists (main.py:147). Guest seconds and Smart Action counters live in `usage.json`.
- `last_known_pro` is a `{user_id: bool}` map used only for the `pro_activated` event (main.py:2311-2330).
- Hotkeys can be mouse buttons (main.py:2854).
- `PRO_FEATURES` (entitlements.py:43) covers only meetings, Smart Actions and cloud. Live Assist is gated by `is_pro()` (live_assist.py:1026) and long files by constants (file_transcribe.py:31-32).

**Engines and cloud**
- **Pro Live Assist always uses the managed cloud (Gusan Cloud in the new app)** (actions.py:410-420). Cloud speech falls back to on-device on errors (`_fallback_to_local_or_error`).
- **The cloud cap in the repo is 3000 requests per user per day** (transcribe-proxy:24, 112). A monthly-seconds function exists in the migrations but isn't called.

**Meetings and files**
- **Resource rules:** dictation is refused during a meeting (main.py:3046-3052) and during a file job (3132-3140). A meeting is refused during a file job (ui/meetings.py:658-664). Starting a meeting stops any running dictation (668).
- **Meetings keep at most about 2 hours of audio.** Past that, the transcript is kept but there are no speaker labels (ui/meetings.py:1044-1050, 1117-1124).
- **"Your notes" typed during a meeting go to the notes AI but are never saved** (ui/meetings.py:958, 1219).

**Other things already in the working copy**
- The dictation bar is draggable, remembered and double-click-to-home (ui/overlay.py).
- The billing portal is already wired (main.py `billing_portal_session`, uncommitted).
- The release manifest already carries a macOS DMG with a sha256 (build.yml:387-392).

---

## 1. Executive summary

**The new app in one paragraph.** The app becomes one calm main window called **Gusan**.

- **Home** is a single timeline of everything you have captured: dictations, meetings, Live Assist sessions and files, with full-text search.
  - Above the list is a one-line **status card** ("● Ready · Alt+R · On this computer · Detect language") with the three capture actions.
  - A sticky **Now** section shows anything live, processing or failed.
- **Sidebar:** Home, Dictations, Meetings, Files, Dictionary. Settings is pinned at the bottom, with the plan chip and live status in the footer.
- **Capture happens outside the window**, on two floating surfaces: the **dictation bar**, and a **meeting capsule** that expands into the **Live Assist card**. Every capture becomes a Library item the moment it starts.
- **The tray** shows status and launches actions.
- **Settings** is eight short sections that apply changes instantly.
- **Upgrades** go through one **Pro sheet** that names the feature you tried and resumes that action after checkout.
- **Delivery:** five releases, starting with a 7-9-week **Foundations** release. It fixes updates, first run, hotkeys and status accuracy before any navigation moves. That order matters because a redesign today couldn't reach users: 6 of 6 non-admin installs are out of date.

**The 10 biggest changes**

| # | Today | After |
|---|---|---|
| 1 | The app's home is a dialog titled "Settings" with 7 tabs that mix tools and preferences | A main window "Gusan". Home shows status plus everything you captured. Settings holds preferences only. |
| 2 | Results live in 5 places. File results and Live Assist answers are never saved. Failed dictations are lost. Meeting "Your notes" are never saved. | One Library with one item model (live, processing, ready, failed), one detail pane, one Export menu and search that looks inside transcripts |
| 3 | Meetings is its own window. Live Assist drives it while it is hidden. Notes land in a window you never see. | One meeting session: Record sheet, then a live Library item plus a capsule. Live Assist is the expanded capsule. Notes land in the same item. |
| 4 | A Save button, a stale snapshot that can overwrite newer settings, and meeting pickers that rewrite global keys | Instant apply through one ConfigStore. Each setting has one home; other places show its value and link to it. |
| 5 | 7+ engine pickers, 5 key fields, one key slot shared by 4 providers, and "Local" shown while text goes to the cloud | "Automatic" by default with a truthful "Now using…" line, one list of API keys (one per provider, still in the OS keyring and per account), and the details under Advanced |
| 6 | Onboarding starts with an account wall and engine jargon, has no mic test, and downloads 140 MB silently | First run: mic meter, visible model download, and a practice dictation ending in a real paste. The account step is optional and comes after the first success. macOS permissions are checked live. |
| 7 | An update modal appears at launch and after every dictation, and updating is broken on Mac | Background download, a banner plus a tray "Restart to update" item, install on quit, "Skip this version", What's new, and a working Mac path |
| 8 | 13 upsell points and glows. Paying users show as Free, and get blocked, when offline. | One plan chip, a small "Pro" tag on gated actions, one Pro sheet at the moment of value, and a timestamped Pro cache with a 7-day offline grace period |
| 9 | A static tray icon with account rows, "(Pro)" suffixes and no recording state | A status tray: recording row with Stop, Privacy shield, Recent, Paste last dictation, and a Quit that asks first while recording |
| 10 | 68 hex colours, light only, 10-11 px text, white on #3b82f6 at 3.68:1 | Design tokens: one accent (5.17:1), light and dark, 12 px minimum, 4.5:1 text contrast, glass only on floating surfaces |

---

## 2. Principles

| # | Principle | Evidence | What it forces |
|---|---|---|---|
| P1 | **Dictation is the product; nothing may slow it down.** | Dictation was 22 of 22 non-admin feature events in 90 days (the rest were launches). Of all 194 logged dictations (most from the founder), 97% were English and 45% were 100 characters or fewer. How often non-admin users open windows is unknown, because Settings events weren't logged on 1.5.36-1.8.x. | Hotkey, bar and tray carry daily use. The window opens by itself only on first run. Language and engine are never asked at setup. The dictation bar gets the most polish. File jobs must not lock out dictation for their whole length (D18). |
| P2 | **Fix reach before redesign.** | 6 of 6 non-admin installs are behind (2 on 1.5.47). 3 of 7 installs never dictated, including both Macs. The usage drop in June came alongside a run of `timeout_120s` failures, last seen on 1.6.15 and gone since 1.7.0; stuck installs never got the fix. The server allowlist fix catches events from 1.7.0+ clients once deployed, but 1.5.x clients never send them. | v1.10 Foundations (updater, first run, hotkeys, status accuracy, instrumentation) ships before any navigation change. Later phases are gated on its data. |
| P3 | **Everything you capture becomes an item, right away.** | Five result surfaces. File results are wiped when you pick a new file. Live Assist answers are never stored. Edits to notes are thrown away. "Your notes" typed during a meeting are never saved. Meetings are stored twice. Competitors (Wispr, Superwhisper, MacWhisper) all use one history. | One `library_store`, one item model, one detail pane per kind, one exporter. Failed dictations keep their audio for Retry, except in Privacy mode. |
| P4 | **Doing is separate from configuring; one home per setting; changes apply instantly.** | Microsoft guidance: no workflows in Settings and no confirm button. The `cfg_working` snapshot can overwrite newer settings. Save and instant-apply controls sit side by side. Meeting pickers rewrite `action_model`. | Tools leave Settings. No Save button anywhere. A ConfigStore with change signals. Mirrors write the same key and never keep their own copy. |
| P5 | **Status is always truthful.** | Pro users are shown "Local" while text goes to the cloud. "✓ Working (Saved)" appears on keys nobody tested. "Pasted" appears when nothing was focused. Privacy mode is one-way, and its "everything stays on your device" copy ignores usage statistics. Paying users show as Guest, then Free, when offline. | One engine resolver drives both the runtime and the UI. "Now using" lines, honest paste results, honest cloud-fallback notes, and Privacy mode as a layer that restores your previous choices and truly blocks the network (D19). Cached Pro. |
| P6 | **Sell at the moment of value. Pro gates making new things, never your existing stuff.** | Organic paywall exposure was never measured (the allowlist dropped `paywall_*` until 1.9.1). 0 paying subscribers and 1 trial ever. 13 upsell points. A guest who starts checkout is never brought back to it. The Pro dialog omits Live Assist. | The Pro sheet opens only from a click on a gated action or the plan chip. Viewing, searching, copying and exporting stay free. The sheet resumes the blocked action. |
| P7 | **Calm, native and accessible by default.** | 68 colours, text as small as 10-11 px (11 px used 37 times), failing contrast, and no accessible names. Stylesheet cost is 1.7-3.8 s per 160 controls. NN/g's critique of glass. Fluent and HIG type ramps. | Tokens, one accent, a 12 px minimum, 4.5:1 text and 3:1 UI contrast, glass only on floating surfaces, painted list delegates, a focus ring everywhere, reduced motion, status announcements. |

---

## 3. Information architecture

### 3.1 The new app

```
TRAY / MENU BAR  (status + launcher)
  ● Recording · Weekly sync · 12:34   [only while recording]
  Stop and write notes                [only while recording]
  Open Gusan (bold)
  Dictate · Record a meeting… · Live Assist · Transcribe a file…
  Recent ▸ · Paste last dictation
  Privacy mode ✓ · Restart to update [when ready] · Sign in again… [when expired] · Fix shortcut… [when a hotkey failed]
  Settings… · Quit Gusan

FLOATING LAYER  (never inside the window)
  Dictation bar                 every dictation
  Meeting capsule <-> Live Assist card   one surface, two sizes, one session

MAIN WINDOW "Gusan"
  Sidebar
    Home        Ctrl+1   status card · Now · everything, date-grouped · search
    Dictations  Ctrl+2   same list, dictations only    primary: Dictate
    Meetings    Ctrl+3   meetings + Live Assist sessions  primary: Record a meeting
    Files       Ctrl+4   imported files                primary: Transcribe a file
    Dictionary  Ctrl+5   Words · Replacements · Advanced
    Trash                (shown only when not empty) 30-day restore
    --- footer ---
    ● Recording 12:34 · Weekly sync     (only while live; click opens item)
    [shield] Privacy mode on            (only when on; click toggles)
    ? Help      Send feedback · Keyboard shortcuts · What's new · Help (website) · Copy diagnostics
    Plan chip   "Aram · Free" -> Account
    [gear] Settings  Ctrl+,
      General · Shortcuts · Dictation · Meetings & Live Assist · AI & engines ·
      Privacy · Account · About
  Detail pane (right of the list): Dictation | Meeting (Notes · Transcript · Live Assist · My notes) | File (Transcript · Summary)
  Sheets (in-window, one at a time): Record a meeting · Transcribe a file · Pro · Sign in ·
      Your own key · Change shortcut · What's new · confirmations
  Command palette  Ctrl+K   actions · pages · library items · settings
  macOS: standard app menu while the window is open (About, Settings… ⌘,, Quit ⌘Q; Edit menu)
  (All Ctrl shortcuts are ⌘ on macOS; keycaps render the platform key.)
```

**Why this shape:**
- **No separate Home dashboard.** Home is the Library, so there are no duplicate "Recent" lists (the user-clarity reviewer's point).
- **No Meetings page separate from the Meetings view.** A live meeting is simply an item.
- **No Files page.** A file is an input, and its result is an item.
- **Dictations, Meetings and Files are sidebar entries** rather than filter chips. Each feature is visible as a noun with its own primary action. That keeps unmeasured features discoverable, and the data says not to cut them.

### 3.2 Every current feature, setting and window, and where it goes

Legend:
- **K** kept
- **→** moved
- **M** merged
- **R** renamed
- **A** behind "Advanced"
- **X** removed
- **N** new
- **F** bug fixed
- **C** config-only on purpose

**A. The Settings window frame**

| Today | New home | | Why |
|---|---|---|---|
| Dialog titled "Settings", opened by tray left-click | Main window "Gusan". Opens on Home, or on the live item while recording. | R M | It is already the app. A home should show status and your stuff. |
| Header "Welcome, {name}" | Name in the plan chip | X | Uses space and does nothing |
| GUEST/FREE/PRO badge | Plan chip (sidebar footer) | M | One calm plan indicator |
| Header CTA (Sign up · N min / Upgrade · Trial Nd / Upgrade to Pro, purple glow) | Plan chip states, plus the Pro sheet at gates | M X | No sales styling aimed at paying users |
| 7-tab strip, gradient "Transcribe Files" pill (`shiny_tabs.py`) | Sidebar plus Settings sections | X | The gradient made the tab look permanently half-selected |
| Footer Privacy Mode checkbox | Settings › Privacy (its home). Mirrored by the tray check item, the sidebar shield row and the Home status card. | → | One key, several switches, always in sync |
| "● Unsaved changes", Save, Close, Save/Discard/Cancel dialog, "✓ Settings saved" toast | Removed; changes apply instantly | X F | Save couldn't be trusted and caused stale overwrites |
| Tab title "About (Update vX!)" | Update banner, tray item, About status line | X | The badge never refreshed |
| Window sizing (`winfit.py`) | Shell sizing rules (§4.2). `winfit` kept for sheets and floating surfaces. | K | |

**B. General tab**

| Today | New home | | Why |
|---|---|---|---|
| Your Device (CPU/RAM/GPU) | About › This computer. It also drives "Recommended for this computer" in the model list. | → | The specs exist to help pick a model |
| Dictation hotkey (modifier+key, F1-F12, middle/back/forward mouse buttons) | Shortcuts › Dictate. Shown on the Home status card, tray labels and tooltip. | → | One table for all global shortcuts. **Mouse-button hotkeys are kept.** |
| "Open Live Assistance" button | Home status card, Meetings view toolbar, tray, hotkey, ✦ on the capsule | M | A launcher, not a setting |
| Live Assistance hotkey | Shortcuts › Live Assist | → | |
| "Private by default…" | Privacy › Screen sharing › "Keep Live Assist out of screen shares and recordings". The card's shield mirrors it. | → R | Factual, platform-scoped wording |
| "Answer automatically…" | Meetings & Live Assist › Live Assist. The card's ⋯ menu mirrors it. | → | It was in two places |
| Default Spoken Language (9 values) | Dictation › Language (Detect automatically + 8 languages), plus a toggle "I switch languages within one dictation" (= `multi`). The Home status chip mirrors it. Record and File sheets carry a per-capture chip. | R M | Auto and Multilingual really differ (checked). The toggle explains the difference. |
| Custom Vocabulary (with its engine scope note) | Dictionary › Words, with the scope note kept and corrected | → R | Content you curate, and your main lever on accuracy |
| "Clean up transcriptions" | Dictation › Clean up transcripts (description corrected) | R | |
| "Remove filler words" | Dictation | K | |
| "Always replace" table, Add, "Remove selected" | Dictionary › Replacements, with ✕ on each row | → F | Fixes "Remove selected" deleting the last row when nothing is selected |
| Default Meeting Audio Device | Meetings & Live Assist › Record from (default), plus a chip on the Record sheet | M | Same key as the setup-page picker |
| "Start Smart Meeting Transcription" / "(Pro)" | Home status card "Record a meeting", Meetings view, tray, optional hotkey | X (in Settings) | A tool inside Settings |
| "Fast cloud transcription" checkbox | AI & engines › Transcription › Gusan Cloud (Pro) | M R F | Fixes `backend=managed` being set with no provider |

**C. Transcribe Files tab (`ui/file_transcribe.py`)**

| Today | New home | | Why |
|---|---|---|---|
| The tab | "Transcribe a file" sheet, opened from the status card, Files view, tray, Ctrl+O, or by dropping onto the window | M | A tool |
| Drop zone | Drop anywhere on the window, plus the Files empty state | M | |
| Per-job Model dropdown | Sheet › Accuracy: Automatic / Fast / Balanced / Most accurate | R | Replaces a copy of the Models tab |
| Speaker labels, Timestamps | Sheet, remembered between runs | K | |
| (no language picker; silently used General's) | Sheet › Language chip | N | |
| Plan-limit text (built once, goes stale) | Shown only when the file is over the limit, read live from the entitlement table | K F | |
| Progress, time left, Cancel | Now row plus the item's progress stepper, with Cancel | M | You can leave while it works |
| Preview, "Save Word document", "Copy text" | Saved File item: Export ▾ (Word, Text, Markdown), Copy | M F | Results are finally saved |
| Two-way exclusion: refused while dictating or during a meeting, and it refuses both while it runs | Queued behind a meeting ("Waiting for the meeting to finish"). Starting a meeting during a file job offers "Cancel the file and record". Dictation cuts in between file chunks (D18); if D18 is rejected, dictation stays refused with a clear message on the bar. | N F | The Whisper model is shared; P1 |
| Guest recording cap (10 min) and 1 h / 5 h file split | Removed: transcription runs on the user's computer, so nobody is limited; files up to 5 h for everyone (memory) - decided 2026-10-04 | F | Local transcription costs nothing to serve |

**D. Models tab**

| Today | New home | | Why |
|---|---|---|---|
| 6 Whisper cards | AI & engines › Transcription › Advanced › On-device model: **Fast** (base, 140 MB), **Balanced** (small, 460 MB), **Most accurate** (large-v3-turbo, 1.6 GB). "Show all sizes" reveals tiny, medium and large-v3. | A R | Nobody picks a model by RAM |
| "Select … based on your system RAM" | Removed; a "Recommended for this computer" tag instead | X | |
| Download / Use Model / Remove with modal success boxes | Inline progress, no dialogs. A finished download offers "Use it now" and never switches on its own. Remove confirms inline. | K F | Fixes two cards both claiming to be active |
| Mistral Voxtral card | Your API keys › Mistral, and Transcription › Your own key › Mistral | M | |
| Google Gemini Speech card | Your API keys › Google Gemini ("Free key from Google AI Studio") | M | One key serves both speech and AI |
| Per-key eye / Copy / Test / Save, "✓ Working (Saved)" | A masked row ("Key ending 4f2a"), checked automatically on paste, with Replace and Remove. Status appears only after a real check. | X F | |
| "~$0.003/min", "Fast / Low Cost", "120+ languages" | Caption on the provider row under Advanced | A | Prices aren't consumer UI |
| Pro "Use with Pro" (`managed_provider`) | Advanced › "Gusan Cloud uses: Automatic (Gemini) / Google Gemini / Mistral" (Pro only) | A | Matters for quality in some languages; kept |
| `mistral_stt_model` (set only by the single Voxtral card), `google_stt_model` (no UI) | Advanced › Model, shown for the chosen provider | A | |

**E. AI Actions tab**

| Today | New home | | Why |
|---|---|---|---|
| OUTPUT MODE: Transcribe only / Smart actions (guests: disabled, "sign up for 5 tries") | Dictation › "When you dictate: **Exact words** / **Write with AI**". The choice persists across launches (D2), shows a ✦ AI chip on the bar while on, and gets an optional "Write with AI" shortcut. Guests see "Create a free account to try it". | R N | One term, three touchpoints |
| Smart mode reset at every launch (main.py:1968-1977, added in 1.6.12) | Removed (D2) | X | Use stopped days after the reset shipped |
| "N/5 FREE", "0/5 · PRO", guest hint | One line under the option ("3 of 5 free tries left"). The bar mentions it only when 2 or fewer remain. | R | |
| Primary Action Engine (7 rows) | AI & engines › AI features: Automatic / This computer / Gusan Cloud (Pro) / Your own key | M R | |
| API Key, API Base URL, API Model Identifier, "Custom Model…" | "Your own key" sheet: Provider, Key (checked automatically), Model (current models only), "Other model…", Server address (OpenAI-compatible and Cerebras only) | A R | |
| One `action_api_key` shared by 4 providers | One key per provider, still in the OS keyring and still scoped per account (migration) | M F | Stops one provider's key showing under another |
| Model lists with $/1M prices and 2024-era claude-3 IDs | Up to 3 current models per provider, plus "Other model…" | X | |
| 4 local LLM cards | Advanced › On-device AI: Small (Qwen 1.5B, 1.1 GB, 8 GB memory), Medium (Qwen 3B, 2.3 GB, 12 GB), Large (Qwen 7B, 4.7 GB, 16 GB). Gemma 2B under "Show all". Existing Gemma users stay on it. | A R F | One consistent memory note, fixing the 8 vs 16 GB contradiction |
| "Managed by Transcribe Pro" card | The "Gusan Cloud" option | M | |
| Rule-based page (unreachable) | Silent fallback, shown as "Now using: basic formatting (no AI connected)" | X (as UI) | Honest about what runs |
| `action_model` shared with meeting notes and Retry | One default, plus Advanced › "Use a different engine for: Meeting notes / Live Assist". Today Pro Live Assist always uses the managed cloud (Gusan Cloud in the new app); that stays the default. "Regenerate notes with…" changes one item only. | A F | Stops the notes engine changing dictation |
| `translate_target` (no UI) | Dictation › Write with AI › Translate into | N | |

**F. History tab**

| Today | New home | | Why |
|---|---|---|---|
| Meeting cards (max 60), Dictation cards (max 100) | Home / Dictations / Meetings views: virtualized, no display cap | M | 30-90 ms instead of 2.3-2.9 s |
| Search (3 implementations) | One full-text search (FTS5) over titles, transcripts, notes and Live Assist answers | M F | Finds words spoken inside meetings |
| Clear All History (dictations only / everything; keeps the meeting being recorded) | Privacy › "Delete all history…" (names the count; never touches a live recording), plus view ⋯ › "Delete everything in this view…" | M | |
| Export CSV / Export TXT (dictations only) | View ⋯ › "Export list" (CSV, TXT, Markdown zip). Exports the current view and search, meetings included. | M F | |
| "Save local transcription history" | Privacy › History › Save my dictations | → | |
| "Share anonymous usage metrics to improve Armenian AI models" | Privacy › "Share usage statistics" plus "What we collect" | → R | The old wording was misleading |

**G. About tab**

| Today | New home | | Why |
|---|---|---|---|
| Version, Check for Updates / Install Update | About status line ("1.11.0 · Up to date · checked 10 min ago [Check now]"), Home banner, tray item | → | |
| "…integrates with Google Cloud Speech API" | Removed | X | Out of date |
| AIBUBEN card, LinkedIn, "Open Source" card | One row of links: Website · LinkedIn · Source code · Licences | M R | The licence is non-commercial (source available), so "Open Source" was inaccurate |

**H. Account tab**

| Today | New home | | Why |
|---|---|---|---|
| Account card, status and plan line | Settings › Account; the plan chip opens it | → | |
| Sign in / Create account, Upgrade, Sign out | Account. Sign out says "Your Library stays on this computer." | → | History isn't per account |
| Manage subscription (dead end in 1.9.1: `STRIPE_PORTAL_URL=""`) | Account › Manage subscription → Stripe billing portal. The client code is already in the working copy (`billing_portal_session`); the founder deploys `supabase/functions/billing-portal/`. | F | |
| Terms / Privacy links | Account footer, About, Pro sheet footer, Sign in sheet | → | |
| Delete account (red link next to the legal links) | Account › a separate "Delete account…" card at the bottom (type DELETE). The copy says it cancels any subscription immediately. | → | Matches delete-account/index.ts |
| Admin · Force tier | Account › "Developer: preview plan" (super-admin only) | R | |
| Feedback box (sign-in required, up to 4 images of 6 MB) | Help (?) › Send feedback, in the same place on every page. **Image paste is kept (4 × 6 MB).** Guests can send it too if D10 is approved. | → | WCAG 3.2.6 consistent help |
| Pro perks list | Pro sheet | M | |

**I. Tray menu (main.py:2054-2146)**

| Today | New home | | Why |
|---|---|---|---|
| Disabled row "Transcribe v1.9.1 · PRO" | Removed. The version is in About; the tooltip reads "Gusan – Ready". | X | MS guidance |
| "Open Settings" (bold, tier dot) | "Open Gusan" (bold), plus a separate "Settings…" | R | |
| Account rows (email · tier, Upgrade, Manage, Sign in/up, Sign out) | Removed; they live in the plan chip and Account. Exception: "Sign in again…" appears when the session has expired. | X | |
| "Record Meeting... (Pro)" | "Record a meeting…". While recording: a status row plus "Stop and write notes". | R N | |
| "Transcribe Files..." | "Transcribe a file…" | R | |
| "Live Assistance overlay (ctrl+alt+a)" | "Live Assist  Ctrl+Alt+A" | R | |
| "History Log" | "Recent ▸" (last 5) plus "Open Gusan" | X M | |
| "Privacy Mode (on-device only)" | "Privacy mode ✓" | R | |
| "Quit" (no confirmation) | "Quit Gusan", which asks while recording or processing | F | |
| Single, double and middle click all open Settings | Windows: left-click opens the window. macOS: click shows the menu (HIG). | R | |
| Static full-colour flag icon (colour argument ignored) | Monochrome glyph with one badge (§4.1). The flag stays as the app and installer icon. | R | |
| (none) | Paste last dictation, Restart to update, Sign in again…, Fix shortcut… | N | |

**J. History window (`ui/history.py`)**

| Today | New home | | Why |
|---|---|---|---|
| The window (with count label) | Removed; replaced by the Library views | X | |
| Delete Selected (confirms) and right-click Delete (no confirmation) | Multi-select, then Delete → Trash with an Undo toast | M | |
| Double-click "pastes" into the History window itself | Removed. Replaced by Copy, a global "Paste last dictation" and tray Recent. | X F | |
| Download Audio | Export ▾ › Audio | M | |
| Export TXT/CSV (filtered) | View ⋯ › Export list | M | |
| Clear All (its warning wrongly says meetings go too) | Privacy › Delete all history | M F | |

**K. Record Meeting window (`ui/meetings.py`)**

| Today | New home | | Why |
|---|---|---|---|
| Setup: Title, Attendees | Record sheet: Title (prefilled "Meeting · Tue 14:30"), Details › Attendees (also editable later) | K | |
| Setup: Meeting Recording Mode | Record sheet chip "Record from" | M | |
| Setup: Spoken Language | Record sheet language chip (shared with Live Assist) | M | |
| Setup: Transcription model, Summary & notes AI, API key | Removed from the sheet; global settings live in AI & engines. **New per-meeting chip "Keep this meeting on this computer"** (on-device transcript and notes). "Regenerate notes with…" per item. | X N | They were silently rewriting global config |
| Description promising loopback on macOS | macOS chip reads "My microphone only" with (i): "Recording call audio isn't available on Mac yet." | F | |
| One-time consent dialog (`meeting_consent_ack`) | A reminder line every time on the Record sheet and at Live Assist Start, with [Copy notice]. Meetings & Live Assist › "Remind me…" (on). | R | Consent law; the Otter lawsuits |
| Starting a meeting silently stops a running dictation | The Record sheet says "Your dictation will stop" when one is running | F | |
| Dictation refused during a meeting (tray toast with hard-coded "Alt-R") | Kept as a rule, now shown on the dictation bar with the real shortcut ("Dictation is off while a meeting records"). The status card and tray tooltip say so too. | K F | Shared recorder |
| Meeting refused during a file job (message box) | Record sheet: "A file is being transcribed (about 6 min left). [Cancel the file and record] [Cancel]" | F | |
| Recording page: REC banner, Live Summary, Live Transcription, Your Notes | The live Meeting item (Live notes / Transcript / My notes) plus the capsule | M | |
| Your Notes: sent to the notes AI, never saved | My notes: saved with the item and still sent to the notes AI | F | |
| (none) | Level meters for you and the call, a warning after 30 s of silent call audio, Pause, Discard | N | Silent call audio used to be discovered only after Stop |
| Audio kept only up to about 2 h (memory cap); past it, no speaker labels | Banner at 1:50: "Audio is saved up to 2 hours. The transcript continues, without speaker labels after that." The stepper and item show it. | K F | Was only visible after Stop |
| "Live Assistance" button on the recording page | [Live Assist] in the live item header and ✦ on the capsule. It attaches to the running session and catches up without answering old questions. | M | |
| "Stop & Generate Notes" (renders as "Stop  Generate Notes") | "Stop and write notes" | R F | |
| 4-button close dialog ("Minimize to Tray" etc.) | Removed. Closing the window never stops a recording; the capsule and tray show it. Discard lives in ⋯. | X | |
| Processing page (no cancel); first-meeting speaker-model download (47 MB) was silent | Item stepper: Saving ✓ · Transcribing · Speakers (shows "Downloading speaker model 47 MB" the first time) · Notes, with [Cancel notes] (the recording is kept) | M N | |
| Notes silently downgraded to rule-based when no engine or key, with a banner in the notes | Item state "Notes written with basic formatting (no AI connected)" [Connect an AI engine] [Regenerate] | F | |
| Done: Retry Summary plus engine picker | "Regenerate notes with…" (this item only) | R F | |
| Done: Send to Notion, Copy Markdown, Save to File | Export ▾ | M | |
| Done: "Reset" | "New meeting" (Meetings view toolbar) | R | |
| Editable summary whose edits are discarded | Edits are saved to notes.md and used by every export | F | |
| Title and attendees never sent to the notes AI | Sent | F | |
| `_format_share` email/Slack (dead code) | Export › Copy for email / Copy for Slack | N | |
| Notion connect dialog (Done page only; token in plain config) | Meetings & Live Assist › Send notes to Notion: Connect / Change page / Disconnect. Token moved to the OS keyring. | → N F | |
| 20 fps timer running while idle | Removed | X | Performance |

**L. Meeting detail dialog (`ui/meeting_detail.py`)**

| Today | New home | | Why |
|---|---|---|---|
| Summary / Transcript tabs; meta "recording N in parts" | Meeting item: Notes / Transcript / Live Assist / My notes; meta keeps "recorded in 2 parts" | M | |
| Resume session (Pro) | "Continue recording" (Pro) in the item header | R | |
| Save recording…, Save as Word… (raw `##`), Copy notes (no feedback), Open folder (`os.startfile`) | Export ▾ (Audio, Word with Markdown rendered properly), Copy notes with a toast, ⋯ › Show in folder (`QDesktopServices`, works on Mac) | M F | |
| (missing) delete, rename, speaker names, regenerate | ⋯ › Rename, Delete. Click "Speaker 1" to name them; names flow into exports and regeneration. Speaker numbers restart in each continued part, so names are set per part. Regenerate notes. | N | `docx_export` already supports names |

**M. Live Assistance card (`ui/live_assist.py`)**

| Today | New home | | Why |
|---|---|---|---|
| Header: title, Start / timer / stop, Private chip, Screen, theme, collapse, close; double-click to collapse | Status (Start, or ● 12:34 [Stop]), language chip, shield, ⋯, collapse, close; double-click still collapses | M | At most 6 items |
| Theme button (`live_assist_theme`) | ⋯ and Settings › Card appearance: Match app / Light / Dark | → | Kept (reviewers flagged removing it) |
| `live_assist_opacity` (no UI; fades the text too) | ⋯ and Settings › Card background 86-100% (existing values below 0.86 are raised). Applies to the plate only; text stays fully opaque. | N F | Contrast rule |
| Hidden "NOW" block | Removed | X | Dead UI |
| LIVE TRANSCRIPT (110 px, no speakers) | A 2-line drawer tinted You / Them, expandable to full scrollback | R | |
| Answer / Follow-ups / Solve screen / Auto pills | Composer chips: Answer latest question / Suggest follow-ups / Read my screen. Auto becomes a header status, editable in ⋯. | M R | Three "answer now" paths become one |
| Screen icon toggle (on by default); Mistral OCR used when the engine can't read images | A labelled "Screen" pill in the composer, plus a "used screen" chip on each answer ("Screen · read by Mistral OCR" when OCR ran). Disabled with "Off in Privacy mode" when that applies. Screenshots are never saved. | R F | |
| Single answer slot, overwritten each time; a user-asked answer holds off auto-answers for 12 s | Answer feed: each card quotes its question, with Copy, Pin and source chips, saved to the item's Live Assist tab. Auto-answers never interrupt one you asked for; they queue under it. | N | |
| Footer timings, "AI can be wrong" | Timings in a hover tooltip. "AI can be wrong" as a one-time hint and in ⋯. | X | |
| Errors as text in the answer box | Inline error card with one action ("Connect an AI engine" / "Retry") | F | |
| Collapsed "pill" (480 px wide) | A ~240 px capsule (§4.6) | F | |
| Close hides the card while still recording | Close collapses it to the capsule; it closes fully only when idle | F | |
| Stop → "notes are generated in Record Meeting" | End sheet: Writing notes… ✓ [Open notes] [Save audio] [New session] | F | |
| Download audio | End sheet, and the item's Export › Audio | M | |
| Language forced to "en" (`live_assist_language`, `_output_language`) | Session language chip (Detect automatically / list) and "Answer in: Same as the call". In 1.10 a quick fix makes it follow the Dictation language. | N F | |
| Paywall appears only when Start is pressed | Non-Pro users see a preview panel and [Try Pro free], which opens the Pro sheet inside the card (avoids the always-on-top z-order problem) | F | |
| Start drives the hidden Meetings window; live summary is suppressed during Live Assist | `MeetingSession`; one Library item. Live notes stay off during Live Assist (unchanged) and notes are written at Stop. | F | |
| Pro answers always use the managed cloud; Privacy mode keeps it on this computer | Unchanged by default and shown on each answer chip. The override is under Advanced. | K | |

**N. Dictation HUD (`ui/overlay.py`, working-copy capsule)**

| Today | New | | Why |
|---|---|---|---|
| Capsule design (uncommitted): draggable, remembered, double-click sends it home, only the capsule takes the mouse, never focusable | Kept and committed in 1.10 | K | The most modern part of the app |
| Silent 140 MB download, then "still working…", then a 120 s timeout | "Getting ready · 42%" state; the dictation waits instead of timing out | N | |
| Raw IDs ("gemini-2.5-flash AI", "Local AI") | "On this computer" / "Gusan Cloud" / "Your Mistral key" | R | |
| Cloud error or offline silently falls back to on-device | Chip "Cloud didn't answer · used this computer" | F | Truthful status |
| "Thinking" | "✦ Writing email…" (names the action) | R | |
| Errors click-through, gone after 4.5 s | Clickable for 6 s: [Retry], "Open in Library". The failed item stays in Now. Failed audio is kept for 7 days (not in Privacy mode). | F | |
| "Pasted to cursor" when nothing was focused | "Pasted", or "Copied · press Ctrl+V" (and the clipboard is not restored) | F | |
| Enter/Esc/hotkey also reach the app you're typing in | Suppressed while recording (Windows first, with an Alt mask key; macOS after verification) | F | |
| Keycap hints on the first 5 dictations of each launch | The first 5 dictations ever; Settings › Dictation › Keyboard hints | R | |
| `overlay_theme`, `overlay_private`, `overlay_pos` (no UI) | Dictation › Dictation bar › Appearance, Reset position. Privacy › Hide the dictation bar. | N | |

**O. Onboarding, sign-in and permissions**

| Today | New home | | Why |
|---|---|---|---|
| Account wall first, defaulting to Sign in | An optional step after the first successful dictation, plus a "Sign in" link on Welcome | → | Value first |
| Email sign-up: leave the app, come back, type the password again | Verification is polled and signs in automatically | F | |
| Google sign-in moves on without waiting and fails silently | "Finish signing in in your browser… [Cancel]", then success or a clear error | F | |
| Forgot password?, Sign in / Sign up switch, name field (sign-up), 6-character minimum, Terms/Privacy line | Kept in the Sign in sheet | K | Reviewers flagged these as unmapped |
| "Continue as guest" | The default path ("Get started") | R | |
| Step 1: hotkey | The first-run "Try it" step (with a "Change shortcut" link) | M | |
| Step 2: language | Removed (Detect automatically) | X | |
| Step 3: backend and API keys, "Missing API Key" Yes/No | Removed (Automatic) | X | Jargon, and it named the wrong Google product |
| Analytics box pre-ticked 3 times | One neutral question after the first success, with two equal buttons. Installer pre-tick removed. | F | Planet49 |
| Auth gate reused as "Welcome to Transcribe" for Upgrade | Sign in sheet, titled for the task ("Create a free account to start your trial") | R | |
| macOS permissions dialog at 1.2 s, never checked | Built into first run with live polling. Screen Recording is asked on first use. Also listed in Settings › General › Permissions. | M F | |
| `known_emails`, `last_signin_email` prefill | Kept | K | |

**P. Pro dialog, upsells, updates, notifications, installer**

| Today | New home | | Why |
|---|---|---|---|
| Pro dialog (4 benefits incl. "Priority models and support"; no Live Assist, no 5 h files; gold card) | Pro sheet driven by the entitlement table: names the feature that triggered it and resumes the action afterwards. Priority support stays only if the founder still offers it (D20). | M F | |
| Fallback message box (€7.99 / €59) | Removed | X | |
| ~13 upsell touchpoints, "(Pro)" suffixes, PRO pills shown to Pro users | Plan chip, a neutral "Pro" tag on gated actions, the Pro sheet, and a meter only near limits | M X | |
| A toast after every free Smart Action | Removed; the bar mentions it only when 2 or fewer remain | X | |
| "long_files is a Pro feature." | "Files can be up to 5 hours. Split longer ones." (no plan talk) | R | |
| "Create a free account… get 3 days of Pro" (stale) | Removed | X | The no-card trial was retired |
| Update Yes/No at launch and after every dictation (default Yes, "No" never remembered) | Removed. Background download, banner, tray item, install on quit, Skip this version, What's new. | X N | |
| Updater downloads the Windows .exe on macOS | Reads the manifest's existing `macos` entry: DMG with sha256, or the release page | F | |
| Two update flows (main.py and settings.py) | One `updater.py` | M | |
| System toasts for everything | In-window banners and toasts. System notifications only while the window is hidden: Notes ready, File done, Update ready (once), Still recording after close, Microphone blocked. | R | |
| ~96 message boxes | Sheets for decisions. Confirmations only for irreversible deletes and quitting while recording. | M | |
| "Right-click the tray icon to restore" | Removed | X | |
| Raw exception text (`"Microphone error: {e}"`) | Mapped messages plus "Copy details" | F | |
| Installer: analytics page, pre-ticked | Removed (asked in the app) | X | |
| Installer: "Start automatically" (a `{userstartup}` shortcut with `--background`) ticked, desktop shortcut unticked | Kept. Settings › General › Startup manages the same shortcut on Windows and SMAppService on macOS. | K N | |
| `calendar_sync.py` (built, not wired) | Not in this roadmap; see D13 | — | Scope |

### 3.3 Every config key and where it lives

| Key(s) | New home | |
|---|---|---|
| `hotkey` | Shortcuts › Dictate | → |
| `live_assist_hotkey` | Shortcuts › Live Assist | → |
| `language` (`auto`/`multi`/code) | Dictation › Language, plus the "I switch languages within one dictation" toggle (`multi`) | R |
| `whisper_model` | AI & engines › Transcription › Advanced › On-device model | A |
| `backend` | Migrated to `speech_engine` (automatic/local/managed/own_key+provider). The old key is kept for one release. **Both are in the per-account scope.** | M |
| `managed_provider`, `mistral_stt_model`, `google_stt_model` | AI & engines › Advanced | A |
| `google_api_key`, `mistral_api_key`, `action_api_key` | Your API keys: one per provider, values in the OS keyring, scoped per account via `user_secrets`/`secrets_owner` | M |
| `action_api_provider` | The provider picked in "Your own key" (per account) | M |
| `action_api_base_url`, `action_api_model` | "Your own key" sheet › Server address, Model | A |
| `action_api_model_recap`, `action_api_reasoning_effort` | Config-only (Live Assist and notes tuning) | C |
| `action_model` | AI features choice, plus new `ai_engine_meeting_notes` and `ai_engine_live_assist` overrides (per account) | M A |
| `output_action` | Dictation › When you dictate (persists) | R |
| `translate_target` | Dictation › Write with AI › Translate into | N |
| `initial_prompt` | Derived from `vocabulary` (no UI) | C |
| `vocabulary` | Dictionary › Words | → |
| `vocabulary_share_with_cloud` | Dictionary footer toggle, plus a row in Privacy's "Where your data goes" | → |
| `cleanup_enabled`, `cleanup_strip_hallucinations`, `cleanup_strip_artifacts` | Dictation › Clean up transcripts (+ Advanced) | A |
| `cleanup_remove_fillers` | Dictation › Remove filler words | K |
| `cleanup_replacements` | Dictionary › Replacements | → |
| `cleanup_custom_hallucinations` | Dictionary › Advanced › Phrases to always remove | A |
| `restore_clipboard` | Dictation › Put my clipboard back after pasting | N |
| `input_device_index` | General › Microphone | N |
| `meeting_audio_mode` | Meetings & Live Assist › Record from, plus the sheet chip | M |
| `notion_api_key`, `notion_parent_page` | Meetings & Live Assist › Send notes to Notion (token moved to the OS keyring) | → F |
| `live_assist_private` | Privacy › Screen sharing | → |
| `live_assist_auto_answer` | Meetings & Live Assist, mirrored in ⋯ | → |
| `live_assist_auto` (1.9.0 legacy) | Ignored; removed by the v2 migrator | X |
| `live_assist_screen_auto` | Meetings & Live Assist › "Let Live Assist see my screen" (new installs: only when I turn it on) | R |
| `live_assist_language`, `live_assist_output_language` | Session language chip / Call language; Answer in | N |
| `live_assist_theme`, `live_assist_opacity` | Meetings & Live Assist › Card appearance, Card background (floor 0.86, plate only) | N |
| `live_assist_pos`, `overlay_pos` | Remembered; "Reset position" in ⋯ and in Dictation › Dictation bar (double-click also resets the bar) | C + action |
| `overlay_theme` | Dictation › Dictation bar › Appearance | N |
| `overlay_private` | Privacy › Screen sharing › Hide the dictation bar | N |
| `privacy_mode` | Privacy › Privacy mode, plus new `pre_privacy_state` | → F |
| `save_history` | Privacy › History | → |
| `analytics_enabled` | Privacy › Usage statistics | → |
| `admin_tier_override` | Account › Developer | R |
| `dismissed_update_version`, `pending_update_version`, `pending_update_body`, `previous_version` | Update banner, Skip, What's new | used |
| `tray_hint_shown` | First-run tray and pin hint | used |
| `meeting_consent_ack` | Replaced by `meeting_consent_reminder` (default on) | R |
| `onboarding_done`, `account_gate_seen`, `macos_perms_guide_shown`, `analytics_consent_applied` | First-run flags (internal) | C |
| `last_known_pro` | Kept for `pro_activated` only. It has no timestamp, so it can't drive the grace period. | K |
| `secrets_owner`, `user_secrets`, `known_emails`, `last_signin_email`, `google_oauth_client_*` | Internal | C |
| `usage.json` (Smart Action counters per user) | Internal | C |
| `sample_rate`, `chunk_size`, `silence_trigger_sec`, `min_speech_sec`, `max_speech_sec`, `clipboard_restore_delay_ms`, `analytics_endpoint` | Config-only | C |
| `config_schema_version` | Bumped to 2 by the migrator | C |
| `accent_color` | Retired (there is one accent) | X |
| **New keys** | `ui_v2`, `app_theme`, `enter_to_finish`, `ai_shortcut`, `paste_last_shortcut`, `meeting_shortcut`, `open_shortcut`, `file_speakers`, `file_timestamps`, `history_autodelete_days`, `meeting_audio_retention`, `install_updates_on_quit`, `notify_when_ready`, `keyboard_hints`, `ai_engine_meeting_notes`, `ai_engine_live_assist`, `pre_privacy_state`, `speech_engine`, `entitlement_cache` (`{user_id: {pro, plan, period_end, checked_at}}`) | N |

### 3.4 Concept ideas deliberately not adopted

| Idea | Why not |
|---|---|
| Popover as the app's home (Tray-first) | Windows 11 hides new tray icons in the overflow. A popover that closes when you click away is hostile to screen readers and breaks drag and drop. It can't be tested offscreen. It stays an optional later surface (D12). |
| A separate Home dashboard and a separate Meetings page (Hub) | They duplicated "Recent" and the live view. Home is now the Library, and a live meeting is an item. |
| Gusan Cloud as "Automatic" for Pro dictation | It would silently move Pro dictation off the computer, add per-minute cost and hit the usage cap. Cloud speech is an explicit choice. |
| Removing "Multilingual" | It is real per-chunk re-detection (checked). Kept as a toggle. |
| A "Languages I speak" multi-select | Whisper takes one language or auto, so a list has no meaning to the engine. |
| 90-day default retention for dictations | It would delete existing history on first launch. The default is Keep forever, with auto-delete available. |
| Storing which app each dictation was pasted into | Makes stored data more sensitive for little benefit |
| Pinned view, folders, tags | Scope. Search covers it until users average over 100 items. |
| Mica / vibrancy, QML, qfluentwidgets, frameless windows | See §7 |
| Making "Hold to talk" and "Pause shortcuts" core | New features. Hold to talk is optional in 1.14 (D11). |
| Dictating during a meeting recording | The recorder and audio device are shared; out of scope. The bar says so clearly instead. |

---

## 4. Screen-by-screen spec

### 4.0 Global patterns (they apply to every screen)

- **One primary button per view**, in the accent colour. Everything else is secondary or subtle.
- **Banner slot** at the top of the content area, for problems that persist:
  - update ready
  - microphone blocked
  - sign-in expired
  - no AI connected
  - cloud limit reached
  - macOS permissions reset

  A banner has one action and a close ✕. Only one shows at a time, chosen by priority.
- **Toasts** appear bottom-centre inside the window. They are never system toasts while the window is visible.
  - A toast with no action lasts 4 s.
  - A toast with an action (Undo) stays at least 10 s, pauses while hovered or focused, and its action is also reachable by keyboard (Ctrl+Z for Undo). This follows WCAG 2.2.1.
- **Sheets:** a scrim with a 480-560 px card and a focus trap. Esc cancels. They have Cancel plus one primary button, and only one sheet is open at a time.
- **Disabled means explained:** every disabled control carries a reason in its description, e.g. "Paused by Privacy mode [Turn off]" or "Needs Pro [See Pro]". The reason text uses secondary text colour, never the disabled colour.
- **Mirror rule:** a setting has one home. Mirrors (tray check, status chip, card ⋯) show and toggle the same key through ConfigStore and never keep their own copy.
- **Status announcements** (WCAG 4.1.3): these surfaces never take focus, so each state change raises an accessibility announcement (a UIA notification on Windows, an NSAccessibility announcement on macOS). That covers the bar (recording, pasted, copied, error), the capsule (recording, paused, no call audio) and the card (answer ready).
- **Keyboard** (⌘ in place of Ctrl on macOS):
  - Ctrl+1 to 5 switch pages, Ctrl+, opens Settings, Ctrl+K opens the command palette.
  - Ctrl+F searches the current page (Library or Settings).
  - Ctrl+N records a meeting, Ctrl+O transcribes a file.
  - Enter opens, Ctrl+C copies, Delete moves to Trash (with Undo).
  - F6 cycles regions, Alt+↑/↓ moves through sidebar pages, Esc closes a sheet or the detail pane, Ctrl+/ shows the shortcut sheet.
  - Tooltips show each action's shortcut.
- **Error copy follows one pattern:** what happened, what is safe, one action. Raw text goes only to the log and "Copy details".

### 4.1 Tray / menu bar

```
● Recording · Weekly sync · 12:34            (only while recording; click opens the item)
Stop and write notes                          (only while recording)
-----------------------------
Open Gusan                                    bold, default
-----------------------------
Dictate                             Alt+R     -> "Stop dictation" while dictating; disabled with "Off during a meeting" while one records
Record a meeting…                             hidden while a meeting records
Live Assist                     Ctrl+Alt+A
Transcribe a file…
-----------------------------
Recent                                  ▸     last 5 (first ~40 characters): dictation -> copy; meeting/file -> open
Paste last dictation            (shortcut if set)
-----------------------------
Privacy mode                            ✓
Restart to update (1.11.2)                    only when downloaded
Sign in again…                                only when the session expired
Fix shortcut…                                 only when a hotkey failed to register
Settings…
Quit Gusan                                    asks only while recording or processing
```

Only global shortcuts appear in the tray. Ctrl+, works only inside the window, so it isn't shown here.

- **Click:**
  - Windows: left-click opens the window (on Home, or on the live item). Right-click shows the menu.
  - macOS: click shows the menu. While the window is open, the Dock menu mirrors Dictate, Record a meeting, Live Assist and Privacy mode.
- **Icon:** a monochrome mic glyph in 16/20/24 px ICO that follows the taskbar theme. On macOS it is a template image (`setIsMask`). The icon never animates.
- **Badge:** at most one, bottom-right, in priority order. Each badge has a distinct shape so colour is never the only signal:
  1. Recording: filled red dot.
  2. Needs you (mic blocked, shortcut failed, sign-in expired, permissions reset): amber "!" triangle.
  3. Something unread (notes ready, update ready): small accent ring.
  4. Privacy mode: shield.
- **Tooltip:** "Gusan – Ready · Alt+R", "Gusan – Recording 12:34", "Gusan – Privacy mode on", or "Gusan – Shortcut not working".
- **Quit while recording:** "A meeting is recording. [Stop and write notes] [Discard recording] [Keep recording]". Esc keeps recording.
- **Empty and loading:** while the app is loading, only Open, Settings and Quit appear. Recent is hidden until the Library has items.

### 4.2 Main window shell

```
+--------------------+---------------------------------------------------------------+
| Gusan              | [banner slot]                                                 |
|                    |                                                               |
| [#] Home    Ctrl+1 |   page content (24 px margins, content layer bg.card,         |
|   Dictations       |   8 px top-left radius, 1 px stroke.card)                     |
|   Meetings         |                                                               |
|   Files            |                                                               |
|   Dictionary       |                                                               |
|   Trash (3)        |                                                               |
|                    |                                                               |
|--------------------|                                                               |
| ● Recording 12:34  |                                                               |
| [shield] Privacy on|                                                               |
| ? Help             |                                                               |
| (A) Aram · Free  › |                                                               |
| [gear] Settings    |                              [toasts bottom-centre]           |
+--------------------+---------------------------------------------------------------+
```

- **Window type:** a `QMainWindow` with a native title bar (dark via `DWMWA_USE_IMMERSIVE_DARK_MODE`, corners via `DWMWA_WINDOW_CORNER_PREFERENCE`). It is solid, with no Mica.
- **Closing** hides the window and the app keeps running; the first time, a one-time hint says where the tray icon is. The window reopens on the last page, or on the live item while recording.
  - It appears in the taskbar and Dock only while open. On macOS this uses an activation-policy switch; today the Dock icon is always shown.
  - While open on macOS, it has the standard app menu.
- **Size:**
  - Default is min(1160, 80% of work-area width) × min(760, 80% of height). On the founder's 1536×816 work area that is 1160×653.
  - Minimum 720×520.
- **Responsive layout:**
  - At 1008 px and wider: sidebar 240 px, list 380 px, detail pane the rest.
  - From 720 to 1007 px: a 48 px icon rail with tooltips; list and detail stay.
  - At the minimum size: one pane at a time, with Back.
- **Sidebar:**
  - 36 px items with 20 px Fluent icons, filled when selected, and a 3×16 accent indicator.
  - Counts appear only on Trash.
  - The footer rows (live, privacy) appear only when relevant.
- **Loading:**
  - Pages are built lazily.
  - The first launch after migration shows skeleton rows and "Building your library… 214 items" (non-blocking).
- **Page error:** a page factory that fails shows "This page couldn't open. [Try again] [Copy details]". Other pages keep working.

### 4.3 Home

```
| [banner] Version 1.11.2 is ready.  [Restart to update]  What's new · Skip this version  ✕ |
| +--------------------------------------------------------------------------------------+ |
| | Press [Alt] [R] in any app to dictate. Press it again to paste.                       | |
| | ● Ready · On this computer (Balanced) · Detect language ▾            Change shortcut › | |
| | [ ● Record a meeting ]  [ Live Assist  Pro ]  [ Transcribe a file ]                   | |
| +--------------------------------------------------------------------------------------+ |
| [ Search everything                                                          Ctrl+F ]  |
| NOW                                                                                     |
|  ● Weekly sync · Recording 12:34 · Both sides                            [Stop]         |
|  ◌ interview.m4a · Transcribing 42% · about 6 min left                   [Cancel]       |
|  ! Dictation failed · 09:12 · took longer than 2 minutes                 [Retry]        |
| TODAY                                                                                   |
|  [mic] Thanks Anna, I'll send the deck tonight and…               10:41   [Copy] [⋯]   |
|  [people] Q4 roadmap · 42 min · Notes ready                       09:30                 |
|     Ship v2 on Oct 14; Anna owns pricing                                                |
| YESTERDAY                                                                               |
|  [doc] board_call.mp3 · 1 h 04 · 2 speakers                        16:40                |
| GET SET UP   2 of 5 · Hide                                                              |
|  ✓ First dictation   ○ Add names to your Dictionary   ○ Try a meeting                   |
|  ○ Create a free account (5 Write with AI tries)   ○ Pin the tray icon                  |
```

- **Primary action:** the hotkey itself. Until the first dictation succeeds, the card shows [Try it here], which focuses a practice box. After that the status card has no filled button, and the capture buttons are secondary.
- **Status card:**
  - It is the most important sentence in the app. Each part is clickable: the engine opens AI & engines; the language opens a menu that writes `language`.
  - After 3 days it collapses to one line: "● Ready · Alt+R · On this computer · Detect language".
  - Variants (highest priority wins):

| State | Text | Action |
|---|---|---|
| Ready | "● Ready · On this computer (Balanced) · Detect language" | — |
| Model downloading | "Getting ready · downloading speech model 42%" | — |
| Mic blocked | "Gusan can't hear you: microphone access is off." | [Open settings] (`ms-settings:privacy-microphone` / Mac pane) |
| Shortcut failed | "Alt+R isn't working. Another app may be using it." | [Change shortcut] |
| macOS permission missing or reset | "The shortcut needs Input Monitoring." / "macOS reset permissions after the update." | [Grant again] |
| Meeting recording | "● Weekly sync is recording · dictation is off until you stop" | [Open meeting] |
| File transcribing (if D18 is rejected) | "A file is being transcribed · dictation is paused · about 6 min left" | [Cancel file] |
| Privacy mode | "[shield] Privacy mode · your voice, text and screen stay on this computer" | [Turn off] |
| Write with AI on | "✦ Write with AI is on · 3 of 5 free tries left" | [Turn off] |
| Cloud limit (Pro, cloud chosen) | "Today's Gusan Cloud limit is reached. Dictation continues on this computer." (Wording follows the live cap; the repo proxy caps requests per day.) | [Details] |
| Offline with cloud chosen | "No internet · using on-device transcription" | — |

- **List rows:**
  - Dictation rows are 56 px, showing 2 lines of text with the time.
  - Meeting and file rows are 64 px: title, summary first line, duration and a state chip.
  - The Now section is sticky and holds live, processing and failed items.
  - Groups: Today / Yesterday / This week / Earlier this month / Month Year.
  - Hover shows Copy and ⋯ (also reachable by keyboard focus). Unopened new items carry a "New" dot. Multi-select with Shift or Ctrl+click brings up a bulk bar: Export, Delete.
- **Selecting a row** opens the detail pane (§4.5). Esc closes it.
- **Setup checklist:** hidden when complete or when you click Hide.
- **Empty states:**
  - History off: "History is off, so dictations aren't saved here. [Turn on]".
  - No results: "No results for 'budget'. Try fewer words."
- **Error:** if the index can't be read, "Your Library couldn't load. Your files are safe. [Rebuild library] [Copy details]".

### 4.4 Dictation bar (floating)

- **Position:** bottom-centre of the monitor with the active window, 16 px above the taskbar, or wherever you dragged it. The position is remembered; double-click, or Settings › Dictation bar › Reset position, sends it home.
- **Input:** it never takes focus. Only the capsule itself takes the mouse, for dragging, and for clicks in the error and done states. The rest of its window lets clicks through.
- **Glass:** plate opacity 0.86 or more in light and 0.82 or more in dark. It falls back to an opaque plate when transparency effects are off or the bar isn't hidden from capture.
- **Text on glass** uses primary or secondary colour only. Tertiary text fails on glass (3.4-3.5:1).

| State | Shows | Clickable | Duration |
|---|---|---|---|
| Getting ready | "Getting ready · 42%" (the dictation is queued, not timed out) | no | until ready |
| Listening | ● dot, 15 level bars, timer. Hint keycaps on the first 5 dictations: "Alt+R to finish · Esc to cancel" (Enter shown only if `enter_to_finish` is on and suppressed). ✦ chip if Write with AI is on. Shield in Privacy mode. | drag only | |
| Transcribing | Partial words, plus an "On this computer" / "Gusan Cloud" chip | drag only | |
| Writing | "✦ Writing email…" / "✦ Making a to-do list…" | drag only | |
| Pasted | ✓ "Pasted" | yes (click opens the item) | 1.4 s |
| Copied | ✓ "Copied · press Ctrl+V". Used when no editable focus is detected; the clipboard is not restored. | yes | 2.6 s |
| Error | "Couldn't transcribe · took too long" · [Retry] [Open]. Or "No microphone · [Fix]". Or "Key rejected · [Fix]". The failed item also stays in Home › Now and tray › Recent, for keyboard users. | yes | 6 s |
| Refused | "Dictation is off while a meeting records" [Open meeting]. Or "A file is being transcribed · [Cancel file]" (only if D18 is rejected). | yes | 4 s |
| Notes | "2 free Write with AI tries left" / "Free tries used · pasted your exact words [See Pro]" / "Cloud didn't answer · used this computer" | yes | with the result |

- **Behaviour:**
  - Esc is suppressed while recording and cancels the dictation.
  - The dictation hotkey is suppressed so it doesn't trigger Office's Review tab. When the hotkey uses Alt, a mask key is injected before Alt is released, so a lone Alt doesn't show KeyTips or focus a menu bar.
  - Mouse-button hotkeys are suppressed on Windows through pynput's `win32_event_filter`; on macOS they aren't suppressed.
  - Enter behaviour follows D4.
  - Failed audio is kept 7 days so it can be retried (not in Privacy mode).
  - "Pasted" is shown only when a caret or editable focus was found in the foreground window (Windows `GetGUIThreadInfo.hwndCaret` or the UIA focused element; macOS the AX focused element). Otherwise "Copied".
- **Dictation during a file job:** D18 recommends that file jobs run in chunks, so a dictation waits at most one chunk (a few seconds, shown as "Getting ready"). If D18 is rejected, today's refusal stays, with the "Refused" copy above.

### 4.5 Library views and item detail

**Views.**
- Dictations, Meetings and Files are the Home list filtered by kind.
- Each view has its own primary action:
  - Dictations → [Dictate], which starts a voice note that is saved to the Library and copied.
  - Meetings → [● Record a meeting] and [New meeting].
  - Files → [Transcribe a file]; the whole view is a drop target.
- The view ⋯ menu: Export list (CSV / TXT / Markdown zip), Delete everything in this view….
- Search in a view searches that view, with a "Search everything instead" link.
- **Empty states:**
  - Dictations: "Press Alt+R in any app. Your words appear here." [Try it here]
  - Meetings: "Record a meeting to get a transcript with speakers and AI notes." [Record a meeting] · "See example notes"
  - Files: a drop zone with "Drop audio or video (mp3, m4a, wav, mp4…). Up to 5 hours per file." [Choose a file]

**Dictation detail**

```
| Tue 14:02 · 23 words · English · On this computer              [⋯] |
| Thanks Anna, I'll send the deck tonight and we can review it on    |
| Thursday.                                           (editable)     |
| (What I said | Result)                  shown only if Write with AI ran |
| [ Copy ]   [ ✦ Write with AI ▾ ]   [ Retry ]   (Retry only when audio kept) |
```

- **Primary:** Copy.
- **Write with AI ▾:** Email, To-do list, Summary, Translate into {language}, Custom instruction…. Each run creates a linked result and keeps the original. It uses a free try or needs Pro; guests are asked to create a free account.
- **Right-click a word:** "Always write this as…" adds a Dictionary replacement, so fixing mistakes teaches the Dictionary. The same action is in ⋯ for keyboard users.
- **⋯ menu:** Add word to Dictionary, Delete.
- **Failed dictation:** "Couldn't transcribe: took longer than 2 minutes. The audio is kept for 7 days." [Retry] [Retry with Gusan Cloud] (the second only if the plan allows it).

**Meeting detail: one screen across all statuses**

```
| Weekly product sync  ✎                 [Continue recording Pro] [Export ▾] [⋯] |
| Tue 14:02 · 42 min · recorded in 2 parts · Anna, David +2 ✎ · English · On this computer · Notes: Gusan Cloud |
| (Notes) (Transcript) (Live Assist · 4) (My notes)                               |
| ------------------------------------------------------------------------------- |
| Written by AI · [Regenerate notes ▾]                                           |
| ## Summary …   ## Decisions …   ## Action items  ☐ Anna: send deck by Fri      |
|                                         (editable; saved to notes.md)          |
```

| Status | Header primary | Body |
|---|---|---|
| Live | Red [Stop and write notes] · [Pause] · [Live Assist] · ⋯ › Discard recording | Level meters (You ▮▮▮▯ · Call ▮▮▯▯). Tabs: Live notes ("Appears after about a minute of conversation"; off while Live Assist runs) / Transcript (live, You/Them) / My notes. Banner after 30 s of silent call audio: "No sound from the call. Check the output device. [Open sound settings]". Banner at 1:50: "Audio is saved up to 2 hours. The transcript continues, without speaker labels after that." |
| Processing | [Cancel notes] | Stepper: Saving recording ✓ · Transcribing ✓ · Adding speaker labels (on this computer; "Downloading speaker model 47 MB" the first time) … · Writing notes. The transcript is readable while it runs. |
| Ready | [Export ▾] | Notes (editable, saved), Transcript (click "Speaker 1" to name; names are per part in continued recordings; Ctrl+F finds "3 matches ↑↓"), Live Assist (saved Q&A), My notes (saved) |
| Basic notes | [Regenerate] | "Notes were written with basic formatting because no AI engine was connected." [Connect an AI engine] [Regenerate] |
| Failed notes | [Try again] | "Notes couldn't be written (the AI service didn't respond). Your recording and transcript are saved." [Try again] [Use another AI ▾] |
| Nothing heard | [Transcribe again] | "Nothing was heard. The recording is saved." [Show in folder] |

- **Export ▾:** Copy notes, Copy for email, Copy for Slack, Word (.docx, Markdown rendered), Markdown (.md, safe filename), Notion, Text (.txt), Audio (MP3/WAV).
- **⋯ menu:** Rename, Edit attendees, Show in folder, Delete.
- **Always free:** viewing, copying and exporting. Continue recording and Regenerate (AI) follow the entitlement table.

**File detail**
- **Header:** file name (renameable) · duration · speakers · language.
- **Tabs:** Transcript (timestamps and speakers) and Summary. Summary starts empty with [Write a summary], which reuses the meeting-notes pipeline and the same allowance.
- **Primary:** Export ▾ (Word, Text, Markdown).
- **Other:** "Transcribe again with Most accurate"; ⋯ › Show original file, Delete.
- **Processing:** stepper (Reading · Downloading model · Transcribing 42% · Speakers · Building document) with [Cancel].

**Trash**
- The same list layout. Rows show "Deletes in 23 days".
- Actions: [Restore], [Delete now] (confirm once), and ⋯ › "Empty trash" ("Delete 14 items forever?" with a button labelled [Delete 14 items]).

### 4.6 Meetings flow

**Record sheet**

It opens from the status card, the Meetings view, the tray, Ctrl+N or the meeting hotkey. The hotkey skips the sheet and uses the defaults; the consent line then shows on the capsule for 15 s.

```
| Record a meeting                                                         ✕ |
| Title   [ Meeting · Tue 14:30                                         ]    |
| Record  [ Both sides of the call ▾ ]   Language [ Detect automatically ▾ ] |
| [ ] Keep this meeting on this computer   (on-device transcript and notes)  |
|     Notes use basic formatting unless the on-device AI (1.1 GB) is downloaded. |
| [ ] Start Live Assist too                                          Pro     |
| › Details: attendees (they help the AI name people)                        |
| Let everyone know you're recording.   [Copy notice]                        |
|                                   [Cancel]   [ ● Start recording ]         |
```

- **Copy notice** copies: "Heads up: I'm using Gusan to take notes on this call."
- **Non-Pro users** (if D1 is not approved): the Start button reads [See Pro] and opens the Pro sheet. After a trial activates, the meeting starts automatically.
- **macOS:** the Record chip is fixed to "My microphone only" with an (i).
- **Conflicts:**
  - A running dictation: "Your dictation will stop when the meeting starts."
  - A running file job: "A file is being transcribed (about 6 min left). [Cancel the file and record] [Cancel]".
- **Flow:**
  1. Start closes the sheet. The item appears in Now and is selected, the capsule appears, the tray gets its red badge and status row, and the sidebar live row appears.
  2. Dictation is off until Stop (§4.4).
  3. Closing the window does not stop the recording.
  4. Stop (from the capsule, tray, item or hotkey): the item moves to Processing. A system notification "Notes ready · Weekly sync" appears only if the window is hidden, and the tray gets its accent ring.
  5. Ready: edit, name speakers, export.
- **Errors:**
  - Device lost mid-recording: "Microphone disconnected at 12:34. The recording up to then is saved." [Continue with another mic] [Stop].
  - Disk full: "Not enough disk space to keep recording. The recording up to now is saved." [Stop].

**Meeting capsule**

A floating glass capsule of about 240×44. It sits top-centre by default, can be dragged (position remembered, with a ⋯ › Reset position equivalent), and is hidden from screen sharing when that setting is on.

```
( ● 12:34 · Weekly sync   ▮▮▯ You ▮▯▯ Call   ‖   ■   ✦   ⋯ )
```

- Controls: Pause ‖, Stop ■, ✦ (expand to Live Assist), and ⋯ (Reset position, Hide until Stop). Clicking the title opens the item in the window.
- "Hide until Stop" leaves the tray row and badge as the recording indicator.
- It turns amber with "No call audio · Fix" after 30 s of silent system audio.
- Under reduced motion the dot is static and a "REC" label is added.

### 4.7 Live Assist card (the expanded capsule)

```
Expanded (440-480 px wide, height 360-800 resizable)
+----------------------------------------------------------------+
| ● 12:34 [Stop]  Live Assist     (EN ▾)  [shield]  [⋯]  [_]  [✕] |
| Auto-answer on                                                 |
| ▾ Them: …so can we ship before October?                        |
|   You:  If QA moves to the 20th…                    (2 lines)  |
|----------------------------------------------------------------|
| "Can we ship before October?"                        14:05     |
|  Yes, if the QA freeze moves to the 20th. Two open risks: …    |
|  [Copy] [Pin]                    Transcript · Screen · Cloud   |
|  ---------------------------------------------                 |
|  "What did Anna say about pricing?"                  14:02     |
|                                          ( 1 new answer ↓ )    |
|----------------------------------------------------------------|
| (Answer latest question) (Suggest follow-ups) (Read my screen) |
| [ Ask anything about the call…            ] [Screen ○] [ ➤ ]   |
+----------------------------------------------------------------+
```

- **Primary:** the composer.
  - Enter sends. Ctrl+Enter, or Enter with an empty field, answers the latest question.
  - While an answer streams, ➤ becomes ■ Stop.
  - A new question you ask replaces the one still streaming. Auto-answers never interrupt one you asked for; they queue under it.
  - Answers stream at body-large (18/24).
- **Engine:** Pro answers use Gusan Cloud (today's behaviour), shown in each card's chips. In Privacy mode the card uses on-device AI or says none is available.
- **Idle state** (opened with Ctrl+Alt+A, no recording running):
  - Three steps: "Start listening → ask, or let it answer questions it hears → answers are saved with the meeting".
  - A session line: "Listening to: Both sides · Language: Detect · Screen: off".
  - The consent line with [Copy notice], then [Start listening].
  - A positioning line: "For your own calls. Not for interviews, exams or assessments where help isn't allowed."
  - For non-Pro users: a sample answer card plus [Try Pro free for 3 days], which opens the Pro sheet inside the card.
  - If a recording is already running, the card attaches to it (catching up without answering old questions) and there is no second Start.
- **Shield** (status only, never used as a selling point):
  - Green "Hidden from screen sharing": excluded from shares and recordings made on this PC (Windows 10 2004 or later).
  - Neutral grey "Visible in screen sharing": your choice, not an error.
  - Amber "Can't hide here", with a one-line reason (macOS, Windows before 10 2004, Remote Desktop).
  - All shield colours come from tokens, so they are readable in dark mode.
- **⋯ menu:** Answer questions automatically ✓, Answer in…, Card appearance, Card background, Reset position, Open meeting in Gusan, Settings.
- **Collapsed:** the meeting capsule (§4.6). While collapsed it pulses "Answer ready ›" with a one-line preview, and announces it to screen readers. With reduced motion, the "Answer ready" text appears without the pulse. Double-clicking the header also collapses and expands the card.
- **Close ✕:** while live it collapses to the capsule; when idle it closes.
- **End sheet:** "Meeting saved · 47 min". Steps: ✓ Recording saved · ◌ Speaker labels · ○ Notes. Buttons: [Open notes] [Save audio] [New session]. "You can close this. We'll tell you when notes are ready."
- **Errors:**
  - Inline card with one action, e.g. "No AI engine is connected. [Connect one]" or "The answer didn't arrive. [Retry]".
  - Screen in Privacy mode: the pill is disabled with "Off in Privacy mode".
  - Text-only engine with no OCR available: the answer's chip reads "Couldn't read the screen with this engine".
  - First use of "Read my screen" (both platforms): explain first that "A screenshot of the monitor under your cursor goes to the answering AI (read by Mistral OCR if that AI can't see images). It is never saved." On macOS, the Screen Recording prompt follows.
- **Loading:** a "Warming up…" shimmer on the first answer after a pause, replacing the countdown text.

### 4.8 Files

**"Transcribe a file" sheet**

It opens from the status card, the Files view, the tray, Ctrl+O, or by dropping a file onto the window.

```
| Transcribe a file                                                    ✕ |
| interview_03.m4a · 1 h 12 min                                           |
| Language [ Detect automatically ▾ ]   [✓] Speaker labels  [✓] Timestamps |
| Runs on this computer · about 9 min                                     |
| › Accuracy: Automatic ▾                                                 |
|                                       [Cancel]   [ Transcribe ]         |
```

- **Over 5 hours:** "This file is 5 h 40 min - files can be up to 5 hours. Split it and transcribe the parts."
- **Busy:**
  - During a meeting: "Waiting for the meeting to finish" (queued). The button still reads Transcribe.
  - During a dictation: it starts as soon as the dictation is pasted.
- **After start:** the sheet closes, a row appears in Now, and the item's detail shows the stepper. With D18, dictation can still cut in between chunks.
- **Errors:**
  - "This file type isn't supported. Try mp3, m4a, wav or mp4."
  - "The file couldn't be read. It may be damaged. [Copy details]"
  - Cancelled: the item is removed unless partial text exists ("Cancelled at 42% · [Start again]").

### 4.9 Dictionary

```
| Dictionary                                                   [ + Add word ] |
| [ Search ]                                                                  |
| Words · help Gusan spell names and terms                                    |
|  [Aibuben ✕] [PySide6 ✕] [Anna Sargsyan ✕] …        (paste a list works)   |
| Replacements · always change what it hears                                  |
|  I say "pie side six"   ->   Write "PySide6"                          ✕    |
|  [ + Add replacement ]                                                      |
| [on] Send my words to cloud engines (improves accuracy there)              |
| › Advanced: phrases to always remove                                        |
```

- **Primary:** Add word.
- **Empty state:** examples (a name, a product term, a company) and "Works on this computer and in the cloud."
- **Scope caption,** shown when Mistral is the speech engine: "Mistral can't take word hints, but near-miss spellings are still corrected. Replacements work with every engine."

### 4.10 Settings

```
| Settings                                          [ Search settings   Ctrl+F ] |
| +---------------------+-------------------------------------------------------+ |
| | General             | Dictation                                             | |
| | Shortcuts           | ┌ When you dictate          (•) Exact words          ┐ | |
| |>Dictation           | │                           ( ) Write with AI        │ | |
| | Meetings & Live Ast.| │   Say "email to Anna…" or "make a to-do list".     │ | |
| | AI & engines        | │   3 of 5 free tries left · Translate into [English]│ | |
| | Privacy             | └────────────────────────────────────────────────────┘ | |
| | Account             | Language                  [ Detect automatically ▾ ]  | |
| | About               | I switch languages within one dictation      [ off ]  | |
| |                     | Clean up transcripts                         [ on  ]  | |
| |                     |   › Advanced                                          | |
| +---------------------+-------------------------------------------------------+ |
```

- **Layout:** the sidebar stays visible and Settings has its own section list.
- **Rows** are SettingsCards: title, one-line description, control on the right, maximum width 1000 px. At most about 6 cards per section, plus one Advanced expander nested one level deep.
- **Behaviour:** changes apply instantly. Settings reopens on the last section. Search highlights and scrolls to the matching row.

**General**

| Row | Control | Default |
|---|---|---|
| Appearance | Use system setting / Light / Dark (Windows only; macOS follows the system) | Use system setting |
| Microphone | Dropdown plus a live level meter | System default |
| Startup | "Start Gusan when I sign in" (Windows: manages the installer's Startup shortcut with `--background`; macOS: SMAppService) · "Open the window at startup" | installer choice · off |
| Notifications | Tell me when notes and files are ready | on |
| Permissions | Mac: Microphone, Accessibility, Input Monitoring, Screen Recording, each ✓ / ✗ [Fix] / ○ "asked on first use". Windows: microphone privacy. | — |

**Shortcuts:** one table. Each row has a keycap chip plus [Change] [Clear] [Reset].

| Action | Default (existing users keep theirs) |
|---|---|
| Dictate | Alt+R on Windows (see D3) |
| Press Enter to finish | toggle (D4) |
| Write with AI (dictate) | not set |
| Live Assist | Ctrl+Alt+A (not set on AltGr layouts for new installs) |
| Start or stop a meeting | not set |
| Paste last dictation | not set |
| Open Gusan | not set |

- **Capture sheet:** "Press keys, or click the middle, back or forward mouse button. Esc cancels · Backspace clears."
- **Conflict checks** run both ways ("Ctrl+Alt+A is already used by Live Assist [Replace] [Cancel]"). A warning list covers known clashes:
  - Office: Alt+R, Ctrl+Alt+V/M/T
  - AltGr layouts: Ctrl+Alt+letter, detected from the active keyboard layout
  - macOS: Ctrl+Space, Ctrl+Option+Space, Option+letter, Option+Space, and any Control+Option combination (VoiceOver's modifier)
- **In-app shortcuts** are listed read-only below the table.

**Dictation**
- When you dictate
- Translate into
- Language
- I switch languages within one dictation: "Re-detects after each pause. On-device only."
- Clean up transcripts: "Removes things the speech model sometimes invents, like '[BLANK_AUDIO]' or 'Thank you. Thank you.' Never changes your words." Advanced: noise tags, repeated made-up phrases.
- Remove filler words
- Put my clipboard back after pasting
- Dictation bar: Appearance (Automatic / Light / Dark); Keyboard hints (First 5 dictations / Always / Never); [Reset position]
- Link: "Words and replacements → Dictionary"

**Meetings & Live Assist**
- Record from.
- Call language.
- Remind me to tell people I'm recording.
- Send notes to: Notion. Either "Connected to 'Meeting notes'" [Change page] [Disconnect], or [Connect].
- Keep meeting audio (the Privacy row is mirrored here).
- **Live Assist group** (caption: "For your own calls. Not for interviews, exams or assessments where help isn't allowed."):
  - Answer questions automatically (on).
  - Let Live Assist see my screen: "Only when I turn it on" (new installs) / "Every answer".
  - Answer in: Same as the call.
  - Card appearance: Match app / Light / Dark.
  - Card background: 86-100%.

**AI & engines**

```
Transcription · how speech becomes text
 (•) Automatic (recommended)    This computer. Starts with Fast (140 MB); suggests a larger
                                size for this computer and downloads it only when you say so.
 ( ) This computer only         Private, works offline.
 ( ) Gusan Cloud           Pro  Fastest, nothing to set up. Audio goes through our server
                                to Google Gemini or Mistral.
 ( ) Your own key               Mistral or Google Gemini           [Choose ▾]
 Now using: this computer · Balanced (460 MB, downloaded)
 › Advanced: On-device model (Fast / Balanced / Most accurate · Show all sizes;
             inline download, "Recommended for this computer") ·
             Gusan Cloud uses (Pro) · Model for your provider
AI features · Write with AI, meeting notes, Live Assist
 (•) Automatic   Pro: Gusan Cloud. Otherwise this computer's AI model if
                 downloaded, else your key, else basic formatting (Live Assist needs a real AI).
 ( ) This computer (download 1.1 GB)  ( ) Gusan Cloud  Pro  ( ) Your own key
 Now using: Gusan Cloud (Google Gemini)
 › Advanced: Use a different engine for Meeting notes / Live Assist ·
             On-device AI models (Small / Medium / Large · Show all)
Your API keys  (stored in Windows Credential Manager / macOS Keychain, per account)
 Mistral          Connected · checked today           [Replace] [Remove]
 Google Gemini    Not connected                       [Add key]
 OpenAI or compatible · Anthropic · Cerebras          [Add key]
```

- **Your own key sheet:** Provider → Key (masked, checked on paste: "✓ Works" or "This key was rejected. Check it was copied in full.") → Model ▾ (current models plus "Other model…") → Server address (OpenAI-compatible and Cerebras only) → [Done].
- **Cloud limits:** the app shows no limits (decided 2026-10-04). The proxy keeps a quiet daily safety cap (3000/day; Write with AI 1000/day); if it's ever hit, the app transcribes on the computer and says only that.
- **Privacy mode:** cloud options show "Paused by Privacy mode [Turn off]".

**Privacy**
- **Privacy mode:** "Your voice, text and screen stay on this computer. Cloud engines and screen reading pause, usage statistics pause (D19), and sending to Notion asks first. Account and update checks still connect. Your choices come back when you turn it off. While it's on, dictations aren't saved to your Library and failed dictations aren't kept; meetings are saved on this computer as usual."
- **History:**
  - Save my dictations (on).
  - Delete automatically after [Never ▾].
  - Keep meeting audio [Until I delete it / 30 days / 90 days].
  - Storage: "1.2 GB · [Show folder]".
  - [Delete all history…], which names the count, includes meetings and never touches a live recording.
- **Screen sharing:**
  - "Keep Live Assist out of screen shares and recordings made on this PC" (on). Caption: "Windows 10 2004 or later. Someone could still photograph your screen."
  - Hide the dictation bar from screen sharing (on). Caption: "When off, the bar can appear in shares and uses a plain background instead of live glass."
  - macOS: "Not available on Mac."
- **Usage statistics:** "Share usage statistics", with "What we collect →". It lists: feature events, app version, OS, plan and a random install ID. It never includes audio, text, clipboard, keys, file paths or window titles.
- **Where your data goes:** a read-only table built live from the current config.
  - Rows: dictation audio / Write with AI text / vocabulary / meeting audio and transcript / meeting notes / Live Assist transcript and screen (including Mistral OCR) / Notion / feedback / usage statistics / account and update checks.
  - Columns: where it runs, what is stored, for how long.
  - The copy needs a check against the real retention and training policy (D15).

**Account**
- Identity.
- Plan line: "Pro · Yearly · renews 1 Mar 2027", "Pro trial · 2 days left", "Free", or "Guest · 7 of 10 min left".
- Buttons: [Upgrade to Pro] or [Manage subscription] · Compare plans · [Sign out] ("Your Library stays on this computer").
- Links: Terms · Privacy policy.
- A separate card: "Delete account…" (type DELETE). It explains: "This cancels your subscription right away and deletes your account."
- Developer: preview plan (super-admin only; the plan chip shows "Previewing: Free" in the caution tone).

**About**
- Status: "Gusan 1.11.0 · Up to date · checked 10 min ago [Check now]", or "1.11.2 is ready [Restart to update] · What's new".
- Install updates when I quit (on).
- This computer: CPU · RAM · GPU.
- Links: Website · LinkedIn · Source code · Licences.
- [Copy diagnostics] (version, OS, engine, no content).
- "Try the new Gusan" (only in 1.11-1.12).

### 4.11 First run

- **Where it runs:** in 1.10 it is a standalone 560×620 window, built as a reusable page. From 1.13 it runs inside the shell with the sidebar hidden.
- **Navigation:** each screen has one primary button and Back. Skip is available everywhere except the macOS pre-alert, which has only [Continue], per the HIG.

| # | Screen | Content | Primary |
|---|---|---|---|
| 1 | Welcome | "Speak anywhere you type." The Fast model (140 MB) starts downloading **on this screen**, with a progress line at the bottom. "I already have an account · Sign in". | [Get started] |
| 2 | Microphone | macOS: pre-alert, then the system prompt. Mic dropdown and live meter: "Say something. The bar should move." After 5 s of silence: "We can't hear you. [Choose another mic] [Open sound settings]". | [Sounds good] |
| 3 | Try it | A focused practice box. "Press [Alt] [R], say 'Hello, this is my first dictation', press [Alt] [R] again." If the model isn't ready, the bar shows "Getting ready · 80%" and transcribes when ready. Success: "✓ That's it. It works in any app." "Change shortcut" link. | [Next] |
| 3m | macOS: Use it in any app | Step 3 first uses the [Dictate] button: the app types into its own box, which proves the mic and model with no extra permission. Then Input Monitoring and Accessibility, each with a one-sentence reason, [Open System Settings] and a live ✓ (polling `IOHIDCheckAccess` and `AXIsProcessTrusted`). Then press the real shortcut in the practice box. "Later" is allowed; Home then shows the Fix line. | [Continue] |
| 4 | Account (optional) | "Save your history and get 5 free Write with AI tries." [Continue with Google] [Use email] [Skip for now] | per choice |
| 5 | Help improve Gusan? | Two equal buttons, [Share usage statistics] [Don't share], plus "What we collect". On Windows 11 with the icon not pinned (read-only `IsPromoted` check): a "Find Gusan near the clock" illustration with drag-to-pin instructions. | [Done] → Home (1.11+) / closes, with a tray balloon "Press Alt+R anywhere" (1.10) |

- **Target:** under 90 s and about 5 clicks. There are no engine, language or key questions.
- **Analytics consent:** events from before the answer are buffered locally, sent on "Share" and deleted on "Don't share".
- **Existing users** never see first run. Users with `onboarding_done` and no account see nothing new.

### 4.12 Account, Pro and upgrade

**Plan chip states** (sidebar footer):

| State | Chip | Click opens |
|---|---|---|
| Guest | "Guest · 7 min left" (caution tone at 3 min or less) | Sign in sheet |
| Free | "Aram · Free", with "· 2 AI tries left" when 2 or fewer remain | Account |
| Trial | "Pro trial · 2 days left" | Account |
| Pro | "Aram · Pro" | Account |
| Pro, offline or entitlement unknown | "Aram · Pro", from `entitlement_cache` for this user. Valid for 7 days from the last server check and never past `period_end`. Server-side features (Gusan Cloud) still check with the server. | Account |
| Pro, cloud limit reached | "Pro · cloud limit reached" | Account (usage) |

**Pro sheet** (opened only by a click: a Pro-tagged action, the plan chip, Account › Upgrade or Compare plans)

```
| Meetings with AI notes are part of Pro                                  ✕ |
| [ sample: AI notes for a 30-min meeting — Summary · Decisions · Actions ] |
| With Pro                                                                  |
|  · AI meeting notes, speaker names, continue recordings                   |
|  · Live Assist during your calls                                          |
|  · Write with AI without the 5-try limit                                  |
|  · Gusan Cloud: fastest, nothing to set up                                |
|  (· Priority support, only if still offered: D20)                         |
| ( ) Monthly   €7.99 / month                                               |
| (•) Yearly    €59 / year · €4.92 a month · Save 38%                       |
| [ Start 3-day free trial ]                                                |
| Free for 3 days, then €59 a year from 1 Oct. Cancel anytime in Account.   |
| Secure checkout by Stripe.  Terms · Privacy                               |
| Not now                                                                   |
```

- **Content:** the headline and benefits come from `entitlements.FEATURES`, so none can go missing. The headline names the triggering feature.
- **Guests:** the button reads "Continue: create a free account" and switches the same sheet to the sign-in step. Checkout then continues.
- **Checkout:** the sheet shows "Finish checkout in your browser… [Cancel]" while the existing `watch_for_upgrade` polling runs. When done it shows "You're on Pro ✓", and the blocked action runs (the meeting starts, the Write with AI rewrite runs, the file starts).
- **Not shown during dictation or recording.** Pro-gated actions inside floating surfaces open the sheet inside that surface.

**Sign in sheet**
- Continue with Google, or email.
- A Sign in | Create account switch, defaulting to Create account when there is no known email.
- Name (Create account only).
- Password with show/hide and the rule "at least 6 characters" shown inline.
- **Forgot password?**
- Paste and password managers allowed (WCAG 3.3.8).
- Line: "By creating an account you agree to the Terms and Privacy policy."
- **Google:** "Finish signing in in your browser… [Cancel]", then success, or "Sign-in didn't complete. [Try again]".
- **Email:** verification is polled, then you are signed in automatically.

**Limits UX:**
- **Free tries used:** exact words are pasted, the bar shows a [See Pro] note, and the Dictation setting line updates.

### 4.13 Update

- **Background:** the check runs at launch and every 12 h. The download happens silently, verified against the manifest's sha256.
- **Surfaces when ready:**
  - Home banner: "Version 1.11.2 is ready. [Restart to update] · What's new · Skip this version ✕".
  - The tray item and accent ring.
  - The About status line.
- **Install on quit** happens when `install_updates_on_quit` is on (the default). The installer is per-user (`PrivilegesRequired=lowest`), so this runs without an admin prompt. Installs made "for all users" would need one, so they get only "Restart to update".
- **What's new:**
  - A sheet whose body is `pending_update_body`.
  - After an update (detected via `previous_version`), a one-time banner "Updated to 1.11 · What's new". The window does not open by itself.
- **Skip this version** sets `dismissed_update_version`, finally giving that key a use.
- **Errors:**
  - "Couldn't download the update. [Try again] [Get it from the website]"
  - "The update couldn't be installed. Your version still works. [Copy details]"
- **macOS:** reads the manifest's `macos` entry, downloads and verifies the DMG and opens it, or opens the release page. It never uses the `.exe`. Proper in-place updating needs Developer ID signing (D14).
- **Never** a modal, never while dictating or recording, and never with Yes as the default.

### 4.14 Command palette (Ctrl+K)

```
| [ > priv                                                         Esc ] |
| Actions        Privacy mode                                     ( on) |
| Settings       Keep Live Assist out of screen shares            ( on) |
| Library        [people] Privacy review · 3 Sep · "…the privacy policy…" |
```

- **Sources:** one registry (`commands.py`) of about 25 actions, plus every settings row and FTS results. The actions:
  - Dictate, Record, Stop, Live Assist, Transcribe a file, Paste last
  - Privacy mode, Change language, Change microphone, Add word
  - each Settings section, Check for updates, Send feedback
  - Upgrade / Manage subscription, Sign in/out, Export list, Quit
- **Enter:** runs the action. Boolean settings toggle inline; other settings open Settings scrolled to the row.
- **Empty:** "Type an action, a setting, or words from a transcript."

---

## 5. Naming and microcopy

### 5.1 Old name → new name

| Today | New |
|---|---|
| Transcribe (the app) | **Gusan** |
| Settings (the window) / Open Settings | **Gusan** / **Open Gusan**. Settings is a section inside it. |
| History, History Log, Transcription History | **Library** (sidebar: Home, Dictations, Meetings, Files) |
| Transcribe Files / Audio file → Word document | **Transcribe a file** (action), **Files** (view) |
| Record Meeting, Smart Meeting Transcription | **Record a meeting** (action), **Meetings** (view) |
| Stop & Generate Notes / Generating AI Meeting Notes… | **Stop and write notes** / **Writing notes…** |
| Retry Summary / Reset / Resume session | **Regenerate notes** / **New meeting** / **Continue recording** |
| Minimize to Tray (Keep Recording) / Abort & Discard | (removed) / **Discard recording** |
| Live Assistance, overlay, prompter, copilot, private call assistant, "Live session HH:MM" | **Live Assist** everywhere. Sessions are "Meeting · Tue 14:05" with a Live Assist tag. Telemetry keeps the `live_prompter_*` event names for continuity. |
| Answer / Follow-ups / Solve screen / Auto | **Answer latest question** / **Suggest follow-ups** / **Read my screen** / **Answer questions automatically** |
| Private / Visible / Not private / Private n/a | **Hidden from screen sharing** / **Visible in screen sharing** / **Can't hide here** |
| Smart Actions, OUTPUT MODE, Smart actions | **Write with AI** (mode, shortcut and item action: one term); the setting is **When you dictate** |
| Transcribe only | **Exact words** |
| Primary Action Engine | **AI features** |
| Transcribe Pro - managed cloud, Managed by Transcribe Pro, Fast cloud transcription | **Gusan Cloud** |
| Local Offline AI (Whisper), Local Offline LLM Engine (Qwen / Gemma) | **This computer** |
| Mistral Voxtral STT (Cloud), Google Gemini Speech (AI Studio), Google Cloud Engine | **Mistral**, **Google Gemini** (under Your own key) |
| OpenAI-compatible | **OpenAI or compatible service** |
| API Key / API Base URL (Optional…) / API Model Identifier / Custom Model… | **Key** / **Server address** / **Model** / **Other model…** |
| Whisper TINY … LARGE-V3 | **Fast / Balanced / Most accurate** (sizes under "Show all sizes") |
| Default Spoken Language / Auto-detect / Multilingual | **Language** / **Detect automatically** / **I switch languages within one dictation** |
| Custom Vocabulary / Always replace these words (Heard → Replace with) | **Dictionary › Words** / **Replacements** (I say → Write) |
| Corrections / Clean up transcriptions | **Clean up transcripts** |
| Default Meeting Audio Device / Meeting Recording Mode | **Record from** |
| System sound + Microphone / Microphone only / System sound only | **Both sides of the call** / **My microphone only** / **Call audio only** |
| Privacy Mode (on-device only) | **Privacy mode** ("Your voice, text and screen stay on this computer") |
| Save local transcription history | **Save my dictations** |
| Share anonymous usage metrics to improve Armenian AI models | **Share usage statistics** |
| Your Device / RAM: min 8 GB · GPU Acceleration: No | **This computer** / "Needs 8 GB memory · uses your graphics card" |
| Open Source & Contributions | **Source code** · **Licences** |
| GUEST / FREE / PRO | **Guest / Free / Pro** (sentence case) |
| Upgrade to Pro / Sign up · N min left | **Upgrade to Pro** / **Create free account** |
| "long_files is a Pro feature." | **Files can be up to 5 hours. Split longer ones.** |
| Check for Updates / Install Update vX / About (Update vX!) | **Check now** / **Restart to update** / **About** |
| Dictation Hotkey | **Dictation shortcut** |
| "Transcribing · gemini-2.5-flash AI" / "Thinking" / "Pasted to cursor" | "Transcribing · Gusan Cloud" / "✦ Writing email…" / "Pasted" or "Copied · press Ctrl+V" |
| Rule-based formatting | **Basic formatting (no AI connected)** |
| Open folder / Download audio / Save recording… | **Show in folder** / **Export › Audio** |
| Admin · Force tier | **Developer: preview plan** |
| Send feedback or request a feature | **Send feedback** |
| Manage subscription (tray) | **Manage subscription** (Account only) |

### 5.2 Tone rules

- **Style:** plain words and sentence case. Semibold rather than bold. No ALL CAPS, no "!", no marketing adjectives, no "(Pro)" suffixes.
- **Name the result, not the technology.** Provider and model names appear only under Your own key, Advanced, and where data leaves the computer.
- **Errors:** what happened, what is safe, then one action. Never blame the user, and never show exception text (offer "Copy details").
  - "Gusan can't hear you: microphone access is off. [Open settings]"
  - "Notes couldn't be written (the AI service didn't respond). Your recording and transcript are saved. [Try again] [Use another AI]"
  - "No internet · using on-device transcription."
- **Upsells:** state the benefit and the terms, and give a neutral decline ("Not now"). No guilt-trip wording.
- **Hotkeys** are always shown as keycaps built from the real binding (⌘ and ⌥ on macOS), never hard-coded ("Alt-R").
- **Privacy claims** are factual and platform-scoped ("Hidden from screen sharing on this PC (Windows)"), never "undetectable" or "invisible". "Anonymous" is used only for data with no identifier.
- **Live Assist positioning:** a helper for your own calls. It is never marketed as hidden, and never for interviews, exams or assessments. Being hidden from shares appears only as a status and a privacy setting, never as a benefit or onboarding step.
- **Enforcement:** one string table (`ui/strings.py`) plus a test that fails on retired terms. The list: Smart Actions, Live Assistance, History Log, Primary Action Engine, Multilingual, PRO in caps, "Alt-R", "undetectable", "invisible", "interview", and the old app name used as a name ("Transcribe App", "Transcribe Pro", "Open Transcribe", "Quit Transcribe"; the verb "Transcribe a file" stays).

---

## 6. Design system

### 6.1 Tokens (source: `scratchpad\redesign\tokens.json`)

**Type** (size/line-height in logical px)
- Windows: Segoe UI Variable Text/Display, falling back to Segoe UI.
- macOS: `QFontDatabase.systemFont`.
- Mono: Cascadia Mono / SF Mono.

| Token | Windows | macOS | Use |
|---|---|---|---|
| caption | 12/16 | 11/14 | timestamps, meta, keycaps (the floor) |
| body | 14/20 | 13/16 | all UI and list text |
| body-strong | 14/20 · 600 | 13/16 · 600 | group headers, row titles |
| body-large | 18/24 | 15/20 | live transcript, Live Assist answers |
| subtitle | 20/28 · 600 | 17/22 · 600 | item titles, sheet titles |
| title | 28/36 · 600 | 22/26 · 700 | page title (one per page) |
| title-large | 40/52 · 600 | 26/32 · 700 | first run and Pro headline only |

The reading column is capped at 720 px (14/22 in the detail pane). Timers use tabular figures. All 10-11 px uses are retired.

**Spacing**
- 4 px grid: 0, 2, 4, 8, 12, 16, 20, 24, 32, 40, 48, 56.
- Control gap 8 · label to control 12 · card to card 4 · group gap 12.
- Card padding 16×12 · page margin 24 (12 when narrow) · section gap 32 · expander indent 48 · settings max width 1000.

**Radii**
- Controls: 4 on Windows, 6 on macOS.
- Cards, menus, sheets and the content layer: 8.
- Floating surfaces (bar, capsule, card): 12.
- Pills and chips: 999.
- Nested radii are concentric. Window corners are left to the OS.

**Sizes**
- Controls 32 (compact 24 = minimum target) · icon button 32 · nav item 36 · rail 48 · pane 240 · toggle 40×20.
- Rows 56 (dictation) and 64 (meeting/file).
- Icons 16 / 20 / 24 / 48.
- Focus ring 2 px outer + 1 px inner, offset 1.

**Elevation**

| Level | Use | Treatment |
|---|---|---|
| e0 | window base | none |
| e1 | cards, rows | 1 px stroke.card, no shadow |
| e2 | dragged or raised item | shadow4 |
| e3 | menus, combo popups, tooltips | native OS shadow |
| e4 | sheets, floating surfaces | shadow28 (sheets shadow64). Painted only on our own frameless surfaces; no `QGraphicsDropShadowEffect` inside windows. |

**Colour** (all opaque; light / dark)

| Token | Light | Dark | Rule |
|---|---|---|---|
| bg.base / layer / card | #F3F3F3 / #F9F9F9 / #FFFFFF | #202020 / #282828 / #2B2B2B | window / sidebar / content |
| bg.card_hover | #F6F6F6 | #323232 | |
| text.primary / secondary / tertiary / disabled | #1B1B1B / #5C5C5C / #6E6E6E / #A0A0A0 | #FFFFFF / #CFCFCF / #9E9E9E / #6E6E6E | Tertiary is at least 4.6:1 on opaque layers and **never used on glass**. Disabled is only for disabled controls, never for explanations. |
| stroke.control / card / strong | #E5E5E5 / #EBEBEB / #8A8A8A | #3A3A3A / #333333 / #9A9A9A | Strong is used for input bottoms and toggle-off borders (3:1 or better) |
| accent.fill / hover / pressed | #2563EB / #1D4ED8 / #1E40AF | #60A5FA / #93C5FD / #3B82F6 | The one accent: primary button, selection, on-toggles, links, progress |
| accent.on / accent.text | #FFFFFF / #1D4ED8 | #0B1220 / #7DB6FF | |
| pro.fill / pro.on / pro.text | #7C3AED / #FFFFFF / #6D28D9 | #C4B5FD / #0B1220 / #C4B5FD | Only the Pro tag, plan chip and Pro sheet headline; not used for AI content |
| record.fill / record.on / record.text | #C42B1C / #FFFFFF / #B42318 | #FF6B6B / #0B1220 / #FF99A4 | Only live recording and Stop. White on dark record.fill is 2.78:1, so dark mode uses record.on. |
| success / caution | #0F7B0F / #9D5D00 | #6CCB5F / #FCE100 | Always paired with an icon or text |
| focus.outer / inner | #1B1B1B / #FFFFFF | #FFFFFF / #000000 | |
| glass.plate / stroke | rgba(249,249,249,.86) / rgba(0,0,0,.08) | rgba(32,32,32,.82) / rgba(255,255,255,.08) | Floating surfaces only |

**Contrast** (recomputed by `contrast.py`, light / dark)

| Pair | Light | Dark |
|---|---|---|
| text.primary on card | 17.22 | 14.16 |
| text.secondary on card | 6.69 | 9.09 |
| text.tertiary on base | 4.60 | 6.08 |
| accent.on on accent.fill | 5.17 | 7.36 |
| accent.text on card | 6.70 | 6.75 |
| pro.on on pro.fill | 5.70 | 10.14 |
| record.on on record.fill | 5.66 | 6.75 |
| success on card | 5.44 | 6.98 |
| caution on card | 5.25 | 10.73 |
| stroke.strong on card (3:1 required) | 3.45 | 5.03 |
| glass worst case, secondary text | 4.60 (0.86 over black) | 5.87 (0.82 over white) |
| glass worst case, tertiary text (not allowed) | 3.51 | 3.41 |

**Retired:** gold cards, blue-to-violet gradients, purple glows, and the leftover colours #3b1818 and #27272a. The 68 hex values collapse into these tokens.

**Theme**
- The app follows the system via `QStyleHints.colorScheme` and `colorSchemeChanged`. PySide6 6.8.2 also has `setColorScheme` for the override.
- Windows offers an override (Use system setting / Light / Dark); macOS has none, per the HIG.
- Floating surfaces follow the app theme unless overridden.

### 6.2 Components (`ui/components/`)

Each component reads tokens at paint time and exposes `accessibleName` and `accessibleDescription`. Painted ones register a `QAccessibleWidget` interface via `QAccessible.installFactory` (available in PySide6 6.8.2). List items expose `Qt.AccessibleTextRole` and `Qt.AccessibleDescriptionRole` from the model.

| Component | Notes |
|---|---|
| NavRail / NavItem | 240/48, indicator slide 167 ms, keyboard arrows, tooltips in rail mode |
| PageHeader | title, one primary button, ⋯ |
| Button | primary / secondary / subtle / destructive / icon-only (needs an accessible name) |
| ToggleSwitch, Checkbox, Radio, SegmentedControl | Fluent recipe; state via dynamic properties |
| SettingsCard, SettingsExpander | title, description, control on the right, disabled reason |
| TextField, SearchField, KeyField (masked + auto-check), Combo | bottom-edge focus in stroke.strong, then 2 px accent |
| Chip | filter / status / tag ("Pro" neutral outline) / engine ("On this computer") |
| Keycap | renders a real binding, including mouse buttons and ⌘/⌥ |
| ListRowDelegate | dictation / meeting / file / Now; hover and focus actions; "New" dot |
| DetailPane scaffold, InPaneTabs | |
| Banner, Toast (with Undo), Sheet (scrim, focus trap), EmptyState, Skeleton | |
| ProgressBar, ProgressRing, Stepper | |
| LevelMeter | You / Call |
| PlanChip, ShieldBadge (3 states), StatusCard, TrayBadge (dot / triangle / ring / shield) | |
| GlassPlate, DictationBar, MeetingCapsule, LiveAssistCard, AnswerCard, Composer | floating layer; each raises status announcements |
| ShortcutCapture | keys and mouse buttons, conflict checks |
| CommandPalette | |

### 6.3 Iconography

- **Set:** Fluent UI System Icons (MIT), vendored as SVG through the bundled qsvgicon plugin. 20 px regular, filled when selected; 24 px in list rows, 48 px in empty states. The same set is used on macOS, because the SF Symbols licence limits them to Apple platforms.
- **Kind icons:** mic (dictation), people (meeting), document (file), plus ✦ for sessions that used Live Assist.
- **Tray:** a monochrome mic in 16/20/24 ICO, and a template PNG on macOS. Badge shapes per §4.1. The flag circle stays as the app and installer icon only.
- **No emoji in UI chrome.** The warning emoji in notes becomes a caution icon plus text.

### 6.4 Motion

| Use | Duration | Easing |
|---|---|---|
| Hover, press | 83 ms | standard |
| Toast, tooltip, sidebar indicator, exits | 167 ms | exit (1,0,1,1) with a fade |
| Page change (fade + 8 px rise), sheet, floating surface enter, bar state | 250 ms | enter (0,0,0,1) |
| New row insert (height 0 → 56) | 250 ms | enter |
| Detail pane swap | 83 ms crossfade | |
| Recording dot pulse | 1.6 s loop, only on the bar, capsule and Now row | |

- **Reduced motion:** Windows `SPI_GETCLIENTAREAANIMATION`, macOS `accessibilityDisplayShouldReduceMotion`. Everything becomes 0 ms or a crossfade of 83 ms or less; pulses become a static dot plus a "REC" label.
- **Reduced transparency:** Windows "Transparency effects" off, or macOS Reduce Transparency, gives opaque plates. The Live Assist card must honour this too; today it doesn't.
- **The tray icon never animates.**

### 6.5 Accessibility requirements (acceptance criteria for every screen)

1. **Contrast:** text at least 4.5:1, and UI boundaries, states and focus at least 3:1, enforced by token tests. Colour is never the only signal, including tray badges and shield states.
2. **Size:** minimum text 12 px on Windows and 11 pt on macOS. Minimum target 24×24.
3. **Keyboard:** everything is reachable. Tab order follows reading order, arrows work inside composite widgets, F6 cycles regions, Enter and Space activate, Esc closes. No keyboard traps except the sheet focus trap, which Esc releases. Every action on a non-focusable floating surface has a keyboard path (Now row, tray, palette).
4. **Focus ring:** a visible two-tone ring on every focusable element, including painted rows, toggles, nav and floating-surface buttons (keyboard focus only).
5. **Names and roles:** every icon-only control has an accessible name. Painted widgets expose role, name, state and value through `QAccessible` factories. A test fails on any unnamed icon button.
6. **Status messages** (WCAG 4.1.3): these are announced for the bar, capsule, card, toasts and banners.
7. **Timing** (WCAG 2.2.1): toasts with actions persist at least 10 s and pause on hover or focus.
8. **Screen-reader testing** with NVDA and Narrator on Windows and VoiceOver on macOS, for: first run, dictate, find a transcript, record a meeting, upgrade. Shortcuts must never use VoiceOver's Control+Option modifier.
9. **Scaling:** layouts hold at 100 / 125 / 150 / 200% scale and at the minimum window size without clipping. That includes the Record sheet, which is unusable at 125% today.
10. **System settings:** reduced motion and reduced transparency are honoured (§6.4). **High contrast is honoured from 1.13:** detect Windows `SPI_GETHIGHCONTRAST` and macOS "Increase contrast" (NSWorkspace) directly, and drop the app stylesheet in favour of system colours. This needs no Qt upgrade; PySide6 6.10+ `contrastPreference` can replace the detection later.
11. **Dragging** always has a click alternative (the bar and capsule have Reset position; reordering has menu equivalents).
12. **Help** is in the same place on every page (WCAG 3.2.6). Sign-in allows paste and password managers (3.3.8). Focus is never hidden behind the floating surfaces (2.4.11).

---

## 7. Technical approach (PySide6)

### 7.1 Stack decision

- **Keep QWidgets** (feasibility research). Measured cost: plain widgets 0.14-0.35 s per page; QML 1.4-2.1 s and +20-35 MB.
- **Styling:** one token module, `ui/theme.py`, generates a `QPalette` and a stylesheet.
  - `QWidget.setStyle` doesn't reach child widgets, so **Fusion is set app-wide** (`QApplication.setStyle("Fusion")`) in 1.11. Legacy windows get a pinned light palette and one day of visual checks.
  - While classic windows remain (1.11-1.13), the v2 palette and QSS are **scoped to v2 top-level windows** (window-level `setPalette`/`setStyleSheet`, which cascade to children).
  - In 1.14 they move to `QApplication` once the classic windows are deleted. They are regenerated when the colour scheme changes.
- **State changes** use dynamic properties (`setProperty("tone", …)` then re-polish), never per-widget `setStyleSheet`.
- **Hand-paint only the defining components** (§6.2). All lists use `QAbstractListModel` plus `QStyledItemDelegate` plus a proxy model.
- **Not used:**
  - QML
  - qfluentwidgets (GPLv3 or a commercial licence, which clashes with the app's non-commercial licence; also per-widget styling)
  - qt-material
  - frameless windows (you lose snap layouts and the system shadow)
  - **Mica / vibrancy**: text in a translucent window loses ClearType, and it adds cost for little gain

### 7.2 Architecture changes

| Module | Responsibility | Replaces or unblocks |
|---|---|---|
| `config_store.py` | Single writer. `get`, `set(key, value, source)`, `set_many`. Qt signal `changed(key, old, new)`. Atomic write debounced 250 ms. Pure-function migrators (`v1 → v2`) with a `config.v1.bak` backup; old keys kept for one release. Keeps the per-account swap (`reconcile_user_secrets`) and keyring split intact, and adds the new engine keys to `USER_SCOPED_SECRET_KEYS`. | `cfg_working` snapshots, Save, stale overwrites |
| `engines.py` | `resolve(feature, cfg, auth, net_state) → EngineChoice(kind, provider, model, label, runs_where, reason)`. Used by the runtime **and** by every "Now using" line, bar chip and data-flow table. Handles privacy mode, the cloud cap, offline, missing keys, cloud-to-local fallbacks, the Pro-always-cloud rule for Live Assist, and Mistral OCR. | "Shown Local, sent to cloud" |
| `entitlements.py` (extended) | One `FEATURES` table: id → {guest, free, pro, limit, headline, benefit, gate_copy}. Adds `live_assist`, `long_files`, `meeting_ai_notes`, `resume`, `write_with_ai`. Cached Pro via the new `entitlement_cache` (per user, `checked_at` + `period_end`, 7-day grace). This deliberately changes the module's "never decides Pro on its own" rule for on-device features only (D16a); Gusan Cloud stays server-checked. | Inconsistent paywall reasons, missing benefits |
| `library_store.py` (grows out of `meeting_store.py`) | SQLite in WAL mode with an FTS5 index. FTS5 is present in the Windows build; a startup probe falls back to LIKE if a Mac build lacks it. Single writer thread fed by a queue; readers on workers. Schema: `items(id, kind, assist, title, created, duration_s, language, status, speech_engine, ai_engine, folder, raw_text, text, action, error, audio_path, deleted_at, meta_json)` plus `fts(title, text, transcript, notes, answers, my_notes)`. Meeting and file folders keep today's layout (meta.json v2 adds `kind`). **Migration is read-only and additive:** index `meetings/` and `history.json`, drop meeting duplicates by `audio_dir`, never delete originals, and keep writing `history.json` through 1.13 for downgrade safety. "Rebuild library" action. | 5 result surfaces, 3 searches, double storage, the 200 cap |
| `meeting_session.py` | `MeetingSession(QObject)`: `start(opts)`, `pause`, `resume`, `stop`, `discard`, `regenerate_notes(item, engine)`. Signals `state`, `levels(mic, sys)`, `chunk_text`, `recap`, `step`, `finished(item_id)`, `error`. Owns the save-before-transcribe order, resume across parts, the 2-hour audio cap, diarization (including the model download), dictation and file exclusion, and saving My notes. Pause is built on the existing resume-part mechanism. | Live Assist calling `meetings_win._start_meeting` on a hidden window |
| `exporters.py` | One exporter per format for any item: docx with Markdown rendered, md, txt, csv list, Notion for any kind, email/Slack text, and audio via one `export_recording` | 3 audio exporters, 2 CSV exporters, raw `##` in Word |
| `commands.py` | Registry of actions and settings rows (id, title, keywords, shortcut, handler, enabled-reason). It powers the palette, the tray, Help › Shortcuts and search. | |
| `hotkeys.py` | Registration and suppression. Windows: the `keyboard` library with `suppress=True`, plus a mask-key injection for Alt combos; mouse buttons via pynput `win32_event_filter`. macOS: a Quartz event tap / pynput `darwin_intercept` when Accessibility is granted, after verification. Two-way conflict checks, a known-conflict table (including VoiceOver), AltGr detection. | Silent drops, keys leaking into other apps |
| `updater.py` | One pipeline: check, download, verify (manifest sha256), install on quit, skip, What's new. Picks the per-OS asset from the manifest's existing `windows`/`macos` entries. | Two flows, the Mac failure |
| `file_jobs.py` | File transcription in 30 s windows, releasing the inference lock between windows so dictation can cut in (D18); queue behind meetings | Two-way exclusion that blocks dictation for a whole file |
| `ui/shell.py` | `QMainWindow`, NavRail/rail, lazy `QStackedWidget` page factories (try/except error card per page), banner / sheet / toast hosts, routing `open(route)`, macOS app menu | 5 separate dialogs |
| `ui/pages/…`, `ui/sheets/…`, `ui/floating/…` | Home/Library, Dictionary, Settings sections, sheets; the floating dictation bar (`overlay.py`), capsule and Live Assist card | `settings.py` (4,756 lines), `history.py`, `meeting_detail.py`, the `meetings.py` UI, `onboarding.py`, `pro_dialog.py`, `shiny_tabs.py` |
| `ui/strings.py` | Glossary and string table | Inconsistent names |
| `AppController` | `show_settings(section)`, `show_history`, `show_meeting`, `show_file_transcribe` and the IPC actions keep their names and route to `shell.open()` when `ui_v2` is on. Windows are built lazily after the tray. Model scans run off the GUI thread. No stylesheet re-apply in `save_config`. IPC action names (`live_prompter`, …) stay for compatibility. | 5-7 s Settings build, late tray icon |

**Platform work:**
- **Windows:**
  - DWM dark title bar and corner preference.
  - A caret check for an honest "Pasted".
  - Registry read for the tray pin hint.
  - Deep links to `ms-settings:` pages.
  - Startup shortcut management at the installer's path, so the uninstaller still removes it.
  - `SPI_GETHIGHCONTRAST` detection.
  - UIA notifications for status announcements.
- **macOS:**
  - Activation-policy switch for the Dock icon while the window is open, and the standard app menu.
  - Template menu-bar icon.
  - Permission polling: `AVCaptureDevice` authorization for the mic, `AXIsProcessTrusted`, `IOHIDCheckAccess`, `CGPreflightScreenCaptureAccess`.
  - Detecting grants lost after an update (ad-hoc signing changes the code hash).
  - SMAppService for startup.
  - Confirm pyobjc is bundled by PyInstaller (pynput already depends on it).
  - `QDesktopServices` instead of `os.startfile`.
  - Keychain writes through the Security framework instead of passing the secret as a `security` command-line argument.

**Performance budgets** (enforced by loose timing tests):
- Tray icon visible within 1.5 s of a warm launch.
- Window first paint under 500 ms warm.
- Page switch under 100 ms.
- Search under 150 ms for 5,000 items.
- The dictation bar appears within 150 ms of the hotkey.
- No stylesheet work on save.

### 7.3 Testing strategy

| Layer | What | Where |
|---|---|---|
| Pure logic (runs under today's stubbed CI) | Config migrators (every v1 shape → v2, idempotent, downgrade keys kept, per-account scope preserved, keyring split preserved); engine resolver matrix (tier × privacy × network × keys × cap × fallback); entitlement table and cache expiry; library migration (dedupe, idempotency, no deletes); FTS queries; exporters (golden files); hotkey conflict table; glossary test; colour lint (no hex outside `theme.py`, with an allowlist for legacy files that shrinks each release); token contrast test (including a ban on tertiary text on glass); **Privacy-mode network guard** (with privacy on, only account and update requests are allowed; analytics, cloud and OCR calls fail the test) | ubuntu job (existing) |
| Characterization tests (before any refactor) | `_start_meeting`, `_stop_meeting`, `_process_meeting_notes`, resume, discard, "nothing captured", the 2-hour cap path, rule-based downgrade, with a stub recorder and a stub engine. Existing tests keep passing after the move into `MeetingSession`. | ubuntu + windows |
| Real-Qt smoke tests (new job, separate pytest process because `test_core` stubs PySide6) | pytest-qt offscreen: each page and sheet builds in light and dark; every icon-only button has an accessible name; routing; a record → stop → notes run with a stub engine; list model with 5,000 items; the palette | windows-latest, macos-latest (the runners already build there) |
| Manual QA matrix (each release) | Win10 / Win11, light / dark, 100/125/150/200%, taskbar on all 4 edges, multi-monitor, Remote Desktop, transparency off, reduced motion, high contrast. macOS 13-26 **on real hardware**: permissions, re-grant after update, menu bar, Dock, updater, VoiceOver. Office apps (including the lone-Alt KeyTips check) and Slack/Teams for key leakage; an AltGr layout (Polish, German). | Founder, with a written checklist |
| Usability | A 5-user test on 5 tasks (first dictation; record and share notes; find last week's "budget" meeting; switch to on-device only; upgrade) before the 1.12 default flip | |

Per the release-approval rule: every release is tested, and the dev build is restarted and checked, before the founder approves the commit, push and release.

---

## 8. Roadmap

Estimates are for one developer (the founder with Claude Code) and already include the 25-40% overrun the feasibility review expects.
- **Total:** about 147-187 engineer-days (29-37 weeks).
  - The earlier 130-165 omitted docs, signing, startup, guest feedback, file chunking and the privacy network guard.
  - It also mis-summed 1.10.
- **Gate:** 1.11 onward starts only after the 1.10 data review (§8.3).

### 8.1 Phases

**1.10 "Foundations"**: 35-46 days. Nothing moves in the navigation.

| Work | Days |
|---|---|
| Updater: background download verified against the manifest sha256, install on quit, banner in the legacy Settings header + tray item + About, Skip this version, What's new, Mac path via the existing `macos` manifest entry | 5-6 |
| Dictation bar: commit the working-copy capsule; Getting ready state; clickable errors with Retry and failed audio kept 7 days; honest Pasted / Copied; plain engine names and the cloud-fallback chip; action names; "off during a meeting" / file-job notes; guest-minute and cloud-cap notes; status announcements | 5-6 |
| Hotkeys: suppress the dictation hotkey, Esc and Enter while recording (Windows), with the Alt mask key; mouse-button suppression via `win32_event_filter`; two-way conflict checks; Esc cancels capture; Backspace clears; **mouse buttons kept**; known-conflict, AltGr and VoiceOver warnings; real hotkey in all messages; Mac verification | 4-6 |
| First run v1 (standalone window, reusable page): mic meter, model pre-download, practice, optional account, analytics question, macOS permission polling, tray pin hint | 6-7 |
| Minimal ConfigStore: single writer and signals; legacy Settings Save writes only changed keys; Meetings writes through the store | 2-3 |
| Truthfulness fixes: `engines.resolve` driving both runtime and labels (rule_based → "Gusan Cloud" for Pro), `managed_provider` default, `entitlement_cache` with 7-day grace, Privacy mode restores `pre_privacy_state` and pauses usage statistics (D19) with honest copy, key status only after a check | 3-4 |
| Tray v2: menu, monochrome icon with shaped badges, status row and Stop, Recent, Paste last, Quit guard. Manage subscription: commit the working-copy billing-portal client; the founder deploys the function. | 3 |
| Pro dialog quick fixes: benefits from the entitlement table (adds Live Assist), guest returns to checkout, readable reasons | 1-2 |
| Live Assist quick fixes: language follows the Dictation language instead of forced English; Stop shows the notes instead of pointing at a hidden window | 1 |
| Analytics: new events in the client `ALLOWED_EVENTS` and `_sanitize` **and** the server allowlist, consent buffering, installer pre-tick removed | 2-3 |
| Performance: lazy window construction, model scan off the GUI thread, no stylesheet re-apply on save | 1-2 |
| CI: real-Qt jobs on windows-latest and macos-latest | 1-2 |
| Docs: README (fix the stale Live Assist, Notion and Privacy claims), release notes, help page | 1 |

- **Risks:**
  - Key suppression on macOS is unproven. Mitigation: ship suppression on Windows only; on Mac, stop advertising Enter.
  - Suppressing Alt+R without the mask key would trade the Review tab for KeyTips. Mitigation: the mask key plus a manual Office check.
  - The updater can't reach 1.5.x installs until they update once through their old prompt. Accept this and measure.
  - The billing-portal function must be deployed by the founder (the founder sets the secrets).
  - The Supabase allowlist deploy must happen with the release.
- **Flag:** none needed. Every change is a fix to an existing surface. First run applies only to new installs.

**1.11 "Library"**: 39-47 days, behind `ui_v2`.

| Work | Days |
|---|---|
| `theme.py` tokens, palette + QSS scoped to v2 windows, app-wide Fusion with a pinned light palette for legacy windows (plus legacy visual checks), component set v1 with accessibility interfaces | 8-10 |
| `library_store`: schema, WAL, FTS5 with LIKE fallback, single writer, read-only index of meetings and history.json, dedupe, dual writes, Trash, Rebuild | 7-9 |
| Shell: QMainWindow, NavRail/rail, lazy pages, routing, sizing, banners / sheets / toasts, macOS app menu | 4-5 |
| Home: status card, Now, date-grouped list, checklist; Dictations / Meetings / Files views | 4-5 |
| Detail panes (dictation, finished meeting with editable saved notes, file) plus `exporters.py` | 6-7 |
| Transcribe a file sheet, saved file items, queue behind meetings, chunked file jobs so dictation can cut in (D18) | 5-6 |
| Dictionary page | 2 |
| Command palette v1 (actions, pages, Library) | 2 |
| Docs and release notes | 1 |

- **Interim behaviour:** Settings in the v2 shell opens the legacy Settings dialog until 1.13. Recording still uses the legacy Meetings window, but its result lands in the Library.
- **Risks:**
  - Migration trust. Mitigation: additive only, a "Migrated 214 items" note, originals untouched.
  - App-wide Fusion changing legacy looks. Mitigation: pinned palette plus a manual pass over every legacy window.
  - FTS5 missing on Mac. Mitigation: the startup probe and fallback.

**1.12 "Meetings & Live Assist"**: 31-38 days.

| Work | Days |
|---|---|
| Characterization tests for the meeting pipeline | 3-4 |
| Extract `MeetingSession` with no behaviour change; MeetingsWindow becomes a thin view | 5-6 |
| Record sheet (with dictation and file conflicts); live item (level meters, silence warning, 2-hour cap banner, Pause, Discard, saved My notes); processing stepper with Cancel notes and the speaker-model download | 7-8 |
| Meeting capsule (with Hide until Stop); tray and sidebar live rows | 3-4 |
| Live Assist card: feed, composer, drawer, shield, ⋯, capsule mode, end sheet, saved answers, language chip, non-Pro preview, positioning line, OCR chips, reduced transparency | 7-8 |
| Regenerate notes, speaker names (per part), title and attendees sent to the AI, Copy for email / Slack, Notion for any item | 3-4 |
| macOS Developer ID signing and notarization in CI (D14; Apple enrollment started during 1.10) | 2-3 |
| Docs and release notes | 1 |

- **Risks:** this touches the most fragile code (save-before-transcribe, resume parts, diarization, discard). Mitigations:
  - move the code first, redesign second;
  - the stub-engine smoke test;
  - keep the legacy Meetings window reachable for one release.
- **Flag:** `ui_v2` becomes the default for new installs; existing users can opt in from About.

**1.13 "Settings, first run, Pro"**: 29-39 days.

| Work | Days |
|---|---|
| Config migration v2 (per-provider keys in the keyring, per-account scope extended, `speech_engine`, per-feature overrides, `pre_privacy_state`, Live Assist language merge, Notion token to the keyring, `live_assist_auto` removed, backup) plus the engine resolver everywhere | 5-7 |
| Settings: 8 sections, SettingsCard / Expander, instant apply, search, disabled reasons | 8-10 |
| AI & engines: model rows with inline download, Your own key sheet, keys list with auto-check | 4-5 |
| Shortcuts table and ShortcutCapture | 2-3 |
| First run moved into the shell; macOS re-grant flow | 2-3 |
| Entitlement table, Pro sheet, Sign in sheet, plan chip, resume after checkout | 4-5 |
| Palette covers settings; glossary test on; v2 routes no longer reach legacy Settings | 1-2 |
| Startup toggle (Windows Startup shortcut, macOS SMAppService); high-contrast detection | 1-2 |
| Guest feedback backend (D10: optional email, per-install rate limit) | 1 |
| Docs and release notes | 1 |

- **Risks:**
  - Key migration bugs (the most tangled state, now also per account). Mitigations: the pure-function migrator is fully tested, and the "Now using" line shows what will actually run.
  - Dark mode surfacing leftovers. Mitigation: the lint allowlist must be empty for v2 files.
- **Flag:** `ui_v2` is on for everyone. About › "Use classic windows" stays for this one release.

**1.14 "Polish & platform"**: 13-17 days, plus optional items.

| Work | Days |
|---|---|
| Dark mode everywhere. Optional: PySide6 6.10+/6.11 as its own build for native `contrastPreference` (drops macOS 12; no known install uses it: the two Macs are Darwin 24 and 25). | 4-5 |
| Accessibility audit with NVDA and VoiceOver, 150/200% fixes | 3-4 |
| Delete classic code (`settings.py`, `history.py`, `meeting_detail.py`, the `meetings.py` UI pages, `onboarding.py`, `pro_dialog.py`, `shiny_tabs.py`); move palette and QSS to `QApplication`; trim the bundle (exclude QML/Quick/`opengl32sw` after a verified build, roughly 35-40 MB) | 2-3 |
| Audio player in the meeting detail (QtMultimedia); click a transcript line to seek | 3-4 |
| Docs and release notes | 1 |
| Optional: Hold to talk (D11) | +3 |
| Optional: Quick popover Ctrl+Alt+… with Recent and search (D12, only if the data supports it) | +5-7 |

### 8.2 Shipping behind a flag without breaking current users

- **`ui_v2` = off | preview | on.**
  - 1.11: off by default, with a "Try the new Gusan" toggle in About that takes effect without a restart.
  - 1.12: on for new installs.
  - 1.13: on for all, with "Use classic windows" available.
  - 1.14: classic removed.
- **The tray, hotkeys, dictation bar and IPC work in both modes** at every step. `AppController.show_*` routes by flag.
- **Data:**
  - `library.db` is an index that can always be rebuilt from files.
  - `history.json` is still written through 1.13.
  - Meeting folders are unchanged.
  - The config v2 migration keeps v1 keys and writes `config.v1.bak`.
  - A downgrade to 1.10 loses nothing. File items, saved Live Assist answers and Trash state reappear after upgrading again, and items in Trash may show again in 1.10's history.
- **Existing users keep their current value for every changed default:** hotkeys, Enter, Write with AI state, history (kept forever), analytics choice, screen reading, Live Assist theme and language, cloud routing. Each change gets a line in the release notes.
- **Crash safety:** each page factory is wrapped, and a failing v2 page shows an error card, not a crash. Any unhandled v2 exception within 3 launches offers "Switch back to classic" (1.11-1.13).

### 8.3 What to measure

- Events are opted-in and **never contain content**. They carry a random install ID, so they are described as usage statistics, not "anonymous".
- Ship them with 1.10, updating the client `ALLOWED_EVENTS`, `_sanitize` and the server allowlist in the same release. Event names like `live_prompter_*` stay.

| Area | Events |
|---|---|
| Reach | `app_started{version, ui_v2, launch_source}`, `update_available`, `update_downloaded`, `update_clicked{surface: banner\|tray\|about}`, `update_skipped`, `update_installed{from,to}`, `update_failed{stage}` |
| Activation | `first_run_step{step, result, ms}`, `mic_level_ok{ms}`, `mac_permission{name, granted, after_update}`, `first_dictation{ms_since_install, engine_kind}` |
| Core loop | `transcription_completed{engine_kind, ai_action, chars_bucket, paste_result: pasted\|copied, fell_back}`, `transcription_failed{reason, engine_kind}` (separate from action failures), `dictation_retry{source}`, `dictation_refused{reason: meeting\|file}`, `hotkey_registration_failed{which}`, `hotkey_changed{which, input: key\|mouse}` |
| Navigation | `window_opened{source: tray_click\|tray_menu\|hotkey\|launch\|notification\|first_run}`, `nav_opened{page, source: sidebar\|shortcut\|palette\|link}`, `tray_item_clicked{item}`, `palette_used{result_kind}`, `settings_section_opened{section, source}` (including the default section) |
| Library | `library_search{view, results_bucket}`, `item_opened{kind, status}`, `item_action{kind, action: copy\|paste_last\|export_<fmt>\|write_with_ai\|regenerate\|retry\|delete\|restore}` |
| Capture | `capture_started{kind: meeting\|live_assist\|file, source}`, `capture_finished{kind, status, duration_bucket}`, `meeting_warning{type}`, `notes_generated{engine_kind, ok}`, `live_assist_answer{trigger, used_screen, ok, first_token_bucket}` |
| Money | `paywall_viewed{feature, source}` (fixes the empty `feature` prop), `paywall_action{action}`, `checkout_opened{plan}`, `pro_activated{plan}`, `resumed_action{feature}`, `cloud_cap_reached` |
| Settings | `setting_changed{key}` (allowlisted keys; enum values only), `privacy_mode_changed{on, source}` |

**Success criteria.** With the current base (about 2-5 active non-admin installs), percentages are noise. Each target applies only once at least N installs (D16) are in the sample; below that, review the counts by hand.

| Release | Target |
|---|---|
| 1.10 | ≥70% of launching installs on the latest version within 14 days · ≥80% of new Windows installs (≥60% on Mac) reach a first dictation · median time to first paste under 90 s · `transcription_failed` under 2% of dictations |
| 1.11 | ≥30% of weekly active installs reuse an item (copy, paste last, export, Write with AI) · ≥20% of window opens use search or a view · zero data-loss reports |
| 1.12 | 0 lost recordings (`capture_finished{status=lost}`) · notes failure under 5% · every Live Assist session ends in a saved item |
| 1.13 | Engine changes reverted within 7 days under 30% · `paywall_viewed → trial` baseline established · Settings opened by at least 10% of weekly actives (one home that is findable) |

**Decision rules**, written down now and applied 60 days after 1.13:
- **Gate after 1.10:** proceed to 1.11 if the update rule is met (≥60% of launching installs on 1.10 within 21 days) and first run has seen at least N new installs. The founder sets N (D16). If update adoption fails, fix distribution before redesigning.
- **Dictionary** gets under 3% of page views → fold it into Settings › Dictation.
- **Write with AI** is under 5% of dictations after the shortcut exists → move it under Advanced.
- **Meetings** is opened by ≥20% of Free users but trial conversion stays under 2% → test "first meeting free" or the D1 variant.
- **`window_opened{source=tray_click}`** is low while Recent and Paste last are high → build the Quick popover (D12).
- **Fewer than 50% of first runs reach a first paste** → run a 5-user first-run test before anything else.

---

## 9. Renaming to Gusan

The app is renamed from Transcribe to **Gusan**. The founder decided this on 30 Sep 2026. This section covers the name, whether it is free to use, when it ships, what changes, what must stay the same, and what the founder does outside the code.

### 9.1 The name

- **Gusan**, pronounced goo-SAHN (Armenian: Գուսան).
- **The story in one line:** a gusan was an Armenian bard who carried stories by voice. Gusan carries yours: you speak, it writes.

| Use | Write | Don't write |
|---|---|---|
| The app, in prose | **Gusan** ("Gusan is still recording") | Transcribe, Transcribe App, GUSAN |
| The paid plan | **Pro** inside the app ("Upgrade to Pro", §5.1). **Gusan Pro** where it stands alone: the Pro window title, Stripe, receipts, the website. | Transcribe Pro, GUSAN PRO |
| The managed cloud engine | **Gusan Cloud** (§5.1) | Transcribe Pro (managed cloud), Gusan Pro (managed cloud) |
| Actions | The verb stays: **Transcribe a file**, **Transcribe again** | "Gusan a file" |
| Maker line | **Gusan by AIBUBEN** | Transcribe by Aibuben |
| During the switch only | **Gusan (formerly Transcribe)**: Terms, Privacy, README and the one-time note (§9.6) | |

### 9.2 Is the name free? (checked 30 Sep 2026)

Web research, re-checked against live registries, stores and APIs. This is not legal advice: a trademark lawyer should confirm before filing.

| Check | Result | Status |
|---|---|---|
| Other software called Gusan | None on the web, the Apple and Mac App Stores, Google Play, the Microsoft Store, Product Hunt or GitHub | Confirmed |
| Apple store search | Apple reads "gusan" as "gusano" and shows worm and snake games | Confirmed |
| A Korean logistics driver app called Gusan (earlier research) | Not found again | Unclear |
| Exact GUSAN mark in classes 9 or 42 (software) | None in the USPTO (US), EUIPO (EU), the UK or TMview. TMview has 975 "gusan" records; 41 are in classes 9/42 and none reads exactly GUSAN. The only exact-name class 9 marks are two Turkish "güsan" marks from 1991, both ended. | Confirmed |
| Exact GUSAN marks in other classes | About 30 worldwide, none in 9 or 42. France has "Gusan" in classes 15/16/41 (musical instruments, printed matter, education and entertainment; filed 8 May 2026) and GUSAN in class 36 (real estate). Others are in Germany (29/41/43), Russia (33), Paraguay and Vietnam (35), and elsewhere. | Confirmed |
| Close names in software classes | GUZAN (US, classes 6/9, electrical grounding gear). Gusanz (UK, class 9, registered; goods list not re-checked). GUSSAN (Brazil, class 42). GUSUN (class 9 in China, India and Vietnam, phone and communications equipment). Gusandi (Iceland, filed 17 Sep 2026 in classes 35/41/42, still pending). | Confirmed |
| WIPO (Madrid) | TMview's WIPO data has no GUSAN and nothing in 9/42. The WIPO Global Brand Database itself was not searched (bot check). | Unclear |
| Armenia (AIPA) | Not checked. It matters because gusan is an ordinary Armenian word. | Unclear |
| Domains that look free | gusan.ai, getgusan.com, gusanapp.com, usegusan.com, trygusan.com (the registries return "not found" and DNS has no record). Premium pricing for gusan.ai was not checked. | Confirmed |
| Domains that are taken | gusan.com (since 2001, parked with Sedo, expires 2030; unknown whether it is for sale). gusan.io (since 2014, no site). gusan.app (since April 2026, a remote-access login page). | Confirmed |
| Plain "gusan" handles | Taken on LinkedIn (a French real-estate network, 463 followers) and GitHub (a personal account). YouTube @gusan returns "not found", so it looks unclaimed. | Confirmed |
| getgusan, gusanapp and similar | Free on LinkedIn, YouTube and GitHub | Confirmed |
| X and Instagram | @gusan and @gusanai are in use (Instagram @gusan has 0 posts). getgusan, gusanapp and similar show nothing, but that can also mean suspended or reserved. | Unclear |
| Meaning in Armenian and Russian | Armenian: a bard, singer and storyteller. Russian гусан: a dictionary word for an Armenian folk singer. Both positive. Some old texts looked down on gusans as low-class entertainers; that is history only. | Confirmed |
| Meaning in Spanish (main risk) | "Gusano" means worm and is also a Cuban slur for exiles. "Gusa" is slang for hunger in Spain. | Confirmed |
| Other languages | Welsh: a form of "cusan" (kiss). Icelandic: "the splash". Hindi: sounds like "gussa" (anger). Romanian: sounds like "gușă" (goiter). Arabic, Okinawan and Chinese: nothing negative found (Chinese without a native check). | Confirmed |
| Korean and Turkish | Korean 구산 is a Seoul place name and station; its "burial site" sense couldn't be re-checked. Turkish: nothing negative found, but not ruled out. | Unclear |

**What to do, in this order:**
1. **Ask a trademark lawyer** to close the two gaps (WIPO and Armenia) and to look at the pending Icelandic "Gusandi" filing in class 42.
2. **File an EU trademark (EUIPO) for GUSAN in classes 9 and 42** (downloadable software and online software services) **before announcing**. Add Armenia and other countries later if needed.
3. **Don't make the name public until the filing is in:** no website, social or release-notes use, and no deploy of gusan.aibuben.xyz (already set up in the uncommitted transcribe-site/wrangler.jsonc:4, 15).
4. **Register gusan.ai and getgusan.com now.** Both look free today.
5. **Claim one handle family everywhere**, for example **getgusan**, plus YouTube @gusan. On X and Instagram, the only real test is trying to register.
6. **Always pair the name with a plain description** ("Gusan: voice typing for Windows and Mac"), because Apple's search mixes it up with "gusano".

### 9.3 When it ships

**Recommendation: ship the rename in 1.10 "Foundations", everywhere at once, on one day.**

Why 1.10:
- 1.10 already rebuilds the places where the name shows: the tray (Tray v2), first run, the updater banner and What's new, the Pro dialog and the installer's analytics page (§8.1).
- 1.10 builds the What's new banner that carries the one-time "Transcribe is now Gusan" note (§9.6).
- 1.11 to 1.14 wait for the 1.10 data review (§8.3, D16). A rename that waits for them may never ship. Meanwhile 1.10 already shows "Gusan Cloud" (the truthfulness row in §8.1) inside an app still called Transcribe.
- It is the cheapest moment on Mac. Ad-hoc signing already resets permissions on every update (D14), and Developer ID signing in 1.12 will fix the bundle id for good. Only two Mac installs are known (§2, P2).
- **Cost:** an estimated 3-4 extra days on 1.10's 35-46 (strings, installer clean-up, Windows version info, Mac bundle, tests, website and legal copy).

"At once" means:

| When | What |
|---|---|
| Weeks before | Lawyer check and EU filing (§9.2). Domains and handles. The Google consent-screen change, which may need Google's review. |
| Release day | Publish 1.10 on GitHub. Publish the website (Lovable and the Cloudflare site). Rename the Stripe product and portal. Switch the Supabase emails and redeploy `confirmed` and `submit-feedback`. Update the GitHub About panel. |
| After | Old web addresses keep redirecting. Change the card statement descriptor only after telling customers (0 paying subscribers today, D1). |

As for every release: it is tested, and the dev build is restarted and checked, before the founder approves the commit, push and release (§7.3).

### 9.4 What changes in the app

Paths starting with `src/`, `public/`, `vite-plugin-` or `transcribe-site/` are in the website repo (aibuben). Everything else is in this repo.

| Area | Today | New | Notes |
|---|---|---|---|
| Window and tray | Tray header "Transcribe  v{version} · PRO" (main.py:2164) | "Gusan v{version} · Pro" | Tray v2 (1.10) rebuilds this menu anyway |
| Window and tray | Tray tooltip "Transcribe" / "Transcribe - Pro" (main.py:2247) | "Gusan" / "Gusan - Pro" | |
| Window and tray | No app name set (main.py:3799), so untitled windows such as the dictation bar (ui/overlay.py:313) and the screen snip (ui/live_assist.py:510) are named "TranscribeApp" | Add `qapp.setApplicationName("Gusan")` right after main.py:3799 | Not `setApplicationDisplayName`: that adds " - Gusan" to every window title |
| Window and tray | "Welcome to Transcribe", "Sign in to Transcribe", "Transcribe is an always-on assistant…" (ui/onboarding.py:26, 116, 432, 438) | "Welcome to Gusan", "Sign in to Gusan", "Gusan is an always-on assistant…" | First run v1 (1.10) rebuilds most of this |
| Window and tray | Pro window "Transcribe Pro" and "TRANSCRIBE PRO" (ui/pro_dialog.py:19, 36; main.py:2584) | "Gusan Pro" | No capitals (§5.2) |
| Window and tray | "Transcribe Pro" in upgrade and error copy (main.py:3592; actions.py:345, 397, 467; ui/file_transcribe.py:98; ui/meetings.py:923; ui/settings.py:3773) | "Gusan Pro", or "Pro" inside a sentence | |
| Window and tray | Engine labels "Transcribe Pro (managed cloud)", "Transcribe Pro - managed cloud (no key needed)", "Managed by Transcribe Pro" (actions.py:48; ui/settings.py:2127, 2258) | "Gusan Cloud" | As in §5.1 |
| Window and tray | "Try again or restart Transcribe" (ui/overlay.py:110) | "Try again or restart Gusan" | Change tests/test_overlay.py:46 and 259 with it |
| Window and tray | "Transcribe App will record system loopback audio…" (ui/meetings.py:199) | "Gusan will record…" | |
| Window and tray | Export names and footers: `transcribe_history.<fmt>` (ui/settings.py:3409; ui/history.py:360), "· Transcribe App" (ui/meetings.py:1516; ui/file_transcribe.py:589; ui/meeting_detail.py:237), Word author "Transcribe App" (docx_export.py:163) | `gusan_history.<fmt>`, "· Gusan", author "Gusan" | Files already exported keep the old name |
| Installer and shortcuts | `MyAppName "Transcribe"` (installer/installer.iss:6) | "Gusan" | Renames the Apps entry ("Gusan 1.10.0"), the Start Menu, desktop and Startup shortcuts, "Launch Gusan" and "Start Gusan automatically…". The old shortcuts must be removed (§9.6). |
| Installer and shortcuts | "Help improve Transcribe by sharing safe usage events…" (installer.iss:88) | "Help improve Gusan…" | 1.10 also removes the pre-tick (D5) |
| Installer and shortcuts | The exe has no version info (no `--version-file` at .github/workflows/build.yml:123 or build.ps1:17), so Windows shows "TranscribeApp" in Task Manager and Startup apps | Add a version file: ProductName and FileDescription "Gusan", CompanyName "AIBUBEN" | The exe file name stays (§9.5) |
| Installer and shortcuts | Mac menu bar, Force Quit and permission lists show "TranscribeApp", taken from `--name` (build.yml:221) | Set CFBundleName and CFBundleDisplayName to "Gusan" (next to build.yml:256) | Finder shows the .app file name (§9.6) |
| Installer and shortcuts | Mac permission prompts "Transcribe records your microphone…", "Transcribe pastes…", "Transcribe needs to listen…" (build.yml:257-264) | "Gusan records…", "Gusan pastes…", "Gusan needs…" | |
| Installer and shortcuts | DMG volume "TranscribeApp" (build.yml:278) | "Gusan" | Nothing depends on it |
| Installer and shortcuts | Source-install scripts "Transcribe App" (setup.ps1:1; run.bat:7; setup_autostart.ps1:4, 11, 14) | "Gusan" | setup_autostart.ps1 must also delete the old "Transcribe App.lnk" |
| Updater | "Transcribe - Update Available" (main.py:2707), "A new version (…) of Transcribe is available!" (ui/settings.py:4526), "…the latest version of Transcribe." (ui/settings.py:4574) | "Gusan" | 1.10 replaces the modal with the banner (§4.13). Only the words change, never the download (§9.5). |
| Updater | Release notes from docs/release-notes/<tag>.md (build.yml:315) | The v1.10.0 notes open with "Transcribe is now Gusan" | Add one line: the download files keep their TranscribeApp names on purpose, so updates keep working |
| Notifications | "Transcribe is still recording in the background…" (ui/meetings.py:1690) | "Gusan is still recording…" | |
| Notifications | No AppUserModelID is set anywhere, so Windows takes the sender name from the exe or its Start Menu shortcut | "Gusan", once the version file and the renamed Start Menu shortcut ship | Check on a real install (§9.8) |
| About and legal | About heading "Transcribe" (ui/settings.py:3471); "Transcribe App is 100% Open Source!" (ui/settings.py:3571) | "Gusan"; "Gusan is…" | |
| About and legal | "Transcribe App — Non-Commercial License" (LICENSE:1) | "Gusan — Non-Commercial License" | The copyright line stays |
| About and legal | Terms "the Transcribe app" (src/pages/Terms.tsx:30, 86); Privacy "makes Transcribe…for Windows", "Transcribe is not directed at children" (src/pages/Privacy.tsx:83, 213) | "Gusan (formerly Transcribe)" | Bump "Last updated" (Terms.tsx:52, Privacy.tsx:49). Privacy.tsx:83 wrongly says Windows only; add Mac. |
| README | "Transcribe" as the app (README.md:3, 28, 30, 64, 66, 70, 80, 161, 304; RELEASING.md:1) | "Gusan", plus one line: "Gusan was called Transcribe until v1.10" | Keep the download links and file names (README.md:18-22, 62, 74, 277) and the data path (README.md:123) |
| README | Mac steps quote "TranscribeApp" (README.md:76-79, 82) | "Gusan" | Only in the release that renames the .app (§9.6) |
| README | "Newer versions also use update-manifest.json and .sha256 files" (RELEASING.md:14) | Correct it: the hard contract is the latest release plus the fixed installer name | Shipped updaters download by tag and name (main.py:2733-2742) |
| README | Banner image text "Transcribe" (assets/banner.svg:51) | "Gusan" | Shorter word; re-centre it |
| Website | Product card "Transcribe App" (src/data/products.ts:47) | "Gusan" | The slug `transcribe-app` (products.ts:46) stays, or gets a redirect |
| Website | Header, nav and footer "Transcribe", "Transcribe is built in Yerevan" (src/components/transcribe/TranscribeLayout.tsx:107, 110, 138, 234) | "Gusan"; drop "built in Yerevan" (no Armenian focus - 2026-10-04) | |
| Website | Titles and schema name "Transcribe: Free Voice Typing…", "Transcribe by AIBUBEN" (src/transcribe/seo.ts:11, 29, 84-104; vite-plugin-blog-prerender.ts:634-639, 734; public/llms.txt:24-29) | "Gusan: Free Voice Typing for Windows & Mac", "Gusan vs Wispr Flow", "Gusan by AIBUBEN" | The OG image shows no product name, so it stays |
| Website | Page copy in DownloadButton, DownloadsSection, CompareCTA, FeaturesSection, PricingSection, ShareLinkButtons, the three compare pages, TranscribeHome and faq.ts | "Gusan" | TranscribeHome is the place for the name's story |
| Website | "Drag Transcribe to Applications… so Transcribe can hear you" (src/transcribe/next-steps.tsx:12) | "…so Gusan can hear you" now | The first mention must match the real .app name (§9.6) |
| Website | The transcribe-site copy of all of the above, plus "Transcribe by AIBUBEN" (transcribe-site/src/transcribe/seo.ts:125, 144), its llms.txt, NotFound.tsx and README | The same changes | Same strings on the same lines as the main site |
| Emails | Email-confirmed page "Transcribe - Email confirmed", "Your Transcribe account is verified", "Transcribe by Aibuben" (supabase/functions/confirmed/index.ts:6, 23-25) | "Gusan" | A function redeploy, not an app release. Keep the `confirmed` name. |
| Emails | Feedback issues "Submitted from Transcribe" (supabase/functions/submit-feedback/index.ts:133); User-Agent "transcribe-feedback" (141) | "Submitted from Gusan"; "gusan-feedback" | Redeploy |
| Emails | Browser page after Google sign-in "You're signed in to Transcribe" (auth.py:72, 77, 81) and after connecting Calendar (calendar_sync.py:48, 52, 54); server name "TranscribeAuth/1.0" (auth.py:100) | "Gusan" | |
| Emails | Supabase Auth emails (confirm sign-up, reset password, magic link) | "Gusan" | Dashboard only (§9.7) |

### 9.5 What must stay the same

Existing users must upgrade and keep everything: settings, history, meetings, local models, sign-in and keys. These names are invisible, or appear only in download links, so keeping them costs nothing.

| Keep | Where | Why |
|---|---|---|
| Installer id (AppId) | installer/installer.iss:17 | A new id makes Windows see a second app: two Apps entries, a parallel install, and an updater that adds a copy instead of upgrading |
| Data folder "Transcribe" | storage.py:11; installer.iss:69, 102 | It holds settings, history, meetings, local AI models (several GB) and consent choices. A new name starts every user from zero. |
| Keyring name "TranscribeApp" | storage.py:12 | It holds the sign-in token, API keys and the Calendar link. A new name signs everyone out and loses their keys. |
| Installer download name `TranscribeApp-Windows-Setup.exe` | main.py:2735; ui/settings.py:4545; build.yml:178; RELEASING.md:7 | Every installed copy downloads this exact name. A new name strands them on their current version. A Gusan-named copy may be added, never instead. |
| GitHub repo `Aram2K/transcribe-app` | main.py:64-66; ui/settings.py:4380 | The updater asks this repo for the latest version. If it is ever renamed, never create a new repo at the old name: old copies run whatever installer it serves, unchecked (main.py:2733-2742). |
| Windows exe `TranscribeApp.exe` | installer.iss:10; build.yml:123 | Windows remembers the pinned tray icon by exe path, and taskbar pins and the Startup shortcut point at it. A new name sends the tray icon back into the hidden overflow. The version file shows "Gusan" instead (§9.4). |
| Cloud function names `transcribe-proxy`, `transcribe-analytics`, `confirmed`, `stripe-webhook` | main.py:50, 117 | Installed copies, email links and Stripe call these addresses. Renaming breaks Pro cloud dictation, analytics, email confirmation or billing. |
| Sign-in callback `http://127.0.0.1:53682/auth/callback` | auth.py:47 | It must stay on the Supabase allow-list, or Google sign-in breaks for every installed copy |
| Single-instance port 47823 and the `transcribe-ok` handshake | main.py:70, 373, 384 | During an update the old and new copy must recognise each other, or two tray icons run |
| Saved setting value `transcribe_only` | main.py:101 | It is stored in users' config.json. A new value would misread their choice. |
| Installer task names `desktopicon` and `startup` | installer.iss:52-53 | Inno uses them to keep each user's desktop and startup choices on upgrade. Only the descriptions change. |
| Stripe price IDs | supabase/functions/stripe-webhook/index.ts:30-31 | They carry no name. New prices would break the plan mapping. Rename the product in the dashboard only. |
| Usage event names | telemetry.py:58 | They match the server allow-list and keep the history comparable. None contains the product name. |
| Old web addresses: `/transcribe`, `/products/transcribe-app`, transcribe.aibuben.xyz | src/App.tsx:55-58; public/sitemap.xml:9; supabase/functions/billing-portal/index.ts:22 | Search engines and shared links use them, and the billing portal returns to /transcribe. If pages move, keep these as permanent (301) redirects. |
| Internal names | `TRANSCRIBE_*` variables (storage.py:23), logger names and transcribe.log (main.py:73, 79) | Never shown to users. Renaming only adds risk. |

The Mac and zip download names (`TranscribeApp-Mac.dmg`, `TranscribeApp-Windows.zip`) are used only by links, not by any updater. Keep them for now so shared links keep working.

### 9.6 Migration steps

**Windows installer** (installer/installer.iss)
1. **Install folder:** keep `AppId` and the default of reusing the previous folder. Upgraders stay in the old `…\Transcribe` folder; new installs go to `…\Gusan`. Nothing in the code depends on the folder.
2. **Start Menu:** set `UsePreviousGroup=no` so the folder becomes "Gusan". Use `[InstallDelete]` to remove the old `Transcribe.lnk` and `Uninstall Transcribe.lnk` (per-user and all-users Start Menu), then the old folder if it is empty.
3. **Desktop:** delete `{autodesktop}\Transcribe.lnk`. Users who had a desktop shortcut get `Gusan.lnk`.
4. **Startup:** delete `{userstartup}\Transcribe.lnk`, or the app starts twice at boot. If the user had turned "Transcribe" off in Task Manager › Startup apps, copy that setting to `Gusan.lnk` (Windows stores it per shortcut name), so startup stays off.
5. **Exe:** keep `TranscribeApp.exe` (installer.iss:10; build.yml:123; build.ps1:17) and add the version file, so Windows shows "Gusan".

**App start**
6. Call `qapp.setApplicationName("Gusan")` right after main.py:3799.

**Mac** (.github/workflows/build.yml:221-281)
7. **Bundle:** rename to `Gusan.app`, and pin the bundle id with `--osx-bundle-identifier` to a final value chosen now (for example `xyz.aibuben.gusan`). Today PyInstaller takes the id from `--name`, so changing the name alone would change the id silently. Change the plist path, codesign and `-srcfolder` lines (build.yml:256, 272-273, 279) with it.
8. **Old copy:** on first launch, if `/Applications/TranscribeApp.app` exists, offer to move it to the Trash (never delete it). Say in the notes that Mac permissions must be granted again; they already reset with every ad-hoc-signed update (D14).
9. **Docs:** change the Mac steps in README.md:76-82 and the website (src/transcribe/next-steps.tsx:12 and the transcribe-site copy) in the same release.
   - If the .app rename is rejected: keep `TranscribeApp.app`. The menu bar and prompts still say Gusan, but Finder and Applications say TranscribeApp.

**Source installs**
10. setup_autostart.ps1 creates `Gusan.lnk` and deletes the old `Transcribe App.lnk`.

**Website**
11. **One home for the product page:** today the main site uses aibuben.xyz/transcribe (src/data/transcribe-release.ts:50) and the uncommitted transcribe-site uses gusan.aibuben.xyz. Pick one and 301-redirect the others, including transcribe.aibuben.xyz. Lovable may not do server redirects, so use Cloudflare redirect rules.
12. **If `/transcribe` moves**, change these together: src/App.tsx:55-58, src/transcribe/TranscribeApp.tsx:21-24 (the prerendered pages have their own routes), seo.ts, the prerender route list, the internal links, and the `BILLING_RETURN_URL` secret.

**One-time note for upgraders**
13. **The banner:** show "Transcribe is now Gusan. Same app, and your settings, history and sign-in are kept. · What's new ✕" once, to installs whose settings came from 1.9.x or older, then remember it was shown. In 1.10 it sits in the Settings window header with the update banner (§8.1); from 1.11 it goes on Home. Never a modal, and the window never opens by itself (§4.13).
14. **What's new and the GitHub release notes** (docs/release-notes/v1.10.0.md, read by build.yml:315) open with the same line, the name's story in one sentence, and "Download file names still start with TranscribeApp, so updates keep working."

**User data**
15. No data migration. Past exports, meeting notes and history files keep the old name. They are the user's files.

### 9.7 Outside the app (the founder does these in dashboards)

- [ ] **Trademark:** lawyer check of WIPO and Armenia, then the EUIPO filing in classes 9 and 42, before any public use (§9.2).
- [ ] **Stripe:** rename the product to "Gusan Pro". The name isn't in the code: main.py:57-58 holds only the Payment Link URLs.
  - Check each Payment Link's confirmation message and redirect, the Customer Portal's business name, headline and links, and the receipts.
  - Keep the price IDs.
  - Change the card statement descriptor only after telling customers.
- [ ] **Google OAuth consent screen:** app name, logo, and home, privacy and terms links, for Google sign-in and for the Calendar client. A name or logo change may need Google's review, so start early.
- [ ] **Supabase Auth:**
  - Email templates (confirm sign-up, reset password, magic link), the sender name and the Site URL.
  - Keep `http://127.0.0.1:53682/auth/callback` on the Redirect URLs list.
  - Redeploy `confirmed` and `submit-feedback` with the new copy (the usual deploy flags).
  - Set `BILLING_RETURN_URL` if the product page moves. Keep every function name.
- [ ] **GitHub:** keep the repo name. Update the About panel (description, website, social preview image). Publish the v1.10.0 notes.
- [ ] **Domain and DNS:**
  - Register gusan.ai and getgusan.com.
  - Deploy the gusan.aibuben.xyz Worker (transcribe-site/wrangler.jsonc), and 301 transcribe.aibuben.xyz to it.
  - Disconnect the old Cloudflare Pages project, or pushes may deploy to both hosts.
  - The Worker doesn't load Lovable's analytics script, so download-click events stop there unless it is added.
- [ ] **Website (Lovable):** publish the renamed pages, and Terms and Privacy with new dates. Upload a new icon only if the icon changes.
- [ ] **Code signing:** nothing to rename today. The Windows build is unsigned and the Mac build is ad-hoc signed (build.yml:272). Set up Developer ID (D14) under the final bundle id.
- [ ] **Social handles:** claim one family (for example getgusan) on X, Instagram, LinkedIn, YouTube and GitHub, plus YouTube @gusan.
- [ ] **Support email:** aibuben.xyz@gmail.com (main.py:54) has no product name. No change.

### 9.8 Tests to add

**Automated** (in `tests/`)

| Test | What it checks |
|---|---|
| No old name on screen | Scans user-facing strings (ui/, main.py, actions.py, auth.py, calendar_sync.py, docx_export.py, installer.iss and the plist strings in build.yml). It fails on "Transcribe" used as the app's name: "Transcribe App", "Transcribe Pro", "Transcribe Cloud", "Welcome to Transcribe", "Open Transcribe", "Quit Transcribe", "restart Transcribe". Verbs ("Transcribe a file", "Transcribe again") and the kept names in §9.5 pass. When `ui/strings.py` lands, these join the retired terms in §5.2. |
| Updater still finds the release | tests/test_core.py:477-512 keep passing. New: main.py and ui/settings.py build `…/releases/download/{tag}/TranscribeApp-Windows-Setup.exe`, and build.yml still publishes that file. |
| Kept names don't drift | APP_DIR_NAME is "Transcribe", SECRET_SERVICE is "TranscribeApp", the AppId is unchanged, and the installer's data paths (installer.iss:69, 102) match APP_DIR_NAME. The function URLs, REDIRECT_URI, SINGLE_INSTANCE_PORT and the handshake are unchanged. |
| App name | After start-up, the application name is "Gusan", and no window title ends in " - Gusan" |
| Error hint | tests/test_overlay.py:46 and 259 expect "restart Gusan" |

**By hand, before release**

| Test | Pass when |
|---|---|
| Windows upgrade over 1.9.x | Set up 1.9.3: sign in, add an API key, dictate, record a meeting, tick the desktop and startup shortcuts, turn startup off in Task Manager, pin the tray icon. Then update through the in-app updater. Pass: one "Gusan 1.10.0" entry in Apps, and the same install folder. Still signed in, with keys, history, meetings and local models all there. Only Gusan shortcuts, startup still off, tray icon still pinned. "Gusan" in Task Manager, the microphone-in-use tooltip and the notification header. The one-time note shows once. |
| Old copy running during the update | 1.9.3 is running when 1.10 starts: still one tray icon |
| Fresh Windows install | Gusan everywhere, installed to `…\Gusan` |
| Mac upgrade over 1.9.x | Settings, history and sign-in kept. Prompts and menus say Gusan. The old TranscribeApp.app is offered to the Trash. One entry in each Privacy list. |
| Website, emails and billing | Every page says Gusan, and old addresses return 301. A test sign-up email and the confirmed page say Gusan. Stripe checkout, receipt and portal say Gusan Pro. |

---

## 10. Decisions for the founder

| # | Decision | Recommendation | Why |
|---|---|---|---|
| D1 | Free users and meetings | Free can record meetings with an on-device transcript and speaker labels, and the first 2 AI notes are free. Pro unlocks unlimited AI notes, Live Assist, Continue recording and Gusan Cloud. If rejected: keep the Pro wall, plus "See example notes". | Organic paywall exposure was never measured, and there are 0 paying subscribers. People can't want notes they have never seen. |
| D2 | Write with AI persisting across launches | Yes: persist it, with a ✦ chip on the bar while on, plus an optional shortcut. This reverses the 1.6.12 per-launch reset. | Smart mode was on at 49 launches across 2 installs (one of them the founder's) until the reset shipped on 12 June; no Smart Action has run since 16 June. There is a cost impact for Pro. |
| D3 | New-install hotkeys | **Windows:** keep Alt+R, now suppressed, with the Alt mask key (Office's Review tab stops opening and KeyTips don't appear). **macOS:** avoid Option+letter, Ctrl+Space, Ctrl+Option+Space, Option+Space, and **any Control+Option combination (VoiceOver's modifier)**; test Control+Shift+Space on real hardware before 1.10. **Live Assist:** Ctrl+Alt+A, except on AltGr layouts (left unset). Everything else unset. Existing users keep theirs. | Smallest change; avoids the AltGr clash (Polish ą), Mac typing clashes and VoiceOver |
| D4 | Enter to finish | Keep it on and suppress it on Windows so it no longer reaches Slack/Teams. On macOS keep it on but unadvertised until suppression is verified, then decide. | Power users rely on it; today it leaks |
| D5 | Analytics consent | Ask neutrally after the first success, with equal buttons; buffer events until the answer; remove the installer pre-tick; existing choices kept | Planet49; still yields data |
| D6 | Live Assist "see my screen" | "Only when I turn it on" for new installs; existing users keep theirs | Privacy. The 6 failed attempts were on 1.9.0, before the screen-answer fixes in 1.9.1, so re-measure on 1.9.1+. |
| D7 | Guest file transcription | No limit: transcription runs on the user's computer, so nothing is metered (decided 2026-10-04) | Local transcription costs nothing to serve |
| D8 | Recording consent | A reminder line on every recording (can be turned off), plus [Copy notice]. Legal review of the notice, "Where your data goes" and the analytics copy before 1.12. | All-party consent states; the Otter lawsuits |
| D9 | History retention | Keep forever by default; auto-delete optional; the 200-entry cap removed | Anything else deletes existing history |
| D10 | Guest feedback | Allow it with an optional email, rate-limited per install (backend change, 1 day in 1.13) | Help should be reachable before sign-in |
| D11 | Hold to talk | Build it in 1.14 as an option under Shortcuts | A common dictation pattern; not needed earlier |
| D12 | Quick popover (Ctrl+Alt+…, Recent + search, Enter pastes back) | Only if the 1.13 data shows window opens are rare and Recent / Paste last are heavily used | It needs a foreground hook and is hard to test |
| D13 | Google Calendar ("Coming up", "starts now, record?") | After 1.14; `calendar_sync.py` is ready | Scope |
| D14 | macOS signing | Buy Developer ID signing and notarization. Start the Apple enrollment during 1.10 (organization enrollment needs a D-U-N-S number and can take weeks), with the CI work in 1.12. | Ad-hoc signing resets permissions on every update; a real Mac updater needs it |
| D15 | Privacy copy | Confirm the Gusan Cloud and provider retention and training policy (Google Gemini, Mistral, Mistral OCR) before writing any "never used for training" copy. Diarization runs on this computer via sherpa-onnx (checked); confirm no voice embeddings are persisted before saying so. | Trust claims must be true |
| D16 | Budget and gate | Approve 1.10 (35-46 days) now. Approve 1.11-1.14 (about 112-141 more days) only after the 1.10 data review, with N new installs as the threshold. | 29-37 weeks of work for about 2 active non-admin installs is a bet. 1.10 captures most of the dictation value. |
| D16a | Cached Pro | Allow on-device Pro features for up to 7 days offline from a timestamped, per-user `entitlement_cache` (never past `period_end`). Gusan Cloud stays server-checked. | This changes entitlements.py's "never decides Pro on its own" rule; the source is public anyway, and paying users get blocked offline today |
| D17 | Gusan Cloud speech for Pro | An explicit choice only; never part of Automatic for dictation. Automatic AI features for Pro, and Live Assist, stay on Gusan Cloud (today's actual behaviour, now shown). | Privacy positioning, per-minute cost and the usage cap |
| D18 | Dictation during a file job | Run file jobs in 30 s chunks so a dictation waits at most one chunk (+2 days in 1.11). If rejected, keep today's refusal with clear copy. | P1: today a file job blocks dictation for its whole length |
| D19 | Privacy mode and usage statistics | Privacy mode also pauses usage statistics (nothing is buffered). The copy then reads "Your voice, text and screen stay on this computer", and says that account and update checks still connect. | Today analytics keep sending in Privacy mode (telemetry.py:120-127), so "nothing leaves this computer" is false |
| D20 | "Priority models and support" benefit | Keep it in the Pro sheet only if it is still a real commitment; otherwise drop it from all copy | It is promised today (pro_dialog.py:62) and the redesign would otherwise drop it silently |
| D21 | The app's name | **Gusan**, decided 30 Sep 2026. Ship it in 1.10, everywhere on one day (§9.3). | File the EU trademark (classes 9 and 42) and register gusan.ai and getgusan.com before announcing (§9.2) |