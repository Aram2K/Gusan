# Gusan for iPhone: design spec

This spec covers the containing app (`ios/Gusan`) and the keyboard extension (`ios/GusanKeyboard`).
The build is reviewed against it. Every screen and state below must exist and look like this.
Platform: iOS 16+, iPhone first, portrait and landscape. Follows Apple's Human Interface Guidelines.

## 1. Principles
1. **The keyboard is the product.** Gusan replaces the user's keyboard; it must type as well as the system keyboard, with Gusan one tap away.
2. **Context is always visible.** Before any action, the user sees exactly which text Gusan will use.
3. **Nothing is lost.** Every replacement can be undone. A failed or cancelled request leaves the text untouched.
4. **No dead ends.** Each error says what happened and offers the next step (a button where one exists).
5. **Calm, native, branded.** iOS materials, SF Symbols and system fonts, with Gusan red/blue used sparingly for identity and the primary action.

## 2. Tokens
All tokens live in one file per target (`Theme.swift` in the app, `KeyboardTheme.swift` in the keyboard).

| Token | Light | Dark | Use |
|---|---|---|---|
| `brandRed` | #D91E18 | #E5483F | logo, mic key gradient start |
| `brandBlue` | #0033A0 | #4A6FD8 | logo, mic key gradient end, hero gradient |
| `brandOrange` | #F2A815 | #F2A815 | logo only |
| `accent` | #2563EB | #3B82F6 | primary buttons, links, selected states, Run |
| `pro` | #9333EA → #7E22CE gradient | same | Pro badge / upgrade only |
| `success` | #16A34A | #22C55E | done states |
| `danger` | #DC2626 | #EF4444 | errors, destructive |
| `surface` | systemGroupedBackground | same | app backgrounds |
| `card` | secondarySystemGroupedBackground | same | cards |
| `keyboardBg` | #D1D3D9 | #2C2C2E | keyboard background |
| `key` | #FFFFFF | #6B6B6B | letter keys, action tiles |
| `keySpecial` | #ACB3BC | #4A4A4A | shift, delete, 123, return |

- **Type:** system font (SF Pro) with Dynamic Type text styles only (`.largeTitle`, `.title2`, `.headline`, `.body`, `.subheadline`, `.footnote`, `.caption`). No fixed point sizes in the app. Keyboard keys use 22pt regular letters and 16pt labels; they don't scale, like the system keyboard.
- **Spacing:** 4, 8, 12, 16, 20, 24, 32. Screen margin 20; card padding 16; gap between cards 12.
- **Radius:** keys 5, keyboard tiles 10, cards 16, buttons 14 (continuous corners).
- **Touch targets:** at least 44×44pt in the app. Keyboard keys follow system key sizes, and a touch in the gap between keys goes to the nearest key.
- **Motion:** spring animations (response 0.35, damping 0.85) for panel transitions; respect Reduce Motion (cross-fade instead).
- **Haptics:**
  - Success notification when a result is inserted.
  - Error notification on a failure.
  - Light impact on Gusan button, mic and tile taps.
  - Key clicks via `UIDevice.playInputClick()`.

## 3. Brand assets
- **App icon:** the Gusan mark (white microphone) on a full-bleed red/blue/orange striped background (the desktop logo, `logo.py`, extended to the square). No white corners.
- **Gusan button (keyboard):** the round logo, 30pt.
- **Gusan mic key:** a white `mic.fill` glyph on a circular `brandRed → brandBlue` diagonal gradient, 34pt. It is visually distinct from the system dictation mic below the keyboard.

## 4. App
Navigation: a first-run **Onboarding** flow, then a `TabView` with **Home**, **Keyboard**, and **Account**. A sign-in sheet appears whenever an action needs an account.

### 4.1 Onboarding (full screen, paged; skippable after sign-in)
1. **Welcome:** large logo, "Gusan, the AI keyboard", one-line value: "Rewrite, translate, summarize and more, in any app." Primary: "Get started".
2. **Sign in:**
   - Primary: **Continue with Google** (white button, Google "G", full width, 52pt tall).
   - Secondary: "Use email instead" expands email and password fields with a "Sign in" button.
   - Link: "Create an account".
   - Loading: the button shows a spinner and the others are disabled.
   - Errors show inline under the buttons in `danger`, in plain language.
   - Cancelling Google returns here silently.
