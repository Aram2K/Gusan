"""Offline self-test of the bench: no network (a local fake server on 127.0.0.1 at most), and
nothing is written outside one temporary folder.

    python selftest_offline.py            -> "selftest_offline: N checks passed" or the failures
    python selftest_offline.py --keep     keep the temporary folder to look at

What it checks:
  metrics      known WER / CER / chrF values (English, Armenian, Japanese), normalisation, the
               bit-parallel edit distance against a plain DP, transcript-vs-timeline scoring
  budget       CharBudget stops before going over (run and month), the ledger, the TTS cache
  audio        WAV round trip, trimming, SNR, the room leak, resampling, the fake voices' pitch
  generate     --dry-run on two conversations (the corpus's when there are any, else two inline
               samples): files, the timeline contract, tracks hold the right speakers, noise at
               the asked SNR, the speakers leak, cache hits cost nothing, --max-chars stops early
  client       BenchClient against a local fake function: retries on 503 / 429, a streamed
               translation split inside a UTF-8 character with a think block, one try when asked
               (the interpreter runs ask so, like the app), judge batches of 20, clear errors for a
               wrong token, no token and a stubbed (410) function
  run_stt      the fake engines over the dry-run audio: results contract, sane scores, echo words
               only in speakers mode, multi mode only on Gladia
  report       tables for both kinds and the diff against the previous results
"""
import argparse
import base64
import collections
import http.server
import json
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import numpy as np

import bench_client as bc
import benchaudio as ba
import generate
import metrics
import report
import run_stt

HERE = Path(__file__).resolve().parent
TOKEN = "selftest-token-0123456789abcdef"

SAMPLES = [
    {"id": "t01_en_hy_sample", "title": "Sample standup", "scenario": "inline sample", "tags": ["sample", "mixed"],
     "me": "aram", "my_lang": "en", "their_lang": "hy",
     "speakers": {"aram": {"name": "Aram", "gender": "male", "voice": "nPczCjzI2devNBz1zQrb", "accent_note": ""},
                  "anna": {"name": "Anna", "gender": "female", "voice": "cgSgspJ2msm6clMCkdW9", "accent_note": ""}},
     "glossary": ["Gusan"],
     "turns": [
         {"speaker": "anna", "lang": "hy", "langs": ["hy"], "text": "Բարև, Արամ։ Ինչպե՞ս ես։",
          "ref": {"en": "Hi, Aram. How are you?"}, "gap_before": 0.0, "notes": ""},
         {"speaker": "aram", "lang": "en", "langs": ["en"], "text": "Hi Anna, I am fine. Did the Gusan build pass?",
          "ref": {"hy": "Բարև, Աննա, լավ եմ։ Gusan-ի build-ը անցա՞վ։", "en": "Hi Anna, I am fine. Did the Gusan build pass?"},
          "gap_before": 0.4, "notes": ""},
         {"speaker": "anna", "lang": "hy", "langs": ["hy", "en"],
          "text": "Այո, build-ը անցավ, բայց release notes-ը դեռ պատրաստ չեն։",
          "ref": {"en": "Yes, the build passed, but the release notes are not ready yet."}, "gap_before": -0.3, "notes": ""},
         {"speaker": "aram", "lang": "en", "langs": ["en"], "text": "Okay, send them to me by Friday.",
          "ref": {"hy": "Լավ, ուրբաթ օրը ինձ ուղարկիր։", "en": "Okay, send them to me by Friday."}, "gap_before": 0.5,
          "notes": ""}]},
    {"id": "t02_en_ja_sample", "title": "Sample pilot call", "scenario": "inline sample", "tags": ["sample", "ja"],
     "me": "maria", "my_lang": "en", "their_lang": "ja",
     "speakers": {"maria": {"name": "Maria", "gender": "female", "voice": "Xb7hH8MSUJpSbSDYk0k2", "accent_note": ""},
                  "kenji": {"name": "Kenji", "gender": "male", "voice": "JBFqnCBsd6RMkjVDRZzb", "accent_note": ""}},
     "glossary": ["Gusan", "Slack"],
     "turns": [
         {"speaker": "kenji", "lang": "ja", "langs": ["ja"], "text": "おはようございます。今日の会議は何時からですか？",
          "ref": {"en": "Good morning. What time does today's meeting start?"}, "gap_before": 0.2, "notes": ""},
         {"speaker": "maria", "lang": "en", "langs": ["en"], "text": "It starts at ten. I will share the Gusan demo.",
          "ref": {"ja": "十時に始まります。Gusanのデモを共有します。", "en": "It starts at ten. I will share the Gusan demo."},
          "gap_before": 0.5, "notes": ""},
         {"speaker": "kenji", "lang": "ja", "langs": ["ja", "en"], "text": "了解です。demoのリンクをSlackで送ってください。",
          "ref": {"en": "Understood. Please send the demo link on Slack."}, "gap_before": -0.4, "notes": ""}]},
]


class Checks:
    def __init__(self):
        self.n = 0
        self.failed = []
        self.section = ""

    def ok(self, cond, what):
        self.n += 1
        if not cond:
            self.failed.append(f"[{self.section}] {what}")
            print(f"  FAIL [{self.section}] {what}")
        return bool(cond)

    def eq(self, a, b, what):
        return self.ok(a == b, f"{what}: {a!r} != {b!r}")

    def close(self, a, b, tol, what):
        return self.ok(a is not None and abs(a - b) <= tol, f"{what}: {a!r} not within {tol} of {b!r}")


def _quiet(*_a, **_k):
    pass


# -- metrics --------------------------------------------------------------------------------

