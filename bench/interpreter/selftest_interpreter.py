"""Self-test for run_interpreter.py and sim_audio.py: offline, well under a minute.

    python selftest_interpreter.py           everything
    python selftest_interpreter.py --keep    keep the temp folder (audio, results) to look at

1. unit checks: text units, the sink's timeline (play / queue / clear / on_played, silence included),
   the capture mix (the loopback hears Gusan's voice 10 ms later; the mic in the speakers variant
   40 ms later at -10 dB), the sampler, the character budget, the oracle translator and listener,
   the phrase-to-script alignment (most exact matches, inside each turn's time), echo words,
   faults, which sink is which
2. a small Armenian / English call made from the bundled preset voice samples (assets/voices: real
   voices, so the pitch-based voice matching has something real to judge): me (a man, the mic) and
   two others on the call (a woman and a man), with a short gap between two of their turns, an
   overlap, a mixed-language line and a run-on turn
3. dry runs of run_interpreter.py on it, as parallel processes, clock x4:
   a. oracle, clean: both directions scored, delays, voices matched, no echo loops, characters
   b. oracle, speakers variant with the output setting on headphones (so the mic is never gated):
      the echo loop and the cross-talk must be found
   c. sockets (the REAL eleven.ScribeRealtime / DialogueVoice against in-process fake sockets),
      clean, half the phrases spoken under a small character budget
   d. oracle, captions only: no characters, translations still scored
   e. sockets with faults: both live STT sockets hang up at 8 s and each voice socket mid-phrase
      after 12 s: the reconnects (fresh bench tokens) are counted and both directions go on
   f. sockets with --url: the real BenchClient over HTTP against a local stand-in of the bench
      function (preflight ping, tokens, streamed translation, judge), as a real run talks to it;
      every phrase is translated by the app's own client (interpreter/translate.translate_phrase),
      one try each
   g. two conversations against a stand-in that is stubbed (410) right after the check: the run
      stops after the first refusal and skips the rest
   On a busy machine a simulation can fall behind real time among the parallel runs (its delays are
   then the machine's): a run whose checks fail after that is run again on its own at half the clock
   (down to x1), and only what still fails counts.
"""
import argparse
import collections
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
for _p in (str(REPO), str(HERE)):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

import numpy as np  # noqa: E402

import benchaudio  # noqa: E402
import sim_audio as S  # noqa: E402

RATE = S.RATE
FAILS = []
_BUF = None          # while one dry run's checks are judged: [(kind, ok, text)], kind "check" | "say"


def check(cond, what):
    if _BUF is not None:
        _BUF.append(("check", bool(cond), what))
        return cond
    if cond:
        print(f"  ok   {what}")
    else:
        print(f"  FAIL {what}")
        FAILS.append(what)
    return cond


def say(text):
    if _BUF is not None:
        _BUF.append(("say", None, text))
    else:
        print(text)


