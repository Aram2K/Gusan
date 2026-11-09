"""Made-up text guard (A1), their voice under a Hear phrase (V5) and the translator's warm-up (S3),
bench findings 2026-11-09: guard.py on its own, then wired into the direction pipeline and the
controller with the other interpreter tests' fakes."""
import threading
import time

import numpy as np
import pytest

from interpreter import audio_io, controller, guard, segmenter, translate
from interpreter.guard import Guard, VoiceMeter, drop_repeats, find_repeat, words
from interpreter.pipeline import DirectionPipeline, wait_until
from tests import test_interpreter_controller as ctl
from tests.test_interpreter_core import FakeResp, FakeSink, FakeSTT, FakeVoice, sse


@pytest.fixture
def real_numpy():
    """The voice meter measures sound: skipped while test_core's suite-wide numpy stub is in."""
    try:
        ok = abs(audio_io.rms(b"\x10\x00" * 4) - 16.0) < 1e-6 and float(np.asarray([1.0, 3.0]).mean()) == 2.0
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        pytest.skip("real numpy needed (suite-wide stub in test_core)")


# ── words, the bench's way ───────────────────────────────────────────────────────────────────
class TestWords:
    def test_punctuation_is_ignored_and_an_address_is_one_word(self):
        assert words("How there？") == ["how", "there"]
        assert segmenter.units("How there？") == 3                 # why the cutter's units can't be used
        assert words("billing@nordsoft.dev.") == ["billing@nordsoft.dev"]
        assert words("aibuben.xyz") == ["aibuben.xyz"]
        assert words("No, no, no.") == ["no", "no", "no"]
        assert words("Mmm。") == ["mmm"] and words("はい。はい。") == ["はい", "はい"]
        assert words("$4,500?") == ["4,500"] and words("10:30") == ["10:30"]
        assert words("That's M-O-S-S.") == ["that's", "m-o-s-s"]
        assert words("լա՞վ") == ["լա՞վ"]                           # an Armenian mark inside a word
        assert words("Բարի լույս, Բարի լույս:") == ["բարի", "լույս", "բարի", "լույս"]


