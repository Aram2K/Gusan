"""Interpreter core (Qt-free): phrase cutting, the direction pipeline with fake streams, the
ElevenLabs clients against a fake websocket, the translator and the session client with fake HTTP."""
import base64
import json
import queue
import re
import threading
import time

import pytest
import requests

import action_api
from interpreter import eleven, pipeline, segmenter, session, translate
from interpreter.pipeline import DirectionPipeline, wait_until


# ── segmenter ────────────────────────────────────────────────────────────────────────────────
class TestPhraseCutter:
    def test_committed_text_goes_out_whole(self):
        c = segmenter.PhraseCutter()
        assert c.committed("Thanks for joining today.") == ["Thanks for joining today."]

    def test_unstable_partials_never_cut(self):
        c = segmenter.PhraseCutter(stable_s=0.35)
        assert c.partial("So the main thing, for this week is", 0.0) == []
        assert c.partial("So the main thing, for this week is the", 0.1) == []

    def test_stable_clause_goes_out_early_then_only_the_rest(self):
        c = segmenter.PhraseCutter(min_units=3, stable_s=0.3)
        t = "So the main thing for this week, is the launch checklist and"
        assert c.partial(t, 0.0) == []
        out = c.partial(t + " the", 0.4)
        assert out == ["So the main thing for this week,"]
        # the commit sends only what was not sent yet
        assert c.committed(t + " the budget.") == ["is the launch checklist and the budget."]

    def test_cjk_clause_marks_cut_without_spaces(self):
        c = segmenter.PhraseCutter(min_units=4, stable_s=0.3)
        t = "明日の会議は三時からです、資料は"
        c.partial(t, 0.0)
        assert c.partial(t + "水曜", 0.5) == ["明日の会議は三時からです、"]
        assert c.committed(t + "水曜日に送ります。") == ["資料は水曜日に送ります。"]

    def test_long_run_without_punctuation_is_cut_at_a_word(self):
        c = segmenter.PhraseCutter(min_units=4, max_units=10, stable_s=0.3)
        words = " ".join("w%d" % i for i in range(14))
        c.partial(words, 0.0)
        out = c.partial(words + " w14", 0.5)
        assert out and segmenter.units(out[0]) == 12          # all but the last 2 stable units

    def test_revised_commit_is_aligned_by_units(self):
        c = segmenter.PhraseCutter(min_units=2, stable_s=0.2)
        c.partial("Hello there, how are", 0.0)
        assert c.partial("Hello there, how are", 0.3) == ["Hello there,"]
        # the service rewrote the first words; we still send only what follows 2 units
        assert c.committed("Hello, there how are you?") == ["how are you?"]

    def test_punctuation_only_rest_is_dropped(self):
        c = segmenter.PhraseCutter(min_units=2, stable_s=0.2)
        c.partial("alpha beta gamma, delta", 0.0)
        assert c.partial("alpha beta gamma, delta", 0.3) == ["alpha beta gamma,"]
        assert c.committed("alpha beta gamma .") == []

    # a live STT grows the words one at a time: a CJK character, or a word with its marks
    _GROW = re.compile(r"\s*([\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]|[^\s\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]+)")

    _LINES = [
        "这个 project 的 deadline 是 Friday，我们需要在周四之前完成所有的测试和文档然后发给客户。",
        "我今天把PI发到你邮箱，tom艾特brightloop点io，对吧？",                # a mark glued to a word
        "今日の meeting は Zoom で、資料は明日の朝までに送りますのでよろしくお願いしますね。",
        "감사합니다, 오늘 회의에 참석해 주셔서 정말 감사드립니다.",
        "알겠습니다, 그럼 다음 주 수요일까지 자료를 보내 드리겠습니다.",
        "明日の会議は三時からです、資料は水曜日に送ります。",
        "So the main thing for this week, is the launch checklist and the budget.",
        "Բարև, Օլգա։ Մինչև հունվարի տասը պետք է ուղարկենք հաշվետվությունը։",
        "We met Dr. Smith in the U.S. last week, e.g. at the 9 a.m. call, and it cost 3.5 million.",
    ]

    @pytest.mark.parametrize("line", _LINES)
    def test_every_part_of_a_line_goes_out_once(self, line):
        c = segmenter.PhraseCutter()
        sent, t = [], 0.0
        for m in self._GROW.finditer(line):
            sent += c.partial(line[:m.end()], t)
            t += 0.1
        for _ in range(4):                          # on screen during the pause before the commit
            sent += c.partial(line, t)
            t += 0.1
        sent += c.committed(line, t)
        assert "".join(sent).replace(" ", "") == line.replace(" ", "")

    @pytest.mark.parametrize("line", _LINES)
    def test_every_part_goes_out_once_with_ticks_and_short_pauses(self, line):
        # the app ticks the cutter every 50 ms; the speaker stops briefly after each clause mark
        c = segmenter.PhraseCutter()
        sent, t = [], 0.0
        for m in self._GROW.finditer(line):
            sent += c.partial(line[:m.end()], t)
            stop = t + (0.5 if line[m.end() - 1] in ",.?։，。、？" else 0.1)
            while t < stop:
                t = round(t + 0.05, 2)
                sent += c.tick(t)
        sent += c.committed(line, t + 0.4)
        assert "".join(sent).replace(" ", "") == line.replace(" ", "")

    @pytest.mark.parametrize("line", _LINES)
    def test_every_part_goes_out_once_with_partials_a_second_apart(self, line):
        # what the bench saw: a partial ~1 s apart, a few words on, each ending in a full stop that
        # Scribe takes back with the next one ("。" in Chinese and Japanese, ":" in Armenian)
        c = segmenter.PhraseCutter()
        sent, t = [], 0.0
        ends = [m.end() for m in self._GROW.finditer(line)]
        for k in range(3, len(ends), 3):
            text = line[:ends[k - 1]]
            if text[-1] not in ",.?!:;։՝，。、？":
                text += ("。" if re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", text)
                         else ":" if re.search(r"[\u0531-\u058f]", text) else ".")
            sent += c.partial(text, t)
            for _ in range(20):                         # the app ticks; the next partial in 1 s
                t = round(t + 0.05, 2)
                sent += c.tick(t)
        sent += c.committed(line, t + 0.4)
        assert "".join(sent).replace(" ", "") == line.replace(" ", "")   # no mark it took back

    # -- cut on time (no new partial needed) ------------------------------------------------------
    @pytest.mark.parametrize("text", [
        "So the main thing for this week,",
        "Если всё будет готово к пятнице,",
        "Եթե ամեն ինչ պատրաստ լինի ուրբաթ օրը,",
        "Մենք կթողարկենք ուրբաթ օրը՝",                       # the Armenian pause mark
        "إذا كان كل شيء جاهزاً يوم الجمعة،",
    ])
    def test_still_words_ending_in_a_comma_are_cut_on_time(self, text):
        c = segmenter.PhraseCutter(min_units=3, stable_s=0.3)
        assert c.partial(text, 0.0) == []
        assert c.tick(0.2) == []                             # not still long enough yet
        assert c.tick(0.31) == [text]                        # no new partial needed
        assert c.tick(0.5) == []
        assert c.committed(text, 0.8) == []                  # the commit does not send it again

    def test_a_commit_after_a_cut_on_time_sends_only_the_rest(self):
        c = segmenter.PhraseCutter(min_units=3, stable_s=0.3)
        c.partial("We will ship it on Friday,", 0.0)
        assert c.tick(0.35) == ["We will ship it on Friday,"]
        assert c.committed("We will ship it on Friday, okay?", 0.9) == ["okay?"]

    # Scribe ends what it heard so far with a full stop even mid-sentence (its pause commits do:
    # "Let's keep it quick." for "...quick, I have an investor call"), and partials come ~1 s apart
    # (0.8-1.0 per second of speech in every bench run), so words that stood still prove no pause.
    @pytest.mark.parametrize("first, then, final", [
        ("We will ship the new version.", "We will ship the new version on Friday morning",
         "We will ship the new version on Friday morning at nine."),
        ("Can you send me the file today?", "Can you send me the file today or tomorrow",
         "Can you send me the file today or tomorrow?"),
        ("Մենք ուրբաթ օրը կթողարկենք նոր տարբերակը:",        # Scribe's Armenian full stop is ":"
         "Մենք ուրբաթ օրը կթողարկենք նոր տարբերակը առավոտյան",
         "Մենք ուրբաթ օրը կթողարկենք նոր տարբերակը առավոտյան ժամը ինը:"),
        ("Մենք ուրբաթ օրը կթողարկենք։", "Մենք ուրբաթ օրը կթողարկենք նորը", "Մենք ուրբաթ օրը կթողարկենք նորը։"),
        ("こちらこそよろしく。", "こちらこそよろしくお願いいたします",          # the 。 is a unit of its own:
         "こちらこそよろしくお願いいたします。"),                           # sent, "お" would be lost
        ("明日の会議は三時からです、", "明日の会議は三時からです資料は",            # any CJK mark: the same
         "明日の会議は三時からです資料は水曜日に送ります。"),
    ])
    def test_a_full_stop_at_the_live_edge_waits_for_the_next_partial(self, first, then, final):
        c = segmenter.PhraseCutter()                         # the app's defaults
        sent = c.partial(first, 0.0)
        for i in range(1, 20):                               # the app ticks; no partial for a second
            sent += c.tick(i * 0.05)
        sent += c.partial(then, 1.0)
        for i in range(21, 40):
            sent += c.tick(i * 0.05)
        assert sent == []
        assert c.committed(final, 2.0) == [final]            # one sentence, nothing lost

    def test_a_full_stop_goes_out_once_the_next_partial_goes_on_after_it(self):
        c = segmenter.PhraseCutter()
        c.partial("We will ship it on Friday.", 0.0)
        assert c.tick(0.5) == []
        assert c.partial("We will ship it on Friday. Then we", 1.0) == ["We will ship it on Friday."]
        assert c.committed("We will ship it on Friday. Then we test it.", 1.8) == ["Then we test it."]
        c.partial("明日の会議は三時からです。", 3.0)
        assert c.tick(3.5) == []
        assert c.partial("明日の会議は三時からです。資料は", 4.0) == ["明日の会議は三時からです。"]

    @pytest.mark.parametrize("first, then, final", [
        ("The whole pilot will cost about 1,", "The whole pilot will cost about 1,500 euros",
         "The whole pilot will cost about 1,500 euros."),
        ("The new price for the pilot is 3.", "The new price for the pilot is 3.5 million",
         "The new price for the pilot is 3.5 million."),
        ("The call starts tomorrow at 9:", "The call starts tomorrow at 9:30 sharp", "The call starts tomorrow at 9:30 sharp."),
    ])
    def test_a_number_at_the_live_edge_is_never_split(self, first, then, final):
        c = segmenter.PhraseCutter()
        sent = c.partial(first, 0.0) + c.tick(0.5) + c.tick(0.9)
        sent += c.partial(then, 1.0) + c.tick(1.5)
        assert sent == []                                    # "1," + "euros" would lose the 500
        assert c.committed(final, 2.0) == [final]

    def test_a_short_pause_cuts_the_clause_before_the_next_word(self):
        c = segmenter.PhraseCutter()                         # the app's defaults: still for 0.35 s
        head = "So the main thing for this week,"
        c.partial("So the main thing", 0.0)
        c.partial("So the main thing for this", 0.2)
        assert c.partial(head, 0.4) == []
        assert c.tick(0.7) == []                             # a short pause: no commit comes
        assert c.tick(0.76) == [head]                        # out during the pause
        line = head + " is the launch checklist and the budget."
        assert c.partial(head + " is the launch", 1.0) == []
        assert c.partial(line, 1.4) == []
        assert c.tick(1.8) == []                             # a full stop: Scribe may take it back
        assert c.committed(line, 2.2) == ["is the launch checklist and the budget."]

    def test_a_mark_the_newest_words_go_on_from_counts_at_once(self):
        c = segmenter.PhraseCutter(min_units=3, stable_s=0.3)
        c.partial("So the main thing for this week,", 0.0)
        c.partial("So the main thing for this week, is", 0.2)
        # the comma has stood still and a new word follows it: no need to wait for " is" to settle
        assert c.tick(0.35) == ["So the main thing for this week,"]

    def test_a_number_is_not_cut_at_its_point(self):
        c = segmenter.PhraseCutter(min_units=3, stable_s=0.3)
        c.partial("The new price is 3.", 0.0)
        c.partial("The new price is 3.5", 0.2)
        assert c.tick(0.4) == []                             # "3." stood still, but "5" follows it
        assert c.tick(1.0) == []
        assert c.committed("The new price is 3.5 million.", 1.2) == ["The new price is 3.5 million."]

    @pytest.mark.parametrize("text", [
        "We will need tools like Zoom e.g.",
        "The office is moving to the U.S.",
        "Let us meet tomorrow at nine a.m.",
        "I already talked about it with Dr.",
        "Please pass this on to Mrs.",
        "Bring the cables and adapters etc.",
        "We need better tools e.g. Zoom and Slack and",      # not inside the words either
        "I talked to Dr. Smith about the plan and",
    ])
    def test_a_dot_after_a_single_letter_or_a_title_is_no_clause_end(self, text):
        c = segmenter.PhraseCutter(min_units=3, stable_s=0.3)
        c.partial(text, 0.0)
        assert c.tick(1.0) == []

    def test_tick_does_no_work_when_nothing_is_pending(self, monkeypatch):
        c = segmenter.PhraseCutter(min_units=3, stable_s=0.3)
        looked = []
        cut = c._cut
        monkeypatch.setattr(c, "_cut", lambda now: looked.append(now) or cut(now))
        assert c.tick(1.0) == [] and looked == []            # nothing heard yet
        c.partial("We will ship it", 2.0)
        assert c.tick(2.1) == [] and c.tick(2.25) == []
        assert looked == [2.0]                               # nothing can change before 2.3: no look
        assert c.tick(2.35) == [] and looked[-1] == 2.35     # stood still (no mark): judged in full
        assert c.tick(2.4) == [] and c.tick(9.0) == []
        assert looked[-1] == 2.35                            # ...and then left alone
        c.committed("We will ship it.", 9.1)
        assert c.tick(9.5) == [] and looked[-1] == 2.35      # committed: nothing pending

    def test_tick_looks_again_when_a_partial_leaves_the_window(self):
        c = segmenter.PhraseCutter(min_units=3, stable_s=0.3)
        c.partial("So the main thing for this", 0.0)
        c.partial("So the main thing for this week,", 0.4)
        assert c.partial("So the main thing for this week, is", 0.5) == []   # 0.4 is in the window
        assert c.tick(0.65) == []
        assert c.tick(0.71) == ["So the main thing for this week,"]           # 0.4 left it

    def test_flush_sends_the_unsent_words_and_starts_afresh(self):
        c = segmenter.PhraseCutter(min_units=3, stable_s=0.3)
        t = "So the plan for the next quarter, is to hire two more engineers and"
        c.partial(t, 0.0)
        assert c.partial(t, 0.4) == ["So the plan for the next quarter,"]
        assert c.flush() == ["is to hire two more engineers and"]
        assert c.committed("Can you send me the file today?") == ["Can you send me the file today?"]


