"""Run the real Gusan Interpreter on the generated conversations and measure it.

    python run_interpreter.py                                  all conversations, clean audio
    python run_interpreter.py --only c01 --variant speakers    one call on laptop speakers
    python run_interpreter.py --captions-only                  STT + translation only (no characters)
    python run_interpreter.py --tts-sample 0.25 --max-chars 800
    python run_interpreter.py --dry-run --speed 4              offline: fake providers, 4x faster
    python run_interpreter.py --dry-run sockets --fault stt_drop@12,voice_drop@20   offline reconnects

What runs: the REAL engine - interpreter/controller.py Interpreter with the real pipeline (phrase
cutting, keep-up rules), eleven.ScribeRealtime + eleven.DialogueVoice, pitch-based voice matching
and OwnVoiceCanceller / ListenGate - on simulated devices (sim_audio.SimAudio). Per conversation
both directions run at once, like in a call: HEAR listens to the call track (their_lang ->
my_lang, played to the headphones / speakers), SPEAK to the mic track (my_lang -> their_lang,
played into the virtual microphone). The bench function stands in for the app's server: the same
single-use ElevenLabs tokens (BenchSession) and the same translation as smart-action "interpret",
asked by the app's own translate client (interpreter/translate.translate_phrase: its request, ONE try
per phrase, its timeouts, think filter and cleaning) through BenchHTTP; --route picks the model chain.

Dry runs (no network): --dry-run (= oracle) swaps the ElevenLabs clients for sim_audio's oracles;
--dry-run sockets keeps the REAL ScribeRealtime / DialogueVoice and gives them in-process fake
sockets. Both use FakeBench for tokens and translation (answers from the reference translations).

What is measured, per phrase the engine cut and per turn of the script:
  accuracy  STT WER / CER of each direction's phrases against its track (metrics.score_transcript,
            the same scoring as run_stt.py); translation chrF++ against the reference (end to end:
            dropped turns and stray phrases count) and on matched turns only; an LLM judge with
            --judge (--judge-route openai / mistral: another model than the translator's); the voice
            used against the speaker's real gender
  speed     speech end -> STT commit -> first translated words -> translation done -> first audio
            played (mouth-to-ear), each stage's share, the end-of-turn delays and the delay the app
            itself shows ("spoken" latency)
  failures  echo loops (a phrase that is Gusan's own voice, carries some of it along with mine, or
            came while only Gusan's voice played), cross-talk (the call heard by the mic through
            speakers), dropped turns, merged / duplicated / stray phrases, speech the gate muted,
            Gusan's voice reaching the STT, errors, reconnects (a new single-use token after the
            start), voice sockets left open
  cost      ElevenLabs characters spoken (--max-chars caps them; --tts-sample speaks a share of the
            phrases; --captions-only speaks none) and the minutes of audio the live STT was fed.
            Real runs add the characters to results/chars_ledger.json.

Phrases are matched to the script by an alignment that keeps the most exact matches, each word
only inside its own turn's time; the words of a phrase that are not the script's are checked
against what Gusan said (echo) and what the call said (cross-talk), two neighbouring words at a time.

Times: every time is on the simulation clock (seconds). "Speech end" of a phrase is where its last
word ends on the timeline: the turn's end when that is the turn's last word (turns are placed
exactly by generate.py), else the start of the voice's pause - or of the gate muting the STT's
input - nearest the estimate (words spread over the turn by length), else the estimate
(speech_end_by says which). The delays counted from the speech end (stt, first_delta, final, ear)
use the phrases whose end is known (turn end, pause, gate); turn_commit / turn_ear count from the
exact end of each turn. Results:
results/interpreter_<YYYYmmdd-HHMMSS>.json (layout below) + a summary table.

Results layout (besides the shared kind / stamp / config / git):
  conversations[]: {id, title, variant, my_lang, their_lang, pair, status (ok | error | skipped),
      error, duration_s, cut_s, speakers_mode, directions_run, timing, sim, chars_spoken,
      chars_would_speak, chars_charged, echo_loops, echo_loops_by_kind{echo, echo_mixed,
      echo_suspect}, echo_chain_max, crosstalk, reconnects{stt, voice}, errors{code: n},
      error_messages[], leaks, faults[] (sockets dry runs), directions: {hear|speak: DIR},
      turns[], phrases[]}
  DIR: {src, dst, track, n_turns, n_phrases, stt{wer, cer, wer_all, errors, ref_units, ..., by_lang,
      mixed, single}, translation{chrf, chrf_matched, chrf_stats, chrf_matched_stats, groups,
      judge}, latency{stt, first_delta, final, ear, turn_commit, turn_ear, first_caption, app},
      stages{translate_wait, ttft, translate_rest, speak_wait, ttfb, play_wait}, counts{phrases,
      real, extra, echo, echo_mixed, echo_suspect, crosstalk, crosstalk_mixed, duplicates,
      merged_turns, split_turns, turns, dropped_turns, partial_turns, untranslated,
      translate_errors}, gender{...} | null, tts{...}, stt_input{speech_s, muted_s, muted_ratio,
      gusan_present_s, gusan_reached_stt_s, sent_s, ...}, reconnects{stt, voice},
      speech_end_by{turn_end, pause, gate, estimate}}
  Each latency / stage is metrics.summary(): {n, mean, p50, p90, p95, min, max} in seconds.
  summary: {conversations, ok, failed, skipped, chars_spoken, chars_would_speak, chars_charged,
      echo_loops, echo_loops_by_kind, echo_chain_max, crosstalk, errors, reconnects, stt_audio_s,
      leaks, sim, hear: AGG, speak: AGG, by_pair{pair: {hear, speak}}}
  AGG: {wer, cer, wer_all, chrf, chrf_matched, judge, latency{...}, stages{...}, counts{...},
      gender{...}, tts{...}, stt_input{...}, reconnects{...}, mixed{wer, chrf}, single{wer, chrf}}
"""
import argparse
import collections
import concurrent.futures as cf
import functools
import inspect
import json
import logging
import math
import os
import queue
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
for _p in (str(REPO), str(HERE)):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

import numpy as np  # noqa: E402

import bench_client  # noqa: E402
import generate  # noqa: E402
import metrics  # noqa: E402
import run_stt  # noqa: E402
import sim_audio  # noqa: E402
from sim_audio import RATE, Utterance  # noqa: E402

log = logging.getLogger("bench.interpreter")

RESULTS_DIR = HERE / "results"
DIRECTIONS = ("hear", "speak")
TRACK = {"hear": "system", "speak": "mic"}
CAPTURE = {"hear": "loopback", "speak": "mic"}
TOKEN_KIND = {"scribe": "realtime_scribe", "tts": "ttd_websocket"}
BIG = 10 ** 7
DEFAULT_MAX_CHARS = 1500
JUDGE_BATCH = 20
LATE_MATCH_S = 20.0      # a phrase holds words of a turn that ended at most this long before it was cut
ECHO_WINDOW_S = 25.0     # ... of Gusan's voice that stopped playing at most this long before
CROSS_WINDOW_S = 20.0    # ... of the call (through the speakers) that ended at most this long before
PAIR_GAP = 3             # echo / cross-talk words: two neighbours found at most this far apart in the source
PAUSE_S = 0.15           # a pause inside a turn this long can end a phrase (sentence ends, most commas)
SNAP_S = 0.6             # a phrase's estimated speech end moves to a pause start at most this far away


def engine():
    """The real interpreter modules (imported on first use, so --help works without them)."""
    from interpreter import controller, eleven, voices
    return controller, eleven, voices


# ── small helpers ────────────────────────────────────────────────────────────────────────────
def _filtered(fn, kwargs):
    """(the kwargs ``fn`` takes, the names it does not): the engine's small API details may still
    change while it is being fixed, and this keeps the adapter working."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return dict(kwargs), []
    if any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
        return dict(kwargs), []
    ok = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return ok, sorted(set(kwargs) - set(ok))


def _accepts(fn, name):
    return name in _filtered(fn, {name: None})[0]


def _fatal(e):
    """A wrong / missing token or a missing / stubbed function: no later conversation can run."""
    is_fatal = getattr(bench_client, "is_fatal", None)
    if is_fatal is not None:
        return bool(is_fatal(e))
    return isinstance(e, bench_client.BenchError) and e.status in bench_client.FATAL_STATUS


def _r(x, d=4):
    return None if x is None else round(float(x), d)


def _pct(x):
    return "   -  " if x is None else f"{100 * x:5.1f}%"


def _num(x, d=2, w=6):
    return " " * (w - 1) + "-" if x is None else f"{x:{w}.{d}f}"


class Spend:
    """The run's ElevenLabs characters (bench_client.CharBudget, shared by all conversations of
    a run): take(n) -> True when n more characters may be spent (and counts them)."""

    def __init__(self, max_chars, monthly_cap=None, ledger=None):
        self.budget = bench_client.CharBudget(max_chars, monthly_cap=monthly_cap, ledger=ledger)
        self.refused = 0
        self.exhausted = False
        self._lock = threading.Lock()

    def take(self, n):
        n = int(n)
        if n <= 0:
            return True
        try:
            self.budget.charge(n)
            return True
        except bench_client.BudgetExceeded:
            with self._lock:
                self.refused += 1
                if not self.exhausted:
                    self.exhausted = True
                    log.warning("character budget reached (%s): later phrases are not spoken", self.budget.status())
            return False

    @property
    def used(self):
        return self.budget.used


class Sampler:
    """Says yes to an evenly spread share of the calls, starting with the first (deterministic):
    0.25 -> phrases 1, 5, 9, ..."""

    def __init__(self, fraction):
        self.f = min(1.0, max(0.0, float(fraction)))
        self.k = 0
        self._lock = threading.Lock()

    def take(self):
        with self._lock:
            k, self.k = self.k, self.k + 1
        off = 1.0 - self.f
        return math.floor((k + 1) * self.f + off + 1e-9) > math.floor(k * self.f + off + 1e-9)


# ── the server stand-in ──────────────────────────────────────────────────────────────────────
class BenchSession:
    """Stands in for interpreter.session.InterpreterSession (same methods): the single-use tokens
    come from the bench function (BenchClient.tokens); no account, no allowance. Hear speaks in the
    app's VoiceChoice; Speak in --clone-voice-id when given (own_voice), else the choice too."""

    def __init__(self, bench, *, voice_choice=None, clone_voice_id=None, need_tts=True, tts_model=None,
                 stt_model=None):
        self.bench = bench
        self.voice_choice = voice_choice
        self.clone_voice_id = clone_voice_id or None
        self.need_tts = need_tts
        self.tts_model = tts_model          # what interpreter-session would name (the engine's own default)
        self.stt_model = stt_model
        self.id = ""
        self.info = {}
        self.seconds_left = BIG
        self.requested = collections.Counter()      # (kind, direction) -> tokens asked for after the start
        self.start_s = None
        self.beats = 0
        self.stopped_with = None
        self._lock = threading.Lock()

    def _tokens(self, kinds, n):
        data = self.bench.tokens(list(kinds), n=n) or {}
        toks = data.get("tokens") if isinstance(data.get("tokens"), dict) else data
        out = {}
        for k in kinds:
            items = toks.get(k) or data.get(k) or []
            out[k] = [it.get("token") if isinstance(it, dict) else str(it) for it in items]
            if len(out[k]) < n or not all(out[k]):
                raise RuntimeError(f"bench tokens: got {len(out[k])} {k} token(s), needed {n}")
        return out

    def start(self, hear=None, speak=None, **_kw):
        dirs = [d for d, v in (("hear", hear), ("speak", speak)) if v]
        kinds = ["realtime_scribe"] + (["ttd_websocket"] if self.need_tts else [])
        t0 = time.perf_counter()
        toks = self._tokens(kinds, len(dirs)) if dirs else {}
        self.start_s = round(time.perf_counter() - t0, 3)
        ids = self.voice_choice.all_ids() if self.voice_choice is not None else []
        voices = {}
        if hear:
            voices["hear"] = ids[0] if ids else ""
        if speak:
            voices["speak"] = self.clone_voice_id or (ids[-1] if ids else "")
        self.id = f"bench-{int(time.time() * 1000)}"
        self.info = {
            "session_id": self.id, "seconds_left": BIG, "allowance_seconds": BIG,
            "stt_model": self.stt_model or "scribe_v2_realtime", "tts_model": self.tts_model or "eleven_v4_turbo",
            "scribe_tokens": {d: toks["realtime_scribe"][i] for i, d in enumerate(dirs)},
            "tts_tokens": {d: toks["ttd_websocket"][i] for i, d in enumerate(dirs)} if self.need_tts else {},
            "voices": voices, "own_voice": bool(self.clone_voice_id and speak),
        }
        return self.info

    def token(self, kind, direction=None, **_kw):
        with self._lock:
            self.requested[(kind, direction or "")] += 1
        k = TOKEN_KIND.get(kind, kind)
        return self._tokens([k], 1)[k][0]

    def heartbeat(self, seconds, **_kw):
        self.beats += 1
        return self.seconds_left

    def stop(self, seconds, **_kw):
        self.stopped_with = seconds
        return self.seconds_left


def _app_translate():
    """The app's own translate client, interpreter/translate.translate_phrase, when it still takes a
    requests-like ``session`` (else None: the runner then calls the bench client directly)."""
    try:
        from interpreter import translate as app_translate
    except Exception:  # noqa: BLE001
        return None
    fn = getattr(app_translate, "translate_phrase", None)
    return fn if fn is not None and _accepts(fn, "session") else None


class _AppAnswer:
    """What translate_phrase reads back from its POST: smart-action's event stream (text/event-stream,
    OpenAI-style delta events) made from the bench function's text stream as it arrives - or an error
    answer carrying the function's HTTP status (the app turns those into its TranslateError)."""

    def __init__(self, status, headers, q=None, body=None, first=None):
        import requests
        self.status_code = int(status)
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        self.encoding = None
        self._q = q
        self._body = body
        self._first = first

    def json(self):
        if self._body is None:
            raise ValueError("not a JSON answer")
        return self._body

    def iter_lines(self, *_a, **_k):
        import requests
        if self._q is None:
            return
        while True:
            if self._first is not None:
                (kind, val), self._first = self._first, None
            else:
                try:
                    kind, val = self._q.get(timeout=180)
                except queue.Empty:
                    raise requests.ConnectionError("bench translate: no word for 180 s")
            if kind == "delta":
                if val:
                    yield ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": val}}]},
                                                 ensure_ascii=False)).encode("utf-8")
            elif kind == "done":
                yield b"data: [DONE]"
                return
            elif kind == "error":
                raise requests.ConnectionError(f"{type(val).__name__}: {val}"[:300])

    def close(self):
        pass