def t_metrics(c, _tmp):
    c.close(metrics.wer("the cat sat on the mat", "the cat sit on mat"), 2 / 6, 1e-9, "English WER")
    c.eq(metrics.wer("Hello, World!", "hello world"), 0.0, "case and punctuation ignored")
    c.close(metrics.cer("hello world", "helo world"), 0.1, 1e-9, "English CER (spaces ignored)")
    # Armenian: ։ ՞ and the և ligature
    c.eq(metrics.normalize("Բարև, Արամ։ Ինչպե՞ս ես։"), "բարեւ արամ ինչպես ես", "Armenian normalisation")
    c.eq(metrics.normalize("ինչո՞ւ"), metrics.normalize("ինչու"), "Armenian question mark inside a word")
    c.eq(metrics.wer("Բարև, ինչպե՞ս ես։", "բարեւ ինչպես ես"), 0.0, "Armenian WER 0")
    c.close(metrics.wer("Բարև, ինչպե՞ս ես։", "Բարև ինչպես"), 1 / 3, 1e-9, "Armenian WER one deletion")
    c.close(metrics.cer("ինչպես ես", "ինչպէս ես"), 1 / 8, 1e-9, "Armenian CER one substitution")
    # Japanese: characters are the units
    c.eq(metrics.units(metrics.normalize("今日は晴れです。")), list("今日は晴れです"), "Japanese units")
    c.close(metrics.wer("今日は晴れです。", "今日は雨です"), 2 / 7, 1e-9, "Japanese WER (= CER)")
    c.close(metrics.cer("今日は晴れです。", "今日は雨です"), 2 / 7, 1e-9, "Japanese CER")
    c.eq(metrics.units(metrics.normalize("了解です。demoのリンクをSlackで送って")),
         ["了", "解", "で", "す", "demo", "の", "リ", "ン", "ク", "を", "slack", "で", "送", "っ", "て"],
         "mixed ja/en units")
    c.eq(metrics.primary_rate("ja", 0.5, 0.2), 0.2, "CER is primary for Japanese")
    c.eq(metrics.primary_rate("hy", 0.5, 0.2), 0.5, "WER is primary for Armenian")
    # numbers, quotes, Arabic marks
    c.eq(metrics.normalize("It costs 1,000 dollars or 3,5 euros — “quoted” don't"),
         "it costs 1000 dollars or 3.5 euros quoted dont", "numbers, dashes, quotes, apostrophes")
    c.eq(metrics.normalize("مَرْحَبًا بِكُمْ، أهلاً"), "مرحبا بكم اهلا", "Arabic marks and alef forms")
    c.eq(metrics.normalize("[laughs] Okay [sighs]"), "okay", "audio tags dropped")
    c.eq(metrics.normalize("لدينا ١٥٠ مستخدماً و ١٬٠٠٠ و ٣٫٥"), "لدينا 150 مستخدما و 1000 و 3.5",
         "Arabic-Indic digits and separators")
    c.eq(metrics.wer("Gusan-ի build-ը անցա՞վ", "Gusanի buildը անցավ"), 0.0, "a word that changes script is split")
    c.eq(metrics.normalize("meet at 3pm, Приветmeeting"), "meet at 3 pm привет meeting", "digit / script boundaries")
    c.eq(metrics.normalize("3.5kg"), "3.5 kg", "a decimal stays one number")
    # addresses read out score like written ones (the corpus reads them out in five languages)
    for spoken, written in (("Send it to r dot moss at northpeak dot io.", "Send it to r.moss@northpeak.io."),
                            ("На billing собака nordsoft точка dev.", "На billing@nordsoft.dev."),
                            ("à claire point dubois arobase telva point fr", "à claire.dubois@telva.fr"),
                            ("laura punto vega arroba gmail punto com", "laura.vega@gmail.com"),
                            ("你邮箱，tom艾特brightloop点io，对吧？", "你邮箱，tom@brightloop.io，对吧？"),
                            ("It's aibuben dot xyz.", "It's aibuben.xyz.")):
        c.eq(metrics.normalize(spoken), metrics.normalize(written), f"address read out = written ({written})")
    c.eq([metrics.normalize(s) for s in ("Точка зрения", "三点开会", "C'est le point important", "at 9 a.m.")],
         ["точка зрения", "三点开会", "cest le point important", "at 9 a m"], "address words elsewhere stay words")
    c.eq((metrics.wer("That's M-O-S-S.", "That's MOSS."), metrics.wer("M O S S", "moss")), (0.0, 0.0),
         "a word spelled letter by letter is one word")
    # chrF (hand-computed, sacrebleu's averaging)
    c.close(metrics.chrf("abc", "abd", word_order=0), 700 / 18, 1e-6, "chrF abc/abd")
    c.close(metrics.chrf("abc", "abd"), 700 / 24, 1e-6, "chrF++ abc/abd")
    c.close(metrics.chrf("今日は晴れ", "今日は雨", lang="ja"), 37.73657, 1e-4, "chrF Japanese")
    c.eq(metrics.chrf("Բարև ձեզ", "Բարև ձեզ"), 100.0, "chrF identical Armenian")
    c.eq(metrics.chrf("something", ""), 0.0, "chrF empty hypothesis")
    c.close(metrics.corpus_chrf([("abc", "abc"), ("abc", "abc")]), 100.0, 1e-9, "corpus chrF")
    # edit distance against a plain DP
    rng = random.Random(7)

    def plain(a, b):
        prev = list(range(len(b) + 1))
        for i, x in enumerate(a, 1):
            cur = [i]
            for j, y in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
            prev = cur
        return prev[-1]
    bad = 0
    for _ in range(300):
        a = [rng.choice("abcde") for _ in range(rng.randint(0, 40))]
        b = [rng.choice("abcde") for _ in range(rng.randint(0, 40))]
        if metrics.edit_distance(a, b) != plain(a, b):
            bad += 1
        counts, ops = metrics.align(a, b)
        if counts["sub"] + counts["del"] + counts["ins"] != plain(a, b):
            bad += 1
    c.eq(bad, 0, "edit distance and alignment vs plain DP (300 random pairs)")
    c.eq(metrics.align(list("abc"), list("axcd"))[0], {"hits": 2, "sub": 1, "del": 0, "ins": 1}, "align counts")
    s = metrics.summary([1, 2, 3, 4, 5, None, "x"])
    c.eq((s["n"], s["mean"], s["p50"], s["p90"], s["p95"]), (5, 3.0, 3.0, 4.6, 4.8), "summary stats")
    c.eq(metrics.summary([])["n"], 0, "empty summary")

    # a transcript against a timeline: by word times, with an echo word, and by alignment
    turns = [{"i": 0, "speaker": "a", "lang": "en", "langs": ["en"], "text": "good morning team", "start": 0.0, "end": 1.5},
             {"i": 2, "speaker": "a", "lang": "en", "langs": ["en", "hy"], "text": "the build passed today",
              "start": 4.0, "end": 6.0}]
    other = [{"i": 1, "speaker": "b", "lang": "hy", "langs": ["hy"], "text": "Բարև ձեզ", "start": 2.0, "end": 3.5}]
    words = [{"text": "good", "start": 0.1, "end": 0.4}, {"text": "morning", "start": 0.5, "end": 0.9},
             {"text": "team", "start": 1.0, "end": 1.3}, {"text": "Բարև", "start": 2.3, "end": 2.8},
             {"text": "the", "start": 4.1, "end": 4.3}, {"text": "build", "start": 4.4, "end": 4.8},
             {"text": "past", "start": 4.9, "end": 5.3}, {"text": "today", "start": 5.4, "end": 5.9}]
    sc = metrics.score_transcript(turns, " ".join(w["text"] for w in words), words, other_turns=other)
    c.eq(sc["method"], "timing", "word times used")
    c.eq(sc["overall"]["errors"], 2, "overall errors (one substitution, one echo insertion)")
    c.eq(sc["overall"]["echo_units"], 1, "echo word found by time")
    c.eq([t["errors"] for t in sc["per_turn"]], [0, 1], "per-turn errors by time")
    c.eq(sc["mixed"]["n_turns"], 1, "mixed line counted")
    sc2 = metrics.score_transcript(turns, "good morning team Բարև the build past today", None, other_turns=other)
    c.eq(sc2["method"], "alignment", "alignment without word times")
    c.eq(sc2["overall"]["echo_units"], 1, "echo word found by alignment")
    c.eq(sum(t["errors"] for t in sc2["per_turn"]), sc2["overall"]["errors"], "per-turn errors add up")
    sc3 = metrics.score_transcript(turns, "", None)
    c.eq((sc3["overall"]["wer"], sc3["overall"]["dropped_turns"]), (1.0, 2), "empty transcript = all dropped")
    c.eq(sc2["overall"]["wer_turns"], sc2["overall"]["wer"], "turn-by-turn WER = WER without word times")
    # cross-talk written in time order: the whole-text WER counts the order, the turn-by-turn WER does not
    ta = {"i": 0, "lang": "en", "langs": ["en"], "text": "alpha beta gamma delta", "start": 0.0, "end": 2.0}
    tb = {"i": 1, "lang": "en", "langs": ["en"], "text": "one two three", "start": 1.0, "end": 2.5}
    ws = [("alpha", 0.1), ("beta", 0.5), ("one", 1.05), ("gamma", 1.25), ("two", 1.45), ("delta", 1.7), ("three", 2.1)]
    words = [{"text": w, "start": s, "end": s + 0.15} for w, s in ws]
    sc4 = metrics.score_transcript([ta, tb], " ".join(w for w, _ in ws), words)
    c.ok(sc4["overall"]["wer"] > 0 and sc4["overall"]["wer_turns"] == 0.0,
         f"cross-talk: WER {sc4['overall']['wer']}, by turn {sc4['overall']['wer_turns']}")
    c.eq(metrics.score_transcript([ta], "alpha beta", [{"text": "alpha", "start": "x", "end": None}])["method"],
         "alignment", "unusable word times fall back to the alignment")
    # the call leaking in while I speak: an echo word, not an error of my turn
    mine = {"i": 0, "lang": "en", "langs": ["en"], "text": "good morning team", "start": 0.0, "end": 1.5}
    call = {"i": 1, "lang": "hy", "langs": ["hy"], "text": "Բարև ձեզ", "start": 0.5, "end": 2.0}
    ws = [("good", 0.1), ("Բարև", 0.6), ("morning", 0.7), ("team", 1.0)]
    sc5 = metrics.score_transcript([mine], " ".join(w for w, _ in ws),
                                   [{"text": w, "start": s, "end": s + 0.2} for w, s in ws], other_turns=[call])
    c.eq((sc5["overall"]["echo_units"], sc5["per_turn"][0]["errors"], sc5["overall"]["turn_errors"]), (1, 0, 1),
         "cross-talk echo during my own turn")