# ── fakes ────────────────────────────────────────────────────────────────────────────────────
class FakeSTT:
    def __init__(self, on_event):
        self.on_event = on_event
        self.audio = []
        self.started = self.closed = False

    def start(self):
        self.started = True

    def send_audio(self, pcm):
        self.audio.append(pcm)

    def close(self):
        self.closed = True


class FakeVoice:
    def __init__(self, seconds_per_char=0.0, fail_first=False):
        self.spoken = []
        self.opened = 0
        self.fail_first = fail_first
        self.delay = seconds_per_char

    def open(self):
        self.opened += 1

    def reopen(self):
        self.opened += 1

    def speak(self, text, on_audio, should_stop):
        if self.fail_first:
            self.fail_first = False
            raise eleven.VoiceError("lost")
        if self.delay:
            time.sleep(self.delay * len(text))
        self.spoken.append(text)
        on_audio(b"\x00\x00" * 1600)
        return 0.1

    def close(self):
        pass


class FakeSink:
    def __init__(self, queued=0.0):
        self.played = []
        self.queued = queued
        self.cleared = 0

    def play(self, pcm):
        self.played.append(pcm)

    def queued_seconds(self):
        return self.queued

    def clear(self):
        self.cleared += 1


class PlayingSink(FakeSink):
    """Plays in real time: what is queued goes down as time goes on."""

    def __init__(self):
        super().__init__()
        self.until = 0.0

    def play(self, pcm):
        super().play(pcm)
        self.until = max(self.until, time.monotonic()) + len(pcm) / 32000

    def queued_seconds(self):
        return max(0.0, self.until - time.monotonic())