class _Lanes:
    """Where BenchHTTP asks each phrase: one kept worker thread per engine thread that asks (each
    direction translates on its own worker), so BenchClient's requests.Session per thread keeps that
    direction's connection open - as the app does (interpreter/translate.py keeps one Session per
    thread: only a direction's first phrase pays the connection set-up). A lane still busy with an
    earlier answer (one the app stopped reading) is not waited for: that phrase gets a thread, and a
    connection, of its own, as the app's connection pool would open one."""

    def __init__(self):
        self._lock = threading.Lock()
        self._lanes = {}                 # the asking thread's id -> {"q", "busy"}

    def run(self, fn):
        key = threading.get_ident()
        with self._lock:
            lane = self._lanes.get(key)
            if lane is None:
                lane = self._lanes[key] = {"q": queue.Queue(), "busy": False}
                threading.Thread(target=self._serve, args=(lane,), name="bench-translate", daemon=True).start()
            if lane["busy"]:
                threading.Thread(target=fn, name="bench-translate-extra", daemon=True).start()
                return
            lane["busy"] = True
        lane["q"].put(fn)

    def _serve(self, lane):
        while True:
            fn = lane["q"].get()
            try:
                fn()
            except BaseException:  # noqa: BLE001 - the phrase's own queue already has the error
                log.debug("bench translate lane", exc_info=True)
            finally:
                with self._lock:
                    lane["busy"] = False


_LANES = _Lanes()


class BenchHTTP:
    """A requests-like session for the app's translate_phrase: its one POST to smart-action (mode
    "interpret") goes to the bench function's "translate" action instead - the phrase, languages,
    context and Dictionary words the app itself picked, one try as the app makes it (no retries),
    with the app's own timeouts - and the answer goes back as smart-action's event stream, so the
    app's own code reads it (think filter, cleaning, error codes). ``timings``: the bench client's.

    Each direction's phrases are asked on one kept thread (_Lanes), so BenchClient (one
    requests.Session per thread) keeps one HTTPS connection open per direction - as the app does:
    translate_phrase keeps one requests.Session per thread, so only a direction's first phrase pays
    its connection set-up."""

    def __init__(self, bench, route):
        self.bench = bench
        self.route = route
        self.timings = None
        self.sent = None

    def post(self, url, json=None, headers=None, stream=False, timeout=None, **_kw):
        import requests
        body = json if isinstance(json, dict) else {}
        self.sent = {k: body.get(k) for k in ("text", "source_lang", "target_lang", "context", "glossary")}
        q = queue.Queue()

        def work():
            try:
                _out, tm = self.bench.translate_stream(
                    body.get("text") or "", body.get("source_lang"), body.get("target_lang"),
                    context=list(body.get("context") or ()), glossary=list(body.get("glossary") or ()),
                    route=self.route, on_delta=lambda p: q.put(("delta", p)), on_open=lambda h: q.put(("open", h)),
                    timeout=timeout, retries=0)
                self.timings = tm
                q.put(("done", tm))
            except BaseException as e:  # noqa: BLE001 - the app's error path reads it
                q.put(("error", e))
                if not isinstance(e, Exception):
                    raise
        _LANES.run(work)
        try:
            kind, val = q.get(timeout=180)
        except queue.Empty:
            raise requests.ConnectionError("bench translate: no answer for 180 s")
        if kind == "error":
            status = getattr(val, "status", None)
            if status is None:                      # never answered (network, no token): the app's "network"
                raise requests.ConnectionError(f"{type(val).__name__}: {val}"[:300])
            err = getattr(val, "body", None)
            return _AppAnswer(status, {"Content-Type": "application/json"}, body=err if isinstance(err, dict) else {})
        hdrs = requests.structures.CaseInsensitiveDict(val if kind == "open" and val else {})
        hdrs["Content-Type"] = "text/event-stream"
        return _AppAnswer(200, hdrs, q=q, first=None if kind == "open" else (kind, val))


def make_translate(bench, route, run):
    """The engine's translate(text, a, b, ctx, on_delta, token=, glossary=). It runs the app's own
    translate client (interpreter/translate.translate_phrase: the request it builds, one try, its
    timeouts, its think filter and cleaning) with BenchHTTP as its session, so every phrase goes to
    the bench function's "translate": smart-action "interpret"'s prompt, model chain and parameters
    (--route picks the chain). Should that client stop taking a session, BenchClient.translate_stream
    is called directly (still one try). Every call's timings are kept on the run."""
    app_fn = _app_translate()

    def bench_translate(text, a, b, ctx, on_delta, token=None, glossary=()):
        call = {"direction": run.direction_of(a, b), "text": text, "src": a, "dst": b, "t_call": run.clock(),
                "t_first": None, "t_done": None, "out": None, "error": None, "timings": None}
        run.add_call(call)

        def delta(piece):
            if call["t_first"] is None and (piece or "").strip():
                call["t_first"] = run.clock()
            on_delta(piece)
        http = BenchHTTP(bench, route) if app_fn is not None else None
        try:
            if http is not None:
                out = app_fn(text, a, b, list(ctx or ()), delta, token=token or "bench", glossary=list(glossary or ()),
                             session=http)
                timings = http.timings
            else:
                out, timings = bench.translate_stream(text, a, b, context=list(ctx or ()), glossary=list(glossary or ()),
                                                      route=route, on_delta=delta, retries=0)
        except Exception as e:  # noqa: BLE001 - the pipeline reports it as a translate error
            call["error"], call["t_done"] = f"{type(e).__name__}: {e}"[:300], run.clock()
            call["timings"] = http.timings if http is not None else None
            raise
        call["t_done"], call["out"], call["timings"] = run.clock(), out, timings
        return out
    bench_translate.client = ("interpreter.translate.translate_phrase" if app_fn is not None
                              else "bench_client.translate_stream")
    return bench_translate


# ── wrappers around the engine's providers ───────────────────────────────────────────────────
def make_stt_cls(inner_factory, run):
    """A class with ScribeRealtime's constructor that wraps the real (or dry-run) client: it keeps
    what the client was sent (on the timeline) and every event it raised."""

    class BenchSTT:
        def __init__(self, token, language, on_event, **kw):
            self._inner = None
            self.direction = run.direction_for_src(language)
            run.stt[self.direction]["sessions"] += 1

            def wrapped(kind, text=""):
                run.note_stt(self.direction, kind, text)
                on_event(kind, text)
            self._inner = inner_factory(token, language, wrapped, **kw)

        def start(self):
            t0 = run.clock()
            self._inner.start()
            run.stt[self.direction]["connect_s"].append(run.clock() - t0)

        def send_audio(self, pcm):
            run.note_stt_audio(self.direction, pcm, sim_audio.current_chunk())
            self._inner.send_audio(pcm)

        def close(self):
            self._inner.close()

        def __getattr__(self, name):
            inner = self.__dict__.get("_inner")
            if inner is None:
                raise AttributeError(name)
            return getattr(inner, name)

    return BenchSTT


class NullVoice:
    """--captions-only: no voice at all (no socket, no characters)."""
    voice_id = None
    warm_text = ""

    def open(self, warm=True):
        pass

    def reopen(self):
        pass

    def set_voices(self, ids):
        pass

    def close(self):
        pass

    def speak(self, text, on_audio, should_stop=lambda: False, voice_id=None):
        return 0.0


class VoiceProxy:
    """Wraps one direction's voice: caps the characters (Spend), speaks only a sampled share of the
    phrases (Sampler), and marks each phrase (sim_audio.speaking) so the sink files its audio under
    it. A phrase that is not spoken returns at once with no audio, as if nothing were said."""

    def __init__(self, inner, direction, run, *, spend, sampler, captions):
        self._inner = inner
        self.direction = direction
        self.run = run
        self._spend = spend
        self._sampler = sampler
        self._captions = captions
        self.opens = self.reopens = 0

    def open(self, warm=True):
        self.opens += 1
        if self._captions:
            return None
        warm_text = getattr(self._inner, "warm_text", "") or ""
        if warm and warm_text:
            if self._spend.take(len(warm_text)):
                self.run.add_warmup(self.direction, len(warm_text))
                self.run.add_charge(len(warm_text))
            else:
                warm = False
        if _accepts(self._inner.open, "warm"):
            return self._inner.open(warm=warm)
        return self._inner.open()

    def reopen(self):
        self.reopens += 1
        return self._inner.reopen()

    def set_voices(self, ids):
        return self._inner.set_voices(ids)

    def close(self):
        return self._inner.close()

    def speak(self, text, on_audio, should_stop=lambda: False, voice_id=None, **kw):
        utt = Utterance(self.direction, (text or "").strip(), voice_id or getattr(self._inner, "voice_id", None))
        self.run.add_utt(utt)
        if not utt.text:
            return 0.0
        if self._captions:
            utt.skipped = "captions"
            return 0.0
        if not self._sampler.take():
            utt.skipped = "sample"
            return 0.0
        if not self._spend.take(len(utt.text)):
            utt.skipped = "budget"
            return 0.0
        self.run.add_charge(len(utt.text))

        def audio(pcm):
            if utt.t_first is None:
                utt.t_first = self.run.clock()
            on_audio(pcm)
        extra = dict(kw)
        if voice_id is not None:
            extra["voice_id"] = voice_id
        utt.t_call = self.run.clock()
        try:
            with sim_audio.speaking(utt):
                secs = self._inner.speak(utt.text, audio, should_stop, **extra)
        except Exception as e:  # noqa: BLE001
            utt.error, utt.t_done = f"{type(e).__name__}: {e}"[:300], self.run.clock()
            raise
        utt.t_done, utt.seconds = self.run.clock(), secs
        return secs

    def __getattr__(self, name):
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)


def make_voice_cls(inner_factory, run, *, spend, tts_sample, captions):
    """A voice_cls(token_provider, voice_id, language) for the controller: the real (or dry-run)
    voice inside a VoiceProxy. Each direction gets its own Sampler."""
    samplers = {}

    def voice_cls(token_provider, voice_id, language, **kw):
        direction = run.direction_for_dst(language)
        inner = NullVoice() if captions else inner_factory(token_provider, voice_id, language, **kw)
        sampler = samplers.setdefault(direction, Sampler(tts_sample))
        proxy = VoiceProxy(inner, direction, run, spend=spend, sampler=sampler, captions=captions)
        run.voices[direction] = proxy
        return proxy
    return voice_cls


class SocketTracker:
    """Wraps a websocket connect() so the run can tell which voice / STT sockets were still open
    after the interpreter stopped (and close them)."""

    def __init__(self, connect):
        self._connect = connect
        self.sockets = []
        self._lock = threading.Lock()

    def __call__(self, url):
        ws = self._connect(url)
        kind = "voice" if "text-to-dialogue" in url else "stt" if "speech-to-text" in url else "other"
        with self._lock:
            self.sockets.append((kind, ws))
        return ws

    @staticmethod
    def is_open(ws):
        if hasattr(ws, "_closed"):                       # the dry-run fakes
            return not ws._closed
        state = getattr(getattr(ws, "protocol", None), "state", None)
        if state is not None:
            return getattr(state, "name", str(state)).upper() in ("OPEN", "CONNECTING")
        return getattr(ws, "close_code", None) is None

    def open_ones(self):
        with self._lock:
            return [(k, ws) for k, ws in self.sockets if self.is_open(ws)]

    def close_all(self):
        for _k, ws in self.open_ones():
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass


# ── one conversation's record ────────────────────────────────────────────────────────────────
class Run:
    """Everything one conversation's run records (thread-safe)."""

    def __init__(self, timeline, sim, cut_s):
        self.timeline = timeline
        self.sim = sim
        self.clock = sim.clock
        self.cut_s = cut_s
        self.my, self.their = timeline.get("my_lang"), timeline.get("their_lang")
        self.events = []
        self.calls = []
        self.utts = []
        self.voices = {}
        self.session = None
        self.warm = collections.Counter()
        self.charged = 0                     # ElevenLabs characters this conversation was charged (exact)
        self.stt = {d: {"sessions": 0, "connect_s": [], "events": [], "partials": 0, "commits": 0, "errors": 0,
                        "dropped": 0, "closed": 0, "sent_samples": 0} for d in DIRECTIONS}
        self.stt_in = {d: sim_audio.Timeline() for d in DIRECTIONS}
        self.last_activity = None
        self.stopped_at = None
        self._lock = threading.Lock()

    def direction_for_src(self, lang):
        return "hear" if lang == self.their and lang != self.my else "speak" if lang == self.my else "hear"

    def direction_for_dst(self, lang):
        return "hear" if lang == self.my and lang != self.their else "speak" if lang == self.their else "hear"

    def direction_of(self, a, b):
        return "hear" if (a, b) == (self.their, self.my) else "speak" if (a, b) == (self.my, self.their) else "?"

    def on_event(self, e):
        t = self.clock()
        with self._lock:
            self.events.append((t, dict(e)))
            if e.get("type") in ("phrase", "translation", "spoken", "live", "merged", "skipped"):
                self.last_activity = t

    def note_stt(self, d, kind, text):
        t = self.clock()
        rec = self.stt[d]
        with self._lock:
            rec["events"].append((t, kind, text))
            # "dropped": the connection broke and a reconnect follows; "closed": gone for good
            rec[{"partial": "partials", "committed": "commits", "error": "errors",
                 "dropped": "dropped"}.get(kind, "closed")] += 1
            self.last_activity = t

    def note_stt_audio(self, d, pcm, chunk):
        self.stt[d]["sent_samples"] += len(pcm) // 2         # one capture thread per direction
        if chunk is not None:
            self.stt_in[d].write(chunk.pos, np.frombuffer(bytes(pcm), dtype=np.int16))

    def add_charge(self, n):
        with self._lock:
            self.charged += int(n)

    def add_call(self, call):
        with self._lock:
            self.calls.append(call)

    def add_utt(self, utt):
        with self._lock:
            self.utts.append(utt)
            self.last_activity = self.clock()

    def add_warmup(self, d, n):
        with self._lock:
            self.warm[d] += n

    def busy(self):
        """Work still under way: a translation or a voice call that has not returned."""
        with self._lock:
            if any(c["t_done"] is None for c in self.calls):
                return True
            return any(u.t_call is not None and u.t_done is None for u in self.utts)