3. **Add the keyboard:**
   - Three numbered steps, each with an icon: "Open Settings", "Keyboards › Add New Keyboard › Gusan", "Turn on Allow Full Access".
   - Button "Open Gusan Settings": `UIApplication.openSettingsURLString`, which lands in Gusan's own settings, where Keyboards lives.
   - A short privacy note explains Full Access: "Gusan reads text only when you tap an action, and sends only that text to Gusan Cloud."
   - Status chip: "Keyboard added ✓" or "Not added yet". It is detected via `UITextInputMode.activeInputModes` / the keyboard's shared heartbeat, if that's available.
4. **Make Gusan your main keyboard:**
   - Path: Settings › General › Keyboard › Keyboards › Edit, then drag Gusan to the top.
   - Mention that the globe key switches keyboards.
   - Note that the mic under the keyboard is Apple's dictation, and Gusan's own mic key is in the Gusan bar.
5. **Try it:**
   - A text field pre-filled with a sample sentence.
   - Coach marks: "Select text → tap the Gusan button → pick an action, or type an instruction."
   - Done → Home.

### 4.2 Home
- **Header card:** greeting with the user's name/email, plan badge (Pro / Free), keyboard status (added / Full Access / ready). If anything is missing, a single "Finish setup" call to action resumes onboarding at the missing step.
- **"Try Gusan"** playground: a multiline editor with a sample. Users test the keyboard here.
- **"What you can do"** cards, each with an example before → after:
  - Ask Gusan (free-form).
  - Rephrase.
  - Fix grammar.
  - Email.
  - To-do.
  - Translate.
  - Summarize.
- **"How to use"** row → How to use screen (4.5).

### 4.3 Keyboard tab (settings)
- **Default action:** a picker of Smart / Rephrase / Fix grammar / Email / To-do / Translate / Summarize. It is used by a long-press on the Gusan button.
- **Translate to:** language picker (en, hy, ru, fr, de, es, it, pt, ar), shown with native names.
- **Typing:** toggles for key clicks, auto-capitalization, and "double-space for period". These are stored in shared settings.
- Setup status rows: "Keyboard added" and "Full Access", each with a fix button.
- A "Diagnostics" row, small and at the bottom, goes to 4.6.

### 4.4 Account
- Avatar initial, email, plan (Pro badge or "Free"), and "Manage plan" (opens gusan.aibuben.xyz).
- "Sign out" (destructive, with confirmation).
- Signed out: the sign-in card (same as onboarding step 2).

### 4.5 How to use
Short illustrated sections:
1. Select text, or just type.
2. Tap the Gusan button.
3. Pick an action, or tap **Ask Gusan…** and type what you want ("make it more formal", "translate to Armenian").
4. The result replaces your text; tap **Undo** to restore it.
5. Tap the keyboard mic to activate a voice session. Gusan briefly opens when activation is needed; follow its return guidance. Later taps record in place while the session is available. Tap Stop to process; Cancel discards the request.
6. Troubleshooting: "Sign in to use Gusan" in the keyboard, Full Access, switching keyboards.

### 4.6 Diagnostics (hidden behind the row)
For each store, show what the app and the keyboard can each read and write, with timestamps:
- App group (name).
- Shared keychain group.
- Shared file.
- Team pasteboard.

Also shows:
- The last keyboard heartbeat.
- Which store holds the current session.
- "Copy report".

Developer wording is allowed only on this screen.

### 4.7 App states
Every async view has loading (a skeleton or spinner), empty, and error (message + retry) states. Offline shows "You're offline" with retry.
URL routes:
- `gusan://auth-callback` (OAuth).
- `gusan://signin?from=keyboard`: opens the sign-in sheet; after success it shows "You're signed in. Go back to your app" with a hint to use the ◀ back link at the top-left.
- `gusan://onboarding`.

## 5. Keyboard
Height: system keyboard height plus a 44pt Gusan toolbar. The height doesn't change between modes.

