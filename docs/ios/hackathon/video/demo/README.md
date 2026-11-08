# Gusan iPhone keyboard — click-by-click demo

Self-contained HTML/CSS/JS animation (no network, fonts and logo are embedded) of one
scenario on the Gusan keyboard, built to be played live during the judging presentation.

**Scenario (~65 s).** WhatsApp-style chat. Anna asks for a good used iPhone 13 under €400
in Paris → the user switches the strip chip to **Agent**, taps the blue mic and speaks →
the Agent card (research, read-only) collapses into the Dynamic Island / Live Activity and
shows its steps (Opening Leboncoin → Searching → Comparing 12 listings → Writing up the
answer) while the user keeps chatting → **Result ready → Insert** pastes three Leboncoin
listings into the composer → send. Then **Smart**: the user types a rough reply, selects it,
taps the mic and says “Make it friendlier and shorter” → the selection is replaced, with an
**Undo** chip.

## Play it live

Open `demo.html` in Chrome/Safari (double-click the file, press `F` for fullscreen). It opens
paused on the first frame; press `Space` (or click anywhere) to start. It plays once and stops on
the last frame. Add `?autoplay=1` to the URL to start automatically.

| Key | Action |
|---|---|
| `Space` or click | pause / play |
| `→` / `←` | jump to the next / previous step (the 16 captions) |
| `↑` / `↓` | nudge 1 s forward / back |
| `Home` / `R` | restart |
| `End` | jump to the end |
| `F` | toggle fullscreen |
| `H` | hide/show the HUD (progress bar + key hints) |

The timeline is deterministic (`render(t)`), so pausing on a step and narrating, then
pressing `→`, always produces the same frames. Click/drag the progress bar to scrub.

`demo.html?layout=portrait` gives a 1080×1920 vertical version (phone + captions below)
for a portrait screen or a reel.

## Files

- `demo.html` — the animation (landscape 1920×1080 by default, `?layout=portrait` for 1080×1920).
- `render.py` — exports MP4s and key frames with Playwright + ffmpeg (see docstring).
- `demo-full.mp4` — `../intro.mp4` + landscape demo + `../outro.mp4`, H.264 30 fps, 1920×1080.
- `demo-portrait.mp4` — standalone 1080×1920 export.
- `keyframe-*.png` — stills used in the PR / slides.

## Re-export

```bash
pip install playwright && playwright install chromium   # once
python render.py --full
```

What is shown follows `../../PRODUCT_DESCRIPTION.md` and the v4 design
(`docs/ios/design/v4/`): Smart writes at the cursor with Undo; Agent runs a read-only
research task through the H Company agent, reports progress in the Dynamic Island / Live
Activity, and offers Insert when the result is ready. Listings, prices and ad numbers in the
demo are illustrative.