# -- budget, ledger, cache ------------------------------------------------------------------

def t_budget(c, tmp):
    b = bc.CharBudget(10)
    b.charge("hello")
    c.eq(b.used, 5, "charged")
    try:
        b.charge("123456")
        c.ok(False, "over budget must raise")
    except bc.BudgetExceeded:
        c.eq(b.used, 5, "nothing charged when refused")
    b.refund(5)
    c.eq(b.remaining, 10, "refund")
    led = bc.CharLedger(tmp / "ledger.json")
    led.add(19995, "generate")
    led.add(3, "bench")
    c.eq(led.month_total(), 19998, "ledger month total")
    c.eq(led.month()["by"], {"generate": 19995, "bench": 3}, "ledger by purpose")
    mb = bc.CharBudget(1000, monthly_cap=20000, ledger=led)
    c.eq(mb.remaining, 2, "monthly cap leaves 2")
    c.ok(not mb.can_spend(3), "monthly cap refuses 3")
    k1 = bc.cache_key("eleven_v3", "v", None, "hi")
    c.eq(k1, bc.cache_key("eleven_v3", "v", "", "hi"), "cache key: None language == ''")
    c.ok(k1 != bc.cache_key("eleven_v3", "v", "en", "hi"), "cache key depends on language")
    cache = bc.TTSCache(tmp / "cache")
    fake = bc.FakeBench()
    budget = bc.CharBudget(100)
    pcm1, i1 = bc.cached_tts(fake, cache, budget, "Hello there", "EXAVITQu4vr4xnSDxMaL", "eleven_v3", "en")
    pcm2, i2 = bc.cached_tts(fake, cache, budget, "Hello there", "EXAVITQu4vr4xnSDxMaL", "eleven_v3", "en")
    c.eq((i1["cached"], i2["cached"], i2["chars"], budget.used), (False, True, 0, 11), "second clip is free")
    c.eq(pcm1, pcm2, "cached clip identical")


# -- audio ----------------------------------------------------------------------------------

def t_audio(c, tmp):
    x = (np.arange(-32768, 32768, 7, dtype=np.int64).astype(np.float32) / 32768.0)
    ba.write_wav(tmp / "rt.wav", x)
    y, sr = ba.read_wav(tmp / "rt.wav")
    c.ok(sr == 16000 and np.array_equal(x, y), "WAV round trip is exact")
    v = ba.synth_voice("hello there friend", f0=200.0, seed=1)
    t = ba.trim_silence(v)
    c.ok(len(t) < len(v) and len(t) > 0.8 * (len(v) - 0.4 * ba.SR), "trim cuts only the silence")
    rng = np.random.default_rng(3)
    speech = np.concatenate([v, v, v])
    noisy, g = ba.add_noise_at_snr(speech, ba.cafe_noise(len(speech), rng, [v]), 15.0)
    c.close(ba.snr_db(speech, noisy - speech), 15.0, 0.05, "noise at 15 dB SNR")
    ir = ba.room_ir(delay_ms=35.0, seed=2)
    c.eq(int(np.argmax(np.abs(ir))), int(round(0.035 * ba.SR)), "room IR: direct sound after 35 ms")
    leaked = ba.apply_leak(np.zeros(len(v), np.float32), v, 0.3, ir)
    c.close(ba.rms(leaked) / ba.rms(v), 0.33, 0.06, "leak level ~0.3")
    r = ba.resample(np.zeros(22050, np.float32), 22050, 16000)
    c.eq(len(r), 16000, "resample length")
    try:
        sys.path.append(str(HERE.parents[1]))
        from interpreter import pitch                       # the app's own gender guess
        f0s = {}
        for gender, vid in (("female", "EXAVITQu4vr4xnSDxMaL"), ("male", "cjVigY5qzO86Huf0OWal")):
            pcm, _ = bc.FakeBench().tts("one two three four five six seven", vid, "eleven_v3")
            a = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
            hz = [pitch.frame_pitch(a[i:i + pitch.FRAME]) for i in range(0, len(a) - pitch.FRAME, pitch.FRAME)]
            hz = [h for h in hz if h]
            f0s[gender] = float(np.median(hz)) if hz else None
        c.ok(f0s["female"] and f0s["female"] > pitch.FEMALE_ABOVE, f"fake woman's voice reads female ({f0s['female']})")
        c.ok(f0s["male"] and f0s["male"] < pitch.MALE_BELOW, f"fake man's voice reads male ({f0s['male']})")
    except ImportError:
        print("  (interpreter.pitch not importable: fake voice pitch not checked)")


# -- generate --dry-run ---------------------------------------------------------------------

def prepare_corpus(tmp):
    """Two conversations into tmp/corpus: the real corpus's first valid ones, else the samples."""
    dst = tmp / "corpus"
    (dst / "conversations").mkdir(parents=True, exist_ok=True)
    picked = []
    for _path, conv in generate.load_conversations(generate.CORPUS_DIR):
        if not generate.validate(conv)[0]:
            picked.append(conv)
        if len(picked) == 2:
            break
    source = "corpus"
    if len(picked) < 2:
        picked, source = SAMPLES, "inline samples"
    for conv in picked:
        (dst / "conversations" / f"{conv['id']}.json").write_text(json.dumps(conv, ensure_ascii=False), encoding="utf-8")
    voices = generate.load_voices(generate.CORPUS_DIR)
    if voices:
        (dst / "voices.json").write_text(json.dumps(voices), encoding="utf-8")
    return dst, [cv["id"] for cv in picked], source


