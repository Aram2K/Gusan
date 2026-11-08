# Gusan iOS — technical specification v2

Status: engineering draft v2 (2026-11-08), incorporating the security + engineering review of v1 (second engineer). Written against the code on `devin/1794056309-ios-keyboard` (PR #13, which already contains PR #17), `devin/1794085675-ios-agent-tasks` (PR #19), `devin/1794090252-ios-screen` (PR #20) and `devin/1794085578-agent-action` (PR #18). Nothing in this document has been implemented; nothing was committed.

How to read the evidence tags used throughout:

| Tag | Meaning |
|---|---|
| **[Apple doc]** | Stated in the cited Apple documentation page (fetched 2026-11-08). |
| **[H doc]** | Stated in the cited H Company page under https://hub.hcompany.ai/agents-api/. |
| **[repo]** | Verified by reading the code at the file/function named. |
| **[device test needed]** / **[API test needed]** | Cannot be verified from the repo or the docs; a numbered spike in §13 covers it. |
| **[unverified]** | Widely reported but not in Apple's reference docs; treat as a hypothesis. |
| **Reviewer disagreed: …** | A point where the v1 author keeps a position the reviewer challenged, with the reason. |

Changes from v1 (summary): App Intents citations corrected (§2.2, §2.4); the Shortcuts/Controls HUD is now stated; session hand‑off hardened (§1.5, no refresh token in the pasteboard, 15‑min lifetime); off‑plan navigation kill switch and Face‑ID‑gated Confirm (§6); vault redesigned around an **empty ephemeral vault bound at `confirm`** with the item added mid‑session (primary, conditional on spike S9), release authorisation, cleanup sweeper, keychain flags, the four assumptions behind "the agent never sees a password" and the honest statements about H's reveal endpoint and Gusan Cloud's transient plaintext (§7); "always ask off / persistent vault" removed from v1; connections schema (§8); logging/redaction/retention/audit checklist (§10.6); P5 re‑estimated 12–14 d, P4 split, P2 conditional on S4 (§10); spikes reduced to S1, S4(+S5), S8, S9, S10 with S2/S3/S6/S7 demoted (§13); owner decisions updated (§11); new §12 implementation plan with tickets.

---

## 0. Executive summary (read this if nothing else)

1. **The strip "on top of any keyboard" is impossible on iOS; the strip inside Gusan's own keyboard is the only legal form.** Nothing can draw over another app's keyboard; custom keyboards get a single view (§1).
2. **The keyboard itself can never record audio.** Apple: *"Custom keyboards, like all app extensions in iOS 8.0, have no access to the device microphone, so dictation input is not possible."* **[Apple doc]** There is no entitlement, WebRTC trick or sideload workaround that changes this (§2.2). Every design must have the *containing app* record.
3. **The best "record anywhere" mechanism that exists today is iOS 18's `AudioRecordingIntent`**: a Control Center control, Action‑button, Back‑Tap Shortcut or Siri trigger launches the Gusan app *in the background*, it records, and it must show a Live Activity while recording — which is the Dynamic‑Island glow the owner asked for. **[Apple doc]** Honest caveats: a brief **system HUD/banner** appears when a Shortcut or Control runs (not a silent start), the remaining gap is that a tap *inside the keyboard* cannot fire an App Intent without opening an app, and **spike S4 is the go/no‑go** for this whole path on a sideloaded build. Recommendation and exact user‑visible behaviour in §2.6.
4. **Live Activities can be started only from the foreground app or from an App Intent (`LiveActivityIntent`, iOS 17); push‑to‑start needs APNs, which a free Apple ID cannot have.** The plan keeps one "Gusan session" Live Activity alive and *updates* it from the background for listening → working → agent progress → result (§3).
5. **Screen context** stays on the already‑built ReplayKit broadcast extension (`GusanScreen/SampleHandler.swift`). "Always on" is not possible; recommend *"starts when first needed, stops after idle"*; screen frames are pinned to one LLM provider (§4).
6. **Vault — the honest statement.** "The agent never sees or knows a password" **holds for the H model under four assumptions (§7.5)**; it does **not** hold for Gusan Cloud (plaintext in memory during one `release` call) nor for H (at rest for the ephemeral vault's lifetime, and **revealable by any member of the H organisation holding the org API key via H's "Reveal an item" endpoint, which cannot be disabled**). End‑to‑end is therefore **not achievable** with H's internal provider; the design minimises exposure: secrets in the iPhone's biometric keychain, released per use into an **empty ephemeral vault bound to the execute session at `confirm`** (item added only when the user approves, no session swap — if spike S9 confirms H picks up items added mid‑session), deleted by a sweeper on every exit path, never persisted or logged by Gusan Cloud. The "always ask off / persistent vault" mode is **removed from v1** (§7).
7. **Security fixes folded in from the review:** the Supabase session (incl. refresh token) must no longer sit in the team pasteboard without expiry (§1.5, lands in P1); an **off‑plan navigation kill switch** in `agent-action sync()` plus plan‑derived `max_steps` (§6.2); Face ID for Confirm of payment/send/delete in the app (§6.3); generic lock‑screen notification bodies (§6.4); `stepText` strips query strings; a logging/redaction/retention/audit checklist (§10.6).
8. **Free Apple ID (Sideloadly) vs App Store** remains the biggest fork: no APNs, 7‑day expiry, 10 App IDs/7 days and 3 side‑loaded apps, and the App Group may not survive re‑signing (observed on the owner's phone **[repo]**). Each section has a "Sideload vs App Store" row.

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

### 1.5 Session hand‑off between app and keyboard (security fix from the review — lands in P1)
Today **[repo: `SessionStore.swift:169`, `SessionBackend.swift:75–100`]** the serialised `Session` (access token, **refresh token**, user id, e‑mail) is written to the "Team pasteboard" backend `UIPasteboard(name: "com.aram2k.gusan.session")` whenever the App Group is unusable and `pasteboardFallback` is on, and `NamedSessionPasteboard` sets an `expirationDate` only when a `lifetime` is given — the session key has none. On a free Apple ID the "team" is the owner's personal team, i.e. every other IPA sideloaded with that Apple ID can read it. Fix:
1. **Never put the refresh token in the pasteboard.** Add `Session.shareable` (access token, `expiresAt`, user id) and write only that to the pasteboard backend; the keychain group and App Group file keep the full session. The keyboard, lacking a refresh token, asks the app to refresh via Darwin notification `gusan.session.refresh.<group>` (the app republishes on `writeRefreshed`); if the app is not alive the strip says "Open Gusan to sign in again".
2. The session pasteboard gets `lifetime: 900` (15 min) and the app re‑publishes it on every foreground, refresh and Darwin request (mirror `ScreenChannel`'s heartbeat).
3. `SettingsStore.pasteboardFallback` defaults **off** when `AppGroupResolver` reports a working group; the toggle stays for the broken case with the diagnostics warning "readable by other apps you install with this Apple ID".
4. Voice pasteboard lifetime 600 s → 120 s (`SessionStore.swift:167`); frames stay at 10 s.
5. GusanKit tests: the pasteboard backend never stores `refreshToken`; every pasteboard write carries an `expirationDate`.

### 1.6 Sideload vs App Store
Identical, except the App Group fallback (pasteboard) matters only on sideload, and TestFlight/App Store would let us drop the `pasteboardFallback` toggle.

### 1.7 Effort
Strip redesign + removal of panel/instruction surfaces: **3–4 engineer‑days**; session hand‑off hardening (§1.5): **1 day** in GusanKit.

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
| C | **App Intents / Shortcuts: `AudioRecordingIntent`** (iOS 18) | "Adopt this protocol to create an app intent for audio recording functionality… In iOS, iPadOS, and watchOS, when you adopt the `AudioRecordingIntent` protocol, you **must start a Live Activity when you begin the audio recording and keep it active as long as you record**. If you don't start a Live Activity, the audio recording stops." **[Apple doc: appintents/audiorecordingintent]** The intent runs **in the app's process in the background** (default `openAppWhenRun = false` on iOS 16–18 — the property is deprecated in favour of `supportedModes = .background`, which exists only from iOS 26 **[Apple doc: appintents/appintent/openappwhenrun; appintents/appintent/supportedmodes — platforms iOS 26.0]**; *Reviewer disagreed: the review asked for `supportedModes` on iOS 18; Apple's reference lists `IntentModes`/`supportedModes` as iOS 26+, so a deployment target of 17/18 keeps the default `openAppWhenRun = false` and adds `supportedModes` under `#available(iOS 26)`*), so a trigger from Shortcuts, the **Action button**, **Back Tap** (Settings › Accessibility › Touch › Back Tap → any Shortcut), a **Control Center control**, a **Lock Screen control** or **Siri** ("Gusan, listen" App Shortcut phrase) starts recording with *no app on screen*. Expect a **brief system HUD/banner** while a Shortcut or Control runs (review B1) — not a silent start. The Live Activity it must start *is* the Dynamic‑Island glow. | **No app; brief HUD** | Works: App Intents and Live Activities need no capability/entitlement (Live Activities need only `NSSupportsLiveActivities` in Info.plist **[Apple doc]**). The widget extension is a 4th App ID. **[device test needed: S4]** | Works | **Primary "anywhere" mechanism.** |
| C′ | Keyboard → Shortcut | A keyboard extension cannot run an App Intent of another process and cannot call `INInteraction`/`AppIntent.perform()` across processes. The only bridge is the `shortcuts://run-shortcut?name=…` URL, which **opens the Shortcuts app** (visible, and not on the App Store without user consent flows). | Yes | — | — | Rejected. |
| D | **Live Activity push‑to‑start** (iOS 17.2) | `Activity.pushToStartToken` **[Apple doc: iOS 17.2+]** lets a server start a Live Activity via APNs. Requires the **Push Notifications capability** — unavailable to personal (free) teams. Also, a push‑started Live Activity does not grant microphone runtime; it would only draw the island. | No | **Impossible** (no APNs) | Possible, but useless for the mic | Not a mic solution; see §3 for its role in Agent progress on App Store builds. |
| D′ | **Interactive Live Activity button** (iOS 17) | Interactive Live Activities are iOS 17+; a `Button(intent:)` runs a **`LiveActivityIntent`** (iOS 17.0) in the app's process: "the system launches your app process without opening the app, performs the intent, and starts the Live Activity… people might place a control in Control Center that performs a LiveActivityIntent" **[Apple doc: appintents/liveactivityintent]**. (`LiveActivityStartingIntent` is a separate protocol listed as iOS 16.1 with no usage text **[Apple doc]** — do not rely on it.) If that intent also conforms to `AudioRecordingIntent`, a single tap on the (expanded) Dynamic Island could start recording. Needs a Live Activity to exist first (8 h max **[Apple doc]**). | No | Works (no APNs needed for *local* activities) | Works | **Secondary mechanism:** a "Gusan ready" Live Activity with a mic button; **[device test needed: S5]**. |
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
        │ App Intent: StartListeningIntent (AudioRecordingIntent & LiveActivityIntent; background: openAppWhenRun default false,
        │                                 supportedModes = .background under #available(iOS 26)); brief system HUD
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
1. Ship the iOS 18 `StartListeningIntent` with a Control Center control (set the control to require device authentication on the Lock Screen **[Apple doc: creating‑controls]**), Action‑button/Back‑Tap setup guide (Home › "Record from anywhere" card with one‑tap "Add to Control Center"/"Open Back Tap settings"), and a Siri phrase. **This is the "anywhere and everywhere" answer.** Deployment target moves to iOS 17 for the app (Live Activity buttons) with iOS 18 checks around the intent; keyboard stays iOS 16‑compatible.
2. Keyboard mic: instant when Gusan is ready; otherwise one activation (short, sideload returns automatically). Default readiness 15 min after any use so the activation is rare.
3. Tell the user the truth in the strip the first time: "iOS lets only the Gusan app hear the microphone. Add the Gusan control to Control Center or the Action button to start listening from anywhere." (One sentence, once.)
4. Owner decision needed: accept the orange indicator during the ready window, the brief HUD on Shortcut/Control triggers, and the default duration (D1).
5. **P2 is conditional on spike S4**: if the sideloaded build gets no background microphone runtime from the intent, the record‑from‑anywhere scope collapses to the Live Activity button (S5) plus the ready window.

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
- **Two frames:** extend `ScreenRequest` with `kind: .start | .finish`. `.start` = frozen pre‑keyboard frame (today's behaviour). `.finish` = at mic‑stop, the newest frame; if the keyboard is still up it is the same frozen frame (skip sending a duplicate: compare `ScreenFrame.hash`), if the user navigated (keyboard dismissed and re‑shown), it is the new pre‑keyboard frame. Both go to `smart-action` as `image_url` data‑URI parts (the function keeps only data URIs **[repo: `onlyDataImages`]**). Two frames double the exposure (review A4c): send the earlier frame **only if the keyboard‑covered region of the latest frame exceeds 50 %**, otherwise one frame; cap 2 × 1 MiB in `agent-action`.
- Consent & indicators: the system picker and the red indicator are the consent; Gusan adds (a) the iPhone icon in the strip being **selected** (intent) vs **live** (red dot prefix on the context chip, already implemented **[repo: `updateContextTitle`]**), (b) a one‑time acknowledgement sheet with version (`ScreenSettings.acknowledged`) **[repo]**.
- The strip icon: replace `eye`/`eye.slash` with an iPhone outline (`iphone` SF Symbol, filled when live) per the owner.

### 4.3 "Always on" vs "starts when first selected, stops after idle"
| | Always on | Start when needed, stop after idle (recommended) |
|---|---|---|
| Feasibility | **Not possible**: no programmatic start, and the red indicator would be permanent; iOS kills long broadcasts under pressure. | Possible today: `ScreenIdleTimer` ends the broadcast after N minutes without a request (default 10) **[repo]**. |
| UX | — | Selected icon + first mic tap with no live broadcast → the strip says "Start Gusan Screen" with **Open** (`gusan://screen` → Home › Gusan Screen → picker). After Start, the user returns to the field; every later use is silent until idle stop. |
| Privacy | — | Red indicator only while live; frames only on request; nothing stored. |

Recommendation: **start when first needed, stop after idle**, with idle options 5 / 10 / 30 min / "until I stop" (D2; choosing "until I stop" re‑asks the acknowledgement). P3 acceptance also checks whether the broadcast survives screen lock and incoming calls on the owner's iOS (review). The icon's *selected* state persists in `ScreenSettings`; *live* is read from the heartbeat.

### 4.4 Privacy
Screen requests are **pinned to one LLM provider** with zero‑data‑retention terms (`smart-action` route override for requests carrying images; provider named in the "What is shared" card — D‑new3) instead of whichever route is active. The `pasteboardFallback` toggle is also shown on the Screen page. Sent to Gusan Cloud only on a request: ≤2 JPEGs (long edge 1280, ≤450 KB each), the instruction/transcript, the field text iOS exposes, and the session token. Never audio, never continuous frames. The pasteboard copy of a frame is readable by apps signed by the same team for ≤10 s — acceptable on sideload; on App Store the App Group works and the pasteboard store can be disabled. Secure fields: the keyboard is not shown at all, so no frame can be requested there; but ReplayKit does not blank other apps' secure content — the acknowledgement sheet says so **[repo: DESIGN.md §9.5]**.

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
- Providers (privacy text, D‑new3): Gladia first; fallbacks by route Mistral Voxtral / ElevenLabs Scribe / OpenAI / Gemini **[repo: `CATALOG`, `FAST_ORDER`]**; provider error `detail` reduced to status + `error.type/code` (§10.6).
- Latency budget: 5‑second take ≈ 1.5–3 s with Gladia batch **[unverified until measured; S6, run from CI]**. Streaming STT would need a WebSocket from the app, out of v1.

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
| Telegram, Slack, Teams | custom text views; context may be partial | same insert‑only fallback as Gmail when context is empty |
| All hosts | `documentContextBeforeInput` may return only a **partial** context (sentence/paragraph) — documented behaviour | the Undo validator (`ReplacementUndo.step`) already matches by suffix (`expectedBefore.hasSuffix(context.before)`) **[repo]**; keep tolerating truncation in tests |
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
- **Prepare/confirm/execute gate**: `do` tasks first run a *text‑mode, read‑only* session (`mode: {type: "text"}` — H's text mode "cannot click or type" **[H doc: browser/configuration]**, 300 s / 60 steps) whose `answer_format` is a plan schema; the task becomes `awaiting_confirmation` for 15 min; `confirm` re‑reads the server‑stored plan (never a client‑supplied one) and creates a **new visual session** (600 s / 120 steps) with `executePrompt(plan)` and idempotency key `${task.id}:execute`; `cancel` ends it; `expire()` is authoritative even if H is down. Research runs text‑mode only. Custom tool calls are currently refused with an `error_event` in `sync()`.
- iOS: `AgentService` protocol, `AgentClient` (live), `AgentJob: GusanJob` (polls, emits `.progress`/`.needsConfirmation`/`.result`), `AgentIntent.detect` (deterministic verb lists), `AgentCenter` (polling, local notifications with `.authenticationRequired` actions, BGAppRefresh `com.aram2k.gusan.agent-refresh`), `AgentKeyboardController` (card in the keyboard; Confirm armed after `confirmArmDelay` 0.7 s, never in Cancel's slot), `ActivityView`.

### 6.2 Target state machine (server, `agent_tasks.status`)
```
queued ─► running(prepare) ─► awaiting_confirmation ─► running(execute) ─► completed | failed | timed_out
   │            │                   │ (15 min)                 │
   │            │                   └─► cancelled               ├─► awaiting_login ─► running(execute, same session) …
   │            │                                               ├─► awaiting_code  ─► running …
   │            │                                               └─► failed(off_plan_navigation)
   │            └─► completed (research) ─► idle ─► running (continue) …
   └─► cancelled
```
Additions over PR #18:
- **Custom‑tool path (execute sessions only).** Declare `request_login(hostname)` and `request_otp(source, prompt)` in `agent.tools` at session create (**[H doc: custom-tools — "Declare the tools at session create… inline on the agent or via the `agent.tools` override"]**). In `sync()`, on an `ActiveStateChangeEvent` with `state = awaiting_tool_results`, validate every pending call: `tool_name` in the allowlist; `args.hostname` a valid hostname that **matches a `vault_logins.hostname` of this user or the plan's `start_url` host**; otherwise post an `error_event` tool result ("no login for that site") and continue. Store the call in `agent_task_runs.tool_req` (never expose `call.id`), set `status = awaiting_login` / `awaiting_code`, and emit the `release_nonce` (§7.4). Tool results are **at‑least‑once** **[H doc]** → key the parked call by `(task_id, hostname)` so a duplicate delivery resolves to the same result. Deadline 15 min → `login_expired` / `otp_timeout`; the task is cancelled and the vault deleted (§7.4).
- **Off‑plan navigation kill switch (structural, review A3.1).** Execute is bounded by prompt only today. In `sync()`, for `run.phase === "execute"`, derive an allowlist of hostnames from `plan.start_url`, every URL in `plan.steps`, and the login hostnames of this task; for each `policy_event` whose `tool_reqs[].tool_name === "go_to_web"`, compare `new URL(args.url).hostname` by suffix match; on mismatch → `DELETE /sessions/{id}` (**[H doc: sessions/cancel]**), `status = failed`, `error_code = off_plan_navigation`, delete the ephemeral vault, write a `security_events` row. Also lower execute `max_steps` to `min(120, 15 + 10 × plan.steps.length)`.
- **`stepText` stores full URLs** (`Opening ${call.args.url}`) into `agent_tasks.steps` **[repo: `stepText`, line ~479]** → strip `search` and `hash` (`origin + pathname`), and store no steps while a `request_login` call is parked.
- **Body read before auth** **[repo: `Deno.serve` reads up to 8 MiB before the Bearer check]** → check the `Authorization` header shape and pick the webhook branch *before* `req.body.getReader()`; keep the cap. Image cap for the iPhone: 2 × 1 MiB (frames are ≤450 KB) instead of 4 × 6 MiB.
- `idle` (research results continuable): create research sessions with `idle_timeout_s` (e.g. 600) **[H doc: sessions/create]**; H ends sessions idle ≥120 s at the concurrency limit (Free plan: 3) **[H doc: plans‑and‑limits]** → a `continue` after that `send_messages` to a finished session, which *restarts* it at `pending` and is billed again **[H doc]**. New actions: `continue {task_id, message}`, `stop {task_id}` (`DELETE /sessions/{id}` → `interrupted`, resumable by a message), `chat {task_id}` (user/agent message events from `GET /sessions/{id}/events`, never tool payloads).
- **Memory: removed from the v1 scope** (review B2.10: undefined). If reinstated later: `agent_memory (user_id, kind, text, approved_at)`, user‑approved only, sent as a per‑session inline **skill** **[H doc: send‑messages `skills`]**, never as instruction text.
- Retention: null `agent_task_runs.plan` and `tool_req` when a task becomes terminal; pg_cron deletes `agent_tasks` older than 30 days; H sessions `delete_after_min: 1440`, screenshots 60 — and **60 / 10 min for sessions that performed a login release**, with `overrides["agent.environments[kind=web].mode"] = {type: "visual", markdown: false}` so page text does not also land in H storage.
- Mobile‑only: H has no mobile‑emulation flag **[H doc: browser/configuration — viewport `width`/`height` only]**; "mobile‑only" is a *Gusan* scope statement. Optionally `mode: {type:"visual", width: 430, height: 932}` for execute sessions (D3).
- Irreversible‑action confirm expiry stays 15 min; "Confirm never where Cancel was": fixed slots in the strip card and the Live Activity.

### 6.3 Client flow
Strip (Agent mode) → mic → transcript → `agent-action start {instruction, mode: AgentIntent, context: selected text (+ ≤2 screen frames), timezone}` → 3 s card ("Understood: …") → collapse. Progress: Live Activity (§3) if present, else Sessions tab; polling by the app (`AgentCenter`) while alive. **Confirm:** for `action_kind ∈ {purchase, send, delete, account_change}` the keyboard card and the Live Activity show **"Confirm in Gusan"** (deep link) and the app gates Confirm behind `LAContext.evaluatePolicy(.deviceOwnerAuthentication)`; for `submit`/`other` the keyboard card's Confirm stays (armed after 0.7 s, never in Cancel's slot **[repo]**). Login: §7. Result: Live Activity "Copy / Open", Sessions › chat view with Continue, Stop.

### 6.4 Notifications
| | Sideload (free Apple ID) | App Store |
|---|---|---|
| Remote push | **Not available** (Push Notifications capability requires a paid team; `aps-environment` comes from the provisioning profile **[Apple doc]**) | APNs token‑based auth (.p8) from a new edge function `push-notify` called by the `agent-action` webhook path; device tokens in `agent_devices` |
| Local notifications | `UNUserNotificationCenter` from the app process — only while the app runs. `AgentCenter.deliver` exists **[repo]** | same, plus remote |
| Live Activity updates | from the app process only | ActivityKit push (`liveactivity` push type) |
| Content (review A7) | **Generic bodies:** "Gusan needs your OK — open to review" (never the plan summary, which today is `content.body = pending.confirmation?.summary` **[repo: `AgentCenter.swift:207`]**); `subtitle = task.title` for research only; `interruptionLevel = .timeSensitive` for confirmations | same |
| Practical consequence | Keep the app alive during tasks (the §2 ready window is extended until the task is terminal) | works even if the app is killed |

### 6.5 Failure modes (server codes exist **[repo]**): `agent_not_configured`, `agent_busy` (H 429 → 503), `too_many_active`, `quota_exceeded`, `plan_missing`, `confirmation_expired`, `upstream_error`, deadlines +180 s grace. New: `login_expired`, `otp_timeout`, `off_plan_navigation`, `vault_error` (vault create/delete failed; task fails closed before any release).

### 6.6 Effort
Server: custom‑tool plumbing (`agent.tools`, parked calls, `awaiting_login/awaiting_code`, idempotent results) **2 days**; kill switch + `max_steps` + `stepText`/auth‑order/image‑cap fixes **1 day**; `continue/stop/chat` + idle sessions **2 days**; APNs (App Store only) **2 days**. iOS: Live Activity progress + Face‑ID Confirm **(P4a, 4 days)**; Sessions tab chat view, continue/stop **(P4b, 3 days)**.

---

## 7. Vault: logins the agent never sees

### 7.1 What iOS allows
- **There is no API to read or enumerate the user's iCloud Keychain passwords.** `ASAuthorizationPasswordProvider` returns credentials *only for the app's own associated domains* ("Password AutoFill recommends credentials only for your app's associated domain, and the user must authenticate using Face ID or Touch ID") **[Apple doc: security/password‑autofill; authenticationservices/asauthorizationpasswordprovider]**. Associated domains also require a paid team.
- **What does work:** a form with `UITextField`s of `textContentType = .username / .password`. iOS shows the Password AutoFill QuickType bar; the user taps the key icon → **Face ID** → picks *any* saved login (iCloud Keychain or a third‑party credential provider) → iOS types username and password into the fields **[Apple doc: Password AutoFill "heuristics… even if those apps haven't been updated"]**. The app then holds the plaintext in memory — unavoidable on iOS. Consequence for the brief: "connect iPhone Passwords" is really **"import a copy of this login"** — Gusan keeps its own copy and a password changed later in iCloud Keychain does not propagate; the UI must say so. Whether the key icon appears for a SwiftUI `SecureField` vs a `UITextField` on the owner's iOS is **spike S10**.
- Face ID and storage: keychain items created with `kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly` (guarantees a passcode exists; items are erased if the passcode is removed) and `SecAccessControlCreateWithFlags(.biometryCurrentSet)` (the *read* requires Face ID and is invalidated if biometrics are re‑enrolled — tell the user) **[Apple doc: Security › keychain accessibility; LocalAuthentication]**; one shared `LAContext` passed via `kSecUseAuthenticationContext` so Face ID is asked once per release, not once per field. **Invariant:** vault items have **no `kSecAttrAccessGroup`**, are never in the App Group file and never in any pasteboard; the keyboard extension cannot reach them (`KeychainSessionBackend` only stores the Supabase session **[repo]**). A GusanKit test asserts `VaultStore` writes carry no access group and that `SessionStore` backends never see `Gusan.vault.*` services.

### 7.2 What H allows **[H doc: vaults/overview, create‑item, update‑item, reveal‑item, browser/configuration, custom‑tools]**
- Vaults: `provider: internal` (H‑hosted) or `onepassword` (service‑account token, write‑only, "never returned by any endpoint"). Items: `urls` (1–20), `values` of `username|password|email|totp|api_key` (≤4096 chars); **create and update both say "values travel in clear text inside the body… never log the request"**; 422 never echoes values. A vault is bound to a *web environment at session creation* via `vault_id` (inline `agent.environments` or `overrides["agent.environments[kind=web].vault_id"]`); no endpoint mutates a running session's environments; "Cloud browsers only". `fill_secret_at` exists **only in `visual` mode** (text mode "cannot click or type") — the execute session is visual **[repo: `sessionRequest`]**. Matching is by page hostname (equal or subdomain), closest host wins, ties → most recently created item; with one item per ephemeral vault there is no tie.
- **Reveal an item** returns `values` in clear text (never the TOTP seed, but its current code) to **any member of the organisation**; audit‑logged by H; `Cache-Control: no-store`. **No documented way to disable it, scope it per key, or restrict it per vault.** The overview's "No endpoint returns secrets" is inaccurate for H‑hosted vaults. Treat `H_company_computer_agents` as the crown jewel: keep the H org at **one member**, rotate quarterly and on any suspicion (D6, D‑new2).
- Limits: 1000 items per organisation; cloud environment starts are rate‑limited per user **[H doc: plans‑and‑limits]**. Custom tools: declared at session create; the session goes `awaiting_tool_results`; results are posted by our code; **at‑least‑once**; H does not validate our result against the pending call **[H doc: custom‑tools]**.

### 7.3 Is end‑to‑end (no plaintext at Gusan Cloud) achievable?
**No**, not with the `internal` provider: the item must be created by a server‑held H key with plaintext values in the request body; the phone cannot call H directly without exposing that key. Two mitigated designs:

| | A. Internal provider via Gusan Cloud (v1) | B. 1Password provider (later option) |
|---|---|---|
| Plaintext at Gusan Cloud | **Transient, in memory of one edge‑function invocation**, never stored, never logged | **Never** — the user saves logins in 1Password; H fetches them with the user's service‑account token |
| Plaintext at H | yes (H‑hosted vault, revealable with the org key) | H decrypts on fill; token "validated against 1Password, write‑only, never returned" |
| User prerequisite | none | 1Password subscription + a service account (power‑user step) |
| Lifetime at H | minutes (ephemeral vault per task) | n/a |

Recommendation: **A with ephemeral per‑task vaults for v1; B as an opt‑in Connection later** (D4).

### 7.4 Chosen mechanism (A): empty ephemeral vault bound at `confirm`, item added on approval
**Primary design (adopted, conditional on spike S9).** H's docs bind the *vault* to the environment at session creation; nothing states that items are snapshotted at that moment, and matching is described as "the page URL picks one item" at fill time **[H doc: vaults/overview — "How secrets are matched"]**, so adding an item to an already‑bound vault should take effect at the agent's next step. The docs are silent either way **[API test needed: S9, ~1 h, no device]**. If S9 passes, the design is:

```
Add login (Vault tab)                                  Use login (agent needs it)
───────────────────                                    ─────────────────────────
Site, Username, Password[, TOTP] fields                confirm → vault-action creates EMPTY vault "gusan-task-<task_id>"
 └ key icon → Face ID → iOS Password AutoFill             → execute session created with vault_id = that vault,
Save → biometric keychain write (§7.1 flags)                tools = [request_login, request_otp]; h_vault_id saved on agent_task_runs
 └ POST vault-action {add, hostname, username_hint}    agent reaches a login page → request_login(hostname)
   (metadata only; never the username)                  └ agent-action: validate hostname (§6.2) → status = awaiting_login,
                                                           release_nonce (uuid, 15 min) stored on agent_task_runs,
                                                           returned ONLY in the status response to the owner
                                                       app (or strip → deep link): "example.com asks you to sign in ·
                                                         agent is on https://example.com/login" (current page URL from /changes;
                                                         deny on hostname mismatch) → Face ID (one LAContext)
                                                       app reads the keychain item → POST vault-action
                                                         {release, task_id, login_id, release_nonce, values}
                                                       vault-action: authorise (below) → POST /vaults/{h_vault_id}/items
                                                         {urls:[https://<hostname>], values} → post tool result "login available"
                                                         → status = running; nothing else changes, same session, same page
                                                       agent sees fill_secret_at on the matching page only; never the value
                                                       task terminal (any path) → DELETE vault (cascades items); audit rows
```
No session swap, no browser profile, no "resume from step k", one fewer cloud‑browser start per login. The same ephemeral vault accepts a second `request_login` for a different hostname of the same task (another item, another Face ID).

**Fallback if S9 fails (v1's session swap, kept for the record).** Cancel the execute session, then create a new one with `vault_id` and the user's **per‑user browser profile** (`browser_profile_id` + `persist_browser_profile: true`) so cookies carry over, and a prompt that *restates the goal* (not "step k"). `persist_browser_profile` saves state **only when the old session ends** **[H doc: browser/profiles]**, so the cancel must complete before the create (serialise; expect several seconds), and the profile has a one‑writer lock. Cost: +1–3 days and a per‑user profile to manage/delete. Why we do not prefer it: more moving parts, a second environment start per login, and a long‑lived per‑user cookie jar at H.

**`vault-action release` authorisation (review G3):** (a) `agent_tasks.status = 'awaiting_login'` **and** `agent_task_runs.tool_req.hostname` equals `vault_logins.hostname` for a `login_id` owned by `auth.uid()`; (b) one‑time `release_nonce` consumed with compare‑and‑set; (c) ≤3 releases per task (`agent_task_runs.release_count`); (d) body cap 64 KB, `Content-Type: application/json` only; (e) `values` validated to the five field names, ≤4096 chars each; (f) the body object is never passed to a logger, `Failure`, or thrown error — catch blocks log a code only; a CI test greps `vault-action/index.ts` for `console.*(body|values|raw)` and fails on a match.

**Cleanup on every path (review G2):** vaults are named `gusan-task-<task_id>`; `h_vault_id` lives on `agent_task_runs`; delete in `expire()`, `cancel`, `off_plan_navigation`, and in `sync()` when the status becomes terminal; a scheduled sweeper (pg_cron → edge function, every 10 min) lists `GET /vaults`, deletes any `gusan-task-*` whose task is terminal or older than `max_time_s + 900 s`, and logs `vault_deleted`/`vault_delete_failed` + task id only. If vault creation fails at `confirm`, the task **fails closed** (`vault_error`) before any release.

**MFA/TOTP:** the Add‑login screen accepts an `otpauth://` URI (camera QR scan or paste) → stored in the keychain item and sent with the release; H generates codes; the agent types them via `fill_secret_at` without seeing the seed **[H doc]**. SMS/e‑mail codes: `request_otp` → `awaiting_code` → the user types the digits in the strip card or app → posted as the tool result; single‑use, never stored. Sign in with Google/Apple inside third‑party sites: **not supported in v1** (D5).

**"Always ask before using a login" is always on in v1.** The v1 "off = persistent per‑user H vault" mode is removed (review G6): it was the only asset H's reveal endpoint would expose for longer than minutes. If shipped later (D‑new1): one vault per user named by a hash of `user_id`, items deleted on login/account deletion, shown in the Vault tab as "stored at H (revealable by Gusan operators)".

Keyboard out of the loop: the strip only shows "Login needed · Open Gusan"; Face ID, keychain reads and the release happen in the app.

### 7.5 Does "the agent never sees or knows a password" hold?
**Yes for the H model, under four assumptions** (review A2.3):
1. H implements `fill_secret_at` as documented — the value is typed by the browser runtime, outside the model context. Not independently verifiable; H says so in writing **[H doc]**.
2. The target field masks its value. `fill_secret_at` types at coordinates into *whatever* input is there; if the agent picks a `type=text` field (phishing page, "show password" toggled, a search box), the next screenshot shows the password and the model sees it. Domain gating bounds this to the user's hostname and its subdomains, not to the password field. **Residual; Gusan cannot remove it.**
3. The page does not echo the credential.
4. The task never asks the user to type the password anywhere the model reads (strip, chat, OTP card). `instructions()` already forbids asking for credentials **[repo]**; enforce in UI copy.

**Who does know it:** the phone (biometric keychain); Gusan Cloud **in memory during `release`**; H **at rest for the ephemeral vault's lifetime** (minutes), **revealable by any H‑org member with the org key**, and H staff per their own controls. Username/e‑mail is visible to the model by design — treat it as PII (hence `username_hint` only in our DB), not a secret.

| Party | Sees plaintext? | How it is prevented / bounded |
|---|---|---|
| H agent (model) | **No** (assumptions 1–4) | `fill_secret_at`; domain gating; instructions |
| Gusan DB | No | no secret columns; `vault_logins` holds `hostname`, `username_hint`, `has_totp`; owner RLS |
| Edge‑function logs | No | `vault-action` never logs bodies (CI test); Supabase platform logs record metadata only — **S8 verifies** that `Authorization` and query strings are absent |
| Prompts / chat / notifications / Live Activity | No | task object exposes `hostname` + `username_hint`; `agent_task_runs` is service‑role only; steps are not recorded while a login call is parked |
| Clients (keyboard, widget) | No | only the app process reads the biometric keychain items; no access group |
| Gusan Cloud (transient) | **Yes, in memory during `release`** | no persistence, no logging, immediate forward to H, release authorisation (§7.4) |
| H (at rest) | **Yes, minutes** | ephemeral vault deleted on every exit path + sweeper; **reveal endpoint exists and cannot be disabled** — one‑member org, key rotation, ask H (D6) |

Residual risks, ranked: (1) reveal endpoint + leaked org key or a second org member; (2) an H insider reading post‑login screenshots (balances, messages) — bounded by `delete_screenshot_after_min: 10` after a release; (3) visible‑field echo (assumption 2); (4) an ephemeral vault surviving a failure path — closed by the sweeper; (5) a Gusan Cloud compromise exfiltrating during `release` — bounded by the small reviewed function, the nonce and the rate limit.

Audit log (append‑only, no values, no username, no H ids in clear): `vault_events (id, user_id, login_id nullable, task_id, event ∈ added|release_requested|released|denied|expired|deleted|vault_created|vault_deleted|vault_delete_failed, hostname, at)` with owner `select` RLS, `insert` for `service_role` only (same pattern as `agent_task_starts` **[repo]**); shown in the Vault tab ("Used for booking.com · yesterday 14:02"). `security_events` mirrors it for auth failures, off‑plan navigation and denied releases. Deletion: Vault › Delete removes the keychain item and the `vault_logins` row; account deletion (`delete-account`) also deletes any `gusan-task-*` vault still present, `connections`/`connection_secrets` (with Google revoke), `agent_tasks`, and the client clears its keychain items.

### 7.6 Threat model (abridged)
- Stolen H API key → reveal of in‑flight ephemeral items (minutes) and reading of every session's screenshots/events. Mitigation: ephemeral only, one‑member org, key in Supabase secrets, quarterly rotation.
- Stolen Supabase JWT (see §1.5) + a task in `awaiting_login` → cannot choose the hostname (it comes from the agent's tool call) and needs the `release_nonce` from the owner's `status` response plus the keychain value — an attacker without the phone cannot release.
- Compromised Gusan Cloud code → exfiltration during `release`. Mitigation: `vault-action` is small and separately reviewed; nonce; ≤3 releases/task; CI never‑log test.
- Phishing by the agent → domain gating; the approval screen shows the agent's current page URL next to the requested hostname and denies on mismatch (review G7b).
- Device compromise / shoulder surfing → biometric keychain, `WhenPasscodeSetThisDeviceOnly`, no sync, no backup.
- Prompt injection from pages → existing guards; off‑plan kill switch (§6.2); item URLs are set by the user, not the agent.

### 7.7 Sideload vs App Store
Identical: the biometric keychain and `LocalAuthentication` need no entitlement; vault items deliberately use the app's **own** keychain (no shared group, so re‑signing cannot break or widen it). Password AutoFill's key icon works in any app (S10 confirms the form type).

### 7.8 Effort (re‑estimated with the review): **12–14 days** — custom tools (2 d, in §6), `vault-action` add/release/delete with nonce, cleanup sweeper and tests (3 d), vault binding (1 d if S9 passes; 3 d for the swap fallback), iOS Vault tab (AutoFill form, QR TOTP, Face ID, audit UI, 4 d), review + S8 (1 d). S9 first saves 2–3 days.

---

## 8. Connections: Google Calendar and iPhone Calendar

### 8.1 Google Calendar
- OAuth must end **server‑side** (tokens never on the phone): new edge function `google-oauth` (`start` → 302 to `accounts.google.com/o/oauth2/v2/auth` with PKCE + `state` bound to the user; `callback` exchanges the code with a **Web** client id/secret held in Supabase secrets; stores the refresh token **encrypted** with Supabase Vault/pgsodium). Schema (review A6): `connections (user_id, provider, email, scopes, status, created_at)` with owner `select` RLS; **`connection_secrets (user_id, provider, enc_refresh_token, access_token, access_expires_at)`** and **`oauth_states (user_id, state, code_verifier, expires_at)`** with `revoke all … from anon, authenticated` (service‑role only, the `agent_task_runs` pattern); `state` is a random uuid, single‑use, 10 min; the access token is cached ≤55 min to avoid a refresh per `smart-action` call; disconnect calls `https://oauth2.googleapis.com/revoke` then deletes the rows; S8 checks that the `callback?code=` query string is not retained in edge logs. The app opens the start URL in `ASWebAuthenticationSession` with callback `gusan://connections/google` (the function's callback redirects there with a one‑time code; the token itself stays on the server). The desktop already implements the Google dance with the narrowest scope ("events on calendars you own") and a fallback **[repo: `calendar_sync.py SCOPES/SCOPE_OWNED/EVENT_SCOPES`]** — reuse the same scopes and the same "granted_ok" validation.
- Scopes v1 (read‑only): `https://www.googleapis.com/auth/calendar.events.owned.readonly` (the desktop's `SCOPE_OWNED` **[repo: `calendar_sync.py:75`]**) + `openid email`. Later write: `calendar.events` behind the Agent confirm gate.
- Google verification: calendar scopes are *sensitive* (not restricted) → app verification required before >100 users; Gmail scopes are **restricted** (CASA assessment) → defer Gmail (D7).

### 8.2 iPhone Calendar (EventKit)
- iOS 17: `requestFullAccessToEvents` or `requestWriteOnlyAccessToEvents`; **"Your app can't request read‑only access… To read events… your app needs full access"** **[Apple doc: eventkit/accessing‑the‑event‑store]**; `NSCalendarsFullAccessUsageDescription` required. So "read‑only first" is a *Gusan* promise (we never call save), not an iOS permission level — say so in the consent copy.
- The keyboard must not prompt for EventKit; the **app** reads events on foreground, on `EKEventStoreChanged`, and in BGAppRefresh, and writes a compact 14‑day digest (`[{start,end,title?,allDay,calendar}]`, ≤8 KB, **titles off by default**) to the App Group (`CalendarDigest.json`) with the same dual‑store fallback as settings **[repo: `SettingsStore` pattern]**. The keyboard attaches the digest to Smart requests when the Calendar connection is on.
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

## 10. Build plan, CI/signing, tests, logging, risks

### 10.1 Landing order for the open PRs
1. Merge **#13** (keyboard + voice) and **#18** (agent‑action) to `main` — they are the base of everything.
2. Rebase **#19** (iOS agent) and **#20** (Screen) onto `main`; merge.
3. Do the §9 removal (`privacyMode` touches `SessionStore` and all four branches) **only after all four are merged**.
4. New work in phases below, each as one PR on top.

### 10.2 Phases (the owner tests on his iPhone after each)
| Phase | Scope | Gate | Effort |
|---|---|---|---|
| P0 | Spikes S1, S4(+S5), S8, S9, S10 (throwaway branch / API scripts) | — | 1.5–2 d |
| P1 | Strip redesign + Smart‑only pipeline + 30 s Undo + Privacy‑Mode/dead‑code removal (§1, §5, §9) **+ session hand‑off hardening (§1.5) + the one‑line server fixes (`stepText`, auth‑before‑body, image cap)** | — | 7.5 d |
| P2 | Record‑from‑anywhere: `StartListeningIntent`, Control Center control, Live Activity (listening/done) (§2, §3) | **Conditional on S4.** If S4 fails, P2 collapses to the Live Activity button (S5) + ready window (≈3 d) | 8 d |
| P3 | Screen two‑frame + iPhone icon + idle options + provider pinning (§4) | — | 2 d |
| P4a | Agent: Live Activity progress, Face‑ID Confirm, confirm card/activity, off‑plan kill switch (§3, §6) | — | 4 d |
| P4b | Sessions tab chat, continue/stop, idle sessions (§6) | — | 3 d |
| P5 | Vault + custom tools + `awaiting_login`/OTP (§7) | **S9 and S10 first** | 12–14 d |
| P6 | Connections: Google Calendar, iPhone Calendar (§8) | — | 5 d |
| P7 (App Store track, when a paid team exists) | APNs: push‑to‑start, remote Live Activity updates, remote notifications; drop private API and pasteboard fallbacks | paid team | 5 d |

### 10.3 CI and signing (sideload)
- `.github/workflows/ios.yml` runs GusanKit `swift test`, prompt parity, a Simulator build and `scripts/build_unsigned_ipa.sh` on `macos-15` **[repo]**. Add: the widget extension to the `codesign --sign -` loop and PlistBuddy checks (`NSSupportsLiveActivities`, `NSExtensionPointIdentifier = com.apple.widgetkit-extension`); `GUSAN_SIDELOAD` stays the only place private API compiles **[repo: `build_unsigned_ipa.sh`]**; `GusanKeyboardPreview` stays Simulator‑only; Debug builds log `os_proc_available_memory()` in the keyboard like `SampleHandler` does.
- Sideloadly: app + keyboard + screen + widget = **4 App IDs** of the 10/7‑day budget; 3 side‑loaded apps max; 7‑day expiry **[repo: README]**. Re‑signing may rewrite bundle ids and the App Group → every IPC keeps the dual‑store fallback (with the §1.5 limits); the Live Activity needs no shared container and `Button(intent:)` runs in the app process.
- Deployment target: app iOS 17 (Live Activity buttons, `LiveActivityIntent`) with `#available(iOS 18)` around `AudioRecordingIntent`/controls; keyboard and screen extensions stay iOS 16.
- Supabase: functions deployed manually with the owner's OK; migrations `2026110700NN_*.sql`; new secrets `GOOGLE_OAUTH_CLIENT_ID/SECRET`, later `APNS_KEY_ID/TEAM_ID/P8`; a `SECRETS.md` runbook (each secret: where it lives, who can read it, last rotation).

### 10.4 Test strategy
- **GusanKit (`swift test`)**: 131 tests today **[repo]**; add `StripStateTests`, `VoiceSmartResponse` with `answer`, `CalendarDigest` size/encoding, **`VaultStoreTests`** (no access group; `SessionStore` backends never see `Gusan.vault.*`; `Codable` output of every vault type contains no `password` key), **`SessionPasteboardTests`** (never stores `refreshToken`; every write has `expirationDate`), `LiveActivityState` ≤4 KB, `AgentTask` decoding of `awaiting_login/awaiting_code`.
- **Edge functions**: `tests/test_agent_action.py` (45 tests, source‑extraction style **[repo]**); add `test_vault_action.py` (release requires `awaiting_login` + hostname match + nonce; ≤3 releases; vault deleted on every terminal path; **never‑log grep**), `test_google_oauth.py` (single‑use `state`, scope validation reusing the desktop's `granted_ok` semantics), `smart-action` knowledge injection, `stepText` strips query strings, **401 without a token for every function**, `vault_events` insert‑only for `service_role`. `deno check` per function in CI.
- **Device checklist** in `ios/TESTING.md`: host‑app matrix (§5.5, incl. Messages, Safari URL bar, Telegram, Slack, Teams), spike list, sideload re‑sign procedure.

### 10.5 Risks register
| # | Risk | Likelihood | Impact | Mitigation / owner |
|---|---|---|---|---|
| R1 | iOS suspends the "ready" app despite the active audio session (S1) | medium | keyboard mic needs the hop more often | keep the engine running; shorter default window; accept |
| R2 | `AudioRecordingIntent` gives no mic runtime on the sideloaded build (S4) | low–medium | no record‑from‑anywhere; P2 shrinks | Live Activity button (S5) / hop; budget P2 after S4 |
| R3 | `LSApplicationWorkspace` stops working on a future iOS | medium | visible return step on sideload | already optional (`autoReturn`) |
| R4 | Broadcast extension killed at ~50 MB; broadcast does not survive lock/calls | low | screen context unavailable until restart | frame budget ≈15 MB; heartbeat detects; check in P3 acceptance |
| R5 | App Group broken after re‑signing | **observed** | IPC via pasteboard only; Full Access required | dual‑store everywhere; §1.5 limits; diagnostics |
| R6 | H ends idle sessions at the concurrency limit (Free: 3) | high at ≥2 users | "continue" restarts and re‑bills | Builder plan when ≥2 users (D8) |
| R7 | H reveal endpoint + key leak / second org member | low | in‑flight vault items + all screenshots | ephemeral only; one‑member org; rotation; ask H (D6) |
| R8 | Google restricted‑scope assessment (Gmail) | certain for Gmail | Gmail blocked | calendar first (sensitive scopes need verification only >100 users) |
| R9 | Gladia batch latency too high for short dictations | medium | slow Smart | measure from CI (S6 demoted); pin a faster FAST engine for short clips |
| R10 | App Store review of keyboard + background audio + Live Activity | medium | rejection | disclosure; user‑ended ready window; no audio processed while idle |
| R11 | 4 KB Live Activity state / 8 h cap | low | truncated UI | preview truncation; end on completion |
| R12 | Password AutoFill key icon absent in our form | low | manual paste | `UITextField` + `textContentType`; S10 |
| R13 | Session token (incl. refresh token) in the team pasteboard on sideload | **present today** | account takeover by another sideloaded app | closed by §1.5 in P1 |
| R14 | Ephemeral vault leaks on a failure path | medium without fix | password at H for the retention period | closed by the sweeper + delete‑on‑terminal (§7.4) |
| R15 | Off‑plan navigation in execute | medium | agent acts on a site outside the plan | kill switch + plan‑derived `max_steps` (§6.2) |

### 10.6 Logging, redaction, retention, audit — checklist (from the review, adopted)
- **Never log:** `Authorization`/JWT, refresh tokens, `apikey`, the H key, webhook secret, OAuth `code`/tokens, `vault-action` bodies, audio, transcripts, instructions, plans, page text/`steps`, screen frames or any data URL, e‑mail addresses, H `agent_view_url`.
- **Redaction:** one `_shared/log.ts` `log(event, fields)` that allowlists field names, truncates strings to 120 chars, scrubs known secrets (move `scrub()` from `smart-action`); ban raw `console.*` in functions via CI grep; catch blocks log a `code`, never upstream `e.message`; `transcribe-proxy` error `detail` reduced to status + `error.type/code` (provider moderation messages can contain transcript text).
- **Platform logs:** Supabase edge logs keep method/path/status/header metadata — **S8 confirms** `Authorization` and query strings (`callback?code=`) are not retained; every function keeps `auth.getUser()` and gets a 401‑without‑token test.
- **Retention:** `agent_tasks` 30 d (pg_cron); `agent_task_runs.plan/tool_req` nulled at terminal; `vault_events` 1 y; H sessions 1440 min / screenshots 60 min, **60 / 10 after a login release**; STT/LLM providers: zero‑retention terms where available; screen frames pinned to one provider (§4.4).
- **Key rotation:** H key quarterly + on incident (`supabase secrets set`, redeploy); H webhook secret via H `rotate` (dual‑verify window) together with the key; Google client secret yearly; service‑role/JWT secret on incident only.
- **Audit tables:** `vault_events` and `security_events` as in §7.5 — append‑only for `service_role`, owner `select`, no values, no username, `sha256(h_vault_id)` if correlation is needed.
- **Supabase RPC audit:** confirm `is_pro` and `use_smart_action_quota` are `security definer` keyed on `auth.uid()` (not in the reviewed migrations).
- **Privacy text:** list processors per feature — STT (Gladia; fallbacks Mistral/ElevenLabs/OpenAI/Gemini by route), LLM (per route; one pinned provider for screen frames), H (agent sessions, ephemeral vault), Google (calendar).

---

## 11. Decisions only the owner can make
- **D1** Microphone readiness: accept the orange indicator while Gusan stays ready, and the default window (15 min proposed; 1 h / until stopped as options). Also accept that Shortcut/Control triggers show a brief system HUD.
- **D2** Gusan Screen idle‑stop default (10 min proposed) and whether "until I stop" is offered (re‑asks the acknowledgement).
- **D3** Agent execute sessions: desktop viewport (default) or a phone‑sized viewport.
- **D4** Vault v1 = H internal provider via Gusan Cloud with ephemeral per‑task vaults (plaintext transits Gusan Cloud in memory; revealable at H for minutes); 1Password as a later opt‑in. Confirm, or require 1Password‑only.
- **D5** Sites using Sign in with Google/Apple: out of v1.
- **D6** Ask H in writing: can reveal be disabled or scoped per API key; staff access policy to vault values and screenshots; retention of `fill_secret_at` events; whether `delete_screenshot_after_min` covers user‑supplied images.
- **D7** Gmail/Contacts: defer — confirm.
- **D8** H plan: Free (3 concurrent) is fine for one user; Builder once there are ≥2 users.
- **D9** Deployment target iOS 17 for the app (the keyboard still runs on iOS 16).
- **D10** Paid Apple Developer account timing (APNs, TestFlight, stable App Groups, no 7‑day expiry).
- **D‑new1** Ship a persistent per‑user H vault ("always ask" off) later? **Recommend no.**
- **D‑new2** H organisation membership = owner only; quarterly key rotation — confirm the policy.
- **D‑new3** Which STT/LLM providers are acceptable for audio and screen content (zero‑retention terms), and which single provider receives screen frames.
- **D‑new4** Confirm that "Memory" is dropped from v1 (undefined in the brief at the engineering level).

## 12. Implementation plan for engineers (tickets)
Each ticket: title · files touched · acceptance test on the owner's iPhone (or where noted). Branch names `devin/<ts>-<slug>`; one PR per ticket or small group.

**P0 — spikes**
- **P0‑1 S1 ready‑window keep‑alive.** `ios/Gusan/VoiceSession.swift` (debug flag to keep/pause the engine; heartbeat to App Group). Accept: heartbeats continue at 30 s / 5 min / 15 min with the screen locked; battery delta after 1 h noted.
- **P0‑2 S4+S5 intent spike.** New `ios/GusanWidgets` target (`StartListeningIntent: AudioRecordingIntent & LiveActivityIntent`, `ControlWidget`, minimal activity), `ios/project.yml`, `build_unsigned_ipa.sh`. Accept: from Control Center / Action button / Back Tap on the sideloaded build, recording starts in the background, the island shows the activity, HUD behaviour noted; the activity's button starts recording (S5).
- **P0‑3 S9 H vault mid‑session.** Script in `supabase/scripts/spike_s9.ts` (never committed with a key). Accept: a visual session bound to an empty vault fills a login on a test page after the item is added mid‑session; `fill_secret_at` absent in text mode confirmed.
- **P0‑4 S8 log hygiene.** Canary requests to `smart-action`/`agent-action`; read edge logs via Management API. Accept: no `Authorization`, no query strings, no bodies in `edge_logs`.
- **P0‑5 S10 AutoFill form.** Throwaway screen with `UITextField` vs SwiftUI `SecureField`. Accept: key icon appears; Face ID; both fields filled.

**P1 — strip, Smart, cleanup, session hardening**
- **P1‑1 Gusan strip.** `GusanKeyboard/GusanSurface.swift` → `GusanStrip.swift`, `KeyboardViewController.swift`, `VoiceKey.swift` (full‑width waveform), `KeyboardTheme.swift`. Accept: idle/listening/working/done states as in §1.2 in Messages; dark mode; globe only when needed.
- **P1‑2 Smart contract.** `GusanKit/SmartAction.swift` (`voiceSmart` payload with selected/before/after/screen/knowledge, `answer` kind), `GusanKit/GusanJob.swift` (`VoiceSmartResponse`), `supabase/functions/smart-action/index.ts` (system prompt), tests. Accept: selection replaced; dictation inserted; a question answered at the cursor.
- **P1‑3 30 s Undo.** `GusanKit/ReplacementUndo.swift` (expiry), `KeyboardViewController` timer. Accept: type 5 chars after insertion, Undo restores; after 31 s Undo gone.
- **P1‑4 Remove Privacy Mode and presets.** Files in §9.1. Accept: no Privacy Mode anywhere; 4 tabs; `swift test` green; `check_prompt_parity.py` green.
- **P1‑5 Session hand‑off hardening (§1.5).** `GusanKit/SessionStore.swift`, `SessionBackend.swift`, `AuthClient.swift`, `Gusan/AppModel.swift` (Darwin refresh), tests. Accept: with the App Group broken, the keyboard still works; Diagnostics shows "pasteboard: expires in <15 min, no refresh token".
- **P1‑6 Server one‑liners.** `agent-action/index.ts` (`stepText` strips `search/hash`; Bearer check before body read; image cap 2 × 1 MiB), `tests/test_agent_action.py`. Accept: tests.

**P2 — record from anywhere (after S4)**
- **P2‑1 `GusanWidgets` extension.** `ios/GusanWidgets/{GusanSessionActivity.swift, StartListeningIntent.swift, StopIntent.swift, CopyResultIntent.swift, GusanControls.swift}`, `project.yml`, `Gusan/Info.plist` (`NSSupportsLiveActivities`), `build_unsigned_ipa.sh`. Accept: Control Center control starts listening; island compact/expanded/lock‑screen render; Stop ends recording.
- **P2‑2 VoiceSession ↔ activity.** `Gusan/VoiceSession.swift` (activity lifecycle, ready window 15 min/1 h/∞, chime), `GusanKit/Voice/VoiceMachine.swift` (readiness), `GusanKit/Voice/VoiceProtocol.swift`. Accept: island shows listening → done; result copied to clipboard when no field; keyboard mic instant while ready.
- **P2‑3 Keyboard decision tree + copy.** `GusanKeyboard/VoiceKey.swift` (`KeyboardVoice.tap`), Home "Record from anywhere" card (`Gusan/HomeView.swift`). Accept: first‑time hint shown once; activation hop only when not ready.

**P3 — screen**
- **P3‑1 Two frames + icon + idle options + 50 % rule.** `GusanKit/Screen/ScreenChannel.swift` (`ScreenRequest.kind`), `GusanScreen/SampleHandler.swift`, `GusanKeyboard/ScreenKey.swift` (iPhone symbol), `Gusan/ScreenView.swift`, `smart-action` pinned provider for image requests. Accept: dictate about a Safari page → answer uses the page; red dot; idle stop at 5/10/30.

**P4a / P4b — agent**
- **P4a‑1 Live Activity progress + Face‑ID Confirm.** `Gusan/AgentCenter.swift` (activity updates, `LAContext` for purchase/send/delete/account_change, generic notification bodies), `GusanKeyboard/AgentKeyboard.swift` ("Confirm in Gusan" for those kinds), `GusanWidgets`. Accept: a `do` task shows progress in the island; Confirm for a purchase requires Face ID in the app.
- **P4a‑2 Off‑plan kill switch + `max_steps`.** `agent-action/index.ts` (`sync()`), migration adding `security_events`, tests. Accept: a plan on `example.com` whose agent navigates to another host fails with `off_plan_navigation`.
- **P4b‑1 Sessions tab + continue/stop + idle sessions.** `Gusan/ActivityView.swift` → `SessionsView.swift` (Smart | Agent), `GusanKit/Agent/AgentClient.swift` (`continue/stop/chat`), `agent-action` (`idle_timeout_s`, new actions). Accept: continue a research task; stop a running one.

**P5 — vault (after S9, S10)**
- **P5‑1 Custom tools in `agent-action`.** `agent-action/index.ts` (`agent.tools`, parked calls, `awaiting_login/awaiting_code`, `release_nonce`, `h_vault_id`, `release_count`), migration `202611080001_vault.sql` (`vault_logins`, `vault_events`, columns on `agent_task_runs`), tests. Accept: `test_agent_action.py` login/OTP cases.
- **P5‑2 `vault-action` function.** `supabase/functions/vault-action/index.ts` (`add`, `release`, `delete`), `_shared/log.ts`, sweeper `supabase/functions/vault-sweeper`, `tests/test_vault_action.py` incl. never‑log grep. Accept: tests; S8 re‑run.
- **P5‑3 iOS Vault tab.** `Gusan/VaultView.swift`, `GusanKit/Vault/{VaultStore.swift (keychain flags), VaultClient.swift, VaultModels.swift}`, `Gusan/LoginApprovalView.swift` (current page URL vs hostname, Face ID), tests. Accept: import a login from iPhone Passwords; agent task on that site pauses, Face ID, agent signs in; Vault › audit shows "Used"; delete removes it; re‑enrolling Face ID invalidates (documented).

**P6 — connections**
- **P6‑1 `google-oauth` + schema.** `supabase/functions/google-oauth/index.ts`, migration (`connections` owner‑RLS; `connection_secrets` and `oauth_states` service‑role only), `smart-action` knowledge hook (parallel fetch, 60 s cache, 55‑min access‑token cache, revoke on disconnect), tests. Accept: "am I free Friday at 3?" answered from Google in <2 s.
- **P6‑2 EventKit digest.** `Gusan/CalendarDigest.swift` (14‑day digest, titles off by default, App Group + pasteboard like settings), `Gusan/SettingsView.swift`. Accept: same question answered from the iPhone calendar with Google disconnected.

## 13. Spikes (what is truly necessary)
| ID | Verdict | Question / how |
|---|---|---|
| S1 | **Keep** | Does iOS keep Gusan running with an active `.playAndRecord` session when the engine is paused, and for how long? Throwaway build, heartbeats, screen locked; read Settings › Battery after the run (S2 folded in). ~2 h. |
| S4 (+S5) | **Essential — go/no‑go for P2** | `AudioRecordingIntent` from Control Center / Action button / Back Tap on a Sideloadly build: mic runtime in the background? HUD/banner? Must mic permission be pre‑granted in the foreground? Same build: Live Activity `Button(intent:)` (`LiveActivityIntent` + `AudioRecordingIntent`) starting recording — the fallback if S4 fails. |
| S8 | **Essential, cheap** | Supabase edge logs never contain bodies, `Authorization`, or query strings (`callback?code=`). |
| S9 (new) | **Essential before P5; API‑only, ~1 h** | Create an empty vault → start a visual session with `vault_id` → add an item mid‑session → does the agent get `fill_secret_at` on the matching page? Also confirm `fill_secret_at` is absent in text mode and what `delete_screenshot_after_min` covers. Decides §7.4 primary vs fallback. |
| S10 (new) | **Cheap, before P5** | Does the Password AutoFill key icon appear in Gusan's Add‑login form (SwiftUI `SecureField` vs `UITextField`) on the owner's iOS? 30 min. |
| S2 | Folded into S1 | Battery reading after the S1 run. |
| S3 | Demoted to a 15‑min check | `LSApplicationWorkspace` return on the owner's iOS version (`autoReturn` already in the repo). |
| S6 | Not a device spike | Gladia latency for 3 s / 10 s / 60 s takes: run from CI/desktop against `transcribe-proxy` (`testLiveVoiceInstruction`). |
| S7 | Not a spike | Host‑app matrix is P1 acceptance testing; do a 1‑h Gmail‑only pre‑check to decide the insert‑only fallback. |
