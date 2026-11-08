# Gusan on iPhone — product brief (owner-approved direction, 2026-11-08)

Owner: Aram (Aram2K). Repos: Aram2K/Gusan (desktop Python app + `ios/` SwiftUI app, keyboard extension, GusanKit package, `supabase/functions/*` edge functions), Aram2K/aibuben (website). Backend: Supabase project `hftcelxzfoubheqeoool` (edge functions `smart-action`, `transcribe-proxy`, `agent-action`). Speech: Gladia via `transcribe-proxy`. Agent: H Company Agents API (key is server-side only, secret name `H_company_computer_agents`), mobile-only scope. Web search: Linkup for Smart mode only, never for Agent mode.

## Owner's words (verbatim, keep these)
- "I want to see if it's possible to have the keyboard, just the thing on top of it, not a separate keyboard … I would prefer to have the Gusan on top of any keyboard I use"
- "I still need the keyboard below but it should sit on top of the actual keyboard and opens when the keyboard opens"
- "when I press the record button, it doesn't open up some complicated stuff. But anywhere I am, let's just start the transcription … with like some sound that triggers the talking transcription"
- "it should be able to record it on spot anywhere and everywhere" (the first-tap app hop is NOT accepted; find the best possible solution and state precisely what iOS allows)
- "by default, it should only be like smart actions … it does what I say" (Smart mode default; Agent mode second; no preset actions like Translate/Rephrase)
- "he will either paste it on the cursor if the cursor is active, or it will just copy it to the clipboard"
- "I don't need all this translate to do that stuff … we don't need this done typing thing"
- "the mockups are not good and user friendly"; "overall I feel like a very AI slop app"; "the settings icon is very weird"
- Idle strip: "I need a camera icon and a mic icon … the mic icon by default is on and when I press the mic icon it will start the recording"; later: "better to have screen icon instead of camera and by default it should be on and selected on the keyboard we can select and deselect"; "the screen icon I want to look like it's an iPhone icon simple"
- Listening: waveform "should go until the end" (full strip width); controls "should be the mic sign or cancel sign … icons I mean"
- "when it's recording I want to see this part [Dynamic Island] to light up in some kind of animation around it"
- Context: "if I have a text selected and the camera icon on then I want both to be in the context and consider the selected text as a context always"
- Working state: no "weird dots"; a small spinner + the heard words.
- Agent: "this is a bit weird to have it covering your screen … it should probably have this as first moment and then it should go in the notification section that you cannot remove unless it's done and has outputted and where you can copy the output with a small button there or click on it and it will open the session with the agent and you can either continue there or just swipe the notification to remove the notification but you will still have it in the app session"
- "the sessions should also have agent or smart sections just like home"
- Passwords: "the user should be able to bring the password of the phone connect them the app … every time it is trying to login it can find the password and with a FaceID you can automatically input the password relevant"; "the agent should be in a way that he never exposes or knows the passwords and logins research this part how it's done and what are the best practices for handling it in the backend"
- "you should be able to connect your Google calendar or calendar and the agent should be able to answer to this fast based on that knowledge"
- "what is even privacy mode in this case it doesn't make sense" → Privacy Mode is removed from iOS. Only privacy controls: the screen icon (deselect) and Vault Face ID.
- "I need a more detailed technical description discussed by the engineering team to find the solutions and implement the features hassle free"

## Product definition (what to design / specify)
Gusan = an AI assistant that lives in the iPhone keyboard. Two modes only: **Smart** (default) and **Agent**.

### Keyboard
- A custom keyboard that is a faithful copy of Apple's iPhone keyboard (letters, shift, delete, 123, globe, space, return; no dictation key because custom keyboards get no microphone). Dark mode, haptics/sounds as iOS allows.
- A slim **Gusan strip** where Apple's suggestion row is. It appears every time the keyboard opens, in every app.
- Idle strip: mode chip (Smart | Agent, tap to switch) left; right: **screen icon** (simple iPhone outline, selected by default, tap toggles) and **mic icon** (filled blue circle, the primary action).
- Listening: chime; waveform fills the whole strip; right end: **X** (cancel) and **mic** (finish + send), icons only. Dynamic Island glows with an animated ring while listening. Small context chip shows what is used (selected text / screen).
- Working: spinner + the recognised words, Cancel.
- Done: result inserted at the cursor (or replaces the selection); "Inserted · Undo" (Undo 30 s, survives further typing); mic ready again.
- Context rules: selected text is ALWAYS context and is replaced by the result; else text around the cursor; with the screen icon selected also two screen frames (one at start, one at finish). Clipboard is only the fallback when there is no text field.
- No Copy buttons, no transcript screens, no "Done typing", no preset actions.

