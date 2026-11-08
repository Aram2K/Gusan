"""Render the click-by-click demo to MP4 (and key-frame PNGs).

Requires: Python `playwright` (+ `playwright install chromium`) and ffmpeg.
  python render.py                 # demo-landscape.mp4 (1920x1080), demo-portrait.mp4 (1080x1920), key frames
  python render.py --stills 3.9 7  # only PNG stills at the given times (seconds) into stills/
  python render.py --full          # also concatenate ../intro.mp4 + landscape demo + ../outro.mp4 -> demo-full.mp4
"""
import subprocess, sys
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
FPS = 30
KEYFRAMES = {"1-agent-chip": 4.8, "2-listening": 10.5, "3-live-activity": 24.0, "4-result-insert": 35.0, "5-inserted": 38.0, "6-smart-undo": 59.5}
LAYOUTS = {"landscape": (1920, 1080), "portrait": (1080, 1920)}

def url(layout):
    return (HERE / "demo.html").as_uri() + f"?render=1&layout={layout}"

def open_page(browser, layout):
    w, h = LAYOUTS[layout]
    page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
    page.goto(url(layout))
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(300)
    return page

def encode(page, out, duration):
    enc = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-vcodec", "png", "-framerate", str(FPS), "-i", "-",
                            "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE)
    frames = int(duration * FPS)
    for f in range(frames):
        page.evaluate("t => render(t)", f / FPS)
        enc.stdin.write(page.screenshot())
        if f % 300 == 0:
            print(f"  {out.name}: {f}/{frames}", flush=True)
    enc.stdin.close()
    if enc.wait():
        raise RuntimeError("ffmpeg failed")

def main(argv):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        if "--stills" in argv:
            times = [float(x) for x in argv[argv.index("--stills") + 1:] if not x.startswith("--")]
            layout = "portrait" if "--portrait" in argv else "landscape"
            page = open_page(browser, layout)
            (HERE / "stills").mkdir(exist_ok=True)
            for t in times:
                page.evaluate("t => render(t)", t)
                page.screenshot(path=str(HERE / "stills" / f"{layout}-{t:05.1f}.png"))
            return
        for layout in LAYOUTS:
            page = open_page(browser, layout)
            duration = page.evaluate("DUR")
            if layout == "landscape":
                for name, t in KEYFRAMES.items():
                    page.evaluate("t => render(t)", t)
                    page.screenshot(path=str(HERE / f"keyframe-{name}.png"))
            encode(page, HERE / f"demo-{layout}.mp4", duration)
            print(f"{layout}: {duration}s rendered", flush=True)
        browser.close()
    if "--full" in argv:
        parts = [HERE.parent / "intro.mp4", HERE / "demo-landscape.mp4", HERE.parent / "outro.mp4"]
        inputs = sum((["-i", str(x)] for x in parts), [])
        subprocess.check_call(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]",
                               "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p", "-r", str(FPS), "-movflags", "+faststart", str(HERE / "demo-full.mp4")])
        print("demo-full.mp4 written", flush=True)

if __name__ == "__main__":
    main(sys.argv[1:])