# ── running one conversation ─────────────────────────────────────────────────────────────────
def variant_files(tl, folder, variant):
    """{"mic", "system"} paths of a variant (and the clean ones), or raise ValueError."""
    vs = tl.get("variants") or {}
    want = vs.get(variant)
    if not want:
        raise ValueError(f"no '{variant}' audio (generate.py --variants ...{variant})")
    clean = vs.get("clean") or {"mic": "mic.wav", "system": "system.wav"}
    out = {}
    for key, files in (("", want), ("clean_", clean)):
        for inp in ("mic", "system"):
            p = Path(folder) / (files.get(inp) or f"{inp}.wav")
            if not p.is_file():
                raise ValueError(f"missing {p.name}")
            out[key + inp] = p
    return out


def ground_truth(tl, cut_s):
    """The script's turns that end before ``cut_s`` (all when None), with clock times added later."""
    speakers = tl.get("speakers") or {}
    out = []
    for k, t in enumerate(tl.get("turns") or []):
        if cut_s is not None and float(t["end"]) > cut_s + 1e-6:
            continue
        spk = t.get("speaker")
        out.append({"i": t.get("i", k), "speaker": spk, "track": "mic" if t.get("track") == "mic" else "system",
                    "lang": t.get("lang"), "langs": t.get("langs") or [t.get("lang")], "text": t.get("text") or "",
                    "ref": t.get("ref") or {}, "gender": t.get("gender") or (speakers.get(spk) or {}).get("gender"),
                    "start": float(t["start"]), "end": float(t["end"])})
    return out


def _cut(tl, limit_s):
    """Where to stop the audio for --limit-seconds: just after the last turn that ends before it."""
    if not limit_s:
        return None
    ends = [float(t["end"]) for t in tl.get("turns") or [] if float(t["end"]) <= limit_s]
    return (max(ends) if ends else limit_s) + 0.3


def parse_faults(spec):
    """--fault "stt_drop@12,voice_drop@20" -> {"stt_drop": [12.0], "voice_drop": [20.0]} (seconds
    into the conversation)."""
    out = {}
    for item in [x.strip() for x in (spec or "").split(",") if x.strip()]:
        kind, _, at = item.partition("@")
        if kind not in sim_audio.FAULT_KINDS:
            raise ValueError(f"--fault {item!r}: the kinds are {', '.join(sim_audio.FAULT_KINDS)}")
        try:
            out.setdefault(kind, []).append(float(at))
        except ValueError:
            raise ValueError(f"--fault {item!r}: write kind@seconds, e.g. stt_drop@12")
    return out


def providers(mode, eleven, world, tts_model, tracker, faults=None):
    """(STT factory, voice factory, notes) for a real run (mode "real") or a dry run ("oracle",
    "sockets"). faults (sockets dry runs): sockets that drop at given times."""
    notes = {}
    if mode == "oracle":
        stt = functools.partial(sim_audio.OracleScribe, world=world)

        def voice(tp, vid, lang, **kw):
            return sim_audio.OracleVoice(tp, vid, lang, model=tts_model, world=world)
        return stt, voice, notes
    base = sim_audio.fake_connect(world, faults=faults) if mode == "sockets" else eleven.default_connect
    tracker._connect = base
    stt = functools.partial(eleven.ScribeRealtime, connect=tracker) if _accepts(eleven.ScribeRealtime, "connect") \
        else eleven.ScribeRealtime
    if stt is eleven.ScribeRealtime and mode == "sockets":
        raise RuntimeError("eleven.ScribeRealtime no longer takes connect=: use --dry-run oracle")
    model_ok = _accepts(eleven.DialogueVoice, "model")
    if tts_model and not model_ok:
        notes["tts_model"] = "eleven.DialogueVoice takes no model=: the engine's default model was used"

    def voice(tp, vid, lang, **kw):
        extra = _filtered(eleven.DialogueVoice, kw)[0]      # whatever the engine passes along
        if tts_model and model_ok:
            extra["model"] = tts_model
        if _accepts(eleven.DialogueVoice, "connect"):
            extra["connect"] = tracker
        elif mode == "sockets":
            raise RuntimeError("eleven.DialogueVoice no longer takes connect=: use --dry-run oracle")
        return eleven.DialogueVoice(tp, vid, lang, **extra)
    return stt, voice, notes


def _echo_ir(tl, folder, how):
    """The room path for Gusan's voice into the mic (speakers variant): "simple" = 40 ms at -10 dB,
    "room" = the corpus's own leak (gain x leak_ir.npy, as the call itself leaks in)."""
    if how != "room":
        return None, "simple: 40 ms, -10 dB"
    lk = tl.get("leak") or {}
    p = Path(folder) / (lk.get("ir_file") or "leak_ir.npy")
    if p.is_file():
        ir = np.load(p).astype(np.float32)
        src = "leak_ir.npy"
    else:
        ir = np.asarray(sim_audio.benchaudio.room_ir(RATE, lk.get("delay_ms", 35.0), lk.get("rt60_s", 0.3),
                                                     lk.get("length_s", 0.3), lk.get("seed", 0),
                                                     lk.get("reverb_db", -6.0)), np.float32)
        src = "benchaudio.room_ir"
    gain = float(lk.get("gain", 0.3))
    return ir * gain, f"room: gain {gain} x {src}"


def run_conversation(folder, tl, args, spend, bench_shared, mode, results_audio=None):
    """Run one conversation through the real engine and evaluate it. -> its result dict."""
    controller, eleven, voices = engine()
    cid = tl.get("id") or Path(folder).name
    res = {"id": cid, "title": tl.get("title") or "", "variant": args.variant, "my_lang": tl.get("my_lang"),
           "their_lang": tl.get("their_lang"), "pair": f"{tl.get('their_lang')}<->{tl.get('my_lang')}",
           "tags": tl.get("tags") or [], "status": "error", "error": None}
    files = variant_files(tl, folder, args.variant)
    cut = _cut(tl, args.limit_seconds)
    mic, system = sim_audio.load_pcm(files["mic"]), sim_audio.load_pcm(files["system"])
    mic_c, sys_c = sim_audio.load_pcm(files["clean_mic"]), sim_audio.load_pcm(files["clean_system"])
    if cut is not None:
        n = int(cut * RATE)
        mic, system, mic_c, sys_c = mic[:n], system[:n], mic_c[:n], sys_c[:n]
    speakers_variant = args.variant == "speakers"
    output = args.output or ("speakers" if speakers_variant else "headphones")
    echo_ir, echo_desc = _echo_ir(tl, folder, args.echo_path) if speakers_variant else (None, "none")
    sim = sim_audio.SimAudio(
        mic, system, mic_clean=mic_c, system_clean=sys_c, speed=args.speed, speakers_echo=speakers_variant,
        echo_ir=echo_ir, output_name="Speakers (Gusan bench)" if speakers_variant else "Headphones (Gusan bench)",
        virtual_mic=None if args.no_speak else 1, leak_is_speech=speakers_variant)
    turns = ground_truth(tl, cut)
    run = Run(tl, sim, cut)
    res.update(duration_s=round(sim.duration, 2), cut_s=_r(cut, 2), output=output, echo_path=echo_desc)

    choice = voices.VoiceChoice(mode=args.voice, **{k: v for k, v in (("female", args.female), ("male", args.male),
                                                                        ("fixed", args.fixed_voice)) if v})
    settings_kw = dict(my_lang=tl.get("my_lang"), their_lang=tl.get("their_lang"), hear=not args.no_hear,
                       speak=not args.no_speak, output=output, duck=not args.no_duck, duck_level=args.duck_level,
                       vad_silence=args.vad, glossary=list(tl.get("glossary") or []),
                       captions_only=args.captions_only, voice_choice=choice, own_voice=bool(args.clone_voice_id))
    ok_kw, dropped_kw = _filtered(controller.Settings, settings_kw)
    settings = controller.Settings(**ok_kw)
    world = sim_audio.OracleWorld(sim, tl.get("turns") or [], error_rate=args.stt_error_rate, seed=args.seed,
                                  leak_delay_s=float((tl.get("leak") or {}).get("delay_ms", 35.0)) / 1000.0) \
        if mode != "real" else None
    if mode == "real" or bench_shared is not None:     # a dry run with --url: that function's tokens / translation
        bench = bench_shared
    else:
        bench = bench_client.FakeBench(seed=args.seed, simulate_latency=True, latency_scale=1.0 / sim.speed,
                                       translations=sim_audio.OracleTranslator(tl.get("turns") or []))
    session = BenchSession(bench, voice_choice=choice, clone_voice_id=args.clone_voice_id,
                           need_tts=not args.captions_only,
                           tts_model=args.tts_model or getattr(eleven, "TTS_MODEL", None),
                           stt_model=getattr(eleven, "STT_MODEL", None))
    run.session = session
    tracker = SocketTracker(None)
    stt_inner, voice_inner, notes = providers(mode, eleven, world, args.tts_model, tracker,
                                              faults=parse_faults(getattr(args, "fault", "")))
    translate = make_translate(bench, args.route, run)
    it_kw = dict(audio=sim, session=session, translate=translate,
                 stt_cls=make_stt_cls(stt_inner, run),
                 voice_cls=make_voice_cls(voice_inner, run, spend=spend, tts_sample=args.tts_sample,
                                          captions=args.captions_only),
                 clock=sim.clock)
    ok_it, dropped_it = _filtered(controller.Interpreter, it_kw)
    if "clock" in dropped_it and sim.speed != 1.0:
        raise RuntimeError("controller.Interpreter takes no clock=: run with --speed 1")
    res["adapter"] = {"settings_ignored": dropped_kw, "interpreter_ignored": dropped_it,
                      "translate_client": translate.client, **notes}
    it = controller.Interpreter(lambda: "bench", run.on_event, settings, **ok_it)
    t_wall = time.perf_counter()
    started = False
    try:
        it.start()
        started = True
        res["timing"] = {"engine_start_s": round(time.perf_counter() - t_wall, 3), "tokens_s": session.start_s}
        res["speakers_mode"] = bool(getattr(it, "speakers_mode", False))
        res["directions_run"] = sorted(getattr(it, "_dirs", {}) or [])
        sim.begin(args.lead_in)
        audio_end = sim.origin + sim.duration
        while sim.clock() < audio_end:
            time.sleep(0.02)
        # a machine that fell behind may still be handing out audio: wait for all of it, so delays
        # can be late but no speech is ever cut off (flagged in "sim" below)
        give_up = time.perf_counter() + 120.0
        while not all(c.done_through(sim.length) for c in sim.captures) and time.perf_counter() < give_up:
            time.sleep(0.02)
        audio_done = max(audio_end, sim.clock())
        quiet = max(2.0, 3.0 * args.vad)
        while True:
            now = sim.clock()
            busy = run.busy() or any(s.is_playing(0.0) for s in sim.sinks)
            idle = now - max(audio_done, run.last_activity or audio_done)
            if (not busy and idle >= quiet) or now - audio_done >= args.tail:
                break
            time.sleep(0.02)
        res["timing"]["caught_up_s"] = round(audio_done - audio_end, 2)
        res["timing"]["drain_s"] = round(sim.clock() - audio_done, 2)
        res["status"] = "ok"
    except KeyboardInterrupt:
        res["error"] = "interrupted"
        raise
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"[:500]
        res["traceback"] = traceback.format_exc()[-3000:]
        if isinstance(e, bench_client.BenchError):
            res["bench_status"] = e.status
            res["bench_fatal"] = _fatal(e)
        log.error("%s: %s", cid, res["error"])
    finally:
        run.stopped_at = sim.clock()
        try:
            it.stop("bench")
        except Exception as e:  # noqa: BLE001
            res.setdefault("stop_error", f"{type(e).__name__}: {e}")
        time.sleep(0.3 / max(1.0, sim.speed) + 0.2)
        res["leaks"] = {"sockets_open_after_stop": [k for k, _ws in tracker.open_ones()],
                        "utterances_after_stop": sum(1 for u in run.utts if u.t_call is not None
                                                     and u.t_call > run.stopped_at + 0.05)}
        tracker.close_all()
        sim.close()
        res.setdefault("timing", {})["wall_s"] = round(time.perf_counter() - t_wall, 2)
        res["chars_charged"] = run.charged          # what the budget let this conversation speak (the ledger)
    if started:
        ran = res.get("directions_run") or [d for d in DIRECTIONS if
                                            (d == "hear" and not args.no_hear) or (d == "speak" and not args.no_speak)]
        t_eval = time.perf_counter()
        try:
            res.update(evaluate(run, turns, args, bench if args.judge else None, settings, ran))
            res["timing"]["eval_s"] = round(time.perf_counter() - t_eval, 3)
        except Exception as e:  # noqa: BLE001
            res["status"] = "error"
            res["error"] = f"evaluation failed: {type(e).__name__}: {e}"
            res["traceback"] = traceback.format_exc()[-3000:]
            log.error("%s: %s", cid, res["error"])
        if results_audio is not None:
            try:
                save_audio(run, Path(results_audio) / cid)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: could not save the audio: %s", cid, e)
    res["sim"] = sim_health(sim)
    res["session"] = {"tokens_after_start": {f"{k}:{d}": n for (k, d), n in session.requested.items()},
                      "heartbeats": session.beats}
    if world is not None and getattr(world, "faults", None) is not None:
        o = sim.origin or 0.0
        res["faults"] = [{"kind": k, "at": at, "language": lang, "t": _r(t - o, 3) if t is not None else None}
                         for k, at, lang, t in world.faults.fired]
    if args.events:
        o = sim.origin or 0.0
        res["events"] = [{"t": _r(t - o, 3), **e} for t, e in run.events if e.get("type") != "live"]
        res["stt_events"] = {d: [{"t": _r(t - o, 3), "kind": k, "text": x} for t, k, x in run.stt[d]["events"]]
                             for d in DIRECTIONS}
        res["voice_calls"] = [{"id": u.id, "direction": u.direction, "text": u.text, "voice_id": u.voice_id,
                               "t_call": _r(u.t_call - o, 3) if u.t_call is not None else None,
                               "t_first": _r(u.t_first - o, 3) if u.t_first is not None else None,
                               "t_done": _r(u.t_done - o, 3) if u.t_done is not None else None,
                               "play": [_r(u.first_pos / RATE, 3), _r(u.end_pos / RATE, 3)] if u.first_pos is not None
                               else None, "skipped": u.skipped, "error": u.error} for u in run.utts]
    return res


