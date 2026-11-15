# Gusan iPhone keyboard — click-by-click demo

Self-contained HTML/CSS/JS animation (no network, fonts and logo are embedded) of one
scenario on the Gusan keyboard, built to be played live during the judging presentation.

**Scenario (~96 s, narrated).** WhatsApp-style chat. Anna asks for a good used iPhone 13 under €400
in Paris → the user switches the strip chip to **Agent**, taps the blue mic and speaks →
the Agent card (research, read-only) collapses into the Dynamic Island / Live Activity and
shows its steps (Opening Leboncoin → Searching → Comparing 12 listings → Writing up the
answer) while the user keeps chatting → **Result ready → Insert** pastes three Leboncoin
listings into the composer → send. Then **Smart**: the user types a rough reply, selects it,
taps the mic and says “Make it friendlier and shorter” → the selection is replaced, with an
**Undo** chip.

A warm English voice-over narrates each of the 17 steps (one sentence per caption, script in
`audio/script.json`) over an uplifting music bed that ducks under the voice.

## Play it live

Open `demo.html` in Chrome/Safari (double-click the file, press `F` for fullscreen). It opens
paused on the first frame; press `Space` (or click anywhere) to start. It plays once and stops on
the last frame. Add `?autoplay=1` to the URL to start automatically.

**Sound.** The voice-over + music are embedded in the page but start **muted** (browsers block
autoplay with sound). Click **Unmute** in the HUD or press `M` once — the audio stays in sync
with play/pause, stepping and scrubbing, so you can also keep it muted and narrate yourself.

| Key | Action |
|---|---|
| `Space` or click | pause / play |
| `→` / `←` | jump to the next / previous step (the 17 captions) |
| `↑` / `↓` | nudge 1 s forward / back |
| `Home` / `R` | restart |
| `End` | jump to the end |
| `F` | toggle fullscreen |
| `H` | hide/show the HUD (progress bar + key hints) |
| `M` or Unmute button | sound on / off |

The timeline is deterministic (`render(t)`), so pausing on a step and narrating, then
pressing `→`, always produces the same frames. Click/drag the progress bar to scrub.

`demo.html?layout=portrait` gives a 1080×1920 vertical version (phone + captions below)
for a portrait screen or a reel.

## Files

- `demo.html` — the animation (landscape 1920×1080 by default, `?layout=portrait` for 1080×1920).
- `render.py` — exports MP4s and key frames with Playwright + ffmpeg (see docstring).
- `demo-full.mp4` — `../intro.mp4` + landscape demo + `../outro.mp4`, H.264 30 fps, 1920×1080.
- `demo-portrait.mp4` — standalone 1080×1920 export.
- `demo-full.mp4` / `demo-portrait.mp4` carry the voice-over + music as AAC.
- `keyframe-*.png` — stills used in the PR / slides.
- `audio/` — the sound:
  - `script.json` — the 17 narration lines (one per caption) and the caption time they belong to.
  - `narration.mp3` — voice only; `demo-audio.mp3` — voice + music for the 96 s demo (this is what
    `demo.html` embeds); `full-audio.mp3` — the same with music continuing under the 15 s intro and outro.
  - `timeline.json` — how each caption step was stretched so its line fits (`SEG` in `demo.html`).
  - `make_audio.py` — regenerates all of the above (`tts` → `timeline` → `mix`); `music.py` — the music.

## Re-export

```bash
pip install playwright && playwright install chromium   # once
python render.py --full       # muxes audio/demo-audio.* and audio/full-audio.* as AAC
```

To change the narration, edit `audio/script.json`, then `python audio/make_audio.py all` (needs
`pip install edge-tts numpy`; `tts` uses the free Microsoft Edge neural voice `en-US-AriaNeural`
through the `edge-tts` package — no key; `timeline` re-stretches the steps and rewrites `SEG`/`DUR`
in `demo.html`; `mix` rebuilds the MP3s and re-embeds `demo-audio.mp3`) and re-export.

## Audio credits / licence

- Music: generated procedurally by `audio/music.py` (a 124 BPM C–G–Am–F synth loop: pad, pluck
  arpeggio, bass, kick/hat/clap, lead from bar 5). No samples or third-party recordings; released
  as CC0 / public domain together with this repo. Mixed at about −10 dBFS, ducked to −18 dBFS
  under the voice, 1.5 s fade in / 2.5 s fade out.
- Voice: synthetic (`en-US-AriaNeural` via `edge-tts`), not a recording of a person.
- `../intro.mp4` and `../outro.mp4` have no audio track of their own, so the music bed runs under them.

What is shown follows `../../PRODUCT_DESCRIPTION.md` and the v4 design
(`docs/ios/design/v4/`): Smart writes at the cursor with Undo; Agent runs a read-only
research task through the H Company agent, reports progress in the Dynamic Island / Live
Activity, and offers Insert when the result is ready. Listings, prices and ad numbers in the
demo are illustrative.