def make(translate_fn=None, voice=None, sink=None, **kw):
    events = []
    holder = {}

    def factory(on_event):
        holder["stt"] = FakeSTT(on_event)
        return holder["stt"]

    def tr(text, src, dst, ctx, on_delta):
        on_delta(text.upper())
        return text.upper()
    p = DirectionPipeline("hear", "ja", "en", stt_factory=factory, translate=translate_fn or tr,
                          voice=voice or FakeVoice(), sink=sink or FakeSink(), on_event=events.append, **kw)
    return p, holder, events


class TestPipeline:
    def test_commit_is_translated_spoken_and_reported(self):
        voice, sink = FakeVoice(), FakeSink()
        p, h, ev = make(voice=voice, sink=sink)
        p.start()
        try:
            p.feed(b"\x01\x00" * 160)
            assert h["stt"].audio
            h["stt"].on_event("partial", "hello")
            h["stt"].on_event("committed", "hello there")
            assert wait_until(lambda: voice.spoken == ["HELLO THERE"])
            assert wait_until(lambda: any(e["type"] == "spoken" for e in ev))
            types = [e["type"] for e in ev]
            assert "live" in types and "phrase" in types and "translation" in types
            assert all(e["direction"] == "hear" for e in ev)
            assert sink.played
        finally:
            p.stop()
        assert h["stt"].closed and sink.cleared == 1

    def test_context_carries_the_last_phrases(self):
        seen = []

        def tr(text, src, dst, ctx, on_delta):
            seen.append(list(ctx))
            return text
        p, h, _ev = make(translate_fn=tr, context_size=2)
        p.start()
        try:
            for t in ("one", "two", "three"):
                h["stt"].on_event("committed", t)
            assert wait_until(lambda: len(seen) == 3)
        finally:
            p.stop()
        assert seen == [[], ["one"], ["one", "two"]]

    def test_translation_failure_skips_the_phrase_only(self):
        calls = []

        def tr(text, src, dst, ctx, on_delta):
            calls.append(text)
            if text == "bad":
                raise RuntimeError("boom")
            return text
        voice = FakeVoice()
        p, h, ev = make(translate_fn=tr, voice=voice)
        p.start()
        try:
            h["stt"].on_event("committed", "bad")
            h["stt"].on_event("committed", "good")
            assert wait_until(lambda: voice.spoken == ["good"])
        finally:
            p.stop()
        assert any(e["type"] == "error" and e["code"] == "translate" for e in ev)

    def test_fillers_are_skipped_when_far_behind(self):
        voice = FakeVoice()
        p, h, ev = make(voice=voice, sink=FakeSink(queued=8.0))
        p.start()
        try:
            h["stt"].on_event("committed", "okay")
            h["stt"].on_event("committed", "the budget is approved")
            assert wait_until(lambda: voice.spoken == ["THE BUDGET IS APPROVED"])
        finally:
            p.stop()
        assert any(e["type"] == "skipped" for e in ev)

    def test_backlog_is_merged_into_one_turn(self):
        gate = threading.Event()

        def tr(text, src, dst, ctx, on_delta):
            return text

        class SlowVoice(FakeVoice):
            def speak(self, text, on_audio, should_stop):
                gate.wait(2)
                return super().speak(text, on_audio, should_stop)
        voice = SlowVoice()
        p, h, ev = make(translate_fn=tr, voice=voice, max_waiting=2)
        p.start()
        try:
            for t in ("a1 a2 a3", "b1 b2 b3", "c1 c2 c3", "d1 d2 d3"):
                h["stt"].on_event("committed", t)
            time.sleep(0.2)
            gate.set()
            assert wait_until(lambda: sum(len(s.split()) for s in voice.spoken) == 12)
        finally:
            p.stop()
        assert len(voice.spoken) < 4
        assert any(e["type"] == "merged" for e in ev)

    def test_voice_drop_reopens_once_and_retries(self):
        voice = FakeVoice(fail_first=True)
        p, h, ev = make(voice=voice)
        p.start()
        try:
            h["stt"].on_event("committed", "hi")
            assert wait_until(lambda: voice.spoken == ["HI"])
        finally:
            p.stop()
        assert voice.opened == 2

    def test_captions_only_never_speaks(self):
        voice = FakeVoice()
        p, h, ev = make(voice=voice, speak_enabled=False)
        p.start()
        try:
            h["stt"].on_event("committed", "hi")
            assert wait_until(lambda: any(e["type"] == "translation" and e.get("final") for e in ev))
            time.sleep(0.05)
        finally:
            p.stop()
        assert voice.spoken == [] and voice.opened == 0

    def test_latency_is_phrase_end_to_first_audio_plus_queue(self):
        clock = {"t": 100.0}
        voice = FakeVoice()
        sink = FakeSink(queued=0.5)

        class Clock:
            def __call__(self):
                return clock["t"]
        p, h, ev = make(voice=voice, sink=sink, clock=Clock())
        p.start()
        try:
            h["stt"].on_event("committed", "hello")
            clock["t"] = 101.2
            assert wait_until(lambda: any(e["type"] == "spoken" for e in ev))
        finally:
            p.stop()
        spoken = [e for e in ev if e["type"] == "spoken"][0]
        assert spoken["latency"] == pytest.approx(1.7, abs=0.01)

    def test_is_filler(self):
        assert pipeline.is_filler("Okay.") and pipeline.is_filler("はい")
        assert not pipeline.is_filler("Okay, send it tonight")

    def test_no_and_numbers_are_never_filler(self):
        for t in ("No.", "Nein.", "Non.", "Нет.", "50.", "15", "不", "没有", "Go."):
            assert not pipeline.is_filler(t), t
        for t in ("Okay.", "Yeah.", "um", "はい"):
            assert pipeline.is_filler(t), t

    # -- a voice that fails, and Stop ----------------------------------------------------------
    def test_stop_mid_phrase_does_not_reopen_the_voice(self):
        started = threading.Event()

        class HangingVoice(FakeVoice):
            calls = 0

            def __init__(self):
                super().__init__()
                self.gone = threading.Event()

            def speak(self, text, on_audio, should_stop):
                self.calls += 1
                started.set()
                self.gone.wait(2)                    # until close() ends the socket
                raise eleven.VoiceError("voice connection lost")

            def close(self):
                self.gone.set()
        voice = HangingVoice()
        p, h, ev = make(voice=voice)
        p.start()
        h["stt"].on_event("committed", "hello there")
        assert started.wait(2)
        p.stop()
        assert voice.calls == 1 and voice.opened == 1          # no new socket, nothing sent again
        assert not any(e["type"] == "error" for e in ev)

    def test_a_phrase_cut_mid_way_is_not_said_again(self):
        class DropMidTurn(FakeVoice):
            calls = 0

            def speak(self, text, on_audio, should_stop):
                self.calls += 1
                if self.calls == 1:
                    on_audio(b"\x01\x00" * 160)              # its first words are heard ...
                    raise eleven.VoiceError("voice connection lost")       # ... then Wi-Fi blips
                return super().speak(text, on_audio, should_stop)
        voice, sink = DropMidTurn(), FakeSink()
        p, h, ev = make(voice=voice, sink=sink)
        p.start()
        try:
            h["stt"].on_event("committed", "first phrase")
            assert wait_until(lambda: voice.opened == 2)       # a fresh socket for the next one
            h["stt"].on_event("committed", "second phrase")
            assert wait_until(lambda: voice.spoken == ["SECOND PHRASE"])
        finally:
            p.stop()
        assert voice.calls == 2 and len(sink.played) == 2
        assert not any(e["type"] == "error" for e in ev)

    def test_the_voice_comes_back_after_failing(self):
        class Down(FakeVoice):
            down = True

            def speak(self, text, on_audio, should_stop):
                if self.down:
                    raise eleven.VoiceError("voice connection lost")
                return super().speak(text, on_audio, should_stop)
        voice = Down()
        p, h, ev = make(voice=voice, voice_rest_s=0.05)
        p.start()
        try:
            h["stt"].on_event("committed", "one")             # fails, reopens, fails again
            assert wait_until(lambda: any(e["type"] == "error" for e in ev))
            voice.down = False                                 # the network is back
            time.sleep(0.15)                                   # (after the short rest)
            h["stt"].on_event("committed", "two")
            assert wait_until(lambda: voice.spoken == ["TWO"])
        finally:
            p.stop()
        assert [e["code"] for e in ev if e["type"] == "error"] == ["voice"]       # said once

    def test_a_voice_that_did_not_open_is_tried_again(self):
        class NoOpen(FakeVoice):
            def open(self):
                raise eleven.VoiceError("no token")
        voice = NoOpen()
        p, h, ev = make(voice=voice, voice_rest_s=0.05)
        p.start()
        try:
            time.sleep(0.15)
            h["stt"].on_event("committed", "hello")
            assert wait_until(lambda: voice.spoken == ["HELLO"])
        finally:
            p.stop()

    def test_the_voice_warms_up_beside_the_speech_to_text(self):
        # F28: start() never waits for the voice's warm-up (~3 s for a cloned voice): what is said
        # meanwhile is heard and translated, and a phrase ready first waits for the voice
        go = threading.Event()

        class SlowOpen(FakeVoice):
            def open(self):
                go.wait(5)
                super().open()
        voice = SlowOpen()
        p, h, ev = make(voice=voice)
        t0 = time.monotonic()
        p.start()
        try:
            assert time.monotonic() - t0 < 1.0 and voice.opened == 0
            assert [e["state"] for e in ev if e["type"] == "state"] == ["starting", "running"]
            h["stt"].on_event("committed", "hello")
            assert wait_until(lambda: any(e["type"] == "translation" and e.get("final") for e in ev))
            time.sleep(0.1)
            assert voice.spoken == []                                # it waits for the voice ...
            go.set()
            assert wait_until(lambda: voice.spoken == ["HELLO"])     # ... and is said, never lost
        finally:
            go.set()
            p.stop()
        assert not any(e["type"] in ("error", "skipped") for e in ev)

    def test_a_stop_while_the_voice_connects_ends_at_once(self):
        go = threading.Event()

        class SlowOpen(FakeVoice):
            def open(self):
                go.wait(5)
                super().open()
        voice = SlowOpen()
        p, h, ev = make(voice=voice)
        p.start()
        h["stt"].on_event("committed", "hello")
        assert wait_until(lambda: any(e["type"] == "translation" and e.get("final") for e in ev))
        t0 = time.monotonic()
        p.stop()
        assert time.monotonic() - t0 < 1.0                          # the waiting phrase never holds it up
        go.set()
        time.sleep(0.1)
        assert voice.spoken == [] and not any(e["type"] == "error" for e in ev)

    # -- translations that go wrong ----------------------------------------------------------------
    def test_reasoning_replaced_by_the_answer_is_not_kept(self):
        def tr(text, src, dst, ctx, on_delta):
            on_delta("The user wants an Armenian translation")
            on_delta(action_api.ReplaceText("Բարև"))
            on_delta("։")
            return "Բարև։"
        voice = FakeVoice()
        p, h, ev = make(translate_fn=tr, voice=voice)
        p.start()
        try:
            h["stt"].on_event("committed", "Hi.")
            assert wait_until(lambda: voice.spoken == ["Բարև։"])
        finally:
            p.stop()
        live = [e["text"] for e in ev if e["type"] == "translation" and not e["final"]]
        assert live == ["The user wants an Armenian translation", "Բարև", "Բարև։"]

    def test_a_cut_translation_is_shown_cut_and_never_said(self):
        calls = []

        def tr(text, src, dst, ctx, on_delta):
            calls.append(text)
            on_delta("We can sign")
            raise translate.TranslateError("The translation was cut off.", code="cut", partial="We can sign")
        voice = FakeVoice()
        p, h, ev = make(translate_fn=tr, voice=voice)
        p.start()
        try:
            h["stt"].on_event("committed", "On peut signer seulement si le prix baisse")
            assert wait_until(lambda: any(e["type"] == "translation" and e["final"] for e in ev))
            time.sleep(0.05)
        finally:
            p.stop()
        final = [e for e in ev if e["type"] == "translation" and e["final"]]
        assert final == [dict(final[0], text="We can sign …", cut=True)]
        assert voice.spoken == [] and len(calls) == 2           # asked once more, at once

    def test_a_translation_cut_at_once_is_asked_again(self):
        calls = []

        def tr(text, src, dst, ctx, on_delta):
            calls.append(text)
            if len(calls) == 1:
                on_delta("We can")
                raise translate.TranslateError("The translation was cut off.", code="cut", partial="We can")
            on_delta("We can sign only if the price drops")
            return "We can sign only if the price drops"
        voice = FakeVoice()
        p, h, ev = make(translate_fn=tr, voice=voice)
        p.start()
        try:
            h["stt"].on_event("committed", "On peut signer seulement si le prix baisse")
            assert wait_until(lambda: voice.spoken == ["We can sign only if the price drops"])
        finally:
            p.stop()
        assert not any(e["type"] == "error" for e in ev)

    def test_signed_out_is_said_once_not_hidden(self):
        def tr(text, src, dst, ctx, on_delta):
            raise translate.TranslateError("Sign in to use the interpreter.", code="auth")
        p, h, ev = make(translate_fn=tr)
        p.start()
        try:
            for t in ("one", "two", "three"):
                h["stt"].on_event("committed", t)
            assert wait_until(lambda: sum(1 for e in ev if e.get("code") == "translate") == 3)
        finally:
            p.stop()
        assert [e["code"] for e in ev if e["type"] == "error" and e["code"] != "translate"] == ["auth"]

    def test_a_run_of_failed_translations_is_said(self):
        ok = {"now": False}

        def tr(text, src, dst, ctx, on_delta):
            if not ok["now"]:
                raise translate.TranslateError("Network error: offline", code="network")
            return text
        p, h, ev = make(translate_fn=tr)

        def told():
            return [e["code"] for e in ev if e["type"] == "error" and e["code"] != "translate"]

        def failed():
            return sum(1 for e in ev if e.get("code") == "translate")
        p.start()
        try:
            for t in ("one", "two"):
                h["stt"].on_event("committed", t)
            assert wait_until(lambda: failed() == 2)
            assert told() == []                              # one or two: a blip, not said
            for t in ("three", "four"):
                h["stt"].on_event("committed", t)
            assert wait_until(lambda: failed() == 4)
            ok["now"] = True
            h["stt"].on_event("committed", "five")
            assert wait_until(lambda: any(e["type"] == "spoken" for e in ev))
        finally:
            p.stop()
        assert told() == ["network"]                         # said once

    # -- speech-to-text that drops ---------------------------------------------------------------
    def test_a_dropped_connection_keeps_the_heard_words_and_the_next_phrase(self):
        clock = {"t": 0.0}
        p, h, ev = make(translate_fn=lambda text, *a: text, clock=lambda: clock["t"],
                        cutter=segmenter.PhraseCutter(min_units=3, stable_s=0.3))
        p.start()
        try:
            t = "So the plan for the next quarter, is to hire two more engineers and"
            h["stt"].on_event("partial", t)
            clock["t"] = 0.4
            h["stt"].on_event("partial", t)                  # "So the plan ... quarter," goes early
            h["stt"].on_event("dropped", "")                 # gone before the commit; reconnected
            h["stt"].on_event("committed", "Can you send me the file today?")
        finally:
            p.stop()
        assert [e["text"] for e in ev if e["type"] == "phrase"] == [
            "So the plan for the next quarter,", "is to hire two more engineers and",
            "Can you send me the file today?"]

    def test_speech_to_text_gone_for_good_is_said_once(self):
        p, h, ev = make()
        p.start()
        try:
            h["stt"].on_event("error", "rate_limited: busy")
            h["stt"].on_event("error", "rate_limited: busy")     # the next connection, the same
            h["stt"].on_event("closed", "")                      # it gave up
        finally:
            p.stop()
        assert [e["code"] for e in ev if e["type"] == "error"] == ["stt"]
        assert [e for e in ev if e["type"] == "state" and e["state"] == "stt_closed"][0]["code"] == ""

    def test_speech_to_text_closed_for_an_account_reason_says_it(self):
        p, h, ev = make()
        p.start()
        try:
            h["stt"].on_event("closed", "allowance_used")
        finally:
            p.stop()
        assert [e["code"] for e in ev if e["type"] == "error"] == ["allowance_used"]

    def test_speech_to_text_gone_for_good_leaves_the_one_sentence_to_the_app(self):
        # F94: the app starts it again (nothing to say) or stops and says so - never both
        p, h, ev = make()
        p.start()
        try:
            h["stt"].on_event("closed", "network")
        finally:
            p.stop()
        assert [e for e in ev if e["type"] == "error"] == []
        assert [e["code"] for e in ev if e["type"] == "state" and e["state"] == "stt_closed"] == ["network"]

    # -- delay and keeping up ----------------------------------------------------------------------
    def test_latency_counts_the_pause_the_stt_waited_for(self):
        clock = {"t": 100.0}
        go = threading.Event()

        def tr(text, src, dst, ctx, on_delta):
            go.wait(2)
            return text
        p, h, ev = make(translate_fn=tr, sink=FakeSink(queued=0.5), clock=lambda: clock["t"])
        p.start()
        try:
            h["stt"].vad_silence = 0.4                       # it commits after 0.4 s of quiet
            h["stt"].on_event("committed", "hello")
            clock["t"] = 101.2
            go.set()
            assert wait_until(lambda: any(e["type"] == "spoken" for e in ev))
        finally:
            p.stop()
        spoken = [e for e in ev if e["type"] == "spoken"][0]
        assert spoken["latency"] == pytest.approx(1.2 + 0.4 + 0.5, abs=0.01)

    def test_an_early_cut_counts_from_when_its_words_stood_still(self):
        clock = {"t": 0.0}
        go = threading.Event()

        def tr(text, src, dst, ctx, on_delta):
            go.wait(2)
            return text
        p, h, ev = make(translate_fn=tr, clock=lambda: clock["t"],
                        cutter=segmenter.PhraseCutter(min_units=3, stable_s=0.3))
        p.start()
        try:
            t = "So the plan for the next quarter, is to"
            h["stt"].on_event("partial", t)
            clock["t"] = 0.4
            h["stt"].on_event("partial", t)                  # cut: unchanged for 0.3 s and more
            clock["t"] = 1.0
            go.set()
            assert wait_until(lambda: any(e["type"] == "spoken" for e in ev))
        finally:
            p.stop()
        spoken = [e for e in ev if e["type"] == "spoken"][0]
        assert spoken["latency"] == pytest.approx(1.0 - (0.4 - 0.3), abs=0.01)

    def test_feed_cuts_words_that_stood_still_with_no_new_partial(self):
        clock = {"t": 0.0}
        p, h, ev = make(translate_fn=lambda text, *a: text, clock=lambda: clock["t"],
                        cutter=segmenter.PhraseCutter(min_units=3, stable_s=0.3))

        def phrases():
            return [e["text"] for e in ev if e["type"] == "phrase"]
        p.start()
        try:
            h["stt"].on_event("partial", "We will ship it on Friday,")
            clock["t"] = 0.27
            p.feed(b"\x00\x00" * 320)                       # looked: not still long enough
            assert phrases() == []
            clock["t"] = 0.31
            p.feed(b"\x00\x00" * 320)                       # still now, but looked < 50 ms ago
            assert phrases() == []
            clock["t"] = 0.35
            p.feed(b"\x00\x00" * 320)                       # cut on time: no new partial came
            assert phrases() == ["We will ship it on Friday,"]
            h["stt"].on_event("committed", "We will ship it on Friday, okay?")
            assert wait_until(lambda: any(e["type"] == "spoken" for e in ev))
        finally:
            p.stop()
        assert phrases() == ["We will ship it on Friday,", "okay?"]   # not sent again
        spoken = [e for e in ev if e["type"] == "spoken"][0]
        assert spoken["latency"] == pytest.approx(0.3, abs=0.01)   # said >= stable_s before the cut

    def test_a_commit_during_a_cut_on_time_waits_so_the_phrases_stay_in_order(self):
        # the tick cuts on the capture thread, the commit comes on the STT reader: without the lock
        # the commit's rest ("okay?") could be queued before the head the tick just cut
        cutting, go = threading.Event(), threading.Event()

        class SlowCutter(segmenter.PhraseCutter):
            def tick(self, now):
                out = super().tick(now)
                if out:
                    cutting.set()
                    go.wait(2)                               # the commit comes right now
                return out
        clock = {"t": 0.0}
        p, h, ev = make(translate_fn=lambda text, *a: text, clock=lambda: clock["t"],
                        cutter=SlowCutter(min_units=3, stable_s=0.3))
        p.start()
        try:
            h["stt"].on_event("partial", "We will ship it on Friday,")
            clock["t"] = 0.35
            feeder = threading.Thread(target=p.feed, args=(b"\x00\x00" * 320,))
            feeder.start()
            assert cutting.wait(2)
            commit = threading.Thread(target=h["stt"].on_event,
                                      args=("committed", "We will ship it on Friday, okay?"))
            commit.start()
            commit.join(0.2)
            assert commit.is_alive()                         # it waits for the cut in flight
            go.set()
            feeder.join(2)
            commit.join(2)
        finally:
            go.set()
            p.stop()
        assert [e["text"] for e in ev if e["type"] == "phrase"] == ["We will ship it on Friday,", "okay?"]

    def test_the_voice_never_runs_far_ahead_of_the_ear(self):
        sink = PlayingSink()
        seen = []

        class Voice(FakeVoice):
            def speak(self, text, on_audio, should_stop):
                seen.append(sink.queued_seconds())
                self.spoken.append(text)
                on_audio(b"\x00\x00" * 3200)                 # 0.2 s of audio, made at once
                return 0.2
        voice = Voice()
        p, h, ev = make(translate_fn=lambda text, *a: text, voice=voice, sink=sink, ahead_s=0.3)
        p.start()
        try:
            for i in range(8):
                h["stt"].on_event("committed", "phrase number %d" % i)
            assert wait_until(lambda: sum(s.count("phrase") for s in voice.spoken) == 8, timeout=5)
        finally:
            p.stop()
        assert max(seen) <= 0.3 + 0.05                      # never more than ahead_s queued
        assert any(e["type"] == "merged" for e in ev)       # the rest waited, and went as one turn