# ── A1: repeats ──────────────────────────────────────────────────────────────────────────────
class TestRepeats:
    @pytest.mark.parametrize("text,out", [
        ("No, no, no.", "No."),
        ("I think, I think we should go.", "I think we should go."),
        ("Thank you, thank you, thank you!", "Thank you!"),
        ("100 percent, I'll look. 100 percent, I'll look.", "100 percent, I'll look."),
        ("はい。はい。はい。", "はい。"),
    ])
    def test_a_run_said_again_back_to_back_is_said_once(self, text, out):
        assert drop_repeats(text) == out

    @pytest.mark.parametrize("text", [
        "No, no, that's not what I meant.",              # a real "no, no"
        "It's very very good.",
        "Bye-bye!",
        "Bye, Lusine. Bye.",
        "My code is 5 5 5 1.",                           # numbers
        "That's M O S S.",                               # a name spelled out
        "We will see, we will.",                         # not back to back
        "Please send it to r.moss@northpeak.io.",
        "Да, да, конечно.",
    ])
    def test_real_speech_is_left_alone(self, text):
        assert find_repeat(words(text)) is None
        assert drop_repeats(text) == text
        assert Guard("en").check(text, 0.0) == (text, True)

    def test_armenian_and_georgian_lose_the_whole_sentence_holding_a_repeat(self):
        # bench: route-mistral c02 39, keyterm-fix c10 20, voiced c01 8 (all made up)
        assert drop_repeats("Մի րոպե, ստուգեմ: Այո, այո, այո, այո:", True) == "Մի րոպե, ստուգեմ:"
        assert drop_repeats("სასტუმრო „ვერედან“. რა შეგიძლიათ დაგეხმაროთ? რა შეგიძლიათ დაგეხმაროთ?",
                            True) == "სასტუმრო „ვერედან“."
        hy = Guard("hy")
        assert hy.check("Մի րոպե, ստուգեմ: Այո, այո, այո, այո:", 30.0) == ("Մի րոպե, ստուգեմ:", True)
        # the same repeat in another language is said once instead
        assert drop_repeats("100 տոկոս նայեմ: 100 տոկոս նայեմ:") == "100 տոկոս նայեմ:"

    @pytest.mark.parametrize("src,text,out", [
        ("hy", "Ալո, ալո, ալո:", "Ալո:"),                          # a bad line: never lost
        ("hy", "Լսու՞մ եք, լսու՞մ եք:", "Լսու՞մ եք:"),
        ("hy", "Բարի լույս, Բարի լույս:", "Բարի լույս:"),          # bench speakers c01 3: said once, written twice
        ("hy", "100 տոկոս նայեմ: 100 տոկոս նայեմ:", "100 տոկոս նայեմ:"),
        ("ka", "გისმენთ, გისმენთ, გისმენთ.", "გისმენთ."),
    ])
    def test_in_armenian_and_georgian_a_repeat_that_is_the_whole_phrase_is_said_once(self, src, text, out):
        assert Guard(src).check(text, 0.0) == (out, True)

    def test_a_phrase_repeating_the_end_of_the_one_before_is_dropped(self):
        # bench: baseline c04 79/80 - the speech-to-text wrote the tail again, 2.3 s later
        before = "Այո, իհարկե: սովորաբար այն անդորագիրը կարող եք գրել այն անգամ մեկ անգամ ան"
        g = Guard("hy")
        assert g.check(before, 39.0) == (before, True)
        assert g.check("այն անգամ մեկ անգ", 41.3) is None
        assert g.check("Մեծ գումար չէ:", 47.6) == ("Մեծ գումար չէ:", True)
        # ...but only right after it: later, the same words are said again on purpose
        g = Guard("hy")
        g.check(before, 39.0)
        assert g.check("այն անգամ մեկ անգ", 39.0 + guard.END_S + 1)[1] is True

    def test_a_short_reply_said_again_is_a_real_answer(self):
        g = Guard("en")
        for i, text in enumerate(("Okay.", "Okay.", "Yes, sure.", "Yes, sure.", "Great.", "Great.")):
            assert g.check(text, i * 2.0) == (text, True)

    def test_a_whole_sentence_said_again_soon_is_dropped(self):
        g = Guard("en")
        assert g.check("Can we start the meeting now?", 0.0)[1]
        assert g.check("Sorry, one moment.", 3.0)[1]
        assert g.check("Can we start the meeting now?", 8.0) is None
        g = Guard("en")
        g.check("Can we start the meeting now?", 0.0)
        assert g.check("Can we start the meeting now?", guard.RECENT_S + 5)[1]      # a new question


# ── A1: flips ────────────────────────────────────────────────────────────────────────────────
class TestFlips:
    @pytest.mark.parametrize("src,text", [
        ("hy", "Love."), ("hy", "Masamp."), ("hy", "Buddy Lewis."), ("hy", "Sətəsütün."),
        ("zh", "How there？"), ("zh", "Mmm。"), ("ka", "Hello"), ("ru", "Love you"), ("ja", "Hai."),
        ("ar", "Sorry."), ("ko", "NE"),
    ])
    def test_short_latin_words_where_the_language_has_its_own_script_are_shown_not_voiced(self, src, text):
        assert Guard(src).check(text, 0.0) == (text, False)

    @pytest.mark.parametrize("src,text,glossary", [
        ("hy", "Telegram.", ["Telegram"]),                  # a Dictionary word
        ("hy", "Davit.", ["Davit Petrosyan"]),               # a word of a Dictionary name
        ("ru", "billing@nordsoft.dev.", []),                # an email
        ("hy", "aibuben.xyz", []),                          # a domain
        ("hy", "Metsgumart6.", []),                         # digits
        ("hy", "I owe you.", []),                           # three words
        ("hy", "OK.", []), ("ka", "Okay", []),              # said as a loanword everywhere
        ("hy", "Gusan-ի", []),                              # not all Latin letters
        ("hy", "Լավ:", []),
        ("en", "Love.", []), ("fr", "Love.", []),           # a language written in Latin letters
        ("sr", "Dobro.", []),                               # Serbian is written in Latin letters too
        ("auto", "Love.", []),
    ])
    def test_what_is_never_a_flip(self, src, text, glossary):
        assert Guard(src, glossary).check(text, 0.0) == (text, True)