### 5.1 Toolbar (always visible)
`[Gusan button] [context chip / status] [mic key]`
- **Gusan button:** tap toggles the Actions panel; long-press runs the default action.
- **Context chip:**
  - "Selected · 12 words".
  - "Around cursor · 40 words".
  - "Nothing selected".
  - It updates on `selectionDidChange` / `textDidChange`.
- **Status replaces the chip while busy or after a result:**
  - Spinner + "Rephrasing…" + ✕ cancel.
  - "Replaced · Undo" for 8s.
  - An error (2 lines max, tap to expand) with an action button when one applies (Sign in / How to enable).
- **Mic key:** calls `startVoice()`, the single voice entry point. Opens Gusan for microphone access the first time; shared results return to the keyboard. Shows stop while recording and cancel while transcribing/thinking.

### 5.2 Typing (default)
- QWERTY with shift, caps lock, 123 and #+= layers.
- Globe only when `needsInputModeSwitchKey`.
- Space, double-space period, return with a dynamic label, repeating delete.
- Key previews, nearest-key hit testing, light and dark.

### 5.3 Actions panel (replaces the keys while open)
- **"Ask Gusan…"** field-style button across the top.
- A tile grid of 2 rows, each with an SF Symbol and a label:
  - Smart (`sparkles`).
  - Rephrase (`arrow.triangle.2.circlepath`).
  - Fix grammar (`checkmark.seal`).
  - Email (`envelope`).
  - To-do (`checklist`).
  - Translate → XX (`character.bubble`).
  - Summarize (`text.alignleft`).
- The default action has a small dot.
- A context preview card: "Using: “first words…”" (2 lines) or "No text: Ask Gusan will write it".
- A "Keyboard" button returns to typing.

### 5.4 Instruction mode
- The context is snapshotted on entry.
- An instruction field with a caret and the placeholder "Tell Gusan what to do…".
- Suggestion chips: "More formal", "Shorter", "Friendlier", "Fix grammar", "Translate to Armenian".
- The full keyboard types into the field.
- The return key becomes **Run** (accent).
- ✕ cancels.
- The instruction is a plain string passed to one method; Milestone 2 voice fills it with a transcript.

### 5.5 Results
- The result replaces the selection, or the whole around-cursor text. With no context, it is inserted at the cursor.
- If the text changed meanwhile, nothing is replaced: "Text changed. Try again."
- Undo restores the original exactly while the document is unchanged.

### 5.6 Gates (typing always works)
- **No Full Access:** "Gusan needs Full Access to reach Gusan Cloud", with a "How to enable" disclosure listing the steps.
- **No session:** "Sign in to use Gusan" + **Sign in**, which opens `gusan://signin?from=keyboard` in the app.
  - The session is shared through the first working store: shared keychain group, then app-group file, then the team-scoped named pasteboard (works after Sideloadly re-signing because both targets share one team).
- **Free plan:** "Gusan Pro is needed for AI actions" + "See plans".
- **Errors:**
  - Offline / timeout: retry.
  - Daily limit: message.
  - Server: retry.

## 6. Accessibility
- **VoiceOver:** every control has a label and a hint. Keys announce their characters, and shift state is announced. The context chip reads the full count. Results announce "Replaced. Undo available."
- **Dynamic Type:** supported up to AX5 in the app; layouts wrap and nothing truncates important text.
- **Contrast:** at least 4.5:1 for text in light and dark.
- **Reduce Motion and Reduce Transparency** are respected.
- No information is conveyed by color alone.

## 7. Smart voice
`KeyboardViewController.startVoice()` remains the mic entry point. The keyboard snapshots the document identifier, selection, and before/after context at the mic tap. Recording is hosted in the containing app; the existing one-time launch authorization and App Group / temporary pasteboard channel rules still apply.

### 7.1 Voice surface
- Listening, transcribing and thinking replace the entire keyboard surface without changing its height. Crossfade in 0.2 seconds; switch instantly with Reduce Motion.
- Show a large live blue waveform, or an animated idle waveform when no levels are available. Reduce Motion makes the idle waveform static.
- Show a status line, a Stop button at least 64 points high, and a smaller Cancel control with a 44-point touch target. Stop finishes capture while listening; during processing it cancels the job.
- Use Gusan blue `#0033A0` and accent `#2563EB`, never red. Use `UIGlassEffect` on iOS 26+, otherwise ultra-thin material blur. Respect Reduce Transparency, light and dark. The containing-app voice sheet uses the same blue-only palette and mark.
- Done exposes an Undo chip and a Keyboard control. A changed document opens the result card with explicit Copy; never copy automatically.

