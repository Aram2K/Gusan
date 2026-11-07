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
5. Tap the mic to open Gusan and start a voice session the first time, then return to the keyboard. Tap stop to process the recording; tap cancel while transcribing/thinking.
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

## 7. Voice hook (Milestone 2)
`KeyboardViewController.startVoice()` is the only mic entry point. It may open the containing app via `openContainingApp(_:)`, and it can feed a transcript into instruction mode or insert it directly.
