# Hackathon bookends

Two silent, 15-second MP4s: 1920×1080, 30 fps, H.264/yuv420p, fast-start.
Designed as intro/outro around the live device demo, not as footage of a shipping iOS app.

## Re-render

Install Python `playwright`, run `playwright install chromium`, and install ffmpeg.
Then run `python docs/ios/hackathon/video/render.py` from any working directory.
The renderer captures the idle, listening and inserted phone frames directly from
`design/v3/Gusan-iPhone-Design-v3.html` with headless Chromium, then advances the
animation at exact 1/30-second intervals and pipes PNG frames into ffmpeg.
The supplied `design/drafts/icon160.png` is referenced unchanged.

Intro: 0–5 s app-switching pain; 5–8 s keyboard idle; 8–11 s listening with Dynamic
Island glow; 11–15 s result inserted. Outro: 0–7 s architecture; 7–11 s credential
privacy; 11–15 s logo and tagline. Architecture is conceptual, not a literal network
sequence. No music or voiceover is included, so the live demo can keep one soundtrack.