def sim_health(sim):
    caps, sinks = list(sim.captures), list(sim.sinks)
    ducked, last_t, last_g = 0.0, None, 1.0
    for t, g in sim.duck_log:
        if last_t is not None and last_g < 1.0:
            ducked += t - last_t
        last_t, last_g = t, g
    lag = max([c.max_lag for c in caps] or [0.0])
    chunks = sum(c.chunks for c in caps)
    late = sum(c.late for c in caps)
    return {"speed": sim.speed, "chunks": chunks, "capture_max_lag_s": _r(lag, 3), "late_chunks": late,
            "realtime_ok": lag <= 0.25 and late <= 0.01 * max(1, chunks),
            "capture_errors": sum(c.errors for c in caps),
            "first_capture_error": next((c.first_error for c in caps if c.first_error), None),
            "ducks": sum(d.ducks for d in sim.duckers), "ducked_s": _r(ducked, 2),
            "sink_clears": sum(len(s.clears) for s in sinks)}


def save_audio(run, folder):
    """What each STT was sent, what Gusan said into each sink, and a stereo "what I heard"
    (left: the call, right: Gusan's Hear voice) - for listening to a run."""
    sim = run.sim
    folder.mkdir(parents=True, exist_ok=True)
    gi = folder.parent / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    end = sim.length + int(4 * RATE)
    for d in DIRECTIONS:
        sim_audio.save_pcm(folder / f"{d}_stt_in.wav", run.stt_in[d].export(0, end))
    for s in sim.sinks:
        sim_audio.save_pcm(folder / f"{s.name}_out.wav", s.out.export(0, end))
    hear = sim.sinks_named("hear")
    right = hear[0].out.export(0, end) if hear else np.zeros(end, np.int16)
    left = np.zeros(end, np.int16)
    left[:len(sim.system)] = sim.system
    st = np.stack([left, right[:end]], axis=1).astype(np.float32) / 32768.0
    with open(folder / "what_i_heard.wav", "wb") as f:
        import wave
        w = wave.open(f, "wb")
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.clip(np.round(st * 32767), -32768, 32767).astype("<i2").tobytes())
        w.close()


# ── evaluation ───────────────────────────────────────────────────────────────────────────────
class _Grams:
    """Character-trigram bags of normalised text (cached), for "how much of A is in B"."""

    def __init__(self):
        self._c = {}
        self._n = {}

    def norm(self, text):
        t = self._n.get(text)
        if t is None:
            t = self._n[text] = metrics.normalize(metrics.spoken_text(text))
        return t

    def __call__(self, text):
        g = self._c.get(text)
        if g is None:
            t = self.norm(text)
            g = collections.Counter([t] if 0 < len(t) < 3 else (t[i:i + 3] for i in range(len(t) - 2)))
            self._c[text] = g
        return g

    def contained(self, a, b):
        ga = self(a)
        n = sum(ga.values())
        if not n:
            return 0.0
        if n == 1:
            t = next(iter(ga))
            if len(t) < 3:              # "OK", "含运": too short for a trigram, so is it in B at all
                nb = self.norm(b)
                if any(sim_audio._CJK.match(c) for c in t):
                    return 1.0 if t in nb else 0.0
                return 1.0 if re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", nb) else 0.0
        return sum((ga & self(b)).values()) / n


def collect(run):
    """The engine's phrases per direction, with their translation, errors and the app's latency."""
    phrases = {d: [] for d in DIRECTIONS}
    idx = {}
    merged = {d: [] for d in DIRECTIONS}
    skipped = collections.Counter()
    errors = collections.Counter()
    messages = []
    for t, e in run.events:
        typ, d = e.get("type"), e.get("direction")
        if typ == "phrase" and d in phrases:
            p = {"id": e.get("id"), "dir": d, "text": e.get("text") or "", "t": t, "gender": e.get("gender"),
                 "t_delta": None, "t_final": None, "translation": None, "tr_error": None, "app_latency": None,
                 "utt": None, "call": None, "cls": None, "flags": [], "links": {}, "hits": []}
            phrases[d].append(p)
            idx[(d, p["id"])] = p
        elif typ == "translation":
            p = idx.get((d, e.get("id")))
            if p is None:
                continue
            if e.get("final"):
                p["t_final"], p["translation"] = t, e.get("text") or ""
                if p["t_delta"] is None:
                    p["t_delta"] = t
            elif p["t_delta"] is None and (e.get("text") or "").strip():
                p["t_delta"] = t
        elif typ == "error":
            errors[e.get("code") or "?"] += 1
            if len(messages) < 30:
                messages.append({"t": _r(t, 2), "direction": d, "code": e.get("code"),
                                 "message": str(e.get("message") or "")[:300]})
            p = idx.get((d, e.get("id")))
            if p is not None and e.get("code") == "translate":
                p["tr_error"] = str(e.get("message") or "")[:200]
        elif typ == "spoken":
            p = idx.get((d, e.get("id")))
            if p is not None:
                p["app_latency"] = e.get("latency")
        elif typ == "merged" and d in merged:
            merged[d].append(list(e.get("ids") or []))
        elif typ == "skipped":
            skipped[(d, e.get("reason") or "?")] += 1
    return phrases, idx, merged, skipped, errors, messages


def map_utterances(d, phrases, idx, merged, utts):
    """Which phrase(s) each voice call said: in order, by text (a merged turn says its phrases'
    translations joined by spaces)."""
    spoken = [p for p in phrases if p["translation"]]
    joined = []
    for ids in merged:
        ps = [idx.get((d, i)) for i in ids]
        if all(ps):
            joined.append((" ".join(p["translation"].strip() for p in ps).strip(), ps))
    pos = 0
    for u in utts:
        if u.error and not u.samples:
            continue                                    # a failed try: the retry carries the phrase
        target = None
        for k in range(pos, len(spoken)):
            if spoken[k]["utt"] is None and spoken[k]["translation"].strip() == u.text:
                target, pos = [spoken[k]], k + 1
                break
        if target is None:
            for text, ps in joined:
                if text == u.text and all(p["utt"] is None for p in ps):
                    target = ps
                    pos = max(pos, spoken.index(ps[-1]) + 1) if ps[-1] in spoken else pos
                    break
        for p in target or ():
            p["utt"] = u
        if target and len(target) > 1:
            for p in target:
                p["flags"].append("spoken_merged")


def map_calls(phrases, calls):
    pos = 0
    for p in phrases:
        for k in range(pos, len(calls)):
            if calls[k]["text"] == p["text"]:
                p["call"], pos = calls[k], k + 1
                break


def _overlap(a0, a1, spans):
    return sum(max(0.0, min(a1, e) - max(a0, s)) for s, e in spans)


MATCH_CELLS = 30_000_000


