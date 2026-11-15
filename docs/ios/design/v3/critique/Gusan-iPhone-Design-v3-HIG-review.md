# Gusan on iPhone — design spec v3: HIG / product-design review

**Reviewed:** `Gusan-iPhone-Design-v3.html` (23 iPhone 15 Pro frames, rendered in Chrome at 100 %, every frame screenshotted at 1× and 2×, geometry and colours read from the DOM), the 3 PNG exports, against `BRIEF.md` (owner's verbatim requirements) and Apple's HIG / iOS 17–18 system keyboard.
**Reviewer role:** senior iOS product-design critic. Scope: fidelity to the brief, HIG correctness, UX in WhatsApp / Gmail / Notes, 1× rendering defects. No redesign, no code.

## Summary

v3 is a real step up: it is native-looking, has no gradients, sparkles or invented chrome, the gear is now a credible `gearshape.fill`, the keyboard metrics are within a point of Apple's iPhone 15 Pro keyboard, and every state the owner asked for is present (Smart chip default, iPhone-outline screen icon selected, blue mic, full-width waveform with X + mic, glowing Dynamic Island, context chip, spinner + heard words, Inserted · Undo, 3-second Agent card → Dynamic Island, Live Activity with Copy/Open, Confirm, Needs-a-login, Sessions with Smart | Agent, Vault from iPhone Passwords with Face ID, Connections; no Privacy Mode, no preset actions, no dictation key).

It cannot go to the owner as-is: three frames are visibly broken at 1× (invisible text in the expanded Dynamic Island, the app icon under the Dynamic Island on First run, and Agent cards that cover the bottom key row while the caption promises the opposite — exactly the "covering your screen" complaint). Below those are a set of should-fixes that an App Store reviewer or a HIG-literate owner would catch: wrong shift/lowercase state on every keyboard frame, the missing orange recording indicator, two platform claims that iOS does not allow, and four contrast failures (one of which the spec table wrongly reports as passing).

Contrast ratios below were computed (WCAG 2.x relative luminance) from the hex values in the file.

---

## Findings

Severity: **blocker** = owner or App Store reviewer will reject on sight · **should-fix** = wrong against the brief / HIG / platform, fix before showing · **nit** = polish.

### Blockers

**1. `la-expanded` — three texts render black on black (invisible).** `blocker`
The expanded Dynamic Island has no `color` set; "Gusan Agent" (15 pt), "Booking Yerevan → Paris, Fri 14 Nov" (17 pt) and the "3/5" inside the ring inherit `#000` from `.screen` and vanish into the island. The frame reads as an empty black card with a progress bar.
*Fix:* `.island.wide{color:#fff}` (or wrap the content in `.in`). Then check line heights: with the title visible, the card is 150 pt; Apple's expanded island maxes at 160 pt, OK.

**2. `app-firstrun` — the app icon sits under the Dynamic Island.** `blocker`
`.hero` starts at the top of the screen (`inset:0`, `padding-top:28`), so the 96 pt icon spans y 28–124 while the island occupies y 11–48 and the status bar ends at y 54. At 1× the icon is clipped by the island.
*Fix:* top safe area 59 pt + 24 pt → `.hero{padding-top:83px}`; shift the checklist block from `top:318` to `top:373` and shrink the hero paragraph to one line ("Say what you want. Gusan writes it where your cursor is.") so "Finish later" still clears the bottom safe area (34 pt + 16).

**3. `kb-login`, `kb-agent-start`, `kb-confirm` — cards cover the bottom key row; captions say they don't.** `blocker`
Cards start at y 567 (strip 45 + 6). Row 4 (123 / space / return) starts at y 731. Measured card bottoms: Needs-a-login **779** (hides row 4 completely, 2 pt from the globe), Agent-start **760** (29 of 42 pt of row 4 covered), Confirm **733**. The Agent-start caption says "The bottom two key rows stay usable the whole time; nothing else covers the app" — false for all three. This is the one thing the owner explicitly objected to ("a bit weird to have it covering your screen").
*Fix (geometry):* hard rule card bottom ≤ 725 → max card height **158** from y 567, padding 12/14.
- Agent-start (193 → ≤158): drop the two-line `.sub` ("Working in a cloud browser…") — the Dynamic Island already shows progress; keep title + Stop. Height ≈ 149.
- Needs-a-login (212 → ≤158): header "Needs a login · airfrance.com"; remove the 17 pt question (the header says it); login row subtitle "aram@…com · from Vault · typed in by Gusan Cloud, never shown to the agent"; buttons. Height ≈ 156.
- Confirm (166 → ≤158): padding 14 → 10 top/bottom.
*Fix (copy):* "Space, return and 123 stay usable; the card goes away by itself after 3 s or when you swipe it down." If you prefer to keep the taller cards, say honestly that they cover the keys and add swipe-down to dismiss (otherwise the user has no way out while a Confirm waits).

### Should-fix

**4. All keyboard frames — shift state and letter case are wrong against iOS.** `should-fix`
Every frame shows uppercase key labels with the shift key *off* (gray `#ABB0BA`, outline arrow). Since iOS 9 the system keyboard shows **lowercase** letters when shift is off, and when autocapitalisation turns shift on (empty "Message" field in `kb-idle`, `kb-agent-idle`, `kb-agent-start`) the shift key is **white** with `shift.fill` and the letters are uppercase. The owner asked for "a faithful copy of Apple's keyboard"; this is the first thing an iPhone user notices.
*Fix:* `kb-idle`, `kb-agent-*`, `kb-confirm`, `kb-login`: shift on → key `#FFFFFF`, glyph `shift.fill`, uppercase. `kb-done` (caret after "?"), `kb-dark` (mid-sentence), `kb-listen-sel` (Gmail mid-sentence): shift off → **lowercase** letters `q w e r t y…`, gray key, `shift` outline.

**5. `kb-listen`, `kb-listen-sel` — the system's orange microphone indicator is missing.** `should-fix`
Whenever any process records audio iOS draws the orange privacy indicator at the trailing end of the Dynamic Island. The design shows a blue ring and a blue dot but no orange; on a device the owner will see both. A blue dot inside the island also collides with iOS's meaning (blue = location/Screen Mirroring).
*Fix:* add the 6 pt orange dot (`#FF9F0A`) at the island's trailing edge in both listening frames and mention it in the caption ("the orange dot is iOS's recording indicator"). Drop the custom blue dot inside the island; keep the 7-bar mini waveform.

**6. `kb-listen` caption + §5 motion, `ls-running` caption — two things iOS does not allow.** `should-fix`
(a) "glow that breathes on a 1.6 s loop" and "bars update at 60 fps" inside the Dynamic Island: ActivityKit has no continuous-animation API; island content changes only on (budgeted) activity updates with system transitions. The glow ring is fine as a *static* tinted compact Live Activity; the live waveform/breathing is not possible. (b) `ls-running`: "The card cannot be swiped away while the task runs" — the user can always dismiss a Live Activity (swipe → Clear) and the app cannot prevent it. The brief asks the team to "state honestly what iOS allows".
*Fix copy:* §5 Listening → "Dynamic Island: static 2-pt blue ring with a soft glow (a compact Live Activity); inside, a 7-bar waveform that updates when the strip does." `ls-running` → "Swiping the card away only hides it; the task keeps running and stays in Sessions › Agent (iOS lets the user dismiss any Live Activity)."

**7. Strip text buttons fail WCAG AA; the spec table claims they pass.** `should-fix`
"Undo" (`kb-done`) and "Cancel" (`kb-working`) are 15 pt semibold `#0B6AD1` on the keyboard `#D1D3D9` = **3.5:1** (AA needs 4.5:1). §4 colour table states "4.5:1 on the light keyboard" — incorrect.
*Fix:* light keyboard accent-as-text `#0058B8` (4.5:1 on `#D1D3D9`, 6.8:1 on white). Dark keyboard `#4DA3FF` on `#2B2B2D` = 5.4:1, fine. Correct the table row.

**8. `kb-idle`, `kb-done`, `kb-agent-*` — selected screen icon fails 3:1.** `should-fix`
Glyph `#0A84FF` on its 16 % blue tint over the keyboard (≈ `#B1C6DF`) = **2.1:1**; UI components need 3:1. Also the selected/deselected pair differs only in colour + a slash at 21 pt, which is hard to read at a glance on a gray keyboard.
*Fix:* selected glyph `#0058B8` (3.9:1) on the same tint; deselected stays `#6C6C70` `iphone.slash` (3.5:1, OK). Dark keyboard: `#4DA3FF` on 28 % tint, passes.

**9. `kb-listen-sel` (Gmail) — selection handles are drawn wrong.** `should-fix`
Both `.h1` and `.h2` land at x 298 (the end of the first line) and span both lines (47 pt tall), so one lollipop has a dot on top *and* bottom and the end handle after "come back" is missing. iOS draws two handles: start handle with the dot on top at "we", end handle with the dot at the bottom after "back", each one line tall.
*Fix:* position handles per line fragment (two absolutely positioned spans inside the first and last words), 2 × 22 pt bars, 10 pt dots.

**10. `kb-working` — the heard words truncate at the end, hiding the newest words.** `should-fix`
"reply that 3 works, ask her to send th…" — the user is checking that the last thing they said was heard; the ellipsis hides exactly that.
*Fix:* truncate the head (`lineBreakMode = .byTruncatingHead`: "…ask her to send the numbers before") or let the text scroll left as tokens arrive. Spec §5 "words type in as they arrive" matches head truncation.

**11. Agent tint is inconsistent across the flow.** `should-fix`
Agent chip dot and strip pill ring are indigo `#5E5CE6`; the Dynamic Island ring (`kb-agent-run`, `la-compact`, `la-expanded`), the lock-screen progress bar and the finished checkmark are systemBlue/green; the Agent-start header is indigo. The owner cannot tell Smart from Agent from the island.
*Fix:* one rule: **all Agent progress (ring, bar, pill, card header) = `#5E5CE6`**, Smart/mic/primary actions = `#0A84FF`, done = `#34C759`. Alternatively drop indigo entirely and mark Agent with the chip only — but pick one.

**12. Strip hit targets overlap at 44 pt.** `should-fix`
Screen toggle centre x 327, mic centre x 368 → 41 pt apart; with 44 pt hit areas they overlap by 3 pt (same for X / mic while listening). The table says "32–34 visual, 44 hit".
*Fix:* gap between the two right-hand controls 8 → **12 pt** (centres 45 pt apart); strip right padding stays 8.

**13. `app-vault` — the Face ID glyph in the navigation bar has no meaning.** `should-fix`
A 26 pt `faceid` symbol where iOS puts an action button; nothing in the caption says what tapping it does, and "Add" is already a row. It reads as decoration (the kind of thing that got v2 called "AI slop").
*Fix:* remove it. If a nav action is wanted, use "Edit" (text) for delete/reorder.

**14. `app-home` — the Smart | Agent segmented control is unexplained; it means different things on two tabs.** `should-fix`
On Home it is a *setting* (which mode the strip opens in); on Sessions the identical control is a *filter*. On Home there is no header or footer telling the user what it does.
*Fix:* Home: group header "MODE" and footer "The strip opens in this mode. You can also switch from the chip in the strip." Keep the control on Sessions as the filter (as the owner asked), default to Home's mode.

**15. `app-sessions-smart` — one-line 17 pt rows truncate every result at ~35 characters.** `should-fix`
All five rows end in "…"; the Armenian row loses half its words. Home's "Last result" uses a two-line 15 pt preview — use the same here.
*Fix:* title 15/20 Regular, `lineLimit(2)`, subtitle 13 pt "WhatsApp · 09:41"; row grows to 76 pt like Home.

**16. `app-firstrun` — dimmed steps fail contrast.** `should-fix`
Steps 3–4 title/subtitle `#AEAEB2` on white = **2.2:1**.
*Fix:* keep titles `#000` and subtitles `#6C6C70`; show "not yet" only with the gray numbered circle (`#C7C7CC` ring, `#6C6C70` number). If a dimmed look is wanted, minimum `#8E8E93` (3.3:1) for 17 pt titles only.

**17. `ls-done` — "Link copied with Copy." is confusing copy.** `should-fix`
It reads as if something was already copied, and the button is also called Copy.
*Fix:* result text only: "$379.99 at Best Buy (Amazon $399.00, Sony $399.99)". Copy copies the result and the link; Open opens the session.

**18. No dark-mode frame for the app or the cards.** `should-fix`
The brief asks for dark mode; §4 lists dark colours and `.kb.dark .kcard` exists, but only `kb-dark` (Notes) is shown. Owners judge dark mode from frames, not tables.
*Fix:* add two frames: `app-home` dark (`#000` grouped bg, `#1C1C1E` cards, separators `rgba(84,84,88,.6)`) and `kb-confirm` dark.

**19. Two states from the brief have no frame, and the first-tap reality is not acknowledged.** `should-fix`
(a) "No text field → result copied to the clipboard" exists only as a table row; the owner wrote this rule himself ("or it will just copy it to the clipboard") — one frame (notification "Copied · tap to open Gusan" + strip "Insert" on next focus). (b) Nothing in 23 frames or §6 says what the *first* mic tap does when the Gusan app is not alive (today: a visible hop to the app). The owner rejected the hop and asked for the honest answer; a spec that only shows in-place listening will be read as a promise.
*Fix:* add §6 item or a frame "First tap when Gusan is not running": exact behaviour on the sideloaded build vs App Store build, and the fallback (Back Tap / Action button start the recording with no hop).

**20. Copy voice is mixed.** `should-fix`
Agent-start card: "I'll ask before paying"; Live Activity: "will ask before paying"; Home footer: "Gusan reads text…". HIG style avoids first person in system-like UI.
*Fix:* third person or imperative everywhere: "Asks before paying." / "Gusan asks before paying."

**21. `kb-idle` in WhatsApp — two blue microphones 45 pt apart.** `should-fix (verify)`
WhatsApp's own voice-note mic (blue outline, compose bar) sits directly above Gusan's blue filled mic. New users will hit the wrong one; the brief wants "icons only", so the differentiator must be shape, not a label.
*Fix:* test with 3–5 users. If confusion shows, make Gusan's mic a **48 × 32 capsule** (`mic.fill` 19 pt) instead of a 34 pt circle — reads as a button, not a host-app icon — and keep it blue.

### Nits

**22. Tab bar inactive items `#999999` on `#F9F9F9` = 2.7:1.** `nit` Apple's own `systemGray` `#8E8E93` is 3.1:1; use it for parity (label 10 pt Medium).

**23. Home indicator is 134 pt; iPhone 15 Pro draws 139 × 5 pt.** `nit` Set width 139.

**24. Row chevrons `#C4C4C6` (1.7:1).** `nit` Apple parity (tertiaryLabel); acceptable, note it as intentional in §4.

**25. The mode chip disappears while listening/working.** `nit` In Agent mode the user loses the only mode cue mid-flow. Tint the context chip's leading icon with the mode colour, or keep a 8 pt mode dot before the context chip.

**26. `kb-agent-start` offers "Open" on a 3-second card.** `nit` It leaves the host app in the exact moment the owner wants to keep typing; "Stop" alone answers §6 Q3. The island/pill already open the session.

**27. `kb-confirm` "Details".** `nit` Keep it, but also make the card title tappable (opens the session) so the three-button row can drop to Cancel / Confirm when width is tight (dark mode, larger text).

**28. `app-vault` caption "the system picker".** `nit` There is no public API to pick from iPhone Passwords directly. The real path is: Gusan shows a sign-in form for the site (`textContentType .username/.password`), the user taps the key in the QuickType bar → Passwords → Face ID; Gusan then stores the credential in the per-user H vault. Say that, so engineering and the owner expect the same screen.

**29. Spec §4 says "Secondary is 5.1:1".** `nit` Measured 5.2:1 on white, 4.7:1 on `#F2F2F7` — fine; "#248A3D ok text" is 4.4:1 on white (just under 4.5 at 15 pt) → `#1D7A3B` (5.4:1).

**30. `ls-done` shows the full result on the lock screen (§6 Q6).** `nit` Recommend default "Result ready · tap to view" until Face ID, matching Hide Previews; the owner will approve the privacy-safe default.

**31. `kb-working` Cancel is text, `kb-listen` cancel is an X circle.** `nit` The brief literally says "Cancel" for Working, so it is compliant; if you want one gesture, reuse the 32 pt X circle in both.

**32. `kb-dark` key colours.** `nit` Apple's dark keyboard is `#2C2C2E`-ish translucent with keys ≈ `#6C6C6C` / special `#464646`; yours are within tolerance. OK.

**33. Agent chip in `kb-agent-idle` changes only an 8 pt dot (§6 Q1).** `nit` Keep the blue mic (one primary action, one colour) but tint the chip background `rgba(94,92,230,.12)` and label `#5E5CE6` in Agent so the mode is glanceable without reading.

---

## What passes (so nobody "fixes" it)

- Keyboard geometry vs Apple iPhone 15 Pro: keys 33 × 42, gap 6, row pitch 54, side margin 3–4.5, shift/delete 42, 123/return 87, space 201, globe glyph bottom-left, no dictation key, strip 45 pt where the prediction row is, total 336 pt. Return key gray "return" is right for WhatsApp, Gmail and Notes.
- Status bar (9:41 left ear, signal/Wi-Fi/battery right), Dynamic Island 126 × 37 at y 11, tab bar 83 pt, large titles 34/41, inset-grouped lists 16 pt margin, 44 pt rows, 10 pt radius, 0.5 pt separators inset 16/60. Notes' formatting bar stacked above the strip in `kb-dark` is correct.
- Icons are SF-faithful: `mic.fill`, `iphone` / `iphone.slash`, `xmark`, `chevron.up.chevron.down`, `gearshape` / `.fill` (the gear is no longer "weird"), `list.bullet.rectangle`, `lock`, `faceid`, `key`, `doc.on.doc`, `arrow.up.forward.app`.
- No gradients, sparkles or invented UI; copy is short and human ("Sees the screen when you speak", "Not set up · takes a minute", "Typed in by Gusan Cloud; the agent never sees it").
- Brief fidelity: every requested state exists; Confirm's Cancel-left / Confirm-right is honoured in the strip, the chat and the Live Activity; selected text is always context; Smart is default; no Privacy Mode, no preset actions, no transcript screens, no "Done typing".

## Verdict

**Fix blockers first, then ship to the owner.** Items 1–3 are rendering/geometry defects he will see in the first minute; items 4–8 are the HIG/platform points an App Store-quality bar requires and all are small CSS/copy changes (about half a day). Items 9–21 should land in the same pass if time allows; nits can wait for v3.1.

## Annotated screenshots (attached)

1. `1-la-expanded-black-on-black.png` — finding 1
2. `2-firstrun-icon-under-island.png` — finding 2
3. `3-cards-cover-bottom-row.png` — finding 3
4. `4-listening-selection-handles-and-mic-indicator.png` — findings 5 and 9
5. `5-strip-contrast-and-shift-state.png` — findings 4, 7 and 8
