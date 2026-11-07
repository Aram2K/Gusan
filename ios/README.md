# Gusan for iPhone — Milestone 1

A native companion app and a custom keyboard Smart Actions bar: Smart, Email,
To-do, Translate and Summarize, plus Undo. Actions use **Gusan Pro / Gusan Cloud
only**; no provider API keys live on the phone. Voice input is not included yet.

## Project and Mac builds

- `project.yml`: XcodeGen app + keyboard targets (iOS 16+, Swift 5).
- `Gusan/`: SwiftUI account, keyboard setup/settings and sharing diagnostics.
- `GusanKeyboard/`: lightweight UIKit keyboard extension.
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
and **10 App IDs per 7 days**; Gusan's app and keyboard consume **2 App IDs**.

## Enable and use

1. Settings › General › Keyboard › Keyboards › Add New Keyboard › **Gusan**.
2. Tap Gusan › **Allow Full Access** (required for network access).
3. Sign in in the Gusan app; cloud actions require Pro. Choose the default action
   and translation language there.
4. In a text field, switch using the globe key. Select text, or leave the cursor
   in the field, then tap an action. The logo runs your default action. **Undo**
   restores the original while the replacement context remains unchanged.

**Privacy:** Full Access enables networking and shared storage. Text is sent to
Gusan Cloud only when you tap an action. Authentication and entitlement checks
also contact Gusan's Supabase service. Without a selection, only the surrounding
text iOS exposes is submitted, not necessarily the entire document.

Google sign-in requires `gusan://auth-callback` in the Supabase Auth redirect
allow-list. Email/password sign-in works without this redirect configuration.

## Known limits and next milestone

iOS only gives keyboards text near the cursor: in very long documents, select
the text first. Secure fields and phone pads do not allow custom keyboards;
some apps disable them entirely. Undo is disabled if the document/context changes.
Actual Sideloadly installation, shared login and live cloud actions still need
physical-device verification.

Milestone 2 plans voice dictation using the containing app for microphone access,
with a URL-routed bounce (for example `gusan://dictate`) and a shared result for
the keyboard to insert. The keyboard has no microphone button or recording
implementation in M1; background behavior and the return-to-keyboard experience
will need device validation.