### Agent mode (H Company cloud browser; mobile-only)
- Same strip; tap mic in Agent mode → task starts. A card shows for ~3 s (what it understood), then collapses; keyboard stays fully usable.
- Progress lives as a **Live Activity**: Dynamic Island + lock screen, until done; then result + **Copy** + **Open** (opens the session chat). Swipe hides it only; the session stays in the app.
- Irreversible actions (pay, send, submit, delete): structural prepare/confirm/execute gate in Gusan's backend; Confirm card in strip when keyboard is open, else notification/Live Activity. Confirm never where Cancel was. Expires 15 min.
- Sessions: alive, continuable, stoppable, chat, memory; listed in the app.
- Needs-a-login: agent pauses; user approves with Face ID; the credential is filled by the backend/H vault; the agent never sees it.

### App (4 tabs, simple)
- **Home**: mode (Smart | Agent), readiness (keyboard added, Full Access, microphone), Gusan Screen switch, Connections, Back Tap, last result.
- **Sessions**: same Smart | Agent segmented switch as Home; Smart results and Agent sessions; session chat view (progress, confirm, continue, stop, memory).
- **Vault**: logins added from iPhone Passwords (iOS password AutoFill picker + Face ID), one per site; "always ask before using a login"; delete; audit (when used, never values). No payment cards in v1.
- **Settings**: account, language, sounds, notifications, Connections (Google Calendar, iPhone Calendar, later Contacts/Gmail), keyboard setup, advanced/diagnostics. No Privacy Mode.
- First run: one checklist (sign in, add keyboard + Full Access, microphone, try it).

### Connections (new)
- Google Calendar and iPhone Calendar (EventKit), read-only first; Smart answers "am I free Friday at 3?" instantly; Agent uses it when booking. Later: write (create events) after Confirm; Contacts; Gmail.

### Credentials the agent never sees (research done; design on top)
H Agents API has **Vaults** (providers `internal` and `onepassword`): items hold `username`, `password`, `email`, `totp`, `api_key` + allowed domains; the session exposes `fill_secret_at` which types the value into the page outside the agent's context; no endpoint returns secrets; vault bound to a cloud-browser environment via `vault_id`; domain-gated by page URL. Proposed: one H vault per Gusan user; sessions start without the vault; at a login page the task becomes `awaiting_login`; after Face ID Gusan Cloud rebinds for that site only; plaintext never in Gusan DB/logs/prompts/chat/notifications/clients; keyboard extension never handles passwords. Open: how iOS Passwords → H vault is transported (TLS to Gusan Cloud → H API; is end-to-end possible?), internal vs 1Password, MFA (SMS/Google/Apple sign-in need user help).

### Hard platform facts (verify against Apple docs, cite them)
- Nothing can draw over another app's keyboard; only a custom keyboard can be "on top of the keyboard". Custom keyboards: no microphone, no screenshots of the host app, Full Access needed for network.
- Known workaround used today: keyboard opens the containing app (URL scheme) which records with a background audio session; later taps work while the app is alive. Owner rejects a visible hop → investigate every alternative (keep app alive with audio session, push-to-start Live Activities, App Intents / Shortcuts / Back Tap / Action button, Apple dictation key, Siri, Control Center controls iOS 18, Live Activity interactivity, AirPods, Watch) and state honestly what is possible on a sideloaded (free Apple ID, 7-day) build vs App Store build.
- Screen frames: only ReplayKit broadcast upload extension (user consent, red indicator) can see other apps. Frames must be taken from before the keyboard covered the screen.
- Live Activities: ActivityKit (iOS 16.1+, Dynamic Island on 14 Pro+, push-to-start 17.2+, interactive buttons 17+). Can't be started from background without push-to-start.
- Memory limit for keyboard extensions (~ 60–70 MB), startup time; free Apple ID limits (no push notifications entitlement? verify), App Groups for app↔keyboard data.

## Existing code to read first
`ios/Gusan/*.swift`, `ios/GusanKeyboard/*.swift`, `ios/GusanKit/Sources/GusanKit/**`, `ios/DESIGN.md`, `supabase/functions/agent-action/index.ts`, `supabase/functions/smart-action`, `supabase/functions/transcribe-proxy`, `supabase/migrations/202611070001_agent_tasks.sql`, `202611070002_agent_task_phases.sql`. Open PRs (not merged): #13 voice redesign, #14 Gladia, #15 Linkup, #17 voice, #18 H backend, #19 iOS agent, #20 ReplayKit, #21 desktop screen chat. Branch `devin/1794056309-ios-keyboard`.

## Brand
Logo: the owner-picked "Pearl & Champagne" mic on the glassy Armenian flag; do not replace. Keep iOS-native feel (SF Pro, SF Symbols-like icons, system colors), not generic "AI app" gradients.
