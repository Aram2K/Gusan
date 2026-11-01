# Releasing Gusan

Gusan was called Transcribe until 2.0.0. The download files and other technical names keep the old name on purpose (see Compatibility Rules).

Do not publish a release manually unless you intentionally want to bypass the tested build pipeline.

## Compatibility Rules

- Publish the Windows installer as both `Gusan-Windows-Setup.exe` and `TranscribeApp-Windows-Setup.exe` (the same bytes).
- Publish the portable Windows build as both `Gusan-Windows.zip` and `TranscribeApp-Windows.zip` (the same bytes).
- Publish the macOS disk image as both `Gusan-Mac.dmg` and `TranscribeApp-Mac.dmg` (the same bytes).
- `update-manifest.json` keeps `windows.installer`, `windows.portable` and `macos.dmg` on the TranscribeApp names. `windows.gusan` and `macos.gusan` are the Gusan names. New builds download `windows.gusan.installer`. Already-shipped builds read `windows.installer` and the GitHub asset `TranscribeApp-Windows-Setup.exe`.
- The repo is `Aram2K/Gusan` (renamed from `Aram2K/transcribe-app`). Installed copies still call the old address and rely on GitHub's redirect, so never create another repo named `transcribe-app` under this account. Keep the exe `TranscribeApp.exe`, the Mac bundle `TranscribeApp.app` and its bundle id, the installer `AppId` and task names (`desktopicon`, `startup`), the data folder `Transcribe`, the keyring service `TranscribeApp` and the start-at-login Run value `Transcribe`.
- Keep what running copies and the server rely on: the single-instance port `47823` and its `transcribe-ok` reply, the IPC action names, config keys, usage event names, the cloud function names (`transcribe-proxy`, `transcribe-analytics`, `confirmed`, `stripe-webhook`) and the sign-in callback `http://127.0.0.1:53682/auth/callback`.
- Push semantic version tags like `v2.0.0`; do not reuse an old tag for a different build.
- Let GitHub Actions create the release and upload assets.
- Do not delete the installer asset from a published release.

Installed copies from 1.9 through 2.1.0 find updates through GitHub's latest release API and download `releases/download/<tag>/TranscribeApp-Windows-Setup.exe`. That exact name in the latest release is the hard contract. A 2.1.1 build downloads `Gusan-Windows-Setup.exe` (from `windows.gusan.installer` in `update-manifest.json`, or that asset name on the release). Each installer is published with its `.sha256`. The in-app update needs a readable checksum: with none it installs nothing (the person gets Try again and the download page), so never publish an installer without it. The two copies of each file must be byte-identical.

## What Needs a Release

- App code, tray UI, installer behavior, bundled dependencies, icons, and anything users run locally require a new tagged release.
- README, website text, docs, GitHub release notes, and backend/server changes can update without a new app release.
- Remote config can change without a release only after the app has shipped code that reads that remote config.

## Version Checks

The version lives in one place: `APP_VERSION` in `app_info.py` (with `APP_NAME = "Gusan"`).

- The build reads it from `app_info.py` as text (it never imports `main.py`) and **fails when a tag differs from it** (the tag without the `v`), on Windows and on Mac. `build.ps1 -Tag v2.0.0` does the same check locally.
- It goes into the Windows version info (`Gusan` by AIBUBEN, FileVersion `2.0.0.0`, file name still `TranscribeApp.exe`) and the Mac Info.plist (`CFBundleShortVersionString`, `CFBundleVersion`).
- The release notes come from `docs/release-notes/v<APP_VERSION>.md` when it exists.
- `tests/test_release_2_0_0.py` checks the name, the version, the release notes file and every kept name above.

## Build Notes

- `sherpa-onnx` must be **1.13.5 or newer** (`requirements.txt` says so). It runs speaker detection and, from 2.0.1, the local Armenian speech model; versions 1.12.26 to 1.13.4 return an empty text for about one clip in eight with that model. The app checks the version before it offers the model.
- `speech_hy.py` and `gpu_precision.py` are plain imports of `main.py`, so PyInstaller bundles them with no extra flag; `sherpa_onnx` is already bundled (`--collect-all`).
- The Armenian model (NVIDIA FastConformer, CC BY 4.0, converted to int8: a 105 MB archive) is not inside the app. It stays hidden until it has somewhere to be downloaded from. To switch it on without an app update, host the archive on `gusan.aibuben.xyz`, the GitHub releases or Hugging Face and publish `https://gusan.aibuben.xyz/models/manifest.json` as `{"models": {"hy-fastconformer-int8": {"url": "https://...", "sha256": "<ARMENIAN_MODEL_SHA256>", "size": <ARMENIAN_MODEL_SIZE>, "version": "1"}}}` (the values are in `speech_hy.py`; the app only installs the exact archive its code pins, whatever the manifest says). Or set `ARMENIAN_MODEL_URL` in `speech_hy.py` for the next release.

## Release Steps

1. Set `APP_VERSION` in `app_info.py` to the tag version without the `v` prefix, and write `docs/release-notes/v<version>.md`.
2. Run tests locally:

   ```powershell
   .\venv\Scripts\python.exe -m pytest -q tests
   .\venv\Scripts\python.exe -m pytest -q tests_qt
   ```

3. Commit and push to `main`.
4. Create and push the tag:

   ```powershell
   git tag v<version>
   git push origin v<version>
   ```

5. Wait for GitHub Actions to finish. A tag that differs from `APP_VERSION` stops the build at "Check the version".
6. Confirm the release contains both names, and that each pair shares one sha256:
   - `Gusan-Windows-Setup.exe` and `TranscribeApp-Windows-Setup.exe`, each with `.sha256`
   - `Gusan-Windows.zip` and `TranscribeApp-Windows.zip`, each with `.sha256`
   - `Gusan-Mac.dmg` and `TranscribeApp-Mac.dmg`, each with `.sha256`
   - `update-manifest.json`

7. Test these latest-download URLs:
   - `https://github.com/Aram2K/Gusan/releases/latest/download/TranscribeApp-Windows-Setup.exe`
   - `https://github.com/Aram2K/Gusan/releases/latest/download/update-manifest.json`

8. On a real Windows machine, update an installed copy from the previous release (CI never runs a real Setup, UAC or Inno Setup):
   - Per user: Install update shows the window, Gusan closes, the quiet Setup runs, Gusan reopens by itself.
   - Per user, "Install updates when I quit": quit, the quiet Setup runs, then start Gusan by hand while Setup still works: it must stay closed and open by itself once Setup is done.
   - All users (Program Files): Windows asks for permission, a No keeps Gusan open, a Yes updates it and Gusan reopens. Also try `/ALLUSERS /relaunch=1` by hand: if Setup's own reopen works for an elevated install, the watcher is no longer the only way back.
   - Kill the installer while it runs: Gusan comes back and says the install stopped, with Try again.