# ── the pipeline ─────────────────────────────────────────────────────────────────────────────
TRANSLATIONS = {"Love.": "Love.", "Սպիտակ:": "White:", "Լավ:": "Good.", "Բարև ձեզ:": "Hello."}


class GusanSink(FakeSink):
    """Says whether Gusan's own voice played lately, as PlaybackSink.is_playing(tail_s) does."""

    def __init__(self, playing=False):
        super().__init__()
        self.playing = playing
        self.tails = []

    def is_playing(self, tail_s=0.3):
        self.tails.append(tail_s)
        return self.playing


def build(src="hy", dst="en", direction="hear", translate_fn=None, voice=None, sink=None, **kw):
    events, holder, asked = [], {}, []

    def factory(on_event):
        holder["stt"] = FakeSTT(on_event)
        return holder["stt"]

    def tr(text, a, b, ctx, on_delta):
        asked.append(text)
        out = TRANSLATIONS.get(text, text.upper())
        on_delta(out)
        return out
    p = DirectionPipeline(direction, src, dst, stt_factory=factory, translate=translate_fn or tr,
                          voice=voice or FakeVoice(), sink=sink if sink is not None else FakeSink(),
                          on_event=events.append, **kw)
    p.asked = asked
    return p, holder, events


def skipped(ev, reason):
    return [e for e in ev if e["type"] == "skipped" and e.get("reason") == reason]


def finals(ev):
    return [e["text"] for e in ev if e["type"] == "translation" and e.get("final")]


class TestPipelineGuard:
    def test_a_flip_is_translated_and_shown_but_never_voiced(self):
        voice = FakeVoice()
        p, h, ev = build(voice=voice, guard=Guard("hy"))
        p.start()
        try:
            h["stt"].on_event("committed", "Love.")
            h["stt"].on_event("committed", "Լավ:")
            assert wait_until(lambda: voice.spoken == ["Good."])
            assert finals(ev) == ["Love.", "Good."]                       # both shown
            flip = [e for e in ev if e["type"] == "phrase" and e["text"] == "Love."][0]
            assert [e["id"] for e in skipped(ev, "flip")] == [flip["id"]]
        finally:
            p.stop()
        assert voice.spoken == ["Good."]

    def test_a_repeat_is_dropped_before_it_is_translated(self):
        p, h, ev = build(guard=Guard("hy"))
        p.start()
        try:
            h["stt"].on_event("committed", "Այո, իհարկե: կարող եք գրել այն անգամ մեկ անգամ ան")
            h["stt"].on_event("committed", "այն անգամ մեկ անգ")
            h["stt"].on_event("committed", "Մեծ գումար չէ:")
            assert wait_until(lambda: len(p.asked) == 2)
            time.sleep(0.05)
        finally:
            p.stop()
        assert p.asked == ["Այո, իհարկե: կարող եք գրել այն անգամ մեկ անգամ ան", "Մեծ գումար չէ:"]
        assert [e["text"] for e in skipped(ev, "repeat")] == ["այն անգամ մեկ անգ"]
        assert "այն անգամ մեկ անգ" not in [e["text"] for e in ev if e["type"] == "phrase"]

    def test_the_phrase_queued_is_the_one_without_the_repeat(self):
        voice = FakeVoice()
        p, h, ev = build(voice=voice, guard=Guard("hy"))
        p.start()
        try:
            h["stt"].on_event("committed", "Մի րոպե, ստուգեմ: Այո, այո, այո, այո:")
            assert wait_until(lambda: voice.spoken == ["ՄԻ ՐՈՊԵ, ՍՏՈՒԳԵՄ:"])
        finally:
            p.stop()
        assert p.asked == ["Մի րոպե, ստուգեմ:"]
        assert [e["text"] for e in ev if e["type"] == "phrase"] == ["Մի րոպե, ստուգեմ:"]

    def test_a_broken_check_never_costs_a_phrase(self):
        class Broken(Guard):
            def check(self, text, now):
                raise RuntimeError("bug")
        voice = FakeVoice()
        p, h, _ev = build(voice=voice, guard=Broken("hy"))
        p.start()
        try:
            h["stt"].on_event("committed", "Լավ:")
            assert wait_until(lambda: voice.spoken == ["Good."])
        finally:
            p.stop()

    def test_a_flip_never_rides_along_in_a_merged_turn(self):
        gate = threading.Event()

        class SlowVoice(FakeVoice):
            def speak(self, text, on_audio, should_stop):
                gate.wait(2)
                return super().speak(text, on_audio, should_stop)
        voice = SlowVoice()
        p, h, ev = build(voice=voice, guard=Guard("hy"), max_waiting=1)
        p.start()
        try:
            for t in ("Բարև ձեզ:", "Love.", "Մի րոպե:", "Masamp.", "Հիմա կնայեմ:", "Լավ:"):
                h["stt"].on_event("committed", t)
            assert wait_until(lambda: len(finals(ev)) == 6)
            gate.set()
            assert wait_until(lambda: "Good." in " ".join(voice.spoken))
            time.sleep(0.05)
        finally:
            p.stop()
        said = " ".join(voice.spoken)
        assert "LOVE" not in said.upper() and "MASAMP" not in said
        assert len(skipped(ev, "flip")) == 2