def t_generate(c, tmp, state):
    corpus, ids, source = prepare_corpus(tmp)
    state.update(corpus=corpus, ids=ids)
    print(f"  generate: {', '.join(ids)} ({source})")
    audio, cache = tmp / "audio", tmp / "cache"
    common = ["--dry-run", "--corpus", str(corpus), "--audio-dir", str(audio), "--cache-dir", str(cache)]
    c.eq(_run(generate.main, common), 0, "generate --dry-run exit code")
    for cid in ids:
        folder = audio / cid
        tl = json.loads((folder / "timeline.json").read_text(encoding="utf-8"))
        conv = json.loads((corpus / "conversations" / f"{cid}.json").read_text(encoding="utf-8"))
        for key in ("id", "sample_rate", "duration", "turns", "variants", "chars"):
            c.ok(key in tl, f"{cid}: timeline has {key}")
        c.eq((tl["id"], tl["sample_rate"], tl["dry_run"]), (cid, 16000, True), f"{cid}: id, rate, dry flag")
        c.eq(tl["chars"], sum(len(t["text"]) for t in conv["turns"]), f"{cid}: chars")
        c.eq(len(tl["turns"]), len(conv["turns"]), f"{cid}: one timeline turn per turn")
        for t in tl["turns"]:
            missing = [k for k in ("i", "speaker", "track", "lang", "langs", "text", "ref", "gender", "start", "end")
                       if k not in t]
            if missing:
                c.ok(False, f"{cid}: turn {t.get('i')} lacks {missing}")
                break
        c.eq(set(tl["variants"]), {"clean", "noisy", "speakers"}, f"{cid}: variants")
        for variant, files in tl["variants"].items():
            for inp in ("mic", "system", "mix"):
                path = folder / files[inp]
                with open(path, "rb") as f:
                    head = f.read(4)
                c.ok(head == b"RIFF", f"{cid}: {variant}/{inp} is a WAV")
        mic, sr = ba.read_wav(folder / "mic.wav")
        system, _ = ba.read_wav(folder / "system.wav")
        c.eq(sr, 16000, f"{cid}: 16 kHz")
        c.close(len(mic) / sr, tl["duration"], 1.0 / sr, f"{cid}: duration")
        mask_mic = np.zeros(len(mic), bool)
        for t in tl["turns"]:
            if t["track"] == "mic":
                mask_mic[int(t["start"] * sr):int(t["end"] * sr)] = True
        c.ok(ba.rms(mic[mask_mic]) > 0.01, f"{cid}: mic holds my turns")
        c.ok(ba.rms(mic[~mask_mic]) < 0.002, f"{cid}: mic is silent outside my turns")
        c.ok(ba.rms(system[mask_mic & ~_mask(tl, len(mic), sr, "system")]) < 0.002,
             f"{cid}: the call is silent while only I speak")
        me = conv["me"]
        c.ok(all((t["track"] == "mic") == (t["speaker"] == me) for t in tl["turns"]), f"{cid}: tracks follow 'me'")
        starts = [t["start"] for t in tl["turns"]]
        c.ok(starts == sorted(starts), f"{cid}: turns keep their order")
        last_end = {}
        overlap_ok = True
        prev_end = None
        for t in tl["turns"]:
            if t["speaker"] in last_end and t["start"] < last_end[t["speaker"]]:
                overlap_ok = False
            if prev_end is not None and t["start"] < prev_end - 0.6 - 1e-6:
                overlap_ok = False
            last_end[t["speaker"]] = t["end"]
            prev_end = max(prev_end or 0, t["end"])
        c.ok(overlap_ok, f"{cid}: no self-overlap, overlaps at most 0.6 s")
        mic_n, _ = ba.read_wav(folder / "mic_noisy.wav")
        sys_n, _ = ba.read_wav(folder / "system_noisy.wav")
        if ba.active_rms(mic) > 0:
            c.close(ba.snr_db(mic, mic_n - mic), 15.0, 0.5, f"{cid}: mic noise at 15 dB")
        c.close(ba.snr_db(system, sys_n - system), 15.0, 0.5, f"{cid}: call noise at 15 dB")
        mic_sp, _ = ba.read_wav(folder / "mic_speakers.wav")
        c.close(ba.rms(mic_sp - mic) / max(1e-9, ba.rms(system)), 0.33, 0.08, f"{cid}: speakers leak ~0.3 of the call")
        c.ok(tl.get("leak", {}).get("interpreter_playback_leaks") is True and (folder / "leak_ir.npy").is_file(),
             f"{cid}: leak settings for the interpreter's own voice")
    # cache hits cost nothing
    c.eq(_run(generate.main, common), 0, "second generate exit code")
    tl = json.loads((audio / ids[0] / "timeline.json").read_text(encoding="utf-8"))
    c.eq(tl["chars_billed"], 0, "second run: every clip from the cache")
    # the budget stops before spending
    small = ["--dry-run", "--corpus", str(corpus), "--audio-dir", str(tmp / "audio_small"),
             "--cache-dir", str(tmp / "cache_small"), "--max-chars", "20"]
    c.eq(_run(generate.main, small), 0, "over-budget generate exit code")
    c.ok(not list((tmp / "audio_small").glob("*/timeline.json")), "nothing generated over budget")
    c.ok(not list((tmp / "cache_small").glob("*.pcm")), "nothing spent over budget")
    state["audio"] = audio

    # eleven_v3 refusing a language: that language moves to eleven_v4_turbo, the rest stays on v3
    class RefusingFake(bc.FakeBench):
        def tts(self, text, voice_id, model="eleven_v3", language=None, *, budget=None, **extra):
            self.calls[("tts", model, language)] += 1
            if model == "eleven_v3" and language == "hy":
                raise bc.BenchError("tts failed: elevenlabs 400", action="tts", status=502, code="elevenlabs 400")
            return super().tts(text, voice_id, model, language, budget=budget, **extra)

    sample = SAMPLES[0]
    fake = RefusingFake()
    tl = generate.generate_conversation(sample, fake, cache=bc.TTSCache(tmp / "cache_fb"), budget=bc.CharBudget(None),
                                        out_root=tmp / "audio_fb", opts=generate.Options(dry_run=True, workers=1),
                                        log=_quiet)
    models = {(t["lang"], len(t["langs"]) > 1): t["model"] for t in tl["turns"]}
    c.eq(models, {("hy", False): "eleven_v4_turbo", ("en", False): "eleven_v3", ("hy", True): "eleven_v4_turbo"},
         "v3 refused for hy -> every hy turn on v4 turbo, en stays on v3")
    c.eq(fake.calls[("tts", "eleven_v3", "hy")], 1, "v3 is not asked again for a language it refused")

    # a model that speaks a language without its code is reported; --model-for picks another model
    class DroppingFake(bc.FakeBench):
        def tts(self, text, voice_id, model="eleven_v3", language=None, *, budget=None, **extra):
            pcm, info = super().tts(text, voice_id, model, language, budget=budget, **extra)
            return pcm, dict(info, language_dropped=(model == "eleven_v3" and language == "hy"))

    notes = []
    tl3 = generate.generate_conversation(SAMPLES[0], DroppingFake(), cache=bc.TTSCache(tmp / "cache_drop"),
                                         budget=bc.CharBudget(None), out_root=tmp / "audio_drop",
                                         opts=generate.Options(dry_run=True), log=notes.append)
    c.ok(sum("did not take language 'hy'" in n for n in notes) == 1, "a dropped language code is reported once")
    c.ok(any(t.get("tts_language_dropped") for t in tl3["turns"] if t["lang"] == "hy" and len(t["langs"]) == 1),
         "the timeline marks those turns")
    opts = generate.Options(model_for={"ka": "eleven_v4_turbo"})
    c.eq((opts.models("ka"), opts.models("en")), (["eleven_v4_turbo"], ["eleven_v3", "eleven_v4_turbo"]),
         "--model-for puts a model first for one language")

    # a line said twice by the same voice is voiced (and paid) once
    import copy
    twice = copy.deepcopy(SAMPLES[0])
    twice["id"] = "t03_repeat"
    twice["turns"].append(dict(twice["turns"][3], gap_before=0.6))
    counting = bc.FakeBench()
    tl2 = generate.generate_conversation(twice, counting, cache=bc.TTSCache(tmp / "cache_rep"), budget=bc.CharBudget(None),
                                         out_root=tmp / "audio_rep", opts=generate.Options(dry_run=True), log=_quiet)
    c.eq((counting.calls["tts"], len(tl2["turns"]), tl2["chars_billed"]),
         (4, 5, sum(len(t["text"]) for t in SAMPLES[0]["turns"])), "a repeated line is voiced and paid once")
    c.eq(generate.cost(generate.plan_turns(twice, [], generate.Options()), None, generate.Options())[1],
         sum(len(t["text"]) for t in SAMPLES[0]["turns"]), "the estimate counts a repeated line once")

    # a wrong token / stubbed function stops at once (no other model is tried)
    class FatalFake(bc.FakeBench):
        def tts(self, *a, **k):
            self.calls["fatal"] += 1
            raise bc.BenchError("the bench function is stubbed (HTTP 410)", action="tts", status=410)

    fatal = FatalFake()
    try:
        generate.generate_conversation(sample, fatal, cache=bc.TTSCache(tmp / "cache_fatal"), budget=bc.CharBudget(None),
                                       out_root=tmp / "audio_fatal", opts=generate.Options(dry_run=True, workers=1),
                                       log=_quiet)
        c.ok(False, "a stubbed function must raise")
    except bc.BenchError as e:
        c.eq((e.status, fatal.calls["fatal"]), (410, 1), "410 raised after one call, no fallback")


