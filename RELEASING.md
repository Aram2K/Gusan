# Releasing Gusan

Gusan was called Transcribe until 2.0.0. The download files and other technical names keep the old name on purpose (see Compatibility Rules).

Do not publish a release manually unless you intentionally want to bypass the tested build pipeline.

## Compatibility Rules

- Keep the Windows installer asset named `TranscribeApp-Windows-Setup.exe`.
- Keep the portable Windows asset named `TranscribeApp-Windows.zip`.
- Keep the macOS asset named `TranscribeApp-Mac.dmg`.
- Keep the repo `Aram2K/transcribe-app`, the exe `TranscribeApp.exe`, the Mac bundle `TranscribeApp.app` and its bundle id, the installer `AppId` and task names (`desktopicon`, `startup`), the data folder `Transcribe`, the keyring service `TranscribeApp` and the start-at-login Run value `Transcribe`.
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
   git tag v2.0.0
   git push origin v2.0.0
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
   - `https://github.com/Aram2K/transcribe-app/releases/latest/download/TranscribeApp-Windows-Setup.exe`
   - `https://github.com/Aram2K/transcribe-app/releases/latest/download/update-manifest.json`