# ── V5: their voice under a Hear phrase ──────────────────────────────────────────────────────
def chunk(level, n=320):
    """20 ms whose rms is about ``level`` (0: the gate's silence)."""
    if not level:
        return bytes(2 * n)
    t = np.arange(n) / 16000.0
    return (np.sin(2 * np.pi * 220.0 * t) * level * np.sqrt(2.0)).astype(np.int16).tobytes()


def feed(p, clock, levels):
    for lv in levels:
        clock["t"] += 0.02                     # the clock moves with the sound
        p.feed(chunk(lv))


# The real "Լավ" (Davit, 0.48 s) of the bench corpus, 20 ms rms: on the call's own track, and what the
# canceller + gate passed of it over Gusan's voice with the call turned down to 0.25 (offline replay).
LAV = [265, 1601, 2582, 3216, 3513, 4347, 3579, 3609, 3238, 3634, 2801, 1916, 2340, 1682, 1182, 816, 238,
       351, 269, 127, 67, 40, 35]
LAV_OVER_GUSAN = [0, 0, 0, 0, 878, 1086, 894, 902, 809, 908, 700, 478, 584, 420, 0, 0, 0, 0, 0, 0, 0, 0, 0]


@pytest.mark.usefixtures("real_numpy")
class TestTheirVoice:
    def test_the_meter_counts_only_chunks_with_a_voice(self):
        m = VoiceMeter()
        for i in range(10):
            m.feed(chunk(0), 1.0 + i * 0.02)                # the gate's silence
            m.feed(chunk(30), 1.0 + i * 0.02)               # a quiet line
        for i in range(10):
            m.feed(chunk(2000), 2.0 + i * 0.02)
        assert m.since(0.0) == pytest.approx(0.2)
        assert m.since(2.1) == pytest.approx(0.08)          # older chunks are forgotten
        assert m.since(0.0) == pytest.approx(0.08)

    def run_hear(self, levels, text, playing=True, before=None):
        clock = {"t": 50.0}
        sink, voice = GusanSink(playing=playing), FakeVoice()
        p, h, ev = build(voice=voice, sink=sink, clock=lambda: clock["t"], guard=Guard("hy"),
                         min_voice_s=guard.MIN_VOICE_S)
        p.start()
        try:
            if before:
                feed(p, clock, before[0])
                h["stt"].on_event("committed", before[1])
                assert wait_until(lambda: len(voice.spoken) == 1)
            feed(p, clock, levels)
            h["stt"].on_event("committed", text)
            assert wait_until(lambda: len(finals(ev)) == (2 if before else 1))
            time.sleep(0.05)
        finally:
            p.stop()
        return voice, ev, sink

    def test_a_phrase_heard_while_only_gusan_spoke_is_shown_not_voiced(self):
        # bench: voiced c01 23 "Սպիտակ" -> "White:" - the gate let none of the call through
        voice, ev, sink = self.run_hear([0] * 100, "Սպիտակ:")
        assert finals(ev) == ["White:"] and len(skipped(ev, "quiet")) == 1
        assert voice.spoken == []
        assert sink.tails and sink.tails[-1] == pytest.approx(2.0 + 0.3)   # did Gusan play in its 2 s?

    @pytest.mark.parametrize("levels", [LAV, LAV_OVER_GUSAN], ids=["call-track", "over-gusan"])
    def test_a_real_short_word_is_voiced(self, levels):
        voice, ev, _sink = self.run_hear([0] * 25 + levels + [0] * 30, "Լավ:")
        assert voice.spoken == ["Good."] and not skipped(ev, "quiet")

    def test_with_gusan_quiet_nothing_changes(self):
        voice, ev, _sink = self.run_hear([0] * 100, "Սպիտակ:", playing=False)
        assert voice.spoken == ["White:"]

    def test_the_voice_of_the_phrase_before_does_not_count(self):
        voice, ev, _sink = self.run_hear([0] * 100, "Սպիտակ:", before=([2000] * 40, "Բարև ձեզ:"))
        assert voice.spoken == ["Hello."] and len(skipped(ev, "quiet")) == 1

    # -- the window: a phrase's own words count even when they came before the last phrase's end --
    def drive(self, p, h, clock, until, voice, stt_events):
        """20 ms chunks up to clock time ``until``: their voice (900 rms, nothing gated) inside the
        ``voice`` spans, silence elsewhere; ``stt_events`` [(time, kind, text)] are sent on time."""
        todo = sorted(stt_events)
        while clock["t"] < until - 1e-9:
            while todo and todo[0][0] <= clock["t"] + 1e-9:
                _t, kind, text = todo.pop(0)
                h["stt"].on_event(kind, text)
            t = clock["t"]
            clock["t"] = round(t + 0.02, 4)
            p.feed(chunk(900 if any(a <= t < b for a, b in voice) else 0))

    def hear_over_gusan(self, start, until, voice_spans, stt_events, src="fr", n_spoken=2):
        """A Hear direction while Gusan's voice plays (an earlier phrase), the STT's wait 0.4 s."""
        clock = {"t": start}
        voice = FakeVoice()
        p, h, ev = build(src=src, voice=voice, sink=GusanSink(playing=True), clock=lambda: clock["t"],
                         guard=Guard(src), min_voice_s=guard.MIN_VOICE_S)
        p.start()
        try:
            h["stt"].vad_silence = 0.4
            self.drive(p, h, clock, until, voice_spans, stt_events)
            assert wait_until(lambda: len(finals(ev)) == 2 and len(voice.spoken) >= n_spoken)
            time.sleep(0.05)
        finally:
            p.stop()
        return voice, ev

    def test_a_short_tail_said_right_after_an_early_cut_is_voiced(self):
        # S1: "Mais le premier décembre," is cut from the live words 0.35 s after they stood still,
        # while "ça me paraît faisable." was already said (the live words come ~0.4 s late) - its
        # voice ends before that cut's end, and it was spoken loud and clear
        voice, ev = self.hear_over_gusan(50.0, 52.8, [(50.0, 51.7)], [
            (51.0, "partial", "Mais le premier"), (51.6, "partial", "Mais le premier décembre"),
            (51.75, "partial", "Mais le premier décembre, ça me"),
            (52.1, "partial", "Mais le premier décembre, ça me paraît faisable"),
            (52.5, "committed", "Mais le premier décembre, ça me paraît faisable.")])
        assert [e["text"] for e in ev if e["type"] == "phrase"] == ["Mais le premier décembre,", "ça me paraît faisable."]
        assert not skipped(ev, "quiet")
        assert voice.spoken == ["MAIS LE PREMIER DÉCEMBRE,", "ÇA ME PARAÎT FAISABLE."]

    def test_a_phrase_right_after_a_late_one_is_voiced(self):
        # bench c05 (5 runs): "Mais le premier décembre," went out 2 s late, 0.2 s before the rest of
        # the turn ("ça me paraît faisable.", said 45.6-47.05 s) - the rest was still voiced
        voice, ev = self.hear_over_gusan(44.0, 48.0, [(44.6, 47.05)], [
            (47.672, "committed", "Mais le premier décembre,"), (47.85, "committed", "ça me paraît faisable.")])
        assert not skipped(ev, "quiet") and len(voice.spoken) == 2

    def test_made_up_text_after_a_real_phrase_is_still_shown_not_voiced(self):
        # bench voiced c01: Davit's turn ends at 52.1 s and goes out at 52.87; "Սպիտակ" comes at
        # 54.27 from the gate's silence while Gusan talks
        real = "Իսկ ես թեստ կեյսերը կգրեմ մինչև երկու շաբթի, լավ:"
        voice, ev = self.hear_over_gusan(49.0, 54.4, [(49.22, 52.1)], [
            (52.873, "committed", real), (54.269, "committed", "Սպիտակ:")], src="hy", n_spoken=1)
        assert finals(ev)[-1] == "White:" and len(skipped(ev, "quiet")) == 1
        assert voice.spoken == [real.upper()]

    def test_nothing_is_measured_when_nothing_is_voiced(self, monkeypatch):
        calls = []
        monkeypatch.setattr(VoiceMeter, "feed", lambda self, pcm, now: calls.append(1))
        for kw in ({"speak_enabled": False, "min_voice_s": guard.MIN_VOICE_S},   # captions only
                   {"direction": "speak", "min_voice_s": 0.0}):                  # Speak
            p, _h, _ev = build(**kw)
            assert p._meter is None
            p.start()
            try:
                p.feed(chunk(2000))
            finally:
                p.stop()
        assert calls == []
        p, _h, _ev = build(min_voice_s=guard.MIN_VOICE_S)                       # Hear, voiced: one look per chunk
        p.start()
        try:
            p.feed(chunk(2000))
        finally:
            p.stop()
        assert calls == [1]