### 7.2 Smart by default
Smart voice is the default for users without an explicitly saved voice preset. Existing explicit presets remain available in Keyboard settings; Ask Gusan remains a separate secondary flow.
- Selection: speech is the instruction about that selection. Replace only the unchanged selection.
- Empty field: polish the dictation and insert it at the cursor.
- Around-cursor text: send one `smart_auto` request using `.voiceSmart(transcript:)`. The system asks only for JSON `{"kind":"edit"|"insert","text":"..."}`. Edit returns the complete context replacement; insert returns polished dictation. Existing text is data, never instructions; preserve its language unless asked.
- Parse the first complete JSON object, including fenced answers and escaped braces in strings. An invalid schema or malformed answer falls back to insertion of the cleaned transcript, without deleting text.
- Apply only when document ID, selection and both context sides still match the snapshot. Otherwise present the result card. Undo restores the original text for both insert and edit; around-cursor Undo also restores the cursor offset.

### 7.3 Jobs and development previews
`GusanJob.run()` produces an `AsyncThrowingStream<JobEvent, Error>` with progress, optional confirmation requests, and one typed `JobResult`. Results distinguish replace-selection, replace-context, insert and card-only. `JobCard` carries a title, body and optional insertable text; a card can be informative without offering insertion. Smart actions, presets, Ask Gusan and voice use jobs. Network requests, request encoding and classifier parsing execute off the main thread; UIKit presentation stays on it. Cancelling a stream cancels its worker.

Extension point: a new file can implement the public `GusanJob` protocol and pass its instance directly to `JobRunner.events(for:)` or `JobRunner.result(of:progress:confirmation:)`. There is no job-type enum or central factory switch to modify. A producer yields `.needsConfirmation(JobConfirmation(prompt: ...))` and awaits the request's `response()` before continuing. A consumer can resolve the request directly, or provide the runner's async confirmation callback. Decisions are one-shot; cancellation unblocks the waiter. With no confirmation handler, the runner declines and cancels, never silently approves. Current keyboard actions do not request confirmation; no agent confirmation UI is included.

DEBUG builds support `--gusan-debug-voice` on the containing app. This explicitly enables a local keyboard preview: mic starts a fake level feed, Stop advances to thinking, and Stop again delivers a fixed fake transcript/result through the normal snapshot/range/Undo guards. No microphone, credential or network access is involved and no fake IPC packets are persisted. Launch the app without the argument to disable it. Release builds omit the preview code.

### 7.4 Idle audio and sideload-only return
The idle window defaults to two minutes. Recording installs an input tap and starts the engine; Stop or Cancel pauses the engine and removes the tap. The audio session remains active until Stop session or timeout. This reserves the mic and uses some battery, but does not intentionally capture idle audio. Verify the orange indicator and background survival on a physical device: an active audio session alone does not guarantee that iOS will keep the app running. A suspended or terminated app requires guided activation again.

Only `ios/scripts/build_unsigned_ipa.sh` enables `GUSAN_SIDELOAD`. Under that compilation condition, the experimental automatic return uses the private `LSApplicationWorkspace.openApplicationWithBundleID:` selector. Ordinary builds omit the private implementation and settings toggle; do not enable this flag for App Store or TestFlight. If automatic return is unavailable, the activation sheet explains how to return manually.

## 8. Agent actions (future)
A future long-running job may call a server-side computer-use agent from H Company. Keep its credential only in Supabase as `H_company_computer_agents`, never on the phone or in a client request. The server owns agent execution and authorization. Stream progress events through the same job interface and return a card-only result that the user reviews and explicitly inserts. Do not add an on-device agent or automatic agent-result insertion.

## 9. Gusan Screen (live screen context)
The user lets Gusan see the iPhone screen through a ReplayKit **broadcast upload extension** (`ios/GusanScreen`, bundle `com.aram2k.gusan.screen`), started from the app's **Gusan Screen** page (`RPSystemBroadcastPickerView`) or from Control Center's screen recording control. The extension never uploads anything: it holds a second or two of downscaled frames in memory and gives one to the keyboard only when the keyboard asks.

