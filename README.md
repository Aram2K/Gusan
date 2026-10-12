<div align="center">

<img src="assets/banner.svg" alt="Gusan" width="820"/>

<br/>

[![License: Non-Commercial](https://img.shields.io/badge/license-Non--Commercial-red?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-blue?style=flat-square)]()
[![Python](https://img.shields.io/badge/python-3.9%2B-green?style=flat-square)]()
[![Offline](https://img.shields.io/badge/works-offline-success?style=flat-square)]()

<h3>Press a key. Speak. Your words appear - wherever your cursor is.</h3>

<p>A lightweight, always-on speech-to-text tool that lives in your system tray.<br/>
Works completely offline using Whisper AI, or in the cloud when you choose.</p>

**[⬇ Download for Windows (Installer)](https://github.com/Aram2K/transcribe-app/releases/latest/download/TranscribeApp-Windows-Setup.exe)** &nbsp;·&nbsp;
**[⬇ Download for macOS (.dmg)](https://github.com/Aram2K/transcribe-app/releases/latest/download/TranscribeApp-Mac.dmg)** &nbsp;·&nbsp;
[View all releases](https://github.com/Aram2K/transcribe-app/releases)

<sub>Prefer a portable build? Grab the [Windows .zip](https://github.com/Aram2K/transcribe-app/releases/latest/download/TranscribeApp-Windows.zip) instead.</sub>

</div>

---

## What is Gusan?

Gusan was called Transcribe until version 2.0.0. Same app, new name: your settings, history and sign-in are kept.

Gusan is a hotkey-triggered dictation tool that runs silently in the background on your computer. Press your configured key, speak naturally, and when you stop - the transcription is automatically pasted wherever your cursor is: a chat window, a document, an email, a code editor, anything.

It writes each language in its own script (never Latin transliteration) - English, Spanish, French, German, Russian, Arabic and more - even when you switch languages mid-sentence.

---

## Features

| | |
|---|---|
| 🎙 **Hotkey recording** | Press any key combo (or mouse button) to start/stop |
| 🪟 **Live Assistant** | A private copilot over your calls: live transcript, instant streamed answers the moment you are asked something, screen-aware answers - **hidden from screen sharing** |
| 🎧 **Record meetings** | System audio + mic, speaker labels, AI notes with action items, Notion export, resumable sessions, MP3/WAV recordings |
| 📄 **Audio to text** | Drop any audio/video file → a transcript with speaker labels and timestamps, and a Word document if you want one |
| 📋 **Smart paste** | Auto-pastes at your cursor; falls back to clipboard |
| 🗣 **Mixed languages** | Switch languages mid-sentence - every word stays in the language and alphabet you spoke it in |
| 🔒 **Fully offline** | Local Whisper AI and Qwen/Gemma action summaries offline |
| ☁️ **Cloud option** | Gusan Cloud with Pro (no setup), or your own Gemini or Mistral key |
| ⚡ **Streaming results** | Background chunks transcribed while you speak |
| 📊 **Dictation pill** | Liquid-glass pill with a live waveform - drag it anywhere, never steals focus |
| 🕘 **Recent** | Your latest dictations, meetings and files on the Home page |
| ⚙️ **Settings on each page** | Every page has its settings next to what they change; changes apply at once |
| 📝 **Dictionary** | Seed recognition with names or domain terms |
| 🪟 **System tray** | Zero footprint background daemon |

---

## Download & Install

### Windows

1. Download **[TranscribeApp-Windows-Setup.exe](https://github.com/Aram2K/transcribe-app/releases/latest/download/TranscribeApp-Windows-Setup.exe)**
2. Run it - the installer wizard handles everything (Start Menu shortcut, optional autostart, optional desktop icon)
3. Click **Finish** - Gusan launches automatically and a microphone icon appears in your system tray
4. The welcome window checks your computer, recommends a speech model, and sets up your microphone and shortcut
5. To uninstall later: **Settings → Apps → Gusan → Uninstall**

> **Note:** Windows may show a "Windows protected your PC" warning the first time because the app isn't code-signed. Click **More info → Run anyway**. The app is open-source and safe.

**Updates:** when a new version is published, Gusan shows one quiet notice. Click it, or use **Settings › Updates & about › Install update**. The download keeps its `TranscribeApp-Windows-Setup.exe` name, so every installed copy finds it.

### macOS

1. Download **[TranscribeApp-Mac.dmg](https://github.com/Aram2K/transcribe-app/releases/latest/download/TranscribeApp-Mac.dmg)**
2. Open the `.dmg` and drag the app to your Applications folder. In Finder it is still called **TranscribeApp**; the menu bar and the permission prompts say **Gusan**.
3. **First launch - Gatekeeper warning.** macOS will block the app with *"Apple could not verify TranscribeApp is free of malware"* because it isn't notarized (notarization needs a paid Apple Developer ID - the app is open-source and safe). Use **one** of these:
   - **Easiest:** Open **System Settings → Privacy & Security**, scroll to the bottom and click **Open Anyway** next to the TranscribeApp message.
   - **Or:** In Finder, right-click (or Control-click) **TranscribeApp.app → Open**, then click **Open** in the dialog.
   - **Or via Terminal:** `xattr -dr com.apple.quarantine /Applications/TranscribeApp.app` then launch normally.
4. **Where did the app go?** Gusan has no Dock window - it runs as a **microphone icon in the menu bar** at the top-right of your screen, next to the wifi/battery icons. Click it any time to open Gusan, dictate, record a meeting, or quit.
5. **Grant Accessibility permission** - required for the global hotkey:
   - The first time you press your hotkey, macOS prompts you to allow Gusan under **System Settings → Privacy & Security → Accessibility**. Toggle Gusan (TranscribeApp) **on**. Until you do, the hotkey silently won't fire.
6. **Hotkey:** the default is **Option + R** (`alt+r`). To change it: open Gusan → **Dictation** → **Shortcut**, then press any combination of `⌘ ⌥ ⌃ ⇧` plus a key. It's read by physical key code, so any keyboard layout (French, German, Russian, …) works.

---

## How to Use

```
1.  Press Alt+R  →  overlay appears, recording starts
2.  Speak        →  waveform pulses, partial results preview
3.  Press Enter  →  transcription is pasted at your cursor
    Press Esc    →  cancel without pasting
```

Press **Alt+S** (**Option+S** on Mac) instead for a Smart Action: say what you want ("Email Anna: the demo moves to Thursday"), press again, and the result is pasted with real formatting. Alt+R always pastes your exact words.

Click the **tray icon** to open Gusan, or right-click it for Dictate, Record a meeting, Live Assistant, Convert audio to text, Settings, or Quit.

---

## Record meetings 🎙

Capture a Google Meet, Zoom, or any conference call and automatically get back a transcript, a short summary, and a checkbox list of action items.

```
1.  Open the Meetings page (or tray icon → Record a meeting…)
2.  (Optional) Add the meeting title + the people in the call - these
    anchor action-item ownership in the final notes
3.  Pick what to record from, click Record a meeting
4.  A live timer and level meters show the recording is on, next to
    the live transcript and a running summary (So far). Pause any time.
5.  Type your own points in "My notes" while you listen. They'll be
    merged into the AI notes.
6.  Click Stop and write notes → wait 10-60s while the AI engine runs
7.  Copy notes, or Export to Word, Markdown, Notion or the recording
    (MP3/WAV) - everything is also stored on disk
```

The AI notes follow a research-backed structure (Microsoft Research, 2023): `## Summary` in third person, `## Key decisions`, `## Action items` as `- [ ] task (Owner: name)` checkboxes, `## Open questions`. When you provide an attendee list, owner attribution kicks in: phrases like *"I'll draft the spec"* and *"Aram should review it"* get attributed to the right person.

Long silence gaps (>1.4s) are tagged as `[speaker change]` in the transcript so the LLM can attribute who said what - a lightweight diarization signal that needs no extra ML dependency.

Each meeting is stored in `%APPDATA%\Transcribe\meetings\<timestamp>\` with:
- `chunks.jsonl` - every transcribed chunk as it arrived (crash-recoverable)
- `transcript.txt` - full transcript
- `notes.md` - Summary + Key decisions + Action items + Open questions
- `meta.json` - title, attendees, duration, timestamp
- `audio_partN.wav` - the raw recording, one file per recording segment

The **Meetings** page lists every meeting. Open one for its **Notes**, **Transcript** and **My notes**, **Copy notes**, **Export** (Word, Markdown, Notion, or the recording as MP3 or WAV), **Continue recording** (continue the same meeting later - the new part is appended and the notes rewritten) and **Rewrite notes**.

### Live Assistant 🪟

While a meeting is being recorded (or straight from the tray, the **Live Assistant** page or **Ctrl+Alt+A**), the **Live Assistant** card floats over your call as a translucent glass card:

- **Start** begins live transcription right from the card; a green timer runs, the red button stops and generates the notes.
- **NOW** shows the last thing said; **SO FAR** a running AI summary.
- **A companion, not a summary**: when someone asks you something, the answer starts streaming on its own; **Answer / Follow-ups / Solve screen** or any typed question work any time. Answers are formatted (bold key phrases, bullets, code blocks).
- **Screen context**: with **Screen** on, every answer sees the screen under your cursor - the card itself is never in the screenshot.
- **Session context**: **+ Add context** tells it what the call is about (who you are, who you're talking to, what you want); it's locked in for the session and shapes every answer.
- **Images**: snip part of your screen (the snipping screen is hidden from screen sharing on Windows) or paste one with Ctrl+V - it goes with your next question.
- **Engine**: Pro answers come from DeepSeek V4.1 Flash on a Modal endpoint (vision, thinking off for speed), with Gemini Flash and Mistral as automatic fallbacks. Your own Cerebras / OpenAI-compatible / Gemini key works too.
- **Private**: the card is excluded from screen sharing and recordings on Windows 10 2004+ and macOS 14 and earlier, and the card's **Hidden** chip always reports the real state (it says so when the OS can't guarantee it - e.g. Remote Desktop, or macOS 15+, where sharing a single window keeps the card out).

It is a privacy feature for your own notes during calls; recording-consent guidance applies, and it is not intended for interviews, exams or assessments where assistance is prohibited.

### Capturing the *other* participants' audio

By default, the meeting recorder is set to **Smart Meeting Mode (Record BOTH Computer Sound + My Microphone)**. This dynamically scans for the active default playback device (Speakers or Headphones) and automatically captures/mixes what the other people in the meeting are saying directly, with zero manual host API configuration needed!

- **Windows:** Completely automatic using the dynamic WASAPI loopback mixer. No virtual cables or complicated drivers needed.
- **macOS:** Install [BlackHole](https://existential.audio/blackhole/) (free) to enable native loopback routing.
- **Linux:** Select standard PulseAudio monitor inputs.

### Notes quality

The summary and action items use the AI engine picked under **Notes written by** on the Meetings page (your own AI keys live in **Settings › Speech & AI**):

| Engine | Notes quality | Cost |
|---|---|---|
| **Gusan Cloud** (with Pro) | Best quality, zero setup | Included in Pro |
| **Cerebras** (bring your own key) | Fastest - sub-second streamed answers, screen-aware (vision) | ~$0.003 per Live Assistant answer |
| **Mistral AI** (bring your own key) | Ministral models with image input; the same key runs Voxtral speech and Mistral OCR for screen text | Pennies per meeting |
| **Local Qwen / Gemma** (1.5B–7B) | Good summarisation, no network required | Free (one-time download) |
| **OpenAI / Gemini / Anthropic** | Best quality (GPT-5.x, Gemini Flash, Claude 4.x) | Pennies per meeting |

Meetings of any length work with local models too - long transcripts are summarised in stages automatically. The built-in formatter is only a last-resort fallback for final notes; the live features (Live Assistant, running summary) use real AI models or tell you to connect one.

---

## Backends

| Backend | Best for | Speed | Cost | Internet |
|---------|----------|-------|------|----------|
| **Local (Whisper AI)** | Privacy, offline use | 0.5-15s | Free forever | ❌ No |
| **Gusan Cloud** | Accuracy without a fast computer, no setup | Fast | Included in Pro | ✅ Yes |
| **Mistral Voxtral / Google Gemini** | Your own key | Fast | Your key's pricing | ✅ Yes |

### Using your own key

1. Get an API key from [Mistral](https://console.mistral.ai) or [Google AI Studio](https://aistudio.google.com)
2. Paste it in **Settings › Speech & AI** (API keys) and test it
3. Pick it as the speech engine and set Language to **Auto-detect** or your language

---

## Local Models

| Model | Size | Speed | Quality | RAM needed |
|-------|------|-------|---------|------------|
| `tiny` | 75 MB | ~0.5s | Good | 2 GB |
| `base` | 140 MB | ~1s | Better | 4 GB |
| `small` | 460 MB | ~3s | Great | 6 GB |
| `medium` | 1.4 GB | ~8s | Excellent | 10 GB |
| `large-v3` | 3 GB | ~15s | Best | 16 GB |

Models are downloaded automatically on first use. The app grays out models your machine can't run.

---

## Settings

Each page has its own settings, right next to what they change: the shortcut and language on **Dictation**, the audio source on **Meetings**, the answer engine on **Live Assistant**, and so on. Changes apply at once.

The **Settings** page (tray icon → Settings…) has the rest:
*   **General:** start at login, the app look, notifications.
*   **Microphone:** pick and test your microphone.
*   **Speech & AI:** speech models by their real names (Whisper models on your computer; Gusan Cloud, or Mistral and Google Gemini with your own key), the AI models that write notes and answers, API keys, and the optional NVIDIA speed-up.
*   **Privacy:** Privacy mode (everything stays on this computer), saving or deleting your history, usage statistics, and hiding the dictation bar and Live Assistant from screen shares.
*   **Updates & about:** the version you have, Check now / Install update, installing updates when you quit, and Send feedback.

---

## Auto-start on Login

Turn on **Start Gusan when Windows starts** (or **when your Mac starts**) in **Settings › General**. On Windows it also shows in Task Manager › Startup apps.

**Running from source on Windows** - run once:
```powershell
powershell -ExecutionPolicy Bypass -File setup_autostart.ps1
```

---

## For Developers - Run from Source

### Requirements
- Python 3.9+
- Windows 10/11 or macOS 12+
- A working microphone

### Windows
```bash
git clone https://github.com/Aram2K/transcribe-app.git
cd transcribe-app
powershell -ExecutionPolicy Bypass -File setup.ps1
# then:
run.bat
```

### macOS
```bash
git clone https://github.com/Aram2K/transcribe-app.git
cd transcribe-app
bash setup_mac.sh
# then:
bash run.sh
```

### Configuration
Copy `config.example.json` → `config.json` and edit. The file is git-ignored (it contains your API key).

---

## Building Distributables

### Windows `.exe`
```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
# Output: dist\TranscribeApp\TranscribeApp.exe  (folder, not single file)
```

The exe keeps its `TranscribeApp.exe` name so installed copies, pins and updates keep working; Windows shows it as **Gusan** from its version info.

### Automated CI (GitHub Actions)
Push a version tag that matches `APP_VERSION` in `app_info.py` to trigger automatic builds for both platforms (see [RELEASING.md](RELEASING.md)):
```bash
git tag v2.0.0
git push origin v2.0.0
```
GitHub Actions builds `TranscribeApp-Windows-Setup.exe`, `TranscribeApp-Windows.zip` and `TranscribeApp-Mac.dmg` and creates a public release automatically.

---

## Roadmap

- [x] Local offline transcription (Whisper AI)
- [x] Cloud transcription backends
- [x] Auto language detection
- [x] Smart paste at cursor
- [x] Streaming / chunked transcription
- [x] System tray, one main window with pages, settings on each page
- [x] Custom vocabulary / prompt (Dictionary)
- [x] macOS support
- [x] GitHub Actions CI - auto-builds .exe and .dmg on tag
- [x] Meeting recording with speaker labels, AI notes, Notion export
- [x] Audio to text - audio/video file → transcript and Word document
- [x] Live Assistant - private, screen-share-excluded live copilot with streamed answers
- [x] Export history to CSV / TXT; meeting recordings to MP3 / WAV
- [ ] One-click `.dmg` installer (macOS)
- [ ] Configurable silence detection (auto-stop)
- [ ] Two-model live ASR (sub-second partials) and speculative suggestions

---

## License

**Gusan** (formerly Transcribe) is free for personal and non-commercial use.

You **may**:
- Use it for personal dictation, study, or research
- Modify and share it (with attribution)

You **may not**:
- Sell it, sublicense it, or use it in a paid product or service
- Generate revenue from it in any way

See the full [LICENSE](LICENSE) for details. For commercial licensing inquiries: **aramatamian15@gmail.com**

---

## Author

Created by **[Aram Adamyan](https://www.linkedin.com/in/aram-adamyan-2k/)**, Founder of [Aibuben.xyz](https://aibuben.xyz).

[Aibuben.xyz](https://aibuben.xyz) · [LinkedIn](https://www.linkedin.com/in/aram-adamyan-2k/) · [GitHub](https://github.com/Aram2K) · [Report an issue](https://github.com/Aram2K/transcribe-app/issues)

---

<div align="center">
<sub>Made with ❤️ for anyone who types too slowly.</sub>
</div>