# ── S3: the translator's warm-up ─────────────────────────────────────────────────────────────
class SlowWarmHTTP:
    """The warm-up waits until released; a phrase is answered at once."""

    def __init__(self, fail=False):
        self.release = threading.Event()
        self.fail = fail
        self.calls = []

    def post(self, url, json=None, headers=None, **kw):
        self.calls.append({"thread": threading.current_thread().name, "url": url, "json": json,
                           "headers": headers})
        if json.get("warmup"):
            if self.fail:
                raise ConnectionError("offline")
            self.release.wait(5)
            return FakeResp(status=202, ctype="application/json", data={"ok": True})
        return FakeResp(lines=sse("Bonjour."))


class TestWarmUp:
    def test_it_sends_smart_actions_warm_up_on_a_side_thread(self):
        http, seen = SlowWarmHTTP(), []

        def token():
            seen.append(threading.current_thread().name)
            return "jwt"
        t0 = time.monotonic()
        th = translate.warm_up(token, session=http)
        assert time.monotonic() - t0 < 0.2                  # never waited for
        assert wait_until(lambda: http.calls)
        http.release.set()
        th.join(2)
        call = http.calls[0]
        assert call["json"] == {"warmup": True, "mode": "interpret"}       # 202, no quota (smart-action)
        assert call["url"] == translate.action_api.SMART_ACTION_URL
        assert call["headers"] == {"Authorization": "Bearer jwt"}
        assert call["thread"] == "interp-translate-warm" and seen == ["interp-translate-warm"]

    def test_a_phrase_never_waits_for_the_warm_up(self):
        http = SlowWarmHTTP()
        th = translate.warm_up("jwt", session=http)
        try:
            assert wait_until(lambda: http.calls)
            out = translate.translate_phrase("Hello.", "en", "fr", [], lambda d: None, token="jwt", session=http)
            assert out == "Bonjour." and th.is_alive()      # the warm-up is still out
        finally:
            http.release.set()
            th.join(2)

    def test_it_warms_this_threads_own_kept_connection(self, monkeypatch):
        made = []

        class Session:
            def __init__(self):
                self.bodies = []
                made.append(self)

            def post(self, url, json=None, **kw):
                self.bodies.append(json)
                if json.get("warmup"):
                    return FakeResp(status=202, ctype="application/json", data={"ok": True})
                return FakeResp(lines=sse("Hi"))
        monkeypatch.setattr(translate.requests, "Session", Session)
        monkeypatch.setattr(translate, "_local", threading.local(), raising=False)
        translate.warm_up("jwt").join(2)
        assert translate.translate_phrase("x", "fr", "en", [], lambda d: None, token="j") == "Hi"
        assert len(made) == 1                               # the phrase used the warmed session
        assert [b.get("warmup", False) for b in made[0].bodies] == [True, False]

    def test_a_failed_or_tokenless_warm_up_is_ignored(self):
        http = SlowWarmHTTP(fail=True)
        translate.warm_up("jwt", session=http).join(2)      # raised inside: nothing comes out
        assert len(http.calls) == 1
        http = SlowWarmHTTP()
        translate.warm_up(lambda: None, session=http).join(2)
        assert http.calls == []

    def test_the_pipeline_warms_up_at_start_and_after_a_quiet_spell(self):
        calls = []
        p, h, ev = build(src="en", dst="fr", warm_up=lambda: calls.append(threading.current_thread().name))
        p.warm_idle_s = 0.8
        p.start()
        try:
            assert wait_until(lambda: len(calls) == 1)
            assert calls == ["interp-hear-_translate_loop"]      # the thread whose connection translates
            for i in range(6):                                    # phrases keep it warm: no warm-up between
                h["stt"].on_event("committed", "phrase number %d" % i)
                time.sleep(0.1)
            assert len(calls) == 1 and len(p.asked) == 6
            assert wait_until(lambda: len(calls) == 2, timeout=3)   # quiet for warm_idle_s: once more
        finally:
            p.stop()

    def test_a_failing_warm_up_never_costs_a_phrase(self):
        def warm():
            raise RuntimeError("offline")
        voice = FakeVoice()
        p, h, _ev = build(src="en", dst="fr", voice=voice, warm_up=warm)
        p.start()
        try:
            h["stt"].on_event("committed", "See you tomorrow.")
            assert wait_until(lambda: voice.spoken == ["SEE YOU TOMORROW."])
        finally:
            p.stop()