### 9.1 App page (Home › Gusan Screen, Keyboard › Screen)
- Hero with the `eye` glyph in Gusan blue, one-line value ("say 'reply to this' or 'what does this say?' from the keyboard").
- **What is shared** card: red indicator, nothing leaves the phone on its own, one picture is sent only on request together with the instruction and field text (the picture is from just before the keyboard came up), **the picture shows everything on the screen at that moment — messages, names, codes, balances; Gusan never offers it while typing in a password field** (ReplayKit does not blank secure fields; the chip gate is the only guard), automatic stop after the idle period.
- First visit: primary button **Got it, continue** (`ScreenSettings.explained`). The acknowledgement is valid for 30 days and for the app version it was given in; after that, or after an update, the page says "Please read this again" and asks again. The keyboard never offers the screen before this, including for Control Center starts.
- Then a status card (**Live** with start time, picture size, "stops at 3:14 PM if you don't ask anything", or **Not running**, with "Stopped after 10 min idle at 3:14 PM" when that is how the last broadcast ended, the **Start Gusan Screen** picker button and the Control Center hint) and a **Stop after** picker (2, 5, 10, 20, 30 minutes; default 10).
- The picker is the system view stretched under a `PrimaryButton` label; it opens Apple's broadcast sheet, where the user taps Start Broadcast. Debug builds assert that the system view still exposes a `UIButton` to stretch.
- Home: the Gusan Screen card moves to the top of Home while a broadcast is live.

### 9.2 Keyboard
- A capsule **Screen** chip (`eye`, caption font scaled with Dynamic Type, as tall as the mic) sits in the toolbar left of the mic once the user has read the explanation, Full Access is on and the field is not secure (`isSecureTextEntry`). It is hidden otherwise.
  - Broadcast live: full opacity. Tap arms it (`eye.fill`, blue fill) and opens Instruction mode with the placeholder "Ask about your screen…" and the chips "Reply to this", "What does this say?", "Summarize", "Translate this". The context line reads "Around cursor · 12 words · Screen" and starts with a red dot whenever the broadcast is live. The first time the chip is armed the status line says "Your next Run sends a picture of this screen". VoiceOver: label "Screen", value "On/Off", a layout-changed post to the instruction header and the announcement "Screen on. Ask about your screen; Run sends a picture of it."
  - Not live: ghosted (`eye.slash`, 55 % opacity). Tap shows "Start Gusan Screen in the Gusan app to ask about your screen" with **Open** (`gusan://screen` → Home › Gusan Screen).
  - When the broadcast ends while the keyboard is up: "Gusan Screen stopped" in the status line (announced); if the chip was armed the open question is cancelled instead of falling through to a text action.
- Run sends the instruction, the field text (selection, or the around-cursor text marked as context only, so "this" is unambiguous) and the frame to `smart-action` (mode `smart_auto`, the image as an OpenAI `image_url` data-URL content part on the last user turn, the shape the desktop uses in `action_api.openai_messages_with_image`). Status: "Looking at your screen…" then "Thinking…", with Cancel.
- The answer replaces the selection or is inserted at the cursor; it never rewrites the around-cursor text. Undo works like text actions. Errors: "No picture from Gusan Screen" (Retry), "Gusan Screen isn't available" (tap for details, which include the channel diagnostics).
- Voice: `ScreenInstructionJob` is a `GusanJob`; a voice transcript can be routed through it the same way a typed instruction is. The voice redesign owns that wiring.

