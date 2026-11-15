# Gusan for Android

The Gusan keyboard and meeting recorder for Android phones, written in Kotlin. It signs in to the same
account as Gusan on the desktop and the iPhone and uses the same Gusan Cloud functions
(`transcribe-proxy`, `smart-action`).

## What it does

- **Keyboard.** An ordinary keyboard (letters, numbers, symbols) with the Gusan strip on top. Tap the mic,
  say what you want, and the finished text lands at the cursor. With text selected, what you say is
  applied to the selection ("make it shorter", "translate to French") and replaces it. **Undo** puts the
  old text back for 30 seconds. Android lets a keyboard use the microphone, so there is no hop to the app.
- **Meetings.** Record with the screen off, then get a transcript and notes (Summary, Key decisions,
  Action items, Open questions, Discussion). The notes prompt is the desktop's, word for word.
  A long recording is sent in pieces of up to 7 minutes, cut at a quiet moment; if something fails
  half-way, **Try again** carries on from the piece it stopped at.
- **Sessions.** What Gusan wrote from the keyboard, kept on the phone; tap to copy.
- **Account.** Email and password, Google, create an account, reset a password.

Voice and meeting notes use Gusan Cloud, so they need Gusan Pro (the server decides; the app shows what
it answers). Typing works without an account.

Not in this version: Agent mode, the Vault, Gusan Screen and calendar connections of the iPhone app,
key previews and long-press accents, and layouts other than English.

## Layout

| Folder | What is in it |
|---|---|
| `core/` | Plain Kotlin, no Android: sign-in, speech and Smart Action clients, the notes prompt, the meeting pipeline, keyboard rules. Has the tests. |
| `app/` | The Android app: the keyboard service (`keyboard/`), the recording service (`meetings/`), the screens (`ui/`). |
| `scripts/export_notes_prompt.py` | Writes the notes prompt and its test samples from the desktop's `notes_prompt.py`. Run it after changing that file; CI fails when the copy is stale. |

## Build

Android Studio: open `mobile/android`. Command line, with the Android SDK installed:

```sh
cd mobile/android
./gradlew :core:test            # the logic tests (no Android SDK needed)
./gradlew :app:assembleDebug    # app/build/outputs/apk/debug/app-debug.apk
```

Without an Android SDK (`ANDROID_HOME` unset and no `local.properties`) only `:core` is part of the build.

`core/src/main/resources/gusan/prompts.json` is a copy of the iPhone app's
`mobile/ios/GusanKit/Sources/GusanKit/Resources/prompts.json`; CI fails when the two differ.

The [Android workflow](../../.github/workflows/android.yml) runs the tests, builds the debug APK and
uploads it as the `Gusan-Android-debug` artifact.

## Install and set up

1. Install the APK (allow installs from your browser or file manager when Android asks).
2. Open Gusan and sign in.
3. **Turn on the Gusan keyboard** (Settings › System › Keyboard › On-screen keyboard), then **choose** it.
   The Home page has a button for each step and shows what is still missing.
4. Allow the microphone.

Google sign-in needs `gusan://auth-callback` in the Supabase Auth redirect allow-list (the iPhone app
uses the same address).

## Privacy

Audio leaves the phone only when you tap the mic or stop a meeting recording, and goes to Gusan Cloud.
What you type is never sent anywhere. Recordings, transcripts, notes and Sessions are stored in the
app's private folder and are excluded from backups. The sign-in is encrypted with a key in the Android
Keystore. The mic is off in password fields.

## Not yet checked on a phone

This was built without a device: the logic is covered by tests and the app is compiled by CI, but the
keyboard, recording in the background and Google sign-in have not been tried on a real phone yet.
