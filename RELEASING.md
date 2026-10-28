# Releasing Gusan

Gusan was called Transcribe until 2.0.0. The download files and other technical names keep the old name on purpose (see Compatibility Rules).

Do not publish a release manually unless you intentionally want to bypass the tested build pipeline.

## Compatibility Rules

- Keep the Windows installer asset named `TranscribeApp-Windows-Setup.exe`.
- Keep the portable Windows asset named `TranscribeApp-Windows.zip`.
- Keep the macOS asset named `TranscribeApp-Mac.dmg`.
- The repo is `Aram2K/Gusan` (renamed from `Aram2K/transcribe-app`). Installed copies still call the old address and rely on GitHub's redirect, so never create another repo named `transcribe-app` under this account. Keep the exe `TranscribeApp.exe`, the Mac bundle `TranscribeApp.app` and its bundle id, the installer `AppId` and task names (`desktopicon`, `startup`), the data folder `Transcribe`, the keyring service `TranscribeApp` and the start-at-login Run value `Transcribe`.
- Keep what running copies and the server rely on: the single-instance port `47823` and its `transcribe-ok` reply, the IPC action names, config keys, usage event names, the cloud function names (`transcribe-proxy`, `transcribe-analytics`, `confirmed`, `stripe-webhook`) and the sign-in callback `http://127.0.0.1:53682/auth/callback`.
- Push semantic version tags like `v2.0.0`; do not reuse an old tag for a different build.
- Let GitHub Actions create the release and upload assets.
- Do not delete the installer asset from a published release.

Installed copies find updates through GitHub's latest release API and download `releases/download/<tag>/TranscribeApp-Windows-Setup.exe`. That exact name in the latest release is the hard contract. Newer versions also check the `.sha256` file and `update-manifest.json` when present, but older copies only need the installer name.

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
6. Confirm the release contains:
   - `TranscribeApp-Windows-Setup.exe`
   - `TranscribeApp-Windows-Setup.exe.sha256`
   - `TranscribeApp-Windows.zip`
   - `TranscribeApp-Windows.zip.sha256`
   - `TranscribeApp-Mac.dmg`
   - `TranscribeApp-Mac.dmg.sha256`
   - `update-manifest.json`

7. Test these latest-download URLs:
   - `https://github.com/Aram2K/Gusan/releases/latest/download/TranscribeApp-Windows-Setup.exe`
   - `https://github.com/Aram2K/Gusan/releases/latest/download/update-manifest.json`