def _mask(tl, n, sr, track):
    m = np.zeros(n, bool)
    for t in tl["turns"]:
        if t["track"] == track:
            m[int(t["start"] * sr):int(t["end"] * sr)] = True
    return m


def _run(fn, argv):
    """Run a CLI main() with its prints swallowed; returns its exit code."""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(argv)
    _run.last = buf.getvalue()
    return code


# -- the HTTP client against a local fake function -----------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def _json(self, code, obj, headers=()):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        st = self.server.state
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.headers.get("x-bench-token") != TOKEN:
            return self._json(401, {"error": "unauthorized"})
        body = json.loads(raw or b"{}")
        action = body.get("action")
        st["calls"][action] += 1
        st["bodies"].append(body)
        if st.get("gone"):
            return self._json(410, {"error": "gone"})
        if action == "ping":
            return self._json(200, {"ok": True, "providers": {"elevenlabs": True}, "server_ms": 1})
        if action == "tts":
            if body.get("text") == "REFUSE":
                return self._json(400, {"error": "bad_request", "detail": "text"})
            if body.get("text") == "V3REFUSE":
                return self._json(502, {"error": "elevenlabs 400", "detail": "unsupported language"})
            if body.get("text") == "NOKEY":
                return self._json(503, {"error": "elevenlabs not_configured", "detail": "no key"})
            if body.get("text") == "SLOW":                      # times out once, then answers
                st["slow"] = st.get("slow", 0) + 1
                if st["slow"] == 1:
                    return self._json(504, {"error": "elevenlabs timeout", "detail": "no answer within 90000 ms"})
            if body.get("text") == "STUCK":                     # always times out
                return self._json(504, {"error": "elevenlabs timeout", "detail": "no answer within 90000 ms"})
            if st["calls"]["tts"] == 1:
                return self._json(503, {"error": "elevenlabs 503"})
            pcm = (np.sin(np.arange(1600) / 5.0) * 8000).astype("<i2").tobytes()
            return self._json(200, {"audio": base64.b64encode(pcm).decode(), "sample_rate": 16000,
                                    "chars": len(body["text"]), "billed_chars": len(body["text"]), "server_ms": 12})
        if action == "translate" and body.get("text") == "BUSY":        # every engine of the chain failed
            return self._json(503, {"error": "deepseek 503", "detail": "busy"})
        if action == "translate":
            raw_text = "<think>plan</think>«Բարեւ ձեզ»".encode("utf-8")
            cut = raw_text.index("Բ".encode("utf-8")) + 3          # inside the second Armenian letter
            chunks = [raw_text[:cut - 6], raw_text[cut - 6:cut], raw_text[cut:]]
            self.send_response(200)
            for k, v in (("Content-Type", "text/plain; charset=utf-8"), ("Transfer-Encoding", "chunked"),
                         ("X-Server-Ms", "321"), ("X-Bench-Route", "deepseek"), ("X-Bench-Model", "m1"),
                         ("X-Answer-Family", "deepseek"), ("X-Bench-Attempts", '[{"route": "deepseek"}]')):
                self.send_header(k, v)
            self.end_headers()
            for ch in chunks:
                self.wfile.write(b"%X\r\n%s\r\n" % (len(ch), ch))
                self.wfile.flush()
                time.sleep(0.03)
            self.wfile.write(b"0\r\n\r\n")
            return None
        if action == "stt":
            if st["calls"]["stt"] == 1:
                return self._json(429, {"error": "busy"}, [("Retry-After", "0")])
            return self._json(200, {"text": "hello world", "language": "en", "server_ms": 50, "provider_ms": 40,
                                    "words": [{"text": "hello", "start": 0.1, "end": 0.4},
                                              {"text": "world", "start": 0.5, "end": 0.9}], "engine_model": "x"})
        if action == "judge":
            items = body.get("items") or []
            if len(items) > 20:
                return self._json(400, {"error": "bad_request", "detail": "items"})
            return self._json(200, {"scores": [{"adequacy": 80, "fluency": 90, "note": ""} for _ in items]})
        if action == "tokens":
            return self._json(200, {"tokens": {k: [{"token": f"t-{k}-{i}"} for i in range(body.get("n", 1))]
                                               for k in body.get("kinds") or []}})
        if action == "usage":
            used = 1000 + 25 * st["calls"]["stt"]
            return self._json(200, {"used": used, "limit": 20000, "left": 20000 - used, "server_ms": 1})
        return self._json(400, {"error": "bad_request", "detail": "action"})