# ── ElevenLabs clients against a fake websocket ──────────────────────────────────────────────
class FakeWS:
    def __init__(self, incoming=()):
        self.sent = []
        self.incoming = queue.Queue()
        for m in incoming:
            self.incoming.put(m)
        self.closed = False

    def send(self, msg):
        self.sent.append(json.loads(msg))

    def recv(self, timeout=None):
        try:
            return self.incoming.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError

    def __iter__(self):
        while not self.closed:
            try:
                m = self.incoming.get(timeout=0.05)
            except queue.Empty:
                continue
            if m is None:
                return
            yield m

    def close(self):
        self.closed = True
        self.incoming.put(None)


class FlakyTokens:
    """A token provider that fails ``fails`` times first (offline, or a refusal), then works."""

    def __init__(self, fails, code="network"):
        self.fails, self.code, self.calls = fails, code, 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.fails:
            raise session.InterpreterError(self.code)
        return "t%d" % self.calls


class TestScribe:
    def test_url_and_audio_chunks(self):
        ws = FakeWS()
        urls = []
        events = []
        s = eleven.ScribeRealtime("tok1", "hy", lambda k, t: events.append((k, t)),
                                  keyterms=["Gusan", "Aibuben"], vad_silence=0.3,
                                  connect=lambda u: (urls.append(u), ws)[1])
        s.start()
        try:
            s.send_audio(b"\x00" * 3000)
            assert ws.sent == []                     # under 100 ms: buffered
            s.send_audio(b"\x00" * 3400)            # 6400 bytes = 2 chunks of 3200
            assert len(ws.sent) == 2
            m = ws.sent[0]
            assert m["message_type"] == "input_audio_chunk" and m["commit"] is False and m["sample_rate"] == 16000
            assert len(base64.b64decode(m["audio_base_64"])) == 3200
            ws.incoming.put(json.dumps({"message_type": "partial_transcript", "text": "Բարև"}))
            ws.incoming.put(json.dumps({"message_type": "committed_transcript", "text": "Բարև ձեզ։"}))
            ws.incoming.put(json.dumps({"message_type": "quota_exceeded", "error": "no credit"}))
            assert wait_until(lambda: len(events) == 4)
        finally:
            s.close()
        u = urls[0]
        assert "token=tok1" in u and "language_code=hy" in u and "commit_strategy=vad" in u
        assert "vad_silence_threshold_secs=0.3" in u and "keyterms=Gusan" in u and "keyterms=Aibuben" in u
        assert events[0] == ("partial", "Բարև") and events[1] == ("committed", "Բարև ձեզ։")
        assert events[2][0] == "error" and "quota_exceeded" in events[2][1]
        assert events[3] == ("closed", "quota_exceeded")            # no credit: not tried again

    def test_dictionary_words_are_cut_to_what_scribe_takes(self):
        """Scribe refuses the WHOLE session for one keyterm over 20 characters (the bench's e-mail
        addresses, 2026-11-09): a long term gives its parts, nothing over 20 goes, no repeats."""
        got = eleven.clean_keyterms(["Gusan", "claire.dubois@telva.fr", "  Davit  ", "gusan",
                                     "Armenian Dram Exchange Rate", "x" * 21, "", None, "AI"])
        assert got == ["Gusan", "claire", "dubois", "telva", "Davit", "Armenian", "Dram", "Exchange", "Rate", "AI"]
        assert all(len(k) <= eleven.KEYTERM_MAX_CHARS for k in got)
        assert len(eleven.clean_keyterms(["term%d" % i for i in range(200)])) == eleven.KEYTERMS_MAX
        s = eleven.ScribeRealtime("t", "fr", lambda k, t: None, keyterms=["james.reed@harbourline.co.uk"])
        assert s.keyterms == ["james", "reed", "harbourline"]
        assert "harbourline.co.uk" not in s.url("t")

    def test_refused_keyterms_reopen_the_session_without_them(self):
        """If Scribe still refuses a keyterm, the session comes back at once without the Dictionary
        words - never a dead interpreter, and nothing said to the person."""
        first, second = FakeWS(), FakeWS()
        socks = [first, second]
        urls, events = [], []
        s = eleven.ScribeRealtime("t1", "en", lambda k, t: events.append((k, t)), keyterms=["Gusan"],
                                  token_provider=lambda: "t2", connect=lambda u: (urls.append(u), socks.pop(0))[1])
        s.start()
        try:
            first.incoming.put(json.dumps({"message_type": "invalid_request",
                                           "error": "Each keyterm must be at most 20 characters."}))
            assert wait_until(lambda: len(urls) == 2, timeout=3)
            second.incoming.put(json.dumps({"message_type": "committed_transcript", "text": "back"}))
            assert wait_until(lambda: ("committed", "back") in events)
        finally:
            s.close()
        assert "keyterms=Gusan" in urls[0] and "keyterms" not in urls[1] and "token=t2" in urls[1]
        assert not [e for e in events if e[0] in ("error", "closed")]

    def test_another_invalid_request_is_not_tried_again(self):
        ws = FakeWS()
        urls, events = [], []
        s = eleven.ScribeRealtime("t1", "en", lambda k, t: events.append((k, t)),
                                  token_provider=lambda: "t2", connect=lambda u: (urls.append(u), ws)[1])
        s.start()
        try:
            ws.incoming.put(json.dumps({"message_type": "invalid_request", "error": "bad audio_format"}))
            assert wait_until(lambda: ("closed", "invalid_request") in events, timeout=3)
        finally:
            s.close()
        assert len(urls) == 1 and events[0][0] == "error"

    def test_drop_reconnects_with_a_fresh_token(self):
        first, second = FakeWS(), FakeWS()
        socks = [first, second]
        urls = []
        events = []
        s = eleven.ScribeRealtime("t1", "en", lambda k, t: events.append((k, t)),
                                  token_provider=lambda: "t2", connect=lambda u: (urls.append(u), socks.pop(0))[1])
        s.start()
        try:
            first.incoming.put(None)                 # server closed
            assert wait_until(lambda: len(urls) == 2, timeout=3)
            second.incoming.put(json.dumps({"message_type": "committed_transcript", "text": "back"}))
            assert wait_until(lambda: ("committed", "back") in events)
        finally:
            s.close()
        assert "token=t2" in urls[1]
        assert events[0] == ("dropped", "")            # the pipeline sends what was heard, starts afresh

    def test_a_long_drop_is_tried_until_the_network_is_back(self):
        first, second = FakeWS(), FakeWS()
        socks = [first, second]
        events = []
        tokens = FlakyTokens(fails=5)                # offline for five tries (more than ~2 s)
        s = eleven.ScribeRealtime("t0", "en", lambda k, t: events.append((k, t)), token_provider=tokens,
                                  connect=lambda u: socks.pop(0), retry_s=0.01, retry_max_s=0.05)
        s.start()
        try:
            first.incoming.put(None)
            assert wait_until(lambda: tokens.calls == 6, timeout=3)
            second.incoming.put(json.dumps({"message_type": "committed_transcript", "text": "back"}))
            assert wait_until(lambda: ("committed", "back") in events)
        finally:
            s.close()
        assert not any(k == "closed" for k, _t in events)

    def test_a_connection_refused_each_time_is_given_up(self):
        made = []

        def connect(u):
            # accepted, then refused and closed: words never come back
            made.append(FakeWS([json.dumps({"message_type": "session_started"}),
                                json.dumps({"message_type": "rate_limited", "error": "busy"}), None]))
            return made[-1]
        events = []
        s = eleven.ScribeRealtime("t0", "en", lambda k, t: events.append((k, t)), token_provider=lambda: "t",
                                  connect=connect, retry_s=0.01, retry_max_s=0.02, retry_window_s=0.3)
        s.start()
        try:
            assert wait_until(lambda: any(k == "closed" for k, _t in events), timeout=3)
        finally:
            s.close()
        assert len(made) < 40

    def test_no_credit_is_not_tried_again(self):
        ws = FakeWS([json.dumps({"message_type": "quota_exceeded", "error": "no credit"})])
        tokens = FlakyTokens(fails=0)
        events = []
        s = eleven.ScribeRealtime("t0", "en", lambda k, t: events.append((k, t)), token_provider=tokens,
                                  connect=lambda u: ws)
        s.start()
        try:
            assert wait_until(lambda: ("closed", "quota_exceeded") in events)
        finally:
            s.close()
        assert tokens.calls == 0 and ws.closed

    def test_an_ended_session_is_not_tried_again(self):
        first = FakeWS()
        tokens = FlakyTokens(fails=99, code="allowance_used")
        events = []
        s = eleven.ScribeRealtime("t0", "en", lambda k, t: events.append((k, t)), token_provider=tokens,
                                  connect=lambda u: first, retry_s=0.01)
        s.start()
        try:
            first.incoming.put(None)
            assert wait_until(lambda: ("closed", "allowance_used") in events)
        finally:
            s.close()
        assert tokens.calls == 1

    def test_close_while_waiting_to_reconnect_opens_nothing(self):
        made = []

        def connect(u):
            made.append(FakeWS())
            return made[-1]
        tokens = FlakyTokens(fails=0)
        s = eleven.ScribeRealtime("t0", "en", lambda k, t: None, token_provider=tokens, connect=connect)
        s.start()
        made[0].incoming.put(None)                   # dropped: the reader waits 0.3 s first
        time.sleep(0.1)
        s.close()
        time.sleep(0.6)
        assert len(made) == 1 and tokens.calls == 0

    def test_close_while_connecting_closes_the_new_socket(self):
        entered, gate, made = threading.Event(), threading.Event(), []

        def connect(u):
            if made:
                entered.set()
                gate.wait(2)                         # the reconnect is still on its way
            made.append(FakeWS())
            return made[-1]
        s = eleven.ScribeRealtime("t0", "en", lambda k, t: None, token_provider=lambda: "t", connect=connect)
        s.start()
        made[0].incoming.put(None)
        assert entered.wait(2)
        s.close()
        gate.set()
        assert wait_until(lambda: len(made) == 2)
        assert wait_until(lambda: made[1].closed)

    def test_speech_to_text_notices_a_dead_link_soon(self, monkeypatch):
        import websockets.sync.client as wsc
        seen = {}
        monkeypatch.setattr(wsc, "connect", lambda url, **kw: seen.update(kw) or "ws")
        assert eleven.stt_connect("wss://example.invalid") == "ws"
        assert seen["ping_interval"] == 5 and seen["ping_timeout"] == 5


