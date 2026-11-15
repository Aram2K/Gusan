"""Render deterministic 15-second segments. Requires playwright + Chromium and ffmpeg."""
from pathlib import Path
import subprocess
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
IOS = HERE.parents[1]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    for state in ("idle", "listen", "done"):
        page.goto((IOS / "design/v3/Gusan-iPhone-Design-v3.html").as_uri() + "?only=kb-" + state)
        page.locator(".show .phone").screenshot(path=str(HERE / (state + ".png")))
    page.goto((HERE / "animation.html").as_uri())
    page.evaluate("document.fonts.ready")
    for segment in ("intro", "outro"):
        encoder = subprocess.Popen([
            "ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-vcodec", "png",
            "-framerate", "30", "-i", "-", "-an", "-c:v", "libx264", "-preset", "fast",
            "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(HERE / (segment + ".mp4"))], stdin=subprocess.PIPE)
        for frame in range(450):
            page.evaluate("([s,t]) => render(s,t)", [segment, frame / 30])
            png = page.screenshot()
            encoder.stdin.write(png)
            if frame == (390 if segment == "intro" else 390):
                (HERE / (segment + "-still.png")).write_bytes(png)
        encoder.stdin.close()
        if encoder.wait():
            raise RuntimeError("ffmpeg failed")
        print(segment + ": 450 frames rendered (15s, 1920x1080, 30fps)", flush=True)
    browser.close()