def t_client(c, tmp):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.state = {"calls": collections.Counter(), "bodies": []}
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/speed-bench"
    ledger = tmp / "client_ledger.json"
    try:
        cl = bc.BenchClient(url, TOKEN, backoff=0.01, ledger=ledger, trust_env=False, purpose="selftest")
        c.eq(cl.ping()["ok"], True, "ping")
        budget = bc.CharBudget(100)
        pcm, info = cl.tts("Hello there", "EXAVITQu4vr4xnSDxMaL", "eleven_v3", "en", budget=budget)
        c.eq((len(pcm), info["chars"], srv.state["calls"]["tts"]), (3200, 11, 2), "tts retried once after 503")
        c.eq((budget.used, bc.CharLedger(ledger).month_total()), (11, 11), "tts charged and written to the ledger")
        try:
            cl.tts("REFUSE", "EXAVITQu4vr4xnSDxMaL", "eleven_v3", budget=budget)
            c.ok(False, "a refused tts must raise")
        except bc.BenchError as e:
            c.eq((e.status, budget.used), (400, 11), "a refused tts is refunded")
        before = srv.state["calls"]["tts"]
        try:
            cl.tts("V3REFUSE", "EXAVITQu4vr4xnSDxMaL", "eleven_v3", "hy", budget=budget)
            c.ok(False, "a provider refusal must raise")
        except bc.BenchError as e:
            c.eq((e.status, e.code, srv.state["calls"]["tts"] - before, budget.used), (502, "elevenlabs 400", 1, 11),
                 "a provider's 4xx (wrapped in 502) is not retried and is refunded")
        before = srv.state["calls"]["tts"]
        try:
            cl.tts("NOKEY", "EXAVITQu4vr4xnSDxMaL", "eleven_v3", budget=budget)
            c.ok(False, "a provider without a key must raise")
        except bc.BenchError as e:
            c.eq((e.status, srv.state["calls"]["tts"] - before, budget.used), (503, 1, 11),
                 "a provider without a key is not retried and is refunded")
        # a timed-out try may have been voiced: it counts, and the retry is paid again
        pcm, info = cl.tts("SLOW", "EXAVITQu4vr4xnSDxMaL", "eleven_v3", budget=budget)
        month = bc.CharLedger(ledger).month()
        c.eq((info["attempts"], info["unsure_chars"], budget.used, month["by"].get("selftest unsure"), month["total"]),
             (2, 4, 19, 4, 19), "a timeout then an answer: both tries counted (one as unsure)")
        tight = bc.CharBudget(5)
        before = srv.state["calls"]["tts"]
        try:
            cl.tts("STUCK", "EXAVITQu4vr4xnSDxMaL", "eleven_v3", budget=tight)
            c.ok(False, "a retry that does not fit the budget must raise")
        except bc.BudgetExceeded:
            c.eq((tight.used, srv.state["calls"]["tts"] - before, bc.CharLedger(ledger).month()["by"].get("selftest unsure")),
                 (5, 1, 9), "no budget for a retry after a timeout: stops, the timed-out try stays counted")
        try:
            cl.tts("Hello", "EXAVITQu4vr4xnSDxMaL", "eleven_v3")
            c.ok(False, "tts without a budget must raise")
        except TypeError:
            c.ok(True, "tts needs a budget")
        nobudget = bc.CharBudget(50)
        try:
            bc.BenchClient(url, "", ledger=None, trust_env=False).tts("Hello", "EXAVITQu4vr4xnSDxMaL", budget=nobudget)
            c.ok(False, "tts without a token must raise")
        except bc.BenchError as e:
            c.ok(bc.is_fatal(e) and e.code == bc.NO_TOKEN and nobudget.used == 0,
                 "no token: fatal, and nothing charged")
        deltas = []
        text, tm = cl.translate_stream("Hello", "en", "hy", context=["a", "b", "c", "d"], glossary=["Gusan"],
                                       on_delta=deltas.append)
        c.eq(text, "Բարեւ ձեզ", "streamed translation decoded and cleaned like the app")
        c.eq("".join(deltas), tm["raw"], "deltas add up to the raw stream")
        c.ok(tm["first_delta_s"] is not None and 0 < tm["first_delta_s"] <= tm["total_s"], "first words before the end")
        c.ok(tm["first_byte_s"] < tm["first_delta_s"], "the think block arrives before the first visible words")
        c.eq((tm.get("server_ms"), tm.get("route"), tm.get("model"), tm.get("attempts")),
             (321, "deepseek", "m1", [{"route": "deepseek"}]), "translate headers")
        sent = [b for b in srv.state["bodies"] if b.get("action") == "translate"][-1]
        c.eq((sent["context"], sent["from"], sent["to"]), (["b", "c", "d"], "en", "hy"), "context trimmed to 3 like the app")
        # the interpreter run asks like the app: one try per phrase (a 5xx is a failed phrase, not a retry),
        # the app's (connect, read) timeouts, and the headers before the first words
        before = srv.state["calls"]["translate"]
        try:
            cl.translate_stream("BUSY", "en", "hy", retries=0, timeout=(5, 12))
            c.ok(False, "a failed translation must raise")
        except bc.BenchError as e:
            c.eq((e.status, srv.state["calls"]["translate"] - before), (503, 1), "translate with retries=0: one try")
        seen = []
        cl.translate_stream("Hello", "en", "hy", timeout=(5, 12), on_delta=lambda p: seen.append("delta"),
                            on_open=lambda h: seen.append(("open", h.get("X-Bench-Route"), h.get("X-Answer-Family"))))
        c.eq(seen[:2], [("open", "deepseek", "deepseek"), "delta"], "on_open gets the headers before the first delta")
        res = cl.stt(b"RIFF....", "scribe_v2", None, languages=None, vocabulary=["Gusan"])
        c.eq((res["text"], srv.state["calls"]["stt"]), ("hello world", 2), "stt retried after 429")
        c.ok(isinstance(res.get("client_ms"), int), "stt client time")
        sent = [b for b in srv.state["bodies"] if b.get("action") == "stt"][-1]
        c.ok("languages" not in sent and sent.get("vocabulary") == ["Gusan"], "stt drops None extras")
        scores = cl.judge([{"src": "a", "src_lang": "en", "ref": "b", "hyp": "b", "tgt_lang": "hy"}] * 25)
        c.eq((len(scores), srv.state["calls"]["judge"]), (25, 2), "judge in batches of 20")
        toks = cl.tokens(["realtime_scribe", "ttd_websocket"], n=2)
        c.eq((len(toks["tokens"]["realtime_scribe"]), toks["ttd_websocket"][1]["token"]), (2, "t-ttd_websocket-1"),
             "tokens (and the top-level alias)")
        try:
            bc.BenchClient(url, "wrong-token", backoff=0.01, ledger=None, trust_env=False).ping()
            c.ok(False, "a wrong token must raise")
        except bc.BenchError as e:
            c.ok(e.status == 401 and ".bench_token" in str(e), "a wrong token says where the token goes")
        try:
            bc.BenchClient(url, "", ledger=None, trust_env=False).ping()
            c.ok(False, "no token must raise")
        except bc.BenchError as e:
            c.ok(e.status is None and "token" in str(e), "no token: clear error, no call")
        srv.state["gone"] = True
        try:
            cl.ping()
            c.ok(False, "410 must raise")
        except bc.BenchError as e:
            c.ok(e.status == 410 and "stubbed" in str(e), "a stubbed function says so")
    finally:
        srv.shutdown()
        srv.server_close()


# -- the real code paths against the local fake function ----------------------------------

