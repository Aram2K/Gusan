"""Phrase-level batch speech-to-text: could a stronger batch engine re-transcribe each phrase the live
interpreter cuts (hy, ka ... where live Scribe is weak and invents words), and how long would it take?

Each script turn's audio is cut from its own clean track (start - 0.15 s .. end + 0.25 s, like a phrase
the live STT commits after its pause) and sent to the bench function's batch `stt` - one request per
phrase, as the app would. Per engine / mode / language: WER, CER, words added beyond the script
(made-up text), and the request time (p50 / p90).

    python run_phrase_stt.py                                   # hy, ka (both directions), scribe_v2 + openai
    python run_phrase_stt.py --langs hy,ka,ru,en --engines scribe_v2,openai,voxtral --modes fixed,auto
Writes results/phrase_stt_<stamp>.json and prints a table. Costs: audio time per engine (no TTS).
"""
import argparse
import collections
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bench_client  # noqa: E402
import benchaudio  # noqa: E402
import metrics  # noqa: E402

AUDIO = HERE / "corpus" / "audio"
RESULTS = HERE / "results"
SR = 16000
PRE_S, POST_S = 0.15, 0.25


def pct(values, q):
    v = sorted(values)
    if not v:
        return None
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--langs", default="hy,ka")
    ap.add_argument("--engines", default="scribe_v2,openai")
    ap.add_argument("--modes", default="fixed,auto")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args(argv)
    langs = [x for x in a.langs.split(",") if x]
    engines = [x for x in a.engines.split(",") if x]
    modes = [x for x in a.modes.split(",") if x]
    bench = bench_client.BenchClient()
    jobs = []
    for folder in sorted(AUDIO.iterdir()):
        tl_path = folder / "timeline.json"
        if not tl_path.exists():
            continue
        tl = json.loads(tl_path.read_text(encoding="utf-8"))
        tracks = {}
        for t in tl["turns"]:
            if t["lang"] not in langs:
                continue
            track = t["track"]
            if track not in tracks:
                x, _sr = benchaudio.read_wav(folder / f"{track}.wav")
                tracks[track] = x
            x = tracks[track]
            a0, a1 = max(0, int((t["start"] - PRE_S) * SR)), min(len(x), int((t["end"] + POST_S) * SR))
            wav = benchaudio.wav_bytes(x[a0:a1])
            for engine in engines:
                for mode in modes:
                    jobs.append({"conv": tl["id"], "i": t["i"], "lang": t["lang"], "langs": t.get("langs") or [],
                                 "mixed": len(set(t.get("langs") or [])) > 1, "track": track, "text": t["text"],
                                 "engine": engine, "mode": mode, "seconds": round((a1 - a0) / SR, 2), "wav": wav})
    print(f"{len(jobs)} phrase requests: languages {langs}, engines {engines}, modes {modes}")

    def run(job):
        lang = job["lang"] if job["mode"] == "fixed" else None
        try:
            r = bench.stt(job["wav"], job["engine"], language=lang)
            hyp = r.get("text") or ""
            return dict(job, wav=None, hyp=hyp, ms=r.get("client_ms"), provider_ms=r.get("provider_ms"),
                        language_sent=r.get("language_sent"), error=None)
        except Exception as e:  # noqa: BLE001
            return dict(job, wav=None, hyp="", ms=None, error=str(e)[:200])

    with ThreadPoolExecutor(a.workers) as ex:
        out = list(ex.map(run, jobs))
    rows = collections.defaultdict(list)
    for r in out:
        r.pop("wav", None)
        if r["error"]:
            r.update(wer=None, cer=None, added=None)
        else:
            r["wer"] = metrics.wer(r["text"], r["hyp"], r["lang"])
            r["cer"] = metrics.cer(r["text"], r["hyp"], r["lang"])
            nr = len(metrics.normalize(r["text"], r["lang"]).split())
            nh = len(metrics.normalize(r["hyp"], r["lang"]).split())
            r["added"] = max(0, nh - nr)
        rows[(r["lang"], r["engine"], r["mode"])].append(r)
    print("\nlang engine     mode   phrases  WER    CER   made-up(+3 words)  p50 s  p90 s  errors")
    summary = []
    for (lang, engine, mode), rs in sorted(rows.items()):
        ok = [r for r in rs if not r["error"]]
        w = statistics.mean(r["wer"] for r in ok) if ok else None
        c = statistics.mean(r["cer"] for r in ok) if ok else None
        madeup = sum(1 for r in ok if r["added"] >= 3)
        ms = [r["ms"] / 1000.0 for r in ok if r["ms"] is not None]
        s = {"lang": lang, "engine": engine, "mode": mode, "n": len(rs), "wer": w, "cer": c, "made_up": madeup,
             "p50_s": pct(ms, 0.5), "p90_s": pct(ms, 0.9), "errors": len(rs) - len(ok)}
        summary.append(s)
        print("%-4s %-10s %-6s %5d   %5.1f%% %5.1f%%   %3d              %5.2f  %5.2f  %3d" % (
            lang, engine, mode, len(rs), 100 * (w or 0), 100 * (c or 0), madeup, s["p50_s"] or 0, s["p90_s"] or 0,
            s["errors"]))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = RESULTS / f"phrase_stt_{stamp}.json"
    path.write_text(json.dumps({"kind": "phrase_stt", "stamp": stamp, "langs": langs, "engines": engines,
                                "modes": modes, "summary": summary, "rows": out}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"results: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
