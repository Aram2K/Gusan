# Gusan for iPhone

A native companion app and a custom keyboard Smart Actions bar: Smart, Email,
To-do, Translate and Summarize, plus Undo. Actions use **Gusan Pro / Gusan Cloud
only**; no provider API keys live on the phone. Voice input uses the containing
app for microphone access and returns results to the keyboard.

## Project and Mac builds

- `project.yml`: XcodeGen app + keyboard targets (iOS 16+, Swift 5).
- `Gusan/`: SwiftUI account, keyboard setup/settings and sharing diagnostics.
- `GusanKeyboard/`: lightweight UIKit keyboard extension.
- `GusanScreen/`: ReplayKit broadcast upload extension ("Gusan Screen"); keeps one downscaled frame in memory for the keyboard's Screen chip.
- `GusanKit/`: shared prompts, cloud/auth clients, session/settings stores and tests.
- `scripts/`: desktop prompt parity check and IPA packaging.

With Xcode and its iOS platform installed:

```sh
brew install xcodegen
cd ios
xcodegen generate
open Gusan.xcodeproj
# No development team is needed for Simulator builds.
xcodebuild -project Gusan.xcodeproj -scheme Gusan -destination 'generic/platform=iOS Simulator' build
(cd GusanKit && swift test)
python3 scripts/tests/check_prompt_parity.py
bash scripts/build_unsigned_ipa.sh
```

The generated project is ignored. Device installation needs re-signing; the
script produces `build/Gusan-iOS-unsigned.ipa` with ad-hoc signatures embedding
the app-group entitlements, not an Apple-authorized install signature.

The [iOS (unsigned IPA) workflow](https://github.com/Aram2K/Gusan/actions/workflows/ios.yml)
runs package tests, prompt parity and a Simulator build, then uploads the
`Gusan-iOS-unsigned` artifact (retained for 30 days). No signing secrets are needed.

## Install on iPhone from Windows — Sideloadly + free Apple ID

1. Download `Gusan-iOS-unsigned` from a successful workflow run's **Artifacts**
   section (GitHub sign-in may be required). Unzip the artifact ZIP to get the `.ipa`.
2. Install **iTunes and iCloud from Apple's website**, not the Microsoft Store
   versions; Sideloadly needs Apple's desktop components. Install
   [Sideloadly](https://sideloadly.io/) using its current Windows prerequisites.
3. Connect the iPhone by USB, unlock it and trust the computer. Select the phone
   in Sideloadly, drag in the `.ipa` and enter your Apple ID.
4. In advanced options, **keep app extensions**: do not enable the option to
   remove extensions. Leave signing mode as **Apple ID sideload** and leave
   app-group/entitlement handling at its defaults. Labels vary by release.
   Start installation and complete Apple's authentication prompts.
5. On iPhone, enable **Settings › Privacy & Security › Developer Mode** and
   restart when prompted. Trust the developer profile under **Settings › General
   › VPN & Device Management** if required.
6. Open Gusan and check **Sharing diagnostics**. Sideloadly may rewrite bundle
   and group IDs; Gusan tries to discover the installed group. If diagnostics
   say **not shared**, the keyboard cannot see the app's login. This signing
   path has not been verified on a physical iPhone; preservation of working
   app-group access is not guaranteed.

Free Apple ID installs expire after **7 days**: re-sign/refresh with Sideloadly.
Data is usually kept when using the same Apple ID and bundle ID, but is not
guaranteed. Free provisioning generally permits **3 installed sideloaded apps**
and **10 App IDs per 7 days**; Gusan's app, keyboard and Gusan Screen extension consume **3 App IDs**.

## Enable and use

1. Settings › General › Keyboard › Keyboards › Add New Keyboard › **Gusan**.
2. Tap Gusan › **Allow Full Access** (required for network access).
3. Sign in in the Gusan app; cloud actions require Pro. Choose the default action
   and translation language there.
4. In a text field, switch using the globe key. Select text, or leave the cursor
   in the field, then tap an action. The logo runs your default action. **Undo**
   restores the original while the replacement context remains unchanged.
5. Tap the Gusan mic to open the app and start a voice session the first time,
   then return to the keyboard. Tap stop to process your recording, or cancel
   while transcribing/thinking. Results return to the keyboard for insertion.

**Simulator preview of the keyboard.** Third-party keyboards cannot be enabled in the
Simulator, so the `GusanKeyboardPreview` scheme (Simulator-only, never embedded in the
.ipa) hosts `KeyboardViewController` inside a plain app. Launch it with `--screen` to
show the Screen chip armed, or `--screen-offer` for the ghosted chip shown when no
broadcast is live. The same can be done with the real keyboard extension in a debug
build by setting the `debugScreenPreview` / `debugScreenOffer` boolean in the keyboard's
defaults (`xcrun simctl spawn booted defaults write group.com.aram2k.gusan debugScreenPreview -bool YES`).
Debug launch arguments for the app: `--gusan-debug-screen-page` opens Home › Gusan Screen;
`--gusan-debug-screen-live` fakes a live broadcast on that page.

6. **Gusan Screen:** open Home › Gusan Screen, read what is shared, then Start.
   While the red indicator shows, the keyboard gets a **Screen** chip; tap it,
   type "reply to this" (or pick a suggestion) and Run. One picture of the
   screen from just before the keyboard came up goes to Gusan Cloud with your
   instruction; the answer is inserted with Undo. The picture contains everything
   visible on the screen at that moment; the chip is never offered in password
   fields. It stops by itself after 10 minutes without a question (Keyboard ›
   Screen). The Simulator cannot run broadcasts; test on a device. If Sideloadly
   renames the extension so it no longer ends in `.screen`, the broadcast sheet
   lists every broadcast extension instead of preselecting Gusan Screen.

**Privacy:** Full Access enables networking and shared storage. Text is sent to
Gusan Cloud only when you tap an action. Authentication and entitlement checks
also contact Gusan's Supabase service. Without a selection, only the surrounding
text iOS exposes is submitted, not necessarily the entire document.

Google sign-in requires `gusan://auth-callback` in the Supabase Auth redirect
allow-list. Email/password sign-in works without this redirect configuration.

## Known limits

iOS only gives keyboards text near the cursor: in very long documents, select
the text first. Secure fields and phone pads do not allow custom keyboards;
some apps disable them entirely. Undo is disabled if the document/context changes.
Actual Sideloadly installation, shared login and live cloud actions still need
physical-device verification.

Voice dictation opens the containing app via `gusan://voice` for microphone
access and shares results with the keyboard. Background behavior and the
return-to-keyboard experience still need physical-device validation.