### 9.3 IPC (`GusanKit/Screen/ScreenChannel.swift`)
Sideloadly with a free Apple ID often breaks the App Group and the shared keychain, so the channel does not pick one store: every packet is written to **every** store that works and read back newest-first. Stores: the App Group file directory `screen/` (when the container resolves) and the team-scoped named pasteboard `com.aram2k.gusan.screen` (when the user's pasteboard-fallback setting allows it; the extension reads the same setting). Packets carry `issuedAt` and expire (status 60 s, request 30 s, frame 10 s, keyboard 180 s); expired or future-dated packets are deleted on read.
- `status`: heartbeat every 3 s from the extension (start time, idle deadline, frame size, request count). Live = heartbeat younger than 10 s. Cleared when the broadcast ends. The keyboard reads it on each Darwin post and once more from a one-shot watchdog 11 s after the last heartbeat; there is no periodic polling while the user types.
- `keyboard` → Darwin `gusan.screen.keyboard.com.aram2k.gusan`: when the Gusan keyboard appears it publishes `ScreenKeyboardState` (the instant it appeared, its height and the screen height); it rewrites it after 90 s and removes it when it disappears. The extension freezes the newest frame from before that instant and answers requests with it while the packet exists, so the keyboard and the typed question are never in the picture. If no such frame is left (broadcast started after the keyboard, or just resumed) it crops the keyboard's band off the bottom of the latest frame instead (at most 70 %).
- `request` → Darwin `gusan.screen.request.com.aram2k.gusan`: the keyboard asks for the frame. The extension also checks on each heartbeat in case the notification is lost. A request that finds no frame (right after a pause) waits up to 4 s for the next one.
- `frame` → Darwin `gusan.screen.frame.com.aram2k.gusan`: one JPEG (long edge 1280, quality stepped down to fit 450 KB) tagged with the request id and flagged `frozen`/`cropped`. The keyboard removes it from every store as soon as it takes it, rejects it as "no picture" when it is older than 5 s (frozen frames are judged against the keyboard's appearance), and clears it on timeout; unconsumed frames expire after 10 s.
- The keyboard, not the extension, calls Gusan Cloud: it already owns the session token, refresh, errors and Undo, so the extension never holds a token, never talks to the network and stays small.
- `ScreenSettings` (idle minutes, acknowledgement date and version, arm reminder, last idle stop) mirrors `SettingsStore`: App Group defaults plus a pasteboard copy, newest wins, so the extension reads the idle period even when the group is broken.

### 9.4 Extension (`GusanScreen/SampleHandler.swift`)
- Accepts one video frame every 0.4 s for 10 s after the start, a keyboard packet or a request, then one a second; orients it, downscales it with `CILanczosScaleTransform` to a 1280 px long edge (about 3 MB as a bitmap), and keeps the last four plus the frozen pre-keyboard copy (≈ 15 MB worst case). No disk, no log, no network. Debug builds log `os_proc_available_memory()` at start and on each request.
- Encodes a JPEG only to answer a request. `broadcastPaused` drops the kept frames; `broadcastFinished` and the idle stop share one idempotent, lock-guarded `end()` (no `queue.sync`, so a re-entrant `broadcastFinished` cannot deadlock).
- `ScreenIdleTimer`: without a request for the configured period the extension records the stop in `ScreenSettings` and calls `finishBroadcastWithError` with "Gusan Screen stopped: 10 minutes without a question." (the only API an upload extension has to end a broadcast; iOS shows it as an alert).
- The system shows the red recording indicator for the whole broadcast; tapping it, or Control Center, stops it.

### 9.5 Privacy and limits
- Sent to Gusan Cloud, only on an explicit request: one downscaled JPEG of the screen (from just before the keyboard appeared, or with the keyboard cropped out), the instruction, the selection or the field text iOS exposes to the keyboard, and the session token. Never sent: audio, continuous frames. The chip is hidden in secure fields, but ReplayKit does not blank anything: whatever is visible on the screen is in the picture, and the explanation says so. The iOS app has no Privacy Mode; the (expiring) acknowledgement and the chip gate are the equivalent opt-in.
- The pasteboard copy of a frame is readable by apps signed by the same team for up to 10 s; the pasteboard-fallback toggle in Keyboard settings turns that store off for the keyboard and the extension alike.
- Free Apple ID: the app, keyboard and broadcast extension are 3 App IDs (10 per 7 days allowed). The broadcast extension needs no extra entitlement beyond the App Group; Sideloadly may rename the bundle, so the picker's `preferredExtension` is derived from `Bundle.main.bundleIdentifier + ".screen"`. If the re-signed extension id does not follow that pattern the sheet simply lists every broadcast extension on the phone, with Gusan Screen among them; Control Center is unaffected.
