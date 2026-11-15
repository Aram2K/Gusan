"""Voice-over + music for the demo. Everything is regenerated from script.json:

  python make_audio.py tts        # edge-tts (free, no key; needs network once) -> lines/NN.wav, durations into timeline.json
  python make_audio.py timeline   # stretch the caption steps so each voice line fits -> timeline.json + SEG/DUR in ../demo.html
  python make_audio.py mix        # music.py (procedural, CC0) + narration -> narration.mp3, demo-audio.mp3, full-audio.mp3,
                                  # and re-embed demo-audio.mp3 into ../demo.html
  python make_audio.py all

Requires ffmpeg, numpy, edge-tts (`pip install edge-tts numpy`). The checked-in MP3s are the output of `all`.
"""
import asyncio, base64, json, re, subprocess, sys, wave
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
DEMO = HERE.parent / "demo.html"
LINES = HERE / "lines"
SR = 48000
OLD_DUR = 64.5          # length of the un-narrated animation (the EV timeline in demo.html)
LEAD = 0.35             # voice starts this long after the caption appears
VOICE = "en-US-AriaNeural"
INTRO = OUTRO = 15.0    # ../intro.mp4 and ../outro.mp4
PY = sys.executable

def script():
    return json.load(open(HERE / "script.json", encoding="utf-8"))

def ffprobe_dur(p):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)]))

def tts():
    import edge_tts
    LINES.mkdir(exist_ok=True)
    async def one(l):
        await edge_tts.Communicate(l["text"], VOICE).save(str(LINES / f"{l['id']}.mp3"))
    async def run():
        await asyncio.gather(*(one(l) for l in script()))
    asyncio.run(run())
    for l in script():
        trim = "silenceremove=start_periods=1:start_threshold=-45dB,areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse"
        subprocess.check_call(["ffmpeg", "-y", "-loglevel", "error", "-i", str(LINES / f"{l['id']}.mp3"), "-af", trim, "-ar", str(SR), "-ac", "1", str(LINES / f"{l['id']}.wav")])
    print("tts: ", len(script()), "lines")

def timeline():
    L = script(); seg = []; new = 0.0
    for i, l in enumerate(L):
        old = l["caption_at"]; nxt = L[i + 1]["caption_at"] if i + 1 < len(L) else OLD_DUR
        gap = nxt - old; dur = ffprobe_dur(LINES / f"{l['id']}.wav")
        need = round(max(gap, dur + LEAD + (1.2 if i == len(L) - 1 else 0.5)) * 10) / 10
        seg.append({"id": l["id"], "old": old, "new": round(new, 1), "oldLen": round(gap, 2), "newLen": need, "voiceAt": round(new + LEAD, 2), "dur": round(dur, 2)})
        new += need
    T = {"OLD_DUR": OLD_DUR, "NEW_DUR": round(new, 1), "seg": seg}
    json.dump(T, open(HERE / "timeline.json", "w"), indent=1)
    h = DEMO.read_text(encoding="utf-8")
    SEG = json.dumps([[s["new"], s["old"], s["newLen"], s["oldLen"]] for s in seg])
    h, n1 = re.subn(r"const SEG = \[.*?\];", f"const SEG = {SEG};", h, count=1, flags=re.S)
    h, n2 = re.subn(r"const DUR = [0-9.]+;", f"const DUR = {T['NEW_DUR']};", h, count=1)
    assert n1 == n2 == 1
    DEMO.write_text(h, encoding="utf-8")
    print("timeline: demo is now", T["NEW_DUR"], "s")
    return T

def rd(p):
    with wave.open(str(p)) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32767
        return a.reshape(-1, 2) if w.getnchannels() == 2 else a

def wr(p, a):
    a = np.clip(a, -1, 1)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1 if a.ndim == 1 else a.shape[1]); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((a * 32767).astype(np.int16).tobytes())

def db(x): return 10 ** (x / 20)

def narration(T, total, offset):
    out = np.zeros(int(total * SR), np.float32)
    for s in T["seg"]:
        v = rd(LINES / f"{s['id']}.wav"); i = int((offset + s["voiceAt"]) * SR)
        out[i:i + len(v)] += v[:len(out) - i]
    return out * db(-1) / (np.max(np.abs(out)) + 1e-9)        # voice peak -1 dBFS

def duck(voice, base_db=-10, duck_db=-18, att=0.15, rel=0.9):
    """Music gain envelope: base_db (peak) when alone, duck_db under narration."""
    hop = int(0.02 * SR); n = len(voice) // hop + 1
    act = np.array([np.max(np.abs(voice[i * hop:(i + 1) * hop]), initial=0) for i in range(n)]) > 0.02
    g = np.empty(n); cur = 0.0
    for i, a in enumerate(act):
        tgt = 1.0 if a else 0.0; cur += (tgt - cur) * min(1, 0.02 / (att if tgt > cur else rel)); g[i] = cur
    return np.interp(np.arange(len(voice)), np.arange(n) * hop, db(base_db + (duck_db - base_db) * g)).astype(np.float32)

def build(T, total, voice_offset, tmp):
    subprocess.check_call([PY, str(HERE / "music.py"), str(tmp), str(total)])
    m = rd(tmp)[:int(total * SR)]; m /= np.max(np.abs(m)) + 1e-9
    v = narration(T, total, voice_offset)
    return v, m * duck(v)[:len(m), None] + v[:len(m), None]

def mp3(wav, out, kbps):
    subprocess.check_call(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", f"{kbps}k", str(out)])

def mix():
    T = json.load(open(HERE / "timeline.json")); D = T["NEW_DUR"]; tmp = HERE / "_music.wav"
    v, demo = build(T, D, 0.0, tmp)
    _, full = build(T, INTRO + D + OUTRO, INTRO, tmp)
    for name, a, k in [("narration", v, 96), ("demo-audio", demo, 128), ("full-audio", full, 128)]:
        wr(HERE / f"{name}.wav", a); mp3(HERE / f"{name}.wav", HERE / f"{name}.mp3", k)
    tmp.unlink()
    b = base64.b64encode((HERE / "demo-audio.mp3").read_bytes()).decode()
    h, n = re.subn(r"data:audio/mpeg;base64,[A-Za-z0-9+/=_]*", "data:audio/mpeg;base64," + b, DEMO.read_text(encoding="utf-8"), count=1)
    assert n == 1; DEMO.write_text(h, encoding="utf-8")
    print(f"mix: demo-audio {D}s, full-audio {INTRO + D + OUTRO}s, embedded {len(b) // 1024} KB into demo.html")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("tts", "all"): tts()
    if cmd in ("timeline", "all"): timeline()
    if cmd in ("mix", "all"): mix()