# ── 1. unit checks ───────────────────────────────────────────────────────────────────────────
def unit_checks():
    print("unit checks")
    check(S.norm_units("Ինչո՞ւ ես ուշացել։ Don't, 東京") == ["ինչու", "ես", "ուշացել", "dont", "東", "京"],
          "comparison units: Armenian marks stay inside the word, CJK one unit per character")
    units, joiner, ends = S.spoken_units("Hello, world. [laughs] Bye.")
    check(units == ["Hello,", "world.", "Bye."] and joiner == " " and abs(ends[-1] - 1.0) < 1e-9,
          "spoken units: audio tags are not said, shares end at 1")

    # the sink's timeline
    sim = S.SimAudio(np.zeros(RATE * 2, np.int16), np.zeros(RATE * 2, np.int16), speed=8.0)
    played = []
    sink = sim.playback(None, on_played=lambda b: played.append(len(b) // 2))
    sim.begin(0.0)
    u1, u2 = S.Utterance("hear", "one"), S.Utterance("hear", "two")
    with S.speaking(u1):
        sink.play(np.full(1600, 1000, np.int16).tobytes())
    with S.speaking(u2):
        sink.play(np.full(1600, 2000, np.int16).tobytes())
    check(u2.first_pos == u1.end_pos and u1.end_pos - u1.first_pos == 1600,
          "sink: a second phrase is queued right after the first")
    q = sink.queued_seconds()
    check(0.15 < q <= 0.23, f"sink: queued_seconds tells the truth ({q:.3f} s for 0.2 s + 20 ms latency)")
    check(sink.is_playing(), "sink: is_playing while audio is queued")
    check([x.text for x in sink.utterances_in(u1.first_pos + 10, 320)] == ["one"]
          and [x.text for x in sink.utterances_in(u2.first_pos + 10, 320)] == ["two"],
          "sink: knows which phrase is where on the timeline")
    sink.render_until(u1.first_pos + 800)
    check(sum(played) == 800, "sink: on_played gets the audio as it plays")
    sink.clear()
    check(sink.queued_seconds() == 0.0 and sink.read(u2.end_pos - 100, 50).max() == 0,
          "sink: clear() drops what had not played")
    # the silence between phrases reaches on_played too (like audio_io.PlaybackSink: the own-voice
    # canceller counts samples, so its reference must move with the device)
    sim2 = S.SimAudio(np.zeros(RATE * 4, np.int16), np.zeros(RATE * 4, np.int16), speed=8.0)
    got = []
    s2 = sim2.playback(None, on_played=lambda b: got.append(np.frombuffer(b, np.int16)))
    sim2.begin(0.0)
    s2.play(np.full(320, 500, np.int16).tobytes())
    first_start, first_end = s2.sched_end - 320, s2.sched_end
    time.sleep(0.05)                                    # 0.4 s of clock: the sink runs dry
    s2.play(np.full(320, 500, np.int16).tobytes())
    s2.render_until(s2.sched_end)
    allp = np.concatenate(got) if got else np.zeros(0, np.int16)
    check(s2.sched_end - first_end > 320 and allp.size == s2.sched_end - first_start
          and int(np.count_nonzero(allp)) == 640,
          "sink: the silence between phrases reaches on_played too, in step with the device")
    # a started sink hands on everything from its start, nothing twice
    sim5 = S.SimAudio(np.zeros(RATE * 2, np.int16), np.zeros(RATE * 2, np.int16), speed=8.0)
    got5 = []
    s5 = sim5.playback(None, on_played=lambda b: got5.append(len(b) // 2))
    sim5.begin(0.0)
    s5.start()
    at = s5._rendered
    s5.render_until(at + 640)
    s5.render_until(at + 320)                           # (a slower capture: already handed on)
    s5.render_until(at + 960)
    check(got5 == [640, 320], f"sink: on_played runs on from the device's start without gaps ({got5})")

    # the capture mix
    sim3 = S.SimAudio(np.zeros(RATE * 2, np.int16), np.zeros(RATE * 2, np.int16), speed=1.0, speakers_echo=True)
    hs = sim3.playback(None, on_played=None)
    sim3.begin(0.0)
    hs.out.write(1000, np.full(3200, 10000, np.int16))
    lb = sim3.mix("loopback", 1000 + sim3.loop_delay, 320)
    mic = sim3.mix("mic", 1000 + sim3.echo_delay, 320)
    check(abs(float(lb.mean()) - 10000) < 1 and abs(float(mic.mean()) - 10000 * 10 ** (-10 / 20)) < 2,
          "mix: the loopback hears Gusan 10 ms later, the mic (speakers) 40 ms later at -10 dB")
    check(float(np.abs(sim3.mix("loopback", 1000, sim3.loop_delay - 1)).max()) == 0,
          "mix: nothing of Gusan before it played")
    ir = benchaudio.room_ir(RATE, 35.0, 0.3, 0.3, 7, -6.0) * 0.3
    sim6 = S.SimAudio(np.zeros(RATE * 2, np.int16), np.zeros(RATE * 2, np.int16), speed=1.0, speakers_echo=True,
                      echo_ir=ir)
    hs6 = sim6.playback(None)
    sim6.begin(0.0)
    hs6.play((np.sin(np.arange(8000) * 0.07) * 8000).astype(np.int16).tobytes())
    want = np.convolve(hs6.read(4000 - len(ir) + 1, 320 + len(ir) - 1), ir, mode="valid")
    check(np.allclose(sim6._gusan("mic", 4000, 320), want, atol=0.5)
          and np.allclose(sim6.gusan_track("mic", 8000)[4000:4320], want, atol=0.5)
          and float(np.abs(want).max()) > 100,
          "mix: the room path (FFT, cached) equals a plain convolution, live and for the analysis")

    # sampler and budget
    import run_interpreter as R
    smp = R.Sampler(0.25)
    seq = [smp.take() for _ in range(8)]
    check(seq == [True, False, False, False, True, False, False, False], f"sampler 0.25 -> {seq}")
    check(all(R.Sampler(1.0).take() for _ in range(5)) and not any(R.Sampler(0.0).take() for _ in range(5)),
          "sampler 1.0 says yes always, 0.0 never")
    sp = R.Spend(10)
    check(sp.take(6) and not sp.take(6) and sp.take(4) and sp.used == 10 and sp.refused == 1,
          "budget: refuses what would go over, keeps counting")

    # oracle translator
    tr = S.OracleTranslator([{"lang": "hy", "text": "Բարև, ինչպե՞ս ես։ Վաղը կհանդիպենք։",
                              "ref": {"en": "Hello, how are you? We will meet tomorrow."}}])
    check(tr("Վաղը կհանդիպենք", "hy", "en") == "We will meet tomorrow."
          and tr("We will meet tomorrow", "en", "hy") == "Վաղը կհանդիպենք։"
          and tr("nothing like it at all", "hy", "en").startswith("[en] "),
          "oracle translator: sentence by sentence, both ways, unknown text tagged")

    # oracle listener: hears the script, not what was muted, and hears Gusan when it gets in
    call = np.zeros(RATE * 4, np.int16)
    call[:2 * RATE] = (np.sin(np.arange(2 * RATE) * 0.05) * 3000).astype(np.int16)     # the turn: 0..2 s
    sim4 = S.SimAudio(np.zeros(RATE * 4, np.int16), call, speed=1.0)
    hear = sim4.playback(None, on_played=None)
    sim4.begin(0.0)
    world = S.OracleWorld(sim4, [{"track": "system", "start": 0.0, "end": 2.0, "text": "one two three four"}],
                          error_rate=0.0)
    ch = S.Chunk(sim4, "loopback", int(0.2 * RATE), 320)
    speech, marks = world.hear(ch, sim4.mix("loopback", ch.pos, 320).tobytes())
    check(speech and [(m[0]["key"], m[3]) for m in marks] == [(("turn", 0), True)],
          "oracle: the turn being said gets through")
    speech, marks = world.hear(ch, bytes(640))
    check(not speech and [m[3] for m in marks] == [False], "oracle: nothing gets through muted audio")
    said = []
    ear = S.OracleEar(world, 0.4, lambda kind, text: said.append((kind, text)) if kind == "committed" else None)
    for pos in range(0, int(3.0 * RATE), 320):
        mixed = sim4.mix("loopback", pos, 320)
        muted = int(0.5 * RATE) <= pos < int(1.5 * RATE)          # "two" and "three" never reach it
        ear.feed(bytes(640) if muted else mixed.tobytes(), S.Chunk(sim4, "loopback", pos, 320))
    check([t for _k, t in said] == ["one", "four"], f"oracle: muted words are lost, the rest committed ({said})")
    utt = S.Utterance("hear", "hello there")
    with S.speaking(utt):
        hear.play(np.full(RATE, 4000, np.int16).tobytes())
    ch2 = S.Chunk(sim4, "loopback", utt.first_pos + sim4.loop_delay + int(0.2 * RATE), 320)
    speech, marks = world.hear(ch2, sim4.mix("loopback", ch2.pos, 320).tobytes())
    check(any(m[0]["key"] == ("utt", utt.id) and m[3] for m in marks),
          "oracle: Gusan's own voice gets through when it is in the audio")

    g = R._Grams()
    check(g.contained("review it today", "Nice. Davit, can you review it today?") == 1.0
          and g.contained("something else", "Nice. Davit") < 0.3, "similarity: containment of trigrams")
    check(g.contained("含运。", "每个12美元，含运费。") == 1.0 and g.contained("OK", "ok then") == 1.0
          and g.contained("OK", "book it") == 0.0, "similarity: texts too short for a trigram still match (whole words)")

    # phrases against the script: the most exact matches (a plain edit-distance backtrace trades
    # "2000 units" for "price for" when a stray phrase follows), each word inside its turn's time
    ref = "ok and the price for 2000 units this week if you can ship".split()
    hyp = "ok and the 2000 units 含 运 its each this week you".split()
    eq = {(ref[r], hyp[h]) for op, r, h in R.match_align(ref, hyp) if op == "="}
    plain = {(ref[r], hyp[h]) for op, r, h in R.metrics.align(ref, hyp)[1] if op == "="}
    check({("2000", "2000"), ("units", "units"), ("this", "this"), ("week", "week")} <= eq and len(eq) > len(plain),
          f"alignment keeps the most matches ({len(eq)} vs {len(plain)} for the plain backtrace)")
    win = [(9.0, 32.0)] * 7 + [(19.0, 45.0)] * 6
    early = [11.0] * 7 + [14.0] * 5                     # "its each this week you" cut before that turn began
    eq_t = [(r, h) for op, r, h in R.match_align(ref, hyp, win, early) if op == "="]
    check(eq_t and not any(r >= 7 for r, _h in eq_t), "alignment: a word only matches inside its turn's time")
    cov = R._pair_cover(["ni", "its", "each", "this", "week"], [True, True, True, False, False],
                        [["its", "12", "dollars", "each"]])
    check(cov == {1, 2}, f"echo words: two neighbours found in what Gusan said, holes allowed ({sorted(cov)})")
    talk = (np.sin(np.arange(4 * RATE) * 0.06) * 6000).astype(np.int16)
    talk[int(2.0 * RATE):int(2.4 * RATE)] = 0                          # a sentence pause
    talk[int(3.0 * RATE):int(3.1 * RATE)] = 0                          # a comma: too short
    ps = R.turn_pauses(talk, 0.5, 4.0)
    check(len(ps) == 1 and abs(ps[0][0] - 2.0) < 0.02 and abs(ps[0][1] - 2.4) < 0.02,
          f"pauses inside a turn: the sentence pause, not the comma ({ps})")
    sent = S.Timeline()
    sent.write(0, talk[:int(1.0 * RATE)])                               # then the gate mutes it (zeros)
    sent.write(int(1.6 * RATE), talk[int(1.6 * RATE):int(2.0 * RATE)])
    mo = R.muted_onsets(sent, int(2.5 * RATE))
    check(len(mo) >= 1 and abs(mo[0] - 1.0) < 0.02, f"where the gate muted the STT's input ({mo})")

    cells = R.MATCH_CELLS
    try:
        R.MATCH_CELLS = 10                              # hours of text: falls back to the plain alignment
        fb = R.match_align(ref, hyp)
    finally:
        R.MATCH_CELLS = cells
    check([r for _o, r, _h in fb if r is not None] == list(range(len(ref)))
          and sorted(h for _o, _r, h in fb if h is not None) == list(range(len(hyp))) and all(o in "=DI" for o, _r, _h in fb),
          "alignment: very long texts fall back to the plain alignment (every unit once, no substitutions)")

    # real runs check the function first: up, and holding the keys the run needs
    class _Ping:
        def __init__(self, prov=None, err=None):
            self.prov, self.err = prov, err

        def ping(self):
            if self.err:
                raise R.bench_client.BenchError(self.err, action="ping", status=410)
            return {"ok": True, "providers": self.prov}
    a_def, a_ds = argparse.Namespace(route="default"), argparse.Namespace(route="deepseek")
    allp = {"elevenlabs": True, "llm": True, "mistral": True, "openai": True}
    quiet = lambda *_a: None  # noqa: E731
    check(R.preflight(_Ping(allp), a_def, quiet) is None
          and "stubbed" in (R.preflight(_Ping(err="the bench function is stubbed"), a_def, quiet) or "")
          and "ElevenLabs" in (R.preflight(_Ping(dict(allp, elevenlabs=False)), a_def, quiet) or "")
          and "llm" in (R.preflight(_Ping(dict(allp, llm=False)), a_ds, quiet) or "")
          and R.preflight(_Ping(dict(allp, llm=False)), a_def, quiet) is None,
          "preflight: stubbed / no ElevenLabs key / the route's key missing stop a real run before it spends")

    # the judge's null scores (the bench function's "unparsed") are left out of the means, never a crash
    def _dir(judge):
        return {"stt": {"ref_units": 10, "errors": 1, "errors_all": 1, "ref_chars": 40, "char_errors": 2},
                "translation": {"chrf_stats": None, "chrf_matched_stats": None, "judge": judge}, "counts": {},
                "tts": {}, "stt_input": {}, "reconnects": {}, "speech_end_by": {}}
    agg = R._agg_dir([_dir({"n": 2, "n_fluency": 1, "adequacy": 80.0, "fluency": 90.0, "unparsed": 1}),
                      _dir({"n": 0, "n_fluency": 0, "adequacy": None, "fluency": None, "unparsed": 3}),
                      _dir({"n": 1, "n_fluency": 0, "adequacy": 50.0, "fluency": None, "unparsed": 0})], [])
    check(agg["judge"] == {"n": 3, "unparsed": 4, "adequacy": 70.0, "fluency": 90.0},
          f"judge: unparsed (null) scores are left out of the means ({agg['judge']})")

    # faults (sockets dry runs) and which sink is which
    check(R.parse_faults("stt_drop@12,voice_drop@20,stt_drop@30") == {"stt_drop": [12.0, 30.0], "voice_drop": [20.0]},
          "faults: parsed")
    f = S.Faults({"stt_drop": [5.0]})
    check([f.due("stt_drop", 4.9, "en"), f.due("stt_drop", 5.1, "en"), f.due("stt_drop", 6.0, "en"),
           f.due("stt_drop", 6.0, "hy")] == [False, True, False, True], "faults: each fires once per direction")
    sim5 = S.SimAudio(np.zeros(RATE, np.int16), np.zeros(RATE, np.int16), virtual_mic=1)
    check([sim5.playback(None).name, sim5.playback(1).name, sim5.playback(4).name] == ["hear", "speak", "hear"],
          "sinks: the virtual mic's index is SPEAK, any other output HEAR")


# ── 2. a small call ──────────────────────────────────────────────────────────────────────────
VOICES = {"aram": ("nPczCjzI2devNBz1zQrb", "male", "Aram"),     # Brian
          "anna": ("Xb7hH8MSUJpSbSDYk0k2", "female", "Anna"),   # Alice
          "davit": ("JBFqnCBsd6RMkjVDRZzb", "male", "Davit")}   # George
SCRIPT = [  # speaker, lang, langs, text, ref (into the other language + English), gap_before
    ("anna", "hy", ["hy"], "Բարև, Արամ։ Պատրաստ ե՞ս սկսել։",
     {"en": "Hi Aram. Are you ready to start?"}, 0.5),
    ("aram", "en", ["en"], "Yes, let's start. I finished the Gusan release notes yesterday.",
     {"hy": "Այո, սկսենք։ Երեկ ավարտեցի Gusan-ի թողարկման նշումները։",
      "en": "Yes, let's start. I finished the Gusan release notes yesterday."}, 0.8),
    ("davit", "hy", ["hy"], "Լավ է։ Ես երկու զանգում փորձարկեցի թարգմանիչը։",
     {"en": "Good. I tested the interpreter in two calls."}, 0.9),
    ("anna", "hy", ["hy", "en"], "Իսկ ես demo-ն պատրաստեցի, link-ը Slack-ում է։",
     {"en": "And I prepared the demo, the link is in Slack."}, 0.25),
    ("aram", "en", ["en"], "Great. Can we show it to the client on Friday at three?",
     {"hy": "Հիանալի է։ Կարո՞ղ ենք այն ցույց տալ հաճախորդին ուրբաթ օրը ժամը երեքին։",
      "en": "Great. Can we show it to the client on Friday at three?"}, 1.0),
    ("davit", "hy", ["hy"], "Այո, ուրբաթը հարմար է։ Ես կուղարկեմ հրավերը, իսկ Աննան կպատրաստի սլայդները։",
     {"en": "Yes, Friday works. I'll send the invite, and Anna will prepare the slides."}, -0.3),
    ("aram", "en", ["en"], "Perfect, thanks everyone.",
     {"hy": "Հիանալի է, շնորհակալություն բոլորին։", "en": "Perfect, thanks everyone."}, 1.2),
]


def _calm_part(a):
    """The sample after its first pause: the bundled samples open with an excited "Hi!", which
    would start every looped turn with an exclamation."""
    db = benchaudio.frame_db(a / 32768.0, RATE, 10)
    quiet = db < db.max() - 35
    run = 0
    for i, q in enumerate(quiet):
        run = run + 1 if q else 0
        if run >= 6 and i > 15:
            return a[int((i + 1) * 0.01 * RATE):]
    return a


def _clip(voice_id, text):
    """The voice's bundled sample, looped to the time the text takes to say (~14 characters/s)."""
    base = _calm_part(S.voice_sample(voice_id).astype(np.float32))
    n = max(int(0.8 * RATE), int(len(text) / 14.0 * RATE))
    x = np.tile(base, n // len(base) + 1)[:n]
    r = 320
    x[:r] *= np.linspace(0, 1, r)
    x[-r:] *= np.linspace(1, 0, r)
    return x / 32768.0


def build_call(root):
    """Write <root>/audio/st01_en_hy/{mic,system,mix,mic_speakers}.wav + timeline.json."""
    out = root / "audio" / "st01_en_hy"
    out.mkdir(parents=True, exist_ok=True)
    floor_end, spans, clips = 0.5, [], []
    for k, (spk, _lang, _langs, text, _ref, gap) in enumerate(SCRIPT):
        clip = _clip(VOICES[spk][0], text)
        start = 0.5 + max(0.0, gap) if k == 0 else floor_end + gap
        start = round(max(0.0, start) * RATE) / RATE
        end = start + len(clip) / RATE
        spans.append((start, end))
        clips.append(clip)
        floor_end = max(floor_end, end)
    total = int((floor_end + 0.8) * RATE)
    mic, system = np.zeros(total, np.float32), np.zeros(total, np.float32)
    for (spk, *_rest), clip, (s, _e) in zip(SCRIPT, clips, spans):
        a = int(round(s * RATE))
        (mic if spk == "aram" else system)[a:a + len(clip)] += clip[:total - a]
    ir = benchaudio.room_ir(RATE, 35.0, 0.3, 0.3, 7, -6.0)
    benchaudio.write_wav(out / "mic.wav", mic)
    benchaudio.write_wav(out / "system.wav", system)
    benchaudio.write_wav(out / "mix.wav", mic + system)
    benchaudio.write_wav(out / "mic_speakers.wav", benchaudio.apply_leak(mic, system, 0.3, ir))
    np.save(out / "leak_ir.npy", ir)
    turns = []
    for k, ((spk, lang, langs, text, ref, gap), (s, e)) in enumerate(zip(SCRIPT, spans)):
        turns.append({"i": k, "speaker": spk, "track": "mic" if spk == "aram" else "system", "lang": lang,
                      "langs": langs, "text": text, "ref": ref, "gender": VOICES[spk][1], "start": round(s, 3),
                      "end": round(e, 3), "voice": VOICES[spk][0], "gap_before": gap})
    tl = {"id": "st01_en_hy", "sample_rate": RATE, "duration": round(total / RATE, 3), "turns": turns,
          "variants": {"clean": {"mic": "mic.wav", "system": "system.wav", "mix": "mix.wav"},
                       "speakers": {"mic": "mic_speakers.wav", "system": "system.wav", "mix": "mix.wav"}},
          "chars": sum(len(t["text"]) for t in turns), "title": "Selftest call", "me": "aram",
          "my_lang": "en", "their_lang": "hy",
          "speakers": {k: {"name": v[2], "gender": v[1], "voice": v[0]} for k, v in VOICES.items()},
          "glossary": ["Gusan", "Slack", "Aram", "Anna", "Davit"], "dry_run": True,
          "leak": {"gain": 0.3, "delay_ms": 35.0, "rt60_s": 0.3, "length_s": 0.3, "reverb_db": -6.0, "seed": 7,
                   "ir_file": "leak_ir.npy"}}
    (out / "timeline.json").write_text(json.dumps(tl, ensure_ascii=False, indent=1), encoding="utf-8")
    # the same call twice under other ids, for the run that finds the function stubbed
    for cid in ("st01_en_hy", "st02_en_hy"):
        dst = root / "audio2" / cid
        shutil.copytree(out, dst)
        (dst / "timeline.json").write_text(json.dumps(dict(tl, id=cid), ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    return tl


# ── 3. dry runs ──────────────────────────────────────────────────────────────────────────────
RUNS = {
    "a_clean": ["--dry-run", "oracle"],
    "b_speakers_no_gate": ["--dry-run", "oracle", "--variant", "speakers", "--output", "headphones"],
    "c_sockets": ["--dry-run", "sockets", "--tts-sample", "0.5", "--max-chars", "150"],
    "d_captions": ["--dry-run", "oracle", "--captions-only"],
    "e_faults": ["--dry-run", "sockets", "--fault", "stt_drop@8,voice_drop@12"],
    "f_http": ["--dry-run", "sockets", "--judge", "--events", "--url", "{url}"],
    "g_stubbed": ["--dry-run", "oracle", "--url", "{url}/stubbed", "--audio-dir", "{root}/audio2"],
}
BENCH_TOKEN = "selftest-bench-token-0123456789"
THINK_LEAK = "<think>Plan: translate it word for word.</think>"


class _BenchStandIn(http.server.BaseHTTPRequestHandler):
    """The bench function's API (ping, tokens, translate streamed as text/plain, judge) on
    127.0.0.1, answering translations from the call's references: run f_http uses the real
    BenchClient over HTTP, as a real run does."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        st = self.server.state
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.headers.get("x-bench-token") != BENCH_TOKEN:
            return self._json(401, {"error": "unauthorized"})
        action = body.get("action")
        stubbed = self.path.rstrip("/").endswith("/stubbed")
        with st["lock"]:
            st["stubbed_calls" if stubbed else "calls"][action] += 1
            k = st["calls"][action]
        if stubbed and action != "ping":           # stubbed between the check and the first call
            return self._json(410, {"error": "gone"})
        if action == "ping":
            return self._json(200, {"ok": True, "providers": {p: True for p in
                                                              ("elevenlabs", "llm", "openai", "mistral", "gladia")}})
        if action == "tokens":
            return self._json(200, {"tokens": {kind: [{"token": f"http-{kind}-{k}-{i}", "ms": 3}
                                                      for i in range(int(body.get("n") or 1))]
                                               for kind in body.get("kinds") or []}, "server_ms": 3})
        if action == "translate":
            with st["lock"]:
                st["translate_bodies"].append({k: body.get(k) for k in ("text", "from", "to", "context", "glossary",
                                                                         "route")})
                if getattr(self, "_translated", False):     # a second phrase on the same connection
                    st["translate_reused"] += 1
            self._translated = True
            out = st["oracle"](body.get("text") or "", body.get("from"), body.get("to"))
            self.send_response(200)
            # a DeepSeek answer that leaks a <think> block first: the app's own filter must drop it
            for h, v in (("Content-Type", "text/plain; charset=utf-8"), ("Transfer-Encoding", "chunked"),
                         ("X-Bench-Route", "fake-http"), ("X-Bench-Model", "oracle"), ("X-Server-Ms", "7"),
                         ("X-Answer-Family", "deepseek")):
                self.send_header(h, v)
            self.end_headers()
            for piece in [THINK_LEAK] + (re.findall(r"\S+\s*", out) or [out]):
                data = piece.encode("utf-8")
                self.wfile.write(b"%X\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            return None
        if action == "judge":
            return self._json(200, {"scores": [{"adequacy": 77, "fluency": 88, "note": "stand-in"}
                                               for _ in body.get("items") or []]})
        return self._json(400, {"error": "bad_request", "detail": "action"})


def start_bench(turns):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _BenchStandIn)
    srv.daemon_threads = True
    srv.state = {"calls": collections.Counter(), "stubbed_calls": collections.Counter(), "lock": threading.Lock(),
                 "oracle": S.OracleTranslator(turns), "translate_bodies": [], "translate_reused": 0}
    threading.Thread(target=srv.serve_forever, name="bench-stand-in", daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/speed-bench"


TIMED_OUT = "(timed out)"
LAG_OK_S = 0.5           # a run whose simulation stayed this close to its clock kept up: its failures are real


def _start(root, name, speed, url):
    extra = RUNS[name]
    cmd = [sys.executable, str(HERE / "run_interpreter.py"), "--speed", str(speed), "--quiet",
           "--corpus", str(root / "corpus"), "--audio-dir", str(root / "audio"),
           "--results-dir", str(root / f"results_{name}"),
           *[x.replace("{url}", url).replace("{root}", str(root)) for x in extra]]   # a run's own options win
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace",
                            env=dict(os.environ, PYTHONIOENCODING="utf-8", GUSAN_BENCH_TOKEN=BENCH_TOKEN,
                                     NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost"))


def _finish(p, timeout):
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        out = TIMED_OUT
    return p.returncode, out


def run_all(root, speed, url=""):
    procs = {name: _start(root, name, speed, url) for name in RUNS}
    return {name: _finish(p, 150) for name, p in procs.items()}


def run_one(root, name, speed, url=""):
    """One dry run on its own (the machine was too busy for it among the others)."""
    return _finish(_start(root, name, speed, url), 300)


def load_result(root, name):
    files = sorted((root / f"results_{name}").glob("interpreter_*.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def _lag(root, name):
    """How far (clock seconds) the run's simulation fell behind real time at worst; None when the
    run left no result."""
    data = load_result(root, name)
    sims = [c.get("sim") or {} for c in (data or {}).get("conversations") or []]
    if not sims:
        return None
    return max(float(s.get("capture_max_lag_s") or 0.0) for s in sims)


def _judge(root, name, code, out, srv):
    """One run's checks, collected (not printed yet)."""
    global _BUF
    _BUF = []
    try:
        check_run(root, name, code, out, srv)
    except Exception:  # noqa: BLE001 - a check that raises is a failed check
        _BUF.append(("check", False, f"{name}: the checks raised\n{traceback.format_exc()[-1500:]}"))
    finally:
        buf, _BUF = _BUF, None
    return buf


def check_runs(root, outs, srv=None, speed=4.0, url=""):
    """Every run's checks. All runs go at once, each with its own clock x``speed``: on a busy machine a
    simulation can fall behind real time, and then its delays (and what the gate muted) are the
    machine's, not the engine's. A run whose checks fail AFTER falling behind is run again on its own,
    at half the clock (down to x1); only what still fails then counts."""
    for name in RUNS:
        code, out = outs[name]
        buf = _judge(root, name, code, out, srv)
        s = float(speed)
        while name != "g_stubbed" and s > 1.0 and any(k == "check" and not ok for k, ok, _t in buf):
            lag = _lag(root, name)
            if out != TIMED_OUT and (lag is None or lag <= LAG_OK_S):
                break                                   # it kept up: the failure is real
            where = "among the parallel runs" if s == float(speed) else "even alone"
            s = max(1.0, s / 2.0)
            why = "timed out" if out == TIMED_OUT else f"fell {lag:.1f} s behind real time"
            print(f"  ({name}: the simulation {why} {where} - running it again alone at clock x{s:g})")
            code, out = run_one(root, name, s, url)
            buf = _judge(root, name, code, out, srv)
        for kind, ok, text in buf:
            if kind == "say":
                print(text)
            else:
                check(ok, text)


def check_run(root, name, code, out, srv=None):
    say(f"run {name}: exit {code}")
    data = load_result(root, name)
    if name == "g_stubbed":
        convs = (data or {}).get("conversations") or []
        st = [c.get("status") for c in convs]
        if not check(code == 3 and st == ["error", "skipped"] and convs[0].get("bench_fatal"),
                     f"g: a function stubbed after the check stops the run: the first call fails, the "
                     f"rest are skipped, exit 3 ({st}, exit {code})"):
            say("    " + "\n    ".join((out or "").splitlines()[-15:]))
        sc = srv.state["stubbed_calls"] if srv is not None else {}
        check(sc.get("ping") == 1 and sc.get("tokens") == 1 and not sc.get("translate"),
              f"g: nothing more was asked of it ({dict(sc)})")
        return
    if not check(code == 0 and data is not None, f"{name}: finished and saved results"):
        say("    " + "\n    ".join((out or "").splitlines()[-25:]))
        return
    check(all(k in data for k in ("kind", "stamp", "config", "git", "conversations", "summary"))
          and data["kind"] == "interpreter", f"{name}: results follow the contract")
    c = data["conversations"][0]
    if not check(c.get("status") == "ok", f"{name}: conversation ran ({c.get('error')})"):
        say("    " + (c.get("traceback") or "")[-1500:])
        return
    hear, speak = c["directions"].get("hear"), c["directions"].get("speak")
    check(hear and speak and hear["n_turns"] == 4 and speak["n_turns"] == 3,
          f"{name}: both directions scored against their own track")
    check((c.get("sim") or {}).get("capture_errors") == 0, f"{name}: the engine never raised on the audio")
    check(not (c.get("errors") or {}), f"{name}: no engine errors ({c.get('errors')})")
    check(isinstance(c.get("reconnects"), dict) and isinstance(c.get("chars_charged"), int)
          and isinstance(c.get("echo_loops_by_kind"), dict) and (data["summary"].get("stt_audio_s") or 0) > 0,
          f"{name}: reconnects, characters charged, loop kinds and the STT's audio minutes are reported")
    if name == "a_clean":
        check(speak["stt"]["wer"] is not None and speak["stt"]["wer"] <= 0.15,
              f"a: speak WER {speak['stt']['wer']} (oracle STT, 3% word errors, no gate)")
        check(hear["n_phrases"] >= 2 and hear["stt"]["ref_units"] > 0, "a: hear phrases scored")
        ear = speak["latency"]["ear"]
        check(ear["n"] >= 2 and 0.3 < ear["p50"] < 5.0, f"a: speak mouth-to-ear p50 {ear['p50']} s")
        check(all((speak["latency"][k] or {}).get("n") for k in ("stt", "first_delta", "final")),
              "a: every stage of the delay is measured")
        check(speak["translation"]["chrf"] is not None and speak["translation"]["chrf"] > 40,
              f"a: speak translation chrF++ {speak['translation']['chrf']}")
        # the engine's own quality is what the bench reports, not what this test asserts: only that
        # the voice-matching numbers are there and add up (my voice is a clear ~100 Hz man's voice)
        g = speak.get("gender") or {}
        check(g.get("judged", 0) >= 1 and g.get("right", -1) <= g["judged"] and g.get("voice_judged", 0) >= 1
              and g.get("voice_right", -1) <= g["voice_judged"],
              f"a: my pitch is read as a man's; the voice used is scored ({g.get('voice_right')}/"
              f"{g.get('voice_judged')} voices right, {g.get('unknown')} phrase(s) before the pitch was known)")
        gh = hear.get("gender") or {}
        check(gh.get("judged", 0) >= 1 and "expected_switches" in gh,
              f"a: their voices are scored ({gh.get('right')}/{gh.get('judged')} by pitch, "
              f"{gh.get('switches')} voice switches, {gh.get('expected_switches')} expected)")
        check(c["echo_loops"] == 0 and c["crosstalk"] == 0, "a: headphones: no echo loop, no cross-talk")
        check(c["chars_spoken"] > 0 and c["chars_spoken"] == data["summary"]["chars_spoken"],
              f"a: characters counted ({c['chars_spoken']})")
        check(c["chars_charged"] == c["chars_spoken"] and c["reconnects"] == {"stt": 0, "voice": 0},
              f"a: what the budget charged = what was spoken ({c['chars_charged']}); no reconnects")
        check(isinstance(hear["stt_input"].get("muted_ratio"), float),
              f"a: what the gate muted is measured ({hear['stt_input'].get('muted_ratio')})")
    elif name == "b_speakers_no_gate":
        check(c["speakers_mode"] is False, "b: output=headphones keeps the mic gate off")
        n_echo = speak["counts"]["echo"] + speak["counts"]["echo_mixed"]
        check(n_echo >= 1 and c["echo_loops"] >= n_echo,
              f"b: echo loop found ({speak['counts']['echo']} phrase(s) of Gusan's voice, "
              f"{speak['counts']['echo_mixed']} mixed with mine; {c['echo_loops_by_kind']})")
        check(speak["counts"]["crosstalk"] >= 1, f"b: cross-talk found ({speak['counts']['crosstalk']} phrase(s))")
        check((speak["stt_input"].get("gusan_reached_stt_s") or 0) > 0.5,
              f"b: Gusan's voice reached the speak STT ({speak['stt_input'].get('gusan_reached_stt_s')} s)")
        check(speak["stt"]["wer_all"] > speak["stt"]["wer"],
              "b: the echo and cross-talk phrases are kept out of the STT WER but count in wer_all")
    elif name == "c_sockets":
        check(c["chars_spoken"] <= 150, f"c: the budget held ({c['chars_spoken']} <= 150)")
        sk = (speak["tts"].get("skipped") or {}).get("sample", 0) + (hear["tts"].get("skipped") or {}).get("sample", 0)
        check(sk >= 1 and speak["tts"]["spoken"] + hear["tts"]["spoken"] >= 1,
              f"c: half the phrases spoken, the rest skipped by the sampler ({sk} skipped)")
        check(c["session"]["tokens_after_start"] == {} or all(v >= 0 for v in c["session"]["tokens_after_start"].values()),
              "c: tokens came from the bench session")
        check(speak["stt"]["wer"] is not None and speak["stt"]["wer"] <= 0.2,
              f"c: the real ScribeRealtime client fed the fake socket (speak WER {speak['stt']['wer']})")
        check(hear["tts"]["warmup_chars"] + speak["tts"]["warmup_chars"] <= 10, "c: warm-ups counted")
    elif name == "d_captions":
        check(c["chars_spoken"] == 0 and hear["tts"]["spoken"] == 0 and speak["tts"]["spoken"] == 0,
              "d: captions only speaks nothing")
        check(speak["translation"]["chrf"] is not None and hear["translation"]["chrf"] is not None,
              "d: translations still scored")
        check(speak["latency"]["ear"]["n"] == 0 and speak["latency"]["final"]["n"] >= 1,
              "d: no mouth-to-ear, translation delays measured")
    elif name == "e_faults":
        fired = sorted({(f["kind"], f["language"]) for f in c.get("faults") or []})
        check(("stt_drop", "en") in fired and ("stt_drop", "hy") in fired,
              f"e: both live STT sockets hung up at 8 s ({fired})")
        check(c["reconnects"]["stt"] == 2, f"e: each STT reconnected with a fresh bench token ({c['reconnects']})")
        vd = sum(1 for k, _lang in fired if k == "voice_drop")
        check(vd >= 1 and c["reconnects"]["voice"] >= vd,
              f"e: the voice socket reopened after hanging up mid-phrase ({vd} drop(s), {c['reconnects']})")
        check(hear["counts"]["real"] >= 2 and speak["counts"]["real"] >= 2 and speak["latency"]["ear"]["n"] >= 2,
              "e: both directions kept translating and speaking after the drops")
    elif name == "f_http" and srv is not None:
        calls = srv.state["calls"]
        n_tr = sum(1 for p in c["phrases"] if p.get("translate"))
        check(calls["ping"] >= 1 and calls["tokens"] >= 1 and calls["translate"] >= n_tr >= 3,
              f"f: preflight, tokens and every translation went through the HTTP client ({dict(calls)})")
        routes = {(p.get("translate") or {}).get("route") for p in c["phrases"] if p.get("translate")}
        check(routes == {"fake-http"}, f"f: the function's route header is kept per phrase ({routes})")
        check((speak["translation"]["chrf"] or 0) > 40 and (hear["translation"]["chrf"] or 0) > 20,
              f"f: streamed translations scored (chrF++ speak {speak['translation']['chrf']}, "
              f"hear {hear['translation']['chrf']})")
        j = speak["translation"].get("judge") or {}
        check(calls["judge"] >= 1 and j.get("n", 0) >= 1 and j.get("adequacy") == 77,
              f"f: the judge was asked over HTTP ({j})")
        check(data["config"].get("url", "").startswith("http://127.0.0.1"), "f: the bench URL is in the config")
        # the app's own translate client asked: its request, one try, its think filter
        tried = [(p.get("translate") or {}).get("tries") for p in c["phrases"] if p.get("translate")]
        check((c.get("adapter") or {}).get("translate_client") == "interpreter.translate.translate_phrase"
              and tried and all(t == 1 for t in tried),
              f"f: the app's translate client asked the function, one try per phrase ({(c.get('adapter') or {}).get('translate_client')}, tries {sorted(set(tried))})")
        sent = list(srv.state["translate_bodies"])
        check(sent and all(len(b.get("context") or []) <= 3 and b.get("glossary") == ["Gusan", "Slack", "Aram", "Anna",
                                                                                        "Davit"] for b in sent),
              f"f: the request is the app's (its context, the Dictionary words) ({sent[:1]})")
        reused = srv.state["translate_reused"]
        opened = calls["translate"] - reused
        check(reused >= 1 and opened <= 4,
              f"f: each direction keeps its connection open, as the app's translate client ({opened} "
              f"connection(s) for {calls['translate']} phrases)")
        texts = [p.get("translation") or "" for p in c["phrases"]] + [t.get("translation") or "" for t in c["turns"]]
        live = [e.get("text") or "" for e in c.get("events") or [] if e.get("type") == "translation"]
        check(any(texts) and live and not any("think>" in x or "word for word" in x for x in texts + live),
              f"f: a leaked <think> block never reached a caption or a translation (the app's own filter; "
              f"{len(live)} translation events)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep", action="store_true", help="keep the temp folder")
    ap.add_argument("--speed", type=float, default=4.0, help="dry-run clock speed (default 4)")
    args = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    t0 = time.perf_counter()
    root = Path(tempfile.mkdtemp(prefix="gusan_interp_selftest_"))
    try:
        try:
            unit_checks()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            FAILS.append("unit checks raised")
        print("building the test call")
        tl = build_call(root)
        srv, url = start_bench(tl["turns"])
        print(f"  {len(tl['turns'])} turns, {tl['duration']} s; running {len(RUNS)} dry runs (clock x{args.speed}); "
              f"bench stand-in at {url}")
        try:
            outs = run_all(root, args.speed, url)
            check_runs(root, outs, srv, args.speed, url)
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        if args.keep:
            print(f"kept: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
    dt = time.perf_counter() - t0
    print(f"\n{'FAILED: ' + str(len(FAILS)) + ' check(s)' if FAILS else 'all checks passed'} in {dt:.1f} s")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