def match_align(ref, hyp, ref_win=None, hyp_t=None):
    """Which script units the engine's phrases hold: an alignment of ``ref`` (the direction's
    script, unit by unit) and ``hyp`` (the units of its phrases, in order) that keeps the MOST exact
    matches - an LCS: a substitution costs a deletion plus an insertion, so "... 2000 units" is never
    traded for "price for" to save a step, as a plain edit-distance backtrace does when a stray
    phrase follows. ref_win[i] = (lo, hi): the clock times between which the phrase holding a match
    for ref unit i must have been committed (its turn's time); hyp_t[j]: when hyp unit j's phrase
    was committed (non-decreasing). -> [(op, ref index, hyp index)], op "=", "D" or "I"."""
    n, m = len(ref), len(hyp)
    if not n or not m:
        return [("D", i, None) for i in range(n)] + [("I", None, j) for j in range(m)]
    if (n + 1) * (m + 1) > MATCH_CELLS:          # hours of text: the plain alignment's matches
        out = []
        for op, r, h in metrics.align(ref, hyp)[1] or ():
            out += [("D", r, None), ("I", None, h)] if op == "S" else [(op, r, h)]
        return out
    vocab = {}
    ai = np.fromiter((vocab.setdefault(x, len(vocab)) for x in ref), dtype=np.int64, count=n)
    bi = np.fromiter((vocab.setdefault(x, len(vocab)) for x in hyp), dtype=np.int64, count=m)
    if ref_win is not None and hyp_t is not None:
        ht = np.asarray(hyp_t, dtype=np.float64)
        lo = np.searchsorted(ht, [w[0] for w in ref_win], side="left")
        hi = np.searchsorted(ht, [w[1] for w in ref_win], side="right")
    else:
        lo, hi = np.zeros(n, np.int64), np.full(n, m, np.int64)
    idx = np.arange(m + 1, dtype=np.int64)
    D = np.empty((n + 1, m + 1), dtype=np.int32)
    D[0] = idx
    for i in range(1, n + 1):
        eq = bi == ai[i - 1]
        eq[:lo[i - 1]] = False
        eq[hi[i - 1]:] = False
        prev = D[i - 1].astype(np.int64)
        cand = np.empty(m + 1, dtype=np.int64)
        cand[0] = i
        cand[1:] = np.minimum(prev[1:] + 1, prev[:-1] + np.where(eq, 0, 2))
        D[i] = np.minimum.accumulate(cand - idx) + idx       # insertions: a running minimum
    i, j, ops = n, m, []
    while i > 0 or j > 0:
        if (i > 0 and j > 0 and ai[i - 1] == bi[j - 1] and lo[i - 1] <= j - 1 < hi[i - 1]
                and D[i, j] == D[i - 1, j - 1]):
            ops.append(("=", i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i > 0 and D[i, j] == D[i - 1, j] + 1:
            ops.append(("D", i - 1, None))
            i -= 1
        else:
            ops.append(("I", None, j - 1))
            j -= 1
    ops.reverse()
    return ops


def turn_pauses(track, start, end, min_pause_s=PAUSE_S, frame=160):
    """[(start, end)] in timeline seconds of the pauses inside a turn: stretches of at least
    ``min_pause_s`` where its clean track is 30 dB under the turn's loud frames (10 ms frames).
    A phrase the STT closed at a pause ends where that pause starts."""
    a, b = int(start * RATE), int(end * RATE)
    x = np.asarray(track[a:b], dtype=np.float32)
    n = len(x) // frame
    if n < 10:
        return []
    e = 10.0 * np.log10((x[:n * frame].reshape(n, frame) ** 2).mean(axis=1) + 1e-3)
    quiet = e < np.percentile(e, 95) - 30.0
    out, run = [], 0
    need = int(round(min_pause_s * RATE / frame))
    for k, q in enumerate(np.append(quiet, False)):
        if q:
            run += 1
            continue
        if run >= need and k - run > 0 and k < n:              # inside the turn, not at its edges
            out.append((start + (k - run) * frame / RATE, start + k * frame / RATE))
        run = 0
    return out


def muted_onsets(timeline, n_samples, min_s=PAUSE_S, frame=160):
    """Timeline seconds where what the STT was sent turned to digital silence (the gate muting it)
    for at least ``min_s`` after sound: a phrase the STT closed there ends where the muting began."""
    x = timeline.read(0, int(n_samples))
    n = len(x) // frame
    if n < 2:
        return []
    e = (x[:n * frame].reshape(n, frame) ** 2).mean(axis=1)
    quiet = e < 1.0                                          # under 1 LSB: zeros, not a quiet room
    loud = e > 100.0 ** 2
    out, run = [], 0
    need = int(round(min_s * RATE / frame))
    for k, q in enumerate(np.append(quiet, False)):
        if q:
            run += 1
            continue
        start = k - run
        if run >= need and start > 0 and loud[start - 1]:
            out.append(start * frame / RATE)
        run = 0
    return out


def _pair_cover(units, free, sources, gap=PAIR_GAP):
    """Indices k of ``units`` (only where free[k]) that belong to a pair of neighbouring free units
    found in the same order in one of ``sources`` (unit lists), at most ``gap`` units apart there
    (what was heard of a source can have holes: the gate, the STT): words heard from somewhere else,
    two at a time, so a lone common word ("the", "是") is never enough."""
    pairs = set()
    for s in sources:
        for i, a in enumerate(s):
            for b in s[i + 1:i + 1 + gap]:
                pairs.add((a, b))
    out = set()
    for k in range(len(units) - 1):
        if free[k] and free[k + 1] and (units[k], units[k + 1]) in pairs:
            out.update((k, k + 1))
    return out


def evaluate(run, turns, args, judge_bench, settings, ran=DIRECTIONS):
    sim = run.sim
    ran = [d for d in DIRECTIONS if d in ran]
    o = sim.origin
    for T in turns:
        T["t_start"], T["t_end"] = o + T["start"], o + T["end"]
    phrases, idx, merged, skipped, errors, messages = collect(run)
    calls = {d: [c for c in run.calls if c["direction"] == d] for d in DIRECTIONS}
    utts = {d: [u for u in run.utts if u.direction == d] for d in DIRECTIONS}
    for d in DIRECTIONS:
        map_utterances(d, phrases[d], idx, merged[d], utts[d])
        map_calls(phrases[d], calls[d])
    grams = _Grams()

    # Gusan's Hear voice: the only one that can get back into our own inputs (the loopback always,
    # the mic through the room in the speakers variant)
    def play_span(u, kind):
        sp = u.span()
        if sp is None:
            return None
        shift = sim.gusan_shift(kind) / RATE
        return sim.time_of(sp[0]) + shift, sim.time_of(sp[1]) + shift
    reach = {"hear": [(u, play_span(u, "loopback")) for u in utts["hear"]],
             "speak": [(u, play_span(u, "mic")) for u in utts["hear"]] if sim.speakers_echo else []}
    reach = {d: [(u, sp) for u, sp in v if sp] for d, v in reach.items()}
    by_track = {tr: [T for T in turns if T["track"] == tr] for tr in ("system", "mic")}

    out = {"directions": {}}
    all_rows = []
    for d in ran:
        own = by_track[TRACK[d]]
        other = by_track["system"] if (d == "speak" and sim.leak_is_speech) else []
        r, rows = evaluate_direction(run, d, phrases[d], own, other, reach[d], grams, args, judge_bench, settings)
        r["pipeline_skipped"] = {reason: n for (dd, reason), n in skipped.items() if dd == d}
        out["directions"][d] = r
        all_rows += rows

    # echo chains: an echo of a phrase that was itself an echo
    by_utt = {}
    for d in DIRECTIONS:
        for p in phrases[d]:
            if p["utt"] is not None:
                by_utt.setdefault(p["utt"].id, []).append(p)
    depth_memo = {}

    def is_echo(p):
        return p["cls"] == "echo" or "echo_words" in p["flags"]

    def depth(p, seen=()):
        key = (p["dir"], p["id"])
        if key in depth_memo:
            return depth_memo[key]
        if not is_echo(p) or key in seen:
            return 0
        srcs = by_utt.get(p.get("echo_of"), [])
        dd = 1 + max([depth(q, seen + (key,)) for q in srcs] or [0])
        depth_memo[key] = dd
        return dd
    chains = [depth(p) for d in DIRECTIONS for p in phrases[d] if is_echo(p)]

    # A feedback loop: a phrase that is Gusan's own voice (echo), that carries some of it along with
    # mine (echo_mixed), or that came while only Gusan's voice was playing (echo_suspect).
    kinds = {k: sum(out["directions"][d]["counts"][k] for d in ran) for k in ("echo", "echo_mixed", "echo_suspect")}
    out["echo_loops"] = sum(kinds.values())
    out["echo_loops_by_kind"] = kinds
    out["echo_chain_max"] = max(chains or [0])
    out["crosstalk"] = sum(out["directions"][d]["counts"]["crosstalk"] + out["directions"][d]["counts"]["crosstalk_mixed"]
                           for d in ran)
    out["reconnects"] = {k: sum(out["directions"][d]["reconnects"][k] for d in ran) for k in ("stt", "voice")}
    out["chars_spoken"] = sum(out["directions"][d]["tts"]["chars"] for d in ran)
    out["chars_would_speak"] = sum(out["directions"][d]["tts"]["would_chars"] for d in ran)
    out["errors"] = dict(errors)
    out["error_messages"] = messages
    out["turns"] = all_rows
    out["phrases"] = [phrase_row(p, sim) for d in ran for p in phrases[d]]
    return out


def evaluate_direction(run, d, phrases, own, other, reach, grams, args, judge_bench, settings):
    sim = run.sim
    src, dst = (run.their, run.my) if d == "hear" else (run.my, run.their)
    vad = float(getattr(settings, "vad_silence", args.vad) or args.vad)

    # 1. what each phrase is: the script's words, Gusan's own voice, the call through the room...
    gus_spans = [sp for _u, sp in reach]
    own_spans = [(T["t_start"], T["t_end"]) for T in own] + [(T["t_start"], T["t_end"]) for T in other]
    prev_real = []
    for p in phrases:
        cands = [T for T in own if T["t_start"] - 1.0 <= p["t"] and T["t_end"] >= p["t"] - LATE_MATCH_S]
        turn_sim = max([grams.contained(p["text"], T["text"]) for T in cands] +
                       [grams.contained(p["text"], a["text"] + " " + b["text"]) for a, b in zip(cands, cands[1:])]
                       or [0.0])
        echo_sim, echo_u = 0.0, None
        for u, (s, e) in reach:
            if s <= p["t"] and e >= p["t"] - ECHO_WINDOW_S:
                c = grams.contained(p["text"], u.text)
                if c > echo_sim:
                    echo_sim, echo_u = c, u
        cross_srcs = [T for T in other if T["t_start"] - 1.0 <= p["t"] and T["t_end"] >= p["t"] - CROSS_WINDOW_S]
        cross_sim = max([grams.contained(p["text"], T["text"]) for T in cross_srcs] or [0.0])
        w1 = p["t"] - vad - 0.1
        w0 = w1 - min(8.0, max(0.5, len(p["text"]) / 14.0))
        own_share = _overlap(w0, w1, own_spans) / (w1 - w0)
        gus_share = _overlap(w0, w1, gus_spans) / (w1 - w0)
        p["sim"] = {"turn": _r(turn_sim, 3), "echo": _r(echo_sim, 3), "cross": _r(cross_sim, 3),
                    "own_share": _r(own_share, 2), "gusan_share": _r(gus_share, 2)}
        p["_echo_srcs"] = [u for u, (s, e) in reach if s <= p["t"] and e >= p["t"] - ECHO_WINDOW_S]
        p["_echo_u"], p["_cross_srcs"] = echo_u, cross_srcs
        if echo_sim >= 0.5 and echo_sim >= turn_sim + 0.15:
            p["cls"], p["echo_of"] = "echo", echo_u.id
        elif cross_sim >= 0.5 and cross_sim >= turn_sim + 0.15:
            p["cls"] = "crosstalk"
        elif turn_sim < 0.25 and own_share < 0.15 and gus_share > 0.3:
            p["cls"] = "echo_suspect"
        else:
            p["cls"] = "candidate"
            units = metrics.text_units(p["text"])
            recent = [q for q in prev_real[-2:] if p["t"] - q["t"] <= 15.0]
            if len(units) >= 2 and recent:
                bag = collections.Counter(u for q in recent for u in metrics.text_units(q["text"]))
                if sum((collections.Counter(units) & bag).values()) >= 0.8 * len(units):
                    p["flags"].append("duplicate")
            prev_real.append(p)

    # 2. which phrase holds which words of which turn: the most exact matches (match_align), a word
    #    only matching inside its own turn's time
    cands = [p for p in phrases if p["cls"] == "candidate"]
    ref_units, ref_owner, ref_end, ref_win = [], [], [], []
    clean = sim.system_clean if TRACK[d] == "system" else sim.mic_clean
    mutes = muted_onsets(run.stt_in[d], sim.length)
    for T in own:
        u = metrics.text_units(metrics.spoken_text(T["text"]))
        T["n_units"] = len(u)
        # where each word ends: spread over the turn by length (the voice gives no word times)...
        w = [len(x) + 1 for x in u]
        tot, acc = float(sum(w) or 1), 0.0
        for x, wx in zip(u, w):
            acc += wx
            ref_units.append(x)
            ref_owner.append(T)
            ref_end.append(T["t_start"] + acc / tot * (T["t_end"] - T["t_start"]))
            ref_win.append((T["t_start"] - 1.0, T["t_end"] + LATE_MATCH_S))
        T["_last_ri"] = len(ref_units) - 1
        # ... and the pauses the voice made inside it, or where the gate muted it: a phrase closed
        # there ends where one starts
        T["_pauses"] = [(sim.origin + s, "pause") for s, _e in turn_pauses(clean, T["start"], T["end"])] + \
            [(sim.origin + s, "gate") for s in mutes if T["start"] < s < T["end"]]
    hyp_units, hyp_owner, hyp_t = [], [], []
    for p in cands:
        u = metrics.text_units(p["text"])
        p["_units"], p["_own"] = u, [False] * len(u)
        hyp_units += u
        hyp_owner += [(p, k) for k in range(len(u))]
        hyp_t += [p["t"]] * len(u)
    for T in own:
        T["links"], T["last_hit"] = {}, None
    for op, ri, hi in match_align(ref_units, hyp_units, ref_win, hyp_t):
        if op != "=":
            continue
        (p, k), T = hyp_owner[hi], ref_owner[ri]
        p["_own"][k] = True
        p["links"].setdefault(T["i"], {"=": 0, "S": 0})["="] += 1
        T["links"].setdefault(p["id"], {"=": 0, "S": 0})["="] += 1
        p["hits"].append((ri, "="))
        if T["last_hit"] is None or ri > T["last_hit"][0]:
            T["last_hit"] = (ri, p)
    by_i = {T["i"]: T for T in own}
    for p in cands:
        # the words that are not the script's: Gusan's own voice or the call through the room?
        units, free = p["_units"], [not x for x in p["_own"]]
        e_units = [metrics.text_units(u.text) for u in p["_echo_srcs"]]
        echo_cov = _pair_cover(units, free, e_units)
        cross_cov = _pair_cover(units, free, [metrics.text_units(T["text"]) for T in p["_cross_srcs"]])
        n = max(1, len(units))
        p["sim"].update(echo_units=len(echo_cov), cross_units=len(cross_cov))
        best_e = None
        if echo_cov:
            best_e = max(range(len(e_units)), key=lambda k: len(_pair_cover(units, free, [e_units[k]])))
            best_e = p["_echo_srcs"][best_e]
        strong = {i: lk for i, lk in p["links"].items() if lk["="] >= 1}
        if not strong:
            e_sim, c_sim = p["sim"]["echo"] or 0.0, p["sim"]["cross"] or 0.0
            if len(echo_cov) >= 0.5 * n or (e_sim >= 0.6 and e_sim >= c_sim):
                p["cls"] = "echo"
                p["echo_of"] = (best_e or p["_echo_u"]).id if (best_e or p["_echo_u"]) else None
            elif len(cross_cov) >= 0.5 * n or c_sim >= 0.6:
                p["cls"] = "crosstalk"
            else:
                p["cls"] = "extra"
            continue
        p["cls"] = "real"
        eq = [ri for ri, op in p["hits"] if op == "="]
        last = max(eq, key=lambda ri: ref_end[ri])
        T_last = ref_owner[last]
        se, how = ref_end[last], "estimate"
        if last == T_last["_last_ri"]:
            se, how = T_last["t_end"], "turn_end"
        else:
            near = [(t, kind) for t, kind in T_last["_pauses"] if abs(t - se) <= SNAP_S and t < p["t"]]
            if near:
                se, how = min(near, key=lambda tk: abs(tk[0] - se))
        p["speech_end"], p["speech_end_by"] = min(se, p["t"]), how
        p["turns"] = sorted(strong, key=lambda i: by_i[i]["start"])
        p["main_turn"] = max(strong, key=lambda i: strong[i]["="])
        big = [i for i, lk in strong.items() if lk["="] >= 2 or lk["="] >= 0.5 * max(1, by_i[i]["n_units"])]
        if len(big) >= 2:
            p["flags"].append("merged_turns")
        if len(echo_cov) >= 2 and len(echo_cov) >= 0.25 * n:
            p["flags"].append("echo_words")              # my words AND Gusan's: still a loop
            p["echo_of"] = best_e.id
        if len(cross_cov) >= 2 and len(cross_cov) >= 0.25 * n:
            p["flags"].append("crosstalk_words")

    # 3. STT accuracy: the phrases against the track (as run_stt.py scores a transcript), without
    #    the ones that are Gusan's voice or the call through the room (those count in wer_all)
    mine = [p for p in phrases if p["cls"] in ("real", "extra")]
    mt_turns = [{"i": T["i"], "speaker": T["speaker"], "text": T["text"], "lang": T["lang"], "langs": T["langs"],
                 "start": T["start"], "end": T["end"]} for T in own]
    other_mt = [{"i": T["i"], "text": T["text"], "lang": T["lang"], "start": T["start"], "end": T["end"]}
                for T in other]
    sc = metrics.score_transcript(mt_turns, " ".join(p["text"] for p in mine), other_turns=other_mt)
    sc_all = metrics.score_transcript(mt_turns, " ".join(p["text"] for p in phrases))
    per_turn = {row["i"]: row for row in sc["per_turn"]}

    # 4. translation: turns and phrases joined where they share words; chrF++ per group
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for T in own:
        find(("T", T["i"]))
    for p in cands:
        find(("P", p["id"]))
        for i in p.get("turns") or ():
            parent[find(("P", p["id"]))] = find(("T", i))
    groups = collections.defaultdict(lambda: {"turns": [], "phrases": []})
    for T in own:
        groups[find(("T", T["i"]))]["turns"].append(T)
    for p in cands:
        groups[find(("P", p["id"]))]["phrases"].append(p)
    word_order = 0 if (dst or "").split("-")[0] in metrics.NO_SPACE_LANGS else 2
    tot, tot_m = None, None
    judge_items, judge_groups = [], []
    group_rows = []
    for g in groups.values():
        g["turns"].sort(key=lambda T: T["start"])
        g["phrases"].sort(key=lambda p: p["t"])
        ref = " ".join((T["ref"].get(dst) or "") for T in g["turns"]).strip()
        hyp = " ".join((p["translation"] or "") for p in g["phrases"]).strip()
        st = metrics.chrf_stats(metrics.spoken_text(ref), hyp, 6, word_order)
        tot = st if tot is None else [tuple(a + b for a, b in zip(x, y)) for x, y in zip(tot, st)]
        matched = bool(g["turns"]) and any(p["translation"] for p in g["phrases"])
        if matched:
            tot_m = st if tot_m is None else [tuple(a + b for a, b in zip(x, y)) for x, y in zip(tot_m, st)]
            if judge_bench is not None and ref:
                judge_items.append({"src": " ".join(metrics.spoken_text(T["text"]) for T in g["turns"]),
                                    "src_lang": src, "ref": ref, "hyp": hyp, "tgt_lang": dst})
                judge_groups.append(g)
        g["chrf"] = metrics.chrf_from_stats(st) if (ref or hyp) else None
        g["ref"], g["hyp"] = ref, hyp
        group_rows.append(g)
    judge = None
    if judge_items:
        try:
            scores = []
            judge_route = getattr(args, "judge_route", "") or None
            for k in range(0, len(judge_items), JUDGE_BATCH):
                scores += list(judge_bench.judge(judge_items[k:k + JUDGE_BATCH], route=judge_route))
            for g, s in zip(judge_groups, scores):
                g["judge"] = s
            # the bench function answers null scores (note "unparsed") when it could not read the
            # judge's answer: those are left out, not counted as zeros
            ad = metrics.summary([s.get("adequacy") for s in scores if isinstance(s, dict)])
            fl = metrics.summary([s.get("fluency") for s in scores if isinstance(s, dict)])
            judge = {"n": ad["n"], "n_fluency": fl["n"], "asked": len(judge_items),
                     "unparsed": len(judge_items) - ad["n"], "adequacy": ad["mean"], "fluency": fl["mean"]}
        except Exception as e:  # noqa: BLE001 - a judge failure must not lose the run
            judge = {"error": f"{type(e).__name__}: {e}"[:300]}

    # 5. delays. Those counted from the speech end use only phrases whose end is known (the turn's
    #    end, or a pause): a phrase cut mid-sentence has only the estimate, which can be off by more
    #    than the delay itself (its row still shows it). The stages need no speech end.
    lat = collections.defaultdict(list)
    stages = collections.defaultdict(list)
    for p in phrases:
        if p["cls"] != "real":
            continue
        se = p["speech_end"]
        known = p.get("speech_end_by") != "estimate"
        if known:
            lat["stt"].append(p["t"] - se)
        if p["app_latency"] is not None:
            lat["app"].append(p["app_latency"])
        c = p["call"]
        if c is not None:
            stages["translate_wait"].append(c["t_call"] - p["t"])
            if c["t_first"] is not None:
                stages["ttft"].append(c["t_first"] - c["t_call"])
        if p["t_delta"] is not None and known:
            lat["first_delta"].append(p["t_delta"] - se)
        if p["t_final"] is not None:
            if known:
                lat["final"].append(p["t_final"] - se)
            if p["t_delta"] is not None:
                stages["translate_rest"].append(p["t_final"] - p["t_delta"])
        u = p["utt"]
        if u is not None and u.first_pos is not None:
            play = sim.time_of(u.first_pos)
            p["t_play"] = play
            if known:
                lat["ear"].append(play - se)
            if p["t_final"] is not None and u.t_call is not None:
                stages["speak_wait"].append(u.t_call - p["t_final"])
            if u.t_first is not None and u.t_call is not None:
                stages["ttfb"].append(u.t_first - u.t_call)
                stages["play_wait"].append(play - u.t_first)
    rows = []
    for T in own:
        row = {"i": T["i"], "speaker": T["speaker"], "track": T["track"], "direction": d, "lang": T["lang"],
               "langs": T["langs"], "mixed": len(set(T["langs"])) > 1, "gender": T["gender"],
               "start": _r(T["start"], 3), "end": _r(T["end"], 3), "text": T["text"],
               "ref": T["ref"].get(dst), "phrases": sorted(T["links"], key=lambda pid: pid)}
        pt = per_turn.get(T["i"]) or {}
        row.update(wer=pt.get("wer"), cer=pt.get("cer"), recall=pt.get("recall"), dropped=bool(pt.get("dropped")),
                   hyp=pt.get("hyp"))
        last = T["last_hit"][1] if T["last_hit"] else None
        if last is not None:
            row["turn_commit"] = _r(last["t"] - T["t_end"], 3)
            lat["turn_commit"].append(last["t"] - T["t_end"])
            if last.get("t_play") is not None:
                row["turn_ear"] = _r(last["t_play"] - T["t_end"], 3)
                lat["turn_ear"].append(last["t_play"] - T["t_end"])
        first_cap = None
        for t, kind, text in run.stt[d]["events"]:
            if kind == "partial" and T["t_start"] < t <= T["t_end"] + 3.0 and grams.contained(text, T["text"]) >= 0.3:
                first_cap = t - T["t_start"]
                break
        if first_cap is not None:
            row["first_caption"] = _r(first_cap, 3)
            lat["first_caption"].append(first_cap)
        rows.append(row)
    for g in group_rows:
        for T in g["turns"]:
            row = next(r for r in rows if r["i"] == T["i"])
            row["translation"] = g["hyp"]
            row["chrf"] = _r(g["chrf"], 2)
            row["group_turns"] = len(g["turns"])
            if g.get("judge"):
                row["judge"] = g["judge"]

    # 6. voices: a woman's voice for a woman, a man's for a man (when the direction uses the choice)
    gender = None
    proxy = run.voices.get(d)
    choice = getattr(settings, "voice_choice", None)
    uses_choice = d == "hear" or not args.clone_voice_id
    if choice is not None and uses_choice:
        judged = right = unknown = vj = vr = 0
        seq, exp_seq = [], []
        for p in phrases:
            if p["cls"] != "real":
                continue
            truth = by_i[p["main_turn"]]["gender"]
            if truth not in ("male", "female"):
                continue
            if p["gender"] is None:
                unknown += 1
            else:
                judged += 1
                right += p["gender"] == truth
            u = p["utt"]
            if u is not None and u.voice_id and u.samples and "spoken_merged" not in p["flags"]:
                want = choice.for_gender(truth)
                vj += 1
                vr += u.voice_id == want
                seq.append(u.voice_id)
                exp_seq.append(want)
        switches = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
        exp_switches = sum(1 for a, b in zip(exp_seq, exp_seq[1:]) if a != b)
        gender = {"mode": getattr(choice, "mode", None), "judged": judged, "right": right, "unknown": unknown,
                  "accuracy": _r(right / judged) if judged else None, "voice_judged": vj, "voice_right": vr,
                  "voice_accuracy": _r(vr / vj) if vj else None, "switches": switches,
                  "expected_switches": exp_switches}

    # 7. the voice's characters
    du = [u for u in run.utts if u.direction == d]
    tts = {"calls": len(du), "spoken": sum(1 for u in du if u.samples),
           "skipped": dict(collections.Counter(u.skipped for u in du if u.skipped)),
           "failed": sum(1 for u in du if u.error),
           "chars": sum(len(u.text) for u in du if not u.skipped) + int(run.warm.get(d, 0)),
           "warmup_chars": int(run.warm.get(d, 0)),
           "would_chars": sum(len(u.text) for u in du) + int(run.warm.get(d, 0)),
           "audio_s": _r(sum(u.samples for u in du) / RATE, 2),
           "opens": getattr(proxy, "opens", 0), "reopens": getattr(proxy, "reopens", 0)}

    # 8. what reached the STT: own speech the gate muted, Gusan's voice that got through
    stt_in = stt_input(run, d)
    rec = run.stt[d]
    stt_in.update(sessions=rec["sessions"], partials=rec["partials"], commits=rec["commits"],
                  stt_errors=rec["errors"], dropped=rec["dropped"], closed=rec["closed"],
                  connect=metrics.summary(rec["connect_s"]),
                  sent_s=_r(rec["sent_samples"] / RATE, 1))
    # a new single-use token after the start = the STT reconnecting / the voice socket reopening
    asked = getattr(getattr(run, "session", None), "requested", None) or {}
    reconnects = {"stt": int(asked.get(("scribe", d), 0)), "voice": int(asked.get(("tts", d), 0))}

    counts = collections.Counter(p["cls"] for p in phrases)
    counts_out = {
        "phrases": len(phrases), "real": counts["real"], "extra": counts["extra"], "echo": counts["echo"],
        "echo_mixed": sum(1 for p in phrases if "echo_words" in p["flags"]),
        "echo_suspect": counts["echo_suspect"], "crosstalk": counts["crosstalk"],
        "crosstalk_mixed": sum(1 for p in phrases if "crosstalk_words" in p["flags"]),
        "duplicates": sum(1 for p in phrases if "duplicate" in p["flags"]),
        "merged_turns": sum(1 for p in phrases if "merged_turns" in p["flags"]),
        "split_turns": sum(1 for T in own if len([1 for lk in T["links"].values() if lk["="]]) >= 2),
        "turns": len(own), "dropped_turns": sum(1 for r in rows if r["dropped"]),
        "partial_turns": sum(1 for r in rows if not r["dropped"] and (r["recall"] or 0) < 0.6),
        "untranslated": sum(1 for p in phrases if p["cls"] == "real" and not p["translation"]),
        "translate_errors": sum(1 for p in phrases if p["tr_error"]),
    }
    ov = sc["overall"]
    stt = {k: ov.get(k) for k in ("wer", "cer", "errors", "sub", "del", "ins", "hits", "ref_units", "hyp_units",
                                  "char_errors", "ref_chars", "echo_units", "dropped_turns")}
    stt.update(wer_all=sc_all["overall"]["wer"], errors_all=sc_all["overall"]["errors"], bag=ov.get("bag"),
               by_lang=sc.get("by_lang"), mixed=sc.get("mixed"), single=sc.get("single"))
    result = {
        "src": src, "dst": dst, "track": TRACK[d], "n_turns": len(own), "n_phrases": len(phrases),
        "stt": stt,
        "translation": {"chrf": _r(metrics.chrf_from_stats(tot), 2) if tot else None,
                        "chrf_matched": _r(metrics.chrf_from_stats(tot_m), 2) if tot_m else None,
                        "chrf_stats": tot, "chrf_matched_stats": tot_m, "word_order": word_order,
                        "groups": len(group_rows), "judge": judge},
        "latency": {k: metrics.summary(lat.get(k, [])) for k in
                    ("stt", "first_delta", "final", "ear", "turn_commit", "turn_ear", "first_caption", "app")},
        "stages": {k: metrics.summary(stages.get(k, [])) for k in
                   ("translate_wait", "ttft", "translate_rest", "speak_wait", "ttfb", "play_wait")},
        "counts": counts_out, "gender": gender, "tts": tts, "stt_input": stt_in, "reconnects": reconnects,
        # how each delay's start ("speech end") was found: the turn's end, a pause, or the estimate
        "speech_end_by": dict(collections.Counter(p.get("speech_end_by") for p in phrases if p["cls"] == "real")),
        "_raw": {"lat": dict(lat), "stages": dict(stages)},
    }
    return result, rows


def stt_input(run, d):
    """Seconds of the direction's own speech that reached its STT muted (the gate), and seconds in
    which Gusan's own voice reached it (the canceller / gate let it through)."""
    sim = run.sim
    C = sim_audio.CHUNK
    kind = CAPTURE[d]
    n = sim.length // C
    if n <= 0:
        return {"speech_s": 0.0, "muted_s": 0.0, "muted_ratio": None, "gusan_present_s": 0.0,
                "gusan_reached_stt_s": 0.0}
    N, dt = n * C, C / RATE
    x = run.stt_in[d].read(0, N).reshape(n, C)                  # what the STT was sent, 20 ms rows
    own = sim.system_clean if d == "hear" else sim.mic_clean
    src = np.zeros(N, np.float32)
    src[:min(N, len(own))] = own[:N]
    src = src.reshape(n, C)
    r_in = np.sqrt((x * x).mean(axis=1))
    r_src = np.sqrt((src * src).mean(axis=1))
    if kind == "loopback":
        r_src = r_src * sim.duck_gains(sim.origin + np.arange(n) * dt)
    speech = r_src > 300.0
    muted = speech & (r_in < 0.1 * r_src)
    present = got = np.zeros(n, bool)
    g = sim.gusan_track(kind, N)
    if g is not None:
        g = g.reshape(n, C)
        den = (g * g).sum(axis=1)
        present = np.sqrt(den / C) > 300.0
        ratio = (x * g).sum(axis=1) / np.maximum(den, 1e-9)
        got = present & (r_in > sim_audio.RX_FLOOR) & (ratio > sim_audio.HEARD_GAIN)
    sp, mu = float(speech.sum() * dt), float(muted.sum() * dt)
    return {"speech_s": _r(sp, 2), "muted_s": _r(mu, 2), "muted_ratio": _r(mu / sp) if sp else None,
            "gusan_present_s": _r(float(present.sum() * dt), 2), "gusan_reached_stt_s": _r(float(got.sum() * dt), 2)}


def phrase_row(p, sim):
    u = p["utt"]
    row = {"id": p["id"], "direction": p["dir"], "t": _r(p["t"] - sim.origin, 3), "text": p["text"],
           "class": p["cls"], "flags": p["flags"], "turns": p.get("turns") or [], "gender": p["gender"],
           "translation": p["translation"], "translate_error": p["tr_error"], "sim": p.get("sim"),
           "app_latency": p["app_latency"]}
    if p.get("speech_end") is not None:
        se = p["speech_end"]
        row["speech_end"] = _r(se - sim.origin, 3)
        row["speech_end_by"] = p.get("speech_end_by")
        row["lat"] = {"stt": _r(p["t"] - se, 3),
                      "first_delta": _r(p["t_delta"] - se, 3) if p["t_delta"] is not None else None,
                      "final": _r(p["t_final"] - se, 3) if p["t_final"] is not None else None,
                      "ear": _r(p["t_play"] - se, 3) if p.get("t_play") is not None else None}
    if p.get("echo_of") is not None:
        row["echo_of_utterance"] = p["echo_of"]
    if u is not None:
        row["voice"] = {"utterance": u.id, "voice_id": u.voice_id, "skipped": u.skipped, "error": u.error,
                        "audio_s": _r(u.samples / RATE, 2),
                        "ttfb": _r(u.t_first - u.t_call, 3) if u.t_first is not None and u.t_call is not None else None,
                        "play_at": _r(sim.time_of(u.first_pos) - sim.origin, 3) if u.first_pos is not None else None}
    c = p["call"]
    if c is not None:
        tm = c.get("timings") or {}
        row["translate"] = {"ttft": _r(c["t_first"] - c["t_call"], 3) if c["t_first"] is not None else None,
                            "total": _r(c["t_done"] - c["t_call"], 3) if c["t_done"] is not None else None,
                            "route": tm.get("route"), "model": tm.get("model"), "tries": tm.get("tries"),
                            "server_ms": tm.get("server_ms")}
    return row


# ── across conversations ─────────────────────────────────────────────────────────────────────
def _sum_stats(a, b):
    if not a:
        return [tuple(x) for x in b] if b else None
    if not b:
        return a
    return [tuple(x + y for x, y in zip(p, q)) for p, q in zip(a, b)]


def aggregate(convs):
    ok = [c for c in convs if c.get("status") == "ok" and c.get("directions")]
    skipped = sum(1 for c in convs if c.get("status") == "skipped")
    summ = {"conversations": len(convs), "ok": len(ok), "failed": len(convs) - len(ok) - skipped, "skipped": skipped,
            "chars_spoken": sum(c.get("chars_spoken") or 0 for c in ok),
            "chars_would_speak": sum(c.get("chars_would_speak") or 0 for c in ok),
            "chars_charged": sum(c.get("chars_charged") or 0 for c in convs),
            "echo_loops": sum(c.get("echo_loops") or 0 for c in ok),
            "echo_loops_by_kind": {k: sum((c.get("echo_loops_by_kind") or {}).get(k) or 0 for c in ok)
                                   for k in ("echo", "echo_mixed", "echo_suspect")},
            "echo_chain_max": max([c.get("echo_chain_max") or 0 for c in ok] or [0]),
            "crosstalk": sum(c.get("crosstalk") or 0 for c in ok),
            "errors": sum(sum((c.get("errors") or {}).values()) for c in ok),
            "reconnects": {k: sum((c.get("reconnects") or {}).get(k) or 0 for c in ok) for k in ("stt", "voice")},
            "stt_audio_s": _r(sum((x.get("stt_input") or {}).get("sent_s") or 0 for c in ok
                                  for x in (c.get("directions") or {}).values()), 1),
            "leaks": {"sockets_open_after_stop": sum(len((c.get("leaks") or {}).get("sockets_open_after_stop") or [])
                                                     for c in convs),
                      "utterances_after_stop": sum((c.get("leaks") or {}).get("utterances_after_stop") or 0
                                                   for c in convs)},
            "sim": {"capture_max_lag_s": max([(c.get("sim") or {}).get("capture_max_lag_s") or 0 for c in convs] or [0]),
                    "late_chunks": sum((c.get("sim") or {}).get("late_chunks") or 0 for c in convs),
                    "capture_errors": sum((c.get("sim") or {}).get("capture_errors") or 0 for c in convs)}}
    for d in DIRECTIONS:
        summ[d] = _agg_dir([c["directions"][d] for c in ok if d in c["directions"]],
                           [t for c in ok for t in c.get("turns") or [] if t.get("direction") == d])
    pairs = collections.defaultdict(list)
    for c in ok:
        pairs[c.get("pair")].append(c)
    summ["by_pair"] = {pair: {d: _agg_dir([c["directions"][d] for c in cs if d in c["directions"]], [], brief=True)
                              for d in DIRECTIONS} for pair, cs in sorted(pairs.items())}
    return summ


def _agg_dir(ds, turn_rows, brief=False):
    if not ds:
        return None
    st = [x["stt"] for x in ds]
    ref_u = sum(s.get("ref_units") or 0 for s in st)
    ref_c = sum(s.get("ref_chars") or 0 for s in st)
    out = {"wer": _r(sum(s.get("errors") or 0 for s in st) / ref_u) if ref_u else None,
           "wer_all": _r(sum(s.get("errors_all") or 0 for s in st) / ref_u) if ref_u else None,
           "cer": _r(sum(s.get("char_errors") or 0 for s in st) / ref_c) if ref_c else None}
    tot = tot_m = None
    for x in ds:
        tot = _sum_stats(tot, x["translation"].get("chrf_stats"))
        tot_m = _sum_stats(tot_m, x["translation"].get("chrf_matched_stats"))
    out["chrf"] = _r(metrics.chrf_from_stats(tot), 2) if tot else None
    out["chrf_matched"] = _r(metrics.chrf_from_stats(tot_m), 2) if tot_m else None
    lat = collections.defaultdict(list)
    stg = collections.defaultdict(list)
    for x in ds:
        for k, v in (x.get("_raw") or {}).get("lat", {}).items():
            lat[k] += v
        for k, v in (x.get("_raw") or {}).get("stages", {}).items():
            stg[k] += v
    out["latency"] = {k: metrics.summary(lat.get(k, [])) for k in
                      ("stt", "first_delta", "final", "ear", "turn_commit", "turn_ear", "first_caption", "app")}
    counts = collections.Counter()
    for x in ds:
        counts.update({k: v for k, v in x["counts"].items() if isinstance(v, int)})
    out["counts"] = dict(counts)
    if brief:
        return out
    out["stages"] = {k: metrics.summary(stg.get(k, [])) for k in
                     ("translate_wait", "ttft", "translate_rest", "speak_wait", "ttfb", "play_wait")}
    jall = [x["translation"].get("judge") for x in ds if isinstance(x["translation"].get("judge"), dict)]
    js = [j for j in jall if j.get("n")]
    if js or any(j.get("unparsed") for j in jall):
        def wmean(key, nkey):
            got = [(j[key], j.get(nkey, j["n"]) or 0) for j in js if isinstance(j.get(key), (int, float))]
            tot = sum(k for _v, k in got)
            return _r(sum(v * k for v, k in got) / tot, 1) if tot else None
        out["judge"] = {"n": sum(j["n"] for j in js), "unparsed": sum(j.get("unparsed") or 0 for j in jall),
                        "adequacy": wmean("adequacy", "n"), "fluency": wmean("fluency", "n_fluency")}
    g = [x["gender"] for x in ds if x.get("gender")]
    if g:
        j, rt = sum(x["judged"] for x in g), sum(x["right"] for x in g)
        vj, vr = sum(x["voice_judged"] for x in g), sum(x["voice_right"] for x in g)
        out["gender"] = {"judged": j, "accuracy": _r(rt / j) if j else None, "voice_judged": vj,
                         "voice_accuracy": _r(vr / vj) if vj else None,
                         "switches": sum(x["switches"] for x in g),
                         "expected_switches": sum(x["expected_switches"] for x in g)}
    tts = collections.Counter()
    for x in ds:
        tts.update({k: v for k, v in x["tts"].items() if isinstance(v, (int, float)) and not isinstance(v, bool)})
    out["tts"] = {k: (_r(v, 2) if isinstance(v, float) else v) for k, v in tts.items()}
    si = collections.Counter()
    for x in ds:
        si.update({k: v for k, v in x["stt_input"].items() if k.endswith("_s") and isinstance(v, (int, float))})
    out["stt_input"] = {k: _r(v, 2) for k, v in si.items()}
    if si.get("speech_s"):
        out["stt_input"]["muted_ratio"] = _r(si.get("muted_s", 0) / si["speech_s"])
    out["reconnects"] = {k: sum((x.get("reconnects") or {}).get(k) or 0 for x in ds) for k in ("stt", "voice")}
    se = collections.Counter()
    for x in ds:
        se.update(x.get("speech_end_by") or {})
    out["speech_end_by"] = dict(se)
    for kind in ("mixed", "single"):
        rows = [t for t in turn_rows if bool(t.get("mixed")) == (kind == "mixed")]
        if rows:
            out[kind] = {"turns": len(rows), "wer_mean": metrics.summary([t.get("wer") for t in rows])["mean"],
                         "chrf_mean": metrics.summary([t.get("chrf") for t in rows])["mean"],
                         "dropped": sum(1 for t in rows if t.get("dropped"))}
    return out


def strip_raw(convs):
    for c in convs:
        for x in (c.get("directions") or {}).values():
            x.pop("_raw", None)


# ── printing ─────────────────────────────────────────────────────────────────────────────────
def print_table(data, out=print):
    cfg = data["config"]
    mode = cfg.get("dry_run")
    dry = f"  DRY RUN ({mode}, clock x{cfg.get('speed')})" if mode else ""
    caps = "  captions only" if cfg.get("captions_only") else ""
    out(f"\nGusan Interpreter bench  {data['stamp']}  variant={cfg['variant']} route={cfg['route']} "
        f"vad={cfg['vad']} output={cfg.get('output') or 'by variant'} tts={cfg.get('tts_model') or 'default'}"
        f"{caps}{dry}")
    head = (f"{'conversation':28} {'dir':5} {'WER':>6} {'CER':>6} {'chrF':>5} {'stt':>5} {'xlat':>5} "
            f"{'ear50':>5} {'ear90':>5} {'gendr':>6} {'loop':>4} {'xtlk':>4} {'drop':>4} {'muted':>6} {'chars':>5}")
    out(head)
    out("-" * len(head))

    def line(name, d, x, chars):
        if not x:
            out(f"{name:28} {d:5} (no data)")
            return
        lat = x.get("latency") or {}
        g = x.get("gender") or {}
        c = x.get("counts") or {}
        si = x.get("stt_input") or {}
        stt = x.get("stt") or x
        tr = x.get("translation") or x
        out(f"{name[:28]:28} {d:5} {_pct(stt.get('wer'))} {_pct(stt.get('cer'))} "
            f"{_num(tr.get('chrf'), 1, 5)} {_num((lat.get('stt') or {}).get('p50'), 2, 5)} "
            f"{_num((lat.get('final') or {}).get('p50'), 2, 5)} {_num((lat.get('ear') or {}).get('p50'), 2, 5)} "
            f"{_num((lat.get('ear') or {}).get('p90'), 2, 5)} {_pct(g.get('voice_accuracy') if g.get('voice_accuracy') is not None else g.get('accuracy'))} "
            f"{c.get('echo', 0):4d} {c.get('crosstalk', 0):4d} {c.get('dropped_turns', 0):4d} "
            f"{_pct(si.get('muted_ratio'))} {chars:5d}")
    for c in data["conversations"]:
        if c.get("status") != "ok":
            out(f"{str(c.get('id'))[:28]:28} {str(c.get('status') or 'error').upper()} {c.get('error')}")
            continue
        for k, d in enumerate(DIRECTIONS):
            x = c["directions"].get(d)
            line(c["id"] if k == 0 else "", d, x, (x or {}).get("tts", {}).get("chars", 0))
    s = data["summary"]
    out("-" * len(head))
    for k, d in enumerate(DIRECTIONS):
        x = s.get(d)
        line("ALL" if k == 0 else "", d, x, ((x or {}).get("tts") or {}).get("chars", 0))
    out(f"columns: WER/CER of the phrases vs the script; chrF++ of the translations (end to end); stt = speech end"
        f" -> STT commit, xlat = -> translation done, ear50/90 = -> first audio played (s, p50/p90); gendr = right"
        f" voice for the speaker's gender; loop = echo loops; xtlk = the call heard by the mic; drop = dropped turns;"
        f" muted = own speech the gate muted")
    kinds = s.get("echo_loops_by_kind") or {}
    rc = s.get("reconnects") or {}
    out(f"ElevenLabs characters: {s['chars_spoken']} spoken (of {s['chars_would_speak']} if every phrase were spoken;"
        f" budget {cfg.get('max_chars')}, {cfg.get('budget_refused', 0)} phrase(s) refused); live STT fed "
        f"{(s.get('stt_audio_s') or 0) / 60:.1f} min of audio. Echo loops: {s['echo_loops']} ({kinds.get('echo', 0)} "
        f"Gusan's voice, {kinds.get('echo_mixed', 0)} mixed with mine, {kinds.get('echo_suspect', 0)} while only Gusan "
        f"played; longest chain {s['echo_chain_max']}), cross-talk: {s['crosstalk']}, errors: {s['errors']}, "
        f"reconnects: STT {rc.get('stt', 0)} / voice {rc.get('voice', 0)}."
        + (f" Leaks after stop: {s['leaks']}" if any(s['leaks'].values()) else "")
        + (f" Skipped: {s['skipped']}." if s.get("skipped") else ""))
    chunks = sum((c.get("sim") or {}).get("chunks") or 0 for c in data["conversations"]) or 1
    if s["sim"]["late_chunks"] > 0.01 * chunks or s["sim"]["capture_max_lag_s"] > 0.25 or s["sim"]["capture_errors"]:
        out(f"note: the machine fell behind real time ({s['sim']['late_chunks']} late audio chunks, max "
            f"{s['sim']['capture_max_lag_s']} s) or the engine raised in a capture callback "
            f"({s['sim']['capture_errors']}): delays may be off. Use fewer --parallel / a lower --speed.")


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────
def _proc_one(k, folder, tl, argd, mode, audio_out):
    """One conversation in a worker process (dry runs with --parallel: every simulation gets its
    own interpreter, its own GIL, so the clock can run fast without the captures falling behind)."""
    bench_client._utf8_console()
    logging.basicConfig(level=logging.INFO if argd.get("verbose") else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = argparse.Namespace(**argd)
    bench = bench_client.BenchClient(args.url, ledger=None, purpose="interpreter") if args.url else None
    try:
        r = run_conversation(Path(folder), tl, args, Spend(None), bench, mode, Path(audio_out) if audio_out else None)
    except Exception as e:  # noqa: BLE001
        r = {"id": tl.get("id"), "status": "error", "error": f"{type(e).__name__}: {e}"[:500],
             "traceback": traceback.format_exc()[-3000:]}
    return k, r


def ensure_dry_audio(corpus, audio_dir, only, cache_dir, log=print):
    """Dry runs voice any conversation that has no audio yet with generate.py's fake voices
    (run_stt.ensure_dry_audio, with the cache where the audio goes)."""
    have = {f.name for f, _tl in run_stt.load_timelines(audio_dir, only)}
    missing = [c for _p, c in generate.load_conversations(corpus, only) if c["id"] not in have]
    if not missing:
        return
    bench = bench_client.FakeBench(voices=generate.load_voices(corpus))
    cache = bench_client.TTSCache(cache_dir)
    for conv in missing:
        try:
            generate.generate_conversation(conv, bench, cache=cache, budget=bench_client.CharBudget(None),
                                           out_root=audio_dir, opts=generate.Options(dry_run=True), voices=None,
                                           log=lambda *_: None)
            log(f"  (made fake audio for {conv['id']})")
        except ValueError as e:
            log(f"  ! {conv['id']}: {e}")


ROUTE_PROVIDER = {"deepseek": "llm", "mistral": "mistral", "openai": "openai"}


def preflight(bench, args, say=print):
    """Real runs: is the bench function up, and does it hold the keys this run needs? -> None to
    go on, else the reason not to start (nothing has been spent yet)."""
    try:
        info = bench.ping() or {}
    except bench_client.BenchError as e:
        return str(e)
    prov = info.get("providers") or {}
    if prov.get("elevenlabs") is False:
        return ("the bench function has no ElevenLabs key: no tokens for the live speech-to-text and the voice "
                "(set the secret, deploy again)")
    need = ROUTE_PROVIDER.get(args.route)
    if need and prov.get(need) is False:
        return f"--route {args.route} needs the {need} key, which the bench function does not have"
    if args.route == "default" and prov and not any(prov.get(k) for k in ("llm", "mistral", "openai")):
        return "the bench function has no translation key at all (llm / mistral / openai)"
    missing = [k for k in ("llm", "mistral", "openai") if prov.get(k) is False]
    if missing and args.route == "default":
        say(f"  note: no {', '.join(missing)} key on the bench function: the default chain skips those models")
    return None


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Gusan Interpreter benchmark: the real engine on generated calls.")
    ap.add_argument("--only", default="", help="conversation ids or prefixes, comma-separated")
    ap.add_argument("--variant", default="clean", choices=generate.VARIANTS)
    ap.add_argument("--route", default="default", choices=bench_client.ROUTES, help="translation model chain")
    ap.add_argument("--vad", type=float, default=0.4, help="end-of-phrase silence (s) for Scribe (app default 0.4)")
    ap.add_argument("--output", choices=("auto", "headphones", "speakers"), default=None,
                    help="the interpreter's output setting (default: speakers for the speakers variant, else headphones)")
    ap.add_argument("--voice", choices=("match", "fixed"), default="match", help="Hear voices: match the speaker or one")
    ap.add_argument("--female", default="", help="the woman's voice id (default the app's: Sarah)")
    ap.add_argument("--male", default="", help="the man's voice id (default the app's: Eric)")
    ap.add_argument("--fixed-voice", default="", help="the one voice for --voice fixed (default River)")
    ap.add_argument("--clone-voice-id", default="",
                    help="Speak in this voice (a clone in the bench function's ElevenLabs account): own_voice on")
    ap.add_argument("--tts-model", default="", help="voice model for DialogueVoice (default: the engine's)")
    ap.add_argument("--captions-only", action="store_true", help="no voice in either direction (no characters)")
    ap.add_argument("--tts-sample", type=float, default=1.0, help="speak only this share of the phrases (0..1)")
    ap.add_argument("--max-chars", type=int, default=None,
                    help=f"ElevenLabs characters this run may speak (default {DEFAULT_MAX_CHARS}; dry runs: no limit)")
    ap.add_argument("--monthly-cap", type=int, default=bench_client.MONTHLY_CHARS,
                    help="also stop before the month's ledger passes this")
    ap.add_argument("--parallel", type=int, default=1,
                    help="conversations at a time (each its own interpreter); a real one holds 4 ElevenLabs sockets "
                         "(2 live STT, 2 voice), so the plan's concurrency limit applies")
    ap.add_argument("--dry-run", nargs="?", const="oracle", choices=("oracle", "sockets"), default=None,
                    help="offline: oracle (fake ElevenLabs clients) or sockets (the real clients, fake sockets)")
    ap.add_argument("--speed", type=float, default=1.0, help="dry runs only: run the clock this many times faster")
    ap.add_argument("--fault", default="", help="--dry-run sockets only: sockets that hang up on purpose, e.g. "
                                                "stt_drop@12,voice_drop@20 (s into each conversation, once per "
                                                "direction) - do the reconnects work, what is lost")
    ap.add_argument("--judge", action="store_true", help="also score translations with the LLM judge")
    ap.add_argument("--judge-route", choices=("deepseek", "openai", "mistral"), default="",
                    help="the judge's model (the function's default is DeepSeek, the translator itself, which may "
                         "favour its own wording: openai / mistral cross-check it)")
    ap.add_argument("--no-speak", action="store_true", help="Hear only (no virtual microphone)")
    ap.add_argument("--no-hear", action="store_true", help="Speak only")
    ap.add_argument("--no-duck", action="store_true", help="do not turn the call down while a translation plays")
    ap.add_argument("--duck-level", type=float, default=0.25)
    ap.add_argument("--echo-path", choices=("simple", "room"), default="simple",
                    help="speakers variant: Gusan's voice into the mic as 40 ms at -10 dB, or through the corpus room")
    ap.add_argument("--limit-seconds", type=float, default=0.0, help="play only the first N s of each conversation")
    ap.add_argument("--lead-in", type=float, default=1.0, help="silence before the conversation starts (s)")
    ap.add_argument("--tail", type=float, default=20.0, help="wait at most this long after the audio ends (s)")
    ap.add_argument("--corpus", default=str(generate.CORPUS_DIR))
    ap.add_argument("--audio-dir", default="", help="default corpus/audio (corpus/audio_dry for --dry-run)")
    ap.add_argument("--results-dir", default=str(RESULTS_DIR))
    ap.add_argument("--save-audio", action="store_true", help="also write what each STT got and what Gusan said")
    ap.add_argument("--events", action="store_true", help="keep the engine's raw events in the results")
    ap.add_argument("--url", default="", help="bench function URL (default bench_client.DEFAULT_URL); with --dry-run, "
                                              "tokens and translations come from it (voices and STT stay fake)")
    ap.add_argument("--seed", type=int, default=0, help="dry runs: fake STT errors")
    ap.add_argument("--stt-error-rate", type=float, default=0.03, help="dry runs: fake STT word error rate")
    ap.add_argument("--label", default="", help="a note stored with the results")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv=None):
    bench_client._utf8_console()
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    say = (lambda *a: None) if args.quiet else print
    mode = args.dry_run or "real"
    if mode == "real" and args.speed != 1.0:
        print("--speed is for dry runs only (real providers run in real time)", file=sys.stderr)
        return 2
    if args.no_hear and args.no_speak:
        print("--no-hear and --no-speak leave nothing to run", file=sys.stderr)
        return 2
    try:
        faults = parse_faults(args.fault)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if faults and mode != "sockets":
        print("--fault needs --dry-run sockets (the real eleven.py clients on fake sockets)", file=sys.stderr)
        return 2
    args.tts_sample = min(1.0, max(0.0, args.tts_sample))
    only = [x.strip() for x in args.only.split(",") if x.strip()]
    audio_dir = Path(args.audio_dir) if args.audio_dir else generate.default_dirs(args.corpus, mode != "real")[0]
    if mode != "real":
        cache = audio_dir.parent / "cache_dry" if args.audio_dir else generate.default_dirs(args.corpus, True)[1]
        ensure_dry_audio(args.corpus, audio_dir, only, cache, log=say)
    timelines = run_stt.load_timelines(audio_dir, only)
    if not timelines:
        print(f"no generated audio in {audio_dir}: run python generate.py{' --dry-run' if mode != 'real' else ''} first")
        return 1
    if mode == "real":
        fake = [tl.get("id") for _f, tl in timelines if tl.get("dry_run")]
        if fake:
            print(f"refusing: {', '.join(fake)} in {audio_dir} hold fake (dry-run) audio; regenerate them for real")
            return 1
    usable = []
    for folder, tl in timelines:
        try:
            variant_files(tl, folder, args.variant)
            usable.append((folder, tl))
        except ValueError as e:
            say(f"  skip {tl.get('id')}: {e}")
    if not usable:
        print(f"no conversation has the '{args.variant}' variant")
        return 1

    max_chars = args.max_chars if args.max_chars is not None else (None if mode != "real" else DEFAULT_MAX_CHARS)
    ledger = bench_client.CharLedger() if mode == "real" else None
    spend = Spend(0 if args.captions_only else max_chars, monthly_cap=args.monthly_cap if mode == "real" else None,
                  ledger=ledger)
    est = 0
    for _f, tl in usable:
        cut = _cut(tl, args.limit_seconds)
        for t in ground_truth(tl, cut):
            dst = tl.get("my_lang") if t["track"] == "system" else tl.get("their_lang")
            est += len(t["ref"].get(dst) or "")
    est = 0 if args.captions_only else int(est * args.tts_sample) + 10 * len(usable)
    bench = None
    if mode == "real" or args.url:
        # a dry run with --url talks to that function for tokens and translation (the voices and the
        # live STT stay fake): the self-test points it at a local stand-in
        bench = bench_client.BenchClient(args.url or bench_client.DEFAULT_URL, purpose="interpreter",
                                         **({} if mode == "real" else {"ledger": None}))
        why = preflight(bench, args, say)
        if why:
            print(f"not running: {why}", file=sys.stderr)
            return 2
    say(f"{'DRY RUN (' + mode + ') ' if mode != 'real' else ''}Gusan Interpreter on {len(usable)} conversation(s), "
        f"variant {args.variant}, {args.parallel} at a time; voice: "
        f"{'off (captions only)' if args.captions_only else f'{args.tts_sample:.0%} of the phrases'}; "
        f"about {est} characters needed, budget {max_chars if max_chars is not None else 'none'}"
        + (f" (month so far {ledger.month_total()} of {args.monthly_cap})" if ledger else ""))
    if max_chars is not None and est > max_chars and not args.captions_only:
        say(f"  note: the budget runs out before the end: later phrases are shown, not spoken "
            f"(--tts-sample / --max-chars / --captions-only)")
    results_dir = Path(args.results_dir)
    stamp = run_stt.new_stamp(results_dir, "interpreter")
    audio_out = results_dir / "audio" / f"interpreter_{stamp}" if args.save_audio else None
    convs = [None] * len(usable)
    t0 = time.perf_counter()
    interrupted = False
    fatal = {}

    def one(k):
        folder, tl = usable[k]
        if fatal:                                 # the function is gone / refuses the token: stop
            return k, {"id": tl.get("id"), "status": "skipped", "error": f"not run: {fatal['why']}"}
        try:
            r = run_conversation(folder, tl, args, spend, bench, mode, audio_out)
        except Exception as e:  # noqa: BLE001
            r = {"id": tl.get("id"), "status": "error", "error": f"{type(e).__name__}: {e}"[:500],
                 "traceback": traceback.format_exc()[-3000:]}
            if isinstance(e, bench_client.BenchError):
                r["bench_status"], r["bench_fatal"] = e.status, _fatal(e)
        if r.get("bench_fatal"):
            fatal.setdefault("why", r.get("error"))
        if ledger is not None:
            spent = r.get("chars_charged") or 0   # exact per conversation, also when it failed
            if spent:
                try:
                    ledger.add(spent, "interpreter")
                except OSError as e:
                    print(f"  (could not write the character ledger: {e})", file=sys.stderr)
        return k, r
    # Dry runs in parallel use processes: one GIL can not keep several fast clocks in real time.
    # Real runs (clock x1) use threads and share the character budget and the bench client.
    use_procs = mode != "real" and args.parallel > 1 and max_chars is None and len(usable) > 1
    if use_procs:
        ex = cf.ProcessPoolExecutor(max_workers=min(args.parallel, len(usable)))
    else:
        ex = cf.ThreadPoolExecutor(max_workers=max(1, args.parallel))
    try:
        if use_procs:
            futs = [ex.submit(_proc_one, k, str(folder), tl, vars(args), mode, str(audio_out) if audio_out else None)
                    for k, (folder, tl) in enumerate(usable)]
        else:
            futs = [ex.submit(one, k) for k in range(len(usable))]
        for fut in cf.as_completed(futs):
            k, r = fut.result()
            convs[k] = r
            if r.get("status") == "ok":
                h, s = (r["directions"].get("hear") or {}), (r["directions"].get("speak") or {})
                say(f"  {r['id']}: hear WER {_pct((h.get('stt') or {}).get('wer'))} ear p50 "
                    f"{_num(((h.get('latency') or {}).get('ear') or {}).get('p50'), 2, 4)} s | speak WER "
                    f"{_pct((s.get('stt') or {}).get('wer'))} ear p50 "
                    f"{_num(((s.get('latency') or {}).get('ear') or {}).get('p50'), 2, 4)} s | loops "
                    f"{r.get('echo_loops')} | {r.get('chars_spoken')} chars | {r['timing'].get('wall_s')} s")
            else:
                say(f"  {r.get('id')}: {str(r.get('status') or 'error').upper()} {r.get('error')}")
    except KeyboardInterrupt:
        interrupted = True
        print("interrupted: saving what finished", file=sys.stderr)
    finally:
        ex.shutdown(wait=not interrupted, cancel_futures=True)
    convs = [c for c in convs if c is not None]
    summary = aggregate(convs)
    strip_raw(convs)
    config = {k: getattr(args, k) for k in ("variant", "route", "vad", "output", "voice", "female", "male",
                                            "fixed_voice", "tts_model", "captions_only", "tts_sample", "parallel",
                                            "speed", "judge", "judge_route", "no_speak", "no_hear", "no_duck",
                                            "duck_level", "echo_path", "limit_seconds", "lead_in", "tail", "seed",
                                            "fault", "label")}
    config.update(dry_run=None if mode == "real" else mode, only=only, max_chars=max_chars,
                  clone_voice=bool(args.clone_voice_id), audio_dir=str(audio_dir),
                  budget_used=spend.used if not use_procs else summary["chars_spoken"],
                  budget_refused=spend.refused, interrupted=interrupted, workers="processes" if use_procs else "threads",
                  wall_s=round(time.perf_counter() - t0, 1), python=sys.version.split()[0],
                  stt_error_rate=args.stt_error_rate if mode != "real" else None,
                  url=args.url or ("default" if mode == "real" else ""))
    data = {"kind": "interpreter", "stamp": stamp, "config": config, "git": run_stt.git_sha(),
            "conversations": convs, "summary": summary}
    path = run_stt.save_results(results_dir, "interpreter", data)
    if not args.quiet:
        print_table(data)
    print(f"results: {path}")
    if audio_out is not None and audio_out.exists():
        print(f"audio: {audio_out}")
    if fatal:
        print(f"stopped early: {fatal['why']}", file=sys.stderr)
    return 0 if summary["failed"] == 0 and not summary["skipped"] and not interrupted else 3


if __name__ == "__main__":
    sys.exit(main())