class TestDialogueVoice:
    def audio_msgs(self, n=2):
        pcm = base64.b64encode(b"\x01\x00" * 800).decode()
        return [json.dumps({"audio": pcm})] * n + [json.dumps({"is_final_audio_for_turn": True})]

    def test_open_warms_up_and_speaks_turns(self):
        ws = FakeWS(self.audio_msgs(1) + self.audio_msgs(2))
        urls = []
        v = eleven.DialogueVoice(lambda: "ttd1", "voiceA", "ja", connect=lambda u: (urls.append(u), ws)[1])
        v.open()
        got = []
        secs = v.speak("こんにちは", got.append)
        v.close()
        assert "single_use_token=ttd1" in urls[0] and "model_id=eleven_v4_turbo" in urls[0]
        assert "output_format=pcm_16000" in urls[0] and "language_code=ja" in urls[0]
        assert ws.sent[0] == {"voices": ["voiceA"]}
        assert ws.sent[1]["inputs"][0]["text"] == "Okay."             # warm-up turn
        assert ws.sent[3] == {"inputs": [{"text": "こんにちは", "voice_id": "voiceA", "new_turn": True}]}
        assert ws.sent[4] == {"flush": True}
        assert len(got) == 2 and secs == pytest.approx(0.1)
        assert ws.sent[-1] == {"close_socket": True}

    def test_two_voices_on_one_socket(self):
        ws = FakeWS(self.audio_msgs(1) + self.audio_msgs(1))
        socks = [ws]
        v = eleven.DialogueVoice(lambda: "t", ["woman", "man"], "en", connect=lambda u: socks.pop(0), warm_text="")
        v.open()
        assert ws.sent[0] == {"voices": ["woman", "man"]}
        v.speak("hi", lambda pcm: None, voice_id="man")
        assert ws.sent[1]["inputs"][0]["voice_id"] == "man"
        v.speak("hi", lambda pcm: None)
        assert ws.sent[3]["inputs"][0]["voice_id"] == "woman"            # the first is the default
        v.close()

    def test_a_new_voice_reopens_the_socket(self):
        first = FakeWS()
        second = FakeWS(self.audio_msgs(1))
        socks = [first, second]
        v = eleven.DialogueVoice(lambda: "t", "woman", "en", connect=lambda u: socks.pop(0), warm_text="")
        v.open()
        v.set_voices(["alice", "brian"])
        v.speak("hi", lambda pcm: None, voice_id="brian")
        assert first.sent[-1] == {"close_socket": True}
        assert second.sent[0] == {"voices": ["alice", "brian"]}
        assert second.sent[1]["inputs"][0]["voice_id"] == "brian"
        v.close()

    def test_server_error_raises(self):
        ws = FakeWS([json.dumps({"message": "quota", "error": "quota_exceeded", "code": 1008})])
        v = eleven.DialogueVoice(lambda: "t", "v", "en", connect=lambda u: ws, warm_text="")
        v.open()
        with pytest.raises(eleven.VoiceError):
            v.speak("hello", lambda pcm: None)
        v.close()

    def test_timeout_raises(self):
        ws = FakeWS()
        v = eleven.DialogueVoice(lambda: "t", "v", "en", connect=lambda u: ws, warm_text="", turn_timeout=0.2)
        v.open()
        with pytest.raises(eleven.VoiceError):
            v.speak("hello", lambda pcm: None)
        v.close()

    def test_keepalive_when_idle(self):
        ws = FakeWS()
        v = eleven.DialogueVoice(lambda: "t", "v", "en", connect=lambda u: ws, warm_text="", keepalive_s=0.05)
        v.open()
        try:
            assert wait_until(lambda: {"keep_alive": True} in ws.sent, timeout=3)
        finally:
            v.close()

    def test_close_is_final(self):
        class LosingWS(FakeWS):
            def recv(self, timeout=None):
                m = super().recv(timeout)
                if m is None:                       # close() ended the socket
                    raise ConnectionError("closed")
                return m
        socks = []

        def connect(u):
            socks.append(LosingWS())
            return socks[-1]
        v = eleven.DialogueVoice(lambda: "t", "v", "en", connect=connect, warm_text="")
        v.open()
        errors = []

        def talk():
            try:
                v.speak("hello", lambda pcm: None)
            except eleven.VoiceError as e:
                errors.append(e)
        t = threading.Thread(target=talk)
        t.start()
        time.sleep(0.1)
        v.close()                                    # Stop, mid-phrase
        t.join(2)
        assert errors
        with pytest.raises(eleven.VoiceError):
            v.reopen()
        assert len(socks) == 1
        assert wait_until(lambda: not v._keepalive.is_alive(), timeout=3)

    def test_a_long_turn_that_keeps_talking_does_not_time_out(self):
        pcm = base64.b64encode(b"\x01\x00" * 800).decode()

        class SlowWS(FakeWS):
            n = 0

            def recv(self, timeout=None):
                if self.n < 6:
                    self.n += 1
                    time.sleep(0.08)                 # a piece of audio every 80 ms, ~0.5 s in all
                    return json.dumps({"audio": pcm})
                return json.dumps({"is_final_audio_for_turn": True})
        v = eleven.DialogueVoice(lambda: "t", "v", "en", connect=lambda u: SlowWS(), warm_text="",
                                 turn_timeout=0.2)
        v.open()
        got = []
        try:
            v.speak("a long merged turn", got.append)
        finally:
            v.close()
        assert len(got) == 6