def t_real_paths(c, tmp, state):
    """generate.py and run_stt.py without --dry-run, talking HTTP to the local fake function: the
    token from the environment, retries, the ledger, the account usage, fake audio refused."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.state = {"calls": collections.Counter(), "bodies": []}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/speed-bench"
    saved = {k: os.environ.get(k) for k in (bc.TOKEN_ENV, "NO_PROXY", "no_proxy")}
    os.environ[bc.TOKEN_ENV] = TOKEN
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"     # never via a proxy
    try:
        corpus = tmp / "corpus_real"
        (corpus / "conversations").mkdir(parents=True, exist_ok=True)
        conv = SAMPLES[0]
        (corpus / "conversations" / f"{conv['id']}.json").write_text(json.dumps(conv, ensure_ascii=False),
                                                                      encoding="utf-8")
        audio, ledger = tmp / "audio_real", tmp / "ledger_real.json"
        common = ["--corpus", str(corpus), "--audio-dir", str(audio), "--url", url]
        code = _run(generate.main, common + ["--cache-dir", str(tmp / "cache_real"), "--ledger", str(ledger),
                                             "--max-chars", "1000", "--variants", "clean,speakers"])
        chars = sum(len(t["text"]) for t in conv["turns"])
        tl = json.loads((audio / conv["id"] / "timeline.json").read_text(encoding="utf-8")) if code == 0 else {}
        c.eq((code, tl.get("dry_run"), tl.get("chars_billed"), bc.CharLedger(ledger).month_total()),
             (0, False, chars, chars), "generate over HTTP: real audio, billed once, in the ledger")
        results = tmp / "results_real"
        code = _run(run_stt.main, common + ["--results-dir", str(results), "--engines", "scribe_v2,gladia",
                                            "--workers", "1", "--quiet"])
        files = sorted(results.glob("stt_*.json"))
        d = json.loads(files[-1].read_text(encoding="utf-8")) if files else {}
        jobs = [j for cv in d.get("conversations") or [] for j in cv["jobs"]]
        usage = (d.get("config") or {}).get("elevenlabs_usage") or {}
        c.ok(code == 0 and jobs and all(j["status"] == "ok" for j in jobs) and d["config"]["dry_run"] is False,
             f"run_stt over HTTP: every job answered (exit {code})")
        c.ok(any(j.get("tries") == 2 for j in jobs) and all(j["client_s"] <= j["client_total_s"] for j in jobs),
             "the 429 was retried; the answering try's time is kept apart")
        c.ok((usage.get("delta") or 0) > 0, f"account usage before / after the run ({usage})")
        sent = [b for b in srv.state["bodies"] if b.get("action") == "stt"]
        c.ok(sent and all(b.get("vocabulary") == conv["glossary"] for b in sent), "the glossary goes as vocabulary")
        c.eq(_run(report.main, ["--results-dir", str(results), "--ledger", str(ledger)]), 0, "report of the real run")
        rep = sorted(results.glob("report_*.md"))
        text = rep[-1].read_text(encoding="utf-8") if rep else ""
        c.ok("rose by" in text and "DRY RUN" not in text, "the report carries the account usage, no dry-run mark")
        if state.get("audio"):
            code = _run(run_stt.main, ["--corpus", str(state["corpus"]), "--audio-dir", str(state["audio"]),
                                       "--results-dir", str(tmp / "results_refuse"), "--url", url, "--quiet"])
            c.ok(code == 1 and "refusing" in _run.last, "a real run refuses dry-run audio")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        srv.shutdown()
        srv.server_close()


# -- run_stt with the fake engines ----------------------------------------------------------

def t_run_stt(c, tmp, state):
    results = tmp / "results"
    argv = ["--dry-run", "--corpus", str(state["corpus"]), "--audio-dir", str(state["audio"]),
            "--results-dir", str(results), "--variants", "clean,noisy,speakers", "--quiet"]
    c.eq(_run(run_stt.main, argv), 0, "run_stt --dry-run exit code")
    files = sorted(results.glob("stt_*.json"))
    c.eq(len(files), 1, "one results file")
    c.ok(re.fullmatch(r"stt_\d{8}-\d{6}\.json", files[0].name) is not None, "results file name")
    d = json.loads(files[0].read_text(encoding="utf-8"))
    for key in ("kind", "stamp", "config", "git", "conversations", "summary"):
        c.ok(key in d, f"results have {key}")
    c.eq(d["kind"], "stt", "kind")
    c.eq(d["summary"]["n_errors"], 0, "no failed jobs")
    c.eq(sorted(cv["id"] for cv in d["conversations"]), sorted(state["ids"]), "every conversation scored")
    cfg = {(x["engine"], x["variant"], x["input"], x["mode"]): x for x in d["summary"]["configs"]}
    best = cfg.get(("scribe_v2", "clean", "system", "auto"))
    c.ok(best is not None and 0 < best["wer"] < 0.25, f"fake scribe WER is small but not zero ({best and best['wer']})")
    echo_clean = sum(x.get("echo_units") or 0 for k, x in cfg.items() if k[1] == "clean" and k[2] == "mic")
    echo_spk = sum(x.get("echo_units") or 0 for k, x in cfg.items() if k[1] == "speakers" and k[2] == "mic")
    c.ok(echo_clean == 0 and echo_spk > 0, f"echo words only in speakers mode ({echo_clean}, {echo_spk})")
    c.ok(all(k[0] == "gladia" for k in cfg if k[3] == "multi"), "multi mode only on Gladia")
    c.ok(not any(k[2] == "mix" and k[3] == "fixed" for k in cfg), "the mix runs auto only")
    c.ok(not any(k[1] == "speakers" and k[2] == "system" for k in cfg), "speakers mode skips the unchanged call track")
    langs = {r["lang"] for r in d["summary"]["by_lang"]}
    want = {t["lang"] for cid in state["ids"]
            for t in json.loads((state["audio"] / cid / "timeline.json").read_text(encoding="utf-8"))["turns"]}
    c.eq(langs, want, "per-language rows for every language")
    job = d["conversations"][0]["jobs"][0]
    c.ok(job["method"] == "timing" and job["per_turn"], "fake words carry times; per-turn scores present")
    c.ok(all("wer_turns" in x for x in d["summary"]["configs"] if x.get("n_ok")), "turn-by-turn WER in the summary")
    uncovered = sorted(want - bc.FAKE_VOXTRAL_LANGS)
    c.ok(uncovered and all(any(x["engine"] == "voxtral" and x["language"] == lg for x in d["summary"]["fixed_not_sent"])
                           for lg in uncovered if any(j["mode"] == "fixed" and j["language"] == lg
                                                      for cv in d["conversations"] for j in cv["jobs"])),
         f"fixed jobs that sent no language are listed ({d['summary']['fixed_not_sent']})")
    vox = [j for cv in d["conversations"] for j in cv["jobs"] if j["engine"] == "voxtral" and j["mode"] == "fixed"]
    c.ok(vox and all(j.get("covered") is (j["language"] in bc.FAKE_VOXTRAL_LANGS) for j in vox),
         "Voxtral's coverage is recorded per job")
    mix_clean, mix_spk = cfg.get(("scribe_v2", "clean", "mix", "auto")), cfg.get(("scribe_v2", "speakers", "mix", "auto"))
    c.ok(mix_clean and mix_spk and mix_spk["wer"] < 2 * mix_clean["wer"] + 0.05,
         f"the speakers mix is not transcribed twice ({mix_clean and mix_clean['wer']} vs {mix_spk and mix_spk['wer']})")
    c.ok(all(j.get("tries") == 1 and j.get("client_total_s") is not None for cv in d["conversations"]
             for j in cv["jobs"]), "tries and total client time recorded")

    # a stubbed function stops the run after the first answer (results still saved)
    class StubbedFake(bc.FakeBench):
        def stt(self, *a, **k):
            self.calls["stubbed"] += 1
            raise bc.BenchError("the bench function is stubbed (HTTP 410)", action="stt", status=410)

    stub = StubbedFake()
    code = _run(lambda a: run_stt.main(a, bench=stub),
                ["--dry-run", "--corpus", str(state["corpus"]), "--audio-dir", str(state["audio"]),
                 "--results-dir", str(tmp / "results_stub"), "--workers", "1", "--quiet"])
    stub_files = sorted((tmp / "results_stub").glob("stt_*.json"))
    stub_data = json.loads(stub_files[0].read_text(encoding="utf-8")) if stub_files else {}
    c.ok(code == 3 and stub.calls["stubbed"] == 1 and (stub_data.get("config") or {}).get("stopped_on_error"),
         f"run_stt stops on a stubbed function (exit {code}, {stub.calls['stubbed']} calls)")

    # a second run (other fake errors) for the report's diff
    time.sleep(1.1)
    c.eq(_run(run_stt.main, argv[:-1] + ["--engines", "scribe_v2,voxtral", "--seed", "5", "--quiet"]), 0,
         "second run_stt exit code")
    state["results"] = results


# -- report ---------------------------------------------------------------------------------

LAT_KEYS = ("stt", "first_delta", "final", "ear", "turn_commit", "turn_ear", "first_caption", "app")
STAGE_KEYS = ("translate_wait", "ttft", "translate_rest", "speak_wait", "ttfb", "play_wait")


def _dir_agg(rng, ear_shift, chrf):
    """One direction's aggregate in run_interpreter.py's layout."""
    return {"wer": 0.08, "wer_all": 0.1, "cer": 0.04, "chrf": chrf, "chrf_matched": chrf + 4,
            "latency": {k: metrics.summary(rng.uniform(0.2, 1.0, 10) + (ear_shift if k == "ear" else 0.0))
                        for k in LAT_KEYS},
            "stages": {k: metrics.summary(rng.uniform(0.05, 0.4, 10)) for k in STAGE_KEYS},
            "counts": {"turns": 6, "phrases": 7, "real": 6, "dropped_turns": 1, "merged_turns": 0, "echo": 0,
                       "echo_mixed": 0, "crosstalk": 1},
            "judge": {"n": 6, "adequacy": 82.0, "fluency": 91.0},
            "gender": {"judged": 6, "accuracy": 0.83, "voice_judged": 6, "voice_accuracy": 0.83, "switches": 2,
                       "expected_switches": 2},
            "tts": {"chars": 120},
            "stt_input": {"speech_s": 30.0, "muted_s": 6.0, "muted_ratio": 0.2, "gusan_reached_stt_s": 1.5,
                          "sent_s": 120.0},
            "reconnects": {"stt": 1, "voice": 0}, "speech_end_by": {"turn_end": 4, "pause": 1, "gate": 1},
            "mixed": {"turns": 2, "wer_mean": 0.12, "chrf_mean": chrf - 5},
            "single": {"turns": 4, "wer_mean": 0.06, "chrf_mean": chrf + 2}}


def _interp_results(results, ear_shift, chrf):
    rng = np.random.default_rng(1)
    hear, speak = _dir_agg(rng, ear_shift, chrf), _dir_agg(rng, ear_shift, chrf - 2)
    summary = {"conversations": 3, "ok": 2, "failed": 0, "skipped": 1, "chars_spoken": 240, "chars_would_speak": 900,
               "chars_charged": 250, "echo_loops": 1, "echo_loops_by_kind": {"echo": 1, "echo_mixed": 0, "echo_suspect": 0},
               "echo_chain_max": 1, "crosstalk": 1, "errors": 0, "reconnects": {"stt": 2, "voice": 0},
               "stt_audio_s": 240.0,
               "leaks": {"sockets_open_after_stop": 0, "utterances_after_stop": 0},
               "sim": {"capture_max_lag_s": 0.01, "late_chunks": 0, "capture_errors": 0},
               "hear": hear, "speak": speak,
               "by_pair": {"en-hy": {"hear": {k: hear[k] for k in ("wer", "chrf", "latency", "counts")},
                                     "speak": {k: speak[k] for k in ("wer", "chrf", "latency", "counts")}}}}
    data = {"kind": "interpreter", "stamp": None, "git": "", "config": {"dry_run": True, "route": "default",
                                                                        "max_chars": 800},
            "conversations": [{"id": "x", "status": "ok"},
                              {"id": "y", "status": "skipped", "error": "not run: the bench function is stubbed"}],
            "summary": summary}
    return run_stt.save_results(results, "interpreter", data)


def t_report(c, tmp, state):
    results = state["results"]
    _interp_results(results, 1.1, 55.0)
    time.sleep(1.1)
    _interp_results(results, 0.6, 58.0)
    ledger = tmp / "ledger.json"
    c.eq(_run(report.main, ["--results-dir", str(results), "--ledger", str(ledger)]), 0, "report exit code")
    reports = sorted(results.glob("report_*.md"))
    c.eq(len(reports), 1, "one report written")
    text = reports[0].read_text(encoding="utf-8")
    for needle in ("## In short", "## Meeting transcription", "### Best per input", "### Every configuration",
                   "### Per language", "### Best engine per language", "Most accurate per language (clean):",
                   "*fixed* sent no language for: ", "words per 100 words of mine",
                   "### Mixed-language lines", "## Interpreter", "### Latency per stage",
                   "| stt_commit |", "| translate_first |", "| translate_total |", "| tts_first_audio |",
                   "| mouth_to_ear |", "### Where the time goes", "### Accuracy and translation quality",
                   "voice of the right gender", "### Phrases and turns", "### Whole run", "echo loops",
                   "### Per language pair", "## ElevenLabs characters", "19998",
                   "## Changes since the previous results", "### Interpreter:", "DRY RUN",
                   "### What reached the speech-to-text", "| own speech muted before the STT (the listen gate) | 20.0% |",
                   "| reconnects: STT / voice (fresh tokens after the start) | 1 / 0 |",
                   "| phrase ends from: turn end / pause / gate / estimate | 4 / 1 / 1 / 0 |",
                   "| audio fed to the live STT (min) | 2 |", "| reconnects: STT / voice | 2 / 0 |",
                   "| conversations failed / skipped | 0 / 1 |", "### Conversations that did not run (1)",
                   "- y: skipped - not run: the bench function is stubbed", "reached the STT as silence",
                   "| echo_mixed |"):
        c.ok(needle in text, f"report has {needle!r}")
    c.ok(re.search(r"\| hear mouth_to_ear p50 \| [\d.]+ \| [\d.]+ \| better \|", text) is not None,
         "interpreter diff marks a faster mouth-to-ear as better")
    c.ok(re.search(r"\| hear chrf \| 55 \| 58 \| better \|", text) is not None, "a higher chrF is better")
    # the baseline is the newest older run with the same settings, else one with a note
    pdir = tmp / "results_pick"
    for stamp, variant in (("20261101-100000", "clean"), ("20261101-110000", "speakers"), ("20261101-120000", "clean")):
        run_stt.save_results(pdir, "interpreter", {"kind": "interpreter", "stamp": stamp, "git": "", "conversations": [],
                                                   "config": {"variant": variant}, "summary": {}})
    _p, _d, prev, _pd = report.pick(pdir, "interpreter")
    c.eq(prev.name if prev else None, "interpreter_20261101-100000.json", "baseline with the same settings")
    c.eq(report.setting_diffs("interpreter", {"config": {"variant": "clean"}}, {"config": {"variant": "speakers"}}),
         ["variant 'clean' -> 'speakers'"], "settings that differ are named")
    _p, _d, prev, _pd = report.pick(pdir, "interpreter", given=pdir / "interpreter_20261101-110000.json")
    c.eq(prev.name if prev else None, "interpreter_20261101-100000.json", "else the newest older run (with a note)")
    # a dry run made after a real one does not hide it
    run_stt.save_results(pdir, "interpreter", {"kind": "interpreter", "stamp": "20261102-090000", "git": "",
                                               "conversations": [], "config": {"variant": "clean", "dry_run": "oracle"},
                                               "summary": {}})
    got = report.pick(pdir, "interpreter")[0]
    got_dry = report.pick(pdir, "interpreter", dry=True)[0]
    c.eq((got.name if got else None, got_dry.name if got_dry else None),
         ("interpreter_20261101-120000.json", "interpreter_20261102-090000.json"), "real runs win; --dry picks dry runs")
    text_p, _used = report.build_report(pdir, ledger=ledger)
    c.ok("Newer dry runs are left out" in text_p and "interpreter_20261102-090000.json" in text_p,
         "the report says which newer dry run it left out")
    # the ElevenLabs account's count around a real STT run
    c.eq(set(run_stt.account_usage(bc.FakeBench()) or {}), {"used", "limit", "left"}, "account usage read")
    cs = "\n".join(report.chars_section(ledger, None, {"config": {
        "elevenlabs_usage": {"before": {"used": 100, "limit": 20000}, "after": {"used": 220, "limit": 20000}, "delta": 120},
        "audio_min_by_engine": {"scribe_v2": 9.5}}}))
    c.ok("rose by 120" in cs and "scribe_v2 9.5 min" in cs, "the STT run's account usage is reported")
    # any other layout still renders (generic tables)
    generic = {"config": {}, "conversations": [],
               "summary": {"latency": {st: metrics.summary([0.5, 1.0, 1.5]) for st in report.STAGES},
                           "quality": {"chrf": 50.0}, "gender_match": 0.9}}
    g = "\n".join(report.interp_section(Path("interpreter_x.json"), generic))
    c.ok("### Latency per stage" in g and "mouth_to_ear" in g and "gender_match" in g, "generic interpreter layout")


# -- main -----------------------------------------------------------------------------------

def main(argv=None):
    bc._utf8_console()
    ap = argparse.ArgumentParser(description="Offline self-test of the interpreter bench.")
    ap.add_argument("--keep", action="store_true", help="keep the temporary folder")
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    tmp = Path(tempfile.mkdtemp(prefix="gusan_bench_selftest_"))
    c = Checks()
    state = {}
    sections = [("metrics", lambda: t_metrics(c, tmp)), ("budget", lambda: t_budget(c, tmp)),
                ("audio", lambda: t_audio(c, tmp)), ("generate", lambda: t_generate(c, tmp, state)),
                ("client", lambda: t_client(c, tmp)), ("real paths", lambda: t_real_paths(c, tmp, state)),
                ("run_stt", lambda: t_run_stt(c, tmp, state)), ("report", lambda: t_report(c, tmp, state))]
    try:
        for name, fn in sections:
            c.section = name
            t = time.perf_counter()
            try:
                fn()
            except Exception:                                     # noqa: BLE001 - report and go on
                c.ok(False, "crashed:\n" + traceback.format_exc())
            print(f"  {name}: done in {time.perf_counter() - t:.1f} s")
    finally:
        if args.keep:
            print(f"kept {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    took = time.perf_counter() - t0
    if c.failed:
        print(f"selftest_offline: {len(c.failed)} of {c.n} checks FAILED ({took:.1f} s)")
        for f in c.failed:
            print("  - " + f.splitlines()[0])
        return 1
    print(f"selftest_offline: {c.n} checks passed ({took:.1f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
