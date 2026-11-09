"""Turn the corpus conversations into audio: one voice per speaker, one timeline, three tracks.

For each conversation (corpus/conversations/<id>.json) every turn is voiced with ElevenLabs
through the bench function (default eleven_v3; eleven_v4_turbo when v3 fails for a language),
cached by sha1(model|voice|lang|text) so a clip is paid for once, trimmed, levelled per
speaker, and laid on one timeline honouring gap_before (negative = a small overlap):

    corpus/audio/<id>/mic.wav        only "me" (the person using Gusan)
                      system.wav     everyone else (the call)
                      mix.wav        both
                      *_noisy.wav    café noise at ~15 dB SNR (babble, pink/brown noise, clinks)
                      mic_speakers.wav   mic + the call leaking from the speakers (0.3 x, 35 ms,
                                     short reverb); at run time the interpreter's own played
                                     voice must leak in the same way (timeline "leak")
                      mix_speakers.wav
                      timeline.json  exact start/end of every turn + the variant file names

Characters are counted against --max-chars (default 8000) and this month's ledger; a
conversation that does not fit is skipped before anything is spent.

    python generate.py --estimate          what it would cost, no calls
    python generate.py                     everything not generated yet (cache hits are free)
    python generate.py --only c01,c03      some conversations
    python generate.py --dry-run           offline fake voices -> corpus/audio_dry/
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

import benchaudio as ba
from bench_client import (BENCH_DIR, MAX_TTS_CHARS, MONTHLY_CHARS, BenchError, BudgetExceeded, CharBudget,
                          CharLedger, TTSCache, cache_key, cached_tts, is_fatal, make_bench, _utf8_console)

CORPUS_DIR = BENCH_DIR / "corpus"
SR = ba.SR
LEAD_IN = 0.5            # s of silence before the first turn
TAIL = 0.8               # s after the last
MIN_GAP = -0.6           # the largest overlap allowed
MIN_SELF_GAP = 0.08      # one person never overlaps themselves
TARGET_DB = -24.0        # speech level of every speaker (dBFS RMS of speech frames)
APP_DEFAULT_VOICES = {"EXAVITQu4vr4xnSDxMaL", "cjVigY5qzO86Huf0OWal"}   # Sarah, Eric: the app's Hear voices
VARIANTS = ("clean", "noisy", "speakers")
LEAK = {"gain": 0.3, "delay_ms": 35.0, "rt60_s": 0.3, "length_s": 0.3, "reverb_db": -6.0}


@dataclass
class Options:
    model: str = "eleven_v3"
    fallback: str = "eleven_v4_turbo"
    variants: tuple = VARIANTS
    snr_db: float = 15.0
    seed: int = 1234
    lang_hint: str = "single"           # single | always | never
    workers: int = 3
    dry_run: bool = False
    leak: dict = field(default_factory=lambda: dict(LEAK))
    model_for: dict = field(default_factory=dict)   # language -> model, before the default model

    def models(self, lang=None):
        """The models to try for a turn in ``lang``, in order."""
        first = self.model_for.get(lang or "", self.model)
        return [first] + ([self.fallback] if self.fallback and self.fallback != first else [])


def _seed(*parts):
    return int(hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:8], 16)


_NOTE_TAG = re.compile(r"#([a-z][a-z-]*)")


def note_tags(notes):
    """A turn's #tags ("#numbers #email ..." -> ["email", "numbers"]): run_stt / run_interpreter /
    report.py group the scores by them (corpus/DESIGN.md lists them)."""
    return sorted(set(_NOTE_TAG.findall(notes or "")))


# -- corpus ---------------------------------------------------------------------------------

def load_voices(corpus_dir=CORPUS_DIR):
    try:
        data = json.loads((Path(corpus_dir) / "voices.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("voices") or []
    return [v for v in data if isinstance(v, dict) and v.get("id")]


def _matches(cid, only):
    return not only or any(cid == o or cid.startswith(o) for o in only)


def load_conversations(corpus_dir=CORPUS_DIR, only=None):
    """[(path, conversation)] sorted by id; ``only``: ids or id prefixes."""
    out = []
    for path in sorted((Path(corpus_dir) / "conversations").glob("*.json")):
        try:
            conv = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"  ! {path.name}: not readable JSON ({e})", file=sys.stderr)
            continue
        if not isinstance(conv, dict):
            continue
        conv.setdefault("id", path.stem)
        if _matches(conv["id"], only):
            out.append((path, conv))
    return out


def validate(conv):
    """-> (errors, warnings). Errors stop that conversation."""
    errs, warns = [], []
    for key in ("id", "me", "speakers", "turns"):
        if not conv.get(key):
            errs.append(f"missing {key!r}")
    speakers = conv.get("speakers") or {}
    if conv.get("me") and conv.get("me") not in speakers:
        errs.append(f"me={conv.get('me')!r} is not one of the speakers")
    for key in ("my_lang", "their_lang"):
        if not conv.get(key):
            warns.append(f"missing {key!r}")
    for sid, sp in speakers.items():
        if (sp or {}).get("gender") not in ("female", "male", "neutral"):
            warns.append(f"speaker {sid}: gender {(sp or {}).get('gender')!r}")
    for i, t in enumerate(conv.get("turns") or []):
        where = f"turn {i}"
        if t.get("speaker") not in speakers:
            errs.append(f"{where}: unknown speaker {t.get('speaker')!r}")
        text = t.get("text") or ""
        if not text.strip():
            errs.append(f"{where}: empty text")
        if len(text) > MAX_TTS_CHARS:
            errs.append(f"{where}: {len(text)} characters (the bench voices at most {MAX_TTS_CHARS})")
        if not t.get("lang"):
            errs.append(f"{where}: no lang")
        gap = t.get("gap_before", 0)
        if not isinstance(gap, (int, float)):
            errs.append(f"{where}: gap_before {gap!r} is not a number")
        elif gap < MIN_GAP:
            warns.append(f"{where}: gap_before {gap} below {MIN_GAP} (clamped)")
    return errs, warns


def pick_voice(conv, sid, voices):
    """The speaker's voice: their own "voice", else a catalog voice of their gender that the
    app does not use for Hear (so the corpus voice is never the output voice)."""
    sp = (conv.get("speakers") or {}).get(sid) or {}
    if sp.get("voice"):
        return sp["voice"]
    used = {(s or {}).get("voice") for s in (conv.get("speakers") or {}).values()}
    gender = sp.get("gender") or "neutral"
    pool = [v["id"] for v in voices if v.get("gender") == gender and v["id"] not in APP_DEFAULT_VOICES]
    pool += [v["id"] for v in voices if v.get("gender") == gender and v["id"] in APP_DEFAULT_VOICES]
    pool = [v for v in pool if v not in used] or pool or [v["id"] for v in voices] or ["SAz9YHcvj6GT2YYXdXww"]
    return pool[_seed(conv.get("id"), sid) % len(pool)]


def plan_turns(conv, voices, opts):
    """One entry per turn with everything needed to voice and place it."""
    me = conv.get("me")
    speakers = conv.get("speakers") or {}
    plan = []
    for i, t in enumerate(conv.get("turns") or []):
        sid = t.get("speaker")
        langs = [x for x in (t.get("langs") or [t.get("lang")]) if x]
        if opts.lang_hint == "always" or (opts.lang_hint == "single" and len(set(langs)) <= 1):
            hint = t.get("lang")
        else:
            hint = None
        plan.append({"i": i, "speaker": sid, "track": "mic" if sid == me else "system",
                     "text": t.get("text") or "", "lang": t.get("lang"), "langs": langs or [t.get("lang")],
                     "lang_hint": hint, "voice": pick_voice(conv, sid, voices),
                     "gender": (speakers.get(sid) or {}).get("gender") or "neutral",
                     "ref": t.get("ref") or {}, "gap_before": t.get("gap_before") or 0.0,
                     "notes": t.get("notes") or ""})
    return plan


def _clip_id(p):
    """Turns with the same voice, language hint and text share one clip (paid once)."""
    return (p["voice"], p["lang_hint"], p["text"])


def cost(plan, cache, opts):
    """-> (all characters, characters not in the cache yet; a repeated line counts once)."""
    total = uncached = 0
    seen = set()
    for p in plan:
        n = len(p["text"])
        total += n
        if _clip_id(p) in seen:
            continue
        seen.add(_clip_id(p))
        if cache is None or not any(cache.has(cache_key(m, p["voice"], p["lang_hint"], p["text"]))
                                    for m in opts.models(p["lang"])):
            uncached += n
    return total, uncached


# -- voicing --------------------------------------------------------------------------------

_ELEVEN_REFUSED = re.compile(r"^elevenlabs (400|403|404|422)$")


def _model_refused(e):
    """True when this model refused the request (ElevenLabs 400 / 403 / 404 / 422, which the
    function passes on as a 502 "elevenlabs <status>", or the function's own 400 / 422 checks):
    a reason to try the next model. A rate limit, a timeout, ElevenLabs being down, a used-up
    quota (401), the token, the function or the network are not: no other model would do."""
    if e.status in (400, 422):
        return True
    return bool(_ELEVEN_REFUSED.match(e.code or ""))


class Voicer:
    """Voices turns through the cache, falling back to the next model for a language the first
    one fails on (remembered for the rest of the run)."""

    def __init__(self, bench, cache, budget, opts, log=print):
        self.bench, self.cache, self.budget, self.opts, self.log = bench, cache, budget, opts, log
        self.failed = {}                      # model -> {lang}
        self.billed = 0
        self.fatal = None                     # the first error that stops every call (token, stub, budget)
        self.dropped = set()                  # (model, lang): voiced without the language code
        self._lock = threading.Lock()

    def voice(self, p):
        if self.fatal is not None:
            raise self.fatal                  # the other workers stop too, without another call
        try:
            return self._voice(p)
        except BudgetExceeded as e:
            self.fatal = e
            raise
        except BenchError as e:
            if is_fatal(e):
                self.fatal = e
            raise

    def _voice(self, p):
        models = self.opts.models(p["lang"])
        for m in models:                      # any model's earlier clip is free
            key = cache_key(m, p["voice"], p["lang_hint"], p["text"])
            if self.cache is not None and self.cache.has(key):
                pcm, info = cached_tts(self.bench, self.cache, self.budget, p["text"], p["voice"], m, p["lang_hint"])
                return pcm, info
        last = None
        for m in models:
            if p["lang"] in self.failed.get(m, ()):
                continue
            try:
                pcm, info = cached_tts(self.bench, self.cache, self.budget, p["text"], p["voice"], m, p["lang_hint"])
            except BenchError as e:
                if not _model_refused(e):
                    raise                     # the token, the function or the network: no model would do
                last = e
                with self._lock:
                    self.failed.setdefault(m, set()).add(p["lang"])
                self.log(f"    {m} failed for {p['lang']} ({e}); trying the next model")
                continue
            with self._lock:
                self.billed += int(info.get("chars") or 0)
                warn = info.get("language_dropped") and (m, p["lang"]) not in self.dropped
                if warn:
                    self.dropped.add((m, p["lang"]))
            if warn:
                self.log(f"    note: {m} did not take language {p['lang']!r} and spoke it without the code: listen "
                         f"to it, or use --model-for {p['lang']}={self.opts.fallback or 'eleven_v4_turbo'}")
            return pcm, info
        raise last or BenchError(f"no model left for language {p['lang']}")


def voice_all(plan, voicer, workers=3):
    """-> [(float clip, info)] in plan order; a line repeated with the same voice is voiced once.
    BudgetExceeded / BenchError propagate."""
    first = {}
    for k, p in enumerate(plan):
        first.setdefault(_clip_id(p), k)
    out = [None] * len(plan)
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(voicer.voice, plan[k]): k for k in sorted(set(first.values()))}
        try:
            for fut in cf.as_completed(futs):
                pcm, info = fut.result()
                out[futs[fut]] = (ba.from_pcm16(pcm), info)
        except BaseException:
            for f in futs:
                f.cancel()
            raise
    for k, p in enumerate(plan):
        if out[k] is None:
            clip, info = out[first[_clip_id(p)]]
            out[k] = (clip.copy(), dict(info, cached=True, chars=0, repeat_of=first[_clip_id(p)]))
    return out


# -- assembling -----------------------------------------------------------------------------

def place_turns(plan, clips):
    """Start/end (s) of every turn on one timeline. gap_before counts from the moment the
    floor is free (the latest end so far); a negative gap overlaps by that much (>= -0.6 s);
    nobody overlaps themselves and turns keep their order."""
    floor_end = LEAD_IN
    prev_start = None
    speaker_end = {}
    spans = []
    for k, (p, clip) in enumerate(zip(plan, clips)):
        gap = max(MIN_GAP, float(p.get("gap_before") or 0.0))
        start = LEAD_IN + max(0.0, gap) if k == 0 else floor_end + gap
        if prev_start is not None:
            start = max(start, prev_start + 0.2)
        if p["speaker"] in speaker_end:
            start = max(start, speaker_end[p["speaker"]] + MIN_SELF_GAP)
        start = round(max(0.0, start) * SR) / SR
        end = start + len(clip) / SR
        spans.append((start, end))
        floor_end = max(floor_end, end)
        prev_start = start
        speaker_end[p["speaker"]] = end
    return spans


def _add(track, clip, start):
    a = int(round(start * SR))
    track[a:a + len(clip)] += clip[: max(0, len(track) - a)]


def build_tracks(plan, clips, spans):
    total = int(np.ceil((max(e for _, e in spans) + TAIL) * SR)) if spans else int(TAIL * SR)
    mic = np.zeros(total, np.float32)
    system = np.zeros(total, np.float32)
    for p, clip, (s, _e) in zip(plan, clips, spans):
        _add(mic if p["track"] == "mic" else system, clip, s)
    return mic, system


def generate_conversation(conv, bench, *, cache, budget, out_root, opts=None, voices=None, log=print):
    """Voice, assemble and write one conversation. -> its timeline (also written as
    <out_root>/<id>/timeline.json). Raises BudgetExceeded / BenchError / ValueError."""
    opts = opts or Options()
    voices = voices or []
    cid = conv["id"]
    errs, warns = validate(conv)
    if errs:
        raise ValueError(f"{cid}: " + "; ".join(errs))
    for w in warns:
        log(f"    note: {w}")
    plan = plan_turns(conv, voices, opts)
    voicer = Voicer(bench, cache, budget, opts, log)
    voiced = voice_all(plan, voicer, opts.workers)

    # trim, then one level per speaker
    clips = []
    for p, (x, _info) in zip(plan, voiced):
        y = ba.trim_silence(x)
        if len(y) < int(0.05 * SR):
            log(f"    note: turn {p['i']} is nearly silent after trimming; kept untrimmed")
            y = x
        if not len(y):
            raise ValueError(f"{cid}: turn {p['i']} came back with no audio")
        clips.append(y)
    gains = {}
    for sid in {p["speaker"] for p in plan}:
        gains[sid] = ba.speaker_gain([c for p, c in zip(plan, clips) if p["speaker"] == sid], TARGET_DB)
    clips = [(c * gains[p["speaker"]]).astype(np.float32) for p, c in zip(plan, clips)]

    spans = place_turns(plan, clips)
    mic, system = build_tracks(plan, clips, spans)
    out = Path(out_root) / cid
    out.mkdir(parents=True, exist_ok=True)
    (out / "timeline.json").unlink(missing_ok=True)     # never an old timeline next to new tracks
    files = {}

    def write(name, x):
        y, g = ba.limit_peak(x, -1.0)
        ba.write_wav(out / name, y)
        files[name] = {"peak_db": round(ba.amp_to_db(ba.peak(y)), 2), "limited_db": round(ba.amp_to_db(g), 2)}

    write("mic.wav", mic)
    write("system.wav", system)
    write("mix.wav", mic + system)
    variants = {"clean": {"mic": "mic.wav", "system": "system.wav", "mix": "mix.wav"}}
    extra = {}
    if "noisy" in opts.variants:
        sources = clips                                        # babble from other turns, shifted
        rng_m = np.random.default_rng(_seed(cid, opts.seed, "noise-mic"))
        rng_s = np.random.default_rng(_seed(cid, opts.seed, "noise-system"))
        ref_level = ba.active_rms(system) or ba.active_rms(mic)
        mic_n, _ = ba.add_noise_at_snr(mic, ba.cafe_noise(len(mic), rng_m, sources), opts.snr_db, ref_level=ref_level)
        sys_n, _ = ba.add_noise_at_snr(system, ba.cafe_noise(len(system), rng_s, sources), opts.snr_db,
                                       ref_level=ba.active_rms(mic))
        write("mic_noisy.wav", mic_n)
        write("system_noisy.wav", sys_n)
        write("mix_noisy.wav", mic_n + sys_n)
        variants["noisy"] = {"mic": "mic_noisy.wav", "system": "system_noisy.wav", "mix": "mix_noisy.wav"}
        extra["noise"] = {"snr_db": opts.snr_db, "seed": opts.seed,
                          "kind": "cafe: 6-talker babble from time-shifted turns + pink + brown noise + clinks"}
    if "speakers" in opts.variants:
        lk = dict(LEAK, **(opts.leak or {}))
        ir_seed = _seed(cid, opts.seed, "room")
        ir = ba.room_ir(SR, lk["delay_ms"], lk["rt60_s"], lk["length_s"], ir_seed, lk["reverb_db"])
        mic_sp = ba.apply_leak(mic, system, lk["gain"], ir)
        write("mic_speakers.wav", mic_sp)
        write("mix_speakers.wav", mic_sp + system)
        np.save(out / "leak_ir.npy", ir)
        variants["speakers"] = {"mic": "mic_speakers.wav", "system": "system.wav", "mix": "mix_speakers.wav"}
        extra["leak"] = dict(lk, seed=ir_seed, ir_file="leak_ir.npy", interpreter_playback_leaks=True,
                             note=("Speakers mode: the mic hears the call AND, at run time, the interpreter's "
                                   "own played voice. Leak the played translation into the mic the same way: "
                                   "benchaudio.apply_leak(mic, played, gain, numpy.load(ir_file)) or "
                                   "benchaudio.room_ir(16000, delay_ms, rt60_s, length_s, seed, reverb_db)."))

    speakers = conv.get("speakers") or {}
    turns = []
    for p, (s, e), (_x, info) in zip(plan, spans, voiced):
        turns.append({"i": p["i"], "speaker": p["speaker"], "track": p["track"], "lang": p["lang"],
                      "langs": p["langs"], "text": p["text"], "ref": p["ref"], "gender": p["gender"],
                      "start": round(s, 3), "end": round(e, 3), "voice": p["voice"],
                      "model": info.get("model"), "gap_before": p["gap_before"], "notes": p["notes"],
                      "tags": note_tags(p["notes"])})
        if info.get("language_dropped"):
            turns[-1]["tts_language_dropped"] = True
    timeline = {
        "id": cid, "sample_rate": SR, "duration": round(len(mic) / SR, 3), "turns": turns,
        "variants": variants, "chars": sum(len(p["text"]) for p in plan), "chars_billed": voicer.billed,
        "title": conv.get("title") or "", "scenario": conv.get("scenario") or "", "tags": conv.get("tags") or [],
        "me": conv.get("me"), "my_lang": conv.get("my_lang"), "their_lang": conv.get("their_lang"),
        "speakers": {sid: {"name": (sp or {}).get("name") or sid, "gender": (sp or {}).get("gender"),
                           "voice": pick_voice(conv, sid, voices)} for sid, sp in speakers.items()},
        "glossary": conv.get("glossary") or [], "dry_run": bool(opts.dry_run),
        "levels": {"target_db": TARGET_DB, "speaker_gain_db": {k: round(ba.amp_to_db(g), 2) for k, g in gains.items()},
                   "files": files},
        "generated": datetime.now().isoformat(timespec="seconds"),
    }
    timeline.update(extra)
    (out / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=1), encoding="utf-8")
    return timeline


# -- CLI ------------------------------------------------------------------------------------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Voice the corpus conversations into timeline-aligned tracks.")
    ap.add_argument("--only", default="", help="comma-separated conversation ids (or id prefixes)")
    ap.add_argument("--model", default="eleven_v3", help="TTS model (default eleven_v3)")
    ap.add_argument("--fallback", default="eleven_v4_turbo",
                    help="model for a language the first one fails on ('' = none)")
    ap.add_argument("--model-for", default="",
                    help="a model per language before the default, e.g. ka=eleven_v4_turbo,hy=eleven_v4_turbo")
    ap.add_argument("--max-chars", type=int, default=8000, help="characters this run may spend (default 8000)")
    ap.add_argument("--monthly-cap", type=int, default=MONTHLY_CHARS,
                    help=f"stop before the month's ledger passes this (default {MONTHLY_CHARS})")
    ap.add_argument("--variants", default=",".join(VARIANTS), help="clean,noisy,speakers")
    ap.add_argument("--snr", type=float, default=15.0, help="SNR of the noisy variant in dB")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--lang-hint", choices=("single", "always", "never"), default="single",
                    help="send the turn's language to TTS: single-language lines only (default), always, never")
    ap.add_argument("--workers", type=int, default=3, help="parallel TTS calls")
    ap.add_argument("--estimate", action="store_true", help="only print what generating would cost")
    ap.add_argument("--dry-run", action="store_true", help="fake voices, offline (writes corpus/audio_dry)")
    ap.add_argument("--corpus", default=str(CORPUS_DIR))
    ap.add_argument("--audio-dir", default="", help="default corpus/audio (corpus/audio_dry for --dry-run)")
    ap.add_argument("--cache-dir", default="", help="default corpus/cache (corpus/cache_dry for --dry-run)")
    ap.add_argument("--url", default="")
    ap.add_argument("--ledger", default="", help="character ledger file (default results/chars_ledger.json)")
    return ap.parse_args(argv)


def default_dirs(corpus, dry_run):
    corpus = Path(corpus)
    return (corpus / ("audio_dry" if dry_run else "audio"), corpus / ("cache_dry" if dry_run else "cache"))


def main(argv=None):
    _utf8_console()
    args = parse_args(argv)
    only = [x.strip() for x in args.only.split(",") if x.strip()]
    model_for = {}
    for pair in [x.strip() for x in args.model_for.split(",") if x.strip()]:
        lang, _, model = pair.partition("=")
        if not lang.strip() or not model.strip():
            print(f"--model-for: {pair!r} is not lang=model", file=sys.stderr)
            return 2
        model_for[lang.strip()] = model.strip()
    opts = Options(model=args.model, fallback=args.fallback or "", model_for=model_for,
                   variants=tuple(v.strip() for v in args.variants.split(",") if v.strip()),
                   snr_db=args.snr, seed=args.seed, lang_hint=args.lang_hint, workers=args.workers,
                   dry_run=args.dry_run)
    bad = [v for v in opts.variants if v not in VARIANTS]
    if bad:
        print(f"unknown variant(s): {', '.join(bad)} (choose from {', '.join(VARIANTS)})", file=sys.stderr)
        return 2
    audio_dir, cache_dir = default_dirs(args.corpus, args.dry_run)
    audio_dir = Path(args.audio_dir) if args.audio_dir else audio_dir
    cache = TTSCache(Path(args.cache_dir) if args.cache_dir else cache_dir)
    voices = load_voices(args.corpus)
    convs = load_conversations(args.corpus, only)
    if not convs:
        print(f"no conversations in {Path(args.corpus) / 'conversations'}" + (f" matching {only}" if only else ""))
        return 1

    if args.dry_run:
        ledger = None
    else:
        ledger = CharLedger(args.ledger) if args.ledger else CharLedger()
    budget = CharBudget(args.max_chars, monthly_cap=None if args.dry_run else args.monthly_cap, ledger=ledger)
    month_used = ledger.month_total() if ledger else 0

    if args.estimate:
        print(f"{'conversation':<34}{'turns':>6}{'chars':>8}{'to pay':>8}")
        tot = pay = 0
        for _path, conv in convs:
            errs, _ = validate(conv)
            if errs:
                print(f"{conv['id']:<34}  invalid: {'; '.join(errs)[:80]}")
                continue
            plan = plan_turns(conv, voices, opts)
            a, b = cost(plan, cache, opts)
            tot, pay = tot + a, pay + b
            print(f"{conv['id']:<34}{len(plan):>6}{a:>8}{b:>8}")
        print(f"{'total':<34}{'':>6}{tot:>8}{pay:>8}")
        print(f"this run may spend {args.max_chars}; this month {month_used} of {args.monthly_cap} used so far")
        return 0

    bench = make_bench(args.dry_run, args.url or None, purpose="generate", voices=voices,
                       **({} if args.dry_run else {"ledger": ledger.path if ledger else None}))
    print(f"{'DRY RUN (fake voices) ' if args.dry_run else ''}{len(convs)} conversation(s) -> {audio_dir}")
    print(f"budget: {budget.status()}" + ("" if args.dry_run else f"; month so far {month_used}/{args.monthly_cap}"))
    if not args.dry_run:
        try:
            u = bench.usage()
            print(f"ElevenLabs account: {u.get('used')} of {u.get('limit')} characters used ({u.get('left')} left)")
            if isinstance(u.get("left"), (int, float)) and u["left"] < budget.remaining:
                print("  note: the account has fewer characters left than this run may spend")
        except BenchError as e:
            print(f"(the account's own count is not readable with this key: {str(e)[:120]})")
    done, skipped, failed = [], [], []
    for _path, conv in convs:
        cid = conv["id"]
        errs, _w = validate(conv)
        if errs:
            print(f"- {cid}: invalid, skipped: {'; '.join(errs)}")
            failed.append(cid)
            continue
        plan = plan_turns(conv, voices, opts)
        total, need = cost(plan, cache, opts)
        if need > budget.remaining:
            print(f"- {cid}: needs {need} characters, {budget.remaining} left - skipped")
            skipped.append(cid)
            continue
        t0 = time.perf_counter()
        print(f"- {cid}: {len(plan)} turns, {total} characters ({need} to pay)")
        try:
            tl = generate_conversation(conv, bench, cache=cache, budget=budget, out_root=audio_dir, opts=opts,
                                       voices=voices, log=print)
        except BudgetExceeded as e:
            print(f"  stopped: {e}")
            skipped.append(cid)
            break
        except BenchError as e:
            print(f"  failed: {e}")
            failed.append(cid)
            if is_fatal(e):
                print("  stopping: the other conversations would fail the same way")
                break
            continue
        except (ValueError, OSError) as e:
            print(f"  failed: {e}")
            failed.append(cid)
            continue
        mixed = sum(1 for t in tl["turns"] if len(set(t["langs"])) > 1)
        models = sorted({t["model"] for t in tl["turns"] if t.get("model")})
        print(f"  {tl['duration']:.1f} s, {len(tl['turns'])} turns ({mixed} mixed-language), "
              f"{tl['chars_billed']} characters billed, models {', '.join(models)}, "
              f"{time.perf_counter() - t0:.1f} s; budget: {budget.status()}")
        done.append(cid)
    print(f"done {len(done)}, skipped {len(skipped)}, failed {len(failed)}; {budget.status()}")
    if ledger:
        print(f"this month: {ledger.month_total()} of {args.monthly_cap} ElevenLabs characters used")
    if skipped:
        print("skipped (budget): " + ", ".join(skipped))
    if failed:
        print("failed: " + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