# ── translator and session client with fake HTTP ─────────────────────────────────────────────
class FakeResp:
    def __init__(self, status=200, lines=(), ctype="text/event-stream", data=None, headers=None):
        self.status_code = status
        self._lines = [l.encode() for l in lines]
        self.headers = {"Content-Type": ctype, **(headers or {})}
        self._data = data
        self.encoding = None

    def iter_lines(self):
        return iter(self._lines)

    def json(self):
        if self._data is None:
            raise ValueError
        return self._data

    def close(self):
        pass


class FakeHTTP:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def post(self, url, json=None, headers=None, **kw):
        self.calls.append((url, json, headers))
        return self.resp


def sse(*pieces):
    return ["data: " + json.dumps({"choices": [{"delta": {"content": p}}]}) for p in pieces] + ["data: [DONE]"]


class TestTranslate:
    def test_streams_and_sends_the_contract_fields(self):
        http = FakeHTTP(FakeResp(lines=sse("Tomorrow's ", "meeting.")))
        got = []
        out = translate.translate_phrase("明日の会議。", "ja", "en", ["前の文"], got.append,
                                         token="jwt", glossary=["Gusan"], session=http)
        assert out == "Tomorrow's meeting." and got == ["Tomorrow's ", "meeting."]
        _url, body, headers = http.calls[0]
        assert headers["Authorization"] == "Bearer jwt"
        assert body["mode"] == "interpret" and body["stream"] is True
        assert body["source_lang"] == "ja" and body["target_lang"] == "en"
        assert body["context"] == ["前の文"] and body["glossary"] == ["Gusan"]
        assert "Japanese" in body["messages"][0]["content"] and "English" in body["messages"][0]["content"]
        assert "Gusan" in body["messages"][0]["content"]

    def test_quotes_are_removed(self):
        http = FakeHTTP(FakeResp(lines=sse('"Hello."')))
        assert translate.translate_phrase("x", "fr", "en", [], lambda d: None, token="j", session=http) == "Hello."

    def test_plain_json_answer(self):
        http = FakeHTTP(FakeResp(ctype="application/json", data={"text": "Hi"}))
        assert translate.translate_phrase("x", "fr", "en", [], lambda d: None, token="j", session=http) == "Hi"

    @pytest.mark.parametrize("status,code", [(401, "auth"), (402, "pro_required"), (500, "server")])
    def test_errors(self, status, code):
        http = FakeHTTP(FakeResp(status=status, data={}))
        with pytest.raises(translate.TranslateError) as e:
            translate.translate_phrase("x", "fr", "en", [], lambda d: None, token="j", session=http)
        assert e.value.code == code

    def test_no_token(self):
        with pytest.raises(translate.TranslateError):
            translate.translate_phrase("x", "fr", "en", [], lambda d: None, token="")

    def test_leaked_reasoning_is_dropped_not_spoken(self):
        # a self-hosted DeepSeek whose "thinking off" did not take: "{reasoning}</think>{answer}"
        http = FakeHTTP(FakeResp(lines=sse("The user wants an Armenian translation", "</think>", "Բարև։"),
                                 headers={"X-Answer-Family": "deepseek"}))
        got = []
        out = translate.translate_phrase("Hi", "en", "hy", [], got.append, token="j", session=http)
        assert out == "Բարև։"
        assert any(isinstance(d, action_api.ReplaceText) for d in got)      # the caption starts over

    @pytest.mark.parametrize("tail", [
        [],                                                                   # ended with no [DONE]
        ['data: {"error": "upstream failed"}', "data: [DONE]"],             # an error after words
        ["data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "length"}]}), "data: [DONE]"],
    ])
    def test_an_answer_cut_short_is_never_returned_as_whole(self, tail):
        http = FakeHTTP(FakeResp(lines=sse("We can ", "sign")[:-1] + tail))
        with pytest.raises(translate.TranslateError) as e:
            translate.translate_phrase("x", "fr", "en", [], lambda d: None, token="j", session=http)
        assert e.value.code == "cut" and e.value.partial == "We can sign"

    def test_a_connection_dropped_mid_answer_is_cut(self):
        exc = getattr(translate.requests, "RequestException", None)
        if not (isinstance(exc, type) and issubclass(exc, BaseException)):
            pytest.skip("real requests needed (suite-wide stub in test_core)")

        class Dropping(FakeResp):
            def iter_lines(self):
                yield from self._lines[:2]
                raise requests.exceptions.ChunkedEncodingError("connection broken")
        http = FakeHTTP(Dropping(lines=sse("We can ", "sign", " only if the price drops")))
        with pytest.raises(translate.TranslateError) as e:
            translate.translate_phrase("x", "fr", "en", [], lambda d: None, token="j", session=http)
        assert e.value.code == "cut" and e.value.partial == "We can sign"

    def test_a_finish_reason_is_a_whole_answer_even_without_done(self):
        lines = sse("Hello.")[:-1] + ["data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})]
        http = FakeHTTP(FakeResp(lines=lines))
        assert translate.translate_phrase("x", "fr", "en", [], lambda d: None, token="j", session=http) == "Hello."

    def test_phrases_share_one_kept_open_connection(self, monkeypatch):
        made = []

        class Session:
            def __init__(self):
                self.calls = 0
                made.append(self)

            def post(self, url, **kw):
                self.calls += 1
                return FakeResp(lines=sse("Hi"))

        def fresh_post(*_a, **_k):
            raise AssertionError("a new connection for every phrase")
        monkeypatch.setattr(translate.requests, "Session", Session)
        monkeypatch.setattr(translate.requests, "post", fresh_post)
        monkeypatch.setattr(translate, "_local", threading.local(), raising=False)
        for _ in range(3):
            assert translate.translate_phrase("x", "fr", "en", [], lambda d: None, token="j") == "Hi"
        assert len(made) == 1 and made[0].calls == 3

    def test_the_stream_is_read_to_its_end_so_the_connection_is_reused(self):
        class Resp(FakeResp):
            consumed = closed = False

            def iter_lines(self):
                yield from self._lines
                self.consumed = True                 # (requests gives the connection back now)

            def close(self):
                self.closed = True
        resp = Resp(lines=sse("Hi") + [""])
        assert translate.translate_phrase("x", "fr", "en", [], lambda d: None, token="j",
                                          session=FakeHTTP(resp)) == "Hi"
        assert wait_until(lambda: resp.consumed and resp.closed)


class TestSession:
    def test_start_heartbeat_stop(self):
        http = FakeHTTP(FakeResp(ctype="application/json", data={
            "session_id": "s1", "seconds_left": 1000, "scribe_tokens": {"hear": "a"}, "tts_tokens": {"hear": "b"},
            "voices": {"hear": "v"}}))
        s = session.InterpreterSession(lambda: "jwt", http=http)
        info = s.start(hear=("ja", "en"))
        assert info["scribe_tokens"]["hear"] == "a" and s.id == "s1"
        body = http.calls[0][1]
        assert body == {"action": "start", "hear": {"from": "ja", "to": "en"}}
        http.resp = FakeResp(ctype="application/json", data={"seconds_left": 970})
        assert s.heartbeat(30.4) == 970
        assert http.calls[-1][1] == {"action": "heartbeat", "session_id": "s1", "seconds": 30}
        http.resp = FakeResp(ctype="application/json", data={"token": "t9"})
        assert s.token("tts", "speak") == "t9"
        http.resp = FakeResp(ctype="application/json", data={"seconds_left": 960})
        assert s.stop(10) == 960

    @pytest.mark.parametrize("status,data,code", [
        (402, {"error": "pro_required"}, "pro_required"),
        (429, {"error": "allowance_used", "seconds_left": 0}, "allowance_used"),
        # the server's daily ceilings: never told as "you've used this month's time"
        (429, {"error": "quota_exceeded"}, "quota_exceeded"),
        (401, {}, "auth"),
        (503, {"error": "provider_credits"}, "provider_credits"),
        (500, {"error": "weird_new_code"}, "server"),
    ])
    def test_errors_have_calm_messages(self, status, data, code):
        http = FakeHTTP(FakeResp(status=status, ctype="application/json", data=data))
        s = session.InterpreterSession(lambda: "jwt", http=http)
        with pytest.raises(session.InterpreterError) as e:
            s.start(hear=("ja", "en"))
        assert e.value.code == code and str(e.value) == session.MESSAGES[code]

    def test_voice_store(self):
        pcm = base64.b64encode(b"\x00\x01" * 4).decode()
        http = FakeHTTP(FakeResp(ctype="application/json", data={"audio": pcm, "sample_rate": 16000}))
        vs = session.VoiceStore(lambda: "jwt", http=http)
        audio, sr = vs.preview("Hi", "fr")
        assert audio == b"\x00\x01" * 4 and sr == 16000
        http.resp = FakeResp(ctype="application/json", data={"voice_id": "v1", "created_at": "x"})
        assert vs.create(["AAA="], "recorded", "blue river seven")["voice_id"] == "v1"
        body = http.calls[-1][1]
        assert body["consent"] is True and body["consent_phrase"] == "blue river seven" and body["source"] == "recorded"
