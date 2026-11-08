# Gusan actual iOS UI testing — d560611

Completed the test procedure on **iPhone 17 Pro, iOS 26.5 (23F73), Xcode 26.6**. Rebuilt and installed the app, used its real Settings-enabled keyboard in Messages, and authenticated the fresh confirmed Pro account. No preview host, demo session, runtime mutation or source changes were used as evidence.

## Findings and coverage

| Result | Flow | Observation |
|---|---|---|
| PASS | Keyboard sizing | Initial appearance, three total Cmd+K hide/show cycles, multiple app hops, landscape and return to portrait retained the complete strip and four normal-sized rows, without runaway or excess blank height. Visual geometry is consistent with the intended 262pt portrait / 208pt compact content; exact runtime bounds were not instrumented in this run. |
| PASS | Fresh Pro login and readiness | Email login succeeded through the UI using the Keychain-held generated password. Home had no redundant sign-in/setup checklist; Keyboard was On. Settings showed Gusan Pro. |
| PASS | Vault | Signed-in Vault refreshed to empty Logins/Accounts sections. “Couldn't update Vault” did not appear. Adding or using real credentials was not tested. |
| PASS | Keyboard access states | Signed out, signed in and Full Access off all retained the mode chip, screen/iPhone toggle, mic and four typing rows. Off state visibly explained “Allow Full Access”; normal typing remained usable. Full Access was restored afterward. |
| PASS | Microphone handoff/error recovery | Signed-in mic opened Gusan, which promptly displayed “Couldn't finish — Audio failed: Microphone unavailable.” Returning showed Retry on the keyboard; Retry repeated the explicit error rather than silently stalling. |
| UNTESTED | Live recording, transcription, insertion and Undo | macOS reports no audio devices. Actual microphone input cannot be verified on this VM; no synthesized audio or fake success substituted. |
| PASS | Agent task creation and Live Activity | Read-only example.com task was created and its real Working activity appeared in the Dynamic Island, including expanded form. |
| FAIL | Autonomous Agent progress/completion | Task stayed Queued for over four minutes. A parent investigator manually polled the server; only afterward the UI displayed the Example Domain result and a completion notification. This is a server-assisted observation, not an end-to-end autonomous completion pass. |
| PASS, assisted | Agent result UI | Server-assisted terminal result rendered page title and first sentence, and Copy changed to Copied. No external forms or irreversible actions were submitted. |
| PASS | Diagnostics | Settings → Advanced → Diagnostics → Copy report produced JSON showing a readable app-group session in both app and keyboard, Full Access true and App Group voice transport. |
| UNTESTED | Agent cancel and completed activity actions | Task was deliberately left uncanceled at the parent investigator's request. Completed Live Activity Copy/Open actions and follow-up continuation were not tested. |

Earlier unchanged-surface checks established real Settings installation/Full Access, keyboard shift/delete/123, strip mode/screen toggles, permission readiness and four-tab responsiveness. The current run also typed through the actual keyboard with Full Access disabled.

Agent task: `3080d3d5-a9c2-4968-bbad-79f4b8a8cd75`; created **2026-11-08 08:44:06 UTC**. Latest observed status: **Done**, server-assisted, approximately 4m41s displayed duration.

## Confirmed observations versus causes

- **Observed here:** fresh-process keyboard sizing is stable at d560611; no missing rows, oversized keys or blank excess height reproduced.
- **Lead's LLDB-confirmed sizing cause:** manual-frame-only children did not propagate fitted height to the host. Adding strip Auto Layout constraints changed host encapsulated height from 444 to 262. This run confirms resulting UI behavior but did not repeat LLDB mutation.
- **Confirmed environment limit:** `system_profiler SPAudioDataType` lists no devices; app reports microphone unavailable.
- **Lead/parent-confirmed Agent cause:** refresh lists tasks but does not status-poll listed nonterminal tasks. Server polling advanced the task. No separate fix is included in this tested revision.
- **Vault:** previous reported error did not reproduce after the parent-reported backend permission fix; this run does not independently prove the backend root cause.

## Visual evidence

| 🔴 Before: runaway keyboard | 🟢 After: bounded actual extension |
|---|---|
| ![Original runaway keyboard](https://app.devin.ai/attachments/1ac5131f-e6cb-47cb-993e-ddabf84a7c0b/gusan-keyboard-runaway.png) | ![Fixed signed-out actual extension](https://app.devin.ai/attachments/fa6f09e3-4063-4254-93e8-e6dd31ab25ea/gusan-d560611-signed-out.png) |

| Signed in, Full Access on | Signed in, Full Access off |
|---|---|
| ![Signed-in keyboard](https://app.devin.ai/attachments/4478acd8-64a9-4505-bfee-cb67447187fc/gusan-d560611-signed-in.png) | ![Full Access disabled](https://app.devin.ai/attachments/5830d75c-bdd6-403b-b5e6-8b1f3117b2ae/gusan-d560611-full-access-off.png) |

| Signed-in Vault: no update error | Explicit microphone limitation |
|---|---|
| ![Vault](https://app.devin.ai/attachments/410c469c-c5b0-4318-b4d6-81b8bf876d15/gusan-d560611-vault.png) | ![Microphone unavailable](https://app.devin.ai/attachments/ec7c8f2c-318b-4f64-8e3e-a2835f5250a8/gusan-d560611-microphone-unavailable.png) |

| Live Agent activity | Server-assisted Agent result, not autonomous pass |
|---|---|
| ![Working Dynamic Island activity](https://app.devin.ai/attachments/7785ee83-d69a-4de8-8309-3110e19055d5/gusan-d560611-live-activity.png) | ![Assisted result](https://app.devin.ai/attachments/7291eff9-16bc-4eaa-b545-f36153a73990/gusan-d560611-agent-server-assisted.png) |

## Artifacts

- Primary recording: `/Users/devin/screencasts/rec-d7133d33-5f83-40b4-ab70-23ee0aa077dc/rec-d7133d33-5f83-40b4-ab70-23ee0aa077dc-edited.mp4`
- Signed-in Diagnostics: `/Users/devin/gusan-diagnostics-signed-in.txt`
- Disabled-access Diagnostics: `/Users/devin/gusan-diagnostics-full-access-off.txt`
- Host audio inventory: `/Users/devin/gusan-host-audio-devices.txt`
- Mic logs: `/Users/devin/gusan-d560611-mic.log`
- Build log: `/Users/devin/gusan-d560611-build.log`
- Agent copied result: `/Users/devin/gusan-agent-copied-result.txt`

Still required for full coverage: the Agent polling fix and a fresh autonomous task retest; a real audio-capable device/host for recording, transcription, cursor insertion and Undo. No credentials or Pro grant are needed from the user now.
