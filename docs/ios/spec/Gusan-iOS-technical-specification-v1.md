# Gusan iOS — technical specification v1

Status: engineering draft for review (2026-11-08). Written against the code on `devin/1794056309-ios-keyboard` (PR #13, which already contains PR #17), `devin/1794085675-ios-agent-tasks` (PR #19), `devin/1794090252-ios-screen` (PR #20) and `devin/1794085578-agent-action` (PR #18). Nothing in this document has been implemented; nothing was committed.

How to read the evidence tags used throughout:

| Tag | Meaning |
|---|---|
| **[Apple doc]** | Stated in the cited Apple documentation page (fetched 2026-11-08). |
| **[H doc]** | Stated in the cited H Company page under https://hub.hcompany.ai/agents-api/. |
| **[repo]** | Verified by reading the code at the file/function named. |
| **[device test needed]** | Cannot be verified from the repo or the docs; a numbered spike in §12 covers it. |
| **[unverified]** | Widely reported but not in Apple's reference docs; treat as a hypothesis. |

The product brief (BRIEF.md) is the source of truth for *what*; this document is about *how*, *what iOS allows*, and *what it costs*.

---

## 0. Executive summary (read this if nothing else)

1. **The strip "on top of any keyboard" is impossible on iOS; the strip inside Gusan's own keyboard is the only legal form.** Nothing can draw over another app's keyboard; custom keyboards get a single view (§1).
2. **The keyboard itself can never record audio.** Apple: *"Custom keyboards, like all app extensions in iOS 8.0, have no access to the device microphone, so dictation input is not possible."* **[Apple doc]** There is no entitlement, WebRTC trick or sideload workaround that changes this (§2.3). Every design must have the *containing app* record.
3. **The best "record anywhere, no visible hop" mechanism that exists today is iOS 18's `AudioRecordingIntent`**: a Shortcut/Control Center/Action‑button/Back‑Tap/Siri trigger launches the Gusan app *in the background*, it records, and it must show a Live Activity while recording — which is exactly the Dynamic‑Island glow the owner asked for. **[Apple doc]** The remaining gap: a tap *inside the keyboard* cannot fire an App Intent without opening an app. Recommendation and exact user‑visible behaviour in §2.6.
4. **Live Activities can be started only from the foreground app or from an App Intent; push‑to‑start needs APNs, which a free Apple ID cannot have.** The plan keeps one "Gusan session" Live Activity alive (started by the intent or by the app) and *updates* it from the background for listening → working → agent progress → result (§3).
5. **Screen context** stays on the already‑built ReplayKit broadcast extension (`GusanScreen/SampleHandler.swift`). "Always on" is not possible (a broadcast can only be started by a user tap in the system picker); recommend *"starts when first needed, stops after idle"* (§4).
6. **Vault: end‑to‑end (no plaintext at Gusan Cloud) is NOT achievable with H's API** — item values travel in clear text in the request body to H and only our server may hold the H key. The design minimises exposure: secrets live in the iPhone keychain behind Face ID, are released per use into an *ephemeral per‑session H vault*, and Gusan Cloud never persists or logs them. Note that H exposes a **Reveal an item** endpoint to any holder of the org API key — the H key is therefore the crown jewel (§7).
7. **Free Apple ID (Sideloadly) vs App Store** is the single biggest fork: no APNs (no push‑to‑start, no remote Live Activity updates, no remote notifications), 7‑day expiry, 10 App IDs/7 days and 3 side‑loaded apps, and the App Group may not survive re‑signing (already observed on the owner's phone, worked around with the team pasteboard channel **[repo]**). Each section has a "Sideload vs App Store" row.

---

## 1. The keyboard: a faithful copy of Apple's keyboard with the Gusan strip

### 1.1 What iOS allows
- A custom keyboard is an app extension with `NSExtensionPointIdentifier = com.apple.keyboard-service` whose principal class subclasses `UIInputViewController`; it may draw **only inside its primary view** and must provide a "next keyboard" (globe) affordance. **[Apple doc: App Extension Programming Guide › Custom Keyboard]**
- Apple lists the system‑keyboard features *users expect but no API provides*: layout by `UIKeyboardType`, **autocorrection and suggestion**, auto‑capitalization, period on double space, caps lock, keycap artwork, multistage input. *"There is no dedicated API for any of the features just listed."* **[Apple doc]** So "faithful copy" means we re‑implement them; the system lexicon is available read‑only via `UILexicon` (`requestSupplementaryLexicon`) for names/shortcuts, but there is no access to the user's autocorrect dictionary.
- Unavailable to custom keyboards **[Apple doc]**: secure text fields (`isSecureTextEntry`) and phone pads (system keyboard is swapped in), apps that reject extensions (`application:shouldAllowExtensionPointIdentifier:`), text selection and the edit menu, inline autocorrect bubbles, key‑pop artwork above the top edge, **microphone**.
- Full Access (`RequestsOpenAccess = YES`, already set in `GusanKeyboard/Info.plist` **[repo]**): without it there is no network, no shared `UserDefaults`/container writes that matter to us, and no `UIPasteboard.general`. `UIInputViewController.hasFullAccess` (iOS 11+) **[Apple doc]** is what `KeyboardViewController` checks before every store/pasteboard access **[repo: `SessionStore(role: .keyboard, pasteboardAccessAllowed:)`]**.
- App Groups: `group.com.aram2k.gusan` on both targets **[repo: `project.yml`, both `.entitlements`]**. On the owner's phone the group did **not** survive Sideloadly re‑signing; `AppGroupResolver` reads the embedded provisioning profile to discover the rewritten group and `SessionStore` falls back to a team‑scoped named `UIPasteboard` **[repo: `SessionStore.swift`, `SessionBackend.swift: NamedSessionPasteboard`]**.
- Memory / launch: Apple publishes no number for keyboard extensions. Field reports put the jetsam limit around 60–70 MB on recent devices **[unverified]**; Xcode's "Increased Debugging Memory Limit" capability is for debugging only. The practical rules: no image decoding of screen frames inside the keyboard beyond one JPEG (≤450 KB **[repo: `ScreenFrame`]**), no large Swift Concurrency fan‑out, lazy‑load panels. Launch: every text field focus can re‑create the extension process; `viewDidLoad` must stay under ~100 ms — today `KeyboardViewController.viewDidLoad` builds toolbar, panel, instruction header, key area and voice panel eagerly **[repo]**; the redesign removes the panel and the instruction header entirely (§9), which also helps launch.
- Globe: show the key only when `needsInputModeSwitchKey` is true and forward touches to `handleInputModeList(from:with:)` — already done **[repo: `keyArea.globeButton.addTarget(... handleInputModeList ...)`]**. Dictation key: **none** (no mic) — the brief is right.
- Dark mode: `textDocumentProxy.keyboardAppearance` plus trait collection drive `KeyboardTheme` **[repo: `updateContext` passes `appearance:`]**.
- Haptics and sounds: `UIDevice.current.playInputClick()` works only when the view conforms to `UIInputViewAudioFeedback` and the user has keyboard clicks on — done **[repo: `TypingKeyboardView: UIInputViewAudioFeedback`]**. Haptic feedback (`UIImpactFeedbackGenerator`) from a keyboard extension **requires Full Access** and is known to work only on iPhone **[unverified; Apple's HIG says keyboards should use system key‑click feedback]**; Apple's own keyboard haptics (iOS 16 "Haptic feedback" toggle) is not exposed. Plan: light impact on strip controls, `playInputClick` on keys, all gated by `hasFullAccess` and the "Sounds" setting.

### 1.2 Chosen mechanism
Keep the UIKit keyboard (`TypingKeyboardView`) and replace the 44 pt toolbar (`GusanToolbar`) with the **Gusan strip** described in the brief. One `UIInputViewController` (`KeyboardViewController`), one height (`keyboardHeight` = system key area + strip) **[repo]**, no height changes between states (the voice panel today crossfades over the keys at the same height — reuse that).

Strip states (all in `GusanStrip`, a new UIView replacing `GusanToolbar`):

| State | Left | Middle | Right |
|---|---|---|---|
| Idle | Mode chip `Smart ▾` / `Agent ▾` (tap toggles) | context chip ("Selected · 12 words" / "Near cursor") | iPhone‑outline icon (Screen, selected by default) · blue mic circle |
| Listening | — | waveform spanning the full strip width (`VoiceWaveform`, widen from 65 pt to the strip) | ✕ (cancel) · mic (finish & send) |
| Working | small `UIActivityIndicatorView` + the heard words (marquee if long) | | ✕ (cancel) |
| Done | "Inserted · Undo" for 30 s (timer, see §5.5) | | mic ready |
| Agent | 3‑second card (what it understood) then collapses to the idle strip + a thin progress dot | | |

Autocorrect/prediction: out of scope for v1 — the brief does not ask for it and Apple gives no API; document the gap in Settings ("Gusan has no autocorrect yet; use Apple's keyboard for long typing") rather than ship a half‑working one.

### 1.3 Data flow
Typing never leaves the device. Context for Smart is read at the moment of the mic tap from `textDocumentProxy.selectedText`, `documentContextBeforeInput`, `documentContextAfterInput`, `documentIdentifier` **[repo: `currentContext`]** and snapshotted into `VoiceContext` **[repo: `VoiceKeyController.currentContext()`]**.

### 1.4 Failure modes
- Full Access off → the strip shows "Turn on Full Access" with an Open Settings deep link (`gusan://open-settings` → `UIApplication.openSettingsURLString`) **[repo: `VoiceKeyController.handleTap`]**.
- Shared sign‑in unreadable → "Open Gusan to reconnect" (diagnostics already exist) **[repo: `SessionDiagnostics`]**.
- Secure field / phone pad → iOS hides Gusan entirely; nothing to do.
- Extension killed mid‑request → the result is also written to the App Group/pasteboard channel by the app (today's `VoiceChannel.status`), so the next keyboard appearance can still offer it.

### 1.5 Sideload vs App Store
Identical, except the App Group fallback (pasteboard) matters only on sideload, and TestFlight/App Store would let us drop the `pasteboardFallback` toggle.

### 1.6 Effort
Strip redesign + removal of panel/instruction surfaces: **3–4 engineer‑days** (the hard parts — Undo, context snapshotting, voice state machine — exist).

---

## 2. Microphone without a visible app hop

### 2.1 The constraint, verified
"Custom keyboards, like all app extensions in iOS 8.0, have no access to the device microphone, so dictation input is not possible." **[Apple doc: App Extension Programming Guide › Custom Keyboard › "System Keyboard Features Unavailable to Custom Keyboards"]** This has not changed through iOS 18; `AVAudioSession.recordPermission` in a keyboard extension is `.denied` regardless of Full Access. Therefore **recording always happens in the Gusan app process**, and the question is only *how the app process gets to run, and whether the user sees it*.

Today's mechanism **[repo]**: `KeyboardViewController.startVoice()` → `VoiceKeyController.tap()` → `KeyboardVoice.tap(status:access:)` decides `.openApp` when the app's `VoiceStatus` heartbeat is older than 3 s (`VoiceStatus.isAlive`) → `openContainingApp(gusan://voice?nonce=…&return=<host bundle id>)` via the `UIApplication.openURL:options:completionHandler:` responder‑chain call. The app (`VoiceSession.handleURL`) validates the nonce (`VoiceChannel.consumeLaunch`), requests mic permission, `activateAudio()` (`.playAndRecord`, `.mixWithOthers`), starts `AVAudioEngine`, and publishes status every second; the keyboard and app exchange `VoiceCommand`/`VoiceStatus`/`VoiceTranscript` packets through the App Group or the team pasteboard with Darwin notifications (`VoiceChannel`). The app stays alive thanks to `UIBackgroundModes = [audio]` **[repo: `Gusan/Info.plist`]** and auto‑returns to the host app only on `GUSAN_SIDELOAD` builds via the private `LSApplicationWorkspace.openApplicationWithBundleID:` **[repo: `VoiceSession.returnToHost`]**. Idle timeout defaults to 2 min (`VoiceSettings.idleMinutes`, options 1…60), max recording 300 s (`VoiceMachine.maxRecording`).

### 2.2 Every option, evaluated

| # | Option | What iOS allows | Visible hop? | Sideload (free ID) | App Store | Verdict |
|---|---|---|---|---|---|---|
| A | **Keep the app alive with a background audio session** (today) | An app with the `audio` background mode keeps running while its audio session is active *and* audio I/O is running. **[Apple doc: UIBackgroundModes → "Configuring background execution modes"]** Pausing the engine but leaving the session active (what `pauseCapture()` does) is a grey zone: Apple does not promise runtime for a silent active session. **[device test needed: S1]** The orange microphone indicator shows whenever the input route is in use (iOS 14+), i.e. for the whole alive window. Battery: a running `AVAudioEngine` input tap at 16 kHz ≈ a voice memo recording in the background; expect low single‑digit %/hour **[unverified; S2 measures it]**. Killed by: user swipe‑kill, memory pressure, phone call interruption without `.shouldResume`, another app taking the mic exclusively (`.mixWithOthers` reduces this), and reboot. | Only the **first** time per alive window | Works (no entitlement) | Works | Keep as the fallback layer; lengthen the window (§2.6). |
| B | **Relaunch invisibly** via URL scheme + auto‑return | Opening `gusan://` from the keyboard necessarily brings the app to the foreground (one app‑switch animation each way). Returning to the host app has **no public API**; `LSApplicationWorkspace` is private. On a sideloaded build it is only an ordinary Objective‑C call, so it runs if the selector still exists on the installed iOS (**[device test needed: S3]** on iOS 18.x). On the App Store it is an automatic rejection (private API scan) and `openURL` of another app's scheme would also need that app's scheme. | Yes (≈1 s there, ≈1 s back) | Possible, fragile | Not allowed | Keep behind `GUSAN_SIDELOAD` as today; never the primary design. |
| C | **App Intents / Shortcuts: `AudioRecordingIntent`** (iOS 18) | "Adopt this protocol to create an app intent for audio recording functionality… In iOS, iPadOS, and watchOS, when you adopt the `AudioRecordingIntent` protocol, you **must start a Live Activity when you begin the audio recording and keep it active as long as you record**. If you don't start a Live Activity, the audio recording stops." **[Apple doc: appintents/audiorecordingintent]** The intent runs **in the app's process in the background** (`openAppWhenRun = false`), so a trigger from Shortcuts, the **Action button**, **Back Tap** (Settings › Accessibility › Touch › Back Tap → any Shortcut), a **Control Center control**, a **Lock Screen control** or **Siri** ("Gusan, listen" App Shortcut phrase) starts recording with *no app on screen*. The Live Activity it must start *is* the Dynamic‑Island glow. | **No** | Works: App Intents and Live Activities need no capability/entitlement (Live Activities need only `NSSupportsLiveActivities` in Info.plist **[Apple doc]**). The widget extension is a 4th App ID. **[device test needed: S4]** | Works | **Primary "anywhere" mechanism.** |
| C′ | Keyboard → Shortcut | A keyboard extension cannot run an App Intent of another process and cannot call `INInteraction`/`AppIntent.perform()` across processes. The only bridge is the `shortcuts://run-shortcut?name=…` URL, which **opens the Shortcuts app** (visible, and not on the App Store without user consent flows). | Yes | — | — | Rejected. |
| D | **Live Activity push‑to‑start** (iOS 17.2) | `Activity.pushToStartToken` **[Apple doc: iOS 17.2+]** lets a server start a Live Activity via APNs. Requires the **Push Notifications capability** — unavailable to personal (free) teams. Also, a push‑started Live Activity does not grant microphone runtime; it would only draw the island. | No | **Impossible** (no APNs) | Possible, but useless for the mic | Not a mic solution; see §3 for its role in Agent progress on App Store builds. |
| D′ | **Interactive Live Activity button** (iOS 17) | A `Button(intent:)` in a Live Activity runs a `LiveActivityIntent` in the app's process in the background **[Apple doc: appintents/liveactivitystartingintent, iOS 16.1+; interactivity iOS 17]**. If that intent also conforms to `AudioRecordingIntent`, a single tap on the (expanded) Dynamic Island could start recording. Needs a Live Activity to exist first (8 h max **[Apple doc]**). | No | Works (no APNs needed for *local* activities) | Works | **Secondary mechanism:** a "Gusan ready" Live Activity with a mic button; **[device test needed: S5]**. |
| E | **Control Center control** (iOS 18) | `ControlWidget` buttons perform an App Intent "from Control Center, the Lock Screen, or by using the Action button" **[Apple doc: widgetkit/creating-controls-to-perform-actions-across-the-system]**. Swipe down from the top‑right while the keyboard is up → tap Gusan → recording starts (via C). | No (Control Center overlay only) | Works (same widget extension) | Works | Part of the primary mechanism. |
| F | **Siri** | App Shortcuts (`AppShortcutsProvider`) give a phrase like "Start Gusan". With C, recording starts in the background; Siri's own UI appears briefly and the user has to wait for Siri to dismiss before speaking to Gusan — awkward but hands‑free. | Siri sheet | Works | Works | Offer, don't promote. |
| G | **AirPods / Apple Watch** | AirPods stem = media remote commands (`MPRemoteCommandCenter`), delivered only to the Now Playing app; Gusan would have to be playing (silent) audio continuously — a hack Apple rejects on review and that drains battery. A watchOS companion can `WCSession.sendMessage` and iOS launches the phone app in the background to receive it, but an app launched in the background may **not** start recording (the audio session must have been activated in the foreground) unless via `AudioRecordingIntent`; a Watch App Intent could trigger C indirectly through Shortcuts automation. | No | Works‑ish | Works | Post‑v1. |
| H | **Apple's own dictation** | Not available inside a custom keyboard. The user can globe‑switch to Apple's keyboard, dictate, switch back; the Gusan strip could then offer "Run the text I just dictated as an instruction". Degraded, but it is the only path that works even when the Gusan app is dead and the user refuses background audio. | No (two globe taps) | Works | Works | Keep as documented fallback. |
| I | **WebRTC / `getUserMedia` / `AVAudioRecorder` in the extension** | Blocked by the same rule as I above; a `WKWebView` inside the keyboard gets `NotAllowedError` for `getUserMedia`. | — | No | No | Rejected (verified by Apple's statement; no need to spike). |
| J | **Entitlements** | There is no public entitlement granting microphone to keyboard extensions. Private entitlements require a provisioning profile that lists them; Apple's free/paid provisioning never does, and Sideloadly can only sign what the profile allows. | — | No | No | Rejected. |

### 2.3 Honest conclusion
On iOS, **a tap inside any keyboard cannot start recording without the containing app being alive**. There are exactly two ways to make the app alive *without showing it*: (1) it is already alive (background audio), or (2) iOS 18's `AudioRecordingIntent` fired from a system trigger (Action button, Back Tap, Control Center, Lock Screen control, Siri, Live Activity button). Neither can be fired by a keyboard tap. The owner's requirement "record on the spot anywhere" is therefore met by **system triggers**, while the **keyboard mic** is instant whenever Gusan is alive and otherwise needs one unavoidable, visible activation (which we make as short as iOS allows).

### 2.4 Chosen mechanism (data flow)
```
[Action button / Back Tap / Control Center / Siri / Live Activity button]
        │ App Intent: StartListeningIntent (AudioRecordingIntent, LiveActivityIntent; openAppWhenRun = false)
        ▼
Gusan app process (background)  ── Activity.request(GusanSessionAttributes) ──► Dynamic Island "listening" glow
        │ VoiceSession.start() → activateAudio() → resumeCapture()            (Live Activity is mandatory, Apple)
        │ chime (AVAudioPlayer, short, .mixWithOthers)
        │ VoiceStatus(.recording) → VoiceChannel → keyboard strip shows waveform if the keyboard is up
        ▼
Stop: Live Activity button ■ / Action button again / keyboard mic / silence timeout
        │ WAV 16 kHz mono → transcribe-proxy (Gladia) → smart-action (Smart) or agent-action (Agent)
        ▼
Result: keyboard up → VoiceTranscript/VoiceStatus(.done) → insertion at cursor (§5)
        keyboard not up → UIPasteboard.general (app process) + Live Activity "Copied · Open"
```
Keyboard mic tap decision tree (`KeyboardVoice.tap` extended):
1. App alive (`VoiceStatus.isAlive`) → `VoiceCommand(.start)`; recording starts in <300 ms; strip goes to Listening; the Live Activity (if any) updates to "listening". **No hop.**
2. App not alive, iOS 18, user has set up a trigger → still no public bridge; the strip performs the activation hop **once**, then the app is alive for the configured window.
3. Activation hop: `gusan://voice` → the app opens straight into recording (already true: `start(pendingWithin: 60)`), shows a minimal full‑screen "Listening" view, **no onboarding, no tabs**; on sideload it returns automatically (B); on App Store it shows "◀ back to <app>" text (iOS adds the back link in the status bar automatically after an `openURL`).

Keep‑alive policy (replaces the 1…60 min idle picker):
- "Stay ready" durations: **15 min (default), 1 h, until I stop**. While ready the orange dot is visible — this is honest and unavoidable; the Live Activity shows "Gusan is ready · Stop" so the user can end it and the dot together.
- While ready, `AVAudioEngine` keeps running (don't pause; S1 decides whether pausing is safe). Samples are discarded unless `.recording` — already how `VoiceAudioBuffer.append` behaves **[repo]**.

### 2.5 Failure modes
- Mic permission denied → intent fails with a user‑visible error dialog from Shortcuts; app shows the Settings deep link (`VoiceError.micPermissionDenied` exists **[repo]**).
- Interruption (call) → `AVAudioSession.interruptionNotification` handled; without `.shouldResume` the session stops and the Live Activity ends with "Interrupted" **[repo: `VoiceSession.interrupted`]**.
- App killed while ready → next keyboard tap sees no heartbeat → activation hop (rule 3). The Live Activity is ended by iOS when the process dies? **No** — a Live Activity survives its process; we must end it in `applicationWillTerminate` best‑effort and show a stale‑state guard (`staleDate`, iOS 16.2+) of e.g. 20 min so iOS greys it out.
- `AudioRecordingIntent` without Live Activity → iOS stops recording (Apple) — the intent must `Activity.request` *before* `activateAudio()`.
- Another app holds the mic exclusively → `AVAudioEngine.start()` throws → `audioFailure` → `.failed` status with message **[repo]**.

### 2.6 Recommendation and exact user‑visible behaviour
1. Ship the iOS 18 `StartListeningIntent` with a Control Center control, Action‑button/Back‑Tap setup guide (Home › "Record from anywhere" card with one‑tap "Add to Control Center"/"Open Back Tap settings"), and a Siri phrase. **This is the "anywhere and everywhere" answer.** Deployment target moves to iOS 17 for the app (Live Activity buttons) with iOS 18 checks around the intent; keyboard stays iOS 16‑compatible.
2. Keyboard mic: instant when Gusan is ready; otherwise one activation (short, sideload returns automatically). Default readiness 15 min after any use so the activation is rare.
3. Tell the user the truth in the strip the first time: "iOS lets only the Gusan app hear the microphone. Add the Gusan control to Control Center or the Action button to start listening from anywhere." (One sentence, once.)
4. Owner decision needed: accept the orange indicator during the ready window, and the default duration (D1).

### 2.7 Effort
Intent + Live Activity scaffolding + control: **4–5 days**; keyboard decision tree + copy: **1 day**; device spikes S1–S5: **1–2 days** on the owner's phone (the macOS VM has no microphone and the Simulator cannot host third‑party keyboards for this).

---

## 3. Dynamic Island glow and Live Activity for Agent progress

### 3.1 What iOS allows **[Apple doc: activitykit/displaying-live-data-with-live-activities, activity/request(attributes:content:pushType:), pushToStartToken, ActivityAuthorizationInfo]**
- ActivityKit iOS 16.1; `Activity.request` from the **foreground only** — "you can't do this while your app is in the background, unless you adopt `LiveActivityIntent` and start the Live Activity using a `LiveActivityIntent`" — so the intent path of §2 is also the only way to start one from the background on a free Apple ID.
- `update(_:)` / `end(_:dismissalPolicy:)` are allowed from the background while the app has runtime (background audio or a BGTask window). Content (static + dynamic) ≤ 4 KB. Max 8 h active; then it stays on the Lock Screen up to 4 more hours (12 h total). A Live Activity "runs in its own sandbox… can't access the network"; UI lives in a **widget extension** with `NSSupportsLiveActivities = YES`. The user can turn Live Activities off per app (`ActivityAuthorizationInfo.areActivitiesEnabled`).
- Push‑to‑start (`pushToStartToken`) iOS 17.2+, and remote `update`/`end` need APNs → Push Notifications capability → **paid team only**. Frequent pushes need `NSSupportsFrequentLiveActivityUpdates` and the user's permission.
- Interactive buttons/toggles (iOS 17) run App Intents in the app's process; deep links via `widgetURL`/`Link`.
- Dynamic Island: compact leading/trailing, minimal, expanded (long press). With two apps' activities, the second becomes minimal (detached).
- Dismissal: the user swipes the Lock Screen presentation away → the activity is gone from the UI (the app still sees it via `Activity.activities` until it ends). There is no "cannot remove" Live Activity; the owner's "notification you cannot remove unless it's done" is approximated by `ActivityUIDismissalPolicy.default` + ending with `.after(date)`, and by **re‑surfacing the result in the Sessions tab** (always).

### 3.2 Chosen mechanism
One activity type, `GusanSessionAttributes` (static: sessionID, mode) with `ContentState { phase: listening | working | agentRunning | needsConfirm | needsLogin | done | failed, title, step, progress, resultPreview, copied }`:

| Place | Listening | Agent running | Needs confirm / login | Done |
|---|---|---|---|---|
| Compact leading | mic glyph with animated **variable‑colour ring** (`Image(systemName: "waveform").symbolEffect(.variableColor)`; symbol effects in widgets are iOS 17+, continuous animation support inside Live Activities is limited **[unverified]** → fallback: two‑frame pulse on each `update`) | spinner glyph | ⚠︎ | ✓ |
| Compact trailing | elapsed `Text(timerInterval:)` | step count | "Tap" | "Copy" |
| Expanded | waveform bars (last levels, ≤ 24 values in state) + **■ Stop** button (intent) | title, last step, **Stop** | summary + **Confirm**/**Cancel** (Confirm never where Cancel was: fixed left/right slots) or **Open to sign in** | result preview + **Copy** (intent writes `UIPasteboard.general`) + **Open** (deep link `gusan://session/<id>`) |
| Lock Screen | same as expanded | same | same | same |

"Dynamic Island glow": the island itself cannot be tinted or outlined by third‑party apps; what *is* possible is a compact presentation whose leading/trailing views animate (symbol effect / opacity pulse), which reads as a glow. Inside Gusan's own app (foreground), the listening sheet can additionally draw an animated ring behind the island area — purely cosmetic, in‑app only.

Starting rules:
- Intent path (§2): the intent starts the activity (mandatory).
- Keyboard mic with the app alive but in the background: the app **cannot** `Activity.request` → if no activity exists, no island; the strip's waveform is the feedback. **Mitigation:** the app requests the "Gusan ready" activity whenever it is foregrounded (activation hop, app open) and keeps it for the ready window, so in practice it exists.
- Agent task from the keyboard: same rule; progress goes into the existing activity if present, else into the Sessions tab + local notification (§6.6).

Updating from the server (Agent): the app process polls `agent-action status` (`wait_s` long‑poll up to 20 s **[repo: agent-action header]**) while alive (`AgentCenter.setActive` already polls **[repo]**) and calls `activity.update`. When the app is dead, nothing updates until it runs again (BGAppRefresh is opportunistic — `AgentCenter.registerBackgroundRefresh` exists **[repo]** but iOS decides when). App Store builds: `agent-action` webhook → Supabase → APNs `liveactivity` push to the activity's `pushToken` (stored by a new `register_device` action in an `agent_devices` table) → updates even when the app is dead; push‑to‑start can create the activity when a task reaches `awaiting_confirmation`.

### 3.3 Failure modes
- Activities disabled by the user → `ActivityAuthorizationError.denied`: fall back to strip + local notifications; show a one‑time hint in Settings › Notifications.
- 8 h cap: agent tasks have deadlines ≤ 900 s + grace **[repo: agent-action]**; the "ready" activity ends with the ready window — never near 8 h.
- App killed → activity frozen; `staleDate` greys it; on next launch `Activity.activities` is reconciled with `agent_tasks` and ended/updated.
- 4 KB state: keep `resultPreview` ≤ 300 chars; the full result is in the DB.

### 3.4 Sideload vs App Store
| | Sideload | App Store |
|---|---|---|
| Local start/update/end, buttons, deep links | yes | yes |
| Push‑to‑start, remote updates, remote end | **no** | yes |
| Extra App ID | +1 (widget extension; total app+keyboard+screen+widget = 4 of 10 per 7 days) | n/a |

### 3.5 Effort
Widget extension + activity views + intents: **4 days**; app‑side state sync with `VoiceSession`/`AgentCenter`: **2 days**; APNs path (App Store only): **2 days** server + **1 day** client.

---

## 4. Screen context (two frames via ReplayKit)

### 4.1 What iOS allows
- Only a **broadcast upload extension** (`com.apple.broadcast-services-upload`, `RPBroadcastSampleHandler`) sees other apps' screens; it starts only after the user taps Start in `RPSystemBroadcastPickerView` (or Control Center's screen‑recording control) and iOS shows the red status indicator for the whole broadcast; tapping it or Control Center stops it. **[Apple doc: ReplayKit; repo: `ScreenView.swift`, DESIGN.md §9]**
- A broadcast cannot be started programmatically, cannot auto‑restart after a stop or reboot, and the extension is killed when it exceeds its memory limit — widely reported at **50 MB** **[unverified; not in the API reference]**. The repo already budgets ≈15 MB worst case (four 1280 px frames + the frozen one) and logs `os_proc_available_memory()` in debug builds **[repo: `SampleHandler`]**.
- The extension has no UI and no network in our design (it never uploads) **[repo]**; the keyboard calls Gusan Cloud because it owns the session token.

### 4.2 Chosen mechanism (builds on PR #20)
- `ScreenChannel` (App Group file dir `screen/` **and** team pasteboard `com.aram2k.gusan.screen`, newest‑wins, packet lifetimes status 60 s / request 30 s / frame 10 s / keyboard 180 s) **[repo]**.
- **"Frame from before the keyboard covered the screen":** the keyboard publishes `ScreenKeyboardState` (appearance time, height, screen height) on `viewDidAppear`; the extension freezes the newest frame older than that instant and answers requests with it; if none exists it crops the keyboard band (≤70 %) off the latest frame **[repo: `SampleHandler.keyboardChanged`, DESIGN.md §9.3]**. This is exactly the brief's requirement and is implemented.
- **Two frames:** extend `ScreenRequest` with `kind: .start | .finish`. `.start` = frozen pre‑keyboard frame (today's behaviour). `.finish` = at mic‑stop, the newest frame; if the keyboard is still up it is the same frozen frame (skip sending a duplicate: compare `ScreenFrame.hash`), if the user navigated (keyboard dismissed and re‑shown), it is the new pre‑keyboard frame. Both go to `smart-action` as two `image_url` data‑URI parts (the function keeps only data URIs **[repo: `onlyDataImages`]**; max 4 images in `agent-action` **[repo]**).
- Consent & indicators: the system picker and the red indicator are the consent; Gusan adds (a) the iPhone icon in the strip being **selected** (intent) vs **live** (red dot prefix on the context chip, already implemented **[repo: `updateContextTitle`]**), (b) a one‑time acknowledgement sheet with version (`ScreenSettings.acknowledged`) **[repo]**.
- The strip icon: replace `eye`/`eye.slash` with an iPhone outline (`iphone` SF Symbol, filled when live) per the owner.

### 4.3 "Always on" vs "starts when first selected, stops after idle"
| | Always on | Start when needed, stop after idle (recommended) |
|---|---|---|
| Feasibility | **Not possible**: no programmatic start, and the red indicator would be permanent; iOS kills long broadcasts under pressure. | Possible today: `ScreenIdleTimer` ends the broadcast after N minutes without a request (default 10) **[repo]**. |
| UX | — | Selected icon + first mic tap with no live broadcast → the strip says "Start Gusan Screen" with **Open** (`gusan://screen` → Home › Gusan Screen → picker). After Start, the user returns to the field; every later use is silent until idle stop. |
| Privacy | — | Red indicator only while live; frames only on request; nothing stored. |

Recommendation: **start when first needed, stop after idle**, with idle options 5 / 10 / 30 min / "until I stop" (D2). The icon's *selected* state persists in `ScreenSettings`; *live* is read from the heartbeat.

### 4.4 Privacy
Sent to Gusan Cloud only on a request: ≤2 JPEGs (long edge 1280, ≤450 KB each), the instruction/transcript, the field text iOS exposes, and the session token. Never audio, never continuous frames. The pasteboard copy of a frame is readable by apps signed by the same team for ≤10 s — acceptable on sideload; on App Store the App Group works and the pasteboard store can be disabled. Secure fields: the keyboard is not shown at all, so no frame can be requested there; but ReplayKit does not blank other apps' secure content — the acknowledgement sheet says so **[repo: DESIGN.md §9.5]**.

### 4.5 Failure modes
- No frame within 4 s (`pendingWindow`) → "No picture from Gusan Screen" + Retry **[repo]**.
- Broadcast ended mid‑dictation → `screen.onLost` cancels screen use for that request; the Smart request proceeds **without** the frame (don't fail the dictation).
- Memory kill of the extension → heartbeat stops within 10 s → icon goes to *selected, not live*.

### 4.6 Sideload vs App Store
Same code; +1 App ID (already counted). Sideloadly may rename the extension so the picker can't preselect it (`preferredExtension` derived from `Bundle.main.bundleIdentifier + ".screen"`) → the sheet lists all broadcast extensions **[repo: README]**.

### 4.7 Effort
Two‑frame request + icon + idle options + strip wiring: **2 days** on top of PR #20.

---

## 5. Smart mode pipeline

### 5.1 Transcription (`transcribe-proxy`)
- Client: `TranscribeClient.request` posts JSON `{audio: base64 WAV, sample_rate: 16000, language, provider, languages, model: "fast"}` with the Supabase JWT; ≤ 24 MiB base64 (≈9 min) else 413 → `VoiceError.tooLong`; 90 s timeout **[repo]**. The app caps a take at 300 s **[repo: `VoiceMachine.maxRecording`]**.
- Server (PR #14 branch): Gladia is selected with `model: "gladia"` (Solaria 3 → Solaria 1) or `"gladia-fusion"`; secret `Gladia_STT`; async batch API (upload → start → poll → delete) inside one timer; falls through the BEST order on failure **[repo: gladia-stt branch, `transcribeGladia`]**. Change for iOS: send `model: "gladia"` and `language` from Settings (default auto); Armenian is covered by Solaria‑1, while Solaria‑3 is limited to en/fr/de/es/it **[repo: line 612]**.
- Latency budget: 5‑second take ≈ 1.5–3 s with Gladia batch **[unverified until measured; S6]**. Streaming STT would need a WebSocket from the app, out of v1.

### 5.2 Decide + act (`smart-action`)
- Today the voice path already asks for JSON `{"kind":"edit"|"insert","text":...}` with `SmartAction.voiceSmart(transcript:)` and parses it defensively (`VoiceSmartResponse.parse`, fenced/escaped JSON tolerated, fallback = insert the transcript) **[repo: `Prompts.messages`, `SmartActionJob`]**. Keep this contract; extend the user payload to `{"speech", "selected", "before", "after", "screen": [2 images], "knowledge": {"calendar": [...], "now": ISO, "tz"}}` and the system prompt to the brief's rules: selected text is always context and is what gets replaced; otherwise around‑cursor; the answer is the full replacement for `edit`, or new text for `insert`; add a third kind `"answer"` for questions ("am I free Friday at 3?") which is inserted at the cursor like dictation.
- Server: mode `smart_auto` (Pro or free quota), images only as data URIs, `DAILY_CAP` 1000/day **[repo]**; routes live model → OpenAI → Claude → Mistral → Gemini. Calendar knowledge is fetched server‑side in parallel with auth (see §8) and injected as a system message — the client never computes it.
- Web search: Linkup only in Smart (PR #15 wiring), never in Agent **[brief]**.

### 5.3 Insertion at the cursor (`UITextDocumentProxy`)
- `insertText` inserts at the insertion point and **replaces the current selection** (UIKeyInput semantics) — that is how both the keyboard's Smart path and the voice path replace a selection **[repo: `run()` and `VoiceEdit.plan(.replaceSelection)` with `deleteCount 0`]**.
- Around‑cursor edit: `adjustTextPosition(byCharacterOffset: after.utf16.count)` then `deleteBackward()` × characters, then `insertText` **[repo]**. This is the only portable way; it is slow for long contexts (one call per character) — cap the editable window at the paragraph (`VoiceContext.paragraph` already does) **[repo]**.
- Guards: apply only if `documentIdentifier`, selection and both context sides still equal the snapshot; otherwise show the result card **[repo: DESIGN.md §7.2; `KeyboardViewController.run` "Text changed"]**. `documentIdentifier` can trap when bridged inside some hosts → use the `documentID` KVC wrapper from PR #20 (`GusanKeyboard/DocumentProxy.swift`) **[repo]**.

### 5.4 Undo that survives further typing
`ReplacementUndo` records original/replacement/snapshot and a `tail` of characters typed after the insertion (`append`, `deleteBackward`); `step(context:)` validates the document against `snapshot.before + replacement + tail` and computes advance/delete/insert/restore; `inverted()` gives Redo **[repo: `ReplacementUndo.swift`, tests in `ReplacementUndoTests.swift`]**. Owner rule "Undo 30 s": add `expiresAt` to the strip state (timer started at insertion, reset on nothing), after which `invalidateUndo()` runs; typing keeps Undo valid within the 30 s (already supported by the tail). Selection change or document switch invalidates (already).

### 5.5 Host‑app quirks (test matrix; all **[device test needed: S7]** — these are known classes of problems, not verified facts for each app)
| Host | Known issue class | Mitigation |
|---|---|---|
| WhatsApp | `documentContextBeforeInput` limited to the current paragraph; `documentIdentifier` stable; `adjustTextPosition` fine | none needed |
| Gmail (web‑based compose in WKWebView) | `selectedText` often nil even with a selection; `adjustTextPosition` unreliable; `documentContext*` may be empty | detect empty context + non‑empty field → fall back to insert‑only (never delete); offer "Copy" via the result card only when insertion verifiably failed |
| Notes | attributed text, checklists, tables: `deleteBackward` across list bullets behaves oddly | limit edits to a single paragraph; verify by re‑reading context after the edit (`VoiceEdit.containsInsertedResult`) |
| Safari URL bar | `UIKeyboardType.URL`, no autocorrect, `returnKeyType .go`; return must not insert "\n" | respect `returnKeyType` (already passed to `keyArea.updateContext`) |
| Messages | fine; emoji keyboard switching via globe | none |
| Secure fields, phone pads | Gusan not shown | none |

### 5.6 Clipboard fallback
- There is no "no text field" case *inside* the keyboard (no field → no keyboard). The fallback applies to the system‑trigger path (§2): the app writes `UIPasteboard.general` and the Live Activity says "Copied · Open". Writing the pasteboard from a keyboard extension requires Full Access (we have it) and is used only when insertion verifiably failed (context unchanged after `insertText`), shown as a one‑line "Couldn't insert here · Copied" — this is a *fallback*, not a Copy button (brief: no Copy buttons).

### 5.7 Failure modes
Network/timeouts map to `VoiceError`/`GusanError` with messages already written **[repo]**; cancellation anywhere leaves text untouched (`Task.checkCancellation` before mutation) **[repo]**; empty transcript → `nothingHeard`.

### 5.8 Effort
Payload/prompt extension + `answer` kind + calendar hook: **2 days** (server + GusanKit tests); strip wiring and the 30 s Undo: **1 day**; host matrix on device: **1 day**.

---

## 6. Agent mode on H Company (mobile‑only)

### 6.1 What exists (PR #18 `supabase/functions/agent-action/index.ts`, PR #19 iOS) **[repo]**
- Actions `start | status | confirm | cancel` (+ `?action=webhook` with HMAC + 300 s window). Task object: `task_id, mode (research|do), status, title, steps (≤25), confirmation {summary, action_kind, start_url, steps}, result {markdown, links, images}, outcome, error_code, timestamps, deadline_at`. Statuses `queued, running, awaiting_confirmation, completed, failed, cancelled, timed_out`. Limits 2 active / 40 starts per 24 h (`begin_agent_task` RPC with advisory lock), 8 MiB body. Tables `agent_tasks` (owner RLS, realtime publication), `agent_task_runs` (service‑role only: `h_session_id`, `phase prepare|execute`, `plan`), `agent_task_starts`.
- **Prepare/confirm/execute gate** (the owner's structural requirement, motivated by the observation that H agents ignore "ask before submitting" prompts): `do` tasks first run a *text‑mode, read‑only* session (`mode: {type: "text"}`, 300 s / 60 steps) whose `answer_format` is a plan schema; the task becomes `awaiting_confirmation` for 15 min; `confirm` creates a **new visual session** (600 s / 120 steps) with `executePrompt(plan)` and idempotency key `${task.id}:execute`; `cancel` ends it. Research runs text‑mode only. H agent `h/web-surfer-flash`, `max_time_s`, `max_steps`, `timezone`, instructions with prompt‑injection guards, screen context fenced in `<user_screen_context>`.
- iOS: `AgentService` protocol, `AgentClient` (live), `AgentJob: GusanJob` (polls, emits `.progress`/`.needsConfirmation`/`.result`), `AgentIntent.detect` (deterministic verb lists: do/research/edit), `AgentCenter` (polling, local notifications, BGAppRefresh `com.aram2k.gusan.agent-refresh`), `AgentKeyboardController` (card in the keyboard), `ActivityView`.

### 6.2 Target state machine (server, `agent_tasks.status`)
```
queued ─► running(prepare) ─► awaiting_confirmation ─► running(execute) ─► completed | failed | timed_out
   │            │                   │ (15 min)                 │
   │            │                   └─► cancelled               ├─► awaiting_login ─► running(execute, new session) …
   │            │                                               ├─► awaiting_code  ─► running …
   │            └─► completed (research) ─► idle ─► running (continue) …
   └─► cancelled
```
Additions over PR #18:
- `awaiting_login {hostname, login_request_id}` and `awaiting_code {source: sms|email|authenticator, prompt}` — raised by **custom tools** `request_login(hostname)` and `request_otp` (H's documented custom‑tool mechanism; the session goes `awaiting_tool_results`) **[H doc: vaults/overview "Email, SMS, and confirmation links"; sessions/overview]**. PR #18 explicitly uses no custom tools ("No phase supports custom tools; approval is a new session") — this changes for the login/OTP flows only; the confirm gate stays a new session.
- `idle` (research results can be continued): create sessions with `idle_timeout_s` (e.g. 600) so the agent waits for follow‑ups **[H doc: sessions/create `idle_timeout_s`]**; note H ends idle sessions ≥120 s old to make room at the concurrency limit (3 on Free, 10 on Builder) **[H doc: plans‑and‑limits]** → a `continue` after that must `send_messages` to a finished session, which *restarts* it at `pending` (billed again) **[H doc]**. New actions: `continue {task_id, message}`, `stop {task_id}` (`DELETE /sessions/{id}` → `interrupted`, resumable by a message **[H doc: sessions/cancel]**), `chat {task_id}` (returns the user/agent message events from `GET /sessions/{id}/events`, filtered to `user_message`/answer events — never tool payloads).
- Memory: table `agent_memory (user_id, kind: preference|fact, text, approved_at)`; the app proposes memories from completed tasks ("Remember: you prefer aisle seats?"), the user approves; approved items are sent as a per‑session inline **skill** (`skills: [{name:"user-memory", body}]`) **[H doc: sessions/send‑messages `skills`]** — not as instruction text, so they cannot be confused with the task.
- Mobile‑only: H has no mobile‑emulation flag **[H doc: browser/configuration — viewport `width`/`height` only]**; "mobile‑only" is a *Gusan* scope statement (the feature exists only in the iPhone app). Optionally set `mode: {type:"visual", width: 430, height: 932}` for execute sessions so sites render their mobile layouts (D3).
- Irreversible‑action confirm expiry stays 15 min; "Confirm never where Cancel was": fixed slots in the strip card and the Live Activity.

### 6.3 Client flow
Strip (Agent mode) → mic → transcript → `agent-action start {instruction, mode: AgentIntent, context: selected text (+ screen frames ≤4), timezone}` → 3 s card ("Understood: …") → collapse. Progress: Live Activity (§3) if present, else Sessions tab; polling by the app (`AgentCenter`) while alive. Confirm: strip card if the keyboard is up, else Live Activity/notification. Login: §7. Result: Live Activity "Copy / Open", Sessions › chat view with Continue (`continue`), Stop, memory proposals.

### 6.4 Notifications
| | Sideload (free Apple ID) | App Store |
|---|---|---|
| Remote push | **Not available** (Push Notifications capability requires a paid team) | APNs token‑based auth (.p8) from a new edge function `push-notify` called by the `agent-action` webhook path; device tokens in `agent_devices` |
| Local notifications | `UNUserNotificationCenter` from the app process — only while the app runs (foreground, background audio, or a BGAppRefresh window). `AgentCenter.deliver` exists **[repo]** | same, plus remote |
| Live Activity updates | from the app process only | ActivityKit push (`liveactivity` push type) |
| Practical consequence | Keep the app alive during tasks (the §2 ready window is reused: an active task extends it until the task is terminal) | works even if the app is killed |

### 6.5 Failure modes (server codes exist **[repo]**): `agent_not_configured`, `agent_busy` (H 429 → 503), `too_many_active`, `quota_exceeded`, `plan_missing`, `confirmation_expired`, `upstream_error`, deadlines +180 s grace. New: `login_expired` (user did not approve within 15 min → cancelled), `otp_timeout`.

### 6.6 Effort
Server: `awaiting_login`/`awaiting_code` via custom tools + ephemeral vault binding (§7) **4 days**; `continue/stop/chat` + idle sessions **2 days**; memory **1 day**; APNs (App Store only) **2 days**. iOS: Sessions tab chat view, continue/stop, memory UI **3 days**; Live Activity hooks **(in §3)**.

---

## 7. Vault: logins the agent never sees

### 7.1 What iOS allows
- **There is no API to read or enumerate the user's iCloud Keychain passwords.** `ASAuthorizationPasswordProvider` returns credentials *only for the app's own associated domains* ("Password AutoFill recommends credentials only for your app's associated domain, and the user must authenticate using Face ID or Touch ID") **[Apple doc: security/password‑autofill; authenticationservices/asauthorizationpasswordprovider]**. Associated domains also require a paid team.
- **What does work:** a form with `UITextField`s of `textContentType = .username / .password`. iOS shows the Password AutoFill QuickType bar; the user taps the key icon → **Face ID** → picks *any* saved login (iCloud Keychain or a third‑party credential‑provider extension) → iOS types username and password into the fields **[Apple doc: Password AutoFill "heuristics… even if those apps haven't been updated"]**. The app then holds the plaintext in memory — unavoidable on iOS; nothing lets the system hand a secret to a server on the app's behalf.
- Face ID: `LAContext.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics)` **[Apple doc]**, or better, keychain items created with `SecAccessControl(.biometryCurrentSet)` so the *read* itself requires Face ID and is invalidated if biometrics are re‑enrolled. Keychain class `kSecAttrAccessibleWhenUnlockedThisDeviceOnly` (not synced, not in backups). The keyboard extension **never** touches these items (different access group; `KeychainSessionBackend` only stores the Supabase session **[repo]**).

### 7.2 What H allows **[H doc: vaults/overview, create‑item, reveal‑item, browser/configuration]**
- Vaults: `provider: internal` (H‑hosted) or `onepassword` (service‑account token, write‑only). Items: `urls` (1–20), `values` of `username|password|email|totp|api_key` (≤4096 chars). **"The values travel in clear text inside the body. Send them only over HTTPS and never log the request."** A vault is bound to a *web environment at session creation* via `vault_id`; "Cloud browsers only". The agent gets a `fill_secret_at` action when a vault item matches the page hostname (domain‑gated, closest host wins); *"types the value into the page, outside the agent's context"*; for `totp` it types the current code.
- **Reveal an item** (`GET /vaults/{vault}/items/{item}/reveal` equivalent) returns `values` in clear text (never the TOTP secret, but its current code) to **any member of the organisation**; every reveal is audit‑logged by H. The docs' sentence "No endpoint returns secrets" in the overview is therefore inaccurate for H‑hosted vaults: *the API key can reveal everything*. Treat `H_company_computer_agents` as the crown jewel.
- Limits: 1000 items per organisation across all vaults **[H doc]**. No documented endpoint changes a *running* session's environment, so a vault cannot be bound mid‑session **[H doc: absence in browser/configuration and sessions pages — unverified beyond the docs]**.

### 7.3 Is end‑to‑end (no plaintext at Gusan Cloud) achievable?
**No**, not with the `internal` provider: the item must be created by a server‑held H key with plaintext values in the request body; the phone cannot call H directly without exposing that key. Two mitigated designs:

| | A. Internal provider via Gusan Cloud (v1) | B. 1Password provider (later option) |
|---|---|---|
| Plaintext at Gusan Cloud | **Transient, in memory of one edge‑function invocation**, never stored, never logged | **Never** — the user saves logins in 1Password; H fetches them with the user's service‑account token |
| Plaintext at H | yes (H‑hosted vault, revealable with the org key) | H decrypts on fill; token "validated against 1Password, write‑only, never returned" |
| User prerequisite | none | 1Password subscription + creating a service account (power‑user step) |
| Per‑user isolation | one H vault per Gusan user *or* ephemeral per session (below) | one H vault per 1Password vault |

Recommendation: **A with ephemeral per‑session vaults for v1; B as an opt‑in Connection later** (D4).

### 7.4 Chosen mechanism (A, ephemeral per‑session, Face ID per use)
Storage: the credential lives **on the iPhone** in Gusan's app keychain (`service: Gusan.vault.<uuid>`, `kSecAttrAccessibleWhenUnlockedThisDeviceOnly`, `SecAccessControl(.biometryCurrentSet)`), plus metadata in `vault_logins` (Supabase: `id, user_id, hostname, username_hint ("a***@gmail.com"), has_totp, always_ask, created_at, last_used_at`) — **no secret columns anywhere in Gusan's DB**.

```
Add login (Vault tab)                        Use login (agent needs it)
───────────────────                          ─────────────────────────
Site, Username, Password[, TOTP] fields      agent → request_login(hostname) custom tool
 └ key icon → Face ID → iOS AutoFill          └ agent-action: task.status = awaiting_login, tool call parked
Save → LAContext/biometric keychain write     app/strip: "Login needed for example.com · Use <hint>"  → Face ID
 └ POST vault-action {add, hostname, hint}    app reads the keychain item (Face ID) → POST vault-action
   (metadata only)                              {release, task_id, login_id, values} over TLS (body never logged)
                                              vault-action: create ephemeral H vault  →  add 1 item {urls:[hostname], values}
                                                            → create NEW execute session with vault_id = ephemeral,
                                                              browser_profile_id = user's per-user profile (persist: true)
                                                              so cookies/tab state carry over, prompt = resume plan from step k
                                                            → old session cancelled; task.status = running
                                              agent sees fill_secret_at on the matching page only; never the value
                                              session terminal → vault-action deletes the item and the vault; audit row
```
Why a new session: the vault must be bound at creation (§7.2). This mirrors the existing prepare→execute pattern (`confirm` already creates a new session with an idempotency key **[repo]**). The per‑user **browser profile** (`use_default_browser_profile` is per *H user* = Gusan's org account, so instead create one named profile per Gusan user and pass `browser_profile_id` + `persist_browser_profile: true`) **[H doc: browser/profiles]** preserves the login state across the session swap and across tasks — after the first Face‑ID‑assisted login, later tasks on that site are usually already signed in and no secret is released at all.

"Always ask before using a login" (default **on**) = the flow above. Off = at the user's explicit choice, the login is additionally copied into a **persistent per‑user H vault** bound to every execute session (silent sign‑in, domain‑gated). Both modes keep the keyboard extension out of the loop: the strip only shows "Login needed · Open Gusan" (deep link) — Face ID and keychain reads happen in the app.

MFA/TOTP: the Add‑login screen accepts an `otpauth://` URI (camera QR scan via `DataScannerViewController`/AVFoundation, or paste); H generates codes; the agent types them via `fill_secret_at` without seeing the secret **[H doc]**. SMS/email codes: `request_otp` custom tool → `awaiting_code` → the user types the 6 digits in the strip card or app → posted as the tool result → agent continues; the code is single‑use and never stored. Sign in with Google/Apple inside third‑party sites: **not supported in v1** (would need the user's Google session in the cloud browser; the browser profile may keep it after a one‑time manual sign‑in via H's Agent View — D5).

### 7.5 Guarantees (who can see what)
| Party | Sees plaintext? | How it is prevented |
|---|---|---|
| H agent (model) | **No** | `fill_secret_at` types outside the agent context; domain gating; instructions forbid asking for credentials **[repo: `instructions()`]** |
| Gusan DB | No | no secret columns; `vault_logins` is metadata; RLS owner‑only |
| Edge‑function logs | No | `vault-action` never logs bodies; Supabase platform logs record metadata/status only (verify with a test request + log read **[S8]**); error responses never echo values |
| Prompts / chat / notifications / Live Activity | No | the task object exposes only `hostname` + `username_hint`; `agent_task_runs` (service‑role only) holds no values; H events may contain the *page* but `fill_secret_at` is logged by H as an action without the value |
| Clients (keyboard, widget) | No | only the app process reads the biometric keychain items |
| Gusan Cloud (transient) | **Yes, in memory during `release`** | no persistence, no logging, immediate forward to H, item/vault deleted at session end |
| H (at rest) | Yes, for the lifetime of the ephemeral vault (minutes) or the persistent vault (if "always ask" is off) | H's encryption at rest; **the org API key can reveal** — key kept only in Supabase secrets; rotate on any suspicion; ask H whether reveal can be disabled for an org (D6) |

Audit log: `vault_events (user_id, login_id, task_id, event: added|released|denied|expired|deleted|failed, hostname, at)` shown in the Vault tab ("Used for Booking.com · yesterday 14:02"); never values. Deletion: Vault › Delete removes the keychain item, `vault_logins` row, any persistent H item; account deletion (`delete-account` function) also deletes the per‑user H vault and browser profile.

### 7.6 Threat model (abridged)
- Stolen H API key → reveal of persistent vault items (only users with "always ask" off) and of in‑flight ephemeral items. Mitigation: ephemeral default, short lifetime, key in Supabase secrets only, no client ever holds it (unchanged policy **[repo: DESIGN.md §8]**).
- Compromised Gusan Cloud code → could exfiltrate during `release`. Mitigation: `vault-action` is a small, separately reviewed function; releases require a valid task in `awaiting_login` for that user and hostname; rate limits (≤3 releases per task).
- Phishing by the agent ("page asks for password") → domain gating means `fill_secret_at` is only offered on the stored hostname; `request_login` for an unexpected hostname is shown to the user verbatim before Face ID.
- Device compromise / shoulder surfing → biometric keychain; items not in backups; no sync.
- Prompt injection from pages → existing guards; the login item URLs are set by the user, not the agent.

### 7.7 Sideload vs App Store
Identical: the biometric keychain and `LocalAuthentication` need no entitlement. (Keychain *sharing* groups may break on re‑signing — we deliberately keep vault items in the app's **own** keychain, not a shared group.) Password AutoFill's key icon works in any app.

### 7.8 Effort
`vault-action` function (add/release/delete/cleanup) + ephemeral vault + session swap + `request_login`/`request_otp` tools: **5 days**; iOS Vault tab (AutoFill form, QR TOTP, Face ID, audit): **4 days**; threat‑model review + log verification S8: **1 day**.

---

## 8. Connections: Google Calendar and iPhone Calendar

### 8.1 Google Calendar
- OAuth must end **server‑side** (tokens never on the phone): new edge function `google-oauth` (`start` → 302 to `accounts.google.com/o/oauth2/v2/auth` with PKCE + `state` bound to the user; `callback` exchanges the code with a **Web** client id/secret held in Supabase secrets; stores the refresh token **encrypted** with Supabase Vault/pgsodium in `connections (user_id, provider, enc_refresh_token, scopes, email, created_at)`). The app opens the start URL in `ASWebAuthenticationSession` with callback `gusan://connections/google` (the function's callback redirects there with a one‑time code; the token itself stays on the server). The desktop already implements the Google dance with the narrowest scope ("events on calendars you own") and a fallback **[repo: `calendar_sync.py SCOPES/SCOPE_OWNED/EVENT_SCOPES`]** — reuse the same scopes and the same "granted_ok" validation.
- Scopes v1 (read‑only): `https://www.googleapis.com/auth/calendar.events.owned.readonly` (the desktop's `SCOPE_OWNED` **[repo: `calendar_sync.py:75`]**) + `openid email`. Later write: `calendar.events` behind the Agent confirm gate.
- Google verification: calendar scopes are *sensitive* (not restricted) → app verification required before >100 users; Gmail scopes are **restricted** (CASA assessment) → defer Gmail (D7).

### 8.2 iPhone Calendar (EventKit)
- iOS 17: `requestFullAccessToEvents` or `requestWriteOnlyAccessToEvents`; **"Your app can't request read‑only access… To read events… your app needs full access"** **[Apple doc: eventkit/accessing‑the‑event‑store]**; `NSCalendarsFullAccessUsageDescription` required. So "read‑only first" is a *Gusan* promise (we never call save), not an iOS permission level — say so in the consent copy.
- The keyboard must not prompt for EventKit; the **app** reads events on foreground, on `EKEventStoreChanged`, and in BGAppRefresh, and writes a compact 14‑day digest (`[{start,end,title?,allDay,calendar}]`, ≤8 KB, titles optional per setting) to the App Group (`CalendarDigest.json`) with the same dual‑store fallback as settings **[repo: `SettingsStore` pattern]**. The keyboard attaches the digest to Smart requests when the Calendar connection is on.
- Later: `EKEventEditViewController` (no permission needed, out‑of‑process UI on iOS 17 **[Apple doc]**) for "add this to my calendar"; Contacts via `CNContactStore` with the same digest approach.

### 8.3 "Am I free Friday at 3?" instantly
`smart-action` receives `knowledge: {calendar: "google"|"iphone"|"both", now, tz}` plus the iPhone digest. For Google, the function fetches `events.list(timeMin=now-1d, timeMax=now+14d, singleEvents=true)` **in parallel** with the `is_pro`/quota checks it already runs in parallel **[repo: `Promise.all([userRes, proRes, quotaRes])`]**, normalises to the same digest shape, caches per user for 60 s in the function instance, and prepends a system message "Calendar (Europe/Paris, next 14 days): …". The model answers with `kind:"answer"`. Expected added latency ≈ 200–500 ms for the Google call (none for the iPhone digest). If Google fails, answer from the iPhone digest and say which source was used.

### 8.4 Failure modes
Token revoked → `connections.status = needs_reauth`, Settings shows "Reconnect"; EventKit denied → Settings row with Open Settings; digest stale (>24 h) → the answer says "based on your calendar as of yesterday".

### 8.5 Sideload vs App Store
Same. (Associated Domains / universal links are paid‑team only, hence the custom‑scheme callback.)

### 8.6 Effort
`google-oauth` + encrypted storage + knowledge hook: **3 days**; EventKit digest + settings: **2 days**.

---

## 9. Removal of Privacy Mode and other dead code; the 4‑tab app

### 9.1 Remove **[repo, locations]**
- `SettingsStore.privacyMode` (`GusanKit/SessionStore.swift:432`), `AgentCenter.setPrivacyMode`/`privacyMode` guards (`AgentCenter.swift:45–165, 257`), the `ActivityView` toggle (`ActivityView.swift:102`), `VoiceSession` agent‑handoff guard (`VoiceSession.swift:181` on PR #19), `AgentKeyboard.swift:88`, `KeyboardViewController.swift:289`, `AgentError.privacyMode`. Server: keep `agent-action`'s `privacy_mode`/`x-gusan-privacy-mode` refusal (desktop still sends it) but iOS never sends it.
- Preset actions and their UI: `GusanPanel` tiles, `InstructionHeader` + suggestion chips, `KeyboardDefaultAction`, `SettingsStore.translateTarget/keyboardAction`, long‑press default action on the logo, `VoiceAction` presets (`VoiceSettings.defaultAction`), the "View/Copy" buttons in `VoiceKeyController`, `HowToUseView`, Onboarding pages 1–5 → single checklist, the Keyboard tab (`KeyboardSettingsView`) merged into Settings. `prompts.json` modes other than `smart_auto` stay in GusanKit only if `scripts/tests/check_prompt_parity.py` still needs them (it compares with the desktop **[repo]**) — otherwise delete.
- `VoiceSettings.idleMinutes` picker → the ready‑window picker (§2.4); `autoReturn` stays (sideload only).

### 9.2 Keep
`ReplacementUndo`, `VoiceMachine`/`VoiceChannel`, `SessionStore` + diagnostics, `AgentClient/AgentJob/AgentTaskTracker`, `ScreenChannel/SampleHandler`, `JobRunner`/`GusanJob` (the agent and screen jobs plug into it **[repo: DESIGN.md §7.3]**).

### 9.3 App = 4 tabs
Home (mode, readiness checklist, Gusan Screen switch, "Record from anywhere" setup, Connections summary, last result) · Sessions (Smart | Agent segmented; chat view) · Vault · Settings (account, language, sounds, notifications, Connections, keyboard setup, diagnostics). Brand per brief; iOS‑native components only.

### 9.4 Effort
**3 days** including test updates (`SettingsTests`, `VoiceTests`, `AgentTests` reference the removed fields).

---

## 10. Build plan, CI/signing, tests, risks

### 10.1 Landing order for the open PRs
1. Merge **#13** (keyboard + voice) and **#18** (agent‑action) to `main` — they are the base of everything.
2. Rebase **#19** (iOS agent) and **#20** (Screen) onto `main`; merge.
3. New work in phases below, each as one PR on top.

### 10.2 Phases (the owner tests on his iPhone after each)
| Phase | Scope | Owner test on device | Effort |
|---|---|---|---|
| P0 | Spikes S1–S5, S8 (throwaway branch) | tap through the spike checklist | 2–3 d |
| P1 | Strip redesign + Smart‑only pipeline + 30 s Undo + Privacy‑Mode/dead‑code removal (§1, §5, §9) | dictate in Messages/WhatsApp/Notes/Gmail; Undo after typing | 7 d |
| P2 | Record‑from‑anywhere: `StartListeningIntent`, Control Center control, Live Activity (listening/done) (§2, §3) | Action button / Back Tap / Control Center start; island glow; result to clipboard; keyboard mic instant while ready | 8 d |
| P3 | Screen two‑frame + iPhone icon + idle options (§4) | start Gusan Screen, dictate about the screen | 2 d |
| P4 | Agent: Live Activity progress, confirm card/activity, Sessions tab chat, continue/stop, memory (§3, §6) | research + do task end‑to‑end | 8 d |
| P5 | Vault + `awaiting_login`/OTP (§7) | add a login from iPhone Passwords, agent logs in after Face ID | 10 d |
| P6 | Connections: Google Calendar, iPhone Calendar (§8) | "am I free Friday at 3?" | 5 d |
| P7 (App Store track, when a paid team exists) | APNs: push‑to‑start, remote Live Activity updates, remote notifications; drop private API and pasteboard fallbacks | — | 5 d |

### 10.3 CI and signing (sideload)
- `.github/workflows/ios.yml` runs GusanKit `swift test`, prompt parity, a Simulator build and `scripts/build_unsigned_ipa.sh` on `macos-15` **[repo]**. Add: the widget extension to the `codesign --sign -` loop and the PlistBuddy checks (`NSSupportsLiveActivities`, `NSExtensionPointIdentifier = com.apple.widgetkit-extension`), `GUSAN_SIDELOAD` stays the only place private API compiles **[repo: `build_unsigned_ipa.sh`]**; keep `GusanKeyboardPreview` Simulator‑only and never embedded.
- Sideloadly: app + keyboard + screen + widget = **4 App IDs** of the 10/7‑day budget; 3 side‑loaded apps max; 7‑day expiry — the owner re‑signs weekly (data usually kept with the same Apple ID/bundle id) **[repo: README]**. Re‑signing may rewrite bundle ids and the App Group → every IPC (`VoiceChannel`, `ScreenChannel`, settings, session) must keep the dual‑store fallback; the Live Activity needs no shared container (ActivityKit carries content) and the widget's `Button(intent:)` runs in the app process, so it is unaffected.
- Deployment target: app iOS 17 (Live Activity buttons) with `#available(iOS 18)` around `AudioRecordingIntent`/controls; keyboard and screen extensions stay iOS 16.
- Supabase: functions are deployed manually with the owner's OK (`npx supabase functions deploy <fn> --project-ref hftcelxzfoubheqeoool --no-verify-jwt --use-api`); migrations numbered `2026110700NN_*.sql`; new secrets: `H_AGENTS_WEBHOOK_SECRET` (exists in #18), `GOOGLE_OAUTH_CLIENT_ID/SECRET`, later `APNS_KEY_ID/TEAM_ID/P8`.

### 10.4 Test strategy
- **GusanKit (`swift test`)**: 131 tests on the screen branch + agent tests **[repo]**; add: `StripStateTests` (idle/listening/working/done transitions, 30 s Undo expiry), `VoiceSmartResponse` with `answer`, `CalendarDigest` encoding/size, `VaultMetadata` (no secret ever encoded — a test that `Codable` output of every vault type contains no `password` key), `LiveActivityState` ≤4 KB, `AgentTask` decoding of `awaiting_login/awaiting_code`.
- **Edge functions**: `tests/test_agent_action.py` (45 tests, extracts functions from the TS source because importing starts `Deno.serve`) **[repo]**; add `test_vault_action.py` (release requires `awaiting_login`, body never logged — assert no `console.*` call receives `values`, item/vault deleted on terminal), `test_google_oauth.py` (state binding, scope validation reusing desktop `granted_ok` semantics), `smart-action` knowledge injection. Deno `deno check` in CI for each function.
- **Device checklist** (manual, per phase) in `ios/TESTING.md`: the host‑app matrix (§5.5), the spike list, the sideload re‑sign procedure.
- **Simulator**: keyboard via `GusanKeyboardPreview`; Live Activities render in the Simulator; broadcasts and microphone do not.

### 10.5 Risks register
| # | Risk | Likelihood | Impact | Mitigation / owner |
|---|---|---|---|---|
| R1 | iOS suspends the "ready" app despite the active audio session (S1) | medium | keyboard mic needs the hop more often | keep the engine running; shorten the default window; accept |
| R2 | `AudioRecordingIntent` behaves differently on sideloaded builds or on the owner's iOS version (S4) | low–medium | no record‑from‑anywhere | fall back to Live Activity button (S5) / hop |
| R3 | `LSApplicationWorkspace` stops working on a future iOS | medium | visible return step on sideload | already optional (`autoReturn`) |
| R4 | Broadcast extension killed at 50 MB on older devices | low | screen context unavailable until restart | frame budget ≈15 MB; heartbeat detects |
| R5 | App Group broken after re‑signing | **observed** | IPC via pasteboard only; Full Access required | dual‑store everywhere; diagnostics screen |
| R6 | H ends idle sessions at the concurrency limit (Free plan: 3) | high at scale | "continue" restarts and re‑bills | queue + `idle_timeout_s` tuning; Builder plan (D8) |
| R7 | H reveal endpoint + key leak | low | all persistent vault items exposed | ephemeral default; key hygiene; ask H (D6) |
| R8 | Google OAuth verification / CASA | certain for Gmail | Gmail blocked | calendar first; Gmail later |
| R9 | Gladia batch latency too high for 5 s dictations (S6) | medium | slow Smart | pin a faster FAST engine for short clips; measure |
| R10 | Apple review (App Store track): keyboard + background audio + Live Activity "always listening" perception | medium | rejection | clear in‑app disclosure, the ready window is user‑ended, no audio processed while idle |
| R11 | 4 KB Live Activity state / 8 h cap | low | truncated UI | preview truncation; end on completion |
| R12 | Password AutoFill heuristics don't surface the key icon in our form on some iOS versions | low | manual paste | set `textContentType`, use a `UITextField` not SwiftUI `SecureField` if needed |

---

## 11. Decisions only the owner can make
- **D1** Microphone readiness: accept the orange indicator while Gusan stays ready, and the default window (15 min proposed; options 1 h / until stopped).
- **D2** Gusan Screen idle stop default (10 min proposed) and whether "until I stop" is offered.
- **D3** Agent execute sessions: desktop viewport (default) or a phone‑sized viewport so sites render mobile layouts.
- **D4** Vault v1 = H internal provider via Gusan Cloud with ephemeral per‑session vaults (plaintext transits Gusan Cloud in memory); 1Password as a later opt‑in. Confirm acceptable, or require 1Password‑only (no Gusan transit, but every user needs 1Password).
- **D5** Sites using Sign in with Google/Apple: out of v1, or one‑time manual sign‑in through H's Agent View into the per‑user browser profile.
- **D6** Contact H to ask whether item reveal can be disabled/limited for the organisation; until then, accept that the org key can reveal persistent vault items.
- **D7** Gmail/Contacts: defer (Google restricted‑scope assessment) — confirm.
- **D8** H plan: Free (3 concurrent sessions, 60M tokens) will throttle Agent mode quickly; Builder ($20/mo, 10 concurrent) or Startup.
- **D9** Deployment target iOS 17 for the app (drops iOS 16 users of the app; the keyboard still runs on iOS 16).
- **D10** Paid Apple Developer account timing — it unlocks APNs (push‑to‑start, remote Live Activity updates, remote notifications), TestFlight, App Groups that survive signing, and removes the 7‑day expiry.

## 12. Spikes (device tests needed before committing to a design)
| ID | Question | How |
|---|---|---|
| S1 | Does iOS keep Gusan running with an active `.playAndRecord` session when the engine is **paused**? For how long? | throwaway build; log `UIApplication.backgroundTimeRemaining` and heartbeats to the App Group; check after 30 s, 5 min, 15 min with the screen locked |
| S2 | Battery cost of the ready window with the engine running | 1 h with Settings › Battery before/after |
| S3 | `LSApplicationWorkspace.openApplicationWithBundleID:` on the owner's iOS version, sideloaded | existing `autoReturn` path |
| S4 | `AudioRecordingIntent` from Control Center / Action button / Back Tap on a Sideloadly build: does the app get mic runtime in the background; does the Live Activity requirement behave as documented | minimal intent + activity in the widget extension |
| S5 | Live Activity `Button(intent:)` starting recording (`LiveActivityIntent` + `AudioRecordingIntent`) | same build |
| S6 | Gladia latency for 3 s / 10 s / 60 s takes through `transcribe-proxy` | GusanKit `testLiveVoiceInstruction` with `GUSAN_LIVE_WAV` |
| S7 | Host‑app matrix (§5.5) | manual |
| S8 | Supabase edge‑function logs never contain request bodies for `vault-action` | send a test release with a canary string; search logs via the Management API |