# ── the controller: wired for the app, never for a test or the bench translator ─────────────────
def make_interp(settings=None, **kw):
    ctl.FakeSTT.made.clear()
    ctl.FakeVoice.made.clear()
    events = []

    def tr(text, a, b, ctx, on_delta, token=None, glossary=()):
        return f"[{b}] {text}"
    audio = ctl.FakeAudio()
    it = controller.Interpreter(lambda: "jwt", events.append,
                                settings or controller.Settings(my_lang="en", their_lang="ja", glossary=["Gusan"]),
                                audio=audio, session=ctl.FakeSession(), translate=tr,
                                stt_cls=ctl.FakeSTT, voice_cls=ctl.FakeVoice, **kw)
    return it, events, audio


@pytest.mark.usefixtures("real_numpy")
class TestController:
    def test_only_gusan_clouds_translator_is_warmed_up(self):
        app = controller.Interpreter(lambda: "jwt", None, controller.Settings(), audio=object(), session=object())
        assert app._warm_up is translate.warm_up            # (not started: no network here)
        it, _ev, _audio = make_interp()
        assert it._warm_up is None                          # a test's / the bench's own translator
        calls = []
        it, _ev, _audio = make_interp(warm_up=lambda token: calls.append((threading.current_thread().name, token())))
        it.start()
        try:
            assert wait_until(lambda: len(calls) == 2)
        finally:
            it.stop()
        assert sorted(calls) == [("interp-hear-_translate_loop", "jwt"), ("interp-speak-_translate_loop", "jwt")]

    def test_made_up_text_and_their_voice_are_checked_on_hear(self):
        it, ev, audio = make_interp()
        it.start()
        try:
            hear_sink = audio.sinks[0]
            loop = [c for c in audio.captures if c.loopback][0]
            hear_stt = [s for s in ctl.FakeSTT.made if s.language == "ja"][0]
            hear_voice = [v for v in ctl.FakeVoice.made if v.language == "en"][0]
            # Gusan's voice plays and the gate passes none of the call: shown, not voiced
            hear_sink.playing = True
            loop.on_pcm(bytes(16000))
            hear_stt.on_event("committed", "ありがとう")
            assert wait_until(lambda: skipped(ev, "quiet"))
            hear_sink.playing = False
            hear_stt.on_event("committed", "Hello there")              # a flip in Japanese
            assert wait_until(lambda: skipped(ev, "flip"))
            hear_stt.on_event("committed", "Gusan")                    # a Dictionary word: said
            assert wait_until(lambda: hear_voice.spoken == ["[en] Gusan"])
        finally:
            it.stop()
        assert "[en] ありがとう" in finals(ev) and "[en] Hello there" in finals(ev)
